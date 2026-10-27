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


def test_legacy_case_colliding_account_labels_fail_closed():
    publish_mail(mail("Upper account body", mid="upper", account="Work"),
                 mail("Lower account body", mid="lower", account="work"))
    result = dispatch(email_account="Work")["sources"]["email"]
    assert not result["records"] and "unknown" in result["state"]
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
    assert failed['failure_entries'] == 1
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


def v2_coverage(*inventory):
    return {'version': 2, 'account_inventory': [{'id': key, 'label': label} for key, label in inventory],
            'successful_account_ids': [key for key, _ in inventory], 'failed_account_ids': [], 'complete': True}


def test_stable_ids_admit_duplicate_labels_and_empty_inventory_accounts():
    row = {**mail(account='Google'), 'account_id': 'native-A'}
    coverage = v2_coverage(('native-A', 'Google'), ('native-B', 'Google'))
    candidate = M._raw_snapshot_candidate(email_raw([row]), coverage, STAMP)
    assert candidate.complete
    assert candidate.account_inventory == (('native-A', 'Google'), ('native-B', 'Google'))


@pytest.mark.parametrize('label', ['Google', 'google'])
def test_v2_scopes_use_full_inventory_before_message_matches(monkeypatch, label):
    monkeypatch.setattr(M, '_headers', '')
    monkeypatch.setattr(M, '_swift_headers', '')
    monkeypatch.setattr(M, '_external_accounts', {})
    raw = email_raw([{**mail(account='Google'), 'account_id': 'native-A'}])
    M.cache_raw_emails(raw, v2_coverage(('native-A', 'Google'), ('native-B', label)))
    assert len(M.raw_email_snapshot().rows) == 1
    assert len(dispatch()['sources']['email']['records']) == 1
    ambiguous = dispatch(email_account='Google')['sources']['email']
    assert not ambiguous['records'] and 'Which one?' in ambiguous['state']
    assert len(dispatch(email_account='native-A')['sources']['email']['records']) == 1
    assert not dispatch(email_account='native-B')['sources']['email']['records']
    assert not dispatch(email_account='native-C')['sources']['email']['records']
    assert not M._raw_account_scope(M._parse_raw(), 'native-B')[0]
    assert 'Which one?' in M._raw_account_scope(M._parse_raw(), 'Google')[1]
    assert 'several email accounts' in asyncio.run(M.summarize_inbox_recent(account='Google')).lower()
    assert len(M.native_email_accounts()) == 2


@pytest.mark.parametrize('change', [
    {'version': 1}, {'version': 3}, {'version': '2'}, {'version': True}, {'version': None},
    {'account_inventory': {}}, {'account_inventory': [None]},
    {'account_inventory': [{'id': 'native-A', 'label': 'Google'}, {'id': 'native-A', 'label': 'Google'}]},
    {'account_inventory': [{'id': '', 'label': 'Google'}]},
    {'account_inventory': [{'id': 'native-A', 'label': 'Google\nunsafe'}]},
    {'account_inventory': [{'id': 'native-A\x02unsafe', 'label': 'Google'}]},
    {'account_inventory': [{'id': 'native-A', 'label': 'Other'}]},
    {'successful_account_ids': []}, {'successful_account_ids': ['native-A', 'native-A']},
    {'successful_account_ids': ['native-A', 'unknown']},
    {'failed_account_ids': ['native-A']}, {'failed_account_ids': ['unknown']},
    {'successful_account_ids': [], 'failed_account_ids': ['native-A']},
    {'decode_complete': False}, {'complete': False},
])
def test_v2_rejects_malformed_or_incomplete_coverage_without_legacy_fallback(change):
    raw = email_raw([{**mail(account='Google'), 'account_id': 'native-A'}])
    envelope = {**v2_coverage(('native-A', 'Google')), **change,
                'accounts': ['Google'], 'failed_accounts': []}
    assert not M._raw_snapshot_candidate(raw, envelope, STAMP).complete


