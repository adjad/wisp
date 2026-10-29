"""Ask answers about the user's day (2026-10-29 dev7 fresh-install export).

Three unhelpful answers, replayed with scripted models and fixture data only:
no model, no server, no user store.

* "what is up for tommorow": the typo kept get_upcoming out of the head start,
  the model's call only loaded its schema, and it then answered "I'm checking
  your schedule now. Give me just a moment!" with nothing run.
* "help me plan my day tommorow": get_my_day_plan was today-only, so the model
  used predict_my_day (learned habits) and missed the calendar and reminders.
* "what is on my email and messages": an outgoing text rendered as "You
  shared ..." came back as "You received ...".
"""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from service import model_only as mo
from service.agent import loop
from service.router.model_led import DISCOVERY_TOOL
from tests.test_model_led_integration import call, run  # noqa: F401
from tests.test_model_led_integration import synthetic  # noqa: F401  (fixture)

TZ = "America/Los_Angeles"
STALL = "I'm checking your schedule now to see what you have lined up for tomorrow. Give me just a moment!"


@pytest.fixture(autouse=True)
def _model_only(monkeypatch):
    monkeypatch.setenv("WISP_MODEL_ONLY", "1")
    monkeypatch.delenv("WISP_MODEL_ONLY_NARRATION_THINKING", raising=False)
    monkeypatch.setattr(loop, "audit", lambda *a, **k: None)


def executed(effects, name):
    return [args for tool, args in effects if tool == name]


def tool_messages(request):
    return [m for m in request["messages"] if m.get("role") == "tool"]


# --- request shape ------------------------------------------------------------------

@pytest.mark.parametrize("prompt", ["what is up for tommorow", "what's up tomorrow?", "what is happening tmrw",
                                    "anything planned for friday", "whats going on today"])
def test_day_questions_ask_for_personal_data(prompt):
    assert mo.asks_personal_data(prompt)


def test_tomorrow_typos_are_normalized_for_matching_only():
    assert mo.normalize_day_words("what is up for tommorow") == "what is up for tomorrow"
    assert mo.normalize_day_words("plan tmrw and 2morrow") == "plan tomorrow and tomorrow"
    assert mo.day_plan_question("help me plan my day tommorow")


@pytest.mark.parametrize("text", [
    STALL, "Let me check your calendar for tomorrow.", "One moment while I look that up!",
    "Sure! I'll pull up your reminders now.", "Hang tight, I'm looking into your inbox."])
def test_stall_replies_are_detected(text):
    assert mo.stall_reply(text)


@pytest.mark.parametrize("text", [
    "Tomorrow you have Office Hours with TA Hangyu from 10 to 11 AM.",
    "Rayleigh scattering makes the sky look blue.",
    "Let me know if you'd like the details of any email.", "",
    "I'm seeing two events tomorrow: Office Hours at 10 AM and Attendance at 4 PM."])
def test_real_answers_are_not_stalls(text):
    assert not mo.stall_reply(text)


# --- the "what is up for tommorow" turn ----------------------------------------------

def test_tomorrow_typo_ranks_the_calendar_read_into_the_head_start():
    from service import main
    preload, _ = main._ask_tool_rank("what is up for tommorow")
    assert "get_upcoming" in preload


def test_stub_load_then_stall_is_reprompted_and_reads_the_calendar(synthetic):
    """The export's turn: get_upcoming offered by name only, called (schema
    load, nothing ran), then a "give me a moment" reply with no tool."""
    effects, _ = synthetic
    client, events, answer = run(
        [[call("get_upcoming", period="tomorrow")], STALL,
         [call("get_upcoming", period="tomorrow")], "Tomorrow: Office Hours 10–11 AM."],
        prompt="what is up for tommorow", ask_quality=True, model_led_preload=["view_emails"])
    # The stub call ran nothing and says so in its receipt.
    receipt = json.loads(tool_messages(client.requests[1])[-1]["content"])
    assert receipt["source_tools_executed"] is False and receipt["not_run_yet"] == ["get_upcoming"]
    assert "Call get_upcoming now" in receipt["next"]
    # The stall was not the answer: a tool-required re-prompt followed.
    assert client.requests[2]["tool_choice"] == "required"
    assert executed(effects, "get_upcoming") == [{"period": "tomorrow"}]
    assert answer == "Tomorrow: Office Hours 10–11 AM."
    assert STALL not in [e.get("text") for e in events if e["type"] == "text"]


