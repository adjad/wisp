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
    assert "postponed" in forward and "previous report" in forward
    assert "2 earlier reports superseded" in forward
    assert "confirmed" not in forward  # no obsolete confirmation presented as current


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
    # The factual excerpt now retains the time verbatim; it must not be used
    # as the speaker label of the reply signal.
    assert "Reply check: Meet at 7:" not in out


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


@pytest.fixture(params=["offline", "valid", "failure", "echo"])
def semantic_model(request, monkeypatch):
    mode = request.param
    if mode == "offline":
        return mode
    async def choose(_model, messages, **kwargs):
        if mode == "failure":
            raise RuntimeError("synthetic model failure")
        candidates = json.loads(messages[1]["content"])
        content = ("[Alex -> Group] RAW TRANSCRIPT ECHO" if mode == "echo" else
                   json.dumps({key: values[:1] for key, values in candidates.items()}))
        return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    stub = client(monkeypatch)
    stub.side_effect = choose
    return mode


@pytest.mark.parametrize("left,right,required_left,required_right", [
    ("The flight is delayed until tomorrow.", "The flight is on time tomorrow.", "delayed until tomorrow", "on time tomorrow"),
    ("Dinner is off.", "Dinner is still happening.", "Dinner is off", "still happening"),
    ("Dinner is cancelled.", "Dinner is happening.", "cancelled", "happening"),
    ("Rent increased to $2500 today.", "Rent decreased to $1500 today.", "increased to $2500", "decreased to $1500"),
    ("Rent increased to $2500 today.", "Rent decreased to $2500 today.", "increased to $2500", "decreased to $2500"),
    ("Rent increased to $2,500.50 today.", "Rent decreased to $1,500.25 today.", "$2,500.50", "$1,500.25"),
    ("The appointment moved from Monday to Tuesday.", "The appointment moved from Tuesday to Monday.", "from Monday to Tuesday", "from Tuesday to Monday"),
    ("The flight is not delayed tomorrow.", "The flight is delayed tomorrow.", "not delayed", "is delayed"),
    ("Is rent increased to $2500 today?", "Is rent decreased to $1500 today?", "increased to $2500", "decreased to $1500"),
])
def test_paired_material_propositions_survive_all_model_paths(
        semantic_model, left, right, required_left, required_right):
    a = summarize([(1, "Alex", "Alex: " + left)])
    b = summarize([(1, "Alex", "Alex: " + right)])
    assert a != b
    assert required_left in a and required_right in b
    for out in (a, b):
        assert "Alex" in out and len(out) <= D.MAX_OUTPUT_CHARS
        assert ("Basic digest" in out) == (semantic_model != "valid")
        assert "RAW TRANSCRIPT ECHO" not in out


@pytest.mark.parametrize("first,last,latest,previous", [
    ("Dinner confirmed Friday at 7 pm.", "Dinner is off.", "Dinner is off", "Friday at 7 pm"),
    ("Dinner canceled Friday at 7 pm.", "Dinner rescheduled to Saturday at 8 pm.", "rescheduled to Saturday at 8 pm", "canceled Friday at 7 pm"),
    ("The flight is delayed until tomorrow.", "The flight is on time tomorrow.", "on time tomorrow", "delayed until tomorrow"),
    ("Rent increased to $2500 today.", "Rent decreased to $1500 today.", "decreased to $1500", "increased to $2500"),
    ("Rent increased to $2500 today.", "Actually $1500, not $2500.", "Actually $1500, not $2500", "increased to $2500"),
    ("Dinner confirmed Monday at 7 pm.", "Correction: dinner is Tuesday at 8 pm.", "Tuesday at 8 pm", "Monday at 7 pm"),
    ("Dinner is canceled.", "Dinner is not canceled.", "Dinner is not canceled", "Dinner is canceled"),
])
def test_later_reports_replace_earlier_state_with_explicit_previous_context(
        semantic_model, first, last, latest, previous):
    rows = [(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + last)]
    out = summarize(rows)
    assert out == summarize(rows[::-1])
    assert out.count("Latest report:") == 1
    current, prior = out.split("previous report from", 1)
    assert latest in current and previous in prior
    assert "1 earlier reports superseded" in out
    # The earlier report cannot remain in a second category as a current plan.
    assert previous not in current


