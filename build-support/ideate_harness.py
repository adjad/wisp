"""Ideate's coding harness bundled in Wisp.app: a pinned, pruned Pi release and a Node binary.

Optional: `scripts/wisp-build preview --ideate-harness <dir>` stages it; without the flag the
app ships no harness and GET /ideate/status says Ideate's coding agent isn't included.

Layout in the bundle (service/ideate/harness.py looks here):

    Contents/Resources/ideate/pi/                 the Pi release (package.json, node_modules)
    Contents/Resources/ideate/node/bin/node       Node, thinned to arm64
    Contents/Resources/ideate/LICENSES/           Pi's MIT text, Node's licence
    Contents/Resources/ideate/HARNESS.json        what was staged, with digests

What is read from the source, and nothing else:
- `<dir>` is a Pi release folder (`~/.pi/agent/install/releases/<version>`), or the Pi install
  root (`~/.pi/agent`), from which only `install/current-version` is read to find that
  folder. Never auth.json, settings.json, sessions/ or models-store.json: they are not under
  the release folder, and nothing outside it is copied;
- the Node binary (`--ideate-node`, default the Pi installer's
  `~/.local/share/pi-node/current/bin/node`), its LICENSE when it sits beside the install, and
  `include/node/node_version.h` for its version. Nothing is executed.

Pruned: docs, examples, tests, changelogs, source maps, TypeScript declarations, and native
addons for other systems (Linux, Windows, Intel Mac). Pi's own docs stay (its prompt can
point at them). The pin (ideate-harness.lock.json) is the Pi version plus the SHA-256 of a
deterministic manifest of the pruned tree's non-Mach-O files, and the source Node's SHA-256;
Mach-O files are listed by path (stripping and signing change their bytes later; codesign
seals them). A release build refuses anything but the pinned harness; a preview records the
mismatch and goes on.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

SUPPORT = Path(__file__).resolve().parent
LOCK = SUPPORT / "ideate-harness.lock.json"
BUNDLE_DIR = "Contents/Resources/ideate"
PI_ENTRY = "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
NODE_PATH = "node/bin/node"
NODE_ENTITLEMENTS = "build-support/node.entitlements"
MIN_NODE = (22, 19, 0)
MACH_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe",
               b"\xbe\xba\xfe\xca"}
_UNWANTED = {".DS_Store", "__pycache__", "pyvenv.cfg", ".env", ".git", ".github", ".npmignore",
             ".eslintrc", ".prettierrc", ".editorconfig", ".travis.yml", ".npmrc", ".yarnrc", ".yarnrc.yml"}
_UNWANTED_SUFFIX = (".db", ".pyc", ".p12", ".p8", ".map", ".tsbuildinfo")
_PRUNE_DIRS = {"test", "tests", "__tests__", "example", "examples", "docs", "doc", "benchmark", "benchmarks",
               "coverage", ".bin-cache"}
_KEEP_DOCS = PurePosixPath("node_modules/@earendil-works/pi-coding-agent/docs")
_OTHER_PLATFORMS = re.compile(r"(?:^|[-_/])(?:linux|win32|windows|android|freebsd|openbsd|sunos|aix)(?:[-_/]|$)"
                              r"|darwin[-_]x64|darwin[-_]x86_64")


class HarnessError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in MACH_MAGICS


def load_lock(path: Path = LOCK) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HarnessError(f"Unreadable {path.name}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != 1 or doc.get("harness") != "pi":
        raise HarnessError(f"{path.name} is not a schema-1 Pi lock")
    return doc


# ------------------------------------------------------------------------------ source ----

def release_dir(source: Path) -> Path:
    """The Pi release folder for `source` (a release folder or the Pi install root)."""
    source = Path(source).expanduser()
    current = source / "install" / "current-version"
    if current.is_file():
        version = current.read_text().strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", version):
            raise HarnessError("Pi's install/current-version is not a version")
        source = source / "install" / "releases" / version
    if not (source / "package.json").is_file() or not (source / PI_ENTRY).is_file():
        raise HarnessError(f"{source} is not a Pi release folder (no {PI_ENTRY})")
    if source.is_symlink():
        raise HarnessError("The Pi release folder is a link")
    return source


def pi_version(release: Path) -> str:
    package = release / "node_modules/@earendil-works/pi-coding-agent/package.json"
    try:
        return str(json.loads(package.read_text())["version"])
    except (OSError, ValueError, KeyError) as exc:
        raise HarnessError("Pi's package.json has no version") from exc


def pi_author(release: Path) -> str:
    package = release / "node_modules/@earendil-works/pi-coding-agent/package.json"
    try:
        author = json.loads(package.read_text()).get("author") or ""
    except (OSError, ValueError):
        author = ""
    if isinstance(author, dict):
        author = author.get("name") or ""
    return re.sub(r"\s*[<(].*$", "", str(author)).strip() or "the Pi authors"


def default_node() -> Path | None:
    for candidate in (Path.home() / ".local/share/pi-node/current/bin/node",):
        if candidate.is_file():
            return candidate
    found = shutil.which("node")
    return Path(found) if found else None


def node_version(node: Path) -> str:
    header = Path(os.path.realpath(node)).parent.parent / "include" / "node" / "node_version.h"
    try:
        text = header.read_text()
    except OSError:
        return ""
    parts = [re.search(rf"#define NODE_{name}_VERSION (\d+)", text) for name in ("MAJOR", "MINOR", "PATCH")]
    return ".".join(m.group(1) for m in parts) if all(parts) else ""


def node_license(node: Path) -> Path | None:
    # A Node installed from the official .pkg ships no LICENSE beside it; the builder may
    # name Node's LICENSE text explicitly (WISP_IDEATE_NODE_LICENSE), and the build records it.
    named = os.environ.get("WISP_IDEATE_NODE_LICENSE")
    if named:
        path = Path(named).expanduser()
        if not path.is_file() or not path.read_text(errors="replace").startswith("Node.js is licensed"):
            raise HarnessError(f"WISP_IDEATE_NODE_LICENSE is not a Node.js LICENSE file: {named}")
        return path
    for candidate in (Path(os.path.realpath(node)).parent.parent / "LICENSE",
                      Path(os.path.realpath(node)).parent.parent / "share" / "doc" / "node" / "LICENSE"):
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------------------------- prune ----

def pruned(rel: PurePosixPath, is_dir: bool) -> bool:
    """True when `rel` (relative to the release folder) is left out of the bundle."""
    name = rel.name
    if name in _UNWANTED or name.endswith(_UNWANTED_SUFFIX) or name.startswith(".env"):
        return True
    if rel == _KEEP_DOCS or _KEEP_DOCS in rel.parents:
        return name == "images" and is_dir
    if is_dir and name in _PRUNE_DIRS and rel.parts[:1] == ("node_modules",):
        return True
    if is_dir and name == "@types":
        return True
    if not is_dir and (name.endswith((".d.ts", ".d.mts", ".d.cts")) or name.upper().startswith("CHANGELOG")):
        return True
    text = str(rel)
    if ("prebuilds" in rel.parts or "native" in rel.parts) and _OTHER_PLATFORMS.search(text):
        return True
    if not is_dir and name.endswith(".node") and _OTHER_PLATFORMS.search(text):
        return True
    return False


def _copy_tree(source: Path, target: Path) -> None:
    for top, dirs, names in os.walk(source, followlinks=False):
        top_path = Path(top)
        rel_top = PurePosixPath(top_path.relative_to(source).as_posix()) if top_path != source else PurePosixPath()
        (target / rel_top).mkdir(parents=True, exist_ok=True)
        kept = []
        for name in sorted(dirs):
            full = top_path / name
            rel = rel_top / name
            if full.is_symlink():
                names.append(name)          # handled as a link below
                continue
            if not pruned(rel, True):
                kept.append(name)
        dirs[:] = kept
        for name in sorted(set(names)):
            full = top_path / name
            rel = rel_top / name
            if pruned(rel, full.is_dir() and not full.is_symlink()):
                continue
            dest = target / rel
            if full.is_symlink():
                link = os.readlink(full)
                resolved = Path(os.path.realpath(full))
                if os.path.isabs(link) or not (resolved == source.resolve() or source.resolve() in resolved.parents):
                    raise HarnessError(f"Pi release has a link that leaves it: {rel}")
                if not resolved.exists():
                    continue                # a dangling link is dropped
                resolved_rel = PurePosixPath(resolved.relative_to(source.resolve()).as_posix())
                if pruned(resolved_rel, resolved.is_dir()):
                    continue
                os.symlink(link, dest)
            elif full.is_file():
                info = full.lstat()
                if info.st_mode & 0o6000:
                    raise HarnessError(f"setuid/setgid file in the Pi release: {rel}")
                shutil.copyfile(full, dest)
                dest.chmod(0o755 if info.st_mode & 0o111 else 0o644)
            else:
                raise HarnessError(f"Unsupported entry in the Pi release: {rel}")


def manifest(root: Path) -> tuple[str, int]:
    """(sha256, entries) over the tree: path, kind, content digest (non-Mach-O), link target,
    executable bit. Deterministic: sorted, no times, no owners."""
    rows = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append(f"{rel}\0link\0{os.readlink(path)}")
        elif path.is_dir():
            rows.append(f"{rel}\0dir")
        elif is_macho(path):
            rows.append(f"{rel}\0macho")
        else:
            executable = "x" if path.stat().st_mode & 0o111 else "-"
            rows.append(f"{rel}\0file\0{_sha256(path)}\0{executable}")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest(), len(rows)


MIT = """MIT License

