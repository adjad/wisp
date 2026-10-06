"""Offline tests for the release performance benchmark (scripts/release_performance.py).

Everything here is inert: scripted events, a loopback-only SSE server, temporary
git repositories, and evidence CONSTRUCTED inside pytest temp directories to
exercise the real orchestration, grader, evaluator and checker. No engine, model,
Wisp backend, user data or network is touched, and nothing in this file measures
performance. Constructed receipts live only under tmp_path and are never
performance claims; the product refuses fake-model and offline results as PASS,
and `test_offline_fixture_run_can_never_pass` proves it.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_performance.py"
LEGACY = ROOT / "scripts" / "bench_latency_changes.py"
_spec = importlib.util.spec_from_file_location("release_performance", SCRIPT)
rp = importlib.util.module_from_spec(_spec)
sys.modules["release_performance"] = rp
_spec.loader.exec_module(rp)

UNKNOWN = rp.UNKNOWN
MS = rp.MS
BUNDLE = rp.load_bundle()
CORPUS, POLICY = BUNDLE["corpus"], BUNDLE["policy"]
SPECS = rp.scenario_by_id(BUNDLE)
REQUIRED = [s["id"] for s in rp.required_scenarios(BUNDLE)]


OWNED_PORTS: set[int] = set()   # loopback ports a test itself bound; nothing else may be contacted


@pytest.fixture(autouse=True)
def inert_environment(monkeypatch):
    """Tests may reach loopback ports THEY OWN and run git or this interpreter, nothing else."""
    OWNED_PORTS.clear()
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_init = subprocess.Popen.__init__
    blocked: list[str] = []

    def check(address):
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost")
                and address[1] in OWNED_PORTS):
            blocked.append(repr(address))
            raise AssertionError(f"test attempted a connection to a port it does not own: {address!r}")

    def connect(self, address):
        check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return real_connect_ex(self, address)

    def init(self, args, *a, **k):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        first = os.fspath(argv[0])
        if os.path.basename(first) not in ("git",) and first != sys.executable:
            blocked.append(first)
            raise AssertionError(f"test attempted to run {first!r}")
        return real_init(self, args, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(subprocess.Popen, "__init__", init)
    yield blocked
    assert not blocked


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def ev(t_ms: float, **event) -> dict:
    return {"t_ns": int(t_ms * MS), "event": event}


def counts(chat=1, **overrides) -> dict:
    base = {"engine_http_calls": chat, "chat_calls": chat, "engine_state_changes": 0,
            "authority_loads": 0, "process_checks": 0, "binding_checks": 0, "peer_checks": 0,
            "blocked_effects": 0, "completion_reasons": ["stop"] * chat, "other_side_engine_requests": 0,
            "instrumentation_records": chat}
    base.update(overrides)
    return base


def sink(events, **extra) -> dict:
    out = rp.HttpTurnDriver.new_sink()
    out["events"] = events
    out.update(extra)
    return out


def grade(scenario_id: str, events, instrument=None, **driver):
    spec = SPECS[scenario_id]
    drv = sink(events, **driver)
    analysis = rp.analyze_events(events)
    return rp.grade_sample(spec, analysis, drv, counts() if instrument is None else instrument, CORPUS), analysis


OK_GREETING = [ev(1, type="session", id="s"), ev(5, type="routed", direct_calls=[], needs_tools=False),
               ev(200, type="delta", text="Hello!"), ev(260, type="done")]


# ----------------------------------------------------------------------------
# Percentiles, UNKNOWN handling, warmups
# ----------------------------------------------------------------------------

def test_nearest_rank_percentiles_are_stable_and_order_independent():
    values = list(range(1, 21))                         # 1..20
    assert rp.nearest_rank(values, 50) == 10             # ceil(0.50*20) = 10th
    assert rp.nearest_rank(values, 95) == 19             # ceil(0.95*20) = 19th
    shuffled = [7, 19, 1, 13, 4, 20, 11, 2, 16, 9, 5, 18, 3, 14, 8, 12, 6, 17, 10, 15]
    assert rp.nearest_rank(shuffled, 50) == 10 and rp.nearest_rank(shuffled, 95) == 19
    assert rp.nearest_rank([5, 5, 5, 9], 50) == 5 and rp.nearest_rank([42], 95) == 42
    with pytest.raises(ValueError):
        rp.nearest_rank([], 50)


def test_aggregate_never_turns_a_missing_measurement_into_a_number():
    assert rp.aggregate([1, 2, 3])["p50_ns"] == 2
    mixed = rp.aggregate([1, 2, UNKNOWN, 4])
    assert mixed["p50_ns"] == UNKNOWN and mixed["p95_ns"] == UNKNOWN
    assert (mixed["n"], mixed["n_known"], mixed["n_unknown"]) == (4, 3, 1)
    assert rp.aggregate([])["p50_ns"] == UNKNOWN
    assert rp.aggregate([True, 2])["p50_ns"] == UNKNOWN   # a bool is not a timing


def test_instrument_counts_are_unknown_not_zero_when_a_surface_is_missing():
    window = [{"kind": "engine_http", "method": "POST", "path": "/v1/chat/completions"},
              {"kind": "engine_http", "method": "GET", "path": "/health"},
              {"kind": "effect_blocked", "what": "exec"}]
    got = rp.instrument_counts(window, ["peer_check"])
    assert got["engine_http_calls"] == 2 and got["chat_calls"] == 1
    assert got["peer_checks"] == UNKNOWN                  # unavailable, not zero
    assert got["authority_loads"] == 0                    # available and genuinely none
    assert got["blocked_effects"] == 1
    assert all(v == UNKNOWN for v in rp.instrument_counts(None, None).values())
    assert rp.instrument_counts([], ["engine_http"])["chat_calls"] == UNKNOWN


def test_state_changes_are_counted_from_engine_load_and_unload_calls():
    window = [{"kind": "engine_http", "method": "POST", "path": "/v1/models/Some-Model/unload"},
              {"kind": "engine_http", "method": "POST", "path": "/v1/models/Some-Model/load"},
              {"kind": "engine_http", "method": "GET", "path": "/v1/models/status"}]
    assert rp.instrument_counts(window, [])["engine_state_changes"] == 2


def make_sample(side, scenario, phase, rep, metrics, *, correct=True, refusal=False, reasons=None,
                unverifiable=None, outcome=None, cpu=0.1):
    return {"schema": rp.SAMPLE_SCHEMA, "run_id": "r", "id": rp.sample_id(side, scenario, phase, rep),
            "side": side, "scenario": scenario, "phase": phase, "rep": rep, "variant": 0, "order_slot": 0,
            "started_ns": 1, "ended_ns": 2, "prompt_sha256": "x",
            "outcome": outcome or ("ok" if correct else "failed"),
            "grade": {"correct": correct, "reasons": reasons or ([] if correct else ["x"]),
                      "refusal": refusal, "unverifiable": unverifiable or []},
            "metrics_ns": metrics,
            "diagnostics": {"engine_http_calls": 1, "chat_calls": 1, "authority_loads": 0,
                            "process_checks": 0, "binding_checks": 0, "peer_checks": 0,
                            "backend_cpu_s": cpu}}


def metrics_for(spec_id: str, ms_value: float) -> dict:
    return {m: int(ms_value * MS) for m in SPECS[spec_id]["required_metrics"]}


def world(cand_ms=1000.0, base_ms=1000.0, n=20, warmup_ms=None):
    samples = []
    for sid in REQUIRED:
        for side, value in (("candidate", cand_ms), ("baseline", base_ms)):
            for rep in range(1, n + 1):
                samples.append(make_sample(side, sid, "measured", rep, metrics_for(sid, value)))
            if warmup_ms is not None:
                for rep in range(1, 3):
                    samples.append(make_sample(side, sid, "warmup", rep, metrics_for(sid, warmup_ms)))
    return samples


APPROVED = {"approved": True, "problem": None}


def test_warmups_are_labelled_counted_and_excluded_from_percentiles():
    summary = rp.summarize(world(1000.0, 1000.0, warmup_ms=90000.0), BUNDLE)
    cell = summary["greeting_warm"]["candidate"]
    assert cell["warmup_samples"] == 2 and cell["measured"] == 20
    assert cell["metrics"]["total_completion_s"]["p95_ns"] == 1000 * MS     # the 90 s warmups never enter
    assert rp.decide(summary, BUNDLE, APPROVED)["verdict"] == "PASS"


def test_only_correct_samples_contribute_latency_so_failures_cannot_look_faster():
    samples = world(1000.0, 1000.0)
    for s in samples:
        if s["side"] == "candidate" and s["scenario"] == "greeting_warm" and s["rep"] <= 5:
            s["grade"].update(correct=False, reasons=["empty_answer"])
            s["outcome"] = "failed"
            s["metrics_ns"] = {k: 1 for k in s["metrics_ns"]}       # absurdly fast, but wrong
    summary = rp.summarize(samples, BUNDLE)
    cell = summary["greeting_warm"]["candidate"]
    assert cell["failed"] == 5 and cell["correct"] == 15
    assert cell["metrics"]["total_completion_s"]["p50_ns"] == 1000 * MS    # the 1 ns "wins" are not counted
    decision = rp.decide(summary, BUNDLE, APPROVED)
    assert decision["verdict"] == "BLOCK"
    assert any(r["code"] == "candidate_correctness_failure" for r in decision["reasons"])


# ----------------------------------------------------------------------------
# Event timing: what the user saw, versus what the model did
# ----------------------------------------------------------------------------

def test_visible_answer_model_delta_completion_and_approval_are_distinct_timings():
    events = [ev(10, type="routed", direct_calls=[], needs_tools=True),
              ev(120, type="reasoning", text="thinking about it"),
              ev(450, type="delta", text="   "),                    # whitespace is not an answer
              ev(500, type="delta", text="Here is"),
              ev(900, type="confirm", id="c1", tool="send_message", args={"to": "x"}),
              ev(1300, type="delta", text=" the answer."),
              ev(1400, type="done"), ev(1500, type="done")]
    a = rp.analyze_events(events)
    assert a["routed_ns"] == 10 * MS
    assert a["first_model_delta_ns"] == 120 * MS            # the reasoning event is the first model output
    assert a["first_visible_answer_ns"] == 500 * MS         # blank delta ignored
    assert a["approval_boundary_ns"] == 900 * MS
    assert a["total_completion_ns"] == 1400 * MS             # the FIRST done, not the second
    assert a["answer_text"].strip() == "Here is the answer." and not a["answer_buffered"]


def test_a_retracted_preamble_is_not_the_first_visible_answer():
    events = [ev(100, type="delta", text="Let me check that."), ev(150, type="clear_answer"),
              ev(400, type="delta", text="Your answer."), ev(420, type="done")]
    a = rp.analyze_events(events)
    assert a["first_visible_answer_ns"] == 400 * MS and a["clear_answers"] == 1
    assert a["answer_text"] == "Your answer."


def test_buffered_publication_makes_visible_answer_equal_to_the_end():
    events = [ev(20, type="routed", direct_calls=[], needs_tools=True), ev(2000, type="reasoning", text="r"),
              ev(2001, type="text", text="Final answer."), ev(2002, type="done")]
    a = rp.analyze_events(events)
    assert a["answer_buffered"] is True
    assert a["first_visible_answer_ns"] == 2001 * MS and a["total_completion_ns"] == 2002 * MS


def test_derived_metrics_mark_missing_timings_unknown():
    a = rp.analyze_events([ev(5, type="done")])
    metrics = rp.derive_metrics(SPECS["greeting_warm"], a, {})
    assert metrics["first_visible_answer_s"] == UNKNOWN and metrics["total_completion_s"] == 5 * MS
    http = rp.derive_metrics(SPECS["health_status_overhead"], a, {"latency_ns": 12 * MS})
    assert http == {"request_latency_s": 12 * MS}
    assert rp.derive_metrics(SPECS["health_status_overhead"], a, {})["request_latency_s"] == UNKNOWN


# ----------------------------------------------------------------------------
# Grading: failed, empty, truncated, refused and false-success are never "fast"
# ----------------------------------------------------------------------------

def test_a_good_greeting_is_correct():
    g, _ = grade("greeting_warm", OK_GREETING)
    assert g["correct"] and g["reasons"] == [] and not g["refusal"]


@pytest.mark.parametrize("events,driver,reason", [
    ([ev(1, type="done")], {}, "empty_answer"),
    ([ev(200, type="delta", text="Hello!")], {}, "no_terminal_done"),
    ([ev(5, type="error", message="boom"), ev(6, type="done")], {}, "error_event"),
    ([ev(200, type="delta", text="I ran out of room working that one out and didn't get to an answer."),
      ev(210, type="done")], {}, "failure_message_as_answer"),
    ([ev(200, type="delta", text="Hello!"), ev(210, type="done")], {"deadline": True}, "deadline_exceeded"),
    ([ev(200, type="delta", text="Hello!"), ev(210, type="done")], {"cancelled": True}, "cancelled"),
    ([ev(200, type="delta", text="Hello!"), ev(210, type="done")], {"transport_error": "ReadError"}, "transport_error"),
    ([ev(200, type="delta", text="x"), ev(210, type="done")], {}, "answer_too_short"),
])
def test_failed_empty_or_aborted_greetings_are_incorrect(events, driver, reason):
    g, _ = grade("greeting_warm", events, **driver)
    assert not g["correct"] and any(r.startswith(reason) for r in g["reasons"]), g


def test_truncated_reasoning_leak_and_missing_streaming_are_failures():
    leak = OK_GREETING[:-1] + [ev(205, type="raw_model_io", model="m", request={"tools": []},
                                  response={"content": "", "_think_leak": True}), ev(260, type="done")]
    g, _ = grade("greeting_warm", leak)
    assert "truncated_reasoning_leak" in g["reasons"]
    buffered = [ev(5, type="routed", direct_calls=[], needs_tools=False), ev(300, type="text", text="Hello there!"),
                ev(310, type="done")]
    g, _ = grade("greeting_warm", buffered)
    assert "expected_streaming_absent" in g["reasons"]


def test_attribution_refusal_is_a_failure_and_is_flagged_as_a_refusal():
    events = [ev(5, type="error", message="Wisp couldn't verify the local AI engine (oMLX) just now."),
              ev(6, type="done")]
    g, _ = grade("greeting_warm", events)
    assert g["refusal"] and not g["correct"] and "refused" in g["reasons"]


def test_engine_state_changes_and_blocked_effect_attempts_fail_the_sample():
    g, _ = grade("greeting_warm", OK_GREETING, instrument=counts(1, engine_state_changes=1))
    assert "engine_state_change" in g["reasons"]
    g, _ = grade("greeting_warm", OK_GREETING, instrument=counts(1, blocked_effects=2))
    assert "blocked_effect_attempt" in g["reasons"]


def test_bounded_reasoning_needs_the_right_answer():
    right = [ev(900, type="text", text="It arrives at 6:30 PM."), ev(901, type="done")]
    assert grade("bounded_reasoning", right)[0]["correct"]
    wrong = [ev(900, type="text", text="It arrives at 7:15 PM."), ev(901, type="done")]
    assert "missing_any_pattern" in grade("bounded_reasoning", wrong)[0]["reasons"]
    called = right[:1] + [ev(850, type="tool_call", id="1", name="get_upcoming", args={})] + right[1:]
    assert any(r.startswith("unexpected_tool_call") for r in grade("bounded_reasoning", called)[0]["reasons"])


def selection_events(*, tool="search_notes", query="Orchard Lane workshop", offered=True, direct=None,
                     text="Your note says Room 204 at 2:00 PM and to bring a charging cable.", effect=False):
    direct = [] if direct is None else direct
    schemas = [{"type": "function", "function": {"name": "search_notes",
                                                 "parameters": {"properties": {"query": {}, "count": {}}}}}] if offered else []
    out = [ev(1, type="session", id="s"), ev(20, type="routed", direct_calls=direct, needs_tools=True),
           ev(30, type="raw_model_io", model="m", request={"tools": schemas}, response={"content": ""})]
    out.append(ev(40, type="tool_call", id="t1", name=tool, args={"query": query}))
    if effect:
        out.append(ev(41, type="tool_call", id="t2", name="send_message", args={"to": "x", "text": "y"}))
    out += [ev(900, type="text", text=text), ev(901, type="done")]
    return out


def test_scoped_tool_selection_checks_route_tool_arguments_offer_and_facts():
    assert grade("scoped_tool_selection", selection_events())[0]["correct"]
    assert "missing_tool_call:search_notes" in grade("scoped_tool_selection", selection_events(tool="get_upcoming"))[0]["reasons"]
    assert "tool_argument_mismatch:search_notes.query" in grade(
        "scoped_tool_selection", selection_events(query="groceries"))[0]["reasons"]
    assert "tool_not_offered:search_notes" in grade(
        "scoped_tool_selection", selection_events(offered=False))[0]["reasons"]
    assert "route_used_direct_call" in grade("scoped_tool_selection", selection_events(
        direct=[{"tool": "search_notes", "args": {}}]))[0]["reasons"]
    assert any(r.startswith("effect_tool_called") for r in grade(
        "scoped_tool_selection", selection_events(effect=True))[0]["reasons"])
    wrong_facts = grade("scoped_tool_selection", selection_events(text="You have a grocery list with oat milk and lemons."))[0]
    assert any(r.startswith("missing_fact") for r in wrong_facts["reasons"])
    assert any(r.startswith("forbidden_content") for r in wrong_facts["reasons"])


def test_offered_tool_check_is_unverifiable_without_debug_events_not_a_pass():
    events = [e for e in selection_events() if e["event"].get("type") != "raw_model_io"]
    g, _ = grade("scoped_tool_selection", events)
    assert g["unverifiable"] == ["offered_tools"] and g["reasons"] == [] and g["correct"] is False


def deterministic_events(text="Messages: Alex moved the workshop to Room 204 and asked for the budget numbers."):
    return [ev(1, type="session", id="s"),
            ev(10, type="routed", direct_calls=[{"tool": "summarize_messages", "args": {}}], needs_tools=True),
            ev(20, type="tool_call", id="t1", name="summarize_messages", args={}),
            ev(30, type="tool_result", id="t1", result="digest"), ev(60, type="text", text=text), ev(61, type="done")]


def test_zero_generation_read_requires_no_model_call_and_unknown_is_unverifiable():
    assert grade("deterministic_read", deterministic_events(), instrument=counts(0))[0]["correct"]
    over = grade("deterministic_read", deterministic_events(), instrument=counts(1))[0]
    assert any(r.startswith("model_calls:1!=exactly_0") for r in over["reasons"]) and not over["correct"]
    unknown = grade("deterministic_read", deterministic_events(),
                    instrument=counts(0, chat_calls=UNKNOWN))[0]
    assert unknown["unverifiable"] == ["model_calls"] and unknown["reasons"] == [] and not unknown["correct"]
    empty_digest = grade("deterministic_read", deterministic_events(text="No messages were synced for this period."),
                         instrument=counts(0))[0]
    assert any(r.startswith("forbidden_content") for r in empty_digest["reasons"])


def outbound_events(*, answer="I didn't send it. The message needs your approval first.", confirm=True,
                    result="denied by the user", extra_confirm=False):
    out = [ev(1, type="session", id="s"), ev(15, type="routed", direct_calls=[], needs_tools=True),
           ev(900, type="tool_call", id="c1", name="send_message",
              args={"to": "Alex Rivera", "text": "I will be ten minutes late"})]
    if confirm:
        out.append(ev(1500, type="confirm", id="c1", tool="send_message",
                      args={"to": "Alex Rivera", "text": "I will be ten minutes late"}))
    if extra_confirm:
        out.append(ev(1510, type="confirm", id="c2", tool="send_email", args={}))
    if result is not None:
        out.append(ev(1600, type="tool_result", id="c1", result=result))
    out += [ev(2000, type="text", text=answer), ev(2001, type="done")]
    return out


DENIED = {"approvals": [{"t_ns": 1, "action_id": "c1", "session_id": "s", "approved": False, "accepted": True}]}


def test_outbound_request_must_stop_at_the_real_approval_boundary_without_false_success():
    good, analysis = grade("outbound_approval_boundary", outbound_events(), **DENIED)
    assert good["correct"] and analysis["approval_boundary_ns"] == 1500 * MS
    assert "approval_boundary_not_reached" in grade("outbound_approval_boundary", outbound_events(confirm=False), **DENIED)[0]["reasons"]
    false_success = grade("outbound_approval_boundary", outbound_events(answer="Done! I've sent the message to Alex."), **DENIED)[0]
    assert "false_success_claim" in false_success["reasons"] and not false_success["correct"]
    assert "false_success_claim" in grade("outbound_approval_boundary", outbound_events(
        answer="The message was delivered."), **DENIED)[0]["reasons"]
    assert any(r.startswith("effect_result_without_denial") for r in grade(
        "outbound_approval_boundary", outbound_events(result="Message sent to +15555550123"), **DENIED)[0]["reasons"])
    assert "harness_approved_an_effect" in grade("outbound_approval_boundary", outbound_events(), approvals=[
        {"approved": True}])[0]["reasons"]
    assert any(r.startswith("unexpected_confirm") for r in grade(
        "outbound_approval_boundary", outbound_events(extra_confirm=True), **DENIED)[0]["reasons"])
    assert "empty_answer" in grade("outbound_approval_boundary", outbound_events(answer=" "), **DENIED)[0]["reasons"]


def test_negated_delivery_language_is_not_a_false_success():
    for honest in ("I couldn't send that; it needs your approval.", "The message was not sent.",
                   "Nothing was sent. Approve it and I'll send it."):
        g, _ = grade("outbound_approval_boundary", outbound_events(answer=honest), **DENIED)
        assert "false_success_claim" not in g["reasons"], honest


def test_health_overhead_requires_ok_status_and_flags_refusals():
    ok = {"http": {"status": 200, "json": {"status": "ok"}, "text": ""}, "latency_ns": 5 * MS}
    assert grade("health_status_overhead", [], instrument=counts(1), **ok)[0]["correct"]
    bad_body = {"http": {"status": 200, "json": {"status": "weird"}, "text": ""}}
    assert "http_body_mismatch" in grade("health_status_overhead", [], **bad_body)[0]["reasons"]
    refused = {"http": {"status": 503, "json": {"detail": "Wisp couldn't verify the local AI engine"}, "text": ""}}
    g = grade("health_status_overhead", [], **refused)[0]
    assert g["refusal"] and not g["correct"] and "http_status:503" in g["reasons"]
    assert "no_http_response" in grade("health_status_overhead", [])[0]["reasons"]
    assert "deadline_exceeded" in grade("health_status_overhead", [], deadline=True)[0]["reasons"]


# ----------------------------------------------------------------------------
# Policy: baseline, regression, refusal, unknown
# ----------------------------------------------------------------------------

def test_equal_timings_with_an_approved_baseline_pass_and_without_one_are_inconclusive():
    summary = rp.summarize(world(1000.0, 1000.0), BUNDLE)
    assert rp.decide(summary, BUNDLE, APPROVED)["verdict"] == "PASS"
    none = rp.decide(summary, BUNDLE, {"approved": False, "problem": "no approved baseline"})
    assert none["verdict"] == "INCONCLUSIVE"
    assert any(r["code"] == "missing_or_unapproved_baseline" for r in none["reasons"])


def test_regression_threshold_needs_both_relative_and_absolute_increase():
    def verdict(c, b):
        return rp.decide(rp.summarize(world(c, b), BUNDLE), BUNDLE, APPROVED)["verdict"]
    assert verdict(1300.0, 1000.0) == "BLOCK"       # +30% and +300 ms: both exceeded
    assert verdict(1100.0, 1000.0) == "PASS"        # +10%, +100 ms: neither
    assert verdict(450.0, 300.0) == "PASS"          # +50% but only +150 ms: absolute not exceeded
    assert verdict(5250.0, 5000.0) == "PASS"        # +250 ms (not strictly greater) and only +5%
    assert verdict(10300.0, 10000.0) == "PASS"      # +300 ms but only +3%: relative not exceeded
    assert verdict(1200.0, 1000.0) == "PASS"        # exactly +20%: not greater than 20%
    assert verdict(1500.0, 1000.0) == "BLOCK"
    assert verdict(500.0, 1000.0) == "PASS"         # faster is fine


def test_p95_only_regression_blocks_and_deltas_and_sample_counts_are_reported():
    samples = world(1000.0, 1000.0)
    for s in samples:      # the slowest two of 20 candidate samples regress, p50 does not
        if s["side"] == "candidate" and s["scenario"] == "bounded_reasoning" and s["rep"] >= 19:
            s["metrics_ns"] = metrics_for("bounded_reasoning", 4000.0)
    decision = rp.decide(rp.summarize(samples, BUNDLE), BUNDLE, APPROVED)
    assert decision["verdict"] == "BLOCK"
    row = decision["comparison"]["bounded_reasoning"]["required_metrics"]["total_completion_s"]
    assert row["p50"]["regression"] is False and row["p95"]["regression"] is True
    assert row["p95"]["delta_ms"] == 3000.0 and row["n_candidate"] == 20 and row["n_baseline"] == 20


def test_new_refusals_block_even_when_total_failures_match():
    samples = world()
    for s in samples:
        if s["side"] == "candidate" and s["scenario"] == "health_status_overhead" and s["rep"] == 1:
            s.update(outcome="refused")
            s["grade"].update(correct=False, refusal=True, reasons=["refused"])
    decision = rp.decide(rp.summarize(samples, BUNDLE), BUNDLE, APPROVED)
    codes = {r["code"] for r in decision["reasons"]}
    assert decision["verdict"] == "BLOCK" and {"new_refusal", "candidate_correctness_failure"} <= codes


def baseline_world(*, n_cand=30, n_base=30, base_fail=(), scenario="greeting_warm", cand_ms=1000.0, base_ms=1000.0,
                   failed_ms=99_999.0, refusal=False, reasons=None, unverifiable=None):
    """Both sides measured, with the named baseline repetitions of one scenario failing. A failed sample keeps a
    wildly different latency so that any leak of it into a percentile changes the answer."""
    samples = []
    for sid in REQUIRED:
        for rep in range(1, n_cand + 1):
            samples.append(make_sample("candidate", sid, "measured", rep, metrics_for(sid, cand_ms)))
        for rep in range(1, n_base + 1):
            if sid == scenario and rep in base_fail:
                sample = make_sample(
                    "baseline", sid, "measured", rep, metrics_for(sid, failed_ms), correct=False, refusal=refusal,
                    reasons=reasons if reasons is not None else (["refused"] if refusal else ["empty_answer"]),
                    unverifiable=unverifiable, outcome="refused" if refusal else None)
                sample["grade"]["reasons"] = list(reasons if reasons is not None else sample["grade"]["reasons"])
                samples.append(sample)
            else:
                samples.append(make_sample("baseline", sid, "measured", rep, metrics_for(sid, base_ms)))
    return samples


def decide_for(samples, lifecycle=None, baseline=APPROVED):
    return rp.decide(rp.summarize(samples, BUNDLE), BUNDLE, baseline, lifecycle)


def reason_codes(decision):
    return [r["code"] for r in decision["reasons"]]


def test_baseline_failures_no_longer_decide_the_verdict_when_enough_correct_baseline_samples_remain():
    """The v1.1.5-style baseline: some samples are wrong or refused. With >= 20 correct ones left on every metric
    the comparison is still valid, and the failures are recorded, never hidden."""
    decision = decide_for(baseline_world(base_fail={3, 9, 14}))
    assert decision["verdict"] == "PASS" and decision["reasons"] == []
    note = [r for r in decision["recorded"] if r["code"] == "baseline_correctness_failure"]
    assert len(note) == 1 and note[0]["scenario"] == "greeting_warm" and "3 of 30" in note[0]["detail"]
    assert "empty_answer" in note[0]["detail"]
    samples = decision["comparison"]["greeting_warm"]["baseline_samples"]
    assert samples == {"measured": 30, "correct": 27, "failed": 3, "refused": 0, "unverifiable": 0,
                       "unverifiable_only": 0, "failure_reasons": {"empty_answer": 3}}
    row = decision["comparison"]["greeting_warm"]["required_metrics"]["total_completion_s"]
    assert row["n_baseline"] == 27 and row["n_candidate"] == 30
    assert row["p95"]["baseline_ms"] == 1000.0                     # the failed samples' 99,999 ms never entered a percentile


def test_refused_baseline_samples_are_recorded_with_their_count_and_are_not_a_candidate_problem():
    decision = decide_for(baseline_world(base_fail={1, 2}, refusal=True))
    assert decision["verdict"] == "PASS"
    cell = decision["comparison"]["greeting_warm"]["baseline_samples"]
    assert cell["refused"] == 2 and cell["failed"] == 2 and cell["failure_reasons"] == {"refused": 2}
    assert [r["code"] for r in decision["recorded"]] == ["baseline_correctness_failure"]


def test_exactly_the_floor_of_correct_baseline_samples_is_enough_and_one_fewer_is_inconclusive():
    floor = POLICY["min_measured_samples"]
    assert floor == 20
    ok = decide_for(baseline_world(n_base=22, base_fail={1, 2}))                  # 20 correct
    assert ok["verdict"] == "PASS"
    thin = decide_for(baseline_world(n_base=22, base_fail={1, 2, 3}))             # 19 correct
    assert thin["verdict"] == "INCONCLUSIVE" and reason_codes(thin) == ["baseline_insufficient_correct_samples"]
    reason = thin["reasons"][0]
    assert reason["scenario"] == "greeting_warm" and "n=19" in reason["detail"] and "fewer than 20" in reason["detail"]
    for metric, row in thin["comparison"]["greeting_warm"]["required_metrics"].items():
        assert row["n_baseline"] == 19, metric                                     # per metric, as the policy says


def test_a_v2_style_failing_baseline_cannot_pass_a_run_that_only_kept_the_old_sample_count():
    """With 20 measured samples and even one baseline failure the old floor leaves 19 correct: not a verdict."""
    decision = decide_for(baseline_world(n_cand=20, n_base=20, base_fail={7}))
    assert decision["verdict"] == "INCONCLUSIVE" and reason_codes(decision) == ["baseline_insufficient_correct_samples"]


def test_a_baseline_with_no_correct_samples_is_inconclusive_for_both_the_count_and_the_unknown_metric():
    decision = decide_for(baseline_world(base_fail=set(range(1, 31))))
    assert decision["verdict"] == "INCONCLUSIVE"
    assert set(reason_codes(decision)) == {"baseline_insufficient_correct_samples", "unknown_gating_metric"}


def test_baseline_samples_that_could_not_be_checked_stay_inconclusive_unlike_baseline_failures():
    decision = decide_for(baseline_world(base_fail={5}, reasons=[], unverifiable=["shared_engine_interference"]))
    assert decision["verdict"] == "INCONCLUSIVE" and reason_codes(decision) == ["baseline_unverifiable_sample"]
    assert decision["comparison"]["greeting_warm"]["baseline_samples"]["unverifiable_only"] == 1


def test_a_refused_baseline_sample_that_is_also_unverifiable_is_a_recorded_failure_not_an_inconclusive_one():
    """Seen live on v1.1.5: a refused attribution leaves the engine call without a completion record, so the
    sample is `refused` AND `completion_status_incomplete`. It is the old release's failure, not a measurement defect."""
    decision = decide_for(baseline_world(base_fail={2, 7}, refusal=True, reasons=["error_event", "refused"],
                                         unverifiable=["completion_status_incomplete"]))
    assert decision["verdict"] == "PASS" and decision["reasons"] == []
    cell = decision["comparison"]["greeting_warm"]["baseline_samples"]
    assert cell["refused"] == 2 and cell["unverifiable"] == 2 and cell["unverifiable_only"] == 0
    assert cell["failure_reasons"] == {"error_event": 2, "refused": 2, "unverifiable:completion_status_incomplete": 2}


