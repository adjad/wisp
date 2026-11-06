"""Disk-backed persistence for the synced-content caches.

Mail/Messages/Notes content is pushed in by the Swift app and was previously
held ONLY in module-level globals — so every backend restart silently emptied
them. That's invisible for ordinary use (each source re-syncs within minutes
and a chat query just waits), but it quietly broke `build_profile`: a profile
built shortly after a restart saw whichever sources had happened to re-sync so
far and simply omitted the rest, with no error. A real build came back with
only `messages` and `calendar` — no email, no notes — because Mail's ~1-year
history scan takes minutes and Notes syncs only once a day.

Persisting each cache here makes a restart a non-event: the previous contents
load straight back at import time, and the next sync overwrites them as usual.

Plain UTF-8 text files under ~/.moe/cache/, one per cache. These hold real
personal content (email bodies, messages, notes), so the directory is created
0700 and files 0600 — same machine-local trust model as ~/.moe/sessions.db and
assistant.db, which already store conversation and commitment data unencrypted.
"""
from __future__ import annotations

from pathlib import Path

from service.paths import MOE_DIR, private_write_bytes

CACHE_DIR = MOE_DIR / "cache"


def _path(name: str) -> Path:
    return CACHE_DIR / f"{name}.txt"


def load(name: str) -> str:
    """Previously-saved contents for `name`, or "" if never saved/unreadable."""
    try:
        return _path(name).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — a missing/corrupt cache is just "empty"
        return ""


def save(name: str, text: str) -> None:
    """Persist `text` under `name`. Best-effort: a failure here must never
    break the sync that produced the data — the in-memory cache is still
    correct either way, it just won't survive the next restart."""
    try:
        # Contacts, messages and mail text: 0600 from the first byte (the temp
        # file used to be written 0644 and chmod-ed after), and atomic, so a
        # crash mid-write can't truncate the cache.
        private_write_bytes(_path(name), (text or "").encode("utf-8"))
        # The temp name builds before this used, which a crash could leave 0644.
        _path(name).with_suffix(".tmp").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
