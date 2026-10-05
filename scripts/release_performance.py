#!/usr/bin/env python3
"""Wisp release performance benchmark: exact-candidate measurement and a fail-closed receipt checker.

Two real jobs live here, and one rule runs through both:

* `run` measures REAL Wisp turns (routing, attributed engine transport, approval
  and publication code are the candidate's own) on isolated backends it spawns
  from two verified clean worktrees, against synthetic data only. It is a
  separate, explicitly confirmed step; nothing in this file runs it implicitly.
* `check` is the release gate. It re-derives every timing, grade and verdict
  from the raw JSONL and refuses anything it cannot verify. It exits nonzero
  for BLOCK, INCONCLUSIVE, refused or missing evidence, and it never accepts a
  fake-model or offline-fixture result as a performance PASS. It requires the
  receipt digest the measurement owner recorded, accepts only a receipt from
  the live measurement class, and its result says whether it authorizes a release.

The rule: a sample that is empty, truncated, failed, refused, cancelled, past
its deadline or a false success is a FAILURE. It never contributes a latency
and can only make a release look worse. A metric that was not measured is the
string "UNKNOWN", never zero.

Legacy `scripts/bench_latency_changes.py capture|compare` results (fake model,
routing CPU) are not release evidence and are refused by `check`.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import stat
from contextlib import contextmanager
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

HARNESS_VERSION = "release_performance/3"
BUNDLE_SCHEMA = "wisp.release_performance.bundle/1"
SAMPLE_SCHEMA = "wisp.release_performance.sample/2"
RECEIPT_SCHEMA = "wisp.release_performance.receipt/2"
BASELINE_SCHEMA = "wisp.release_performance.baseline/1"

ACTUAL_MODE = "actual_candidate_isolated_attributed"
OFFLINE_MODE = "offline_fixture"

# The floor comes from the release scope and cannot be lowered by a policy file.
ABSOLUTE_MIN_SAMPLES = 20
UNKNOWN = "UNKNOWN"

EXIT_PASS, EXIT_BLOCK, EXIT_INCONCLUSIVE, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2, 3, 4
VERDICT_EXIT = {"PASS": EXIT_PASS, "BLOCK": EXIT_BLOCK, "INCONCLUSIVE": EXIT_INCONCLUSIVE}

GATING_METRICS = ("first_visible_answer_s", "first_model_delta_s", "total_completion_s",
                  "approval_boundary_s", "request_latency_s")
SIDES = ("candidate", "baseline")
# Every instrumented surface. A surface a candidate lacks is UNKNOWN, never zero.
SURFACES = ("engine_http", "engine_completion", "authority_load", "process_check", "binding_check", "peer_check")
LIVE_DEPS_CLASS = "LiveDeps"   # the only measurement source a release check accepts
FULL_SHA = re.compile(r"[0-9a-f]{40}")
MS = 1_000_000

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = _REPO_ROOT / "test_fixtures" / "performance" / "release_v1.json"


# --------------------------------------------------------------------------
# Hashing and small helpers
# --------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def canonical_sha(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj))


def harness_sha256() -> str:
    return sha256_file(Path(__file__).resolve())


def utc_now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def sample_id(side: str, scenario: str, phase: str, rep: int) -> str:
    return f"{side}:{scenario}:{phase}:{rep}"


# --------------------------------------------------------------------------
# Corpus and policy bundle
# --------------------------------------------------------------------------

def load_bundle(path: Path | str = DEFAULT_BUNDLE) -> dict:
    path = Path(path)
    raw = path.read_bytes()
    data = json.loads(raw)
    corpus, policy = data.get("corpus"), data.get("policy")
    return {"path": str(path), "file_sha256": sha256_bytes(raw), "schema": data.get("schema"),
            "corpus": corpus, "policy": policy,
            "corpus_sha256": canonical_sha(corpus), "policy_sha256": canonical_sha(policy)}


def _fraction(value: Any) -> Fraction | None:
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return parsed if parsed >= 0 else None


def validate_bundle(bundle: dict) -> list[str]:
    """Return every problem found; an empty list means the bundle is usable."""
    problems: list[str] = []
    if bundle.get("schema") != BUNDLE_SCHEMA:
        problems.append("bundle schema mismatch")
    corpus, policy = bundle.get("corpus") or {}, bundle.get("policy") or {}
    if not corpus.get("version"):
        problems.append("corpus.version missing")
    if not policy.get("version"):
        problems.append("policy.version missing")
    scenarios = corpus.get("scenarios") or []
    seen: set[str] = set()
    for spec in scenarios:
        sid = spec.get("id")
        if not sid or sid in seen:
            problems.append(f"scenario id missing or duplicated: {sid!r}")
        seen.add(sid)
        if spec.get("kind") not in ("turn", "http"):
            problems.append(f"{sid}: kind must be turn or http")
        if not spec.get("variants"):
            problems.append(f"{sid}: no variants")
        if spec.get("kind") == "turn" and not spec.get("deadline_s"):
            problems.append(f"{sid}: deadline_s missing")
        for metric in spec.get("required_metrics") or []:
            if metric not in GATING_METRICS:
                problems.append(f"{sid}: unknown metric {metric}")
        if spec.get("required") and not spec.get("required_metrics"):
            problems.append(f"{sid}: required scenario has no required_metrics")
    if not any(s.get("required") for s in scenarios):
        problems.append("no required scenarios")
    minimum = policy.get("min_measured_samples")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < ABSOLUTE_MIN_SAMPLES:
        problems.append(f"min_measured_samples must be an integer >= {ABSOLUTE_MIN_SAMPLES}")
    warmups = policy.get("warmup_samples")
    if not isinstance(warmups, int) or isinstance(warmups, bool) or warmups < 0:
        problems.append("warmup_samples must be a non-negative integer")
    if policy.get("percentile_method") != "nearest-rank" or policy.get("percentiles") != [50, 95]:
        problems.append("only nearest-rank p50/p95 is defined")
    regression = policy.get("regression") or {}
    relative, absolute = _fraction(regression.get("relative_increase")), regression.get("absolute_increase_ms")
    if relative is None or relative == 0:
        problems.append("regression.relative_increase must be a positive number")
    if not isinstance(absolute, (int, float)) or isinstance(absolute, bool) or absolute < 0:
        problems.append("regression.absolute_increase_ms must be non-negative")
    if regression.get("rule") != "both_exceed":
        problems.append("regression.rule must be both_exceed")
    execution = policy.get("execution") or {}
    if execution.get("accepted_mode") != ACTUAL_MODE:
        problems.append("execution.accepted_mode must be the actual-candidate mode")
    if execution.get("fake_model_can_pass") is not False:
        problems.append("fake_model_can_pass must be false")
    lanes = policy.get("lanes") or {}
    if not (lanes.get("desktop") or {}).get("release_gate"):
        problems.append("desktop lane must be the release gate")
    if any((lane or {}).get("release_gate") for name, lane in lanes.items() if name != "desktop"):
        problems.append("only the desktop lane may be a release gate")
    if not isinstance(policy.get("max_receipt_age_s"), int) or policy["max_receipt_age_s"] <= 0:
        problems.append("max_receipt_age_s must be a positive integer")
    for key in ("candidate_correctness_failure", "new_refusal", "measured_material_regression"):
        if (policy.get("outcomes") or {}).get(key) != "BLOCK":
            problems.append(f"outcomes.{key} must be BLOCK")
    for key in ("missing_or_unapproved_baseline", "unverifiable_expectation",
                "baseline_correctness_failure", "unknown_gating_metric", "baseline_lifecycle_effect",
                "integrity_failure"):
        if (policy.get("outcomes") or {}).get(key) != "INCONCLUSIVE":
            problems.append(f"outcomes.{key} must be INCONCLUSIVE")
    if (policy.get("outcomes") or {}).get("lifecycle_blocked_effect") != "BLOCK":
        problems.append("outcomes.lifecycle_blocked_effect must be BLOCK")
    environment = policy.get("environment") or {}
    if environment.get("weights_identity") != "full_sha256" or environment.get("compare_before_and_after") is not True:
        problems.append("environment must require full weight digests compared before and after")
    if not (corpus.get("sentinels") or {}).get("acceptable_finish_reasons"):
        problems.append("corpus.sentinels.acceptable_finish_reasons missing")
    return problems


def required_scenarios(bundle: dict) -> list[dict]:
    return [s for s in bundle["corpus"]["scenarios"] if s.get("required")]


def scenario_by_id(bundle: dict) -> dict[str, dict]:
    return {s["id"]: s for s in bundle["corpus"]["scenarios"]}


# --------------------------------------------------------------------------
# Percentiles and aggregation. Integer nanoseconds, nearest-rank, UNKNOWN-aware.
# --------------------------------------------------------------------------

def nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    rank = max(1, (percentile * len(ordered) + 99) // 100)
    return ordered[rank - 1]


def aggregate(values: list[Any]) -> dict:
    """p50/p95 over the sample. If ANY value is UNKNOWN the percentiles are UNKNOWN.

    A missing measurement is never silently dropped and never counted as zero.
    """
    known = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    unknown = len(values) - len(known)
    if not values or unknown:
        return {"n": len(values), "n_known": len(known), "n_unknown": unknown,
                "p50_ns": UNKNOWN, "p95_ns": UNKNOWN}
    return {"n": len(values), "n_known": len(known), "n_unknown": 0,
            "p50_ns": nearest_rank(known, 50), "p95_ns": nearest_rank(known, 95)}


def ms(value: Any) -> Any:
    return value if value == UNKNOWN else round(value / MS, 3)


# --------------------------------------------------------------------------
# /agent event analysis: what the user could see, and when
# --------------------------------------------------------------------------

def analyze_events(events: list[dict]) -> dict:
    """Turn a timed event list into timings and observations.

    Each item is {"t_ns": ns since the request was sent, "event": <SSE payload>}.

    * first_model_delta: first non-blank `delta` or `reasoning` event.
    * first_visible_answer: first non-blank answer `delta`/`text` after the most
      recent `clear_answer`. A preamble the loop retracts is not the answer.
    * total_completion: the first `done` event.
    * approval_boundary: the first `confirm` event.
    Agent-loop routes publish once at the end, so for them first_visible_answer
    and total_completion are close together. That is a measurement of the
    product, not a defect of the harness.
    """
    out: dict[str, Any] = {
        "first_event_ns": events[0]["t_ns"] if events else None,
        "routed_ns": None, "first_model_delta_ns": None, "first_visible_answer_ns": None,
        "total_completion_ns": None, "approval_boundary_ns": None,
        "saw_done": False, "session_id": None, "route": None,
        "tool_calls": [], "tool_results": [], "confirms": [], "errors": [],
        "statuses": [], "confirm_timeouts": 0, "clear_answers": 0, "delta_events": 0,
        "offered_tools": {}, "think_leak": False, "raw_model_io_calls": 0,
        "event_types": {},
    }
    visible = None
    deltas: list[str] = []
    final_text: str | None = None
    for record in events:
        t, event = record["t_ns"], record["event"]
        kind = event.get("type")
        out["event_types"][kind] = out["event_types"].get(kind, 0) + 1
        if kind == "session":
            out["session_id"] = event.get("id")
        elif kind == "routed":
            if out["routed_ns"] is None:
                out["routed_ns"] = t
                out["route"] = {k: event.get(k) for k in
                                ("direct_calls", "needs_tools", "role", "model", "source", "reason")}
        elif kind in ("delta", "reasoning"):
            text = event.get("text") or ""
            if text.strip() and out["first_model_delta_ns"] is None:
                out["first_model_delta_ns"] = t
            if kind == "delta":
                out["delta_events"] += 1
                deltas.append(text)
                if text.strip() and visible is None:
                    visible = t
        elif kind == "text":
            text = event.get("text") or ""
            if text.strip():
                final_text = text
                if visible is None:
                    visible = t
        elif kind == "clear_answer":
            out["clear_answers"] += 1
            deltas, final_text, visible = [], None, None
        elif kind == "tool_call":
            out["tool_calls"].append({"id": event.get("id"), "name": event.get("name"),
                                      "args": event.get("args") or {}})
        elif kind == "tool_result":
            out["tool_results"].append({"id": event.get("id"), "result": str(event.get("result", ""))})
        elif kind == "confirm":
            out["confirms"].append({"id": event.get("id"), "tool": event.get("tool"),
                                    "args": event.get("args") or {}, "t_ns": t})
            if out["approval_boundary_ns"] is None:
                out["approval_boundary_ns"] = t
        elif kind == "confirm_timeout":
            out["confirm_timeouts"] += 1
        elif kind == "error":
            out["errors"].append({"message": str(event.get("message", "")),
                                  "detail": str(event.get("detail", ""))})
        elif kind == "status":
            out["statuses"].append(str(event.get("text", "")))
        elif kind == "raw_model_io":
            out["raw_model_io_calls"] += 1
            request = event.get("request") or {}
            for schema in request.get("tools") or []:
                function = (schema or {}).get("function") or {}
                name = function.get("name")
                if name:
                    props = ((function.get("parameters") or {}).get("properties") or {})
                    out["offered_tools"][name] = sorted(props)
            response = event.get("response") or {}
            if isinstance(response, dict) and response.get("_think_leak"):
                out["think_leak"] = True
        elif kind == "done":
            if not out["saw_done"]:
                out["saw_done"] = True
                out["total_completion_ns"] = t
    out["first_visible_answer_ns"] = visible
    out["answer_text"] = final_text if final_text is not None else "".join(deltas)
    out["answer_buffered"] = out["delta_events"] == 0 and bool(out["answer_text"].strip())
    return out


def derive_metrics(spec: dict, analysis: dict, driver: dict) -> dict:
    """Gating metrics for one sample, in integer ns; missing ones are UNKNOWN."""
    if spec.get("kind") == "http":
        latency = driver.get("latency_ns")
        return {"request_latency_s": latency if isinstance(latency, int) else UNKNOWN}
    pick = lambda value: value if isinstance(value, int) else UNKNOWN
    return {"first_visible_answer_s": pick(analysis["first_visible_answer_ns"]),
            "first_model_delta_s": pick(analysis["first_model_delta_ns"]),
            "total_completion_s": pick(analysis["total_completion_ns"]),
            "approval_boundary_s": pick(analysis["approval_boundary_ns"])}


# --------------------------------------------------------------------------
# Instrumentation window -> counts. Missing instrumentation is UNKNOWN, never 0.
# --------------------------------------------------------------------------

_ENGINE_STATE_CHANGE = re.compile(r"/v1/models/[^/]+/(?:load|unload)$")
_INSTRUMENT_KINDS = {name: name for name in SURFACES}
_COUNT_NAMES = ("engine_http_calls", "chat_calls", "engine_state_changes", "authority_loads",
                "process_checks", "binding_checks", "peer_checks", "blocked_effects",
                "completion_reasons", "other_side_engine_requests", "instrumentation_records")


def instrument_counts(window_events: list[dict] | None, unavailable: list[str] | None,
                      other_window: list[dict] | None = None,
                      other_unavailable: list[str] | None = None) -> dict:
    """Counts for one sample window. `other_window` is the idle backend's window: it shares the engine,
    so any request it makes while this sample runs can contaminate the measurement."""
    if window_events is None or unavailable is None:
        return {name: UNKNOWN for name in _COUNT_NAMES}
    missing = set(unavailable)

    def count(kind: str, predicate: Callable[[dict], bool] = lambda e: True) -> Any:
        if _INSTRUMENT_KINDS.get(kind, kind) in missing:
            return UNKNOWN
        return sum(1 for e in window_events if e.get("kind") == kind and predicate(e))

    completions: Any = UNKNOWN
    if "engine_completion" not in missing:
        completions = [e.get("finish_reason") for e in window_events if e.get("kind") == "engine_completion"]
    other: Any = UNKNOWN
    if other_window is not None and other_unavailable is not None and "engine_http" not in set(other_unavailable):
        other = sum(1 for e in other_window if e.get("kind") == "engine_http" and e.get("method") != "GET")
    return {
        "engine_http_calls": count("engine_http"),
        "chat_calls": count("engine_http", lambda e: e.get("method") == "POST"
                            and str(e.get("path", "")).endswith("/chat/completions")),
        "engine_state_changes": count("engine_http", lambda e: e.get("method") == "POST"
                                      and bool(_ENGINE_STATE_CHANGE.search(str(e.get("path", ""))))),
        "authority_loads": count("authority_load"),
        "process_checks": count("process_check"),
        "binding_checks": count("binding_check"),
        "peer_checks": count("peer_check"),
        # The effect guard is the harness's own code, so it is always available.
        "blocked_effects": sum(1 for e in window_events if e.get("kind") == "effect_blocked"),
        # The finish_reason of every engine chat response, observed passively. UNKNOWN when the surface is missing.
        "completion_reasons": completions,
        "other_side_engine_requests": other,
        # A cost proxy for instrumentation itself (see docs, measurement boundary).
        "instrumentation_records": len(window_events),
    }


# --------------------------------------------------------------------------
# Grading. Correctness is decided BEFORE timing is considered.
# --------------------------------------------------------------------------

def _answer_checks(rules: dict, answer: str, corpus: dict, reasons: list[str]) -> None:
    folded = answer.casefold()
    if len(answer.strip()) < rules.get("min_chars", 1):
        reasons.append("answer_too_short")
    if "max_chars" in rules and len(answer) > rules["max_chars"]:
        reasons.append("answer_too_long")
    for needle in rules.get("all_of", []):
        if needle.casefold() not in folded:
            reasons.append(f"missing_fact:{needle}")
    if rules.get("any_of") and not any(n.casefold() in folded for n in rules["any_of"]):
        reasons.append("missing_any_fact")
    if rules.get("any_regex") and not any(re.search(p, answer, re.I) for p in rules["any_regex"]):
        reasons.append("missing_any_pattern")
    for needle in rules.get("forbidden_any", []):
        if needle.casefold() in folded:
            reasons.append(f"forbidden_content:{needle}")
    if rules.get("forbid_false_success"):
        for pattern in corpus["sentinels"]["false_success_patterns"]:
            if re.search(pattern, answer, re.I):
                reasons.append("false_success_claim")
                break


def _tool_checks(rules: Any, analysis: dict, corpus: dict, reasons: list[str]) -> None:
    calls, effects = analysis["tool_calls"], set(corpus.get("effect_tools") or [])
    if rules == "none":
        if calls:
            reasons.append("unexpected_tool_call:" + ",".join(c["name"] or "?" for c in calls))
        return
    if not isinstance(rules, dict):
        return
    for want in rules.get("must_include", []):
        matching = [c for c in calls if c["name"] == want["name"]]
        if not matching:
            reasons.append(f"missing_tool_call:{want['name']}")
            continue
        for arg, pattern in (want.get("arg_regex") or {}).items():
            if not any(isinstance(c["args"], dict) and re.search(pattern, str(c["args"].get(arg, "")))
                       for c in matching):
                reasons.append(f"tool_argument_mismatch:{want['name']}.{arg}")
    if rules.get("forbid_effect_tools"):
        bad = sorted({c["name"] for c in calls if c["name"] in effects})
        if bad:
            reasons.append("effect_tool_called:" + ",".join(bad))
    if rules.get("forbid_successful_effect_results"):
        ids = {c["id"]: c["name"] for c in calls if c["name"] in effects}
        for result in analysis["tool_results"]:
            if result["id"] in ids and "denied" not in result["result"].casefold():
                reasons.append(f"effect_result_without_denial:{ids[result['id']]}")


def _approval_checks(approval: dict, analysis: dict, driver: dict, reasons: list[str]) -> None:
    """The unapproved request must stop at the REAL approval boundary: one confirmation, one accepted denial
    bound to the same session and action, and a terminal tool result that says the action did not happen."""
    confirms = [c for c in analysis["confirms"] if c["tool"] == approval["tool"]]
    if not confirms:
        reasons.append("approval_boundary_not_reached")
    elif len(confirms) > 1:
        reasons.append("duplicate_confirmation")
    unexpected = [c for c in analysis["confirms"] if c["tool"] != approval["tool"]]
    if unexpected:
        reasons.append("unexpected_confirm:" + ",".join(str(c["tool"]) for c in unexpected))
    approvals = driver.get("approvals") or []
    if any(a.get("approved") is not False for a in approvals):
        reasons.append("harness_approved_an_effect")
    if len(confirms) != 1:
        return
    confirm = confirms[0]
    blob = json.dumps(confirm["args"], sort_keys=True)
    patterns = approval.get("args_any_regex") or []
    if patterns and not any(re.search(p, blob) for p in patterns):
        reasons.append("approval_arguments_mismatch")
    for group in approval.get("args_groups") or []:      # every group must be satisfied by at least one pattern
        if not any(re.search(p, blob) for p in group):
            reasons.append("approval_arguments_mismatch")
            break
    mine = [a for a in approvals if a.get("action_id") == confirm["id"]]
    if len(mine) != len(approvals):
        reasons.append("denial_for_another_action")
    if not mine:
        reasons.append("denial_not_recorded")
    elif len(mine) > 1:
        reasons.append("duplicate_denial")
    else:
        record = mine[0]
        if record.get("approved") is False and record.get("accepted") is not True:
            reasons.append("denial_not_accepted")
        if not analysis["session_id"] or record.get("session_id") != analysis["session_id"]:
            reasons.append("denial_session_mismatch")
    results = [r for r in analysis["tool_results"] if r["id"] == confirm["id"]]
    if not results:
        reasons.append("denial_result_missing")
    elif not any("denied" in r["result"].casefold() for r in results):
        reasons.append("denial_result_not_a_denial")


def _completion_checks(instrument: dict, expect: dict, sentinels: dict, reasons: list[str],
                       unverifiable: list[str]) -> None:
    """A terminal `done` proves the loop finished, not that the model's answer was complete. The engine's own
    finish_reason is observed passively; a length-terminated answer is a truncated answer."""
    chats = instrument.get("chat_calls", UNKNOWN)
    if chats == UNKNOWN or chats == 0:
        return          # an unknown call count is already unverifiable through the model_calls rule
    completions = instrument.get("completion_reasons", UNKNOWN)
    if completions == UNKNOWN:
        unverifiable.append("completion_status")
        return
    if len(completions) != chats:
        unverifiable.append("completion_status_incomplete")
    acceptable = set(sentinels.get("acceptable_finish_reasons") or [])
    for reason in completions:
        if reason in (None, ""):
            unverifiable.append("completion_status_missing")
        elif reason not in acceptable:
            reasons.append(f"incomplete_completion:{reason}")


def grade_sample(spec: dict, analysis: dict, driver: dict, instrument: dict, corpus: dict) -> dict:
    """Return {"correct", "reasons", "refusal", "unverifiable"} for one sample.

    `unverifiable` lists expectations that could not be checked because the
    needed instrumentation was missing. It is not a correctness failure, and a
    sample with unverifiable required checks can never be called correct.
    """
    reasons: list[str] = []
    unverifiable: list[str] = []
    refusal = False
    sentinels = corpus["sentinels"]
    expect = spec.get("expect") or {}

    if driver.get("deadline"):
        reasons.append("deadline_exceeded")
    if driver.get("cancelled"):
        reasons.append("cancelled")
    if driver.get("transport_error"):
        reasons.append("transport_error:" + str(driver["transport_error"])[:120])

    if spec.get("kind") == "http":
        http = driver.get("http") or {}
        rules = expect.get("http") or {}
        status = http.get("status")
        if status is None:
            reasons.append("no_http_response")
        elif status != rules.get("status", 200):
            reasons.append(f"http_status:{status}")
        if status == 503:
            refusal = True
        text = json.dumps(http.get("json"), sort_keys=True) if http.get("json") is not None else ""
        if any(p.casefold() in (http.get("text") or text).casefold() for p in sentinels["refusal_phrases"]):
            refusal = True
        if "json_equals" in rules and http.get("json") != rules["json_equals"]:
            reasons.append("http_body_mismatch")
        if refusal:
            reasons.append("refused")
    else:
        answer = analysis["answer_text"]
        if not analysis["saw_done"]:
            reasons.append("no_terminal_done")
        for error in analysis["errors"]:
            reasons.append("error_event")
            blob = (error["message"] + " " + error["detail"]).casefold()
            if any(p.casefold() in blob for p in sentinels["refusal_phrases"]):
                refusal = True
        if refusal:
            reasons.append("refused")
        if analysis["confirm_timeouts"]:
            reasons.append("confirm_timeout")
        if not answer.strip():
            reasons.append("empty_answer")
        for phrase in sentinels["failure_phrases"]:
            if phrase.casefold() in answer.casefold():
                reasons.append("failure_message_as_answer")
                refusal = refusal or any(phrase.casefold() == p.casefold() for p in sentinels["refusal_phrases"])
                break
        if analysis["think_leak"]:
            reasons.append("truncated_reasoning_leak")
        if expect.get("streams") and analysis["delta_events"] == 0:
            reasons.append("expected_streaming_absent")

        _answer_checks(expect.get("answer") or {}, answer, corpus, reasons)
        _tool_checks(expect.get("tool_calls"), analysis, corpus, reasons)

        route_rules = expect.get("route")
        if route_rules:
            route = analysis["route"]
            if route is None:
                reasons.append("route_event_missing")
            else:
                direct = route.get("direct_calls") or []
                if route_rules.get("direct_calls") == "none" and direct:
                    reasons.append("route_used_direct_call")
                if route_rules.get("direct_calls") == "nonempty" and not direct:
                    reasons.append("route_has_no_direct_call")
                if "needs_tools" in route_rules and bool(route.get("needs_tools")) != route_rules["needs_tools"]:
                    reasons.append("route_needs_tools_mismatch")

        offered = expect.get("offered_tools")
        if offered:
            if analysis["raw_model_io_calls"] == 0:
                unverifiable.append("offered_tools")
            else:
                for want in offered.get("must_include", []):
                    props = analysis["offered_tools"].get(want["name"])
                    if props is None:
                        reasons.append(f"tool_not_offered:{want['name']}")
                    elif not set(want.get("schema_properties") or []) <= set(props):
                        reasons.append(f"tool_schema_incomplete:{want['name']}")

        approval = expect.get("approval")
        if approval:
            _approval_checks(approval, analysis, driver, reasons)
        elif analysis["confirms"]:
            reasons.append("unexpected_confirm:" + ",".join(str(c["tool"]) for c in analysis["confirms"]))
        elif driver.get("approvals"):
            reasons.append("approval_without_a_confirm")

        calls_rule = expect.get("model_calls")
        if calls_rule:
            chats = instrument.get("chat_calls", UNKNOWN)
            if chats == UNKNOWN:
                unverifiable.append("model_calls")
            else:
                if "exactly" in calls_rule and chats != calls_rule["exactly"]:
                    reasons.append(f"model_calls:{chats}!=exactly_{calls_rule['exactly']}")
                if "min" in calls_rule and chats < calls_rule["min"]:
                    reasons.append(f"model_calls:{chats}<min_{calls_rule['min']}")

    if spec.get("kind") != "http":
        _completion_checks(instrument, expect, sentinels, reasons, unverifiable)
    other = instrument.get("other_side_engine_requests", UNKNOWN)
    if other == UNKNOWN:
        unverifiable.append("other_side_engine_activity")
    elif other:
        unverifiable.append("shared_engine_interference")
    if instrument.get("engine_state_changes") not in (0, UNKNOWN):
        reasons.append("engine_state_change")
    if instrument.get("blocked_effects") not in (0, UNKNOWN, None):
        reasons.append("blocked_effect_attempt")
    reasons = sorted(set(reasons))
    return {"correct": not reasons and not unverifiable, "reasons": reasons,
            "refusal": refusal, "unverifiable": sorted(set(unverifiable))}


# --------------------------------------------------------------------------
# Schedule: warmups first and labelled, then measured repetitions in
# alternating order so drift cannot line up with one side.
# --------------------------------------------------------------------------

def build_schedule(scenario_ids: list[str], variant_counts: dict[str, int],
                   warmups: int, measured: int) -> list[dict]:
    items: list[dict] = []
    for phase, count in (("warmup", warmups), ("measured", measured)):
        for rep in range(1, count + 1):
            order = list(SIDES) if rep % 2 == 1 else list(reversed(SIDES))
            for scenario in scenario_ids:
                items.append({"phase": phase, "rep": rep, "scenario": scenario,
                              "variant": (rep - 1) % variant_counts[scenario], "order": order})
    return items


def verify_alternation(samples: list[dict], scenario_ids: list[str], warmups: int,
                       measured: int) -> list[str]:
    """The recorded samples must follow the schedule exactly."""
    problems: list[str] = []
    index: dict[tuple, dict] = {}
    for sample in samples:
        key = (sample["scenario"], sample["phase"], sample["rep"], sample["side"])
        if key in index:
            problems.append(f"duplicate sample {key}")
        index[key] = sample
    expected_keys = set()
    for phase, count in (("warmup", warmups), ("measured", measured)):
        for scenario in scenario_ids:
            for rep in range(1, count + 1):
                first = SIDES[0] if rep % 2 == 1 else SIDES[1]
                second = SIDES[1] if rep % 2 == 1 else SIDES[0]
                pair = []
                for slot, side in enumerate((first, second)):
                    key = (scenario, phase, rep, side)
                    expected_keys.add(key)
                    sample = index.get(key)
                    if sample is None:
                        problems.append(f"missing sample {key}")
                        continue
                    if sample.get("order_slot") != slot:
                        problems.append(f"order violates alternation {key}")
                    pair.append(sample)
                if len(pair) == 2:
                    if pair[0].get("variant") != pair[1].get("variant"):
                        problems.append(f"pair used different variants {(scenario, phase, rep)}")
                    if not pair[0].get("started_ns", 0) < pair[1].get("started_ns", 0):
                        problems.append(f"pair not run in recorded order {(scenario, phase, rep)}")
    for key in index:
        if key not in expected_keys:
            problems.append(f"unexpected sample {key}")
    return problems


# --------------------------------------------------------------------------
# Subject identity: derived by the harness from git, never accepted from a caller
# --------------------------------------------------------------------------

def run_cmd(argv: list[str], cwd: str | Path | None = None, timeout: float = 30,
            env: dict | None = None) -> tuple[int, str, str]:
    full_env = dict(os.environ if env is None else env)
    full_env["GIT_OPTIONAL_LOCKS"] = "0"  # a status read must not rewrite the index
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=full_env)
    return done.returncode, done.stdout, done.stderr


def verify_subject(root: Path | str, expected_sha: str, *, runner: Callable = run_cmd,
                   clock: Callable[[], float] = utc_now) -> dict:
    root = Path(root).resolve()
    record: dict[str, Any] = {"root": str(root), "expected": expected_sha, "verified_at": iso(clock()),
                              "verified_at_ts": clock(), "head": None, "tree": None, "toplevel": None,
                              "clean": False, "dirty_entries": [], "matches_expected": False,
                              "problems": []}
    if not FULL_SHA.fullmatch(expected_sha or ""):
        record["problems"].append("expected sha is not a full 40-character lowercase hex id")

    def git(*args: str) -> tuple[int, str]:
        code, out, _ = runner(["git", *args], cwd=str(root))
        return code, out.strip()

    code, head = git("rev-parse", "HEAD")
    if code:
        record["problems"].append("not a git worktree")
        return record
    record["head"] = head
    record["tree"] = git("rev-parse", "HEAD^{tree}")[1]
    record["toplevel"] = git("rev-parse", "--show-toplevel")[1]
    code, status = runner(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=str(root))[:2]
    entries = [line for line in status.splitlines() if line]
    record["clean"] = code == 0 and not entries
    record["dirty_entries"] = entries[:20]
    record["matches_expected"] = head == expected_sha
    if record["toplevel"] and Path(record["toplevel"]).resolve() != root:
        record["problems"].append("path is not the worktree root")
    if not record["clean"]:
        record["problems"].append("worktree is not clean")
    if not record["matches_expected"]:
        record["problems"].append("HEAD does not equal the expected sha")
    return record


def subject_ok(record: dict) -> bool:
    return bool(record.get("clean") and record.get("matches_expected") and not record.get("problems"))


# --------------------------------------------------------------------------
# Environment identity, collected from the machine for a live run
# --------------------------------------------------------------------------

_GENERATION_KEYS = ("temperature", "top_p", "top_k", "repetition_penalty", "min_p", "presence_penalty",
                    "max_tokens", "max_context_window", "enable_thinking", "thinking_budget_enabled",
                    "thinking_budget_tokens", "force_sampling", "turboquant_kv_enabled",
                    "turboquant_kv_bits", "active_profile_name")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def find_model_dir(model_dirs: list[str], model_id: str) -> Path | None:
    for base in model_dirs:
        base_path = Path(base).expanduser()
        for candidate in [base_path / model_id, *sorted(base_path.glob(f"*/{model_id}"))]:
            if (candidate / "config.json").is_file():
                return candidate
    return None


def collect_environment(model_id: str, lane: str, warmups: int, *, runner: Callable = run_cmd,
                        home: Path | None = None, hash_weights: bool = True,
                        plist: Path = Path("/Applications/oMLX.app/Contents/Info.plist")) -> dict:
    """Identity of everything that can move a latency number besides the code.

    Reads files and runs read-only commands. Secrets are never copied: only
    whitelisted keys of the engine settings are kept. Anything that cannot be
    determined is UNKNOWN, which makes the cohort unusable rather than guessed.
    """
    home = Path(home) if home else Path.home()

    def cmd(*argv: str) -> str:
        try:
            code, out, _ = runner(list(argv), timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return UNKNOWN
        return out.strip() if code == 0 and out.strip() else UNKNOWN

    settings = _read_json(home / ".omlx" / "settings.json") or {}
    model_settings = ((_read_json(home / ".omlx" / "model_settings.json") or {}).get("models") or {}).get(model_id)
    model_dir = find_model_dir((settings.get("model") or {}).get("model_dirs") or [], model_id)
    config = _read_json(model_dir / "config.json") if model_dir else None
    weights = []
    if model_dir:
        # Names and sizes cannot tell a same-named, same-sized artifact from a replaced one, so the policy
        # identity is the full content digest of every weight file. UNKNOWN (never skipped) when not hashed.
        weights = [[p.name, p.stat().st_size, sha256_file(p) if hash_weights else UNKNOWN]
                   for p in sorted(model_dir.glob("*.safetensors"))]
    try:
        import plistlib
        info = plistlib.loads(Path(plist).read_bytes())
        app_version = {"short": info.get("CFBundleShortVersionString", UNKNOWN),
                       "build": info.get("CFBundleVersion", UNKNOWN)}
    except (OSError, ValueError):
        app_version = {"short": UNKNOWN, "build": UNKNOWN}
    battery = cmd("pmset", "-g", "batt").splitlines()[0] if cmd("pmset", "-g", "batt") != UNKNOWN else UNKNOWN
    manifest = home / ".moe" / "omlx-runtime-authorization.json"
    return {
        "hardware": {"model": cmd("sysctl", "-n", "hw.model"),
                     "chip": cmd("sysctl", "-n", "machdep.cpu.brand_string"),
                     "memory_bytes": cmd("sysctl", "-n", "hw.memsize"),
                     "cpu_count": cmd("sysctl", "-n", "hw.ncpu")},
        "os": {"product_version": cmd("sw_vers", "-productVersion"),
               "build": cmd("sw_vers", "-buildVersion"), "kernel": cmd("uname", "-r")},
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "engine": {"name": "oMLX", "app": app_version, "port": 8000},
        "model": {"id": model_id,
                  "dir_found": bool(model_dir),
                  "config_sha256": sha256_file(model_dir / "config.json") if model_dir else UNKNOWN,
                  "tokenizer_sha256": (sha256_file(model_dir / "tokenizer.json")
                                       if model_dir and (model_dir / "tokenizer.json").is_file() else UNKNOWN),
                  "quantization": (config or {}).get("quantization") or (config or {}).get("quantization_config") or UNKNOWN,
                  "weights_manifest": {"files": weights,
                                       "sha256": canonical_sha(weights) if weights else UNKNOWN,
                                       "note": "name, size and full SHA-256 of every weight file"}},
        "generation_settings": {
            "model_settings": ({k: model_settings.get(k) for k in _GENERATION_KEYS if k in model_settings}
                               if isinstance(model_settings, dict) else UNKNOWN),
            "engine_sampling_defaults": settings.get("sampling") or UNKNOWN,
            "request_settings": "not declared here: the sampling fields of every engine chat request are "
                                "observed per call and compared across sides (request_identity)"},
        "cache_warmup_treatment": {
            "engine_cache_settings": settings.get("cache") or UNKNOWN,
            "cache_cleared_before_run": False,
            "warmup_samples_per_scenario_per_side": warmups,
            "variants_rotate_across_repetitions": True},
        "power": battery,
        "lane_evidence": {"managed_authorization_manifest_present": manifest.exists()},
    }


def cohort_key(receipt: dict) -> dict:
    """What must match for two measurements to be comparable. The Wisp commit is deliberately NOT part of
    it: candidate and baseline are different subjects by design."""
    environment = receipt.get("environment") or {}
    return {"lane": receipt.get("lane"), "execution_mode": receipt.get("execution_mode"),
            "hardware": environment.get("hardware"), "os": environment.get("os"),
            "engine": environment.get("engine"), "model": environment.get("model"),
            "generation_settings": environment.get("generation_settings"),
            "cache_warmup_treatment": environment.get("cache_warmup_treatment"),
            "child_runtime": receipt.get("runtime_identity"),
            "containment": receipt.get("containment"),
            "harness_sha256": (receipt.get("harness") or {}).get("script_sha256"),
            "corpus_sha256": (receipt.get("corpus") or {}).get("sha256"),
            "policy_sha256": (receipt.get("policy") or {}).get("sha256")}


def has_unknown(value: Any) -> bool:
    if value == UNKNOWN:
        return True
    if isinstance(value, dict):
        return any(has_unknown(v) for v in value.values())
    if isinstance(value, list):
        return any(has_unknown(v) for v in value)
    return False


# --------------------------------------------------------------------------
# Synthetic connector leaves: the same payloads the native readers push, rendered
# from the fictional fixture. They are posted to the backend's real sync endpoints.
# --------------------------------------------------------------------------

def thread_context(name: str | None, members: list[str], is_group: bool) -> str:
    name = (name or "").strip()
    if name:
        return f'Group "{name}"' if is_group else name
    if is_group:
        shown = sorted(members)[:4]
        more = f", +{len(members) - 4} more" if len(members) > 4 else ""
        return f"Group of {len(members)} ({', '.join(shown)}{more})"
    return members[0] if members else "Unknown"


def messages_lines(rows: list[dict]) -> str:
    lines = []
    for r in sorted(rows, key=lambda r: r["ts"], reverse=True):
        text = str(r["text"]).replace("\r", " ").replace("\n", " ")
        lines.append(f"{r['ts']} | {r['context']} | {r['who']}: {text}")
    return "\n".join(lines)


def notes_raw(rows: list[dict]) -> str:
    return "".join("\x01".join([str(n["ts"]), n["title"], n.get("folder", ""), n["body"]]) + "\x02"
                   for n in rows)


def render_leaf_payloads(corpus: dict, now_ts: float) -> list[dict]:
    """[{"path", "body"}] in the order they must be posted."""
    leaf = corpus["leaf_data"]
    rows = []
    for message in leaf["messages"]:
        group = message["kind"] == "group"
        rows.append({"ts": int(now_ts - message["age_s"]),
                     "context": thread_context(message["thread"] if group else None,
                                               message["participants"], group),
                     "who": "Me" if message["from"] == "me" else message["from"],
                     "text": message["text"]})
    lines = messages_lines(rows)
    notes = [{"ts": int(now_ts - n["age_s"]), "title": n["title"], "folder": n.get("folder", ""),
              "body": n["body"]} for n in leaf["notes"]]
    return [
        {"path": "/assistant/sync/messages", "body": {"contacts": dict(leaf["contacts"])}},
        {"path": "/assistant/sync/messages",
         "body": {"lines": lines, "diagnostics": {"available": True, "reason": "",
                                                  "count": lines.count("\n") + 1 if lines else 0}}},
        {"path": "/assistant/sync/notes", "body": {"raw": notes_raw(notes)}},
    ]


# --------------------------------------------------------------------------
# Effect guard. Runs inside the spawned backend only, never in the harness.
# --------------------------------------------------------------------------

class EffectBlocked(PermissionError):
    pass


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# The engine operations a benchmarked Wisp may issue. Everything else sent to the engine port (a model load or
# unload, a settings write, an admin call) is refused BEFORE it is transmitted, and recorded.
ENGINE_ALLOWED_OPS = (("GET", r"/health"), ("GET", r"/v1/models"), ("GET", r"/v1/models/status"),
                      ("POST", r"/v1/chat/completions"), ("POST", r"/v1/embeddings"), ("POST", r"/v1/rerank"))

# The exact argument shapes of the read-only inspectors the engine attribution runs at the base commit. A
# program name alone is not a capability: lsof and ps can each do more than inspect.
EXEC_GRAMMAR: dict[str, tuple[str, ...]] = {
    "/usr/sbin/lsof": (r"-nP -a -iTCP:[0-9]{1,5} -sTCP:LISTEN -Fpufn",
                       r"-nP -a -p [1-9][0-9]* -d txt -Fn",
                       r"-nP -a -iTCP -sTCP:LISTEN -Fpufn",
                       r"-nP -a -iTCP:[0-9]{1,5} -sTCP:ESTABLISHED -FpufPtTn -Ts"),
    "/bin/ps": (r"-ww -p [1-9][0-9]* -o ppid=,uid=,comm=",),
}
ALLOWED_EXEC = frozenset(EXEC_GRAMMAR)


class FsPolicy:
    """Where the spawned backend may read and write.

    Writes are allowed only inside the throwaway WISP_HOME (which also holds TMPDIR), plus the one credential
    lease file the product itself creates. Under the real HOME, reads are limited to the exact configuration
    inputs the attributed engine path needs (the engine settings and authorization record) and to the code and
    interpreter the child runs. Everything else under the real HOME is refused and recorded. Python-level
    only: native code that opens files itself is outside this boundary.
    """

    def __init__(self, *, home: str | os.PathLike, read_roots=(), write_roots=(), read_exact=(),
                 write_exact=(), mkdir_exact=(), labels: dict | None = None) -> None:
        self.labels = dict(labels or {})
        self.home = self.absolute(home)
        self.read_roots = self._both(read_roots)
        self.write_roots = self._both(write_roots)
        self.read_exact = self._both(read_exact)
        self.write_exact = self._both(write_exact)
        self.mkdir_exact = self._both(mkdir_exact)

    @staticmethod
    def absolute(path: str | os.PathLike) -> str:
        return os.path.normpath(os.path.join(os.getcwd(), os.fsdecode(os.fspath(path))))

    @classmethod
    def _both(cls, paths) -> tuple[str, ...]:
        out: list[str] = []
        for path in paths:
            absolute = cls.absolute(path)
            for form in (absolute, os.path.realpath(absolute)):
                if form not in out:
                    out.append(form)
        return tuple(out)

    @staticmethod
    def _under(path: str, roots) -> bool:
        return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)

    def read_allowed(self, path: str) -> bool:
        path = os.path.realpath(path)
        if not self._under(path, [os.path.realpath(self.home)]):
            return True
        return (self._under(path, self.read_roots) or self._under(path, self.write_roots)
                or path in self.read_exact or path in self.write_exact)

    def write_allowed(self, path: str) -> bool:
        real = os.path.realpath(path)
        return (real == "/dev/null" or self._under(real, self.write_roots) or real in self.write_exact
                or path in self.write_exact)

    def mkdir_allowed(self, path: str) -> bool:
        return self.write_allowed(path) or path in self.mkdir_exact or os.path.realpath(path) in self.mkdir_exact

    def portable(self) -> dict:
        """The policy WITHOUT per-run paths, so two children of one run (different worktree roots) have the
        same containment identity and a different policy has a different one."""
        def relative(paths) -> list[str]:
            return sorted({("~" + path[len(self.home):]) if path == self.home or path.startswith(self.home + os.sep)
                           else path for path in paths})
        return {"real_home_denied_by_default": True, "read_roots": self.labels.get("read_roots", []),
                "write_roots": self.labels.get("write_roots", []), "read_exact": relative(self.read_exact),
                "write_exact": relative(self.write_exact), "mkdir_exact": relative(self.mkdir_exact)}

    def describe(self) -> dict:
        return {"home": self.home, "read_roots": list(self.read_roots), "write_roots": list(self.write_roots),
                "read_exact": list(self.read_exact), "write_exact": list(self.write_exact),
                "mkdir_exact": list(self.mkdir_exact)}


def build_fs_policy(root: Path, home: Path, *, real_home: Path | None = None) -> FsPolicy:
    real_home = Path(real_home) if real_home else Path.home()
    interpreter = [sys.prefix, sys.base_prefix, sys.exec_prefix]
    # Arbitrary sys.path entries are not read capabilities (a test/plugin or candidate
    # can add HOME or a broad scratch parent). Only explicit interpreter prefixes qualify.
    return FsPolicy(
        home=real_home,
        read_roots=[root, *interpreter],
        write_roots=[home],
        read_exact=[real_home / ".omlx" / "settings.json", real_home / ".omlx" / "model_settings.json",
                    real_home / ".moe" / "omlx-runtime-authorization.json",
                    real_home / ".moe" / ".credential-generation",
                    real_home / ".moe" / ".helper-transaction.json"],
        write_exact=[real_home / ".moe" / ".provisioning.lock"],   # the credential lease the product creates
        mkdir_exact=[real_home / ".moe"],
        labels={"read_roots": ["child_worktree", "interpreter_prefixes"],
                "write_roots": ["throwaway_wisp_home"]})


_FS_FUNCS: dict[str, tuple[str, tuple[tuple[int, str], ...]]] = {
    "mkdir": ("mkdir", ((0, "path"),)),
    "rmdir": ("write", ((0, "path"),)), "remove": ("write", ((0, "path"),)), "unlink": ("write", ((0, "path"),)),
    "truncate": ("write", ((0, "path"),)), "chmod": ("write", ((0, "path"),)), "chown": ("write", ((0, "path"),)),
    "utime": ("write", ((0, "path"),)),
    "rename": ("write", ((0, "src"), (1, "dst"))), "replace": ("write", ((0, "src"), (1, "dst"))),
    "link": ("write", ((0, "src"), (1, "dst"))), "symlink": ("write", ((1, "dst"),)),
    "listdir": ("read", ((0, "path"),)), "scandir": ("read", ((0, "path"),)),
}


class EffectGuard:
    """Refuses real-world effects while leaving the attribution path intact.

    Blocks, before they happen: any process whose complete argument vector is not one of the read-only
    inspector shapes (and any shell or executable override); any connection except loopback to the engine
    port; any engine request that is not an explicitly permitted inference or status operation; any other
    HTTP request; and, when a policy is given, filesystem reads and writes outside it. Every refusal is
    recorded. Raw sockets or files opened by a C extension are not interceptable from Python.
    """

    def __init__(self, emit: Callable[..., None], *,
                 exec_grammar: dict[str, tuple[str, ...] | None] | None = None,
                 allowed_ports: set[int] | frozenset[int] = frozenset({8000}),
                 engine_ops: tuple[tuple[str, str], ...] = ENGINE_ALLOWED_OPS,
                 fs: FsPolicy | None = None) -> None:
        self.emit, self.allowed_ports = emit, set(allowed_ports)
        self.exec_grammar = dict(EXEC_GRAMMAR if exec_grammar is None else exec_grammar)
        self.engine_ops, self.fs = tuple(engine_ops), fs
        self._originals: list[tuple[Any, str, Any]] = []
        import contextvars
        # Task-local and reset after each reviewed HTTP operation. This is a Python guard,
        # not protection against malicious code recovering the originals or native I/O.
        self._http_operation = contextvars.ContextVar("release_http_operation", default=None)

    def policy_description(self) -> dict:
        return {"exec": {k: (list(v) if v is not None else None) for k, v in sorted(self.exec_grammar.items())},
                "engine_ops": [list(op) for op in self.engine_ops], "ports": sorted(self.allowed_ports),
                "fs": self.fs.portable() if self.fs else None}

    def policy_sha256(self) -> str:
        return canonical_sha(self.policy_description())

    def _block(self, what: str, target: str) -> None:
        self.emit("effect_blocked", what=what, target=target)
        raise EffectBlocked(f"blocked {what}: {target}")

    def _patch(self, owner: Any, name: str, replacement: Any) -> None:
        self._originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def _check_address(self, address: Any, what: str) -> None:
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
            if host in LOCAL_HOSTS and port in self.allowed_ports:
                return
            self._block(what, f"{host}:{port}")
        self._block(what, repr(address)[:80])

    def _check_engine_op(self, method: str, path: str) -> None:
        method = str(method).upper()
        if not any(method == m and re.fullmatch(pattern, path) for m, pattern in self.engine_ops):
            self._block("engine_operation", f"{method} {path}"[:160])

    def _check_exec(self, bound: dict) -> None:
        argv_in = bound.get("args")
        argv = [argv_in] if isinstance(argv_in, (str, bytes, os.PathLike)) else list(argv_in or [])
        first = os.fsdecode(argv[0]) if argv else ""
        if bound.get("shell"):
            self._block("exec", f"shell=True {first}"[:120])
        executable = bound.get("executable")
        if executable is not None and os.fsdecode(executable) != first:
            self._block("exec", f"executable override {os.fsdecode(executable)} for {first}"[:160])
        if first not in self.exec_grammar:
            self._block("exec", first or "<empty>")
        patterns = self.exec_grammar[first]
        rest = " ".join(os.fsdecode(a) for a in argv[1:])
        if patterns is not None and not any(re.fullmatch(p, rest) for p in patterns):
            self._block("exec", f"{first} {rest}"[:160])

    def _install_fs(self) -> None:
        import builtins
        import io
        fs, guard = self.fs, self
        descriptors: dict[int, tuple[str, tuple[int, ...]]] = {}
        lease_paths = set(fs.write_exact)
        original_fstat = os.fstat

        def identity(info):
            return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_rdev)

        def check(kind: str, value: Any, *, file_object=False) -> str:
            if isinstance(value, int):
                tracked = descriptors.get(value)
                if tracked is None:
                    guard._block("fs_descriptor", str(value))
                path, expected = tracked
                try:
                    info = original_fstat(value)
                except OSError:
                    descriptors.pop(value, None)
                    guard._block("fs_descriptor_stale", str(value))
                # io.FileIO/BufferedIO close in C, and dup2 can replace an FD:
                # a numeric descriptor is never a durable path capability.
                if identity(info) != expected:
                    descriptors.pop(value, None)
                    guard._block("fs_descriptor_stale", str(value))
                regular = stat.S_ISREG(info.st_mode)
                null_device = os.path.realpath(path) == "/dev/null" and stat.S_ISCHR(info.st_mode)
                if (kind != "read" or file_object) and not (regular or null_device):
                    guard._block("fs_descriptor_type", str(value))
                value = path
            try:
                path = fs.absolute(value)
            except (TypeError, ValueError):
                guard._block("fs_path", repr(value)[:80])
            real = os.path.realpath(path)
            # The only exception is acquisition of the product's empty credential lock,
            # through the exact os.open grammar below. No data/metadata mutation is allowed.
            if kind != "read" and (path in lease_paths or real in lease_paths):
                guard._block("fs_lease_mutation", path)
            allowed = {"write": fs.write_allowed, "mkdir": fs.mkdir_allowed}.get(kind, fs.read_allowed)(path)
            if not allowed:
                guard._block("fs_read" if kind == "read" else "fs_write", path)
            return path

        original_open = builtins.open

        def guarded_open(file, mode="r", *a, **k):
            check("write" if any(c in str(mode) for c in "wax+") else "read", file, file_object=True)
            return original_open(file, mode, *a, **k)

        self._patch(builtins, "open", guarded_open)
        self._patch(io, "open", guarded_open)
        original_os_open, original_close = os.open, os.close
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
            absolute = fs.absolute(path)
            if dir_fd is not None:
                guard._block("fs_dir_fd", absolute)
            if absolute in lease_paths:
                wanted = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
                if flags != wanted or mode != 0o600 or os.path.realpath(absolute) not in lease_paths:
                    guard._block("fs_lease_open", absolute)
            else:
                check("write" if flags & write_flags else "read", path)
            fd = original_os_open(path, flags, mode)
            try:
                descriptors[fd] = (absolute, identity(original_fstat(fd)))
            except BaseException:
                original_close(fd)
                raise
            return fd

        def close(fd):
            try:
                return original_close(fd)
            finally:
                descriptors.pop(fd, None)

        self._patch(os, "open", guarded_os_open)
        self._patch(os, "close", close)
        for name in ("write", "pwrite", "writev", "ftruncate", "fchmod", "fchown"):
            if not hasattr(os, name):
                continue
            original = getattr(os, name)
            def make_fd(original):
                def guarded(fd, *args, **kwargs):
                    check("write", fd)
                    return original(fd, *args, **kwargs)
                return guarded
            self._patch(os, name, make_fd(original))
        for name, (kind, slots) in _FS_FUNCS.items():
            original = getattr(os, name, None)
            if original is None:
                continue
            def make(original, kind, slots):
                def guarded(*args, **kwargs):
                    if any(kwargs.get(k) is not None for k in ("dir_fd", "src_dir_fd", "dst_dir_fd")):
                        guard._block("fs_dir_fd", original.__name__)
                    for position, keyword in slots:
                        value = args[position] if len(args) > position else kwargs.get(keyword, ".")
                        check(kind, value)
                    return original(*args, **kwargs)
                return guarded
            self._patch(os, name, make(original, kind, slots))

    def install(self) -> "EffectGuard":
        import inspect
        import socket
        import subprocess as sp
        guard = self
        original_init = sp.Popen.__init__
        signature = inspect.signature(original_init)

        def popen_init(self_, *args, **kwargs):
            try:
                arguments = signature.bind(self_, *args, **kwargs).arguments
            except TypeError:
                guard._block("exec", "unparseable Popen call")
            bound: dict[str, Any] = {}
            for name, value in arguments.items():
                kind = signature.parameters[name].kind
                if kind is inspect.Parameter.VAR_KEYWORD:
                    bound.update(value)
                elif kind is not inspect.Parameter.VAR_POSITIONAL:
                    bound[name] = value
            guard._check_exec(bound)
            return original_init(self_, *args, **kwargs)

        original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

        def connect(self_, address):
            guard._check_address(address, "connect")
            return original_connect(self_, address)

        def connect_ex(self_, address):
            guard._check_address(address, "connect")
            return original_connect_ex(self_, address)

        self._patch(sp.Popen, "__init__", popen_init)
        self._patch(socket.socket, "connect", connect)
        self._patch(socket.socket, "connect_ex", connect_ex)
        for name in ("send", "sendall", "sendto", "sendmsg", "sendfile"):
            if not hasattr(socket.socket, name):
                continue
            original = getattr(socket.socket, name)
            def make_send(original, name):
                def guarded(self_, *args, **kwargs):
                    if name == "sendfile":
                        guard._block("raw_socket_send", name)
                    if name == "sendto":
                        address = args[-1] if args else kwargs.get("address")
                        guard._check_address(address, "sendto")
                        guard._block("raw_socket_send", name)
                    if name == "sendmsg" and (len(args) > 3 or kwargs.get("address") is not None):
                        guard._block("raw_socket_send", name)
                    try:
                        peer = self_.getpeername()
                    except OSError:
                        guard._block("raw_socket_send", name)
                    if isinstance(peer, tuple) and peer[1] in guard.allowed_ports:
                        if guard._http_operation.get() != (peer[0], peer[1]):
                            guard._block("raw_socket_send", name)
                    return original(self_, *args, **kwargs)
                return guarded
            self._patch(socket.socket, name, make_send(original, name))
        # Reviewed JSON inference/status operations never transfer file bytes.
        # These Python APIs can bypass socket.send/sendall, even with no fs policy.
        for name in ("sendfile", "splice"):
            if hasattr(os, name):
                self._patch(os, name, lambda *a, _n=name, **k: guard._block("raw_descriptor_transfer", f"os.{_n}"))
        for name in ("system", "posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe", "execl",
                     "execle", "execlp", "execlpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
                     "spawnve", "spawnvp", "spawnvpe", "fork", "forkpty"):
            if hasattr(os, name):
                self._patch(os, name, lambda *a, _n=name, **k: guard._block("exec", f"os.{_n}"))
        import http.client
        original_putrequest = http.client.HTTPConnection.putrequest

        def putrequest(self_, method, url, *a, **k):
            if self_.host not in LOCAL_HOSTS or self_.port not in guard.allowed_ports:
                guard._block("http", f"{self_.host}:{self_.port}")
            guard._check_engine_op(method, str(url).split("?", 1)[0])
            self_._release_operation = ("::1" if self_.host == "::1" else "127.0.0.1", self_.port)
            return original_putrequest(self_, method, url, *a, **k)

        self._patch(http.client.HTTPConnection, "putrequest", putrequest)
        original_endheaders = http.client.HTTPConnection.endheaders
        def endheaders(self_, *args, **kwargs):
            operation = getattr(self_, "_release_operation", None)
            if operation is None:
                guard._block("raw_socket_send", "http.client without reviewed request")
            token = guard._http_operation.set(operation)
            try:
                return original_endheaders(self_, *args, **kwargs)
            finally:
                guard._http_operation.reset(token)
                self_._release_operation = None
        self._patch(http.client.HTTPConnection, "endheaders", endheaders)
        if self.fs is not None:
            self._install_fs()
        try:
            import httpx
        except ImportError:
            return self
        for klass in (httpx.AsyncClient, httpx.Client):
            original_send = klass.send

            def make(original, is_async):
                def check(request):
                    host, port = request.url.host, request.url.port or (443 if request.url.scheme == "https" else 80)
                    if host not in LOCAL_HOSTS or port not in guard.allowed_ports:
                        guard._block("http", f"{host}:{port}")
                    guard._check_engine_op(request.method, request.url.path)
                    # Socket peers normalize localhost to an address.
                    return ("::1" if host == "::1" else "127.0.0.1", port)
                if is_async:
                    async def send(self_, request, *a, **k):
                        token = guard._http_operation.set(check(request))
                        try:
                            return await original(self_, request, *a, **k)
                        finally:
                            guard._http_operation.reset(token)
                else:
                    def send(self_, request, *a, **k):
                        token = guard._http_operation.set(check(request))
                        try:
                            return original(self_, request, *a, **k)
                        finally:
                            guard._http_operation.reset(token)
                return send

            self._patch(klass, "send", make(original_send, klass is httpx.AsyncClient))
        return self

    def uninstall(self) -> None:
        for owner, name, original in reversed(self._originals):
            setattr(owner, name, original)
        self._originals.clear()

    def __enter__(self) -> "EffectGuard":
        return self.install()

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()


# --------------------------------------------------------------------------
# Delegating instrumentation. Wraps; never alters arguments, results or errors.
# --------------------------------------------------------------------------

class InstrumentRecorder:
    """Append-only, line-buffered JSONL log of one backend's whole lifetime.

    Every record carries a gap-free sequence number and a timestamp taken under the same lock, so the file
    is totally ordered and a missing record is detectable from the file alone."""

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._file = open(path, "a", buffering=1, encoding="utf-8")

    def emit(self, kind: str, **fields: Any) -> None:
        with self._lock:
            record = {"kind": kind, "seq": self._seq, "t_ns": time.monotonic_ns(), "pid": os.getpid(), **fields}
            self._seq += 1
            self._file.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def close(self) -> None:
        with self._lock:
            self._file.close()


def _wrap_sync(recorder: InstrumentRecorder, kind: str, fn: Callable, describe: Callable) -> Callable:
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic_ns()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            recorder.emit(kind, t_start_ns=start, dur_ns=time.monotonic_ns() - start,
                          error=type(exc).__name__, **describe(args, kwargs))
            raise
        recorder.emit(kind, t_start_ns=start, dur_ns=time.monotonic_ns() - start, **describe(args, kwargs))
        return result
    return wrapper


_REQUEST_FIELDS = ("model", "temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty",
                   "max_tokens", "stream", "n", "reasoning_effort")
_TEE_LINE_LIMIT = 1 << 16
_TEE_BODY_LIMIT = 4 << 20


def request_settings(content: Any) -> dict | None:
    """The sampling and model fields of an engine chat request. Never the messages, the tools' content or any
    credential. None when the body is unavailable or not JSON."""
    try:
        body = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    out: dict[str, Any] = {k: body[k] for k in _REQUEST_FIELDS if k in body}
    kwargs = body.get("chat_template_kwargs")
    if isinstance(kwargs, dict):
        out["chat_template_kwargs"] = {str(k): v for k, v in sorted(kwargs.items(), key=lambda kv: str(kv[0]))
                                       if isinstance(v, (bool, int, float, str))}
    out["tool_count"] = len(body["tools"]) if isinstance(body.get("tools"), list) else 0
    return out


class CompletionParser:
    """Passive reader of an engine chat response: the last finish_reason, from an SSE stream or a JSON body.

    Memory is bounded: a stream keeps only one partial line, a JSON body is capped. It never raises."""

    def __init__(self) -> None:
        self.mode: str | None = None
        self.reason: Any = None
        self.saw_data = False
        self.choices = 0
        self.bad = False
        self.truncated = False
        self._line = b""
        self._body = bytearray()

    def _choices(self, data: Any) -> None:
        rows = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            self.bad = True
            return
        self.choices = max(self.choices, len(rows))
        for row in rows:
            if isinstance(row, dict) and row.get("finish_reason"):
                self.reason = row["finish_reason"]

    def feed(self, chunk: bytes) -> None:
        if self.mode is None:
            head = chunk.lstrip()[:1]
            if not head:
                return
            self.mode = "json" if head == b"{" else "sse"
        if self.mode == "json":
            if len(self._body) + len(chunk) <= _TEE_BODY_LIMIT:
                self._body.extend(chunk)
            else:
                self.truncated = True
            return
        self._line += chunk
        *lines, self._line = self._line.split(b"\n")
        if len(self._line) > _TEE_LINE_LIMIT:
            self._line, self.truncated = b"", True
        for line in lines:
            self._sse(line)

    def _sse(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        self.saw_data = True
        try:
            self._choices(json.loads(payload))
        except ValueError:
            self.bad = True

    def result(self) -> tuple[Any, int, str]:
        if self.mode == "sse" and self._line.strip():
            self._sse(self._line)
            self._line = b""
        if self.mode == "json" and not self.truncated:
            try:
                self._choices(json.loads(bytes(self._body)))
            except ValueError:
                self.bad = True
        if self.mode is None or self.bad or (self.mode == "sse" and not self.saw_data):
            return None, self.choices, "unparseable"
        if self.truncated and self.reason is None:
            return None, self.choices, "capture_truncated"
        return self.reason, self.choices, "ok" if self.reason else "none"


_TEE_CLASS: Any = None


def _completion_tee(recorder: InstrumentRecorder, inner: Any) -> Any:
    """Wrap a response byte stream so the engine's finish_reason is recorded. Every byte passes through
    unchanged and in order; the record is written when the stream closes."""
    global _TEE_CLASS
    if _TEE_CLASS is None:
        import httpx

        class CompletionTee(httpx.AsyncByteStream):
            def __init__(self, recorder: InstrumentRecorder, inner: Any) -> None:
                self.recorder, self.inner, self.parser = recorder, inner, CompletionParser()
                self.exhausted = self.emitted = False

            async def __aiter__(self):
                async for chunk in self.inner:
                    self.parser.feed(chunk)
                    yield chunk
                self.exhausted = True

            async def aclose(self):
                try:
                    await self.inner.aclose()
                finally:
                    self._finish()

            def _finish(self):
                if self.emitted:
                    return
                self.emitted = True
                reason, choices, parse = self.parser.result()
                self.recorder.emit("engine_completion", finish_reason=reason, n_choices=choices, parse=parse,
                                   complete_stream=self.exhausted)

        _TEE_CLASS = CompletionTee
    return _TEE_CLASS(recorder, inner)


def _wrap_async_request(recorder: InstrumentRecorder, fn: Callable) -> Callable:
    import functools

    @functools.wraps(fn)
    async def wrapper(self_, request, *args, **kwargs):
        start = time.monotonic_ns()
        fields: dict[str, Any] = {"method": str(request.method), "path": str(request.url.path)}
        chat = fields["method"] == "POST" and fields["path"].endswith("/chat/completions")
        if chat:
            try:
                content = request.content
            except Exception:  # noqa: BLE001 - a streamed request body is simply not observable
                content = None
            settings = request_settings(content)
            fields["request_settings"] = UNKNOWN if settings is None else settings
        try:
            response = await fn(self_, request, *args, **kwargs)
        except BaseException as exc:
            recorder.emit("engine_http", t_start_ns=start, t_headers_ns=time.monotonic_ns(),
                          error=type(exc).__name__, **fields)
            raise
        recorder.emit("engine_http", t_start_ns=start, t_headers_ns=time.monotonic_ns(),
                      status=getattr(response, "status_code", None), **fields)
        if chat and hasattr(response, "stream"):
            response.stream = _completion_tee(recorder, response.stream)
        return response
    return wrapper


def install_instrumentation(recorder: InstrumentRecorder, attributed_transport: Any,
                            local_peer: Any) -> tuple[list[str], list[str]]:
    """Wrap the attribution and engine-call surfaces. Returns (available, unavailable).

    A surface the candidate code does not have is reported unavailable, and the
    counts derived from it are UNKNOWN. They are never reported as zero.
    """
    available: set[str] = set()
    at, lp = attributed_transport, local_peer

    transport = getattr(at, "CredentialTransport", None)
    if transport is not None and hasattr(transport, "handle_async_request"):
        transport.handle_async_request = _wrap_async_request(recorder, transport.handle_async_request)
        available.update(("engine_http", "engine_completion"))

    authority = getattr(at, "RuntimeAuthority", None)
    if authority is not None and hasattr(authority, "load"):
        authority.load = _wrap_sync(recorder, "authority_load", authority.load, lambda a, k: {})
        available.add("authority_load")

    if callable(getattr(at, "inspect_command", None)):
        at.inspect_command = _wrap_sync(
            recorder, "process_check", at.inspect_command,
            lambda a, k: {"argv0": (a[0][0] if a and a[0] else "")})
        available.add("process_check")
    for owner in (lp, at):
        if callable(getattr(owner, "tcp_listeners", None)):
            owner.tcp_listeners = _wrap_sync(recorder, "process_check", owner.tcp_listeners,
                                             lambda a, k: {"argv0": "/usr/sbin/lsof(tcp_listeners)"})

    for owner, name in ((getattr(at, "DesktopOmlx", None), "binding"), (getattr(lp, "ManagedOmlx", None), "binding")):
        if owner is not None and name in vars(owner):
            setattr(owner, name, _wrap_sync(recorder, "binding_check", getattr(owner, name), lambda a, k: {}))
            available.add("binding_check")

    managed = getattr(lp, "ManagedOmlx", None)
    if managed is not None and hasattr(managed, "connected_peer"):
        managed.connected_peer = _wrap_sync(recorder, "peer_check", managed.connected_peer, lambda a, k: {})
        available.add("peer_check")

    return sorted(available), [n for n in SURFACES if n not in available]


def runtime_identity() -> dict:
    """What this interpreter actually is. Reported by the CHILD about itself, never inferred by the parent."""
    executable = Path(sys.executable).resolve()
    try:
        digest = sha256_file(executable)
    except OSError:
        digest = UNKNOWN
    return {"python_version": sys.version.split()[0], "implementation": sys.implementation.name,
            "executable": str(executable), "executable_sha256": digest, "platform": sys.platform}


ACTIVE_GUARD: EffectGuard | None = None  # kept reachable so a test can undo what serve_main installs
RESIDENCY_PROBE_PATH = "/__release_probe/residency"


def serve_main(args: argparse.Namespace) -> int:
    """Child entry point: run the candidate's own backend with the guard and instrumentation installed."""
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    home = Path(args.home).resolve()
    os.environ["WISP_HOME"] = str(home)
    recorder = InstrumentRecorder(args.instrument_out)
    global ACTIVE_GUARD
    guard = EffectGuard(recorder.emit, allowed_ports={args.engine_port}, fs=build_fs_policy(root, home))
    # The header is always the first record: identity of this exact child, written before anything can fail.
    recorder.emit("header", root=str(root), harness=HARNESS_VERSION, run_id=getattr(args, "run_id", ""),
                  side=getattr(args, "side", ""), effect_guard=True, guard_policy_sha256=guard.policy_sha256(),
                  runtime=runtime_identity())
    ACTIVE_GUARD = guard.install()
    import importlib
    try:
        at = importlib.import_module("service.inference.attributed_transport")
        lp = importlib.import_module("service.inference.local_peer")
    except ImportError as exc:
        recorder.emit("surfaces", error=f"import failed: {exc}", available=[], unavailable=list(SURFACES))
        raise
    available, unavailable = install_instrumentation(recorder, at, lp)
    recorder.emit("surfaces", available=available, unavailable=unavailable)
    import uvicorn
    main_module = importlib.import_module("service.main")
    app = main_module.app

    async def residency():
        """Resident model ids as the candidate's OWN attributed client reports them. The harness never sends
        the engine credential itself."""
        from fastapi.responses import JSONResponse
        try:
            return {"loaded": sorted(await main_module.client.loaded_models())}
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"error": type(exc).__name__})

    app.add_api_route(RESIDENCY_PROBE_PATH, residency, methods=["GET"], include_in_schema=False)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


