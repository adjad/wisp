"""On-device model bundles the Mac offers the phone.

Safety rules (the phone downloads and runs these, so they are strict):
only regular files directly inside the bundle directory, an allowed extension,
no symlinks, no subdirectories, no .py. File names are matched against a
strict pattern before any path is built, and files are opened with O_NOFOLLOW.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .store import atomic_write_json

ALLOWED_EXTENSIONS = (".safetensors", ".json", ".jinja", ".txt", ".model")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ID_RE = re.compile(r"[a-z0-9._-]{1,128}")
_ID_SANITIZE_RE = re.compile(r"[^a-z0-9._-]+")
_MODEL_TYPE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_HASH_CHUNK = 1024 * 1024
_CONFIG_MAX = 1024 * 1024


def make_id(directory_name: str) -> str:
    return _ID_SANITIZE_RE.sub("-", directory_name.lower()).strip("-")[:128]


def is_allowed_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name)) and name.lower().endswith(ALLOWED_EXTENSIONS)


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int
    mtime_ns: int


class ModelCatalog:
    def __init__(self, state: Path, extra_dirs: Iterable[Path] = ()):
        self.state = state
        self.extra_dirs = [Path(p) for p in extra_dirs]
        self._cache_path = state / "model_hashes.json"
        self._lock = threading.Lock()

    # -- configuration -----------------------------------------------------
    def bundles(self) -> dict[str, Path]:
        """id -> resolved directory. CLI dirs first, then models.json."""
        found: dict[str, Path] = {}

        def add(bundle_id: str, path: Path) -> None:
            bundle_id = make_id(bundle_id)
            if bundle_id and ID_RE.fullmatch(bundle_id) and bundle_id not in found:
                found[bundle_id] = path

        for directory in self.extra_dirs:
            try:
                resolved = directory.expanduser().resolve()
            except OSError:
                continue
            add(resolved.name, resolved)
        try:
            entries = json.loads((self.state / "models.json").read_text("utf-8"))
        except (OSError, ValueError):
            entries = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                raw = Path(entry["path"]).expanduser()
                if not raw.is_absolute():
                    continue
                try:
                    resolved = raw.resolve()
                except OSError:
                    continue
                ident = entry.get("id") if isinstance(entry.get("id"), str) else resolved.name
                add(ident, resolved)
        return found

    # -- scanning ----------------------------------------------------------
    @staticmethod
    def scan(directory: Path) -> list[FileInfo]:
        files: list[FileInfo] = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if not is_allowed_name(entry.name):
                        continue
                    try:
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    files.append(FileInfo(entry.name, st.st_size, st.st_mtime_ns))
        except OSError:
            return []
        return sorted(files, key=lambda f: f.name)

    @staticmethod
    def model_type(directory: Path) -> str | None:
        try:
            fd = os.open(directory / "config.json", os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(_CONFIG_MAX)
            value = json.loads(data).get("model_type")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and _MODEL_TYPE_RE.fullmatch(value) else None

    # -- hashing -------------------------------------------------------------
    @staticmethod
    def _key(directory: Path, info: FileInfo) -> str:
        return f"{directory}/{info.name}:{info.size}:{info.mtime_ns}"

    def _load_cache(self) -> dict[str, str]:
        try:
            raw = json.loads(self._cache_path.read_text("utf-8"))
            entries = raw.get("entries", {})
            return {k: v for k, v in entries.items()
                    if isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)}
        except (OSError, ValueError, AttributeError):
            return {}

    @staticmethod
    def _hash_file(path: Path, info: FileInfo) -> str | None:
        """SHA-256 of the file, or None if it changed while being read."""
        digest = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        if (after.st_size, after.st_mtime_ns) != (info.size, info.mtime_ns):
            return None
        return digest.hexdigest()

    def listing(self) -> dict[str, Any]:
        """Blocking: callers run this in a thread (hashing can take seconds)."""
        with self._lock:
            cache = self._load_cache()
            fresh: dict[str, str] = {}
            models: list[dict[str, Any]] = []
            bundles = self.bundles()
            for bundle_id, directory in bundles.items():
                files_out = []
                for info in self.scan(directory):
                    key = self._key(directory, info)
                    digest = cache.get(key)
                    if digest is None:
                        try:
                            digest = self._hash_file(directory / info.name, info)
                        except OSError:
                            digest = None
                        if digest is None:
                            continue  # changed or vanished mid-hash: do not offer it
                    fresh[key] = digest
                    files_out.append({"name": info.name, "size": info.size, "sha256": digest})
                if not files_out:
                    continue
                models.append({
                    "id": bundle_id,
                    "model_type": self.model_type(directory),
                    "total_bytes": sum(f["size"] for f in files_out),
                    "files": files_out,
                })
            # Keep entries for directories that are not configured right now,
            # drop stale versions of files in the ones that are.
            prefixes = tuple(f"{d}/" for d in bundles.values())
            kept = {k: v for k, v in cache.items() if not k.startswith(prefixes)}
            kept.update(fresh)
            if kept != cache:
                try:
                    atomic_write_json(self._cache_path, {"version": 1, "entries": kept})
                except OSError:
                    pass
            return {"models": models}

    # -- serving -------------------------------------------------------------
    def open_file(self, model_id: str, name: str) -> tuple[Any, int] | None:
        """Open one served file safely. Returns (binary file object, size) or None."""
        if not ID_RE.fullmatch(model_id) or not is_allowed_name(name):
            return None
        directory = self.bundles().get(model_id)
        if directory is None:
            return None
        try:
            # Exact spelling only: a case-insensitive filesystem would otherwise
            # answer for names the listing never offered.
            if name not in os.listdir(directory):
                return None
            fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                return None
        except OSError:
            os.close(fd)
            return None
        return os.fdopen(fd, "rb"), st.st_size
