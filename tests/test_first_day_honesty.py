"""A new user's first Brief and Day: a source Wisp can't read says so, with the
setting that fixes it, never an all-clear; all-day markers and the end of the
working day never leave the Day empty with a reason the user can't act on; the
check-in asks only about what can be answered; and an unreachable engine is a
stand-down, not a failed attempt. Synthetic state only: no native sources,
models or network."""
from __future__ import annotations

import asyncio
from datetime import datetime
import os
import tempfile
from zoneinfo import ZoneInfo

_scratch = tempfile.TemporaryDirectory(prefix="wisp-first-day-tests-")
os.environ["WISP_HOME"] = _scratch.name

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.assistant import checkin as C
from service.assistant import daily_workspace as W
from service.assistant import scheduler
from service.assistant import sync_status as S
from service.assistant import today_api
from service.assistant.store import AssistantStore
from service.assistant.today import (build_plan, day_bounds, marker_calendar, source_access,
                                     validate_preferences, with_access)

DAY = "2026-09-24"
ZONE = "America/Los_Angeles"


def at(hour, minute=0, day=24):
    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


NOW = at(8)
PREF = dict(start_minute=540, end_minute=1080, not_before=None)


def ready(now=NOW):
    return {s: dict(available=True, last_sync=now, diagnostics=dict(coverage_start=now - 86400 * 365,
                                                                     coverage_end=now + 86400 * 30))
            for s in ("calendar", "reminders")}


def task(**values):
    return dict(id="t1", title="Study", day=DAY, kind="study", duration_minutes=60,
                priority=2, status="active", due_ts=None, pinned_start=None, revision=1) | values


def all_day(title, **values):
    lo, hi = day_bounds(DAY, ZONE)
    return dict(id="a1", source_id="a1", source="calendar", title=title, kind="event",
                when_ts=lo, end_ts=hi, all_day=True) | values


def plan(commitments=(), preferences=None, now=NOW, tasks=None):
    return build_plan(DAY, ZONE, [task()] if tasks is None else tasks, list(commitments), preferences or PREF,
                      ready(now), now=now)


@pytest.fixture
def store(tmp_path):
    s = AssistantStore(tmp_path / "assistant.db")
    yield s
    s._db.close()


# --- Can Wisp read Calendar and Reminders? -----------------------------------------


def live(**values):
    return dict(available=False, syncing=False, last_sync=NOW, syncing_since=None,
                diagnostics=dict(authorized=False)) | values


def test_denied_calendar_names_the_setting_that_fixes_it():
    access = source_access("calendar", live(diagnostics=dict(authorized=False, status=2)), now=NOW, started_at=NOW - 600)
    assert access["state"] == "unavailable" and access["blocked"] == "calendar_access"
    assert "Privacy & Security > Calendars" in access["reason"] and "off" in access["reason"]


def test_add_only_calendar_access_is_not_reading_access():
    access = source_access("calendar", live(diagnostics=dict(authorized=False, status=4)), now=NOW, started_at=NOW - 600)
    assert access["blocked"] == "calendar_access" and "only add" in access["reason"]


def test_an_unanswered_prompt_is_a_short_wait_then_a_next_step():
    waiting = live(syncing=True, syncing_since=NOW - 10, diagnostics=dict(authorized=False, syncing=True, status=0))
    assert source_access("calendar", waiting, now=NOW, started_at=NOW - 600)["state"] == "syncing"
    stalled = source_access("calendar", waiting, now=NOW + 120, started_at=NOW - 600)
    assert stalled["state"] == "unavailable" and stalled["blocked"] == "calendar_access"
    # The pane may not list Wisp yet: say to quit and reopen, never only "turn Wisp on".
    assert "quit and reopen Wisp" in stalled["reason"]


def test_the_readers_own_reason_kind_and_sentence_win():
    reported = live(diagnostics=dict(authorized=False, reason_kind="reminders_access",
                                     reason="Reminders access is turned off for Wisp."))
    access = source_access("reminders", reported, now=NOW, started_at=NOW - 600)
    assert access == dict(state="unavailable", blocked="reminders_access",
                          reason="Reminders access is turned off for Wisp.")


