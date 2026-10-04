"""Isolated metadata journal and offline replay checks. No live state or tools."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile

# Even direct invocation cannot write the user's journal.
_scratch = tempfile.TemporaryDirectory(prefix="wisp-diagnostic-tests-")
os.environ["WISP_HOME"] = _scratch.name

import pytest
from service import diagnostics


def read(root, trace):
    return json.loads((root / "diagnostics" / (trace.id + ".json")).read_text())


def test_content_allowlist_and_terminal_failure(tmp_path):
    trace = diagnostics.Trace("agent", root=tmp_path)
    secret = "private-prompt-Bearer-sk-do-not-save"
    trace.observe({"type": "routed", "role": "agent", "route_source": "rules", "reason": secret})
    trace.observe({"type": "tool_call", "name": secret, "args": {"secret": secret}})
    trace.observe({"type": "error", "message": secret, "detail": secret})
    trace.event("plan", tasks=2, revision=3, calendar_ready=False, title=secret)
    trace.finish()
    trace.finish("completed")
    payload = read(tmp_path, trace)
    assert secret not in json.dumps(payload)
    assert payload["status"] == "failed"
    assert payload["events"][-1]["stage"] == "failed"
    assert payload["events"][1]["route_role"] == "agent"
    assert (tmp_path / "diagnostics").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "diagnostics" / (trace.id + ".json")).stat().st_mode & 0o777 == 0o600


def test_disabled_trace_and_nested_span_never_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    trace = diagnostics.Trace("agent", enabled=False, root=tmp_path)
    with diagnostics.use(trace):
        with diagnostics.span("native"):
            diagnostics.event("result", ok=True)
    trace.finish()
    assert not list(tmp_path.iterdir())


def test_bounds_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_TRACES", 3)
    for _ in range(5):
        trace = diagnostics.Trace("today", root=tmp_path)
        trace.finish()
    assert len(list((tmp_path / "diagnostics").glob("*.json"))) == 3
    for _ in range(140):
        trace.event("tool_result")
    payload = read(tmp_path, trace)
    assert len(payload["events"]) == 128
    assert payload["events"][0]["stage"] == "started"
    assert payload["events_dropped"] > 0
    # Data is complete JSON on disk, requiring neither the Trace nor service.
    assert json.loads(json.dumps(payload))["trace_id"] == trace.id


def test_disk_failure_and_symlink_are_fail_open(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("occupied")
    trace = diagnostics.Trace("agent", root=bad)
    trace.finish("cancelled")
    assert trace.status == "cancelled"
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "diagnostics").symlink_to(outside, target_is_directory=True)
    diagnostics.Trace("agent", root=root).finish()
    assert not list(outside.iterdir())


def test_async_context_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    async def work(index):
        trace = diagnostics.Trace("agent", root=tmp_path)
        with diagnostics.use(trace):
            await asyncio.sleep(0)
            with diagnostics.span("native"):
                diagnostics.event("result", revision=index)
        trace.finish()
        return read(tmp_path, trace)
    async def run():
        return await asyncio.gather(*(work(i) for i in range(6)))
    payloads = asyncio.run(run())
    assert len({p["trace_id"] for p in payloads}) == 6
    for i, p in enumerate(payloads):
        assert [e["revision"] for e in p["events"] if "revision" in e] == [i]
        assert any(e.get("component") == "native" for e in p["events"])


def offline_module():
    script = Path(__file__).resolve().parents[1] / "scripts/replay_diagnostic.py"
    spec = importlib.util.spec_from_file_location("fixture_replay", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_replay_fixed_clock_without_effects(tmp_path, monkeypatch):
    import socket
    import subprocess
    def forbidden(*args, **kw):
        raise AssertionError("Replay attempted an external effect")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    module = offline_module()
    from service.assistant.today import build_plan
    capture = dict(schema_version=1, planner_fingerprint=diagnostics.planner_fingerprint(), day="2026-10-04",
                   timezone="UTC", now=1791072000, tasks=[], commitments=[],
                   preferences={}, sources={})
    capture["expected_plan"] = build_plan(capture["day"], capture["timezone"], [], [], {}, {}, now=capture["now"])
    report = {"schema_version": 1, "details": {"replay": capture}}
    assert module.replay(report)["matches_capture"] is True
    capture["expected_plan"]["warnings"] = []
    assert module.replay(report)["matches_capture"] is False
    capture["planner_fingerprint"] = "wrong"
    with pytest.raises(ValueError, match="version differs"):
        module.replay(report)
    capture["planner_fingerprint"] = diagnostics.planner_fingerprint()
    capture["tasks"] = [{}] * 1001
    with pytest.raises(ValueError, match="1000"):
        module.replay(report)


def test_timeline_playback_is_data_only():
    result = offline_module().replay({"schema_version": 1, "events": [{"stage": "send_email"}], "status": "failed"})
    assert result["mode"] == "timeline"
    assert "execution is unavailable" in result["notice"]


def test_cancelled_background_span(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    with pytest.raises(asyncio.CancelledError):
        with diagnostics.span("daily_brief") as trace:
            raise asyncio.CancelledError()
    assert read(tmp_path, trace)["status"] == "cancelled"


def test_native_unknown_and_failed_are_not_success(tmp_path, monkeypatch):
    from service.assistant import outbox
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    async def fixture(*args, **kw):
        return {"ok": False, "status": "unknown", "error": "PRIVATE-NATIVE-DETAIL"}
    monkeypatch.setattr(outbox, "_request", fixture)
    result = asyncio.run(outbox.request("create_calendar_event", {"title": "PRIVATE-NATIVE-TITLE"}))
    assert result["status"] == "unknown"
    payload = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text())
    assert payload["status"] == "unknown"
    assert any(e.get("native_outcome") == "unknown" for e in payload["events"])
    assert "PRIVATE" not in json.dumps(payload)
    async def disconnected(*args, **kw):
        return {"ok": False, "error": "App disconnected"}
    monkeypatch.setattr(outbox, "_request", disconnected)
    asyncio.run(outbox.request("create_calendar_event", {}))
    payloads = [json.loads(p.read_text()) for p in (tmp_path / "diagnostics").glob("*.json")]
    assert any(p["status"] == "failed" for p in payloads)


def test_unavailable_sources_not_reported_ready(tmp_path, monkeypatch):
    from service.assistant import sync_status
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    async def fixture(*args, **kw):
        return {"syncing": False, "sources": [{"id": "calendar", "state": "unavailable", "reason": "PRIVATE-SOURCE-DETAIL"},
                                               {"id": "email", "state": "disabled"}]}
    monkeypatch.setattr(sync_status, "_ensure_sources", fixture)
    asyncio.run(sync_status.ensure_sources(["calendar", "email"]))
    payload = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text())
    assert any(e.get("source_state") == "unavailable" for e in payload["events"])
    assert any(e.get("source_state") == "disabled" for e in payload["events"])
    assert payload["events"][-2]["ok"] is False
    assert "PRIVATE" not in json.dumps(payload)
