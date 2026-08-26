"""Content-addressed cache for normalized page fetches, shared across jobs.

Re-fetching the exact same URL for a second research job (or a second round
of the same job) wastes network time and re-exposes Wisp to the same site
twice. This cache is purely an optimization: a miss always falls back to a
real fetch, and nothing here is treated as evidence until the orchestrator's
own extraction/validation runs against the returned text.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from service.paths import MOE_DIR

CACHE_DIR = MOE_DIR / "research_cache"
_TTL_SECONDS = 6 * 3600
_MAX_AGE_DAYS = 30


def _key(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def _path(canonical_url: str) -> Path:
    return CACHE_DIR / f"{_key(canonical_url)}.json"


def get(canonical_url: str) -> dict | None:
    path = _path(canonical_url)
    try:
        raw = path.read_text("utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if time.time() - float(data.get("cached_at", 0)) > _TTL_SECONDS:
        return None
    return data


def put(canonical_url: str, *, url: str, title: str, text: str,
        content_type: str, published_at: str, via_archive: bool = False) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _path(canonical_url)
    data = {"url": url, "canonical_url": canonical_url, "title": title, "text": text,
            "content_type": content_type, "published_at": published_at,
            "via_archive": via_archive, "cached_at": time.time()}
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def purge_older_than(days: float = _MAX_AGE_DAYS) -> int:
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for entry in CACHE_DIR.glob("*.json"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed
