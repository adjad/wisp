"""Real collector dispatch over fictional coherent caches; no inference/effects."""
import asyncio
from dataclasses import replace
import datetime as dt
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest

from service.tools import task_evidence as T, everyday as E, email_tools as M, imessage_tools as I, notes_tools as N
from service.tools.registry import REGISTRY, run_tool
from tests.fixtures.wire import email_raw
from service.assistant.store import AssistantStore, parse_undated_snapshot
from service.tools import assistant_tools

from service.main import _agent_memory_context_allowed as _real_memory_context_allowed
from tests.test_workflow20_knowledge import knowledge_entrypoint, storage

NOW = dt.datetime(2026, 10, 12, 8)
STAMP = NOW.timestamp()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(time, "time", lambda: STAMP)
    monkeypatch.setattr(M, "_raw_mail_snapshot", None)
    monkeypatch.setattr(M, "_raw_snapshot_generation", 0)
    monkeypatch.setattr(M, "_email_available", True)
    monkeypatch.setattr(M.cache_store, "save", Mock())
    monkeypatch.setattr(I, "_lines", "")
    monkeypatch.setattr(I, "_available", True)
    monkeypatch.setattr(I, "_sync_completed", True)
    monkeypatch.setattr(I, "_unavailable_reason", "")
    for name, value in {"_notes": "", "_notes_at": 0., "_snapshot_at": 0., "_snapshot_started_at": 0.,
            "_received_at": 0., "_receipt_started": 0., "_receipt_available": None, "_receipt_reason": "",
            "_available": None, "_notes_origin": "none", "_notes_generation": 0}.items():
        monkeypatch.setattr(N, name, value)
    store = AssistantStore(tmp_path / "synthetic.db")
    monkeypatch.setattr(assistant_tools, "assistant_store", store)
    store.sync_source("calendar", [{"source_id": "cal.fixture", "title": "Fictional review", "kind": "event",
        "when_ts": STAMP + 3600, "end_ts": STAMP + 7200}], diagnostics={"snapshot_started_at": STAMP,
        "coverage_start": STAMP - 86400, "coverage_end": STAMP + 62 * 86400})
    store.sync_source("reminders", [{"source_id": "rem.fixture", "title": "Fictional form", "kind": "reminder",
        "when_ts": STAMP + 86400}, {"source_id": "overdue", "title": "Overdue fictional task", "kind": "reminder", "when_ts": STAMP - 60}],
        diagnostics={"snapshot_started_at": STAMP}, undated=parse_undated_snapshot([
            {"source_id": "undated", "title": "Undated fictional task", "context": "Work"}], 1))
    def forbidden(*args, **kwargs): raise AssertionError("no model/native/network effects")
    import socket
    import subprocess
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(I, "_c", forbidden)
    from service.assistant.hub import hub
    import importlib
    monkeypatch.setattr(hub, "publish", AsyncMock(side_effect=forbidden))
    monkeypatch.setattr(importlib.import_module("service.assistant.outbox"), "request", AsyncMock(side_effect=forbidden))
    yield store
    store._db.close()


def mail(body="Please submit the blue form tomorrow.", *, mid="one", account="Work", ts=STAMP - 86400 * 5):
    return {"ts": ts, "unread": True, "account": account, "account_id": "native." + account,
        "sender_name": "Maya", "sender_addr": "maya@synthetic.invalid", "to": ["self@synthetic.invalid"],
        "subject": "Project update", "message_id": mid, "body": body}


def publish_mail(*rows):
    M.cache_raw_emails(email_raw(list(rows)), {"accounts": list(dict.fromkeys(row["account"] for row in rows)),
        "failed_accounts": [], "complete": True})


def publish_messages(monkeypatch, *items):
    coverage = {"version": 1, "kind": "coverage", "attempted": len(items), "emitted": len(items),
        "skipped": 0, "truncated": 0, "limit": 2000, "byte_limit": 300000, "window_days": 7,
        "row_limit": 2000, "reached_row_limit": False}
    records = []
    for index, item in enumerate(items):
        guid, chat = str(index), item.get("chat", "chat.fixture")
        records.append({"version": 1, "kind": "record", "record": {"guid": guid, "conversation": chat,
            "sender": item.get("sender", "Maya"), "direction": item.get("direction", "incoming"),
            "timestamp": item.get("timestamp"), "text": item["text"], "links": []},
            "source": {"kind": "messages", "message_guid": guid, "chat_guid": chat, "navigation_url": None},
            "coverage": {"text": "complete", "links": "complete"}})
    monkeypatch.setattr(I, "_lines", "\n".join("V3 | " + json.dumps(row) for row in [coverage, *records]))


def dispatch(**args):
    assert REGISTRY["collect_task_evidence"].func is T.collect_task_evidence
    result = asyncio.run(run_tool(REGISTRY["collect_task_evidence"], args))
    assert len(result) <= 11_000
    return json.loads(result)


def test_old_received_body_is_not_filtered_by_task_window():
    publish_mail(mail())
    result = dispatch(period="tomorrow")
    row = result["sources"]["email"]["records"][0]
    assert row["body"] == "Please submit the blue form tomorrow."
    assert row["received_at"] == STAMP - 5 * 86400
    assert row["message_id"] == "one" and row["account_id"] == "native.Work"
    assert result["sources"]["email"]["receipt_date_filter_applied"] is False
    assert result["task_window"]["start"] == NOW.replace(hour=0).timestamp() + 86400
    assert result["sources"]["calendar"]["snapshot_started_at"] == STAMP
    reminders = result["sources"]["reminders"]["records"]
    assert {row["source_id"] for row in reminders} == {"rem.fixture", "overdue", "undated"}
    assert next(row for row in reminders if row["source_id"] == "undated")["when_ts"] is None


def test_clock_and_source_dates_are_separate_and_absolute(monkeypatch):
    publish_mail(mail('Submit on October 20, 2026.', ts=STAMP - 86400 * 5))
    publish_messages(monkeypatch, {'text':'Send it tomorrow.', 'timestamp':'2026-10-11T12:00:00Z'},
                     {'text':'Tomorrow has no known anchor.', 'timestamp':None})
    data = dispatch(period='tomorrow')
    assert data['reference_time']['epoch'] == STAMP
    assert data['reference_time']['local_date'] == NOW.date().isoformat()
    assert data['task_window']['start_date'] == (NOW.date() + dt.timedelta(days=1)).isoformat()
    assert data['sources']['email']['records'][0]['received_at_local_iso'] == T._local_iso(STAMP - 86400 * 5)
    rows = data['sources']['messages']['records']
    assert dt.datetime.fromisoformat(rows[0]['original_timestamp']) == dt.datetime.fromisoformat('2026-10-11T12:00:00Z')
    assert rows[0]['original_timestamp_local_iso'] == T._local_iso('2026-10-11T12:00:00Z')
    assert rows[1]['original_timestamp_local_iso'] is None
    assert all(source['coverage_summary']['absence_supported'] is False for source in data['sources'].values())


@pytest.mark.parametrize('instant', ['2026-10-19T23:59:59-07:00',
                                    '2026-10-20T00:00:01-07:00',
                                    '2026-11-01T01:30:00-07:00',
                                    '2026-11-01T01:30:00-08:00'])
def test_reference_clock_is_one_capture_at_midnight_and_dst(monkeypatch, instant):
    monkeypatch.setenv('TZ', 'America/Los_Angeles')
    time.tzset()
    try:
        epoch = dt.datetime.fromisoformat(instant).timestamp()
        clock = Mock(return_value=epoch)
        monkeypatch.setattr(T.time, 'time', clock)
        data = dispatch(period='tomorrow', include_calendar=False, include_reminders=False,
                        include_email=False, include_messages=False, include_notes=False)
        assert data['reference_time']['local_iso'] == instant
        assert data['task_window']['start_date'] == (dt.date.fromisoformat(instant[:10]) + dt.timedelta(days=1)).isoformat()
    finally:
        monkeypatch.undo()
        time.tzset()


def test_same_headers_different_bodies_preserve_cancellation_and_source_ids():
    publish_mail(mail("Please submit the old form tomorrow.", mid="old"),
        mail("The old form is cancelled. Submit the new form next week.", mid="new", ts=STAMP - 1))
    result = dispatch()
    rows = result["sources"]["email"]["records"]
    assert [row["message_id"] for row in rows] == ["new", "old"]
    assert "cancelled" in rows[0]["body"] and "old form" in rows[1]["body"]
    assert all("unknown" in row["direction"] for row in rows)


def test_messages_direction_group_owner_and_missing_relative_anchor_preserved(monkeypatch):
    publish_messages(monkeypatch,
        {"text": "I will send the form tomorrow.", "direction": "outgoing", "timestamp": "2026-10-11T12:00:00Z"},
        {"text": "Jordan will bring the projector tomorrow.", "sender": "Casey", "timestamp": None},
        {"text": "Cancel the earlier projector plan.", "timestamp": "2026-10-12T12:00:00Z"})
    result = dispatch()
    rows = result["sources"]["messages"]["records"]
    assert len(rows) == 3
    assert rows[0]["direction"] == "outgoing" and rows[0]["message_guid"] == "0"
    assert rows[1]["original_timestamp"] is None and "Jordan" in rows[1]["text"]
    assert "Cancel" in rows[2]["text"]
    assert result["sources"]["messages"]["freshness"].startswith("unknown")
    assert any("missing original timestamps" in value for value in result["limitations"])


def test_exact_account_chat_and_content_filters_do_not_broaden(monkeypatch):
    publish_mail(mail("blue form", mid="work"), mail("blue form", mid="home", account="Home"),
                 mail("red form", mid="red"))
    publish_messages(monkeypatch, {"text": "blue form", "chat": "chat.one"},
        {"text": "blue form", "chat": "chat.two"}, {"text": "red form", "chat": "chat.one"})
    result = dispatch(email_account="Work", chat_id="chat.one", query="blue")
    assert [row["message_id"] for row in result["sources"]["email"]["records"]] == ["work"]
    assert [row["message_guid"] for row in result["sources"]["messages"]["records"]] == ["0"]
    result = dispatch(email_account="Missing", chat_id="missing")
    assert result["sources"]["email"]["records"] == result["sources"]["messages"]["records"] == []
    assert "unknown" in result["sources"]["email"]["state"]


def test_case_colliding_account_labels_select_one_exact_identity():
    publish_mail(mail("Upper account body", mid="upper", account="Work"),
                 mail("Lower account body", mid="lower", account="work"))
    result = dispatch(email_account="Work")["sources"]["email"]
    assert [row["message_id"] for row in result["records"]] == ["upper"]
    assert [row["account_id"] for row in result["records"]] == ["native.Work"]
    result = dispatch(email_account="WORK")["sources"]["email"]
    assert not result["records"] and "unknown" in result["state"]