@pytest.mark.parametrize("later", [
    "If dinner is canceled, please tell me.", "Is dinner canceled?",
    "Please cancel dinner.", "I will cancel dinner.", "Don't cancel dinner.",
    "Dinner should be canceled.", "Dinner could be canceled.",
    "Dinner may be canceled.", "Let's cancel dinner.",
])
def test_questions_conditions_and_proposals_do_not_override_confirmed_plans(later):
    out = summarize([(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, "Alex", "Alex: " + later)])
    assert "Latest report:" not in out and "superseded" not in out
    assert "Dinner confirmed Friday at 7 pm" in out
    assert later.rstrip(".!?") in out


def test_same_topic_in_different_conversations_never_replaces_other_group(semantic_model):
    out = summarize([(1, 'Group "A"', "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, 'Group "B"', "Alex: Dinner is off."),
                     (3, 'Group "A"', "Alex: The flight is delayed tomorrow.")])
    assert "Latest report:" not in out
    for fact in ("Dinner confirmed Friday at 7 pm", "Dinner is off", "delayed tomorrow"):
        assert fact in out
    assert "2 conversations" in out


def test_same_chat_different_entities_and_named_plans_do_not_conflate():
    rows = [(1, "Alex", "Alex: Dinner with Sam confirmed Friday at 7 pm."),
            (2, "Alex", "Alex: Dinner with Blair confirmed Saturday at 8 pm."),
            (3, "Alex", "Alex: Lunch is off."),
            (4, "Alex", "Alex: Dinner with Blair is off.")]
    out = summarize(rows)
    assert "Dinner with Sam confirmed Friday at 7 pm" in out
    assert "Lunch is off" in out
    assert out.count("Latest report:") == 1
    current, old = out.split("previous report from")
    assert "Dinner with Blair is off" in current
    assert "Dinner with Blair confirmed Saturday at 8 pm" in old


def test_ambiguous_untimed_cancellation_keeps_both_possible_prior_events():
    out = summarize([(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, "Alex", "Alex: Dinner confirmed Saturday at 8 pm."),
                     (3, "Alex", "Alex: Dinner is off.")])
    assert "Latest report:" not in out
    assert "several earlier events may match" in out
    assert "Friday at 7 pm" in out and "Saturday at 8 pm" in out and "Dinner is off" in out


@pytest.mark.parametrize("first,second", [
    ("Dinner confirmed Friday at 7 pm.", "Dinner confirmed Saturday at 7 pm."),
    ("Dinner confirmed Friday at 7 pm.", "Dinner confirmed Friday at 8 pm."),
    ("Dinner at Cafe Blue confirmed Friday at 7 pm.", "Dinner at Cafe Red is off."),
    ("Flight AB123 confirmed tomorrow.", "Flight CD456 is delayed tomorrow."),
])
def test_conflicting_day_clock_venue_and_flight_id_never_match(first, second):
    out = summarize([(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + second)])
    assert "Latest report:" not in out and "superseded" not in out
    assert first.rstrip(".") in out and second.rstrip(".") in out


@pytest.mark.parametrize("first,last", [
    ("Dinner at Cafe Blue confirmed Friday at 7 pm.", "Dinner at Cafe Blue is off."),
    ("Flight AB123 confirmed tomorrow.", "Flight AB123 is delayed tomorrow."),
    ("Dinner confirmed Friday at 7 pm.", "Dinner rescheduled Saturday at 7 pm."),
])
def test_matching_event_and_explicit_rescheduling_still_supersede(first, last):
    out = summarize([(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + last)])
    current, prior = out.split("previous report from")
    assert "Latest report:" in current and last.rstrip(".") in current
    assert first.rstrip(".") in prior


def test_relative_day_identity_uses_source_date_and_preserves_legitimate_match():
    from datetime import datetime
    first = datetime(2026, 9, 1, 12).timestamp()
    out = summarize([(first, "Alex", "Alex: Dinner confirmed tomorrow at 7 pm."),
                     (first + 86400, "Alex", "Alex: Dinner confirmed tomorrow at 7 pm.")])
    assert "Latest report:" not in out
    assert "2026-09-01" in out and "2026-09-02" in out
    # Tomorrow from the first source day and today from the next mean the same day.
    out = summarize([(first, "Alex", "Alex: Dinner confirmed tomorrow at 7 pm."),
                     (first + 86400, "Alex", "Alex: Dinner is off today.")])
    assert "Latest report:" in out and "Dinner is off today" in out


def test_different_speakers_and_addressees_do_not_erase_each_others_reports(monkeypatch):
    monkeypatch.setattr(M, "_contacts", {"1": "Sam", "2": "Blair"})
    rows = [(1, 'Group "A"', "Alex: @Sam dinner confirmed Friday at 7 pm."),
            (2, 'Group "A"', "Alex: @Blair dinner is off."),
            (3, 'Group "A"', "Casey: Dinner is off.")]
    out = summarize(rows)
    assert "Latest report:" not in out
    assert "to Sam" in out and "to Blair" in out and "Casey" in out
    assert "confirmed Friday at 7 pm" in out


def test_repeated_reversal_keeps_last_fact_and_previous_schedule(semantic_model):
    out = summarize([(1, "Alex", "Alex: Dinner is off."),
                     (2, "Alex", "Alex: Dinner rescheduled Friday at 7 pm."),
                     (3, "Alex", "Alex: Dinner is off.")])
    current, previous = out.split("previous report from")
    assert "Dinner is off" in current
    assert "rescheduled Friday at 7 pm" in previous
    assert "2 earlier reports superseded" in out


def test_historical_single_day_and_distant_untimed_updates_remain_anchored(monkeypatch):
    from datetime import datetime
    first = datetime(2026, 9, 1, 12).timestamp()
    cache(monkeypatch, [(first, "Alex", "Alex: The flight is delayed until tomorrow.")])
    monkeypatch.setattr(M, "resolve_span", lambda _: (0, 2_000_000_000, "September"))
    assert "2026-09-01" in asyncio.run(M.summarize_messages(period="September"))
    out = summarize([(first, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (first + 14 * 86400, "Alex", "Alex: Dinner is off.")])
    assert "Latest report:" not in out
    assert "2026-09-01" in out and "2026-09-15" in out


def test_long_clause_does_not_drop_trailing_negation_or_amount():
    prefix = "The flight, following a detailed discussion about the aircraft schedule and the airport "
    clause = prefix + "and after checking several separate departure boards with the travel desk, is not delayed tomorrow."
    out = summarize([(1, "Alex", "Alex: " + clause)])
    assert "not delayed tomorrow" in out and "statement shortened" in out
    clause = "Rent, following a review of the proposed terms and several conversations with the property manager " + \
             "about the renewal details and the current agreement for the apartment, decreased to $1500 today."
    out = summarize([(1, "Alex", "Alex: " + clause)])
    assert "decreased to $1500" in out and "statement shortened" in out
    assert len(out) <= D.MAX_OUTPUT_CHARS


def test_long_ordinary_updates_and_actions_preserve_quantities_and_exclusions(semantic_model):
    prefix = "The balance, following the discussion with the development team and after our separate review of the account records, is "
    a = summarize([(1, "Alex", "Alex: " + prefix + "$1500.")])
    b = summarize([(1, "Alex", "Alex: " + prefix + "$2500.")])
    assert "$1500" in a and "$2500" in b and a != b
    clause = "I will send the report following the discussion with the development team and after our separate review of the document, but not the confidential attachment."
    out = summarize([(1, "Alex", "Alex: " + clause)])
    assert "not the confidential attachment" in out
    assert len(out) <= D.MAX_OUTPUT_CHARS


def test_comparison_operator_is_preserved_as_safe_rendered_text():
    a = summarize([(1, "Alex", "Alex: Rent is < $1500 today.")])
    b = summarize([(1, "Alex", "Alex: Rent is > $1500 today.")])
    assert "&lt; $1500" in a and "&gt; $1500" in b and a != b


def test_many_status_changes_stay_a_digest_with_latest_state(semantic_model):
    rows = [(i, "Alex", f"Alex: Rent increased to ${1000 + i} today.") for i in range(1000)]
    out = summarize(rows)
    assert "increased to $1999" in out
    assert "increased to $1998" in out and "999 earlier reports superseded" in out
    assert out.count("\n- ") <= 2 and len(out) < 1400
    assert "increased to $1000" not in out


@pytest.mark.parametrize("later", [
    "Update: the train is delayed.", "Actually, the shop is closed.",
    "Update: the project deadline moved to Friday at 7 pm.",
    "Instead, the bus is delayed tomorrow.", "Correction: the package is delayed.",
    "Actually, it is the train that is delayed.",
    "Update: the dinner train is delayed.", "Actually, dinner tickets are canceled.",
])
def test_discourse_marker_cannot_supply_missing_event_identity(semantic_model, later):
    first = "Dinner confirmed Friday at 7 pm."
    out = summarize([(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + later)])
    assert "Latest report:" not in out and "superseded" not in out
    assert first.rstrip(".") in out and later.rstrip(".") in out


@pytest.mark.parametrize("noun,first_id,last_id,match", [
    ("Invoice", "#123", "#456", False), ("Invoice", "#123", "#123", True),
    ("Flight", "123", "456", False), ("Flight", "123", "123", True),
    ("Order", "#123", "#456", False), ("Order", "#123", "#123", True),
    ("Flight", "AB123", "CD456", False), ("Flight", "AB123", "AB123", True),
    ("Invoice", "#123", "", False), ("Invoice", "", "#123", False),
    ("Order", "123-A", "123-B", False), ("Order", "123-A", "123-A", True),
    ("Invoice", "#2026-09-01", "#2026-09-02", False),
    ("Invoice", "#2026-09-01", "#2026-09-01", True),
])
def test_numeric_event_identifiers_are_material(semantic_model, noun, first_id, last_id, match):
    first = f"{noun} {first_id} confirmed tomorrow."
    last = f"{noun} {last_id} canceled tomorrow."
    out = summarize([(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + last)])
    assert ("Latest report:" in out) == match
    assert " ".join(first.rstrip(".").split()) in out
    assert " ".join(last.rstrip(".").split()) in out


@pytest.mark.parametrize("first,last,match", [
    ("Dinner confirmed Friday at 7 pm.", "Dinner moved from Friday to Saturday.", True),
    ("Dinner confirmed Friday at 7 pm.", "Dinner moved from Saturday to Sunday.", False),
    ("Dinner confirmed Friday at 7 pm.", "Dinner on Friday rescheduled to Sunday.", True),
    ("Dinner confirmed Friday at 7 pm.", "Dinner on Saturday rescheduled to Sunday.", False),
    ("Dinner confirmed Friday at 7 pm.", "Dinner at 7 pm rescheduled to 9 pm.", True),
    ("Dinner confirmed Friday at 7 pm.", "Dinner at 8 pm rescheduled to 9 pm.", False),
    ("Dinner confirmed Friday.", "Dinner at 7 pm rescheduled to 9 pm.", False),
    ("Dinner confirmed at 7 pm.", "Dinner moved from Friday to Saturday.", False),
    ("Dinner confirmed Friday at 7 pm.", "Dinner moved from Friday at 7 pm to Saturday at 8 pm.", True),
    ("Dinner confirmed Friday at 7 pm.", "Dinner moved from Friday at 8 pm to Saturday at 9 pm.", False),
    ("Dinner confirmed Friday at 7 pm.", "Dinner moved from Friday.", False),
    ("Dinner confirmed Friday at 7 pm.", "Dinner rescheduled to Saturday at 8 pm.", True),
    ("Flight 123 from Boston confirmed tomorrow.", "Flight 456 from Boston delayed tomorrow.", False),
    ("Flight 123 from Boston confirmed tomorrow.", "Flight 123 from Boston delayed tomorrow.", True),
    ("Cafe Blue dinner confirmed Friday.", "Cafe Red dinner is off.", False),
    ("Dinner train confirmed Friday.", "Dinner is off.", False),
    ("Dinner tickets confirmed Friday.", "Dinner is off.", False),
    ("Dinner at Cafe Blue confirmed Friday.", "Dinner is off.", False),
    ("Dinner with Sam confirmed Friday.", "Dinner is off.", False),
    ("Dinner is at Cafe Blue, confirmed Friday at 7 pm.", "Dinner is at Cafe Red, canceled Friday at 7 pm.", False),
    ("Flight is confirmed, number 123.", "Flight is canceled, number 456.", False),
    ("Dinner confirmed Friday at 7 pm.", "Dinner moved from Cafe Red Friday to Cafe Blue Saturday.", False),
    ("Invoice #2026-09-01 increased to $2500.", "Update: Invoice #2026-09-02 decreased to $1500.", False),
])
def test_origin_constraints_and_full_subjects_control_replacement(semantic_model, first, last, match):
    out = summarize([(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + last)])
    assert ("Latest report:" in out) == match
    assert first.rstrip(".") in out and last.rstrip(".") in out


@pytest.mark.parametrize("origin,expected", [("Friday", True), ("Saturday", True), ("Monday", False), ("", False)])
def test_origin_must_uniquely_select_among_prior_events(semantic_model, origin, expected):
    first = "Dinner confirmed Friday at 7 pm."
    second = "Dinner confirmed Saturday at 7 pm."
    last = f"Dinner moved {'from ' + origin + ' ' if origin else ''}to Sunday."
    out = summarize([(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + second),
                     (3, "Alex", "Alex: " + last)])
    assert ("Latest report:" in out) == expected
    if expected:
        assert f"Dinner confirmed {origin} at 7 pm" in out.split("previous report from")[1]
    else:
        assert first.rstrip(".") in out and second.rstrip(".") in out
    assert last.rstrip(".") in out


@pytest.mark.parametrize("last,changes", [
    ("Dinner moved from Friday to Sunday.", 1),
    ("Dinner moved from Saturday to Sunday.", 2),
    ("Dinner at 7 pm rescheduled to 9 pm.", 1),
])
def test_move_destination_does_not_inherit_stale_origin(semantic_model, last, changes):
    out = summarize([(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, "Alex", "Alex: Dinner moved from Friday to Saturday."),
                     (3, "Alex", "Alex: " + last)])
    assert f"{changes} earlier reports superseded" in out
    assert ("2 earlier reports superseded" in out) == (changes == 2)
    assert last.rstrip(".") in out


@pytest.mark.parametrize("context,sender", [("Other", "Alex"), ("Alex", "Casey")])
def test_matching_id_and_origin_cannot_cross_conversation_or_actor(semantic_model, context, sender):
    out = summarize([(1, "Alex", "Alex: Flight 123 confirmed Friday at 7 pm."),
                     (2, context, sender + ": Flight 123 moved from Friday to Saturday.")])
    assert "Latest report:" not in out and "superseded" not in out
    assert "confirmed Friday at 7 pm" in out and "moved from Friday to Saturday" in out


@pytest.mark.parametrize("first,intervening,last", [
    ("Dinner confirmed Friday at 7 pm.", "The train is on time tomorrow.", "It is delayed."),
    ("Dinner confirmed Friday at 7 pm.", "The concert is confirmed Saturday at 8 pm.", "It is canceled."),
    ("Dinner confirmed Friday at 7 pm.", "The package is arriving tomorrow.", "Actually, it is delayed."),
    ("Dinner confirmed Friday at 7 pm.", "The package arrived at the depot.", "It is delayed."),
    ("Rent increased to $2500 today.", "The account balance increased to $3500 today.", "Actually $1500, not $3500."),
    ("Dinner confirmed Friday at 7 pm.", "Is the train delayed?", "It is canceled."),
    ("Dinner confirmed Friday at 7 pm.", "Please check the package.", "It is delayed."),
    ("Dinner confirmed Friday at 7 pm.", "The dinner train is on time tomorrow.", "It is delayed."),
    ("Rent increased to $2500 today.", "Flight 123 confirmed tomorrow.", "Actually $1500, not $2500."),
])
def test_implicit_corrections_cannot_skip_substantive_context(semantic_model, first, intervening, last):
    rows = [(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + intervening),
            (3, "Alex", "Alex: " + last)]
    out = summarize(rows)
    assert "Latest report:" not in out and "earlier reports superseded" not in out
    # The renderer may omit an older item under its per-category budget; the
    # analyzer must retain each independent report rather than supersede it.
    group = D.analyze(rows, ["", "", ""])[0]
    retained = " ".join(text for values in group.signals.values() for text in values)
    assert all(body.rstrip(".?") in retained for body in (first, intervening, last))
    assert len(out) <= D.MAX_OUTPUT_CHARS


@pytest.mark.parametrize("combined", [False, True])
def test_context_boundary_follows_clause_order_not_timestamp(semantic_model, combined):
    first = "Dinner confirmed Friday at 7 pm."
    middle = "The train is on time tomorrow."
    last = "It is delayed."
    texts = [first + " " + middle + " " + last] if combined else [first, middle, last]
    out = summarize([(1, "Alex", "Alex: " + body) for body in texts])
    assert "Latest report:" not in out and "earlier reports superseded" not in out
    assert first.rstrip(".") in out and middle.rstrip(".") in out and last.rstrip(".") in out


@pytest.mark.parametrize("context,sender,intervening,expected", [
    ("Alex", "Casey", "Flight 123 confirmed tomorrow.", False),
    ("Other", "Alex", "The train is on time tomorrow.", True),
    ("Alex", "Alex", "Thanks!", True),
    ("Alex", "Alex", "Flight 123 confirmed tomorrow.", False),
])
def test_implicit_context_keeps_conversation_and_ambiguity_rules(
        semantic_model, context, sender, intervening, expected):
    out = summarize([(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, context, sender + ": " + intervening), (3, "Alex", "Alex: It is canceled.")])
    assert ("Latest report:" in out) == expected
    if context == "Alex" and sender == "Alex" and intervening.startswith("Flight"):
        assert "several earlier events may match" in out


def test_unresolved_implicit_reports_cannot_start_a_supersession_chain(semantic_model):
    out = summarize([(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, "Alex", "Alex: The train is on time tomorrow."),
                     (3, "Alex", "Alex: It is delayed."), (4, "Alex", "Alex: It is canceled.")])
    assert "Latest report:" not in out and "earlier reports superseded" not in out
    assert "Dinner confirmed Friday at 7 pm" in out


@pytest.mark.parametrize("first,last", [
    ("Dinner confirmed Friday at 7 pm.", "It is canceled."),
    ("Rent increased to $2500 today.", "Actually $1500, not $2500."),
])
def test_established_adjacent_reports_still_accept_implicit_corrections(semantic_model, first, last):
    out = summarize([(1, "Alex", "Alex: " + first), (2, "Alex", "Alex: " + last)])
    assert "Latest report:" in out and "1 earlier reports superseded" in out
    assert first.rstrip(".") in out and last.rstrip(".") in out


def test_explicit_reference_can_revisit_event_after_untracked_context(semantic_model):
    out = summarize([(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, "Alex", "Alex: The train is on time tomorrow."),
                     (3, "Alex", "Alex: Dinner is off.")])
    assert "Latest report:" in out and "1 earlier reports superseded" in out
    assert "Dinner is off" in out and "The train is on time tomorrow" in out


def _cutoff_context_body(hidden=""):
    prefix = " ".join(f"We reviewed itinerary item {i} with the coordinator." for i in range(76))
    prefix += " The route was rechecked. "
    assert len(prefix) == 3967
    return prefix + "Dinner confirmed Friday at 7 pm." + (" " + hidden if hidden else "")


@pytest.mark.parametrize("hidden", ["The train is on time tomorrow.", "The concert is confirmed Saturday at 8 pm."])
@pytest.mark.parametrize("acknowledgment", [False, True])
def test_unread_suffix_cannot_establish_implicit_adjacency(semantic_model, hidden, acknowledgment):
    body = _cutoff_context_body(hidden)
    assert len(body) > D.MAX_BODY_CHARS
    assert body[:D.MAX_BODY_CHARS].endswith("Dinner confirmed Friday at 7 pm. ")
    assert hidden not in body[:D.MAX_BODY_CHARS]
    texts = [body] + (["Thanks!"] if acknowledgment else []) + ["It is delayed."]
    out = summarize([(i, "Alex", "Alex: " + text) for i, text in enumerate(texts)])
    assert "Long messages analyzed only in part" in out
    assert "Latest report:" not in out and "earlier reports superseded" not in out
    assert "Dinner confirmed Friday at 7 pm" in out and "It is delayed" in out
    assert len(out) <= D.MAX_OUTPUT_CHARS


@pytest.mark.parametrize("pad_to_limit", [False, True])
def test_complete_message_at_cutoff_retains_valid_implicit_context(semantic_model, pad_to_limit):
    body = _cutoff_context_body()
    if pad_to_limit:
        body += " " * (D.MAX_BODY_CHARS - len(body))
    assert len(body) <= D.MAX_BODY_CHARS
    out = summarize([(1, "Alex", "Alex: " + body), (2, "Alex", "Alex: It is delayed.")])
    assert "1 earlier reports superseded" in out
    assert "Long messages analyzed only in part" not in out


@pytest.mark.parametrize("subject", ["The train is on time", "The package arrived at the depot"])
@pytest.mark.parametrize("layout", ["rows", ". ", "; ", "\n"])
def test_filtered_substantive_clause_breaks_implicit_context(semantic_model, subject, layout):
    middle = " ".join([subject] * 20)
    texts = ["Dinner confirmed Friday at 7 pm", middle, "It is delayed"]
    rows = ([(i, "Alex", "Alex: " + text + ".") for i, text in enumerate(texts)] if layout == "rows"
            else [(1, "Alex", "Alex: " + layout.join(texts) + ".")])
    out = summarize(rows)
    assert "Latest report:" not in out and "earlier reports superseded" not in out
    assert "Dinner confirmed Friday at 7 pm" in out and "It is delayed" in out
    assert len(out) <= D.MAX_OUTPUT_CHARS


@pytest.mark.parametrize("omission", ["truncated", "filtered"])
def test_fresh_explicit_report_restores_context_after_omission(semantic_model, omission):
    texts = ([_cutoff_context_body("The train is on time tomorrow.")] if omission == "truncated" else
             ["Dinner confirmed Friday at 7 pm.", "The train is on time " * 20 + "."])
    texts += ["Dinner is off.", "It is confirmed."]
    out = summarize([(i, "Alex", "Alex: " + text) for i, text in enumerate(texts)])
    assert "2 earlier reports superseded" in out
    current, previous = out.split("previous report from", 1)
    assert "It is confirmed" in current and "Dinner is off" in previous


@pytest.mark.parametrize("omission", ["truncated", "filtered"])
def test_omitted_context_does_not_start_unresolved_pronoun_chain(semantic_model, omission):
    texts = ([_cutoff_context_body("The train is on time tomorrow.")] if omission == "truncated" else
             ["Dinner confirmed Friday at 7 pm.", "The package arrived at the depot " * 20 + "."])
    texts += ["It is delayed.", "It is canceled."]
    out = summarize([(i, "Alex", "Alex: " + text) for i, text in enumerate(texts)])
    assert "Latest report:" not in out and "earlier reports superseded" not in out


@pytest.mark.parametrize("omitted", [_cutoff_context_body("The train is on time tomorrow."), "The train is on time " * 20 + "."])
def test_omission_boundary_stays_with_its_conversation(semantic_model, omitted):
    out = summarize([(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
                     (2, "Other", "Alex: " + omitted), (3, "Alex", "Alex: It is canceled.")])
    assert "1 earlier reports superseded" in out


@pytest.mark.parametrize("terminal", ["It is canceled", "Dinner is canceled"])
@pytest.mark.parametrize("same_message", [False, True])
@pytest.mark.parametrize("hidden_suffix", [False, True])
def test_terminal_fragment_requires_complete_source(semantic_model, terminal, same_message, hidden_suffix):
    first = "Dinner confirmed Friday at 7 pm."
    prefix = _cutoff_context_body().split("Dinner confirmed", 1)[0]
    tail = (first + " " if same_message else "") + terminal
    # Padding is before the meaningful tail so the source cut falls inside the
    # conditional clause, leaving a short but incomplete apparent cancellation.
    if len(prefix) + len(tail) > D.MAX_BODY_CHARS:
        prefix = "We reviewed itinerary details. " * 100
    prefix += " " * (D.MAX_BODY_CHARS - len(prefix) - len(tail))
    body = prefix + tail + (" only if the train is canceled." if hidden_suffix else "")
    assert body[:D.MAX_BODY_CHARS].endswith(terminal)
    texts = [body] if same_message else [first, body]
    texts.append("It is confirmed.")
    out = summarize([(i, "Alex", "Alex: " + text) for i, text in enumerate(texts)])
    expected = not hidden_suffix and (same_message or terminal.startswith("Dinner"))
    assert ("Latest report:" in out) == expected
    assert ("2 earlier reports superseded" in out) == expected
    assert ("Long messages analyzed only in part" in out) == hidden_suffix


def _public_digest(monkeypatch, path, source, mode):
    cache(monkeypatch, source)
    start, end, _ = M._day_bounds("today")
    monkeypatch.setattr(M, "resolve_span", lambda _: (start, end + 86400, "Synthetic two days"))
    async def topics(_model, request, **kwargs):
        if mode == "offline":
            raise RuntimeError("synthetic offline")
        candidates = json.loads(request[1]["content"])
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({key: values[:2] for key, values in candidates.items()})}}]}
    chat = client(monkeypatch)
    chat.side_effect = topics
    args = {"day": {"day": "today"}, "period": {"period": "synthetic"}, "recent": {"count": 30}}[path]
    out = asyncio.run(M.summarize_messages(**args))
    assert chat.await_count == 1
    assert len(out) <= D.MAX_OUTPUT_CHARS
    assert ("Basic digest" in out) == (mode == "offline")
    assert "source_before" not in out and "source_after" not in out
    assert "source_before" not in str(chat.call_args) and "source_after" not in str(chat.call_args)
    return out


