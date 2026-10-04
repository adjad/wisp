"""Study availability and assent regressions; synthetic sources and fixed clocks only."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import service.tools  # noqa: F401
from service.router import router as R
from service.tools import schedule_extras as S


class Saturday(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 3, 22, 57)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(S, "dt", SimpleNamespace(datetime=Saturday, date=dt.date,
        time=dt.time, timedelta=dt.timedelta))
    monkeypatch.setattr("service.tools.timeranges.datetime", Saturday)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,minutes", [
    ("Plan a 90-minute homework task for me tmrow", 90),
    ("find time for me to study tmrow for a zybook assignment", 30),
    ("find me a 90 minute study slot tomorrow", 90),
    ("find an hour to study tomorrow", 60),
    ("find two hours to study tomorrow", 120),
    ("when can I study for an hour tomorrow?", 60),
])
async def test_exact_study_route(text, minutes, clock):
    d = await R.route(text)
    assert d.direct_calls == [("find_free_time", {"period": "2026-10-04", "minutes": minutes})]
    assert d.verified_results_only
    assert "Sunday, 2026-10-04" in d.resolved_request
    assert {"recall", "add_reminder", "add_calendar_event", "search_notes", "run_shell"} <= d.forbidden_tools


@pytest.mark.asyncio
async def test_duration_from_immediate_study_request_only(clock):
    d = await R.route("find time for me to study tmrow for a zybook assignment",
        last_user="Plan a 90-minute homework task for me tmrow")
    assert d.direct_calls == [("find_free_time", {"period": "2026-10-04", "minutes": 90})]
    d = await R.route("find time for me to study tmrow", last_user="Show a 90-minute movie")
    assert d.direct_calls == [("find_free_time", {"period": "2026-10-04", "minutes": 30})]


@pytest.mark.asyncio
async def test_calendar_exclusion_blocks_availability(clock):
    d = await R.route("don't read my calendar; find an hour to study tomorrow")
    assert d.direct_calls == []
    assert "find_free_time" in d.forbidden_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    'Create a note saying "find an hour to study tomorrow"',
    "find an hour to study tomorrow and text Mom",
    "find public study plans for tomorrow", "schedule an hour of study tomorrow",
    "find a 0-minute study slot tomorrow", "find a 2000-minute study slot tomorrow",
])
async def test_non_read_and_invalid_duration_not_preexecuted(text):
    with patch.object(R, "_semantic_core", new=AsyncMock(return_value=["calculate"])):
        d = await R.route(text)
    assert not any(name == "find_free_time" for name, _ in d.direct_calls)


def stamp(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute).timestamp()


async def availability(rows, tasks=(), **kwargs):
    store = SimpleNamespace(today_snapshot=lambda day, timezone: {"commitments": rows, "tasks": tasks})
    ready = {"sources": [{"id": "calendar", "label": "Calendar", "state": "ready"},
                         {"id": "reminders", "label": "Reminders", "state": "ready"}],
             "reminders_fresh": True, "syncing": False}
    with patch.object(S, "_store", store), patch("service.assistant.sync_status.ensure_sources",
            new=AsyncMock(return_value=ready)):
        return await S.find_free_time(period="tomorrow", minutes=90, **kwargs)


@pytest.mark.asyncio
async def test_one_day_concrete_duration(clock):
    result = await availability([])
    assert "Sunday, 2026-10-04" in result
    assert "9:00 AM–10:30 AM" in result
    assert "Monday" not in result
    assert "not been created" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("end_minute,qualifies", [(30, True), (31, False)])
async def test_exact_length_boundary(clock, end_minute, qualifies):
    rows = [{"when_ts": stamp(4, 0), "end_ts": stamp(4, 16, end_minute),
             "source": "calendar", "status": "active"}]
    result = await availability(rows)
    assert ("4:30 PM–6:00 PM" in result) is qualifies
    if not qualifies:
        assert "No gaps" in result


@pytest.mark.asyncio
async def test_outside_window_and_midnight_cannot_widen_slot(clock):
    rows = [{"when_ts": stamp(4, 20), "end_ts": stamp(4, 21), "source": "calendar"},
            {"when_ts": stamp(5, 0), "end_ts": stamp(5, 1), "source": "calendar"}]
    result = await availability(rows)
    assert "9:00 AM–10:30 AM" in result
    assert "8:00 PM" not in result


@pytest.mark.asyncio
async def test_overnight_overlap_and_all_day(clock):
    result = await availability([{"when_ts": stamp(3, 22), "end_ts": stamp(4, 11),
                                  "source": "calendar"}])
    assert "11:00 AM–12:30 PM" in result
    result = await availability([{"when_ts": stamp(4, 0), "all_day": True,
                                  "source": "calendar"}])
    assert "No gaps" in result


@pytest.mark.asyncio
async def test_unavailable_source_does_not_claim_free_time(clock):
    with patch("service.assistant.sync_status.ensure_sources", new=AsyncMock(return_value={
            "sources": [{"id": "calendar", "label": "Calendar", "state": "unavailable"}],
            "syncing": False})):
        result = await S.find_free_time(period="tomorrow", minutes=90)
    assert "could not check" in result
    assert "9:00 AM" not in result


GOOD_OFFER = ("Study candidate: Sunday, 2026-10-04, 9:00 AM–10:30 AM. "
              "Would you like me to set a reminder at 8:30 AM tomorrow "
              "so you have time to prep before the study session?")


@pytest.mark.asyncio
async def test_single_grounded_study_reminder_confirmation(clock):
    d = await R.route("sure, on 2026-10-04", last_user="find time for me to study tmrow for a zybook assignment",
                      last_assistant=GOOD_OFFER, last_tools="find_free_time")
    assert d.tool_subset == ["add_reminder"]
    assert d.direct_calls == []  # stays in ordinary model/tool approval execution
    assert d.reminder_action == "create"
    assert d.tool_argument_bindings["add_reminder"]["when_iso"] == "2026-10-04T08:30"
    assert d.required_tool_groups == (frozenset({"add_reminder"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("offer", [
    GOOD_OFFER + " Would you like me to email Mom too?",
    GOOD_OFFER.replace("so you have time", "and email Mom so you have time"),
    "I already set the study reminder. " + GOOD_OFFER,
    'The sample invitation says "' + GOOD_OFFER + '"',
])
async def test_unsafe_offers_expose_no_write(offer, clock):
    d = await R.route("sure, on 2026-10-04", last_user="find time for me to study tmrow",
                      last_assistant=offer, last_tools="find_free_time")
    assert "add_reminder" not in (d.tool_subset or [])
    assert d.direct_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [
    ("find time to study tomorrow for math but remind me at 8", None),
    ("find time to study tomorrow for math plus create a calendar event", None),
    ("find time to study tomorrow for math except after 5 pm", None),
    ("find time to study tomorrow for a zybook assignment no reminders", None),
    ("find time to study tomorrow for no reminder", None),
    ("find time to study tomorrow for 90 minutes", 90),
])
async def test_tail_constraints_are_not_dropped(text, expected, clock):
    with patch.object(R, "_semantic_core", new=AsyncMock(return_value=["calculate"])):
        d = await R.route(text)
    calls = [(name, args) for name, args in d.direct_calls if name == "find_free_time"]
    assert calls == ([] if expected is None else [("find_free_time", {"period": "2026-10-04", "minutes": expected})])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["I've already set your study reminder.", "Your study reminder is set.",
    "Reminder created.", "I have scheduled the preparation reminder.", "Your study reminder is already set.",
    "I have already set up your reminder."])
async def test_completed_offer_does_not_replay(status, clock):
    d = await R.route("sure, on 2026-10-04", last_user="find time to study tomorrow",
                      last_assistant=status + " " + GOOD_OFFER)
    assert "add_reminder" not in (d.tool_subset or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("next_day", [4, 5])
async def test_wrong_monday_offer_cannot_become_authorized_after_midnight(next_day, clock):
    offer = GOOD_OFFER.replace("Sunday, 2026-10-04", "Monday, 2026-10-05")
    with patch("service.tools.timeranges.resolve_span", return_value=(stamp(next_day, 0), stamp(next_day + 1, 0), "tomorrow")):
        d = await R.route("sure", last_user="find time to study tmrow", last_assistant=offer)
    assert d.tool_subset == []
    assert "complete reminder command" in d.resolved_request


@pytest.mark.parametrize("zone,month,day", [
    ("UTC", 10, 3), ("America/Los_Angeles", 10, 3), ("Pacific/Kiritimati", 10, 3),
    ("America/Los_Angeles", 3, 7), ("America/Los_Angeles", 10, 31),
])
def test_real_snapshot_local_zone_and_dst(tmp_path, zone, month, day):
    import os
    import subprocess
    import sys
    script = f'''
import asyncio, datetime as dt, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
time.tzset()
from service.tools import schedule_extras as S
class Fixed(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, {month}, {day}, 22, 57)
S.dt = SimpleNamespace(datetime=Fixed, date=dt.date, time=dt.time, timedelta=dt.timedelta)
tomorrow = Fixed.now().date() + dt.timedelta(days=1)
start = dt.datetime.combine(tomorrow, dt.time(9)).timestamp()
S._store.sync_source("calendar", [{{"source_id":"synthetic-event", "kind":"event", "title":"Synthetic busy", "when_ts":start, "end_ts":dt.datetime.combine(tomorrow, dt.time(11)).timestamp()}}])
ready = {{"sources":[{{"id":"calendar","label":"Calendar","state":"ready"}}], "reminders_fresh":True}}
with patch("service.assistant.sync_status.ensure_sources", new=AsyncMock(return_value=ready)):
    result = asyncio.run(S.find_free_time(period="tomorrow", minutes=90))
assert tomorrow.isoformat() in result, result
assert "11:00 AM–12:30 PM" in result, result
assert "error" not in result.lower(), result
S._store._db.close()
'''
    result = subprocess.run([sys.executable, "-c", script], env={**os.environ,
        "TZ": zone, "WISP_HOME": str(tmp_path)}, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_unknown_end_duration_is_disclosed(clock):
    result = await availability([{"when_ts": stamp(4, 9), "source": "manual"}])
    assert "10:00 AM–11:30 AM" in result
    assert "assumed to run an hour" in result


@pytest.mark.asyncio
async def test_pinned_today_task_blocks_the_slot(clock):
    result = await availability([], tasks=[{"pinned_start": stamp(4, 9), "duration_minutes": 90,
        "status": "active"}, {"pinned_start": None, "duration_minutes": 90, "status": "active"}])
    assert "10:30 AM–12:00 PM" in result


@pytest.mark.asyncio
async def test_route_date_does_not_drift_when_tool_executes_after_midnight(clock):
    d = await R.route("find me a 90-minute study slot tomorrow")
    class AfterMidnight(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 4, 0, 1)
    ready = {"sources": [{"id": "calendar", "state": "ready"}], "reminders_fresh": True}
    store = SimpleNamespace(today_snapshot=lambda day, zone: {"commitments": []})
    with patch.object(S, "dt", SimpleNamespace(datetime=AfterMidnight, date=dt.date,
            time=dt.time, timedelta=dt.timedelta)), patch.object(S, "_store", store), \
         patch("service.assistant.sync_status.ensure_sources", new=AsyncMock(return_value=ready)):
        result = await S.find_free_time(**d.direct_calls[0][1])
    assert "Sunday, 2026-10-04" in result
    assert "Monday" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["tomorrow", "tommorow", "tommorrow", "tomorow", "tmrw", "tmrow"])
async def test_study_aliases(spelling, clock):
    d = await R.route(f"find me a 90-minute study slot {spelling}")
    assert d.direct_calls == [("find_free_time", {"period": "2026-10-04", "minutes": 90})]


@pytest.mark.asyncio
async def test_multiple_overlapping_blocks_do_not_create_gap(clock):
    rows = [{"when_ts": stamp(4, 9), "end_ts": stamp(4, 11), "source": "calendar"},
            {"when_ts": stamp(4, 10), "end_ts": stamp(4, 17), "source": "calendar"}]
    assert "No gaps" in await availability(rows)


@pytest.mark.asyncio
async def test_old_relative_offer_cannot_drift_across_midnight(clock):
    with patch("service.tools.timeranges.resolve_span", return_value=(stamp(5, 0), stamp(6, 0), "tomorrow")):
        d = await R.route("sure", last_user="find time for me to study tmrow",
                          last_assistant=GOOD_OFFER)
    assert d.tool_subset == []
    assert "date cannot be verified" in d.resolved_request


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,offer", [
    ("Show my email", GOOD_OFFER),
    ("don't set a reminder; find time for me to study tmrow", GOOD_OFFER),
    ("sure", "Added reminder: Prepare for the study session."),
    ("find time for me to study tmrow", "9:00 AM is available."),
    ("find time for me to study tmrow", "Would" + GOOD_OFFER.split("Would", 1)[1]),
])
async def test_unrelated_denied_completed_or_unanchored_context(previous, offer, clock):
    d = await R.route("sure", last_user=previous, last_assistant=offer)
    assert "add_reminder" not in (d.tool_subset or [])


class StudyClient:
    def __init__(self, write=False):
        self.target = SimpleNamespace(context_window=64000)
        self.messages = []
        self.write = write

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, **kwargs):
        self.messages.append(list(messages))
        if self.write and len(self.messages) == 1:
            import json
            yield {"kind": "final", "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": "study-reminder", "type": "function", "function": {"name": "add_reminder",
                "arguments": json.dumps({"title": "WRONG", "when_iso": "2026-10-05T08:30"})}}]}}
        else:
            yield {"kind": "final", "message": {"role": "assistant", "content":
                "I created it." if self.write else "Verified candidate for Sunday, 2026-10-04."}}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "denied", "failed"])
async def test_confirmed_reminder_runs_permission_and_audit_and_requires_receipt(case, clock):
    from service.agent import loop
    from service.safety.policy import Tier
    d = await R.route("sure, on 2026-10-04", last_user="find time for me to study tmrow", last_assistant=GOOD_OFFER)
    client = StudyClient(write=True)
    approver = SimpleNamespace(confirm=AsyncMock(return_value=case != "denied"))
    result = "Reminder set: Prepare for the study session, Sunday 2026-10-04 08:30." if case == "success" else "(error: synthetic native write failed.)"
    execute = AsyncMock(return_value=result)
    policy = SimpleNamespace(tier=Tier.CONFIRM, reason="synthetic confirmation required")
    with patch.object(loop, "decide", return_value=policy) as decide, \
         patch.object(loop, "run_tool", execute), patch.object(loop, "audit") as audit:
        output = await loop.run_agent(client, "fixture-model", [{"role": "user", "content": d.resolved_request}],
            AsyncMock(), approver, tools=d.tool_subset, direct_calls=d.direct_calls,
            force_first_tool=d.force_first_tool, required_tool_groups=d.required_tool_groups,
            forbidden_tools=d.forbidden_tools, tool_argument_bindings=d.tool_argument_bindings,
            reminder_action=d.reminder_action, include_memory_context=False, max_steps=3)
    decide.assert_called()
    approver.confirm.assert_awaited_once()
    action = approver.confirm.await_args.args[0]
    assert action["tool"] == "add_reminder"
    assert action["args"] == d.tool_argument_bindings["add_reminder"]
    assert any(call.args[0] == ("confirm_deny" if case == "denied" else "confirm_allow")
               for call in audit.call_args_list)
    if case == "denied":
        execute.assert_not_awaited()
    else:
        execute.assert_awaited_once()
    if case == "success":
        assert output == result
    else:
        assert "not completed" in output or "did not return a verified success" in output
        assert "I created it" not in output


@pytest.mark.asyncio
async def test_availability_direct_execution_only_reads_bound_day(clock):
    from service.agent import loop
    d = await R.route("find me a 90-minute study slot tomorrow")
    client = StudyClient()
    execute = AsyncMock(return_value="90-minute candidate slots: Sunday, 2026-10-04, 9:00 AM–10:30 AM.")
    approver = SimpleNamespace(confirm=AsyncMock())
    with patch.object(loop, "run_tool", execute), patch.object(loop, "audit"):
        await loop.run_agent(client, "fixture-model", [{"role": "user", "content": d.resolved_request}],
            AsyncMock(), approver, tools=d.tool_subset, direct_calls=d.direct_calls,
            required_tool_groups=d.required_tool_groups, forbidden_tools=d.forbidden_tools,
            tool_argument_bindings=d.tool_argument_bindings, strict_read_limits=d.strict_read_limits,
            narration_after=d.narration_after, include_memory_context=False, max_steps=3)
    execute.assert_awaited_once()
    assert execute.await_args.args[0].name == "find_free_time"
    assert execute.await_args.args[1] == {"period": "2026-10-04", "minutes": 90}
    approver.confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [
    "(error: Wisp could not check current Calendar and Reminders availability. No study slot has been verified.)",
    "No matches: No gaps of 90+ minutes in Sunday, 2026-10-04 between 9:00 and 18:00.",
])
async def test_no_availability_is_terminal_before_narration(receipt, clock):
    from service.agent import loop
    d = await R.route("find me a 90-minute study slot tomorrow")
    client = StudyClient()
    with patch.object(loop, "run_tool", new=AsyncMock(return_value=receipt)), patch.object(loop, "audit"):
        output = await loop.run_agent(client, "fixture-model", [{"role": "user", "content": d.resolved_request}],
            AsyncMock(), SimpleNamespace(confirm=AsyncMock()), tools=d.tool_subset,
            direct_calls=d.direct_calls, required_tool_groups=d.required_tool_groups,
            forbidden_tools=d.forbidden_tools, tool_argument_bindings=d.tool_argument_bindings,
            strict_read_limits=d.strict_read_limits, narration_after=d.narration_after,
            include_memory_context=False, max_steps=3)
    assert client.messages == []
    assert "I created" not in output