def test_a_stall_on_an_unrecognized_prompt_is_still_reprompted(synthetic):
    effects, _ = synthetic
    client, _, answer = run(
        ["Let me check that for you, one moment!", [call("get_upcoming", days=1)], "Nothing new since this morning."],
        prompt="anything new?", ask_quality=True, model_led_preload=["get_upcoming"])
    assert client.requests[1]["tool_choice"] == "required"
    assert executed(effects, "get_upcoming") == [{"days": 1}]
    assert answer == "Nothing new since this morning."


def test_an_answer_after_a_read_is_never_treated_as_a_stall(synthetic):
    client, _, answer = run(
        [[call("get_upcoming", period="tomorrow")], "Let me know if you want more detail on Office Hours."],
        prompt="what is up for tomorrow", ask_quality=True, model_led_preload=["get_upcoming"])
    assert answer == "Let me know if you want more detail on Office Hours."
    assert len(client.requests) == 2


def test_a_general_answer_is_not_reprompted(synthetic):
    client, _, answer = run(["Rayleigh scattering."], prompt="Why is the sky blue?", ask_quality=True,
                            model_led_preload=["get_upcoming"])
    assert answer == "Rayleigh scattering." and len(client.requests) == 1


# --- the "help me plan my day tommorow" turn -----------------------------------------