def test_v2_rows_require_native_identity_and_complete_decoding():
    first = {**mail(account='Google', mid='shared'), 'account_id': 'native-A'}
    second = {**first, 'account_id': 'native-B'}
    coverage = v2_coverage(('native-A', 'Google'), ('native-B', 'Google'))
    assert M._raw_snapshot_candidate(email_raw([first, second]), coverage, STAMP).complete
    for rows in ([first, {**first, 'body': 'conflict'}], [{**first, 'account_id': 'unknown'}],
                 [{**first, 'message_id': ''}], [{**first, 'account_id': ''}]):
        assert not M._raw_snapshot_candidate(email_raw(rows), coverage, STAMP).complete
    assert not M._raw_snapshot_candidate(email_raw([first]) + 'malformed', coverage, STAMP).complete


def test_v2_metadata_is_owned_immutable_and_failure_revokes_publication(monkeypatch):
    raw = email_raw([{**mail(account='Google'), 'account_id': 'native-A'}])
    coverage = v2_coverage(('native-A', 'Google'), ('native-B', 'Google'))
    M.cache_raw_emails(raw, coverage)
    snapshot = M.raw_email_snapshot()
    M.set_email_availability(True)
    witness = M.prepare_publication_witness(snapshot)
    assert witness is not None
    coverage['account_inventory'][0]['label'] = 'Changed'
    coverage['account_inventory'].append({'id': 'unknown', 'label': 'Injected'})
    coverage['successful_account_ids'].clear()
    assert M.raw_email_snapshot() is snapshot
    assert M.raw_reference_metadata()['account_inventory'] == (('native-A', 'Google'), ('native-B', 'Google'))
    with pytest.raises(TypeError):
        snapshot.rows[0]['account_id'] = 'other'
    assert M.prepare_publication_witness(replace(snapshot)) is None
    M.cache_raw_emails(raw, {**v2_coverage(('native-A', 'Google')), 'failed_account_ids': ['native-A']})
    assert M.raw_email_snapshot() is None
    assert M.prepare_publication_witness(snapshot) is None
    # Restart restores display bytes only: process-local inventory cannot be reused.
    monkeypatch.setattr(M, '_raw_mail_snapshot', None)
    monkeypatch.setattr(M, '_raw_reference_scan', {})
    assert M.raw_email_snapshot() is None
    assert not M.raw_reference_metadata()['complete']
    markers = 'C2\x01Google\x01native-A\x010\x010\x010\nC2\x01Google\x01native-B\x010\x010\x010\n'
    monkeypatch.setattr(M, '_headers', markers)
    monkeypatch.setattr(M, '_swift_headers', markers)
    monkeypatch.setattr(M, '_external_accounts', {})
    assert 'Which one?' in M._raw_account_scope(list(snapshot.rows), 'Google')[1]


def test_empty_v2_and_unique_legacy_remain_valid_but_ambiguous_legacy_is_blocked():
    assert M._raw_snapshot_candidate('', v2_coverage(), STAMP).complete
    assert M._raw_snapshot_candidate('', v2_coverage(('native-A', 'Google'), ('native-B', 'Google')), STAMP).complete
    assert M._raw_snapshot_candidate(email_raw([mail()]), {'accounts': ['Work'], 'failed_accounts': [], 'complete': True}, STAMP).complete
    for labels in (['Google', 'Google'], ['Google', 'google']):
        assert not M._raw_snapshot_candidate('', {'accounts': labels, 'failed_accounts': [], 'complete': True}, STAMP).complete


@pytest.mark.parametrize('message_id,native_id', [
    ('<shared@synthetic.invalid>', ''), ('', 'mail:shared'),
    ('<shared@synthetic.invalid>', 'mail:shared'),
])
@pytest.mark.parametrize('labels', [('Work', 'Work'), ('Work', 'work')])
@pytest.mark.parametrize('pipeline', ['parse', 'unique'])
def test_header_deduplication_preserves_case_distinct_native_account_ids(message_id, native_id, labels, pipeline):
    def header(account_id, label, flag='U'):
        return '\x01'.join(('H2', str(STAMP), flag, label, account_id, 'Sender',
                            'sender@synthetic.invalid', message_id, 'Same subject', native_id))
    upper = header('Native-A', labels[0])
    lower = header('native-a', labels[1])
    if pipeline == 'parse':
        actual = M._parse_header_records(upper + '\n' + lower)
    else:
        # Parse independently so a parser defect cannot hide a second dedupe defect.
        separate = M._parse_header_records(upper, snapshot='recent') + M._parse_header_records(lower, snapshot='history')
        actual = M._unique_records(separate)
    assert {item['account_id'] for item in actual} == {'Native-A', 'native-a'}
    assert len(M._parse_header_records(upper + '\n' + upper)) == 1
    same_exact_account = (M._parse_header_records(upper, snapshot='recent') +
                          M._parse_header_records(header('Native-A', labels[0], flag='R'), snapshot='history'))
    deduped = M._unique_records(same_exact_account)
    assert len(deduped) == 1 and deduped[0]['unread'] is True


