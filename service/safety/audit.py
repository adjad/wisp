"""Append-only audit log of every tool action and its outcome."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from service.paths import MOE_DIR, private_append_text

AUDIT_DIR = MOE_DIR
AUDIT_LOG = AUDIT_DIR / "audit.jsonl"
_warned = False


def audit(event: str, **fields) -> None:
    """Append one JSON line to the audit log. Never raises.

    Owner-only (0600): each line names a tool and its arguments (recipients,
    reminder titles, reply fields), so another account must not read it."""
    try:
        from service.safety.redaction import scrub_obj
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **scrub_obj(fields)}
        private_append_text(AUDIT_LOG, json.dumps(rec, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        global _warned
        if not _warned:
            # e.g. a symlink in its place (refused, never written through).
            _warned = True
            logging.getLogger(__name__).warning("audit log %s not written: %s", AUDIT_LOG, exc)
