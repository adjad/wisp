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


@pytest.fixture(autouse=True)
def inert_environment(monkeypatch):
    """Tests may reach loopback and run git or this interpreter, nothing else."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_init = subprocess.Popen.__init__
    blocked: list[str] = []

    def check(address):
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost")):
            blocked.append(repr(address))
            raise AssertionError(f"test attempted a non-loopback connection: {address!r}")

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
            "blocked_effects": 0}
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


DENIED = {"approvals": [{"t_ns": 1, "action_id": "c1", "approved": False, "accepted": True}]}


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


def test_baseline_failures_make_the_cohort_inconclusive_not_a_candidate_block():
    samples = world()
    for s in samples:
        if s["side"] == "baseline" and s["scenario"] == "greeting_warm" and s["rep"] == 3:
            s.update(outcome="failed")
            s["grade"].update(correct=False, reasons=["empty_answer"])
    decision = rp.decide(rp.summarize(samples, BUNDLE), BUNDLE, APPROVED)
    assert decision["verdict"] == "INCONCLUSIVE"
    assert [r["code"] for r in decision["reasons"]] == ["baseline_correctness_failure"]


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
        extra["approvals"] = [{"t_ns": 1, "action_id": "c1", "approved": False, "accepted": True}]
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
    def __init__(self, deps, side, root, instrument_path):
        self.deps, self.side, self.root, self.instrument_path = deps, side, Path(root), Path(instrument_path)
        self.port = 18775 if side == "candidate" else 18776
        self.python = sys.executable
        self.stopped, self._cpu = False, 0.0
        header = {"kind": "header", "t_ns": deps.clock_ns(), "pid": 4000 + (side == "baseline"),
                  "root": str(self.root.resolve()), "harness": rp.HARNESS_VERSION, "effect_guard": True,
                  "available": [n for n in ("engine_http", "authority_load", "process_check", "binding_check",
                                            "peer_check") if n not in deps.unavailable],
                  "unavailable": list(deps.unavailable)}
        self.instrument_path.write_text(json.dumps(header, sort_keys=True) + "\n")

    def wait_ready(self, timeout_s=0):
        return True

    def stop(self):
        self.stopped = True

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
        deps, side = self.deps, self.backend.side
        deps.total_calls += 1
        if deps.cancel_at and deps.total_calls == deps.cancel_at:
            raise asyncio.CancelledError
        n = deps.calls[(side, spec["id"])] = deps.calls.get((side, spec["id"]), 0) + 1
        behavior = deps.behaviors.get((side, spec["id"]), "ok")
        kind = behavior(n) if callable(behavior) else behavior
        scale, add = (deps.cand_mult, deps.cand_add_ms) if side == "candidate" else (deps.base_mult, 0.0)
        add += (n * 7 % 11) * 0.1
        events, extra, chats = scripted(spec["id"], kind, scale, add)
        sink["abs_start_ns"] = deps.clock_ns()
        sink.update(events=events, **extra)
        lines = [{"kind": "engine_http", "t_ns": sink["abs_start_ns"] + 1000 + i, "pid": 4000, "method": "POST",
                  "path": "/v1/chat/completions", "status": 200, "t_start_ns": sink["abs_start_ns"],
                  "t_headers_ns": sink["abs_start_ns"] + 500} for i in range(chats)]
        if spec["kind"] == "http":
            lines.append({"kind": "engine_http", "t_ns": sink["abs_start_ns"] + 2000, "pid": 4000, "method": "GET",
                          "path": "/health", "status": 200, "t_start_ns": sink["abs_start_ns"],
                          "t_headers_ns": sink["abs_start_ns"] + 900})
        with self.backend.instrument_path.open("a") as handle:
            for line in lines:
                handle.write(json.dumps(line, sort_keys=True) + "\n")
        sink["abs_end_ns"] = deps.clock_ns()


class ConstructedEvidenceDeps:
    """Builds evidence inside pytest temp dirs. It measures nothing."""

    def __init__(self, ctx, *, is_real=True, cand_mult=1.0, cand_add_ms=0.0, base_mult=1.0, behaviors=None,
                 unavailable=(), missing=None, cancel_at=None, dirty_after=False, now_ts=1_800_000_000.0):
        self.ctx, self.is_real = ctx, is_real
        self.cand_mult, self.cand_add_ms, self.base_mult = cand_mult, cand_add_ms, base_mult
        self.behaviors, self.unavailable = behaviors or {}, list(unavailable)
        self.missing, self.cancel_at, self.dirty_after, self.now_ts = missing or [], cancel_at, dirty_after, now_ts
        self.calls, self.total_calls, self._clock, self._tick, self.spawned = {}, 0, 10_000_000_000, 0, []
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
        return {"hardware": {"model": "Mac17,8", "chip": "Apple M5 Pro", "memory_bytes": "25769803776", "cpu_count": "18"},
                "os": {"product_version": "27.0", "build": "TEST", "kernel": "27.0.0"},
                "python": {"version": "3.14.3", "executable": sys.executable},
                "engine": {"name": "oMLX", "app": {"short": "0.6.4", "build": "6"}, "port": 8000},
                "model": {"id": model_id, "dir_found": True, "config_sha256": "c" * 64, "tokenizer_sha256": "d" * 64,
                          "quantization": {"bits": 4, "group_size": 64}, "weights_manifest": {"sha256": "e" * 64}},
                "generation_settings": {"model_settings": {"temperature": 1.0}, "request_temperature": "omitted"},
                "cache_warmup_treatment": {"cache_cleared_before_run": False, "warmup_samples_per_scenario_per_side": warmups},
                "power": "AC Power"}

    def preflight(self, lane, model_id):
        return list(self.missing)

    def spawn_backend(self, side, root, home, instrument_path):
        backend = FakeBackend(self, side, root, instrument_path)
        self.spawned.append(backend)
        return backend

    def make_driver(self, backend):
        return FakeDriver(self, backend)

    def push_leaves(self, backend, payloads):
        return [200] * len(payloads)


def run_evidence(tmp_path, ctx, name="evidence", *, lane="desktop", samples=None, diagnostic=False,
                 scenario_ids=None, approved=None, **deps_kwargs):
    deps = ConstructedEvidenceDeps(ctx, **deps_kwargs)
    out = tmp_path / name
    opts = {"candidate_root": ctx.cand_wt, "candidate_sha": ctx.cand_sha, "baseline_root": ctx.base_wt,
            "baseline_sha": ctx.base_sha, "lane": lane, "model_id": "Test-Model-oQ4e", "output_dir": out,
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


def check_opts(evd, ctx, *, approval=True, **overrides):
    receipt = evd.receipt
    path, digest = getattr(evd, "approval", (None, None)) if approval is True else (approval or (None, None))
    values = dict(receipt=evd.receipt_path, bundle=rp.DEFAULT_BUNDLE, expect_candidate_sha=ctx.cand_sha,
                  expect_corpus_sha256=BUNDLE["corpus_sha256"], expect_policy_sha256=BUNDLE["policy_sha256"],
                  expect_harness_sha256=rp.harness_sha256(), approved_baseline=path,
                  approved_baseline_sha256=digest, expect_receipt_sha256=None, lane=receipt["lane"],
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


def test_pass_through_the_cli_exits_zero_and_everything_else_is_nonzero(good, ctx, tmp_path):
    def run_cli(*extra, receipt=None):
        approval = good.approval
        argv = [sys.executable, str(SCRIPT), "check", "--receipt", str(receipt or good.receipt_path),
                "--expect-candidate-sha", ctx.cand_sha, "--expect-corpus-sha256", BUNDLE["corpus_sha256"],
                "--expect-policy-sha256", BUNDLE["policy_sha256"], "--expect-harness-sha256", rp.harness_sha256(),
                "--approved-baseline", str(approval[0]), "--approved-baseline-sha256", approval[1],
                "--repo", str(ctx.repo), "--now", str(good.deps.now_ts + 60), *extra]
        return subprocess.run(argv, capture_output=True, text=True)
    ok = run_cli()
    assert ok.returncode == 0 and json.loads(ok.stdout)["verdict"] == "PASS"
    assert run_cli("--expect-receipt-sha256", "0" * 64).returncode == rp.EXIT_REFUSED
    assert run_cli(receipt=tmp_path / "missing.json").returncode == rp.EXIT_REFUSED
    usage = subprocess.run([sys.executable, str(SCRIPT), "check", "--receipt", "x"], capture_output=True, text=True)
    assert usage.returncode == rp.EXIT_USAGE            # never collides with INCONCLUSIVE (2)


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
    assert code == rp.EXIT_REFUSED and "receipt_verdict_mismatch" in refusal_codes(result)
    wrong_subject = write_approval(tmp_path, good.receipt, subject={"sha": ctx.cand_sha, "tree": "x"})
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


def test_a_window_event_not_present_in_the_instrumentation_file_is_refused(good, ctx, tmp_path):
    evd = clone(good, tmp_path)
    (evd.out / "raw" / "instrumentation.candidate.jsonl").write_text(
        "".join(l for l in (evd.out / "raw" / "instrumentation.candidate.jsonl").read_text().splitlines(True)
                if '"/health"' not in l))
    seal(evd.out, evd.receipt_path)
    assert "raw_derivation_mismatch" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


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
    assert code == rp.EXIT_REFUSED and {"not_an_actual_measurement", "fake_or_unverified_model"} <= refusal_codes(result)
    # and the same constructed evidence flagged as real passes, which proves the refusal above is about provenance
    real = pass_evidence(tmp_path, ctx, "offline_as_real")
    assert rp.check_receipt(check_opts(real, ctx))[0] == rp.EXIT_PASS


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
                "baseline_sha": ctx.base_sha, "lane": "desktop", "model_id": "m", "output_dir": tmp_path / name,
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
    assert refusal_codes(result) & {"raw_derivation_mismatch", "no_attributed_engine_evidence"}


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
    assert not (evd.out / "raw" / "samples.jsonl").exists()
    evd.approval = write_approval(tmp_path, evd.receipt)
    assert "incomplete_run" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


def test_an_interrupted_run_keeps_its_partial_evidence_and_is_inconclusive(tmp_path, ctx):
    evd = run_evidence(tmp_path, ctx, "aborted", cancel_at=40)
    assert evd.receipt["aborted"] is True and evd.code == rp.EXIT_INCONCLUSIVE
    lines = (evd.out / "raw" / "samples.jsonl").read_text().splitlines()
    assert len(lines) == 39 and all(b.stopped for b in evd.deps.spawned)
    evd.approval = write_approval(tmp_path, evd.receipt)
    assert "incomplete_run" in refusal_codes(rp.check_receipt(check_opts(evd, ctx))[1])


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
    assert rp.messages_lines(rows) == wire.messages_lines(rows)
    for args in (("Crew", ["A", "B"], True), (None, ["A", "B", "C", "D", "E"], True), (None, ["Al"], False),
                 ("Al", ["Al"], False), (None, [], False)):
        assert rp.thread_context(*args) == wire.thread_context(*args)
    notes = [{"ts": 5, "title": "T", "folder": "F", "body": "B"}, {"ts": 6, "title": "U", "body": "C"}]
    assert rp.notes_raw(notes) == wire.notes_raw(notes)


def test_rendered_payloads_use_the_real_sync_endpoints_with_fictional_data():
    payloads = rp.render_leaf_payloads(CORPUS, 1_800_000_000.0)
    assert [p["path"] for p in payloads] == ["/assistant/sync/messages", "/assistant/sync/messages",
                                              "/assistant/sync/notes"]
    assert payloads[0]["body"] == {"contacts": CORPUS["leaf_data"]["contacts"]}
    body = payloads[1]["body"]
    lines = body["lines"].splitlines()
    assert body["diagnostics"] == {"available": True, "reason": "", "count": len(lines)} and len(lines) == 3
    stamps = [int(l.split(" | ")[0]) for l in lines]
    assert stamps == sorted(stamps, reverse=True)                      # newest first, as the native reader sends
    assert all(len(l.split(" | ", 2)) == 3 for l in lines)
    assert "Orchard Lane workshop" in payloads[2]["body"]["raw"] and "\x02" in payloads[2]["body"]["raw"]
    assert rp.render_leaf_payloads(CORPUS, 1_800_000_000.0) == payloads        # deterministic for a given clock


def test_role_config_points_every_text_role_at_the_resident_model_only():
    text = rp.render_role_config("Some-Model-oQ4e")
    roles = [line.split(":")[0].strip() for line in text.splitlines()[1:]]
    assert roles == ["fast", "router", "general", "agent", "coding", "reasoning", "profile", "profile_map"]
    assert text.count('"Some-Model-oQ4e"') == len(roles) and "research" not in text


# ----------------------------------------------------------------------------
# Effect guard
# ----------------------------------------------------------------------------

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
        subprocess.Popen(["/usr/sbin/lsof", "-nP"])
        subprocess.Popen(["/bin/ps", "-p", "1"])
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
    assert popen == [["/usr/sbin/lsof", "-nP"], ["/bin/ps", "-p", "1"]]       # only the two read-only inspectors ran
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
    assert unavailable == [] and available == ["authority_load", "binding_check", "engine_http",
                                               "peer_check", "process_check"]
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
    assert available == [] and len(unavailable) == 5
    recorder.close()


def test_instrument_window_selection_and_header_parsing(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"kind": "header", "t_ns": 1, "unavailable": ["peer_check"], "available": ["engine_http"]},
        {"kind": "engine_http", "t_ns": 100}, {"kind": "engine_http", "t_ns": 200}, {"kind": "engine_http", "t_ns": 300}]) + "\nnot json\n")
    records = rp.read_instrument_lines(path)
    assert rp.instrument_header(records)["unavailable"] == ["peer_check"]
    assert [r["t_ns"] for r in rp.window_events(records, 150, 300)] == [200, 300]
    tail = rp.InstrumentTail(path)
    assert len(tail.refresh()) == 5
    with path.open("a") as handle:
        handle.write(json.dumps({"kind": "engine_http", "t_ns": 400}) + "\n")
        handle.write('{"kind": "partial')                                          # a half-written line is not consumed
    assert len(tail.refresh()) == 6


# ----------------------------------------------------------------------------
# The HTTP driver against a loopback SSE server
# ----------------------------------------------------------------------------

class LoopbackBackend:
    def __init__(self, script=(), status=200, health=None):
        self.script, self.status, self.health = list(script), status, health or ({"status": "ok"}, 200)
        self.agent_bodies, self.approvals = [], []
        state = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                if self.path == "/agent/approve":
                    state.approvals.append(body)
                    payload = json.dumps({"ok": True}).encode()
                    self.send_response(200)
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

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
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

    refused = drive("http://127.0.0.1:9", spec)                               # nothing listens there
    assert refused["transport_error"] == "ConnectError" and refused["abs_end_ns"]

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
    assert env["model"]["weights_manifest"]["files"] == [["w.safetensors", 4]] or \
        env["model"]["weights_manifest"]["files"] == [("w.safetensors", 4)]
    assert env["engine"]["app"]["short"] == UNKNOWN                      # plist absent: UNKNOWN, not a guess
    assert env["lane_evidence"]["managed_authorization_manifest_present"] is False
    assert env["cache_warmup_treatment"]["warmup_samples_per_scenario_per_side"] == 2

    gone = rp.collect_environment("No-Such-Model", "desktop", 2, runner=lambda *a, **k: (1, "", ""),
                                  home=home, plist=tmp_path / "none.plist")
    assert gone["model"]["config_sha256"] == UNKNOWN and gone["hardware"]["model"] == UNKNOWN
    assert rp.has_unknown(gone) and not rp.has_unknown({"a": [1, {"b": "x"}]})


def test_the_spawned_backend_entry_point_wires_root_state_guard_and_instrumentation(tmp_path, monkeypatch):
    fake = FakeAttribution()
    launched = []
    monkeypatch.setitem(sys.modules, "service.inference.attributed_transport", fake.at)
    monkeypatch.setitem(sys.modules, "service.inference.local_peer", fake.lp)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **k: launched.append((a, k))))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WISP_HOME", raising=False)
    root, home, out = tmp_path / "root", tmp_path / "home", tmp_path / "instr.jsonl"
    root.mkdir()
    args = argparse.Namespace(root=str(root), port=18999, home=str(home), instrument_out=str(out), engine_port=8000)
    try:
        assert rp.serve_main(args) == 0
        assert sys.path[0] == str(root.resolve()) and os.getcwd() == str(root.resolve())
        assert os.environ["WISP_HOME"] == str(home.resolve())
        assert launched == [(("service.main:app",), {"host": "127.0.0.1", "port": 18999, "log_level": "warning"})]
        with pytest.raises(rp.EffectBlocked):                                   # the guard really is installed
            subprocess.Popen(["osascript", "-e", "1"])
    finally:
        assert rp.ACTIVE_GUARD is not None
        rp.ACTIVE_GUARD.uninstall()
    records = rp.read_instrument_lines(out)
    header = rp.instrument_header(records)
    assert header["effect_guard"] is True and header["root"] == str(root.resolve())
    assert header["unavailable"] == [] and "engine_http" in header["available"]
    assert any(r["kind"] == "effect_blocked" and r["what"] == "exec" for r in records)


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
