"""Local endpoints for the attention runner: see what it did, change its mode, undo one."""
import time
from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from service.assistant import attention_runner as runner
from service.attention.live import MODES, load_settings, local_midnight, save_settings, settings_path, updated
from service.attention.detectors import local_timezone
from service.paths import MOE_DIR

router = APIRouter(prefix="/assistant/attention", tags=["attention"])


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str | None = None
    daily_cap: StrictInt | None = Field(default=None, ge=0, le=50)
    quiet_start: StrictInt | None = Field(default=None, ge=0, le=23)
    quiet_end: StrictInt | None = Field(default=None, ge=0, le=23)
    lead_minutes: StrictInt | None = Field(default=None, ge=0, le=1440)
    default_hour: StrictInt | None = Field(default=None, ge=0, le=23)


class Undo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)


@router.get("")
def status():
    ledger = runner.get_ledger()
    settings = load_settings(settings_path(MOE_DIR))
    since = local_midnight(time.time(), local_timezone())
    return {
        "settings": asdict(settings),
        "modes": list(MODES),
        "created_today": ledger.count_since(since),
        "recent": [{"source_id": r["source_id"], "state": r["state"], "title": r["title"],
                    "due_ts": r["due_ts"], "event_ts": r["event_ts"], "decided_at": r["decided_at"],
                    "when": r["detail"].get("when"), "quote": r["detail"].get("quote")}
                   for r in ledger.recent(20)],
    }


@router.put("/settings")
def put_settings(body: SettingsPatch):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        new = updated(load_settings(settings_path(MOE_DIR)), **changes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    save_settings(settings_path(MOE_DIR), new)
    return asdict(new)


@router.post("/undo")
async def undo(body: Undo):
    result = await runner.undo(body.source_id)
    if not result["ok"]:
        raise HTTPException(404, result["error"])
    return result