def test_a_slower_candidate_still_blocks_against_a_baseline_that_has_failures():
    decision = decide_for(baseline_world(base_fail={3, 9, 14}, cand_ms=1300.0, base_ms=1000.0))
    assert decision["verdict"] == "BLOCK" and set(reason_codes(decision)) == {"measured_material_regression"}
    fast_failures = decide_for(baseline_world(base_fail={3, 9, 14}, cand_ms=1300.0, failed_ms=1.0))
    assert fast_failures["verdict"] == "BLOCK"                                    # fast failures cannot hide a regression
    slow_failures = decide_for(baseline_world(base_fail=set(range(1, 11)), cand_ms=1300.0, failed_ms=500_000.0))
    assert slow_failures["verdict"] == "BLOCK"                                    # slow failures cannot inflate the baseline


def test_candidate_failures_and_new_refusals_block_even_when_the_baseline_fails_more():
    samples = baseline_world(base_fail={1, 2, 3, 4, 5})
    for s in samples:
        if s["side"] == "candidate" and s["scenario"] == "greeting_warm" and s["rep"] == 6:
            s.update(outcome="failed")
            s["grade"].update(correct=False, reasons=["empty_answer"])
    decision = decide_for(samples)
    assert decision["verdict"] == "BLOCK" and reason_codes(decision) == ["candidate_correctness_failure"]
    samples = baseline_world(base_fail={1})
    for s in samples:
        if s["side"] == "candidate" and s["scenario"] == "health_status_overhead" and s["rep"] == 2:
            s.update(outcome="refused")
            s["grade"].update(correct=False, refusal=True, reasons=["refused"])
    assert {"new_refusal", "candidate_correctness_failure"} <= set(reason_codes(decide_for(samples)))


def test_baseline_lifecycle_rows_refused_effects_are_recorded_but_state_changes_and_mutations_stay_inconclusive():
    clean = {"records": 3, "blocked_effects": 0, "blocked_targets": [], "engine_state_changes": 0,
             "engine_mutating_calls": 0}
    samples = baseline_world()
    refused = {**clean, "blocked_effects": 2, "blocked_targets": ["exec:omlx-cli"]}
    decision = decide_for(samples, {"candidate": clean, "baseline": refused})
    assert decision["verdict"] == "PASS" and decision["reasons"] == []
    assert [(r["code"]) for r in decision["recorded"]] == ["baseline_blocked_effect"]
    assert "exec:omlx-cli" in decision["recorded"][0]["detail"]
    for field in ("engine_state_changes", "engine_mutating_calls"):
        bad = decide_for(samples, {"candidate": clean, "baseline": {**refused, field: 1}})
        assert bad["verdict"] == "INCONCLUSIVE" and reason_codes(bad) == ["baseline_lifecycle_effect"], field
        assert [r["code"] for r in bad["recorded"]] == ["baseline_blocked_effect"]
        alone = decide_for(samples, {"candidate": clean, "baseline": {**clean, field: 1}})
        assert alone["verdict"] == "INCONCLUSIVE" and reason_codes(alone) == ["baseline_lifecycle_effect"], field


def test_the_candidate_side_of_the_lifecycle_is_unchanged_any_refused_effect_state_change_or_mutation_blocks():
    clean = {"records": 3, "blocked_effects": 0, "blocked_targets": [], "engine_state_changes": 0,
             "engine_mutating_calls": 0}
    samples = baseline_world()
    for field in ("blocked_effects", "engine_state_changes", "engine_mutating_calls"):
        decision = decide_for(samples, {"candidate": {**clean, field: 1}, "baseline": clean})
        assert decision["verdict"] == "BLOCK" and reason_codes(decision) == ["lifecycle_blocked_effect"], field
    both = decide_for(samples, {"candidate": {**clean, "blocked_effects": 1}, "baseline": {**clean, "blocked_effects": 1}})
    assert both["verdict"] == "BLOCK"                                              # a refused baseline effect never excuses the candidate


def test_every_other_outcome_row_is_unchanged():
    samples = baseline_world()
    assert decide_for(samples)["verdict"] == "PASS"
    missing = decide_for(samples, baseline={"approved": False, "problem": "no approved baseline"})
    assert missing["verdict"] == "INCONCLUSIVE" and reason_codes(missing) == ["missing_or_unapproved_baseline"]
    unknown = baseline_world()
    for s in unknown:
        if s["side"] == "candidate" and s["scenario"] == "greeting_warm" and s["rep"] == 2:
            s["metrics_ns"]["first_model_delta_s"] = UNKNOWN
    assert reason_codes(decide_for(unknown)) == ["unknown_gating_metric"] * 2
    unverifiable = baseline_world()
    for s in unverifiable:
        if s["side"] == "candidate" and s["scenario"] == "deterministic_read" and s["rep"] == 4:
            s["grade"].update(correct=False, reasons=[], unverifiable=["model_calls"])
    assert reason_codes(decide_for(unverifiable)) == ["unverifiable_expectation"]


def test_the_policy_is_a_new_version_with_a_new_hash_and_the_changed_rows_are_what_the_bundle_declares():
    assert POLICY["version"] == "release_policy_v3" and CORPUS["version"] == "release_v3"
    assert BUNDLE["policy_sha256"] != "729b14ccdef74d3885177a8fe3fdd24fdeb89d670947a5c65f8f68cbbc25eb07"   # v2
    assert BUNDLE["corpus_sha256"] != "517541e018e9df8ae6986eacdb58aa5c8c56c71e76169227dbd8338ca1a8ebcf"   # v2
    out = POLICY["outcomes"]
    assert out["baseline_correctness_failure"] == "RECORDED_ONLY" and out["baseline_blocked_effect"] == "RECORDED_ONLY"
    assert out["baseline_insufficient_correct_samples"] == "INCONCLUSIVE" and out["baseline_lifecycle_effect"] == "INCONCLUSIVE"
    assert out["candidate_correctness_failure"] == out["new_refusal"] == out["measured_material_regression"] == "BLOCK"
    assert out["lifecycle_blocked_effect"] == "BLOCK" and POLICY["min_measured_samples"] == 20
    assert POLICY["regression"] == {"relative_increase": "0.20", "absolute_increase_ms": 250, "rule": "both_exceed"}
    assert POLICY["advisory"]["recommended_measured_samples"] == 30


@pytest.mark.parametrize("edit,needle", [
    (lambda b: b["policy"]["outcomes"].__setitem__("baseline_correctness_failure", "INCONCLUSIVE"), "baseline_correctness_failure"),
    (lambda b: b["policy"]["outcomes"].__setitem__("baseline_blocked_effect", "BLOCK"), "baseline_blocked_effect"),
    (lambda b: b["policy"]["outcomes"].__setitem__("baseline_insufficient_correct_samples", "RECORDED_ONLY"),
     "baseline_insufficient_correct_samples"),
    (lambda b: b["policy"]["outcomes"].__setitem__("baseline_unverifiable_sample", "RECORDED_ONLY"), "baseline_unverifiable_sample"),
    (lambda b: b["policy"]["outcomes"].pop("baseline_insufficient_correct_samples"), "baseline_insufficient_correct_samples")])
def test_the_relaxed_baseline_rows_cannot_be_relaxed_further_by_editing_the_bundle(edit, needle):
    bundle = copy.deepcopy(BUNDLE)
    edit(bundle)
    assert any(needle in problem for problem in rp.validate_bundle(bundle))


def test_unverifiable_expectations_and_unknown_metrics_are_inconclusive_never_pass():
    samples = world()
    for s in samples:
        if s["side"] == "candidate" and s["scenario"] == "deterministic_read" and s["rep"] == 4:
            s["grade"].update(correct=False, reasons=[], unverifiable=["model_calls"])
    decision = rp.decide(rp.summarize(samples, BUNDLE), BUNDLE, APPROVED)
    assert decision["verdict"] == "INCONCLUSIVE"
    assert any(r["code"] == "unverifiable_expectation" for r in decision["reasons"])

    samples = world()
    for s in samples:
        if s["side"] == "candidate" and s["scenario"] == "greeting_warm" and s["rep"] == 2:
            s["metrics_ns"]["first_model_delta_s"] = UNKNOWN
    decision = rp.decide(rp.summarize(samples, BUNDLE), BUNDLE, APPROVED)
    assert decision["verdict"] == "INCONCLUSIVE"
    assert any(r["code"] == "unknown_gating_metric" for r in decision["reasons"])


def test_cpu_and_count_diagnostics_are_reported_separately_and_unknown_is_kept():
    samples = world()
    for s in samples:
        if s["side"] == "candidate" and s["scenario"] == "greeting_warm":
            s["diagnostics"]["peer_checks"] = UNKNOWN
            s["diagnostics"]["backend_cpu_s"] = UNKNOWN if s["rep"] == 1 else 0.25
    cell = rp.summarize(samples, BUNDLE)["greeting_warm"]["candidate"]
    assert cell["diagnostics"]["peer_checks"]["p50"] == UNKNOWN
    assert cell["backend_cpu"]["p50_s"] == UNKNOWN
    healthy = rp.summarize(world(), BUNDLE)["greeting_warm"]["candidate"]
    assert healthy["backend_cpu"]["p50_s"] == 0.1 and healthy["diagnostics"]["engine_http_calls"]["p50"] == 1


def test_percentage_check_uses_exact_arithmetic_not_floats():
    assert rp.is_regression(1_200_000_001 * 1, 1_000_000_000, POLICY) is False      # >250 ms? 200.000001 ms: no
    assert rp.is_regression(1_300_000_000, 1_000_000_000, POLICY) is True
    assert rp.is_regression(UNKNOWN, 1, POLICY) == UNKNOWN and rp.is_regression(1, UNKNOWN, POLICY) == UNKNOWN
    assert rp.ms(1_234_567) == 1.235 and rp.ms(UNKNOWN) == UNKNOWN


# ----------------------------------------------------------------------------
# Constructed evidence: the REAL run_release, graders, evaluator and checker,
# driven by a scripted fake driver inside pytest temp directories.
# ----------------------------------------------------------------------------

def git(*args, cwd, env_extra=None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    base = tmp_path_factory.mktemp("subjects")
    repo = base / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("one\n")
    git("add", "a.txt", cwd=repo)
    git("commit", "-q", "-m", "baseline", cwd=repo)
    base_sha = git("rev-parse", "HEAD", cwd=repo)
    (repo / "a.txt").write_text("two\n")
    git("commit", "-q", "-am", "candidate", cwd=repo)
    cand_sha = git("rev-parse", "HEAD", cwd=repo)
    base_wt, cand_wt = base / "base_wt", base / "cand_wt"
    git("worktree", "add", "-q", "--detach", str(base_wt), base_sha, cwd=repo)
    git("worktree", "add", "-q", "--detach", str(cand_wt), cand_sha, cwd=repo)
    return SimpleNamespace(repo=repo, base_sha=base_sha, cand_sha=cand_sha, base_wt=base_wt, cand_wt=cand_wt)


MODEL_ID = "Test-Model-oQ4e"
RUNTIME = {"python_version": "3.14.3", "implementation": "cpython", "executable": "/py/bin/python",
           "executable_sha256": "a" * 64, "platform": "darwin"}
GUARD_SHA = "9" * 64
REQUEST_SETTINGS = {"model": MODEL_ID, "temperature": 0.7, "max_tokens": 1024, "stream": False, "tool_count": 0}
BASE_TIMES_MS = {"greeting_warm": (200, 260), "bounded_reasoning": (900, 905),
                 "scoped_tool_selection": (1800, 1805), "deterministic_read": (120, 125),
                 "outbound_approval_boundary": (1500, 2100), "health_status_overhead": (12, 12)}
CHAT_CALLS = {"greeting_warm": 1, "bounded_reasoning": 1, "scoped_tool_selection": 2,
              "deterministic_read": 0, "outbound_approval_boundary": 1, "health_status_overhead": 0}


def retime(events, scale, add_ms):
    return [{"t_ns": int(e["t_ns"] * scale + add_ms * MS), "event": e["event"]} for e in events]


def scripted(spec_id, kind, scale, add_ms):
    """Plausible events for each scenario; `kind` injects a specific failure."""
    chats = CHAT_CALLS[spec_id]
    extra = {}
    if spec_id == "greeting_warm":
        events = OK_GREETING if kind != "empty" else [ev(1, type="session", id="s"), ev(260, type="done")]
    elif spec_id == "bounded_reasoning":
        events = [ev(1, type="session", id="s"), ev(450, type="reasoning", text="working it out"),
                  ev(900, type="text", text="It arrives at 6:30 PM."), ev(905, type="done")]
    elif spec_id == "scoped_tool_selection":
        events = selection_events()
    elif spec_id == "deterministic_read":
        events = deterministic_events()
        if kind == "model_call":
            chats = 1
    elif spec_id == "outbound_approval_boundary":
        events = outbound_events(answer="Done! I've sent the message." if kind == "false_success"
                                 else "I didn't send it. The message needs your approval first.")
        extra["approvals"] = [] if kind == "denial_missing" else [
            {"t_ns": 1, "action_id": "c1", "session_id": "s", "approved": False,
             "accepted": kind != "denial_unaccepted"}]
    else:
        events = []
        extra.update(http={"status": 200, "json": {"status": "ok"}, "text": ""},
                     latency_ns=int((12 * scale + add_ms) * MS))
    if kind == "refusal":
        events = [ev(5, type="error", message="Wisp couldn't verify the local AI engine (oMLX) just now."),
                  ev(6, type="done")] if spec_id != "health_status_overhead" else events
        if spec_id == "health_status_overhead":
            extra["http"] = {"status": 503, "json": {"detail": "Wisp couldn't verify the local AI engine"}, "text": ""}
    return retime(events, scale, add_ms), extra, chats


class FakeBackend:
    """Writes the same log shape the real child does: a header, a surfaces record, then gap-free records."""

    def __init__(self, deps, side, root, instrument_path, run_id=""):
        self.deps, self.side, self.root, self.instrument_path = deps, side, Path(root), Path(instrument_path)
        self.port = 18775 if side == "candidate" else 18776
        self.python = sys.executable
        self.pid = 4000 + (side == "baseline")
        self.stopped, self._cpu, self._seq, self.residency_calls = False, 0.0, 0, 0
        self.write("header", root=str(self.root.resolve()), harness=rp.HARNESS_VERSION, run_id=run_id, side=side,
                   effect_guard=True, guard_policy_sha256=deps.guard_sha.get(side, GUARD_SHA),
                   runtime=deps.runtime.get(side, RUNTIME))
        self.unavailable = list(deps.unavailable) + list(deps.unavailable_by_side.get(side, []))
        self.write("surfaces", available=[n for n in rp.SURFACES if n not in self.unavailable],
                   unavailable=list(self.unavailable))
        for record in deps.startup_events.get(side, []):
            self.write(**record)

    def write(self, kind, **fields):
        record = {"kind": kind, "seq": self._seq, "pid": self.pid, "t_ns": self.deps.clock_ns(), **fields}
        self._seq += 1
        with self.instrument_path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def wait_ready(self, timeout_s=0):
        return True

    def stop(self):
        self.stopped = True
        failure = self.deps.teardown.get(self.side)
        if failure == "raise":
            raise RuntimeError("teardown exploded")
        return {"side": self.side, "pid": self.pid, "terminated": True, "killed": False, "exited": failure is None,
                "errors": [] if failure is None else ["wait:TimeoutExpired"], "ok": failure is None}

    def cpu_seconds(self):
        self._cpu += 0.01
        return round(self._cpu, 3)


class FakeDriver:
    def __init__(self, deps, backend):
        self.deps, self.backend = deps, backend

    async def run_turn(self, spec, prompt, sink):
        await self._run(spec, sink)

    async def run_http(self, spec, sink):
        await self._run(spec, sink)

    async def _run(self, spec, sink):
        deps, side, backend = self.deps, self.backend.side, self.backend
        deps.total_calls += 1
        sink["abs_start_ns"] = deps.clock_ns()
        try:
            if deps.cancel_at and deps.total_calls == deps.cancel_at:
                if deps.cancel_partial:
                    sink["events"] = [ev(1, type="session", id="s"), ev(2, type="delta", text="Hel")]
                raise asyncio.CancelledError
            n = deps.calls[(side, spec["id"])] = deps.calls.get((side, spec["id"]), 0) + 1
            behavior = deps.behaviors.get((side, spec["id"]), "ok")
            kind = behavior(n) if callable(behavior) else behavior
            scale, add = (deps.cand_mult, deps.cand_add_ms) if side == "candidate" else (deps.base_mult, 0.0)
            add += (n * 7 % 11) * 0.1
            events, extra, chats = scripted(spec["id"], kind, scale, add)
            sink.update(events=events, **extra)
            start = sink["abs_start_ns"]
            reason = {"truncated": "length", "other": "content_filter"}.get(kind, "stop")
            if "engine_http" not in backend.unavailable:
                for i in range(chats):
                    backend.write("engine_http", t_ns=start + 1000 + 10 * i, method="POST", path="/v1/chat/completions",
                                  status=200, t_start_ns=start, t_headers_ns=start + 500,
                                  request_settings=deps.request_settings.get(side, REQUEST_SETTINGS))
                    if "engine_completion" not in backend.unavailable and kind != "no_completion":
                        backend.write("engine_completion", t_ns=start + 1005 + 10 * i, finish_reason=reason, n_choices=1,
                                      parse="ok", complete_stream=True)
                if spec["kind"] == "http":
                    backend.write("engine_http", t_ns=start + 2000, method="GET", path="/health", status=200,
                                  t_start_ns=start, t_headers_ns=start + 900)
            if (side, spec["id"]) in deps.interference:
                other = deps.backends["baseline" if side == "candidate" else "candidate"]
                other.write("engine_http", t_ns=start + 3000, method="POST", path="/v1/chat/completions", status=200,
                            t_start_ns=start, t_headers_ns=start + 3100, request_settings=REQUEST_SETTINGS)
            span = max([e["t_ns"] for e in events] + [sink.get("latency_ns") or 0, 4000])
            deps._clock += span
        finally:
            sink["abs_end_ns"] = deps.clock_ns()


class ConstructedEvidenceDeps:
    """Builds evidence inside pytest temp dirs. It measures nothing."""

    def __init__(self, ctx, *, is_real=True, cand_mult=1.0, cand_add_ms=0.0, base_mult=1.0, behaviors=None,
                 unavailable=(), missing=None, cancel_at=None, cancel_partial=False, dirty_after=False,
                 now_ts=1_800_000_000.0, teardown=None, startup_events=None, runtime=None, guard_sha=None,
                 request_settings=None, residency=None, env_drift=False, interference=(),
                 unavailable_by_side=None, env_after_missing=False):
        self.ctx, self.is_real = ctx, is_real
        self.cand_mult, self.cand_add_ms, self.base_mult = cand_mult, cand_add_ms, base_mult
        self.behaviors, self.unavailable = behaviors or {}, list(unavailable)
        self.missing, self.cancel_at, self.dirty_after, self.now_ts = missing or [], cancel_at, dirty_after, now_ts
        self.cancel_partial, self.teardown, self.startup_events = cancel_partial, teardown or {}, startup_events or {}
        self.runtime, self.guard_sha, self.request_settings = runtime or {}, guard_sha or {}, request_settings or {}
        self.residency_plan, self.env_drift, self.interference = residency or {}, env_drift, set(interference)
        self.calls, self.total_calls, self._clock, self._tick, self.spawned = {}, 0, 10_000_000_000, 0, []
        self.backends, self.env_calls = {}, 0
        self.unavailable_by_side, self.env_after_missing = unavailable_by_side or {}, env_after_missing
        self._verifies: dict[str, int] = {}

    def clock_ns(self):
        self._clock += 1_000_000
        return self._clock

    def now(self):
        return self.now_ts

    def _ticker(self):
        self._tick += 1
        return self.now_ts + self._tick

    def verify_subject(self, root, expected):
        record = rp.verify_subject(root, expected, clock=self._ticker)
        key = str(root)
        self._verifies[key] = self._verifies.get(key, 0) + 1
        if self.dirty_after and self._verifies[key] > 1:
            record["clean"], record["problems"] = False, ["worktree is not clean"]
        return record

    def collect_environment(self, model_id, lane, warmups):
        self.env_calls += 1
        if self.env_after_missing and self.env_calls > 1:
            return {}
        build = "6" if not (self.env_drift and self.env_calls > 1) else "7"
        return {"hardware": {"model": "Mac17,8", "chip": "Apple M5 Pro", "memory_bytes": "25769803776", "cpu_count": "18"},
                "os": {"product_version": "27.0", "build": "TEST", "kernel": "27.0.0"},
                "python": {"version": "3.14.3", "executable": sys.executable},
                "engine": {"name": "oMLX", "app": {"short": "0.6.4", "build": build}, "port": 8000},
                "model": {"id": model_id, "dir_found": True, "config_sha256": "c" * 64, "tokenizer_sha256": "d" * 64,
                          "quantization": {"bits": 4, "group_size": 64}, "weights_manifest": {"sha256": "e" * 64}},
                "generation_settings": {"model_settings": {"temperature": 1.0}, "request_settings": "observed per call"},
                "cache_warmup_treatment": {"cache_cleared_before_run": False, "warmup_samples_per_scenario_per_side": warmups},
                "power": "AC Power"}

    def preflight(self, lane, model_id):
        return list(self.missing)

    def spawn_backend(self, side, root, home, instrument_path, run_id=""):
        backend = FakeBackend(self, side, root, instrument_path, run_id)
        self.spawned.append(backend)
        self.backends[side] = backend
        return backend

    def residency(self, backend):
        backend.residency_calls += 1
        moment = "before" if backend.residency_calls == 1 else "after"
        return self.residency_plan.get((backend.side, moment), [MODEL_ID])

    def make_driver(self, backend):
        return FakeDriver(self, backend)

    def push_leaves(self, backend, payloads):
        return [200] * len(payloads)


def run_evidence(tmp_path, ctx, name="evidence", *, lane="desktop", samples=None, diagnostic=False,
                 scenario_ids=None, approved=None, **deps_kwargs):
    deps = ConstructedEvidenceDeps(ctx, **deps_kwargs)
    out = tmp_path / name
    opts = {"candidate_root": ctx.cand_wt, "candidate_sha": ctx.cand_sha, "baseline_root": ctx.base_wt,
            "baseline_sha": ctx.base_sha, "lane": lane, "model_id": MODEL_ID, "output_dir": out,
            "scenario_ids": scenario_ids, "samples": samples, "diagnostic": diagnostic,
            "approved_baseline_path": str(approved[0]) if approved else None,
            "approved_baseline_sha256": approved[1] if approved else None}
    code, receipt_path = asyncio.run(rp.run_release(opts, deps, rp.load_bundle()))
    return SimpleNamespace(out=out, receipt_path=receipt_path, code=code, deps=deps, opts=opts,
                           receipt=json.loads(Path(receipt_path).read_text()))


def write_approval(directory, receipt, **override):
    doc = {"schema": rp.BASELINE_SCHEMA, "status": "approved", "approved_by": "reviewer",
           "review_ref": "audit-1", "approved_at": "2026-10-03T00:00:00Z",
           "subject": {"sha": receipt["baseline"]["sha"], "tree": receipt["baseline"]["tree"]},
           "cohort_key": rp.cohort_key(receipt)}
    doc.update(override)
    path = Path(directory) / "approved_baseline.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return path, rp.sha256_file(path)


def seal(ev_dir, receipt_path):
    """Re-bind the receipt to the raw files after a deliberate edit, to prove deeper checks still catch it."""
    receipt = json.loads(Path(receipt_path).read_text())
    files = rp.raw_manifest(Path(ev_dir) / "raw")
    receipt["raw"] = {"files": files, "manifest_sha256": rp.canonical_sha(files)}
    Path(receipt_path).write_text(json.dumps(receipt, indent=2, sort_keys=True))


def pass_evidence(tmp_path, ctx, name="pass", **kwargs):
    """First run proposes the cohort, a reviewer approves it, the second run is bound to that approval."""
    first = run_evidence(tmp_path, ctx, name + "_first", **kwargs)
    assert first.code in (rp.EXIT_INCONCLUSIVE, rp.EXIT_BLOCK)   # no baseline yet: a qualification run can never pass
    approval = write_approval(tmp_path, first.receipt)
    second = run_evidence(tmp_path, ctx, name, approved=approval, **kwargs)
    second.approval = approval
    return second


FIXTURE_SOURCE = "ConstructedEvidenceDeps"


def check_opts(evd, ctx, *, approval=True, **overrides):
    """Options for the checker through its API-only fixture seam. The receipt digest is taken from the file as it
    is NOW, standing in for the digest a measurement owner would have recorded; a result obtained this way
    never authorizes a release (see test_fixture_verification_never_authorizes_a_release)."""
    receipt = evd.receipt
    path, digest = getattr(evd, "approval", (None, None)) if approval is True else (approval or (None, None))
    receipt_file = Path(evd.receipt_path)
    values = dict(receipt=evd.receipt_path, bundle=rp.DEFAULT_BUNDLE, expect_candidate_sha=ctx.cand_sha,
                  expect_corpus_sha256=BUNDLE["corpus_sha256"], expect_policy_sha256=BUNDLE["policy_sha256"],
                  expect_harness_sha256=rp.harness_sha256(), approved_baseline=path,
                  approved_baseline_sha256=digest,
                  expect_receipt_sha256=rp.sha256_file(receipt_file) if receipt_file.is_file() else None,
                  accept_fixture_source=FIXTURE_SOURCE, lane=receipt["lane"],
                  repo=ctx.repo, candidate_worktree=None, baseline_worktree=None,
                  now=evd.deps.now_ts + 60)
    values.update(overrides)
    return argparse.Namespace(**values)


def refusal_codes(result):
    return {r["code"] for r in result["refusals"]}


@pytest.fixture(scope="module")
def good(tmp_path_factory, ctx):
    base = tmp_path_factory.mktemp("good")
    return pass_evidence(base, ctx)


def test_the_real_orchestration_writes_labelled_warmups_alternating_order_and_a_sealed_manifest(good):
    receipt = good.receipt
    assert receipt["execution_mode"] == rp.ACTUAL_MODE and receipt["inference"]["fake_model"] is False
    raw = good.out / "raw"
    samples = [json.loads(l) for l in (raw / "samples.jsonl").read_text().splitlines()]
    assert len(samples) == len(REQUIRED) * 2 * (2 + 20)                    # 2 warmups + 20 measured, both sides
    assert {s["phase"] for s in samples} == {"warmup", "measured"}
    first = [s for s in samples if s["scenario"] == "greeting_warm" and s["phase"] == "measured"]
    order = {(s["rep"], s["order_slot"]): s["side"] for s in first}
    assert order[(1, 0)] == "candidate" and order[(2, 0)] == "baseline" and order[(3, 0)] == "candidate"
    assert receipt["raw"]["files"] == rp.raw_manifest(raw)
    assert (raw / "provenance.json").is_file() and (raw / "instrumentation.candidate.jsonl").is_file()
    assert receipt["verdict"] == "PASS" and good.code == rp.EXIT_PASS
    for side in rp.SIDES:                                                    # both verified before AND after
        subject = receipt[side]
        assert subject["verified_before"]["clean"] and subject["verified_after"]["clean"]
        assert subject["verified_before"]["head"] == subject["sha"] == subject["verified_after"]["head"]


def test_checker_passes_only_fully_verified_evidence_and_rederives_the_verdict(good, ctx):
    code, result = rp.check_receipt(check_opts(good, ctx))
    assert (code, result["verdict"], result["refusals"]) == (rp.EXIT_PASS, "PASS", [])
    row = result["comparison"]["bounded_reasoning"]["required_metrics"]["total_completion_s"]
    assert row["n_candidate"] == 20 and row["p50"]["regression"] is False


def cli_check(good, ctx, *, receipt=None, digest="auto", extra=()):
    approval = good.approval
    argv = [sys.executable, str(SCRIPT), "check", "--receipt", str(receipt or good.receipt_path),
            "--expect-candidate-sha", ctx.cand_sha, "--expect-corpus-sha256", BUNDLE["corpus_sha256"],
            "--expect-policy-sha256", BUNDLE["policy_sha256"], "--expect-harness-sha256", rp.harness_sha256(),
            "--approved-baseline", str(approval[0]), "--approved-baseline-sha256", approval[1],
            "--repo", str(ctx.repo), "--now", str(good.deps.now_ts + 60), *extra]
    if digest == "auto":
        digest = rp.sha256_file(good.receipt_path)
    if digest:
        argv += ["--expect-receipt-sha256", digest]
    return subprocess.run(argv, capture_output=True, text=True)


def test_the_cli_needs_the_recorded_digest_and_refuses_constructed_evidence_even_with_it(good, ctx, tmp_path):
    assert cli_check(good, ctx, digest=None).returncode == rp.EXIT_USAGE      # a required argument, never INCONCLUSIVE's 2
    done = cli_check(good, ctx)
    assert done.returncode == rp.EXIT_REFUSED                                  # right digest, still not a release-capable source
    assert "measurement_source_not_release_capable" in {r["code"] for r in json.loads(done.stdout)["refusals"]}
    assert json.loads(done.stdout)["authorizes_release"] is False
    wrong = cli_check(good, ctx, digest="0" * 64)
    assert wrong.returncode == rp.EXIT_REFUSED
    assert "receipt_digest_mismatch" in {r["code"] for r in json.loads(wrong.stdout)["refusals"]}
    assert cli_check(good, ctx, receipt=tmp_path / "missing.json", digest="0" * 64).returncode == rp.EXIT_REFUSED
    usage = subprocess.run([sys.executable, str(SCRIPT), "check", "--receipt", "x"], capture_output=True, text=True)
    assert usage.returncode == rp.EXIT_USAGE            # never collides with INCONCLUSIVE (2)


def test_the_cli_exit_codes_and_release_authority_when_the_source_is_release_capable(good, ctx, monkeypatch, capsys):
    """Wiring only: the live class is stood in for by patching the accepted class name in this process. This
    shows how exit codes and `authorizes_release` follow from a verified PASS; it is not a performance claim."""
    monkeypatch.setattr(rp, "LIVE_DEPS_CLASS", FIXTURE_SOURCE)
    approval = good.approval
    base = ["check", "--receipt", str(good.receipt_path), "--expect-candidate-sha", ctx.cand_sha,
            "--expect-corpus-sha256", BUNDLE["corpus_sha256"], "--expect-policy-sha256", BUNDLE["policy_sha256"],
            "--expect-harness-sha256", rp.harness_sha256(), "--approved-baseline", str(approval[0]),
            "--approved-baseline-sha256", approval[1], "--repo", str(ctx.repo), "--now", str(good.deps.now_ts + 60)]
    assert rp.main([*base, "--expect-receipt-sha256", rp.sha256_file(good.receipt_path)]) == rp.EXIT_PASS
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "PASS" and out["authorizes_release"] is True and out["fixture_source"] is False
    assert rp.main([*base, "--expect-receipt-sha256", "0" * 64]) == rp.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["authorizes_release"] is False


def test_a_receipt_cannot_claim_a_pass_the_evidence_does_not_support(good, ctx):
    # The receipt says PASS, but without the reviewed approval the evidence only supports INCONCLUSIVE.
    code, result = rp.check_receipt(check_opts(good, ctx, approval=None))
    assert refusal_codes(result) == {"receipt_verdict_mismatch"} and code == rp.EXIT_REFUSED


def test_a_qualification_run_without_an_approved_baseline_is_inconclusive_never_a_pass(tmp_path, ctx):
    first = run_evidence(tmp_path, ctx, "qual")
    assert first.receipt["verdict"] == "INCONCLUSIVE" and first.code == rp.EXIT_INCONCLUSIVE
    code, result = rp.check_receipt(check_opts(first, ctx, approval=None))
    assert code == rp.EXIT_INCONCLUSIVE and result["verdict"] == "INCONCLUSIVE" and result["refusals"] == []
    assert any(r["code"] == "missing_or_unapproved_baseline" for r in result["reasons"])


def test_approval_that_is_not_digest_bound_or_not_approved_does_not_count(good, ctx, tmp_path):
    path, digest = good.approval
    code, result = rp.check_receipt(check_opts(good, ctx, approval=(path, None)))
    assert "approved_baseline_digest_not_bound" in refusal_codes(result) and code == rp.EXIT_REFUSED
    code, result = rp.check_receipt(check_opts(good, ctx, approval=(path, "f" * 64)))
    assert "approved_baseline_digest_mismatch" in refusal_codes(result)
    proposal = write_approval(tmp_path, good.receipt, status="proposed", approved_by=None)
    code, result = rp.check_receipt(check_opts(good, ctx, approval=proposal))
    assert code == rp.EXIT_REFUSED and "approved_baseline_not_approved" in refusal_codes(result)
    wrong_subject = write_approval(tmp_path, good.receipt, subject={"sha": ctx.cand_sha, "tree": good.receipt["candidate"]["tree"]})
    code, result = rp.check_receipt(check_opts(good, ctx, approval=wrong_subject))
    assert {"baseline_subject_not_the_approved_one", "baseline_equals_candidate"} <= refusal_codes(result)


def test_new_cohort_is_inconclusive_pending_a_reviewed_baseline(tmp_path, ctx):
    first = run_evidence(tmp_path, ctx, "c1")
    other_cohort = rp.cohort_key(first.receipt)
    other_cohort["engine"] = {"name": "oMLX", "app": {"short": "9.9", "build": "9"}, "port": 8000}
    approval = write_approval(tmp_path, first.receipt, cohort_key=other_cohort)
    second = run_evidence(tmp_path, ctx, "c2", approved=approval)
    assert second.receipt["verdict"] == "INCONCLUSIVE" and second.code == rp.EXIT_INCONCLUSIVE
    second.approval = approval
    code, result = rp.check_receipt(check_opts(second, ctx))
    assert code == rp.EXIT_INCONCLUSIVE and result["verdict"] == "INCONCLUSIVE"
    assert any("new cohort" in r["detail"] for r in result["reasons"])


def test_cohort_identity_ignores_the_commit_but_not_the_environment(good):
    receipt = copy.deepcopy(good.receipt)
    base = rp.cohort_key(receipt)
    receipt["candidate"]["sha"], receipt["baseline"]["sha"] = "a" * 40, "b" * 40
    assert rp.cohort_key(receipt) == base                          # commits are different subjects BY DESIGN
    for path, value in ((("environment", "os"), {"build": "OTHER"}), (("lane",), "managed_mini"),
                        (("harness", "script_sha256"), "0" * 64), (("corpus", "sha256"), "0" * 64),
                        (("policy", "sha256"), "0" * 64), (("environment", "model"), {"id": "x"})):
        mutated = copy.deepcopy(good.receipt)
        target = mutated
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert rp.cohort_key(mutated) != base, path


# ---- regressions and correctness through the whole pipeline ----

def test_material_regression_blocks_through_the_checker(tmp_path, ctx):
    slow = pass_evidence(tmp_path, ctx, "slow", cand_mult=1.5, cand_add_ms=400.0)
    assert slow.receipt["verdict"] == "BLOCK"
    code, result = rp.check_receipt(check_opts(slow, ctx))
    assert code == rp.EXIT_BLOCK and result["verdict"] == "BLOCK"
    assert any(r["code"] == "measured_material_regression" for r in result["reasons"])


def test_a_small_slowdown_under_both_thresholds_still_passes(tmp_path, ctx):
    small = pass_evidence(tmp_path, ctx, "small", cand_mult=1.1, cand_add_ms=20.0)
    code, result = rp.check_receipt(check_opts(small, ctx))
    assert code == rp.EXIT_PASS, result


def test_correctness_failures_block_even_when_they_are_much_faster(tmp_path, ctx):
    fast_but_wrong = pass_evidence(tmp_path, ctx, "wrong", cand_mult=0.2,
                                   behaviors={("candidate", "greeting_warm"): lambda n: "empty" if n % 5 == 0 else "ok",
                                              ("candidate", "outbound_approval_boundary"): "false_success"})
    code, result = rp.check_receipt(check_opts(fast_but_wrong, ctx))
    codes = {r["code"] for r in result["reasons"]}
    assert code == rp.EXIT_BLOCK and "candidate_correctness_failure" in codes
    cell = fast_but_wrong.receipt["summary"]["outbound_approval_boundary"]["candidate"]
    assert cell["correct"] == 0 and cell["failed"] == 20
    assert cell["metrics"]["total_completion_s"]["p50_ns"] == UNKNOWN       # no correct sample, so nothing "fast" is reported


def test_a_new_attribution_refusal_blocks(tmp_path, ctx):
    refused = pass_evidence(tmp_path, ctx, "refused",
                            behaviors={("candidate", "health_status_overhead"): lambda n: "refusal" if n == 3 else "ok"})
    code, result = rp.check_receipt(check_opts(refused, ctx))
    assert code == rp.EXIT_BLOCK and any(r["code"] == "new_refusal" for r in result["reasons"])


def test_model_call_in_the_zero_generation_scenario_blocks(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "gen", behaviors={("candidate", "deterministic_read"): "model_call"})
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_BLOCK


def test_missing_instrumentation_makes_the_zero_generation_check_unverifiable_not_passing(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "blind", unavailable=("engine_http",))
    summary = evd.receipt["summary"]["deterministic_read"]["candidate"]
    assert summary["unverifiable"] == 20 and summary["diagnostics"]["engine_http_calls"]["p50"] == UNKNOWN
    assert evd.receipt["verdict"] == "INCONCLUSIVE" and evd.code == rp.EXIT_INCONCLUSIVE
    evd.approval = write_approval(tmp_path, evd.receipt)
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code in (rp.EXIT_REFUSED, rp.EXIT_INCONCLUSIVE) and code != rp.EXIT_PASS


# ---- forged, stale, wrong-head, mocked, under-sampled, incomparable, tampered ----

def clone(good, tmp_path, name="clone"):
    target = tmp_path / name
    shutil.copytree(good.out, target)
    evd = SimpleNamespace(**vars(good))
    evd.out, evd.receipt_path = target, target / "receipt.json"
    evd.receipt = json.loads(evd.receipt_path.read_text())
    return evd


def test_a_forged_summary_in_the_receipt_is_refused(good, ctx, tmp_path):
    evd = clone(good, tmp_path)
    receipt = evd.receipt
    receipt["summary"]["greeting_warm"]["candidate"]["metrics"]["total_completion_s"]["p95_ns"] = 1
    evd.receipt_path.write_text(json.dumps(receipt))
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED and "summary_mismatch" in refusal_codes(result)


def test_edited_raw_files_fail_the_digest_check(good, ctx, tmp_path):
    evd = clone(good, tmp_path)
    samples = evd.out / "raw" / "samples.jsonl"
    samples.write_text(samples.read_text().replace('"outcome": "ok"', '"outcome": "ok" ', 1))
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED and "raw_digest_mismatch" in refusal_codes(result)
    (evd.out / "raw" / "extra.jsonl").write_text("{}\n")
    assert "raw_file_not_declared" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


def test_raw_edits_resealed_with_a_matching_manifest_still_fail_re_derivation(good, ctx, tmp_path):
    evd = clone(good, tmp_path)
    samples_path = evd.out / "raw" / "samples.jsonl"
    rows = [json.loads(l) for l in samples_path.read_text().splitlines()]
    for row in rows:
        if row["side"] == "candidate" and row["scenario"] == "greeting_warm" and row["phase"] == "measured":
            row["metrics_ns"]["total_completion_s"] = 1            # claim an impossibly fast completion
    samples_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    seal(evd.out, evd.receipt_path)
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED and any(r["code"] == "raw_derivation_mismatch" for r in result["refusals"])


def test_events_edited_to_hide_a_failure_are_caught_by_regrading(good, ctx, tmp_path):
    evd = clone(good, tmp_path)
    events_path = evd.out / "raw" / "events.jsonl"
    rows = [json.loads(l) for l in events_path.read_text().splitlines()]
    for row in rows:
        if row["id"].startswith("candidate:greeting_warm:measured"):
            row["events"] = [e for e in row["events"] if e["event"].get("type") != "delta"]  # remove the answer
    events_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    seal(evd.out, evd.receipt_path)
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED and "raw_derivation_mismatch" in refusal_codes(result)


def test_a_record_removed_from_the_instrumentation_log_leaves_a_gap_and_is_refused(good, ctx, tmp_path):
    evd = clone(good, tmp_path)
    (evd.out / "raw" / "instrumentation.candidate.jsonl").write_text(
        "".join(l for l in (evd.out / "raw" / "instrumentation.candidate.jsonl").read_text().splitlines(True)
                if '"/health"' not in l))
    seal(evd.out, evd.receipt_path)
    assert "instrumentation_log_invalid" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


def test_wrong_candidate_sha_wrong_hashes_and_stale_or_future_receipts_are_refused(good, ctx):
    code, result = rp.check_receipt(check_opts(good, ctx, expect_candidate_sha=ctx.base_sha))
    assert code == rp.EXIT_REFUSED and "candidate_sha_mismatch" in refusal_codes(result)
    assert "harness_hash_mismatch" in refusal_codes(rp.check_receipt(check_opts(good, ctx, expect_harness_sha256="0" * 64))[1])
    assert "corpus_hash_mismatch" in refusal_codes(rp.check_receipt(check_opts(good, ctx, expect_corpus_sha256="0" * 64))[1])
    assert "policy_hash_mismatch" in refusal_codes(rp.check_receipt(check_opts(good, ctx, expect_policy_sha256="0" * 64))[1])
    stale = rp.check_receipt(check_opts(good, ctx, now=good.deps.now_ts + POLICY["max_receipt_age_s"] + 5))[1]
    assert "stale_receipt" in refusal_codes(stale)
    future = rp.check_receipt(check_opts(good, ctx, now=good.deps.now_ts - 4000))[1]
    assert "receipt_from_the_future" in refusal_codes(future)


def test_the_candidate_sha_must_exist_in_the_repository_with_the_recorded_tree(good, ctx, tmp_path):
    other = tmp_path / "other_repo"
    other.mkdir()
    git("init", "-q", cwd=other)
    (other / "z").write_text("z")
    git("add", "z", cwd=other)
    git("commit", "-q", "-m", "unrelated", cwd=other)
    code, result = rp.check_receipt(check_opts(good, ctx, repo=other))
    assert code == rp.EXIT_REFUSED and "subject_unknown_to_repo" in refusal_codes(result)


def test_worktree_reverification_catches_a_dirty_or_moved_subject(good, ctx):
    ok = rp.check_receipt(check_opts(good, ctx, candidate_worktree=ctx.cand_wt, baseline_worktree=ctx.base_wt))
    assert ok[0] == rp.EXIT_PASS
    (ctx.cand_wt / "stray.txt").write_text("untracked\n")
    try:
        code, result = rp.check_receipt(check_opts(good, ctx, candidate_worktree=ctx.cand_wt))
        assert code == rp.EXIT_REFUSED and "worktree_reverification_failed" in refusal_codes(result)
    finally:
        (ctx.cand_wt / "stray.txt").unlink()
    code, result = rp.check_receipt(check_opts(good, ctx, candidate_worktree=ctx.base_wt))
    assert "worktree_reverification_failed" in refusal_codes(result)         # right path, wrong head


def test_lane_mismatch_and_the_managed_lane_never_inherit_a_desktop_pass(good, ctx, tmp_path):
    code, result = rp.check_receipt(check_opts(good, ctx, lane="managed_mini"))
    assert code == rp.EXIT_REFUSED and "lane_mismatch" in refusal_codes(result)
    managed = pass_evidence(tmp_path, ctx, "managed", lane="managed_mini")
    assert managed.receipt["lane"] == "managed_mini" and managed.receipt["verdict"] == "INCONCLUSIVE"
    code, result = rp.check_receipt(check_opts(managed, ctx))
    assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "lane_is_not_a_release_gate" for r in result["reasons"])
    code, result = rp.check_receipt(check_opts(managed, ctx, lane="desktop"))
    assert "lane_mismatch" in refusal_codes(result)


