"""Pre-warm the Ask model's audio and vision encoders while the person is still
speaking or picking a picture.

Wisp's bundled engine keeps Gemma 4's audio tower (610 MB) and vision tower
(335 MB) out of memory until something needs them, and lets each go again after
five idle minutes or when the model unloads (engine overlay,
build-support/engine_guard/encoders.py). A load takes about a second, so the app
asks for one here the moment the mic opens or a picture is attached, and the
tower is in by the time the clip or the question arrives.

Rules:
- IMPORTANT: a hint, never a gate. A clip or picture that arrives first loads
  its tower at the engine and waits for it there; nothing here can make a
  transcript or a picture reading fail. Every error is swallowed.
- IMPORTANT: Wisp's own engine only. The user's oMLX.app keeps every tower
  resident as it always has, and an engine without the routes (an older
  bundle) answers 404 once, after which nothing is asked again this run.
- IMPORTANT: never concurrent on the model. The engine runs the load on the
  model's single engine thread, between scheduler steps. The call counts as in
  flight on the model and holds the foreground gate while it runs, so
  background work skips its tick and Save memory never parks the model under it.
- A parked model is brought back first through Save memory's own prewarm, so
  the press loads the model and then the tower, with one request each.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from service import idle

log = logging.getLogger(__name__)

MODALITIES = ("audio", "vision")
_TIMEOUT_S = 20.0          # a cold tower is about 1 s; the engine thread may be mid-step
_STATUS_TIMEOUT_S = 0.5

_tasks: dict[str, asyncio.Task] = {}
_unsupported = False       # this run's engine has no encoder routes


def _target():
    """(client, model) for the Ask model on Wisp's own engine, or None."""
    from service import memory_saver
    from service.config.endpoints import role_target
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        return None
    target = role_target("agent")
    if not (target.endpoint.managed and target.endpoint.name == "local"):
        return None
    from service.tools.imessage_tools import _c
    client = _c()
    if not memory_saver.bundled_engine(client):
        return None
    return client, target.model


def ask_model() -> str | None:
    target = _target()
    return target[1] if target else None


async def prewarm(modality: str, *, target=None) -> str:
    """Start loading `modality`'s tower; returns at once with what happens."""
    if modality not in MODALITIES:
        raise ValueError(modality)
    if _unsupported:
        return "unsupported"
    target = target or _target()
    if target is None:
        return "not_bundled"
    task = _tasks.get(modality)
    if task is not None and not task.done():
        return "loading"
    _tasks[modality] = asyncio.create_task(_prewarm(modality, *target))
    return "loading"


async def _prewarm(modality: str, client, model: str) -> None:
    global _unsupported
    from service import memory_saver
    try:
        if model not in await client.loaded_models():
            # Parked by Save memory: bring it back first, the same way opening Ask does.
            await memory_saver.prewarm(client)
            await memory_saver.settle_prewarm(timeout=_TIMEOUT_S)
            if model not in await client.loaded_models():
                return             # the clip's own request loads the model and the tower
        idle.begin_foreground()
        idle.begin(model)
        try:
            response = await client._client.post(f"/v1/wisp/encoders/{modality}/prewarm",
                                                  json={"model": model}, timeout=_TIMEOUT_S)
        finally:
            idle.end(model)
            idle.end_foreground()
        if response.status_code in (404, 405):
            _unsupported = True
        elif response.status_code != 200:
            log.info("encoders: %s prewarm answered %s", modality, response.status_code)
    except Exception as exc:  # noqa: BLE001 — a hint: the request loads the tower anyway
        log.info("encoders: %s prewarm failed: %s", modality, type(exc).__name__)


async def status() -> dict[str, Any] | None:
    """The Ask model's encoder residency as the engine reports it, or None when
    unknown (the user's oMLX, an older engine, or no answer in time)."""
    target = _target()
    if target is None or _unsupported:
        return None
    client, model = target
    try:
        response = await asyncio.wait_for(client._client.get("/v1/wisp/encoders"), _STATUS_TIMEOUT_S)
        if response.status_code != 200:
            return None
        row = (response.json().get("models") or {}).get(model)
    except Exception:  # noqa: BLE001 — display only
        return None
    return display(row) if isinstance(row, dict) else None


def display(row: dict[str, Any]) -> dict[str, Any]:
    """Only the fields the Models page shows, typed, from the engine's row."""
    towers = {}
    for name in MODALITIES:
        tower = (row.get("towers") or {}).get(name)
        if not isinstance(tower, dict):
            continue
        state = tower.get("state")
        size = tower.get("bytes")
        towers[name] = {"state": state if state in ("resident", "released", "loading") else "unknown",
                        "bytes": size if type(size) is int and size >= 0 else None}
    on_demand = row.get("supported") is True
    idle_s = row.get("idle_release_s")
    return {"on_demand": on_demand,
            "reason": None if on_demand else (row.get("reason") if isinstance(row.get("reason"), str) else None),
            "idle_release_s": idle_s if type(idle_s) in (int, float) else None,
            "resident_bytes": sum(t["bytes"] or 0 for t in towers.values() if t["state"] == "resident"),
            "towers": towers}
