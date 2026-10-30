"""A reminder with a date and no time is due all that day.

dev8 delta review guessed a date-only reminder due today was stored at local
midnight, so search_reminders' past_due (when_ts < now) would call it overdue
all day. It isn't: the store's civil index places a date-only reminder at the
end of its day (wisp_civil_index). This pins that, for past_due and upcoming.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from service.assistant.store import AssistantStore
from service.tools import assistant_tools


def test_a_date_only_reminder_due_today_is_upcoming_not_overdue(tmp_path, monkeypatch):
    store = AssistantStore(tmp_path / "assistant.db")
    now = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    today = now.replace(hour=0)
    yesterday = today - timedelta(days=1)
    store.sync_source("reminders", [
        {"source_id": "ek-today", "kind": "reminder", "title": "Pay rent",
         "when_ts": today.timestamp(), "all_day": True, "due_date": f"{today:%Y-%m-%d}"},
        {"source_id": "ek-yday", "kind": "reminder", "title": "Return books",
         "when_ts": yesterday.timestamp(), "all_day": True, "due_date": f"{yesterday:%Y-%m-%d}"},
        {"source_id": "ek-noon", "kind": "reminder", "title": "Call Sam",
         "when_ts": now.replace(hour=12).timestamp()},
        {"source_id": "ek-eve", "kind": "reminder", "title": "Water plants",
         "when_ts": now.replace(hour=19).timestamp()},
    ])
    monkeypatch.setattr(assistant_tools, "assistant_store", store)
    titles = lambda scope: sorted(r["title"] for r in assistant_tools.reminders_matching(
        scope, now=now.timestamp()))
    assert titles("past_due") == ["Call Sam", "Return books"]
    assert titles("upcoming") == ["Pay rent", "Water plants"]