def test_offline_fixture_run_can_never_pass(tmp_path, ctx):
    # Give the offline run EVERYTHING a real PASS would have: perfect timings and a reviewed baseline. It must
    # still never pass, because it is not an actual measurement.
    first = run_evidence(tmp_path, ctx, "offline_first", is_real=False)
    approval = write_approval(tmp_path, first.receipt)
    offline = run_evidence(tmp_path, ctx, "offline", is_real=False, approved=approval)
    assert offline.receipt["execution_mode"] == rp.OFFLINE_MODE and offline.receipt["inference"]["fake_model"] is True
    assert offline.receipt["verdict"] == "INCONCLUSIVE" and offline.code == rp.EXIT_INCONCLUSIVE
    assert any(r["code"] == "not_an_actual_measurement" for r in offline.receipt["reasons"])
    offline.approval = approval
    code, result = rp.check_receipt(check_opts(offline, ctx))
    assert code == rp.EXIT_REFUSED and {"not_an_actual_measurement", "fake_or_unverified_model",
                                        "measurement_source_not_release_capable"} <= refusal_codes(result)
    # and the same constructed evidence flagged as real verifies as a FIXTURE, which proves the refusal above is
    # about provenance, but a fixture result never authorizes a release
    real = pass_evidence(tmp_path, ctx, "offline_as_real")
    code, result = rp.check_receipt(check_opts(real, ctx))
    assert code == rp.EXIT_PASS and result["authorizes_release"] is False and result["fixture_source"] is True


def test_unknown_environment_identity_makes_the_cohort_unusable(tmp_path, ctx):
    class Blind(ConstructedEvidenceDeps):
        def collect_environment(self, model_id, lane, warmups):
            env = super().collect_environment(model_id, lane, warmups)
            env["os"] = {"product_version": UNKNOWN, "build": UNKNOWN, "kernel": "27.0.0"}
            return env
    deps_factory = Blind
    first_dir = tmp_path / "blind1"
    for name, approval in (("blind1", None), ("blind2", True)):
        deps = deps_factory(ctx)
        opts = {"candidate_root": ctx.cand_wt, "candidate_sha": ctx.cand_sha, "baseline_root": ctx.base_wt,
                "baseline_sha": ctx.base_sha, "lane": "desktop", "model_id": MODEL_ID, "output_dir": tmp_path / name,
                "scenario_ids": None, "samples": None, "diagnostic": False,
                "approved_baseline_path": None, "approved_baseline_sha256": None}
        if approval:
            receipt = json.loads((first_dir / "receipt.json").read_text())
            opts["approved_baseline_path"], opts["approved_baseline_sha256"] = map(str, write_approval(tmp_path, receipt))
        code, receipt_path = asyncio.run(rp.run_release(opts, deps, rp.load_bundle()))
    receipt = json.loads(receipt_path.read_text())
    assert rp.has_unknown(rp.cohort_key(receipt))
    assert receipt["verdict"] == "INCONCLUSIVE" and code == rp.EXIT_INCONCLUSIVE
    assert any("UNKNOWN" in r["detail"] for r in receipt["reasons"])


def test_a_receipt_that_claims_a_real_model_but_whose_raw_shows_no_engine_calls_is_refused(good, ctx, tmp_path):
    evd = clone(good, tmp_path)
    for side in rp.SIDES:
        path = evd.out / "raw" / f"instrumentation.{side}.jsonl"
        path.write_text("".join(l for l in path.read_text().splitlines(True) if '"chat/completions"' not in l
                                and "/chat/completions" not in l))
    rows = [json.loads(l) for l in (evd.out / "raw" / "samples.jsonl").read_text().splitlines()]
    seal(evd.out, evd.receipt_path)
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED
    assert refusal_codes(result) & {"raw_derivation_mismatch", "no_attributed_engine_evidence",
                                    "instrumentation_log_invalid"}


def test_wrong_schema_legacy_and_garbage_files_are_refused(good, ctx, tmp_path):
    legacy = tmp_path / "compare.json"
    legacy.write_text(json.dumps({"before": "a.json", "after": "b.json", "reps": 3, "rows": [],
                                  "summary": {"greeting": {"before": {"median_seconds": 0.1}}}}))
    evd = SimpleNamespace(**vars(good))
    evd.receipt_path = legacy
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED and "not_a_release_receipt" in refusal_codes(result)
    garbage = tmp_path / "garbage.json"
    garbage.write_text("not json {")
    evd.receipt_path = garbage
    assert "receipt_unreadable" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])
    evd.receipt_path = tmp_path / "absent.json"
    assert "receipt_missing" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


def test_under_sampled_partial_and_incomplete_runs_are_refused(good, ctx, tmp_path):
    evd = clone(good, tmp_path, "under")
    receipt = evd.receipt
    receipt["protocol"]["measured"] = 19
    evd.receipt_path.write_text(json.dumps(receipt))
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED and "under_sampled" in refusal_codes(result)

    partial = run_evidence(tmp_path, ctx, "partial", samples=5, diagnostic=True, scenario_ids=["greeting_warm"])
    assert partial.receipt["protocol"]["partial"] is True and partial.code != rp.EXIT_PASS
    partial.approval = write_approval(tmp_path, partial.receipt)
    result = rp.check_receipt(check_opts(partial, ctx))[1]
    assert "partial_run" in refusal_codes(result)

    short = clone(good, tmp_path, "short")
    receipt = short.receipt
    receipt["protocol"]["scenario_ids"] = receipt["protocol"]["scenario_ids"][:-1]
    short.receipt_path.write_text(json.dumps(receipt))
    assert "required_scenarios_incomplete" in refusal_codes(rp.check_receipt(check_opts(short, ctx))[1])


def test_incomparable_sample_orders_and_dropped_samples_are_refused(good, ctx, tmp_path):
    swapped = clone(good, tmp_path, "swapped")
    path = swapped.out / "raw" / "samples.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    for row in rows:
        if row["scenario"] == "bounded_reasoning" and row["phase"] == "measured" and row["rep"] == 2:
            row["order_slot"] = 1 - row["order_slot"]
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    seal(swapped.out, swapped.receipt_path)
    result = rp.check_receipt(check_opts(swapped, ctx))[1]
    assert "incomparable_or_incomplete_samples" in refusal_codes(result)

    dropped = clone(good, tmp_path, "dropped")
    path = dropped.out / "raw" / "samples.jsonl"
    lines = path.read_text().splitlines(True)
    path.write_text("".join(l for i, l in enumerate(lines) if i != 50))
    seal(dropped.out, dropped.receipt_path)
    assert "incomparable_or_incomplete_samples" in refusal_codes(rp.check_receipt(check_opts(dropped, ctx))[1])


def test_a_subject_that_changed_during_the_run_is_refused(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "moved", dirty_after=True)
    evd.approval = write_approval(tmp_path, evd.receipt)
    code, result = rp.check_receipt(check_opts(evd, ctx))
    assert code == rp.EXIT_REFUSED and "subject_not_verified_clean_and_exact" in refusal_codes(result)


def test_a_tampered_schedule_digest_is_refused(good, ctx, tmp_path):
    evd = clone(good, tmp_path, "sched")
    evd.receipt["protocol"]["schedule_sha256"] = "0" * 64
    evd.receipt_path.write_text(json.dumps(evd.receipt))
    assert "schedule_digest_mismatch" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


def test_candidate_and_baseline_must_be_different_subjects_by_sha_and_by_tree(good, ctx, tmp_path):
    def retarget(name, **fields):
        evd = clone(good, tmp_path, name)
        baseline = evd.receipt["baseline"]
        for key, value in fields.items():
            baseline[key] = value
            for stage in ("verified_before", "verified_after"):
                baseline[stage]["tree" if key == "tree" else "head"] = value
        evd.receipt_path.write_text(json.dumps(evd.receipt))
        return evd
    same_tree = retarget("sametree", tree=good.receipt["candidate"]["tree"])
    code, result = rp.check_receipt(check_opts(same_tree, ctx, repo=None))
    assert code == rp.EXIT_REFUSED and "candidate_and_baseline_not_different" in refusal_codes(result)
    same_sha = retarget("samesha", sha=ctx.cand_sha)
    code, result = rp.check_receipt(check_opts(same_sha, ctx, repo=None))
    assert code == rp.EXIT_REFUSED and "candidate_and_baseline_not_different" in refusal_codes(result)


def test_baseline_never_silently_advances_and_proposals_are_unapproved(good, tmp_path):
    target = tmp_path / "proposal.json"
    rp.propose_baseline(good.receipt_path, target)
    doc = json.loads(target.read_text())
    assert doc["status"] == "proposed" and doc["approved_by"] is None
    assert doc["cohort_key"] == rp.cohort_key(good.receipt)
    state, refusals = rp.load_baseline_approval(str(target), rp.sha256_file(target))
    assert state["approved"] is False and refusals == []
    with pytest.raises(FileExistsError):
        rp.propose_baseline(good.receipt_path, target)


# ---- orchestration refusals ----

def test_dirty_wrong_head_and_identical_subjects_stop_before_any_measurement(tmp_path, ctx):
    (ctx.cand_wt / "dirty.txt").write_text("x")
    try:
        with pytest.raises(rp.SubjectError):
            run_evidence(tmp_path, ctx, "dirty")
    finally:
        (ctx.cand_wt / "dirty.txt").unlink()
    deps = ConstructedEvidenceDeps(ctx)
    wrong = {"candidate_root": ctx.cand_wt, "candidate_sha": ctx.base_sha, "baseline_root": ctx.base_wt,
             "baseline_sha": ctx.base_sha, "model_id": "m", "output_dir": tmp_path / "wrong"}
    with pytest.raises(rp.SubjectError):
        asyncio.run(rp.run_release(wrong, deps, rp.load_bundle()))
    same = {**wrong, "candidate_root": ctx.base_wt}
    same["output_dir"] = tmp_path / "same"
    with pytest.raises(rp.SubjectError):
        asyncio.run(rp.run_release(same, deps, rp.load_bundle()))
    assert deps.spawned == []


def test_selection_floor_and_overwrite_protection(tmp_path, ctx):
    with pytest.raises(ValueError):
        run_evidence(tmp_path, ctx, "few", samples=5)                              # below the floor, not diagnostic
    with pytest.raises(ValueError):
        run_evidence(tmp_path, ctx, "subset", scenario_ids=["greeting_warm"])      # partial, not diagnostic
    with pytest.raises(ValueError):
        run_evidence(tmp_path, ctx, "unknown", scenario_ids=["greeting_warm", "nope"], diagnostic=True)
    run_evidence(tmp_path, ctx, "once")
    with pytest.raises(FileExistsError):
        run_evidence(tmp_path, ctx, "once")


def test_missing_prerequisites_produce_an_inconclusive_receipt_and_spawn_nothing(tmp_path, ctx):
    missing = [{"id": "resident_model_mismatch", "detail": "resident models are []; expected exactly ['m']"}]
    evd = run_evidence(tmp_path, ctx, "missing", missing=missing)
    assert evd.code == rp.EXIT_INCONCLUSIVE and evd.deps.spawned == []
    assert evd.receipt["prerequisites_missing"] == missing and evd.receipt["verdict"] == "INCONCLUSIVE"
    assert (evd.out / "raw" / "samples.jsonl").read_text() == ""
    evd.approval = write_approval(tmp_path, evd.receipt)
    assert "incomplete_run" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


def test_an_interrupted_run_keeps_the_running_sample_and_its_partial_stream(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "aborted", cancel_at=40, cancel_partial=True)
    raw = evd.out / "raw"
    assert evd.receipt["aborted"] is True and evd.code == rp.EXIT_INCONCLUSIVE
    rows = [json.loads(l) for l in (raw / "samples.jsonl").read_text().splitlines()]
    assert len(rows) == 40                                              # the in-flight sample is NOT dropped
    assert rows[-1]["outcome"] == "cancelled" and "cancelled" in rows[-1]["grade"]["reasons"]
    assert not rows[-1]["grade"]["correct"] and rows[-1]["metrics_ns"]["total_completion_s"] == UNKNOWN
    events = [json.loads(l) for l in (raw / "events.jsonl").read_text().splitlines()]
    assert len(events) == 40 and events[-1]["driver"]["cancelled"] is True
    assert [e["event"]["type"] for e in events[-1]["events"]] == ["session", "delta"]       # the partial stream survives
    started = [json.loads(l) for l in (raw / "started.jsonl").read_text().splitlines()]
    assert [r["id"] for r in started] == [r["id"] for r in rows]       # recorded before dispatch
    assert all(b.stopped for b in evd.deps.spawned) and set(evd.receipt["teardown"]) == set(rp.SIDES)
    assert all(row["ok"] for row in evd.receipt["teardown"].values())
    evd.approval = write_approval(tmp_path, evd.receipt)
    assert "incomplete_run" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


