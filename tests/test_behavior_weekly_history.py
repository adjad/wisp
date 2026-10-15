"""Evidence, prior-schema migration, local-week arithmetic and import recovery.

Pure temporary SQLite and minimal HTTP; no user sources, model or native apps.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.behavior.engine import BehaviorEngine
from service.behavior.evidence import basis, completion_time
from service.behavior.history import EARLIEST, History
from service.behavior.store import Store
from service.behavior.timeutil import day_start
from service.behavior.types import Observation, Pattern, Window, Nudge

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 14, 12, tzinfo=TZ).timestamp()


@pytest.fixture
def engine(tmp_path):
    result = BehaviorEngine(tmp_path / "behavior.db", clock=lambda: NOW, tz=str(TZ))
    yield result
    result.close()


def batch(job, rows=(), complete=True):
    return {
        "scan_id": job["scan_id"],
        "start_ts": job["page_start"],
        "end_ts": job["page_end"],
        "rows": list(rows),
        "complete": complete,
    }


def test_calendar_and_legacy_completion_have_distinct_evidence():
    event = Observation(
        "e", "event", NOW - 100, attrs={"completed": True, "completion_ts": NOW - 50}
    )
    assert event.to_dict()["outcome"] == "unknown" and basis(event) == "scheduled"
    assert completion_time(event) is None
    done = Observation("r", "reminder_done", NOW - 100, attrs={"due_ts": NOW - 100})
    assert (
        done.to_dict()["outcome"] == "marked_complete" and completion_time(done) is None
    )
    assert (
        completion_time(
            Observation("r", "reminder_done", NOW, attrs={"completion_ts": True})
        )
        is None
    )


def test_v1_open_preserves_verdict_and_safely_normalizes_before_remine(tmp_path):
    path = tmp_path / "legacy.db"
    old = Store(path)
    original = Pattern(
        "gym",
        "habit",
        "fitness",
        "You usually work out on Mondays",
        confidence=0.9,
        stats={"key_type": "category", "key": "fitness"},
        status="confirmed",
    )
    old.replace_patterns([original], NOW)
    old.set_pattern_status("gym", "confirmed")
    old.meta_set("schema_version", 1)
    old.close()
    for _ in range(2):
        new = Store(path)
        p = new.get_pattern("gym")
        assert p.id == "gym" and p.status == "confirmed"
        assert p.stats["basis"] == "scheduled" and "completion unknown" in p.statement
        assert p.stats["original_statement"] == original.statement
        assert new.meta_get("schema_version") == 2
        new.close()
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("original", [
    "When something lands on your workout you move it (4 of 5 times)",
    "When something lands on your workout you move or cancel it (4 of 5 times)",
    "When something lands on your workout you usually keep it (4 of 5 times)",
])
@pytest.mark.parametrize("known_counts", [False, True])
def test_orphan_confirmed_protection_preserves_counts_without_inventing_agency(engine, original, known_counts):
    stats = {"collisions": 5, "moved": 1, "cancelled": 0, "kept": 4} if known_counts else {}
    p = Pattern("protection", "habit_protection", "fitness", original, stats=stats)
    engine._store.replace_patterns([p], NOW)
    engine._store.set_pattern_status(p.id, "confirmed")
    engine._store.replace_patterns([], NOW + 1)
    saved = engine._store.get_pattern(p.id)
    assert saved.status == "confirmed" and saved.stats["original_statement"] == original
    assert all(v not in saved.statement for v in ("you move", "you usually keep", "you cancel"))
    if known_counts:
        assert "1 moved, 0 removed, 4 remained scheduled out of 5" in saved.statement
    else:
        assert "counts unavailable" in saved.statement
    assert "attendance unknown" in saved.statement


@pytest.mark.parametrize("action,original", [
    ("move", "You move about 60% of afternoon meetings"),
    ("move", "When you have 3+ back-to-back meetings you move one about 60% of the time"),
    ("cancel", "You cancel about 60% of afternoon meetings"),
])
def test_legacy_schedule_changes_do_not_attribute_user_agency(engine, action, original):
    p = Pattern("change", "cancel_propensity", "afternoon", original,
                support=3, opportunities=5, stats={"action": action, "bucket": "afternoon"})
    engine._store.replace_patterns([p], NOW)
    engine._store.set_pattern_status(p.id, "confirmed")
    saved = engine._store.get_pattern(p.id)
    assert saved.id == p.id and saved.status == "confirmed"
    assert saved.stats["original_statement"] == original
    assert "3 of the last 5" in saved.statement and "attendance unknown" in saved.statement
    assert "you move" not in saved.statement.lower() and "you cancel" not in saved.statement.lower()


def test_completion_pattern_does_not_inherit_schedule_confirmation(engine):
    planned = Pattern(
        "planned", "habit", "fitness", "Gym on calendar", stats={"key_type": "category"}
    )
    engine._store.replace_patterns([planned], NOW)
    engine._store.set_pattern_status("planned", "confirmed")
    completed = Pattern(
        "completed",
        "habit",
        "fitness",
        "Recorded completions",
        stats={"evidence_version": 2, "basis": "recorded_completion"},
    )
    engine._store.replace_patterns([completed], NOW + 1)
    assert engine._store.get_pattern("completed").status == "active"
    assert engine._store.get_pattern("planned").status == "confirmed"


def test_failed_additive_migration_keeps_old_records_and_can_retry(
    tmp_path, monkeypatch
):
    path = tmp_path / "migration.db"
    old = Store(path)
    old.meta_set("schema_version", 1)
    old.upsert_observations([Observation("keep", "event", NOW - 100)])
    old.close()
    original = Store.meta_set

    def fail(self, key, value):
        if key == "schema_version" and value == 2:
            raise RuntimeError("interrupted migration")
        return original(self, key, value)

    with monkeypatch.context() as m:
        m.setattr(Store, "meta_set", fail)
        with pytest.raises(RuntimeError, match="interrupted migration"):
            Store(path)
    recovered = Store(path)
    assert (
        recovered.get_observation("keep") is not None
        and recovered.meta_get("schema_version") == 2
    )
    recovered.close()


def test_history_retry_restart_stale_page_and_partial_atomicity(engine):
    job = engine.history_next("calendar")
    row = {
        "source_id": "gym",
        "title": "Gym",
        "when_ts": NOW - 86400,
        "end_ts": NOW - 82800,
    }
    first = engine.history_ingest("calendar", batch(job, [row]))
    assert first["pages"] == 1 and first["rows"] == 1
    assert engine.history_ingest("calendar", batch(job, [row]))["pages"] == 1
    assert engine.status()["observations"]["event"] == 1
    following = engine.history_next("calendar")
    engine.history_control("calendar", "pause")
    with pytest.raises(ValueError, match="changed or stopped"):
        engine.history_ingest("calendar", batch(following, []))
    engine.history_control("calendar", "resume")
    resumed = engine.history_next("calendar")
    assert resumed["page_end"] == following["page_end"]
    engine.history_ingest("calendar", batch(resumed, [], False))
    assert engine.history_status()["calendar"]["state"] == "paused"
    assert engine.history_status()["calendar"]["pages"] == 1
    engine.history_control("calendar", "restart")
    with pytest.raises(ValueError):
        engine.history_ingest("calendar", batch(job, [row]))


def test_history_validates_whole_page_before_writes(engine):
    job = engine.history_next("calendar")
    valid = {
        "source_id": "x",
        "title": "Meeting",
        "when_ts": NOW - 1000,
        "end_ts": NOW - 100,
    }
    invalid = {**valid, "source_id": "bad", "when_ts": float("nan")}
    with pytest.raises(ValueError):
        engine.history_ingest("calendar", batch(job, [valid, invalid]))
    assert engine.status()["observation_total"] == 0
    assert engine.history_status()["calendar"]["cursor"] == NOW
    assert engine._store.coverage() == []


def test_history_transaction_rolls_back_rows_coverage_and_cursor(engine):
    import sqlite3

    job = engine.history_next("calendar")
    engine._store._conn.execute(
        "CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON meta "
        "WHEN NEW.key LIKE 'history:%' BEGIN SELECT RAISE(ABORT,'checkpoint failed'); END"
    )
    row = {
        "source_id": "x",
        "title": "Meeting",
        "when_ts": NOW - 1000,
        "end_ts": NOW - 100,
    }
    with pytest.raises(sqlite3.IntegrityError, match="checkpoint failed"):
        engine.history_ingest("calendar", batch(job, [row]))
    assert engine.status()["observation_total"] == 0 and engine._store.coverage() == []
    assert engine.history_status()["calendar"]["cursor"] == NOW


def test_history_row_limit_reduces_range_without_skipping(engine):
    job = engine.history_next("completed_reminders")
    limited = batch(job, [], False)
    limited["reason"] = "row_limit"
    engine.history_ingest("completed_reminders", limited)
    retry = engine.history_next("completed_reminders")
    assert retry["state"] == "scanning" and retry["page_end"] == job["page_end"]
    assert retry["page_start"] > job["page_start"] and retry["pages"] == 0
    assert engine._store.coverage() == []


def test_completed_final_page_retry_is_idempotent_and_content_bound(engine):
    job = engine.history_next("completed_reminders")
    payload = batch(
        job, [{"source_id": "x", "title": "Done", "completion_ts": NOW - 100}]
    )
    a = engine.history_ingest("completed_reminders", payload)
    assert engine.history_ingest("completed_reminders", payload) == a
    modified = batch(
        job, [{"source_id": "x", "title": "Different", "completion_ts": NOW - 100}]
    )
    with pytest.raises(ValueError):
        engine.history_ingest("completed_reminders", modified)


def test_legacy_nudge_is_safe_before_remining_and_keeps_identity():
    legacy = {
        "id": "old",
        "kind": "habit_missed",
        "title": "Missed gym",
        "body": "you skipped it",
        "created_at": NOW,
        "seq": 4,
        "actions": [{"type": "add_hold", "auto": True, "params": {}}],
    }
    n = Nudge.from_dict(legacy)
    assert n.id == "old" and n.seq == 4 and not n.actions and "unknown" in n.body
    assert Nudge.from_dict(n.to_dict()) == n


def test_legacy_contact_nudge_is_neutral_and_retains_undo_identity():
    old = {"id": "contact-old", "kind": "contact_window", "title": "Time to call Sam?",
           "body": "You usually call Sam now", "because": "Calendar calls", "seq": 12,
           "actions": [{"type": "open_prompt", "params": {"prompt": "Call Sam"}}]}
    n = Nudge.from_dict(old)
    assert n.id == old["id"] and n.seq == old["seq"] and not n.actions
    assert "unavailable" in n.because and "communication is unknown" in n.body
    assert "call Sam" not in n.title and Nudge.from_dict(n.to_dict()) == n
    current = Nudge.from_dict({**old, "evidence_version": 2})
    assert current.title == old["title"] and current.actions
    # The native cached-SSE boundary includes the same legacy kind guard.
    from pathlib import Path
    native = (Path(__file__).resolve().parents[1] / "app/Sources/WispApp/OverlayModel.swift").read_text()
    guard = native.split('if (payload["evidence_version"] as? Int) != 2,', 1)[1].split('payload["actions"]', 1)[0]
    assert '"contact_window"' in guard and '"Source details unavailable"' in guard


def test_history_survives_process_restart_and_never_diffs_deletions(tmp_path):
    path = tmp_path / "b.db"
    e = BehaviorEngine(path, clock=lambda: NOW, tz=str(TZ))
    job = e.history_next("calendar")
    row = {"source_id": "x", "title": "Gym", "when_ts": NOW - 1000, "end_ts": NOW - 100}
    e.history_ingest("calendar", batch(job, [row]))
    cursor = e.history_status()["calendar"]["cursor"]
    e.close()
    e = BehaviorEngine(path, clock=lambda: NOW, tz=str(TZ))
    assert e.history_next("calendar")["page_end"] == cursor
    assert e._store.get_snapshot("calendar") is None
    assert not e._store.observations(kinds=["event_cancelled"])
    e.close()


def test_completed_undated_history_uses_actual_completion_not_due_time(engine):
    job = engine.history_next("completed_reminders")
    stamp = day_start(datetime(2026, 10, 12).date(), TZ) + 3600
    row = {
        "source_id": "task",
        "title": "Undated task",
        "completion_ts": stamp,
        "due_ts": NOW + 10000,
    }
    engine.history_ingest("completed_reminders", batch(job, [row]))
    obs = engine._store.observations(kinds=["reminder_done"])
    assert len(obs) == 1 and completion_time(obs[0]) == stamp and obs[0].ts == stamp
    assert engine.history_status()["completed_reminders"]["state"] == "complete"
    report = engine.weekly()
    metric = next(m for m in report["metrics"] if m["id"] == "completed_reminders")
    assert metric["value"] == 1 and metric["coverage"] == 1
    # A rescan of the same source completion is idempotent.
    engine.history_control("completed_reminders", "restart")
    engine.history_ingest(
        "completed_reminders", batch(engine.history_next("completed_reminders"), [row])
    )
    assert (
        next(m for m in engine.weekly()["metrics"] if m["id"] == "completed_reminders")[
            "value"
        ]
        == 1
    )


def test_explicit_rescan_after_incremental_refresh_restores_full_job(tmp_path):
    path = tmp_path / "rescan.db"
    store = Store(path)
    history = History(store)
    row = {"source_id": "old-task", "title": "Done", "completion_ts": NOW - 3 * 86400}
    initial = history.next("completed_reminders", NOW)
    history.ingest("completed_reminders", batch(initial, [row]), NOW, lambda o: o)
    incremental = history.next("completed_reminders", NOW + 301)
    assert incremental["start_ts"] == NOW - 86400
    # Simulate reduced source pages as well as an incremental lower bound.
    job = store.meta_get("history:completed_reminders")
    job["page_seconds"] = 86400
    store.meta_set("history:completed_reminders", job)
    restarted = history.control("completed_reminders", "restart", NOW + 302)
    assert restarted["scan_id"] != incremental["scan_id"]
    assert restarted["start_ts"] == EARLIEST
    assert not {"mode", "page_seconds", "last_start", "last_end", "last_digest"} & restarted.keys()
    assert restarted["pages"] == restarted["rows"] == 0
    with pytest.raises(ValueError, match="scan changed"):
        history.ingest("completed_reminders", batch(incremental), NOW + 302, lambda o: o)
    store.close()
    store = Store(path)
    history = History(store)
    resumed = history.next("completed_reminders", NOW + 303)
    assert resumed["page_start"] == EARLIEST and resumed["page_end"] == NOW + 302
    history.ingest("completed_reminders", batch(resumed, [row]), NOW + 303, lambda o: o)
    assert len(store.observations(kinds=["reminder_done"])) == 1
    again = history.control("completed_reminders", "restart", NOW + 304)
    assert again["start_ts"] == EARLIEST and again["scan_id"] != resumed["scan_id"]
    assert len(store.observations(kinds=["reminder_done"])) == 1
    store.close()


def test_old_history_is_scanned_but_not_retained_or_revived(engine):
    job = engine.history_next("completed_reminders")
    engine.history_ingest(
        "completed_reminders",
        batch(
            job,
            [
                {
                    "source_id": "old",
                    "title": "Old habit",
                    "completion_ts": NOW - 400 * 86400,
                }
            ],
        ),
    )
    assert engine.history_status()["completed_reminders"]["rows"] == 1
    assert engine.status()["observation_total"] == 0 and not engine.patterns()


@pytest.mark.parametrize("day", ["2026-03-09", "2026-11-02"])
def test_week_boundaries_follow_completed_local_dates_across_dst(tmp_path, day):
    date = datetime.fromisoformat(day).date()
    now = datetime.combine(date, datetime.min.time(), TZ).timestamp() + 12 * 3600
    e = BehaviorEngine(tmp_path / "dst.db", clock=lambda: now, tz=str(TZ))
    report = e.weekly()
    assert report["start_date"] == (date - timedelta(days=7)).isoformat()
    assert report["end_date_exclusive"] == day
    hours = (day_start(date, TZ) - day_start(date - timedelta(days=7), TZ)) / 3600
    assert hours == (167 if "03" in day else 169)
    e.close()


def test_weekly_clips_overlaps_and_does_not_equate_schedule_to_completion(engine):
    today = datetime(2026, 10, 14).date()
    start = day_start(today - timedelta(days=7), TZ)
    engine.observe(
        [
            Observation(
                "a",
                "event",
                start - 3600,
                end_ts=start + 3600,
                subject="Meeting",
                category="meeting",
            ),
            Observation(
                "b",
                "event",
                start + 1800,
                end_ts=start + 5400,
                subject="Call",
                category="call",
            ),
            Observation(
                "legacy", "reminder_done", start + 100, attrs={"due_ts": start + 100}
            ),
            Observation(
                "future", "event", NOW + 86400, end_ts=NOW + 90000, category="meeting"
            ),
        ]
    )
    engine.record_coverage("calendar", start - 28 * 86400, NOW, "complete", NOW)
    r = engine.weekly()
    m = {m["id"]: m for m in r["metrics"]}
    assert (
        m["scheduled_meeting_hours"]["value"] == 1.5
    )  # occupied union, not double-counted bookings
    assert m["scheduled_meetings"]["value"] == 2
    assert m["completed_reminders"]["value"] == 0
    assert (
        m["scheduled_meetings"]["baseline_weekly_average"] == 0.25
    )  # the boundary event also overlaps the prior week
    assert m["scheduled_meeting_hours"]["baseline_weekly_average"] == 0.25


def test_unknown_coverage_never_generates_zero_activity_comparison(engine):
    report = engine.weekly()
    assert all(
        m["comparison"] == "insufficient_coverage" and m["change"] is None
        for m in report["metrics"]
    )
    assert "observed records only" in report["summary"]


@pytest.mark.parametrize("coverage,busy_hours,expected_gap", [
    ("unknown", 0, None), ("unknown", 1, None), ("partial", 1, None),
    ("full", 0, 45), ("full", 45, 0), ("current_only", 0, 45),
])
def test_unbooked_hours_require_complete_current_calendar_coverage(
    engine, coverage, busy_hours, expected_gap,
):
    first = datetime.fromtimestamp(NOW, TZ).date() - timedelta(days=7)
    start, end = day_start(first, TZ), day_start(first + timedelta(days=7), TZ)
    weekdays = [first + timedelta(days=n) for n in range(7)
                if (first + timedelta(days=n)).weekday() < 5]
    rows = [Observation(f"busy:{n}", "event", day_start(day, TZ) + 9*3600,
                        end_ts=day_start(day, TZ) + (18 if busy_hours == 45 else 10)*3600,
                        category="meeting")
            for n, day in enumerate(weekdays[:5 if busy_hours == 45 else 1])]
    if busy_hours:
        engine.observe(rows)
    if coverage != "unknown":
        a = start - 28*86400 if coverage == "full" else start
        b = start + 10*3600 if coverage == "partial" else end
        engine.record_coverage("calendar", a, b, "complete", NOW)
    report = engine.weekly()
    metrics = {m["id"]: m for m in report["metrics"]}
    gap = metrics["calendar_gap_hours"]
    assert gap["value"] == expected_gap
    assert metrics["scheduled_meeting_hours"]["value"] == busy_hours
    line = next(line for line in report["summary"].splitlines()
                if "Unbooked weekday hours" in line)
    if expected_gap is None:
        assert gap["value_reason"] == "incomplete_calendar_coverage"
        assert "unknown" in line and "coverage" in line
        assert gap["change"] is None and gap["baseline_weekly_average"] is None
    else:
        assert gap["value_reason"] is None
        assert f": {expected_gap:g} hours" in line
        assert gap["comparison"] == ("available" if coverage == "full" else "insufficient_coverage")


def test_unknown_end_times_do_not_turn_into_free_or_focused_hours(engine):
    engine.observe(
        [
            Observation(
                "no-end", "event", NOW - 86400, subject="Meeting", category="meeting"
            )
        ]
    )
    engine.record_coverage("calendar", NOW - 50 * 86400, NOW, "complete", NOW)
    report = engine.weekly()
    for metric in report["metrics"]:
        if metric["id"] in {
            "calendar_gap_hours",
            "scheduled_meeting_hours",
            "scheduled_focus_hours",
            "adjacent_meeting_pairs",
        }:
            assert metric["value"] is None and metric["change"] is None
    assert "end times are missing" in report["summary"]


def test_weekly_conversation_uses_the_same_grounded_report(monkeypatch, engine):
    from service.tools import behavior_tools

    class Runtime:
        def call(self, method):
            assert method == "weekly"
            return engine.weekly()

    monkeypatch.setattr(behavior_tools, "_runtime", lambda: Runtime())
    assert behavior_tools.my_patterns("weekly trends") == engine.weekly()["summary"]


def test_coverage_merges_overlap_but_preserves_gaps(engine):
    engine.record_coverage("calendar", NOW - 100, NOW - 50, "complete", NOW)
    engine.record_coverage("calendar", NOW - 60, NOW - 10, "complete", NOW + 1)
    engine.record_coverage("calendar", NOW, NOW + 10, "complete", NOW + 2)
    ranges = engine._store.coverage()
    assert len(ranges) == 2
    assert ranges[0]["start_ts"] == NOW - 100 and ranges[0]["end_ts"] == NOW - 10


def test_calendar_guesses_on_fresh_install_are_schedule_only(engine):
    base = NOW - 90 * 86400
    engine.observe(
        [
            Observation(
                str(i),
                "event",
                base + i * 7 * 86400,
                end_ts=base + i * 7 * 86400 + 3600,
                subject="Gym",
                source="calendar",
            )
            for i in range(12)
        ]
    )
    patterns = engine.mine()
    assert any(p.kind == "habit" and p.stats["basis"] == "scheduled" for p in patterns)
    assert all("completion" in p.statement for p in patterns if p.kind == "habit")


def test_schedule_nudge_cannot_be_rewritten_into_attendance_claim(engine):
    class Unsafe:
        def phrase(self, *args):
            return "you skipped it"

    engine._phraser = Unsafe()
    for kind in ("habit_missed", "habit_conflict", "cancel_likely", "day_preview"):
        assert (
            engine._phrase(
                Nudge(
                    "n", kind, "T", "not on calendar", "source counts", created_at=NOW
                )
            ).body
            == "not on calendar"
        )


def test_new_routes_honor_learning_off_and_forget(monkeypatch, engine):
    from service import behavior_api

    class Runtime:
        async def acall(self, method, *args):
            return getattr(engine, method)(*args)

        def invalidate_card(self):
            pass

    monkeypatch.setattr(behavior_api.behavior_runtime, "get_runtime", lambda: Runtime())
    app = FastAPI()
    app.include_router(behavior_api.router)
    client = TestClient(app)
    assert client.get("/behavior/weekly").status_code == 200
    job = client.get("/behavior/history/calendar/next").json()
    assert job["start_ts"] == EARLIEST
    assert (
        client.post("/behavior/history/calendar/batch", json=batch(job)).status_code
        == 200
    )
    assert client.get("/behavior/history/nonsense/next").status_code == 422
    monkeypatch.setattr(behavior_api.behavior_runtime, "get_runtime", lambda: None)
    monkeypatch.setattr(behavior_api.behavior_runtime, "forgetting", lambda: False)
    for path in ("weekly", "history", "history/calendar/next"):
        assert client.get("/behavior/" + path).status_code == 409
    assert (
        client.post("/behavior/history/calendar/batch", json=batch(job)).status_code
        == 409
    )
