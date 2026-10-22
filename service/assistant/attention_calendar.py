"""Private Calendar effect for reviewed, source-backed attention captures.

Generic calendar tools still require confirmation. This non-registered adapter
accepts either an opaque approval for this exact preview, or the separate live
capture opt-in read from disk immediately before dispatch. Neither source text
nor request JSON can supply an approval token.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import time

from service.assistant.calendar_dates import all_day_bounds
from service.paths import MOE_DIR

_SEAL = object()


def _digest(plan: dict) -> str:
    fields = ("capture_key", "title", "day", "timezone", "start_ts", "duration_min",
              "all_day", "automatic", "source_id")
    return hashlib.sha256(json.dumps({k: plan.get(k) for k in fields},
                                    sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class _Approval:
    digest: str
    seal: object


def approve_capture(plan: dict) -> _Approval:
    """Host-only: call after revalidating the exact displayed source preview."""
    return _Approval(_digest(plan), _SEAL)


def _payload(plan: dict) -> dict:
    title = plan.get("title")
    key = plan.get("capture_key")
    if (not isinstance(title, str) or not title.strip() or len(title) > 200
            or not isinstance(key, str) or not key.startswith("capture:") or len(key) > 200
            or not isinstance(plan.get("source_id"), str) or not plan["source_id"]
            or type(plan.get("automatic")) is not bool or type(plan.get("all_day")) is not bool):
        raise ValueError("Invalid source-backed Calendar capture")
    if plan["all_day"]:
        if plan.get("start_ts") is not None or plan.get("duration_min") is not None:
            raise ValueError("A date-only event cannot carry an invented clock")
        start, duration = all_day_bounds(plan.get("day"), plan.get("timezone"))
        return {"title": title.strip(), "when_ts": start, "duration_min": duration,
                "location": "", "all_day": True, "due_date": plan["day"],
                "timezone": plan["timezone"]}
    start, duration = plan.get("start_ts"), plan.get("duration_min")
    if (type(start) not in (int, float) or not math.isfinite(start) or start <= 0
            or type(duration) is not int or not 1 <= duration <= 10080):
        raise ValueError("A timed event requires an explicit start and duration")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(plan["timezone"])
    if datetime.fromtimestamp(start, zone).date().isoformat() != plan.get("day"):
        raise ValueError("Calendar start does not match its source date")
    return {"title": title.strip(), "when_ts": float(start), "duration_min": duration, "location": ""}


async def create_calendar(plan: dict, *, approval: _Approval | None = None) -> dict:
    from service.safety import Tier, audit, decide, read_only
    from service.attention.live import load_settings, settings_path
    from service.assistant.outbox import request

    def blocked(reason):
        return {"ok": False, "status": "blocked", "error": reason}

    try:
        payload = _payload(plan)
        digest = _digest(plan)
        # Grants must not lift view-only for an autonomous source interpreter.
        if read_only():
            return blocked("View-only mode: Calendar capture is disabled")
        decision = decide("calendar_write", payload, tool="add_calendar_event")
        if decision.tier is Tier.DENY:
            return blocked(decision.reason)
        if plan["automatic"]:
            settings = load_settings(settings_path(MOE_DIR))
            if settings.mode != "live" or getattr(settings, "capture_events", False) is not True:
                return blocked("Automatic Calendar capture has not been enabled")
        elif not (isinstance(approval, _Approval) and approval.seal is _SEAL and approval.digest == digest):
            return blocked("Review this exact event before adding it to Calendar")
        # Date-only events stay useful until their civil day ends; timed events
        # must still be ahead. Do not use process-local midnight arithmetic.
        end = payload["when_ts"] + payload["duration_min"] * 60
        if (end if plan["all_day"] else payload["when_ts"]) <= time.time():
            return blocked("The source event is already in the past")
        result = await request("create_calendar_event", payload,
                               idempotency_key=plan["capture_key"])
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return blocked(str(exc))
    except Exception as exc:  # unknown after any possible native dispatch
        return {"ok": False, "status": "unknown", "error": type(exc).__name__}
    audit("allow", tool="add_calendar_event", source="attention_capture",
          args={"capture_sha256": digest}, status=result.get("status", "unknown"))
    if result.get("ok") is True:
        return {"ok": True, "status": "succeeded", "source_id": result["source_id"]}
    return {"ok": False, "status": "unknown" if result.get("status") == "unknown" else "failed",
            "error": result.get("error") or "Calendar creation was not confirmed"}