def test_an_interrupt_before_any_event_still_records_the_started_sample(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "early", cancel_at=3)
    rows = [json.loads(l) for l in (evd.out / "raw" / "samples.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and rows[-1]["outcome"] == "cancelled" and rows[-1]["grade"]["correct"] is False


# ----------------------------------------------------------------------------
# Subject identity: derived from git by the harness, never accepted from a caller
# ----------------------------------------------------------------------------

def test_subject_verification_reports_clean_dirty_untracked_wrong_head_and_not_a_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("1\n")
    git("add", "f.txt", cwd=repo)
    git("commit", "-q", "-m", "one", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    clean = rp.verify_subject(repo, head)
    assert rp.subject_ok(clean) and clean["tree"] == git("rev-parse", "HEAD^{tree}", cwd=repo)
    assert clean["problems"] == [] and clean["toplevel"] and clean["verified_at_ts"] > 0

    (repo / "f.txt").write_text("changed\n")
    dirty = rp.verify_subject(repo, head)
    assert not dirty["clean"] and not rp.subject_ok(dirty) and dirty["dirty_entries"]
    git("checkout", "--", "f.txt", cwd=repo)
    (repo / "new.txt").write_text("x")
    assert not rp.verify_subject(repo, head)["clean"]                          # untracked counts
    (repo / "new.txt").unlink()

    wrong = rp.verify_subject(repo, "0" * 40)
    assert wrong["matches_expected"] is False and not rp.subject_ok(wrong)
    short = rp.verify_subject(repo, head[:12])
    assert not rp.subject_ok(short) and any("40-character" in p for p in short["problems"])
    (repo / "sub").mkdir()
    assert not rp.subject_ok(rp.verify_subject(repo / "sub", head))            # a subdirectory is not the root
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a git worktree" in rp.verify_subject(plain, head)["problems"]


def test_verifying_a_subject_does_not_modify_the_repository(tmp_path):
    repo = tmp_path / "r2"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "f.txt").write_text("1\n")
    git("add", "f.txt", cwd=repo)
    git("commit", "-q", "-m", "one", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    index = repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    rp.verify_subject(repo, head)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


# ----------------------------------------------------------------------------
# Schedule and corpus/policy bundle
# ----------------------------------------------------------------------------

def test_schedule_runs_warmups_first_alternates_sides_and_rotates_variants():
    items = rp.build_schedule(["a", "b"], {"a": 4, "b": 1}, 2, 3)
    assert len(items) == (2 + 3) * 2
    assert [i["phase"] for i in items[:4]] == ["warmup"] * 4 and items[4]["phase"] == "measured"
    rep1 = [i for i in items if i["phase"] == "measured" and i["rep"] == 1]
    rep2 = [i for i in items if i["phase"] == "measured" and i["rep"] == 2]
    assert all(i["order"] == ["candidate", "baseline"] for i in rep1)
    assert all(i["order"] == ["baseline", "candidate"] for i in rep2)
    assert [i["variant"] for i in items if i["scenario"] == "a" and i["phase"] == "measured"] == [0, 1, 2]
    assert {i["variant"] for i in items if i["scenario"] == "b"} == {0}
    assert rp.canonical_sha(items) == rp.canonical_sha(rp.build_schedule(["a", "b"], {"a": 4, "b": 1}, 2, 3))
    assert rp.canonical_sha(items) != rp.canonical_sha(rp.build_schedule(["a", "b"], {"a": 4, "b": 1}, 2, 4))


def schedule_samples(ids=("a", "b"), warmups=1, measured=2):
    out = []
    for item in rp.build_schedule(list(ids), {i: 2 for i in ids}, warmups, measured):
        for slot, side in enumerate(item["order"]):
            out.append({"scenario": item["scenario"], "phase": item["phase"], "rep": item["rep"], "side": side,
                        "variant": item["variant"], "order_slot": slot, "started_ns": len(out) + 1})
    return out


def test_alternation_verifier_accepts_the_schedule_and_rejects_every_deviation():
    ids = ("a", "b")
    assert rp.verify_alternation(schedule_samples(), list(ids), 1, 2) == []
    swapped = schedule_samples()
    swapped[0]["order_slot"] = 1
    assert rp.verify_alternation(swapped, list(ids), 1, 2)
    assert any("missing" in p for p in rp.verify_alternation(schedule_samples()[1:], list(ids), 1, 2))
    extra = schedule_samples() + [dict(schedule_samples()[0])]
    assert any("duplicate" in p for p in rp.verify_alternation(extra, list(ids), 1, 2))
    mixed = schedule_samples()
    mixed[1]["variant"] = 1
    assert any("different variants" in p for p in rp.verify_alternation(mixed, list(ids), 1, 2))
    reordered = schedule_samples()
    reordered[0]["started_ns"], reordered[1]["started_ns"] = 9, 1
    assert any("recorded order" in p for p in rp.verify_alternation(reordered, list(ids), 1, 2))
    foreign = schedule_samples() + [{"scenario": "z", "phase": "measured", "rep": 1, "side": "candidate",
                                     "variant": 0, "order_slot": 0, "started_ns": 99}]
    assert any("unexpected" in p for p in rp.verify_alternation(foreign, list(ids), 1, 2))


def mutated(path_fn):
    bundle = copy.deepcopy(BUNDLE)
    path_fn(bundle)
    return rp.validate_bundle(bundle)


def test_the_shipped_bundle_is_valid_and_the_floor_cannot_be_lowered_by_policy():
    assert rp.validate_bundle(BUNDLE) == [] and rp.ABSOLUTE_MIN_SAMPLES == 20
    assert POLICY["min_measured_samples"] >= 20 and POLICY["warmup_samples"] >= 1
    assert any("min_measured_samples" in p for p in mutated(lambda b: b["policy"].update(min_measured_samples=19)))
    assert any("min_measured_samples" in p for p in mutated(lambda b: b["policy"].update(min_measured_samples="20")))
    assert any("min_measured_samples" in p for p in mutated(lambda b: b["policy"].update(min_measured_samples=True)))
    assert rp.validate_bundle({**BUNDLE, "policy": {**POLICY, "min_measured_samples": 30}}) == []   # raising is allowed


@pytest.mark.parametrize("edit,needle", [
    (lambda b: b["policy"]["execution"].update(fake_model_can_pass=True), "fake_model_can_pass"),
    (lambda b: b["policy"]["execution"].update(accepted_mode="offline_fixture"), "accepted_mode"),
    (lambda b: b["policy"]["lanes"]["managed_mini"].update(release_gate=True), "only the desktop lane"),
    (lambda b: b["policy"]["lanes"]["desktop"].update(release_gate=False), "desktop lane must be"),
    (lambda b: b["policy"].update(percentile_method="linear"), "nearest-rank"),
    (lambda b: b["policy"]["regression"].update(rule="either_exceeds"), "both_exceed"),
    (lambda b: b["policy"]["regression"].update(relative_increase="-1"), "relative_increase"),
    (lambda b: b["policy"]["outcomes"].update(candidate_correctness_failure="INCONCLUSIVE"), "candidate_correctness_failure"),
    (lambda b: b["policy"]["outcomes"].update(new_refusal="PASS"), "new_refusal"),
    (lambda b: b["policy"]["outcomes"].update(missing_or_unapproved_baseline="PASS"), "missing_or_unapproved_baseline"),
    (lambda b: b["corpus"]["scenarios"].append(copy.deepcopy(b["corpus"]["scenarios"][0])), "duplicated"),
    (lambda b: b["corpus"]["scenarios"][0].update(required_metrics=["nope"]), "unknown metric"),
    (lambda b: b["corpus"]["scenarios"][0].update(deadline_s=0), "deadline_s"),
    (lambda b: b["corpus"]["scenarios"][0].update(variants=[]), "no variants"),
    (lambda b: b["corpus"]["scenarios"][0].update(kind="other"), "kind"),
    (lambda b: b["policy"].update(max_receipt_age_s=0), "max_receipt_age_s"),
    (lambda b: b.update(schema="x"), "schema"),
])
def test_bundle_validation_rejects_weakened_policy_and_malformed_corpus(edit, needle):
    assert any(needle in p for p in mutated(edit)), needle


def test_corpus_and_policy_hashes_are_canonical_and_change_only_with_their_own_content():
    base = rp.load_bundle()
    assert base["corpus_sha256"] == rp.canonical_sha(CORPUS) and base["policy_sha256"] == rp.canonical_sha(POLICY)
    reordered = json.loads(json.dumps(CORPUS, sort_keys=True))
    assert rp.canonical_sha(reordered) == base["corpus_sha256"]
    edited = copy.deepcopy(CORPUS)
    edited["scenarios"][0]["variants"][0] = "hello!"
    assert rp.canonical_sha(edited) != base["corpus_sha256"]
    assert rp.canonical_sha({**POLICY, "max_receipt_age_s": 1}) != base["policy_sha256"]
    assert base["file_sha256"] == rp.sha256_file(rp.DEFAULT_BUNDLE)


def test_the_corpus_covers_the_required_scenarios_with_synthetic_data_only():
    assert REQUIRED == ["greeting_warm", "bounded_reasoning", "scoped_tool_selection", "deterministic_read",
                        "outbound_approval_boundary", "health_status_overhead"]
    blob = json.dumps(CORPUS["leaf_data"])
    import re
    assert all(re.fullmatch(r"\+1555555\d{4}", handle) for handle in CORPUS["leaf_data"]["contacts"])
    assert "@" not in blob or "example.com" in blob
    for spec in CORPUS["scenarios"]:
        assert len(spec["variants"]) >= 1 and spec["required_metrics"]
    assert SPECS["outbound_approval_boundary"]["expect"]["approval"]["action"] == "deny"
    assert SPECS["deterministic_read"]["expect"]["model_calls"] == {"exactly": 0}


# ----------------------------------------------------------------------------
# Synthetic connector leaves
# ----------------------------------------------------------------------------

def test_leaf_renderers_match_the_repositorys_own_wire_formats():
    from tests.fixtures import wire
    rows = [{"ts": 100, "context": 'Group "Crew"', "who": "Alex", "text": "hi\nthere | ok"},
            {"ts": 300, "context": "Alex", "who": "Me", "text": "later"}]
    assert rp.messages_lines(rows) == wire.messages_lines(rows)                  # the legacy shape, kept for the pin below
    for args in (("Crew", ["A", "B"], True), (None, ["A", "B", "C", "D", "E"], True), (None, ["Al"], False),
                 ("Al", ["Al"], False), (None, [], False)):
        assert rp.thread_context(*args) == wire.thread_context(*args)
    notes = [{"ts": 5, "title": "T", "folder": "F", "body": "B"}, {"ts": 6, "title": "U", "body": "C"}]
    assert rp.notes_raw(notes) == wire.notes_raw(notes)


def v2_rows():
    return [{"ts": 1_800_000_000 - 2400, "conversation": "chat:1", "unread": True, "context": 'Group "Studio crew"',
             "who": "Alex Rivera", "text": "Workshop moved\nto Room 204 | please confirm"},
            {"ts": 1_800_000_000 - 900, "conversation": "chat:2", "unread": False, "context": "Alex Rivera",
             "who": "Me", "text": "On my way"}]


def test_v2_message_lines_are_exactly_the_native_readers_shape_and_the_product_parses_their_read_state(monkeypatch):
    """MessagesReader.swift: `V2 | epochSecs | U/R | conversationId | context | who: oneLine`, newest first."""
    from service.tools import imessage_tools as im
    lines = rp.messages_lines_v2(v2_rows())
    assert lines.splitlines() == [
        'V2 | 1799999100.0 | R | chat:2 | Alex Rivera | Me: On my way',
        'V2 | 1799997600.0 | U | chat:1 | Group "Studio crew" | Alex Rivera: Workshop moved to Room 204 | please confirm']
    monkeypatch.setattr(im, "_lines", lines)
    parsed = im._parse_records()
    assert [(r[1], r[2], r[4]) for r in parsed] == [("chat:2", "Alex Rivera", False), ("chat:1", 'Group "Studio crew"', True)]
    assert parsed[1][3] == "Alex Rivera: Workshop moved to Room 204 | please confirm"


def test_the_legacy_line_shape_carries_no_read_state_which_is_why_corpus_v2_could_not_work(monkeypatch):
    from service.tools import imessage_tools as im
    monkeypatch.setattr(im, "_lines", rp.messages_lines(
        [{"ts": r["ts"], "context": r["context"], "who": r["who"], "text": r["text"]} for r in v2_rows()]))
    assert [r[4] for r in im._parse_records()] == [None, None]
    monkeypatch.setattr(im, "_lines", rp.messages_lines_v2(v2_rows()))
    assert all(r[4] is not None for r in im._parse_records())


def test_the_corpus_messages_arrive_with_read_state_so_the_summary_does_not_skip_them(monkeypatch):
    """summary_message_rows(require_read_state=True) drops every row whose read state is None: corpus v2's failure."""
    from service.tools import imessage_tools as im
    body = rp.render_leaf_payloads(CORPUS, 1_800_000_000.0)[1]["body"]
    monkeypatch.setattr(im, "_lines", body["lines"])
    records = im._parse_records()
    assert len(records) == 3 and all(r[4] is True for r in records)               # all incoming, all unread
    assert len({r[1] for r in records}) == 2                                      # two chats: the group and the direct one
    assert any("budget" in r[3] for r in records) and any("Room 204" in r[3] for r in records)


def test_rendered_payloads_use_the_real_sync_endpoints_with_fictional_data():
    payloads = rp.render_leaf_payloads(CORPUS, 1_800_000_000.0)
    assert [p["path"] for p in payloads] == ["/assistant/sync/messages", "/assistant/sync/messages",
                                              "/assistant/sync/notes"]
    assert payloads[0]["body"] == {"contacts": CORPUS["leaf_data"]["contacts"]}
    body = payloads[1]["body"]
    lines = body["lines"].splitlines()
    assert body["diagnostics"] == {"available": True, "reason": "", "count": len(lines)} and len(lines) == 3
    stamps = [float(l.split(" | ")[1]) for l in lines]
    assert stamps == sorted(stamps, reverse=True)                      # newest first, as the native reader sends
    assert all(l.startswith("V2 | ") and len(l.split(" | ", 5)) == 6 and l.split(" | ")[2] in ("U", "R") for l in lines)
    assert "Orchard Lane workshop" in payloads[2]["body"]["raw"] and "\x02" in payloads[2]["body"]["raw"]
    assert rp.render_leaf_payloads(CORPUS, 1_800_000_000.0) == payloads        # deterministic for a given clock


def test_outgoing_messages_are_never_unread_even_if_the_corpus_says_so():
    corpus = copy.deepcopy(CORPUS)
    corpus["leaf_data"]["messages"].append({"thread": "Alex Rivera", "kind": "direct", "participants": ["Alex Rivera"],
                                            "age_s": 100, "from": "me", "unread": True, "text": "ok"})
    line = rp.render_leaf_payloads(corpus, 1_800_000_000.0)[1]["body"]["lines"].splitlines()[0]
    assert line == "V2 | 1799999900.0 | R | chat:2 | Alex Rivera | Me: ok"


def test_bounded_reasoning_prompts_forbid_tools_and_the_no_tools_check_is_untouched():
    spec = SPECS["bounded_reasoning"]
    for prompt in spec["variants"]:
        lowered = prompt.lower()
        assert "tool" in lowered and ("without" in lowered or "no tool" in lowered or "not use" in lowered), prompt
    assert spec["expect"]["tool_calls"] == "none" and spec["expect"]["model_calls"] == {"min": 1}
    assert "calculate" not in json.dumps(CORPUS["effect_tools"]) and "calculate" not in json.dumps(spec["expect"])


def test_role_config_points_every_text_role_at_the_resident_model_only():
    text = rp.render_role_config("Some-Model-oQ4e")
    roles = [line.split(":")[0].strip() for line in text.splitlines()[1:]]
    assert roles == ["fast", "router", "general", "agent", "coding", "reasoning", "profile", "profile_map"]
    assert text.count('"Some-Model-oQ4e"') == len(roles) and "research" not in text


# ----------------------------------------------------------------------------
# Effect guard
# ----------------------------------------------------------------------------

LSOF_LISTEN = ["/usr/sbin/lsof", "-nP", "-a", "-iTCP:8000", "-sTCP:LISTEN", "-Fpufn"]
PS_ROW = ["/bin/ps", "-ww", "-p", "123", "-o", "ppid=,uid=,comm="]


def test_effect_guard_blocks_processes_network_and_http_but_delegates_the_allowed(monkeypatch):
    import httpx
    popen, connects, https = [], [], []

    def fake_popen_init(self, args, *a, **k):
        self._child_created = False                      # lets Popen.__del__ run quietly without a real child
        popen.append(list(args))

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_popen_init)
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: connects.append(address))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: 0)

    async def async_send(self, request, *a, **k):
        https.append(str(request.url))
        return "ASYNC"

    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(httpx.Client, "send", lambda self, request, *a, **k: https.append(str(request.url)) or "SYNC")
    emitted = []
    guard = rp.EffectGuard(lambda kind, **fields: emitted.append((kind, fields)), allowed_ports={8000})
    with guard:
        subprocess.Popen(LSOF_LISTEN)
        subprocess.Popen(PS_ROW)
        for argv in (["osascript", "-e", "x"], "open /Applications", ["/usr/bin/open", "x"], ["python3", "-c", "1"]):
            with pytest.raises(rp.EffectBlocked):
                subprocess.Popen(argv)
        with pytest.raises(rp.EffectBlocked):
            os.system("echo hi")
        sock = socket.socket()
        try:
            sock.connect(("127.0.0.1", 8000))
            for blocked in (("93.184.216.34", 443), ("127.0.0.1", 9999), ("example.com", 80), "/tmp/unix.sock"):
                with pytest.raises(rp.EffectBlocked):
                    sock.connect(blocked)
            with pytest.raises(rp.EffectBlocked):
                sock.connect_ex(("8.8.8.8", 53))
        finally:
            sock.close()
        assert httpx.Client().send(httpx.Request("GET", "http://127.0.0.1:8000/health")) == "SYNC"
        assert asyncio.run(httpx.AsyncClient().send(httpx.Request("GET", "http://localhost:8000/v1/models"))) == "ASYNC"
        for url in ("https://example.com/x", "http://127.0.0.1:9000/x", "http://192.168.1.5:8000/"):
            with pytest.raises(rp.EffectBlocked):
                httpx.Client().send(httpx.Request("GET", url))
            with pytest.raises(rp.EffectBlocked):
                asyncio.run(httpx.AsyncClient().send(httpx.Request("GET", url)))
    assert popen == [LSOF_LISTEN, PS_ROW]                                      # only the two read-only inspectors ran
    assert connects == [("127.0.0.1", 8000)] and https[:2] == ["http://127.0.0.1:8000/health", "http://localhost:8000/v1/models"]
    kinds = [(k, f["what"]) for k, f in emitted]
    assert kinds.count(("effect_blocked", "exec")) == 5 and ("effect_blocked", "http") in kinds
    assert ("effect_blocked", "connect") in kinds and len(https) == 2
    # uninstalled: the previous behaviour is back
    assert subprocess.Popen.__init__ is fake_popen_init and os.system.__name__ == "system"


def test_effect_guard_uninstall_restores_every_patched_attribute():
    before = (subprocess.Popen.__init__, socket.socket.connect, os.system, os.posix_spawn)
    with rp.EffectGuard(lambda *a, **k: None):
        assert subprocess.Popen.__init__ is not before[0] and os.system is not before[2]
    assert (subprocess.Popen.__init__, socket.socket.connect, os.system, os.posix_spawn) == before


# ----------------------------------------------------------------------------
# Delegating instrumentation
# ----------------------------------------------------------------------------

class FakeAttribution:
    """Stands in for service.inference.attributed_transport / local_peer."""

    def __init__(self, omit=()):
        omit = set(omit)
        outer = self
        self.calls = []

        class CredentialTransport:
            async def handle_async_request(self, request):
                outer.calls.append(("request", request.url.path))
                if request.url.path == "/boom":
                    raise RuntimeError("engine down")
                return SimpleNamespace(status_code=200)

        class RuntimeAuthority:
            def load(self):
                outer.calls.append(("load",))
                return "authority"

        class DesktopOmlx:
            def binding(self, expected_pid=None):
                outer.calls.append(("desktop_binding", expected_pid))
                return 321

        class ManagedOmlx:
            def binding(self, expected_pid=None):
                outer.calls.append(("managed_binding", expected_pid))
                return 322

            def connected_peer(self, sock, pid, incarnation):
                outer.calls.append(("peer", pid))
                if pid == -1:
                    raise PermissionError("refused")
                return ("owner", pid)

        self.at = SimpleNamespace(CredentialTransport=CredentialTransport, RuntimeAuthority=RuntimeAuthority,
                                  DesktopOmlx=DesktopOmlx,
                                  inspect_command=lambda argv: outer.calls.append(("inspect", argv)) or b"out",
                                  tcp_listeners=lambda: outer.calls.append(("listeners",)) or [("127.0.0.1", 8000)])
        self.lp = SimpleNamespace(ManagedOmlx=ManagedOmlx, tcp_listeners=self.at.tcp_listeners)
        for name in omit:
            owner, attr = name.split(".")
            target = self.at if owner == "at" else self.lp
            if "." in attr:
                raise ValueError(name)
            delattr(target, attr)


def request(path, method="POST"):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def test_instrumentation_delegates_exactly_and_records_every_surface(tmp_path):
    fake = FakeAttribution()
    recorder = rp.InstrumentRecorder(tmp_path / "i.jsonl")
    available, unavailable = rp.install_instrumentation(recorder, fake.at, fake.lp)
    assert unavailable == [] and available == ["authority_load", "binding_check", "engine_completion",
                                               "engine_http", "peer_check", "process_check"]
    transport = fake.at.CredentialTransport()
    assert asyncio.run(transport.handle_async_request(request("/v1/chat/completions"))).status_code == 200
    with pytest.raises(RuntimeError, match="engine down"):                       # errors propagate unchanged
        asyncio.run(transport.handle_async_request(request("/boom")))
    assert fake.at.RuntimeAuthority().load() == "authority"
    assert fake.at.inspect_command(["/usr/sbin/lsof", "-nP"]) == b"out"
    assert fake.at.tcp_listeners() == [("127.0.0.1", 8000)] and fake.lp.tcp_listeners() == [("127.0.0.1", 8000)]
    assert fake.at.DesktopOmlx().binding(7) == 321 and fake.lp.ManagedOmlx().binding() == 322
    managed = fake.lp.ManagedOmlx()
    assert managed.connected_peer(object(), 5, ("inc",)) == ("owner", 5)
    with pytest.raises(PermissionError):
        managed.connected_peer(object(), -1, ())
    recorder.close()
    records = rp.read_instrument_lines(tmp_path / "i.jsonl")
    kinds = [r["kind"] for r in records]
    assert kinds.count("engine_http") == 2 and kinds.count("process_check") == 3
    assert kinds.count("authority_load") == 1 and kinds.count("binding_check") == 2 and kinds.count("peer_check") == 2
    http = [r for r in records if r["kind"] == "engine_http"]
    assert http[0]["path"] == "/v1/chat/completions" and http[0]["status"] == 200
    assert http[1]["error"] == "RuntimeError" and "status" not in http[1]
    assert any(r.get("argv0") == "/usr/sbin/lsof" for r in records)
    assert any(r.get("error") == "PermissionError" for r in records if r["kind"] == "peer_check")
    assert all(isinstance(r["t_ns"], int) for r in records)
    assert [r["seq"] for r in records] == list(range(len(records)))          # gap-free and totally ordered
    assert [r["t_ns"] for r in records] == sorted(r["t_ns"] for r in records)
    assert fake.at.CredentialTransport.handle_async_request.__name__ == "handle_async_request"


def test_a_missing_surface_is_reported_unavailable_and_its_counts_are_unknown(tmp_path):
    fake = FakeAttribution(omit=["at.inspect_command", "lp.tcp_listeners"])
    del fake.at.RuntimeAuthority.load
    del fake.lp.ManagedOmlx.connected_peer
    recorder = rp.InstrumentRecorder(tmp_path / "j.jsonl")
    available, unavailable = rp.install_instrumentation(recorder, fake.at, fake.lp)
    assert set(unavailable) == {"authority_load", "process_check", "peer_check"}
    assert {"engine_http", "binding_check"} <= set(available)
    counts_ = rp.instrument_counts([], unavailable)
    assert counts_["peer_checks"] == UNKNOWN and counts_["authority_loads"] == UNKNOWN
    assert counts_["process_checks"] == UNKNOWN and counts_["engine_http_calls"] == 0
    recorder.close()


def test_a_module_with_none_of_the_surfaces_reports_everything_unavailable(tmp_path):
    recorder = rp.InstrumentRecorder(tmp_path / "k.jsonl")
    available, unavailable = rp.install_instrumentation(recorder, SimpleNamespace(), SimpleNamespace())
    assert available == [] and unavailable == list(rp.SURFACES)
    recorder.close()


def test_instrument_window_selection_and_header_parsing(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"kind": "header", "t_ns": 1, "seq": 0},
        {"kind": "surfaces", "t_ns": 2, "seq": 1, "unavailable": ["peer_check"], "available": ["engine_http"]},
        {"kind": "engine_http", "t_ns": 100}, {"kind": "engine_http", "t_ns": 200}, {"kind": "engine_http", "t_ns": 300}]) + "\nnot json\n")
    records = rp.read_instrument_lines(path)
    assert rp.instrument_header(records)["unavailable"] == ["peer_check"]
    assert rp.instrument_header(records[:1])["unavailable"] == list(rp.SURFACES)   # no surfaces record: nothing available
    assert [r["t_ns"] for r in rp.window_events(records, 150, 300)] == [200, 300]
    tail = rp.InstrumentTail(path)
    assert len(tail.refresh()) == 6
    with path.open("a") as handle:
        handle.write(json.dumps({"kind": "engine_http", "t_ns": 400}) + "\n")
        handle.write('{"kind": "partial')                                          # a half-written line is not consumed
    assert len(tail.refresh()) == 7


# ----------------------------------------------------------------------------
# The HTTP driver against a loopback SSE server
# ----------------------------------------------------------------------------

def owned_fixture_socket():
    """The pipeline parent retains a bound socket so an unrelated receiver cannot take its port.
    Direct development runs bind their own ephemeral socket. No test contacts the engine's port.
    """
    inherited = os.environ.get("BENCHMARK_FIXTURE_FD")
    if inherited is not None:
        sock = socket.fromfd(int(inherited), socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(None)
    else:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
    assert sock.getsockname()[0] == "127.0.0.1" and sock.getsockname()[1] != 8000
    return sock


class LoopbackBackend:
    def __init__(self, script=(), status=200, health=None, approve=None):
        self.script, self.status, self.health = list(script), status, health or ({"status": "ok"}, 200)
        self.approve = approve or ({"ok": True}, 200)
        self.agent_bodies, self.approvals = [], []
        state = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                if self.path == "/agent/approve":
                    state.approvals.append(body)
                    payload = json.dumps(state.approve[0]).encode()
                    self.send_response(state.approve[1])
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                state.agent_bodies.append(body)
                self.send_response(state.status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delay, event in state.script:
                    time.sleep(delay)
                    try:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return

            def do_GET(self):
                payload = json.dumps(state.health[0]).encode()
                self.send_response(state.health[1])
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.socket.close()
        self.server.socket = owned_fixture_socket()
        self.server.server_address = self.server.socket.getsockname()
        self.server.server_activate()
        self.server.daemon_threads = True
        OWNED_PORTS.add(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class FaultingServer:
    """Loopback receiver owned by the test: it accepts a connection and resets it, so a transport failure is
    injected at a real socket without ever contacting a port the test does not own."""

    def __init__(self):
        self.sock = owned_fixture_socket()
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        OWNED_PORTS.add(self.port)
        self.accepted, self._stop = 0, threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.accepted += 1
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            connection.close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


def drive(url, spec, prompt="hello"):
    driver = rp.HttpTurnDriver(url)
    out = driver.new_sink()
    asyncio.run(driver.run_turn(spec, prompt, out))
    return out


def test_the_driver_separates_first_visible_text_from_completion_with_real_streaming():
    script = [(0, {"type": "session", "id": "s1"}), (0.05, {"type": "routed", "direct_calls": [], "needs_tools": False}),
              (0.20, {"type": "delta", "text": "Hello"}), (0.40, {"type": "delta", "text": " there!"}),
              (0.10, {"type": "done"}), (0.0, {"type": "delta", "text": "never read after done"})]
    with LoopbackBackend(script) as backend:
        out = drive(backend.url, {**SPECS["greeting_warm"], "deadline_s": 10})
    assert backend.agent_bodies == [{"prompt": "hello", "debug": False}]
    analysis = rp.analyze_events(out["events"])
    assert analysis["saw_done"] and analysis["answer_text"] == "Hello there!"
    assert analysis["first_visible_answer_ns"] >= 0.24 * 1e9
    assert analysis["total_completion_ns"] - analysis["first_visible_answer_ns"] >= 0.45 * 1e9   # visible long before done
    assert analysis["first_model_delta_ns"] == analysis["first_visible_answer_ns"]
    assert out["deadline"] is False and out["transport_error"] is None and out["abs_end_ns"] > out["abs_start_ns"]
    assert all(e["event"]["type"] != "delta" or e["event"]["text"] != "never read after done" for e in out["events"])
    assert [e["t_ns"] for e in out["events"]] == sorted(e["t_ns"] for e in out["events"])


def test_the_driver_denies_every_confirm_through_the_real_approve_endpoint():
    script = [(0, {"type": "session", "id": "sess-9"}),
              (0.1, {"type": "confirm", "id": "c1", "tool": "send_message", "args": {"to": "Alex Rivera"}}),
              (0.1, {"type": "text", "text": "I didn't send it."}), (0, {"type": "done"})]
    with LoopbackBackend(script) as backend:
        out = drive(backend.url, {**SPECS["outbound_approval_boundary"], "deadline_s": 10}, "Text Alex")
    assert backend.approvals == [{"session_id": "sess-9", "action_id": "c1", "approved": False, "scope": "once"}]
    assert out["approvals"][0]["approved"] is False and out["approvals"][0]["accepted"] is True
    analysis = rp.analyze_events(out["events"])
    assert analysis["approval_boundary_ns"] >= 0.09 * 1e9 and analysis["approval_boundary_ns"] < analysis["total_completion_ns"]
    graded = rp.grade_sample(SPECS["outbound_approval_boundary"], analysis, out, counts(1), CORPUS)
    assert "harness_approved_an_effect" not in graded["reasons"]


def test_deadline_cancellation_and_transport_failures_are_recorded_with_partial_events():
    spec = {**SPECS["greeting_warm"], "deadline_s": 0.4}
    with LoopbackBackend([(0, {"type": "session", "id": "s"}), (5.0, {"type": "delta", "text": "late"})]) as backend:
        out = drive(backend.url, spec)
    assert out["deadline"] is True and [e["event"]["type"] for e in out["events"]] == ["session"]
    graded = rp.grade_sample(spec, rp.analyze_events(out["events"]), out, counts(1), CORPUS)
    assert not graded["correct"] and "deadline_exceeded" in graded["reasons"]

    with LoopbackBackend([(0, {"type": "session", "id": "s"})], status=503) as backend:
        out = drive(backend.url, spec)
    assert out["transport_error"] == "http_status_503" and out["events"] == []

    with FaultingServer() as faulty:                                          # an OWNED receiver that resets every call
        refused = drive(faulty.url, spec)
    assert refused["transport_error"] in ("RemoteProtocolError", "ReadError", "ConnectError") and refused["abs_end_ns"]
    assert faulty.accepted >= 1

    async def cancelled():
        with LoopbackBackend([(0, {"type": "session", "id": "s"}), (5.0, {"type": "delta", "text": "x"})]) as backend:
            driver = rp.HttpTurnDriver(backend.url)
            result = driver.new_sink()
            task = asyncio.create_task(driver.run_turn({**SPECS["greeting_warm"], "deadline_s": 30}, "hi", result))
            await asyncio.sleep(0.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return result
    out = asyncio.run(cancelled())
    assert out["cancelled"] is True and out["events"] and out["abs_end_ns"]


def test_the_http_driver_times_a_plain_call_and_captures_status_and_body():
    spec = {**SPECS["health_status_overhead"], "deadline_s": 5}
    driver = rp.HttpTurnDriver("")
    with LoopbackBackend(health=({"status": "ok"}, 200)) as backend:
        driver = rp.HttpTurnDriver(backend.url)
        out = driver.new_sink()
        asyncio.run(driver.run_http(spec, out))
    assert out["http"]["status"] == 200 and out["http"]["json"] == {"status": "ok"}
    assert isinstance(out["latency_ns"], int) and out["latency_ns"] > 0
    assert rp.grade_sample(spec, rp.analyze_events([]), out, counts(1), CORPUS)["correct"]
    with LoopbackBackend(health=({"detail": "Wisp couldn't verify the local AI engine"}, 503)) as backend:
        driver = rp.HttpTurnDriver(backend.url)
        out = driver.new_sink()
        asyncio.run(driver.run_http(spec, out))
    graded = rp.grade_sample(spec, rp.analyze_events([]), out, counts(1), CORPUS)
    assert graded["refusal"] and not graded["correct"]


# ----------------------------------------------------------------------------
# Environment identity, backend command, CPU parsing
# ----------------------------------------------------------------------------

def test_environment_collection_whitelists_settings_and_never_copies_secrets(tmp_path):
    home = tmp_path / "home"
    (home / ".omlx").mkdir(parents=True)
    model_dir = tmp_path / "models" / "Vendor" / "Test-Model"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text(json.dumps({"quantization": {"bits": 4}}))
    (model_dir / "tokenizer.json").write_text("{}")
    (model_dir / "w.safetensors").write_bytes(b"1234")
    (home / ".omlx" / "settings.json").write_text(json.dumps({
        "auth": {"api_key": "SECRET-KEY-VALUE"}, "model": {"model_dirs": [str(tmp_path / "models")]},
        "sampling": {"temperature": 1.0}, "cache": {"enabled": True}}))
    (home / ".omlx" / "model_settings.json").write_text(json.dumps({"models": {"Test-Model": {
        "temperature": 0.7, "enable_thinking": True, "api_token": "nope", "unrelated": 1}}}))
    stub = lambda argv, timeout=10: (0, "value\n", "")
    env = rp.collect_environment("Test-Model", "desktop", 2, runner=stub, home=home, plist=tmp_path / "none.plist")
    dump = json.dumps(env)
    assert "SECRET-KEY-VALUE" not in dump and "nope" not in dump and "unrelated" not in dump
    assert env["model"]["dir_found"] and env["model"]["quantization"] == {"bits": 4}
    assert env["generation_settings"]["model_settings"] == {"temperature": 0.7, "enable_thinking": True}
    assert env["model"]["weights_manifest"]["files"] == [["w.safetensors", 4, hashlib.sha256(b"1234").hexdigest()]]
    assert env["engine"]["app"]["short"] == UNKNOWN                      # plist absent: UNKNOWN, not a guess
    assert env["lane_evidence"]["managed_authorization_manifest_present"] is False
    assert env["cache_warmup_treatment"]["warmup_samples_per_scenario_per_side"] == 2

    gone = rp.collect_environment("No-Such-Model", "desktop", 2, runner=lambda *a, **k: (1, "", ""),
                                  home=home, plist=tmp_path / "none.plist")
    assert gone["model"]["config_sha256"] == UNKNOWN and gone["hardware"]["model"] == UNKNOWN
    assert rp.has_unknown(gone) and not rp.has_unknown({"a": [1, {"b": "x"}]})


def test_the_spawned_backend_entry_point_wires_root_state_guard_and_instrumentation(tmp_path, monkeypatch):
    fake = FakeAttribution()
    launched, routes, state = [], [], {"models": ["m"], "fail": False}

    class App:
        def add_api_route(self, path, endpoint, **kwargs):
            routes.append((path, endpoint, kwargs))

    class Client:
        async def loaded_models(self):
            if state["fail"]:
                raise RuntimeError("engine down")
            return list(state["models"])

    app = App()
    monkeypatch.setitem(sys.modules, "service.inference.attributed_transport", fake.at)
    monkeypatch.setitem(sys.modules, "service.inference.local_peer", fake.lp)
    monkeypatch.setitem(sys.modules, "service.main", SimpleNamespace(app=app, client=Client()))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **k: launched.append((a, k))))
    harness_dir = str(Path(rp.__file__).resolve().parent)
    monkeypatch.setattr(sys, "path", [harness_dir, *sys.path, harness_dir + "/", ""])
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "hostile-codex-root"))
    real_home = os.environ.get("HOME")
    root, home, out = tmp_path / "root", tmp_path / "home", tmp_path / "instr.jsonl"
    root.mkdir()
    args = argparse.Namespace(root=str(root), port=18999, home=str(home), instrument_out=str(out), engine_port=8000,
                              run_id="run-1", side="candidate")
    try:
        assert rp.serve_main(args) == 0
        assert sys.path[0] == str(root.resolve()) and os.getcwd() == str(root.resolve())
        # python scripts/release_performance.py puts the harness's own directory on sys.path; the guarded child must not
        assert all(not entry or os.path.realpath(entry) != harness_dir for entry in sys.path) and "" in sys.path
        assert os.environ["WISP_HOME"] == str(home.resolve())
        assert os.environ["CODEX_HOME"] == str(home.resolve() / "codex")
        assert (home / "codex").is_dir() and list((home / "codex").iterdir()) == []
        assert os.environ.get("HOME") == real_home
        assert launched == [((app,), {"host": "127.0.0.1", "port": 18999, "log_level": "warning"})]
        with pytest.raises(rp.EffectBlocked):                                   # the guard really is installed
            subprocess.Popen(["osascript", "-e", "1"])
    finally:
        assert rp.ACTIVE_GUARD is not None
        rp.ACTIVE_GUARD.uninstall()
    records = rp.read_instrument_lines(out)
    assert [(r["kind"], r["seq"]) for r in records[:2]] == [("header", 0), ("surfaces", 1)]   # identity first
    header = rp.instrument_header(records)
    assert header["effect_guard"] is True and header["root"] == str(root.resolve())
    assert header["run_id"] == "run-1" and header["side"] == "candidate"
    assert header["runtime"]["python_version"] == sys.version.split()[0] and len(header["guard_policy_sha256"]) == 64
    assert header["unavailable"] == [] and "engine_http" in header["available"]
    assert any(r["kind"] == "effect_blocked" and r["what"] == "exec" for r in records)
    # the residency probe is answered by the candidate's OWN client; the harness never holds the credential
    path, handler, kwargs = routes[0]
    assert path == rp.RESIDENCY_PROBE_PATH and kwargs["methods"] == ["GET"]
    assert asyncio.run(handler()) == {"loaded": ["m"]}
    state["fail"] = True
    assert asyncio.run(handler()).status_code == 503


