"""Model calls are visible in the content-free diagnostics trace.

Before this, an agent trace recorded routing and tool events but nothing about
the model calls between them, so "where did this slow turn spend its time"
could only be answered by lining the trace up with oMLX's server log by hand.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

from service import diagnostics
from service.agent import loop

ROOT = Path(__file__).resolve().parents[1]
SECRET = "PRIVATE-ANSWER-TEXT"


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        kind, *rest = self.script.pop(0) if self.script else ("text", SECRET)
        if kind == "call":
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": rest[0], "arguments": json.dumps(rest[1])}}]}
        else:
            yield {"kind": "content", "text": rest[0]}
            message = {"role": "assistant", "content": rest[0], "tool_calls": None}
        yield {"kind": "final", "message": message}


class Approver:
    async def confirm(self, action):
        return False


async def _emit(event):
    return None


def _run(tmp_path, monkeypatch, script, **kwargs) -> dict:
    trace = diagnostics.Trace("agent", root=tmp_path)
    # A fixture turn: no audit rows, memory facts or identity from real state.
    monkeypatch.setattr(loop, "audit", lambda *args, **kw: None)

    async def go():
        with diagnostics.use(trace):
            await loop.run_agent(FakeClient(script), "fixture-model",
                                 [{"role": "user", "content": "what's on my calendar tomorrow"}],
                                 _emit, Approver(), test_mode=True, debug=False,
                                 include_memory_context=False, include_identity_context=False,
                                 **kwargs)

    asyncio.run(go())
    return json.loads((tmp_path / "diagnostics" / f"{trace.id}.json").read_text())


def test_each_model_call_is_recorded_with_timings_only(tmp_path, monkeypatch):
    data = _run(tmp_path, monkeypatch, [("call", "get_upcoming", {"day": "tomorrow"}), ("text", SECRET)],
                tools=["get_upcoming", "search_notes"], force_first_tool="get_upcoming")
    steps = [e for e in data["events"] if e["stage"] == "model_step"]
    assert len(steps) == 2
    for step in steps:
        assert step["model_ms"] >= 0 and step["ttft_ms"] >= 0
        assert set(step) <= {"stage", "elapsed_ms", "model_ms", "ttft_ms", "output_chars",
                             "tools_offered", "prefix_reset", "empty"}
    assert [s["empty"] for s in steps] == [False, False]
    assert steps[1]["output_chars"] == len(SECRET)
    assert SECRET not in json.dumps(data) and "get_upcoming" not in json.dumps(steps)


def test_a_changed_tool_set_between_steps_is_flagged_as_a_prefix_reset(tmp_path, monkeypatch):
    # The forced first step offers only get_upcoming; the next offers both, so
    # the rendered tools block (inside the system turn) differs.
    data = _run(tmp_path, monkeypatch, [("call", "get_upcoming", {"day": "tomorrow"}), ("text", "ok")],
                tools=["get_upcoming", "search_notes"], force_first_tool="get_upcoming")
    steps = [e for e in data["events"] if e["stage"] == "model_step"]
    assert [s["tools_offered"] for s in steps] == [1, 2]
    assert [s["prefix_reset"] for s in steps] == [False, True]


def test_a_stable_tool_set_is_not_a_prefix_reset(tmp_path, monkeypatch):
    data = _run(tmp_path, monkeypatch, [("call", "get_upcoming", {"day": "tomorrow"}), ("text", "ok")],
                tools=["get_upcoming", "search_notes"])
    steps = [e for e in data["events"] if e["stage"] == "model_step"]
    assert len(steps) == 2 and not any(s["prefix_reset"] for s in steps)


def _report():
    spec = importlib.util.spec_from_file_location("harness_report", ROOT / "scripts/harness_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_splits_turn_time_into_model_tools_and_overhead():
    report = _report()
    row = report.turn_breakdown({"kind": "agent", "trace_id": "t", "events": [
        {"stage": "started", "elapsed_ms": 0},
        {"stage": "routed", "elapsed_ms": 5},
        {"stage": "model_step", "elapsed_ms": 900, "model_ms": 800, "ttft_ms": 300, "empty": False, "prefix_reset": False},
        {"stage": "tool_call", "elapsed_ms": 905},
        {"stage": "tool_result", "elapsed_ms": 1405},
        {"stage": "model_step", "elapsed_ms": 2100, "model_ms": 600, "ttft_ms": 200, "empty": True, "prefix_reset": True},
        {"stage": "done", "elapsed_ms": 2110},
    ]})
    assert row["total_ms"] == 2110 and row["model_ms"] == 1400 and row["tool_ms"] == 500
    assert row["overhead_ms"] == 210 and row["model_calls"] == 2
    assert row["empty_calls"] == 1 and row["prefix_resets"] == 1
    summary = report.summarize([row])
    assert summary["empty_call_rate"] == 0.5 and summary["model_turns"] == 1


def test_report_leaves_model_time_unknown_for_traces_from_before_model_step():
    row = _report().turn_breakdown({"kind": "agent", "events": [
        {"stage": "started", "elapsed_ms": 0}, {"stage": "done", "elapsed_ms": 40}]})
    assert row["model_ms"] is None and row["overhead_ms"] is None and row["model_calls"] == 0
    assert _report().turn_breakdown({"kind": "today", "events": [{"stage": "started"}]}) is None
