"""Preparation only: disposable child, fake inference, no native/user data."""
import asyncio
import datetime
import json
from pathlib import Path
import time

import pytest

from scripts import run_workflow20_model_pilot as pilot


def payload(prompt="Hello there"):
    return {"case_id": "offline-control", "turns": [{"role": "user", "text": prompt}],
            "scenario": {"clock": "2026-10-10T12:00:00-07:00", "timezone": "America/Los_Angeles", "sources": {}},
            "transport": [{"message": {"role": "assistant", "content": "Hello from the synthetic transport."}}]}


def test_execution_projection_has_no_oracle_or_expectations():
    case = {"id": "x", "turns": [{"role": "user", "text": "hello"}], "expect": {"secret": "oracle"}}
    scenario = {**payload()["scenario"], "facts": {"oracle.secret": {"value": "do not inject"}}}
    projected = pilot.execution_input(case, scenario)
    assert "expect" not in projected and "facts" not in projected["scenario"]
    assert "oracle" not in json.dumps(projected)
    pilot.validate_input(projected)


@pytest.mark.parametrize("field", ["facts", "expect", "grant", "resource_lease", "expected_tool"])
def test_unknown_execution_fields_are_rejected(field):
    data = payload()
    data[field] = {}
    with pytest.raises(ValueError, match="Unknown execution input"):
        pilot.validate_input(data)


@pytest.mark.parametrize("limit", [0, 3, True, 1.0, "2"])
def test_nested_model_budget_cannot_expand(limit):
    data = payload()
    data["max_calls"] = limit
    with pytest.raises(ValueError, match="budget"):
        pilot.validate_input(data)


def test_nested_chat_and_stream_share_one_counter():
    ledger = pilot.Ledger()
    transport = pilot.FakeTransport([{"message": {"content": "a"}}, {"message": {"content": "b"}}], ledger, 2)
    async def run():
        assert (await transport.message("chat", "fake", []))["content"] == "a"
        assert (await transport.message("stream_events", "fake", []))["content"] == "b"
        with pytest.raises(pilot.BoundaryViolation, match="budget"):
            await transport.message("chat", "fake", [])
    asyncio.run(run())
    assert transport.total_calls == 3
    assert ledger.events[-1]["surface"] == "nested_transport_budget"


def test_approval_is_exact_single_use_and_ordered():
    ledger = pilot.Ledger()
    args = {"path": "/synthetic/file", "content": "body"}
    with pytest.raises(pilot.BoundaryViolation):
        ledger.claim("write_file", args)
    ledger.accepted("request", "action", "write_file", args, True)
    args["content"] = "mutated"
    with pytest.raises(pilot.BoundaryViolation):
        ledger.claim("write_file", args)
    approved = ledger.claim("write_file", {"path": "/synthetic/file", "content": "body"})
    effect = ledger.emit("synthetic_effect_invoked")
    assert approved["sequence"] < effect["sequence"]
    with pytest.raises(pilot.BoundaryViolation):
        ledger.claim("write_file", {"path": "/synthetic/file", "content": "body"})


def test_denial_does_not_create_effect_authority():
    ledger = pilot.Ledger()
    ledger.accepted("r", "a", "write_file", {}, False)
    with pytest.raises(pilot.BoundaryViolation):
        ledger.claim("write_file", {})


def test_sqlite_native_open_requires_disposable_path(tmp_path):
    import sqlite3
    ledger = pilot.Ledger()
    with pilot.sqlite_boundary(tmp_path.resolve(), ledger):
        connection = sqlite3.connect(tmp_path / "synthetic.sqlite3")
        connection.close()
        for path in [tmp_path.parent / "outside.sqlite3", "file:outside?mode=ro", ":memory:"]:
            with pytest.raises(pilot.BoundaryViolation):
                sqlite3.connect(path)
        with pytest.raises(pilot.BoundaryViolation):
            sqlite3.connect(str(tmp_path / "inside"), uri=True)
    assert not (tmp_path.parent / "outside.sqlite3").exists()


def test_domain_clock_does_not_freeze_stdlib_or_residency(monkeypatch):
    import types
    import sys
    module = types.ModuleType("service.tasks.temporal")
    module.datetime = datetime.datetime
    module.dt = datetime
    monkeypatch.setitem(sys.modules, module.__name__, module)
    before = time.time()
    real_datetime = datetime.datetime
    with pilot.domain_clock(payload()["scenario"]):
        assert module.datetime.now().isoformat() == "2026-10-10T12:00:00"
        assert module.dt.datetime.now().isoformat() == "2026-10-10T12:00:00"
        assert datetime.datetime is real_datetime
        assert time.time() >= before
        assert time.monotonic() > 0
    assert module.datetime is real_datetime and module.dt is datetime


@pytest.mark.parametrize("timeout", [0, -1, 31, True, "30"])
def test_watchdog_limit_cannot_expand(timeout):
    with pytest.raises(ValueError, match="deadline"):
        pilot.run_offline(payload(), timeout=timeout)


def test_live_mode_always_refuses():
    with pytest.raises(SystemExit) as exc:
        pilot.main(["live"])
    assert exc.value.code == 2


