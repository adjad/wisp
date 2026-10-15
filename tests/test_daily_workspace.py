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


@pytest.mark.parametrize("title,quote", [("Study for the quiz", "Quiz on Friday"),
    ("Prepare a budget", "Budget review is Friday"), ("Review the garden proposal", "Proposal review is Friday")])
def test_versioned_generic_demands_require_grounding_and_duration_choice(title, quote):
    demand = dict(id="work", kind="task", ownership="user", title=title, source="email", source_reference="email:work",
        quote=quote, due_ts=at(17) + 2 * 86400, confidence=.8, current=True,
        timezone=ZONE, coverage="adequate", evidence_refs=["email:work"])
    contract = dict(version=1, available=True, status="current", generated_at=NOW, timezone=ZONE,
                    coverage="adequate", windows=[], demands=[demand])
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)
    assert result["attention"][0]["task_kind"] == "task" and result["suggested_count"] == 0
    assert "Duration unknown" in result["attention"][0]["reason"] and "Importance is unknown" in result["attention"][0]["reason"]
    demand.update(suggested_minutes=45, duration_basis="user")
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)
    assert result["suggested_count"] == 1
    assert result["proposed_plan"]["blocks"][0]["end"] - result["proposed_plan"]["blocks"][0]["start"] == 45 * 60
    contract["version"] = 2
    assert not assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)["attention"]


@pytest.mark.parametrize("basis", ["user", "source", "model", "default"])
def test_generic_estimates_preserve_basis_and_unknown_ownership_is_review_work(basis):
    demand = dict(id="arbitrary", kind="follow_up", title="Check the project", source="notes", source_reference="notes-local:work",
        quote="Check the project", due_ts=at(17), confidence=.8, current=True, timezone=ZONE, coverage="adequate",
        evidence_refs=["notes-local:work"], suggested_minutes=20, duration_basis=basis)
    contract = dict(version=1, available=True, status="current", generated_at=NOW, timezone=ZONE, coverage="adequate", windows=[], demands=[demand])
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)
    assert result["proposed_plan"]["blocks"][0]["title"] == "Review: Check the project"
    assert result["attention"][0]["duration_basis"] == basis and result["attention"][0]["importance"] == "unknown"
    demand["ownership"] = "other"
    assert not assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)["attention"]


def test_generic_demands_do_not_promote_inferred_importance_over_user_priority():
    demand = dict(id="work", kind="task", ownership="user", title="Explicit work", source="notes",
        source_reference="notes-local:work", quote="Work to do", confidence=.8, current=True, timezone=ZONE,
        coverage="adequate", evidence_refs=["notes-local:work"], suggested_minutes=20, duration_basis="default", priority=1)
    demand["uncertainty"] = ["Deadline interpretation needs review"]
    contract = dict(version=1, available=True, status="current", generated_at=NOW, timezone=ZONE, coverage="adequate", windows=[], demands=[demand])
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)
    assert result["attention"][0]["priority"] == 3 and result["attention"][0]["importance"] == "unknown"
    assert "Deadline interpretation needs review" in result["attention"][0]["reason"]
    demand["priority_basis"] = "user"
    result = assess(DAY, ZONE, state(), {}, now=NOW, behavior=contract)
    assert result["attention"][0]["priority"] == 1 and result["attention"][0]["importance"] == "user priority"


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


@pytest.fixture
def composed_header_sources(cached_sources, monkeypatch):
    """Actual public header accessors over synthetic in-memory backing only."""
    store, _, mail, *_ = cached_sources
    account = "work@example.test"
    def header(label=account, native_id=account, message="message", subject="Assignment due Friday"):
        return "\x01".join(["H2", str(int(NOW)), "U", label, native_id,
            "Course", "course@example.test", message, subject, message])
    backing = {account: dict(headers=header(), headers_at=NOW, headers_verified=True,
        headers_error="", history=None, raw=None)}
    for name, value in {"_external_accounts": backing, "_swift_headers": "", "_headers": header(),
        "_email_available": False, "_email_read_source": "local_index", "_email_reason": "Synthetic Mail closed",
        "_native_headers_at": 0, "_native_source": "", "_swift_identity": [],
        "_raw_emails": "PRIVATE_BODY_WITHOUT_ELIGIBLE_SNAPSHOT"}.items():
        monkeypatch.setattr(mail, name, value)
    monkeypatch.setattr(mail.time, "time", lambda: NOW)
    return store, mail, backing, header


