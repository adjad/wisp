"""Native action bridge. Outbound communications remain transient and never
replay. Calendar actions have persistent identities, exclusive native claims,
and receipts reconciled before the waiting tool reports success."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import time

from service.assistant.hub import hub

# Long enough for the app to run an AppleScript against a cold Mail.app (which
# `tell application "Mail"` may have to launch first), short enough that a
# non-responding app doesn't hold the whole agent turn open.
DEFAULT_TIMEOUT_S = 45.0

_pending: dict[str, asyncio.Future] = {}
_pending_waiters: dict[str, int] = {}


def _reminder_base_action_id(event_type: str, payload: dict) -> str | None:
    try:
        semantic = json.dumps({"type": event_type, "payload": payload},
                              sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return "reminder:" + hashlib.sha256(semantic.encode()).hexdigest()[:32]


def _generation(base_action_id: str, row: dict) -> int:
    identifier = row["payload"]["action_id"]
    if identifier == base_action_id:
        return 0
    suffix = identifier.removeprefix(base_action_id + ":")
    return int(suffix) if suffix.isdecimal() else -1


def reminder_create_fallback_allowed(payload: dict) -> bool:
    """True only when no native create for this exact reminder may exist.

    A Wisp-only fallback must never twin a native item, so any earlier
    generation that is unclaimed, claimed without a receipt, unknown, or
    succeeded and still present blocks it. Only a verified pre-write failure
    or a create retired by a later verified complete/delete allows it.
    """
    base_action_id = _reminder_base_action_id("create_reminder", payload)
    if base_action_id is None:
        return False
    try:
        attempts = hub.store.reminder_actions_by_prefix(base_action_id)
    except Exception:  # noqa: BLE001
        return False
    if not attempts:
        return True
    previous = max(attempts, key=lambda row: _generation(base_action_id, row))
    result = previous["result"]
    if not result:
        return False
    if result.get("status") == "failed":
        return True
    if result.get("status") == "succeeded":
        try:
            return hub.store.reminder_create_retry_state(previous) == "retired"
        except Exception:  # noqa: BLE001
            return False
    return False


async def request(event_type: str, payload: dict,
                  timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    from service import diagnostics
    standalone = diagnostics.current_id() is None
    with diagnostics.span("native") as trace:
        trace.event("requested", component="native", native_operation=event_type)
        result = await _request(event_type, payload, timeout)
        outcome = "succeeded" if result.get("ok") is True else "unknown" if result.get("status") == "unknown" else "failed"
        trace.event("result", component="native", ok=result.get("ok") is True, native_outcome=outcome)
        if standalone and outcome != "succeeded":
            trace.finish("unknown" if outcome == "unknown" else "failed")
        return result


async def _request(event_type: str, payload: dict,
                   timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Ask the app to do something and wait for its result.

    Returns {"ok": bool, "error": str, ...}. Never raises.
    """
    calendar = event_type in {"create_calendar_event", "delete_calendar_event"}
    reminder = event_type in {"create_reminder", "update_reminder", "complete_reminder", "delete_reminder"}
    native = calendar or reminder
    if native and not hub.has_subscribers:
        return {"ok": False, "error": ("Wisp app is not connected; Calendar was not changed"
                                        if calendar else "Wisp app is not connected; Reminders was not changed")}
    if reminder:
        # Stable across process restart and a user's immediate retry. An
        # uncertain native write must keep its original action identity; a
        # fresh random ID could otherwise create a second Reminders item.
        base_action_id = _reminder_base_action_id(event_type, payload)
        if base_action_id is None:
            return {"ok": False, "error": "Invalid reminder action payload"}
        try:
            attempts = hub.store.reminder_actions_by_prefix(base_action_id)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "Reminder claim store is unavailable; nothing was sent"}
        def generation(row: dict) -> int:
            return _generation(base_action_id, row)
        previous = max(attempts, key=generation) if attempts else None
        if previous:
            if previous["result"] and previous["result"]["status"] == "succeeded":
                if event_type == "create_reminder":
                    try:
                        current = hub.store.reminder_create_retry_state(previous)
                    except Exception:  # noqa: BLE001
                        return {"ok": False, "error": "Reminder state is unavailable; nothing was sent"}
                    if current == "present":
                        return previous["result"]
                    if current != "retired":
                        return {"ok": False, "error":
                                "Prior reminder state is uncertain; check it before creating again"}
                    action_id = base_action_id + ":" + str(generation(previous) + 1)
                elif event_type == "update_reminder":
                    try:
                        current = hub.store.reminder_update_state(payload)
                    except Exception:  # noqa: BLE001
                        return {"ok": False, "error": "Reminder state is unavailable; nothing was sent"}
                    if current == "desired":
                        return previous["result"]
                    if current != "expected":
                        return {"ok": False, "error":
                                "Exact reminder state changed; refresh it before another update"}
                    action_id = base_action_id + ":" + str(generation(previous) + 1)
                elif event_type in {"complete_reminder", "delete_reminder"}:
                    try:
                        current = hub.store.reminder_terminal_retry_state(event_type, payload)
                    except Exception:  # noqa: BLE001
                        return {"ok": False, "error": "Reminder state is unavailable; nothing was sent"}
                    if current == "desired":
                        return previous["result"]
                    if current != "expected":
                        return {"ok": False, "error":
                                "Exact reminder state changed; refresh it before retrying"}
                    action_id = base_action_id + ":" + str(generation(previous) + 1)
                else:
                    return previous["result"]
            elif previous["result"] and previous["result"]["status"] == "unknown":
                return previous["result"]
            if previous["claim_token"] and not previous["result"]:
                return {"ok": False, "status": "unknown",
                        "action_id": previous["payload"]["action_id"],
                        "error": "Prior native outcome is unknown; reconcile the exact item before retrying"}
            if previous["result"] and previous["result"]["status"] == "failed":
                # A verified pre-write failure can be tried after the user
                # fixes access or the stale target. Unknown is never retried.
                action_id = base_action_id + ":" + str(generation(previous) + 1)
            elif previous["result"] is None:
                action_id = previous["payload"]["action_id"]
        else:
            action_id = base_action_id
    else:
        action_id = uuid.uuid4().hex[:12]
    fut = _pending.get(action_id)
    first_waiter = fut is None
    if fut is None:
        fut = asyncio.get_running_loop().create_future()
        _pending[action_id] = fut
    _pending_waiters[action_id] = _pending_waiters.get(action_id, 0) + 1
    try:
        event = {"type": event_type, "action_id": action_id, **payload}
        if native and first_waiter:
            await hub.publish(event, dedupe_key="action:" + action_id,
                              target={"type": "calendar" if calendar else "verified_reminder"},
                              expires_at=time.time() + timeout)
        elif not native:
            # Replaying a send after an uncertain result can send it twice.
            await hub.publish(event, durable=False)
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except asyncio.TimeoutError:
        if native:
            return {"ok": False, "status": "unknown", "action_id": action_id,
                    "error": "Native result was not confirmed; reconcile the exact item before retrying"}
        return {"ok": False,
                "error": ("the Wisp app didn't respond — it may not be running, "
                          "or this build of the app doesn't support this action yet")}
    except Exception as e:  # noqa: BLE001
        if native:
            return {"ok": False, "status": "unknown", "action_id": action_id,
                    "error": f"Native action state is unknown: {e}"}
        return {"ok": False, "error": str(e)}
    finally:
        _pending_waiters[action_id] -= 1
        if _pending_waiters[action_id] == 0:
            _pending_waiters.pop(action_id, None)
            _pending.pop(action_id, None)


def complete(action_id: str, result: dict) -> bool:
    """Resolve a pending request. Returns False if nothing was waiting on it
    (already timed out, or an id the backend never issued)."""
    fut = _pending.get(action_id)
    if fut and not fut.done():
        fut.set_result(result)
        return True
    return False
