"""What a predictor returns. Its own module so detectors and the scorer can both
import it without importing each other."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Prediction:
    alert: bool
    reason: str | None = None       # reason code of an alert, for per-detector reporting
    blocked_by: str | None = None   # the gate that said no, when alert is False
