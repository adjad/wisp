#!/usr/bin/env python3
"""Sample/test data for manually exercising Wisp before release.

Two kinds of data, handled differently on purpose:

1. **Wisp's own databases** (~/.moe/assistant.db commitments, ~/.moe/facts.db
   facts) — seeded IN PLACE, safely. Commitments use source="wisp_seed" (a
   value the real Calendar/Mail syncers never write, so they can never step on
   it, and re-running this script just upserts the same rows instead of
   duplicating them — see AssistantStore.sync_source's diff-by-source
   semantics). Facts are prefixed "[Wisp QA] " so they can never fuzzy-collide
   with a real remembered fact and are trivially greppable/deletable.

2. **Synced-content caches** (Messages/Mail/Notes — ~/.moe/cache/*.txt) — these
   are wholesale wholesale-replace snapshots pushed by the real Swift readers
   from the user's REAL chat.db/Mail.app/Notes.app, so this script never
   touches them directly. Instead `build-cache` renders synthetic fixtures to
   test_fixtures/cache/ (plain repo files); scripts/install_cache_fixtures.sh
   is the explicit, backed-up swap-in step for whoever wants to test that
   surface too.

Usage:
    .venv/bin/python scripts/wisp_testdata.py seed-db       # commitments + facts
    .venv/bin/python scripts/wisp_testdata.py clear-db       # remove exactly what seed-db added
    .venv/bin/python scripts/wisp_testdata.py build-cache    # write test_fixtures/cache/*.txt
    .venv/bin/python scripts/wisp_testdata.py status         # what's currently seeded
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QA_TAG = "[Wisp QA] "
SEED_SOURCE = "wisp_seed"
FIXTURE_DIR = ROOT / "test_fixtures" / "cache"

NOW = time.time()
TODAY = datetime.fromtimestamp(NOW)


def _at(days: int = 0, hour: int = 9, minute: int = 0) -> float:
    d = (TODAY + timedelta(days=days)).replace(hour=hour, minute=minute,
                                                 second=0, microsecond=0)
    return d.timestamp()


def _in_minutes(m: int) -> float:
    return NOW + m * 60


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


def build_commitments() -> list[dict]:
    items = [
        # Meeting starting soon — exercises the T-30m/T-10m reminder stages and
        # the live countdown chip during a real test session.
        _commitment("seed_meeting_soon", "meeting", "Aurora Proposal Sync",
                    _in_minutes(45), organizer="Priya Shah", context="Work",
                    location="Zoom", account="iCloud"),
        # Tomorrow, different linked account — pairs with the item above so
        # get_upcoming's multi-account tag (and the `account` filter param)
        # both have something real to exercise.
        _commitment("seed_1on1_tomorrow", "meeting", "1:1 with Sam",
                    _at(1, 10, 0), organizer="Sam Rivera", context="Work",
                    account="Work Gmail"),
        # Exam a couple days out — EXAM label + the T-1w/1d/3h stage ladder.
        _commitment("seed_exam_orgo", "exam", "Organic Chemistry Midterm",
                    _at(2, 14, 0), context="CHEM 201"),
        # Assignment due tomorrow — "assignment" T-1d/3h stage, TOMORROW tag.
        _commitment("seed_assignment_pset", "assignment", "Problem Set 5",
                    _at(1, 23, 59), context="CHEM 201"),
        # Assignment due later this week — weekday-name day tag.
        _commitment("seed_assignment_essay", "assignment",
                    "Essay Draft — Cold War Historiography", _at(6, 23, 59),
                    context="HIST 340"),
        # All-day event today — exercises the "all day" clock rendering.
        _commitment("seed_offsite", "event", "Company Offsite", _at(0, 9, 0),
                    all_day=True, context="Work"),
        # Recurring event: SAME source_id, two occurrences — the exact shape
        # of the recurring-event dedup bug (EventKit gives every occurrence
        # the same eventIdentifier); both must survive as separate rows.
        _commitment("seed_standup_recurring", "meeting", "Team Standup",
                    _at(1, 9, 30), context="Work"),
        {**_commitment("seed_standup_recurring", "meeting", "Team Standup",
                        _at(8, 9, 30), context="Work")},
        # Far out — exercises the "Wed Aug 26"-style far-future date tag
        # instead of a bare weekday name.
        _commitment("seed_wedding", "event", "Cousin's Wedding", _at(35, 16, 0),
                     location="Napa, CA", context="Personal"),
        # No organizer AND no context — the "no who" rendering fallback.
        _commitment("seed_bare_event", "event", "Dentist Follow-up",
                    _at(4, 11, 15)),
        # Two similarly-titled events — cancel_event("coffee with alex") should
        # hit BOTH via substring match and force the disambiguation reply
        # rather than guessing.
        _commitment("seed_coffee_alex", "event", "Coffee with Alex", _at(1, 17, 0),
                    context="Personal"),
        _commitment("seed_coffee_alexis", "event", "Coffee with Alexis", _at(3, 17, 0),
                    context="Personal"),
        # Past events — get_past_events / "what did I have last week".
        _commitment("seed_past_review", "meeting", "Quarterly Planning Review",
                    _at(-3, 15, 0), organizer="Dana Kim", context="Work"),
        _commitment("seed_past_birthday", "event", "Adi's Birthday Dinner",
                    _at(-40, 19, 0), context="Personal"),
        # Manual reminders — near-term one fires almost immediately after
        # seeding+launch (good for a live "did the notification fire" check);
        # the other is a normal few-days-out reminder.
        _commitment("seed_reminder_laundry", "reminder", "Take the laundry out",
                    _in_minutes(3)),
        _commitment("seed_reminder_dentist", "reminder",
                    "Call Dr. Patel to reschedule", _at(2, 12, 0)),
    ]
    return items


# ---------------------------------------------------------------------------
# Facts (explicit "remember"-style memory)
# ---------------------------------------------------------------------------

def build_facts() -> list[tuple[str, str, bool]]:
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


def seed_db() -> None:
    from service.assistant.store import assistant_store
    from service.memory.facts import store as fact_store

    items = build_commitments()
    n = assistant_store.sync_source(SEED_SOURCE, items)
    print(f"✓ seeded/updated {n} commitments (source={SEED_SOURCE!r})")
    # Seed rows are hidden from every store read path unless the backend runs
    # with WISP_QA_SEED=1 (see store._seed_clause). Without that gate a
    # forgotten seed kept surfacing fake events in the daily brief and firing
    # fake reminder notifications, which looked exactly like the model
    # hallucinating today's schedule.
    print("  NOTE: these are INVISIBLE to Wisp unless the backend runs with "
          "WISP_QA_SEED=1 — set it before launching, and run `clear-db` when "
          "you're done testing.")

    for category, text, pinned in build_facts():
        res = fact_store.add(text, category=category)
        if pinned:
            fact_store.set_pinned(res["id"], True)
        verb = "updated" if res["updated"] else "added"
        print(f"✓ {verb} fact [{category}]: {text[:70]}…")


def clear_db() -> None:
    from service.assistant.store import assistant_store
    from service.memory.facts import store as fact_store

    with assistant_store._lock:
        ids = [r["id"] for r in assistant_store._db.execute(
            "SELECT id FROM commitments WHERE source=?", (SEED_SOURCE,)).fetchall()]
        for cid in ids:
            assistant_store._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
        cur = assistant_store._db.execute("DELETE FROM commitments WHERE source=?", (SEED_SOURCE,))
        assistant_store._db.commit()
    print(f"✓ removed {cur.rowcount} seeded commitments")

    with fact_store._lock:
        cur = fact_store._db.execute("DELETE FROM facts WHERE text LIKE ?", (QA_TAG + "%",))
        fact_store._db.commit()
    print(f"✓ removed {cur.rowcount} seeded facts")


def status() -> None:
    from service.assistant.store import assistant_store
    from service.memory.facts import store as fact_store

    n_commit = assistant_store._db.execute(
        "SELECT COUNT(*) AS n FROM commitments WHERE source=?", (SEED_SOURCE,)
    ).fetchone()["n"]
    n_facts = fact_store._db.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE text LIKE ?", (QA_TAG + "%",)
    ).fetchone()["n"]
    print(f"seeded commitments: {n_commit}")
    print(f"seeded facts:       {n_facts}")
    print(f"cache fixtures:     {'present' if FIXTURE_DIR.exists() and any(FIXTURE_DIR.iterdir()) else 'not built'} ({FIXTURE_DIR})")


# ---------------------------------------------------------------------------
# Cache fixtures (Messages / Mail / Notes / Contacts) — repo-local files only
# ---------------------------------------------------------------------------

def _contacts() -> dict[str, str]:
    """handle(any format) -> (norm_handle computed the same way imessage_tools
    does, display name). Returned as the final {norm: name} the real cache
    file holds."""
    from service.tools.imessage_tools import _norm_handle
    raw = {
        "+1 (925) 555-1234": "Mom",
        "+1 (925) 555-5678": "Dad",
        "+1 (415) 555-9012": "Priya",
        "priya.test@example.com": "Priya",
        "+1 (512) 555-3456": "Jordan Ellis",
        "+1 (650) 555-7788": "Alex Chen",
        "+1 (650) 555-9911": "Alexis Nguyen",
        "+1 (408) 555-2222": "Dr. Patel",
    }
    return {_norm_handle(h): name for h, name in raw.items()}, raw


def _contact_handles(raw: dict[str, str]) -> dict[str, list[str]]:
    by_name: dict[str, list[str]] = {}
    for handle, name in raw.items():
        by_name.setdefault(name.strip().lower(), []).append(handle)
    return by_name


def build_messages() -> str:
    lines = [
        # Mom — 1:1, a question waiting on a reply, today.
        (_at(0, 9, 5), "Mom", "Mom: Are you free Saturday for Aunt Reeta's "
         "party? Should I save you a seat?"),
        (_at(0, 9, 30), "Mom", "Me: Should be! I'll confirm tonight."),
        # Yesterday, same thread.
        (_at(-1, 18, 0), "Mom", "Mom: Don't forget to bring the folding chairs"),
        # Dad — 1:1, casual, contains a detail worth a verbatim lookup test.
        (_at(0, 8, 10), "Dad", "Dad: Wifi password is Sunflower88 if you need it"),
        # Priya — 1:1, time-sensitive.
        (_at(0, 16, 45), "Priya", "Priya: My flight lands at 6pm, can you "
         "pick me up from SFO?"),
        # Alex Chen — 1:1, work, ties to the "Coffee with Alex" calendar item.
        (_at(0, 11, 0), "Alex Chen", "Alex Chen: Still on for coffee "
         "tomorrow? Want to run the deck by you first"),
        # Unsaved number — not in contacts, tests the unresolved-handle path.
        (_at(0, 13, 20), "+19998887777",
         "+19998887777: Hey it's Marcus from the conference, great meeting you!"),
        # Group chat #1 — "Group" prefix per the summarizer's own convention.
        (_at(0, 20, 0), "Group: Grad School GC",
         "Jordan Ellis: anyone free to do the reunion trip in October?"),
        (_at(0, 20, 5), "Group: Grad School GC", "Priya: I'm in!"),
        # Group chat #2 — family.
        (_at(0, 19, 0), "Group: Family", "Dad: dinner's at 7 tonight"),
        # Old message with a since-past relative claim — regression case for
        # the profile builder's date-stamping (a bug this project hit before).
        (_at(-35, 14, 0), "Mom", "Mom: England match tomorrow at 2pm, "
         "you watching?"),
    ]
    lines.sort(key=lambda r: r[0])
    return "\n".join(f"{ts} | {ctx} | {text}" for ts, ctx, text in lines)


def build_email_headers(deep_history: bool = False) -> str:
    rows = [
        (_at(0, 9, 0), "Personal", "Aurora Client <hello@auroraclient.test>",
         "Re: proposal sync Thursday?"),
        (_at(0, 10, 30), "Work", "Priya Shah <priya.shah@examplecorp.test>",
         "Notes from this morning"),
        (_at(-1, 8, 0), "Personal", "Order Confirmation <orders@shoptest.example>",
         "Your order #A19-88231 has shipped"),
        (_at(-1, 17, 0), "Work", "Finance <finance@examplecorp.test>",
         "Final reminder: submit your W-9 by Friday"),
        (_at(0, 7, 45), "Personal", "Daily Deals <deals@promo.test>",
         "50% off everything this weekend!"),
        (_at(-2, 12, 0), "Work", "Dana Kim <dana.kim@examplecorp.test>",
         "Quarterly planning review — thanks for joining"),
    ]
    if deep_history:
        rows.append((_at(-200, 9, 0), "Personal",
                     "Napa Vineyard Tours <info@napatours.test>",
                     "Your wedding weekend itinerary"))
    rows.sort(key=lambda r: r[0])
    return "\n".join(f"{ts} | {acct} | {sender} | {subj}" for ts, acct, sender, subj in rows)


def build_email_raw() -> str:
    FS, RS = "\x01", "\x02"
    rows = [
        (_at(0, 9, 0), "Personal", "Aurora Client <hello@auroraclient.test>",
         "testuser@example.com", "Re: proposal sync Thursday?",
         "Hi! Can we sync Thursday 2pm on the Aurora proposal? Want to "
         "confirm scope before we send the estimate."),
        (_at(-1, 8, 0), "Personal", "Order Confirmation <orders@shoptest.example>",
         "testuser@example.com", "Your order #A19-88231 has shipped",
         "Good news — order #A19-88231 shipped and should arrive Friday. "
         "Track it at shoptest.example/track/A19-88231."),
        (_at(-1, 17, 0), "Work", "Finance <finance@examplecorp.test>",
         "testuser.work@examplecorp.test", "Final reminder: submit your W-9 by Friday",
         "This is a final reminder that your W-9 is due by Friday EOD or "
         "your next payment will be delayed."),
        (_at(0, 7, 45), "Personal", "Daily Deals <deals@promo.test>",
         "testuser@example.com", "50% off everything this weekend!",
         "Everything's on sale this weekend only. Shop now before it's gone!"),
    ]
    return RS.join(FS.join(str(f) for f in row) for row in rows)


def build_notes() -> str:
    FS, RS = "\x01", "\x02"
    rows = [
        (_at(0, 8, 0), "Home WiFi", "Household",
         "Network: NestNet-5G / Password: Sunflower88"),
        (_at(-2, 21, 0), "Napa Trip Packing List", "Travel",
         "- Passport\n- Sunscreen\n- Hiking boots\n- Charger"),
        (_at(-5, 13, 0), "App idea — recipe box", "Ideas",
         "An app that turns a photo of a handwritten recipe card into a "
         "searchable digital recipe. MVP: OCR + tagging."),
        (_at(0, 6, 30), "Quick thought", "",
         "Ask Sam about the Q3 budget numbers before the offsite."),
        (_at(-60, 10, 0), "Gift ideas for Priya birthday (OLD)", "Personal",
         "Maybe the record player she mentioned? Or the trip to Big Sur."),
    ]
    return RS.join(FS.join(str(f) for f in row) for row in rows)


def build_cache() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    norm_contacts, raw_contacts = _contacts()
    files = {
        "messages.txt": build_messages(),
        "contacts.txt": json.dumps(norm_contacts, indent=2),
        "contact_handles.txt": json.dumps(_contact_handles(raw_contacts), indent=2),
        "email_headers.txt": build_email_headers(deep_history=False),
        "email_history.txt": build_email_headers(deep_history=True),
        "email_raw.txt": build_email_raw(),
        "identity_emails.txt": "testuser@example.com\ntestuser.work@examplecorp.test",
        "notes.txt": build_notes(),
    }
    for name, content in files.items():
        (FIXTURE_DIR / name).write_text(content, encoding="utf-8")
    print(f"✓ wrote {len(files)} cache fixture files to {FIXTURE_DIR}")
    print("  (these are repo-local — nothing under ~/.moe was touched; use "
          "scripts/install_cache_fixtures.sh to try them in a real run)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["seed-db", "clear-db", "build-cache", "status"])
    args = ap.parse_args()
    {"seed-db": seed_db, "clear-db": clear_db,
     "build-cache": build_cache, "status": status}[args.cmd]()


if __name__ == "__main__":
    main()