# --------------------------------------------------------------------------
# Drivers: how one turn or one HTTP call is actually issued and timed
# --------------------------------------------------------------------------

class HttpTurnDriver:
    """Issues real `/agent` turns and `/health`-style calls to a backend over HTTP.

    The harness never approves anything: every `confirm` event is answered
    with a denial through the real `/agent/approve` endpoint, which is exactly
    where an unapproved outbound request stops. Whatever happens (deadline,
    cancellation, transport error) the partial events stay in the sink.
    """

    def __init__(self, base_url: str, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self.base_url, self.clock_ns = base_url, clock_ns

    @staticmethod
    def new_sink() -> dict:
        return {"events": [], "approvals": [], "deadline": False, "cancelled": False,
                "transport_error": None, "abs_start_ns": None, "abs_end_ns": None,
                "stream_closed_ns": None, "http": None, "latency_ns": None}

    async def run_turn(self, spec: dict, prompt: str, sink: dict) -> None:
        import httpx
        start = self.clock_ns()
        sink["abs_start_ns"] = start
        session = None
        timeout = httpx.Timeout(10.0, read=None)
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as client:
                async with asyncio.timeout(spec["deadline_s"]):
                    async with client.stream("POST", "/agent",
                                             json={"prompt": prompt, "debug": bool(spec.get("debug"))}) as response:
                        if response.status_code != 200:
                            sink["transport_error"] = f"http_status_{response.status_code}"
                            return
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            try:
                                event = json.loads(line[6:])
                            except ValueError:
                                event = {"type": "_unparseable", "raw": line[:200]}
                            if not isinstance(event, dict):
                                event = {"type": "_unparseable", "raw": line[:200]}
                            now = self.clock_ns() - start
                            sink["events"].append({"t_ns": now, "event": event})
                            kind = event.get("type")
                            if kind == "session":
                                session = event.get("id")
                            elif kind == "confirm":
                                reply = await client.post("/agent/approve", json={
                                    "session_id": session, "action_id": event.get("id"),
                                    "approved": False, "scope": "once"})
                                try:
                                    reply_body = reply.json()
                                except ValueError:
                                    reply_body = None
                                # `accepted` is True only when the real endpoint matched a pending action.
                                sink["approvals"].append({
                                    "t_ns": self.clock_ns() - start, "action_id": event.get("id"),
                                    "session_id": session, "approved": False, "status_code": reply.status_code,
                                    "accepted": reply.status_code == 200 and isinstance(reply_body, dict)
                                    and reply_body.get("ok") is True})
                            elif kind == "done":
                                break
                        sink["stream_closed_ns"] = self.clock_ns() - start
        except TimeoutError:
            sink["deadline"] = True
        except asyncio.CancelledError:
            sink["cancelled"] = True
            raise
        except httpx.HTTPError as exc:
            sink["transport_error"] = type(exc).__name__
        finally:
            sink["abs_end_ns"] = self.clock_ns()

    async def run_http(self, spec: dict, sink: dict) -> None:
        import httpx
        request = spec["request"]
        start = self.clock_ns()
        sink["abs_start_ns"] = start
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(spec["deadline_s"])) as client:
                async with asyncio.timeout(spec["deadline_s"]):
                    response = await client.request(request["method"], request["path"])
            sink["latency_ns"] = self.clock_ns() - start
            try:
                body = response.json()
            except ValueError:
                body = None
            sink["http"] = {"status": response.status_code, "json": body, "text": response.text[:500]}
        except TimeoutError:
            sink["deadline"] = True
        except asyncio.CancelledError:
            sink["cancelled"] = True
            raise
        except httpx.HTTPError as exc:
            sink["transport_error"] = type(exc).__name__
        finally:
            sink["abs_end_ns"] = self.clock_ns()


