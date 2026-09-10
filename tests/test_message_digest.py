"""Synthetic-only summary regressions. No server or native Messages access."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from service import debug_capture
from service.tools import imessage_tools as M
from service.tools import message_digest as D


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from service.memory import identity
    from service.assistant import sync_status
    monkeypatch.setattr(identity, "user_name", lambda: "Test User")
    monkeypatch.setattr(M, "_contacts", {})
    monkeypatch.setattr(M, "_lines", "")
    monkeypatch.setattr(M, "_sync_completed", True)
    monkeypatch.setattr(M, "_available", True)
    monkeypatch.setattr(sync_status, "ensure_sources", AsyncMock())
    monkeypatch.setattr(M, "_c", lambda: (_ for _ in ()).throw(RuntimeError("offline")))


def summarize(rows, label="today"):
    return asyncio.run(M._summarize(rows, label))


def client(monkeypatch, content=None, *, response=None, finish="stop", error=None):
    if response is None:
        response = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
    chat = AsyncMock(return_value=response, side_effect=error)
    class Stub:
        pass
    stub = Stub()
    stub.chat = chat
    monkeypatch.setattr(M, "_c", lambda: stub)
    return chat


ROWS = [
    (1, 'Group "Dinner"', "Alex: Let's meet for dinner Friday at 7 pm."),
    (2, 'Group "Dinner"', "Casey: We chose Thai food for dinner."),
    (3, 'Group "Dinner"', "Alex: Can you bring the dessert?"),
    (4, "Jamie", "Me: I'll send the budget report tomorrow."),
    (5, "Jamie", "Jamie: Please review the project outline by Thursday."),
]


def test_reported_6268_character_source_dump_is_never_the_summary():
    bodies = ["Alex: Please review the project outline tomorrow. " + "synthetic detail " * 11
              for _ in range(32)]
    raw = "\n".join(bodies)[:6268]
    rows = [(i, 'Group "Project"', body) for i, body in enumerate(raw.splitlines())]
    with debug_capture.capture() as records:
        out = summarize(rows)
    assert "Messages digest" in out and "Basic digest" in out
    assert len(out) < 1300
    assert "project outline" in out and "tomorrow" in out
    assert "Quoted from the source" not in out and "->" not in out
    assert "synthetic detail synthetic detail" not in out
    assert out.count("\n- ") <= 4
    assert any("synthetic detail" in r.get("text", "") for r in records if r["kind"] == "source")
    assert all(r.get("text") != out for r in records)
    assert records[-1]["status"] == "degraded"


def test_offline_digest_has_grouped_decisions_times_actions_and_reply_checks():
    out = summarize(ROWS)
    for text in ('Group "Dinner"', "Jamie", "Decisions mentioned", "Friday", "7 pm",
                 "Action items mentioned", "dessert", "budget report", "project outline",
                 "Reply check", "group question/request", "You: commitment", "may already have replies"):
        assert text in out
    for _, _, text in ROWS:
        assert text not in out
        assert text.partition(": ")[2] not in out
    assert "Jamie: commitment" not in out


@pytest.mark.parametrize("response", [None, {}, [], {"choices": []}, {"choices": [None]},
    {"choices": [{"message": {}}]}, {"choices": [{"message": {"content": []}}]}])
def test_malformed_responses_fail_closed(monkeypatch, response):
    chat = AsyncMock(return_value=response)
    stub = type("Stub", (), {"chat": chat})()
    monkeypatch.setattr(M, "_c", lambda: stub)
    assert "Basic digest" in summarize(ROWS)


@pytest.mark.parametrize("content", ["", " ", "null", "{}", "[]", "not json",
    "[Alex -> Group Dinner] raw transcript", "x" * 9000,
    '{"0":["Invented Person confirmed everything"]}', '{"0": ["meals", "meals"]}'])
def test_empty_malformed_echo_and_oversize_model_text_cannot_be_an_answer(monkeypatch, content):
    client(monkeypatch, content)
    out = summarize(ROWS)
    assert "Basic digest" in out and len(out) <= D.MAX_OUTPUT_CHARS
    assert "Invented Person" not in out and "raw transcript" not in out


@pytest.mark.parametrize("finish", ["length", "error", None, "tool_calls"])
def test_incomplete_model_results_degrade(monkeypatch, finish):
    client(monkeypatch, '{"0":["meals"]}', finish=finish)
    assert "Basic digest" in summarize(ROWS)


def test_valid_model_topics_cannot_author_attribution_or_status(monkeypatch):
    async def choose(_model, request, **_kw):
        candidates = json.loads(request[1]["content"])
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({key: value[:2] for key, value in candidates.items()})}}]}
    chat = client(monkeypatch)
    chat.side_effect = choose
    with debug_capture.capture() as records:
        out = summarize(ROWS)
    assert "Basic digest" not in out
    assert "You: commitment" in out and "group question/request" in out
    assert chat.await_count == 1 and records[-1]["status"] == "ok"
    request = chat.call_args.args[1]
    assert "Alex:" not in str(request) and "budget report" in str(request)


def test_timeout_cancels_local_call_and_returns_bounded_fallback(monkeypatch):
    cancelled = []
    async def hanging(*_args, **_kwargs):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)
    chat = client(monkeypatch)
    chat.side_effect = hanging
    monkeypatch.setattr(M, "_SUMMARY_TIMEOUT_SECONDS", 0.01)
    assert "Basic digest" in summarize(ROWS)
    assert cancelled == [True]


def test_superseded_turn_cancellation_is_not_swallowed(monkeypatch):
    client(monkeypatch, error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        summarize(ROWS)


def test_errors_with_private_source_text_never_leak(monkeypatch):
    client(monkeypatch, error=RuntimeError("private synthetic exception body"))
    with debug_capture.capture() as records:
        out = summarize(ROWS)
    assert "private synthetic" not in out
    assert "private synthetic" not in str(records[-1])


def test_large_groups_do_not_erase_quiet_conversations():
    rows = [(i, 'Group "Busy"', f"Person {i % 4}: Meet for lunch at 1 pm, item {i}?")
            for i in range(4000)]
    rows.append((4001, "Quiet Friend", "Quiet Friend: Please review the invoice tomorrow."))
    out = summarize(rows)
    assert 'Group "Busy"' in out and "Quiet Friend" in out
    assert len(out) <= D.MAX_OUTPUT_CHARS and out.count("\n- ") <= 8
    assert "4001 messages" in out


def test_many_chats_and_malicious_labels_have_bounded_output_and_disclosure():
    rows = [(i, f"Group {i} " + "*bad*\n# heading " * 30, "Sam: Please review the document tomorrow?")
            for i in range(200)]
    out = summarize(rows, "*" * 10000)
    assert len(out) <= D.MAX_OUTPUT_CHARS
    assert "of 200 conversations" in out and "more are included" in out
    assert "\n# heading" not in out


def test_unknown_senders_and_group_addressees_are_not_the_user(monkeypatch):
    monkeypatch.setattr(M, "_contacts", {"1": "Blair"})
    rows = [(1, 'Group "Team"', "Alex: @Blair can you review the project outline?"),
            (2, 'Group "Team"', "Alex: Please send the report tomorrow."),
            (3, "Other", "Please review the proposal tomorrow.")]
    out = summarize(rows)
    assert "to Blair" in out and "Unknown sender" in out
    assert "question/request to you" not in out
    assert "Alex to Blair" in out


def test_ambiguous_carry_does_not_choose_a_recipient(monkeypatch):
    monkeypatch.setattr(M, "_contacts", {"1": "Blair", "2": "Casey"})
    rows = [(1, 'Group "Team"', "Alex: @Blair please review the report."),
            (2, 'Group "Team"', "Alex: Can you send the budget?"),
            (3, 'Group "Team"', "Alex: @Casey please review the document.")]
    assert M.summary_addressees(rows) == ["Blair", "", "Casey"]


def test_different_groups_stay_separate_even_with_same_speaker():
    out = summarize([(1, 'Group "A"', "Sam: Please bring the dessert."),
                     (2, 'Group "B"', "Sam: Please review the budget.")])
    assert 'Group "A"' in out and 'Group "B"' in out and "2 conversations" in out


def test_long_messages_are_explicitly_partial():
    out = summarize([(1, "Alex", "Alex: Please review the project. " + "extra " * 10000)])
    assert "Long messages analyzed only in part" in out
    assert len(out) <= D.MAX_OUTPUT_CHARS


def cache(monkeypatch, rows):
    monkeypatch.setattr(M, "_lines", "\n".join(f"{ts} | {ctx} | {txt}" for ts, ctx, txt in rows))


def test_day_boundaries_noise_and_duplicates_are_filtered_before_digest(monkeypatch):
    start, end, label = M._day_bounds("today")
    cache(monkeypatch, [(start - 1, "Old", "Old: dinner yesterday"),
                       (start, "Alex", "Alex: Please bring the dessert tonight."),
                       (start + 1, "Alex", "Alex: Please bring the dessert tonight."),
                       (start + 2, "12345", "12345: Your verification code is 123456."),
                       (start + 3, "67890", "67890: Flash sale today! Reply STOP to opt out."),
                       (end, "Future", "Future: dinner tomorrow")])
    out = asyncio.run(M.summarize_messages(day="today"))
    assert label in out and "1 messages across 1 conversations" in out
    assert "123456" not in out and "Old" not in out and "Future" not in out


def test_friends_discussing_sales_are_retained(monkeypatch):
    cache(monkeypatch, [(1, "Alex", "Alex: Can you check the sale on train tickets?")])
    assert "Alex" in asyncio.run(M.summarize_messages(count=30))


def test_empty_day_and_empty_sync_never_call_model(monkeypatch):
    chat = client(monkeypatch)
    assert "No substantive messages" in asyncio.run(M.summarize_messages(day="today"))
    assert "No substantive messages" in asyncio.run(M.summarize_messages(period="this week"))
    assert "No substantive messages" in asyncio.run(M.summarize_messages())
    chat.assert_not_called()


def test_period_wins_over_day_and_retains_quiet_thread_without_sampling(monkeypatch):
    monkeypatch.setattr(M, "resolve_span", lambda _: (0, 1000, "chosen period"))
    cache(monkeypatch, [(i, 'Group "Busy"', f"Sam: Lunch at 1 pm? {i}") for i in range(500)] +
          [(499.5, "Quiet", "Quiet: Please review the invoice.")])
    out = asyncio.run(M.summarize_messages(day="bad-date", period="chosen"))
    assert "chosen period" in out and "Quiet" in out and "501 messages" in out


def test_reactions_do_not_call_model_or_become_a_transcript(monkeypatch):
    chat = client(monkeypatch)
    out = summarize([(1, "Alex", 'Alex: Loved “Can you review this?”'), (2, "Alex", "Me: 👍")])
    assert "acknowledgments/reactions" in out and "Reply check" not in out
    assert "Can you review this" not in out and "Basic digest" not in out
    chat.assert_not_called()


def test_unavailable_sync_does_not_summarize_stale_cache(monkeypatch):
    cache(monkeypatch, ROWS)
    monkeypatch.setattr(M, "_sync_completed", False)
    assert "still syncing" in asyncio.run(M.summarize_messages())


def test_negated_or_conditional_decision_is_not_claimed_as_an_outcome():
    out = summarize([(1, "Alex", "Alex: Dinner is not confirmed. If agreed, meet tomorrow.")])
    assert "wording about" in out and "not verified outcomes" in out
    assert "Alex confirmed" not in out and "You agreed" not in out


def test_latest_cancellation_survives_limits_and_input_order():
    rows = [(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
            (2, "Alex", "Alex: Dinner postponed to Saturday at 8 pm."),
            (3, "Alex", "Alex: Dinner canceled.")]
    forward = summarize(rows)
    reverse = summarize(rows[::-1])
    assert "canceled" in forward and "canceled" in reverse
    assert "postponed" in forward and "other signals" in forward


def test_repeated_latest_cancellation_keeps_latest_occurrence(monkeypatch):
    start, _, _ = M._day_bounds("today")
    rows = [(start, "Alex", "Alex: Dinner canceled."),
            (start + 1, "Alex", "Alex: Dinner confirmed Friday."),
            (start + 2, "Alex", "Alex: Dinner postponed Saturday."),
            (start + 3, "Alex", "Alex: Dinner canceled.")]
    assert "canceled" in summarize(rows)
    cache(monkeypatch, rows)
    assert "canceled" in asyncio.run(M.summarize_messages(day="today"))
    # The shared helper retains its input ordering for existing brief callers.
    assert [ts for ts, _, body in M.filter_summary_message_rows(rows) if "canceled" in body] == [start]


def test_action_negation_belongs_to_its_own_clause():
    out = summarize([(1, "Alex", "Alex: I will send the report tomorrow. I will not attend dinner.")])
    assert 'Alex: commitment — “I will send the report tomorrow”' in out
    assert 'Alex: negative/declined commitment — “I will not attend dinner”' in out
    out = summarize([(1, "Alex", "Alex: Sounds good. I will not send the report tomorrow.")])
    assert 'negative/declined commitment — “I will not send the report tomorrow”' in out
    assert '— “Sounds good”' not in out


def test_plans_decisions_and_negative_actions_do_not_contaminate_other_clauses():
    out = summarize([(1, "Alex", "Alex: We agreed on dinner. Meet Friday at 7 pm. I will not send the report.")])
    assert "Alex: agreed wording" in out
    assert "negative/declined agreed" not in out
    assert "Friday, 7 pm" in out
    assert 'negative/declined commitment — “I will not send the report”' in out


@pytest.mark.parametrize("text,expected", [
    ("I will not send the report tomorrow.", "negative/declined commitment"),
    ("I will send the report tomorrow.", "commitment —"),
    ("If ready, I will send the report tomorrow.", "conditional commitment"),
    ("Please do not come to dinner.", "negative/declined request"),
    ("Please come to dinner.", "request —"),
])
def test_commitments_and_requests_preserve_negation_and_conditions(text, expected):
    out = summarize([(1, "Alex", f"Alex: {text}")])
    assert expected in out
    assert text.rstrip(".") in out
    if "not" not in text:
        assert "negative/declined" not in out


@pytest.mark.parametrize("text,actor", [
    ("Meet at 7:30 pm tomorrow?", "Unknown sender"),
    ("Alex: Meet at 7:30 pm tomorrow?", "Alex"),
    ("+15551234567: Meet at 7:30 pm tomorrow?", "+15551234567"),
])
def test_time_colons_are_not_sender_delimiters(text, actor):
    out = summarize([(1, "Chat", text)])
    assert f"{actor}:" in out and "7:30 pm" in out
    assert "Meet at 7:" not in out


def test_ordinary_updates_keep_propositions_with_source_owned_speaker():
    out = summarize([(1, "Alex", "Alex: The baby arrived this morning. Everyone is doing well."),
                     (2, "Alex", "Alex: Her name is Juniper.")])
    assert "baby arrived this morning" in out and "name is Juniper" in out
    assert "Alex shared" in out and "Updates" in out
    assert "You had a baby" not in out


def test_relative_times_from_different_days_keep_source_dates(monkeypatch):
    from datetime import datetime
    rows = [(datetime(2026, 9, 1, 12).timestamp(), "Alex", "Alex: Please send the report tomorrow."),
            (datetime(2026, 9, 5, 12).timestamp(), "Alex", "Alex: Please send the report tomorrow.")]
    cache(monkeypatch, rows)
    monkeypatch.setattr(M, "resolve_span", lambda _: (0, 2_000_000_000, "September"))
    out = asyncio.run(M.summarize_messages(period="September"))
    assert "sent 2026-09-01" in out and "sent 2026-09-05" in out
    assert "tomorrow" in out


def test_unusual_long_tokens_cannot_overflow_model_input(monkeypatch):
    chat = client(monkeypatch, "invalid")
    summarize([(i, f"Group {i}", "Alex: " + "z" * 100000) for i in range(20)])
    assert len(chat.call_args.args[1][1]["content"]) < 10000


def test_real_presynthesized_tool_path_returns_digest_after_only_selection_call(monkeypatch):
    from service.agent import loop
    start, _, _ = M._day_bounds("today")
    cache(monkeypatch, [(start + i, 'Group "Synthetic"',
                        f"Alex: Please review the project outline tomorrow. private fixture detail {i} " + "detail " * 100)
                       for i in range(30)])
    class SelectionClient:
        calls = 0
        async def ensure_only(self, *args, **kwargs):
            return None
        async def stream_events(self, *args, **kwargs):
            self.calls += 1
            assert self.calls == 1
            yield {"kind": "final", "message": {"role": "assistant", "content": "",
                "tool_calls": [{"id": "summary-call", "function": {
                    "name": "summarize_messages", "arguments": '{"day":"today"}'}}]}}
    class Approver:
        async def confirm(self, action):
            raise AssertionError("read-only summary must not require approval")
    events = []
    async def emit(event):
        events.append(event)
    selection = SelectionClient()
    output = asyncio.run(loop.run_agent(
        selection, "synthetic-model", [{"role": "user", "content": "generate my messages summaries for today!"}],
        emit, Approver(), tools=["summarize_messages"], max_steps=2,
        short_circuit_tools={"summarize_messages"}))
    assert selection.calls == 1
    assert "Messages digest" in output and "Basic digest" in output
    assert "private fixture detail" not in output
    assert len(output) <= D.MAX_OUTPUT_CHARS
