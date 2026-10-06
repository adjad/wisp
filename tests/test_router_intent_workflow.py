"""Read/delivery grammatical boundaries with synthetic durable state only."""
from service.memory.store import SessionStore
from service.workflows.compiler import compile_new, outbound_verb
from service.workflows.engine import prepare_turn
import pytest


@pytest.mark.parametrize("prompt", [
    "Summarize recent text messages.", "What did people text me?",
    "Show my recent text summary.", "What did Mom message me?",
    "Show text messages from Mom", "Read my message history",
    "Text summary", "What has Imani text me?", "Summarize a text message",
])
def test_message_source_nouns_and_historical_predicates_do_not_start_delivery(prompt, tmp_path):
    assert not outbound_verb(prompt)
    assert compile_new(prompt) is None
    store = SessionStore(tmp_path / "sessions.db")
    sid = store.create_session()
    assert prepare_turn(store, sid, prompt) is None
    assert store.latest_workflow(sid) is None


@pytest.mark.parametrize("prompt", [
    "Text Mom my messages summary", "Message Mom my calendar summary",
    "Send Mom my recent text summary via Messages",
    "Forward my messages summary to Mom", "Draft Mom my calendar summary via Messages",
    "Text summary to Mom", "Text messages to Mom",
    "Schedule a message to Mom with my calendar summary",
    "Summarize text messages and send it to Mom",
    "What did people text me and forward it to Mom?",
    "Show my recent text summary and text Mom my calendar summary",
    "Read text messages; message Mom my calendar summary",
    "What did people say and text Mom my calendar summary",
    "What did people say? Text Mom my calendar summary",
    "What did people say, text Mom my calendar summary",
])
def test_genuine_delivery_and_later_delivery_clauses_remain_workflows(prompt):
    assert outbound_verb(prompt)
    assert compile_new(prompt) is not None


@pytest.mark.parametrize("prompt", [
    "Don't text Mom my messages summary", "Never message Mom my calendar summary",
    'Explain the phrase "text Mom my message summary".',
    'What does "message Mom my calendar summary" mean?',
])
def test_prohibitions_and_quoted_mentions_do_not_start_workflows(prompt):
    assert compile_new(prompt) is None


def test_pending_delivery_continuations_keep_channel_recipient_mode_and_sources(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    sid = store.create_session()
    first = prepare_turn(store, sid, "send Mom my calendar summary tomorrow")
    assert first.plan.status == "waiting_for_channel"
    second = prepare_turn(store, sid, "Messages")
    assert second.plan.status == "running"
    assert second.decision is not None
    assert second.plan.recipient == "Mom" and second.plan.channel == "messages"
    assert second.plan.sources == ["calendar"] and second.plan.delivery == "send"
    assert second.plan.source_args == {"calendar": {"period": "tomorrow"}}


def test_pending_unaddressed_delivery_releases_standalone_message_summary_read(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    sid = store.create_session()
    pending = prepare_turn(store, sid, "send it")
    assert pending.plan.status == "waiting_for_content"
    assert prepare_turn(store, sid, "Show my recent text summary.") is None
    assert store.latest_workflow(sid)["status"] == "superseded"
