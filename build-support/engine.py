"""Stage Wisp's bundled oMLX engine into Wisp.app/Contents/Resources/engine.

Opt-in (`wisp-build ... --engine-app <oMLX.app>`); builds without the flag are unchanged.

The engine is oMLX's own signed release, pruned to the text/embedding/rerank server:

    engine/py/        CPython 3.11 (oMLX's interpreter: py/bin/python3 -> python3.11, no re-exec)
    engine/sp/        site-packages minus build-support/engine-prune.json
    engine/omlx/      the oMLX server package
    engine/omlx_web/  admin UI (oMLX 0.7.1 and later; 0.7.0 keeps it in omlx/admin)
    engine/ENGINE.json  provenance and the engine's own minimum macOS

Design: docs/plans/2026-10-13-bundled-omlx-engine.md and ...-attribution-design.md.
Supply-chain pin: engine.lock.json holds a digest of the exact source files selected from
the oMLX release (tree digest, independent of archive format); a release build refuses a
source that does not match it. A local `--engine-app` without a lock entry builds a
preview that is never release eligible.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from email.parser import Parser
from pathlib import Path

import engine_guard_overlay as guard_overlay

SUPPORT = Path(__file__).resolve().parent
PRUNE = SUPPORT / "engine-prune.json"
LOCK = SUPPORT / "engine.lock.json"
SOURCE = SUPPORT / "engine-source.json"

OMLX_TEAM_ID = "PSK5Q5T46L"
OMLX_BUNDLE_ID = "app.omlx"
ENGINE_SIGNING_ID = "com.wisp.engine.python3"   # must equal service.inference.bundled_peer.ENGINE_SIGNING_ID
ENGINE_DIR = "Contents/Resources/engine"
ENGINE_PYTHON = f"{ENGINE_DIR}/py/bin/python3.11"   # the real executable; py/bin/python3 links to it
ENGINE_ENTITLEMENTS = "build-support/engine.entitlements"
SITE_PACKAGES = "Python/framework-mlx-base/lib/python3.11/site-packages"
INTERPRETER = "Python/cpython-3.11"
_MACH_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"}


class EngineBuildError(RuntimeError):
    pass


# ---------------------------------------------------------------- source ----------------
def load_prune(path: Path = PRUNE) -> dict:
    doc = json.loads(path.read_text())
    if doc.get("schema") != 1 or not isinstance(doc.get("drop"), dict):
        raise EngineBuildError("engine-prune.json is malformed")
    for name in doc["drop"]:
        if not re.fullmatch(r"[A-Za-z0-9_.]+", name):
            raise EngineBuildError(f"Unsafe prune entry: {name!r}")
    return doc


def load_lock(path: Path = LOCK) -> dict | None:
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    if (doc.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{64}", str(doc.get("source_tree_sha256", "")))
            or not isinstance(doc.get("omlx_version"), str) or not doc["omlx_version"]
            or not re.fullmatch(r"\d+(\.\d+){1,2}", str(doc.get("minimum_macos", "")))):
        raise EngineBuildError("engine.lock.json is malformed")
    return doc


def _codesign_info(app: Path) -> str:
    result = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
                            capture_output=True, text=True, timeout=120)
    return result.stdout + result.stderr


def verify_source_identity(app: Path, *, check_signature: bool = True) -> dict:
    """The source must be the genuine, signed oMLX app. Returns its Info.plist facts."""
    app = Path(app).resolve(strict=True)
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException):
        raise EngineBuildError("Not an app bundle: no readable Info.plist") from None
    if info.get("CFBundleIdentifier") != OMLX_BUNDLE_ID:
        raise EngineBuildError(f"Source is not oMLX (bundle id {info.get('CFBundleIdentifier')!r})")
    if check_signature:
        text = _codesign_info(app)
        if f"TeamIdentifier={OMLX_TEAM_ID}" not in text:
            raise EngineBuildError("Source is not signed by the oMLX Team ID")
    return {"version": str(info.get("CFBundleShortVersionString", "")),
            "build": str(info.get("CFBundleVersion", ""))}


def _resources(app: Path) -> Path:
    return Path(app) / "Contents/Resources"


# ------------------------------------------------------------- selection ----------------
def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in _MACH_MAGICS
    except OSError:
        return False


def _top_entry_dropped(name: str, dropped: set[str]) -> bool:
    """Is this top-level site-packages entry (package dir, module file or dist-info) pruned?"""
    if name.endswith(".dist-info"):
        return name.rsplit("-", 1)[0].split("-")[0] in dropped
    return name.split(".")[0] in dropped


def _unwanted(path: Path, prune: dict) -> bool:
    return (path.name in prune["always_remove"]
            or path.suffix in prune["always_remove_suffixes"])


def _admin_ui_location(resources: Path) -> str:
    """Where this oMLX release keeps its admin UI: a separate omlx_web package (0.7.1 and later) or
    omlx/admin/templates inside the server package (0.7.0). A release with neither is refused."""
    if (resources / "omlx_web").is_dir():
        return "omlx_web"
    if (resources / "omlx" / "admin" / "templates").is_dir():
        return "omlx/admin"
    raise EngineBuildError("oMLX source is missing omlx_web")


def _tree_layout(resources: Path) -> list[tuple[str, Path, bool]]:
    layout = [("py", resources / INTERPRETER, False), ("sp", resources / SITE_PACKAGES, True),
              ("omlx", resources / "omlx", False)]
    if _admin_ui_location(resources) == "omlx_web":
        layout.append(("omlx_web", resources / "omlx_web", False))
    return layout


def select_files(app: Path, prune: dict | None = None) -> list[tuple[str, Path]]:
    """Every (destination relative to engine/, source file) pair, sorted, no symlinks followed
    out of the source. Raises if a symlink escapes or a required piece is missing."""
    prune = prune or load_prune()
    resources = _resources(app).resolve(strict=True)
    dropped = set(prune["drop"])
    layout = _tree_layout(resources)
    pairs: list[tuple[str, Path]] = []
    for dest, root, is_site in layout:
        if not root.is_dir():
            raise EngineBuildError(f"oMLX source is missing {root.relative_to(resources)}")
        for current, directories, files in os.walk(root):
            here = Path(current)
            directories[:] = sorted(d for d in directories if not _unwanted(here / d, prune))
            if is_site and here == root:
                directories[:] = [d for d in directories if not _top_entry_dropped(d, dropped)]
                files = [f for f in files if not _top_entry_dropped(f, dropped)]
            for name in sorted(files):
                source = here / name
                if _unwanted(source, prune):
                    continue
                if source.is_symlink():
                    target = source.resolve(strict=True)
                    if not target.is_relative_to(root):
                        raise EngineBuildError(f"Symlink leaves the engine tree: {source}")
                    continue                         # re-created by link_entries()
                pairs.append((f"{dest}/{source.relative_to(root)}", source))
    if not any(d == "py/bin/python3.11" for d, _ in pairs):
        raise EngineBuildError("oMLX interpreter py/bin/python3.11 not found")
    return sorted(pairs)


def link_entries(app: Path, prune: dict | None = None) -> list[tuple[str, str]]:
    """Relative symlinks inside the engine tree: (destination, link target)."""
    prune = prune or load_prune()
    resources = _resources(app).resolve(strict=True)
    dropped = set(prune["drop"])
    links = []
    for dest, root, _ in _tree_layout(resources):
        for current, directories, files in os.walk(root):
            here = Path(current)
            directories[:] = [d for d in directories if not _unwanted(here / d, prune)]
            for name in sorted([*directories, *files]):
                path = here / name
                if not path.is_symlink() or _unwanted(path, prune):
                    continue
                relative = path.relative_to(root)
                if dest == "sp" and _top_entry_dropped(relative.parts[0], dropped):
                    continue
                target = os.readlink(path)
                if os.path.isabs(target) or not path.resolve(strict=True).is_relative_to(root):
                    raise EngineBuildError(f"Non-relocatable symlink in the engine: {path}")
                links.append((f"{dest}/{relative}", target))
    return sorted(links)


def tree_digest(pairs: list[tuple[str, Path]], links: list[tuple[str, str]]) -> str:
    """Canonical digest of exactly what will be staged (names, modes, bytes, links)."""
    digest = hashlib.sha256()
    for destination, source in pairs:
        digest.update(b"F\0" + destination.encode() + b"\0")
        digest.update(f"{stat.S_IMODE(source.stat().st_mode):o}".encode() + b"\0")
        file_hash = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                file_hash.update(chunk)
        digest.update(file_hash.digest())
    for destination, target in links:
        digest.update(b"L\0" + destination.encode() + b"\0" + target.encode() + b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------- staging ---------------
def _otool_minos(path: Path) -> list[str]:
    text = subprocess.run(["/usr/bin/otool", "-arch", "arm64", "-l", str(path)],
                          capture_output=True, text=True, timeout=120).stdout
    return re.findall(r"\bminos (\d+(?:\.\d+)+)", text)


def minimum_macos(pairs: list[tuple[str, Path]], minos_reader=None) -> str:
    """The highest macOS deployment target of any staged Mach-O, from its load commands."""
    best = (0, 0, 0)
    for _, source in pairs:
        if not _is_macho(source):
            continue
        for version in (minos_reader or _otool_minos)(source):
            parts = tuple((list(map(int, version.split("."))) + [0, 0, 0])[:3])
            best = max(best, parts)
    if best == (0, 0, 0):
        raise EngineBuildError("Could not read any macOS deployment target from the engine")
    return ".".join(map(str, best[:2] if best[2] == 0 else best))


def create_lock(app: Path, output: Path = LOCK, *, force: bool = False,
                check_signature: bool = True, minos_reader=None) -> dict:
    """Pin the selected, pruned source tree and write a load_lock-compatible file."""
    facts = verify_source_identity(app, check_signature=check_signature)
    if not facts["version"]:
        raise EngineBuildError("oMLX source has no version")
    prune = load_prune()
    pairs = select_files(app, prune)
    links = link_entries(app, prune)
    document = {"schema": 1, "omlx_version": facts["version"],
                "source_tree_sha256": tree_digest(pairs, links),
                "minimum_macos": minimum_macos(pairs, minos_reader)}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if force:
        output.write_text(serialized)
    else:
        try:
            with output.open("x") as handle:
                handle.write(serialized)
        except FileExistsError:
            raise EngineBuildError(f"Refusing to overwrite existing lock: {output} (use --force)") from None
    return document


def _license_file(app: Path, selected: list[tuple[str, Path]]) -> str | None:
    candidates = [destination for destination, _ in selected
                  if Path(destination).name.lower().startswith(("license", "copying"))
                  and (destination.startswith("omlx/")
                       or (len(Path(destination).parts) > 2
                           and Path(destination).parts[0] == "sp"
                           and Path(destination).parts[1].lower().startswith("omlx-")
                           and Path(destination).parts[1].endswith(".dist-info")))]
    return min(candidates, key=lambda value: (value.count("/"), value.casefold())) if candidates else None


def third_party_inventory(app: Path, *, check_signature: bool = True) -> list[dict]:
    """List kept distribution licences and oMLX's bundled licence file, if present."""
    facts = verify_source_identity(app, check_signature=check_signature)
    prune = load_prune()
    selected = select_files(app, prune)
    resources = _resources(Path(app).resolve(strict=True))
    site_packages = resources / SITE_PACKAGES
    dist_info_names = {Path(destination).parts[1]
                       for destination, _ in selected
                       if len(Path(destination).parts) > 2
                       and Path(destination).parts[0] == "sp"
                       and Path(destination).parts[1].endswith(".dist-info")}
    selected_metadata = {source for destination, source in selected
                         if len(Path(destination).parts) == 3
                         and Path(destination).parts[0] == "sp"
                         and Path(destination).parts[1].endswith(".dist-info")
                         and Path(destination).name == "METADATA"}
    inventory: list[dict] = []
    for dist_info_name in sorted(dist_info_names, key=str.casefold):
        dist_info = site_packages / dist_info_name
        if dist_info.is_symlink() or not dist_info.is_dir():
            continue
        metadata_path = dist_info / "METADATA"
        if metadata_path in selected_metadata:
            metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8", errors="replace"))
        else:
            metadata = Parser().parsestr("")
        name = (metadata.get("Name") or dist_info.name[:-len(".dist-info")].rsplit("-", 1)[0]).strip()
        version = (metadata.get("Version") or dist_info.name[:-len(".dist-info")].rsplit("-", 1)[-1]).strip()
        licence = (metadata.get("License-Expression") or metadata.get("License") or "").strip()
        if not licence:
            classifiers = sorted({value[len("License :: "):].strip()
                                  for value in metadata.get_all("Classifier", [])
                                  if value.startswith("License :: ") and value[len("License :: "):].strip()},
                                 key=str.casefold)
            licence = "; ".join(classifiers)
        inventory.append({"name": name, "version": version, "license": licence})

    own_license = _license_file(Path(app), selected)
    if own_license:
        inventory.append({"name": "oMLX", "version": facts["version"], "license_file": own_license})
    return sorted(inventory, key=lambda item: (item["name"].casefold(), item["version"].casefold()))


