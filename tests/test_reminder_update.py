"""Reminder correction/reschedule regression coverage.

The reported flow was:
  create tomorrow -> "I mean today" -> Wisp claimed success without a write.

These tests cover both layers of the fix: router-direct correction dispatch and
the real store/EventKit update request, including a mirrored duplicate group.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRATCH = tempfile.mkdtemp(prefix="wisp-reminder-update-")
os.environ["WISP_HOME"] = SCRATCH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.assistant.hub import hub  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.router.router import route  # noqa: E402
from service.tools import assistant_tools  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def fresh_store() -> AssistantStore:
    return AssistantStore(Path(SCRATCH) / f"assistant-{time.time_ns()}.db")


def test_immediate_correction_routes_directly() -> None:
    print("\nrouter: immediate correction")
    decision = asyncio.run(route(
        "I mean today sorry",
        last_assistant="Reminder set for tomorrow.",
        last_tools="add_reminder"))
    check("dispatches the update without model tool selection",
          decision.direct_calls == [("update_reminder", {"day": "today"})],
          str(decision.direct_calls))
    check("only exposes the update operation",
          decision.tool_subset == ["update_reminder"], str(decision.tool_subset))
    check("does not force a second selection step",
          decision.required_tool_groups == (), str(decision.required_tool_groups))

    unrelated = asyncio.run(route(
        "I mean today sorry", last_assistant="What period?", last_tools="get_upcoming"))
    check("does not hijack an unrelated date fragment",
          not unrelated.direct_calls, str(unrelated.direct_calls))


def test_reminder_task_is_not_misread_as_an_immediate_send() -> None:
    print("\nrouter: reminder content stays reminder content")
    decision = asyncio.run(route(
        "create a reminder tommorow to send my vaccine report to UCSC"))
    tools = decision.tool_subset or []
    check("date alone uses the standard morning time and creates the reminder",
          set(tools) == {"add_reminder", "get_upcoming"}
          and decision.reminder_action == "create"
          and decision.tool_argument_bindings.get("add_reminder", {}).get(
              "when_iso", "").endswith("09:00"),
          str(tools))
    check("the misspelled date still requests a current schedule lookup",
          decision.expect_tool_first is True, decision.reason)
    check("does not ask which communication channel to use",
          decision.clarify_channel is False, decision.reason)
    check("does not expose immediate email/text actions",
          not ({"send_email", "send_message", "draft_email", "draft_message"}
               & set(tools)), str(tools))


def test_latest_manual_reminder_is_moved() -> None:
    print("\ntool: update latest reminder")
    store = fresh_store()
    now = datetime.now()
    older = store.add_manual("Older reminder", (now + timedelta(days=2)).timestamp())
    time.sleep(0.002)
    latest = store.add_manual("Send vaccine report to UCSC",
                              (now + timedelta(days=1, hours=1)).timestamp())
    target = now + timedelta(days=3)

    async def run() -> tuple[str, list[dict]]:
        queue = hub.subscribe()
        try:
            result = await assistant_tools.update_reminder(
                when_iso=target.strftime("%Y-%m-%dT%H:%M"))
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            return result, events
        finally:
            hub.unsubscribe(queue)

    real = assistant_tools.assistant_store
    assistant_tools.assistant_store = store
    try:
        result, events = asyncio.run(run())
    finally:
        assistant_tools.assistant_store = real

    moved = store.get(latest["id"])
    untouched = store.get(older["id"])
    check("reports a real update", result.startswith("Reminder updated:"), result)
    check("moves the most recently created reminder",
          moved is not None and int(moved["when_ts"] // 60) == int(target.timestamp() // 60),
          str(moved))
    check("leaves older reminders alone",
          untouched is not None and untouched["when_ts"] == older["when_ts"],
          str(untouched))
    updates = [event for event in events if event["type"] == "update_apple_reminder"]
    check("asks the app to update the pre-sync Apple reminder by fallback identity",
          len(updates) == 1 and updates[0]["source_id"] == ""
          and updates[0]["old_title"] == "Send vaccine report to UCSC",
          str(events))


def test_mirrored_group_moves_together() -> None:
    print("\ntool: update mirrored Wisp + Apple group")
    store = fresh_store()
    now = time.time()
    old_when = now + 7200
    manual = store.add_manual("Call dentist", old_when)
    store.sync_source("reminders", [{
        "source_id": "ek-dentist", "kind": "reminder", "title": "Call dentist",
        "context": "Reminders", "when_ts": float(int(old_when // 60) * 60),
    }])
    store.mark_notified(manual["id"], "due")
    target = datetime.now() + timedelta(days=4)

    async def run() -> tuple[str, list[dict]]:
        queue = hub.subscribe()
        try:
            result = await assistant_tools.update_reminder(
                title="dentist", when_iso=target.strftime("%Y-%m-%dT%H:%M"))
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            return result, events
        finally:
            hub.unsubscribe(queue)

    real = assistant_tools.assistant_store
    assistant_tools.assistant_store = store
    try:
        result, events = asyncio.run(run())
    finally:
        assistant_tools.assistant_store = real

    rows = store._db.execute(
        "SELECT source, source_id, when_ts FROM commitments WHERE title='Call dentist'"
    ).fetchall()
    check("update succeeds", result.startswith("Reminder updated:"), result)
    check("manual and mirrored rows move as one group",
          len(rows) == 2 and all(int(row["when_ts"] // 60)
                                 == int(target.timestamp() // 60) for row in rows),
          str([dict(row) for row in rows]))
    check("the collapsed reminder still appears once",
          len(store.upcoming(now=now, days=7)) == 1,
          str(store.upcoming(now=now, days=7)))
    updates = [event for event in events if event["type"] == "update_apple_reminder"]
    check("uses the stable EventKit identifier when available",
          len(updates) == 1 and updates[0]["source_id"] == "ek-dentist",
          str(events))
    check("the manual survivor identity remains stable",
          store.upcoming(now=now, days=7)[0]["id"] == manual["id"])
    remaining_notifications = store._db.execute(
        "SELECT COUNT(*) AS count FROM notify_log").fetchone()["count"]
    check("old notification stages are cleared for the new schedule",
          remaining_notifications == 0, str(remaining_notifications))


def main() -> int:
    try:
        test_immediate_correction_routes_directly()
        test_reminder_task_is_not_misread_as_an_immediate_send()
        test_latest_manual_reminder_is_moved()
        test_mirrored_group_moves_together()
        print(f"\n{PASS} passed, {FAIL} failed")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
