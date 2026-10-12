"""HTTP surface for behavior learning (``/behavior/*``).

Behavior learning is on by default ("Learn my routines" in Settings, or
``WISP_BEHAVIOR=0/1`` at launch). Every route except ``/settings`` answers 409
with a plain explanation while it is off. Engine work runs off the event loop.
Only Wisp-internal state changes here: Undo deletes items Wisp itself created on its own schedule, and ``act``
returns the prefilled request for an ``open_prompt`` action rather than
sending or changing anything.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from service.assistant import behavior_runtime

router = APIRouter(prefix="/behavior", tags=["behavior"])

_DISABLED = "Behavior learning is off. Turn on \"Learn my routines\" in Settings."
_PATTERN_STATUSES = ("active", "confirmed", "rejected")
_VERDICTS = ("useful", "not_now", "wrong")


def _runtime() -> behavior_runtime.BehaviorRuntime:
    runtime = behavior_runtime.get_runtime()
    if runtime is None:
        raise HTTPException(status_code=409, detail=_DISABLED)
    return runtime


def _store_hub():
    from service.assistant.hub import hub
    from service.assistant.store import assistant_store
    return assistant_store, hub


def _body(body: Any) -> dict:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object body required")
    return body


def _settings_payload() -> dict[str, Any]:
    from service import behavior_switch as flags
    override = flags.env_override()
    return {"enabled": flags.enabled(), "setting": flags.setting_enabled(),
            "env_override": override}


@router.get("/settings")
async def behavior_settings() -> dict[str, Any]:
    """The "Learn my routines" setting. ``enabled`` is what is in effect: a
    ``WISP_BEHAVIOR`` launch override beats the saved ``setting``."""
    return _settings_payload()


@router.put("/settings")
async def set_behavior_settings(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn behavior learning on or off now, without restarting Wisp. Off stops
    ingesting, mining and nudging; what was learned is kept (nothing is cleared)."""
    value = _body(body).get("enabled")
    if type(value) is not bool:
        raise HTTPException(status_code=422, detail="enabled must be true or false")
    from service.config import set_behavior_enabled
    from service.tools.behavior_tools import sync_availability
    await asyncio.to_thread(set_behavior_enabled, value)
    sync_availability()
    payload = _settings_payload()
    if payload["enabled"]:
        store, hub = _store_hub()
        behavior_runtime.poke(store, hub)
    return payload


@router.get("/status")
async def behavior_status() -> dict[str, Any]:
    runtime = behavior_runtime.get_runtime()
    if runtime is None:
        return {"enabled": False, "detail": _DISABLED}
    return await asyncio.to_thread(runtime.status)


@router.post("/run")
async def behavior_run() -> dict[str, Any]:
    """Ingest + mine + generate + deliver now."""
    runtime = _runtime()
    store, hub = _store_hub()
    try:
        published = await runtime.tick(store, hub, force=True)
    except behavior_runtime.TickBusy as exc:
        # A scheduled tick is still running (mining or phrasing):
        # say so instead of answering "nothing new".
        return {"ok": False, "busy": True, "published": 0, "nudge_ids": [],
                "detail": f"{exc}; try again in a moment"}
    status = await asyncio.to_thread(runtime.status)
    return {"ok": True, "published": len(published),
            "nudge_ids": [p.get("nudge_id") for p in published if isinstance(p, dict)],
            "status": status}


@router.get("/patterns")
async def behavior_patterns(status: str | None = None, kind: str | None = None) -> dict[str, Any]:
    runtime = _runtime()
    if status is not None and status not in _PATTERN_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(_PATTERN_STATUSES)}")
    from service.behavior.types import PATTERN_KINDS
    if kind is not None and kind not in PATTERN_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(PATTERN_KINDS)}")
    patterns = await runtime.acall("patterns", status=status, kind=kind)
    return {"patterns": [p.to_dict() for p in patterns]}