@pytest.mark.parametrize("condition", ["current", "stale", "future", "error", "restored", "invalid_receipt"])
def test_actual_email_accessor_gates_header_only_daily_evidence(composed_header_sources, condition):
    from service.assistant.daily_workspace import collect_evidence
    store, mail, backing, _ = composed_header_sources
    account = "work@example.test"
    source = backing[account]
    if condition == "stale": source["headers_at"] = NOW - 301
    if condition == "future": source["headers_at"] = NOW + 1
    if condition == "error": source["headers_error"] = "Synthetic disconnected account"
    if condition == "restored": source["headers_verified"] = False
    if condition == "invalid_receipt": source["headers_at"] = "unknown"
    original_stamp = source["headers_at"]
    assert mail.header_rows()  # Display cache alone remains readable.
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert result["ready"] is (condition == "current")
    assert bool(result["records"]) is (condition == "current")
    assert source["headers_at"] == original_stamp  # Assessment cannot renew a receipt.
    assessed = run(e={"email": result})
    assert "PRIVATE_BODY_WITHOUT_ELIGIBLE_SNAPSHOT" not in render(assessed)
    if result["records"]:
        assert result["records"][0]["quote"] == "Assignment due Friday"
        assert "Header-only" in result["records"][0]["review_reason"]
        assert assessed["attention"][0]["due_ts"] is None
        assert assessed["attention"][0]["needs_review"]


def test_actual_email_accessor_keeps_fresh_account_separate_from_stale_account(composed_header_sources, monkeypatch):
    from service.assistant.daily_workspace import collect_evidence
    store, mail, backing, header = composed_header_sources
    stale = "stale@example.test"
    backing[stale] = dict(headers=header(stale, stale, "old", "Old assignment due Friday"),
        headers_at=NOW - 301, headers_verified=True, headers_error="", history=None, raw=None)
    monkeypatch.setattr(mail, "_headers", header() + "\n" + backing[stale]["headers"])
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert result["ready"] and [r["title"] for r in result["records"]] == ["Assignment due Friday"]
    assert len(mail.email_source_details(part="headers")) == 2


def test_actual_email_accessor_fresh_empty_is_bounded_evidence(composed_header_sources, monkeypatch):
    from service.assistant.daily_workspace import collect_evidence
    store, mail, backing, _ = composed_header_sources
    backing["work@example.test"]["headers"] = ""
    monkeypatch.setattr(mail, "_headers", "")
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert result["ready"] and not result["records"] and result["inspected"] == 0
    assert "bounded/unverified" in result["detail"]


def test_actual_email_accessor_preserves_distinct_native_ids_with_duplicate_labels(composed_header_sources, monkeypatch):
    from service.assistant.daily_workspace import collect_evidence
    store, mail, _, header = composed_header_sources
    rows = header("Work", "native-one") + "\n" + header("Work", "native-two")
    for name, value in {"_external_accounts": {}, "_headers": rows, "_swift_headers": rows,
        "_email_available": True, "_email_read_source": "mail_app", "_native_headers_at": NOW}.items():
        monkeypatch.setattr(mail, name, value)
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert result["ready"] and len(result["records"]) == 2
    assert len({r["id"] for r in result["records"]}) == 2
    assert len({r["reference"] for r in result["records"]}) == 2


