"""Assistant background loop.

One asyncio task (started in FastAPI lifespan, like idle_unloader). Its steady
job is the reminder engine: every tick it runs the lead-time rules over the
commitments store and publishes any due reminders to SSE subscribers.

Calendar data is fed IN by the Swift app (POST /assistant/sync/calendar) rather
than read here: macOS Calendar (TCC) permission is per-process, and the app has
the clean Wisp.app identity + Info.plist usage strings, whereas this Python
backend is a separate binary that can't get a coherent "Wisp" prompt. Future
credential-free sources (e.g. a mail IMAP connector) can still poll from here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import math
import json
import time
from datetime import date, datetime

from service.publication_authority import Authority, LIVE_CLOCK, Witness, mutating, revocation
from service import idle
from service.assistant.store import assistant_store
from service.assistant.reminders import due_reminders
from service.assistant.hub import hub

# Legacy completion date, read only for migration compatibility. New receipts
# and completion dates are committed together in AssistantStore. It lives on
# disk (not just here) because the backend is a child process of Wisp.app: every
# relaunch used to reset this, so relaunching inside the schedule's window fired
# the brief again — and right after a launch the sources are always still
# syncing, so what fired was a hold-back message announced as a finished summary.
_last_brief: date | None = None
_last_brief_loaded = False
# Ticks spent waiting for the launch sync before giving a run up. At TICK_S=30
# this is ~10 minutes, which covers a slow multi-account Mail read without
# retrying a genuinely broken source every 30s until midnight.
_MAX_BRIEF_ATTEMPTS = 20
# Keyed by (day, "morning" | "evening"): each run has its own retries.
_brief_attempts: dict[tuple[date, str], int] = {}

TICK_S = 30.0

# Server-side connectors that DON'T need macOS TCC (none yet; mail lands here).
_connectors: list = []

# Last-sync bookkeeping for sources fed by the app, surfaced in /assistant/status.
_sync_status: dict[str, dict] = {
    "calendar": {"available": False, "reason": "waiting for the app to sync", "count": 0},
    "reminders": {"available": False, "reason": "waiting for the app to sync", "count": 0},
}


# When this backend started: a source the app has never reported is measured from here.
STARTED_AT = time.time()

# Receipt authority is deliberately process-local; only record_sync can arm it.
_behavior_calendar_authority = Authority("native-calendar-permission")
_behavior_calendar_metadata = None
_behavior_calendar_expires_monotonic = 0.0


def _behavior_calendar_receipt(value):
    if type(value) is not dict:
        return None
    diagnostics = value.get("diagnostics")
    if (type(diagnostics) is not dict or value.get("available") is not True
            or value.get("syncing") is not False or diagnostics.get("authorized") is not True
            or diagnostics.get("complete", True) is not True
            or diagnostics.get("partial", False) is not False):
        return None
    last, started = value.get("last_sync"), diagnostics.get("snapshot_started_at")
    if any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in (last, started)):
        return None
    coverage = tuple(diagnostics.get(key) for key in ("coverage_start", "coverage_end"))
    if any(t is not None and (type(t) not in (int, float) or not math.isfinite(t)) for t in coverage):
        return None
    return (last, started, *coverage)


def prepare_behavior_source_publication_witness(source, snapshot) -> Witness | None:
    """Qualify copied current Calendar permission metadata, never event content.

    Preparation compares authoritative process-issued metadata outside the
    publisher lease. The resulting witness reads memory/known clocks only.
    """
    with revocation():
        return _prepare_behavior_source_publication_witness(source, snapshot)


def _prepare_behavior_source_publication_witness(source, snapshot):
    if source != "calendar":
        return None
    metadata = _behavior_calendar_receipt(snapshot)
    if (metadata is None or metadata != _behavior_calendar_metadata
            or metadata != _behavior_calendar_receipt(_sync_status.get(source))):
        return None
    return Witness.combine(_behavior_calendar_authority.token(clock=LIVE_CLOCK,
        expires_wall=min(metadata[:2])+1200,
        expires_monotonic=_behavior_calendar_expires_monotonic))


@mutating
def record_sync(source: str, count: int, diagnostics: dict | None = None) -> None:
    global _behavior_calendar_metadata, _behavior_calendar_expires_monotonic
    diagnostics = deepcopy(diagnostics or {})
    if source == "calendar":
        _behavior_calendar_authority.withdraw()
        _behavior_calendar_metadata = None
    # A completed EventKit read with authorized=false is unavailable, not a
    # successful empty calendar/reminder list.
    syncing = bool(diagnostics.get("syncing"))
    available = (not syncing and diagnostics.get("authorized") is not False
                 and diagnostics.get("available") is not False)
    reason = str(diagnostics.get("reason") or (
        "waiting for the app to sync" if syncing else "ok" if available
        else f"{source.title()} could not be read; check access in Settings"))
    now = time.time()
    previous = _sync_status.get(source) or {}
    # The app re-posts the same waiting state every minute, so the clock must keep
    # the FIRST report of an unbroken wait, or no wait could ever be seen to stall.
    since = (previous.get("syncing_since") or now) if syncing and previous.get("syncing") else (
        now if syncing else None)
    _sync_status[source] = {
        "available": available, "syncing": syncing, "reason": reason, "count": count,
        "last_sync": now, "diagnostics": diagnostics, "syncing_since": since,
    }
    if source == "calendar":
        metadata = _behavior_calendar_receipt(_sync_status[source])
        if metadata is not None and all(0 <= now-t < 1200 for t in metadata[:2]):
            _behavior_calendar_metadata = metadata
            _behavior_calendar_expires_monotonic = time.monotonic()+min(metadata[:2])+1200-now
            _behavior_calendar_authority.publish()


def connectors_status() -> dict[str, dict]:
    return deepcopy(_sync_status)  # A status caller cannot mutate the authority cache.


_behavior_task: asyncio.Task | None = None


def _start_behavior_tick() -> asyncio.Task | None:
    """Start one behavior tick in the background unless one is still running.

    While learning is off only the rate-limited retention pass runs (data already on
    disk is still aged out, as Settings says); no engine, phraser or runtime starts."""
    global _behavior_task
    from service.behavior_switch import enabled
    if _behavior_task is not None and not _behavior_task.done():
        return None
    from service.assistant import behavior_runtime
    if not enabled():
        _behavior_task = asyncio.get_running_loop().create_task(_age_out_only(behavior_runtime))
        return _behavior_task
    # behavior_runtime.tick catches every exception itself, so the task never
    # ends with an unretrieved error.
    _behavior_task = asyncio.get_running_loop().create_task(behavior_runtime.tick(assistant_store, hub))
    return _behavior_task


async def _age_out_only(behavior_runtime) -> None:
    try:
        await behavior_runtime._maybe_age_out()
    except Exception:  # noqa: BLE001 — retention is best effort and must never break the loop
        pass


async def run() -> None:
    while True:
        # server-side connectors (none today)
        for c in _connectors:
            try:
                items = await c.poll()
                assistant_store.sync_source(c.name, items)
            except Exception:  # noqa: BLE001 — a connector must never break the loop
                pass
        # fire any due reminders
        try:
            for note in due_reminders(assistant_store):
                await hub.publish(note)
        except Exception:  # noqa: BLE001
            pass
        # Behavior learning (on by default; off via Settings or WISP_BEHAVIOR=0):
        # ingest/mine/nudge on its own internal cadence; never raises. A tick
        # can take seconds (mining, phrasing), so it runs as a background
        # task and never delays reminders, the brief or scheduled sends; at
        # most one runs at a time.
        try:
            _start_behavior_tick()
        except Exception:  # noqa: BLE001 — never break the loop
            pass
        # The Daily Summary, twice a day: one morning run and one evening run
        # (config.get_daily_summary_times), each caught up on the first tick
        # after a launch or wake that missed it. The brief is a deterministic
        # render (no generation — see brief._generate_brief), but composing it
        # asks the Swift app for current Calendar, Reminders, Mail and Messages
        # reads, and a user's own mail/message question waits on those too. So
        # it runs as a single-flight background task that never starts while
        # the user is waiting and stands down if they start a turn mid-run
        # (_start_daily_brief).
        #
        # _fire_scheduled_sends below is deliberately NOT gated: it does no
        # model work, and a send the user scheduled for 6pm should go at 6pm
        # whether or not they happen to be typing.
        try:
            _start_daily_brief()
        except Exception:  # noqa: BLE001 — never break the loop
            pass
        try:
            await _fire_scheduled_sends()
        except Exception:  # noqa: BLE001
            pass
        # Codex task monitoring is a read-only local poll. It establishes a
        # baseline on first use, then publishes only terminal transitions or a
        # one-time stalled alert; ordinary progress commentary stays quiet.
        try:
            from service.codex_monitor import codex_monitor
            for event in codex_monitor.poll_events():
                await hub.publish(event)
        except Exception:  # noqa: BLE001 — Codex may not be installed/open yet
            pass
        # Proactive attention: notice a commitment in a text that is not on the calendar.
        # Mode defaults to "shadow" (records, does nothing); "live" is the user's choice.
        # No model is involved, so it needs no foreground gate.
        try:
            from service.assistant.attention_runner import schedule_tick
            schedule_tick()                  # background, single-flight: never awaited here
        except Exception:  # noqa: BLE001 — must never break the scheduler loop
            pass
        try:
            from service.assistant.day_plan_email import schedule_tick
            schedule_tick()
        except Exception:  # optional daily self-plan delivery never delays the loop
            pass
        # The Brief's MAIL and TEXTS overviews (mail_gists.py): one bounded
        # background model call at a time, only while nobody is waiting, and
        # only for a source whose mail or texts changed.
        try:
            from service.assistant import mail_gists
            mail_gists.schedule_tick()
        except Exception:  # noqa: BLE001 — a missing gist falls back to a plain sentence
            pass
        # The proactive engine (service/proactive/cycle.py): triage, judge and
        # prepare "Before you ask" cards on the resident model, single-flight,
        # only while nobody is waiting, never alongside a gist call.
        try:
            from service.proactive import cycle as proactive_cycle
            proactive_cycle.schedule_tick()
        except Exception:  # noqa: BLE001 — the Brief keeps its last cards
            pass
        # The chief-of-staff loop (service/assistant/checkin.py): carry
        # unfinished tasks onto today each morning, and in the evening ask how
        # the day went, once, when the app is listening. No model, no sends.
        try:
            await _checkin_tick()
        except Exception:  # noqa: BLE001 — never break the scheduler loop
            pass
        await asyncio.sleep(TICK_S)


async def _checkin_tick(now: float | None = None) -> None:
    from service.assistant import checkin
    from service.attention.detectors import local_timezone
    now = time.time() if now is None else now
    zone = local_timezone() or "UTC"
    day = checkin.local_day(now, zone)
    checkin.carry_forward(assistant_store, day, zone, now=now)
    if not checkin.is_due(now, zone) or not hub.has_subscribers or idle.foreground_busy():
        return
    record = checkin.day_record(day)
    if record.get("completed_at") is not None or record.get("notified_at") is not None:
        return
    card = checkin.build(assistant_store, day, zone, now=now)
    if not card["tasks"] and not card["tomorrow"]["events"]:
        checkin.mark_notified(day, now=now)   # nothing to ask about tonight
        return
    planned, done = card["planned"], card["done"]
    if planned:
        text = (f"You planned {planned} thing{'s' if planned != 1 else ''} today"
                + (f" and finished {done}" if done else "") + ". How did the rest go?")
    else:
        text = f"Tomorrow has {len(card['tomorrow']['events'])} event{'s' if len(card['tomorrow']['events']) != 1 else ''}. Anything to move?"
    from service import diagnostics
    await hub.publish({"trace_id": diagnostics.current_id(), "type": "checkin", "day": day,
                       "text": text, "planned": planned, "done": done,
                       "open_tasks": len(card["tasks"]), "events_tomorrow": len(card["tomorrow"]["events"])},
                      dedupe_key=f"checkin:{day}", target={"type": "checkin", "date": day})
    checkin.mark_notified(day, now=now)


async def _fire_scheduled_sends() -> None:
    """Deliver sends the user queued for now, and retire ones that came due
    while Wisp wasn't running.

    The user already approved each of these at scheduling time — see
    outbound_queue's docstring for why the confirmation happens there and not
    here. This function therefore sends WITHOUT prompting, which is the whole
    point, and is also why the staleness sweep matters: an unattended send is
    only acceptable when it goes out roughly when promised.
    """
    from service.assistant.outbound_queue import outbound_queue
    from service.assistant.outbox import request as app_request

    for row in outbound_queue.sweep_stale():
        # Tell the user rather than silently dropping it. A queued message that
        # never went and never said so is the worst outcome here — they think
        # it was delivered.
        await hub.publish({
            "type": "scheduled_send_missed",
            "channel": row["channel"], "display": row["display"],
            "body": row["body"], "when_ts": row["when_ts"],
        })

    # Never silently retry a send whose outcome we lost. Move it out of
    # `sending` once, then keep offering the notice until something is actually
    # connected to receive it — otherwise a recovery that happens while the app
    # is closed tells nobody, ever.
    outbound_queue.recover_in_flight()
    for row in outbound_queue.unacknowledged_unknown():
        # Republished every sweep until the app acknowledges it. `notice_id` is
        # stable across replays so the app can drop the duplicates; publishing
        # is not itself evidence that anyone received the notice.
        await hub.publish({
            "type": "scheduled_send_unknown",
            "notice_id": row["id"],
            "channel": row["channel"], "display": row["display"],
            "body": row["body"], "when_ts": row["when_ts"],
        }, dedupe_key="scheduled_unknown:" + row["id"],
           target={"type": "scheduled_unknown", "id": row["id"]})

    for row in outbound_queue.due():
        if not outbound_queue.claim(row["id"]):
            continue
        if row["channel"] == "email":
            res = await app_request("send_email", {
                "to": [row["recipient"]], "cc": [],
                "subject": row["subject"] or "", "body": row["body"],
            })
        else:
            res = await app_request("send_message",
                                    {"to": row["recipient"], "text": row["body"]})
        ok = res.get("ok") is True and ("status" not in res or res["status"] == "succeeded")
        if not ok and not (res.get("ok") is False and res.get("status") == "not_attempted"
                           and res.get("dispatch_started") is False):
            # The claim already left the pending queue. A lost/generic native
            # receipt must never invite another send or a definite failed UI.
            outbound_queue.mark(row["id"], "unknown", "Sending was not confirmed; check the sending app before retrying")
            await hub.publish({
                "type": "scheduled_send_unknown", "notice_id": row["id"],
                "channel": row["channel"], "display": row["display"],
                "body": row["body"], "when_ts": row["when_ts"],
            }, dedupe_key="scheduled_unknown:" + row["id"],
               target={"type": "scheduled_unknown", "id": row["id"]})
            continue
        outbound_queue.mark(row["id"], "sent" if ok else "failed",
                            "" if ok else str(res.get("error") or "unknown error"))
        await hub.publish({
            "type": "scheduled_send_result",
            "ok": ok, "channel": row["channel"], "display": row["display"],
            "error": "" if ok else str(res.get("error") or "unknown error"),
        })


def _brief_state_path():
    from service.paths import MOE_DIR
    return MOE_DIR / "brief_state.json"


def _brief_date() -> date | None:
    """The date the brief last went out, read from disk once per process."""
    global _last_brief, _last_brief_loaded
    if not _last_brief_loaded:
        _last_brief_loaded = True
        try:
            with _brief_state_path().open() as f:
                _last_brief = date.fromisoformat(json.load(f)["last_brief"])
        except Exception:  # noqa: BLE001 — absent or corrupt is simply "never"
            _last_brief = None
    return _last_brief


def _record_brief_date(day: date) -> None:
    global _last_brief
    _last_brief = day
    try:
        path = _brief_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump({"last_brief": day.isoformat()}, f)
    except Exception:  # noqa: BLE001 — an unwritable state file must not stop the brief
        pass




def _due_slot(now: datetime, times: dict[str, int]) -> str | None:
    """The run that is due now: the latest one whose hour has passed today.

    A run stays due until the next one starts (morning until the evening hour,
    evening until midnight), so a launch or wake at any point in that span
    catches it up. Only the latest run is ever due: waking at 9pm delivers the
    evening summary, not the morning one it also slept through, and nothing
    before the morning hour revives yesterday's evening run."""
    if now.hour >= times["evening"]:
        return "evening"
    if now.hour >= times["morning"]:
        return "morning"
    return None


