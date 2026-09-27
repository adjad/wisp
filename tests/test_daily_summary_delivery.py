"""Daily Summary delivery — regression tests.

Reported live (Wisp debug export, 2026-09-08 11:46): pressing Daily Summary
"takes way too long to read the sources", produced "multiple false 'daily summary
is ready in wisp' notifications while reading the sources", the brief itself was
"hard to read", and a follow-up referring to it "is also not handled correctly".

Four separate mechanisms, one per test class below:

  * READABILITY — `_generate_brief` concatenated get_upcoming, summarize_emails
    and summarize_messages verbatim, and those strings are written for a model:
    the exported brief opened three of its four sections with "each row is tagged
    relative to today:", "A calendar event alone is not a reminder." and "Source
    excerpts (not inferred outcomes); …", tagged every row "[Apple Reminder]",
    and carried a reminder titled "send my vaccine report to UCSC.**" whose stray
    asterisks opened a bold run.

  * LATENCY — composing one brief awaited FOUR source-readiness waits (the
    endpoint's, `_sections`' duplicate of it, then summarize_emails' and
    summarize_messages' own), each of which publishes a sync request the Swift
    readers answer with another serialized AppleScript walk of the same source.

  * FALSE NOTIFICATIONS — the scheduler marked the day done before it knew
    whether a brief had been produced, and published the hold-back message ("Wisp
    is still syncing …") as a `daily_brief` event, which the app announced as
    "Your daily summary is ready in Wisp." The fired-date lived only in memory,
    and the backend is a child of Wisp.app, so every relaunch inside the 4-hour
    window repeated it — with the sources always mid-sync right after a launch.

  * FOLLOW-UPS — the button's brief was never recorded in a session, so the
    backend answering the next message had no previous assistant turn to refer
    to (see the referenced_report case in scripts/verify_tool_calling.py).

    .venv/bin/python -m pytest tests/test_daily_summary_delivery.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-daily-summary-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant import brief as B                       # noqa: E402
from service.assistant import scheduler                        # noqa: E402
from service.assistant.hub import hub                          # noqa: E402
from service.tools import email_tools as E, imessage_tools as M  # noqa: E402

# Text that only ever belonged in a prompt. Any of it in the brief is the leak.
SCAFFOLD = (
    "each row is tagged relative to today",
    "A calendar event alone is not a reminder",
    "Source excerpts",
    "not inferred outcomes",
    "quoted from the original messages",
    "Quoted from the source",
    "[Apple Reminder]",
    "Wisp reminder; Apple mirror not verified",
    "ON THE CALENDAR TODAY",
    "FROM PEOPLE",
    "do NOT",
)


@pytest.fixture
def sources(monkeypatch):
    """Calendar, Reminders, Mail and Messages, all ready, with fixed rows.

    Shaped after the exported failure: no calendar events, three Apple Reminders
    (two of them already past due), an inbox that is mostly automated senders
    across two accounts, and one outgoing plus one group message.
    """
    # Build the fixed wall-clock time in the runner's local timezone. A raw
    # epoch made the final reminder cross midnight on UTC CI but not in PDT.
    now = datetime(2026, 9, 8, 11, 46).timestamp()
    monkeypatch.setattr(scheduler, "_sync_status", {
        source: {"available": True, "count": 1, "last_sync": now}
        for source in ("calendar", "reminders")})
    monkeypatch.setattr(B.assistant_store, "upcoming", lambda now=0, days=7: [
        {"source": "reminders", "kind": "reminder", "when_ts": now - 9_500,
         "title": "the iphone repair thing", "account": "iCloud"},
        {"source": "reminders", "kind": "reminder", "when_ts": now - 9_400,
         "title": "send my vaccine report to UCSC.**", "account": "iCloud"},
        {"source": "reminders", "kind": "reminder", "when_ts": now + 29_000,
         "title": "finish a Canvas assignment", "account": "iCloud"},
    ])
    monkeypatch.setattr(E, "_headers", "\n".join([
        f"{now - 1_000} | U | Google | PayPal | Confirmed: you've been invited to apply",
        f"{now - 2_000} | U | adnjain@ucsc.edu | Trishe Rao | Are you free Thursday?",
    ]))
    monkeypatch.setattr(E, "_headers_sync_generation", 1)
    monkeypatch.setattr(E, "_email_available", True)
    monkeypatch.setattr(E, "_email_sync_pending", False)
    monkeypatch.setattr(E, "_email_read_source", "mail_app")
    monkeypatch.setattr(M, "_parse_lines", lambda: [
        (now - 500, "Trishe", "Me: When are you getting the ChatGPT max plan"),
        (now - 900, 'Group "Grad GC"', "+19255231832: The meeting was moved to 7 pm."),
    ])
    monkeypatch.setattr(M, "_lines", "\n".join([
        f"V2 | {now - 500} | R | chat:1 | Trishe | Me: When are you getting the ChatGPT max plan",
        f'V2 | {now - 900} | U | chat:2 | Group "Grad GC" | +19255231832: The meeting was moved to 7 pm.',
    ]))
    monkeypatch.setattr(M, "_sync_completed", True)
    monkeypatch.setattr(M, "_available", True)
    return now


class TestReadability:
    def test_no_model_facing_scaffolding_reaches_the_user(self, sources):
        text = B._render_brief(sources, B._messages_section(sources))
        leaked = [s for s in SCAFFOLD if s in text]
        assert not leaked, f"leaked {leaked}"

    def test_a_stray_asterisk_in_a_title_cannot_open_a_bold_run(self, sources):
        text = B._render_brief(sources, B._messages_section(sources))
        assert "send my vaccine report to UCSC" in text
        assert "UCSC.**" not in text
        # Every ** left in the brief is one of its own section headers, so they
        # pair up. An odd count means an unclosed run.
        assert text.count("**") % 2 == 0

    def test_events_and_reminders_are_separate_groups(self, sources):
        section = B._schedule_section(sources)
        assert "**📅 Today**" in section and "Nothing on your calendar" in section
        assert "**✅ Reminders due today**" in section
        # The distinction the tool output spent a sentence explaining.
        assert "A calendar event alone is not a reminder" not in section

    def test_a_past_due_reminder_is_not_labelled_now(self, sources):
        section = B._schedule_section(sources)
        assert "overdue" in section and "(now)" not in section

    def test_mail_has_short_sections_for_people_and_automated_mail(self, sources):
        section = B._email_section(sources)
        assert section.index("Trishe Rao") < section.index("PayPal")
        assert "**Worth a look**" in section and "**Other mail**" in section
        assert "Inbox — 2 emails · 2 unread" in section
        assert "Scanned" not in section and "@ucsc.edu" not in section

    def test_daily_groups_by_address_without_reading_bodies(self, sources, monkeypatch):
        now = sources
        def h(ts, account, account_id, address, message_id, subject):
            return "\x01".join(["H2", str(ts), "U", account, account_id,
                                  "Nina", address, message_id, subject])
        monkeypatch.setattr(E, "_headers", "\n".join([
            h(now - 10, "Personal", "a1", "nina@example.test", "one", "Review form"),
            h(now - 20, "School", "a2", "nina@example.test", "two", "Deadline notice"),
            h(now - 30, "School", "a2", "other@example.test", "three", "Update"),
        ]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert section.count("\n- Nina:") == 2
        assert "Review form" in section and "Deadline notice" in section
        assert "nina@example.test" not in section
        assert "accounts: Personal, School" not in section

    def test_daily_fallback_discloses_older_dates_and_cut(self, sources, monkeypatch):
        now = sources
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - 3 * 86400 - i), "U", "Personal", "a1",
                         "Nina", "nina@example.test", str(i), f"Older note {i}"])
            for i in range(25)))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert "20 emails shown · 20 unread among them" in section
        assert "No matching headers in the available snapshot for the last 24 hours" in section
        assert "Older note" in section
        assert "Scanned" not in section

    def test_daily_scan_cap_discloses_unknown_coverage(self, sources, monkeypatch):
        now = sources
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - i), "U", "Personal", "a1",
                         "Nina", "nina@example.test", str(i), f"Note {i}"])
            for i in range(200)))
        section = B._email_section(now)
        assert "200 emails shown · 200 unread among them" in section
        assert "Mail scan incomplete; other messages may be missing" in section
        assert "200-message-per-account" not in section
        assert "Scanned" not in section

    def test_daily_partial_account_scan_discloses_unknown_coverage(self, sources, monkeypatch):
        now = sources
        monkeypatch.setattr(E, "_headers", "\n".join([
            "\x01".join(["H2", str(now - 10), "U", "School", "b", "Nina",
                           "nina@example.test", "school", "School update"]),
            "\x01".join(["C3", "Personal", "", "failed"]),
        ]))
        section = B._email_section(now)
        assert "1 email shown · 1 unread among them" in section
        assert "Mail scan incomplete; other messages may be missing" in section
        assert "Recent header scan did not complete for Personal" not in section

    def test_today_card_discloses_partial_mail_without_complete_counts(self, sources, monkeypatch):
        now = sources
        row = "\x01".join(["H2", str(now - 10), "U", "School", "b", "Nina",
                          "nina@example.test", "school", "School update"])
        for marker in (
            "\x01".join(["C3", "Personal", "", "failed"]),
            "\x01".join(["C2", "School", "b", "2", "1", "0"]),
            "\x01".join(["C2", "School", "b", "200", "0", "1"]),
        ):
            monkeypatch.setattr(E, "_headers", "\n".join([row, marker]))
            card = B._today_card(now)
            assert "Mail: scan incomplete; messages may be missing." in card
            assert "Mail: 1 from people, 0 automated." not in card
        monkeypatch.setattr(E, "_headers", "\x01".join(["C3", "Personal", "", "failed"]))
        assert "Mail: scan incomplete" in B._today_card(now)
        monkeypatch.setattr(E, "_headers", row)
        assert "Mail: 1 from people, 0 automated." in B._today_card(now)

    def test_daily_empty_window_keeps_raw_cap_warning(self, sources, monkeypatch):
        now = sources
        rows = ["\x01".join(["H2", str(now + 3600 + i), "U", "Gmail", "a1",
                           "Nina", "nina@example.test", str(i if i < 199 else 0),
                           f"Future note {i}", f"db:{i}"])
                for i in range(200)]
        monkeypatch.setattr(E, "_headers", "\n".join(rows))
        assert len(E.header_rows()) == 199
        section = B._email_section(now)
        block = B._email_block(now)
        assert "Mail scan incomplete; other messages may be missing" in section
        assert "Scanned" not in section
        assert "Scanned 0 matching cached headers" in block
        assert "total truncation is unknown" in block
        monkeypatch.setattr(E, "_headers", "\n".join(rows[:199]))
        complete = B._email_section(now)
        assert "No matching headers" in complete
        assert "Mail scan incomplete" not in complete

    def test_daily_empty_window_uses_native_skip_marker(self, sources, monkeypatch):
        now = sources
        rows = ["\x01".join(["H2", str(now + 3600 + i), "U", "Gmail", "a1",
                           "Nina", "nina@example.test", str(i), f"Future note {i}",
                           f"db:{i}"])
                for i in range(199)]
        marker = "\x01".join(["C2", "Gmail", "a1", "200", "1", "1"])
        monkeypatch.setattr(E, "_headers", "\n".join(rows + [marker]))
        section, block = B._email_section(now), B._email_block(now)
        assert "Mail scan incomplete; other messages may be missing" in section
        assert "skipped 1 malformed headers" not in section
        assert "total truncation is unknown" in block
        assert "skipped 1 malformed headers" in block

    def test_daily_scan_wide_skip_has_unknown_date(self, sources, monkeypatch):
        now = sources
        today = "\x01".join(["H2", str(now - 100), "U", "Gmail", "a1",
                           "Nina", "nina@example.test", "today", "Today", "db:1"])
        marker = "\x01".join(["C2", "Gmail", "a1", "2", "1", "0"])
        monkeypatch.setattr(E, "_headers", "\n".join([today, marker]))
        section = B._email_section(now)
        assert "Mail scan incomplete; other messages may be missing" in section
        assert "skipped 1 malformed header with unknown dates" not in section
        future = today.replace(str(now - 100), str(now + 3600))
        monkeypatch.setattr(E, "_headers", "\n".join([future, marker]))
        empty = B._email_section(now)
        assert "No matching headers" in empty
        assert "Mail scan incomplete; other messages may be missing" in empty

    def test_daily_mail_groups_export_like_headers_without_body_claims(self, sources, monkeypatch):
        now = sources
        def h(offset, account, sender, address, subject):
            return "\x01".join(["H2", str(now - offset), "U", account, account,
                                  sender, address, str(offset), subject])
        monkeypatch.setattr(E, "_headers", "\n".join([
            h(10, "School", "Beginning Programming in Python", "notifications@instructure.com",
              "Syllabus quiz and Notebook Grader practice assignment deadlines have been extended: Beginning Programming in Python"),
            h(20, "School", "Beginning Programming in Python", "notifications@instructure.com",
              "Arjun's Office Hours: Beginning Programming in Python"),
            h(30, "School", "orders@zybooks.com", "orders@zybooks.com",
              "Your zyBooks.com subscription receipt #123 UCSC"),
            h(40, "School", "no-reply@zybooks.com", "no-reply@zybooks.com",
              "We've created an account for you on zyBooks.com"),
            h(50, "Personal", "Venmo", "venmo@email.venmo.com", "Get in here and get verified"),
            h(60, "School", "Dhruv Kolte", "dkolte@ucsc.edu",
              "Appointment booked: Living Agreement Meetings (Sep 8, 9:00 AM)"),
            h(70, "Personal", "The New York Times", "nytimes@nytimes.com",
              "Politics: 5 stories from this week"),
            h(80, "Personal", "Glassdoor Jobs", "jobs@glassdoor.com",
              "Three jobs in your area. Apply Now."),
        ]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert "**Worth a look**" in section and "**Other mail**" in section
        assert "Inbox — 8 emails · 8 unread" in section
        assert "deadlines have been extended" in section and "Arjun's Office Hours" in section
        assert section.count("Beginning Programming in Python") == 1
        assert section.count("- zyBooks:") == 1 and "subscription receipt" in section
        assert "Venmo" in section and "verified" in section
        assert "For reference, Dhruv Kolte" in section
        assert "Job alerts: Glassdoor Jobs" in section
        assert "Newsletters and updates: The New York Times" in section
        assert "notifications@" not in section and "accounts:" not in section
        assert "Scanned" not in section and "200-message-per-account" not in section
        assert "you need to" not in section.lower() and "upcoming" not in section.lower()

    def test_job_and_newsletter_subject_signals_stay_worth_a_look(self, sources, monkeypatch):
        now = sources
        def h(offset, sender, address, subject):
            return "\x01".join(["H2", str(now - offset), "R", "Personal", "p",
                                  sender, address, str(offset), subject])
        monkeypatch.setattr(E, "_headers", "\n".join([
            h(10, "Glassdoor Jobs", "jobs@glassdoor.com", "Application deadline tomorrow"),
            h(20, "The New York Times", "news@nytimes.com", "Security alert: account sign-in"),
            h(30, "ZipRecruiter", "alerts@ziprecruiter.com", "New jobs near you"),
        ]))
        section = B._email_section(now)
        worth = section.split("**Worth a look**", 1)[1].split("**Other mail**", 1)[0]
        other = section.split("**Other mail**", 1)[1]
        assert "Glassdoor Jobs" in worth and "Application deadline tomorrow" in worth
        assert "The New York Times" in worth and "Security alert" in worth
        assert "ZipRecruiter" in other and "Glassdoor Jobs" not in other
        assert "Inbox — 3 emails · 0 unread" in section

    def test_receipt_subjects_keep_negation_request_and_distinct_meanings(self, sources, monkeypatch):
        now = sources
        subjects = [
            "Payment failed — no receipt issued",
            "Action required: submit your receipt by Friday",
            "Your receipt for purchase #123",
        ]
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - i), "U", "Personal", "p", "Bank",
                          "notices@bank.example.test", str(i), subject])
            for i, subject in enumerate(subjects, start=1)))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert "Payment failed — no receipt issued" in section
        assert "Action required: submit your receipt by Friday" in section
        assert "+1 more subjects" in section
        assert "“receipt”" not in section and "“subscription receipt”" not in section

    def test_distinct_receipt_subjects_are_not_collapsed_by_presentation(self, sources, monkeypatch):
        now = sources
        subjects = ["Receipt required for reimbursement", "No receipt issued for failed payment",
                    "Your receipt for purchase #123"]
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - i), "U", "Personal", "p", "Bank",
                          "notices@bank.example.test", str(i), subject])
            for i, subject in enumerate(subjects, start=1)))
        section = B._email_section(now)
        assert "Receipt required for reimbursement" in section
        assert "No receipt issued for failed payment" in section
        assert "+1 more subjects" in section

    def test_older_urgent_subject_is_visible_ahead_of_same_sender_routine_mail(self, sources, monkeypatch):
        now = sources
        subjects = ["Weekly newsletter", "New jobs available",
                    "Action required: tuition payment due today"]
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - i), "U", "Personal", "p", "Bank",
                          "notices@bank.example.test", str(i), subject])
            for i, subject in enumerate(subjects, start=1)))
        section = B._email_section(now)
        bank_line = next(line for line in section.splitlines() if line.startswith("- Bank:"))
        assert "Action required: tuition payment due today" in bank_line
        assert bank_line.index("Action required") < bank_line.index("New jobs available")
        assert "+1 more subjects" in bank_line

    def test_actionable_confirmation_does_not_disappear_into_booking_reference(self, sources, monkeypatch):
        now = sources
        def h(offset, subject):
            return "\x01".join(["H2", str(now - offset), "U", "Personal", "p", "Venue",
                                  "bookings@venue.example.test", str(offset), subject])
        monkeypatch.setattr(E, "_headers", "\n".join([
            h(10, "Appointment booked: Room A"),
            h(20, "Meeting confirmed — action required: pay by Friday"),
        ]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        worth = section.split("**Worth a look**", 1)[1].split("**Other mail**", 1)[0]
        other = section.split("**Other mail**", 1)[1]
        assert "Meeting confirmed — action required: pay by Friday" in worth
        assert "For reference, Venue: “Appointment booked: Room A”" in other

    def test_long_receipt_subject_retains_negating_end(self, sources, monkeypatch):
        now = sources
        subject = "Your receipt for order " + "X" * 105 + " — no receipt issued"
        monkeypatch.setattr(E, "_headers", "\x01".join([
            "H2", str(now - 10), "U", "Personal", "p", "Bank",
            "notices@bank.example.test", "one", subject]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert f"“{subject}”" in section and len(section) < 350

    @pytest.mark.parametrize("subject,decisive", [
        ("Your subscription receipt for Acme Professional annual plan — no receipt issued — "
         "Reference: billing case 2026-0927-00004721, account ending 1839", "no receipt issued"),
        ("Your tuition payment summary for fall quarter 2026 — action required: pay by Friday — "
         "Reference: student account 2026-0927-00004721, confirmation pending", "action required: pay by Friday"),
    ])
    def test_middle_claim_in_long_subject_remains_visible(self, sources, monkeypatch,
                                                            subject, decisive):
        now = sources
        monkeypatch.setattr(E, "_headers", "\x01".join([
            "H2", str(now - 10), "U", "Personal", "p", "Bank",
            "notices@bank.example.test", "one", subject]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert f"“{subject}”" in section and decisive in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    def test_extreme_subject_uses_unquoted_bounded_notice(self, sources, monkeypatch):
        now = sources
        subject = "Your receipt for order " + "X" * 500 + " — no receipt issued"
        monkeypatch.setattr(E, "_headers", "\x01".join([
            "H2", str(now - 10), "U", "Personal", "p", "Bank",
            "notices@bank.example.test", "one", subject]))
        section = B._email_section(now)
        assert "Subject too long to display here (see Mail)" in section
        assert "Your receipt for order" not in section and "“Subject too long" not in section
        assert len(section) < 200

    def test_plural_deadline_and_receipt_headers_remain_visible(self, sources, monkeypatch):
        now = sources
        def h(offset, sender, address, subject):
            return "\x01".join(["H2", str(now - offset), "U", "Personal", "p",
                                  sender, address, str(offset), subject])
        monkeypatch.setattr(E, "_headers", "\n".join([
            h(10, "Notifications", "notifications@course.example.test",
              "Assignment deadlines tomorrow"),
            h(20, "Receipts", "notifications@store.example.test",
              "Receipts available for reimbursement"),
        ]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        worth = section.split("**Worth a look**", 1)[1]
        assert "Assignment deadlines tomorrow" in worth
        assert "Receipts available for reimbursement" in worth
        assert "Other updates: Receipts, Notifications" not in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    def test_same_sender_plural_deadline_outranks_newer_routine_subjects(self, sources, monkeypatch):
        now = sources
        subjects = ["Please review your profile", "Please confirm your profile",
                    "Assignment deadlines tomorrow"]
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - i), "U", "Personal", "p",
                          "Notifications", "notifications@course.example.test", str(i), subject])
            for i, subject in enumerate(subjects, start=1)))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        note = next(line for line in section.splitlines() if line.startswith("- Notifications:"))
        assert "Assignment deadlines tomorrow" in note
        assert note.index("Assignment deadlines tomorrow") < note.index("Please review your profile")
        assert "+1 more subjects" in note
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("subjects,priority", [
        (["Your receipt for purchase #1", "Your receipt for purchase #2",
          "Urgent: respond by Friday"], "Urgent: respond by Friday"),
        (["Invoice #1 available", "Invoice #2 available",
          "Urgent: respond by Friday"], "Urgent: respond by Friday"),
        (["Your receipt for purchase #1", "Your receipt for purchase #2",
          "Assignment deadlines tomorrow"], "Assignment deadlines tomorrow"),
        (["Invoice #1 available", "Account statement ready",
          "Please review your profile"], "Please review your profile"),
    ])
    def test_same_sender_requests_outrank_newer_routine_signals(self, sources,
                                                                 monkeypatch,
                                                                 subjects, priority):
        now = sources
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - i), "U", "Personal", "p",
                          "Notifications", "notifications@course.example.test", str(i), subject])
            for i, subject in enumerate(subjects, start=1)))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        note = next(line for line in section.splitlines() if line.startswith("- Notifications:"))
        assert priority in note
        assert note.index(priority) < note.index(subjects[0])
        assert "+1 more subjects" in note
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    def test_automated_urgent_subject_without_transaction_signal_is_visible(self,
                                                                             sources,
                                                                             monkeypatch):
        now = sources
        monkeypatch.setattr(E, "_headers", "\x01".join([
            "H2", str(now - 10), "U", "Personal", "p", "Notifications",
            "notifications@course.example.test", "one", "Urgent: respond by Friday"]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert "**Worth a look**\n- Notifications: “Urgent: respond by Friday”" in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    def test_urgent_sender_survives_five_source_display_cap(self, sources, monkeypatch):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i), "U", "Personal", "p", f"Store {i}",
            f"receipts@store{i}.example.test", str(i), f"Your receipt #{i}"])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10), "U", "Personal", "p", "Notifications",
            "notifications@course.example.test", "urgent", "Urgent: respond by Friday"]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        section = B._email_section(now)
        worth = section.split("**Worth a look**\n", 1)[1]
        assert worth.startswith("- Notifications: “Urgent: respond by Friday”")
        assert "1 more sources in the available snapshot." in section

    def test_group_cap_uses_timestamp_of_its_urgent_header(self, sources, monkeypatch):
        now = sources
        headers = []
        for i in range(1, 6):
            address = f"notifications@mixed{i}.example.test"
            headers.extend([
                "\x01".join(["H2", str(now - 23 * 3600), "U", "Personal", "p",
                           f"Mixed {i}", address, f"urgent-{i}", "Urgent: respond by Friday"]),
                "\x01".join(["H2", str(now - i * 60), "U", "Personal", "p",
                           f"Mixed {i}", address, f"receipt-{i}", f"Your receipt #{i}"]),
            ])
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Fresh Urgent",
            "notifications@fresh.example.test", "fresh", "Urgent: respond by Friday"]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        worth = section.split("**Worth a look**\n", 1)[1]
        assert worth.startswith("- Fresh Urgent: “Urgent: respond by Friday”")
        assert "1 more sources in the available snapshot." in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("negated", [
        "No action required",
        "No further action required — invoice ready",
        "Action is not required — account statement ready",
    ])
    def test_negated_action_notices_do_not_hide_real_action_at_group_cap(self,
                                                                         sources,
                                                                         monkeypatch,
                                                                         negated):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", f"Automated {i}",
            f"no-reply@notice{i}.example.test", str(i), negated])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Payment Alert",
            "no-reply@payments.example.test", "payment", "Action required: pay by Friday"]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        worth = section.split("**Worth a look**\n", 1)[1]
        assert worth.startswith("- Payment Alert: “Action required: pay by Friday”")
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("negated,positive", [
        ("No deadline", "Deadline tomorrow: submit report"),
        ("No upcoming deadlines", "Deadline tomorrow: submit report"),
        ("Deadline cancelled", "Deadline tomorrow: submit report"),
        ("Deadline no longer applies", "Deadline tomorrow: submit report"),
        ("Deadline has been cancelled", "Deadline tomorrow: submit report"),
        ("Deadline is no longer due", "Deadline tomorrow: submit report"),
        ("Do not approve", "Please approve the form"),
        ("Don't approve", "Please approve the form"),
        ("Do not submit", "Please submit the form"),
    ])
    def test_negated_deadline_or_approval_does_not_hide_real_request_at_cap(self,
                                                                            sources,
                                                                            monkeypatch,
                                                                            negated, positive):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", f"Automated {i}",
            f"no-reply@notice{i}.example.test", str(i), negated])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Real Request",
            "no-reply@request.example.test", "request", positive]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        worth = section.split("**Worth a look**\n", 1)[1]
        assert worth.startswith(f"- Real Request: “{positive}”")
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("subject", [
        "Deadline cancelled — new deadline tomorrow: submit report",
        "No deadlines are due this week; new deadline tomorrow: submit report",
        "Do not approve the old form — please approve the new form",
    ])
    def test_positive_clause_after_negated_cue_remains_actionable(self, sources,
                                                                  monkeypatch, subject):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", f"Store {i}",
            f"receipts@store{i}.example.test", str(i), f"Your receipt #{i}"])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "one", subject]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert f"**Worth a look**\n- Course Notices: “{subject}”" in section
        assert "1 more sources in the available snapshot." in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("subject", [
        "No deadline extension: Friday at 5 PM remains firm",
        "No deadline changes: Friday at 5 PM remains firm",
        "No deadline extensions will be granted",
        "No deadline changes are permitted",
        "No deadline has changed; Friday at 5 PM remains firm",
        "No deadlines were extended; Friday at 5 PM remains firm",
    ])
    def test_firm_deadline_survives_five_source_cap(self, sources, monkeypatch,
                                                    subject):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", f"Store {i}",
            f"receipts@store{i}.example.test", str(i), f"Your receipt #{i}"])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "firm", subject]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert f"**Worth a look**\n- Course Notices: “{subject}”" in section
        assert "1 more sources in the available snapshot." in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("subject", [
        "No deadline extension: Friday at 5 PM remains firm",
        "No deadline changes: Friday at 5 PM remains firm",
        "No deadline extensions will be granted",
        "No deadline changes are permitted",
        "No deadline has changed; Friday at 5 PM remains firm",
        "No deadlines were extended; Friday at 5 PM remains firm",
    ])
    def test_firm_deadline_survives_same_sender_subject_cap(self, sources,
                                                            monkeypatch, subject):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", str(i), f"Your receipt #{i}"])
            for i in range(1, 3)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "firm", subject]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        note = next(line for line in section.splitlines() if line.startswith("- Course Notices:"))
        assert note.startswith(f"- Course Notices: “{subject}”")
        assert "+1 more subjects" in note
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("absent", [
        "No deadlines due this week",
        "No upcoming deadlines due this week",
        "No deadlines are due this week",
    ])
    def test_absent_due_notices_do_not_hide_real_deadline_at_source_cap(self,
                                                                        sources,
                                                                        monkeypatch,
                                                                        absent):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", f"Automated {i}",
            f"no-reply@notice{i}.example.test", str(i), absent])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Real Deadline",
            "no-reply@course.example.test", "real", "Deadline tomorrow: submit report"]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert "**Worth a look**\n- Real Deadline: “Deadline tomorrow: submit report”" in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("absent", [
        "No deadlines due this week",
        "No upcoming deadlines due this week",
        "No deadlines are due this week",
    ])
    def test_absent_due_notices_do_not_hide_real_deadline_in_sender(self,
                                                                    sources,
                                                                    monkeypatch,
                                                                    absent):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", str(i), absent + f" #{i}"])
            for i in range(1, 3)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "real", "Deadline tomorrow: submit report"]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        note = next(line for line in section.splitlines() if line.startswith("- Course Notices:"))
        assert note.startswith("- Course Notices: “Deadline tomorrow: submit report”")
        assert "+1 more subjects" in note
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("subject", [
        "No deadline has changed; Friday at 5 PM remains firm",
        "No deadlines were extended; Friday at 5 PM remains firm",
    ])
    def test_unchanged_deadline_from_automated_sender_is_visible(self, sources,
                                                                 monkeypatch,
                                                                 subject):
        now = sources
        monkeypatch.setattr(E, "_headers", "\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "firm", subject]))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert f"**Worth a look**\n- Course Notices: “{subject}”" in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("noise,real", [
        ("Do not approve", "Approval required for the form"),
        ("No approval required", "Approval required for the form"),
        ("Update due to routine maintenance", "Deadline tomorrow: submit report"),
        ("Old subject: “Deadline tomorrow” was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Deadline tomorrow' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Today's deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Today’s deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' new deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ new deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' new deadline. Friday' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ new deadline; Friday’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' deadline'", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but deadline'", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students', but deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but urgently deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however you must deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but submit report' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however kindly submit report’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but kindly submit report'", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however you must still submit report’", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however action required’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but no action required' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Updates’ however no action required", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however no upcoming deadlines due’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: “Deadline tomorrow” was replaced", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' was cancelled deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Deadline tomorrow’ was cancelled yesterday", "Deadline tomorrow: submit report"),
        ("Old subject: “Deadline tomorrow” was replaced last week; no action required", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ was cancelled yesterday; deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' was cancelled last week, but deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' new subject: deadline'", "Deadline tomorrow: submit report"),
        ("Old subject: 'The 'deadline tomorrow' notice' was cancelled", "Deadline tomorrow: submit report"),
        ('Old subject: "Students\' new deadline" was cancelled', "Deadline tomorrow: submit report"),
        ("Old subject: 'Reminder, deadline tomorrow' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Reminder, deadline tomorrow’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Reminder; deadline tomorrow' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Reminder; deadline tomorrow’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Weekly update. Deadline Friday' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Weekly update. Deadline Friday’ was cancelled", "Deadline tomorrow: submit report"),
        ('Previous subject: "Deadline tomorrow" was cancelled', "Deadline tomorrow: submit report"),
        ("Reference: https://example.test/deadline/123", "Deadline tomorrow: submit report"),
    ])
    def test_header_context_cannot_hide_real_request_at_source_cap(self, sources,
                                                                    monkeypatch,
                                                                    noise, real):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", f"Automated {i}",
            f"no-reply@notice{i}.example.test", str(i), noise])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Real Request",
            "no-reply@request.example.test", "real", real]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert f"**Worth a look**\n- Real Request: “{real}”" in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("noise,real", [
        ("Do not approve", "Approval required for the form"),
        ("No approval required", "Approval required for the form"),
        ("Update due to routine maintenance", "Deadline tomorrow: submit report"),
        ("Old subject: “Deadline tomorrow” was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Deadline tomorrow' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Today's deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Today’s deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' new deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ new deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' new deadline. Friday' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ new deadline; Friday’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' deadline'", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but deadline'", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students', but deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but urgently deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however you must deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but submit report' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however kindly submit report’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but kindly submit report'", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however you must still submit report’", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however action required’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' but no action required' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Updates’ however no action required", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ however no upcoming deadlines due’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: “Deadline tomorrow” was replaced", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' was cancelled deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Deadline tomorrow’ was cancelled yesterday", "Deadline tomorrow: submit report"),
        ("Old subject: “Deadline tomorrow” was replaced last week; no action required", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Students’ was cancelled yesterday; deadline’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' was cancelled last week, but deadline' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Students' new subject: deadline'", "Deadline tomorrow: submit report"),
        ("Old subject: 'The 'deadline tomorrow' notice' was cancelled", "Deadline tomorrow: submit report"),
        ('Old subject: "Students\' new deadline" was cancelled', "Deadline tomorrow: submit report"),
        ("Old subject: 'Reminder, deadline tomorrow' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Reminder, deadline tomorrow’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Reminder; deadline tomorrow' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Reminder; deadline tomorrow’ was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: 'Weekly update. Deadline Friday' was cancelled", "Deadline tomorrow: submit report"),
        ("Old subject: ‘Weekly update. Deadline Friday’ was cancelled", "Deadline tomorrow: submit report"),
        ('Previous subject: "Deadline tomorrow" was cancelled', "Deadline tomorrow: submit report"),
        ("Reference: https://example.test/deadline/123", "Deadline tomorrow: submit report"),
    ])
    def test_header_context_cannot_hide_real_request_in_sender(self, sources,
                                                                monkeypatch,
                                                                noise, real):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", str(i), noise + f" #{i}"])
            for i in range(1, 3)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "real", real]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        note = next(line for line in section.splitlines() if line.startswith("- Course Notices:"))
        assert note.startswith(f"- Course Notices: “{real}”")
        assert "+1 more subjects" in note
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("subject", [
        "Old subject: “Deadline tomorrow”; new deadline Friday: submit report",
        "Old subject: Weekly update. New deadline tomorrow: submit report",
        "Old subject: 'Weekly update'. New deadline tomorrow: submit report",
        "Old subject: 'Today's deadline'. New deadline tomorrow: submit report",
        "Old subject: ‘Today’s deadline’, but action required: pay by Friday",
        "Old subject: 'Students' deadline'. New deadline tomorrow: submit report",
        "Old subject: ‘Students’ deadline’, but action required: pay by Friday",
        "Old subject: 'Students' new deadline' was cancelled. New deadline tomorrow: submit report",
        "Old subject: ‘Students’ new deadline’; new deadline Friday: submit report",
        "Old subject: 'Students' new deadline' — John's report due tomorrow",
        "Old subject: ‘Students’ new deadline’; John’s report due tomorrow",
        'Old subject: "Weekly update", new subject: "Deadline tomorrow: submit report"',
        "Old subject: 'Weekly update', new subject: 'Deadline tomorrow: submit report'",
        "Old subject: “Weekly update”, new subject: “Deadline tomorrow: submit report”",
        "Old subject: 'Weekly update' but submit 'report' tomorrow",
        "Old subject: ‘Weekly update’ however submit ‘report’ tomorrow",
        "Old subject: 'Weekly update' new subject: 'Deadline tomorrow: submit report'",
        "Old subject: 'Weekly update' and current title 'Deadline tomorrow: submit report'",
        "Old subject: 'Students' but submit 'report' tomorrow",
        "Old subject: 'Updates' however submit 'report' tomorrow",
        "Old subject: ‘Updates’ however submit ‘report’ tomorrow",
        "Old subject: 'News' but submit 'report' tomorrow",
        "Old subject: ‘News’ but submit ‘report’ tomorrow",
        "Old subject: 'Updates' however submit 'John's report' tomorrow",
        "Old subject: ‘Updates’ however submit ‘John’s report’ tomorrow",
        "Old subject: ‘News’ but submit ‘team’s report’ tomorrow",
        "Old subject: “News” but submit “team’s report” tomorrow",
        "Old subject: 'Updates' however you must submit 'John's report' tomorrow",
        "Old subject: ‘News’ but please urgently submit ‘team’s report’ tomorrow",
        "Old subject: ‘Updates’ however you must still submit ‘John’s report’ tomorrow",
        "Old subject: ‘News’ but kindly submit ‘team’s report’ tomorrow",
        "Old subject: 'Updates' however you must still submit John's report tomorrow",
        "Old subject: ‘News’ but kindly submit John’s report tomorrow",
        "Old subject: ‘Updates’ however action required for ‘report’ tomorrow",
        "Old subject: ‘News’ but action required on ‘form’ tomorrow",
        "Old subject: 'Updates' however action required for 'report' tomorrow",
        "Old subject: ‘News’ but approval required for ‘form’ tomorrow",
        "Old subject: ‘Updates’ however we would appreciate it if you could please submit ‘report’ tomorrow",
        "Old subject: ‘Updates’ however we would really appreciate it if you would kindly take a moment to submit ‘report’ tomorrow",
        "Old subject: 'News' but could you kindly take a moment to review 'form' today",
        "Old subject: ‘Updates’ however we would appreciate it if you could please submit ‘John’s report’ tomorrow",
        "Old subject: ‘Updates’ was cancelled, but submit ‘report’ tomorrow",
        "Old subject: “Weekly update” was replaced, however approval required for “form” tomorrow",
        "Old subject: ‘Updates’ was cancelled, but you must submit ‘John’s report’ tomorrow",
        "Old subject: ‘Updates’ was cancelled yesterday; submit ‘report’ tomorrow",
        "Old subject: “Weekly update” was replaced last week. Approval required for “form” tomorrow",
        "Old subject: ‘Updates’ was cancelled yesterday, but submit ‘report’ tomorrow",
        *[
            f"Old subject: {opening}Updates{closing} was {status}{separator} "
            f"{request} {opening}report{closing} tomorrow"
            for opening, closing in (("'", "'"), ("‘", "’"))
            for status in ("cancelled yesterday", "replaced last week")
            for separator in (", but", ";", ".")
            for request in ("submit", "approval required for")
        ],
        *[
            f"Old subject: {opening}Updates{closing} was {status}{separator} "
            f"{request} {opening}report{closing} tomorrow"
            for opening, closing in (("'", "'"), ("‘", "’"))
            for status in ("cancelled", "replaced")
            for separator in (", but", ", however", ";", "—")
            for request in ("submit", "action required for")
        ],
        *[
            f"Old subject: {opening}Updates{closing} {connector} "
            f"{prefix}submit {opening}report{closing} tomorrow"
            for opening, closing in (("'", "'"), ("‘", "’"))
            for connector in ("but", "however")
            for prefix in ("", "please ", "you must ", "urgently ", "you need to ",
                           "please urgently ", "you urgently need to ",
                           "you must still ", "kindly ", "please immediately ",
                           "you may now ", "if possible please ")
        ],
        "Old subject: 'Students, deadline tomorrow'. New deadline Friday: submit report",
        "Old subject: ‘Students; deadline tomorrow’, but action required: pay by Friday",
        "Old subject: 'Weekly update. Deadline Friday'; new deadline tomorrow: submit report",
        "Previous subject: Weekly update, but action required: pay by Friday",
        "Reference: https://example.test/deadline/old; new deadline Friday: submit report",
        "Update due to maintenance; deadline Friday: submit report",
    ])
    def test_real_deadline_after_context_cue_remains_visible(self, sources,
                                                             monkeypatch, subject):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", f"Store {i}",
            f"receipts@store{i}.example.test", str(i), f"Your receipt #{i}"])
            for i in range(1, 6)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "real", subject]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        assert f"**Worth a look**\n- Course Notices: “{subject}”" in section
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    @pytest.mark.parametrize("subject", [
        "Old subject: Weekly update. New deadline tomorrow: submit report",
        "Old subject: 'Weekly update'. New deadline tomorrow: submit report",
        "Old subject: 'Today's deadline'. New deadline tomorrow: submit report",
        "Old subject: ‘Today’s deadline’, but action required: pay by Friday",
        "Old subject: 'Students' deadline'. New deadline tomorrow: submit report",
        "Old subject: ‘Students’ deadline’, but action required: pay by Friday",
        "Old subject: 'Students' new deadline' was cancelled. New deadline tomorrow: submit report",
        "Old subject: ‘Students’ new deadline’; new deadline Friday: submit report",
        "Old subject: 'Students' new deadline' — John's report due tomorrow",
        "Old subject: ‘Students’ new deadline’; John’s report due tomorrow",
        'Old subject: "Weekly update", new subject: "Deadline tomorrow: submit report"',
        "Old subject: 'Weekly update', new subject: 'Deadline tomorrow: submit report'",
        "Old subject: “Weekly update”, new subject: “Deadline tomorrow: submit report”",
        "Old subject: 'Weekly update' but submit 'report' tomorrow",
        "Old subject: ‘Weekly update’ however submit ‘report’ tomorrow",
        "Old subject: 'Weekly update' new subject: 'Deadline tomorrow: submit report'",
        "Old subject: 'Weekly update' and current title 'Deadline tomorrow: submit report'",
        "Old subject: 'Students' but submit 'report' tomorrow",
        "Old subject: 'Updates' however submit 'report' tomorrow",
        "Old subject: ‘Updates’ however submit ‘report’ tomorrow",
        "Old subject: 'News' but submit 'report' tomorrow",
        "Old subject: ‘News’ but submit ‘report’ tomorrow",
        "Old subject: 'Updates' however submit 'John's report' tomorrow",
        "Old subject: ‘Updates’ however submit ‘John’s report’ tomorrow",
        "Old subject: ‘News’ but submit ‘team’s report’ tomorrow",
        "Old subject: “News” but submit “team’s report” tomorrow",
        "Old subject: 'Updates' however you must submit 'John's report' tomorrow",
        "Old subject: ‘News’ but please urgently submit ‘team’s report’ tomorrow",
        "Old subject: ‘Updates’ however you must still submit ‘John’s report’ tomorrow",
        "Old subject: ‘News’ but kindly submit ‘team’s report’ tomorrow",
        "Old subject: 'Updates' however you must still submit John's report tomorrow",
        "Old subject: ‘News’ but kindly submit John’s report tomorrow",
        "Old subject: ‘Updates’ however action required for ‘report’ tomorrow",
        "Old subject: ‘News’ but action required on ‘form’ tomorrow",
        "Old subject: 'Updates' however action required for 'report' tomorrow",
        "Old subject: ‘News’ but approval required for ‘form’ tomorrow",
        "Old subject: ‘Updates’ however we would appreciate it if you could please submit ‘report’ tomorrow",
        "Old subject: ‘Updates’ however we would really appreciate it if you would kindly take a moment to submit ‘report’ tomorrow",
        "Old subject: 'News' but could you kindly take a moment to review 'form' today",
        "Old subject: ‘Updates’ however we would appreciate it if you could please submit ‘John’s report’ tomorrow",
        "Old subject: ‘Updates’ was cancelled, but submit ‘report’ tomorrow",
        "Old subject: “Weekly update” was replaced, however approval required for “form” tomorrow",
        "Old subject: ‘Updates’ was cancelled, but you must submit ‘John’s report’ tomorrow",
        "Old subject: ‘Updates’ was cancelled yesterday; submit ‘report’ tomorrow",
        "Old subject: “Weekly update” was replaced last week. Approval required for “form” tomorrow",
        "Old subject: ‘Updates’ was cancelled yesterday, but submit ‘report’ tomorrow",
        *[
            f"Old subject: {opening}Updates{closing} was {status}{separator} "
            f"{request} {opening}report{closing} tomorrow"
            for opening, closing in (("'", "'"), ("‘", "’"))
            for status in ("cancelled yesterday", "replaced last week")
            for separator in (", but", ";", ".")
            for request in ("submit", "approval required for")
        ],
        *[
            f"Old subject: {opening}Updates{closing} was {status}{separator} "
            f"{request} {opening}report{closing} tomorrow"
            for opening, closing in (("'", "'"), ("‘", "’"))
            for status in ("cancelled", "replaced")
            for separator in (", but", ", however", ";", "—")
            for request in ("submit", "action required for")
        ],
        *[
            f"Old subject: {opening}Updates{closing} {connector} "
            f"{prefix}submit {opening}report{closing} tomorrow"
            for opening, closing in (("'", "'"), ("‘", "’"))
            for connector in ("but", "however")
            for prefix in ("", "please ", "you must ", "urgently ", "you need to ",
                           "please urgently ", "you urgently need to ",
                           "you must still ", "kindly ", "please immediately ",
                           "you may now ", "if possible please ")
        ],
        "Old subject: 'Students, deadline tomorrow'. New deadline Friday: submit report",
        "Old subject: ‘Students; deadline tomorrow’, but action required: pay by Friday",
        "Old subject: 'Weekly update. Deadline Friday'; new deadline tomorrow: submit report",
        "Previous subject: Weekly update, but action required: pay by Friday",
    ])
    def test_real_request_after_old_subject_survives_same_sender_cap(self,
                                                                     sources,
                                                                     monkeypatch,
                                                                     subject):
        now = sources
        headers = ["\x01".join([
            "H2", str(now - i * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", str(i), f"Your receipt #{i}"])
            for i in range(1, 3)]
        headers.append("\x01".join([
            "H2", str(now - 10 * 60), "U", "Personal", "p", "Course Notices",
            "no-reply@course.example.test", "real", subject]))
        monkeypatch.setattr(E, "_headers", "\n".join(headers))
        monkeypatch.setattr(E, "_parse_raw", lambda: (_ for _ in ()).throw(AssertionError("body read")))
        section = B._email_section(now)
        note = next(line for line in section.splitlines() if line.startswith("- Course Notices:"))
        assert note.startswith(f"- Course Notices: “{subject}”")
        assert "+1 more subjects" in note
        assert section in B._render_brief(now, "- Synthetic Messages only.")

    def test_distinct_overlong_subjects_keep_distinct_count(self, sources, monkeypatch):
        now = sources
        monkeypatch.setattr(E, "_headers", "\n".join(
            "\x01".join(["H2", str(now - i), "U", "Personal", "p", "Bank",
                          "notices@bank.example.test", str(i), "Receipt " + str(i) + "X" * 230])
            for i in range(3)))
        section = B._email_section(now)
        assert "Inbox — 3 emails · 3 unread" in section
        assert section.count("Subject too long to display here (see Mail)") == 2
        assert "+1 more subjects" in section
        assert "Receipt 0" not in section and "Receipt 1" not in section

    def test_older_fallback_subject_shows_received_time(self, sources, monkeypatch):
        now = sources
        old = now - 26 * 3600
        monkeypatch.setattr(E, "_headers", "\n".join([
            "\x01".join(["H2", str(now + 3600), "U", "Personal", "p", "Future",
                          "future@example.test", "future", "Future-dated header"]),
            "\x01".join(["H2", str(old), "U", "Personal", "p", "Alex",
                          "alex@example.test", "old", "Older deadline notice"]),
        ]))
        section = B._email_section(now)
        received = datetime.fromtimestamp(old).strftime("%b %-d, %Y at %-I:%M %p")
        assert "Older deadline notice” (received " + received + ")" in section
        assert "Future-dated header" not in section

    def test_messages_name_their_speaker_without_routing_markers(self, sources):
        section = B._messages_section(sources)
        assert "you: “When are you getting the ChatGPT max plan”" not in section
        assert "Grad GC" in section
        assert "->" not in section and "means" not in section

    def test_notification_cards_are_plain_text(self, sources):
        for card in (B._today_card(sources), B._messages_card(sources)):
            assert card and "**" not in card and "- " not in card
            assert not any(s in card for s in SCAFFOLD)
        assert "3 reminders due" in B._today_card(sources)


class TestLatency:
    @pytest.mark.asyncio
    async def test_one_readiness_wait_per_press(self, sources, monkeypatch):
        """The endpoint's wait is the only wait, and no tool adds another."""
        from service import main
        from service.memory.store import SessionStore
        from service.tools import assistant_tools, email_tools, imessage_tools
        ensure = AsyncMock(return_value={"syncing": False, "sources": []})
        monkeypatch.setattr("service.assistant.sync_status.ensure_daily_sources", ensure)
        monkeypatch.setattr("service.assistant.sync_status.ensure_sources",
                            AsyncMock(side_effect=AssertionError("no per-source wait")))
        monkeypatch.setattr(main, "store",
                            SessionStore(Path(_scratch.name) / "one-wait.db"))
        for module, name in ((assistant_tools, "get_upcoming"),
                             (email_tools, "summarize_emails"),
                             (imessage_tools, "summarize_messages")):
            monkeypatch.setattr(module, name,
                                AsyncMock(side_effect=AssertionError(f"{name} must not run")))
        result = await main.assistant_daily_summary()
        assert result["ok"] and result["text"]
        assert ensure.await_count == 1

    @pytest.mark.asyncio
    async def test_composing_the_brief_publishes_no_sync_requests(self, sources):
        with patch.object(hub, "publish", new_callable=AsyncMock) as publish:
            sections = await B._generate_brief("morning")
        assert sections["FULL"]
        publish.assert_not_awaited()