def test_actual_email_receipt_change_during_header_read_withholds_evidence(composed_header_sources, monkeypatch):
    from service.assistant.daily_workspace import collect_evidence
    store, mail, backing, _ = composed_header_sources
    actual_rows = mail.header_rows
    def changed_rows():
        rows = actual_rows()
        backing["work@example.test"]["headers_error"] = "Revoked during assessment"
        return rows
    monkeypatch.setattr(mail, "header_rows", changed_rows)
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert not result["ready"] and not result["records"]


def test_eligible_immutable_body_snapshot_precedes_actual_header_fallback(composed_header_sources, monkeypatch):
    from types import SimpleNamespace
    from service.assistant.daily_workspace import collect_evidence
    store, mail, _, _ = composed_header_sources
    row = dict(ts=NOW, account_id="native-account", message_id="body-message", subject="Project question",
        sender="Course", sender_address="course@example.test", body="Could you submit the project by Friday?", unread=False)
    monkeypatch.setattr(mail, "raw_email_snapshot", lambda: SimpleNamespace(rows=(row,)))
    def no_header_read(*args, **kwargs):
        pytest.fail("An eligible immutable body snapshot should not trigger a second header assessment")
    monkeypatch.setattr(mail, "email_source_details", no_header_read)
    result = collect_evidence(store, state(), DAY, ZONE, now=NOW)["email"]
    assert result["ready"] and result["records"][0]["reference"] == "email:native-account:body-message"
    assert "Could you submit" in result["records"][0]["quote"]


@pytest.mark.parametrize("source,priority", [("reminders", 3), ("reminders", 1), ("email", 2)])
def test_saved_work_keeps_its_fitting_slot_before_more_urgent_or_tied_suggestions(source, priority):
    s = state()
    s["preferences"].update(start_minute=540, end_minute=570)
    s["tasks"] = [dict(id="zz-chosen", title="Chosen project", day=DAY, kind="project",
        duration_minutes=30, priority=priority, status="active", due_ts=None, pinned_start=None, revision=7)]
    original = deepcopy(s)
    row = (dict(id="r", title="Overdue different work", due_ts=NOW - 86400) if source == "reminders" else
           dict(id="e", title="Different source request", quote="Please review this project", review_reason="Review original"))
    baseline = run(s)["proposed_plan"]
    plan = run(s, evidence(**{source: [row]}))["proposed_plan"]
    chosen = next(b for b in plan["blocks"] if b.get("task_id") == "zz-chosen")
    assert chosen == next(b for b in baseline["blocks"] if b.get("task_id") == "zz-chosen")
    assert (chosen["start"], chosen["end"]) == (at(9), at(9, 30))
    assert not [b for b in plan["blocks"] if b["kind"] == "suggestion"]
    assert len(plan["unscheduled"]) == 1 and plan["unscheduled"][0]["task_id"] != "zz-chosen"
    assert plan["tasks"] == original["tasks"] and s == original


def test_suggestions_fit_only_after_authored_work_and_supported_holds():
    s = state()
    s["preferences"].update(start_minute=540, end_minute=660)
    s["commitments"] = [dict(id="event", source="calendar", title="Fixed commitment", kind="event",
        when_ts=at(9), end_ts=at(9, 15), all_day=False)]
    s["tasks"] = [dict(id="chosen", title="Chosen project", day=DAY, kind="project", duration_minutes=30,
        priority=3, status="active", due_ts=None, pinned_start=None, revision=2),
        dict(id="pin", title="Chosen pin", day=DAY, kind="task", duration_minutes=15,
        priority=3, status="active", due_ts=None, pinned_start=at(10), revision=4)]
    original = deepcopy(s)
    holds = [busy(start=at(9, 15), end=at(9, 30))]
    baseline = run(s, windows=holds)["proposed_plan"]
    plan = run(s, evidence(reminders=[dict(id="r", title="Overdue source work", due_ts=NOW - 86400)]), holds)["proposed_plan"]
    assert [b for b in plan["blocks"] if b.get("task_id")] == [b for b in baseline["blocks"] if b.get("task_id")]
    suggestion = next(b for b in plan["blocks"] if b["kind"] == "suggestion")
    assert (suggestion["start"], suggestion["end"]) == (at(10, 15), at(10, 40))
    assert next(b for b in plan["blocks"] if b.get("task_id") == "chosen")["start"] == at(9, 30)
    assert s == original and plan["tasks"] == original["tasks"]


