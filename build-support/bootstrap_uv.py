"""Download one pinned uv binary without executing an installer or extracting paths."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import subprocess
from urllib.parse import unquote, urlsplit
from pathlib import PurePosixPath
import tempfile


def binary_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download(url):
    # macOS curl uses the system trust store even when the bootstrap Python's
    # optional certificate setup has not been run. Never disable TLS validation.
    result = subprocess.run(["/usr/bin/curl", "-q", "--fail", "--silent", "--show-error",
        "--location", "--proto", "=https", "--proto-redir", "=https", "--max-time", "120",
        "--max-filesize", str(100 * 1024 * 1024), url],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=130)
    if result.returncode:
        raise RuntimeError("Public dependency download failed: " + result.stderr.decode(errors="replace"))
    return result.stdout


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
    data = download(config["uv_archive_url"])
    if len(data) > 100 * 1024 * 1024:
        raise RuntimeError("uv download exceeds size limit")
    if hashlib.sha256(data).hexdigest() != config["uv_archive_sha256"]:
        raise RuntimeError("uv archive checksum mismatch")
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


def ensure_python_archive(config, state, offline=False):
    """Verify upstream bytes before letting pinned uv unpack the interpreter."""
    mirror = state / "python-downloads"
    name = unquote(urlsplit(config["python_archive_url"]).path.rsplit("/", 1)[1])
    archive = mirror / config["python_build"] / name
    expected = config["python_archive_sha256"]
    if archive.is_file() and binary_hash(archive) == expected:
        return mirror.as_uri()
    if offline:
        raise RuntimeError("Offline bootstrap requires the verified Python archive")
    data = download(config["python_archive_url"])
    if len(data) > 100 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("Python archive checksum mismatch or size limit exceeded")
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(".download")
    temporary.write_bytes(data)
    temporary.replace(archive)
    return mirror.as_uri()


def runtime_members(members):
    """Validate the complete archive namespace, using filesystem link semantics."""
    entries = {}
    directories = {("python",)}
    for member in members:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or ".." in path.parts or not path.parts
                or path.parts[0] != "python" or path.parts in entries
                or not (member.isfile() or member.isdir() or member.issym())):
            raise RuntimeError("Unsafe Python archive member: " + member.name)
        entries[path.parts] = member
        directories.update(parent.parts for parent in path.parents if parent.parts)
        if member.isdir():
            directories.add(path.parts)
    for path in directories:
        if path in entries and not entries[path].isdir():
            raise RuntimeError("Unsafe Python archive parent: " + "/".join(path))

    def kind(path):
        member = entries.get(tuple(path))
        if member is not None:
            return "link" if member.issym() else "directory" if member.isdir() else "file"
        return "directory" if tuple(path) in directories else None

    def resolve(parts, active):
        resolved = []
        for part in parts:
            if resolved and kind(resolved) != "directory":
                raise RuntimeError("Unsafe Python archive non-directory link component")
            if part in ("", "."):
                continue
            if part == "..":
                if len(resolved) <= 1:
                    raise RuntimeError("Unsafe Python archive escaping symlink chain")
                resolved.pop()
                continue
            resolved.append(part)
            key = tuple(resolved)
            entry_kind = kind(key)
            if entry_kind is None:
                raise RuntimeError("Unsafe Python archive missing symlink target: " + "/".join(key))
            if entry_kind == "link":
                if key in active:
                    raise RuntimeError("Unsafe Python archive symlink loop: " + "/".join(key))
                target = entries[key].linkname
                if not target or target.startswith("/"):
                    raise RuntimeError("Unsafe Python archive absolute or empty symlink")
                # Expand the link BEFORE interpreting following '..' components.
                resolved = resolve([*resolved[:-1], *target.split("/")], active | {key})
        return resolved

    try:
        for path, member in entries.items():
            if member.issym():
                resolve(path, set())
    except RecursionError:
        raise RuntimeError("Unsafe Python archive excessive symlink depth") from None
    return entries, directories


def unpack_runtime(config, state, destination):
    """Copy pristine upstream bytes only after validating every relative link."""
    ensure_python_archive(config, state, offline=True)
    name = unquote(urlsplit(config["python_archive_url"]).path.rsplit("/", 1)[1])
    archive_path = state / "python-downloads" / config["python_build"] / name
    with tarfile.open(archive_path, "r:gz") as archive:
        entries, directories = runtime_members(archive.getmembers())
        with tempfile.TemporaryDirectory(prefix="runtime-", dir=state) as tmp:
            root = Path(tmp)
            # Do not depend on tarfile's version-dependent extraction filters.
            # Materialize directories/files first, then validated links; no write
            # traverses a link, and no archive owner/device/hardlink is restored.
            for path in sorted(directories, key=len):
                root.joinpath(*path).mkdir(exist_ok=True)
            for path, member in entries.items():
                target = root.joinpath(*path)
                if member.isfile():
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(member.mode & 0o777)
            for path, member in entries.items():
                if member.issym():
                    root.joinpath(*path).symlink_to(member.linkname)
            for path, member in entries.items():
                if member.issym() and not root.joinpath(*path).resolve(strict=True).is_relative_to((root / "python").resolve()):
                    raise RuntimeError("Unsafe Python archive extracted symlink")
                if member.isdir():
                    root.joinpath(*path).chmod(member.mode & 0o777)
            shutil.copytree(root / "python", destination, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
