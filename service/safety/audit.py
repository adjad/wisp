"""Append-only audit log of every tool action and its outcome."""
from __future__ import annotations

import json
import time

from service.paths import STATE_DIR

AUDIT_DIR = STATE_DIR
AUDIT_LOG = AUDIT_DIR / "audit.jsonl"


def audit(event: str, **fields) -> None:
    """Append one JSON line to the audit log. Never raises."""
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the action it logs
        pass
