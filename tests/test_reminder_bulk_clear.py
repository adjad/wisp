#!/usr/bin/env python3
"""Regression coverage for wisp-debug-2026-08-24_18-22-45.json."""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.assistant.store import AssistantStore  # noqa: E402
from service.tools import assistant_tools  # noqa: E402

PASS = FAIL = 0
SCRATCH = Path(tempfile.mkdtemp(prefix="wisp-reminder-clear-"))


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def main() -> int:
    store = AssistantStore(SCRATCH / "assistant.db")
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).replace(hour=10, minute=0, second=0,
                                                        microsecond=0)
    today = now.replace(hour=18, minute=0, second=0, microsecond=0)
    tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0,
                                                  microsecond=0)
    store.add_manual("Old personal reminder", yesterday.timestamp())
    store.add_manual("Finish canvas assignment", today.timestamp(), kind="assignment")
    store.sync_source("reminders", [{
        "source_id": "ek-finish", "kind": "reminder",
        "title": "Finish canvas assignment", "when_ts": today.timestamp(),
    }])
    store.add_manual("Call dentist", tomorrow.timestamp())
    store.sync_source("calendar", [{
        "source_id": "cal-lunch", "kind": "meeting", "title": "Lunch with Alex",
        "when_ts": today.timestamp(),
    }])

    real = assistant_tools.assistant_store
    assistant_tools.assistant_store = store
    try:
        today_rows = assistant_tools.reminders_matching("today", now=now.timestamp())
        check("today returns one collapsed reminder",
              [r["title"] for r in today_rows] == ["Finish canvas assignment"],
              str(today_rows))
        check("calendar meeting is outside reminder scope",
              all(r["title"] != "Lunch with Alex" for r in today_rows))
        all_rows = assistant_tools.reminders_matching("all", now=now.timestamp())
        check("all includes past, today, and future reminders",
              {r["title"] for r in all_rows} == {
                  "Old personal reminder", "Finish canvas assignment", "Call dentist"},
              str(all_rows))

        result = asyncio.run(assistant_tools.clear_reminders("today"))
        check("today clear reports its exact scope",
              result == "Cleared 1 reminder(s) for today.", result)
        remaining = store.active_between()
        check("today reminder and mirrored twin are retired",
              all(r["title"] != "Finish canvas assignment" for r in remaining),
              str(remaining))
        check("other reminders and calendar meeting remain",
              {r["title"] for r in remaining} == {
                  "Old personal reminder", "Call dentist", "Lunch with Alex"},
              str(remaining))
    finally:
        assistant_tools.assistant_store = real
        shutil.rmtree(SCRATCH, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
