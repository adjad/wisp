"""Native action bridge. Outbound communications remain transient and never
replay. Calendar actions have persistent identities, exclusive native claims,
and receipts reconciled before the waiting tool reports success."""
from __future__ import annotations

import asyncio
import uuid
import time

from service.assistant.hub import hub

# Long enough for the app to run an AppleScript against a cold Mail.app (which
# `tell application "Mail"` may have to launch first), short enough that a
# non-responding app doesn't hold the whole agent turn open.
DEFAULT_TIMEOUT_S = 45.0

_pending: dict[str, asyncio.Future] = {}


async def request(event_type: str, payload: dict,
                  timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Ask the app to do something and wait for its result.

    Returns {"ok": bool, "error": str, ...}. Never raises.
    """
    calendar = event_type in {"create_calendar_event", "delete_calendar_event"}
    if calendar and not hub.has_subscribers:
        return {"ok": False, "error": "Wisp app is not connected; Calendar was not changed"}
    action_id = uuid.uuid4().hex[:12]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[action_id] = fut
    try:
        event = {"type": event_type, "action_id": action_id, **payload}
        if calendar:
            await hub.publish(event, dedupe_key="action:" + action_id,
                              target={"type": "calendar"}, expires_at=time.time() + timeout)
        else:
            # Replaying a send after an uncertain result can send it twice.
            await hub.publish(event, durable=False)
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        if calendar:
            return {"ok": False, "status": "unknown", "action_id": action_id,
                    "error": "Calendar result was not confirmed; check Calendar before retrying"}
        return {"ok": False,
                "error": ("the Wisp app didn't respond — it may not be running, "
                          "or this build of the app doesn't support this action yet")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        _pending.pop(action_id, None)


def complete(action_id: str, result: dict) -> bool:
    """Resolve a pending request. Returns False if nothing was waiting on it
    (already timed out, or an id the backend never issued)."""
    fut = _pending.get(action_id)
    if fut and not fut.done():
        fut.set_result(result)
        return True
    return False
