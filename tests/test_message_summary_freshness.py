"""Regressions for freshness and promotion filtering in general Messages digests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from service.tools import imessage_tools as messages


def _record(ts: float, state: str, identity: int, context: str, text: str) -> str:
    return f"V2 | {ts} | {state} | chat:{identity} | {context} | {text}"


def test_general_digest_is_recent_unread_non_promotional_and_keeps_true_timestamps(
        monkeypatch):
    now = datetime(2026, 9, 24, 13, 0).timestamp()
    direct_ts = now - 2 * 86400
    urgent_ts = now - 90
    monkeypatch.setattr(messages, "_lines", "\n".join([
        _record(now - 60, "U", 1, "42302",
                "42302: Santa Cruz Backyard: We're 90% SOLDOUT. 17+ with college ID allowed."),
        _record(now - 70, "U", 2, "51023",
                "51023: Vim + Vigor Fitness: Get 30 days on us. No commitment, no enrollment."),
        _record(urgent_ts, "U", 3, "74643",
                "74643: Fraud alert: Card ending 1234 was charged $950. Reply YES or NO."),
        _record(direct_ts, "U", 4, "Alex",
                "Alex: Can you bring the signed form when we meet?"),
        _record(now - 120, "R", 5, "Casey", "Casey: Already-read update."),
        _record(now - 21 * 86400, "U", 6, "Jordan", "Jordan: Weeks-old unread update."),
        _record(now + 1, "U", 7, "Future", "Future: Future-dated update."),
        f"{now - 30} | Legacy | Legacy: Unknown read state.",
    ]))

    rows = messages.recent_priority_message_rows(now=now)

    assert [(ts, context) for ts, context, _text in rows] == [
        (urgent_ts, "74643"),
        (direct_ts, "Alex"),
    ]
    assert all("42302" not in text and "51023" not in text
               for _ts, _context, text in rows)


def test_general_digest_renders_the_source_local_day_instead_of_relabeling_it(
        monkeypatch):
    now = datetime(2026, 9, 24, 13, 0).timestamp()
    source_ts = (datetime.fromtimestamp(now) - timedelta(days=2)).replace(
        hour=8, minute=15, second=0, microsecond=0).timestamp()
    monkeypatch.setattr(messages, "_lines", _record(
        source_ts, "U", 1, "Alex", "Alex: The signed form is ready for pickup."))
    monkeypatch.setattr(messages, "role_to_model", lambda _role: "test-model")

    class OfflineClient:
        async def chat(self, *_args, **_kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(messages, "_client", OfflineClient())
    rows = messages.recent_priority_message_rows(now=now)
    output = asyncio.run(messages._summarize(
        rows, "your unread messages from the last three days"))

    source_day = datetime.fromtimestamp(source_ts).date().isoformat()
    assert source_day in output
    assert "Yesterday" not in output


def test_explicit_group_summary_keeps_read_routine_messages(monkeypatch):
    now = datetime(2026, 9, 24, 13, 0).timestamp()
    monkeypatch.setattr(messages, "_lines", "\n".join([
        _record(now - 14 * 86400, "R", 10, 'Group "Weekend"',
                "Alex: The blue cooler is in the garage."),
        _record(now - 13 * 86400, "R", 10, 'Group "Weekend"',
                "Casey: I put the folding chairs beside it."),
        _record(now - 60, "U", 11, 'Group "Other"',
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

    result = asyncio.run(messages.summarize_messages(conversation="Weekend"))

    assert result == 'Group "Weekend" — recent messages'
    assert [text for _ts, _context, text in captured] == [
        "Alex: The blue cooler is in the garage.",
        "Casey: I put the folding chairs beside it.",
    ]