def test_cross_day_pin_and_saved_task_revisions_survive_source_refresh():
    s = state()
    s["preferences"].update(start_minute=0, end_minute=60)
    for receipt in s["sources"].values():
        receipt.update(last_sync=at(0), snapshot_started_at=at(0))
    s["tasks"] = [dict(id="overnight", title="Overnight pin", day="2026-10-13", kind="project",
        duration_minutes=60, priority=3, status="active", due_ts=None, pinned_start=at(0) - 1800, revision=8),
        dict(id="chosen", title="Morning work", day=DAY, kind="task", duration_minutes=30,
        priority=3, status="active", due_ts=None, pinned_start=None, revision=3)]
    original = deepcopy(s)
    baseline = assess(DAY, ZONE, s, {}, now=at(0))["proposed_plan"]
    result = assess(DAY, ZONE, s, evidence(reminders=[dict(id="r", title="Other overdue work", due_ts=at(0) - 86400)]),
                    now=at(0))
    plan = result["proposed_plan"]
    assert plan["tasks"] == original["tasks"] and s == original
    assert [b for b in plan["blocks"] if b.get("task_id")] == baseline["blocks"]
    assert next(b for b in plan["blocks"] if b.get("task_id") == "chosen")["start"] == at(0, 30)
    assert not [b for b in plan["blocks"] if b["kind"] == "suggestion"]
    pin = next(b for b in plan["blocks"] if b.get("task_id") == "overnight")
    assert pin["start"] == at(0) - 1800 and "Continues from the previous day" in pin["warnings"]


