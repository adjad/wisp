"""Pure synthetic source-first planning; no native sources or models."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import tempfile

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-daily-workspace-tests-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant.daily_workspace import assess, render

DAY, ZONE = "2026-10-14", "America/Los_Angeles"


def at(hour, minute=0):
    return datetime(2026, 10, 14, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


NOW = at(8)


def state():
    return dict(tasks=[], commitments=[], preferences=dict(start_minute=540, end_minute=1200, not_before=None),
                sources={s: dict(available=True, last_sync=NOW,
                    snapshot_started_at=NOW,
                    diagnostics=dict(coverage_start=at(0), coverage_end=at(23, 59) + 60))
                    for s in ("calendar", "reminders")})


def evidence(**sources):
    return {s: dict(ready=True, records=rows, detail="Synthetic bounded source") for s, rows in sources.items()}


def busy(identity="class", start=None, end=None, **changes):
    return dict(id=identity, kind="busy", start=at(14) if start is None else start,
                end=at(16) if end is None else end, confidence=.8, current=True,
                observed_at=NOW - 86400, coverage="adequate", title="Usual busy period",
                timezone=ZONE, evidence_refs=["fixture:activity-coverage"],
                because="Repeated observed availability pattern; activity is unknown.") | changes


def run(s=None, e=None, windows=None, **kw):
    return assess(DAY, ZONE, s or state(), e or {}, now=NOW,
                  behavior=dict(version=1, available=True, status="current", generated_at=NOW,
                                coverage="adequate", timezone=ZONE, windows=windows or []), **kw)


def test_source_tasks_precede_behavior_and_fit_around_two_unlisted_classes():
    s = state()
    s["preferences"]["start_minute"] = 13 * 60 + 45
    result = run(s, evidence(reminders=[dict(id="r", title="Submit assignment", due_ts=at(18))]),
                 [busy(), busy("class2", at(18), at(19))])
    plan = result["proposed_plan"]
    proposed = next(b for b in plan["blocks"] if b["kind"] == "suggestion")
    assert proposed["start"] == at(16)
    assert proposed["end"] == at(16, 25)
    assert result["tentative_count"] == 2
    assert plan["tasks"] == []  # no stored task or source mutation


def test_calendar_and_authored_pin_win_and_behavior_only_fills_unlisted_time():
    s = state()
    s["commitments"] = [dict(id="event", source="calendar", title="Meeting", kind="event",
                              when_ts=at(14, 30), end_ts=at(15), all_day=False)]
    s["tasks"] = [dict(id="pin", title="Chosen work", day=DAY, kind="task", duration_minutes=30,
        priority=1, status="active", due_ts=None, pinned_start=at(15), revision=1)]
    original = deepcopy(s)
    result = run(s, windows=[busy()])
    blocks = result["proposed_plan"]["blocks"]
    assert [(b["start"], b["end"]) for b in blocks if b["kind"] == "behavior"] == [(at(14), at(14, 30)), (at(15, 30), at(16))]
    assert next(b for b in blocks if b["task_id"] == "pin")["start"] == at(15)
    assert s == original


@pytest.mark.parametrize("change", [dict(confidence=.64), dict(current=False), dict(coverage="unknown"),
    dict(observed_at=NOW + 1), dict(observed_at=NOW - 22 * 86400), dict(end=at(14)),
    dict(end=None), dict(kind="usage"), dict(kind="contact"), dict(confidence=float("nan")), dict(because="")])
def test_weak_stale_malformed_or_nonbusy_predictions_do_not_reserve_time(change):
    assert run(windows=[busy() | change])["tentative_count"] == 0


def test_correction_ignores_only_selected_day_window():
    assert run(windows=[busy(), busy("other", at(18), at(19))], ignored_windows=["class"])["tentative_count"] == 1


def test_timezone_mismatch_or_absent_contract_creates_no_busy_time():
    s = state()
    result = assess(DAY, ZONE, s, {}, now=NOW,
                    behavior=dict(ready=True, timezone="UTC", windows=[busy()]))
    assert not result["tentative_count"]
    assert not assess(DAY, ZONE, s, {}, now=NOW)["tentative_count"]


def test_communication_receipt_never_becomes_deadline_or_confirmed_assignment():
    e = evidence(email=[dict(id="mail", title="Project question", quote="Could you review this?",
        review_reason="Review original; request ownership unknown", due_ts=at(10), received_at=at(8))])
    result = run(e=e)
    item = result["attention"][0]
    assert item["due_ts"] is None and item["needs_review"]
    block = result["proposed_plan"]["blocks"][0]
    assert block["title"] == "Review: Project question"
    assert block["end"] - block["start"] == 15 * 60


def test_all_sources_contribute_and_unverified_or_resolved_rows_are_withheld():
    rows = dict(id="x", title="Review source", quote="Evidence", review_reason="Check relevance")
    e = evidence(email=[rows], messages=[rows], notes=[rows],
                 reminders=[dict(id="r", title="Due task", due_ts=at(11))])
    result = run(e=e)
    assert {i["source"] for i in result["attention"]} == {"email", "messages", "notes", "reminders"}
    e["email"]["ready"] = False
    e["notes"]["records"][0] = rows | dict(status="completed")
    result = run(e=e)
    assert {i["source"] for i in result["attention"]} == {"messages", "reminders"}
    assert len(result["source_coverage"]) == 5


def test_unknown_ownership_or_missing_quote_cannot_create_a_suggestion():
    result = run(e=evidence(notes=[dict(id="n", title="An idea")], email=[dict(id="e", title="Email", review_reason="maybe")]))
    assert not result["attention"] and result["suggested_count"] == 0


def test_undated_is_attention_without_fabricated_due_and_overdue_can_fit_today():
    result = run(e=evidence(reminders=[dict(id="u", title="Someday", undated=True),
        dict(id="r", title="Past due", due_ts=NOW - 86400)]))
    assert result["suggested_count"] == 1
    assert next(i for i in result["attention"] if i["title"] == "Someday")["due_ts"] is None
    assert result["proposed_plan"]["blocks"][0]["start"] == at(9)


def test_calendar_freshness_and_unknown_end_gates_still_hold():
    s = state()
    s["sources"]["calendar"]["last_sync"] = NOW - 181
    e = evidence(reminders=[dict(id="r", title="Work", due_ts=at(18))])
    result = run(s, e)
    assert not [b for b in result["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]
    assert result["proposed_plan"]["unscheduled"]
    s = state()
    s["commitments"] = [dict(id="e", source="calendar", kind="event", title="Unknown end", when_ts=at(9), end_ts=None)]
    assert run(s, e)["proposed_plan"]["unscheduled"]


def test_render_explains_provenance_uncertainty_and_missing_coverage():
    result = run(e=evidence(reminders=[dict(id="r", title="Assignment", due_ts=at(17))]), windows=[busy()])
    text = render(result)
    assert "Needs my attention" in text and "Suggested day plan" in text
    assert "[reminders]" in text and "tentative" in text and "Source coverage" in text
    assert "class attendance" not in text


def test_attention_and_suggestion_bounds_are_disclosed():
    result = run(e=evidence(reminders=[dict(id=str(i), title="Task " + str(i), due_ts=at(17)) for i in range(90)]))
    assert len(result["attention"]) == 80 and result["attention_omitted"] == 10
    assert result["suggested_count"] == 8


def test_flexible_authored_work_does_not_erase_busy_time():
    s = state()
    s["tasks"] = [dict(id="long", title="Flexible project", day=DAY, kind="task", duration_minutes=360,
        priority=1, status="active", due_ts=None, pinned_start=None, revision=1)]
    result = run(s, windows=[busy()])
    assert [(b["start"], b["end"]) for b in result["proposed_plan"]["blocks"] if b["kind"] == "behavior"] == [(at(14), at(16))]
    assert result["proposed_plan"]["unscheduled"][0]["task_id"] == "long"


@pytest.mark.parametrize("receipt", [dict(complete=False), dict(diagnostics=dict(partial=True, coverage_start=at(0), coverage_end=at(23, 59) + 60)),
                                     dict(diagnostics=dict(complete=False, coverage_start=at(0), coverage_end=at(23, 59) + 60))])
def test_partial_calendar_receipt_cannot_schedule_flexible_work(receipt):
    s = state()
    s["sources"]["calendar"].update(receipt)
    result = run(s, evidence(reminders=[dict(id="r", title="Work", due_ts=at(18))]))
    assert not [b for b in result["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]


def test_distinct_native_reminders_with_same_title_keep_distinct_work():
    result = run(e=evidence(reminders=[dict(id="one", title="Review", due_ts=at(17)), dict(id="two", title="Review", due_ts=at(17))]))
    assert result["suggested_count"] == 2


@pytest.fixture
def cached_sources(monkeypatch):
    """Existing cache APIs, with no native reader/model/network boundary."""
    from types import SimpleNamespace
    from contextlib import contextmanager
    import sqlite3
    import json
    from service.tools import email_tools as email, imessage_tools as messages, notes_tools as notes
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE today_source_sync(source TEXT,payload TEXT); CREATE TABLE commitments(id TEXT,source TEXT,source_id TEXT,title TEXT,when_ts REAL,kind TEXT,status TEXT);")
    db.execute("INSERT INTO today_source_sync VALUES('reminders',?)", (json.dumps(state()["sources"]["reminders"]),))
    class Store:
        view = dict(freshness="current", status="complete", total=0)
        undated = []
        @contextmanager
        def transaction(self, write=False):
            assert write is False
            yield db
        def undated_listing(self):
            return self.view, self.undated
    store = Store()
    monkeypatch.setattr(email, "raw_email_snapshot", lambda: None)
    monkeypatch.setattr(messages, "messages_sync_state", lambda: "unavailable")
    monkeypatch.setattr(messages, "summary_message_rows", lambda: [])
    monkeypatch.setattr(messages, "structured_messages_snapshot", lambda: dict(records=[], coverage={}))
    empty_note = SimpleNamespace(origin="none", body_started_at=0, body_received_at=0,
        latest_receipt=dict(available=False), records=(), omitted=0, malformed=0)
    monkeypatch.setattr(notes, "raw_notes_snapshot", lambda: empty_note)
    yield store, db, email, messages, notes
    db.close()


def test_native_reminder_cap_is_not_consumed_by_manual_rows_and_true_omissions_count(cached_sources):
    from service.assistant.daily_workspace import collect_evidence
    store, db, *_ = cached_sources
    db.executemany("INSERT INTO commitments VALUES(?,?,?,?,?,?,?)", [
        ("m" + str(i), "manual", str(i), "Local " + str(i), at(11), "reminder", "active") for i in range(700)])
    db.executemany("INSERT INTO commitments VALUES(?,?,?,?,?,?,?)", [
        ("r" + str(i), "reminders", str(i), "Native " + str(i), at(17), "reminder", "active") for i in range(600)])
    store.view = dict(freshness="current", status="truncated", stored=200, total=300)
    store.undated = [dict(source_id="u" + str(i), title="Undated " + str(i)) for i in range(200)]
    bundle = collect_evidence(store, state(), DAY, ZONE, now=NOW)
    reminders = bundle["reminders"]
    assert sum(r["title"].startswith("Native") for r in reminders["records"]) == 500
    assert sum(r.get("wisp_only") is True for r in reminders["records"]) == 100
    assert reminders["omitted"] == 900  # 100 native + 600 local + 200 undated


def test_messages_keep_unique_guid_without_weakening_existing_summary_selection(cached_sources, monkeypatch):
    from service.assistant.daily_workspace import collect_evidence
    store, _, _, messages, _ = cached_sources
    body = "Can you review the project?"
    monkeypatch.setattr(messages, "messages_sync_state", lambda: "ready")
    monkeypatch.setattr(messages, "summary_message_rows", lambda: [(NOW, "Alice", "Alice: " + body)])
    monkeypatch.setattr(messages, "structured_messages_snapshot", lambda: dict(records=[dict(guid="native-message", conversation="native-chat",
        timestamp=datetime.fromtimestamp(NOW, ZoneInfo("UTC")).isoformat(), text=body)], coverage=dict(skipped=2, truncated=1, malformed=0)))
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["messages"]
    assert result["records"][0]["reference"] == "messages:native-message · chat:native-chat"
    assert result["omitted"] == 2 and "1 shortened" in result["detail"]


def test_fresh_notes_without_read_start_are_withheld_and_malformed_count_disclosed(cached_sources, monkeypatch):
    from types import SimpleNamespace
    from service.assistant.daily_workspace import collect_evidence
    store, _, _, _, notes = cached_sources
    data = dict(origin="live", body_started_at=0, body_received_at=NOW,
        latest_receipt=dict(available=True, received_at=NOW), records=(dict(title="School", body="- [ ] Study tomorrow", reference="notes-local:fixture"),),
        omitted=2, malformed=3)
    monkeypatch.setattr(notes, "raw_notes_snapshot", lambda: SimpleNamespace(**data))
    assert not collect_evidence(store, state(), DAY, ZONE, now=NOW)["notes"]["ready"]
    data["body_started_at"] = NOW
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["notes"]
    assert result["ready"] and result["omitted"] == 5
    assert "3 malformed" in result["detail"]
    assert "due_ts" not in result["records"][0]


def test_body_request_can_surface_old_received_mail_without_fabricating_a_due_date(cached_sources, monkeypatch):
    from types import SimpleNamespace
    from service.assistant.daily_workspace import collect_evidence
    store, _, email, _, _ = cached_sources
    row = dict(ts=NOW - 10 * 86400, account_id="account", message_id="message", subject="Project",
        sender="Alice", sender_address="alice@example.test", body="Could you submit the project by Friday?", unread=False)
    monkeypatch.setattr(email, "raw_email_snapshot", lambda: SimpleNamespace(rows=[row]))
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert result["records"] and "Friday" in result["records"][0]["quote"]
    assessed = run(e={"email": result})
    assert assessed["attention"][0]["due_ts"] is None


def test_versioned_behavior_preparation_requires_grounding_and_duration_choice():
    demand = dict(id="quiz", title="Study for the quiz", source="email", source_reference="email:quiz",
        quote="Quiz on Friday", due_ts=at(17) + 2 * 86400, confidence=.8, current=True,
        timezone=ZONE, coverage="adequate", evidence_refs=["email:quiz"])
    contract = dict(version=1, available=True, status="current", generated_at=NOW, timezone=ZONE,
                    coverage="adequate", windows=[], preparation_demands=[demand])
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)
    assert result["attention"][0]["task_kind"] == "study" and result["suggested_count"] == 0
    demand.update(duration_minutes=45, duration_basis="user")
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)
    assert result["suggested_count"] == 1
    assert result["proposed_plan"]["blocks"][0]["end"] - result["proposed_plan"]["blocks"][0]["start"] == 45 * 60
    contract["version"] = 2
    assert not assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)["attention"]


def test_unknown_stale_or_malformed_provider_contract_is_unavailable(monkeypatch):
    from service.assistant import daily_workspace as daily
    for value in [None, {}, dict(version=2), dict(version=1, available=True, status="current", generated_at=NOW - 1201,
        timezone=ZONE, coverage="adequate", windows=[busy()]), dict(version=1, available=True, status="current", generated_at=NOW,
        timezone=ZONE, coverage=[], windows=[busy()])]:
        monkeypatch.setattr(daily, "_availability_provider", lambda *args: value)
        assert daily.availability_snapshot(DAY, ZONE, NOW)["available"] is False


def test_header_only_fallback_requires_matching_fresh_per_account_contract(cached_sources, monkeypatch):
    from service.assistant.daily_workspace import collect_evidence
    store, _, email, _, _ = cached_sources
    details = [dict(account="School", source="gmail", fresh=True, last_success=NOW, error="")]
    monkeypatch.setattr(email, "email_source_details", lambda **kwargs: details, raising=False)
    monkeypatch.setattr(email, "header_rows", lambda: [dict(account="School", account_id="account", message_id="message",
        ts=NOW, subject="Assignment due Friday", sender="Course", sender_address="course@school.edu", unread=True)])
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert result["ready"] and "Header-only" in result["records"][0]["review_reason"]
    details[0]["fresh"] = False
    assert not collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]["records"]
