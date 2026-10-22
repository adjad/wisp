"""Local endpoints for the attention runner: see what it did, change its mode, undo one."""
import time
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool

from service.assistant import attention_runner as runner
from service.attention.live import MODES, load_settings, local_midnight, save_settings, settings_path, updated
from service.paths import MOE_DIR

router = APIRouter(prefix="/assistant/attention", tags=["attention"])

SHOWN_STATES = ("created", "shadow", "capped", "unknown", "failed", "undone", "partial")


def _undoable(row: dict) -> bool:
    """Whether Undo can work at all: a created row whose identity was recorded and whose title
    is still on file (`runner.undo` matches both). The live check still happens on the request."""
    created = (row["detail"].get("result") or {}).get("created")
    return (row["state"] == "created" and row["title"] is not None and isinstance(created, dict)
            and created.get("source") in ("reminders", "manual"))


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str | None = None
    capture_events: StrictBool | None = None
    daily_cap: StrictInt | None = Field(default=None, ge=0, le=50)
    quiet_start: StrictInt | None = Field(default=None, ge=0, le=23)
    quiet_end: StrictInt | None = Field(default=None, ge=0, le=23)
    lead_minutes: StrictInt | None = Field(default=None, ge=0, le=1440)
    default_hour: StrictInt | None = Field(default=None, ge=0, le=23)


class Capture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)
    fingerprint: str = Field(min_length=64, max_length=64)


class Undo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)


@router.get("")
def status():
    ledger = runner.get_ledger()
    settings = load_settings(settings_path(MOE_DIR))
    tz = runner.current_zone()
    now = time.time()
    # Without a named zone, the system clock's own local midnight is still right for "today".
    since = local_midnight(now, tz) if tz else datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    current = set()
    if settings.mode != 'off' and tz:
        try:
            current = {('preview:' + c.source_id, c.review_fingerprint)
                       for c in runner.capture_candidates(now, tz, settings)}
        except Exception:  # source admission unavailable: no Add button
            pass
    recent = ledger.recent(100, SHOWN_STATES)
    paired_sources = {r['source_id'].removeprefix('preview:') for r in recent
                      if r['source_id'].startswith('preview:')}
    recent = [r for r in recent if not r['source_id'].startswith('capture:')
              and r['source_id'] not in paired_sources][:20]
    return {
        "settings": asdict(settings),
        "modes": list(MODES),
        "created_today": ledger.created_count_since(since),
        # Only rows the app shows: an `already_on_file` or in-flight row would crowd them out.
        "recent": [{"source_id": r["source_id"], "state": r["state"], "title": r["title"],
                    "due_ts": r["due_ts"], "event_ts": r["event_ts"], "decided_at": r["decided_at"],
                    "when": r["detail"].get("when"), "quote": r["detail"].get("quote"),
                    "undoable": _undoable(r), "paired": r["detail"].get("paired", False),
                    "fingerprint": r["detail"].get("review_fingerprint"),
                    "effects": r["detail"].get("effects"),
                    "captureable": (r["source_id"], r["detail"].get("review_fingerprint")) in current and r["state"] == "shadow"}
                   for r in recent],
    }


@router.put("/settings")
def put_settings(body: SettingsPatch):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    old = load_settings(settings_path(MOE_DIR))
    try:
        new = updated(old, **changes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    save_settings(settings_path(MOE_DIR), new)
    if new.mode == "live" and new.capture_events and (old.mode != "live" or not old.capture_events):
        runner.get_ledger().set_meta("capture_enabled_at", repr(time.time()))
        runner.get_ledger().set_meta("capture_live", "live")
    elif not new.capture_events or new.mode != "live":
        runner.get_ledger().set_meta("capture_live", "preview")
    if new.mode == "live" and old.mode != "live":
        # Re-baseline at the moment of the switch, not at the next pass: nothing that arrived
        # before this instant is ever acted on.
        runner.get_ledger().mark_live(time.time())
    return asdict(new)


@router.post("/undo")
async def undo(body: Undo):
    result = await runner.undo(body.source_id)
    if not result["ok"]:
        raise HTTPException(404, result["error"])
    return result


@router.post("/capture")
async def capture(body: Capture):
    try:
        row = await runner.capture_one(body.source_id, body.fingerprint)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": row["state"] == "created", "state": row["state"], "effects": row["detail"].get("effects", {})}
