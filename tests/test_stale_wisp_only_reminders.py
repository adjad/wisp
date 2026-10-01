"""Long-overdue Wisp-only reminder records stop reading as current reminders."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

# Import the singleton only after redirecting its default database.
_SCRATCH = tempfile.TemporaryDirectory(prefix="wisp-stale-reminders-")
os.environ["HOME"] = _SCRATCH.name

from service.assistant import sync_status  # noqa: E402
from service.tools import assistant_tools  # noqa: E402

DAY = 86400
READY = {"sources": [{"id": "reminders", "state": "ready"}], "reminders_fresh": True}


def _row(title: str, age_days: float, source: str = "manual", dup=()) -> dict:
    return {"id": title, "source": source, "title": title,
            "when_ts": time.time() - age_days * DAY, "duplicate_sources": list(dup)}


def _search(rows: list[dict], readiness: dict = READY) -> str:
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=readiness), \
         patch.object(assistant_tools, "reminders_matching", return_value=rows):
        return asyncio.run(assistant_tools.search_reminders("x"))


def test_old_wisp_only_record_is_hidden_with_a_count() -> None:
    answer = _search([_row("synthetic recent", 2), _row("synthetic ancient", 40),
                      _row("synthetic ancient two", 90)])
    assert "synthetic recent" in answer
    assert "synthetic ancient" not in answer
    assert "2 older Wisp-only records overdue by more than 14 days are not listed" in answer


def test_apple_backed_and_future_rows_are_never_hidden() -> None:
    answer = _search([_row("synthetic apple", 400, source="reminders"),
                      _row("synthetic mirrored", 400, dup=("reminders",)),
                      _row("synthetic future", -3)])
    for title in ("synthetic apple", "synthetic mirrored", "synthetic future"):
        assert title in answer
    assert "not listed" not in answer


def test_only_stale_rows_still_explain_the_empty_result() -> None:
    answer = _search([_row("synthetic ancient", 60)])
    assert "synthetic ancient" not in answer
    assert "no active match" in answer
    assert "1 older Wisp-only record overdue by more than 14 days is not listed" in answer


def test_unavailable_reminders_path_also_hides_and_counts() -> None:
    answer = _search([_row("synthetic recent", 1), _row("synthetic ancient", 50)],
                     {"sources": [{"id": "reminders", "state": "unavailable"}]})
    assert "synthetic recent" in answer and "synthetic ancient" not in answer
    assert "not listed" in answer

