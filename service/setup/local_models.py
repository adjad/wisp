"""Which of Wisp's catalogue models are already on disk in Wisp's own engine folder.

Before the built-in engine has started, setup can't ask it for its model list. A
model folder already in ~/.moe/engine/models is still installed: offering to
download it again would waste gigabytes.

Only Wisp's own folder is looked at, never a folder the person chose (an oMLX.app
model folder on the Desktop or in Documents would make macOS ask for permission,
and Wisp never asks for one by itself). Read-only and bounded: a few roots, a fixed
number of directory entries examined at each level, small index files only. Only
catalogue ids are looked for, in the layout the engine scans (oMLX's two-level
discovery): `<dir>/<model id>/` or `<dir>/<organisation>/<model id>/`, holding a
config.json and complete weights.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterable
from pathlib import Path

from . import catalog

# Model directories looked at per call.
MAX_ROOTS = 4
# Directory entries examined (not just kept) in any one listing: a model directory,
# an organisation folder, or a model's own folder. A wider folder is cut off there.
MAX_ENTRIES = 512
# A safetensors index is a few hundred KB at most; anything bigger is not read.
MAX_INDEX_BYTES = 1024 * 1024
_SHARD = re.compile(r"-(\d+)-of-(\d+)\.safetensors$")


def catalogue_ids() -> tuple[str, ...]:
    return tuple(m.id for m in (*catalog.PRIMARY_MODELS, *catalog.KEPT_MODELS, catalog.EMBEDDING_MODEL))


def _entries(folder: Path) -> list[Path]:
    """At most MAX_ENTRIES entries of `folder`, counting every entry examined."""
    found: list[Path] = []
    try:
        for count, entry in enumerate(folder.iterdir()):
            if count >= MAX_ENTRIES:
                break
            found.append(entry)
    except OSError:
        return []
    return found


def weights_complete(folder: Path) -> bool:
    """A config.json plus every weight shard the checkpoint promises.

    An interrupted download leaves config.json (fetched first) and only some
    shards; the engine would then fail to load it, so it does not count.
    """
    try:
        if not (folder / "config.json").is_file() or (folder / "adapter_config.json").exists():
            return False
        weights = [p for p in _entries(folder) if p.name.endswith(".safetensors") and p.is_file()]
        if not weights or any(p.stat().st_size == 0 for p in weights):
            return False
    except OSError:
        return False
    names = {p.name for p in weights}
    promised: set[str] = set()
    index = folder / "model.safetensors.index.json"
    try:
        readable = index.is_file() and index.stat().st_size <= MAX_INDEX_BYTES
    except OSError:
        readable = False
    if readable:
        try:
            weight_map = json.loads(index.read_text()).get("weight_map")
        except (OSError, ValueError, AttributeError, RecursionError):
            weight_map = None
        if isinstance(weight_map, dict):
            # Only numbered shards are completeness promises, as in the engine's own
            # check; an index may also list auxiliary files the loader can skip.
            promised = {v for v in weight_map.values()
                        if isinstance(v, str) and "/" not in v and _SHARD.search(v)}
    for name in names:
        match = _SHARD.search(name)
        if match:
            width, total = len(match.group(1)), int(match.group(2))
            promised |= {_SHARD.sub(f"-{n:0{width}d}-of-{match.group(2)}.safetensors", name)
                         for n in range(1, min(total, 999) + 1)}
    return promised <= names


def _folders(folder: Path) -> list[Path]:
    return [e for e in _entries(folder) if not e.name.startswith(".") and e.is_dir()]


def catalogue_models_on_disk(model_dirs: Iterable[Path | str],
                             wanted: Iterable[str] | None = None) -> list[str]:
    """Catalogue model ids whose complete folders are in `model_dirs`, sorted."""
    targets = set(wanted if wanted is not None else catalogue_ids())
    found: set[str] = set()
    for index, raw in enumerate(model_dirs):
        if index >= MAX_ROOTS:
            break
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        for entry in _folders(root):
            if entry.name in targets:
                if weights_complete(entry):
                    found.add(entry.name)
                continue
            if (entry / "config.json").exists():
                continue  # a model itself, not an organisation folder
            for child in _folders(entry):
                if child.name in targets and weights_complete(child):
                    found.add(child.name)
    return sorted(found)


# (path, size, mtime_ns) -> sha256, so a status poll hashes a 5.6 GB download once. One lock per
# file, so a poll checking a small file never waits behind another file's hash.
_hashes: dict[tuple[str, int, int], str] = {}
_hash_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# Besides the pinned files, a downloaded folder may hold only these (and dot-entries such
# as .gitattributes and hub's .cache): an added template or token file changes the model.
ALLOWED_EXTRA = frozenset({"README.md"})


def _sha256(path: Path, stat) -> str:
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _locks_guard:
        lock = _hash_locks.setdefault(str(path), threading.Lock())
    with lock:
        if key not in _hashes:
            digest = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(8 << 20), b""):
                    digest.update(chunk)
            _hashes[key] = digest.hexdigest()
        return _hashes[key]


def bad_pinned_files(folder: Path, model: "catalog.CatalogModel") -> list[str]:
    """Pinned file names in `folder` whose size or sha256 differs (hashes them if needed)."""
    bad = []
    for name, sha256, size in model.pinned_files:
        path = folder / name
        try:
            stat = path.stat()
            if stat.st_size != size or _sha256(path, stat) != sha256:
                bad.append(name)
        except OSError:
            continue        # missing: a fresh download fetches it anyway
    return bad


def verify_download(folder: Path, model: "catalog.CatalogModel", *, cached_only: bool = False) -> str | None:
    """None when `folder` holds a usable copy of `model`; else "incomplete" (weights
    missing or partial) or "mismatch" (a pinned file differs from what Wisp tested, or
    the folder holds a file the tested model didn't have). `cached_only`: never hash;
    a file not hashed yet counts as matching."""
    if not weights_complete(folder):
        return "incomplete"
    if model.pinned_files:
        allowed = {name for name, _, _ in model.pinned_files} | ALLOWED_EXTRA
        try:
            extra = [e.name for e in folder.iterdir() if not e.name.startswith(".") and e.name not in allowed]
        except OSError:
            return "incomplete"
        if extra:
            return "mismatch"
    for name, sha256, size in model.pinned_files:
        path = folder / name
        try:
            stat = path.stat()
        except OSError:
            return "incomplete"
        if stat.st_size != size:
            return "mismatch"
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if cached_only and key not in _hashes:
            continue
        try:
            if _sha256(path, stat) != sha256:
                return "mismatch"
        except OSError:
            return "incomplete"
    return None
