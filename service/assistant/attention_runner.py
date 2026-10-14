"""Wiring for the attention runner: the only attention code that touches live state.

The decisions live in `service/attention/` and are tested with fakes. This module supplies
the real sources (the Messages feed, the commitments store) and the real effects (the
existing verified reminder path and the event hub), and nothing more.

Nothing acts unless the user has set the mode to "live" (`/assistant/attention/settings`).
The default is "shadow": decisions are recorded in the ledger and nothing else happens.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from service.assistant.hub import hub
from service.assistant.store import assistant_store
from service.attention.detectors import local_timezone, matches_clock
from service.attention.ledger import Ledger
from service.attention.live import LOOKBACK_S, Plan, load_settings, local_midnight, settings_path
from service.attention.runner import process
from service.paths import MOE_DIR

MIN_INTERVAL_S = 120                # the scheduler ticks every 30s; parsing the feed is not free
FINGERPRINT_BUCKET_S = 900          # also recompute every 15 minutes: "soon" moves with the clock
ON_FILE_DAYS = 4                    # how far ahead the "already on the calendar?" check looks

_ledger: Ledger | None = None
_last_fingerprint: tuple | None = None
_last_run: float = 0.0
_task: "asyncio.Task | None" = None


def get_ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger(MOE_DIR / "attention" / "ledger.db")
    return _ledger


def reset() -> None:
    """For tests: forget the cached ledger and fingerprint."""
    global _ledger, _last_fingerprint, _last_run, _task
    if _task is not None and not _task.done():
        _task.cancel()
    _ledger, _last_fingerprint, _last_run, _task = None, None, 0.0, None


def current_zone() -> str | None:
    """The zone to read and file times in: the one macOS names now, or else the last zone it
    named, but only while the Mac clock still agrees with that zone's offset.

    Each zone read successfully is remembered in the ledger. If the name later cannot be read,
    the remembered one stands in; if the user has since moved (the clock's offset no longer
    matches it), there is no zone and the pass stops rather than guess."""
    tz = local_timezone()
    try:
        ledger = get_ledger()
        if tz is not None:
            if ledger.meta("last_zone") != tz:
                ledger.set_meta("last_zone", tz)
            return tz
        saved = ledger.meta("last_zone")
    except Exception:  # noqa: BLE001 — an unreadable ledger only loses the fallback
        return tz
    return saved if saved and matches_clock(saved) else None


def _policy_block(**args) -> str | None:
    """Why the safety policy forbids a reminder write right now, or None when it is ALLOW.

    The runner writes through the same door as the model's `add_reminder`, so it answers to the
    same policy: view-only mode, the remote-only profile checks, grants. Anything short of ALLOW
    (a CONFIRM has nobody to ask here) is a refusal. A policy that cannot be evaluated is one too."""
    try:
        from service.safety import Tier, decide
        dec = decide("assistant_write", args, tool="add_reminder")
    except Exception as exc:  # noqa: BLE001
        return f"safety policy unavailable ({type(exc).__name__})"
    return None if dec.tier is Tier.ALLOW else (dec.reason or dec.tier.value)


def _audit(event: str, **fields) -> None:
    from service.safety import audit
    audit(event, **fields)


def _commitments(now: float, tz: str) -> list[dict]:
    # From local midnight, not from now: `upcoming` drops anything that started more than
    # five minutes ago, which would hide today's all-day events and make a message about
    # an exam that is already on the calendar look uncaptured.
    return assistant_store.upcoming(local_midnight(now, tz), days=ON_FILE_DAYS + 1)


def _rows_near(due: float) -> dict[str, dict]:
    """Every commitment row near `due`, collapsed siblings included, keyed by its own id."""
    rows: dict[str, dict] = {}
    for head in assistant_store.upcoming(due - 120, days=2):
        for row in [head] + [assistant_store.get(i) for i in head.get("duplicate_ids") or []]:
            if row:
                rows[str(row["id"])] = row
    return rows


def _payload_action_id(plan: Plan, when_iso: str) -> str | None:
    """The deterministic action id `add_reminder` derives for this exact reminder."""
    from service.assistant.outbox import _reminder_base_action_id
    payload = {"title": plan.title.strip(), "due_ts": datetime.fromisoformat(when_iso).timestamp(),
               "commitment_kind": "reminder"}
    return _reminder_base_action_id("create_reminder", payload)


def _action_ids(base: str | None) -> set[str] | None:
    """Ids of every stored native action for this exact reminder, or None if unreadable."""
    if base is None:
        return None
    try:
        return {str(a["id"]) for a in hub.store.reminder_actions_by_prefix(base)}
    except Exception:  # noqa: BLE001
        return None


def _created_identity(plan: Plan, base: str | None, actions_before: set[str] | None,
                      before: set[str], where: str) -> dict | None:
    """The exact object this call created, or None when that cannot be proved.

    Apple Reminders: a native action row that did NOT exist before the call, whose verified
    result names the `source_id`. `add_reminder` reuses an earlier succeeded create for the same
    title and time without writing a new action, so a reused receipt leaves no new row and gets
    no identity, however recently it was written: time never proves ownership. Exactly one new
    row is required; anything else is ambiguous.
    Wisp only: the one new local row with this title and time.
    A reminder whose identity is not recorded cannot be undone through the endpoint."""
    try:
        if where == "apple_and_wisp":
            if base is None or actions_before is None:
                return None
            fresh = [a for a in hub.store.reminder_actions_by_prefix(base)
                     if str(a["id"]) not in actions_before
                     and isinstance(a.get("result"), dict) and a["result"].get("ok") is True
                     and a["result"].get("source_id")]
            ids = {str(a["result"]["source_id"]) for a in fresh}
            return {"source": "reminders", "source_id": ids.pop()} if len(fresh) == 1 and len(ids) == 1 else None
        new = [r for rid, r in _rows_near(plan.due_ts).items()
               if rid not in before and r.get("source") == "manual" and r.get("title") == plan.title.strip()
               and r.get("when_ts") is not None and abs(float(r["when_ts"]) - plan.due_ts) <= 90]
        return {"source": "manual", "id": str(new[0]["id"])} if len(new) == 1 else None
    except Exception:  # noqa: BLE001 — no identity just means no Undo; the reminder itself stands
        return None


async def create_reminder(plan: Plan) -> dict:
    """Create the reminder through the existing verified path (`add_reminder`).

    That tool already owns the hard parts: a deterministic action id (so an identical
    reminder cannot be created twice), a refusal to fall back when an earlier attempt's
    outcome is unknown, and a Wisp-only fallback when Apple Reminders declines. Its result
    is prose, so it is classified conservatively: only "Reminder set:" is success.

    On success the result also carries `created`: the identity of the object made, which is
    what Undo is bound to. A success that reused an existing reminder is still a success, but
    carries `created: None` because this call did not make it.
    """
    from service.tools.assistant_tools import add_reminder
    blocked = _policy_block(title=plan.title, kind="reminder")
    if blocked:
        # Defence in depth: the tick already checked, but view-only can be switched on mid-pass.
        _audit("deny", tool="add_reminder", source="attention", reason=blocked)
        return {"ok": False, "status": "failed", "error": f"BLOCKED by safety policy: {blocked}"}
    tz = current_zone()
    if tz is None:
        return {"ok": False, "status": "failed", "error": "The local time zone cannot be determined."}
    # With an explicit offset, add_reminder's parse is exact whatever the process's own TZ is,
    # and a time in a repeated DST hour keeps its meaning.
    when_iso = datetime.fromtimestamp(plan.due_ts, ZoneInfo(tz)).isoformat(timespec="minutes")
    try:
        base = _payload_action_id(plan, when_iso)
    except Exception:  # noqa: BLE001
        base = None
    actions_before = _action_ids(base)
    try:
        before = set(_rows_near(plan.due_ts))
    except Exception:  # noqa: BLE001
        before = set()
    try:
        text = await add_reminder(plan.title, when_iso, "reminder")
    except BaseException:
        _audit_create(plan, base, when_iso, "unknown")
        raise
    if text.startswith("Reminder set:"):
        where = "wisp_only" if "Wisp only" in text else "apple_and_wisp"
        _audit_create(plan, base, when_iso, "succeeded")
        return {"ok": True, "status": "succeeded", "where": where,
                "created": _created_identity(plan, base, actions_before, before, where)}
    lowered = text.lower()
    unknown = any(k in lowered for k in ("unconfirmed", "not verified", "nothing was confirmed"))
    status = "unknown" if unknown else "failed"
    _audit_create(plan, base, when_iso, status)
    return {"ok": False, "status": status, "error": text[:300]}


def _audit_create(plan: Plan, action_id: str | None, when_iso: str, status: str) -> None:
    """Audit one reminder write by reference, never by content.

    The title quotes the message, and the audit log is append-only and kept forever, which
    would outlive the ledger's purge of that text. The action id and a short digest of the
    title are enough to tie this line to the reminder and the ledger row while they exist."""
    _audit("allow", tool="add_reminder", source="attention", status=status,
           args={"action_id": action_id, "title_sha256": hashlib.sha256(plan.title.encode()).hexdigest()[:16],
                 "when_iso": when_iso, "kind": "reminder"})


def _name_for(handle: str) -> str | None:
    """Contact name for a phone/email handle, from the Messages contact cache (best effort)."""
    try:
        from service.tools import imessage_tools as im
        key = im._norm_handle(handle)
        if not key:
            return None
        for name, handles in im._name_handles.items():
            if any(im._norm_handle(h) == key for h in handles):
                return name.title()
    except Exception:  # noqa: BLE001 — a name is a nicety, never a reason to fail
        pass
    return None


async def _publish(event: dict) -> None:
    # Transient on purpose. A durable event is stored (with the quote and the sender) and
    # replayed to the app until it acknowledges it, and the app does not yet know this
    # type, so it never would: the rows would pile up in assistant.db without expiry.
    await hub.publish(event, durable=False)


async def run_tick(now: float | None = None) -> list:
    """One scheduler pass. Cheap when nothing changed; never raises into the scheduler."""
    global _last_fingerprint, _last_run
    now = now if now is not None else time.time()
    if now - _last_run < MIN_INTERVAL_S:
        return []
    _last_run = now
    settings = load_settings(settings_path(MOE_DIR))
    ledger = get_ledger()
    try:
        # The words of a message are only needed inside the lookback; keep the id and the state.
        ledger.purge_content(now - LOOKBACK_S, now)
    except Exception:  # noqa: BLE001 — housekeeping must never stop a pass
        pass
    if settings.mode == "off":
        ledger.set_meta("last_mode", "off")           # so resuming live re-baselines
        return []
    if settings.mode == "live":
        blocked = _policy_block()
        if blocked:
            # View-only (or a remote profile) forbids the write. Nothing is claimed or recorded:
            # the pass is treated as "off", so when writes are allowed again the switch back into
            # live re-baselines at that moment and the messages that arrived meanwhile are never
            # swept into reminders after the fact.
            if ledger.meta("last_mode") != "off":
                _audit("deny", tool="add_reminder", source="attention", reason=blocked)
            ledger.set_meta("last_mode", "off")
            return []
    tz = current_zone()
    if tz is None:
        # Every time in a message would be read in a guessed zone. Treated as "off", like a
        # policy block: when the zone is known again, live re-baselines rather than sweeping up
        # what arrived meanwhile.
        ledger.set_meta("last_mode", "off")
        return []
    from service.tools import imessage_tools
    feed = imessage_tools.structured_messages_snapshot()
    # Only a read completed in THIS launch is trusted; restored rows are not "new".
    if feed.get("state") != "ready" or (feed.get("coverage") or {}).get("status") == "unavailable":
        return []
    records = feed.get("records") or []
    fingerprint = (settings, len(records), max((r["timestamp"] for r in records), default=0),
                   int(now // FINGERPRINT_BUCKET_S))
    if fingerprint == _last_fingerprint:
        return []
    outcomes = await process(
        records=records, commitments=_commitments(now, tz), now=now, tz=tz,
        settings=settings, ledger=ledger, create=create_reminder, publish=_publish,
        fresh_commitments=lambda: _commitments(time.time(), tz), name_for=_name_for)
    if not getattr(outcomes, "limited", False):
        _last_fingerprint = fingerprint               # a limited pass has work left: look again soon
    return outcomes


def schedule_tick() -> None:
    """Start a pass in the background, at most one at a time.

    The scheduler loop also delivers the user's scheduled sends and reminders, so it must
    never wait on a reminder write (which can take 45 seconds to time out). A pass that is
    still running when the next tick arrives simply means this tick is skipped.
    """
    global _task
    if _task is not None and not _task.done():
        return

    async def _run() -> None:
        try:
            await run_tick()
        except Exception:  # noqa: BLE001 — a failed pass must never surface in the scheduler
            pass

    _task = asyncio.get_running_loop().create_task(_run())


_undoing: set[str] = set()


def _recorded_target(row: dict) -> tuple[dict | None, str | None]:
    """The one current row that IS the object recorded at creation, or why there is none."""
    created = (row["detail"].get("result") or {}).get("created")
    if not isinstance(created, dict) or created.get("source") not in ("reminders", "manual"):
        return None, "That reminder's identity was not recorded, so it cannot be removed from here."
    due = float(row["due_ts"])
    found = [r for r in _rows_near(due).values()
             if r.get("status") == "active" and r.get("source") == created["source"]
             and ((created["source"] == "reminders" and str(r.get("source_id")) == str(created.get("source_id")))
                  or (created["source"] == "manual" and str(r.get("id")) == str(created.get("id"))))]
    if not found:
        return None, "The reminder was not found. It may already be gone."
    target = found[0]
    if target.get("title") != row["title"] or target.get("when_ts") is None \
            or abs(float(target["when_ts"]) - due) > 90:
        return None, "That reminder was changed after Wisp created it; nothing was removed."
    return target, None


async def undo(source_id: str) -> dict:
    """Remove exactly the reminder this runner created for `source_id`, and nothing else.

    Only a ledger row in state `created` qualifies, and only the object whose identity was
    recorded when it was made (the native `source_id`, or the local row id). A look-alike with
    the same title and time is a different reminder and is never touched, and siblings that the
    list view collapses into the same row are not passed to the deletion. The deletion itself is
    the existing verified path (`_retire`), which reads the native item back.
    """
    blocked = _policy_block(title=source_id, kind="reminder")
    if blocked:
        _audit("deny", tool="add_reminder", source="attention_undo", reason=blocked)
        return {"ok": False, "error": f"Blocked by the safety policy: {blocked}"}
    ledger = get_ledger()
    row = ledger.get(source_id)
    if not row or row["state"] != "created":
        return {"ok": False, "error": "Wisp did not create a reminder for that message."}
    if source_id in _undoing:
        return {"ok": False, "error": "An undo for that reminder is already in progress."}
    _undoing.add(source_id)
    try:
        target, why = _recorded_target(row)
        if target is None:
            return {"ok": False, "error": why}
        from service.tools.assistant_tools import _retire
        error = await _retire({**target, "duplicate_ids": []})
        if error:
            return {"ok": False, "error": error}
        ledger.finish(source_id, "undone", {**row["detail"], "undone": True})
        _audit("allow", tool="add_reminder", source="attention_undo", args={"source_id": source_id})
    finally:
        _undoing.discard(source_id)
    try:
        await hub.publish({"type": "changed"})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}