Copyright (c) {author}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def _thin_node(source: Path, dest: Path, runner=None) -> None:
    """Copy Node, keeping only its arm64 slice when it is a universal binary."""
    with source.open("rb") as handle:
        magic = handle.read(4)
    if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca") and Path("/usr/bin/lipo").exists():
        command = ["/usr/bin/lipo", str(source), "-thin", "arm64", "-output", str(dest)]
        result = subprocess.run(command, env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=300)
        if result.returncode:
            raise HarnessError("Node has no arm64 slice to bundle")
    else:
        shutil.copyfile(source, dest)
    dest.chmod(0o755)


def stage(bundle: Path, source: Path, node: Path | None = None, *, release: bool = False,
          lock: dict | None = None) -> dict:
    """Copy the pruned Pi release and Node into the bundle; return the build-info record."""
    release_folder = release_dir(source)
    node = Path(node).expanduser() if node else default_node()
    if node is None or not node.is_file() or not os.access(node, os.X_OK):
        raise HarnessError("No Node binary to bundle with Ideate's harness (pass --ideate-node)")
    target = Path(bundle) / BUNDLE_DIR
    if target.exists() or target.is_symlink():
        raise HarnessError(f"Bundle already has {BUNDLE_DIR}")
    version = pi_version(release_folder)
    nversion = node_version(node)
    if nversion and tuple(int(x) for x in nversion.split(".")) < MIN_NODE:
        raise HarnessError(f"Node {nversion} is older than Pi needs (22.19 or newer)")
    node_sha = _sha256(node)
    target.mkdir(parents=True)
    _copy_tree(release_folder, target / "pi")
    if not (target / "pi" / PI_ENTRY).is_file():
        raise HarnessError("Pruning removed Pi's entry point")
    (target / NODE_PATH).parent.mkdir(parents=True)
    _thin_node(node, target / NODE_PATH)
    licenses = target / "LICENSES"
    licenses.mkdir()
    (licenses / "Pi-MIT.txt").write_text(MIT.format(author=pi_author(release_folder)))
    license_file = node_license(node)
    if license_file is not None:
        shutil.copyfile(license_file, licenses / "Node-LICENSE")
        node_license_state = "copied"
    else:
        if release:
            raise HarnessError("Node's LICENSE is not beside the Node install; a release must carry it")
        (licenses / "Node-NOTICE.txt").write_text(
            f"Node.js {nversion or ''} is distributed under the MIT License together with the licences of the\n"
            "libraries it bundles. The full text ships with every Node.js release as LICENSE and is published\n"
            f"at https://github.com/nodejs/node/blob/v{nversion or 'main'}/LICENSE\n")
        node_license_state = "reference"
    digest, entries = manifest(target / "pi")
    pin = lock if lock is not None else load_lock()
    pinned = (pin.get("version") == version and pin.get("manifest_sha256") == digest
              and (pin.get("node") or {}).get("sha256") == node_sha)
    if release and not pinned:
        raise HarnessError("The Pi harness or Node does not match build-support/ideate-harness.lock.json")
    record = {"harness": "pi", "version": version, "manifest_sha256": digest, "entries": entries,
              "node_version": nversion, "node_sha256": node_sha, "node_license": node_license_state,
              "pinned": pinned, "path": BUNDLE_DIR}
    (target / "HARNESS.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def validate(bundle: Path, record) -> None:
    """The bundle carries the harness exactly when build-info says so, and it is intact."""
    bundle = Path(bundle)
    target = bundle / BUNDLE_DIR
    if record is None:
        if target.exists() or target.is_symlink():
            raise HarnessError("Bundle has an Ideate harness its build-info does not declare")
        return
    if not isinstance(record, dict) or record.get("harness") != "pi":
        raise HarnessError("Bundled Ideate harness record is not a Pi record")
    try:
        on_disk = json.loads((target / "HARNESS.json").read_text())
    except (OSError, ValueError):
        raise HarnessError("Bundled Ideate harness has no HARNESS.json") from None
    if on_disk != record:
        raise HarnessError("Bundled Ideate harness differs from its build-info record")
    node = target / NODE_PATH
    if node.is_symlink() or not node.is_file() or not os.access(node, os.X_OK) or not is_macho(node):
        raise HarnessError("Bundled Node is missing or not an executable Mach-O")
    if not (target / "pi" / PI_ENTRY).is_file():
        raise HarnessError("Bundled Pi has no entry point")
    if not (target / "LICENSES" / "Pi-MIT.txt").is_file() or not (
            (target / "LICENSES" / "Node-LICENSE").is_file() or (target / "LICENSES" / "Node-NOTICE.txt").is_file()):
        raise HarnessError("Bundled Ideate harness is missing its licence texts")
    root = target.resolve()
    for path in target.rglob("*"):
        if path.is_symlink():
            link = os.readlink(path)
            if os.path.isabs(link) or not path.exists() or not path.resolve().is_relative_to(root):
                raise HarnessError(f"Non-relocatable link in the Ideate harness: {path.relative_to(bundle)}")
        if path.name in _UNWANTED or path.name.endswith(_UNWANTED_SUFFIX) or path.name.startswith(".env"):
            raise HarnessError(f"Unwanted file in the Ideate harness: {path.relative_to(bundle)}")
    digest, entries = manifest(target / "pi")
    if digest != record.get("manifest_sha256") or entries != record.get("entries"):
        raise HarnessError("Bundled Pi differs from the manifest recorded at staging")


def is_node_target(bundle: Path, target: Path) -> bool:
    """For signing: only the Node binary takes node.entitlements (JIT); native addons are signed without them."""
    return Path(target) == Path(bundle) / BUNDLE_DIR / NODE_PATH