@pytest.mark.parametrize('message_id,native_id', [
    ('<shared@synthetic.invalid>', ''), ('', 'mail:shared'),
    ('<shared@synthetic.invalid>', 'mail:shared'),
])
def test_header_deduplication_keeps_legacy_label_matching_case_insensitive(message_id, native_id):
    def header(label):
        return '\x01'.join(('H2', str(STAMP), 'U', label, '', 'Sender',
                            'sender@synthetic.invalid', message_id, 'Same subject', native_id))
    assert len(M._parse_header_records(header('Work') + '\n' + header('work'))) == 1
    separate = M._parse_header_records(header('Work')) + M._parse_header_records(header('work'))
    assert len(M._unique_records(separate)) == 1


@pytest.fixture
def registered_mail_display(monkeypatch):
    """Only refresh is inert; public registry, receipt, scopes and rendering run."""
    from service.publication_authority import Authority
    for name, value in {'_headers': '', '_swift_headers': '', '_history': '', '_swift_history': '',
                        '_raw_emails': '', '_swift_raw': '', '_external_accounts': {},
                        '_raw_reference_scan': {}, '_native_raw_at': 0., '_native_headers_at': 0.,
                        '_email_read_source': 'mail_app', '_email_reason': ''}.items():
        monkeypatch.setattr(M, name, value)
    monkeypatch.setattr(M, '_raw_availability', Authority('synthetic-display-availability'))
    monkeypatch.setattr(M, '_raw_publication', Authority('synthetic-display-body'))
    monkeypatch.setattr(M, '_ensure_email_cache', AsyncMock())
    clock = [STAMP]
    monkeypatch.setattr(M.time, 'time', lambda: clock[0])
    M.set_email_availability(True, read_source='mail_app')
    def publish(second_label='Work', *, complete=False, change=None):
        envelope = v2_coverage(('Native-A', 'Work'), ('Native-B', second_label))
        if not complete:
            envelope.update(successful_account_ids=['Native-A'], failed_account_ids=['Native-B'], complete=False)
        envelope.update(change or {})
        raw = email_raw([{**mail('SYNTHETIC-A-BODY', account='Work'), 'account_id': 'Native-A'}])
        M.cache_raw_emails(raw, envelope)
        return envelope
    def view(account=None, query='Project update'):
        args = {'query': query}
        if account is not None:
            args['account'] = account
        return asyncio.run(run_tool(REGISTRY['view_emails'], args))
    return publish, view, clock


@pytest.mark.parametrize('second_label', ['Work', 'work'])
@pytest.mark.parametrize('complete', [False, True])
def test_registered_display_scopes_include_failed_empty_v2_account(registered_mail_display, second_label, complete):
    publish, view, _ = registered_mail_display
    publish(second_label, complete=complete)
    scoped = view('Work')
    assert 'Which one?' in scoped and 'SYNTHETIC-A-BODY' not in scoped
    assert {item['id'] for item in M.native_email_accounts()} == {'Native-A', 'Native-B'}
    assert {'Native-A', 'Native-B'} <= set(M._known_accounts())
    assert 'SYNTHETIC-A-BODY' in view('Native-A')
    unscoped = view()
    assert 'SYNTHETIC-A-BODY' in unscoped
    failed = view('Native-B')
    assert 'SYNTHETIC-A-BODY' not in failed
    if not complete:
        assert 'incomplete' in unscoped.lower() and 'Native-B' in unscoped
        assert 'failed' in failed.lower() and 'coverage' in failed.lower() and 'unknown' in failed.lower()
        assert 'no cached email account' not in failed.lower()
        assert 'No emails matching' not in failed
        assert M.raw_email_snapshot() is None
        assert not dispatch(email_account='Work')['sources']['email']['records']
        from service.tasks.source_readers import current_mail_reader
        from service.tasks.references import SourceRef, resolve_reference
        result = resolve_reference(SourceRef('email', hints={'account': 'Native-A'}),
                                   current_mail_reader(), now=NOW)
        assert result.status == 'unavailable'
    else:
        assert M.raw_email_snapshot() is not None
        assert 'incomplete' not in unscoped.lower()
        assert 'No emails matching' in failed


