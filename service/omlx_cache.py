"""Keep oMLX's on-disk prompt cache from growing without bound.

oMLX writes a near-full snapshot of every ~11k-token prompt, so Wisp fills a
multi-GB cache within minutes. Its own default ("auto") reserves half of the
free disk and it has no age-based expiry, so entries for retired models or
prompts sit there until the cap forces them out. Two best-effort controls:

* `ensure_cap` lowers `cache.ssd_cache_max_size` in oMLX's settings.json. oMLX
  reads that file at startup, so it takes effect on the next engine start. A
  smaller value the user chose is left alone.
* `sweep_stale` deletes cache files not touched for `MAX_AGE_DAYS`. oMLX
  treats a vanished block file as a cache miss, so this is safe while it runs.

The cache is recomputable state, so the worst outcome of either is one slow
first turn.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from service.config import _OMLX_STATE, OMLX_SETTINGS

CAP_BYTES = 4 * 1024**3
CAP_TEXT = "4GB"
MAX_AGE_DAYS = 7
# Unfinished writes are `.tmp`; oMLX keeps recent ones for another manager.
_TMP_GRACE_S = 24 * 3600

_UNITS = {"": 1, "B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _parse_size(text: str) -> int | None:
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?B?)\s*", str(text).upper())
    if not m or m.group(2) not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def cache_dir(settings: dict | None = None) -> Path:
    configured = ((settings or {}).get("cache") or {}).get("ssd_cache_dir")
    return Path(configured).expanduser() if configured else _OMLX_STATE / "cache"


def ensure_cap(path: Path = OMLX_SETTINGS) -> bool:
    """Lower the SSD cache cap to CAP_TEXT. True when the file was changed."""
    try:
        data = json.loads(path.read_text())
        cache = data.setdefault("cache", {})
        current = cache.get("ssd_cache_max_size", "auto")
        size = None if str(current).lower() == "auto" else _parse_size(current)
        if size is not None and size <= CAP_BYTES:
            return False
        cache["ssd_cache_max_size"] = CAP_TEXT
        tmp = path.with_name(path.name + ".wisp-tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, path.stat().st_mode & 0o777)  # holds the api key
        os.replace(tmp, path)
        return True
    except (OSError, ValueError):
        return False


def sweep_stale(root: Path | None = None, *, now: float | None = None,
                max_age_days: float = MAX_AGE_DAYS) -> tuple[int, int]:
    """Delete cache files untouched for `max_age_days`. Returns (files, bytes)."""
    if root is None:
        try:
            settings = json.loads(OMLX_SETTINGS.read_text())
        except (OSError, ValueError):
            settings = None
        root = cache_dir(settings)
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    files = freed = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath) / name
            try:
                st = p.lstat()
                if not p.is_file() or p.is_symlink():
                    continue
                fresh = max(st.st_mtime, st.st_atime)
                if fresh > (now - _TMP_GRACE_S if name.endswith(".tmp") else cutoff):
                    continue
                p.unlink()
            except OSError:
                continue
            files += 1
            freed += st.st_size
    return files, freed
