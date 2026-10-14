"""Stage Wisp's bundled oMLX engine into Wisp.app/Contents/Resources/engine.

Opt-in (`wisp-build ... --engine-app <oMLX.app>`); builds without the flag are unchanged.

The engine is oMLX's own signed release, pruned to the text/embedding/rerank server:

    engine/py/        CPython 3.11 (oMLX's interpreter: py/bin/python3 -> python3.11, no re-exec)
    engine/sp/        site-packages minus build-support/engine-prune.json
    engine/omlx/      the oMLX server package
    engine/omlx_web/  admin UI
    engine/ENGINE.json  provenance and the engine's own minimum macOS

Design: docs/plans/2026-10-13-bundled-omlx-engine.md and ...-attribution-design.md.
Supply-chain pin: engine.lock.json holds a digest of the exact source files selected from
the oMLX release (tree digest, independent of archive format); a release build refuses a
source that does not match it. A local `--engine-app` without a lock entry builds a
preview that is never release eligible.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
from pathlib import Path

SUPPORT = Path(__file__).resolve().parent
PRUNE = SUPPORT / "engine-prune.json"
LOCK = SUPPORT / "engine.lock.json"

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


def select_files(app: Path, prune: dict | None = None) -> list[tuple[str, Path]]:
    """Every (destination relative to engine/, source file) pair, sorted, no symlinks followed
    out of the source. Raises if a symlink escapes or a required piece is missing."""
    prune = prune or load_prune()
    resources = _resources(app).resolve(strict=True)
    dropped = set(prune["drop"])
    layout = [("py", resources / INTERPRETER, False), ("sp", resources / SITE_PACKAGES, True),
              ("omlx", resources / "omlx", False), ("omlx_web", resources / "omlx_web", False)]
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
    for dest, root in (("py", resources / INTERPRETER), ("sp", resources / SITE_PACKAGES),
                       ("omlx", resources / "omlx"), ("omlx_web", resources / "omlx_web")):
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
    minimum = minimum_macos(pairs, minos_reader)
    if lock is not None and minimum != lock["minimum_macos"]:
        raise EngineBuildError(f"Engine minimum macOS {minimum} differs from the lock ({lock['minimum_macos']})")
    document = {"schema": 1, "omlx_version": facts["version"], "omlx_build": facts["build"],
                "source_tree_sha256": digest, "locked": lock is not None, "release_eligible": lock is not None,
                "minimum_macos": minimum, "signing_identifier": ENGINE_SIGNING_ID,
                "files": len(pairs), "dropped": sorted(prune["drop"])}
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
    for name in ("py/bin/python3", "py/bin/python3.11", "omlx/server.py", "omlx/cli.py", "omlx_web",
                 "sp/mlx/core.cpython-311-darwin.so", "sp/mlx/lib/mlx.metallib"):
        if not (engine / name).exists():
            raise EngineBuildError(f"Engine is missing {name}")
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