# ---------------------------------------------------------------- fetching the pinned release --
# A numeric release with an optional PEP 440 pre-release suffix: 0.7.0, 0.7.3rc1, 0.7.3.dev2.
_RELEASE_VERSION = r"\d+(\.\d+){1,3}((a|b|rc)\d+|\.dev\d+)?"


def load_source(path: Path = SOURCE, lock: dict | None = None) -> dict:
    """The one oMLX release asset the engine may be built from, pinned by size and SHA-256.

    The asset digest authenticates the download; engine.lock.json separately pins the tree that
    is staged from it, so a release build needs both to match."""
    doc = json.loads(path.read_text())
    ok = (isinstance(doc, dict) and doc.get("schema") == 1
          and re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", str(doc.get("repo", "")))
          and re.fullmatch(r"v" + _RELEASE_VERSION, str(doc.get("tag", "")))
          and re.fullmatch(_RELEASE_VERSION, str(doc.get("omlx_version", "")))
          and re.fullmatch(r"[A-Za-z0-9_.-]+\.dmg", str(doc.get("asset", "")))
          and isinstance(doc.get("size"), int) and 0 < doc["size"] <= 2 * 1024 ** 3
          and re.fullmatch(r"[0-9a-f]{64}", str(doc.get("sha256", ""))))
    if not ok:
        raise EngineBuildError("engine-source.json is malformed")
    if doc["tag"] != f"v{doc['omlx_version']}" or doc["omlx_version"] not in doc["asset"]:
        raise EngineBuildError("engine-source.json tag, version and asset disagree")
    lock = lock if lock is not None else load_lock()
    if lock is not None and lock["omlx_version"] != doc["omlx_version"]:
        raise EngineBuildError("engine-source.json and engine.lock.json name different oMLX versions")
    return doc