def test_backend_command_never_exposes_an_alternate_engine_port_or_attribution_switch():
    backend = rp.BackendProcess("candidate", Path("/wt/cand"), 18775, Path("/tmp/h"), Path("/tmp/i.jsonl"), "py")
    command = backend.command()
    assert command[0] == "py" and command[2] == "serve"
    assert command[command.index("--engine-port") + 1] == "8000"
    assert backend.base_url == "http://127.0.0.1:18775" and "--root" in command and "/wt/cand" in command
    parser = rp.build_parser()
    run_options = {a.dest for sub in parser._subparsers._group_actions[0].choices.values() if sub.prog.endswith(" run")
                   for a in sub._actions}
    assert "engine_port" not in run_options and not any("attribution" in o for o in run_options)


@pytest.mark.parametrize("text,seconds", [("0:00.51", 0.51), ("1:02:03.45", 3723.45), ("12:30", 750.0),
                                          ("2-01:00:00", 2 * 86400 + 3600), ("  5:00.00 ", 300.0),
                                          ("", None), ("abc", None), ("x-1:00", None), ("1:2:3:4", None)])
def test_ps_cpu_time_parsing(text, seconds):
    assert rp.parse_ps_time(text) == seconds


def test_live_run_requires_the_explicit_confirmation_flag_and_never_starts_by_default(tmp_path):
    argv = [sys.executable, str(SCRIPT), "run", "--candidate-worktree", str(tmp_path), "--candidate-sha", "a" * 40,
            "--baseline-worktree", str(tmp_path), "--baseline-sha", "b" * 40, "--model-id", "m",
            "--output-dir", str(tmp_path / "out")]
    done = subprocess.run(argv, capture_output=True, text=True)
    assert done.returncode == rp.EXIT_USAGE and not (tmp_path / "out").exists()
    assert "execute-live-release-measurement" in done.stderr


# ----------------------------------------------------------------------------
# Legacy harness: behaviour preserved, release entry point delegated
# ----------------------------------------------------------------------------

LEGACY_FINGERPRINTS = {
    "MODEL": "9857475c09c054460dbe1f4bc95759db2aed240cd3fa43c74c95384fa9cb43ce",
    "CLOCK": "83f1f69ce16d015dd306ad7fe85ad8abea71b7d4a26095601bbb41dd90b3ecbd",
    "BODY": "59cd44638f9ebd418b6b7b69b5b6a5a01578dc73b8049378a296f2afca828d5e",
    "save": "fec33bad53f728225a74f197cae78d69d453f9a7bd91fd09c7a9d925cff81763",
    "capture": "7e4a8d66d5ed6291af837d2531ac6f9169b9fc06d24b602ad8ca50ac0b33fa0f",
    "compare": "b74006539e8b423e22c5932359ca571e507f2da838c7f73dd902f337f5a6ac21",
}


def test_legacy_capture_and_compare_code_is_unchanged():
    import ast
    source = LEGACY.read_text()
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest()
        elif isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("MODEL", "CLOCK", "BODY"):
            found[node.targets[0].id] = hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest()
    assert {k: found.get(k) for k in LEGACY_FINGERPRINTS} == LEGACY_FINGERPRINTS


def run_legacy(*args):
    return subprocess.run([sys.executable, str(LEGACY), *args], capture_output=True, text=True)


def test_legacy_cli_still_offers_capture_and_compare_with_their_original_options():
    top = run_legacy("--help")
    assert top.returncode == 0 and "capture" in top.stdout and "compare" in top.stdout
    capture = run_legacy("capture", "--help").stdout
    assert "--source-root" in capture and "--output" in capture
    compare = run_legacy("compare", "--help").stdout
    for option in ("--before", "--after", "--output", "--reps"):
        assert option in compare
    assert run_legacy("capture").returncode == 2                                # legacy usage errors keep exit 2


def test_release_entry_point_delegates_to_the_release_harness_unchanged():
    via_legacy = run_legacy("release", "info")
    direct = subprocess.run([sys.executable, str(SCRIPT), "info"], capture_output=True, text=True)
    assert via_legacy.returncode == direct.returncode == 0
    assert json.loads(via_legacy.stdout) == json.loads(direct.stdout)
    info = json.loads(direct.stdout)
    assert info["corpus_sha256"] == BUNDLE["corpus_sha256"] and info["policy_sha256"] == BUNDLE["policy_sha256"]
    assert info["harness_sha256"] == rp.harness_sha256() and info["bundle_problems"] == []
    assert info["min_measured_samples"] == 20 and len(info["required_scenarios"]) == 6
    check = run_legacy("release", "check")                                       # missing args pass through as a usage error
    assert check.returncode == rp.EXIT_USAGE


def test_propose_baseline_cli_writes_only_an_unapproved_proposal(good, tmp_path):
    target = tmp_path / "p.json"
    done = subprocess.run([sys.executable, str(SCRIPT), "propose-baseline", "--receipt", str(good.receipt_path),
                           "--output", str(target)], capture_output=True, text=True)
    assert done.returncode == 0 and json.loads(target.read_text())["status"] == "proposed"
    again = subprocess.run([sys.executable, str(SCRIPT), "propose-baseline", "--receipt", str(good.receipt_path),
                            "--output", str(target)], capture_output=True, text=True)
    assert again.returncode == rp.EXIT_REFUSED


# ============================================================================
# Repairs for the independent audit of 4978d2d (F01-F11, N01, N02)
# ============================================================================

BOUNDED_OK = [ev(1, type="session", id="s"), ev(450, type="reasoning", text="working it out"),
              ev(900, type="text", text="It arrives at 6:30 PM."), ev(905, type="done")]


# ---- F07: a terminal done is not proof the model finished its answer ----

@pytest.mark.parametrize("reason", ["length", "content_filter", "abort", "max_tokens"])
def test_f07_a_token_limited_answer_with_every_expected_fact_is_incorrect(reason):
    for events in (BOUNDED_OK, [e for e in BOUNDED_OK if e["event"]["type"] != "reasoning"]):   # with and without reasoning
        graded, _ = grade("bounded_reasoning", events, instrument=counts(1, completion_reasons=[reason]))
        assert not graded["correct"] and f"incomplete_completion:{reason}" in graded["reasons"]
    assert grade("bounded_reasoning", BOUNDED_OK, instrument=counts(1, completion_reasons=["stop"]))[0]["correct"]
    assert grade("scoped_tool_selection", selection_events(), instrument=counts(2, completion_reasons=["tool_calls", "stop"]))[0]["correct"]


def test_f07_unobservable_or_missing_completion_status_is_unverifiable_never_a_pass():
    cases = {"unknown surface": counts(1, completion_reasons=UNKNOWN),
             "no record at all": counts(1, completion_reasons=[]),
             "fewer records than calls": counts(2, completion_reasons=["stop"]),
             "record without a reason": counts(1, completion_reasons=[None])}
    for name, instrument in cases.items():
        graded, _ = grade("bounded_reasoning" if instrument["chat_calls"] == 1 else "scoped_tool_selection",
                          BOUNDED_OK if instrument["chat_calls"] == 1 else selection_events(), instrument=instrument)
        assert not graded["correct"] and graded["reasons"] == [] and graded["unverifiable"], name
    assert "completion_status" in grade("bounded_reasoning", BOUNDED_OK,
                                        instrument=counts(1, completion_reasons=UNKNOWN))[0]["unverifiable"]
    # a length ending is a hard failure (BLOCK territory), an unverifiable one is INCONCLUSIVE territory
    hard = grade("bounded_reasoning", BOUNDED_OK, instrument=counts(1, completion_reasons=["length"]))[0]
    assert hard["reasons"] and not hard["unverifiable"]