@pytest.mark.parametrize('change', [
    {'version': 3}, {'successful_account_ids': ['Native-A', 'unknown']},
    {'failed_account_ids': ['Native-A', 'Native-B']},
    {'account_inventory': [{'id': 'Native-A', 'label': 'Work'}, {'id': 'Native-A', 'label': 'Work'}]},
    {'account_inventory': [{'id': 'Native-A', 'label': 'Other'}, {'id': 'Native-B', 'label': 'Work'}]},
])
def test_registered_display_cannot_infer_native_label_scope_from_malformed_v2(registered_mail_display, change):
    publish, view, _ = registered_mail_display
    publish(change=change)
    scoped = view('Work')
    assert 'SYNTHETIC-A-BODY' not in scoped
    assert 'inventory' in scoped.lower() or 'Which one?' in scoped
    assert M.raw_email_snapshot() is None and M.raw_email_inventory() is None
    assert M.native_email_accounts() == [] and M._known_accounts() == []
    assert not dispatch(email_account='Work')['sources']['email']['records']


@pytest.mark.parametrize('boundary', ['stale', 'withdrawn', 'restart'])
def test_registered_retained_display_cannot_restore_current_native_label_identity(registered_mail_display, monkeypatch, boundary):
    publish, view, clock = registered_mail_display
    publish(complete=True)
    if boundary == 'stale':
        clock[0] += 1201
    elif boundary == 'withdrawn':
        M.set_email_availability(False, reason='Synthetic Mail scan unavailable')
    else:
        monkeypatch.setattr(M, '_raw_mail_snapshot', None)
        monkeypatch.setattr(M, '_raw_reference_scan', {})
        monkeypatch.setattr(M, '_email_available', None)
        monkeypatch.setattr(M, '_email_read_source', '')
    scoped = view('Work')
    assert 'SYNTHETIC-A-BODY' not in scoped
    assert 'inventory' in scoped.lower() or 'Which one?' in scoped
    assert 'SYNTHETIC-A-BODY' in view('Native-A')
    unscoped = view()
    assert 'SYNTHETIC-A-BODY' in unscoped and ('unverified' in unscoped or 'unknown' in unscoped)
    assert M.raw_email_snapshot() is None and M.raw_email_inventory() is None
    assert M.native_email_accounts() == [] and M._known_accounts() == []
    assert not dispatch(email_account='Native-A')['sources']['email']['records']


def test_registered_all_failed_new_inventory_does_not_authorize_old_label_scope(registered_mail_display):
    publish, view, _ = registered_mail_display
    publish(complete=True)
    envelope = {**v2_coverage(('Native-C', 'Home')), 'successful_account_ids': [],
                'failed_account_ids': ['Native-C'], 'complete': False}
    M.cache_raw_emails('', envelope)
    inventory = M.raw_email_inventory()
    assert inventory.account_inventory == (('Native-C', 'Home'),)
    assert 'SYNTHETIC-A-BODY' not in view('Work')
    retained = view('Native-A')
    assert 'SYNTHETIC-A-BODY' in retained and 'unverified' in retained
    failed = view('Native-C')
    assert 'failed' in failed and 'coverage' in failed and 'unknown' in failed
    assert M.raw_email_snapshot() is None
    assert M.prepare_publication_witness(inventory) is None


def test_registered_zero_account_capture_replaces_identity_without_restoring_display(registered_mail_display):
    publish, view, _ = registered_mail_display
    publish()
    previous = M.raw_email_inventory()
    M.cache_raw_emails('', v2_coverage())
    current = M.raw_email_inventory()
    assert current is not None and current.account_inventory == () and current.generation > previous.generation
    assert M.native_email_accounts() == [] and M._known_accounts() == []
    assert 'SYNTHETIC-A-BODY' not in view()
    assert 'No emails matching' in view()
    assert M.raw_email_snapshot() is not None