def parse_ps_time(text: str) -> float | None:
    """`ps -o time=` -> seconds. Accepts [[dd-]hh:]mm:ss[.cc]."""
    text = (text or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        if not day_part.isdigit():
            return None
        days = int(day_part)
    try:
        fields = [float(part) for part in text.split(":")]
    except ValueError:
        return None
    if not 1 <= len(fields) <= 3:
        return None
    seconds = 0.0
    for part in fields:
        seconds = seconds * 60 + part
    return days * 86400 + seconds


TERMINATE_WAIT_S = 15.0     # bounded teardown: ask politely, then insist
KILL_WAIT_S = 5.0
CHILD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# The only parent variables a backend inherits. No credential, loader, bridge, proxy or interpreter setting.
CHILD_ENV_FROM_PARENT = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE")


def child_environment(home: Path, parent: dict | None = None) -> dict:
    """The complete environment of a spawned backend: an explicit allowlist, nothing ambient.

    HOME is kept on purpose: the engine settings and authorization record the attributed path needs live under
    the real HOME, and a fake HOME would not be a real measurement. WISP_HOME and TMPDIR point into the
    throwaway directory, and no bytecode is written into the verified worktree."""
    source = os.environ if parent is None else parent
    env = {key: source[key] for key in CHILD_ENV_FROM_PARENT if key in source}
    env.update({"PATH": CHILD_PATH, "WISP_HOME": str(home), "TMPDIR": str(Path(home) / "tmp"),
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


class BackendProcess:
    """One isolated backend spawned from one verified worktree."""

    def __init__(self, side: str, root: Path, port: int, home: Path, instrument_path: Path,
                 python: str, runner: Callable = run_cmd, run_id: str = "") -> None:
        self.side, self.root, self.port, self.home = side, Path(root), port, Path(home)
        self.instrument_path, self.python, self.runner = Path(instrument_path), python, runner
        self.run_id = run_id
        self.proc: subprocess.Popen | None = None
        self._owned_group: int | None = None
        self.base_url = f"http://127.0.0.1:{port}"

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def command(self) -> list[str]:
        return [self.python, str(Path(__file__).resolve()), "serve", "--root", str(self.root),
                "--port", str(self.port), "--home", str(self.home),
                "--instrument-out", str(self.instrument_path), "--engine-port", "8000",
                "--run-id", self.run_id, "--side", self.side]

    def start(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "tmp").mkdir(exist_ok=True)
        # A new session makes the child the leader of a process group that holds only what it spawns, so
        # teardown can end the descendants without touching any process this harness does not own.
        self.proc = subprocess.Popen(self.command(), cwd=str(self.root), env=child_environment(self.home),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self._owned_group = self.proc.pid

    def wait_ready(self, timeout_s: float = 90.0) -> bool:
        import httpx
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                if httpx.get(self.base_url + "/mode", timeout=2.0).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    def cpu_seconds(self) -> Any:
        if self.pid is None:
            return UNKNOWN
        try:
            code, out, _ = self.runner(["ps", "-o", "time=", "-p", str(self.pid)], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return UNKNOWN
        value = parse_ps_time(out) if code == 0 else None
        return UNKNOWN if value is None else value

    def stop(self) -> dict:
        """Bounded teardown of this child and the process group it leads. Never raises for an ordinary
        failure: the outcome is returned so the caller can keep tearing down the other backend and seal the
        evidence. `ok` is True only if the child has exited and every step succeeded."""
        import signal
        result: dict[str, Any] = {"side": self.side, "pid": self.pid, "terminated": False, "killed": False,
                                  "exited": None, "errors": [], "ok": False}
        proc = self.proc
        if proc is None:
            return {**result, "exited": True, "ok": True}
        group = self._owned_group
        if group is None:
            try:
                if os.getpgid(proc.pid) == proc.pid:     # injected/test process must lead its group
                    group = proc.pid
            except ProcessLookupError:
                pass
            except OSError as exc:
                result["errors"].append(f"group_identity:{type(exc).__name__}")

        def send(sig: int, label: str) -> bool:
            try:
                os.killpg(group, sig) if group else proc.send_signal(sig)
                return True
            except ProcessLookupError:
                return True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{label}:{type(exc).__name__}")
                return False

        result["terminated"] = send(signal.SIGTERM, "terminate")
        try:
            proc.wait(timeout=TERMINATE_WAIT_S)
        except subprocess.TimeoutExpired:
            result["killed"] = send(signal.SIGKILL, "kill")
            try:
                proc.wait(timeout=KILL_WAIT_S)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"wait:{type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"wait:{type(exc).__name__}")
        result["group_exited"] = group is None
        if group is not None:
            deadline = time.monotonic() + KILL_WAIT_S
            killed_group = False
            while True:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    result["group_exited"] = True
                    break
                except OSError as exc:
                    result["errors"].append(f"group_probe:{type(exc).__name__}")
                    break
                if not killed_group:
                    send(signal.SIGKILL, "descendants")
                    killed_group = True
                if time.monotonic() >= deadline:
                    result["errors"].append("group_wait:TimeoutExpired")
                    break
                time.sleep(0.02)
        try:
            result["exited"] = proc.poll() is not None
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"poll:{type(exc).__name__}")
            result["exited"] = False
        result["ok"] = bool(result["exited"]) and result["group_exited"] and not result["errors"]
        return result


def stop_all(backends: dict[str, Any]) -> tuple[dict[str, dict], BaseException | None]:
    """Attempt teardown of EVERY owned backend, whatever happens to the others. Returns the per-backend
    outcomes and any non-ordinary exception (an interrupt) to re-raise AFTER the evidence is sealed."""
    outcome: dict[str, dict] = {}
    pending: BaseException | None = None
    for side, backend in backends.items():
        try:
            reported = backend.stop()
            outcome[side] = reported if isinstance(reported, dict) else {"side": side, "ok": True, "errors": []}
        except Exception as exc:  # noqa: BLE001
            outcome[side] = {"side": side, "pid": getattr(backend, "pid", None), "ok": False, "exited": None,
                             "errors": [f"stop:{type(exc).__name__}"]}
        except BaseException as exc:  # noqa: BLE001 - still tear the rest down, then re-raise
            outcome[side] = {"side": side, "pid": getattr(backend, "pid", None), "ok": False, "exited": None,
                             "errors": [f"stop:{type(exc).__name__}"]}
            pending = pending or exc
    return outcome, pending


class LiveDeps:
    """The real dependencies of a release measurement. `is_real` is what lets a receipt claim
    the actual-candidate mode; the offline tests use a different class with it False."""
    is_real = True

    def __init__(self, python: str, ports: dict[str, int]) -> None:
        self.python, self.ports = python, ports

    def clock_ns(self) -> int:
        return time.monotonic_ns()

    def now(self) -> float:
        return utc_now()

    def verify_subject(self, root: Path, expected: str) -> dict:
        return verify_subject(root, expected)

    def collect_environment(self, model_id: str, lane: str, warmups: int) -> dict:
        return collect_environment(model_id, lane, warmups)

    def preflight(self, lane: str, model_id: str) -> list[dict]:
        """Exact missing prerequisites. It sends nothing to the engine: no request, no credential. Whether the
        engine holds exactly the named model is asked of each spawned backend through the candidate's own
        attributed client (`residency`), never by this harness."""
        import socket
        missing: list[dict] = []
        manifest = Path.home() / ".moe" / "omlx-runtime-authorization.json"
        if lane == "desktop" and manifest.exists():
            missing.append({"id": "desktop_lane_requires_no_managed_authorization",
                            "detail": f"{manifest} exists; this is a managed host, not the Desktop lane"})
        if lane == "managed_mini" and not manifest.exists():
            missing.append({"id": "managed_lane_requires_authorization",
                            "detail": f"{manifest} is absent"})
        for side, port in self.ports.items():
            with socket.socket() as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    missing.append({"id": "backend_port_in_use", "detail": f"{side} port {port} is occupied"})
        with socket.socket() as probe:               # a bare TCP connect: no bytes are sent
            probe.settimeout(1.0)
            if probe.connect_ex(("127.0.0.1", 8000)) != 0:
                missing.append({"id": "engine_unreachable_on_8000",
                                "detail": "nothing is listening on 127.0.0.1:8000"})
        return missing

    def spawn_backend(self, side: str, root: Path, home: Path, instrument_path: Path,
                      run_id: str = "") -> BackendProcess:
        backend = BackendProcess(side, root, self.ports[side], home, instrument_path, self.python, run_id=run_id)
        backend.start()
        return backend

    def residency(self, backend: BackendProcess) -> Any:
        """Resident model ids per the candidate's own attributed client, or None if it cannot say."""
        import httpx
        try:
            reply = httpx.get(backend.base_url + RESIDENCY_PROBE_PATH, timeout=30.0)
            body = reply.json()
            loaded = body.get("loaded") if reply.status_code == 200 and isinstance(body, dict) else None
            return sorted(str(m) for m in loaded) if isinstance(loaded, list) else None
        except (httpx.HTTPError, ValueError):
            return None

    def make_driver(self, backend: BackendProcess) -> HttpTurnDriver:
        return HttpTurnDriver(backend.base_url)

    def push_leaves(self, backend: BackendProcess, payloads: list[dict]) -> list[int]:
        import httpx
        statuses = []
        for payload in payloads:
            statuses.append(httpx.post(backend.base_url + payload["path"], json=payload["body"],
                                       timeout=30.0).status_code)
        return statuses


# --------------------------------------------------------------------------
# Instrumentation file reading
# --------------------------------------------------------------------------

def read_instrument_lines(path: Path) -> list[dict]:
    records = []
    try:
        for line in Path(path).read_text().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except ValueError:
                    records.append({"kind": "_unparseable", "t_ns": -1})
    except OSError:
        pass
    return records


def instrument_header(records: list[dict]) -> dict | None:
    """The header record merged with the `surfaces` record that follows it, or None if there is no header.
    A surfaces record that is missing means NO surface is available."""
    header = next((r for r in records if r.get("kind") == "header"), None)
    if header is None:
        return None
    surfaces = next((r for r in records if r.get("kind") == "surfaces"), None)
    merged = dict(header)
    merged["available"] = list(surfaces.get("available") or []) if surfaces else []
    merged["unavailable"] = list(surfaces["unavailable"]) if surfaces and "unavailable" in surfaces \
        else list(SURFACES)
    return merged


def window_events(records: list[dict], start_ns: int, end_ns: int) -> list[dict]:
    return [r for r in records if r.get("kind") not in ("header", "surfaces", "_unparseable")
            and isinstance(r.get("t_ns"), int) and start_ns <= r["t_ns"] <= end_ns]


def sample_instrument(logs: dict[str, list[dict]], side: str, bounds: dict) -> dict:
    """The instrumentation view of one sample: its own side's window AND the idle side's window (which shares
    the engine). One implementation, used by the run and by the checker, from the COMPLETE logs."""
    other = SIDES[1] if side == SIDES[0] else SIDES[0]

    def view(name: str) -> tuple[list[dict], list[str]]:
        records = logs.get(name) or []
        header = instrument_header(records)
        unavailable = header["unavailable"] if header else list(SURFACES)
        return window_events(records, bounds["start_ns"], bounds["end_ns"]), unavailable

    window, unavailable = view(side)
    other_window, other_unavailable = view(other)
    return {"window": window, "unavailable": unavailable,
            "other_window": other_window, "other_unavailable": other_unavailable}


# --------------------------------------------------------------------------
# One sample
# --------------------------------------------------------------------------

def finalize_sample(*, spec: dict, corpus: dict, driver_sink: dict, instrument: dict,
                    identity: dict) -> tuple[dict, dict]:
    """Derive timings, counts and the grade from raw material. The checker calls this same
    function on the stored raw data and demands identical output."""
    analysis = analyze_events(driver_sink["events"])
    counts = instrument_counts(instrument.get("window"), instrument.get("unavailable"),
                               instrument.get("other_window"), instrument.get("other_unavailable"))
    grade = grade_sample(spec, analysis, driver_sink, counts, corpus)
    metrics = derive_metrics(spec, analysis, driver_sink)
    if grade["correct"]:
        outcome = "ok"
    elif driver_sink.get("deadline"):
        outcome = "deadline"
    elif driver_sink.get("cancelled"):
        outcome = "cancelled"
    elif grade["refusal"]:
        outcome = "refused"
    else:
        outcome = "failed"
    record = {"schema": SAMPLE_SCHEMA, **identity, "outcome": outcome, "grade": grade,
              "metrics_ns": metrics, "diagnostics": {**counts}}
    return record, analysis


# --------------------------------------------------------------------------
# Summary and policy decision
# --------------------------------------------------------------------------

def aggregate_counts(values: list[Any]) -> dict:
    known = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    if not values or len(known) != len(values):
        return {"n": len(values), "p50": UNKNOWN, "p95": UNKNOWN}
    return {"n": len(values), "p50": nearest_rank(known, 50), "p95": nearest_rank(known, 95)}


def aggregate_cpu(values: list[Any]) -> dict:
    known = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not values or len(known) != len(values):
        return {"n": len(values), "p50_s": UNKNOWN, "p95_s": UNKNOWN}
    scaled = [round(v * 1000) for v in known]
    return {"n": len(values), "p50_s": nearest_rank(scaled, 50) / 1000, "p95_s": nearest_rank(scaled, 95) / 1000}


def summarize(samples: list[dict], bundle: dict) -> dict:
    """Per scenario and side. Warmups are counted and labelled but never enter a percentile, and only
    correct measured samples contribute latencies."""
    out: dict[str, Any] = {}
    for spec in bundle["corpus"]["scenarios"]:
        per_side: dict[str, Any] = {}
        for side in SIDES:
            mine = [s for s in samples if s["scenario"] == spec["id"] and s["side"] == side]
            measured = [s for s in mine if s["phase"] == "measured"]
            correct = [s for s in measured if s["grade"]["correct"]]
            per_side[side] = {
                "warmup_samples": sum(1 for s in mine if s["phase"] == "warmup"),
                "measured": len(measured), "correct": len(correct),
                "failed": sum(1 for s in measured if not s["grade"]["correct"]),
                # A real correctness reason, as opposed to an expectation that merely could not be checked.
                "hard_failed": sum(1 for s in measured if s["grade"]["reasons"]),
                "refused": sum(1 for s in measured if s["grade"]["refusal"]),
                "unverifiable": sum(1 for s in measured if s["grade"]["unverifiable"]),
                "outcomes": {o: sum(1 for s in measured if s["outcome"] == o)
                             for o in sorted({s["outcome"] for s in measured})},
                "metrics": {m: aggregate([s["metrics_ns"].get(m, UNKNOWN) for s in correct])
                            for m in spec.get("required_metrics") or []},
                "diagnostics": {name: aggregate_counts([s["diagnostics"].get(name, UNKNOWN) for s in correct])
                                for name in ("engine_http_calls", "chat_calls", "authority_loads",
                                             "process_checks", "binding_checks", "peer_checks",
                                             "instrumentation_records")},
                "backend_cpu": aggregate_cpu([s["diagnostics"].get("backend_cpu_s", UNKNOWN) for s in correct]),
            }
        out[spec["id"]] = per_side
    return out


def is_regression(candidate_ns: Any, baseline_ns: Any, policy: dict) -> Any:
    """True/False, or UNKNOWN if either side is UNKNOWN. Both thresholds must be exceeded."""
    if candidate_ns == UNKNOWN or baseline_ns == UNKNOWN:
        return UNKNOWN
    regression = policy["regression"]
    delta = candidate_ns - baseline_ns
    relative = Fraction(str(regression["relative_increase"]))
    absolute = Fraction(str(regression["absolute_increase_ms"])) * MS
    return delta > relative * baseline_ns and delta > absolute


def lifecycle_summary(logs: dict[str, list[dict]]) -> dict:
    """Effects over each backend's WHOLE lifetime (startup, warmup, idle intervals, teardown), not only the
    measured windows. A refused effect and any engine-state mutation are recorded wherever they happened."""
    out: dict[str, Any] = {}
    for side in SIDES:
        records = logs.get(side) or []
        blocked = [r for r in records if r.get("kind") == "effect_blocked"]
        http = [r for r in records if r.get("kind") == "engine_http"]
        out[side] = {
            "records": len(records), "blocked_effects": len(blocked),
            "blocked_targets": sorted({f"{r.get('what')}:{r.get('target')}" for r in blocked})[:20],
            "engine_state_changes": sum(1 for r in http if r.get("method") == "POST"
                                        and bool(_ENGINE_STATE_CHANGE.search(str(r.get("path", ""))))),
            "engine_mutating_calls": sum(
                1 for r in http if r.get("method") != "GET"
                and not any(str(r.get("path", "")).endswith(tail)
                            for tail in ("/chat/completions", "/embeddings", "/rerank")))}
    return out


def decide(summary: dict, bundle: dict, baseline: dict, lifecycle: dict | None = None) -> dict:
    """Policy verdict from the summary. BLOCK beats INCONCLUSIVE beats PASS."""
    policy = bundle["policy"]
    block: list[dict] = []
    inconclusive: list[dict] = []
    comparison: dict[str, Any] = {}
    for side, row in (lifecycle or {}).items():
        if row["blocked_effects"] or row["engine_state_changes"] or row["engine_mutating_calls"]:
            detail = (f"{side} backend: {row['blocked_effects']} refused effects {row['blocked_targets']}, "
                      f"{row['engine_state_changes']} engine state changes, {row['engine_mutating_calls']} mutating calls")
            if side == "candidate":
                block.append({"code": "lifecycle_blocked_effect", "detail": detail})
            else:
                inconclusive.append({"code": "baseline_lifecycle_effect", "detail": detail})
    for spec in required_scenarios(bundle):
        sid = spec["id"]
        cand, base = summary[sid]["candidate"], summary[sid]["baseline"]
        if cand["hard_failed"]:
            block.append({"code": "candidate_correctness_failure", "scenario": sid,
                          "detail": f"{cand['hard_failed']} of {cand['measured']} measured samples failed"})
        if cand["unverifiable"]:
            inconclusive.append({"code": "unverifiable_expectation", "scenario": sid,
                                 "detail": f"{cand['unverifiable']} samples could not verify a required expectation"})
        if cand["refused"] > base["refused"]:
            block.append({"code": "new_refusal", "scenario": sid,
                          "detail": f"candidate refused {cand['refused']}, baseline {base['refused']}"})
        if base["failed"]:
            inconclusive.append({"code": "baseline_correctness_failure", "scenario": sid,
                                 "detail": f"{base['failed']} baseline samples failed"})
        comparison[sid] = {"required_metrics": {}}
        for metric in spec["required_metrics"]:
            row: dict[str, Any] = {"n_candidate": cand["metrics"][metric]["n"],
                                   "n_baseline": base["metrics"][metric]["n"]}
            for pct in ("p50", "p95"):
                c, b = cand["metrics"][metric][f"{pct}_ns"], base["metrics"][metric][f"{pct}_ns"]
                regressed = is_regression(c, b, policy)
                row[pct] = {"candidate_ms": ms(c), "baseline_ms": ms(b),
                            "delta_ms": UNKNOWN if UNKNOWN in (c, b) else ms(c - b),
                            "regression": regressed}
                if regressed == UNKNOWN:
                    inconclusive.append({"code": "unknown_gating_metric", "scenario": sid,
                                         "detail": f"{metric} {pct} is UNKNOWN on at least one side"})
                elif regressed:
                    block.append({"code": "measured_material_regression", "scenario": sid,
                                  "detail": f"{metric} {pct}: {ms(b)} ms -> {ms(c)} ms"})
            comparison[sid]["required_metrics"][metric] = row
    if not baseline.get("approved"):
        inconclusive.append({"code": "missing_or_unapproved_baseline",
                             "detail": baseline.get("problem") or "no approved baseline supplied"})
    verdict = "BLOCK" if block else "INCONCLUSIVE" if inconclusive else "PASS"
    return {"verdict": verdict, "reasons": block + inconclusive, "comparison": comparison}


# --------------------------------------------------------------------------
# Run orchestration (injected dependencies, so it can be exercised offline)
# --------------------------------------------------------------------------

class SubjectError(RuntimeError):
    """A worktree is dirty, at the wrong commit, or candidate and baseline are not different subjects."""


class InstrumentTail:
    """Incremental reader of one backend's instrumentation file."""

    def __init__(self, path: Path) -> None:
        self.path, self.records, self._offset = Path(path), [], 0

    def refresh(self) -> list[dict]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return self.records
        complete = chunk[: chunk.rfind(b"\n") + 1]
        self._offset += len(complete)
        for line in complete.decode("utf-8", "replace").splitlines():
            if line.strip():
                try:
                    self.records.append(json.loads(line))
                except ValueError:
                    self.records.append({"kind": "_unparseable", "t_ns": -1})
        return self.records


def render_role_config(model_id: str) -> str:
    roles = ("fast", "router", "general", "agent", "coding", "reasoning", "profile", "profile_map")
    return "roles:\n" + "".join(f"  {role}: {json.dumps(model_id)}\n" for role in roles)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


# Untrusted publication evidence is never opened through Path's link-following
# readers. Limits cover the fixed protocol with headroom for complete raw logs.
RECEIPT_MAX_BYTES = 4 * 1024 * 1024
APPROVAL_MAX_BYTES = 1024 * 1024
RAW_FILE_MAX_BYTES = 64 * 1024 * 1024
RAW_PACKAGE_MAX_BYTES = 256 * 1024 * 1024
RAW_MAX_FILES = 64
RAW_MAX_ENTRIES = 128
RAW_MAX_DEPTH = 4


@contextmanager
def evidence_directory(path: Path):
    """Walk from / with no-follow directory FDs, retaining the final directory.

    Do not resolve links first: that would turn a prohibited link into an
    apparently safe target. Relative paths are anchored to the current cwd.
    """
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def evidence_file(parent_fd: int, name: str, limit: int) -> bytes:
    """Hashing and parsing callers share these exact retained bytes.

    NONBLOCK prevents a substituted FIFO from hanging before fstat; only
    bounded regular files are read. Metadata must stay stable across the read.
    """
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("evidence_not_bounded_regular_file")
        chunks, count = [], 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > limit:
                raise ValueError("evidence_file_too_large")
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if count != before.st_size or any(getattr(before, key) != getattr(after, key) for key in fields):
            raise ValueError("evidence_changed_during_snapshot")
        return b"".join(chunks)
    finally:
        os.close(fd)


def evidence_bytes(path: Path, limit: int) -> bytes:
    with evidence_directory(Path(path).parent) as parent:
        return evidence_file(parent, Path(path).name, limit)


def raw_snapshot(raw_dir: Path, *, parent_fd: int | None = None) -> dict[str, bytes]:
    """No-follow, bounded tree snapshot. Links and special files are refused.

    Passing a retained receipt directory binds raw traversal to that same
    package directory even if its pathname is replaced concurrently.
    """
    files: dict[str, bytes] = {}
    total = entries = 0
    def walk(fd, prefix="", depth=0):
        nonlocal total, entries
        if depth > RAW_MAX_DEPTH:
            raise ValueError("evidence_package_entries_exceeded")
        names = []
        with os.scandir(fd) as scan:
            for entry in scan:
                entries += 1
                if entries > RAW_MAX_ENTRIES:
                    raise ValueError("evidence_package_entries_exceeded")
                names.append(entry.name)
        names.sort()
        for name in names:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, relative + "/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if len(files) >= RAW_MAX_FILES:
                    raise ValueError("evidence_package_files_exceeded")
                data = evidence_file(fd, name, min(RAW_FILE_MAX_BYTES, RAW_PACKAGE_MAX_BYTES - total))
                files[relative] = data
                total += len(data)
            else:
                raise ValueError("evidence_package_link_or_special_file")
    if parent_fd is None:
        with evidence_directory(raw_dir) as fd:
            walk(fd)
    else:
        fd = os.open("raw", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            walk(fd)
        finally:
            os.close(fd)
    return files


def raw_manifest(raw_dir: Path) -> dict[str, str]:
    return {name: sha256_bytes(data) for name, data in raw_snapshot(raw_dir).items()}


def load_baseline_approval(path: str | None, expected_sha256: str | None) -> tuple[dict, list[str]]:
    """Only a digest-bound, valid reviewed document can produce approved=True."""
    def invalid(problem, code):
        return {"approved": False, "problem": problem}, [code]
    if not path:
        if expected_sha256:
            return invalid("approval digest supplied without a file", "approved_baseline_missing")
        return {"approved": False, "problem": "no approved baseline was supplied"}, []
    if not expected_sha256:
        return invalid("approval digest was not independently supplied", "approved_baseline_digest_not_bound")
    try:
        raw = evidence_bytes(Path(path), APPROVAL_MAX_BYTES)
    except FileNotFoundError:
        return invalid("approved baseline file is missing", "approved_baseline_missing")
    except (OSError, ValueError):
        return invalid("unsafe or oversized approval", "approved_baseline_unsafe")
    digest = sha256_bytes(raw)
    if digest != expected_sha256:
        return invalid("approval digest does not match retained bytes", "approved_baseline_digest_mismatch")
    try:
        doc = json.loads(raw)
    except ValueError:
        return invalid("unreadable approval", "approved_baseline_unreadable")
    if not isinstance(doc, dict) or doc.get("schema") != BASELINE_SCHEMA:
        return invalid("wrong approval schema", "approved_baseline_wrong_schema")
    subject = doc.get("subject")
    if (not isinstance(subject, dict)
            or any(not isinstance(subject.get(key), str) or not FULL_SHA.fullmatch(subject[key])
                   for key in ("sha", "tree"))
            or not isinstance(doc.get("cohort_key"), dict)):
        return invalid("invalid approval subject or cohort", "approved_baseline_invalid_subject_or_cohort")
    status_ok = (doc.get("status") == "approved"
                 and all(isinstance(doc.get(key), str) and doc[key].strip()
                         for key in ("approved_by", "review_ref", "approved_at")))
    if not status_ok:
        return {"approved": False, "problem": "baseline file is not an approved, reviewed baseline",
                "doc": doc}, []
    return {"approved": True, "problem": None, "doc": doc, "sha256": digest}, []


def baseline_subject_refusals(state: dict, *, baseline_sha: str, baseline_tree: str,
                              candidate_sha: str, supplied: bool) -> list[str]:
    """One run/check rule binds the approval to the selected verified baseline."""
    if not state.get("approved"):
        return ["approved_baseline_not_approved"] if supplied else []
    subject = state["doc"]["subject"]
    problems = []
    if subject["sha"] != baseline_sha:
        problems.append("baseline_subject_not_the_approved_one")
    if subject["tree"] != baseline_tree:
        problems.append("baseline_tree_not_the_approved_one")
    if subject["sha"] == candidate_sha:
        problems.append("baseline_equals_candidate")
    return problems


def effective_verdict(decision: dict, mode: str, fake_model: bool) -> dict:
    """A run that is not a real, attributed, actual-candidate measurement can never be a PASS."""
    result = dict(decision)
    if mode != ACTUAL_MODE or fake_model:
        result["verdict"] = "INCONCLUSIVE" if result["verdict"] == "PASS" else result["verdict"]
        result["reasons"] = result["reasons"] + [{"code": "not_an_actual_measurement",
                                                  "detail": f"execution_mode={mode} fake_model={fake_model}"}]
        if result["verdict"] == "PASS":
            result["verdict"] = "INCONCLUSIVE"
    return result


def apply_context_rules(decision: dict, baseline_state: dict, cohort: dict, lane: str, policy: dict,
                        findings: Any = ()) -> dict:
    """Rules that depend on WHERE and AGAINST WHAT a measurement ran. One implementation, used by both the
    run and the checker, so a receipt's own verdict can never disagree with the checker's."""
    verdict, reasons = decision["verdict"], list(decision["reasons"])

    def demote(code: str, detail: str) -> None:
        nonlocal verdict
        reasons.append({"code": code, "detail": detail})
        if verdict == "PASS":
            verdict = "INCONCLUSIVE"

    if baseline_state.get("approved") and baseline_state["doc"]["cohort_key"] != cohort:
        demote("missing_or_unapproved_baseline",
               "new cohort: the approved baseline was reviewed for a different environment, engine, model, "
               "harness, corpus or policy")
    if has_unknown(cohort):
        demote("unknown_gating_metric", "cohort identity contains UNKNOWN fields")
    gate = policy["lanes"].get(lane) or {}
    if not gate.get("release_gate"):
        demote("lane_is_not_a_release_gate", str(gate.get("label", lane)))
    for finding in findings:      # the measurement is not comparable or not verifiable: never a PASS
        demote(finding["code"], finding["detail"])
    return {**decision, "verdict": verdict, "reasons": reasons}


ENV_COHORT_FIELDS = ("hardware", "os", "engine", "model", "generation_settings", "cache_warmup_treatment")
_CHILD_RUNTIME_KEYS = ("python_version", "implementation", "executable_sha256", "platform")


def environment_fingerprint(environment: Any) -> dict:
    environment = environment if isinstance(environment, dict) else {}
    return {key: environment.get(key, UNKNOWN) for key in ENV_COHORT_FIELDS}


def child_runtime(headers: dict[str, dict | None]) -> Any:
    """The interpreter the CANDIDATE child reported about itself, portable fields only."""
    runtime = ((headers.get("candidate") or {}).get("runtime"))
    if not isinstance(runtime, dict):
        return UNKNOWN
    return {key: runtime.get(key, UNKNOWN) for key in _CHILD_RUNTIME_KEYS}


def containment_identity(headers: dict[str, dict | None]) -> Any:
    digest = (headers.get("candidate") or {}).get("guard_policy_sha256")
    return {"guard_policy_sha256": digest} if isinstance(digest, str) else UNKNOWN


def request_identity(samples: list[dict], events: dict[str, dict], logs: dict[str, list[dict]]) -> dict:
    """The model and sampling fields of every engine chat request inside a measured window, per scenario and
    side. This is what actually reached the engine, not what the corpus says should."""
    models: set[str] = set()
    signatures: dict[str, dict[str, set[str]]] = {}
    unknown = False
    for sample in samples:
        raw = events.get(sample.get("id"))
        if sample.get("phase") != "measured" or raw is None or not isinstance(raw.get("bounds"), dict):
            continue
        for event in sample_instrument(logs, sample["side"], raw["bounds"])["window"]:
            if not (event.get("kind") == "engine_http" and event.get("method") == "POST"
                    and str(event.get("path", "")).endswith("/chat/completions")):
                continue
            settings = event.get("request_settings", UNKNOWN)
            if not isinstance(settings, dict) or "model" not in settings:
                unknown = True
                continue
            models.add(str(settings["model"]))
            signatures.setdefault(sample["scenario"], {}).setdefault(sample["side"], set()).add(
                canonical_json(settings).decode())
    return {"unknown": unknown, "models": sorted(models),
            "signatures": {sc: {side: sorted(v) for side, v in sorted(rows.items())}
                           for sc, rows in sorted(signatures.items())}}


def integrity_findings(*, model_id: str, env_before: Any, env_after: Any, residency: dict,
                       headers: dict[str, dict | None], requests: dict) -> list[dict]:
    """Everything that makes a measurement non-comparable or unverifiable even though every sample graded
    correct. Each is a reason the run cannot be a PASS; none is a candidate defect, so none is a BLOCK."""
    found: list[dict] = []

    def add(code: str, detail: str) -> None:
        found.append({"code": code, "detail": detail})

    before, after = environment_fingerprint(env_before), environment_fingerprint(env_after)
    if not isinstance(env_after, dict) or not env_after:
        add("environment_after_unavailable", "the environment was not re-collected after the run")
    elif before != after:
        add("environment_changed_during_run",
            "engine, model, settings, hardware or OS identity differs before and after the run")
    for moment in ("before", "after"):
        per_side = residency.get(moment) or {}
        for side in SIDES:
            value = per_side.get(side)
            if value is None:
                add("residency_unverified", f"{moment}/{side}: the resident model set could not be read")
            elif value != [model_id]:
                add("residency_not_exclusive", f"{moment}/{side}: resident {value}, expected exactly {[model_id]}")
    runtimes = {side: (headers.get(side) or {}).get("runtime") for side in SIDES}
    for side, runtime in runtimes.items():
        if not isinstance(runtime, dict) or has_unknown({k: runtime.get(k, UNKNOWN) for k in _CHILD_RUNTIME_KEYS}):
            add("child_runtime_unverified", f"{side} backend did not report a complete runtime identity")
    if all(isinstance(r, dict) for r in runtimes.values()) and \
            {k: runtimes["candidate"].get(k) for k in _CHILD_RUNTIME_KEYS} != \
            {k: runtimes["baseline"].get(k) for k in _CHILD_RUNTIME_KEYS}:
        add("child_runtime_differs_between_sides", "candidate and baseline children ran different interpreters")
    guards = {side: (headers.get(side) or {}).get("guard_policy_sha256") for side in SIDES}
    if not all(isinstance(g, str) for g in guards.values()):
        add("containment_unverified", "a backend did not report its containment policy")
    elif guards["candidate"] != guards["baseline"]:
        add("containment_differs_between_sides", "candidate and baseline ran under different containment policies")
    if requests.get("unknown"):
        add("request_settings_unverified", "an engine chat request's model and sampling fields were not observed")
    wrong = [m for m in requests.get("models", []) if m != model_id]
    if wrong:
        add("model_mismatch", f"engine chat requests named {wrong}, the declared model is {model_id!r}")
    for scenario, per_side in (requests.get("signatures") or {}).items():
        if per_side.get("candidate") != per_side.get("baseline"):
            add("request_settings_differ", f"{scenario}: candidate and baseline sent different sampling settings")
    return found


def build_receipt(*, run_id: str, mode: str, fake_model: bool, lane: str, bundle: dict, bundle_problems: list[str],
                  subjects: dict, environment: dict, protocol: dict, raw_files: dict, summary: dict,
                  decision: dict, baseline_state: dict, generated_at_ts: float, missing: list[dict],
                  aborted: bool, runtime: dict, evidence: dict, extra: dict | None = None) -> dict:
    here = Path(__file__).resolve()
    return {
        "schema": RECEIPT_SCHEMA,
        "run_id": run_id,
        "generated_at": iso(generated_at_ts), "generated_at_ts": generated_at_ts,
        "execution_mode": mode, "lane": lane,
        "harness": {"version": HARNESS_VERSION, "script_sha256": harness_sha256(),
                    "files": {n: sha256_file(here.parent / n) for n in
                              ("release_performance.py", "bench_latency_changes.py")
                              if (here.parent / n).is_file()}},
        "corpus": {"version": bundle["corpus"]["version"], "sha256": bundle["corpus_sha256"],
                   "bundle_file_sha256": bundle["file_sha256"]},
        "policy": {"version": bundle["policy"]["version"], "sha256": bundle["policy_sha256"]},
        "bundle_problems": bundle_problems,
        "candidate": subjects["candidate"], "baseline": subjects["baseline"],
        "environment": environment,
        "runtime": runtime,
        "inference": {"kind": "omlx_attributed_local" if not fake_model else "fake",
                      "fake_model": fake_model, **evidence},
        "protocol": protocol,
        "raw": {"files": raw_files, "manifest_sha256": canonical_sha(raw_files)},
        "summary": summary,
        "comparison": decision["comparison"],
        "verdict": decision["verdict"], "reasons": decision["reasons"],
        "baseline_binding": {"approved": bool(baseline_state.get("approved")),
                             "sha256": baseline_state.get("sha256")},
        "prerequisites_missing": missing, "aborted": aborted,
        **(extra or {}),
    }


def sample_identity(run_id: str, side: str, item: dict, slot: int, prompt: str, started_ns: int, ended_ns: int) -> dict:
    return {"run_id": run_id, "id": sample_id(side, item["scenario"], item["phase"], item["rep"]),
            "side": side, "scenario": item["scenario"], "phase": item["phase"], "rep": item["rep"],
            "variant": item["variant"], "order_slot": slot, "started_ns": started_ns, "ended_ns": ended_ns,
            "prompt_sha256": sha256_bytes(prompt.encode())}


_DRIVER_KEYS = ("approvals", "deadline", "cancelled", "transport_error", "http", "latency_ns",
                "stream_closed_ns", "abs_start_ns", "abs_end_ns")


def sample_bounds(sink: dict, started_ns: int, ended_ns: int) -> dict:
    return {"start_ns": sink["abs_start_ns"] if isinstance(sink.get("abs_start_ns"), int) else started_ns,
            "end_ns": sink["abs_end_ns"] if isinstance(sink.get("abs_end_ns"), int) else ended_ns}


def build_sample_record(spec: dict, corpus: dict, identity: dict, driver_sink: dict, logs: dict,
                        backend_cpu: Any) -> dict:
    """One graded sample from raw material and the COMPLETE instrumentation logs. Used by the run (after
    teardown, when the logs are complete) and by the checker, so they cannot disagree."""
    bounds = sample_bounds(driver_sink, identity["started_ns"], identity["ended_ns"])
    record, _analysis = finalize_sample(spec=spec, corpus=corpus, driver_sink=driver_sink, identity=identity,
                                        instrument=sample_instrument(logs, identity["side"], bounds))
    record["diagnostics"]["backend_cpu_s"] = backend_cpu
    return record


async def run_release(opts: dict, deps: Any, bundle: dict, *,
                      progress: Callable[[dict], None] = lambda _event: None) -> tuple[int, Path]:
    """Measure the candidate and baseline in alternating order and write the evidence directory.

    Returns (exit code, receipt path). Raises SubjectError/ValueError for refusals that must not
    produce a receipt at all (dirty worktree, identical subjects, below-floor sample count).
    """
    problems = validate_bundle(bundle)
    if problems:
        raise ValueError("bundle is invalid: " + "; ".join(problems))
    corpus, policy = bundle["corpus"], bundle["policy"]
    specs = scenario_by_id(bundle)
    required_ids = [s["id"] for s in required_scenarios(bundle)]
    selected = list(opts.get("scenario_ids") or required_ids)
    unknown = [s for s in selected if s not in specs]
    if unknown:
        raise ValueError(f"unknown scenario ids: {unknown}")
    measured_n = int(opts.get("samples") or policy["min_measured_samples"])
    diagnostic = bool(opts.get("diagnostic"))
    if not diagnostic and (measured_n < policy["min_measured_samples"] or set(selected) != set(required_ids)):
        raise ValueError("a release measurement needs every required scenario at the policy sample floor; "
                         "pass diagnostic mode for a partial, non-gating run")
    partial = diagnostic or measured_n < policy["min_measured_samples"] or set(selected) != set(required_ids)
    lane = opts.get("lane", "desktop")
    if lane not in policy["lanes"]:
        raise ValueError(f"unknown lane {lane}")

    baseline_state, baseline_refusals = load_baseline_approval(opts.get("approved_baseline_path"),
                                                               opts.get("approved_baseline_sha256"))
    if baseline_refusals or opts.get("approved_baseline_path") and not baseline_state.get("approved"):
        raise ValueError("invalid baseline approval: " + ", ".join(
            baseline_refusals or ["approved_baseline_not_approved"]))

    out = Path(opts["output_dir"])
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} already has content; evidence is never overwritten")
    raw_dir = out / "raw"

    before = {side: deps.verify_subject(Path(opts[f"{side}_root"]), opts[f"{side}_sha"]) for side in SIDES}
    bad = {s: r["problems"] for s, r in before.items() if not subject_ok(r)}
    if bad:
        raise SubjectError(f"subjects are not clean and exact: {bad}")
    if opts["candidate_sha"] == opts["baseline_sha"] or before["candidate"]["tree"] == before["baseline"]["tree"]:
        raise SubjectError("candidate and baseline must be different subjects")

    baseline_refusals = baseline_subject_refusals(
        baseline_state, baseline_sha=opts["baseline_sha"], baseline_tree=before["baseline"]["tree"],
        candidate_sha=opts["candidate_sha"], supplied=bool(opts.get("approved_baseline_path")))
    if baseline_refusals:
        raise ValueError("invalid baseline approval: " + ", ".join(baseline_refusals))
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    mode = ACTUAL_MODE if getattr(deps, "is_real", False) else OFFLINE_MODE
    fake_model = not getattr(deps, "is_real", False)
    scenario_variants = {s: len(specs[s]["variants"]) for s in selected}
    schedule = build_schedule(selected, scenario_variants, policy["warmup_samples"], measured_n)
    protocol = {"warmups": policy["warmup_samples"], "measured": measured_n,
                "min_samples": policy["min_measured_samples"], "order": policy["order"],
                "schedule_sha256": canonical_sha(schedule), "scenario_ids": selected,
                "required_scenario_ids": required_ids, "partial": partial}
    started_ts = deps.now()
    model_id = opts["model_id"]
    missing = list(deps.preflight(lane, model_id))
    environment = deps.collect_environment(model_id, lane, policy["warmup_samples"]) if not missing else {}
    backends: dict[str, Any] = {}
    homes: dict[str, Path] = {}
    runs: list[dict] = []
    aborted = False
    runtime: dict[str, Any] = {}
    residency: dict[str, dict] = {"before": {}, "after": {}}
    try:
        if not missing:
            import tempfile
            for side in SIDES:
                homes[side] = Path(tempfile.mkdtemp(prefix=f"wisp-release-{side}-"))
                (homes[side] / "config.yaml").write_text(render_role_config(model_id))
                backends[side] = deps.spawn_backend(side, Path(opts[f"{side}_root"]), homes[side],
                                                    raw_dir / f"instrumentation.{side}.jsonl", run_id=run_id)
            for side, backend in backends.items():
                if not backend.wait_ready():
                    missing.append({"id": "backend_not_ready", "detail": f"{side} backend did not become ready"})
            if not missing:
                residency["before"] = {side: deps.residency(backend) for side, backend in backends.items()}
                for side, value in residency["before"].items():
                    if value != [model_id]:
                        missing.append({"id": "resident_model_mismatch",
                                        "detail": f"{side}: resident models are {value}; expected exactly {[model_id]}"})
            if not missing:
                payloads = render_leaf_payloads(corpus, deps.now())
                for side, backend in backends.items():
                    statuses = deps.push_leaves(backend, payloads)
                    if any(not 200 <= s < 300 for s in statuses):
                        missing.append({"id": "synthetic_leaf_sync_failed", "detail": f"{side}: {statuses}"})
        if not missing:
            tails = {side: InstrumentTail(raw_dir / f"instrumentation.{side}.jsonl") for side in SIDES}
            drivers = {side: deps.make_driver(backends[side]) for side in SIDES}
            runtime = {side: {"root": str(Path(opts[f"{side}_root"]).resolve()), "port": getattr(backends[side], "port", None),
                              "python": getattr(backends[side], "python", None)} for side in SIDES}
            with (raw_dir / "started.jsonl").open("a", buffering=1) as started_file, \
                    (raw_dir / "events.jsonl").open("a", buffering=1) as events_file:
                for item in schedule:
                    spec = specs[item["scenario"]]
                    prompt = spec["variants"][item["variant"]]
                    for slot, side in enumerate(item["order"]):
                        backend, driver = backends[side], drivers[side]
                        sink = HttpTurnDriver.new_sink()
                        cpu_before = backend.cpu_seconds()
                        started_ns = deps.clock_ns()
                        # Written BEFORE dispatch: even a hard kill leaves the in-flight sample identified.
                        started_file.write(json.dumps(
                            {"id": sample_id(side, item["scenario"], item["phase"], item["rep"]),
                             "side": side, "started_ns": started_ns}, sort_keys=True) + "\n")
                        interrupted: BaseException | None = None
                        try:
                            if spec["kind"] == "http":
                                await driver.run_http(spec, sink)
                            else:
                                await driver.run_turn(spec, prompt, sink)
                        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
                            sink["cancelled"] = True
                            interrupted = exc
                        except Exception as exc:
                            sink["transport_error"] = type(exc).__name__
                            aborted = True
                        finally:
                            ended_ns = deps.clock_ns()
                        cpu_after = backend.cpu_seconds()
                        identity = sample_identity(run_id, side, item, slot, prompt, started_ns, ended_ns)
                        cpu = UNKNOWN if UNKNOWN in (cpu_before, cpu_after) else round(cpu_after - cpu_before, 3)
                        # The partial stream of an interrupted sample is persisted here, synchronously, before
                        # the interruption propagates; it is graded as cancelled and never as a success.
                        events_file.write(json.dumps(
                            {"id": identity["id"], "identity": identity, "events": sink["events"],
                             "driver": {k: sink.get(k) for k in _DRIVER_KEYS},
                             "bounds": sample_bounds(sink, started_ns, ended_ns), "backend_cpu_s": cpu},
                            sort_keys=True) + "\n")
                        runs.append({"spec": spec, "identity": identity, "sink": sink, "cpu": cpu})
                        for tail in tails.values():
                            tail.refresh()
                        provisional, _ = finalize_sample(
                            spec=spec, corpus=corpus, driver_sink=sink, identity=identity,
                            instrument=sample_instrument({k: t.records for k, t in tails.items()}, side,
                                                         sample_bounds(sink, started_ns, ended_ns)))
                        progress({"sample": identity["id"], "outcome": provisional["outcome"], "provisional": True})
                        if interrupted is not None:
                            raise interrupted
                        if aborted:
                            break
                    if aborted:
                        break
            residency["after"] = {side: deps.residency(backend) for side, backend in backends.items()}
    except (KeyboardInterrupt, asyncio.CancelledError):
        aborted = True
    except Exception as exc:
        aborted = True
        missing.append({"id": "runtime_error", "detail": type(exc).__name__})
    finally:
        teardown, pending_interrupt = stop_all(backends)

    after = {side: deps.verify_subject(Path(opts[f"{side}_root"]), opts[f"{side}_sha"]) for side in SIDES}
    environment_after = deps.collect_environment(model_id, lane, policy["warmup_samples"]) if backends else {}
    logs = {side: read_instrument_lines(raw_dir / f"instrumentation.{side}.jsonl") for side in SIDES}
    headers = {side: instrument_header(logs[side]) for side in SIDES}
    samples = [build_sample_record(r["spec"], corpus, r["identity"], r["sink"], logs, r["cpu"]) for r in runs]
    with (raw_dir / "samples.jsonl").open("a") as samples_file:
        for record in samples:
            samples_file.write(json.dumps(record, sort_keys=True) + "\n")
    write_json(raw_dir / "environment.json", {"before": environment, "after": environment_after,
                                              "residency": residency})
    write_json(raw_dir / "provenance.json", {
        "run_id": run_id, "started_at": iso(started_ts), "schedule": schedule,
        "preflight_missing": missing, "subjects_before": before, "subjects_after": after,
        "baseline_refusals": baseline_refusals, "harness_version": HARNESS_VERSION, "teardown": teardown,
        "scenario_variants": {s: specs[s]["variants"] for s in selected},
        "note": "Throwaway state only. No user data, no secrets, no engine settings were read into this record."})
    summary = summarize(samples, bundle) if samples else {}
    lifecycle = lifecycle_summary(logs)
    if samples and not aborted:
        decision = decide(summary, bundle, baseline_state, lifecycle) if not partial else \
            {"verdict": "INCONCLUSIVE", "reasons": [{"code": "partial_run", "detail": "not a full release measurement"}],
             "comparison": {}}
    else:
        reasons = [{"code": "prerequisite_missing", "detail": m["id"] + ": " + m["detail"]} for m in missing]
        if aborted:
            reasons.append({"code": "aborted", "detail": "the run was interrupted"})
        decision = {"verdict": "INCONCLUSIVE", "reasons": reasons, "comparison": {}}
    requests = request_identity(samples, {r["identity"]["id"]: {"bounds": sample_bounds(
        r["sink"], r["identity"]["started_ns"], r["identity"]["ended_ns"])} for r in runs}, logs)
    findings = integrity_findings(model_id=model_id, env_before=environment, env_after=environment_after,
                                  residency=residency, headers=headers, requests=requests)
    unresolved = [side for side, row in teardown.items() if not row.get("ok")]
    if unresolved:
        findings.append({"code": "teardown_unresolved",
                         "detail": f"backend teardown did not complete cleanly: {unresolved}"})
    cohort = cohort_key({"lane": lane, "execution_mode": mode, "environment": environment,
                         "harness": {"script_sha256": harness_sha256()},
                         "corpus": {"sha256": bundle["corpus_sha256"]},
                         "policy": {"sha256": bundle["policy_sha256"]},
                         "runtime_identity": child_runtime(headers), "containment": containment_identity(headers)})
    if samples and not aborted and not partial:
        decision = apply_context_rules(decision, baseline_state, cohort, lane, policy, findings)
    elif unresolved:     # a partial, aborted or prerequisite-missing run is already non-passing; still say so
        decision = {**decision, "reasons": decision["reasons"] + [f for f in findings if f["code"] == "teardown_unresolved"]}
    decision = effective_verdict(decision, mode, fake_model)
    chats = {side: sum(1 for s in samples if s["side"] == side and s["phase"] == "measured"
                       and isinstance(s["diagnostics"].get("chat_calls"), int)
                       and s["diagnostics"]["chat_calls"] > 0) for side in SIDES}
    receipt = build_receipt(
        run_id=run_id, mode=mode, fake_model=fake_model, lane=lane, bundle=bundle, bundle_problems=problems,
        subjects={s: {"sha": opts[f"{s}_sha"], "tree": before[s]["tree"], "root": before[s]["root"],
                      "verified_before": before[s], "verified_after": after[s]} for s in SIDES},
        environment=environment, protocol=protocol, raw_files=raw_manifest(raw_dir), summary=summary,
        decision=decision, baseline_state=baseline_state, generated_at_ts=deps.now(), missing=missing,
        aborted=aborted, runtime=runtime, evidence={"attributed_engine_samples": chats},
        extra={"measurement_source": {"deps_class": type(deps).__name__, "release_capable": bool(mode == ACTUAL_MODE)},
               "runtime_identity": child_runtime(headers), "containment": containment_identity(headers),
               "lifecycle": lifecycle, "teardown": teardown})
    receipt_path = out / "receipt.json"
    write_json(receipt_path, receipt)
    if pending_interrupt is not None:
        raise pending_interrupt
    return VERDICT_EXIT[decision["verdict"]], receipt_path


# --------------------------------------------------------------------------
# The receipt checker: every conclusion is re-derived from the raw files
# --------------------------------------------------------------------------

def load_raw(raw_dir: Path, *, snapshot: dict[str, bytes] | None = None) -> dict:
    """Re-derive only from one immutable snapshot, never reopen hashed paths."""
    snapshot = raw_snapshot(raw_dir) if snapshot is None else snapshot
    def lines(name):
        return [json.loads(line) for line in snapshot.get(name, b"").decode("utf-8").splitlines() if line.strip()]
    def document(name):
        return json.loads(snapshot[name]) if name in snapshot else None
    samples = lines("samples.jsonl")
    event_rows = lines("events.jsonl")
    events: dict[str, dict] = {}
    duplicates: list[str] = []
    for row in event_rows:
        if row["id"] in events:
            duplicates.append(row["id"])
        events[row["id"]] = row
    return {"samples": samples, "events": events, "event_order": [r["id"] for r in event_rows],
            "duplicate_event_ids": duplicates,
            "logs": {side: lines(f"instrumentation.{side}.jsonl") for side in SIDES},
            "started": lines("started.jsonl"), "environment": document("environment.json"),
            "provenance": document("provenance.json")}


_IDENTITY_FIELDS = ("run_id", "id", "side", "scenario", "phase", "rep", "variant", "order_slot",
                    "started_ns", "ended_ns", "prompt_sha256")
_DERIVED_COUNTS = _COUNT_NAMES
_DEADLINE_SLACK_NS = 5_000_000_000


def _is_ns(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def verify_logs(logs: dict[str, list[dict]], *, run_id: str, roots: dict[str, Any]) -> list[str]:
    """Each backend's log must be one complete, ordered, gap-free record of that exact child."""
    problems: list[str] = []
    for side in SIDES:
        records = logs.get(side) or []
        if not records:
            problems.append(f"{side}:log_empty")
            continue
        header = records[0]
        if header.get("kind") != "header" or sum(1 for r in records if r.get("kind") == "header") != 1:
            problems.append(f"{side}:header_not_first_and_unique")
            continue
        if [r.get("seq") for r in records] != list(range(len(records))):
            problems.append(f"{side}:sequence_has_gaps_or_reordering")
        stamps = [r.get("t_ns") for r in records]
        if not all(_is_ns(t) for t in stamps) or any(b < a for a, b in zip(stamps, stamps[1:])):
            problems.append(f"{side}:timestamps_invalid_or_not_monotonic")
        if any(r.get("pid") != header.get("pid") for r in records):
            problems.append(f"{side}:records_from_more_than_one_process")
        if header.get("run_id") != run_id:
            problems.append(f"{side}:header_run_id_mismatch")
        if header.get("side") != side:
            problems.append(f"{side}:header_side_mismatch")
        if header.get("effect_guard") is not True:
            problems.append(f"{side}:effect_guard_not_recorded")
        if header.get("root") != roots.get(side):
            problems.append(f"{side}:header_root_mismatch")
        if sum(1 for r in records if r.get("kind") == "surfaces") != 1:
            problems.append(f"{side}:surfaces_record_missing_or_duplicated")
        elif "engine_http" not in (instrument_header(records) or {}).get("available", []):
            problems.append(f"{side}:engine_http_not_instrumented")
        if any(r.get("kind") == "_unparseable" for r in records):
            problems.append(f"{side}:unparseable_record")
    return problems


def verify_sample_binding(samples: list[dict], raw: dict, schedule: list[dict], bundle: dict,
                          run_id: str) -> list[str]:
    """Every raw sample must be exactly the scheduled one: same position, variant, slot and prompt bytes, with
    sane clocks, no overlap with its neighbours and nothing outside its own interval."""
    problems: list[str] = []
    specs = scenario_by_id(bundle)
    expected = [(item, slot, side) for item in schedule for slot, side in enumerate(item["order"])]
    if len(samples) != len(expected):
        problems.append(f"sample_count:{len(samples)}!={len(expected)}")
    ids = [s.get("id") for s in samples]
    if len(set(ids)) != len(ids):
        problems.append("duplicate_sample_ids")
    if raw["duplicate_event_ids"]:
        problems.append("duplicate_event_records:" + ",".join(sorted(set(raw["duplicate_event_ids"]))[:5]))
    if raw["event_order"] != ids:
        problems.append("event_records_do_not_match_samples_in_order")
    started_ids = [r.get("id") for r in raw["started"]]
    if started_ids != ids:
        problems.append("started_records_do_not_match_samples_in_order")
    previous_end = None
    for index, (sample, (item, slot, side)) in enumerate(zip(samples, expected)):
        spec, sid = specs[item["scenario"]], sample.get("id")
        want = {"run_id": run_id, "id": sample_id(side, item["scenario"], item["phase"], item["rep"]),
                "side": side, "scenario": item["scenario"], "phase": item["phase"], "rep": item["rep"],
                "variant": item["variant"], "order_slot": slot,
                "prompt_sha256": sha256_bytes(spec["variants"][item["variant"]].encode())}
        for key, value in want.items():
            if sample.get(key) != value or type(sample.get(key)) is not type(value):
                problems.append(f"{sid}:{key}_is_not_the_scheduled_value")
        started, ended = sample.get("started_ns"), sample.get("ended_ns")
        if not (_is_ns(started) and _is_ns(ended) and ended >= started):
            problems.append(f"{sid}:sample_clock_invalid")
            continue
        if previous_end is not None and started < previous_end:
            problems.append(f"{sid}:overlaps_or_precedes_the_previous_sample")
        previous_end = ended
        row = raw["events"].get(sid)
        if row is None:
            continue
        if {k: row.get("identity", {}).get(k) for k in _IDENTITY_FIELDS} != {k: sample.get(k) for k in _IDENTITY_FIELDS}:
            problems.append(f"{sid}:raw_identity_differs_from_sample")
        driver = row.get("driver") or {}
        bounds = row.get("bounds") or {}
        a, b = driver.get("abs_start_ns"), driver.get("abs_end_ns")
        if not (_is_ns(a) and _is_ns(b) and started <= a <= b <= ended):
            problems.append(f"{sid}:driver_interval_outside_the_sample")
            continue
        if bounds != {"start_ns": a, "end_ns": b}:
            problems.append(f"{sid}:window_bounds_not_the_driver_interval")
        span = b - a
        times = [e.get("t_ns") for e in row.get("events") or []]
        times += [x.get("t_ns") for x in driver.get("approvals") or []]
        times += [driver[k] for k in ("latency_ns", "stream_closed_ns") if driver.get(k) is not None]
        if not all(_is_ns(t) and t <= span for t in times):
            problems.append(f"{sid}:event_time_outside_the_sample_interval")
        event_times = [e.get("t_ns") for e in row.get("events") or []]
        if any(_is_ns(x) and _is_ns(y) and y < x for x, y in zip(event_times, event_times[1:])):
            problems.append(f"{sid}:events_not_in_time_order")
        deadline = spec.get("deadline_s")
        if deadline and span > deadline * 1_000_000_000 + _DEADLINE_SLACK_NS and not driver.get("deadline"):
            problems.append(f"{sid}:ran_past_its_deadline_without_recording_it")
    return problems


def verify_derivations(samples: list[dict], events: dict[str, dict], logs: dict[str, list[dict]],
                       bundle: dict) -> list[str]:
    """Regrade every sample from its raw events and the COMPLETE logs; windows are rebuilt from the
    persisted bounds, never taken from anything the run stored."""
    problems: list[str] = []
    specs, corpus = scenario_by_id(bundle), bundle["corpus"]
    for sample in samples:
        sid = sample.get("id")
        spec, raw = specs.get(sample.get("scenario")), events.get(sid)
        if spec is None:
            problems.append(f"unknown_scenario:{sid}")
            continue
        if raw is None or not isinstance(raw.get("bounds"), dict):
            problems.append(f"events_missing:{sid}")
            continue
        sink = {"events": raw["events"], **raw["driver"]}
        identity = {k: sample.get(k) for k in _IDENTITY_FIELDS}
        recomputed, _ = finalize_sample(spec=spec, corpus=corpus, driver_sink=sink, identity=identity,
                                        instrument=sample_instrument(logs, sample["side"], raw["bounds"]))
        for field in ("outcome", "grade", "metrics_ns"):
            if recomputed[field] != sample.get(field):
                problems.append(f"derivation_mismatch:{sid}:{field}")
        recorded = sample.get("diagnostics") or {}
        if any(recorded.get(k) != recomputed["diagnostics"].get(k) for k in _DERIVED_COUNTS):
            problems.append(f"derivation_mismatch:{sid}:diagnostics")
        if recorded.get("backend_cpu_s") != raw.get("backend_cpu_s"):
            problems.append(f"derivation_mismatch:{sid}:backend_cpu_s")
    return problems


def check_receipt(opts: argparse.Namespace, *, runner: Callable = run_cmd) -> tuple[int, dict]:
    """Malformed evidence is a structured refusal; never an unhandled success/crash."""
    try:
        return _check_receipt(opts, runner=runner)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as exc:
        return EXIT_REFUSED, {"receipt": str(opts.receipt), "verdict": "REFUSED",
                              "authorizes_release": False, "reasons": [],
                              "refusals": [{"code": "malformed_evidence", "detail": type(exc).__name__}]}


def _check_receipt(opts: argparse.Namespace, *, runner: Callable = run_cmd) -> tuple[int, dict]:
    """Verify a receipt against the raw evidence and the coordinator's expectations.

    Exit codes: 0 PASS, 1 BLOCK, 2 INCONCLUSIVE, 3 refused evidence, 4 usage.
    Nothing is accepted on the receipt's word: sample grades, timings, summaries and the verdict are
    recomputed from the raw files, and the raw files are bound by digest.

    Authenticity: the receipt's SHA-256 MUST be supplied from a record the measurement owner made when the
    run finished. It is what ties this package to a run; without it nothing here is accepted. Only a receipt
    produced by the live dependency class is accepted. `accept_fixture_source` is an API-only seam for the
    offline tests: the command line cannot set it, and a result obtained through it never authorizes a release.
    """
    refusals: list[dict] = []
    result: dict[str, Any] = {"receipt": str(opts.receipt), "refusals": refusals, "verdict": None, "reasons": [],
                              "authorizes_release": False}
    fixture_source = getattr(opts, "accept_fixture_source", None)

    def refuse(code: str, detail: str = "") -> None:
        refusals.append({"code": code, "detail": detail})

    def finish() -> tuple[int, dict]:
        if refusals:
            result["verdict"] = "REFUSED"
            return EXIT_REFUSED, result
        result["authorizes_release"] = result["verdict"] == "PASS" and fixture_source is None
        result["fixture_source"] = fixture_source is not None
        return VERDICT_EXIT[result["verdict"]], result

    receipt_path = Path(opts.receipt)
    try:
        with evidence_directory(receipt_path.parent) as package:
            raw_bytes = evidence_file(package, receipt_path.name, RECEIPT_MAX_BYTES)
            # Capture raw bytes before any hashing or parsing, through the same
            # retained parent descriptor; missing raw is reported below.
            try:
                raw_files = raw_snapshot(receipt_path.parent / "raw", parent_fd=package)
            except FileNotFoundError:
                raw_files = {}
        receipt = json.loads(raw_bytes)
    except OSError:
        refuse("receipt_missing", str(receipt_path))
        return finish()
    except ValueError:
        refuse("receipt_unreadable")
        return finish()
    if not isinstance(receipt, dict):
        refuse("receipt_not_an_object")
        return finish()
    if not opts.expect_receipt_sha256:
        refuse("receipt_digest_not_bound", "a release check needs the digest the measurement owner recorded")
    elif sha256_bytes(raw_bytes) != opts.expect_receipt_sha256:
        refuse("receipt_digest_mismatch")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        refuse("not_a_release_receipt", f"schema={receipt.get('schema')!r}")
        return finish()

    try:
        bundle = load_bundle(opts.bundle)
    except (OSError, ValueError):
        refuse("bundle_unreadable", str(opts.bundle))
        return finish()
    for problem in validate_bundle(bundle):
        refuse("bundle_invalid", problem)
    if bundle["corpus_sha256"] != (receipt.get("corpus") or {}).get("sha256") or \
            bundle["corpus_sha256"] != opts.expect_corpus_sha256:
        refuse("corpus_hash_mismatch")
    if bundle["policy_sha256"] != (receipt.get("policy") or {}).get("sha256") or \
            bundle["policy_sha256"] != opts.expect_policy_sha256:
        refuse("policy_hash_mismatch")
    if (receipt.get("harness") or {}).get("script_sha256") != opts.expect_harness_sha256:
        refuse("harness_hash_mismatch")
    if refusals:
        return finish()
    policy = bundle["policy"]

    source = receipt.get("measurement_source") or {}
    wanted_source = fixture_source or LIVE_DEPS_CLASS
    if source.get("deps_class") != wanted_source or source.get("release_capable") is not True:
        refuse("measurement_source_not_release_capable",
               f"deps_class={source.get('deps_class')!r}, release_capable={source.get('release_capable')!r}")
    if receipt.get("execution_mode") != ACTUAL_MODE:
        refuse("not_an_actual_measurement", f"execution_mode={receipt.get('execution_mode')!r}")
    if (receipt.get("inference") or {}).get("fake_model") is not False:
        refuse("fake_or_unverified_model")
    if receipt.get("lane") != opts.lane:
        refuse("lane_mismatch", f"receipt lane {receipt.get('lane')!r}, expected {opts.lane!r}")
    if receipt.get("aborted") or receipt.get("prerequisites_missing"):
        refuse("incomplete_run", "aborted or prerequisites missing")
    if (receipt.get("protocol") or {}).get("partial"):
        refuse("partial_run")

    now = opts.now if opts.now is not None else utc_now()
    generated = receipt.get("generated_at_ts")
    if type(generated) not in (int, float) or not math.isfinite(generated) or generated < 0:
        refuse("receipt_time_invalid")
        return finish()
    age = now - generated
    if age > policy["max_receipt_age_s"]:
        refuse("stale_receipt", f"{int(age)}s old")
    if age < -300:
        refuse("receipt_from_the_future")

    raw_dir = receipt_path.parent / "raw"
    declared = (receipt.get("raw") or {}).get("files") or {}
    actual = {name: sha256_bytes(data) for name, data in raw_files.items()}
    if not actual:
        refuse("raw_missing")
    for name, digest in declared.items():
        if actual.get(name) != digest:
            refuse("raw_digest_mismatch", name)
    for name in actual:
        if name not in declared:
            refuse("raw_file_not_declared", name)
    if canonical_sha(declared) != (receipt.get("raw") or {}).get("manifest_sha256"):
        refuse("raw_manifest_mismatch")

    subjects = {side: receipt.get(side) or {} for side in SIDES}
    for side, subject in subjects.items():
        sha = subject.get("sha")
        for stage in ("verified_before", "verified_after"):
            record = subject.get(stage) or {}
            if not subject_ok(record) or record.get("head") != sha or record.get("tree") != subject.get("tree"):
                refuse("subject_not_verified_clean_and_exact", f"{side}.{stage}")
        before, after = subject.get("verified_before") or {}, subject.get("verified_after") or {}
        if before.get("head") != after.get("head") or before.get("tree") != after.get("tree"):
            refuse("subject_changed_during_run", side)
        if not float(before.get("verified_at_ts") or 0) <= float(after.get("verified_at_ts") or 0):
            refuse("verification_order_invalid", side)
        if opts.repo:
            code, out, _ = runner(["git", "rev-parse", f"{sha}^{{tree}}"], cwd=str(opts.repo))
            if code or out.strip() != subject.get("tree"):
                refuse("subject_unknown_to_repo", side)
        worktree = getattr(opts, f"{side}_worktree", None)
        if worktree:
            again = verify_subject(Path(worktree), sha, runner=runner)
            if not subject_ok(again) or again.get("tree") != subject.get("tree"):
                refuse("worktree_reverification_failed", side)
    if subjects["candidate"].get("sha") != opts.expect_candidate_sha:
        refuse("candidate_sha_mismatch", f"receipt {subjects['candidate'].get('sha')!r}")
    if subjects["candidate"].get("sha") == subjects["baseline"].get("sha") or \
            subjects["candidate"].get("tree") == subjects["baseline"].get("tree"):
        refuse("candidate_and_baseline_not_different")
    if refusals:
        return finish()

    protocol = receipt.get("protocol") or {}
    scenario_ids = protocol.get("scenario_ids") or []
    required_ids = [s["id"] for s in required_scenarios(bundle)]
    if sorted(scenario_ids) != sorted(required_ids):
        refuse("required_scenarios_incomplete")
    measured_n = protocol.get("measured")
    if (not isinstance(measured_n, int) or measured_n < max(policy["min_measured_samples"], ABSOLUTE_MIN_SAMPLES)):
        refuse("under_sampled", f"measured={measured_n!r}")
    if protocol.get("warmups") != policy["warmup_samples"]:
        refuse("warmup_protocol_mismatch")
    if refusals:
        return finish()
    variant_counts = {s: len(scenario_by_id(bundle)[s]["variants"]) for s in scenario_ids}
    expected_schedule = build_schedule(scenario_ids, variant_counts, policy["warmup_samples"], measured_n)
    if canonical_sha(expected_schedule) != protocol.get("schedule_sha256"):
        refuse("schedule_digest_mismatch")

    try:
        raw = load_raw(raw_dir, snapshot=raw_files)
    except (OSError, ValueError, KeyError, TypeError):
        refuse("raw_unreadable")
        return finish()
    samples, events, logs = raw["samples"], raw["events"], raw["logs"]
    for problem in verify_alternation(samples, scenario_ids, policy["warmup_samples"], measured_n):
        refuse("incomparable_or_incomplete_samples", problem)
    if any(s.get("run_id") != receipt.get("run_id") for s in samples):
        refuse("sample_from_another_run")
    if refusals:
        return finish()
    for problem in verify_sample_binding(samples, raw, expected_schedule, bundle, receipt.get("run_id")):
        refuse("samples_not_bound_to_the_schedule", problem)
    roots = {side: (subjects[side].get("verified_before") or {}).get("root") for side in SIDES}
    for problem in verify_logs(logs, run_id=receipt.get("run_id"), roots=roots):
        refuse("instrumentation_log_invalid", problem)
    if refusals:
        return finish()

    for side in SIDES:
        model_samples = [s for s in samples if s["side"] == side and s["phase"] == "measured"
                         and ((scenario_by_id(bundle)[s["scenario"]].get("expect") or {}).get("model_calls") or {}).get("min")]
        if not any(isinstance(s["diagnostics"].get("chat_calls"), int) and s["diagnostics"]["chat_calls"] > 0
                   for s in model_samples):
            refuse("no_attributed_engine_evidence", side)
    for problem in verify_derivations(samples, events, logs, bundle):
        refuse("raw_derivation_mismatch", problem)
    provenance, environment_doc = raw["provenance"], raw["environment"]
    teardown = (provenance or {}).get("teardown")
    if not isinstance(teardown, dict) or set(teardown) != set(SIDES) or teardown != receipt.get("teardown"):
        refuse("teardown_not_recorded")
    elif any(not row.get("ok") for row in teardown.values()):
        refuse("teardown_unresolved", ",".join(side for side, row in teardown.items() if not row.get("ok")))
    if not isinstance(environment_doc, dict) or environment_doc.get("before") != receipt.get("environment"):
        refuse("environment_record_mismatch")
    headers = {side: instrument_header(logs[side]) for side in SIDES}
    lifecycle = lifecycle_summary(logs)
    if lifecycle != receipt.get("lifecycle"):
        refuse("lifecycle_mismatch")
    if child_runtime(headers) != receipt.get("runtime_identity") or \
            containment_identity(headers) != receipt.get("containment"):
        refuse("runtime_or_containment_identity_mismatch")
    if refusals:
        return finish()

    summary = summarize(samples, bundle)
    if summary != receipt.get("summary"):
        refuse("summary_mismatch")
    baseline_state, baseline_refusals = load_baseline_approval(opts.approved_baseline,
                                                               opts.approved_baseline_sha256)
    for code in baseline_refusals:
        refuse(code)
    if not baseline_refusals:
        for code in baseline_subject_refusals(
                baseline_state, baseline_sha=subjects["baseline"].get("sha"),
                baseline_tree=subjects["baseline"].get("tree"),
                candidate_sha=subjects["candidate"].get("sha"), supplied=bool(opts.approved_baseline)):
            refuse(code)
    if refusals:
        return finish()
    model_id = (receipt.get("environment") or {}).get("model", {}).get("id")
    findings = integrity_findings(
        model_id=model_id, env_before=environment_doc.get("before"), env_after=environment_doc.get("after"),
        residency=environment_doc.get("residency") or {}, headers=headers,
        requests=request_identity(samples, events, logs))
    decision = apply_context_rules(decide(summary, bundle, baseline_state, lifecycle), baseline_state,
                                   cohort_key(receipt), receipt["lane"], policy, findings)
    if decision["verdict"] != receipt.get("verdict"):
        refuse("receipt_verdict_mismatch", f"receipt says {receipt.get('verdict')!r}, evidence says {decision['verdict']!r}")
    result.update({"verdict": decision["verdict"], "reasons": decision["reasons"],
                   "comparison": decision["comparison"]})
    return finish()


def propose_baseline(receipt_path: Path, output: Path) -> Path:
    """Write a PROPOSAL. It is never approved, and it refuses to overwrite anything."""
    if Path(output).exists():
        raise FileExistsError(f"{output} exists; a baseline file is never overwritten")
    receipt_bytes = evidence_bytes(Path(receipt_path), RECEIPT_MAX_BYTES)
    receipt = json.loads(receipt_bytes)
    proposal = {"schema": BASELINE_SCHEMA, "status": "proposed",
                "approved_by": None, "review_ref": None, "approved_at": None,
                "subject": {"sha": (receipt.get("baseline") or {}).get("sha"),
                            "tree": (receipt.get("baseline") or {}).get("tree")},
                "cohort_key": cohort_key(receipt),
                "source_receipt_sha256": sha256_bytes(receipt_bytes),
                "instructions": "A reviewer must set status to approved and fill approved_by, review_ref and "
                                "approved_at, then supply this file's SHA-256 to `check` out of band. "
                                "Nothing in the harness approves or advances a baseline."}
    Path(output).write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n")
    return Path(output)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which means INCONCLUSIVE here
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)

    run = subs.add_parser("run", help="measure candidate vs baseline (a live, separately scoped step)")
    run.add_argument("--candidate-worktree", type=Path, required=True)
    run.add_argument("--candidate-sha", required=True)
    run.add_argument("--baseline-worktree", type=Path, required=True)
    run.add_argument("--baseline-sha", required=True)
    run.add_argument("--lane", choices=("desktop", "managed_mini"), default="desktop")
    run.add_argument("--model-id", required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    run.add_argument("--approved-baseline", type=Path)
    run.add_argument("--approved-baseline-sha256")
    run.add_argument("--scenarios", help="comma-separated ids; narrows a DIAGNOSTIC run only")
    run.add_argument("--samples", type=int, help="measured samples per scenario; below the policy floor needs --diagnostic")
    run.add_argument("--diagnostic", action="store_true", help="partial, non-gating run")
    run.add_argument("--python", default=sys.executable, help="interpreter for the spawned backends")
    run.add_argument("--candidate-port", type=int, default=18775)
    run.add_argument("--baseline-port", type=int, default=18776)
    run.add_argument("--execute-live-release-measurement", action="store_true", required=True,
                     help="acknowledges this talks to the resident engine and spawns two backends")

    check = subs.add_parser("check", help="the release gate: nonzero unless the evidence verifies and PASSES")
    check.add_argument("--receipt", type=Path, required=True)
    check.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    check.add_argument("--expect-candidate-sha", required=True)
    check.add_argument("--expect-corpus-sha256", required=True)
    check.add_argument("--expect-policy-sha256", required=True)
    check.add_argument("--expect-harness-sha256", required=True)
    check.add_argument("--approved-baseline", type=Path)
    check.add_argument("--approved-baseline-sha256")
    check.add_argument("--expect-receipt-sha256", required=True,
                       help="the receipt digest the measurement owner recorded when the run finished")
    check.add_argument("--lane", choices=("desktop", "managed_mini"), default="desktop")
    check.add_argument("--repo", type=Path, default=_REPO_ROOT,
                       help="repository used to confirm each sha and its tree")
    check.add_argument("--candidate-worktree", type=Path)
    check.add_argument("--baseline-worktree", type=Path)
    check.add_argument("--now", type=float, help=argparse.SUPPRESS)

    propose = subs.add_parser("propose-baseline", help="write an UNAPPROVED baseline proposal from a receipt")
    propose.add_argument("--receipt", type=Path, required=True)
    propose.add_argument("--output", type=Path, required=True)

    info = subs.add_parser("info", help="print the hashes a coordinator must bind")
    info.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)

    serve = subs.add_parser("serve", help=argparse.SUPPRESS)
    serve.add_argument("--root", required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--home", required=True)
    serve.add_argument("--instrument-out", required=True)
    serve.add_argument("--engine-port", type=int, default=8000)
    serve.add_argument("--run-id", default="")
    serve.add_argument("--side", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return serve_main(args)
    if args.command == "info":
        bundle = load_bundle(args.bundle)
        problems = validate_bundle(bundle)
        required = required_scenarios(bundle)
        schedule = build_schedule([s["id"] for s in required], {s["id"]: len(s["variants"]) for s in required},
                                  bundle["policy"]["warmup_samples"], bundle["policy"]["min_measured_samples"])
        print(json.dumps({"harness_sha256": harness_sha256(), "bundle_file_sha256": bundle["file_sha256"],
                          "corpus_version": bundle["corpus"]["version"], "corpus_sha256": bundle["corpus_sha256"],
                          "policy_version": bundle["policy"]["version"], "policy_sha256": bundle["policy_sha256"],
                          "required_scenarios": [s["id"] for s in required],
                          "min_measured_samples": bundle["policy"]["min_measured_samples"],
                          "warmup_samples": bundle["policy"]["warmup_samples"],
                          "full_schedule_sha256": canonical_sha(schedule), "bundle_problems": problems}, indent=2))
        return EXIT_USAGE if problems else 0
    if args.command == "propose-baseline":
        try:
            print(json.dumps({"proposal": str(propose_baseline(args.receipt, args.output)), "status": "proposed"}))
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return EXIT_REFUSED
        return 0
    if args.command == "check":
        code, result = check_receipt(args)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return code

    bundle = load_bundle(args.bundle)
    opts = {"candidate_root": args.candidate_worktree, "candidate_sha": args.candidate_sha,
            "baseline_root": args.baseline_worktree, "baseline_sha": args.baseline_sha,
            "lane": args.lane, "model_id": args.model_id, "output_dir": args.output_dir,
            "scenario_ids": args.scenarios.split(",") if args.scenarios else None,
            "samples": args.samples, "diagnostic": args.diagnostic,
            "approved_baseline_path": str(args.approved_baseline) if args.approved_baseline else None,
            "approved_baseline_sha256": args.approved_baseline_sha256}
    deps = LiveDeps(args.python, {"candidate": args.candidate_port, "baseline": args.baseline_port})
    try:
        code, receipt_path = asyncio.run(run_release(
            opts, deps, bundle, progress=lambda e: print(json.dumps(e), flush=True)))
    except (SubjectError, ValueError, FileExistsError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps({"receipt": str(receipt_path), "receipt_sha256": sha256_file(receipt_path),
                      "exit_code": code}))
    return code


if __name__ == "__main__":
    sys.exit(main())