def _slot_delivered(day: date, part: str) -> bool:
    """Whether this run already went out: queued for the app, or acknowledged."""
    iso = day.isoformat()
    completed = assistant_store.completion("daily_brief:" + part)
    if (completed is not None and completed >= iso) or assistant_store.event_by_key(
            f"daily_brief:{iso}:{part}"):
        return True
    # On the day of an upgrade, an older version's once-a-day receipt covers the
    # one run it made, and only that run. Its event says which run that was; the
    # AM/PM setting is only a fallback, because the notch toggle still writes it
    # and a tap after the upgrade would otherwise move the receipt to the other run.
    legacy = assistant_store.event_by_key("daily_brief:" + iso)
    if legacy is not None:
        return legacy["payload"].get("part_of_day") == part
    from service.config import get_daily_summary_hour
    if part != ("morning" if get_daily_summary_hour() < 12 else "evening"):
        return False
    completed = assistant_store.completion("daily_brief")
    return _brief_date() == day or (completed is not None and completed >= iso)


def _pending_slot(now: datetime) -> tuple[date, str] | None:
    """The run to start now, if any: due, not yet delivered, retries left."""
    from service.config import get_daily_summary_times
    part = _due_slot(now, get_daily_summary_times())
    if part is None or _slot_delivered(now.date(), part):
        return None
    if _brief_attempts.get((now.date(), part), 0) >= _MAX_BRIEF_ATTEMPTS:
        return None
    return now.date(), part