@pytest.mark.parametrize("case,flags", [
    ("current", {}), ("explicit_complete", dict(complete=True, partial=False)),
    ("partial", dict(partial=True)), ("incomplete", dict(complete=False)),
    ("both", dict(partial=True, complete=False)),
    *[(f"{key}_{i}", {key: value}) for key in ("partial", "complete")
      for i, value in enumerate([None, 0, 1, "false", "true", [], {}])],
    ("missing_start", {}), ("future_start", {}), ("stale_start", {}),
    ("short_coverage", {}), ("unavailable", {}), ("syncing", {}),
])
def test_actual_calendar_route_persists_admission_through_today_workspace_and_email(
        cached_sources, monkeypatch, tmp_path, case, flags):
    import ast
    import asyncio
    import json
    import sys
    import types
    from pathlib import Path
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace as daily, daily_workspace_api as api, today_api
    from service.assistant.store import AssistantStore
    from service.assistant.today import build_plan
    from service.assistant.day_plan_email import EmailStore, SelfIdentity, deliver_due
    import service.assistant as assistant_package
    actual = AssistantStore(tmp_path / "calendar.sqlite3")
    mail = EmailStore(tmp_path / "email.sqlite3", process="calendar-contract")
    monkeypatch.setattr(api.time, "time", lambda: NOW)
    monkeypatch.setattr(today_api, "assistant_store", actual)
    monkeypatch.setattr(api, "email_store", lambda: mail)
    monkeypatch.setattr(api, "availability", lambda *args: None)
    real_workspace = api.current_workspace
    monkeypatch.setattr(api, "current_workspace", lambda day, timezone, now=None:
                        real_workspace(day, timezone, now=now, store=actual))
    fake_behavior = types.ModuleType("service.assistant.behavior_runtime")
    fake_behavior.poke = lambda *args: None
    monkeypatch.setitem(sys.modules, fake_behavior.__name__, fake_behavior)
    monkeypatch.setattr(assistant_package, "behavior_runtime", fake_behavior, raising=False)
    async def publish(*args):
        pass
    # Bind the unchanged production route, without importing application startup.
    path = Path(__file__).resolve().parents[1] / "service/main.py"
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "assistant_sync_calendar")
    node.decorator_list = []
    namespace = dict(Any=object, HTTPException=HTTPException, assistant_store=actual,
        assistant_scheduler=types.SimpleNamespace(record_sync=lambda *args, **kwargs: None),
        assistant_hub=types.SimpleNamespace(publish=publish))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    app = FastAPI(); app.post("/assistant/sync/calendar")(namespace["assistant_sync_calendar"])
    diagnostics = dict(authorized=True, snapshot_started_at=NOW,
                       coverage_start=at(0), coverage_end=at(23, 59) + 60,
                       event_ids=["private diagnostic never retained"])
    try:
        actual.sync_source("reminders", [dict(source_id="r", kind="reminder", title="Synthetic work", when_ts=at(17))],
                           diagnostics=diagnostics)
        # A newer incomplete/malformed declaration must supersede a successful receipt.
        if case not in {"missing_start", "future_start", "stale_start", "short_coverage", "unavailable", "syncing"}:
            actual.sync_source("calendar", [], diagnostics=diagnostics | dict(snapshot_started_at=NOW - 1))
        if case == "missing_start": diagnostics.pop("snapshot_started_at")
        if case == "future_start": diagnostics["snapshot_started_at"] = NOW + 1
        if case == "stale_start": diagnostics["snapshot_started_at"] = NOW - 1201
        if case == "short_coverage": diagnostics["coverage_end"] = at(12)
        if case == "unavailable": diagnostics["available"] = False
        if case == "syncing": diagnostics["syncing"] = True
        with TestClient(app) as client:
            response = client.post("/assistant/sync/calendar", json=dict(source="calendar", events=[], diagnostics=diagnostics | flags))
        assert response.status_code == 200, response.text
        stored = json.loads(actual._db.execute("SELECT payload FROM today_source_sync WHERE source='calendar'").fetchone()[0])
        assert "event_ids" not in stored["diagnostics"]
        ready = case in {"current", "explicit_complete"}
        if ready:
            with TestClient(app) as client:
                for started in (NOW, NOW - 1):
                    late = client.post("/assistant/sync/calendar", json=dict(source="calendar", events=[],
                        diagnostics=diagnostics | dict(snapshot_started_at=started, partial=True)))
                    assert late.status_code == 409
                    assert json.loads(actual._db.execute("SELECT payload FROM today_source_sync WHERE source='calendar'").fetchone()[0]) == stored
        if flags:
            assert stored["available"] is ready
            assert all(type(stored["diagnostics"][key]) is bool for key in ("partial", "complete"))
            if not ready:
                assert stored["reason"] == "Source snapshot is incomplete"
        supplied = actual.today_snapshot(DAY, ZONE)
        today = today_api.snapshot(DAY, ZONE, debug=True)
        direct = daily.workspace(actual, DAY, ZONE, now=NOW)
        assert today["workspace"] == direct
        assert direct["proposed_plan"]["sources"]["calendar"]["ready"] is ready
        assert bool([b for b in direct["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]) is ready
        assert api.email_contents(DAY, ZONE, NOW)["ready"] is ready
        assert today["replay"]["expected_plan"] == build_plan(DAY, ZONE, supplied["tasks"], supplied["commitments"],
            supplied["preferences"], supplied["sources"], now=NOW)
        assert today["replay"]["sources"] == supplied["sources"]
        own = SelfIdentity(("me@example.test",), NOW - 60, True)
        preview = mail.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480), "Plan", "Body", own, now=NOW - 60)
        mail.enable(preview["token"], own, confirmed=True, now=NOW - 60)
        calls = []
        async def sender(*args):
            calls.append(args)
            return dict(ok=True, status="succeeded")
        result = asyncio.run(deliver_due(mail, identity=lambda: own, compose=api.email_contents,
            sender=sender, policy_allows=lambda _: True, now=NOW))
        assert len(calls) == int(ready)
        assert result == ("submitted" if ready else "waiting_for_sources")
    finally:
        mail.close()
        actual._db.close()