def test_an_app_that_never_reports_is_not_a_permission_problem():
    assert source_access("calendar", None, now=NOW, started_at=NOW - 10)["state"] == "syncing"
    gone = source_access("calendar", None, now=NOW, started_at=NOW - 600)
    assert gone["state"] == "unavailable" and gone["blocked"] == "app_not_reporting"


def test_a_partial_authorized_read_is_unavailable_without_a_setting():
    access = source_access("reminders", live(diagnostics=dict(authorized=True, available=False,
                                                              reason="Reminders lists looked empty")),
                           now=NOW, started_at=NOW - 600)
    assert access == dict(state="unavailable", blocked=None, reason="Reminders lists looked empty")


def test_with_access_leaves_a_ready_source_alone_and_explains_the_rest():
    health = {"calendar": dict(ready=True, last_sync=NOW, reason="Up to date"),
              "reminders": dict(ready=False, last_sync=None, reason="Waiting for the app to sync")}
    out = with_access(health, {"reminders": live(diagnostics=dict(authorized=False, status=2))},
                      now=NOW, started_at=NOW - 600)
    assert out["calendar"] == dict(ready=True, last_sync=NOW, reason="Up to date", state="ready", blocked=None)
    assert out["reminders"]["state"] == "unavailable" and out["reminders"]["blocked"] == "reminders_access"
    assert out["reminders"]["reason"].startswith("Reminders access is off for Wisp.")