@router.post("/patterns/{pattern_id}")
async def behavior_pattern_status(pattern_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = _runtime()
    status = _body(body).get("status")
    if status not in _PATTERN_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(_PATTERN_STATUSES)}")
    known = {p.id for p in await runtime.acall("patterns")}
    if pattern_id not in known:
        raise HTTPException(status_code=404, detail="Unknown pattern")
    try:
        pattern = await runtime.acall("set_pattern_status", pattern_id, status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown pattern") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.invalidate_card()
    return {"ok": True, "pattern": pattern.to_dict()}


@router.get("/predict")
async def behavior_predict(day: str = "tomorrow") -> dict[str, Any]:
    runtime = _runtime()
    if day not in {"today", "tomorrow", "next24h"}:
        raise HTTPException(status_code=422, detail="day must be today, tomorrow or next24h")
    preds = await asyncio.to_thread(behavior_runtime.predictions_for, runtime, day)
    return {"day": day, "predictions": [p.to_dict() for p in preds]}


@router.get("/profile")
async def behavior_profile(subject: str | None = None) -> dict[str, Any]:
    runtime = _runtime()
    card = await asyncio.to_thread(runtime.refresh_card)
    described = await runtime.acall("describe", subject)
    return {"card": card, **(described if isinstance(described, dict) else {})}


@router.get("/nudges")
async def behavior_nudges(limit: int = 20) -> dict[str, Any]:
    """Nudges this Mac delivered (newest first), with verdicts and Undo state."""
    runtime = _runtime()
    limit = max(1, min(int(limit), 100))
    return {"nudges": await asyncio.to_thread(runtime.delivered, limit)}


def _known_nudge(runtime, nudge_id: str):
    nudge = runtime.nudge(nudge_id)
    if nudge is None:
        raise HTTPException(status_code=404, detail="Unknown nudge")
    return nudge


@router.post("/nudges/{nudge_id}/feedback")
async def behavior_feedback(nudge_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = _runtime()
    verdict = _body(body).get("verdict")
    if verdict not in _VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict must be one of {list(_VERDICTS)}")
    await asyncio.to_thread(_known_nudge, runtime, nudge_id)
    store, hub = _store_hub()
    try:
        await asyncio.to_thread(runtime.feedback, store, nudge_id, verdict)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown nudge") from exc
    return {"ok": True, "nudge_id": nudge_id, "verdict": verdict}


@router.post("/nudges/{nudge_id}/undo")
async def behavior_undo(nudge_id: str) -> dict[str, Any]:
    runtime = _runtime()
    await asyncio.to_thread(_known_nudge, runtime, nudge_id)
    store, hub = _store_hub()
    removed = await asyncio.to_thread(runtime.undo, store, nudge_id)
    try:
        await hub.publish({"type": "changed"}, durable=False)
    except Exception:  # noqa: BLE001 — the undo itself already happened
        pass
    return {"ok": True, "nudge_id": nudge_id, "removed": removed}


@router.post("/nudges/{nudge_id}/act")
async def behavior_act(nudge_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """The user tapped an offered action. ``open_prompt`` returns the prefilled
    request for the overlay (nothing is sent or changed here); an offered
    reversible ``add_reminder``/``add_hold`` is created on Wisp's schedule."""
    runtime = _runtime()
    idx = _body(body).get("action_index", 0)
    if type(idx) is not int or idx < 0:
        raise HTTPException(status_code=422, detail="action_index must be a non-negative integer")
    nudge = await asyncio.to_thread(_known_nudge, runtime, nudge_id)
    if idx >= len(nudge.actions):
        raise HTTPException(status_code=404, detail="Unknown action")
    action = nudge.actions[idx]
    store, hub = _store_hub()
    if action.type == "open_prompt":
        prompt = " ".join(str((action.params or {}).get("prompt") or "").split())
        if not prompt:
            raise HTTPException(status_code=409, detail="This action has no prompt")
        await asyncio.to_thread(_feedback_quietly, runtime, store, nudge_id)
        return {"ok": True, "type": "open_prompt", "prompt": prompt, "label": action.label}
    if action.type in {"add_reminder", "add_hold"}:
        done = await asyncio.to_thread(runtime.perform_action, store, nudge_id, idx, action, runtime.clock())
        if done is None:
            return {"ok": False, "type": action.type,
                    "detail": "Nothing was added: it is already on your schedule or its time has passed."}
        await asyncio.to_thread(_feedback_quietly, runtime, store, nudge_id)
        try:
            await hub.publish({"type": "changed"}, durable=False)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "type": action.type, "created": {
            "title": done["title"], "when_ts": done["when_ts"]}, "can_undo": True}
    raise HTTPException(status_code=409, detail="Unsupported action type")


def _feedback_quietly(runtime, store, nudge_id: str) -> None:
    try:
        runtime.feedback(store, nudge_id, "useful")
    except Exception:  # noqa: BLE001 — feedback is advisory
        pass
