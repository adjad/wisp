"""Download one pinned uv binary without executing an installer or extracting paths."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request


def binary_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def ensure_uv(config, state, offline=False):
    expected = config["uv_binary_sha256"]
    owned = state / "tools" / "uv"
    candidates = [owned]
    installed = shutil.which("uv")
    if installed:
        candidates.append(Path(installed))
    for path in candidates:
        if path.is_file() and binary_hash(path) == expected:
            return path
    if offline:
        raise RuntimeError(f"Offline bootstrap requires uv {config['uv']} with the pinned binary hash")
    print(f"→ download and verify uv {config['uv']}", flush=True)
    request = urllib.request.Request(config["uv_archive_url"], headers={"User-Agent": "Wisp-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise RuntimeError("uv download exceeds size limit")
    # Read a single known regular member; never unpack remote paths or run scripts.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("uv-aarch64-apple-darwin/uv")
        if not member.isfile() or member.size > 100 * 1024 * 1024:
            raise RuntimeError("Unexpected uv archive member")
        binary = archive.extractfile(member).read()
    if hashlib.sha256(binary).hexdigest() != expected:
        raise RuntimeError("uv binary checksum mismatch; refusing to execute it")
    owned.parent.mkdir(parents=True, exist_ok=True)
    temporary = owned.with_suffix(".download")
    temporary.write_bytes(binary)
    temporary.chmod(0o755)
    temporary.replace(owned)
    return owned