def sse(*events: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"


def test_f07_the_completion_parser_reads_streams_split_anywhere_and_json_bodies():
    stream = sse({"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]},
                 {"choices": [{"delta": {}, "finish_reason": "length"}]})
    for cut in range(1, len(stream)):                                            # every possible chunk boundary
        parser = rp.CompletionParser()
        parser.feed(stream[:cut])
        parser.feed(stream[cut:])
        assert parser.result() == ("length", 1, "ok"), cut
    body = json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}).encode()
    for cut in (1, 7, len(body) - 1):
        parser = rp.CompletionParser()
        parser.feed(body[:cut])
        parser.feed(body[cut:])
        assert parser.result() == ("stop", 1, "ok")
    tail = rp.CompletionParser()
    tail.feed(b'data: {"choices": [{"finish_reason": "stop"}]}')                  # last line without a newline
    assert tail.result() == ("stop", 1, "ok")
    none = rp.CompletionParser()
    none.feed(sse({"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]}))
    assert none.result() == (None, 1, "none")
    for garbage in (b"", b"<html>", b"data: {not json}\n", b'{"choices": 5}', b'{"no": "choices"}'):
        parser = rp.CompletionParser()
        parser.feed(garbage)
        assert parser.result()[0] is None and parser.result()[2] == "unparseable", garbage


def test_f07_a_capture_limit_never_invents_a_finish_reason(monkeypatch):
    monkeypatch.setattr(rp, "_TEE_BODY_LIMIT", 16)
    parser = rp.CompletionParser()
    parser.feed(json.dumps({"choices": [{"message": {"content": "x" * 50}, "finish_reason": "stop"}]}).encode())
    assert parser.result() == (None, 0, "capture_truncated")


class StreamOf(__import__("httpx").AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = list(chunks), False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def instrumented_transport(tmp_path, chunks, *, status=200):
    import httpx
    created = {}

    class CredentialTransport:
        async def handle_async_request(self, request):
            created["stream"] = StreamOf(chunks)
            return httpx.Response(status, stream=created["stream"])

    at = SimpleNamespace(CredentialTransport=CredentialTransport)
    recorder = rp.InstrumentRecorder(tmp_path / "t.jsonl")
    rp.install_instrumentation(recorder, at, SimpleNamespace())
    return at.CredentialTransport(), recorder, created


def test_f07_the_tee_records_the_engine_finish_reason_without_changing_a_single_byte(tmp_path):
    import httpx
    chunks = [b'data: {"choices": [{"delta": {"content": "Hi"}, "finish_reason": null}]}\n\n',
              b'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}\n\n', b"data: [DONE]\n\n"]
    transport, recorder, created = instrumented_transport(tmp_path, chunks)
    request = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions",
                            json={"model": "m", "temperature": 0.3, "max_tokens": 64, "stream": True,
                                  "messages": [{"role": "user", "content": "SECRET-PROMPT-TEXT"}],
                                  "tools": [{"type": "function"}], "chat_template_kwargs": {"enable_thinking": False}})

    async def go():
        response = await transport.handle_async_request(request)
        body = b"".join([chunk async for chunk in response.stream])
        await response.stream.aclose()
        return body
    assert asyncio.run(go()) == b"".join(chunks)                                # byte-identical, same order
    assert created["stream"].closed is True                                       # closing is delegated
    recorder.close()
    records = rp.read_instrument_lines(tmp_path / "t.jsonl")
    http_record = next(r for r in records if r["kind"] == "engine_http")
    done = next(r for r in records if r["kind"] == "engine_completion")
    assert done["finish_reason"] == "length" and done["parse"] == "ok" and done["complete_stream"] is True
    assert http_record["request_settings"] == {"model": "m", "temperature": 0.3, "max_tokens": 64, "stream": True,
                                               "chat_template_kwargs": {"enable_thinking": False}, "tool_count": 1}
    assert "SECRET-PROMPT-TEXT" not in (tmp_path / "t.jsonl").read_text()        # prompts are never recorded


def test_f07_an_early_close_and_a_non_chat_call_are_each_recorded_honestly(tmp_path):
    import httpx
    transport, recorder, _ = instrumented_transport(tmp_path, [b'data: {"choices": [{"finish_reason": null}]}\n\n'])
    chat = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions", json={"model": "m"})

    async def early():
        response = await transport.handle_async_request(chat)
        await response.stream.__aiter__().__anext__()                             # one chunk, then closed early
        await response.stream.aclose()
    asyncio.run(early())
    other, recorder3, _ = instrumented_transport(tmp_path, [b"{}"])

    async def not_chat():
        response = await other.handle_async_request(httpx.Request("GET", "http://127.0.0.1:8000/health"))
        [chunk async for chunk in response.stream]
    asyncio.run(not_chat())
    recorder.close()
    recorder3.close()
    records = rp.read_instrument_lines(tmp_path / "t.jsonl")
    completions = [r for r in records if r["kind"] == "engine_completion"]
    assert [(c["finish_reason"], c["complete_stream"], c["parse"]) for c in completions] == [(None, False, "none")]
    assert sum(1 for r in records if r["kind"] == "engine_http") == 2           # the health call has no completion record


def test_f07_a_request_whose_body_cannot_be_read_is_unknown_not_empty():
    assert rp.request_settings(b"not json") is None and rp.request_settings(None) is None
    assert rp.request_settings(b"[1, 2]") is None
    assert rp.request_settings(json.dumps({"model": "m", "messages": [{"content": "x"}]}).encode()) == \
        {"model": "m", "tool_count": 0}


# ---- F08: the denial must be a complete, matched lifecycle ----

def outbound(**changes):
    events = changes.pop("events", None) or outbound_events(**changes.pop("event_args", {}))
    record = dict(DENIED["approvals"][0], **changes.pop("record", {}))
    approvals = changes.pop("approvals", [record])
    return grade("outbound_approval_boundary", events, approvals=approvals, **changes)[0]


def test_f08_the_complete_matched_denial_lifecycle_is_the_only_correct_outcome():
    assert outbound()["correct"]


@pytest.mark.parametrize("kwargs,reason", [
    ({"approvals": []}, "denial_not_recorded"),
    ({"record": {"accepted": False}}, "denial_not_accepted"),
    ({"record": {"accepted": None}}, "denial_not_accepted"),
    ({"record": {"action_id": "other-action"}}, "denial_for_another_action"),
    ({"record": {"session_id": "another-session"}}, "denial_session_mismatch"),
    ({"record": {"session_id": None}}, "denial_session_mismatch"),
    ({"record": {"approved": True}}, "harness_approved_an_effect"),
    ({"record": {"approved": 0}}, "harness_approved_an_effect"),                  # only the exact value False is a denial
    ({"event_args": {"result": None}}, "denial_result_missing"),
    ({"event_args": {"result": "Message sent to +15555550123"}}, "denial_result_not_a_denial"),
])
def test_f08_every_break_in_the_denial_lifecycle_is_a_failure(kwargs, reason):
    graded = outbound(**kwargs)
    assert reason in graded["reasons"] and not graded["correct"]


def test_f08_duplicate_confirmations_duplicate_denials_and_stray_approvals_are_failures():
    twice = outbound_events()
    twice.insert(-3, ev(1550, type="confirm", id="c9", tool="send_message", args={"to": "Alex Rivera", "text": "ten minutes"}))
    assert "duplicate_confirmation" in outbound(events=twice)["reasons"]
    record = DENIED["approvals"][0]
    assert "duplicate_denial" in outbound(approvals=[record, dict(record)])["reasons"]
    assert "denial_for_another_action" in outbound(approvals=[record, dict(record, action_id="zz")])["reasons"]
    stray = grade("greeting_warm", OK_GREETING, approvals=[dict(record)])[0]
    assert "approval_without_a_confirm" in stray["reasons"] and not stray["correct"]


def test_f08_the_requested_operation_is_validated_completely():
    wrong_person = outbound_events()
    for event in wrong_person:
        if event["event"].get("type") == "confirm":
            event["event"]["args"] = {"to": "Someone Else", "text": "I will be ten minutes late"}
    assert "approval_arguments_mismatch" in outbound(events=wrong_person)["reasons"]
    wrong_text = outbound_events()
    for event in wrong_text:
        if event["event"].get("type") == "confirm":
            event["event"]["args"] = {"to": "Alex Rivera", "text": "see you at noon"}
    assert "approval_arguments_mismatch" in outbound(events=wrong_text)["reasons"]
    by_number = outbound_events()
    for event in by_number:
        if event["event"].get("type") == "confirm":
            event["event"]["args"] = {"to": "+15555550123", "text": "ten minutes late"}
    assert outbound(events=by_number)["correct"]                                  # the contact's number is also Alex


def test_f08_the_driver_records_the_endpoints_own_verdict_session_and_status():
    script = [(0, {"type": "session", "id": "sess-9"}),
              (0.05, {"type": "confirm", "id": "c1", "tool": "send_message", "args": {"to": "Alex Rivera"}}),
              (0.05, {"type": "text", "text": "I didn't send it."}), (0, {"type": "done"})]
    spec = {**SPECS["outbound_approval_boundary"], "deadline_s": 10}
    with LoopbackBackend(script) as backend:
        record = drive(backend.url, spec, "Text Alex")["approvals"][0]
    assert (record["session_id"], record["action_id"], record["approved"], record["accepted"],
            record["status_code"]) == ("sess-9", "c1", False, True, 200)
    with LoopbackBackend(script, approve=({"ok": False, "error": "no pending action for that id"}, 200)) as backend:
        record = drive(backend.url, spec, "Text Alex")["approvals"][0]
    assert record["accepted"] is False                                           # the endpoint matched nothing
    with LoopbackBackend(script, approve=({"ok": True}, 500)) as backend:
        record = drive(backend.url, spec, "Text Alex")["approvals"][0]
    assert record["accepted"] is False and record["status_code"] == 500


# ---- F01: the preflight never releases the engine credential ----

def test_f01_the_preflight_sends_nothing_to_the_engine_and_never_reads_the_credential(tmp_path, monkeypatch):
    import builtins
    import io
    home = tmp_path / "home"
    (home / ".omlx").mkdir(parents=True)
    (home / ".omlx" / "settings.json").write_text(json.dumps({"auth": {"api_key": "SECRET-KEY-VALUE"}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    opened, connects, sent = [], [], []

    class ProbeSocket:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def settimeout(self, value): pass
        def connect_ex(self, address):
            connects.append(address)
            return 0 if address == ("127.0.0.1", 8000) else 61
        def send(self, data, *a): sent.append(data)
        sendall = send

    real_open, real_os_open = builtins.open, os.open
    monkeypatch.setattr(socket, "socket", ProbeSocket)
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: opened.append(str(f)) or real_open(f, *a, **k))
    monkeypatch.setattr(io, "open", builtins.open)
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(str(p)) or real_os_open(p, *a, **k))
    deps = rp.LiveDeps(sys.executable, {"candidate": 18775, "baseline": 18776})
    assert deps.preflight("desktop", "m") == []
    assert ("127.0.0.1", 8000) in connects and sent == []                          # a bare connect: no bytes at all
    assert not [p for p in opened if ".omlx" in p or "settings" in p]            # the credential file is never opened
    monkeypatch.setattr(ProbeSocket, "connect_ex", lambda self, address: 61)
    missing = deps.preflight("desktop", "m")
    assert [m["id"] for m in missing] == ["engine_unreachable_on_8000"]


def test_f01_the_harness_contains_no_credential_path_or_unattributed_engine_client():
    source = SCRIPT.read_text()
    for forbidden in ("urllib", "api_key", "Authorization", "engine_resident_models", "urlopen"):
        assert forbidden not in source, forbidden
    assert not hasattr(rp, "engine_resident_models")


def test_f01_residency_is_read_through_the_candidates_own_client_and_unknown_is_not_a_model_list():
    class Backend:
        def __init__(self, url): self.base_url = url
    deps = rp.LiveDeps(sys.executable, {})
    with LoopbackBackend(health=({"loaded": ["B", "A"]}, 200)) as backend:
        assert deps.residency(Backend(backend.url)) == ["A", "B"]                  # sorted, from the probe route only
    with LoopbackBackend(health=({"error": "ModelLoadError"}, 503)) as backend:
        assert deps.residency(Backend(backend.url)) is None
    with LoopbackBackend(health=({"loaded": "A"}, 200)) as backend:
        assert deps.residency(Backend(backend.url)) is None
    with FaultingServer() as faulty:
        assert deps.residency(Backend(faulty.url)) is None


def test_residency_retries_a_transient_refusal_a_bounded_number_of_times(monkeypatch):
    import httpx
    class Backend:
        base_url = "http://127.0.0.1:1"
    deps = rp.LiveDeps(sys.executable, {})
    monkeypatch.setattr(rp.LiveDeps, "RESIDENCY_PAUSE_S", 0.0)
    replies, calls = [], []

    def fake_get(url, timeout):
        calls.append(url)
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item[0], json=item[1])

    monkeypatch.setattr(httpx, "get", fake_get)
    replies[:] = [(503, {"error": "ModelLoadError"}), httpx.ConnectError("x"), (200, {"loaded": ["B", "A"]})]
    assert deps.residency(Backend()) == ["A", "B"] and len(calls) == 3
    calls.clear()
    replies[:] = [(200, {"loaded": []}), (503, {})]
    assert deps.residency(Backend()) == [] and len(calls) == 1             # an answer, even an empty one, is final
    calls.clear()
    replies[:] = [(503, {"error": "ModelLoadError"})] * 5
    assert deps.residency(Backend()) is None and len(calls) == rp.LiveDeps.RESIDENCY_ATTEMPTS == 3


# ---- F02: engine operations are permitted explicitly and refused before transmission ----

@pytest.mark.parametrize("method,path,allowed", [
    ("GET", "/health", True), ("GET", "/v1/models", True), ("GET", "/v1/models/status", True),
    ("POST", "/v1/chat/completions", True), ("POST", "/v1/embeddings", True), ("POST", "/v1/rerank", True),
    ("POST", "/v1/models/Some-Model/unload", False), ("POST", "/v1/models/Some-Model/load", False),
    ("DELETE", "/v1/models/Some-Model", False), ("PUT", "/v1/settings", False), ("POST", "/admin/api/settings", False),
    ("POST", "/v1/chat/completions/extra", False), ("GET", "/v1/chat/completions", False),
    ("POST", "/health", False), ("GET", "/v1/models/status/extra", False)])
def test_f02_only_listed_engine_operations_are_transmitted_and_the_rest_never_leave(method, path, allowed, monkeypatch):
    import httpx
    import http.client
    sent = []

    async def fake_send(self, request, *a, **k):
        sent.append(("httpx", request.method, request.url.path))
        return "SENT"

    def fake_putrequest(self, method, url, *a, **k):
        sent.append(("http.client", method, url))

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    monkeypatch.setattr(http.client.HTTPConnection, "putrequest", fake_putrequest)
    emitted = []
    with rp.EffectGuard(lambda kind, **f: emitted.append((kind, f)), allowed_ports={8000}):
        request = httpx.Request(method, f"http://127.0.0.1:8000{path}", headers={"Authorization": "Bearer SYNTHETIC"})
        connection = http.client.HTTPConnection("127.0.0.1", 8000)
        if allowed:
            assert asyncio.run(httpx.AsyncClient().send(request)) == "SENT"
            connection.putrequest(method, path + "?x=1")
            assert len(sent) == 2 and emitted == []
        else:
            with pytest.raises(rp.EffectBlocked):
                asyncio.run(httpx.AsyncClient().send(request))
            with pytest.raises(rp.EffectBlocked):
                connection.putrequest(method, path)
            assert sent == []                                                       # nothing was transmitted, by any client
            assert [(k, f["what"]) for k, f in emitted] == [("effect_blocked", "engine_operation")] * 2
            assert emitted[0][1]["target"] == f"{method} {path}"


def test_f02_http_client_to_any_other_host_or_port_is_refused(monkeypatch):
    import http.client
    monkeypatch.setattr(http.client.HTTPConnection, "putrequest", lambda *a, **k: pytest.fail("transmitted"))
    with rp.EffectGuard(lambda *a, **k: None, allowed_ports={8000}):
        for host, port in (("example.com", 80), ("127.0.0.1", 9999), ("192.168.1.5", 8000)):
            with pytest.raises(rp.EffectBlocked):
                http.client.HTTPConnection(host, port).putrequest("GET", "/health")


# ---- F03: process capability, environment and filesystem containment ----

@pytest.mark.parametrize("argv,allowed", [
    (LSOF_LISTEN, True),
    (["/usr/sbin/lsof", "-nP", "-a", "-p", "123", "-d", "txt", "-Fn"], True),
    (["/usr/sbin/lsof", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fpufn"], True),
    (["/usr/sbin/lsof", "-nP", "-a", "-iTCP:51234", "-sTCP:ESTABLISHED", "-FpufPtTn", "-Ts"], True),
    (PS_ROW, True),
    (["/usr/sbin/lsof"], False), (["/usr/sbin/lsof", "-c", "python"], False), (["/usr/sbin/lsof", "+D", "/Users"], False),
    (["/usr/sbin/lsof", "-nP", "-a", "-p", "123", "-d", "txt", "-Fn", "-r", "1"], False),
    (["/usr/sbin/lsof", "-nP", "-a", "-iTCP:8000", "-sTCP:LISTEN", "-Fpufn;id"], False),
    (["/usr/sbin/lsof", "-nP", "-a", "-p", "0", "-d", "txt", "-Fn"], False),
    (["/bin/ps", "-eo", "pid,command"], False), (PS_ROW + ["-A"], False),
    (["/bin/ps", "-ww", "-p", "0", "-o", "ppid=,uid=,comm="], False),
    (["/bin/sh", "-c", "id"], False), (["lsof", "-nP"], False)])
def test_f03_a_program_name_is_not_a_capability_only_the_exact_inspector_invocations_run(argv, allowed, monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, args, *a, **k: setattr(self, "_child_created", False)
                        or ran.append(list(args)))
    emitted = []
    with rp.EffectGuard(lambda kind, **f: emitted.append((kind, f))):
        if allowed:
            subprocess.Popen(argv)
            assert ran == [argv] and emitted == []
        else:
            with pytest.raises(rp.EffectBlocked):
                subprocess.Popen(argv)
            assert ran == [] and emitted and emitted[0][0] == "effect_blocked"


def test_f03_executable_and_shell_overrides_are_rejected_even_for_an_allowed_argv():
    guard = rp.EffectGuard(lambda *a, **k: None)
    guard._check_exec({"args": LSOF_LISTEN, "executable": None, "shell": False})            # the plain call is fine
    for bound in ({"args": LSOF_LISTEN, "executable": "/bin/sh"},
                  {"args": LSOF_LISTEN, "shell": True},
                  {"args": " ".join(LSOF_LISTEN), "shell": True},
                  {"args": LSOF_LISTEN[0]},                                               # right program, no arguments
                  {"args": b"/bin/sh"}, {"args": []}):
        with pytest.raises(rp.EffectBlocked):
            guard._check_exec(bound)
    guard._check_exec({"args": PS_ROW, "executable": PS_ROW[0]})                        # naming the same program is harmless


def test_f03_the_backend_environment_is_an_explicit_allowlist_with_nothing_ambient(tmp_path, monkeypatch):
    parent = {"HOME": "/Users/x", "USER": "x", "LOGNAME": "x", "LANG": "en_US.UTF-8", "LC_ALL": "C", "PATH": "/opt/evil:/usr/bin",
              "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib", "DYLD_LIBRARY_PATH": "/tmp", "LD_PRELOAD": "x",
              "HTTP_PROXY": "http://p", "HTTPS_PROXY": "http://p", "ALL_PROXY": "socks5://p", "NO_PROXY": "*",
              "PYTHONPATH": "/tmp/x", "PYTHONSTARTUP": "/tmp/s.py", "PYTHONHOME": "/tmp", "PYTHONINSPECT": "1",
              "WISP_BACKEND_URL": "http://elsewhere", "WISP_LOCAL_OMLX_KEY": "SECRET", "WISP_CREDENTIAL_GENERATION": "g",
              "OPENAI_API_KEY": "SECRET2", "SSL_CERT_FILE": "/tmp/ca", "__CF_USER_TEXT_ENCODING": "x",
              "CODEX_HOME": "/private/hostile/codex"}
    home = tmp_path / "home"
    env = rp.child_environment(home, parent)
    assert set(env) == {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "PATH", "WISP_HOME", "TMPDIR",
                        "CODEX_HOME", "PYTHONDONTWRITEBYTECODE"}
    assert env["PATH"] == rp.CHILD_PATH and env["WISP_HOME"] == str(home) and env["TMPDIR"] == str(home / "tmp")
    assert env["CODEX_HOME"] == str(home / "codex")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["HOME"] == "/Users/x"       # HOME is kept: the engine files live there
    assert "SECRET" not in json.dumps(env)

    spawned = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.update(argv=argv, **kwargs)
            self.pid = 1

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    for name, value in parent.items():
        monkeypatch.setenv(name, value)
    backend = rp.BackendProcess("candidate", tmp_path / "wt", 18775, home, tmp_path / "i.jsonl", sys.executable, run_id="r")
    (tmp_path / "wt").mkdir()
    backend.start()
    assert spawned["env"] == rp.child_environment(home) and spawned["start_new_session"] is True
    assert spawned["cwd"] == str(tmp_path / "wt") and (home / "tmp").is_dir()
    assert (home / "codex").is_dir() and list((home / "codex").iterdir()) == []
    assert not any(k.startswith(("DYLD", "HTTP", "PYTHON", "WISP_BACKEND", "WISP_LOCAL", "OPENAI")) for k in spawned["env"]
                   if k not in ("PYTHONDONTWRITEBYTECODE",))
    assert backend.command()[-4:] == ["--run-id", "r", "--side", "candidate"]


def test_the_fresh_codex_monitor_singleton_polls_only_the_empty_owned_root(tmp_path, monkeypatch):
    import sqlite3
    home, real_home = tmp_path / "owned", tmp_path / "real-home"
    (home / "codex").mkdir(parents=True)
    (real_home / ".codex").mkdir(parents=True)
    (real_home / ".codex/state_1.sqlite").write_bytes(b"synthetic private database")
    env = rp.child_environment(home, {"HOME": str(real_home), "CODEX_HOME": str(real_home / ".codex")})
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("CODEX_HOME", env["CODEX_HOME"])
    monkeypatch.setitem(sys.modules, "service.paths", SimpleNamespace(MOE_DIR=home))
    module_name = "_release_fixture_codex_monitor"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "service/codex_monitor.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    connections, scans, emitted = [], [], []
    def no_database(*args, **kwargs):
        connections.append(args)
        raise AssertionError("empty owned Codex root must never open a database")
    original_glob = Path.glob
    def glob(path, *args, **kwargs):
        scans.append(path)
        return original_glob(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", no_database)
    monkeypatch.setattr(Path, "glob", glob)
    policy = rp.build_fs_policy(ROOT, home, real_home=real_home)
    with rp.EffectGuard(lambda kind, **fields: emitted.append((kind, fields)), fs=policy):
        spec.loader.exec_module(module)
        assert module.codex_monitor.codex_dir == home / "codex"
        assert module.codex_monitor.state_path == home / "codex_monitor_state.json"
        for _ in range(2):
            with pytest.raises(module.CodexMonitorUnavailable, match="database was not found"):
                module.codex_monitor.poll_events()
    assert scans == [home / "codex", home / "codex"]
    assert connections == [] and emitted == []
    assert not (home / "codex_monitor_state.json").exists()


def fs_world(tmp_path):
    real_home = tmp_path / "realhome"
    for sub in (".omlx", ".moe", "Documents", "wisp"):
        (real_home / sub).mkdir(parents=True)
    for rel in (".omlx/settings.json", ".omlx/model_settings.json", ".moe/omlx-runtime-authorization.json",
                "Documents/private.txt", ".moe/notes.txt"):
        (real_home / rel).write_text("x")
    code = tmp_path / "realhome" / "wisp" / "worktree"
    code.mkdir()
    (code / "mod.py").write_text("x")
    throwaway = tmp_path / "throwaway"
    throwaway.mkdir()
    policy = rp.build_fs_policy(code, throwaway, real_home=real_home)
    return real_home, code, throwaway, policy


def test_f03_filesystem_policy_denies_the_real_home_except_exact_inputs_and_the_lease_file(tmp_path):
    real_home, code, throwaway, policy = fs_world(tmp_path)
    readable = [real_home / ".omlx/settings.json", real_home / ".omlx/model_settings.json",
                real_home / ".moe/omlx-runtime-authorization.json", code / "mod.py", throwaway / "anything",
                Path("/usr/lib/libSystem.B.dylib"), Path(sys.prefix) / "pyvenv.cfg"]
    for path in readable:
        assert policy.read_allowed(policy.absolute(path)), path
    denied = [real_home / "Documents/private.txt", real_home / ".moe/notes.txt", real_home / ".ssh/id_ed25519",
              real_home / "Library/Messages/chat.db", real_home / ".moe/provisioning/endpoints.json"]
    for path in denied:
        assert not policy.read_allowed(policy.absolute(path)), path
    assert policy.write_allowed(policy.absolute(throwaway / "state.db"))
    assert policy.write_allowed(policy.absolute(real_home / ".moe/.provisioning.lock"))      # the product's own lease file
    assert policy.mkdir_allowed(policy.absolute(real_home / ".moe"))
    for path in (real_home / ".moe/other.lock", real_home / "Documents/new.txt", code / "mod.py", tmp_path / "elsewhere",
                 real_home / ".omlx/settings.json", Path("/tmp/outside"), Path("/etc/hosts")):
        assert not policy.write_allowed(policy.absolute(path)), path
    assert not policy.mkdir_allowed(policy.absolute(real_home / ".moe/sub"))
    link = throwaway / "escape"
    link.symlink_to(real_home / "Documents")                                         # a link inside the sandbox
    assert not policy.write_allowed(policy.absolute(link / "x.txt"))                 # does not lead out of it


def test_f03_the_installed_guard_blocks_real_file_operations_and_records_each_refusal(tmp_path):
    real_home, code, throwaway, policy = fs_world(tmp_path)
    emitted = []
    guard = rp.EffectGuard(lambda kind, **f: emitted.append((kind, f)), fs=policy)
    with guard:
        assert open(real_home / ".omlx/settings.json").read() == "x"                 # exact read-only input
        assert (code / "mod.py").read_text() == "x"
        (throwaway / "ok.txt").write_text("fine")                                   # throwaway state
        os.mkdir(throwaway / "d")
        fd = os.open(real_home / ".moe/.provisioning.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)  # qualified lock acquisition
        os.close(fd)
        for action in (lambda: open(real_home / "Documents/private.txt").read(),
                       lambda: (real_home / "Documents/private.txt").read_text(),
                       lambda: os.listdir(real_home / "Documents"),
                       lambda: list(os.scandir(real_home / "Documents")),
                       lambda: os.open(real_home / "Documents/private.txt", os.O_RDONLY),
                       lambda: open(real_home / "Documents/new.txt", "w"),
                       lambda: (real_home / "Documents/new.txt").write_text("x"),
                       lambda: os.open(real_home / "Documents/new.txt", os.O_WRONLY | os.O_CREAT),
                       lambda: os.mkdir(real_home / "Documents/sub"),
                       lambda: os.remove(real_home / "Documents/private.txt"),
                       lambda: os.rename(real_home / "Documents/private.txt", throwaway / "moved"),
                       lambda: os.rename(throwaway / "ok.txt", real_home / "Documents/moved"),
                       lambda: os.symlink(real_home / "Documents", real_home / "Documents/link"),
                       lambda: os.chmod(real_home / "Documents/private.txt", 0o777),
                       lambda: (code / "mod.py").write_text("tamper"),
                       lambda: open(tmp_path / "outside.txt", "w")):
            with pytest.raises(rp.EffectBlocked):
                action()
    kinds = {f["what"] for _, f in emitted}
    assert kinds == {"fs_read", "fs_write"} and len(emitted) >= 16
    assert (real_home / "Documents/private.txt").read_text() == "x" and not (real_home / "Documents/new.txt").exists()
    assert (code / "mod.py").read_text() == "x" and not (tmp_path / "outside.txt").exists()
    assert (throwaway / "ok.txt").read_text() == "fine" and (real_home / ".moe/.provisioning.lock").exists()


def test_f03_the_filesystem_guard_is_reversible_and_the_policy_digest_has_no_per_run_paths(tmp_path):
    import builtins
    real_home, code, throwaway, policy = fs_world(tmp_path)
    before = (builtins.open, os.open, os.mkdir, os.listdir, os.scandir, os.remove, os.rename)
    with rp.EffectGuard(lambda *a, **k: None, fs=policy):
        assert builtins.open is not before[0] and os.listdir is not before[3]
    assert (builtins.open, os.open, os.mkdir, os.listdir, os.scandir, os.remove, os.rename) == before
    other_code, other_home = tmp_path / "other_wt", tmp_path / "other_throwaway"
    other_code.mkdir()
    other_home.mkdir()
    twin = rp.build_fs_policy(other_code, other_home, real_home=real_home)
    sha = lambda fs: rp.EffectGuard(lambda *a, **k: None, fs=fs).policy_sha256()
    assert sha(policy) == sha(twin)                                                # candidate and baseline children match
    narrower = rp.FsPolicy(home=real_home, read_roots=[code], write_roots=[throwaway], read_exact=[], write_exact=[],
                           mkdir_exact=[], labels=policy.labels)
    assert sha(narrower) != sha(policy)                                            # a different policy is a different identity
    assert rp.EffectGuard(lambda *a, **k: None).policy_sha256() != sha(policy)


SH_GRAMMAR = {"/bin/sh": (r"-c echo [a-z]+",)}   # sh is the one program the QA sandbox lets a test child exec


def test_a_guard_allowed_subprocess_with_pipes_runs_under_the_filesystem_guard(tmp_path):
    """The live failure: subprocess wraps its own pipes with io.open(<fd>), which the descriptor rule refused."""
    real_home, code, throwaway, policy = fs_world(tmp_path)
    emitted = []
    child = [sys.executable, "-c", "print(1)"]
    guard = rp.EffectGuard(lambda kind, **f: emitted.append((kind, f)), fs=policy,
                           exec_grammar={sys.executable: (r"-c print\(1\)",)})
    with guard:
        result = subprocess.run(child, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        assert result.returncode == 0 and result.stdout == b"1\n" and result.stderr == b""
        with subprocess.Popen(child, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            assert proc.communicate(timeout=30)[0] == b"1\n"
        assert subprocess.run(child, stdout=subprocess.PIPE, text=True, timeout=30).stdout == "1\n"
        for argv in ([sys.executable, "-c", "print(2)"], [sys.executable, "-c", "print(1)", "x"], ["/bin/ls"]):
            with pytest.raises(rp.EffectBlocked):
                subprocess.run(argv, stdout=subprocess.PIPE)
    assert [f["what"] for _, f in emitted] == ["exec"] * 3                  # nothing but the refused argv shapes


def test_the_popen_pipe_allowance_covers_only_the_pipes_the_allowed_call_created(tmp_path, monkeypatch):
    import threading
    real_home, code, throwaway, policy = fs_world(tmp_path)
    private = real_home / "Documents/private.txt"
    emitted, opened, errors = [], [], []
    outside_r, outside_w = os.pipe()
    private_fd = os.open(private, os.O_RDONLY)
    sock_a, sock_b = socket.socketpair()
    original_init = subprocess.Popen.__init__

    def fake_init(self, args, *a, **k):
        self._child_created = False
        read_end, write_end = os.pipe()
        try:
            for label, attempt in (
                    ("own_pipe_rb", lambda: open(read_end, "rb", 0)),
                    ("own_pipe_wb", lambda: open(write_end, "wb", 0)),
                    ("own_pipe_text", lambda: open(read_end, "r", closefd=False)),          # not the rb/wb shape
                    ("own_pipe_rplus", lambda: open(write_end, "rb+", closefd=False)),
                    ("outside_pipe", lambda: open(outside_r, "rb", 0)),                     # not created by this call
                    ("private_file_fd", lambda: open(private_fd, "rb", closefd=False)),
                    ("socket_fd", lambda: open(sock_a.fileno(), "rb", closefd=False)),
                    ("by_path", lambda: open(private, "rb"))):
                try:
                    attempt().close()
                    opened.append(label)
                except rp.EffectBlocked:
                    pass

            def other_thread():
                try:
                    open(read_end, "rb", closefd=False).close()
                    opened.append("other_thread")
                except rp.EffectBlocked:
                    pass
                except BaseException as exc:                                                  # noqa: BLE001
                    errors.append(exc)

            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join()
        finally:
            for fd in (read_end, write_end):
                try:
                    os.close(fd)
                except OSError:
                    pass
    try:
        monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
        with rp.EffectGuard(lambda kind, **f: emitted.append((kind, f)), fs=policy, exec_grammar=SH_GRAMMAR):
            subprocess.Popen(["/bin/sh", "-c", "echo ok"])
            assert opened == ["own_pipe_rb", "own_pipe_wb"] and errors == []
            # The window closes with the call: the same numbers (recorded or not) are refused afterwards.
            before = len(emitted)
            for fd in (outside_r, private_fd, sock_a.fileno()):
                with pytest.raises(rp.EffectBlocked):
                    open(fd, "rb", closefd=False)
            with pytest.raises(rp.EffectBlocked):
                subprocess.Popen(["/bin/sh", "-c", "rm x"])
            assert len(emitted) == before + 4
        assert subprocess.Popen.__init__ is fake_init and original_init is not fake_init
    finally:
        for fd in (outside_r, outside_w, private_fd):
            os.close(fd)
        sock_a.close()
        sock_b.close()
    assert {f["what"] for _, f in emitted} == {"fs_descriptor", "fs_read", "exec"}


def test_a_pipe_fd_made_before_the_call_is_not_recognised_even_with_a_matching_number(tmp_path, monkeypatch):
    real_home, code, throwaway, policy = fs_world(tmp_path)
    read_end, write_end = os.pipe()
    try:
        monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, args, *a, **k: setattr(self, "_child_created", False))
        with rp.EffectGuard(lambda *a, **k: None, fs=policy, exec_grammar=SH_GRAMMAR):
            subprocess.Popen(["/bin/sh", "-c", "echo ok"])
            with pytest.raises(rp.EffectBlocked):
                open(read_end, "rb", closefd=False)
    finally:
        os.close(read_end)
        os.close(write_end)


def test_the_identity_lookup_is_allowed_with_exactly_one_argument_shape(monkeypatch):
    popen = []
    def fake_init(self, args, *a, **k):
        self._child_created = False
        popen.append(list(args))

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    emitted = []
    with rp.EffectGuard(lambda kind, **f: emitted.append((kind, f))):
        subprocess.Popen(["id", "-F"], stdout=subprocess.PIPE)
        for argv in (["id"], ["id", "-un"], ["id", "-F", "root"], ["id", "-G"], ["id", "-F", ";", "rm"],
                     ["/usr/bin/id", "-F"], ["whoami"], ["id -F"]):
            with pytest.raises(rp.EffectBlocked):
                subprocess.Popen(argv)
        with pytest.raises(rp.EffectBlocked):
            subprocess.Popen(["id", "-F"], shell=True)
        with pytest.raises(rp.EffectBlocked):
            subprocess.Popen(["id", "-F"], executable="/bin/sh")
    assert popen == [["id", "-F"]] and [f["what"] for _, f in emitted] == ["exec"] * 10
    assert rp.EXEC_GRAMMAR["id"] == (r"-F",) and "id" in rp.ALLOWED_EXEC
    assert rp.EffectGuard(lambda *a, **k: None).policy_sha256() != rp.EffectGuard(
        lambda *a, **k: None, exec_grammar={k: v for k, v in rp.EXEC_GRAMMAR.items() if k != "id"}).policy_sha256()


def test_the_popen_pipe_allowance_is_part_of_the_guard_policy_digest(tmp_path, monkeypatch):
    real_home, code, throwaway, policy = fs_world(tmp_path)
    sha = lambda **k: rp.EffectGuard(lambda *a, **kw: None, fs=policy, **k).policy_sha256()
    description = rp.EffectGuard(lambda *a, **kw: None, fs=policy).policy_description()
    assert description["fs"]["popen_pipe_descriptors"] == rp.POPEN_PIPE_RULE
    assert rp.EffectGuard(lambda *a, **kw: None).policy_description()["fs"] is None
    before = sha()
    monkeypatch.setattr(rp, "POPEN_PIPE_RULE", "any descriptor")
    assert sha() != before                                                  # widening the allowance changes the identity
    monkeypatch.undo()
    assert sha() == before
    assert sha(exec_grammar={**rp.EXEC_GRAMMAR, "/bin/sh": None}) != before


# ---- helpers for editing constructed raw evidence ----

def raw_rows(evd, name):
    return [json.loads(l) for l in (evd.out / "raw" / name).read_text().splitlines()]


def write_rows(evd, name, rows):
    (evd.out / "raw" / name).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def run_check(evd, ctx, **overrides):
    return rp.check_receipt(check_opts(evd, ctx, **overrides))


def details(result, code):
    return " | ".join(r["detail"] for r in result["refusals"] if r["code"] == code)


def renumber(records):
    for index, record in enumerate(records):
        record["seq"] = index
    return records


def edit_sample(evd, sid, **fields):
    """Edit one sample consistently in samples.jsonl and in its raw identity, then reseal the manifest."""
    rows, events = raw_rows(evd, "samples.jsonl"), raw_rows(evd, "events.jsonl")
    for row in rows:
        if row["id"] == sid:
            row.update(fields)
    for row in events:
        if row["id"] == sid:
            row["identity"].update(fields)
    write_rows(evd, "samples.jsonl", rows)
    write_rows(evd, "events.jsonl", events)
    seal(evd.out, evd.receipt_path)


# ---- F04: the checker authenticates an independently recorded run, not just internal consistency ----

def test_f04_without_the_recorded_receipt_digest_nothing_is_accepted(good, ctx):
    code, result = run_check(good, ctx, expect_receipt_sha256=None)
    assert code == rp.EXIT_REFUSED and "receipt_digest_not_bound" in refusal_codes(result)
    assert result["authorizes_release"] is False
    code, result = run_check(good, ctx, expect_receipt_sha256="0" * 64)
    assert code == rp.EXIT_REFUSED and "receipt_digest_mismatch" in refusal_codes(result)


def test_f04_a_complete_consistent_reseal_passes_consistency_but_not_the_recorded_digest(good, ctx, tmp_path):
    recorded = rp.sha256_file(good.receipt_path)                      # what the measurement owner wrote down
    forged = clone(good, tmp_path, "reseal")
    provenance = json.loads((forged.out / "raw" / "provenance.json").read_text())
    provenance["note"] = "an edit that changes bytes, not meaning"
    (forged.out / "raw" / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True))
    seal(forged.out, forged.receipt_path)                              # every digest and manifest made consistent
    assert run_check(forged, ctx)[0] == rp.EXIT_PASS                   # consistency alone proves nothing about origin
    code, result = run_check(forged, ctx, expect_receipt_sha256=recorded)
    assert code == rp.EXIT_REFUSED and "receipt_digest_mismatch" in refusal_codes(result)


def test_f04_constructed_evidence_is_refused_by_the_production_checker_even_when_everything_else_is_valid(good, ctx):
    code, result = run_check(good, ctx, accept_fixture_source=None)
    assert code == rp.EXIT_REFUSED and "measurement_source_not_release_capable" in refusal_codes(result)
    assert "ConstructedEvidenceDeps" in details(result, "measurement_source_not_release_capable")
    code, result = run_check(good, ctx, accept_fixture_source="SomeOtherClass")
    assert code == rp.EXIT_REFUSED and "measurement_source_not_release_capable" in refusal_codes(result)


def test_f04_a_fixture_verification_never_authorizes_a_release(good, ctx):
    code, result = run_check(good, ctx)
    assert code == rp.EXIT_PASS and result["verdict"] == "PASS"
    assert result["authorizes_release"] is False and result["fixture_source"] is True
    assert "accept_fixture_source" not in rp.check_receipt.__doc__.split("Authenticity")[0]      # documented as API-only
    parser = rp.build_parser()
    check_options = {a.dest for sub in parser._subparsers._group_actions[0].choices.values()
                     if sub.prog.endswith(" check") for a in sub._actions}
    assert "accept_fixture_source" not in check_options and "expect_receipt_sha256" in check_options


def test_f04_even_a_forged_live_label_is_stopped_by_the_independently_recorded_digest(good, ctx, tmp_path):
    recorded = rp.sha256_file(good.receipt_path)
    forged = clone(good, tmp_path, "livelabel")
    forged.receipt["measurement_source"] = {"deps_class": rp.LIVE_DEPS_CLASS, "release_capable": True}
    forged.receipt_path.write_text(json.dumps(forged.receipt, indent=2, sort_keys=True))
    # If the forger also controls the digest the checker is shown, nothing in the package can stop it: the
    # receipts are unsigned and authenticity rests on the digest the measurement owner recorded.
    assert run_check(forged, ctx, accept_fixture_source=None)[0] == rp.EXIT_PASS
    code, result = run_check(forged, ctx, accept_fixture_source=None, expect_receipt_sha256=recorded)
    assert code == rp.EXIT_REFUSED and "receipt_digest_mismatch" in refusal_codes(result)


# ---- F05: complete logs, exact windows, lifecycle-wide effects, shared-engine interference ----

def test_f05_windows_are_rebuilt_from_the_complete_log_not_stored_with_the_sample(good, ctx):
    for row in raw_rows(good, "events.jsonl")[:3]:
        assert "instrument" not in row and set(row["bounds"]) == {"start_ns", "end_ns"}
    logs = rp.load_raw(good.out / "raw")["logs"]
    samples = raw_rows(good, "samples.jsonl")
    events = {r["id"]: r for r in raw_rows(good, "events.jsonl")}
    checked = 0
    for sample in samples:
        window = rp.sample_instrument(logs, sample["side"], events[sample["id"]]["bounds"])["window"]
        assert sum(1 for e in window if e["kind"] == "engine_http" and e["path"].endswith("/chat/completions")) \
            == sample["diagnostics"]["chat_calls"]
        checked += 1
    assert checked == len(samples) == len(REQUIRED) * 2 * 22


def test_f05_a_dropped_event_with_the_log_renumbered_still_fails_because_the_window_is_rebuilt(good, ctx, tmp_path):
    evd = clone(good, tmp_path, "omit")
    path = "instrumentation.candidate.jsonl"
    records = raw_rows(evd, path)
    victim = next(i for i, r in enumerate(records) if r.get("kind") == "engine_http"
                  and r.get("path") == "/v1/chat/completions" and i > 40)
    del records[victim]
    write_rows(evd, path, renumber(records))                         # no gap is left behind
    seal(evd.out, evd.receipt_path)
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_REFUSED and "raw_derivation_mismatch" in refusal_codes(result)
    assert "instrumentation_log_invalid" not in refusal_codes(result)   # caught by regrading, not by the gap check


@pytest.mark.parametrize("name,mutate,needle", [
    ("header_not_first", lambda r: renumber([r[1], r[0], *r[2:]]), "header_not_first_and_unique"),
    ("two_headers", lambda r: renumber([*r, {**r[0]}]), "header_not_first_and_unique"),
    ("wrong_run", lambda r: [{**r[0], "run_id": "another-run"}, *r[1:]], "header_run_id_mismatch"),
    ("wrong_side", lambda r: [{**r[0], "side": "baseline"}, *r[1:]], "header_side_mismatch"),
    ("wrong_root", lambda r: [{**r[0], "root": "/elsewhere"}, *r[1:]], "header_root_mismatch"),
    ("guard_not_recorded", lambda r: [{**r[0], "effect_guard": False}, *r[1:]], "effect_guard_not_recorded"),
    ("second_process", lambda r: [*r[:6], {**r[6], "pid": 1}, *r[7:]], "records_from_more_than_one_process"),
    ("clock_goes_backwards", lambda r: [*r[:10], {**r[10], "t_ns": 5}, *r[11:]], "timestamps_invalid_or_not_monotonic"),
    ("no_surfaces", lambda r: renumber([r[0], *r[2:]]), "surfaces_record_missing_or_duplicated"),
    ("gap", lambda r: [*r[:7], *r[8:]], "sequence_has_gaps_or_reordering"),
    ("engine_not_instrumented", lambda r: [r[0], {**r[1], "available": [], "unavailable": list(rp.SURFACES)}, *r[2:]],
     "engine_http_not_instrumented"),
    ("empty", lambda r: [], "log_empty"),
])
def test_f05_log_headers_continuity_and_run_binding_are_verified(good, ctx, tmp_path, name, mutate, needle):
    evd = clone(good, tmp_path, name)
    path = "instrumentation.candidate.jsonl"
    write_rows(evd, path, mutate(raw_rows(evd, path)))
    seal(evd.out, evd.receipt_path)
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_REFUSED and needle in details(result, "instrumentation_log_invalid"), name


def all_samples_correct_and_clean(evd):
    rows = raw_rows(evd, "samples.jsonl")
    return all(r["grade"]["correct"] and r["diagnostics"]["blocked_effects"] == 0
               and r["diagnostics"]["engine_state_changes"] == 0 for r in rows if r["side"] == "candidate")


def test_f05_effects_outside_every_sample_window_still_block_the_candidate(tmp_path, ctx):
    startup = {"candidate": [{"kind": "effect_blocked", "what": "engine_operation",
                              "target": "POST /v1/models/Test-Model-oQ4e/unload"}]}
    evd = pass_evidence(tmp_path, ctx, "startup_effect", startup_events=startup)
    assert all_samples_correct_and_clean(evd)                         # not one measured window saw it
    assert evd.receipt["verdict"] == "BLOCK" and evd.code == rp.EXIT_BLOCK
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_BLOCK and any(r["code"] == "lifecycle_blocked_effect" for r in result["reasons"])
    assert result["authorizes_release"] is False
    assert evd.receipt["lifecycle"]["candidate"]["blocked_effects"] == 1
    assert evd.receipt["lifecycle"]["baseline"]["blocked_effects"] == 0


def test_f05_an_engine_state_change_during_startup_blocks_even_if_it_was_not_blocked(tmp_path, ctx):
    startup = {"candidate": [{"kind": "engine_http", "method": "POST", "path": "/v1/models/Other-Model/load", "status": 200}]}
    evd = pass_evidence(tmp_path, ctx, "startup_load", startup_events=startup)
    assert evd.receipt["verdict"] == "BLOCK"
    assert evd.receipt["lifecycle"]["candidate"]["engine_state_changes"] == 1
    assert run_check(evd, ctx)[0] == rp.EXIT_BLOCK


def test_f05_a_refused_baseline_effect_is_recorded_in_the_receipt_but_does_not_decide_the_verdict(tmp_path, ctx):
    startup = {"baseline": [{"kind": "effect_blocked", "what": "fs_write", "target": "/Users/x/Documents/new.txt"}]}
    evd = pass_evidence(tmp_path, ctx, "baseline_effect", startup_events=startup)
    assert evd.receipt["verdict"] == "PASS" and evd.code == rp.EXIT_PASS
    assert evd.receipt["lifecycle"]["baseline"]["blocked_effects"] == 1
    assert [r["code"] for r in evd.receipt["recorded"]] == ["baseline_blocked_effect"]
    assert "fs_write:/Users/x/Documents/new.txt" in evd.receipt["recorded"][0]["detail"]
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_PASS and result["verdict"] == "PASS" and result["refusals"] == []
    assert result["recorded"] == evd.receipt["recorded"]


def test_f05_a_baseline_engine_state_change_or_mutating_call_makes_the_comparison_inconclusive(tmp_path, ctx):
    for name, record in (("load", {"kind": "engine_http", "method": "POST", "path": "/v1/models/Other-Model/load", "status": 200}),
                         ("mutate", {"kind": "engine_http", "method": "PUT", "path": "/admin/api/settings", "status": 200})):
        evd = pass_evidence(tmp_path, ctx, "baseline_" + name, startup_events={"baseline": [record]})
        assert evd.receipt["verdict"] == "INCONCLUSIVE" and evd.code == rp.EXIT_INCONCLUSIVE, name
        code, result = run_check(evd, ctx)
        assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "baseline_lifecycle_effect" for r in result["reasons"]), name


def failing_baseline(scenario, reps, kind="empty"):
    """Behavior plan: the baseline's measured repetitions `reps` fail (the first two calls are warmups)."""
    return {("baseline", scenario): lambda n, reps=frozenset(r + 2 for r in reps), kind=kind: kind if n in reps else "ok"}


def test_a_run_with_thirty_samples_and_a_failing_baseline_passes_and_check_rederives_the_schedule(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "thirty", samples=30,
                        behaviors=failing_baseline("greeting_warm", range(1, 6)))          # 25 of 30 baseline samples correct
    receipt = evd.receipt
    assert receipt["protocol"]["measured"] == 30 and receipt["protocol"]["partial"] is False
    assert receipt["verdict"] == "PASS" and evd.code == rp.EXIT_PASS
    cell = receipt["summary"]["greeting_warm"]["baseline"]
    assert (cell["measured"], cell["correct"], cell["failed"]) == (30, 25, 5)
    assert cell["failure_reasons"] and all(count == 5 for count in cell["failure_reasons"].values())
    assert receipt["summary"]["greeting_warm"]["candidate"]["correct"] == 30
    assert receipt["comparison"]["greeting_warm"]["baseline_samples"]["failed"] == 5
    assert [r["code"] for r in receipt["recorded"]] == ["baseline_correctness_failure"]
    code, result = run_check(evd, ctx)
    assert (code, result["verdict"], result["refusals"]) == (rp.EXIT_PASS, "PASS", [])
    assert result["recorded"] == receipt["recorded"]
    # the checker rebuilds the schedule for the RECORDED sample count, so a receipt claiming another count is refused
    tampered = clone(evd, tmp_path, "thirty_tampered")
    doc = json.loads(Path(tampered.receipt_path).read_text())
    doc["protocol"]["measured"] = 20
    Path(tampered.receipt_path).write_text(json.dumps(doc, indent=2, sort_keys=True))
    assert "schedule_digest_mismatch" in refusal_codes(run_check(tampered, ctx)[1])


def test_a_baseline_with_too_few_correct_samples_is_inconclusive_end_to_end_and_in_the_checker(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "thin", samples=30, behaviors=failing_baseline("greeting_warm", range(1, 12)))
    assert evd.receipt["verdict"] == "INCONCLUSIVE" and evd.code == rp.EXIT_INCONCLUSIVE
    assert [r["code"] for r in evd.receipt["reasons"]] == ["baseline_insufficient_correct_samples"]
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_INCONCLUSIVE and result["authorizes_release"] is False
    assert result["reasons"][0]["code"] == "baseline_insufficient_correct_samples" and "n=19" in result["reasons"][0]["detail"]


def test_a_refused_baseline_is_recorded_and_a_candidate_failure_still_blocks_end_to_end(tmp_path, ctx):
    refused = pass_evidence(tmp_path, ctx, "refused_base", samples=30,
                            behaviors=failing_baseline("health_status_overhead", (2, 7), kind="refusal"))
    assert refused.receipt["verdict"] == "PASS"
    assert refused.receipt["summary"]["health_status_overhead"]["baseline"]["refused"] == 2
    assert run_check(refused, ctx)[0] == rp.EXIT_PASS
    behaviors = {**failing_baseline("greeting_warm", (1, 2, 3)),
                 ("candidate", "bounded_reasoning"): lambda n: "refusal" if n == 5 else "ok"}
    broken = pass_evidence(tmp_path, ctx, "cand_fail", samples=30, behaviors=behaviors)
    assert broken.receipt["verdict"] == "BLOCK" and broken.code == rp.EXIT_BLOCK
    assert {"candidate_correctness_failure", "new_refusal"} <= {r["code"] for r in broken.receipt["reasons"]}
    code, result = run_check(broken, ctx)
    assert code == rp.EXIT_BLOCK and result["authorizes_release"] is False


def test_a_slower_candidate_blocks_end_to_end_against_a_failing_baseline(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "slow_cand", samples=30, cand_mult=3.0, cand_add_ms=400.0,
                        behaviors=failing_baseline("greeting_warm", range(1, 4)))
    assert evd.receipt["verdict"] == "BLOCK"
    assert any(r["code"] == "measured_material_regression" for r in evd.receipt["reasons"])


def test_a_receipt_recorded_list_that_differs_from_the_evidence_is_refused(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "recorded_forged", samples=30, behaviors=failing_baseline("greeting_warm", (1, 2)))
    doc = json.loads(Path(evd.receipt_path).read_text())
    doc["recorded"] = []
    Path(evd.receipt_path).write_text(json.dumps(doc, indent=2, sort_keys=True))
    assert "receipt_recorded_mismatch" in refusal_codes(run_check(evd, ctx)[1])


def test_check_refuses_a_receipt_made_under_a_different_policy_hash(good, ctx, tmp_path):
    other = copy.deepcopy(json.loads(Path(rp.DEFAULT_BUNDLE).read_text()))
    other["policy"]["max_receipt_age_s"] += 1                                       # any policy value: a new policy hash
    path = tmp_path / "other_policy_bundle.json"
    path.write_text(json.dumps(other))
    changed = rp.load_bundle(path)
    assert changed["policy_sha256"] != BUNDLE["policy_sha256"] and changed["corpus_sha256"] == BUNDLE["corpus_sha256"]
    code, result = rp.check_receipt(check_opts(good, ctx, bundle=path, expect_policy_sha256=changed["policy_sha256"]))
    assert code == rp.EXIT_REFUSED and "policy_hash_mismatch" in refusal_codes(result)
    code, result = rp.check_receipt(check_opts(good, ctx, bundle=path))              # the receipt's own (v3) hash expected
    assert code == rp.EXIT_REFUSED and "policy_hash_mismatch" in refusal_codes(result)
    assert good.receipt["policy"] == {"version": "release_policy_v3", "sha256": BUNDLE["policy_sha256"]}


def test_the_baseline_approval_is_bound_to_the_policy_hash_so_a_v2_approval_cannot_carry_over(tmp_path, ctx):
    first = run_evidence(tmp_path, ctx, "approval_first")
    stale_key = {**rp.cohort_key(first.receipt), "policy_sha256": "729b14ccdef74d3885177a8fe3fdd24fdeb89d670947a5c65f8f68cbbc25eb07"}
    approval = write_approval(tmp_path, first.receipt, cohort_key=stale_key)
    second = run_evidence(tmp_path, ctx, "approval_second", approved=approval)
    assert second.receipt["verdict"] == "INCONCLUSIVE"
    assert any(r["code"] == "missing_or_unapproved_baseline" and "new cohort" in r["detail"] for r in second.receipt["reasons"])


def test_f05_the_idle_backends_engine_traffic_during_a_sample_contaminates_it(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "interference", interference=[("candidate", "greeting_warm")])
    hit = [r for r in raw_rows(evd, "samples.jsonl") if r["side"] == "candidate" and r["scenario"] == "greeting_warm"]
    assert all(r["diagnostics"]["other_side_engine_requests"] == 1 for r in hit)
    assert all("shared_engine_interference" in r["grade"]["unverifiable"] and not r["grade"]["correct"] for r in hit)
    assert evd.receipt["verdict"] == "INCONCLUSIVE"
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "unverifiable_expectation" for r in result["reasons"])
    clean = raw_rows(evd, "samples.jsonl")
    assert all(r["diagnostics"]["other_side_engine_requests"] == 0 for r in clean if r["scenario"] != "greeting_warm")


def test_f05_an_unavailable_idle_side_log_is_unknown_and_never_clean(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "blind_idle", unavailable=("engine_http",))
    assert all(r["diagnostics"]["other_side_engine_requests"] == UNKNOWN for r in raw_rows(evd, "samples.jsonl"))
    assert evd.receipt["verdict"] == "INCONCLUSIVE"


# ---- F06: raw samples are bound to the schedule, the prompts and sane clocks ----

def prompt_digest(scenario, variant):
    return rp.sha256_bytes(SPECS[scenario]["variants"][variant].encode())


def test_f06_both_members_of_a_pair_claiming_the_wrong_variant_and_prompt_are_caught(good, ctx, tmp_path):
    evd = clone(good, tmp_path, "variant")
    for side in rp.SIDES:                                              # rep 2 is scheduled to use variant 1
        edit_sample(evd, f"{side}:greeting_warm:measured:2", variant=0, prompt_sha256=prompt_digest("greeting_warm", 0))
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_REFUSED
    text = details(result, "samples_not_bound_to_the_schedule")
    assert "variant_is_not_the_scheduled_value" in text and "prompt_sha256_is_not_the_scheduled_value" in text
    assert "incomparable_or_incomplete_samples" not in refusal_codes(result)   # the pair check alone would have passed


def test_f06_a_prompt_digest_that_is_not_the_scheduled_prompt_is_caught(good, ctx, tmp_path):
    evd = clone(good, tmp_path, "prompt")
    edit_sample(evd, "candidate:bounded_reasoning:measured:3", prompt_sha256=rp.sha256_bytes(b"a different prompt"))
    assert "prompt_sha256_is_not_the_scheduled_value" in details(run_check(evd, ctx)[1], "samples_not_bound_to_the_schedule")


def test_f06_duplicate_raw_event_records_are_reported_not_merged(good, ctx, tmp_path):
    evd = clone(good, tmp_path, "dupe")
    rows = raw_rows(evd, "events.jsonl")
    write_rows(evd, "events.jsonl", [*rows, dict(rows[7])])
    seal(evd.out, evd.receipt_path)
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_REFUSED and "duplicate_event_records" in details(result, "samples_not_bound_to_the_schedule")


def test_f06_overlapping_and_reordered_samples_are_refused(good, ctx, tmp_path):
    samples = raw_rows(good, "samples.jsonl")
    overlap = clone(good, tmp_path, "overlap")
    edit_sample(overlap, samples[30]["id"], started_ns=samples[29]["ended_ns"] - 1)
    assert "overlaps_or_precedes_the_previous_sample" in details(run_check(overlap, ctx)[1], "samples_not_bound_to_the_schedule")
    reordered = clone(good, tmp_path, "reorder")
    for name in ("samples.jsonl", "events.jsonl", "started.jsonl"):   # consistently, so only the schedule can object
        rows = raw_rows(reordered, name)
        rows[10], rows[11] = rows[11], rows[10]
        write_rows(reordered, name, rows)
    seal(reordered.out, reordered.receipt_path)
    code, result = run_check(reordered, ctx)
    assert code == rp.EXIT_REFUSED and "id_is_not_the_scheduled_value" in details(result, "samples_not_bound_to_the_schedule")


def test_f06_invalid_clocks_are_refused(good, ctx, tmp_path):
    samples = raw_rows(good, "samples.jsonl")
    negative = clone(good, tmp_path, "negative")
    edit_sample(negative, samples[20]["id"], started_ns=samples[20]["ended_ns"] + 5)
    assert "sample_clock_invalid" in details(run_check(negative, ctx)[1], "samples_not_bound_to_the_schedule")
    nonfinite = clone(good, tmp_path, "nan")
    edit_sample(nonfinite, samples[21]["id"], ended_ns=float("nan"))
    assert "sample_clock_invalid" in details(run_check(nonfinite, ctx)[1], "samples_not_bound_to_the_schedule")
    boolean = clone(good, tmp_path, "bool")
    edit_sample(boolean, samples[22]["id"], started_ns=True)
    assert "sample_clock_invalid" in details(run_check(boolean, ctx)[1], "samples_not_bound_to_the_schedule")


def test_f06_event_times_outside_their_sample_or_out_of_order_are_refused(good, ctx, tmp_path):
    def edited(name, mutate):
        evd = clone(good, tmp_path, name)
        rows = raw_rows(evd, "events.jsonl")
        mutate(next(r for r in rows if r["id"] == "candidate:greeting_warm:measured:5"))
        write_rows(evd, "events.jsonl", rows)
        seal(evd.out, evd.receipt_path)
        return details(run_check(evd, ctx)[1], "samples_not_bound_to_the_schedule")
    assert "event_time_outside_the_sample_interval" in edited("late", lambda r: r["events"][-1].update(t_ns=10 ** 15))
    assert "event_time_outside_the_sample_interval" in edited("negative", lambda r: r["events"][0].update(t_ns=-5))
    assert "event_time_outside_the_sample_interval" in edited("nan", lambda r: r["events"][1].update(t_ns=float("nan")))
    assert "events_not_in_time_order" in edited("order", lambda r: r["events"].reverse())
    assert "driver_interval_outside_the_sample" in edited("window", lambda r: r["driver"].update(abs_end_ns=r["identity"]["ended_ns"] + 1))


def test_f06_a_sample_that_ran_past_its_deadline_without_saying_so_is_refused(good, ctx, tmp_path):
    evd = clone(good, tmp_path, "deadline")
    sample = next(r for r in raw_rows(evd, "samples.jsonl") if r["id"] == "candidate:greeting_warm:measured:5")
    stretch = int(200 * 1e9)
    rows = raw_rows(evd, "events.jsonl")
    row = next(r for r in rows if r["id"] == sample["id"])
    row["driver"]["abs_end_ns"] += stretch
    row["bounds"]["end_ns"] += stretch
    write_rows(evd, "events.jsonl", rows)
    edit_sample(evd, sample["id"], ended_ns=sample["ended_ns"] + stretch)
    assert "ran_past_its_deadline_without_recording_it" in details(run_check(evd, ctx)[1], "samples_not_bound_to_the_schedule")


# ---- F09: an interrupted sample is kept, graded as cancelled, and never counted as a success ----

class RealDriverDeps(ConstructedEvidenceDeps):
    """Constructed backends, but the REAL HTTP driver against an owned loopback server."""

    def __init__(self, ctx, url, **kwargs):
        super().__init__(ctx, **kwargs)
        self.url = url

    def make_driver(self, backend):
        return rp.HttpTurnDriver(self.url)


def test_f09_cancelling_mid_stream_keeps_the_started_sample_and_the_events_already_received(tmp_path, ctx):
    script = [(0, {"type": "session", "id": "s"}), (0, {"type": "routed", "direct_calls": [], "needs_tools": False}),
              (0, {"type": "delta", "text": "Hel"}), (8.0, {"type": "done"})]
    out = tmp_path / "real_cancel"

    async def main(url):
        deps = RealDriverDeps(ctx, url)
        opts = {"candidate_root": ctx.cand_wt, "candidate_sha": ctx.cand_sha, "baseline_root": ctx.base_wt,
                "baseline_sha": ctx.base_sha, "lane": "desktop", "model_id": MODEL_ID, "output_dir": out,
                "scenario_ids": None, "samples": None, "diagnostic": False,
                "approved_baseline_path": None, "approved_baseline_sha256": None}
        task = asyncio.create_task(rp.run_release(opts, deps, rp.load_bundle()))
        await asyncio.sleep(1.0)                                            # let the real stream deliver three events
        task.cancel()
        return deps, await task
    with LoopbackBackend(script) as backend:
        deps, (code, receipt_path) = asyncio.run(main(backend.url))
    raw = out / "raw"
    receipt = json.loads(receipt_path.read_text())
    rows = [json.loads(l) for l in (raw / "samples.jsonl").read_text().splitlines()]
    events = [json.loads(l) for l in (raw / "events.jsonl").read_text().splitlines()]
    started = [json.loads(l) for l in (raw / "started.jsonl").read_text().splitlines()]
    assert receipt["aborted"] is True and code == rp.EXIT_INCONCLUSIVE
    assert [r["id"] for r in started] == [r["id"] for r in rows] == [r["id"] for r in events] == ["candidate:greeting_warm:warmup:1"]
    assert [e["event"]["type"] for e in events[0]["events"]] == ["session", "routed", "delta"]   # the partial stream
    assert events[0]["driver"]["cancelled"] is True and events[0]["driver"]["abs_end_ns"]
    assert rows[0]["outcome"] == "cancelled" and rows[0]["grade"]["correct"] is False
    assert rows[0]["metrics_ns"]["total_completion_s"] == UNKNOWN              # a partial answer has no completion time
    assert all(b.stopped for b in deps.spawned) and all(t["ok"] for t in receipt["teardown"].values())


def test_f09_an_interrupted_sample_never_contributes_a_latency_or_a_success(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "never_ok", cancel_at=60, cancel_partial=True)
    rows = raw_rows(evd, "samples.jsonl")
    assert rows[-1]["outcome"] == "cancelled" and not rows[-1]["grade"]["correct"]
    summary = rp.summarize(rows, rp.load_bundle())
    cancelled = [r for r in rows if r["outcome"] == "cancelled"]
    assert cancelled and all(not r["grade"]["correct"] for r in cancelled)
    for scenario, per_side in summary.items():
        for side, cell in per_side.items():
            mine = [r for r in rows if r["scenario"] == scenario and r["side"] == side and r["phase"] == "measured"]
            assert cell["correct"] == sum(1 for r in mine if r["grade"]["correct"])      # cancelled ones never counted
            assert cell["measured"] == len(mine)


# ---- F10: every owned backend gets a teardown attempt; the evidence is sealed whatever happens ----

class FakeStoppable:
    def __init__(self, name, failure, calls):
        self.name, self.failure, self.calls, self.pid = name, failure, calls, 1

    def stop(self):
        self.calls.append(self.name)
        if self.failure == "raise":
            raise RuntimeError("terminate failed")
        if self.failure == "interrupt":
            raise KeyboardInterrupt
        return {"side": self.name, "ok": True, "errors": [], "exited": True}


def test_f10_a_failure_tearing_down_one_backend_never_skips_the_other():
    for first, second in (("raise", None), (None, "raise"), ("raise", "raise")):
        calls = []
        outcome, pending = rp.stop_all({"candidate": FakeStoppable("candidate", first, calls),
                                        "baseline": FakeStoppable("baseline", second, calls)})
        assert calls == ["candidate", "baseline"] and pending is None
        assert outcome["candidate"]["ok"] is (first is None) and outcome["baseline"]["ok"] is (second is None)
        assert all("stop:RuntimeError" in outcome[s]["errors"] for s, f in (("candidate", first), ("baseline", second)) if f)
    calls = []
    outcome, pending = rp.stop_all({"candidate": FakeStoppable("candidate", "interrupt", calls),
                                    "baseline": FakeStoppable("baseline", None, calls)})
    assert calls == ["candidate", "baseline"] and isinstance(pending, KeyboardInterrupt)   # re-raised only after both
    assert outcome["baseline"]["ok"] is True and outcome["candidate"]["ok"] is False


@pytest.mark.parametrize("plan", [{"candidate": "raise"}, {"baseline": "raise"}, {"candidate": "unresolved"},
                                  {"baseline": "unresolved"}, {"candidate": "raise", "baseline": "unresolved"}])
def test_f10_a_teardown_failure_is_sealed_into_a_non_passing_receipt(tmp_path, ctx, plan):
    evd = pass_evidence(tmp_path, ctx, "teardown", teardown=plan)
    assert all(b.stopped for b in evd.deps.spawned)                                # every child was attempted
    assert {s for s, row in evd.receipt["teardown"].items() if not row["ok"]} == set(plan)
    assert evd.receipt["verdict"] == "INCONCLUSIVE" and (evd.out / "receipt.json").is_file()
    assert any(r["code"] == "teardown_unresolved" for r in evd.receipt["reasons"])
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_REFUSED and "teardown_unresolved" in refusal_codes(result)


def test_f10_a_clean_teardown_is_recorded_for_both_backends(good):
    assert set(good.receipt["teardown"]) == set(rp.SIDES) and all(r["ok"] for r in good.receipt["teardown"].values())
    provenance = json.loads((good.out / "raw" / "provenance.json").read_text())
    assert provenance["teardown"] == good.receipt["teardown"]


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ScriptedBackend(rp.BackendProcess):
    script = ""

    def command(self):
        return [sys.executable, "-c", self.script]


def scripted_backend(tmp_path, script):
    root = tmp_path / "wt"
    root.mkdir(exist_ok=True)
    backend = ScriptedBackend("candidate", root, 18775, tmp_path / "home", tmp_path / "i.jsonl", sys.executable)
    backend.script = script
    return backend


def test_f10_stop_ends_the_child_and_the_process_group_it_leads_and_reports_the_outcome(tmp_path):
    marker = tmp_path / "grandchild.pid"
    backend = scripted_backend(tmp_path, (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"))
    backend.start()
    try:
        assert wait_for(marker.exists) and wait_for(lambda: marker.read_text() != "")
        grandchild = int(marker.read_text())
        assert alive(grandchild) and os.getpgid(backend.proc.pid) == backend.proc.pid      # it leads its own group
        result = backend.stop()
        assert result["ok"] is True and result["exited"] is True and result["terminated"] is True
        assert result["killed"] is False and result["errors"] == []
        assert wait_for(lambda: not alive(grandchild))                                   # descendants went with it
    finally:
        if backend.proc and backend.proc.poll() is None:
            backend.proc.kill()


def test_f10_a_child_that_ignores_the_polite_request_is_killed_within_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "TERMINATE_WAIT_S", 0.5)
    ready = tmp_path / "ready"
    backend = scripted_backend(tmp_path, (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').write('1')\n"
        "time.sleep(120)\n"))
    backend.start()
    try:
        assert wait_for(ready.exists)
        result = backend.stop()
        assert result["killed"] is True and result["exited"] is True and result["ok"] is True
    finally:
        if backend.proc and backend.proc.poll() is None:
            backend.proc.kill()


def test_f10_signalling_and_waiting_failures_are_reported_not_raised(tmp_path):
    class Stuck:
        pid = 2 ** 22 + 12345                                                              # no such process or group

        def send_signal(self, signal_number):
            raise PermissionError("nope")

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)

        def poll(self):
            return None

    backend = rp.BackendProcess("baseline", tmp_path, 18776, tmp_path / "h", tmp_path / "i", "py")
    backend.proc = Stuck()
    result = backend.stop()
    assert result["ok"] is False and result["exited"] is False
    assert {"terminate:PermissionError", "kill:PermissionError", "wait:TimeoutExpired"} <= set(result["errors"])
    backend.proc = None
    assert backend.stop()["ok"] is True                                                    # nothing to stop is clean


# ---- F11: the effective runtime, model, residency and request settings define the cohort ----

def test_f11_the_cohort_includes_the_childs_own_runtime_and_containment_identity(good):
    base = rp.cohort_key(good.receipt)
    assert base["child_runtime"] == {k: RUNTIME[k] for k in ("python_version", "implementation", "executable_sha256", "platform")}
    assert base["containment"] == {"guard_policy_sha256": GUARD_SHA}
    for path, value in ((("runtime_identity",), {**base["child_runtime"], "python_version": "3.13.0"}),
                        (("containment",), {"guard_policy_sha256": "1" * 64})):
        mutated = copy.deepcopy(good.receipt)
        mutated[path[0]] = value
        assert rp.cohort_key(mutated) != base, path


def test_f11_candidate_and_baseline_children_must_run_the_same_interpreter(tmp_path, ctx):
    other = {**RUNTIME, "executable_sha256": "b" * 64}
    evd = pass_evidence(tmp_path, ctx, "runtime", runtime={"baseline": other})
    assert evd.receipt["verdict"] == "INCONCLUSIVE"
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "child_runtime_differs_between_sides" for r in result["reasons"])


def test_f11_an_unreported_or_incomplete_runtime_is_unverified_not_assumed(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "noruntime", runtime={"candidate": {"python_version": "3.14.3"}})
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "child_runtime_unverified" for r in result["reasons"])


def test_f11_different_containment_between_the_sides_is_not_comparable(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "contain", guard_sha={"baseline": "8" * 64})
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "containment_differs_between_sides" for r in result["reasons"])


def test_f11_the_engine_model_and_settings_must_be_unchanged_across_the_run(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "drift", env_drift=True)
    assert evd.deps.env_calls == 2                                                # collected before AND after the run
    raw = json.loads((evd.out / "raw" / "environment.json").read_text())
    assert raw["before"]["engine"]["app"]["build"] == "6" and raw["after"]["engine"]["app"]["build"] == "7"
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "environment_changed_during_run" for r in result["reasons"])


@pytest.mark.parametrize("moment", ["before", "after"])
def test_f11_exactly_the_named_model_must_be_resident_on_both_backends_at_both_ends(tmp_path, ctx, moment):
    plan = {("baseline", moment): [MODEL_ID, "Another-Model"]}
    if moment == "before":
        evd = run_evidence(tmp_path, ctx, "res_before", residency=plan)           # never starts measuring
        assert evd.code == rp.EXIT_INCONCLUSIVE and not raw_rows(evd, "samples.jsonl")
        assert evd.receipt["prerequisites_missing"][0]["id"] == "resident_model_mismatch"
        assert all(b.stopped for b in evd.deps.spawned)
    else:
        evd = pass_evidence(tmp_path, ctx, "res_after", residency=plan)
        code, result = run_check(evd, ctx)
        assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "residency_not_exclusive" for r in result["reasons"])
    unreadable = pass_evidence(tmp_path, ctx, "res_unknown", residency={("candidate", "after"): None})
    assert any(r["code"] == "residency_unverified" for r in run_check(unreadable, ctx)[1]["reasons"])


