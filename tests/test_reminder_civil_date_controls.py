"""Civil-date reminder controls using isolated stores and synthetic data only."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from service.assistant.reminders import due_reminders
from service.assistant.store import AssistantStore
from service.assistant.today import day_bounds
from service.assistant.verified_reminders import (
    due_timestamp,
    expected_date_fields,
    expected_matches,
    validate_payload,
)


DATE = "2030-04-15"


@pytest.fixture
def isolated_store(tmp_path: Path):
    store = AssistantStore(tmp_path / "civil-date.sqlite")
    try:
        yield store
    finally:
        store._db.close()


def _set_zone(monkeypatch, zone: str) -> None:
    monkeypatch.setenv("TZ", zone)
    time.tzset()


@pytest.fixture
def restore_zone(monkeypatch):
    previous = os.environ.get("TZ")
    try:
        yield lambda zone: _set_zone(monkeypatch, zone)
    finally:
        if previous is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous)
        time.tzset()


def _day_noon(day: str, zone: str) -> float:
    return datetime.fromisoformat(day).replace(hour=12, tzinfo=ZoneInfo(zone)).timestamp()


def test_manual_floating_date_survives_la_to_kiritimati_projection_and_day_selection(
        isolated_store, restore_zone):
    store = isolated_store
    restore_zone("America/Los_Angeles")
    index = due_timestamp({"all_day": True, "due_date": DATE})
    row = store.add_manual("Review report", index, all_day=True, due_date=DATE)
    raw_before = store._db.execute(
        "SELECT when_ts,due_date,all_day FROM commitments WHERE id=?", (row["id"],)
    ).fetchone()
    assert raw_before["due_date"] == DATE and raw_before["all_day"] == 1

    la_start, la_end = day_bounds(DATE, "America/Los_Angeles")
    la_row = store.get(row["id"])
    assert la_row["due_date"] == DATE
    assert datetime.fromtimestamp(la_row["when_ts"], ZoneInfo("America/Los_Angeles")).date().isoformat() == DATE
    assert [item["id"] for item in store.active_between(la_start, la_end, include_stale=True)] == [row["id"]]
    la_day_rows = store.today_snapshot(DATE, "America/Los_Angeles")["commitments"]
    assert row["id"] in {item["id"] for item in la_day_rows}
    assert next(item for item in la_day_rows if item["id"] == row["id"])["due_date"] == DATE

    restore_zone("Pacific/Kiritimati")
    ki_start, ki_end = day_bounds(DATE, "Pacific/Kiritimati")
    ki_row = store.get(row["id"])
    assert ki_row["due_date"] == DATE
    assert datetime.fromtimestamp(ki_row["when_ts"], ZoneInfo("Pacific/Kiritimati")).date().isoformat() == DATE
    assert [item["id"] for item in store.active_between(ki_start, ki_end, include_stale=True)] == [row["id"]]
    ki_day_rows = store.today_snapshot(DATE, "Pacific/Kiritimati")["commitments"]
    assert row["id"] in {item["id"] for item in ki_day_rows}
    assert next(item for item in ki_day_rows if item["id"] == row["id"])["due_date"] == DATE
    raw_after = store._db.execute(
        "SELECT when_ts,due_date,all_day FROM commitments WHERE id=?", (row["id"],)
    ).fetchone()
    assert dict(raw_after) == dict(raw_before), "the scheduled projection must not rewrite persisted rows"
    assert due_reminders(store, now=ki_row["when_ts"]) == []


def test_reminder_sync_keeps_id_when_local_anchor_shifts_but_civil_date_is_same(
        isolated_store, restore_zone):
    store = isolated_store
    item = {"source_id": "synthetic-ek-id", "kind": "reminder", "title": "Review report",
            "all_day": True, "due_date": DATE}
    restore_zone("America/Los_Angeles")
    item["when_ts"] = due_timestamp({"all_day": True, "due_date": DATE})
    store.sync_source("reminders", [item])
    first = store._db.execute(
        "SELECT id,when_ts,due_date FROM commitments WHERE source='reminders' AND source_id=?",
        (item["source_id"],),
    ).fetchone()
    assert first is not None and first["due_date"] == DATE

    restore_zone("Pacific/Kiritimati")
    changed_anchor = due_timestamp({"all_day": True, "due_date": DATE})
    assert changed_anchor != first["when_ts"]
    item["when_ts"] = changed_anchor
    store.sync_source("reminders", [item])
    after = store._db.execute(
        "SELECT id,when_ts,due_date FROM commitments WHERE source='reminders' AND source_id=?",
        (item["source_id"],),
    ).fetchall()
    assert len(after) == 1
    assert after[0]["id"] == first["id"]
    assert after[0]["due_date"] == DATE


def test_legacy_commitment_schema_adds_nullable_due_date_without_changing_timed_row(tmp_path):
    path = tmp_path / "legacy.sqlite"
    old_when = 2_000_000_000.0
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE commitments (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT,
            kind TEXT NOT NULL, title TEXT NOT NULL, context TEXT,
            when_ts REAL, all_day INTEGER DEFAULT 0, location TEXT, url TEXT,
            status TEXT DEFAULT 'active', confidence REAL DEFAULT 1.0,
            created_at REAL, updated_at REAL, UNIQUE(source,source_id)
        )""")
        db.execute("INSERT INTO commitments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("legacy-timed", "reminders", "legacy-source", "reminder", "Take medicine",
                    None, old_when, 0, None, None, "active", 1.0, old_when, old_when))

    store = AssistantStore(path)
    try:
        columns = {row["name"] for row in store._db.execute("PRAGMA table_info(commitments)")}
        assert "due_date" in columns
        row = store.get("legacy-timed")
        assert row["when_ts"] == old_when
        assert row["all_day"] == 0
        assert row["due_date"] is None
    finally:
        store._db.close()


def test_expected_civil_date_binding_rejects_invalid_mode_and_keeps_timed_contract():
    all_day = {"all_day": True, "due_date": DATE,
               "when_ts": due_timestamp({"all_day": True, "due_date": DATE})}
    expected = expected_date_fields(all_day)
    assert expected == {"expected_due_date": DATE}
    target = {"type": "delete_reminder", "action_id": "delete-day", "source_id": "synthetic-ek-id",
              "expected_title": "Review report", "expected_due_ts": all_day["when_ts"], **expected}
    assert validate_payload("delete_reminder", target) == target
    assert expected_matches(all_day, target)

    # A timed reminder at the same displayed 23:59 anchor is a different target.
    timed_collision = {"all_day": False, "due_date": None, "when_ts": all_day["when_ts"]}
    assert not expected_matches(timed_collision, target)
    assert not expected_matches(all_day, {**target, "expected_due_date": "2030-04-16"})
    with pytest.raises(ValueError):
        expected_date_fields({**all_day, "due_date": "2030-02-30"})
    with pytest.raises(ValueError):
        validate_payload("delete_reminder", {**target, "expected_due_date": "2030-02-30"})

    # Existing timed mutation payloads carry only their previous timestamp target.
    timed_target = {"type": "complete_reminder", "action_id": "complete-time", "source_id": "timed-id",
                    "expected_title": "Take medicine", "expected_due_ts": 2_000_000_000.0}
    assert validate_payload("complete_reminder", timed_target) == timed_target
    assert expected_date_fields(timed_collision) == {}