def test_native_source_rows_and_receipt_are_one_transaction_and_keep_occurrences(isolated):
    with isolated.transaction() as db:
        original = dict(db.execute("SELECT * FROM commitments WHERE source='calendar'").fetchone())
        columns = list(original)
        duplicate = {**original, "id": "distinct-calendar", "source_id": "distinct-native"}
        db.execute("INSERT INTO commitments (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                   [duplicate[key] for key in columns])
        db.execute("INSERT INTO calendar_event_ends(commitment_id,end_ts) VALUES (?,?)", ("distinct-calendar", STAMP + 7200))
    trace = []
    isolated._db.set_trace_callback(trace.append)
    result = dispatch(include_email=False, include_messages=False, include_notes=False, include_reminders=False)
    rows = result["sources"]["calendar"]["records"]
    assert {row["source_id"] for row in rows} == {"cal.fixture", "distinct-native"}
    assert result["sources"]["calendar"]["native_completeness"].startswith("unknown")
    assert trace[0] == "BEGIN" and trace[-1] == "COMMIT"
    assert not any("'reminders'" in sql for sql in trace)


def test_partial_native_receipt_never_certifies_new_rows(isolated):
    with isolated.transaction() as db:
        payload = json.loads(db.execute("SELECT payload FROM today_source_sync WHERE source='calendar'").fetchone()[0])
        payload["complete"] = False
        db.execute("UPDATE today_source_sync SET payload=? WHERE source='calendar'", (json.dumps(payload),))
        db.execute("UPDATE commitments SET title='New partial read' WHERE source='calendar'")
    result = dispatch()["sources"]["calendar"]
    assert result["records"] == [] and result["withheld_unverified"] == 1
    assert result["state"].startswith("unknown")


def test_native_scan_cap_and_query_counts_are_separate(isolated):
    with isolated.transaction() as db:
        original = dict(db.execute("SELECT * FROM commitments WHERE source='calendar'").fetchone())
        columns = list(original)
        sql = "INSERT INTO commitments (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
        rows = []
        for index in range(5001):
            row = {**original, "id": f"cap-{index}", "source_id": f"native-{index}",
                   "title": "Rare target" if index == 5000 else "Unrelated",
                   "when_ts": STAMP + 3601 + index}
            rows.append([row[key] for key in columns])
        db.executemany(sql, rows)
    result = dispatch(query="Rare target", include_email=False, include_messages=False,
                      include_notes=False, include_reminders=False)["sources"]["calendar"]
    assert result["dated_window_cached"] == 5002
    assert result["inspected_dated"] == 5000
    assert result["matching_dated"] == result["matching"] == result["shown"] == 1
    assert result["upstream_uninspected"] == 2
    assert result["native_completeness"].startswith("unknown")


def test_notes_matching_omissions_are_separate_from_uninspected_carrier():
    N.cache_notes(N._RS.join((N._FS.join((str(STAMP), "Target", "Tasks", "Exact task")),
                              N._FS.join((str(STAMP), "Other", "Tasks", "z" * 100_001)))))
    result = dispatch(query="Exact")["sources"]["notes"]
    assert result["matching"] == result["shown"] == 1 and result["omitted"] == 0
    assert result["upstream_uninspected"] == 1


@pytest.mark.parametrize("problem", ["stale", "missing", "partial", "unavailable"])
def test_ineligible_mail_snapshot_is_unknown_not_header_substitution(monkeypatch, problem):
    publish_mail(mail())
    if problem == "stale": monkeypatch.setattr(M, "_raw_mail_snapshot", replace(M._raw_mail_snapshot, synced_at=STAMP - 1201))
    if problem == "missing": monkeypatch.setattr(M, "_raw_mail_snapshot", None)
    if problem == "partial": monkeypatch.setattr(M, "_raw_mail_snapshot", replace(M._raw_mail_snapshot, complete=False))
    if problem == "unavailable": monkeypatch.setattr(M, "_email_available", False)
    result = dispatch()["sources"]["email"]
    assert result["records"] == [] and "unknown" in result["state"]
    expected = {'stale':'snapshot_stale', 'missing':'missing_snapshot',
                'partial':'snapshot_incomplete', 'unavailable':'source_unavailable'}[problem]
    assert expected in result['eligibility_reason_codes']


def test_mail_diagnostic_is_coherent_and_does_not_relax_identity_admission(monkeypatch):
    publish_mail(mail('PRIVATE BODY', mid=''))
    diagnostics = {}
    assert M.raw_email_snapshot(diagnostics=diagnostics) is None
    assert diagnostics['eligible'] is False
    assert 'identity_incomplete' in diagnostics['reason_codes']
    assert 'PRIVATE BODY' not in json.dumps(diagnostics)
    assert 'Synthetic' not in json.dumps(diagnostics)
    evidence = dispatch()['sources']['email']
    assert not evidence['records'] and 'identity_incomplete' in evidence['eligibility_reason_codes']
    rendered = T.render_coverage_text(json.dumps({'task_window':{},'sources':{'email':evidence}}))
    assert 'some messages could not be verified' in rendered


def test_mail_diagnostics_keep_original_generation_and_age_checks(monkeypatch):
    publish_mail(mail())
    snapshot = M.raw_email_snapshot()
    diagnostics = {}
    assert M.raw_email_snapshot(expected_generation=snapshot.generation, diagnostics=diagnostics) is snapshot
    assert diagnostics == {'eligible':True, 'reason_codes':[]}
    assert M.raw_email_snapshot(expected_generation=snapshot.generation + 1, diagnostics=diagnostics) is None
    assert diagnostics['reason_codes'] == ['generation_mismatch']
    monkeypatch.setattr(M, '_raw_mail_snapshot', replace(snapshot, synced_at=STAMP + 1))
    assert M.raw_email_snapshot(diagnostics=diagnostics) is None
    assert diagnostics['reason_codes'] == ['snapshot_clock_invalid']


def test_body_scan_status_is_safe_coherent_and_does_not_admit_failed_cache():
    publish_mail(mail('PRIVATE BODY'))
    first = M.raw_email_scan_status()
    assert first['eligible'] and first['generation'] == 1
    failures = [{'stage': 'body_scan', 'code': -1712, 'account': 'PRIVATE ACCOUNT', 'error': 'PRIVATE ERROR'},
                {'stage': 'PRIVATE BODY', 'code': -1743}, {'stage': 'body_scan', 'code': 'PRIVATE TOKEN'},
                {'stage': 'body_scan', 'code': -999999}, {'stage': 'body_scan', 'code': True},
                {'stage': ['PRIVATE'], 'code': -1743}, ['PRIVATE'], None]
    M.cache_raw_emails('', {'accounts': [], 'failed_accounts': ['PRIVATE ACCOUNT'],
        'complete': False, 'failure_diagnostics': failures})
    failures[0]['code'] = -1743
    failed = M.raw_email_scan_status()
    assert failed['generation'] == 2 and failed['received_at'] == STAMP
    assert not failed['eligible'] and not failed['complete']
    assert failed['failed_accounts'] == 1
    assert failed['failures'] == [{'stage': 'body_scan', 'code': -1712}, {'stage': 'body_scan', 'code': 0}]
    assert M.raw_email_snapshot() is None
    assert 'PRIVATE' not in json.dumps(failed) and 'synthetic' not in json.dumps(failed)
    publish_mail(mail('NEXT PRIVATE BODY'))
    recovered = M.raw_email_scan_status()
    assert recovered['eligible'] and recovered['generation'] == 3 and recovered['failures'] == []
    assert first['generation'] == 1 and failed['generation'] == 2


def test_body_scan_status_missing_and_stale_are_unverified(monkeypatch):
    assert M.raw_email_scan_status()['generation'] is None
    assert not M.raw_email_scan_status()['eligible']
    publish_mail(mail())
    monkeypatch.setattr(M, '_raw_mail_snapshot', replace(M._raw_mail_snapshot, synced_at=STAMP - 1201))
    assert M.raw_email_scan_status()['reason_codes'] == ['snapshot_stale']
    assert not M.raw_email_scan_status()['eligible']


@pytest.mark.parametrize("source", ["calendar", "reminders", "email", "messages", "notes"])
def test_excluded_source_is_never_accessed(monkeypatch, isolated, source):
    target, name = {"calendar": (E, "_daily_calendar"), "reminders": (E, "_daily_reminders"),
        "email": (M, "raw_email_snapshot"), "messages": (I, "structured_messages_snapshot"),
        "notes": (N, "raw_notes_snapshot")}[source]
    monkeypatch.setattr(target, name, Mock(side_effect=AssertionError("excluded source read")))
    trace = []
    isolated._db.set_trace_callback(trace.append)
    result = dispatch(**{f"include_{source}": False})
    assert source not in result["sources"] and source in result["excluded_sources"]
    if source in {"calendar", "reminders"}:
        assert not any(f"'{source}'" in sql for sql in trace)


@pytest.mark.parametrize("args", [{"period": "someday"}, {"period": "last year"}, {"period": "this year"},
    {"period": None}, {"include_email": "no"}, {"email_account": 5}, {"chat_id": "x" * 201}, {"query": True}])
def test_invalid_or_unsupported_scope_fails_before_any_source(monkeypatch, isolated, args):
    for target, name in ((E, "_daily_calendar"), (E, "_daily_reminders"), (M, "raw_email_snapshot"),
                         (I, "structured_messages_snapshot"), (N, "raw_notes_snapshot")):
        monkeypatch.setattr(target, name, Mock(side_effect=AssertionError("no source read")))
    trace = []
    isolated._db.set_trace_callback(trace.append)
    assert asyncio.run(T.collect_task_evidence(**args)).startswith("(error:")
    assert trace == []


def test_immutable_mail_generation_consumed_once_even_if_next_publish_lands(monkeypatch):
    publish_mail(mail("Original exact evidence."))
    original = M.raw_email_snapshot
    snapshot = original()
    def read(**kwargs):
        publish_mail(mail("Replacement body.", mid="replacement"))
        return snapshot
    accessor = Mock(side_effect=read)
    monkeypatch.setattr(M, "raw_email_snapshot", accessor)
    result = dispatch()["sources"]["email"]
    assert accessor.call_count == 1 and result["generation"] == snapshot.generation
    assert result["records"][0]["body"] == "Original exact evidence."
    assert result["raw_digest"] == snapshot.raw_digest


def test_body_and_result_budgets_disclose_omitted_evidence(monkeypatch, isolated):
    publish_mail(*(mail("Item " + str(n) + "z" * 300, mid=str(n), ts=STAMP - n) for n in range(40)))
    with isolated.transaction() as db:
        for n in range(40):
            db.execute("UPDATE commitments SET title=? WHERE source='calendar'", ("Long calendar evidence " + "z" * 2000,))
            db.execute("UPDATE commitments SET title=? WHERE source='reminders'", ("Long reminder evidence " + "z" * 2000,))
    result = dispatch()
    assert result["sources"]["email"]["omitted"] > 0
    assert result["sources"]["email"]["matching"] == 40
    assert result["sources"]["calendar"]["omitted"] > 0
    assert result["sources"]["reminders"]["omitted"] > 0


def test_notes_original_text_is_background_not_current_task_status():
    N.cache_notes(N._FS.join((str(STAMP), "Work", "Tasks", "Jordan's task: draft the old plan.\nCancelled.")))
    result = dispatch()["sources"]["notes"]
    assert result["records"][0]["body"] == "Jordan's task: draft the old plan.\nCancelled."
    assert result["records"][0]["native_id"] is None
    assert "unknown" in result["coverage"]


def test_all_sources_excluded_has_no_reads(monkeypatch, isolated):
    for target, name in ((E, "_daily_calendar"), (E, "_daily_reminders"), (M, "raw_email_snapshot"),
                         (I, "structured_messages_snapshot"), (N, "raw_notes_snapshot")):
        monkeypatch.setattr(target, name, Mock(side_effect=AssertionError("no source read")))
    trace = []
    isolated._db.set_trace_callback(trace.append)
    result = dispatch(**{f"include_{source}": False for source in T._SOURCES})
    assert result["sources"] == {} and set(result["excluded_sources"]) == set(T._SOURCES)
    assert trace == []


@pytest.mark.parametrize('source', ['notes', 'reminders'])
def test_priority_http_excluded_sources_cannot_enter_through_history_or_memory(knowledge_entrypoint, monkeypatch, source):
    from service import main
    from service.memory import context, prompt_blocks
    state = knowledge_entrypoint
    monkeypatch.setattr(context, 'store', state.sessions)
    monkeypatch.setattr(main, '_agent_memory_context_allowed', _real_memory_context_allowed)
    sid = state.sessions.create_session()
    old = 'EXCLUDED_OLD_MAIL_0a1b'
    remembered = 'EXCLUDED_MAIL_MEMORY_1b2c'
    state.sessions.add_turn(sid, 'user', 'Read mail earlier.')
    state.sessions.add_turn(sid, 'assistant', old, tool_digest='view_emails')
    memory_calls = []
    def memory(**kw):
        memory_calls.append(kw)
        return remembered
    monkeypatch.setattr(prompt_blocks, 'memory_block', memory)
    N.cache_notes(N._FS.join((str(STAMP), 'Fictional work', 'Work', 'Current fictional work item')))
    events = state.run('What should I work on today using ' + source + ' only',
        [{'content':'Synthetic priorities based on current selected evidence.'}], sid=sid, transport='asgi')
    assert [name for name, _ in state.tools] == ['collect_task_evidence']
    assert state.tools[0][1]['include_email'] is False
    model_input = json.dumps(state.clients[0].calls)
    assert old not in model_input and remembered not in model_input
    assert memory_calls == []
    assert not any(e['type'] in {'task_plan', 'workflow', 'confirm', 'error'} for e in events)


def test_period_word_passed_as_query_is_not_a_content_filter(isolated):
    # Ling V3 re-called the tool with query="tomorrow" (2026-10-14); the literal
    # filter then matched no title and "no calendar occurrence was found".
    base = dispatch(include_email=False, include_messages=False, include_notes=False)
    for word in ("tomorrow", "Today", "this week", "tmrow"):
        result = dispatch(query=word, include_email=False, include_messages=False, include_notes=False)
        assert result["sources"]["calendar"]["records"] == base["sources"]["calendar"]["records"] != []
        assert result["sources"]["reminders"]["records"] == base["sources"]["reminders"]["records"]
        assert result["scope"]["query"] == "" and word.strip() in result["scope"]["query_ignored"]
    real = dispatch(query="Fictional review", include_email=False, include_messages=False,
                    include_notes=False)["sources"]
    assert [row["title"] for row in real["calendar"]["records"]] == ["Fictional review"]
    assert "query_ignored" not in dispatch(query="Fictional review")["scope"]


def test_records_carry_local_times_so_the_model_never_converts_epochs(isolated):
    row = dispatch(include_email=False, include_messages=False, include_notes=False)["sources"]["calendar"]["records"][0]
    assert row["when_local"] and row["end_local"] and not row["when_local"].isdigit()
