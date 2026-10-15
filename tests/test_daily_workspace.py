"""Pure synthetic source-first planning; no native sources or models."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
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
                timezone=ZONE, evidence=["fixture:activity-coverage"],
                validation="provisional recurrence strength; not calibrated availability probability",
                because="Repeated observed availability pattern; activity is unknown.") | changes


def context(*, windows=(), demands=(), **changes):
    """Public v1 development wire, not the old invented availability envelope."""
    return dict(version=1, ready=True, generated_at=NOW - 60, evidence_checked_at=NOW,
        timezone=ZONE, windows=list(windows) if windows is not None else None,
        demands=list(demands) if demands is not None else None, associations=[],
        source_coverage={"email": "bounded current publication; not full history", "notes": "off"},
        detail="Tentative source-grounded hypotheses; not attendance or availability guarantees.") | changes


def demand(**changes):
    """Shape of associations.materialize_work at producer snapshot 587961a5."""
    quote = changes.get("quote", "Review the garden proposal by October 14 at 5pm")
    source = changes.get("source", "email")
    source_id = source + ":opaque-id"
    time_quote = "October 14 at 5pm"
    offset = quote.find(time_quote)
    time_evidence = (dict(start=7 + offset, end=7 + offset + len(time_quote),
        sha256=hashlib.sha256(time_quote.encode()).hexdigest()) if offset >= 0 else None)
    if changes.get("deadline_precision") == "unknown":
        time_evidence = None
    value = dict(id="work:fixture", source=source, source_id=source_id,
        reference=dict(source=source, account_id="fixture-account", message_id="fixture-message"),
        endpoint=dict(source=source, record_id=source_id, revision="revision:original",
            start=7, end=7 + len(quote), sha256=hashlib.sha256(quote.encode()).hexdigest(),
            source_time=NOW - 10 * 86400, time_basis="source_timestamp", evidence_basis="source_statement"),
        kind="task", title="Review the garden proposal", quote=quote, ownership="user", status="tentative",
        due_ts=at(17), due_range=[at(17), at(17) + 60], deadline_precision="instant",
        timezone=ZONE, analyzed_at=NOW - 60, source_receipt_at=NOW - 120,
        time_evidence=time_evidence, priority_dimensions=dict(importance="unknown",
            importance_basis="not established", inferred_importance="high",
            due_range=changes.get("due_range", [at(17), at(17) + 60]), recent_choices=[],
            preference_basis="voluntary planning choices; not proof of completion or value",
            displacement="unknown; different computer activity cannot establish abandonment"),
        earliest_start=NOW, suggested_minutes=30, duration_basis="adjustable default; not observed effort",
        importance="unknown", importance_hypothesis="high", basis="inferred", confidence=None,
        observed_at=NOW - 10 * 86400, evidence=[source_id], action_authority="none",
        because="Source-backed work hypothesis; ownership, effort and relevance remain uncertain.")
    return value | changes


def run(s=None, e=None, windows=None, **kw):
    return assess(DAY, ZONE, s or state(), e or {}, now=NOW,
                  behavior=context(windows=windows or []), **kw)


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


@pytest.mark.parametrize("title,quote", [("Study for the quiz", "Quiz on October 14 at 5pm"),
    ("Prepare a budget", "Budget review is October 14 at 5pm"),
    ("Review the garden proposal", "Proposal review is October 14 at 5pm")])
def test_public_v1_generic_demands_have_unknown_confidence_and_adjustable_default(title, quote):
    work = demand(title=title, quote=quote)
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=context(demands=[work]))
    item = result["attention"][0]
    assert item["task_kind"] == "task" and item["confidence"] is None and item["importance"] == "unknown"
    assert item["duration_basis"] == "adjustable default; not observed effort"
    assert "not observed effort" in item["reason"] and "Importance is unknown" in item["reason"]
    block = result["proposed_plan"]["blocks"][0]
    assert block["title"] == "Review: " + title and block["end"] - block["start"] == 30 * 60
    assert block["kind"] == "suggestion" and block["task_id"] is None


@pytest.mark.parametrize("change", [dict(ownership="unknown", status="unresolved"),
    dict(due_ts=None, due_range=None, deadline_precision="unknown", status="unresolved")])
def test_unknown_ownership_or_deadline_is_review_attention_only(change):
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=context(demands=[demand(**change)]))
    assert result["attention"][0]["needs_review"] and result["attention"][0]["planning_duration"] is None
    assert not result["suggested_count"] and not result["proposed_plan"]["blocks"]
    assert result["proposed_plan"]["tasks"] == []


def test_date_range_is_preserved_without_fabricating_an_instant():
    span = [at(0) + 2 * 86400, at(0) + 3 * 86400]
    work = demand(quote="Review the garden proposal on October 16", due_ts=None,
        due_range=span, deadline_precision="date")
    work["time_evidence"] = dict(start=7 + work["quote"].index("October 16"),
        end=7 + len(work["quote"]), sha256=hashlib.sha256(b"October 16").hexdigest())
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=context(demands=[work]))
    item = result["attention"][0]
    assert item["due_ts"] is None and item["due_range"] == span and item["deadline_precision"] == "date"
    assert "source range [Oct 16" in render(result)
    assert "not an exact deadline" in item["reason"] and result["suggested_count"] == 1


@pytest.mark.parametrize("change", [dict(suggested_minutes=None), dict(suggested_minutes=True),
    dict(suggested_minutes=481), dict(duration_basis="model"), dict(duration_basis=None)])
def test_unknown_or_invalid_effort_stays_attention_until_user_chooses(change):
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=context(demands=[demand(**change)]))
    assert result["attention"] and result["attention"][0]["planning_duration"] is None
    assert "Duration unknown" in result["attention"][0]["reason"] and not result["suggested_count"]


def test_original_locator_revision_quote_and_ages_survive_without_mutation():
    work = demand(quote="Review 🦉 café proposal by October 14 at 5pm")
    raw = context(demands=[work], associations=[dict(id="link:fixture", status="proposed", confidence=None,
        importance="unknown", action_authority="none", analyzed_at=NOW - 90)])
    before = deepcopy(raw)
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=raw)
    item = result["attention"][0]
    for field in ("quote", "source_id", "evidence", "endpoint", "observed_at", "source_receipt_at", "analyzed_at", "time_evidence", "earliest_start", "priority_dimensions"):
        assert item[field] == work[field]
    assert item["source_reference"] == work["reference"] and json.loads(item["reference"]) == work["reference"]
    assert isinstance(item["reference"], str)  # Existing native attention decoder contract.
    assert result["behavior_context"]["associations"] == raw["associations"]
    assert raw == before
    item["endpoint"]["revision"] = "local"
    assert raw == before


@pytest.mark.parametrize("change", [dict(source_receipt_at=None), dict(source_receipt_at=NOW - 1201),
    dict(source_receipt_at=NOW + 1), dict(analyzed_at=None), dict(analyzed_at=NOW - 1201),
    dict(analyzed_at=NOW + 1), dict(observed_at=NOW + 1), dict(observed_at=NOW - 1), dict(timezone="UTC"),
    dict(confidence=.8), dict(importance="high"), dict(action_authority="send"), dict(ownership="other"),
    dict(status="completed"), dict(evidence=[]), dict(reference="invented://source"),
    dict(due_ts=at(12)), dict(due_range=[at(17), at(16)]),
    dict(deadline_precision="date"), dict(deadline_precision="unknown"), dict(kind="study")])
def test_missing_stale_or_invalid_demand_proof_is_withheld(change):
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=context(demands=[demand(**change)]))
    assert not result["attention"] and not result["suggested_count"]


@pytest.mark.parametrize("change", [dict(source="notes"), dict(record_id="other"), dict(revision=""),
    dict(start=-1), dict(end=8), dict(sha256="wrong")])
def test_bad_endpoint_proof_is_withheld(change):
    work = demand()
    work["endpoint"].update(change)
    assert not assess(DAY, ZONE, state(), {}, now=NOW, behavior=context(demands=[work]))["attention"]


def test_notes_unknown_source_time_is_preserved_and_not_replaced_by_publication():
    work = demand(source="notes", observed_at=None, due_ts=None, due_range=None,
        deadline_precision="unknown", status="unresolved")
    work["endpoint"].update(source_time=None, time_basis="unknown")
    work["reference"] = dict(source="notes", reference="notes-local:fixture")
    item = assess(DAY, ZONE, state(), {}, now=NOW, behavior=context(demands=[work]))["attention"][0]
    assert item["observed_at"] is None and item["due_ts"] is None
    assert item["source_receipt_at"] == NOW - 120 and item["source_reference"] == work["reference"]


def test_explicit_work_and_priority_win_over_producer_importance_hypotheses():
    s = state()
    s["tasks"] = [dict(id="pin", title="Chosen work", day=DAY, kind="task", duration_minutes=30,
        priority=1, status="active", due_ts=None, pinned_start=at(15), revision=1)]
    work = demand(priority=1, priority_basis="model")
    result = assess(DAY, ZONE, s, {}, now=NOW, behavior=context(demands=[work], windows=[busy()]))
    assert result["attention"][0]["source"] == "wisp"
    item = next(i for i in result["attention"] if i["source"] == "behavior")
    assert item["priority"] == 3 and item["importance"] == "unknown" and item["importance_hypothesis"] == "high"
    assert next(b for b in result["proposed_plan"]["blocks"] if b.get("task_id") == "pin")["start"] == at(15)
    s["tasks"][0]["title"] = work["title"]
    assert not assess(DAY, ZONE, s, {}, now=NOW, behavior=context(demands=[work]))["suggested_count"]


@pytest.fixture
def public_behavior(monkeypatch):
    """Fake ONLY the public module; never import/call an actual producer runtime."""
    import sys
    from types import ModuleType
    from service.assistant import daily_workspace as daily
    module = ModuleType("service.assistant.behavior_runtime")
    module.planning_context = lambda *args: None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(daily, "_availability_provider", None)
    return module


@pytest.mark.parametrize("value", [None, {}, dict(version=2), context(ready=False), context(ready=1),
    context(version=True), context(timezone="UTC"), context(generated_at=NOW - 1201),
    context(generated_at=NOW + 1), context(evidence_checked_at=NOW - 1201),
    context(evidence_checked_at=NOW + 1), context(windows=None), context(demands=None),
    context(source_coverage=[]), context(source_coverage={"email": True}),
    dict(version=1, available=True, status="current", generated_at=NOW, timezone=ZONE, coverage="complete")])
def test_missing_disabled_malformed_or_stale_public_context_is_unavailable(public_behavior, value):
    from service.assistant import daily_workspace as daily
    public_behavior.planning_context = lambda *args: value
    result = daily.availability_snapshot(DAY, ZONE, NOW)
    assert result["ready"] is False and result["generated_at"] is None
    assert "unknown" in result["detail"] and not result["windows"] and not result["demands"]
    assert not assess(DAY, ZONE, state(), {}, now=NOW, behavior=result)["tentative_count"]


def test_missing_public_method_and_exception_are_unavailable(public_behavior):
    from service.assistant import daily_workspace as daily
    del public_behavior.planning_context
    assert daily.availability_snapshot(DAY, ZONE, NOW)["ready"] is False
    def failure(*args):
        raise RuntimeError("synthetic failure")
    public_behavior.planning_context = failure
    assert daily.availability_snapshot(DAY, ZONE, NOW)["ready"] is False


@pytest.mark.parametrize("revocation", ["consent", "generation", "source_revision", "analysis_role"])
def test_each_public_read_rechecks_authority_without_caching_or_inference(public_behavior, revocation):
    from service.assistant import daily_workspace as daily
    calls, eligible = [], {revocation: True}
    raw = context(windows=[busy()], demands=[demand()])
    before = deepcopy(raw)
    def wrapper(day, timezone, now=None):
        calls.append((day, timezone, now))
        return raw if eligible[revocation] else context(ready=False)
    public_behavior.planning_context = wrapper
    first = daily.availability_snapshot(DAY, ZONE, NOW)
    assert first == raw and first is not raw
    later = daily.availability_snapshot(DAY, ZONE, NOW + 1)
    assert later["generated_at"] == first["generated_at"] and later["evidence_checked_at"] == NOW
    assert later["demands"][0]["source_receipt_at"] == NOW - 120
    eligible[revocation] = False
    assert daily.availability_snapshot(DAY, ZONE, NOW + 2)["ready"] is False
    assert calls == [(DAY, ZONE, NOW), (DAY, ZONE, NOW + 1), (DAY, ZONE, NOW + 2)] and raw == before


def test_fresh_public_read_cannot_renew_old_analysis_or_source_receipt(public_behavior):
    from service.assistant import daily_workspace as daily
    public_behavior.planning_context = lambda *args: context(evidence_checked_at=NOW, generated_at=NOW - 1201)
    assert not daily.availability_snapshot(DAY, ZONE, NOW)["ready"]
    raw = context(demands=[demand(source_receipt_at=NOW - 1201)])
    public_behavior.planning_context = lambda *args: raw
    snapshot = daily.availability_snapshot(DAY, ZONE, NOW)
    assert snapshot["ready"] and not assess(DAY, ZONE, state(), {}, now=NOW, behavior=snapshot)["attention"]


def test_partial_source_availability_is_descriptive_not_full_coverage(public_behavior):
    from service.assistant import daily_workspace as daily
    raw = context(windows=[busy()], demands=[demand()], source_coverage={"email": "bounded current publication; not full history",
        "messages": "current complete evidence unavailable", "notes": "off", "websites": "off"})
    public_behavior.planning_context = lambda *args: raw
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=daily.availability_snapshot(DAY, ZONE, NOW))
    assert result["suggested_count"] == 1 and result["tentative_count"] == 1
    assert result["behavior_context"]["source_coverage"] == raw["source_coverage"]
    assert "Behavior messages: current complete evidence unavailable" in render(result)
    assert "coverage" not in raw


def test_window_evidence_strength_is_preserved_and_separate_from_demand_confidence():
    result = assess(DAY, ZONE, state(), {}, now=NOW,
        behavior=context(windows=[busy(confidence=.8)], demands=[demand()]))
    block = next(b for b in result["proposed_plan"]["blocks"] if b["kind"] == "behavior")
    assert block["recurrence_strength"] == .8 and block["observed_at"] == NOW - 86400
    assert block["evidence_refs"] == ["fixture:activity-coverage"]
    assert "not calibrated" in block["validation"] and result["attention"][0]["confidence"] is None
    weak = assess(DAY, ZONE, state(), {}, now=NOW,
        behavior=context(windows=[busy(confidence=.64)], demands=[demand()]))
    assert not weak["tentative_count"] and weak["suggested_count"] == 1


def test_public_wrapper_get_composition_preserves_selected_day_zone_and_corrections(public_behavior, cached_sources, monkeypatch):
    from types import SimpleNamespace
    from service.assistant import daily_workspace_api as api
    store, *_ = cached_sources
    monkeypatch.setattr(store, "today_snapshot", lambda *args: state(), raising=False)
    monkeypatch.setattr(api, "email_store", lambda: SimpleNamespace(ignored=lambda day, zone: ["class"]))
    calls = []
    def wrapper(day, timezone, now=None):
        calls.append((day, timezone, now))
        return context(windows=[busy(), busy("other", at(18), at(19))])
    public_behavior.planning_context = wrapper
    result = api.current_workspace(DAY, ZONE, now=NOW, store=store)
    assert calls == [(DAY, ZONE, NOW)] and result["tentative_count"] == 1
    assert result["proposed_plan"]["blocks"][0]["pattern_id"] == "other"
    # The producer's v1 has no day field: the request carries day; bounds clip
    # windows to that requested local day. No consumer date is fabricated.
    assert not assess("2026-10-15", ZONE, state(), {}, now=NOW,
                      behavior=context(windows=[busy()]))["tentative_count"]
    public_behavior.planning_context = lambda day, zone, now: context(timezone=zone, windows=[])
    assert api.availability("2026-10-15", "UTC", NOW)["timezone"] == "UTC"


def test_http_get_uses_only_fake_public_wrapper_and_never_model_or_actions(public_behavior, cached_sources, monkeypatch):
    import importlib
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from service.assistant import daily_workspace_api as api
    store, *_ = cached_sources
    monkeypatch.setattr(store, "today_snapshot", lambda *args: state(), raising=False)
    monkeypatch.setattr(importlib.import_module("service.assistant.store"), "assistant_store", store)
    monkeypatch.setattr(api, "email_store", lambda: SimpleNamespace(ignored=lambda *args: []))
    monkeypatch.setattr(api.time, "time", lambda: NOW)
    calls, forbidden = [], []
    raw = context(windows=[busy()], demands=[demand()])
    before = deepcopy(raw)
    def wrapper(day, timezone, now=None):
        calls.append((day, timezone, now))
        return raw
    def effect(*args, **kwargs):
        forbidden.append(True)
        raise AssertionError("No inference, refresh, collection or action on GET")
    public_behavior.planning_context = wrapper
    for name in ("get_runtime", "tick", "poke", "refresh", "infer", "send", "collect"):
        setattr(public_behavior, name, effect)
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).get("/workspace", params=dict(day=DAY, timezone=ZONE))
    assert response.status_code == 200
    payload = response.json()
    assert payload["behavior_context"]["generated_at"] == NOW - 60
    assert payload["attention"][0]["analyzed_at"] == NOW - 60
    assert payload["attention"][0]["source_receipt_at"] == NOW - 120
    assert payload["proposed_plan"]["tasks"] == [] and not forbidden
    assert calls == [(DAY, ZONE, NOW)] and raw == before


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


@pytest.mark.parametrize("case", ["current", "missing_start", "future_start", "stale_start",
    "stale_sync", "partial", "incomplete", "syncing", "failed", "short_coverage",
    "persisted_current", "persisted_missing", "persisted_future", "persisted_stale"])
def test_today_and_direct_workspace_share_calendar_admission(cached_sources, monkeypatch, case):
    from types import SimpleNamespace
    from service.assistant import daily_workspace as daily, daily_workspace_api as api, today_api
    from service.assistant.today import build_plan
    store, db, *_ = cached_sources
    supplied = state() | dict(revision=0)
    receipt = supplied["sources"]["calendar"]
    if case == "missing_start": receipt.pop("snapshot_started_at")
    if case == "future_start": receipt["snapshot_started_at"] = NOW + 1
    if case == "stale_start": receipt["snapshot_started_at"] = NOW - 1201
    if case == "stale_sync": receipt["last_sync"] = NOW - 181
    if case == "partial": receipt["diagnostics"]["partial"] = True
    if case == "incomplete": receipt["complete"] = False
    if case == "syncing": receipt["syncing"] = True
    if case == "failed": receipt["available"] = False
    if case == "short_coverage": receipt["diagnostics"]["coverage_end"] = at(12)
    if case.startswith("persisted_"):
        import json
        from service.assistant.store import AssistantStore
        diagnostics = dict(receipt["diagnostics"])
        if case != "persisted_missing":
            diagnostics["snapshot_started_at"] = NOW + {"persisted_current": 0,
                "persisted_future": 1, "persisted_stale": -1201}[case]
        db.execute("CREATE UNIQUE INDEX source_identity ON today_source_sync(source)")
        AssistantStore._today_sync_receipt(SimpleNamespace(_db=db), "calendar", diagnostics, NOW, available=True)
        receipt = json.loads(db.execute("SELECT payload FROM today_source_sync WHERE source='calendar'").fetchone()[0])
        supplied["sources"]["calendar"] = receipt
        assert receipt["last_sync"] == (NOW - 1201 if case == "persisted_stale" else NOW)
    original = deepcopy(supplied)
    monkeypatch.setattr(store, "today_snapshot", lambda *args: supplied, raising=False)
    monkeypatch.setattr(today_api, "assistant_store", store)
    monkeypatch.setattr(today_api.time, "time", lambda: NOW)
    monkeypatch.setattr(api, "email_store", lambda: SimpleNamespace(ignored=lambda *args: []))
    monkeypatch.setattr(api, "availability", lambda *args: None)
    db.execute("INSERT INTO commitments VALUES(?,?,?,?,?,?,?)",
               ("work", "reminders", "native:work", "Synthetic work", at(17), "reminder", "active"))
    today = today_api.snapshot(DAY, ZONE, debug=True)
    direct = daily.workspace(store, DAY, ZONE, now=NOW)
    assert today["workspace"] == direct
    ready = case in {"current", "persisted_current"}
    assert direct["proposed_plan"]["sources"]["calendar"]["ready"] is ready
    assert next(s["ready"] for s in direct["source_coverage"] if s["source"] == "calendar") is ready
    assert bool([b for b in direct["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]) is ready
    assert supplied == original
    # The original Today planner and its replay retain their established input contract.
    expected = build_plan(DAY, ZONE, supplied["tasks"], supplied["commitments"],
                          supplied["preferences"], supplied["sources"], now=NOW)
    assert today["replay"]["sources"] == original["sources"]
    assert today["replay"]["expected_plan"] == expected