def _ts(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()


def _snapshot():
    """Fixture shaped like the Day tab the user saw on Thu Oct 29."""
    return {"state": "ready", "summary": "Nothing else on today.",
            "cards": [
                {"title": "Block 2–3 PM to study for STAT 5", "why": "Exam review is due Friday.",
                 "action": {"method": "block_time", "slot": {"start": _ts(30, 14), "end": _ts(30, 15)}}},
                {"title": "Block an hour tonight for the essay", "why": "Due Saturday.",
                 "action": {"method": "block_time", "slot": {"start": _ts(29, 20), "end": _ts(29, 21)}}},
                {"title": "Reply to Dhruv about the balcony", "action": {"method": "draft_reply"}}],
            "todos": [
                {"title": "Office Hours with TA Hangyu [STAT-5-02]", "source": "calendar", "due_ts": _ts(30, 10),
                 "due_label": "Tomorrow 10 AM", "done": False},
                {"title": "Attendance [CSE-20-04]", "source": "calendar", "due_ts": _ts(30, 16),
                 "due_label": "Tomorrow 4 PM", "done": False},
                {"title": "Lab section", "source": "calendar", "due_ts": _ts(29, 13),
                 "due_label": "Today 1 PM", "done": False},
                {"title": "Review reminder for STAT 5 Exam 1", "source": "reminders", "due_ts": _ts(26, 9),
                 "due_label": "Overdue · 3 days", "done": False},
                {"title": "Submit CSE 20 lab", "source": "reminders", "due_ts": _ts(30, 23, 59),
                 "due_label": "Tomorrow 11:59 PM", "done": False},
                {"title": "Buy printer paper", "source": "reminders", "due_ts": None, "due_label": "", "done": False}],
            "threads": [{"who": "Avika", "source": "messages", "summary": "Asked if you're free Friday night.",
                         "needs_reply": True}]}


def test_tomorrow_plan_holds_calendar_reminders_overdue_and_suggestions():
    from service.tools.proactive_tools import render_day_plan
    now = _ts(29, 0, 20)
    text = render_day_plan(_snapshot(), now=now, tz=TZ, day="tomorrow")
    assert text.startswith("Day plan for tomorrow, Fri Oct 30")
    timed = text.split("Time-bound tomorrow:")[1].split("Overdue:")[0]
    assert "10 am · Office Hours with TA Hangyu [STAT-5-02] (calendar)" in timed
    assert "4 pm · Attendance [CSE-20-04]" in timed and "Submit CSE 20 lab (reminder)" in timed
    assert "Lab section" not in text                      # today's class is not tomorrow's plan
    assert "Review reminder for STAT 5 Exam 1 · Overdue · 3 days" in text
    assert "Avika (text)" in text
    assert "Block 2–3 PM to study for STAT 5" in text and "Reply to Dhruv" in text
    assert "Block an hour tonight" not in text            # a slot today is not a suggestion for tomorrow
    assert "Buy printer paper" in text
    assert "Nothing else on today" not in text            # the Brief overview is about today
    assert len(text) <= 1500


def test_today_plan_is_unchanged_by_the_day_parameter():
    from service.tools.proactive_tools import render_day_plan
    now = _ts(29, 8)
    text = render_day_plan(_snapshot(), now=now, tz=TZ)
    assert text.startswith("Day plan for today, Thu Oct 29") and "Time-bound today:" in text
    assert "Lab section" in text and "Office Hours" not in text.split("Other to-dos:")[0]
    assert "Overview: Nothing else on today." in text
    assert "Block an hour tonight" in text and "Block 2–3 PM" not in text


def test_day_plan_tool_takes_a_day_and_only_reads_the_snapshot(monkeypatch):
    from service.proactive import cycle
    from service.tools import proactive_tools, registry
    monkeypatch.setattr(cycle, "snapshot", lambda **kw: _snapshot())
    monkeypatch.setattr(cycle, "_zone", lambda: TZ)
    monkeypatch.setattr("service.proactive.store.store", lambda: (_ for _ in ()).throw(RuntimeError("no store")))
    text = asyncio.run(proactive_tools.get_my_day_plan(day="tomorrow"))
    assert "Day plan for tomorrow" in text
    assert "Day plan for today" in asyncio.run(proactive_tools.get_my_day_plan())
    tool = registry.get_tool("get_my_day_plan")
    assert tool.parameters["properties"]["day"]["enum"] == ["today", "tomorrow"]
    assert "required" not in tool.parameters


def test_day_plan_hint_and_habit_tool_point_tomorrow_at_the_plan():
    from service.tools import registry
    assert "day='tomorrow'" in mo.ASK_DAY_PLAN_HINT and "predict_my_day" in mo.ASK_DAY_PLAN_HINT
    predict = registry.get_tool("predict_my_day")
    assert "does NOT list the calendar" in predict.description
    assert "plus what is already scheduled" not in predict.description


def test_plan_tomorrow_turn_uses_the_day_plan_for_tomorrow(synthetic):
    effects, register = synthetic
    register("get_my_day_plan", "proactive_tools", parameters={"type": "object", "properties": {
        "day": {"type": "string", "enum": ["today", "tomorrow"]}}, "additionalProperties": False})
    client, _, answer = run(
        [[call("get_my_day_plan", day="tomorrow")], "Tomorrow starts with Office Hours at 10 AM."],
        prompt="help me plan my day tommorow", ask_quality=True, model_led_preload=["get_my_day_plan"])
    assert executed(effects, "get_my_day_plan") == [{"day": "tomorrow"}]
    offered = {s["function"]["name"]: s for s in client.requests[0]["tools"]}
    assert "day" in offered["get_my_day_plan"]["function"]["parameters"]["properties"]
    assert answer == "Tomorrow starts with Office Hours at 10 AM."


# --- the "what is on my email and messages" turn -------------------------------------

def test_an_outgoing_text_reads_as_sent_not_shared():
    from service.tools import message_digest as D
    groups = D.analyze([(1.0, "Trishe", "Me: Unverified security incident notice.")], [""])
    updates = groups[0].signals["Updates"]
    assert updates and updates[0].startswith("You sent ")
    groups = D.analyze([(1.0, "Trishe", "Trishe: The landlord fixed the heater.")], [""])
    assert groups[0].signals["Updates"][0].startswith("Trishe shared ")