def test_registered_provider_alias_display_remains_available_when_bound_native_scan_fails(registered_mail_display, monkeypatch):
    publish, view, _ = registered_mail_display
    publish()
    address = 'provider@synthetic.invalid'
    raw = email_raw([{**mail('SYNTHETIC-PROVIDER-BODY', account=address), 'account_id': address}])
    monkeypatch.setattr(M, '_external_accounts', {address: {
        'mail_account_id': 'Native-B', 'mail_account_name': 'Work', 'raw': raw,
        'raw_at': STAMP, 'raw_verified': True, 'raw_error': ''}})
    M._recompose_all()
    assert 'Which one?' in view('Work')
    provider = view(address)
    assert 'SYNTHETIC-PROVIDER-BODY' in provider and 'scan failed' not in provider
    assert 'SYNTHETIC-A-BODY' not in provider
    assert 'SYNTHETIC-PROVIDER-BODY' in view('Native-B')
    assert M.raw_email_snapshot() is None


def test_registered_partial_distinct_label_and_legacy_controls(registered_mail_display):
    publish, view, _ = registered_mail_display
    publish('Home')
    assert 'SYNTHETIC-A-BODY' in view('Work')
    assert 'failed' in view('Home') and 'unknown' in view('Home')
    raw = email_raw([{**mail('SYNTHETIC-A-BODY', account='Work'), 'account_id': 'Native-A'}])
    M.cache_raw_emails(raw, {'accounts': ['Work'], 'failed_accounts': [], 'complete': True})
    assert 'SYNTHETIC-A-BODY' in view('Work') and M.raw_email_snapshot() is not None
    M.cache_raw_emails(raw, {'accounts': ['Work', 'work'], 'failed_accounts': [], 'complete': True})
    assert 'SYNTHETIC-A-BODY' not in view('Work') and M.raw_email_snapshot() is None



def test_registered_partial_identity_receipt_is_immutable_and_never_body_evidence(registered_mail_display):
    publish, view, _ = registered_mail_display
    envelope = publish()
    inventory = M.raw_email_inventory()
    assert inventory.generation == M._raw_mail_snapshot.generation and inventory.synced_at == STAMP
    envelope['account_inventory'][1]['label'] = 'Home'
    envelope['failed_account_ids'].clear()
    envelope['successful_account_ids'].append('Native-B')
    assert M.raw_email_inventory().account_inventory == (('Native-A', 'Work'), ('Native-B', 'Work'))
    assert M.raw_email_inventory().failed_account_ids == ('Native-B',)
    assert 'Which one?' in view('Work')
    with pytest.raises(AttributeError):
        inventory.failed_account_ids += ('injected',)
    assert M.raw_email_snapshot() is None
    assert M.prepare_publication_witness(inventory) is None


def test_long_newsletter_body_does_not_crowd_out_other_mail():
    # Debug export 2026-10-26: one 1.5k-char newsletter filled the whole mail
    # budget, so 1 of 50 cached emails was shown across two accounts.
    footer = "You are receiving this email because you opted in.  Unsubscribe.\n" * 30
    rows = [mail(footer, mid="promo", account="Home", ts=STAMP - 60)]
    rows += [mail(f"Can you confirm slot {i}?", mid=f"ask{i}", account=("Work", "Home")[i % 2],
                  ts=STAMP - 3600 * (i + 1)) for i in range(8)]
    publish_mail(*rows)
    email = dispatch(include_calendar=False, include_reminders=False, include_messages=False,
                     include_notes=False)["sources"]["email"]
    records = {row["message_id"]: row for row in email["records"]}
    assert email["matching"] == 9 and email["shown"] == 9 and email["omitted"] == 0
    assert {row["account_label"] for row in records.values()} == {"Work", "Home"}
    promo = records["promo"]
    assert promo["body_truncated"] is True and promo["body_chars"] == len(footer.strip())
    assert len(promo["body"]) <= T._EMAIL_BODY_CHARS + 1 and promo["body"].endswith("…")
    assert records["ask3"]["body"] == "Can you confirm slot 3?" and "body_truncated" not in records["ask3"]
    assert records["ask3"]["unread"] is True
