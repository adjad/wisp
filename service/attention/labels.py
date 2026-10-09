"""The user's ground truth: what each sampled item should have done.

Labels answer one question per item, in the user's own terms: "if Wisp had seen
this when it arrived, should it have interrupted me?"

  missing  A commitment coming up SOON that is in neither Calendar nor
           Reminders. This is the only label that should produce an alert.
  known    A commitment coming up soon that I already had on file.
  later    A commitment that is NOT soon (more than ~48h out) and not on file.
  none     Nothing I need to act on or show up for.
  promo    Promotional or automated marketing.
  scam     Phishing or scam.
  skip     Unsure; excluded from scoring.

Labels are an append-only JSONL under ~/.moe/attention/ (0600): relabelling
appends, and the last record per item wins, so a mistake is never destructive
and the history of a decision survives.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

LABELS = ("missing", "known", "later", "none", "promo", "scam", "skip")
KEYS = {"m": "missing", "c": "known", "l": "later", "n": "none",
        "p": "promo", "s": "scam", "u": "skip"}
POSITIVE = "missing"
# Labels whose items must never alert; "promo"/"scam" are called out separately
# in reports because the user named them as the failure they care about most.
NEGATIVE = ("known", "later", "none", "promo", "scam")


class LabelStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, item_id: str, label: str, note: str = "", *, now: float | None = None) -> None:
        if label not in LABELS:
            raise ValueError(f"Unknown label {label!r}")
        record = {"id": item_id, "label": label, "note": note.strip()[:200],
                  "at": now if now is not None else time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def current(self) -> dict[str, dict]:
        """Latest record per item. A torn trailing line (crash mid-write) is skipped."""
        out: dict[str, dict] = {}
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("label") in LABELS and isinstance(rec.get("id"), str):
                out[rec["id"]] = rec
        return out
