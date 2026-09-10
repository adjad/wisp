"""Cross-source duplicate commitments — regression tests.

`add_reminder` writes a source='manual' row AND mirrors the reminder into
Reminders.app, which RemindersWriter.sync() posts back as source='reminders'.
`sync_source` is scoped `WHERE source=?`, so it cannot see the manual twin: the
store legitimately ends up holding the same reminder twice. Every read path
therefore collapses duplicates by (normalized title, when_ts to the minute).

What must keep holding:
  * a Wisp-created reminder is listed once, and notifies once;
  * a reminder created directly in Reminders.app (no manual twin) still shows;
  * two genuinely distinct commitments sharing a title survive independently;
  * collapsing never resurrects an already-sent notification, and never
    swallows a genuine one.

    .venv/bin/python tests/test_assistant_dedupe.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Redirect HOME before importing the store: DB_PATH is `Path.home()/".moe"` and
# the module builds a singleton against it at import time. Without this the
# test would open (and migrate) the user's real assistant.db.
SCRATCH = tempfile.mkdtemp(prefix="wisp-dedupe-test-")
os.environ["HOME"] = SCRATCH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.assistant.reminders import due_reminders  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.tools import assistant_tools  # noqa: E402

PASS, FAIL = 0, 0
_n = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def fresh_store() -> AssistantStore:
    global _n
    _n += 1
    return AssistantStore(Path(SCRATCH) / f"assistant{_n}.db")


def mirror_back(store: AssistantStore, *items: dict) -> None:
    """What RemindersWriter.sync() posts: everything currently in Reminders.app,
    as a full replace-set for source='reminders'. EKReminder due dates carry
    only y/m/d/h/m components, so seconds are truncated on the way out."""
    store.sync_source("reminders", [
        {"source_id": it["source_id"], "kind": "reminder", "title": it["title"],
         "context": "Reminders", "when_ts": float(int(it["when_ts"] // 60) * 60)}
        for it in items])


def raw_count(store: AssistantStore, title: str) -> int:
    return store._db.execute(
        "SELECT COUNT(*) c FROM commitments WHERE title=?", (title,)).fetchone()["c"]


# --------------------------------------------------------------------------
def test_wisp_created_reminder_appears_once() -> None:
    print("\ncollapse: a reminder Wisp created and mirrored into Reminders.app")
    store = fresh_store()
    now = time.time()
    when = now + 3600
    c = store.add_manual("Call mom", when)
    mirror_back(store, {"source_id": "ek-call-mom", "title": "Call mom", "when_ts": when})

    check("both rows really are in the table", raw_count(store, "Call mom") == 2,
          f"raw={raw_count(store, 'Call mom')}")

    up = store.upcoming(now=now)
    check("upcoming lists it once", len(up) == 1, f"got {len(up)}")
    check("the manual row is the survivor", up and up[0]["id"] == c["id"],
          str(up and up[0]["source"]))
    check("survivor names its shadowed twin",
          up and up[0].get("duplicate_ids") and up[0]["duplicate_sources"] == ["reminders"],
          str(up and up[0].get("duplicate_sources")))
    check("active_future lists it once", len(store.active_future(now=now)) == 1)
    check("next_active returns the survivor",
          (store.next_active(now=now) or {}).get("id") == c["id"])

    # The survivor must not flip between reads, or notify_log (keyed on the
    # commitment id) would be written under a different id each time.
    ids = {store.upcoming(now=now)[0]["id"] for _ in range(5)}
    mirror_back(store, {"source_id": "ek-call-mom", "title": "Call mom", "when_ts": when})
    ids.add(store.upcoming(now=now)[0]["id"])
    check("survivor is stable across reads and re-syncs", ids == {c["id"]}, str(ids))


def test_reminders_app_original_still_appears() -> None:
    print("\npassthrough: a reminder created directly in Reminders.app")
    store = fresh_store()
    now = time.time()
    mirror_back(store,
                {"source_id": "ek-plants", "title": "Water the plants", "when_ts": now + 7200})

    up = store.upcoming(now=now)
    check("it is listed", len(up) == 1, f"got {len(up)}")
    check("it keeps its own source", up and up[0]["source"] == "reminders",
          str(up and up[0]["source"]))
    check("nothing was marked as a duplicate", up and "duplicate_ids" not in up[0])


def test_same_title_different_times_both_survive() -> None:
    print("\nno over-collapse: same title, genuinely different times")
    store = fresh_store()
    now = time.time()
    a = store.add_manual("Take medication", now + 3600)
    b = store.add_manual("Take medication", now + 3600 * 9)

    up = store.upcoming(now=now)
    check("both are listed", len(up) == 2, f"got {len(up)}")
    check("both ids survive", {r["id"] for r in up} == {a["id"], b["id"]})

    # ...and each still collapses against its own mirrored twin.
    mirror_back(store,
                {"source_id": "ek-med-1", "title": "Take medication", "when_ts": a["when_ts"]},
                {"source_id": "ek-med-2", "title": "Take medication", "when_ts": b["when_ts"]})
    check("raw table holds all four", raw_count(store, "Take medication") == 4)
    up = store.upcoming(now=now)
    check("still exactly two after mirroring", len(up) == 2, f"got {len(up)}")
    check("still the two manual rows", {r["id"] for r in up} == {a["id"], b["id"]})


def test_minute_truncation_and_title_normalization() -> None:
    print("\nmatching: EKReminder second-truncation and title whitespace/case")
    store = fresh_store()
    now = time.time()
    # A reminder set at 16:00:37 comes back from Reminders.app as 16:00:00.
    when = float(int((now + 3600) // 60) * 60) + 37
    store.add_manual("Pickup  the Kids ", when)
    mirror_back(store, {"source_id": "ek-kids", "title": "pickup the kids", "when_ts": when})
    check("truncated seconds + normalized title still collapse",
          len(store.upcoming(now=now)) == 1, str(store.upcoming(now=now)))

    # A minute apart is a different commitment, not a rounding artifact.
    store2 = fresh_store()
    store2.add_manual("Standup", now + 3600)
    store2.add_manual("Standup", now + 3660)
    check("one minute apart stays two", len(store2.upcoming(now=now)) == 2)


def test_notifies_once() -> None:
    print("\nnotifications: a duplicated reminder fires exactly one alert")
    store = fresh_store()
    now = time.time()
    when = now - 5                              # due, inside the 60s grace
    store.add_manual("Sleep at 12am tonight", when)
    mirror_back(store,
                {"source_id": "ek-sleep", "title": "Sleep at 12am tonight", "when_ts": when})

    fired = due_reminders(store, now=now)
    check("one notification, not two", len(fired) == 1, f"got {len(fired)}")
    check("nothing re-fires on the next tick", due_reminders(store, now=now + 1) == [])


def test_no_resurrection_after_survivor_is_deleted() -> None:
    print("\nnotifications: deleting the survivor must not re-fire under the twin")
    store = fresh_store()
    now = time.time()
    when = now - 5
    c = store.add_manual("Alarm", when)
    mirror_back(store, {"source_id": "ek-alarm", "title": "Alarm", "when_ts": when})

    fired = due_reminders(store, now=now)
    check("fired once", len(fired) == 1, f"got {len(fired)}")
    twin = store.duplicate_ids(c["id"])
    check("no stage is final before acknowledgement", not store.already_notified(c["id"], "due"))
    store.acknowledge_event(fired[0]["event_id"], "reminder")
    check("the twin was marked notified too",
          bool(twin) and store.already_notified(twin[0], "due"))

    # The survivor goes away (cancelled, or dropped by a sync) — its notify_log
    # goes with it and the twin stops being shadowed.
    store.delete(c["id"])
    check("the twin is now the visible row", len(store.upcoming(now=now)) == 1)
    check("but it does not notify again", due_reminders(store, now=now) == [])


def test_genuine_notification_not_suppressed() -> None:
    print("\nnotifications: a distinct reminder at the same time still fires")
    store = fresh_store()
    now = time.time()
    when = now - 5
    store.add_manual("Alarm", when)
    store.add_manual("Leave for the airport", when)
    fired = due_reminders(store, now=now)
    check("both distinct reminders notify", len(fired) == 2, f"got {len(fired)}")
    check("titles are the two distinct ones",
          {f["title"] for f in fired} == {"Alarm", "Leave for the airport"})

    # A later, genuinely separate reminder with the same title as an already
    # notified one must still get its own alert.
    store2 = fresh_store()
    store2.add_manual("Alarm", now - 5)
    later = store2.add_manual("Alarm", now + 3600)
    due_reminders(store2, now=now)
    fired = due_reminders(store2, now=now + 3600)
    check("the later same-title reminder fires on its own schedule",
          [f["commitment_id"] for f in fired] == [later["id"]], str(fired))


def test_cancel_retires_the_whole_group() -> None:
    print("\ncancel_event: cancelling a collapsed row retires its twin too")
    store = fresh_store()
    now = time.time()
    when = now + 3600
    store.add_manual("Dentist appointment", when)
    mirror_back(store,
                {"source_id": "ek-dentist", "title": "Dentist appointment", "when_ts": when})

    async def cancel() -> tuple[str, list[dict]]:
        from service.assistant.hub import hub
        q = hub.subscribe()                      # capture what the app is told to do
        try:
            msg = await assistant_tools.cancel_event("dentist")
        finally:
            hub.unsubscribe(q)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return msg, events

    real = assistant_tools.assistant_store
    assistant_tools.assistant_store = store
    try:
        msg, events = asyncio.run(cancel())
    finally:
        assistant_tools.assistant_store = real
    check("it cancelled rather than asking which one", msg.startswith("Cancelled"), msg)
    check("nothing is left upcoming", store.upcoming(now=now) == [],
          str(store.upcoming(now=now)))

    # The EKReminder keeps its own alarm, so cancelling has to reach the app.
    deletes = [e for e in events if e["type"] == "delete_apple_reminder"]
    check("the app is asked to delete the real reminder",
          [e["source_id"] for e in deletes] == ["ek-dentist"], str(events))

    # The Reminders row survives as 'dismissed', which sync_source preserves —
    # a plain delete would come straight back on the next 60s sync.
    mirror_back(store,
                {"source_id": "ek-dentist", "title": "Dentist appointment", "when_ts": when})
    check("and it stays gone after the next Reminders sync",
          store.upcoming(now=now) == [], str(store.upcoming(now=now)))


def test_seed_rows_never_shadow_real_ones() -> None:
    print("\nranking: QA seed rows lose to real rows")
    os.environ["WISP_QA_SEED"] = "1"
    try:
        store = fresh_store()
        now = time.time()
        when = now + 3600
        c = store.add_manual("Dentist Follow-up", when)
        store.sync_source("wisp_seed", [
            {"source_id": "seed-1", "kind": "event", "title": "Dentist Follow-up",
             "when_ts": when}])
        up = store.upcoming(now=now)
        check("collapsed to one", len(up) == 1, f"got {len(up)}")
        check("the real row survived", up and up[0]["id"] == c["id"],
              str(up and up[0]["source"]))
    finally:
        del os.environ["WISP_QA_SEED"]


def main() -> int:
    print(f"scratch: {SCRATCH}")
    test_wisp_created_reminder_appears_once()
    test_reminders_app_original_still_appears()
    test_same_title_different_times_both_survive()
    test_minute_truncation_and_title_normalization()
    test_notifies_once()
    test_no_resurrection_after_survivor_is_deleted()
    test_genuine_notification_not_suppressed()
    test_cancel_retires_the_whole_group()
    test_seed_rows_never_shadow_real_ones()
    print(f"\n{PASS} passed, {FAIL} failed")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
