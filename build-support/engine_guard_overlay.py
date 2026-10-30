"""Exact, reproducible Wisp overlay on an already verified upstream engine.

Never touches the signed input app. All inputs and resulting Python bytes are
pinned; a different upstream source needs an explicit reviewed overlay update.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parent / "engine_guard"
TARGETS = {"omlx/process_memory_enforcer.py", "omlx/engine_pool.py", "omlx/server.py"}
ADDITION = "omlx/wisp_guard.py"


class OverlayError(RuntimeError):
    pass


def _require(value):
    if not value:
        raise OverlayError("Engine guard overlay input or output does not match its pinned manifest")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            _require(key not in out)
            out[key] = value
        return out
    return json.loads(data, object_pairs_hook=unique)


def _read(root, name):
    parts = Path(name).parts
    _require(bool(parts) and not Path(name).is_absolute() and all(p not in ("", ".", "..") for p in parts))
    descriptors = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 4 * 1024 * 1024)
        chunks = []
        remaining = info.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        data = b"".join(chunks)
        unchanged = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        _require(len(data) == info.st_size and all(getattr(info, k) == getattr(after, k) for k in unchanged))
        return data
    except OSError:
        raise OverlayError("Engine guard overlay input cannot be read safely") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _manifest(root):
    raw = _read(root, "manifest.json")
    m = _json(raw)
    _require(set(m) == {"version", "id", "upstream_tree_sha256", "patches", "addition"})
    _require(type(m["version"]) is int and m["version"] == 1 and m["id"] == "wisp-engine-guard-v1")
    _require(isinstance(m["patches"], list) and len(m["patches"]) == len(TARGETS))
    _require({p["path"] for p in m["patches"]} == TARGETS)
    a = m["addition"]
    _require(set(a) == {"path", "source", "sha256"} and a["path"] == ADDITION and a["source"] == "proof.py")
    return m, _hash(raw)


def apply(engine, upstream_digest, *, root=ROOT):
    """Validate the complete plan before writing any derived engine bytes."""
    m, identity = _manifest(root)
    _require(m["upstream_tree_sha256"] == upstream_digest)
    planned = []
    for row in m["patches"]:
        _require(set(row) == {"path", "input_sha256", "output_sha256", "patch", "patch_sha256"})
        _require(row["patch"] == Path(row["path"]).name + ".patch.json")
        raw = _read(root, row["patch"])
        _require(_hash(raw) == row["patch_sha256"])
        patch = _json(raw)
        _require(set(patch) == {"path", "input_sha256", "output_sha256", "edits"})
        _require(all(patch[k] == row[k] for k in ("path", "input_sha256", "output_sha256")))
        data = _read(engine, row["path"])
        _require(_hash(data) == row["input_sha256"])
        text = data.decode("utf-8")
        _require(isinstance(patch["edits"], list) and 0 < len(patch["edits"]) <= 64)
        for change in patch["edits"]:
            _require(set(change) == {"old", "new", "count"})
            _require(type(change["count"]) is int and 0 < change["count"] <= 16)
            _require(type(change["old"]) is str and bool(change["old"]) and type(change["new"]) is str)
            _require(text.count(change["old"]) == change["count"])
            text = text.replace(change["old"], change["new"])
        data = text.encode("utf-8")
        _require(_hash(data) == row["output_sha256"])
        ast.parse(text)
        planned.append((row["path"], data))
    addition = _read(root, "proof.py")
    _require(_hash(addition) == m["addition"]["sha256"])
    _require(not os.path.lexists(Path(engine) / ADDITION))
    ast.parse(addition)
    planned.append((ADDITION, addition))
    for name, data in planned:
        target = Path(engine) / name
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        temporary = target.with_name(target.name + ".wisp-overlay-new")
        with temporary.open("xb") as stream:
            stream.write(data)
        temporary.chmod(mode)
        os.replace(temporary, target)
    receipt = {"id": m["id"], "manifest_sha256": identity,
               "files": {name: _hash(data) for name, data in planned}}
    verify(engine, receipt, root=root)
    return receipt


def verify(engine, receipt, *, root=ROOT):
    m, identity = _manifest(root)
    expected = {p["path"]: p["output_sha256"] for p in m["patches"]}
    expected[ADDITION] = m["addition"]["sha256"]
    _require(receipt == {"id": m["id"], "manifest_sha256": identity, "files": expected})
    for name, wanted in expected.items():
        _require(_hash(_read(engine, name)) == wanted)
