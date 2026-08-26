"""Configuration for the Air periodic node.

Everything lives under ~/.wispair/ (0700 — this directory ends up holding real
email subjects and message text, same machine-local trust model the Pro uses
for ~/.moe/). Defaults are baked in here; ~/.wispair/config.json overlays them,
so a redeploy of this code never clobbers machine-specific settings. Same
overlay pattern, and for the same reason, as the Pro's service/config.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

HOME = Path.home()
# WISPAIR_HOME exists so the test suite (and a second instance, if one is ever
# needed) can run against a scratch directory instead of the real one. The Air
# itself never sets it.
STATE_DIR = Path(os.environ.get("WISPAIR_HOME") or (HOME / ".wispair"))
CONFIG_PATH = STATE_DIR / "config.json"
API_KEY_PATH = STATE_DIR / "api_key"
DB_PATH = STATE_DIR / "state.db"

DEFAULTS: dict[str, Any] = {
    # --- Inference backend -------------------------------------------------
    # Deliberately an OpenAI-compatible base URL rather than hardcoding
    # TurboFieldfare. TurboFieldfare is the intended runtime (26B streamed off
    # SSD, ~0.78 GB RSS — the reason a 16 GB Air can host this at all), but it
    # requires macOS 26 and is unverified on this machine. Keeping the client
    # generic means that if TurboFieldfare can't run, pointing this at any
    # other OpenAI-compatible local server is a config change, not a rewrite.
    # Nothing in this service uses a TurboFieldfare-specific feature.
    "model_base_url": "http://127.0.0.1:8081/v1",
    "model_name": "gemma-4-26b-a4b-it",
    "model_api_key": "",
    # A run is 1-2 calls of a few hundred output tokens, but prefill on this
    # runtime is SSD-bandwidth-bound and the Air's SSD is unbenchmarked, so
    # this is generous on purpose. A run that takes 4 minutes is fine at a 2%
    # duty cycle; a run that times out at 90s and loses the window is not.
    "model_timeout_s": 600,
    "model_max_tokens": 800,

    # --- Schedule ----------------------------------------------------------
    # 7 runs/day. Nothing between 01:00 and 05:00 — the Air is dark. The 05:00
    # run covers the overnight window on its own, because Mail and Messages
    # backfill from their servers when the machine wakes.
    "run_hours": [5, 8, 11, 14, 17, 20, 23],
    # Don't burn a run (and a full prefill) on two routine emails. Below this
    # the window is left unsummarized and rolls forward into the next run,
    # rather than generating seven "nothing happened" summaries a day.
    "min_items_to_summarize": 3,
    # ...unless content has been sitting unsummarized this long, at which point
    # summarize it regardless of how little there is. Without this, a quiet day
    # that never reaches min_items would roll forward forever and the user
    # would silently get nothing at all.
    "max_rollover_hours": 24,

    # --- Readers -----------------------------------------------------------
    # The readers always re-scan at least this far back regardless of cursor.
    # Some IMAP servers deliver mail with a backdated `date received`, so pure
    # early-termination would skip those messages permanently. Duplicates are
    # free (UNIQUE dedupe_key in the item store); a permanent hole is not.
    "reader_overlap_hours": 24,
    # Ceiling on how much a single run hands the model. Prefill cost is linear
    # in context and the server's max-context is 8192, so a burst of mail must
    # be truncated rather than blowing the context and forcing a re-prefill.
    "max_items_per_call": 60,

    # --- Networking --------------------------------------------------------
    "bind_host": "0.0.0.0",   # not 127.0.0.1, or the Pro can't reach it
    "bind_port": 8767,
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def ensure_state_dir() -> None:
    """Create ~/.wispair with 0700. Idempotent."""
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    # mkdir's mode is masked by umask and ignored if the dir already exists,
    # so set it explicitly — this directory holds real personal content.
    os.chmod(STATE_DIR, 0o700)


def load() -> dict[str, Any]:
    ensure_state_dir()
    cfg = dict(DEFAULTS)
    cfg.update(_read_json(CONFIG_PATH))
    return cfg


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the on-disk overlay and return the full config."""
    ensure_state_dir()
    overlay = _read_json(CONFIG_PATH)
    overlay.update(patch)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(overlay, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)   # atomic
    return load()


def api_key() -> str:
    """Shared secret required on every endpoint except /health.

    Cheap defense in depth on top of Tailscale's own ACLs — this service binds
    0.0.0.0 and serves summaries of the user's mail, so it should not be
    readable by anything else that happens to be on the LAN. Generated once and
    persisted; the same value has to be configured on the Pro.
    """
    ensure_state_dir()
    try:
        key = API_KEY_PATH.read_text().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_urlsafe(32)
    API_KEY_PATH.write_text(key)
    os.chmod(API_KEY_PATH, 0o600)
    return key
