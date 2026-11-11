"""0.5.0-rc2 known issue: "when is the next meeting" and "the party on Saturday"
got no tools at all on a model-led Ask turn.

A bare definite event of the kind people keep on their own calendar is the
user's own event: the turn may read the calendar and reminders, and nothing
else (never the web, mail, messages or an effect). Everything else keeps its
existing envelope.
"""
from __future__ import annotations

import pytest

from service import model_only as mo
from tests.test_calendar_time_contract import inert_entrypoint, run_capture  # noqa: F401
from tests.test_workflow20_knowledge import knowledge_entrypoint, storage  # noqa: F401

# Named-time envelopes the typed parser cannot finish: before this fix they
# forbade every tool on a model-led turn.
OWN_EVENTS = ["When is the next meeting?", "when is the next meeting", "When is the party on Saturday?",
              "When is the dinner on Friday?", "When is the next class?", "When is the next team meeting?"]
# Same shape, other owners: "When's ..." is no named-time envelope (ordinary
# routing), and a fully parsed "the meeting tomorrow" keeps its existing route.
OWN_EVENTS_ELSEWHERE = ["When's the next team meeting?", "What time is the meeting tomorrow?",
                        "When is the interview this week?"]
NOT_OWN_EVENTS = [
    # Already owned elsewhere, or not a bare definite event of the user's kind.
    "When is the next lunar eclipse?", "When is the next train to Boston?", "When is the solar eclipse?",
    "When is the next review of my notes?", "When is the next Jordan's thing?",
    "When is the next game?", "When is a meeting?", "When is the meeting with the Lakers?",
    # Compound or a second request.
    "When is the next meeting and email Mom?", "When is the party on Saturday? Also text Sam.",
    # Non-ASCII lookalikes ("Сlass" with a Cyrillic С).
    "When is the next Сlass?",
]


@pytest.mark.parametrize("prompt", OWN_EVENTS + OWN_EVENTS_ELSEWHERE + ["the party on Saturday", "The next meeting?"])
def test_bare_own_events_are_recognised(prompt):
    assert mo.personal_event_question(prompt)


@pytest.mark.parametrize("prompt", OWN_EVENTS_ELSEWHERE)
def test_other_owned_shapes_keep_their_envelope(prompt):
    from service.router.router import routing_guard_contract
    assert routing_guard_contract(prompt, model_led=True) == routing_guard_contract(prompt)


@pytest.mark.parametrize("prompt", NOT_OWN_EVENTS)
def test_other_shapes_are_not_own_events(prompt):
    assert not mo.personal_event_question(prompt)


@pytest.mark.parametrize("prompt", OWN_EVENTS)
def test_model_led_own_event_reads_only_calendar_and_reminders(prompt):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    forbidden, bindings = routing_guard_contract(prompt, model_led=True)
    assert set(REGISTRY) - forbidden == {"get_upcoming", "search_reminders"}
    assert bindings == {"get_upcoming": {"include_holidays": False}}
    assert {"web_search", "web_fetch"} <= forbidden


@pytest.mark.parametrize("prompt", OWN_EVENTS)
def test_rules_route_envelope_is_unchanged(prompt):
    """The legacy route still clarifies, so its envelope still grants nothing."""
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    forbidden, bindings = routing_guard_contract(prompt)
    assert set(REGISTRY) <= forbidden and not bindings


@pytest.mark.parametrize("prompt", [
    "When is the next meeting? Do not use Calendar.", "When is the party on Saturday? Don't read my calendar.",
])
def test_a_calendar_denial_still_closes_every_source(prompt):
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY
    forbidden, bindings = routing_guard_contract(prompt, model_led=True)
    assert set(REGISTRY) <= forbidden and not bindings


def test_calendar_leads_the_ask_head_start():
    assert mo.calendar_first("When is the party on Saturday?")
    assert mo.calendar_first("the party on Saturday")
    assert mo.calendar_first("When is the next meeting?")


@pytest.mark.parametrize("name,args", [
    ("web_search", {"query": "next meeting"}), ("view_emails", {}), ("view_messages", {}),
    ("run_shell", {"command": "fictional"}), ("send_message", {"to": "Fictional", "text": "fictional"}),
    ("add_calendar_event", {"title": "Fictional", "start": "2026-11-14 18:00"}),
])
def test_actual_model_led_own_event_runs_nothing_else(inert_entrypoint, monkeypatch, name, args):
    record = run_capture(inert_entrypoint, monkeypatch, "model-led", "When is the party on Saturday?", name, args)
    assert record["executed"] == []


def test_actual_model_led_own_event_reads_the_calendar_and_reminders(inert_entrypoint, monkeypatch):
    record = run_capture(inert_entrypoint, monkeypatch, "model-led", "When is the next meeting?",
                         "get_upcoming", {"query": "meeting", "include_holidays": True})
    assert record["executed"] == [{"name": "get_upcoming", "args": {
        "query": "meeting", "include_holidays": False}}]


def test_actual_model_led_own_event_may_search_reminders(inert_entrypoint, monkeypatch):
    record = run_capture(inert_entrypoint, monkeypatch, "model-led", "When is the party on Saturday?",
                         "search_reminders", {"query": "party"})
    assert [c["name"] for c in record["executed"]] == ["search_reminders"]
