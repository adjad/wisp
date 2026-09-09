"""Launch-readiness and Daily Summary progress regression tests.

    .venv/bin/python tests/test_sync_readiness.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.assistant import scheduler  # noqa: E402
from service.assistant import sync_status as S  # noqa: E402
from service.tools import email_tools as E  # noqa: E402
from service.tools import imessage_tools as M  # noqa: E402

PASS = FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def main() -> int:
    original_scheduler = scheduler._sync_status.copy()
    original_mail = (E._headers_sync_generation, E._email_available,
                     E._email_sync_pending)
    original_messages = (M._sync_completed, M._available,
                         M._unavailable_reason, M._lines)
    try:
        print("\nrestored caches do not complete a current launch")
        scheduler._sync_status = {
            "calendar": {"available": False, "count": 0,
                         "reason": "waiting for the app to sync"},
            "reminders": {"available": False, "count": 0,
                          "reason": "waiting for the app to sync"},
        }
        E._headers_sync_generation = 0
        E._email_available = None
        E._email_sync_pending = False
        M._lines = "123 | Mom | Mom: old cached message"
        M._sync_completed = False
        M._available = False
        snap = S.summary_snapshot()
        check("all four sources begin in syncing", snap["completed"] == 0, str(snap))
        check("progress begins at zero", snap["progress"] == 0, str(snap))
        check("restored Messages rows remain syncing",
              M.messages_sync_state() == "syncing")
        check("Messages tells the user it is syncing",
              "still syncing your messages" in M._unavailable_message())

        print("\nprogress advances only on current-session terminal reports")
        scheduler.record_sync("calendar", 0, {"authorized": False})
        scheduler.record_sync("reminders", 2, {"authorized": True})
        E._headers_sync_generation = 1
        E._email_available = True
        M._sync_completed = True
        M._available = True
        snap = S.summary_snapshot()
        states = {row["id"]: row["state"] for row in snap["sources"]}
        check("denied Calendar is unavailable, not empty-ready",
              states["calendar"] == "unavailable", str(states))
        check("other completed readers are ready",
              all(states[k] == "ready" for k in ("reminders", "email", "messages")),
              str(states))
        check("unavailable is terminal so the bar can finish",
              snap["progress"] == 1 and not snap["syncing"], str(snap))

        print("\nthe deterministic notice names only unfinished sources")
        scheduler._sync_status["calendar"].pop("last_sync", None)
        E._headers_sync_generation = 0
        partial = S.summary_snapshot()
        notice = S.daily_syncing_message(partial)
        check("notice names Calendar and Email", "calendar and email" in notice, notice)
        check("notice does not falsely name finished Messages",
              "messages" not in notice.lower(), notice)
        check("notice promises an incomplete summary will be held back",
              "incomplete" in notice and "sync status" in notice, notice)
    finally:
        scheduler._sync_status = original_scheduler
        (E._headers_sync_generation, E._email_available,
         E._email_sync_pending) = original_mail
        (M._sync_completed, M._available,
         M._unavailable_reason, M._lines) = original_messages
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
