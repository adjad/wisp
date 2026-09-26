"""Synthetic header-only sender digest presentation."""
from __future__ import annotations

import asyncio

from service.tools import email_tools as E


def digest(lines: list[str], label: str = "your recent inbox") -> str:
    return asyncio.run(E._summarize(lines, label))


def test_empty_digest_is_honest_and_compact():
    assert digest([]) == "No emails found for your recent inbox."


def test_legacy_rows_without_addresses_do_not_group_on_display_name():
    output = digest([
        "[personal@example.com] University | Course registration opens Monday",
        "[school@example.edu] University | Assignment posted",
    ])
    assert "represented 2 messages from 2 sender notes" in output
    assert output.count("**University (address unavailable)**") == 2


def test_subject_urgency_is_labeled_as_a_subject_claim():
    output = digest(["[mail] Casey <casey@example.test> | RSVP for Thursday dinner"])
    assert "**Casey <casey@example.test>**" in output
    assert "Subject says: “RSVP for Thursday dinner”" in output
    assert "Thursday dinner is confirmed" not in output


def test_large_digest_is_bounded_but_discloses_hidden_senders():
    lines = [f"[mail] Sender {i} | General update {i}" for i in range(20)]
    output = digest(lines)
    assert "represented 12 messages from 12 sender notes" in output
    assert "truncated 8 messages" in output
    assert "8 more sender addresses" in output
    assert output.count("\n- **") == 12
