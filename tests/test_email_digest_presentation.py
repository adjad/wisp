"""Synthetic, header-only coverage for the direct email digest presentation."""
from __future__ import annotations

import asyncio

from service.tools import email_tools as E


def digest(lines: list[str], label: str = "your recent inbox") -> str:
    return asyncio.run(E._summarize(lines, label))


def test_empty_digest_is_honest_and_compact():
    assert digest([]) == "No substantive emails found for your recent inbox."


def test_digest_groups_themes_without_relaying_raw_header_lines():
    output = digest([
        "[personal@example.com] Nina <nina@example.com> | Please review the project outline",
        "[school@example.edu] University | Course registration opens Monday",
        "[personal@example.com] Travel Desk | Flight itinerary for Seattle",
    ])

    assert "📬 **Inbox digest — your recent inbox**" in output
    assert "3 emails • 2 linked accounts" in output
    assert "**⚠️ Time-sensitive or action mentioned**" in output
    assert "**💼 Work or school**" in output
    assert "**🧾 Orders and travel**" in output
    assert "Nina <nina@example.com> |" not in output
    assert "**Nina** — Please review the project outline" in output


def test_reply_section_requires_an_explicit_subject_request():
    output = digest([
        "[mail] Jordan | Re: Planning update",
        "[mail] Casey | RSVP for Thursday dinner",
    ])

    assert "**↩️ Reply explicitly requested**" in output
    assert "**Casey** — RSVP for Thursday dinner" in output
    # A thread marker alone cannot establish that the user owes a reply.
    assert output.count("Reply explicitly requested") == 1
    assert "**Jordan** — Re: Planning update" in output


def test_large_digest_is_bounded_but_keeps_compact_metadata():
    lines = [f"[mail] Sender {i} | General update {i}" for i in range(20)]
    output = digest(lines)

    assert "20 emails • 1 linked account" in output
    assert "Showing 3 named emails; 17 more are included in the count above." in output
    assert output.count("\n- **") == 3