def test_f11_declared_cohort_is_validated_against_the_settings_that_actually_reached_the_engine(tmp_path, ctx):
    differ = pass_evidence(tmp_path, ctx, "settings_differ",
                           request_settings={"candidate": {**REQUEST_SETTINGS, "temperature": 0.1}})
    assert any(r["code"] == "request_settings_differ" for r in run_check(differ, ctx)[1]["reasons"])
    wrong_model = pass_evidence(tmp_path, ctx, "wrong_model",
                                request_settings={"candidate": {**REQUEST_SETTINGS, "model": "Some-Other-Model"},
                                                  "baseline": {**REQUEST_SETTINGS, "model": "Some-Other-Model"}})
    reasons = run_check(wrong_model, ctx)[1]["reasons"]
    assert any(r["code"] == "model_mismatch" for r in reasons)                    # identical on both sides, still wrong
    unknown = pass_evidence(tmp_path, ctx, "settings_unknown", request_settings={"candidate": rp.UNKNOWN})
    assert any(r["code"] == "request_settings_unverified" for r in run_check(unknown, ctx)[1]["reasons"])
    same = pass_evidence(tmp_path, ctx, "settings_same")
    code, result = run_check(same, ctx)
    assert code == rp.EXIT_PASS and not any(r["code"] in ("request_settings_differ", "model_mismatch") for r in result["reasons"])


def model_environment(tmp_path, name, weights: bytes, *, hash_weights=True):
    home = tmp_path / name / "home"
    (home / ".omlx").mkdir(parents=True)
    model_dir = tmp_path / name / "models" / "Vendor" / "Test-Model"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text(json.dumps({"quantization": {"bits": 4}}))
    (model_dir / "w.safetensors").write_bytes(weights)
    (home / ".omlx" / "settings.json").write_text(json.dumps({"model": {"model_dirs": [str(tmp_path / name / "models")]}}))
    stub = lambda argv, timeout=10: (0, "value\n", "")
    return rp.collect_environment("Test-Model", "desktop", 2, runner=stub, home=home,
                                  plist=tmp_path / "none.plist", hash_weights=hash_weights)


def test_f11_a_same_named_same_sized_model_artifact_with_different_contents_is_a_different_cohort(tmp_path):
    first = model_environment(tmp_path, "a", b"1234")
    second = model_environment(tmp_path, "b", b"5678")                                # same name, same size, other bytes
    assert first["model"]["weights_manifest"]["files"][0][1] == second["model"]["weights_manifest"]["files"][0][1] == 4
    assert first["model"]["weights_manifest"]["sha256"] != second["model"]["weights_manifest"]["sha256"]
    assert rp.environment_fingerprint(first) != rp.environment_fingerprint(second)
    assert rp.environment_fingerprint(first) == rp.environment_fingerprint(model_environment(tmp_path, "c", b"1234"))
    unhashed = model_environment(tmp_path, "d", b"1234", hash_weights=False)
    assert rp.has_unknown(rp.environment_fingerprint(unhashed))                       # never silently trusted


def test_f11_environment_fingerprint_ignores_only_what_cannot_change_a_latency():
    env = {key: {"k": key} for key in rp.ENV_COHORT_FIELDS}
    assert rp.environment_fingerprint({**env, "power": "Battery", "python": {"x": 1}}) == rp.environment_fingerprint(env)
    for key in rp.ENV_COHORT_FIELDS:
        assert rp.environment_fingerprint({**env, key: {"k": "changed"}}) != rp.environment_fingerprint(env), key
    assert rp.has_unknown(rp.environment_fingerprint({}))


# ---- N01 and N02 ----

def test_n01_the_inert_environment_refuses_loopback_ports_the_test_does_not_own(inert_environment):
    with pytest.raises(AssertionError, match="does not own"):
        socket.socket().connect(("127.0.0.1", 9))
    inert_environment.clear()                                                       # that refusal is the expected outcome here
    with FaultingServer() as owned:
        probe = socket.socket()
        try:
            probe.connect(("127.0.0.1", owned.port))                                  # a port the test itself bound
        finally:
            probe.close()
        assert wait_for(lambda: owned.accepted >= 1)


def test_n02_the_measurement_boundary_is_documented_and_instrumentation_volume_is_reported(good):
    doc = (ROOT / "docs" / "RELEASE_PERFORMANCE_BENCHMARK.md").read_text()
    section = doc.split("## Measurement boundary", 1)[1].split("\n## ", 1)[0]
    for phrase in ("instrumented", "not uninstrumented application latency", "response tee", "identically instrumented",
                   "diagnostics", "never gate", "does not characterize", "attribution is never disabled"):
        assert phrase in section, phrase
    rows = raw_rows(good, "samples.jsonl")
    assert all(isinstance(r["diagnostics"]["instrumentation_records"], int) for r in rows)
    assert good.receipt["summary"]["greeting_warm"]["candidate"]["diagnostics"]["instrumentation_records"]["p50"] >= 1
    assert "instrumentation_records" not in rp.GATING_METRICS                          # volume is a diagnostic, never a gate


@pytest.mark.parametrize("edit,needle", [
    (lambda b: b["corpus"]["sentinels"].pop("acceptable_finish_reasons"), "acceptable_finish_reasons"),
    (lambda b: b["policy"]["outcomes"].__setitem__("lifecycle_blocked_effect", "INCONCLUSIVE"), "lifecycle_blocked_effect"),
    (lambda b: b["policy"]["outcomes"].__setitem__("integrity_failure", "BLOCK"), "integrity_failure"),
    (lambda b: b["policy"]["outcomes"].__setitem__("baseline_lifecycle_effect", "BLOCK"), "baseline_lifecycle_effect"),
    (lambda b: b["policy"]["environment"].__setitem__("weights_identity", "names_and_sizes"), "full weight digests"),
    (lambda b: b["policy"]["environment"].__setitem__("compare_before_and_after", False), "full weight digests"),
    (lambda b: b["policy"].pop("environment"), "full weight digests")])
def test_the_new_policy_requirements_cannot_be_weakened(edit, needle):
    bundle = copy.deepcopy(BUNDLE)
    edit(bundle)
    assert any(needle in problem for problem in rp.validate_bundle(bundle))