class TestScheduledDelivery:
    @pytest.fixture(autouse=True)
    def _quiet_clock(self, monkeypatch, tmp_path):
        monkeypatch.setattr(scheduler, "_brief_state_path",
                            lambda: tmp_path / "brief_state.json")
        monkeypatch.setattr(scheduler, "_last_brief", None)
        monkeypatch.setattr(scheduler, "_last_brief_loaded", False)
        monkeypatch.setattr(scheduler, "_brief_attempts", {})
        from service.assistant.store import AssistantStore
        store = AssistantStore(tmp_path / "assistant.db")
        monkeypatch.setattr(scheduler, "assistant_store", store)
        monkeypatch.setattr(B, "assistant_store", store)
        monkeypatch.setattr(hub, "_store", store)
        monkeypatch.setattr(B, "datetime", _clock_at(8, 5))

    @pytest.mark.asyncio
    async def test_a_holdback_publishes_nothing_and_keeps_the_day_open(self, monkeypatch):
        holdback = {"FULL": "Wisp is still syncing your email after launch."}
        with patch.object(B, "_sections", AsyncMock(return_value=holdback)), \
             patch.object(hub, "publish", new_callable=AsyncMock) as publish:
            delivered = await B.run_scheduled_brief("morning")
        assert delivered is False
        publish.assert_not_awaited()

        # …and the day stays open, so the real brief can still go out.
        with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=False)), \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(8, 5)):
            await scheduler._maybe_daily_brief()
        assert scheduler._brief_date() is None

    @pytest.mark.asyncio
    async def test_a_delivered_brief_publishes_once_and_survives_a_restart(self, monkeypatch):
        ready = {"FULL": "Your day.", "TODAY": "Today: nothing.",
                 "MESSAGES": "1 recent message in Trishe.", "READY": "1"}
        with patch.object(B, "_sections", AsyncMock(return_value=ready)), \
             patch.object(hub, "publish", wraps=hub.publish) as publish, \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(8, 5)):
            await scheduler._maybe_daily_brief()
            assert publish.await_count == 1
            event = publish.await_args.args[0]
            assert event["type"] == "daily_brief" and event["text"] == "Your day."

            assert scheduler._brief_date() is None
            assert scheduler.assistant_store.completion("daily_brief") is None
            row = scheduler.assistant_store.pending_events()[0]
            scheduler.assistant_store.acknowledge_event(row["id"], "daily_brief")
            assert scheduler.assistant_store.completion("daily_brief") == "2026-09-08"

            # A relaunch inside the window re-reads the date from disk instead of
            # firing again. This is the run of false "ready" pings.
            monkeypatch.setattr(scheduler, "_last_brief", None)
            monkeypatch.setattr(scheduler, "_last_brief_loaded", False)
            await scheduler._maybe_daily_brief()
            assert publish.await_count == 1

    @pytest.mark.asyncio
    async def test_a_source_that_never_finishes_gives_the_day_up(self):
        with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=False)) as run, \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(8, 5)):
            for _ in range(scheduler._MAX_BRIEF_ATTEMPTS + 5):
                await scheduler._maybe_daily_brief()
        assert run.await_count == scheduler._MAX_BRIEF_ATTEMPTS
        assert scheduler._brief_date() is None
        assert scheduler.assistant_store.completion("daily_brief") is None

    @pytest.mark.asyncio
    async def test_nothing_fires_outside_the_window(self):
        with patch.object(B, "run_scheduled_brief", AsyncMock()) as run, \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(15, 0)):
            await scheduler._maybe_daily_brief()
        run.assert_not_awaited()


class TestFollowUps:
    @pytest.mark.asyncio
    async def test_the_brief_is_recorded_in_the_conversation(self, sources, monkeypatch):
        from service import main
        from service.memory.store import SessionStore
        store = SessionStore(Path(_scratch.name) / "follow-ups.db")
        monkeypatch.setattr(main, "store", store)
        monkeypatch.setattr("service.assistant.sync_status.ensure_daily_sources",
                            AsyncMock(return_value={"syncing": False, "sources": []}))
        result = await main.assistant_daily_summary({"session_id": ""})
        sid = result["session_id"]
        assert sid, "the endpoint must hand back a session to continue"
        # What a follow-up like "send Trishe my daily summary" routes off.
        assert store.last_user_turn(sid) == "Daily summary"
        assert store.last_assistant_turn(sid) == result["text"]

        # A second press continues the SAME session rather than orphaning it.
        again = await main.assistant_daily_summary({"session_id": sid})
        assert again["session_id"] == sid and store.turn_count(sid) == 4


def _clock_at(hour: int, minute: int):
    """`datetime` with now() pinned, for the scheduler's window arithmetic."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, hour, minute)
    return Clock