@pytest.mark.parametrize("path", ["day", "period", "recent"])
@pytest.mark.parametrize("mode", ["offline", "valid"])
@pytest.mark.parametrize("duplicate", ["The train is on time.", "The TRAIN is on time.", "The train  is on time."])
@pytest.mark.parametrize("intervening_actor", ["Alex", "Casey"])
def test_public_dedup_preserves_substantive_attachment_boundary(monkeypatch, path, mode, duplicate, intervening_actor):
    start, _, _ = M._day_bounds("today")
    source = [(start + 1, "Synthetic Alex", "Alex: Dinner confirmed Friday at 7 pm."),
              (start + 2, "Synthetic Alex", intervening_actor + ": The train is on time."),
              (start + 3, "Synthetic Alex", "Alex: It is delayed."),
              (start + 4, "Synthetic Alex", intervening_actor + ": " + duplicate)]
    out = _public_digest(monkeypatch, path, source, mode)
    assert "Latest report:" not in out and "earlier reports superseded" not in out
    assert "Dinner confirmed Friday at 7 pm" in out and "It is delayed" in out
    assert "3 messages across 1 conversations" in out


@pytest.mark.parametrize("path", ["day", "period", "recent"])
@pytest.mark.parametrize("mode", ["offline", "valid"])
@pytest.mark.parametrize("control", ["absent", "other_chat", "other_day", "reaction", "explicit", "reestablished"])
def test_public_duplicate_controls_preserve_supported_behavior(monkeypatch, path, mode, control):
    start, _, _ = M._day_bounds("today")
    middle = "Thanks!" if control == "reaction" else "The train is on time."
    last = "Dinner is off." if control == "explicit" else "It is canceled."
    source = [(start + 1, "Synthetic Alex", "Alex: Dinner confirmed Friday at 7 pm."),
              (start + 2, "Synthetic Alex", "Alex: " + middle),
              (start + 3, "Synthetic Alex", "Alex: " + last)]
    if control != "absent":
        source.append((start + (86404 if control == "other_day" else 4),
                       "Synthetic Other" if control == "other_chat" else "Synthetic Alex", "Alex: " + middle))
    if control == "reestablished":
        source.extend([(start + 5, "Synthetic Alex", "Alex: Dinner is off."),
                       (start + 6, "Synthetic Alex", "Alex: It is confirmed.")])
    out = _public_digest(monkeypatch, path, source, mode)
    expected = control in {"reaction", "explicit", "reestablished"}
    assert ("Latest report:" in out) == expected
    assert ("2 earlier reports superseded" in out) == (control == "reestablished")