def asset_url(source: dict) -> str:
    return f"https://github.com/{source['repo']}/releases/download/{source['tag']}/{source['asset']}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _curl(url: str, destination: Path, limit: int) -> None:
    # macOS curl uses the system trust store; TLS validation is never disabled and redirects
    # may only stay on https (GitHub release downloads redirect to its asset CDN).
    result = subprocess.run(
        ["/usr/bin/curl", "-q", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
         "--proto-redir", "=https", "--retry", "3", "--max-time", "1800", "--max-filesize", str(limit),
         "--output", str(destination), url],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=1900)
    if result.returncode:
        raise EngineBuildError("oMLX release download failed: " + result.stderr.decode(errors="replace")[-300:])


def _attach(dmg: Path, mountpoint: Path) -> None:
    result = subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                             "-mountpoint", str(mountpoint), str(dmg)], capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise EngineBuildError("Could not mount the oMLX disk image: " + result.stderr[-300:])


def _detach(mountpoint: Path) -> None:
    for force in (False, True):
        result = subprocess.run(["/usr/bin/hdiutil", "detach", *(["-force"] if force else []), str(mountpoint)],
                                capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return


def verified_download(source: dict, cache: Path, *, offline: bool = False,
                      fetch: Callable[[str, Path, int], None] | None = None) -> Path:
    """The pinned DMG in `cache`, downloaded if absent, with size and SHA-256 checked either way."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / source["asset"]

    def good(path: Path) -> bool:
        return path.is_file() and path.stat().st_size == source["size"] and _sha256_file(path) == source["sha256"]

    if target.exists() and not good(target):
        target.unlink()                       # a partial or tampered cache entry is never reused
    if not target.exists():
        if offline:
            raise EngineBuildError("The pinned oMLX release is not cached and this is an offline build")
        part = cache / (source["asset"] + ".part")
        part.unlink(missing_ok=True)
        (fetch or _curl)(asset_url(source), part, source["size"] + 1024 * 1024)
        if not good(part):
            part.unlink(missing_ok=True)
            raise EngineBuildError("The downloaded oMLX release does not match engine-source.json (size or SHA-256)")
        part.replace(target)
    return target


@contextlib.contextmanager
def fetched_app(cache: Path, *, offline: bool = False, source: dict | None = None,
                fetch: Callable[[str, Path, int], None] | None = None,
                attach: Callable[[Path, Path], None] | None = None,
                detach: Callable[[Path], None] | None = None):
    """Mount the digest-checked oMLX release read-only and yield its oMLX.app, always unmounting.

    Nothing from the image is executed: the build reads files from it and verifies the app's
    Developer ID signature and team (verify_source_identity) before staging."""
    source = source or load_source()
    dmg = verified_download(source, cache, offline=offline, fetch=fetch)
    mountpoint = Path(tempfile.mkdtemp(prefix="omlx-mount-", dir=cache))
    attached = False
    try:
        (attach or _attach)(dmg, mountpoint)
        attached = True
        app = mountpoint / "oMLX.app"
        if not app.is_dir():
            raise EngineBuildError("The oMLX disk image does not contain oMLX.app")
        yield app
    finally:
        if attached:
            (detach or _detach)(mountpoint)
        with contextlib.suppress(OSError):
            mountpoint.rmdir()


# ------------------------------------------------------------ the import smoke on this host --
def _host_macos() -> str:
    try:
        return subprocess.run(["/usr/bin/sw_vers", "-productVersion"], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def smoke_plan(bundle: Path, host_version: str | None = None) -> dict:
    """Whether the engine's import smoke can run on this build host.

    MLX cannot be imported below the engine's own minimum macOS, so a host older than that
    (the hosted macos-15 CI runners) records a skip with its reason instead of failing the build:
    the engine tree is still pinned by digest and verified byte for byte. Any other host (including
    one that meets the minimum but has no usable GPU, or whose version cannot be read) runs the
    smoke and fails the build if it fails."""
    document = read_engine_json(Path(bundle)) or {}
    needed = str(document.get("minimum_macos", ""))
    host = host_version if host_version is not None else _host_macos()
    if (re.fullmatch(r"\d+(\.\d+){0,2}", needed) and re.fullmatch(r"\d+(\.\d+){0,2}", host or "")
            and _numeric(host) < _numeric(needed)):
        return {"mode": "skip", "host_macos": host, "engine_minimum_macos": needed,
                "reason": f"host macOS {host} is older than the engine's minimum {needed}"}
    return {"mode": "run", "host_macos": host or "unknown", "engine_minimum_macos": needed}


def _numeric(version: str) -> tuple[int, ...]:
    return tuple((list(map(int, version.split("."))) + [0, 0, 0])[:3])


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Inspect and pin the bundled oMLX engine source.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    lock_parser = subparsers.add_parser("lock", help="create a source tree lock")
    lock_parser.add_argument("--app", type=Path, required=True, help="signed oMLX.app source")
    lock_parser.add_argument("--output", type=Path, default=LOCK, help=f"lock output (default: {LOCK})")
    lock_parser.add_argument("--force", action="store_true", help="replace an existing lock")
    inventory_parser = subparsers.add_parser("inventory", help="list kept package licences")
    inventory_parser.add_argument("--app", type=Path, required=True, help="signed oMLX.app source")
    args = parser.parse_args()
    try:
        if args.command == "lock":
            document = create_lock(args.app, args.output, force=args.force)
            print(f"oMLX {document['omlx_version']} tree {document['source_tree_sha256']}")
        else:
            print(json.dumps(third_party_inventory(args.app), indent=2, sort_keys=True))
    except (EngineBuildError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"engine.py: error: {exc}\n")
    return 0


def stage(bundle: Path, app: Path, *, lock: dict | None, release: bool,
          prune: dict | None = None, check_signature: bool = True, minos_reader=None) -> dict:
    """Copy the pruned engine into the bundle and write ENGINE.json. Returns its content."""
    prune = prune or load_prune()
    facts = verify_source_identity(app, check_signature=check_signature)
    pairs = select_files(app, prune)
    links = link_entries(app, prune)
    digest = tree_digest(pairs, links)
    if lock is not None:
        if digest != lock["source_tree_sha256"] or facts["version"] != lock["omlx_version"]:
            raise EngineBuildError(
                "oMLX source does not match engine.lock.json "
                f"(version {facts['version']!r}, tree {digest[:12]}…)")
    elif release:
        raise EngineBuildError("A release build needs engine.lock.json pinning the oMLX source")
    engine = Path(bundle) / ENGINE_DIR
    if engine.exists():
        raise EngineBuildError("Engine already staged")
    for destination, source in pairs:
        target = engine / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o755 if os.access(source, os.X_OK) else 0o644)
    for destination, link in links:
        target = engine / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link, target)
    try:
        overlay = guard_overlay.apply(engine, digest)
    except (guard_overlay.OverlayError, OSError, ValueError, SyntaxError) as exc:
        raise EngineBuildError("The pinned Wisp engine guard overlay could not be applied") from exc
    effective_pairs = sorted([(name, engine / name) for name, _ in pairs] +
                             [(guard_overlay.ADDITION, engine / guard_overlay.ADDITION)])
    effective_digest = tree_digest(effective_pairs, links)
    minimum = minimum_macos(pairs, minos_reader)
    if lock is not None and minimum != lock["minimum_macos"]:
        raise EngineBuildError(f"Engine minimum macOS {minimum} differs from the lock ({lock['minimum_macos']})")
    document = {"schema": 1, "omlx_version": facts["version"], "omlx_build": facts["build"],
                "source_tree_sha256": digest, "locked": lock is not None, "release_eligible": lock is not None,
                "minimum_macos": minimum, "signing_identifier": ENGINE_SIGNING_ID,
                "files": len(effective_pairs), "dropped": sorted(prune["drop"]),
                "guard_overlay": overlay, "effective_tree_sha256": effective_digest}
    (engine / "ENGINE.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.chmod(engine / "ENGINE.json", 0o644)
    return document


# ------------------------------------------------------------- validation ---------------
def read_engine_json(bundle: Path) -> dict | None:
    path = Path(bundle) / ENGINE_DIR / "ENGINE.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def validate_structure(bundle: Path, expected: dict | None = None) -> dict:
    """The staged engine has every piece the attribution check and the manager rely on."""
    bundle = Path(bundle)
    document = read_engine_json(bundle)
    if document is None:
        raise EngineBuildError("No ENGINE.json in the bundle")
    if document.get("signing_identifier") != ENGINE_SIGNING_ID:
        raise EngineBuildError("ENGINE.json signing identifier is wrong")
    if expected is not None and document != expected:
        raise EngineBuildError("ENGINE.json differs from what was staged")
    engine = bundle / ENGINE_DIR
    try:
        guard_overlay.verify(engine, document.get("guard_overlay"))
    except (guard_overlay.OverlayError, OSError, ValueError) as exc:
        raise EngineBuildError("The staged engine guard overlay is missing or changed") from exc
    for name in ("py/bin/python3", "py/bin/python3.11", "omlx/server.py", "omlx/cli.py",
                 "sp/mlx/core.cpython-311-darwin.so", "sp/mlx/lib/mlx.metallib"):
        if not (engine / name).exists():
            raise EngineBuildError(f"Engine is missing {name}")
    if not ((engine / "omlx_web").is_dir() or (engine / "omlx/admin/templates").is_dir()):
        raise EngineBuildError("Engine is missing omlx_web")
    python = engine / "py/bin/python3.11"
    if python.is_symlink() or not os.access(python, os.X_OK) or not _is_macho(python):
        raise EngineBuildError("Engine interpreter must be one real executable (attribution refuses a re-exec)")
    link = engine / "py/bin/python3"
    if not link.is_symlink() or os.readlink(link) != "python3.11":
        raise EngineBuildError("py/bin/python3 must be a relative link to python3.11")
    for directory in ("py", "sp", "omlx"):
        if not (engine / directory).is_dir() or (engine / directory).is_symlink():
            raise EngineBuildError(f"Engine {directory}/ must be a real directory")
    dropped = set(document.get("dropped", []))
    leaked = [name for name in dropped if (engine / "sp" / name).exists()]
    if leaked:
        raise EngineBuildError(f"Pruned packages are present: {leaked}")
    return document


def is_engine_target(bundle: Path, target: Path) -> bool:
    return (Path(bundle) / ENGINE_DIR) in Path(target).parents


def is_engine_python(bundle: Path, target: Path) -> bool:
    return Path(target) == Path(bundle) / ENGINE_PYTHON


def codesign_identifier_args(bundle: Path, target: Path) -> list[str]:
    """Extra codesign arguments: the engine interpreter carries the identifier the
    attribution check demands; everything else keeps codesign's default."""
    return ["-i", ENGINE_SIGNING_ID] if is_engine_python(bundle, target) else []


if __name__ == "__main__":
    raise SystemExit(_cli())