def test_manifest_does_not_run_case_or_model(tmp_path, capsys, monkeypatch):
    packet = tmp_path / "packet.json"
    packet.write_text(json.dumps({"cases": [{"id": "one", "expect": {"oracle": True}}]}))
    monkeypatch.setattr(pilot, "run_offline", lambda *a, **kw: pytest.fail("manifest executed a case"))
    assert pilot.main(["prepare", "--packet", str(packet)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["model_calls"] == 0 and result["runtime_enabled"] is False
    assert result["release_authorized"] is False


def test_suite_has_external_wall_and_count_caps(monkeypatch):
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(pilot.time, "monotonic", lambda: next(ticks))
    calls = []
    def fake(item, *, timeout):
        calls.append(timeout)
        return {"preparation_check_pass": True}
    monkeypatch.setattr(pilot, "run_offline", fake)
    result = pilot.run_offline_suite([payload(), {**payload(), "case_id": "second"}], wall_timeout=1)
    assert calls == [1]
    assert result["unexecuted_case_ids"] == ["second"]
    assert result["preparation_check_pass"] is False
    assert result["release_authorized"] is False
    with pytest.raises(ValueError):
        pilot.run_offline_suite([payload()] * 26)
    with pytest.raises(ValueError):
        pilot.run_offline_suite([payload()], wall_timeout=1201)


def test_real_endpoint_orchestration_with_fake_transport():
    result = pilot.run_offline(payload())
    assert result["preparation_check_pass"], result
    assert result["test_mode"] is False
    assert result["fake_transport_calls"] == 1
    assert result["release_authorized"] is False
    assert result["answer_grade"] == "UNAVAILABLE"
    assert result["engine_transport_qualified"] is False
    assert any(e["kind"] == "production_branch" for e in result["ledger"])
    assert any(e["type"] == "done" for e in result["events"])
    assert result["child_reaped"] is True


def test_actual_compiler_dispatch_records_source_read_not_menu():
    data = payload("Show my calendar tomorrow.")
    data["scenario"]["sources"]["calendar"] = {"state": "ready", "complete": True, "records": []}
    data["adapters"] = {"get_upcoming": {"kind": "source", "source": "calendar",
                                         "result": "No Calendar events for tomorrow in this synthetic snapshot."}}
    data["transport"] = []
    result = pilot.run_offline(data)
    assert result["preparation_check_pass"], result
    assert result["fake_transport_calls"] == 0
    reads = [e for e in result["ledger"] if e["kind"] == "synthetic_source_read"]
    assert len(reads) == 1 and reads[0]["source"] == "calendar"
    assert reads[0]["args"]["period"] == "tomorrow"
    branches = [e["function"] for e in result["ledger"] if e["kind"] == "production_branch"]
    assert "service.workflows.reads.compile_read" in branches
    assert "service.main.run_agent" not in branches


def effect_payload(approval="approve_synthetic"):
    data = payload("Create a note titled Synthetic with body Synthetic body")
    args = {"title": "Synthetic", "body": "Synthetic body"}
    data["approval"] = approval
    data["adapters"] = {"create_note": {"kind": "effect", "args": args,
                                        "result": "Created synthetic note Synthetic"}}
    data["transport"] = [
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "synthetic_write", "type": "function", "function": {
                "name": "create_note", "arguments": json.dumps(args)}}]}},
        {"message": {"role": "assistant", "content": "The synthetic write returned its receipt."}},
    ]
    return data


def test_production_approval_acceptance_precedes_synthetic_effect_and_receipt():
    result = pilot.run_offline(effect_payload())
    assert result["preparation_check_pass"], result
    ledger = result["ledger"]
    approved = next(e for e in ledger if e["kind"] == "approval_accepted")
    invoked = next(e for e in ledger if e["kind"] == "synthetic_effect_invoked")
    receipt = next(e for e in ledger if e["kind"] == "synthetic_effect_receipt")
    assert approved["approved"] is True
    assert approved["request_id"] == invoked["request_id"]
    assert approved["action_id"] == invoked["action_id"]
    assert approved["sequence"] < invoked["sequence"] < receipt["sequence"]
    assert approved["args"] == invoked["args"] == receipt["args"]
    assert receipt["native_effect"] is False


def test_denied_actual_confirmation_never_invokes_fake_effect():
    result = pilot.run_offline(effect_payload("deny"))
    assert result["preparation_check_pass"], result
    assert any(e["kind"] == "approval_accepted" and e["approved"] is False for e in result["ledger"])
    assert not any(e["kind"].startswith("synthetic_effect") for e in result["ledger"])


def test_undeclared_tool_is_blocked_before_implementation():
    data = effect_payload()
    data["adapters"] = {}
    result = pilot.run_offline(data)
    # Driver denies undeclared effects at the real confirmation boundary.
    assert not any(e["kind"].startswith("synthetic_effect") for e in result["ledger"])
    assert any(e["kind"] == "approval_accepted" and e["approved"] is False for e in result["ledger"])


@pytest.mark.parametrize("name", ["route_to_arrive", "unsubscribe", "update_event"])
def test_unavailable_capabilities_cannot_be_simulated_as_success(name):
    data = payload()
    data["scenario"]["sources"]["traffic"] = {"state": "ready", "complete": True, "records": []}
    data["adapters"] = {name: {"kind": "source", "source": "traffic", "result": "fake success"}}
    result = pilot.run_offline(data)
    assert result["preparation_check_pass"] is False
    assert "Cannot simulate unavailable capability" in result["error"]


def test_external_watchdog_reaps_stuck_fake_turn():
    data = payload()
    data["transport"][0]["delay_seconds"] = 60
    started = time.monotonic()
    result = pilot.run_offline(data, timeout=3)
    assert time.monotonic() - started < 6
    assert result["watchdog"] == "deadline" and result["child_reaped"] is True
    assert result["preparation_check_pass"] is False
    assert result["release_authorized"] is False
