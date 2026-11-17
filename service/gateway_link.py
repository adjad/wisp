"""The phone gateway's own credential for the local backend.

When the Wisp app launches the backend, every request needs the app's per-launch key
(service/local_http.AppKeyGuard). The phone gateway is started by the user by hand and
cannot have that key, so it holds a narrower one: a random secret in a private file
under Wisp's state directory, sent as `X-Wisp-Gateway-Key`. The backend honours it only
for the exact method and path pairs the gateway forwards (GATEWAY_ROUTES, which the
gateway checks too, so the two cannot drift) and only while the file is still the
person's own private regular file.

Why this is safe to accept: the app key defends against other user accounts and
sandboxed apps, which cannot read the private state directory. The gateway secret keeps
that property, because it lives in the same place, while letting a gateway the user
started themself work. It is never logged.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import stat
from pathlib import Path

from service.paths import moe_dir, private_mkdirs

GATEWAY_KEY_HEADER = "x-wisp-gateway-key"
SID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KEY_RE = re.compile(r"[0-9a-f]{64}")

# Exactly what service/phone_gateway/app.py forwards to the backend.
GATEWAY_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"/mode")),
    ("POST", re.compile(r"/agent")),
    ("POST", re.compile(r"/agent/approve")),
    ("GET", re.compile(r"/sessions")),
    ("GET", re.compile(r"/sessions/[A-Za-z0-9_-]{1,64}")),
    ("DELETE", re.compile(r"/sessions/[A-Za-z0-9_-]{1,64}")),
    ("GET", re.compile(r"/assistant/today")),
)


def is_gateway_route(method: str, path: str) -> bool:
    method = method.upper()
    return any(m == method and rx.fullmatch(path) for m, rx in GATEWAY_ROUTES)


def secret_path() -> Path:
    return moe_dir() / "phone_gateway" / "gateway-link.key"


def read_secret(path: Path | None = None) -> str | None:
    """The secret, or None unless the file is the person's own private regular file
    holding 64 lowercase hex characters. Never follows a link."""
    target = path or secret_path()
    try:
        before = os.lstat(target)
        if not stat.S_ISREG(before.st_mode):
            return None
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size != 64
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)):
            return None
        raw = os.read(fd, 65)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if _KEY_RE.fullmatch(text) else None


def ensure_secret(path: Path | None = None) -> str:
    """Create the secret if absent (0600, never through a link) and return it. An
    unusable file or link in its place is removed and replaced."""
    target = path or secret_path()
    private_mkdirs(target.parent)
    for _ in range(2):
        existing = read_secret(target)
        if existing is not None:
            return existing
        key = secrets.token_hex(32)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            os.unlink(target)                   # a link or an unusable file; a folder raises
            continue
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, key.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return key
    raise OSError("could not create the gateway link secret")


def gateway_key_valid(method: str, path: str, sent: list[str]) -> bool:
    """True when one gateway-key header matches the secret file and the request is
    one the gateway forwards."""
    if len(sent) != 1 or not is_gateway_route(method, path):
        return False
    try:
        given = sent[0].encode("ascii")
    except UnicodeEncodeError:
        return False
    secret = read_secret()
    return secret is not None and hmac.compare_digest(given, secret.encode("ascii"))
