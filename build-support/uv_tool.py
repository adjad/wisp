"""The uv/uvx binaries bundled in Wisp.app, fetched at build time and pinned.

Wisp's Google tools run workspace-mcp through uvx (service/google_workspace.py).
Tester Macs don't have uv, so the build ships Astral's release binaries as
Contents/Resources/bin/uv and uvx, signed with the app. The release asset is
pinned here by version, size and SHA-256; a download or cache entry that
differs is refused and the build fails. Nothing is fetched at runtime by this
module (uvx itself fetches workspace-mcp when the user first connects Google).

Same pattern as the pinned engine DMG (engine.verified_download): https-only
curl with the system trust store, a cache under .wisp-build that is
re-verified on every use, and nothing from the archive is executed during the
build. Only the two expected regular files are read out of the tarball; links,
other members and oversized entries are refused.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tarfile
from typing import Callable

UV_VERSION = "0.12.23"          # released 2026-10-03
UV_ASSET = "uv-aarch64-apple-darwin.tar.gz"
UV_SIZE = 17044217
UV_SHA256 = "50487ae565ccd96e499056b4674d438f4c53170202617b4c759defe0c6a1b544"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{UV_ASSET}"
# Where the binaries go in the bundle; service/google_workspace.py looks there.
BUNDLE_DIR = "Contents/Resources/bin"
BINARIES = ("uv", "uvx")
_MEMBER_DIR = "uv-aarch64-apple-darwin"
_MAX_BINARY = 128 * 1024 * 1024


class UVToolError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _curl(url: str, destination: Path, limit: int) -> None:
    result = subprocess.run(
        ["/usr/bin/curl", "-q", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
         "--proto-redir", "=https", "--retry", "3", "--max-time", "600", "--max-filesize", str(limit),
         "--output", str(destination), url],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=700)
    if result.returncode:
        raise UVToolError("uv release download failed: " + result.stderr.decode(errors="replace")[-300:])


def verified_archive(cache: Path, *, offline: bool = False, size: int = UV_SIZE, sha256: str = UV_SHA256,
                     fetch: Callable[[str, Path, int], None] | None = None) -> Path:
    """The pinned uv tarball in `cache`, downloaded if absent, size and SHA-256 checked either way."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{UV_VERSION}-{UV_ASSET}"

    def good(path: Path) -> bool:
        return path.is_file() and not path.is_symlink() and path.stat().st_size == size and _sha256(path) == sha256

    if (target.exists() or target.is_symlink()) and not good(target):
        target.unlink()                       # a partial or tampered cache entry is never reused
    if not target.exists():
        if offline:
            raise UVToolError("The pinned uv release is not cached and this is an offline build "
                              "(run once online, or pass --no-uv)")
        part = cache / (target.name + ".part")
        part.unlink(missing_ok=True)
        (fetch or _curl)(UV_URL, part, size + 1024 * 1024)
        if not good(part):
            part.unlink(missing_ok=True)
            raise UVToolError("The downloaded uv release does not match the pinned size and SHA-256")
        part.replace(target)
    return target


def binaries(archive: Path) -> dict[str, bytes]:
    """{"uv": bytes, "uvx": bytes} from the verified tarball; anything unexpected is refused."""
    found: dict[str, bytes] = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.rstrip("/")
                if member.isdir() and name == _MEMBER_DIR:
                    continue
                base = name.split("/", 1)[1] if name.startswith(_MEMBER_DIR + "/") else ""
                if base not in BINARIES or not member.isfile() or base in found:
                    raise UVToolError("Unexpected entry in the uv release archive")
                if not 0 < member.size <= _MAX_BINARY:
                    raise UVToolError("Unexpected uv binary size")
                handle = tar.extractfile(member)
                data = handle.read() if handle else b""
                if len(data) != member.size or data[:4] != b"\xcf\xfa\xed\xfe":
                    raise UVToolError("The uv release archive holds no arm64 Mach-O " + base)
                found[base] = data
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UVToolError(f"Unreadable uv release archive ({type(exc).__name__})") from None
    if set(found) != set(BINARIES):
        raise UVToolError("The uv release archive lacks uv or uvx")
    return found


def stage(bundle: Path, archive: Path) -> dict:
    """Write uv and uvx into the bundle (mode 0755). Returns the build-info record."""
    target = bundle / BUNDLE_DIR
    target.mkdir(parents=True, exist_ok=True)
    for name, data in binaries(archive).items():
        path = target / name
        if path.exists() or path.is_symlink():
            raise UVToolError(f"Bundle already has {BUNDLE_DIR}/{name}")
        path.write_bytes(data)
        path.chmod(0o755)
    return {"version": UV_VERSION, "asset": UV_ASSET, "sha256": UV_SHA256, "path": BUNDLE_DIR}


def validate(bundle: Path, record) -> None:
    """The bundle carries uv exactly when build-info says it does."""
    target = bundle / BUNDLE_DIR
    if record is None:
        if any((target / name).exists() or (target / name).is_symlink() for name in BINARIES):
            raise UVToolError("Bundle has uv binaries its build-info does not declare")
        return
    if not isinstance(record, dict) or record.get("version") != UV_VERSION or record.get("sha256") != UV_SHA256:
        raise UVToolError("Bundled uv record does not match the pinned release")
    for name in BINARIES:
        path = target / name
        if path.is_symlink() or not path.is_file() or not (path.stat().st_mode & 0o111):
            raise UVToolError(f"Missing or non-executable bundled {name}")
