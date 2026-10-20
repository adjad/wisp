"""Reminder epochs and fallback semantics with process-isolated local zones.

Each case runs the real registered tool in a child with a synthetic WISP_HOME,
fixed clock, fake store/hub/outbox, and denied network/native execution. No TZ
mutation or production-module import occurs in the collecting test process.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


CASES = {
    "normal_native": {"when": "2026-11-02T09:15"},
    "before_gap": {"when": "2027-03-14T01:30"},
    "after_gap": {"when": "2027-03-14T03:30"},
    "before_fold": {"when": "2026-11-01T00:30"},
    "after_fold": {"when": "2026-11-01T02:30"},
    "gap_native": {"when": "2027-03-14T02:30", "invalid": True},
    "fold_native": {"when": "2026-11-01T01:30", "invalid": True},
    "gap_disconnected": {"when": "2027-03-14T02:30", "invalid": True,
                         "mode": "disconnected"},
    "fold_disconnected": {"when": "2026-11-01T01:30", "invalid": True,
                          "mode": "disconnected"},
    "ny_gap": {"when": "2027-03-14T02:30", "invalid": True,
               "zone": "America/New_York"},
    "ny_fold": {"when": "2026-11-01T01:30", "invalid": True,
                "zone": "America/New_York"},
    "half_hour_gap": {"when": "2026-10-04T02:15", "invalid": True,
                      "zone": "Australia/Lord_Howe"},
    "half_hour_fold": {"when": "2027-04-04T01:45", "invalid": True,
                       "zone": "Australia/Lord_Howe"},
    "fold_offset_first": {"when": "2026-11-01T01:30-07:00", "label": "PDT-0700"},
    "fold_offset_second": {"when": "2026-11-01T01:30-08:00", "label": "PST-0800"},
    "offset_gap_clock": {"when": "2027-03-14T02:30-08:00",
                         "label": "3:30 AM PDT-0700"},
    "utc": {"when": "2026-11-02T17:15Z", "label": "9:15 AM PST-0800"},
    "nonlocal_offset": {"when": "2026-11-02T09:15+05:30",
                        "label": "Nov 1 at 7:45 PM PST-0800"},
    "date_only": {"when": "2026-11-02", "invalid": True},
    "space_seconds": {"when": "2026-11-02 09:15:42.125"},
    "offset_seconds": {"when": "2026-11-02T09:15:42.125-08:00"},
    "bad_iso": {"when": "tomorrow at nine", "invalid": True},
    "bad_date": {"when": "2026-02-30T09:15", "invalid": True},
    "bad_type": {"when": None, "invalid": True},
    "past": {"when": "2026-10-10T04:58:59", "past": True},
    "past_tolerance": {"when": "2026-10-10T04:59:00"},
    "disconnected": {"when": "2026-11-02T09:15", "mode": "disconnected"},
    "disconnected_blocked": {"when": "2026-11-02T09:15", "mode": "blocked"},
    "native_failed": {"when": "2026-11-02T09:15", "mode": "failed"},
    "native_unknown": {"when": "2026-11-02T09:15", "mode": "unknown"},
    "native_unverified": {"when": "2026-11-02T09:15", "mode": "unverified"},
    "offset_disconnected": {"when": "2026-11-01T01:30-08:00", "mode": "disconnected",
                            "label": "1:30 AM PST-0800"},
    "offset_failed": {"when": "2026-11-01T01:30-07:00", "mode": "failed",
                      "label": "1:30 AM PDT-0700"},
    "timestamp_overflow": {"when": "2026-11-02T09:15Z", "conversion": "overflow",
                           "invalid": True},
    "timestamp_oserror": {"when": "2026-11-02T09:15Z", "conversion": "oserror",
                          "invalid": True},
    "timestamp_nonfinite": {"when": "2026-11-02T09:15Z", "conversion": "infinite",
                            "invalid": True},
    "local_display_overflow": {"when": "2026-11-02T09:15Z", "conversion": "display",
                              "invalid": True},
}


@pytest.mark.parametrize("case", CASES)
def test_registered_reminder_time_in_isolated_process(case):
    with tempfile.TemporaryDirectory(prefix="wisp-reminder-time-") as home:
        env = {**os.environ, "WISP_HOME": home,
               "TZ": CASES[case].get("zone", "America/Los_Angeles"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--isolated-case", case],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=30,
        )
    assert result.returncode == 0, result.stdout + result.stderr


async def _check_case(case):
    import asyncio
    from contextlib import ExitStack
    from datetime import datetime, timezone
    import importlib
    import socket
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch
    from zoneinfo import ZoneInfo

    time.tzset()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from service.assistant import outbox
    hub_module = importlib.import_module("service.assistant.hub")
    from service.tools import assistant_tools as tools
    from service.tools.registry import get_tool, run_tool, classify_tool_outcome

    spec = CASES[case]
    mode = spec.get("mode", "native")
    native_result = {
        "native": {"ok": True}, "failed": {"ok": False, "status": "failed", "error": "fixture denied"},
        "unknown": {"ok": False, "status": "unknown", "error": "fixture unverified"},
        "unverified": {"ok": False},
    }.get(mode, {"ok": True})
    native = AsyncMock(return_value=native_result)
    fallback_allowed = Mock(return_value=mode != "blocked")
    add_manual = Mock(side_effect=lambda title, ts, *, kind: {"title": title})
    publish = AsyncMock()
    hub = SimpleNamespace(has_subscribers=mode not in {"disconnected", "blocked"}, publish=publish)
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp()
    with ExitStack() as patches:
        patches.enter_context(patch.object(tools, "assistant_store", SimpleNamespace(add_manual=add_manual)))
        patches.enter_context(patch.object(hub_module, "hub", hub))
        patches.enter_context(patch.object(outbox, "request", native))
        patches.enter_context(patch.object(outbox, "reminder_create_fallback_allowed", fallback_allowed))
        patches.enter_context(patch.object(tools.time, "time", return_value=now))
        patches.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        patches.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("native execution forbidden")))
        patches.enter_context(patch.object(asyncio, "create_subprocess_exec", side_effect=AssertionError("native execution forbidden")))
        patches.enter_context(patch.object(asyncio, "create_subprocess_shell", side_effect=AssertionError("native execution forbidden")))
        conversion = spec.get("conversion")
        if conversion:
            parser = patches.enter_context(patch.object(tools, "datetime", wraps=datetime))
            if conversion == "display":
                parser.fromtimestamp.side_effect = OverflowError("fixture local conversion")
            else:
                parsed = Mock(tzinfo=timezone.utc)
                if conversion == "infinite":
                    parsed.timestamp.return_value = float("inf")
                else:
                    parsed.timestamp.side_effect = (OverflowError if conversion == "overflow" else OSError)("fixture timestamp")
                parser.fromisoformat.return_value = parsed
        tool = get_tool("add_reminder")
        assert tool is not None and tool.func is tools.add_reminder
        result = await run_tool(tool, {"title": " Fixture reminder ", "when_iso": spec["when"], "kind": "exam"})
        if case == "bad_type":
            assert classify_tool_outcome("add_reminder", result).status == "failed", result
            # The registry rejects this type too; direct internal callers must
            # receive the same no-effect boundary instead of a TypeError.
            result = await tools.add_reminder("Fixture reminder", spec["when"], kind="exam")

    if spec.get("invalid") or spec.get("past"):
        assert classify_tool_outcome("add_reminder", result).status == "failed", result
        assert "bad when_iso" in result if spec.get("invalid") else "past" in result
        native.assert_not_awaited()
        fallback_allowed.assert_not_called()
        add_manual.assert_not_called()
        publish.assert_not_awaited()
        return

    when = datetime.fromisoformat(spec["when"])
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(os.environ["TZ"]))
    expected = {"title": "Fixture reminder", "due_ts": when.timestamp(), "commitment_kind": "exam"}
    if mode in {"disconnected", "blocked"}:
        native.assert_not_awaited()
        fallback_allowed.assert_called_once_with(expected)
    else:
        native.assert_awaited_once_with("create_reminder", expected)
        fallback_allowed.assert_not_called()

    if mode in {"blocked", "unknown", "unverified"}:
        assert classify_tool_outcome("add_reminder", result).status == "failed", result
        add_manual.assert_not_called()
        publish.assert_not_awaited()
        assert "Reminder set:" not in result
        return

    assert classify_tool_outcome("add_reminder", result).status == "succeeded", result
    if mode in {"disconnected", "failed"}:
        add_manual.assert_called_once_with("Fixture reminder", expected["due_ts"], kind="exam")
        assert "Wisp only" in result and "Apple Reminders was not changed" in result, result
    else:
        add_manual.assert_not_called()
        assert "in Apple Reminders and Wisp" in result, result
    publish.assert_awaited_once_with({"type": "changed"})
    if spec.get("label"):
        assert spec["label"] in result, result
    elif datetime.fromisoformat(spec["when"]).tzinfo is None:
        assert "PST" not in result and "PDT" not in result, result


if __name__ == "__main__":
    import asyncio
    assert len(sys.argv) == 3 and sys.argv[1] == "--isolated-case"
    asyncio.run(_check_case(sys.argv[2]))
