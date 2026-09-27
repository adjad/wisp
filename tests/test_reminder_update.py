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
from unittest.mock import patch

SCRATCH = tempfile.mkdtemp(prefix="wisp-reminder-update-")
os.environ["WISP_HOME"] = SCRATCH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.assistant.hub import Hub  # noqa: E402
from service.assistant import outbox  # noqa: E402
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
    check("date alone uses the standard morning time and only offers the reminder write",
          set(tools) == {"add_reminder"}
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


def test_latest_manual_reminder_fails_closed_without_native_identity() -> None:
    print("\ntool: manual-only reminder has no exact native identity")
    store = fresh_store()
    now = datetime.now()
    older = store.add_manual("Older reminder", (now + timedelta(days=2)).timestamp())
    time.sleep(0.002)
    latest = store.add_manual("Send vaccine report to UCSC",
                              (now + timedelta(days=1, hours=1)).timestamp())
    target = now + timedelta(days=3)

    real = assistant_tools.assistant_store
    assistant_tools.assistant_store = store
    try:
        result = asyncio.run(assistant_tools.update_reminder(
            when_iso=target.strftime("%Y-%m-%dT%H:%M")))
    finally:
        assistant_tools.assistant_store = real

    moved = store.get(latest["id"])
    untouched = store.get(older["id"])
    check("reports missing exact native identity", "exact Reminders identity is unavailable" in result, result)
    check("does not move a manual-only reminder",
          moved is not None and moved["when_ts"] == latest["when_ts"],
          str(moved))
    check("leaves older reminders alone",
          untouched is not None and untouched["when_ts"] == older["when_ts"],
          str(untouched))
    check("does not claim a native update by title fallback",
          store._db.execute("SELECT COUNT(*) FROM assistant_events WHERE kind='update_reminder'").fetchone()[0] == 0)


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
        fixture_hub = Hub(store)
        queue = fixture_hub.subscribe()
        try:
            with patch.object(outbox, "hub", fixture_hub):
                pending = asyncio.create_task(assistant_tools.update_reminder(
                    title="dentist", when_iso=target.strftime("%Y-%m-%dT%H:%M")))
                event = await asyncio.wait_for(queue.get(), 1)
                row = store.event_by_key("action:" + event["action_id"])
                assert row is not None
                claim = store.claim_calendar_action(row["id"], event["type"],
                                                    event["action_id"], row["payload"])
                assert claim["execute"] is True
                receipt = {"ok": True, "status": "succeeded", "error": "",
                           "source_id": "ek-dentist", "title": "Call dentist",
                           "due_ts": event["due_ts"]}
                store.complete_calendar_action(row["id"], event["type"],
                                               claim["claim_token"], receipt)
                assert outbox.complete(event["action_id"], receipt)
                return await pending, [event]
        finally:
            fixture_hub.unsubscribe(queue)

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
    updates = [event for event in events if event["type"] == "update_reminder"]
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
        test_latest_manual_reminder_fails_closed_without_native_identity()
        test_mirrored_group_moves_together()
        print(f"\n{PASS} passed, {FAIL} failed")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