@pytest.mark.parametrize("repeat_filter", [False, True])
def test_filter_sort_and_recent_selection_keep_text_free_source_provenance(repeat_filter):
    source = [(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
              (2, "Alex", "Alex: The train is on time."),
              (3, "Alex", "Alex: It is delayed."),
              (4, "Alex", "Alex: The TRAIN is on time.")]
    kept = M.filter_summary_message_rows(list(reversed(source)))
    assert kept == [source[3], source[2], source[0]]
    assert all(len(row) == 3 and isinstance(row, tuple) for row in kept)
    assert all(set(vars(row)) == {"source_before", "source_after"} for row in kept)
    assert all(isinstance(value, int) for row in kept for value in vars(row).values())
    if repeat_filter:
        kept = M.filter_summary_message_rows(kept)
    kept.sort(key=lambda row: row[0])
    selected, _ = M._recent_rows(sorted(kept, key=lambda row: row[0], reverse=True), 30)
    out = summarize(selected)
    assert "Latest report:" not in out and "earlier reports superseded" not in out


def test_recent_selection_gap_cannot_skip_a_substantive_source_row():
    source = [(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
              (2, "Alex", "Alex: The package arrived."),
              (3, "Alex", "Alex: It is canceled.")]
    annotated = M.filter_summary_message_rows(list(reversed(source)))
    # Row-level provenance must survive a later selection, independently of
    # whether the omitted row was a duplicate at the initial filter boundary.
    selected = [row for row in annotated if row[0] != 2]
    out = summarize(selected)
    assert "Latest report:" not in out and "earlier reports superseded" not in out


def test_filtered_private_noise_is_only_a_context_boundary():
    source = [(1, "Alex", "Alex: Dinner confirmed Friday at 7 pm."),
              (2, "Alex", "Alex: Verification code 123456 SYNTHETIC_PRIVATE_MARKER."),
              (3, "Alex", "Alex: It is canceled.")]
    kept = M.filter_summary_message_rows(list(reversed(source)))
    with debug_capture.capture() as records:
        out = summarize(kept)
    assert "Latest report:" not in out and "earlier reports superseded" not in out
    assert "SYNTHETIC_PRIVATE_MARKER" not in out and "SYNTHETIC_PRIVATE_MARKER" not in str(records)