def test_sync_status_uses_the_same_words_and_kind(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(scheduler, "_sync_status", {})
    monkeypatch.setattr(scheduler.time, "time", lambda: clock["now"])
    monkeypatch.setattr(S.time, "time", lambda: clock["now"])
    monkeypatch.setattr(scheduler, "STARTED_AT", clock["now"])
    scheduler.record_sync("calendar", 0, diagnostics={"authorized": False, "status": 2, "snapshot_started_at": 1.0})
    status = S.source_status("calendar")
    assert status["state"] == "unavailable" and status["reason_kind"] == "calendar_access"
    assert "Privacy & Security > Calendars" in status["reason"]


def test_the_today_snapshot_carries_state_and_blocked(store, monkeypatch):
    monkeypatch.setattr(today_api, "assistant_store", store)
    monkeypatch.setattr(today_api.time, "time", lambda: NOW)
    monkeypatch.setattr(scheduler, "_sync_status", {"calendar": live(diagnostics=dict(authorized=False, status=2))})
    monkeypatch.setattr(scheduler, "STARTED_AT", NOW - 600)
    result = today_api.snapshot(DAY, ZONE)
    assert result["sources"]["calendar"]["blocked"] == "calendar_access"
    assert result["sources"]["calendar"]["state"] == "unavailable"
    # The workspace's coverage says the same, so the Brief's rows agree.
    coverage = {row["source"]: row for row in result["workspace"]["source_coverage"]}
    assert coverage["calendar"]["blocked"] == "calendar_access" and coverage["calendar"]["state"] == "unavailable"
    assert coverage["calendar"]["reason"].startswith("Calendar access is off for Wisp.")


# --- Planner: all-day markers and the end of the working day -----------------------


def test_a_holiday_or_birthday_calendar_never_reserves_the_day():
    assert marker_calendar({"context": "US Holidays"}) and marker_calendar({"context": "Birthdays"})
    assert not marker_calendar({"context": "Work"})
    p = plan([all_day("Moving Day", context="US Holidays")])
    assert p["blocks"] and not p["unscheduled"] and not any("All-day" in w for w in p["warnings"])


def test_plan_my_day_anyway_lifts_a_time_away_marker():
    away = [all_day("Vacation in Tahoe")]
    held = plan(away)
    assert held["unscheduled"][0]["reason_code"] == "all_day" and not any(b.get("task_id") for b in held["blocks"])
    assert not any("pin a task to explicitly" in w for w in held["warnings"])
    anyway = plan(away, preferences=PREF | dict(plan_all_day=True))
    assert any(b.get("task_id") == "t1" for b in anyway["blocks"]) and not anyway["unscheduled"]


def test_plan_all_day_is_stored_only_when_chosen():
    assert "plan_all_day" not in validate_preferences(DAY, ZONE, **PREF)
    assert validate_preferences(DAY, ZONE, **PREF, plan_all_day=True)["plan_all_day"] is True
    with pytest.raises(ValueError):
        validate_preferences(DAY, ZONE, **PREF, plan_all_day="yes")


def test_a_preference_key_from_a_newer_build_is_ignored_on_read():
    """Rollback safety: a stored payload carrying a key this build doesn't know
    still plans (the way builds before plan_all_day crashed on it)."""
    p = plan(preferences=PREF | dict(focus_mode="deep", plan_all_day=False))
    assert p["preferences"] == PREF and any(b.get("task_id") == "t1" for b in p["blocks"])
    # A wrongly typed known key is still refused, and writes stay strict.
    with pytest.raises(ValueError):
        plan(preferences=PREF | dict(start_minute="9"))
    with pytest.raises(TypeError):
        validate_preferences(DAY, ZONE, **PREF, focus_mode="deep")


def test_after_working_hours_says_so_instead_of_a_zero_minute_gap():
    p = plan(now=at(19))
    [held] = p["unscheduled"]
    assert held["reason_code"] == "after_hours" and held["reason"] == "It's after your working hours"
    assert held["largest_gap_minutes"] is None
    # Stretching today's hours to midnight places it.
    tonight = plan(now=at(19), preferences=PREF | dict(end_minute=1440))
    assert any(b.get("task_id") == "t1" for b in tonight["blocks"])


def test_replan_accepts_plan_all_day(store, monkeypatch):
    monkeypatch.setattr(today_api, "assistant_store", store)
    monkeypatch.setattr(today_api.time, "time", lambda: NOW)
    app = FastAPI()
    app.include_router(today_api.router)
    with TestClient(app) as client:
        reply = client.post("/assistant/today/replan",
                            json=dict(day=DAY, timezone=ZONE, revision=0, plan_all_day=True))
        assert reply.status_code == 200 and reply.json()["preferences"]["plan_all_day"] is True
        assert client.post("/assistant/today/replan",
                           json=dict(day=DAY, timezone=ZONE, revision=1, plan_all_day="yes")).status_code == 422


# --- The Brief's coverage and Needs you ----------------------------------------------


def workspace_state():
    return dict(tasks=[], commitments=[], preferences=PREF,
                sources={s: dict(available=True, last_sync=NOW, snapshot_started_at=NOW,
                                 diagnostics=dict(coverage_start=at(0), coverage_end=at(0, day=25)))
                         for s in ("calendar", "reminders")})


def test_coverage_carries_what_blocks_a_source_and_why():
    evidence = {"calendar": dict(ready=True, records=[], detail="ok"),
                "email": dict(ready=False, records=[], detail="x", state="unavailable", blocked="mail_automation",
                              reason="Allow Wisp to control Mail."),
                "messages": dict(ready=False, records=[], detail="x", blocked="full_disk_access")}
    result = W.assess(DAY, ZONE, workspace_state(), evidence, now=NOW)
    rows = {row["source"]: row for row in result["source_coverage"]}
    assert rows["email"]["blocked"] == "mail_automation" and rows["email"]["reason"] == "Allow Wisp to control Mail."
    assert rows["messages"]["blocked"] == "full_disk_access" and rows["messages"]["state"] == "unavailable"
    assert "state" not in rows["calendar"] and "blocked" not in rows["calendar"]


def test_annotate_access_explains_unread_calendar_and_reminders(monkeypatch):
    monkeypatch.setattr(scheduler, "_sync_status", {"calendar": live(diagnostics=dict(authorized=False, status=2)),
                                                    "reminders": live(available=True, diagnostics=dict(authorized=True))})
    monkeypatch.setattr(scheduler, "STARTED_AT", NOW - 600)
    evidence = {"calendar": dict(ready=False, records=[]), "reminders": dict(ready=False, records=[])}
    W._annotate_access(evidence, NOW)
    assert evidence["calendar"]["blocked"] == "calendar_access" and evidence["calendar"]["state"] == "unavailable"
    # Reminders' reader is fine; its receipt is just not current yet: still reading.
    assert evidence["reminders"]["state"] == "syncing" and "blocked" not in evidence["reminders"]


def test_people_the_user_named_rise_to_the_top_of_needs_you():
    attention = [
        dict(id="reminders:a", source="reminders", title="Pay rent", due_ts=NOW - 3600, reason="Due."),
        dict(id="email:dana", source="email", title="Lunch?", sender="Dana", person=True, received_ts=NOW - 60,
             due_ts=None, reason="Review"),
        dict(id="email:mom", source="email", title="Call me", sender="Mom", person=True, received_ts=NOW - 7200,
             due_ts=None, reason="Review", profile_people=["Mom"]),
    ]
    picked = W.needs_you(attention, now=NOW, day_end=at(0, day=25))["items"]
    assert [item["id"] for item in picked] == ["email:mom", "reminders:a", "email:dana"]
    assert all("vip" not in item for item in picked)


def test_the_proactive_brief_never_says_nothing_on_an_unread_calendar():
    from service.proactive import cycle
    view = {"summary": cycle.NO_EVENTS_SENTENCE + " Two people are waiting on a reply.",
            "lede": cycle.NO_EVENTS_SENTENCE}
    out = cycle.honest(view, [dict(source="calendar", ready=False), dict(source="email", ready=True)])
    assert out["unread"] == ["calendar"]
    assert out["lede"] == cycle.CALENDAR_UNREAD_SENTENCE
    assert out["summary"].startswith(cycle.CALENDAR_UNREAD_SENTENCE)
    same = cycle.honest({"summary": cycle.NO_EVENTS_SENTENCE}, [dict(source="calendar", ready=True)])
    assert same["summary"] == cycle.NO_EVENTS_SENTENCE and same["unread"] == [] and same["mail_read_by"] == []


def test_mail_counts_as_read_only_through_a_connected_fresh_google_account(monkeypatch):
    from service import gmail_source
    from service.proactive import cycle
    coverage = [dict(source="email", ready=False), dict(source="messages", ready=False)]
    assert cycle.honest({}, coverage)["unread"] == ["email", "messages"]
    read = cycle.honest({}, coverage, ["ada@gmail.com"])
    assert read["unread"] == ["messages"] and read["mail_read_by"] == ["ada@gmail.com"]
    rows = [dict(email="ok@gmail.com", state="connected", fresh=True),
            dict(email="stale@gmail.com", state="connected", fresh=False),
            dict(email="expired@gmail.com", state="expired", fresh=True),
            dict(email="failing@gmail.com", state="attention", fresh=True),
            dict(email="old@gmail.com", state="needs_reconnect", fresh=True)]
    monkeypatch.setattr(gmail_source, "status", lambda: {"accounts": rows})
    assert cycle.google_mail_read() == ["ok@gmail.com"]
    def broken():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(gmail_source, "status", broken)
    assert cycle.google_mail_read() == [], "unknown Google status is never a read"


# --- The evening check-in on day one --------------------------------------------------


@pytest.fixture
def checkin_state(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_PATH", tmp_path / "checkins.json")


def test_check_in_skips_read_only_events_and_marker_all_day_ones(store, checkin_state):
    tomorrow = "2026-09-25"
    lo, hi = day_bounds(tomorrow, ZONE)
    store.sync_source("calendar", [
        dict(id="bday", source_id="bday", source="calendar", title="Sam's birthday", kind="event",
             when_ts=lo, end_ts=hi, all_day=True, calendar_writable=True, context="Birthdays"),
        # The person's own all-day event can still be cancelled: it stays.
        dict(id="off", source_id="off", source="calendar", title="Day off", kind="event",
             when_ts=lo, end_ts=hi, all_day=True, calendar_writable=True, context="Home"),
        dict(id="holiday", source_id="holiday", source="calendar", title="Columbus Day", kind="event",
             when_ts=lo, end_ts=hi, all_day=True, context="US Holidays"),
        dict(id="course", source_id="course", source="calendar", title="Week 4", kind="event",
             when_ts=lo, end_ts=hi, all_day=True, calendar_writable=False, context="STAT 5"),
        dict(id="lecture", source_id="lecture", source="calendar", title="STAT 5 lecture", kind="event",
             when_ts=at(10, day=25), end_ts=at(11, day=25), all_day=False, calendar_writable=False),
        dict(id="gym", source_id="gym", source="calendar", title="Gym", kind="event",
             when_ts=at(17, day=25), end_ts=at(18, day=25), all_day=False, calendar_writable=True),
    ])
    card = C.build(store, DAY, ZONE, now=at(18))
    assert [e["title"] for e in card["tomorrow"]["events"]] == ["Day off", "Gym"]
    assert C.askable_event(dict(all_day=True, calendar="Birthdays")) is False
    assert C.askable_event(dict(all_day=True, calendar="Personal", calendar_writable=None)) is True


def test_the_first_check_in_says_what_it_is_for_once(store, checkin_state):
    assert C.build(store, DAY, ZONE, now=at(18))["first"] is True
    C.mark_notified(DAY, now=at(18))
    assert C.build(store, DAY, ZONE, now=at(18))["first"] is True, "today's own notice doesn't count"
    assert C.build(store, "2026-09-25", ZONE, now=at(18, day=25))["first"] is False


# --- An unreachable engine is a stand-down ---------------------------------------------


class _Unreachable:
    def __init__(self, at_step):
        self.at_step = at_step

    async def loaded_models(self):
        if self.at_step == "loaded":
            import httpx
            raise httpx.ConnectError("refused")
        return ["fast"]

    def keep_warm(self):
        return set()

    async def chat(self, model, messages, **kwargs):
        from service.inference.inference_errors import EngineGuardUnavailableError
        raise EngineGuardUnavailableError()


def _hung(kind):
    import httpx
    return {"connect": httpx.ConnectError("refused"), "read": httpx.ReadTimeout("hung"),
            "connect_timeout": httpx.ConnectTimeout("hung"), "pool": httpx.PoolTimeout("busy")}[kind]


class _Raising:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    async def loaded_models(self):
        return ["fast"]

    def keep_warm(self):
        return set()

    async def chat(self, model, messages, **kwargs):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize("kind", ["connect", "read", "connect_timeout", "pool"])
def test_a_hung_or_gone_engine_leaves_the_attempts_alone_and_retries(monkeypatch, tmp_path, kind):
    """refresh(): an engine that is gone or hung is a stand-down. No attempt is
    spent (a job has only _MAX_ATTEMPTS) and the next tick is scheduled."""
    import service.config as config
    from service.assistant import brief
    from service.assistant import mail_gists as G
    from pathlib import Path
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(config, "no_thinking_capable", lambda: ["fast"])
    monkeypatch.setattr(config, "thinks_explicitly", lambda name: False)
    monkeypatch.setattr(G, "_PATH", Path(tmp_path) / "cache" / "mail_gists.json")
    monkeypatch.setattr(G, "_cache", None)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    monkeypatch.setattr(G, "_attempts", {})
    monkeypatch.setattr(G, "_retry_at", {})
    monkeypatch.setattr(G, "_last_standdown", "")
    when = 1_790_000_000.0
    monkeypatch.setattr(G, "recent_mail", lambda now: [dict(id="m1", subject="Tech Week starts today", sender="Ann",
                                                            sender_address="ann@example.com", ts=when - 60)])
    monkeypatch.setattr(G, "recent_texts", lambda now: [])
    monkeypatch.setattr(G, "summary_lines", lambda rows: [])
    client = _Raising(_hung(kind))
    monkeypatch.setattr(brief, "_c", lambda: client)
    assert asyncio.run(G.refresh(when, abort=lambda: False)) is False
    assert client.calls == 1, "the call was made and failed, not skipped"
    assert G._attempts == {} and G._retry_at == {}
    assert G._next_call_at == when + G._IDLE_GAP_S


@pytest.mark.parametrize("step", ["loaded", "chat"])
def test_an_unreachable_engine_stands_down_without_an_attempt(monkeypatch, step):
    import service.config as config
    from service.assistant import brief
    from service.assistant import mail_gists as G
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(config, "no_thinking_capable", lambda: ["fast"])
    monkeypatch.setattr(config, "thinks_explicitly", lambda name: False)
    monkeypatch.setattr(brief, "_c", lambda: _Unreachable(step))
    assert asyncio.run(G._resident_reply("s", "m", lambda: False)) is None


# --- The readers' reason and reason_kind reach every row -----------------------------
#
# Agreed values: reason_kind in {calendar_access, reminders_access,
# full_disk_access, mail_automation, mail_not_configured}, with the reader's own
# sentence as `reason`.

READER_KINDS = ("calendar_access", "reminders_access", "full_disk_access", "mail_automation", "mail_not_configured")


def _handler(name):
    """One sync handler from service/main.py, compiled alone: importing
    service.main would start unrelated app machinery."""
    import ast
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "service/main.py"
    node = next(n for n in ast.parse(path.read_text()).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    namespace = {"Any": object}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(scheduler, "_sync_status", {})
    monkeypatch.setattr(scheduler.time, "time", lambda: now["t"])
    monkeypatch.setattr(S.time, "time", lambda: now["t"])
    monkeypatch.setattr(scheduler, "STARTED_AT", now["t"])
    return now


@pytest.mark.parametrize("source,kind", [("calendar", "calendar_access"), ("reminders", "reminders_access")])
def test_a_reader_posted_kind_and_reason_reach_sync_status(clock, source, kind):
    reason = f"{source.title()} access is turned off for Wisp. Turn it on in System Settings."
    scheduler.record_sync(source, 0, diagnostics={"authorized": False, "syncing": False, "reason": reason,
                                                  "reason_kind": kind, "snapshot_started_at": clock["t"]})
    status = S.source_status(source)
    assert status == dict(status, state="unavailable", reason=reason, reason_kind=kind)


def test_the_backend_waits_out_the_readers_prompt_window(clock):
    """The readers report "syncing" for up to 120 s while a prompt is open; the
    backend's own stall guess must not win in between."""
    from service.assistant.today import ACCESS_STALL_S
    assert S.SYNC_STALL_S > 120 and ACCESS_STALL_S == S.SYNC_STALL_S
    waiting = {"authorized": False, "syncing": True, "snapshot_started_at": clock["t"]}
    scheduler.record_sync("calendar", 0, diagnostics=waiting)
    clock["t"] += 110
    scheduler.record_sync("calendar", 0, diagnostics=waiting)
    assert S.source_status("calendar")["state"] == "syncing"
    live = scheduler.connectors_status()["calendar"]
    assert source_access("calendar", live, now=clock["t"], started_at=scheduler.STARTED_AT)["state"] == "syncing"
    # At 120 s the reader posts its own reason; that is what shows.
    clock["t"] += 12
    reason = "The Calendar permission prompt is still waiting for an answer. Look for it, or allow Wisp in Settings."
    scheduler.record_sync("calendar", 0, diagnostics={"authorized": False, "syncing": False, "reason": reason,
                                                      "reason_kind": "calendar_access"})
    assert S.source_status("calendar")["reason"] == reason


@pytest.fixture
def mail_state(monkeypatch, tmp_path):
    from service.tools import cache_store
    from service.tools import email_tools as E
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(E, "_save_email_sources", lambda: None)
    for name in ("_email_available", "_email_reason", "_email_reason_kind", "_email_sync_pending",
                 "_email_read_source", "_native_headers_at", "_native_source", "_external_accounts"):
        monkeypatch.setattr(E, name, getattr(E, name))
    monkeypatch.setattr(E, "_external_accounts", {})
    # set_email_availability publishes or withdraws the raw-mail authority: put
    # its exact head back, so no later test sees this one's availability.
    monkeypatch.setattr(E._raw_availability, "_head", E._raw_availability._head)
    return E


@pytest.mark.parametrize("kind", READER_KINDS)
def test_the_email_handler_keeps_the_kind_on_the_email_row(mail_state, kind):
    E = mail_state
    handler = _handler("assistant_sync_emails")
    reason = "Wisp needs your permission to read Mail."
    available = kind == "mail_not_configured"   # zero accounts: the script ran fine
    assert asyncio.run(handler({"diagnostics": {"available": available, "reason": reason,
                                                "reason_kind": kind}})) == {"ok": True}
    row = S.source_status("email")
    assert row["reason_kind"] == kind and E.email_reason_kind() == kind and row["reason"] == reason
    # A kind that isn't a plain identifier is no kind.
    asyncio.run(handler({"diagnostics": {"available": False, "reason": reason, "reason_kind": "<b>x</b>"}}))
    assert S.source_status("email")["reason_kind"] == ""


def test_no_inbox_message_has_no_doubled_period_and_names_missing_accounts(mail_state):
    E = mail_state
    E.set_email_availability(False, "Wisp needs your permission to control Mail.", reason_kind="mail_automation")
    message = E._no_inbox_message()
    assert ".." not in message and ". ." not in message and "control Mail. No usable" in message
    assert "Automation" in message
    E.set_email_availability(True, "There are no Mail accounts on this Mac.", reason_kind="mail_not_configured")
    message = E._no_inbox_message()
    assert message.startswith("There are no Mail accounts on this Mac.") and "Internet Accounts" in message


def test_a_ready_row_with_no_mail_account_is_not_an_all_clear(mail_state, monkeypatch):
    E = mail_state
    monkeypatch.setattr(scheduler, "_sync_status", {})
    E.set_email_availability(True, "There are no Mail accounts on this Mac.", reason_kind="mail_not_configured")
    monkeypatch.setattr(E, "email_sync_state", lambda: "ready")
    monkeypatch.setattr(E, "email_source_details", lambda *a, **k: [])
    evidence = {"email": dict(ready=True, records=[])}
    W._annotate_access(evidence, NOW)
    assert evidence["email"]["ready"] is False and evidence["email"]["blocked"] == "mail_not_configured"
    assert evidence["email"]["reason"] == "There are no Mail accounts on this Mac."
    # With a Google account supplying mail, the same row is a real read.
    monkeypatch.setattr(E, "_external_accounts", {"a@gmail.com": {}})
    evidence = {"email": dict(ready=True, records=[])}
    W._annotate_access(evidence, NOW)
    assert evidence["email"]["ready"] is True and "blocked" not in evidence["email"]


@pytest.fixture
def notes_state(monkeypatch):
    from service.tools import cache_store
    from service.tools import notes_tools as N
    monkeypatch.setattr(cache_store, "save", lambda name, text: None)
    monkeypatch.setattr(N._publication_authority, "_head", N._publication_authority._head)
    for name in ("_notes", "_notes_at", "_available", "_reason", "_reason_kind", "_snapshot_at",
                 "_snapshot_started_at", "_received_at", "_receipt_started", "_receipt_available",
                 "_receipt_reason", "_notes_generation", "_issued_snapshot"):
        monkeypatch.setattr(N, name, getattr(N, name))
    return N


def test_the_notes_handler_keeps_the_reason_and_kind(notes_state):
    handler = _handler("assistant_sync_notes")
    reason = "Wisp reads Notes while Notes is open. Open Notes, or give Wisp Full Disk Access."
    assert asyncio.run(handler({"raw": "", "diagnostics": {"available": False, "reason": reason,
                                                           "reason_kind": "full_disk_access"}})) == {"ok": True}
    row = S.source_status("notes")
    assert row["state"] == "unavailable" and row["reason"] == reason and row["reason_kind"] == "full_disk_access"
    # Readable again: no stale reason or kind.
    asyncio.run(handler({"raw": "", "diagnostics": {"available": True}}))
    row = S.source_status("notes")
    assert row["state"] == "ready" and row["reason"] == "" and row["reason_kind"] == ""
