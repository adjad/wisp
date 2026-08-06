"""A shared secret between Wisp's backend and the things allowed to drive it.

The backend is a loopback service, and loopback is not a security boundary in
the way it looks like one. Two things reach it that shouldn't:

  * Any other process running as this user. The backend exposes `/agent` — run
    shell commands, read mail, send messages — with no credential at all, so
    anything on the machine could drive the assistant.
  * Any web page the user visits. A form POST or a JSON `fetch()` to
    `http://127.0.0.1:8765/agent` is a *simple request*: no CORS preflight, so
    the browser sends it and the backend answers. The page can't read the
    response cross-origin, but by then the side effect has already happened.

A header the caller must supply closes both. A browser cannot read a 0600 file
in the user's home directory, so it cannot forge the header, and any process
that *can* read that file already has the user's full privileges anyway — so
this is exactly as strong as the filesystem, which is the right ceiling for a
local-first app. Same shape as oMLX's own `~/.omlx/settings.json` key, and as
the Air node's (see air/wispair/config.py).

The key is generated on first use and lives in the state directory.
"""
from __future__ import annotations

import hmac
import os
import secrets

from fastapi import Header, HTTPException

from service.paths import STATE_DIR

API_KEY_PATH = STATE_DIR / "api_key"
HEADER_NAME = "X-Wisp-Key"

# Endpoints reachable without the key. Deliberately tiny: `/ping` is a pure
# liveness probe that returns a constant, so BackendManager can wait for the
# server to come up before it has read the key, and a port conflict is
# distinguishable from a dead backend. It exposes nothing.
PUBLIC_PATHS = {"/ping"}

_cached: str | None = None


def api_key() -> str:
    """The current key, generating and persisting one on first call."""
    global _cached
    if _cached:
        return _cached

    if API_KEY_PATH.exists():
        existing = API_KEY_PATH.read_text().strip()
        if existing:
            _cached = existing
            return _cached

    key = secrets.token_urlsafe(32)
    API_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Create 0600 from the start rather than writing then chmod-ing — otherwise
    # the key is briefly world-readable, which is the whole thing we're avoiding.
    fd = os.open(API_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    _cached = key
    return key


def require_key(x_wisp_key: str = Header(default="")) -> None:
    """FastAPI dependency: reject anything without the shared key.

    Compared with `hmac.compare_digest` rather than `==` so a wrong key can't be
    recovered a character at a time from response timing.
    """
    if not hmac.compare_digest(x_wisp_key or "", api_key()):
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-Wisp-Key header. The key is in "
                f"{API_KEY_PATH}."
            ),
        )