def test_f02_denied_engine_requests_never_reach_an_owned_receiver_and_permitted_ones_do():
    import httpx
    import http.client
    emitted = []
    with LoopbackBackend(health=({"status": "ok"}, 200)) as receiver:
        port = receiver.server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        guard = rp.EffectGuard(lambda kind, **fields: emitted.append((kind, fields)), allowed_ports={port})
        with guard:
            assert httpx.Client().get(base + "/health").json() == {"status": "ok"}        # a permitted read arrives
            assert httpx.Client(timeout=3).post(base + "/v1/chat/completions", json={"model": "m"}).status_code == 200
            sent_before = list(receiver.agent_bodies)
            for method, path in (("POST", "/v1/models/Some-Model/unload"), ("POST", "/v1/models/Some-Model/load"),
                                 ("PUT", "/admin/api/settings"), ("DELETE", "/v1/models/Some-Model")):
                with pytest.raises(rp.EffectBlocked):
                    httpx.Client().request(method, base + path, json={"x": 1}, headers={"Authorization": "Bearer SYNTHETIC"})
                with pytest.raises(rp.EffectBlocked):
                    asyncio.run(httpx.AsyncClient().request(method, base + path, json={"x": 1}))
                with pytest.raises(rp.EffectBlocked):
                    http.client.HTTPConnection("127.0.0.1", port).request(method, path, body=b"{}")
        assert receiver.agent_bodies == sent_before == [{"model": "m"}]                  # only the permitted chat call
    assert [f["what"] for k, f in emitted] == ["engine_operation"] * 12
    # and the refusals are what makes the whole run non-passing
    logs = {"candidate": [{"kind": "header"}, *[{"kind": k, **f} for k, f in emitted]], "baseline": []}
    life = rp.lifecycle_summary(logs)
    assert life["candidate"]["blocked_effects"] == 12 and life["baseline"]["blocked_effects"] == 0
    summary = rp.summarize(world(1000.0, 1000.0), BUNDLE)
    assert rp.decide(summary, BUNDLE, APPROVED, {"candidate": {**life["candidate"]}, "baseline": life["baseline"]})["verdict"] == "BLOCK"
    clean = rp.lifecycle_summary({"candidate": [{"kind": "header"}], "baseline": []})
    assert rp.decide(summary, BUNDLE, APPROVED, clean)["verdict"] == "PASS"
    baseline_side = rp.decide(summary, BUNDLE, APPROVED, {"candidate": clean["candidate"], "baseline": life["candidate"]})
    assert baseline_side["verdict"] == "PASS"                                       # twelve REFUSED effects: recorded, not a verdict
    assert [r["code"] for r in baseline_side["recorded"]] == ["baseline_blocked_effect"]


def test_f10_a_child_that_does_not_lead_its_own_group_is_signalled_alone_never_its_group(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])        # shares the test's group
    try:
        assert os.getpgid(child.pid) != child.pid
        backend = rp.BackendProcess("candidate", tmp_path, 18775, tmp_path / "h", tmp_path / "i", sys.executable)
        backend.proc = child
        result = backend.stop()
        assert result["ok"] is True and result["exited"] is True and child.poll() is not None
        assert os.getpgrp() == os.getpgid(0)                                                # we are plainly still here
    finally:
        if child.poll() is None:
            child.kill()


# ---- gaps found by mutation testing: each test below kills a mutant that the first suite let survive ----

def test_f05_an_idle_side_whose_engine_log_is_unavailable_makes_the_active_samples_unverifiable(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "idle_blind", unavailable_by_side={"baseline": ["engine_http"]})
    mine = [r for r in raw_rows(evd, "samples.jsonl") if r["side"] == "candidate" and r["scenario"] == "greeting_warm"]
    assert all(r["diagnostics"]["chat_calls"] >= 1 and r["diagnostics"]["other_side_engine_requests"] == UNKNOWN for r in mine)
    assert all("other_side_engine_activity" in r["grade"]["unverifiable"] and not r["grade"]["correct"] for r in mine)
    assert not any("shared_engine_interference" in r["grade"]["unverifiable"] for r in mine)


def test_f05_a_receipts_lifecycle_record_that_hides_an_effect_is_refused(tmp_path, ctx):
    startup = {"candidate": [{"kind": "effect_blocked", "what": "engine_operation", "target": "POST /v1/models/x/unload"}]}
    evd = pass_evidence(tmp_path, ctx, "hide_lifecycle", startup_events=startup)
    assert run_check(evd, ctx)[0] == rp.EXIT_BLOCK
    evd.receipt["lifecycle"]["candidate"]["blocked_effects"] = 0
    evd.receipt["lifecycle"]["candidate"]["blocked_targets"] = []
    evd.receipt_path.write_text(json.dumps(evd.receipt, indent=2, sort_keys=True))
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_REFUSED and "lifecycle_mismatch" in refusal_codes(result)


def test_f06_event_and_started_records_must_follow_the_sample_order_each_on_its_own(good, ctx, tmp_path):
    for name, code in (("events.jsonl", "event_records_do_not_match_samples_in_order"),
                       ("started.jsonl", "started_records_do_not_match_samples_in_order")):
        evd = clone(good, tmp_path, "order_" + name.split(".")[0])
        rows = raw_rows(evd, name)
        rows[10], rows[11] = rows[11], rows[10]                    # only this file is out of order
        write_rows(evd, name, rows)
        seal(evd.out, evd.receipt_path)
        result = run_check(evd, ctx)[1]
        assert code in details(result, "samples_not_bound_to_the_schedule"), name


def test_f11_an_environment_that_could_not_be_recollected_after_the_run_is_not_comparable(tmp_path, ctx):
    evd = pass_evidence(tmp_path, ctx, "env_gone", env_after_missing=True)
    assert raw_rows_json(evd, "environment.json")["after"] == {}
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_INCONCLUSIVE and any(r["code"] == "environment_after_unavailable" for r in result["reasons"])


def raw_rows_json(evd, name):
    return json.loads((evd.out / "raw" / name).read_text())

# Takeover repairs: owned receivers/state only; no live measurement.
@pytest.mark.parametrize("method", ["send", "sendall", "sendmsg"])
def test_raw_socket_engine_mutation_is_refused_before_any_bytes(method):
    if not hasattr(socket.socket, method):
        pytest.skip("socket method unavailable")
    emitted = []
    with LoopbackBackend() as receiver:
        port = receiver.server.server_address[1]
        with rp.EffectGuard(lambda kind, **fields: emitted.append((kind, fields)), allowed_ports={port}):
            with socket.socket() as connection:
                connection.connect(("127.0.0.1", port))
                payload = b"POST /v1/models/m/unload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
                with pytest.raises(rp.EffectBlocked):
                    getattr(connection, method)([payload] if method == "sendmsg" else payload)
        assert receiver.agent_bodies == []
    assert any(fields["what"] == "raw_socket_send" for _, fields in emitted)


def test_http_send_capability_is_scoped_and_permitted_http_client_still_works():
    import http.client
    import httpx
    with LoopbackBackend() as receiver:
        port = receiver.server.server_address[1]
        with rp.EffectGuard(lambda *a, **k: None, allowed_ports={port}) as guard:
            client = http.client.HTTPConnection("127.0.0.1", port)
            client.request("GET", "/health")
            assert client.getresponse().status == 200
            client.close()
            with httpx.Client() as client:
                assert client.get(receiver.url + "/health").status_code == 200
            assert guard._http_operation.get() is None
            with socket.socket() as connection:
                connection.connect(("127.0.0.1", port))
                with pytest.raises(rp.EffectBlocked):
                    connection.sendall(b"POST /v1/models/m/load HTTP/1.1\r\n\r\n")


def test_lease_exception_allows_only_lock_acquisition_not_path_or_descriptor_mutation(tmp_path):
    real_home, code, throwaway, policy = fs_world(tmp_path)
    lock = real_home / ".moe/.provisioning.lock"
    lock.write_bytes(b"retained")
    lock.chmod(0o600)
    emitted = []
    with rp.EffectGuard(lambda kind, **fields: emitted.append(fields), fs=policy):
        for operation in (lambda: open(lock, "w"), lambda: os.unlink(lock),
                          lambda: os.chmod(lock, 0o644), lambda: os.truncate(lock, 0),
                          lambda: os.open(lock, os.O_CREAT | os.O_RDWR),
                          lambda: os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)):
            with pytest.raises(rp.EffectBlocked):
                operation()
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            for operation in (lambda: os.write(fd, b"corrupt"), lambda: os.ftruncate(fd, 0),
                              lambda: os.fchmod(fd, 0o644), lambda: os.fdopen(fd, "w", closefd=False)):
                with pytest.raises(rp.EffectBlocked):
                    operation()
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    assert lock.read_bytes() == b"retained" and lock.stat().st_mode & 0o777 == 0o600
    assert len(emitted) == 10


def test_private_read_through_outside_symlink_and_untracked_fd_is_refused(tmp_path):
    real_home, code, throwaway, policy = fs_world(tmp_path)
    private = real_home / "Documents/private.txt"
    alias = tmp_path / "outside-alias"
    alias.symlink_to(private)
    fd = os.open(private, os.O_RDONLY)
    try:
        with rp.EffectGuard(lambda *a, **k: None, fs=policy):
            with pytest.raises(rp.EffectBlocked):
                alias.read_text()
            with pytest.raises(rp.EffectBlocked):
                open(fd, "r", closefd=False)
    finally:
        os.close(fd)


@pytest.mark.parametrize("probe", ["never_exits", "permission"])
def test_group_teardown_does_not_report_success_without_confirmed_group_exit(tmp_path, monkeypatch, probe):
    class Exited:
        pid = 23456789
        def wait(self, timeout): return 0
        def poll(self): return 0
        def send_signal(self, sig): pytest.fail("owned group must be used")
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(rp, "KILL_WAIT_S", 0.01)
    signals = []
    def killpg(group, sig):
        signals.append((group, sig))
        if probe == "permission" and sig == 0:
            raise PermissionError("synthetic probe failure")
    monkeypatch.setattr(os, "killpg", killpg)
    backend = rp.BackendProcess("candidate", tmp_path, 1, tmp_path, tmp_path / "i", sys.executable)
    backend.proc = Exited()
    result = backend.stop()
    assert result["exited"] is True and result["group_exited"] is False and result["ok"] is False
    assert any(e.startswith("group_wait:" if probe == "never_exits" else "group_probe:") for e in result["errors"])


@pytest.mark.parametrize("scalar", [None, [], 7, True, "hello"])
def test_scalar_sse_is_retained_as_unparseable_and_never_a_success(scalar):
    with LoopbackBackend([(0, scalar), (0, {"type": "done"})]) as backend:
        result = drive(backend.url, SPECS["greeting_warm"], "hello")
    assert result["events"][0]["event"]["type"] == "_unparseable"
    record, _ = grade("greeting_warm", result["events"], **{k: v for k, v in result.items() if k != "events"})
    assert not record["correct"]


@pytest.mark.parametrize("scalar", [None, [], 7, True, "hello"])
def test_scalar_receipt_is_a_structured_refusal(good, ctx, tmp_path, scalar):
    path = tmp_path / "scalar.json"
    path.write_text(json.dumps(scalar))
    opts = check_opts(good, ctx, receipt=path, expect_receipt_sha256=rp.sha256_file(path))
    code, result = rp.check_receipt(opts)
    assert code == rp.EXIT_REFUSED and result["authorizes_release"] is False
    assert "receipt_not_an_object" in refusal_codes(result)


def test_driver_exception_retains_inflight_sample_and_seals_aborted_receipt(tmp_path, ctx, monkeypatch):
    # Constructed deps create ScriptedDriver instead: override its dispatch deliberately.
    deps = ConstructedEvidenceDeps(ctx)
    original_make = deps.make_driver
    def make_driver(backend):
        driver = original_make(backend)
        async def broken(spec, prompt, sink):
            sink["events"].append(ev(0, type="delta", text="partial"))
            raise RuntimeError("synthetic failure")
        driver.run_turn = broken
        return driver
    deps.make_driver = make_driver
    out = tmp_path / "crashed"
    opts = {"candidate_root": ctx.cand_wt, "candidate_sha": ctx.cand_sha,
            "baseline_root": ctx.base_wt, "baseline_sha": ctx.base_sha,
            "lane": "desktop", "model_id": MODEL_ID, "output_dir": out}
    code, path = asyncio.run(rp.run_release(opts, deps, rp.load_bundle()))
    receipt = json.loads(path.read_text())
    assert receipt["aborted"] is True and code != rp.EXIT_PASS
    assert all(b.stopped for b in deps.spawned)
    events = [json.loads(row) for row in (out / "raw/events.jsonl").read_text().splitlines()]
    assert len(events) == 1 and events[0]["events"][0]["event"]["text"] == "partial"
    assert events[0]["driver"]["transport_error"] == "RuntimeError"


@pytest.mark.parametrize("error", [PermissionError("synthetic"), OSError(5, "synthetic EIO")])
def test_unknown_group_identity_errors_are_recorded_and_never_cleanup_success(tmp_path, monkeypatch, error):
    class Exited:
        pid = 23456789
        def wait(self, timeout): return 0
        def poll(self): return 0
        def send_signal(self, sig): pass
    def fail(pid): raise error
    monkeypatch.setattr(os, "getpgid", fail)
    backend = rp.BackendProcess("candidate", tmp_path, 1, tmp_path, tmp_path / "i", sys.executable)
    backend.proc = Exited()
    result = backend.stop()
    assert result["exited"] is True and result["ok"] is False
    assert any(e.startswith("group_identity:") for e in result["errors"])


def test_arbitrary_sys_path_is_not_a_private_home_read_capability(tmp_path, monkeypatch):
    real_home, code, throwaway, _ = fs_world(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), str(real_home), *sys.path])
    policy = rp.build_fs_policy(code, throwaway, real_home=real_home)
    assert not policy.read_allowed(policy.absolute(real_home / "Documents/private.txt"))


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), float("-inf"), True, "1800000000"])
def test_nonfinite_or_nonnumeric_receipt_time_cannot_bypass_freshness(good, ctx, tmp_path, stamp):
    evd = clone(good, tmp_path, "invalid_time")
    receipt = json.loads(evd.receipt_path.read_text())
    receipt["generated_at_ts"] = stamp
    evd.receipt_path.write_text(json.dumps(receipt))
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_REFUSED and result["authorizes_release"] is False
    assert "receipt_time_invalid" in refusal_codes(result)


@pytest.mark.parametrize("target", ["receipt", "approval", "raw_file", "raw_directory", "raw_descendant", "receipt_parent"])
def test_release_intake_rejects_outside_file_and_directory_links(good, ctx, tmp_path, target):
    evd = clone(good, tmp_path)
    opts = check_opts(evd, ctx)
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "receipt":
        replacement = outside / "receipt.json"
        replacement.write_bytes(evd.receipt_path.read_bytes())
        evd.receipt_path.unlink()
        evd.receipt_path.symlink_to(replacement)
    elif target == "approval":
        replacement = outside / "approval.json"
        replacement.write_bytes(Path(opts.approved_baseline).read_bytes())
        link = evd.out / "approval-link.json"
        link.symlink_to(replacement)
        opts.approved_baseline = link
    elif target == "raw_file":
        path = evd.out / "raw" / "samples.jsonl"
        replacement = outside / "samples.jsonl"
        replacement.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(replacement)
    elif target == "raw_directory":
        raw = evd.out / "raw"
        moved = outside / "raw"
        raw.rename(moved)
        raw.symlink_to(moved, target_is_directory=True)
    elif target == "raw_descendant":
        (evd.out / "raw" / "linked-directory").symlink_to(outside, target_is_directory=True)
    else:
        linked = tmp_path / "linked-package"
        linked.symlink_to(evd.out, target_is_directory=True)
        opts.receipt = linked / "receipt.json"
    code, result = rp.check_receipt(opts)
    assert code == rp.EXIT_REFUSED and result["authorizes_release"] is False


@pytest.mark.parametrize("target", ["receipt", "approval", "raw"])
def test_release_intake_refuses_fifo_without_reading_or_blocking(good, ctx, tmp_path, monkeypatch, target):
    evd = clone(good, tmp_path)
    opts = check_opts(evd, ctx)
    if target == "receipt":
        path = evd.receipt_path
    elif target == "approval":
        path = evd.out / "fifo-approval"
        opts.approved_baseline = path
    else:
        path = evd.out / "raw" / "samples.jsonl"
    if path.exists():
        path.unlink()
    os.mkfifo(path)
    original_read = os.read
    fifo = path.stat()
    def read_regular_only(fd, count):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) != (fifo.st_dev, fifo.st_ino), "attempted to read the FIFO"
        return original_read(fd, count)
    monkeypatch.setattr(os, "read", read_regular_only)
    code, result = rp.check_receipt(opts)
    assert code == rp.EXIT_REFUSED and result["authorizes_release"] is False


def test_release_raw_hash_and_derivation_use_same_retained_bytes(good, ctx, tmp_path, monkeypatch):
    evd = clone(good, tmp_path)
    samples = evd.out / "raw" / "samples.jsonl"
    valid = samples.read_bytes()
    rows = [json.loads(line) for line in valid.splitlines()]
    rows[0]["side"] = "invented-side"
    samples.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    seal(evd.out, evd.receipt_path)
    opts = check_opts(evd, ctx)
    original_load = rp.load_raw
    seen = []
    def replace_after_hash(raw_dir, *, snapshot=None):
        assert snapshot is not None and snapshot["samples.jsonl"] != valid
        samples.write_bytes(valid)
        seen.append(True)
        return original_load(raw_dir, snapshot=snapshot)
    monkeypatch.setattr(rp, "load_raw", replace_after_hash)
    code, result = rp.check_receipt(opts)
    assert seen and code == rp.EXIT_REFUSED and result["authorizes_release"] is False


@pytest.mark.parametrize("limit", ["receipt", "approval", "raw_file", "raw_package", "file_count", "entry_count"])
def test_release_intake_enforces_bounded_package_limits(good, ctx, tmp_path, monkeypatch, limit):
    evd = clone(good, tmp_path)
    opts = check_opts(evd, ctx)
    name = {"receipt": "RECEIPT_MAX_BYTES", "approval": "APPROVAL_MAX_BYTES",
            "raw_file": "RAW_FILE_MAX_BYTES", "raw_package": "RAW_PACKAGE_MAX_BYTES",
            "file_count": "RAW_MAX_FILES", "entry_count": "RAW_MAX_ENTRIES"}[limit]
    monkeypatch.setattr(rp, name, 1)
    code, result = rp.check_receipt(opts)
    assert code == rp.EXIT_REFUSED and result["authorizes_release"] is False


def test_evidence_snapshot_refuses_mutation_during_read(tmp_path, monkeypatch):
    path = tmp_path / "mutating.json"
    path.write_bytes(b"before")
    original_read = os.read
    changed = []
    def mutate(fd, count):
        data = original_read(fd, count)
        if data and not changed:
            path.write_bytes(b"replacement with different length")
            changed.append(True)
        return data
    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(ValueError, match="evidence_changed_during_snapshot"):
        rp.evidence_bytes(path, 1024)


@pytest.mark.parametrize("target", ["receipt", "approval"])
def test_external_digest_and_parsing_are_bound_to_same_snapshot(good, ctx, tmp_path, monkeypatch, target):
    evd = clone(good, tmp_path)
    opts = check_opts(evd, ctx)
    if target == "receipt":
        path = evd.receipt_path
        valid = path.read_bytes()
        bad = json.loads(valid)
        bad["candidate"]["sha"] = "e" * 40
        path.write_text(json.dumps(bad))
        opts.expect_receipt_sha256 = rp.sha256_file(path)
    else:
        path = evd.out / "snapshot-approval.json"
        valid = Path(opts.approved_baseline).read_bytes()
        bad = json.loads(valid)
        bad["subject"]["sha"] = "e" * 40
        path.write_text(json.dumps(bad))
        opts.approved_baseline = path
        opts.approved_baseline_sha256 = rp.sha256_file(path)
    retained = path.read_bytes()
    original_hash = rp.sha256_bytes
    seen = []
    def swap_after_digest(data):
        digest = original_hash(data)
        if data == retained and not seen:
            path.write_bytes(valid)
            seen.append(True)
        return digest
    monkeypatch.setattr(rp, "sha256_bytes", swap_after_digest)
    code, result = rp.check_receipt(opts)
    assert seen and code == rp.EXIT_REFUSED and result["authorizes_release"] is False
    expected = "candidate_sha_mismatch" if target == "receipt" else "baseline_subject_not_the_approved_one"
    assert expected in refusal_codes(result)


@pytest.mark.parametrize("target", ["receipt", "approval", "raw"])
def test_release_intake_rejects_hard_links_to_outside_state(good, ctx, tmp_path, target):
    evd = clone(good, tmp_path)
    opts = check_opts(evd, ctx)
    if target == "receipt":
        path = evd.receipt_path
    elif target == "approval":
        path = evd.out / "hardlinked-approval"
        path.write_bytes(Path(opts.approved_baseline).read_bytes())
        opts.approved_baseline = path
    else:
        path = evd.out / "raw" / "samples.jsonl"
    outside = tmp_path / "outside-hardlink"
    os.link(path, outside)
    code, result = rp.check_receipt(opts)
    assert code == rp.EXIT_REFUSED and result["authorizes_release"] is False


FD_OPERATIONS = ("write", "writev", "pwrite", "ftruncate", "fchmod", "fchown", "fdopen", "open", "truncate", "chmod", "chown")


def descriptor_operation(name, fd):
    if name == "write": return os.write(fd, b"UNREVIEWED-ENGINE-OPERATION")
    if name == "writev": return os.writev(fd, [b"UNREVIEWED-ENGINE-OPERATION"])
    if name == "pwrite": return os.pwrite(fd, b"UNREVIEWED-ENGINE-OPERATION", 0)
    if name in ("ftruncate", "truncate"): return getattr(os, name)(fd, 0)
    if name in ("fchmod", "chmod"): return getattr(os, name)(fd, 0o600)
    if name in ("fchown", "chown"): return getattr(os, name)(fd, -1, -1)
    if name == "fdopen": return os.fdopen(fd, "rb", closefd=False)
    if name == "open": return open(fd, "rb", closefd=False)
    raise AssertionError(name)


@pytest.mark.parametrize("operation", FD_OPERATIONS)
def test_c_closed_owned_fd_reused_for_socket_never_transmits_or_opens(operation, tmp_path):
    emitted = []
    with owned_fixture_socket() as listener:
        listener.listen(1)
        listener.settimeout(1)
        port = listener.getsockname()[1]
        OWNED_PORTS.add(port)
        policy = rp.FsPolicy(home=tmp_path, write_roots=[tmp_path])
        with rp.EffectGuard(lambda kind, **fields: emitted.append(fields), fs=policy, allowed_ports={port}):
            stale = os.open(tmp_path / "owned.tmp", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(stale, "wb") as owned:
                owned.write(b"owned")  # FileIO's C-level close leaves the numeric mapping behind.
            with socket.socket() as client:
                duplicate = client.fileno() != stale
                if duplicate:
                    os.dup2(client.fileno(), stale)
                try:
                    client.connect(("127.0.0.1", port))
                    with listener.accept()[0] as peer:
                        peer.settimeout(0.02)
                        with pytest.raises(rp.EffectBlocked):
                            descriptor_operation(operation, stale)
                        with pytest.raises(socket.timeout):
                            peer.recv(64)
                finally:
                    if duplicate:
                        os.close(stale)
    assert any(row["what"] == "fs_descriptor_stale" for row in emitted)


@pytest.mark.parametrize("replacement", ["owned", "private"])
@pytest.mark.parametrize("operation", FD_OPERATIONS)
def test_c_closed_owned_fd_reused_for_another_file_is_not_a_capability(replacement, operation, tmp_path):
    real_home, code, throwaway, policy = fs_world(tmp_path)
    path = throwaway / "replacement" if replacement == "owned" else real_home / "Documents/private.txt"
    if replacement == "owned":
        path.write_bytes(b"preserve")
    preserved = path.read_bytes()
    # Fixture owns this original opener; simulate an FD replacement without
    # blessing it through the guard's os.open registration path.
    native_open = os.open
    emitted = []
    with rp.EffectGuard(lambda kind, **fields: emitted.append(fields), fs=policy):
        stale = os.open(throwaway / "first", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(stale, "wb") as owned:
            owned.write(b"first")
        replacement_fd = native_open(path, os.O_RDWR)
        duplicate = replacement_fd != stale
        if duplicate:
            os.dup2(replacement_fd, stale)
        try:
            with pytest.raises(rp.EffectBlocked):
                descriptor_operation(operation, stale)
        finally:
            os.close(replacement_fd)
            if duplicate:
                os.close(stale)
    assert path.read_bytes() == preserved
    assert any(row["what"] == "fs_descriptor_stale" for row in emitted)


def test_valid_owned_fd_writes_metadata_and_fdopen_remain_usable(tmp_path):
    policy = rp.FsPolicy(home=tmp_path, write_roots=[tmp_path])
    with rp.EffectGuard(lambda *a, **k: None, fs=policy):
        fd = os.open(tmp_path / "owned", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            assert os.write(fd, b"abc") == 3
            assert os.pwrite(fd, b"Z", 0) == 1
            assert os.writev(fd, [b"d", b"e"]) == 2
            os.ftruncate(fd, 3)
            os.fchmod(fd, 0o600)
            os.fchown(fd, -1, -1)
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                assert stream.read() == b"Zbc"
        finally:
            os.close(fd)
    assert (tmp_path / "owned").read_bytes() == b"Zbc"


@pytest.mark.parametrize("primitive", ["socket.sendfile", "os.sendfile", "os.splice"])
@pytest.mark.parametrize("http_scope", [False, True])
def test_file_transfer_primitives_refuse_before_underlying_call(monkeypatch, primitive, http_scope):
    owner, name = (socket.socket, "sendfile") if primitive == "socket.sendfile" else (os, primitive.split(".")[1])
    called = []
    monkeypatch.setattr(owner, name, lambda *a, **k: called.append((a, k)), raising=False)
    events = []
    with socket.socket() as connection, rp.EffectGuard(lambda kind, **fields: events.append({"kind": kind, **fields})) as guard:
        token = guard._http_operation.set(("127.0.0.1", 18775)) if http_scope else None
        try:
            with pytest.raises(rp.EffectBlocked):
                if owner is socket.socket:
                    connection.sendfile(object())
                else:
                    getattr(os, name)(-1, -1, 0, 1)
        finally:
            if token is not None:
                guard._http_operation.reset(token)
    assert called == []
    assert len(events) == 1 and events[0]["kind"] == "effect_blocked"
    assert events[0]["what"] == ("raw_socket_send" if owner is socket.socket else "raw_descriptor_transfer")


def test_file_transfer_refusal_preserves_reviewed_http_requests_and_scope(monkeypatch):
    import httpx
    called = []
    monkeypatch.setattr(socket.socket, "sendfile", lambda *a, **k: called.append(True))
    with LoopbackBackend() as receiver:
        port = receiver.server.server_address[1]
        with rp.EffectGuard(lambda *a, **k: None, allowed_ports={port}) as guard:
            with httpx.Client() as client:
                assert client.get(receiver.url + "/health").status_code == 200
                with socket.socket() as connection, pytest.raises(rp.EffectBlocked):
                    connection.sendfile(object())
                assert client.post(receiver.url + "/v1/chat/completions", json={"model": "synthetic"}).status_code == 200
            assert guard._http_operation.get() is None
        assert receiver.agent_bodies == [{"model": "synthetic"}]
    assert called == []


@pytest.mark.parametrize("primitive", ["socket.sendfile", "os.sendfile", "os.splice"])
def test_file_transfer_refusal_cannot_be_hidden_by_clean_samples(tmp_path, ctx, monkeypatch, primitive):
    owner, name = (socket.socket, "sendfile") if primitive == "socket.sendfile" else (os, primitive.split(".")[1])
    called = []
    monkeypatch.setattr(owner, name, lambda *a, **k: called.append(True), raising=False)
    events = []
    with socket.socket() as connection, rp.EffectGuard(lambda kind, **fields: events.append({"kind": kind, **fields})):
        with pytest.raises(rp.EffectBlocked):
            if owner is socket.socket:
                connection.sendfile(object())
            else:
                getattr(os, name)(-1, -1, 0, 1)
    assert called == []
    evd = pass_evidence(tmp_path, ctx, "denied_file_transfer", startup_events={"candidate": events})
    assert all_samples_correct_and_clean(evd)
    code, result = run_check(evd, ctx)
    assert code == rp.EXIT_BLOCK and result["authorizes_release"] is False
    assert evd.receipt["lifecycle"]["candidate"]["blocked_effects"] == 1


@pytest.mark.parametrize("case,refusal", [
    ("missing_digest", "approved_baseline_digest_not_bound"),
    ("wrong_digest", "approved_baseline_digest_mismatch"),
    ("wrong_sha", "baseline_subject_not_the_approved_one"),
    ("wrong_tree", "baseline_tree_not_the_approved_one"),
    ("wrong_schema", "approved_baseline_wrong_schema"),
])
def test_invalid_explicit_approval_refuses_run_before_measurement_and_check(case, refusal, good, ctx, tmp_path):
    changes = {}
    if case == "wrong_sha":
        changes["subject"] = {"sha": ctx.cand_sha, "tree": good.receipt["candidate"]["tree"]}
    elif case == "wrong_tree":
        changes["subject"] = {"sha": ctx.base_sha, "tree": "f" * 40}
    elif case == "wrong_schema":
        changes["schema"] = "invalid"
    path, digest = write_approval(tmp_path, good.receipt, **changes)
    if case == "missing_digest":
        digest = None
    elif case == "wrong_digest":
        digest = "f" * 64
    state, problems = rp.load_baseline_approval(str(path), digest)
    if case in ("missing_digest", "wrong_digest", "wrong_schema"):
        assert state["approved"] is False and refusal in problems
    deps = ConstructedEvidenceDeps(ctx)
    def measurement_must_not_start(*a, **k):
        pytest.fail("invalid approval reached measurement dependencies")
    for name in ("preflight", "collect_environment", "spawn_backend", "residency", "make_driver", "push_leaves"):
        setattr(deps, name, measurement_must_not_start)
    out = tmp_path / "refused-run"
    opts = {**good.opts, "output_dir": out, "approved_baseline_path": str(path), "approved_baseline_sha256": digest}
    with pytest.raises(ValueError, match=refusal):
        asyncio.run(rp.run_release(opts, deps, rp.load_bundle()))
    assert not out.exists() and deps.spawned == [] and deps.total_calls == 0
    code, result = rp.check_receipt(check_opts(good, ctx, approval=(path, digest)))
    assert code == rp.EXIT_REFUSED and result["authorizes_release"] is False
    assert refusal in refusal_codes(result)
