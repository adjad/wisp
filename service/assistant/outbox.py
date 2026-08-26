"""Request/response bridge to the Swift app for actions only IT can perform.

The backend is a separate Python process with no macOS Automation grants —
Mail and Messages are TCC-gated per-process, and only Wisp.app has the stable
signed identity those grants attach to (see MailReader.swift's header comment).
So the backend can't send an email itself; it has to ask the app to.

The existing hub is one-way fire-and-forget (`create_calendar_event` publishes
and never learns whether it worked). That's fine for a calendar write the next
sync will confirm, and completely wrong for sending mail: the agent has to be
able to tell the user "sent" or "that failed, here's why", and reporting
success for something that silently didn't happen is the worst outcome
available.

So: `request()` publishes an event carrying an `action_id`, then waits on a
future keyed by that id. The app performs the action and POSTs the outcome to
/assistant/action_result, which calls `complete()`. A timeout resolves the
future as a failure rather than hanging the agent turn forever — if the app
isn't running or is on an older build that ignores the event, the tool reports
that instead of stalling.
"""
from __future__ import annotations

import asyncio
import uuid

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
    action_id = uuid.uuid4().hex[:12]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[action_id] = fut
    try:
        await hub.publish({"type": event_type, "action_id": action_id, **payload})
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
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
