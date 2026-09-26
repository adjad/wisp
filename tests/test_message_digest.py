"""Synthetic-only summary regressions. No server or native Messages access."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from service import debug_capture
from service.tools import imessage_tools as M
from service.tools import imessage_tools as messages
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
    assert "Messages digest" in out and "Here's what stood out" in out
    assert "Basic digest" not in out and "not verified outcomes" not in out
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
                 "Reply check", "group question/request", "You: commitment",
                 "Here's what stood out"):
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
    assert "Basic digest" not in summarize(ROWS)


@pytest.mark.parametrize("content", ["", " ", "null", "{}", "[]", "not json",
    "[Alex -> Group Dinner] raw transcript", "x" * 9000,
    '{"0":["Invented Person confirmed everything"]}', '{"0": ["meals", "meals"]}'])
def test_empty_malformed_echo_and_oversize_model_text_cannot_be_an_answer(monkeypatch, content):
    client(monkeypatch, content)
    out = summarize(ROWS)
    assert "Basic digest" not in out and len(out) <= D.MAX_OUTPUT_CHARS
    assert "Invented Person" not in out and "raw transcript" not in out


@pytest.mark.parametrize("finish", ["length", "error", None, "tool_calls"])
def test_incomplete_model_results_degrade(monkeypatch, finish):
    client(monkeypatch, '{"0":["meals"]}', finish=finish)
    assert "Basic digest" not in summarize(ROWS)


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
    assert "Basic digest" not in summarize(ROWS)
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
    identities = {ctx: index + 1 for index, ctx in enumerate(dict.fromkeys(
        context for _ts, context, _text in rows))}
    monkeypatch.setattr(M, "_lines", "\n".join(
        f"V2 | {ts} | U | chat:{identities[ctx]} | {ctx} | {txt}"
        for ts, ctx, txt in rows))


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
    cache(monkeypatch, [(time.time(), "Alex", "Alex: Can you check the sale on train tickets?")])
    assert "Alex" in asyncio.run(M.summarize_messages(count=30))


def test_empty_day_and_empty_sync_never_call_model(monkeypatch):
    chat = client(monkeypatch)
    assert "No substantive messages" in asyncio.run(M.summarize_messages(day="today"))
    assert "No substantive messages" in asyncio.run(M.summarize_messages(period="this week"))
    assert "No substantive messages" in asyncio.run(M.summarize_messages())
    chat.assert_not_called()


def test_broad_summary_uses_importance_independently_of_read_state(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        'V2 | 1 | U | chat:10 | Alex | Alex: A routine unread update.',
        'V2 | 2 | R | chat:10 | Alex | Alex: A routine read update.',
        'V2 | 3 | R | chat:11 | Casey | Casey: Can you send the report by Friday?',
        'V2 | 4 | R | chat:12 | Family | Mom: The blue mug is on the counter.',
        'V2 | 5 | R | chat:12 | Family | Mom: The appointment was moved to tomorrow.',
    ]))
    rows = M.summary_message_rows()
    bodies = [text for _ts, _context, text in rows]
    assert not any("routine unread" in text for text in bodies)
    assert any("send the report" in text for text in bodies)
    assert any("appointment was moved" in text for text in bodies)
    assert not any("routine read" in text for text in bodies)
    assert not any("blue mug" in text for text in bodies)


def test_clearly_resolved_read_request_is_not_repeated(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        'V2 | 1 | R | chat:10 | Alex | Alex: Can you send the signed document?',
        'V2 | 2 | R | chat:10 | Alex | Me: Sent the signed document; it is done.',
    ]))
    assert not any("send the signed document" in text for _ts, _context, text
                   in M.summary_message_rows())


def test_read_request_remains_open_after_acknowledgment(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        'V2 | 1 | R | chat:10 | Alex | Alex: Can you send the signed report?',
        'V2 | 2 | R | chat:10 | Alex | Me: Will do.',
        'V2 | 3 | R | chat:10 | Alex | Me: Okay, thanks.',
    ]))
    assert any("signed report" in text for _ts, _context, text
                   in M.summary_message_rows())


def test_read_request_remains_open_after_unrelated_completion(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        "V2 | 1 | R | chat:10 | Alex | Alex: Can you send the budget document?",
        "V2 | 2 | R | chat:10 | Alex | Me: I uploaded the travel document; it is done.",
    ]))
    assert any("budget document" in text for _ts, _context, text
                   in M.summary_message_rows())


@pytest.mark.parametrize("state", ["X", "x", "Unread", "?", ""])
def test_invalid_new_read_state_is_not_treated_as_legacy(monkeypatch, state):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", f'V2 | 1 | {state} | chat:10 | Alex | Alex: routine read line')
    assert M._parse_records() == []
    assert M.summary_message_rows() == []


@pytest.mark.parametrize("identity", ["chat:0", "chat:-0", "10", "not-an-id"])
def test_invalid_new_conversation_identity_is_rejected(monkeypatch, identity):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", f'V2 | 1 | U | {identity} | Alex | Alex: unread line')
    assert M._parse_records() == []


@pytest.mark.parametrize("identity", ["handle:42", "message:42"])
def test_typed_native_fallback_identity_preserves_orphaned_rows(monkeypatch, identity):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", f'V2 | 1 | U | {identity} | Alex | Alex: orphaned unread line')
    assert M._parse_records() == [(1.0, identity, "Alex", "Alex: orphaned unread line", True)]
    assert M._parse_lines() == [(1.0, "Alex", "Alex: orphaned unread line")]


@pytest.mark.parametrize("state,identity", [("x", "bad"), ("Unread", "none"), ("?", "-")])
def test_combined_invalid_versioned_fields_fail_closed(monkeypatch, state, identity):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines",
                        f"V2 | 1 | {state} | {identity} | Alex | Alex: secret")
    assert M._parse_records() == []
    assert M.summary_message_rows() == []


def test_typed_orphan_namespaces_cannot_cross_select(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        "V2 | 1 | R | handle:42 | Alex | Alex: selected private row.",
        "V2 | 2 | R | message:42 | Unknown | Unknown: unrelated secret row.",
    ]))
    chat = client(monkeypatch)
    asyncio.run(M.summarize_messages(conversation="Alex"))
    assert "private row" in str(chat.call_args)
    assert "unrelated secret row" not in str(chat.call_args)


def test_routine_dated_plan_is_not_critical_when_already_read(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        "V2 | 1 | R | chat:41 | Alex | Alex: The weather is nice today.",
        "V2 | 2 | R | chat:41 | Alex | Alex: Dinner is tomorrow at 7 pm.",
    ]))
    rows = M.summary_message_rows()
    assert not any("weather" in text for _ts, _context, text in rows)
    assert not any("Dinner is tomorrow" in text for _ts, _context, text in rows)


@pytest.mark.parametrize("body", [
    "Call me when you can.",
    "FaceTime me now.",
    "Meet me here at the library.",
    "Please pick me up at 7.",
    "Mom got hurt and needs help.",
    "I'm in the hospital.",
    "The meeting was moved to 7 pm.",
    "No one got hurt, but I am in danger.",
    "Don't call me, but please pick me up.",
    "The meeting was not moved, but the appointment was moved to 7 pm.",
    "No one got hurt and I am in danger.",
    "Do not call me and pick me up at 7.",
    "The meeting was not moved and the appointment was canceled.",
    "The meeting with Alex and Casey was canceled.",
    "The appointment with Mom and Dad was moved.",
    "Our pickup with Ben and Sam was canceled.",
    "The meeting was not moved and the appointment with Mom and Dad was canceled.",
    "What if someone got hurt? I am in danger.",
])
def test_critical_read_messages_are_retained(monkeypatch, body):
    monkeypatch.setattr(M, "_lines",
                        f"V2 | 1 | R | chat:41 | Alex | Alex: {body}")
    assert len(M.summary_message_rows()) == 1


@pytest.mark.parametrize("body", [
    "Dinner is tomorrow at 7.",
    "The weather is nice today.",
    "Don't call me.",
    "What if someone got hurt?",
    "No one got hurt.",
    "The meeting was not moved.",
    "Need help with algebra homework.",
    "No one got hurt and no one is in danger.",
    "Don't call me and don't pick me up.",
    "The meeting was not moved and the appointment was not canceled.",
    "No one was hospitalized.",
    "No one got hurt and nobody is in danger.",
    "This isn't an emergency.",
    "What if Alex got hurt and was hospitalized?",
    "Imagine the meeting was canceled and the appointment was moved.",
    "What if I am in danger and need an ambulance?",
])
def test_noncritical_read_messages_are_excluded(monkeypatch, body):
    monkeypatch.setattr(M, "_lines",
                        f"V2 | 1 | R | chat:41 | Alex | Alex: {body}")
    assert M.summary_message_rows() == []


def test_read_group_request_to_another_person_is_not_automatic_priority(monkeypatch):
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi Jain")
    monkeypatch.setattr(M, "_contacts", {"1": "Blair", "2": "Casey"})
    monkeypatch.setattr(M, "_lines", "\n".join([
        'V2 | 1 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Blair call me.',
        'V2 | 2 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Unknown call me.',
        'V2 | 3 | U | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Blair call me.',
        'V2 | 4 | R | chat:42 | Alex | Alex: Call me.',
        'V2 | 5 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Adi Jain, call me now.',
        'V2 | 6 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Casey: @Blair, I am in the hospital.',
        'V2 | 7 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Adi can you call me?',
        'V2 | 8 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Adi Jain Smith, call me.',
    ]))
    rows = M.summary_message_rows(require_read_state=True)
    assert [text for _ts, _context, text in rows] == [
        "Casey: @Blair, I am in the hospital.",
        "Alex: @Adi Jain, call me now.",
        "Alex: Call me.",
    ]


def test_three_part_local_name_is_recognized_without_prefix_match(monkeypatch):
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Mary Ann Smith")
    monkeypatch.setattr(M, "_lines", "\n".join([
        'V2 | 1 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Mary Ann Smith, can you call me?',
        'V2 | 2 | R | chat:41 | Group of 3 (Alex, Blair, Casey) | Alex: @Mary Ann Smith Jones, call me.',
    ]))
    assert [text for _ts, _context, text in M.summary_message_rows(require_read_state=True)] == [
        "Alex: @Mary Ann Smith, can you call me?",
    ]


def test_recent_digest_requires_read_state_and_three_day_window(monkeypatch):
    now = time.time()
    monkeypatch.setattr(M, "_lines", "\n".join([
        f"V2 | {now - 2 * 86400} | U | chat:1 | Alex | Alex: Unread from two days ago.",
        f"V2 | {now - 4 * 86400} | U | chat:1 | Alex | Alex: Old unread message.",
        f"V2 | {now - 60} | R | chat:2 | Casey | Casey: FaceTime me now.",
        f"V2 | {now - 50} | R | chat:2 | Casey | Casey: Ordinary read update.",
        f"V2 | {now - 40} | U | chat:3 | 12345 | 12345: You won a prize; claim your prize.",
        f"{now - 30} | Legacy | Legacy: Unknown read state.",
    ]))
    bodies = [text for _ts, _context, text
              in M.recent_priority_message_rows(now=now)]
    assert len(bodies) == 1
    assert not any("two days ago" in text for text in bodies)
    assert any("FaceTime me" in text for text in bodies)


def test_broad_digest_and_daily_summary_share_important_message_window(monkeypatch):
    from service.assistant import brief

    now = 1_800_000_000.0
    monkeypatch.setattr(M.time, "time", lambda: now)
    monkeypatch.setattr(M, "_lines", "\n".join([
        f"V2 | {now - 3 * 86400} | U | chat:1 | Alex | Alex: Please review the Boundary report.",
        f"V2 | {now} | U | chat:1 | Alex | Alex: Please review the Current report.",
        f"V2 | {now - 3 * 86400 - 1} | U | chat:1 | Alex | Alex: Please review the Expired report.",
        f"V2 | {now + 1} | U | chat:1 | Alex | Alex: Please review the Future report.",
        f"V2 | {now - 60} | R | chat:2 | Casey | Casey: I am in the hospital.",
        f"{now - 30} | Legacy | Legacy: Unknown read state.",
    ]))
    captured = []

    async def digest(rows, label):
        captured.extend(rows)
        return label

    monkeypatch.setattr(M, "_summarize", digest)
    assert "important messages from the last three days" in asyncio.run(M.summarize_messages())
    assert [row[0] for row in captured] == [now, now - 60, now - 3 * 86400]
    assert [row[0] for row in brief._message_rows(now)] == [now, now - 60, now - 3 * 86400]
    block = brief._messages_block()
    assert "Current report" in block and "Boundary report" in block and "hospital" in block
    for excluded in ("Expired", "Future", "Unknown read"):
        assert excluded not in block


@pytest.mark.parametrize("record", [
    "V2 | {ts} | R | chat:1 | Alex | Alex: Nice weather today.",
    "{ts} | Alex | Alex: Unknown legacy state.",
])
def test_read_or_legacy_only_cache_never_falls_back_in_broad_outputs(monkeypatch, record):
    from service.assistant import brief

    now = time.time()
    monkeypatch.setattr(M, "_lines", record.format(ts=now - 60))
    chat = client(monkeypatch)
    assert M.recent_priority_message_rows(now=now) == []
    assert "No substantive messages" in asyncio.run(M.summarize_messages())
    assert brief._message_rows(now) == []
    assert brief._messages_block() == "MESSAGES: no important recent messages requiring attention."
    assert "No important recent messages" in brief._plain_messages_section(now)
    chat.assert_not_called()


def test_explicit_historical_and_named_summary_keep_read_messages(monkeypatch):
    start, _end, _label = M._day_bounds("2026-01-05")
    monkeypatch.setattr(M, "_lines",
                        f"V2 | {start + 60} | R | chat:1 | Alex | Alex: Call me now.")
    captured = []

    async def digest(rows, label):
        captured.append(rows)
        return label

    monkeypatch.setattr(M, "_summarize", digest)
    asyncio.run(M.summarize_messages(day="2026-01-05"))
    asyncio.run(M.summarize_messages(conversation="Alex"))
    assert len(captured) == 2
    assert all(rows == [(start + 60, "Alex", "Alex: Call me now.")] for rows in captured)


def test_explicit_named_group_summary_bypasses_importance_filter(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        'V2 | 1 | R | chat:10 | Group "Dinner" | Alex: The blue mug is on the counter.',
        'V2 | 2 | R | chat:10 | Group "Dinner" | Casey: The napkins are in the drawer.',
        'V2 | 3 | U | chat:11 | Group "Other" | Sam: An unread message in another chat.',
    ]))
    client(monkeypatch)
    assert not any('Group "Dinner"' == context for _ts, context, _text
                   in M.summary_message_rows())
    out = asyncio.run(M.summarize_messages(conversation="Dinner", count=30))
    assert 'Group "Dinner"' in out
    assert "2 messages across 1 conversations" in out
    assert 'Group "Other"' not in out


def test_duplicate_display_names_refuse_cross_chat_summary(monkeypatch):
    cache(monkeypatch, [])
    monkeypatch.setattr(M, "_lines", "\n".join([
        'V2 | 1 | R | chat:10 | Alex | Alex: First private chat.',
        'V2 | 2 | R | chat:11 | Alex | Alex: Second private chat.',
    ]))
    chat = client(monkeypatch)
    out = asyncio.run(M.summarize_messages(conversation="Alex"))
    assert "More than one conversation matched" in out
    assert "First private chat" not in out and "Second private chat" not in out
    chat.assert_not_called()


def test_period_wins_over_day_and_retains_quiet_thread_without_sampling(monkeypatch):
    monkeypatch.setattr(M, "resolve_span", lambda _: (0, 1000, "chosen period"))
    cache(monkeypatch, [(i, 'Group "Busy"', f"Sam: The meeting {i} was canceled.") for i in range(500)] +
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
    assert "wording about" in out and "not verified outcomes" not in out
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
    cache(monkeypatch, [(start + i, 'Group of 2 (Alex, Blair)',
                        f"Alex: @Test User, please review the project outline tomorrow. private fixture detail {i} " + "detail " * 100)
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
    assert "Messages digest" in output and "Here's what stood out" in output
    assert "Basic digest" not in output
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
        assert "Basic digest" not in out
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
    out = asyncio.run(M.summarize_messages(conversation="Synthetic Alex", **args))
    assert chat.await_count == 1
    assert len(out) <= D.MAX_OUTPUT_CHARS
    assert "Basic digest" not in out
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


def _freshness_record(ts: float, state: str, identity: int, context: str, text: str) -> str:
    return f"V2 | {ts} | {state} | chat:{identity} | {context} | {text}"


def test_general_digest_is_recent_unread_non_promotional_and_keeps_true_timestamps(
        monkeypatch):
    now = datetime(2026, 9, 24, 13, 0).timestamp()
    direct_ts = now - 2 * 86400
    billing_ts = now - 80
    fraud_trial_ts = now - 85
    enrollment_ts = now - 88
    urgent_ts = now - 90
    monkeypatch.setattr(messages, "_lines", "\n".join([
        _freshness_record(now - 60, "U", 1, "42302",
                "42302: Santa Cruz Backyard: We're 90% SOLDOUT. 17+ with college ID allowed."),
        _freshness_record(now - 70, "U", 2, "51023",
                "51023: Vim + Vigor Fitness: Get 30 days on us. No commitment, no enrollment."),
        _freshness_record(billing_ts, "U", 3, "74643",
                "74643: Your free trial ends tomorrow. You will be charged $99 unless you cancel by 5 pm."),
        _freshness_record(fraud_trial_ts, "U", 4, "74644",
                "74644: Fraud alert: Card ending 1234 was charged $950 for a free trial subscription. Reply YES or NO."),
        _freshness_record(enrollment_ts, "U", 5, "74645",
                "74645: No enrollment was found for your health coverage. Submit your form by 5 pm today."),
        _freshness_record(urgent_ts, "U", 6, "74646",
                "74646: Fraud alert: Card ending 1234 was charged $950. Reply YES or NO."),
        _freshness_record(direct_ts, "U", 7, "Alex",
                "Alex: Can you bring the signed form when we meet?"),
        _freshness_record(now - 120, "R", 8, "Casey", "Casey: Already-read update."),
        _freshness_record(now - 21 * 86400, "U", 9, "Jordan", "Jordan: Weeks-old unread update."),
        _freshness_record(now + 1, "U", 10, "Future", "Future: Future-dated update."),
        f"{now - 30} | Legacy | Legacy: Unknown read state.",
    ]))

    rows = M.recent_priority_message_rows(now=now)

    assert [(ts, context) for ts, context, _text in rows] == [
        (billing_ts, "74643"),
        (fraud_trial_ts, "74644"),
        (enrollment_ts, "74645"),
        (urgent_ts, "74646"),
        (direct_ts, "Alex"),
    ]
    assert all("42302" not in text and "51023" not in text
               for _ts, _context, text in rows)


@pytest.mark.parametrize("body", [
    "Your free membership ends tomorrow. You will be charged $99 unless you cancel by 5 pm.",
    "Your 30 days of free access ends tomorrow. You will be charged $99 unless you cancel by 5 pm.",
    "Your free personal training appointment has been rescheduled to 3 pm today. Reply YES to confirm.",
])
def test_actionable_short_code_phrases_survive_every_summary_path(monkeypatch, body):
    from service.assistant import brief

    now = datetime(2026, 9, 24, 13, 0).timestamp()
    ts = now - 60
    text = f"74643: {body}"
    monkeypatch.setattr(messages, "_lines", _freshness_record(
        ts, "U", 1, "74643", text))
    monkeypatch.setattr(messages, "_sync_completed", True)
    monkeypatch.setattr(messages, "_available", True)
    monkeypatch.setattr(messages.time, "time", lambda: now)

    async def ready(_sources):
        return None

    captured = []

    async def summarize(rows, label):
        captured.append((list(rows), label))
        return label

    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", ready)
    monkeypatch.setattr(messages, "_summarize", summarize)

    assert asyncio.run(messages.summarize_messages()) == (
        "important messages from the last three days (read or unread)")
    assert asyncio.run(messages.summarize_messages(conversation="74643")) == (
        "74643 — recent messages")
    assert [rows for rows, _label in captured] == [
        [(ts, "74643", text)],
        [(ts, "74643", text)],
    ]
    assert brief._message_rows(now) == [(ts, "74643", "", body)]


@pytest.mark.parametrize("context,body", [
    ("42302", "Santa Cruz Backyard: We're 90% SOLDOUT. 17+ with college ID allowed."),
    ("51023", "Vim + Vigor Fitness: Get 30 days on us. No commitment, no enrollment."),
])
def test_reported_short_code_ads_stay_filtered_from_every_summary_path(
        monkeypatch, context, body):
    from service.assistant import brief

    now = datetime(2026, 9, 24, 13, 0).timestamp()
    ts = now - 60
    monkeypatch.setattr(messages, "_lines", _freshness_record(
        ts, "U", 1, context, f"{context}: {body}"))
    monkeypatch.setattr(messages, "_sync_completed", True)
    monkeypatch.setattr(messages, "_available", True)
    monkeypatch.setattr(messages.time, "time", lambda: now)

    async def ready(_sources):
        return None

    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", ready)

    assert messages.recent_priority_message_rows(now=now) == []
    assert "No substantive messages" in asyncio.run(messages.summarize_messages())
    assert "No substantive messages" in asyncio.run(
        messages.summarize_messages(conversation=context))
    assert brief._message_rows(now) == []


def test_general_digest_renders_the_source_local_day_instead_of_relabeling_it(
        monkeypatch):
    now = datetime(2026, 9, 24, 13, 0).timestamp()
    source_ts = (datetime.fromtimestamp(now) - timedelta(days=2)).replace(
        hour=8, minute=15, second=0, microsecond=0).timestamp()
    monkeypatch.setattr(messages, "_lines", _freshness_record(
        source_ts, "U", 1, "Alex", "Alex: The signed form is ready for pickup."))
    monkeypatch.setattr(messages, "role_to_model", lambda _role: "test-model")

    class OfflineClient:
        async def chat(self, *_args, **_kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(messages, "_client", OfflineClient())
    rows = M.recent_priority_message_rows(now=now)
    output = asyncio.run(M._summarize(
        rows, "important messages from the last three days (read or unread)"))

    source_day = datetime.fromtimestamp(source_ts).date().isoformat()
    assert source_day in output
    assert "Yesterday" not in output


def test_explicit_group_summary_keeps_read_routine_messages(monkeypatch):
    now = datetime(2026, 9, 24, 13, 0).timestamp()
    monkeypatch.setattr(messages, "_lines", "\n".join([
        _freshness_record(now - 14 * 86400, "R", 10, 'Group "Weekend"',
                "Alex: The blue cooler is in the garage."),
        _freshness_record(now - 13 * 86400, "R", 10, 'Group "Weekend"',
                "Casey: I put the folding chairs beside it."),
        _freshness_record(now - 60, "U", 11, 'Group "Other"',
                "Sam: A recent unread message in another chat."),
    ]))
    monkeypatch.setattr(messages, "_sync_completed", True)
    monkeypatch.setattr(messages, "_available", True)

    async def ready(_sources):
        return None

    captured = []

    async def summarize(rows, label):
        captured.extend(rows)
        return label

    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", ready)
    monkeypatch.setattr(messages, "_summarize", summarize)

    result = asyncio.run(M.summarize_messages(conversation="Weekend"))

    assert result == 'Group "Weekend" — recent messages'
    assert [text for _ts, _context, text in captured] == [
        "Alex: The blue cooler is in the garage.",
        "Casey: I put the folding chairs beside it.",
    ]


@pytest.mark.parametrize("state", ["U", "R"])
@pytest.mark.parametrize("body", [
    "I am in the hospital and need help.",
    "Fraud alert: Card ending 1234 was charged $950.",
    "Your trial ends tomorrow; cancel by 5 pm to avoid a charge.",
    "Can you review the project outline?",
    "The flight was delayed to 9 pm.",
    "Your coverage was denied.",
    "I'll submit the application tomorrow.",
])
def test_authorized_importance_policy_includes_read_and_unread(monkeypatch, state, body):
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, state, 1, "Alex", "Alex: " + body))
    rows = M.recent_priority_message_rows(now=now)
    assert len(rows) == 1


@pytest.mark.parametrize("body", [
    "Dinner tomorrow at 7 pm.", "Thanks!", 'Loved “Can you call me?”',
    "Your verification code is A1B2C3.", "Flash sale, reply STOP to unsubscribe.",
])
def test_unread_does_not_make_routine_or_private_content_important(monkeypatch, body):
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "12345", "12345: " + body))
    assert M.recent_priority_message_rows(now=now) == []


@pytest.mark.parametrize("secret", ["123456", "A1B2-C3D4", "12 34 56"])
def test_incident_notice_survives_without_codes_in_any_summary_surface(monkeypatch, secret):
    from service.assistant import brief
    now = time.time()
    body = f"Fraud alert: Your account was compromised. Your security code is {secret}."
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Bank", "Bank: " + body))
    with debug_capture.capture() as records:
        out = asyncio.run(M.summarize_messages())
    assert "Bank" in out and "Unverified security incident notice" in out
    assert secret not in out + str(records) + brief._messages_block() + brief._messages_card(now)
    # Explicit named summaries apply the same privacy boundary.
    assert secret not in asyncio.run(M.summarize_messages(conversation="Bank"))


@pytest.mark.parametrize("completion", [
    "I haven't sent the budget document.", "I will send the budget document.",
    "If I sent the budget document, would that help?", "I sent the travel document.",
])
def test_uncertain_or_unrelated_completion_does_not_close_request(monkeypatch, completion):
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(1, "R", 1, "Alex", "Alex: Can you send the budget document?"),
        _freshness_record(2, "R", 1, "Alex", "Me: " + completion),
    ]))
    assert any("Can you send" in row[2] for row in M.summary_message_rows())


def test_resolved_unread_request_and_other_chat_do_not_cross_resolve(monkeypatch):
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(1, "U", 1, "Alex", "Alex: Can you send the budget document?"),
        _freshness_record(1, "R", 2, "Alex", "Alex: Can you send the budget document?"),
        _freshness_record(2, "R", 1, "Alex", "Me: Sent the budget document."),
    ]))
    selected = M.summary_message_rows()
    assert len(selected) == 1 and selected[0][1] == "Alex (conversation 2)"


@pytest.mark.parametrize("body,expected", [
    ("", "No messages were synced"),
    ("R | chat:1 | Alex | Alex: Nice weather.", "none are unread"),
    ("U | chat:1 | Alex | Alex: Nice weather.", "Routine chatter"),
])
def test_empty_answer_is_grounded_and_model_free(monkeypatch, body, expected):
    start, _, _ = M._day_bounds("today")
    monkeypatch.setattr(M, "_lines", f"V2 | {start + 1} | {body}" if body else "")
    chat = client(monkeypatch)
    out = asyncio.run(M.summarize_messages(day="today"))
    assert expected in out
    chat.assert_not_called()


def test_legacy_empty_answer_does_not_claim_all_read(monkeypatch):
    start, _, _ = M._day_bounds("today")
    monkeypatch.setattr(M, "_lines", f"{start + 1} | Alex | Alex: Call me now.")
    out = asyncio.run(M.summarize_messages(day="today"))
    assert "Read status is unavailable" in out and "none are unread" not in out


def test_urgent_conversation_survives_digest_limit():
    rows = [(i, f"Person {i}", f"Person {i}: Can you send the report?") for i in range(15)]
    rows.append((20, "Safety", "Safety: I am in danger."))
    out = summarize(rows)
    assert "in danger" in out and out.index("Safety") < out.index("Person")


def test_default_digest_keeps_latest_material_correction(monkeypatch):
    now = time.time()
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(now - 3, "R", 1, "Alex", "Alex: The meeting was moved to 7 pm."),
        _freshness_record(now - 2, "R", 1, "Alex", "Alex: It is canceled."),
    ]))
    out = asyncio.run(M.summarize_messages())
    assert "Latest report:" in out and "It is canceled" in out
    assert "1 earlier reports superseded" in out


def test_default_digest_does_not_attach_correction_across_filtered_chatter(monkeypatch):
    now = time.time()
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(now - 3, "R", 1, "Alex", "Alex: The meeting was moved to 7 pm."),
        _freshness_record(now - 2, "U", 1, "Alex", "Alex: The train is on time."),
        _freshness_record(now - 1, "R", 1, "Alex", "Alex: It is canceled."),
    ]))
    out = asyncio.run(M.summarize_messages())
    assert "Latest report:" not in out


def test_recent_budget_does_not_bury_urgent_read_notice(monkeypatch):
    now = time.time()
    lines = [_freshness_record(now - i, "U", i + 1, f"Person {i}",
                              f"Person {i}: Can you review the report?") for i in range(40)]
    lines.append(_freshness_record(now - 100, "R", 50, "Safety", "Safety: I am in danger."))
    monkeypatch.setattr(M, "_lines", "\n".join(lines))
    out = asyncio.run(M.summarize_messages(count=5))
    assert "in danger" in out


@pytest.mark.parametrize("credential", ["Your code is. ABCD-EFGH", "Security code:\n12 34 56", "Your PIN is; 9173"])
def test_incident_credential_redaction_does_not_depend_on_clause_boundaries(monkeypatch, credential):
    text = "Bank: Security alert: suspicious login. " + credential
    safe = M.redact_summary_codes(text)
    assert "Unverified security incident notice" in safe
    assert "ABCD" not in safe and "12 34 56" not in safe and "9173" not in safe
    assert M.redact_summary_codes(safe) == safe


@pytest.mark.parametrize("first,reply", [
    ("Alex: Can you send the report by Friday?", "Me: Sent the report."),
    ("Me: I'll send the budget document tomorrow.", "Me: Sent the budget document."),
])
def test_completed_requests_and_commitments_are_excluded(monkeypatch, first, reply):
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(1, "R", 1, "Alex", first),
        _freshness_record(2, "R", 1, "Alex", reply),
    ]))
    assert M.summary_message_rows() == []


@pytest.mark.parametrize("old,new", [
    ("The meeting was canceled.", "Actually, the meeting is on time."),
    ("The meeting was moved to 7 pm.", "Actually, the meeting was not moved."),
])
def test_explicit_correction_wins_over_stale_important_update(monkeypatch, old, new):
    now = time.time()
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(now - 2, "R", 1, "Alex", "Alex: " + old),
        _freshness_record(now - 1, "R", 1, "Alex", "Alex: " + new),
    ]))
    rows = M.recent_priority_message_rows(now=now)
    assert rows[0][2] == "Alex: " + new
    out = asyncio.run(M.summarize_messages())
    assert new.rstrip(".") in out and "Latest report:" in out


def test_counterfactual_completion_does_not_close_request(monkeypatch):
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(1, "R", 1, "Alex", "Alex: Please send the permit."),
        _freshness_record(2, "R", 1, "Alex", "Me: I should have sent the permit."),
    ]))
    assert len(M.summary_message_rows()) == 1


@pytest.mark.parametrize("body,secret", [
    ("Fraud alert: 123456 is your one-time authorization number.", "123456"),
    ("Suspicious login detected. Use 123456 to log in.", "123456"),
    ("Security alert: Your verification token is ABC123.", "ABC123"),
])
def test_incident_alternative_authentication_terms_do_not_leak(monkeypatch, body, secret):
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Bank", "Bank: " + body))
    with debug_capture.capture() as records:
        out = asyncio.run(M.summarize_messages())
    assert "Bank" in out and "Unverified security" in out
    assert secret not in out + str(records)


def test_software_code_review_request_retains_its_deadline(monkeypatch):
    now = time.time()
    text = "Alex: Please review the code by Friday."
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Alex", text))
    assert M.recent_priority_message_rows(now=now)[0][2] == text
    out = asyncio.run(M.summarize_messages())
    assert "review the code by Friday" in out


@pytest.mark.parametrize("request_text,completion", [
    ("review the budget report", "Reviewed the budget report."),
    ("sign the permit", "Signed the permit."),
    ("confirm the reservation", "Confirmed the reservation."),
    ("pay the invoice", "Paid the invoice."),
])
def test_supported_completed_action_verbs_close_the_request(monkeypatch, request_text, completion):
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(1, "R", 1, "Alex", "Alex: Can you " + request_text + "?"),
        _freshness_record(2, "R", 1, "Alex", "Me: " + completion),
    ]))
    assert not any("Can you" in row[2] for row in M.summary_message_rows())


def test_signed_modifier_is_not_erased_by_completion_canonicalization(monkeypatch):
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(1, "R", 1, "Alex", "Alex: Can you send the signed permit?"),
        _freshness_record(2, "R", 1, "Alex", "Me: Sent the permit."),
    ]))
    assert any("Can you" in row[2] for row in M.summary_message_rows())


@pytest.mark.parametrize("body,secret", [
    ("Fraud alert: suspicious activity on your account. Use 748291 to confirm this was you.", "748291"),
    ("Security alert: your recovery key is H4X9-Q7P2.", "H4X9-Q7P2"),
    ("Security alert: carry this special phrase ORANGE-FOX to prove ownership.", "ORANGE-FOX"),
])
def test_audit_security_notice_fails_closed_for_unfamiliar_credentials(monkeypatch, body, secret):
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Bank", "Bank: " + body))
    with debug_capture.capture() as records:
        out = asyncio.run(M.summarize_messages())
    assert "Bank" in out and "Unverified security incident notice" in out
    assert secret not in out + str(records)


@pytest.mark.parametrize("body", ["Adi, can you send the file?", "@Adi can you review the budget?"])
def test_audit_exact_group_vocative_and_unpunctuated_mention(monkeypatch, body):
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi")
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, 'Group of 3 (Alex, Blair, Casey)', "Alex: " + body))
    rows = M.recent_priority_message_rows(now=now)
    assert len(rows) == 1
    assert M.summary_addressees(rows) == ["Adi"]


def test_audit_deadline_survives_cap_before_routine_requests(monkeypatch):
    now = time.time()
    lines = [_freshness_record(now - i, "U", i + 1, f"Person {i}",
                              f"Person {i}: Can you review the report?") for i in range(10)]
    lines.append(_freshness_record(now - 100, "R", 50, "Billing", "Billing: Your payment is due tomorrow."))
    monkeypatch.setattr(M, "_lines", "\n".join(lines))
    out = asyncio.run(M.summarize_messages(count=5))
    assert "due tomorrow" in out


@pytest.mark.parametrize("body", [
    "Adi Smith, can you send the file?", "@Adi Smith can you send the file?",
    "@Aditya can you send the file?", "@Adi and @Blair can you send the file?",
])
def test_audit_group_self_name_never_matches_fuzzy_or_multiple_addressees(monkeypatch, body):
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi")
    monkeypatch.setattr(M, "_lines", _freshness_record(1, "R", 1, 'Group of 3 (Alex, Blair, Casey)', "Alex: " + body))
    assert M.summary_message_rows() == []


def test_due_request_is_prioritized_and_priority_overflow_is_disclosed(monkeypatch):
    now = time.time()
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(now - 1, "R", 1, "Routine", "Routine: Can you review the report?"),
        _freshness_record(now - 3, "R", 2, "Deadline", "Deadline: Please submit the application by 5 pm."),
        _freshness_record(now - 4, "R", 3, "Urgent", "Urgent: I am in danger."),
    ]))
    out = asyncio.run(M.summarize_messages(count=2))
    assert "by 5 pm" in out and "in danger" in out and "Routine:" not in out
    bounded = asyncio.run(M.summarize_messages(count=1))
    assert "in danger" in bounded and "1 other priority messages not shown" in bounded


@pytest.mark.parametrize("material", [
    "recovery key", "access key", "security key", "verification number",
    "private key", "API key", "recovery phrase", "seed phrase", "backup codes",
    "recovery_key", "access-key",
])
@pytest.mark.parametrize("path", ["recent", "day", "period", "conversation", "direct", "brief"])
def test_authentication_request_is_redacted_independently_of_incident(monkeypatch, material, path):
    from service.assistant import brief
    now = time.time()
    secret = "H4X9-Q7P2"
    text = f"Bank: Can you confirm the {material} is {secret}?"
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Bank", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        if path == "brief":
            out = brief._messages_block() + brief._messages_card(now)
        elif path == "direct":
            out = asyncio.run(M._summarize([(now - 1, "Bank", text)], "today"))
        else:
            args = {"recent": {}, "day": {"day": "today"},
                    "period": {"period": "this week"}, "conversation": {"conversation": "Bank"}}[path]
            out = asyncio.run(M.summarize_messages(**args))
    assert "Bank" in out and "Authentication details omitted" in out
    assert secret not in out + str(records) + str(chat.call_args_list)
    assert secret not in str(D.analyze([(now - 1, "Bank", text)], [""]))


@pytest.mark.parametrize("body,secret", [
    ("Can you confirm the secret answer is ORANGE-FOX?", "ORANGE-FOX"),
    ("Can you confirm the authenticator secret JBSWY3DPEHPK3PXP?", "JBSWY3DPEHPK3PXP"),
    ("Can you confirm the 2FA backup string A1B2-C3D4?", "A1B2-C3D4"),
    ("Can you review this recovery link https://example.invalid/recover?proof=alpha?", "https://example.invalid"),
    ("Can you review https://example.invalid/reset/ABcdEfgHij?", "ABcdEfgHij"),
    ("Can you confirm the secret response is purple fox?", "purple fox"),
    ("Can you confirm the pairing proof is lime-raven?", "lime-raven"),
    ("Please review this value X7rQ9pLm2v.", "X7rQ9pLm2v"),
    ("Please review this value 748291.", "748291"),
])
@pytest.mark.parametrize("path", ["recent", "day", "period", "conversation", "direct", "brief"])
def test_audit_open_ended_authentication_and_values_are_omitted(monkeypatch, body, secret, path):
    from service.assistant import brief
    now = time.time()
    text = "Bank: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Bank", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        if path == "brief":
            out = brief._messages_block() + brief._messages_card(now)
        elif path == "direct":
            out = asyncio.run(M._summarize([(now - 1, "Bank", text)], "today"))
        else:
            args = {"recent": {}, "day": {"day": "today"},
                    "period": {"period": "this week"}, "conversation": {"conversation": "Bank"}}[path]
            out = asyncio.run(M.summarize_messages(**args))
    assert "Bank" in out and "omitted" in out
    assert secret not in out + str(records) + str(chat.call_args_list)
    assert secret not in str(D.analyze([(now - 1, "Bank", text)], [""]))


@pytest.mark.parametrize("body", [
    "Please review the code by Friday.",
    "Can you confirm the meeting is Friday at 7 pm?",
    "Can you confirm the invoice is $2500?",
    "The meeting was moved to 2026-09-25 at 7 pm.",
])
def test_private_value_boundary_preserves_explicit_safe_schedule_amounts_and_code_review(body):
    text = "Alex: " + body
    assert M.redact_summary_codes(text) == text


def test_private_value_redaction_preserves_urgent_priority_before_count_cap(monkeypatch):
    now = time.time()
    rows = [
        _freshness_record(now - 1, "R", 1, "Routine", "Routine: Can you review the report?"),
        _freshness_record(now - 2, "R", 2, "Deadline", "Deadline: Payment due tomorrow. See https://example.invalid/private."),
        _freshness_record(now - 3, "R", 3, "Safety", "Safety: I am in danger. See https://example.invalid/location."),
    ]
    monkeypatch.setattr(M, "_lines", "\n".join(rows))
    out = asyncio.run(M.summarize_messages(count=2))
    assert "Health or safety concern" in out and "Deadline notice" in out
    assert "https" not in out and "Routine:" not in out


@pytest.mark.parametrize("body,secret", [
    ("Please review your security answer: blue river.", "blue river"),
    ("Can you confirm the challenge response purple fox?", "purple fox"),
    ("Can you tell me your security-question answer purple fox?", "purple fox"),
    ("Please review the challenge solution green harbor.", "green harbor"),
    ("Can you validate the reply teal sparrow?", "teal sparrow"),
    ("Can you confirm the response blue meadow?", "blue meadow"),
])
@pytest.mark.parametrize("path", ["recent", "day", "period", "conversation", "direct", "brief"])
def test_security_answers_and_uncertain_verification_fail_closed(monkeypatch, body, secret, path):
    from service.assistant import brief
    now = time.time()
    text = "Bank: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Bank", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        if path == "brief":
            out = brief._messages_block() + brief._messages_card(now)
        elif path == "direct":
            out = asyncio.run(M._summarize([(now - 1, "Bank", text)], "today"))
        else:
            args = {"recent": {}, "day": {"day": "today"}, "period": {"period": "this week"},
                    "conversation": {"conversation": "Bank"}}[path]
            out = asyncio.run(M.summarize_messages(**args))
    assert secret not in out + str(records) + str(chat.call_args_list)
    assert secret not in str(D.analyze([(now - 1, "Bank", text)], [""]))


def test_flight_delay_with_url_preserves_safe_fact_time_and_priority(monkeypatch):
    now = time.time()
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(now - 1, "R", 1, "Routine", "Routine: Can you review the report?"),
        _freshness_record(now - 2, "R", 2, "Airline", "Airline: Your flight AA1234 was delayed to 9 pm; details at https://airline.invalid/flight/AA1234."),
    ]))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        out = asyncio.run(M.summarize_messages(count=1))
    assert "flight AA1234 was delayed to 9 pm" in out
    assert "https" not in out + str(records) + str(chat.call_args_list)
    assert "Routine:" not in out


def test_unsafe_logistics_url_retains_change_category_and_priority(monkeypatch):
    now = time.time()
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(now - 1, "R", 1, "Routine", "Routine: Can you review the report?"),
        _freshness_record(now - 2, "R", 2, "Airline", "Airline: Your flight was delayed; details https://example.invalid. Actually, it is canceled."),
    ]))
    out = asyncio.run(M.summarize_messages(count=1))
    assert "Schedule or logistics change" in out
    assert "was delayed" not in out and "https" not in out and "Routine:" not in out


def test_url_omission_does_not_establish_a_false_implicit_antecedent():
    rows = M.filter_summary_message_rows([
        (1, "Airline", "Airline: Flight AA1234 was delayed to 9 pm; details at https://example.invalid."),
        (2, "Airline", "Airline: It is canceled."),
    ])
    out = summarize(rows)
    assert "Flight AA1234 was delayed to 9 pm" in out
    assert "earlier reports superseded" not in out


@pytest.mark.parametrize("body", [
    "Can you confirm the payment of $2500 and the account answer blue river?",
    "Can you confirm the account answer blue river and the payment is $2500?",
    "Can you confirm the invoice is $2500 and the response blue river?",
    "Please review the account answer blue river. Reply YES to confirm.",
    "The account answer is blue river. Can you confirm the payment of $2500?",
    "Please review the security policy by Friday and the account answer blue river.",
])
@pytest.mark.parametrize("path", ["recent", "day", "period", "conversation", "direct", "brief"])
def test_mixed_verification_never_borrows_safety_from_an_amount(monkeypatch, body, path):
    from service.assistant import brief
    now = time.time()
    text = "Bank: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "Bank", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        if path == "brief":
            out = brief._messages_block() + brief._messages_card(now)
        elif path == "direct":
            out = asyncio.run(M._summarize([(now - 1, "Bank", text)], "today"))
        else:
            args = {"recent": {}, "day": {"day": "today"}, "period": {"period": "this week"},
                    "conversation": {"conversation": "Bank"}}[path]
            out = asyncio.run(M.summarize_messages(**args))
    assert "blue river" not in out + str(records) + str(chat.call_args_list)
    assert "blue river" not in str(D.analyze([(now - 1, "Bank", text)], [""]))


def test_security_policy_work_keeps_action_deadline_and_priority(monkeypatch):
    from service.assistant import brief
    now = time.time()
    text = "IT: Please review the security policy by Friday."
    monkeypatch.setattr(M, "_lines", "\n".join([
        _freshness_record(now - 1, "R", 1, "Routine", "Routine: Can you review the report?"),
        _freshness_record(now - 2, "R", 2, "IT", text),
    ]))
    assert M.redact_summary_codes(text) == text
    for args in ({"count": 1}, {"day": "today"}, {"period": "this week"}, {"conversation": "IT"}):
        assert "review the security policy by Friday" in asyncio.run(M.summarize_messages(**args))
    assert "review the security policy by Friday" in brief._messages_block()


@pytest.mark.parametrize("suffix", ["by Friday, thanks.", "by Friday for the audit.", "and send feedback by Friday."])
def test_security_policy_harmless_suffixes_remain_actionable(monkeypatch, suffix):
    from service.assistant import brief
    now = time.time()
    text = "IT: Please review the security policy " + suffix
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "IT", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    assert M.redact_summary_codes(text) == text
    for args in ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "IT"}):
        out = asyncio.run(M.summarize_messages(**args))
        assert "Action items mentioned" in out and "security policy" in out and "Friday" in out
    assert "security policy" in brief._messages_block()


@pytest.mark.parametrize("suffix", [
    "by Friday, thanks, the account answer is blue river.",
    "by Friday for the audit answer blue river.",
    "and send feedback by Friday and the response blue river.",
    "by Friday; the security answer is blue river.",
])
def test_security_policy_private_qualifiers_keep_deadline_priority_without_source_values(monkeypatch, suffix):
    from service.assistant import brief
    now = time.time()
    text = "IT: Please review the security policy " + suffix
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "IT", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        for args in ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "IT"}):
            out = asyncio.run(M.summarize_messages(**args))
            assert "Action items mentioned" in out and "stated deadline" in out
            assert "Friday" not in M.redact_summary_codes(text)  # exclude the independent date header
            assert "blue river" not in out
        assert "blue river" not in brief._messages_block()
    assert "blue river" not in str(records) + str(chat.call_args_list)
    assert M.redact_summary_codes(M.redact_summary_codes(text)) == M.redact_summary_codes(text)


@pytest.mark.parametrize("body, expected", [
    ("Please review our security policy when you get a chance.", ["review our security policy"]),
    ("Please review the security policy and send me notes by Friday.", ["review the security policy", "send me notes", "Friday"]),
    ("Please read the security report by Friday and reply with comments.", ["read the security report", "reply with comments", "Friday"]),
    ("Please review our security policy after the planning discussion.", ["review our security policy"]),
])
@pytest.mark.parametrize("private", [False, True])
def test_security_work_clauses_preserve_actions_without_credential_values(monkeypatch, body, expected, private):
    from service.assistant import brief
    now = time.time()
    if private:
        body += " The account answer is blue river."
    text = "IT: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "IT", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    assert M.important_message_reason(text) == "direct_request"
    with debug_capture.capture() as records:
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "IT"})]
        outputs += [asyncio.run(M._summarize([(now - 1, "IT", text)], "today")),
                    brief._messages_block(), brief._messages_card(now)]
        for out in outputs:
            assert "blue river" not in out
            if not private:
                for phrase in expected:
                    assert phrase in out
        assert "Action items mentioned" in outputs[0]
    assert "blue river" not in str(records) + str(chat.call_args_list)
    assert M.redact_summary_codes(M.redact_summary_codes(text)) == M.redact_summary_codes(text)


@pytest.mark.parametrize("body", [
    "Please review the security policy by Friday only after approval.",
    "Please review the security policy once Legal signs off.",
    "Please review the security policy when the manager signs off.",
    "Please send the security report only to Legal after clearance.",
    "Please review the security policy by Friday subject to approval.",
])
def test_security_work_unknown_constraints_require_original_before_acting(monkeypatch, body):
    from service.assistant import brief
    now = time.time()
    text = "IT: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "IT", text))
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    for out in (asyncio.run(M.summarize_messages()), brief._messages_block(), brief._messages_card(now)):
        assert "review the original before acting" in out.lower()
        assert "qualifiers omitted" in out.lower()
    assert M.redact_summary_codes(M.redact_summary_codes(text)) == M.redact_summary_codes(text)


@pytest.mark.parametrize("body, expected", [
    ("Please review the security policy by Friday, and send me notes.", ["send me notes", "Friday"]),
    ("Please review the security policy by Friday; then send me notes.", ["send me notes", "Friday"]),
    ("Please review the security policy by next Friday.", ["by next Friday"]),
    ("Please read the security report before September 30.", ["before September 30"]),
])
@pytest.mark.parametrize("private", [False, True])
def test_security_work_punctuation_and_exact_deadlines(monkeypatch, body, expected, private):
    from service.assistant import brief
    now = time.time()
    if private:
        body += " The account answer is blue river."
    text = "IT: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "R", 1, "IT", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "IT"})]
        outputs += [brief._messages_block(), brief._messages_card(now)]
    for out in outputs:
        assert "blue river" not in out
        if not private:
            for phrase in expected:
                assert phrase in out
    assert "blue river" not in str(records) + str(chat.call_args_list)


@pytest.mark.parametrize("secret", ["6432", "2031"])
@pytest.mark.parametrize("label", ["door PIN", "one-time code"])
@pytest.mark.parametrize("order", ["date_first", "secret_first"])
@pytest.mark.parametrize("path", ["recent", "day", "period", "conversation", "direct", "brief"])
def test_credential_context_overrides_date_fragments(monkeypatch, secret, label, order, path):
    from service.assistant import brief
    now = time.time()
    body = (f"Please review the security policy by September 30, {secret} is the {label}."
            if order == "date_first" else
            f"The {label} is {secret}. Please review the security policy by September 30.")
    text = "IT: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "IT", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        selected = M.summary_message_rows(require_read_state=True)
        if path == "brief":
            out = brief._messages_block() + brief._messages_card(now)
        elif path == "direct":
            out = asyncio.run(M._summarize([(now - 1, "IT", text)], "today"))
        else:
            args = {"recent": {}, "day": {"day": "today"}, "period": {"period": "this week"},
                    "conversation": {"conversation": "IT"}}[path]
            out = asyncio.run(M.summarize_messages(**args))
    redacted = M.redact_summary_codes(text)
    assert secret not in redacted + str(selected) + out + str(records) + str(chat.call_args_list)
    assert secret not in str(D.analyze([(now - 1, "IT", text)], [""]))
    assert M.message_priority(redacted) == 1
    assert M.redact_summary_codes(redacted) == redacted


@pytest.mark.parametrize("verb", ["review", "read", "update", "draft", "send", "share", "approve", "finish", "submit"])
@pytest.mark.parametrize("read", ["R", "U"])
def test_all_supported_work_verbs_survive_routine_selection(monkeypatch, verb, read):
    from service.assistant import brief
    now = time.time()
    text = f"IT: Can you {verb} the security policy by Friday?"
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, read, 1, "IT", text))
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    assert M.important_message_reason(text) == "direct_request"
    assert M.message_priority(text) == 1
    assert len(M.summary_message_rows(require_read_state=True)) == 1
    for args in ({}, {"day": "today"}, {"period": "this week"}):
        out = asyncio.run(M.summarize_messages(**args))
        assert f"{verb} the security policy" in out and "Friday" in out
    assert f"{verb} the security policy" in brief._messages_block() + brief._messages_card(now)


@pytest.mark.parametrize("verb", ["draft", "approve", "update", "share"])
@pytest.mark.parametrize("target", ["other", "self", "promotion", "negated", "negated_deadline"])
def test_new_work_verbs_keep_group_and_noise_guards(monkeypatch, verb, target):
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi")
    now = time.time()
    context = 'Group of 3 (Alex, Blair, Casey)'
    body = f"@{'Adi' if target == 'self' else 'Blair'} can you {verb} the security policy by Friday?"
    sender = "Alex"
    if target == "promotion":
        context = sender = "51023"
        body = f"Please {verb} the security policy and get 50% off! Shop now. Reply STOP to unsubscribe."
    elif target.startswith("negated"):
        context = "Alex"
        body = f"Please do not {verb} the security policy" + (" by Friday." if target == "negated_deadline" else ".")
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, context, sender + ": " + body))
    rows = M.summary_message_rows(require_read_state=True)
    assert bool(rows) == (target == "self")
    if rows:
        assert M.summary_addressees(rows) == ["Adi"]
        assert M.summary_addressees(M.filter_summary_message_rows(rows)) == ["Adi"]
        assert "to Adi" in asyncio.run(M._summarize(rows, "today"))
        rendered = "\n".join(M.render_for_summary(rows))
        assert "NOT the user" not in rendered and "the user" in rendered
    elif target == "negated_deadline":
        assert M.important_message_reason(sender + ": " + body) is None
        for args in ({}, {"day": "today"}, {"period": "this week"}):
            assert "Deadline notice" not in asyncio.run(M.summarize_messages(**args))


def test_implausible_year_is_not_a_safe_security_work_deadline():
    text = "IT: Please review the security policy by September 30, 6432."
    out = M.redact_summary_codes(text)
    assert "6432" not in out and "September 30" in out
    assert "Review the original before acting" in out
    ordinary = "IT: Please review the security policy by September 30, 2031."
    assert M.redact_summary_codes(ordinary) == ordinary


def test_same_name_group_participant_is_not_labeled_as_verified_user(monkeypatch):
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi")
    context = "Group of 2 (Adi, Blair)"
    rows = [(time.time(), context, "Alex: @Adi, can you draft the security policy by Friday?")]
    assert not M._read_group_request_is_for_user(context, rows[0][2])
    for candidate in (rows, M.filter_summary_message_rows(rows)):
        rendered = "\n".join(M.render_for_summary(candidate))
        assert "Adi (the user)" not in rendered
        assert "NOT the user" in rendered


@pytest.mark.parametrize("same_name_member", [True, False])
def test_duplicate_group_label_keeps_roster_identity_boundary(monkeypatch, same_name_member):
    from service.assistant import brief
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi")
    now = time.time()
    context = "Group of 2 (Alex, Adi)" if same_name_member else "Group of 2 (Alex, Blair)"
    text = "Alex: @Adi, please review the report by Friday."
    monkeypatch.setattr(M, "_lines", "\n".join(
        _freshness_record(now - identity, "U", identity, context, text)
        for identity in (1, 2)))
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    parsed = M._parse_records()
    assert {row[2] for row in parsed} == {
        context + " (conversation 1)", context + " (conversation 2)"}
    for _ts, _identity, disambiguated, _text, _unread in parsed:
        assert M._label_members(disambiguated) == (["Alex", "Adi"] if same_name_member
                                                  else ["Alex", "Blair"])
        assert M._read_group_request_is_for_user(disambiguated, text) is not same_name_member
    rows = M.summary_message_rows(require_read_state=True)
    assert len(rows) == (0 if same_name_member else 2)
    raw_rows = M.filter_summary_message_rows([(now, row[2], text) for row in parsed])
    assert M.summary_addressees(raw_rows) == ["Adi", "Adi"]
    rendered = "\n".join(M.render_for_summary(raw_rows))
    assert ("Adi (the user)" in rendered) is not same_name_member
    assert ("NOT the user" in rendered) is same_name_member
    with debug_capture.capture() as records:
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"})]
        outputs += [brief._messages_block(), brief._messages_card(now)]
    assert all(("Action items mentioned" in out) is not same_name_member for out in outputs[:3])
    assert ("no important recent messages" in outputs[3]) is same_name_member
    if same_name_member:
        assert "Adi (the user)" not in str(records)


@pytest.mark.parametrize("body", [
    "Please review the security policy by Friday, and do not share it.",
    "Please review the security policy by Friday and do not share it.",
    "Do not share the policy, and please review the security policy by Friday.",
    "Please draft the security policy by Friday, but do not approve it.",
])
def test_positive_work_request_survives_separate_negated_clause(monkeypatch, body):
    from service.assistant import brief
    now = time.time()
    text = "IT: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "IT", text))
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    assert M.important_message_reason(text) == "direct_request"
    assert M.message_priority(text) == 1
    for args in ({}, {"day": "today"}, {"period": "this week"}):
        out = asyncio.run(M.summarize_messages(**args))
        assert "Action items mentioned" in out
        assert "Friday" in out
    assert "Friday" in brief._messages_block()


@pytest.mark.parametrize("context", ['Group "Team"', "Group of 3 (Alex, Blair, +1 more)", "Group of 3 (Alex, Blair)"])
@pytest.mark.parametrize("same_name_contact", [False, True])
def test_self_group_request_requires_complete_membership_evidence(monkeypatch, context, same_name_contact):
    from service.assistant import brief
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi")
    monkeypatch.setattr(M, "_contacts", {"+15550000001": "Adi"} if same_name_contact else {})
    now = time.time()
    text = "Alex: @Adi, can you draft the security policy by Friday?"
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, context, text))
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    assert not M._read_group_request_is_for_user(context, text)
    assert M.summary_message_rows(require_read_state=True) == []
    for args in ({}, {"day": "today"}, {"period": "this week"}):
        assert "Action items mentioned" not in asyncio.run(M.summarize_messages(**args))
    assert "draft" not in brief._messages_block()
    rendered = "\n".join(M.render_for_summary(M.filter_summary_message_rows([(now, context, text)])))
    assert "Adi (the user)" not in rendered


@pytest.mark.parametrize("credential, secret", [("the account answer is blue river", "blue river"), ("the door PIN is 6432", "6432")])
def test_private_group_request_keeps_debug_and_digest_recipient_in_sync(monkeypatch, credential, secret):
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Adi")
    now = time.time()
    text = f"Alex: @Adi, can you draft the security policy by Friday; {credential}."
    context = "Group of 2 (Alex, Blair)"
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, context, text))
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        out = asyncio.run(M.summarize_messages())
    assert "to Adi" in out
    assert "addressed to Adi" in str(records) and "Adi (the user)" in str(records)
    assert secret not in out + str(records)


@pytest.mark.parametrize("body", [
    "Please draft the security policy by Friday; the door PIN is 6432.",
    "The one-time code is 6432. Please review the security policy by Friday.",
    "Please review the report by Friday. Your verification code is 6432.",
    "Please submit the form by September 30, 2031; 2031 is the door PIN.",
])
def test_substantive_work_with_credential_retains_only_generic_action(monkeypatch, body):
    from service.assistant import brief
    now = time.time()
    text = "Alex: " + body
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        rows = M.summary_message_rows(require_read_state=True)
        assert len(rows) == 1
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [asyncio.run(M._summarize([(now - 1, "Alex", text)], "today")),
                    brief._messages_block(), brief._messages_card(now)]
    for out in outputs:
        assert "stated deadline" in out and "Alex" in out
        assert "6432" not in out and "2031" not in out
    assert "Action items mentioned" in outputs[0]
    assert "6432" not in str(records) + str(chat.call_args_list)
    assert "2031" not in str(records) + str(chat.call_args_list)
    assert "Friday" not in rows[0][2] and "September" not in rows[0][2]


@pytest.mark.parametrize("body", [
    "Your verification code is 6432.", "Please send the security code 6432.",
    "Your one-time PIN is 6432.", "Please do not draft the security policy by Friday; the security PIN is 6432.",
])
def test_standalone_credentials_and_negated_work_stay_omitted(monkeypatch, body):
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", "Alex: " + body))
    assert M.summary_message_rows(require_read_state=True) == []


@pytest.mark.parametrize("work_request", [
    "Please review the final report by Friday.",
    "Can you review the attached report by Friday?",
    "Please approve the revised project proposal by Friday.",
    "Please review the annotated storyboard by Friday.",
    "Please review this report by Friday.",
    "Please send the latest budget by Friday",
])
@pytest.mark.parametrize("separator", [" ", "; ", ", and "])
def test_modified_work_request_with_otp_retains_generic_deadline(monkeypatch, work_request, separator):
    from service.assistant import brief
    now = time.time()
    text = "Alex: " + work_request.rstrip(".?") + separator + "Your verification code is 6432."
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        rows = M.summary_message_rows(require_read_state=True)
        assert len(rows) == 1
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [brief._messages_block(), brief._messages_card(now)]
    for out in outputs:
        assert "stated deadline" in out and "Alex" in out and "6432" not in out
    assert "Friday" not in rows[0][2]
    assert "6432" not in str(records) + str(chat.call_args_list)


@pytest.mark.parametrize("body", [
    "Please review the attached verification code 6432.",
    "Please review the report verification code 6432.",
    "Please share the final budget verification code 6432.",
    "Please send the latest one-time PIN 6432.",
    "Can you confirm your verification code 6432?",
    "Please do not review the final report by Friday. Your verification code is 6432.",
    "Your report verification code is 6432.",
])
def test_credential_only_or_negated_modified_requests_stay_noise(monkeypatch, body):
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", "Alex: " + body))
    assert M.summary_message_rows(require_read_state=True) == []


@pytest.mark.parametrize("body", [
    "Please send by Friday your verification code 6432.",
    "Please send urgently by Friday your verification code 6432.",
    "Your verification code is 6432. Please send it back to me.",
    "Your verification code is 6432. Please share it securely.",
    "Your verification code is 6432. Please share this securely.",
    "Your verification code is 6432. Please share those quietly.",
    "Please share by 5 pm the latest one-time PIN 6432.",
    "Your verification code is 6432. Please send it to me.",
    "Your verification code is 6432, please send it to me.",
    "Your verification code is 6432. Please share this by Friday.",
])
def test_dates_and_pronouns_do_not_turn_credentials_into_work(monkeypatch, body):
    from service.assistant import brief
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", "Alex: " + body))
    client(monkeypatch, error=RuntimeError("synthetic offline"))
    assert M.summary_message_rows(require_read_state=True) == []
    for args in ({}, {"day": "today"}, {"period": "this week"}):
        assert "Action items mentioned" not in asyncio.run(M.summarize_messages(**args))
    assert "Alex" not in brief._messages_block()


@pytest.mark.parametrize("work_request", [
    "Please review onboarding checklist by Friday.",
    "Please review final storyboard by Friday.",
    "Review the attached report by Friday.",
    "Draft onboarding checklist by Friday.",
    "Please review release readiness notes by Friday.",
    "Review this onboarding checklist by Friday.",
    "Please review my reply by Friday.",
    "Review the reply by Friday.",
    "Review reply by Friday.",
])
def test_bare_work_objects_and_imperatives_survive_separate_otp(monkeypatch, work_request):
    from service.assistant import brief
    now = time.time()
    text = "Alex: " + work_request + " Your verification code is 6432."
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        rows = M.summary_message_rows(require_read_state=True)
        assert len(rows) == 1
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [asyncio.run(M._summarize([(now - 1, "Alex", text)], "today")),
                    brief._messages_block(), brief._messages_card(now)]
    for out in outputs:
        assert "stated deadline" in out and "Alex" in out and "6432" not in out
    assert "Action items mentioned" in outputs[0]
    assert M.message_priority(rows[0][2]) == 1
    assert "Friday" not in rows[0][2]
    assert "6432" not in str(records) + str(chat.call_args_list)


@pytest.mark.parametrize("body", [
    "Review this verification code 6432 by Friday.",
    "Share the latest one-time PIN 6432 by Friday.",
    "Your verification code is 6432. Send this securely by Friday.",
    "Your verification code is 6432. Share it back to me.",
    "Do not review onboarding checklist by Friday. Your verification code is 6432.",
])
def test_credential_only_and_negated_imperatives_are_not_work(monkeypatch, body):
    now = time.time()
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", "Alex: " + body))
    assert M.summary_message_rows(require_read_state=True) == []


@pytest.mark.parametrize("work_text", [
    "Please send the number back.",
    "Please send the digits back.",
    "Share the number by Friday.",
    "Send those digits to me by Friday.",
    "Please send me the number back.",
    "Please send me those digits by Friday.",
    "Please share the number above.",
    "Share with me those digits by Friday.",
    "Please share that value securely.",
    "Please send the final number 42 today.",
    "Please share the updated number 42 now.",
    "Please send the correct number 42 today.",
    "Please share the current number 42 by Friday.",
    "Please send the temporary number 42 now.",
    "Please send the number and the digits by Friday.",
    "Please send the number and today.",
    "Please share the number and immediately.",
])
def test_credential_references_do_not_become_work(monkeypatch, work_text):
    from service.assistant import brief
    now = time.time()
    text = "Alex: Your verification code is 6432. " + work_text
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        assert M.summary_message_rows(require_read_state=True) == []
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [brief._messages_block(), brief._messages_card(now)]
        direct = asyncio.run(M._summarize([(now - 1, "Alex", text)], "today"))
    assert all("Action items mentioned" not in out and "6432" not in out for out in outputs)
    # _summarize accepts already selected rows; its direct caller still redacts.
    assert "6432" not in direct + str(records) + str(chat.call_args_list)


@pytest.mark.parametrize("action", ["send", "share"])
@pytest.mark.parametrize("object_text", [
    "the number", "the digits", "the six digits", "the last six digits",
    "the updated value", "the next sequence",
])
@pytest.mark.parametrize("tail", ["now", "today", "immediately", "back", "by Friday"])
def test_modified_credential_referents_stay_noise(monkeypatch, action, object_text, tail):
    from service.assistant import brief
    now = time.time()
    text = f"Alex: Your verification code is 6432. Please {action} {object_text} {tail}."
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        assert M.summary_message_rows(require_read_state=True) == []
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [brief._messages_block(), brief._messages_card(now)]
        direct = asyncio.run(M._summarize([(now - 1, "Alex", text)], "today"))
    assert all("Action items mentioned" not in out and "6432" not in out for out in outputs)
    assert "6432" not in direct + str(records) + str(chat.call_args_list)


@pytest.mark.parametrize("work_text", [
    "Please review onboarding checklist by Friday.",
    "Review final storyboard by Friday.",
    "Please send the report by Friday.",
    "Please send the annual report by Friday.",
    "Please share the weekly report by Friday.",
    "Share the attached document by Friday.",
    "Please send the final onboarding checklist by Friday.",
    "Share the updated storyboard by Friday.",
    "Please send report number 42 by Friday.",
    "Please share document number 8 by Friday.",
    "Please send the final report number 42 by Friday.",
    "Please share the document number 8 by Friday.",
    "Please send the number and the report by Friday.",
    "Please send the number and the reply by Friday.",
])
def test_independent_work_survives_prior_credential_notice(monkeypatch, work_text):
    from service.assistant import brief
    now = time.time()
    text = "Alex: Your verification code is 6432. " + work_text
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        rows = M.summary_message_rows(require_read_state=True)
        assert len(rows) == 1
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [asyncio.run(M._summarize([(now - 1, "Alex", text)], "today")),
                    brief._messages_block(), brief._messages_card(now)]
    assert all("Alex" in out and "stated deadline" in out and "6432" not in out for out in outputs)
    assert "Action items mentioned" in outputs[0]
    assert M.message_priority(rows[0][2]) == 1
    assert "Friday" not in rows[0][2]
    assert "6432" not in str(records) + str(chat.call_args_list)


@pytest.mark.parametrize("work_text", [
    "Please send the four characters back.",
    "Please send those four characters back.",
    "Please share that four-character string now.",
    "Please send the text you just received.",
    "Share the exact response you just got by Friday.",
    "Please send the characters, not the report.",
    "Please send the number instead of the report.",
    "Share the string rather than the document by Friday.",
    "Please share the text and not the checklist.",
    "Please send the characters without the report.",
    "Please send the text other than the document.",
    "Please send the string excluding the checklist.",
    "Please send the characters apart from the report.",
    "Please send the four characters and report back.",
    "Please send the number and report it.",
    "Please share the string and file it.",
    "Please send the text and document it.",
    "Please share the value and reply back.",
    "Please send the characters, then report back.",
    "Please send the number; file it.",
    "Please send the characters and quickly report back.",
    "Please send the number and please report back.",
    "Please share the string and quietly file it.",
    "Please send the text and promptly document it.",
    "Please send the characters and just report back.",
    "Please share the string and now file it.",
    "Please send the number and can you report back?",
    "Please send the characters and you document it.",
    "Please send the characters and the system will report back.",
    "Please send the number and my assistant will file it.",
    "Please send the characters and she reports back.",
    "Please share the string and he documents it.",
    "Please send the four characters in a file by Friday.",
    "Please share the string in a document by Friday.",
    "Please send the number inside a document by Friday.",
    "Please send the four characters as a report by Friday.",
    "Please send the four characters in a file or a document by Friday.",
    "Please send the four characters as a report or a document by Friday.",
    "Please send the four characters in a file and report back by Friday.",
    "Please send the four characters via a file by Friday.",
    "Please share the string using a document by Friday.",
    "Please send the number through a report by Friday.",
    "Please send the characters over a document by Friday.",
    "Please send the four characters via a file or a document by Friday.",
    "Please send the four characters into a document by Friday.",
    "Please send the four characters onto a file by Friday.",
    "Please send the four characters formatted like a report by Friday.",
    "Please send the four characters laid out like a report by Friday.",
    "Please send the four characters formatted like a report or document by Friday.",
    "Please send a document containing those four characters by Friday.",
    "Please send a file with just the four characters by Friday.",
    "Please share a report holding the string by Friday.",
    "Please send the number and a document containing those four characters by Friday.",
    "Please send the characters and a file with just the four characters by Friday.",
    "Please send a document containing it by Friday.",
    "Please send the number and a file holding it by Friday.",
])
def test_nonnumeric_credential_references_stay_noise(monkeypatch, work_text):
    from service.assistant import brief
    now = time.time()
    text = "Alex: Your verification code is A7B9. " + work_text
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        assert M.summary_message_rows(require_read_state=True) == []
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [brief._messages_block(), brief._messages_card(now)]
        direct = asyncio.run(M._summarize([(now - 1, "Alex", text)], "today"))
    assert all("Action items mentioned" not in out and "A7B9" not in out for out in outputs)
    assert "A7B9" not in direct + str(records) + str(chat.call_args_list)


@pytest.mark.parametrize("work_text", [
    "Please send the report by Friday.",
    "Share the document by Friday.",
    "Please send onboarding checklist by Friday.",
    "Share the final storyboard by Friday.",
    "Please send the report number 42 by Friday.",
    "Please send the number and the report by Friday.",
    "Please review quasar index by Friday.",
    "Please send the report, not the code, by Friday.",
    "Share the document rather than the characters by Friday.",
    "Please send the report without the characters by Friday.",
    "Share the document other than the code by Friday.",
    "Please send the checklist excluding the code by Friday.",
    "Please send the report apart from the code by Friday.",
    "Please send the number and the report by Friday.",
    "Please share the string and the file by Friday.",
    "Please send the number and an updated report by Friday.",
    "Please send the number and report number 42 by Friday.",
    "Please send the number, the report by Friday.",
    "Please send the number and checklist by Friday.",
    "Please send the number and storyboard by Friday.",
    "Please send the number and file the report by Friday.",
    "Please send the number and report the document by Friday.",
    "Please send the number and can you report the document by Friday?",
    "Please share the string and the final report by Friday.",
    "Please send the number and my document by Friday.",
    "Please share the string and final report by Friday.",
    "Please send the report in a file by Friday.",
    "Please send the number in a file and the report by Friday.",
    "Please send the report via a file by Friday.",
    "Please send the number using a document and the report by Friday.",
    "Please send the number into a document and the report by Friday.",
    "Please send the number and review the report by Friday.",
    "Please send the number and please review the report by Friday.",
    "Please send the status report by Friday.",
    "Please share the project report by Friday.",
    "Please send the number and the report containing the project summary by Friday.",
])
def test_explicit_work_survives_alphanumeric_otp(monkeypatch, work_text):
    from service.assistant import brief
    now = time.time()
    text = "Alex: Your verification code is A7B9. " + work_text
    monkeypatch.setattr(M, "_lines", _freshness_record(now - 1, "U", 1, "Alex", text))
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        rows = M.summary_message_rows(require_read_state=True)
        assert len(rows) == 1
        outputs = [asyncio.run(M.summarize_messages(**args)) for args in
                   ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"})]
        outputs += [asyncio.run(M._summarize([(now - 1, "Alex", text)], "today")),
                    brief._messages_block(), brief._messages_card(now)]
    assert all("Alex" in out and "stated deadline" in out and "A7B9" not in out for out in outputs)
    assert "Action items mentioned" in outputs[0]
    assert "A7B9" not in str(records) + str(chat.call_args_list)


def test_distinct_redacted_requests_keep_both_source_occurrences(monkeypatch):
    now = time.time()
    source = [
        (now - 2, "Alex", "Alex: Please review the report by Friday. Verification code is 1234."),
        (now - 1, "Alex", "Alex: Please review the proposal by Friday. Verification code is 5678."),
    ]
    rows = M.filter_summary_message_rows(source)
    assert len(rows) == 2
    assert rows[0][2] == rows[1][2]
    assert all(secret not in str(rows) for secret in ("1234", "5678", "report", "proposal"))
    assert len(M.filter_summary_message_rows(rows)) == 2
    monkeypatch.setattr(M, "_lines", "\n".join(
        _freshness_record(ts, "U", 1, "Alex", body) for ts, _, body in source))
    selected = M.summary_message_rows(require_read_state=True)
    assert len(selected) == 2
    chat = client(monkeypatch, error=RuntimeError("synthetic offline"))
    with debug_capture.capture() as records:
        for args in ({}, {"day": "today"}, {"period": "this week"}, {"conversation": "Alex"}):
            out = asyncio.run(M.summarize_messages(**args))
            assert "2 messages across 1 conversations" in out
            assert "Action items mentioned" in out
            assert all(secret not in out for secret in ("1234", "5678", "report", "proposal"))
    assert all(secret not in str(records) + str(chat.call_args_list)
               for secret in ("1234", "5678", "report", "proposal"))