_brief_task: asyncio.Task | None = None
# How often a running brief checks whether the user started a turn. Short,
# because the brief's longest step is a source wait of up to 20 seconds.
_YIELD_POLL_S = 0.5


def _start_daily_brief() -> asyncio.Task | None:
    """Start the due Daily Summary run in the background, at most one at a time.

    Never while the user is waiting on a turn, and never before the app is
    connected: only the app can read Calendar, Reminders, Mail and Messages, so
    a run without it could only hold back, and would spend a retry doing so."""
    global _brief_task
    if _brief_task is not None and not _brief_task.done():
        return None
    if idle.foreground_busy() or not hub.has_subscribers:
        return None
    slot = _pending_slot(datetime.now())
    if slot is None:
        return None
    _brief_task = asyncio.get_running_loop().create_task(_run_daily_brief(*slot))
    return _brief_task


async def _run_daily_brief(day: date, part: str) -> None:
    """One Daily Summary run. It stands down as soon as the user starts a turn.

    Standing down is not a failed attempt: the run is still due, and the next
    idle tick starts it again. Only a run that finished without a brief (the
    sources were still syncing) uses up one of the slot's retries."""
    from service import diagnostics
    from service.assistant.brief import run_scheduled_brief
    with diagnostics.span("daily_brief"):
        run = asyncio.ensure_future(
            run_scheduled_brief(part, day=day.isoformat(), abort=idle.foreground_busy))
        yielded = False
        try:
            while not run.done():
                if idle.foreground_busy():
                    yielded = run.cancel()
                    break
                await asyncio.wait({run}, timeout=_YIELD_POLL_S)
            try:
                delivered = await run
            except asyncio.CancelledError:
                # A stand-down is a normal return, unless this task itself is
                # also being cancelled (shutdown), which must propagate.
                if yielded and run.cancelled() and not asyncio.current_task().cancelling():
                    return
                raise
            except Exception:  # noqa: BLE001 — counted as a failed attempt below
                delivered = False
        finally:
            if not run.done():
                run.cancel()
    if delivered is False:
        key = (day, part)
        _brief_attempts[key] = _brief_attempts.get(key, 0) + 1
