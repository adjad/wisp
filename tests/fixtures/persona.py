"""The QA/sandbox persona: one consistent cast of synthetic people, accounts,
and content, used by both scripts/wisp_testdata.py (in-place DB/cache
seeding for manual testing) and the sandbox's fake-phone world.

Every handle/domain is RFC 2606/6761-reserved (.test, .example, 555 numbers)
so it can never collide with anything real — same convention the original
wisp_testdata.py used, kept unchanged here.

NOTE on identity emails: email_tools.cache_identity() (and the load-time
filter in email_tools.py's `_is_placeholder`) drops any address ending in a
placeholder domain (.test/.example/etc) — so IDENTITY_EMAILS below, though
fine for wisp_testdata.py's direct-to-cache-file fixtures, will never survive
a push through the real POST /assistant/sync/emails endpoint. A consumer
that needs identity ground truth to actually stick (e.g. a future sandbox
pushing through the real backend) needs non-placeholder-*looking* domains.

Every `now`-taking function is relative to a caller-supplied reference
epoch — nothing here reads the wall clock — so the same builders work for a
one-shot `build-cache` run and for a long-lived sandbox world anchored to a
`booted_at` that may be far from real now.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from tests.fixtures import wire

QA_TAG = "[Wisp QA] "

# handle (any format) -> display name.
RAW_CONTACTS: dict[str, str] = {
    "+1 (925) 555-1234": "Mom",
    "+1 (925) 555-5678": "Dad",
    "+1 (415) 555-9012": "Priya",
    "priya.test@example.com": "Priya",
    "+1 (512) 555-3456": "Jordan Ellis",
    "+1 (650) 555-7788": "Alex Chen",
    "+1 (650) 555-9911": "Alexis Nguyen",
    "+1 (408) 555-2222": "Dr. Patel",
}

IDENTITY_EMAILS = ["testuser@example.com", "testuser.work@examplecorp.test"]


def contacts() -> tuple[dict[str, str], dict[str, str]]:
    """(norm_handle -> name, raw_handle -> name)."""
    return wire.contacts_map(RAW_CONTACTS), dict(RAW_CONTACTS)


def contact_handles() -> dict[str, list[str]]:
    return wire.contact_handles_map(RAW_CONTACTS)


def _at(now: float, days: int = 0, hour: int = 9, minute: int = 0) -> float:
    today = datetime.fromtimestamp(now)
    d = (today + timedelta(days=days)).replace(hour=hour, minute=minute,
                                                second=0, microsecond=0)
    return d.timestamp()


def _in_minutes(now: float, m: int) -> float:
    return now + m * 60


# ---------------------------------------------------------------------------
# Commitments (calendar events, meetings, exams, assignments, reminders)
# ---------------------------------------------------------------------------

def _commitment(source_id: str, kind: str, title: str, when_ts: float, **kw) -> dict:
    return {
        "source_id": source_id, "kind": kind, "title": title, "when_ts": when_ts,
        "context": kw.get("context"), "organizer": kw.get("organizer"),
        "account": kw.get("account"), "all_day": kw.get("all_day", False),
        "location": kw.get("location"), "url": kw.get("url"),
        "confidence": kw.get("confidence", 1.0),
    }


def commitment_rows(now: float) -> list[dict]:
    at, mins = lambda *a, **k: _at(now, *a, **k), lambda m: _in_minutes(now, m)
    return [
        # Meeting starting soon — exercises the T-30m/T-10m reminder stages
        # and the live countdown chip during a real test session.
        _commitment("seed_meeting_soon", "meeting", "Aurora Proposal Sync",
                    mins(45), organizer="Priya Shah", context="Work",
                    location="Zoom", account="iCloud"),
        # Tomorrow, different linked account — pairs with the item above so
        # get_upcoming's multi-account tag (and the `account` filter param)
        # both have something real to exercise.
        _commitment("seed_1on1_tomorrow", "meeting", "1:1 with Sam",
                    at(1, 10, 0), organizer="Sam Rivera", context="Work",
                    account="Work Gmail"),
        # Exam a couple days out — EXAM label + the T-1w/1d/3h stage ladder.
        _commitment("seed_exam_orgo", "exam", "Organic Chemistry Midterm",
                    at(2, 14, 0), context="CHEM 201"),
        # Assignment due tomorrow — "assignment" T-1d/3h stage, TOMORROW tag.
        _commitment("seed_assignment_pset", "assignment", "Problem Set 5",
                    at(1, 23, 59), context="CHEM 201"),
        # Assignment due later this week — weekday-name day tag.
        _commitment("seed_assignment_essay", "assignment",
                    "Essay Draft — Cold War Historiography", at(6, 23, 59),
                    context="HIST 340"),
        # All-day event today — exercises the "all day" clock rendering.
        _commitment("seed_offsite", "event", "Company Offsite", at(0, 9, 0),
                    all_day=True, context="Work"),
        # Recurring event: SAME source_id, two occurrences — the exact shape
        # of the recurring-event dedup bug (EventKit gives every occurrence
        # the same eventIdentifier); both must survive as separate rows.
        _commitment("seed_standup_recurring", "meeting", "Team Standup",
                    at(1, 9, 30), context="Work"),
        _commitment("seed_standup_recurring", "meeting", "Team Standup",
                    at(8, 9, 30), context="Work"),
        # Far out — exercises the "Wed Aug 26"-style far-future date tag
        # instead of a bare weekday name.
        _commitment("seed_wedding", "event", "Cousin's Wedding", at(35, 16, 0),
                    location="Napa, CA", context="Personal"),
        # No organizer AND no context — the "no who" rendering fallback.
        _commitment("seed_bare_event", "event", "Dentist Follow-up",
                    at(4, 11, 15)),
        # Two similarly-titled events — cancel_event("coffee with alex")
        # should hit BOTH via substring match and force the disambiguation
        # reply rather than guessing.
        _commitment("seed_coffee_alex", "event", "Coffee with Alex", at(1, 17, 0),
                    context="Personal"),
        _commitment("seed_coffee_alexis", "event", "Coffee with Alexis", at(3, 17, 0),
                    context="Personal"),
        # Past events — get_past_events / "what did I have last week".
        _commitment("seed_past_review", "meeting", "Quarterly Planning Review",
                    at(-3, 15, 0), organizer="Dana Kim", context="Work"),
        _commitment("seed_past_birthday", "event", "Adi's Birthday Dinner",
                    at(-40, 19, 0), context="Personal"),
        # Manual reminders — near-term one fires almost immediately after
        # seeding+launch (good for a live "did the notification fire"
        # check); the other is a normal few-days-out reminder.
        _commitment("seed_reminder_laundry", "reminder", "Take the laundry out",
                    mins(3)),
        _commitment("seed_reminder_dentist", "reminder",
                    "Call Dr. Patel to reschedule", at(2, 12, 0)),
    ]


def fact_rows() -> list[tuple[str, str, bool]]:
    """(category, text, pinned)."""
    return [
        ("person", QA_TAG + "Jordan Ellis is a test contact — Adi's close "
         "friend from grad school, lives in Austin.", False),
        ("preference", QA_TAG + "For QA purposes: the user prefers concise "
         "summaries over long ones.", False),
        ("project", QA_TAG + "The 'Aurora' project (test data) is a client "
         "website redesign due in September.", False),
        ("routine", QA_TAG + "Test routine fact: the user goes to the gym "
         "every Tuesday and Thursday morning.", False),
        ("fact", QA_TAG + "The user's test food allergy is peanuts — for "
         "allergy-aware suggestions.", False),
        ("fact", QA_TAG + "Pinned test fact — should always appear first if "
         "pinning works.", True),
        ("fact", QA_TAG + "The user used to work at Initech (test fact) — "
         "should be forgettable via the forget tool.", False),
    ]


# ---------------------------------------------------------------------------
# Messages / Mail / Notes content — row-dicts ready for service.fixtures.wire
# ---------------------------------------------------------------------------

def message_rows(now: float) -> list[dict]:
    at = lambda *a, **k: _at(now, *a, **k)
    grad_gc = wire.thread_context("Grad School GC", [], is_group=True)
    family = wire.thread_context("Family", [], is_group=True)
    return [
        # Mom — 1:1, a question waiting on a reply, today.
        {"ts": at(0, 9, 5), "context": "Mom", "who": "Mom",
         "text": "Are you free Saturday for Aunt Reeta's party? Should I "
                 "save you a seat?"},
        {"ts": at(0, 9, 30), "context": "Mom", "who": "Me",
         "text": "Should be! I'll confirm tonight."},
        # Yesterday, same thread.
        {"ts": at(-1, 18, 0), "context": "Mom", "who": "Mom",
         "text": "Don't forget to bring the folding chairs"},
        # Dad — 1:1, casual, contains a detail worth a verbatim lookup test.
        {"ts": at(0, 8, 10), "context": "Dad", "who": "Dad",
         "text": "Wifi password is Sunflower88 if you need it"},
        # Priya — 1:1, time-sensitive.
        {"ts": at(0, 16, 45), "context": "Priya", "who": "Priya",
         "text": "My flight lands at 6pm, can you pick me up from SFO?"},
        # Alex Chen — 1:1, work, ties to the "Coffee with Alex" calendar item.
        {"ts": at(0, 11, 0), "context": "Alex Chen", "who": "Alex Chen",
         "text": "Still on for coffee tomorrow? Want to run the deck by "
                 "you first"},
        # Unsaved number — not in contacts, tests the unresolved-handle path.
        {"ts": at(0, 13, 20), "context": "+19998887777", "who": "+19998887777",
         "text": "Hey it's Marcus from the conference, great meeting you!"},
        # Group chat #1 — named group.
        {"ts": at(0, 20, 0), "context": grad_gc, "who": "Jordan Ellis",
         "text": "anyone free to do the reunion trip in October?"},
        {"ts": at(0, 20, 5), "context": grad_gc, "who": "Priya", "text": "I'm in!"},
        # Group chat #2 — family.
        {"ts": at(0, 19, 0), "context": family, "who": "Dad",
         "text": "dinner's at 7 tonight"},
        # Old message with a since-past relative claim — regression case for
        # the profile builder's date-stamping.
        {"ts": at(-35, 14, 0), "context": "Mom", "who": "Mom",
         "text": "England match tomorrow at 2pm, you watching?"},
    ]


def email_header_rows(now: float, deep_history: bool = False) -> list[dict]:
    at = lambda *a, **k: _at(now, *a, **k)
    rows = [
        {"ts": at(0, 9, 0), "unread": True, "account": "Personal",
         "sender_name": "Aurora Client", "sender_addr": "hello@auroraclient.test",
         "subject": "Re: proposal sync Thursday?"},
        {"ts": at(0, 10, 30), "unread": True, "account": "Work",
         "sender_name": "Priya Shah", "sender_addr": "priya.shah@examplecorp.test",
         "subject": "Notes from this morning"},
        {"ts": at(-1, 8, 0), "unread": False, "account": "Personal",
         "sender_name": "Order Confirmation", "sender_addr": "orders@shoptest.example",
         "subject": "Your order #A19-88231 has shipped"},
        {"ts": at(-1, 17, 0), "unread": True, "account": "Work",
         "sender_name": "Finance", "sender_addr": "finance@examplecorp.test",
         "subject": "Final reminder: submit your W-9 by Friday"},
        {"ts": at(0, 7, 45), "unread": False, "account": "Personal",
         "sender_name": "Daily Deals", "sender_addr": "deals@promo.test",
         "subject": "50% off everything this weekend!"},
        {"ts": at(-2, 12, 0), "unread": False, "account": "Work",
         "sender_name": "Dana Kim", "sender_addr": "dana.kim@examplecorp.test",
         "subject": "Quarterly planning review — thanks for joining"},
    ]
    if deep_history:
        rows.append({"ts": at(-200, 9, 0), "unread": False, "account": "Personal",
                     "sender_name": "Napa Vineyard Tours",
                     "sender_addr": "info@napatours.test",
                     "subject": "Your wedding weekend itinerary"})
    return rows


def email_raw_rows(now: float) -> list[dict]:
    at = lambda *a, **k: _at(now, *a, **k)
    return [
        {"ts": at(0, 9, 0), "unread": True, "account": "Personal",
         "sender_name": "Aurora Client", "sender_addr": "hello@auroraclient.test",
         "to": ["testuser@example.com"], "subject": "Re: proposal sync Thursday?",
         "message_id": "<seed-aurora-01@sandbox.wisp.test>",
         "body": "Hi! Can we sync Thursday 2pm on the Aurora proposal? Want "
                 "to confirm scope before we send the estimate."},
        {"ts": at(-1, 8, 0), "unread": False, "account": "Personal",
         "sender_name": "Order Confirmation", "sender_addr": "orders@shoptest.example",
         "to": ["testuser@example.com"], "subject": "Your order #A19-88231 has shipped",
         "message_id": "<seed-order-01@sandbox.wisp.test>",
         "body": "Good news — order #A19-88231 shipped and should arrive "
                 "Friday. Track it at shoptest.example/track/A19-88231."},
        {"ts": at(-1, 17, 0), "unread": True, "account": "Work",
         "sender_name": "Finance", "sender_addr": "finance@examplecorp.test",
         "to": ["testuser.work@examplecorp.test"],
         "subject": "Final reminder: submit your W-9 by Friday",
         "message_id": "<seed-w9-01@sandbox.wisp.test>",
         "body": "This is a final reminder that your W-9 is due by Friday "
                 "EOD or your next payment will be delayed."},
        {"ts": at(0, 7, 45), "unread": False, "account": "Personal",
         "sender_name": "Daily Deals", "sender_addr": "deals@promo.test",
         "to": ["testuser@example.com"], "subject": "50% off everything this weekend!",
         "message_id": "<seed-deals-01@sandbox.wisp.test>",
         "body": "Everything's on sale this weekend only. Shop now before "
                 "it's gone!"},
    ]


def note_rows(now: float) -> list[dict]:
    at = lambda *a, **k: _at(now, *a, **k)
    return [
        {"ts": at(0, 8, 0), "title": "Home WiFi", "folder": "Household",
         "body": "Network: NestNet-5G / Password: Sunflower88"},
        {"ts": at(-2, 21, 0), "title": "Napa Trip Packing List", "folder": "Travel",
         "body": "- Passport\n- Sunscreen\n- Hiking boots\n- Charger"},
        {"ts": at(-5, 13, 0), "title": "App idea — recipe box", "folder": "Ideas",
         "body": "An app that turns a photo of a handwritten recipe card "
                 "into a searchable digital recipe. MVP: OCR + tagging."},
        {"ts": at(0, 6, 30), "title": "Quick thought", "folder": "",
         "body": "Ask Sam about the Q3 budget numbers before the offsite."},
        {"ts": at(-60, 10, 0), "title": "Gift ideas for Priya birthday (OLD)",
         "folder": "Personal",
         "body": "Maybe the record player she mentioned? Or the trip to "
                 "Big Sur."},
    ]
