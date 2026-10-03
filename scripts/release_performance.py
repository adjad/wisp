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
  fake-model or offline-fixture result as a performance PASS.

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
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

HARNESS_VERSION = "release_performance/1"
BUNDLE_SCHEMA = "wisp.release_performance.bundle/1"
SAMPLE_SCHEMA = "wisp.release_performance.sample/1"
RECEIPT_SCHEMA = "wisp.release_performance.receipt/1"
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
                "baseline_correctness_failure", "unknown_gating_metric"):
        if (policy.get("outcomes") or {}).get(key) != "INCONCLUSIVE":
            problems.append(f"outcomes.{key} must be INCONCLUSIVE")
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
_INSTRUMENT_KINDS = {"engine_http": "engine_http", "authority_load": "authority_load",
                     "process_check": "process_check", "binding_check": "binding_check",
                     "peer_check": "peer_check"}


def instrument_counts(window_events: list[dict] | None, unavailable: list[str] | None) -> dict:
    if window_events is None or unavailable is None:
        return {name: UNKNOWN for name in
                ("engine_http_calls", "chat_calls", "engine_state_changes", "authority_loads",
                 "process_checks", "binding_checks", "peer_checks", "blocked_effects")}
    missing = set(unavailable)

    def count(kind: str, predicate: Callable[[dict], bool] = lambda e: True) -> Any:
        if _INSTRUMENT_KINDS.get(kind, kind) in missing:
            return UNKNOWN
        return sum(1 for e in window_events if e.get("kind") == kind and predicate(e))

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
            confirms = [c for c in analysis["confirms"] if c["tool"] == approval["tool"]]
            if not confirms:
                reasons.append("approval_boundary_not_reached")
            else:
                blob = json.dumps(confirms[0]["args"], sort_keys=True)
                patterns = approval.get("args_any_regex") or []
                if patterns and not any(re.search(p, blob) for p in patterns):
                    reasons.append("approval_arguments_mismatch")
            unexpected = [c for c in analysis["confirms"] if c["tool"] != approval["tool"]]
            if unexpected:
                reasons.append("unexpected_confirm:" + ",".join(str(c["tool"]) for c in unexpected))
            if any(a.get("approved") for a in driver.get("approvals", [])):
                reasons.append("harness_approved_an_effect")
        elif analysis["confirms"]:
            reasons.append("unexpected_confirm:" + ",".join(str(c["tool"]) for c in analysis["confirms"]))

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
                        home: Path | None = None,
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
        weights = sorted((p.name, p.stat().st_size) for p in model_dir.glob("*.safetensors"))
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
                                       "note": "names and sizes only; weight contents are not hashed"}},
        "generation_settings": {
            "model_settings": ({k: model_settings.get(k) for k in _GENERATION_KEYS if k in model_settings}
                               if isinstance(model_settings, dict) else UNKNOWN),
            "engine_sampling_defaults": settings.get("sampling") or UNKNOWN,
            "request_temperature": "omitted by Wisp; the engine profile applies"},
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


ALLOWED_EXEC = frozenset({"/usr/sbin/lsof", "/bin/ps"})  # engine attribution inspection only
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class EffectGuard:
    """Refuses real-world effects while leaving the attribution path intact.

    Blocks any process other than the two read-only inspectors the engine
    attribution uses, any network connection except loopback to the engine
    port, and any httpx request to anywhere else. Raw libuv sockets opened by
    a C extension are not interceptable from Python; the httpx and stdlib
    socket layers cover every path Wisp's own code uses.
    """

    def __init__(self, emit: Callable[..., None], *, allowed_exec: frozenset[str] = ALLOWED_EXEC,
                 allowed_ports: set[int] | frozenset[int] = frozenset({8000})) -> None:
        self.emit, self.allowed_exec, self.allowed_ports = emit, allowed_exec, set(allowed_ports)
        self._originals: list[tuple[Any, str, Any]] = []

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

    def install(self) -> "EffectGuard":
        import socket
        import subprocess as sp
        guard = self
        original_init = sp.Popen.__init__

        def popen_init(self_, args, *a, **k):
            argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
            first = os.fspath(argv[0]) if argv else ""
            first = first.decode() if isinstance(first, bytes) else first
            if first not in guard.allowed_exec:
                guard._block("exec", first or "<empty>")
            return original_init(self_, args, *a, **k)

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
        for name in ("system", "posix_spawn", "posix_spawnp", "execv", "execve", "execvp"):
            if hasattr(os, name):
                self._patch(os, name, lambda *a, _n=name, **k: guard._block("exec", f"os.{_n}"))
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
                if is_async:
                    async def send(self_, request, *a, **k):
                        check(request)
                        return await original(self_, request, *a, **k)
                else:
                    def send(self_, request, *a, **k):
                        check(request)
                        return original(self_, request, *a, **k)
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
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._file = open(path, "a", buffering=1, encoding="utf-8")

    def emit(self, kind: str, **fields: Any) -> None:
        record = {"kind": kind, "t_ns": time.monotonic_ns(), "pid": os.getpid(), **fields}
        with self._lock:
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


def _wrap_async_request(recorder: InstrumentRecorder, fn: Callable) -> Callable:
    import functools

    @functools.wraps(fn)
    async def wrapper(self_, request, *args, **kwargs):
        start = time.monotonic_ns()
        fields = {"method": str(request.method), "path": str(request.url.path)}
        try:
            response = await fn(self_, request, *args, **kwargs)
        except BaseException as exc:
            recorder.emit("engine_http", t_start_ns=start, t_headers_ns=time.monotonic_ns(),
                          error=type(exc).__name__, **fields)
            raise
        recorder.emit("engine_http", t_start_ns=start, t_headers_ns=time.monotonic_ns(),
                      status=getattr(response, "status_code", None), **fields)
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
        available.add("engine_http")

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

    names = ["engine_http", "authority_load", "process_check", "binding_check", "peer_check"]
    return sorted(available), [n for n in names if n not in available]


ACTIVE_GUARD: EffectGuard | None = None  # kept reachable so a test can undo what serve_main installs


def serve_main(args: argparse.Namespace) -> int:
    """Child entry point: run the candidate's own backend with the guard and instrumentation installed."""
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ["WISP_HOME"] = str(Path(args.home).resolve())
    recorder = InstrumentRecorder(args.instrument_out)
    global ACTIVE_GUARD
    ACTIVE_GUARD = EffectGuard(recorder.emit, allowed_ports={args.engine_port}).install()
    import importlib
    try:
        at = importlib.import_module("service.inference.attributed_transport")
        lp = importlib.import_module("service.inference.local_peer")
    except ImportError as exc:
        recorder.emit("header", pid=os.getpid(), error=f"import failed: {exc}", available=[],
                      unavailable=["engine_http", "authority_load", "process_check",
                                   "binding_check", "peer_check"])
        raise
    available, unavailable = install_instrumentation(recorder, at, lp)
    recorder.emit("header", pid=os.getpid(), root=str(root), harness=HARNESS_VERSION,
                  available=available, unavailable=unavailable, effect_guard=True)
    import uvicorn
    uvicorn.run("service.main:app", host="127.0.0.1", port=args.port, log_level="warning")
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
                            now = self.clock_ns() - start
                            sink["events"].append({"t_ns": now, "event": event})
                            kind = event.get("type")
                            if kind == "session":
                                session = event.get("id")
                            elif kind == "confirm":
                                reply = await client.post("/agent/approve", json={
                                    "session_id": session, "action_id": event.get("id"),
                                    "approved": False, "scope": "once"})
                                sink["approvals"].append({"t_ns": self.clock_ns() - start,
                                                          "action_id": event.get("id"), "approved": False,
                                                          "accepted": bool(reply.json().get("ok"))
                                                          if reply.status_code == 200 else False})
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


class BackendProcess:
    """One isolated backend spawned from one verified worktree."""

    def __init__(self, side: str, root: Path, port: int, home: Path, instrument_path: Path,
                 python: str, runner: Callable = run_cmd) -> None:
        self.side, self.root, self.port, self.home = side, Path(root), port, Path(home)
        self.instrument_path, self.python, self.runner = Path(instrument_path), python, runner
        self.proc: subprocess.Popen | None = None
        self.base_url = f"http://127.0.0.1:{port}"

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def command(self) -> list[str]:
        return [self.python, str(Path(__file__).resolve()), "serve", "--root", str(self.root),
                "--port", str(self.port), "--home", str(self.home),
                "--instrument-out", str(self.instrument_path), "--engine-port", "8000"]

    def start(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in ("WISP_BACKEND_URL",)}
        env["WISP_HOME"] = str(self.home)
        self.proc = subprocess.Popen(self.command(), cwd=str(self.root), env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


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
        """Exact missing prerequisites. Reads state only; it never loads, unloads or configures anything."""
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
        engine = engine_resident_models()
        if engine is None:
            missing.append({"id": "engine_unreachable_on_8000",
                            "detail": "no attributed oMLX engine answered on 127.0.0.1:8000"})
        elif engine != [model_id]:
            missing.append({"id": "resident_model_mismatch",
                            "detail": f"resident models are {engine}; expected exactly {[model_id]}"})
        return missing

    def spawn_backend(self, side: str, root: Path, home: Path, instrument_path: Path) -> BackendProcess:
        backend = BackendProcess(side, root, self.ports[side], home, instrument_path, self.python)
        backend.start()
        return backend

    def make_driver(self, backend: BackendProcess) -> HttpTurnDriver:
        return HttpTurnDriver(backend.base_url)

    def push_leaves(self, backend: BackendProcess, payloads: list[dict]) -> list[int]:
        import httpx
        statuses = []
        for payload in payloads:
            statuses.append(httpx.post(backend.base_url + payload["path"], json=payload["body"],
                                       timeout=30.0).status_code)
        return statuses


def engine_resident_models(timeout_s: float = 5.0) -> list[str] | None:
    """Resident model ids per the engine's own status endpoint, or None if unreachable.
    A read-only status call made only by a live release run."""
    import urllib.request
    try:
        settings = json.loads((Path.home() / ".omlx" / "settings.json").read_text())
        key = ((settings.get("auth") or {}).get("api_key")) or ""
        request = urllib.request.Request("http://127.0.0.1:8000/v1/models/status",
                                         headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read())
        return sorted(m["id"] for m in payload.get("models", []) if m.get("loaded"))
    except (OSError, ValueError, KeyError):
        return None


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
    for record in records:
        if record.get("kind") == "header":
            return record
    return None


def window_events(records: list[dict], start_ns: int, end_ns: int) -> list[dict]:
    return [r for r in records if r.get("kind") not in ("header", "_unparseable")
            and start_ns <= r.get("t_ns", -1) <= end_ns]


# --------------------------------------------------------------------------
# One sample
# --------------------------------------------------------------------------

def finalize_sample(*, spec: dict, corpus: dict, driver_sink: dict, instrument: dict,
                    identity: dict) -> tuple[dict, dict]:
    """Derive timings, counts and the grade from raw material. The checker calls this same
    function on the stored raw data and demands identical output."""
    analysis = analyze_events(driver_sink["events"])
    counts = instrument_counts(instrument.get("window"), instrument.get("unavailable"))
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
                                             "process_checks", "binding_checks", "peer_checks")},
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


def decide(summary: dict, bundle: dict, baseline: dict) -> dict:
    """Policy verdict from the summary. BLOCK beats INCONCLUSIVE beats PASS."""
    policy = bundle["policy"]
    block: list[dict] = []
    inconclusive: list[dict] = []
    comparison: dict[str, Any] = {}
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


def raw_manifest(raw_dir: Path) -> dict[str, str]:
    return {p.relative_to(raw_dir).as_posix(): sha256_file(p)
            for p in sorted(raw_dir.rglob("*")) if p.is_file()}


def load_baseline_approval(path: str | None, expected_sha256: str | None) -> tuple[dict, list[str]]:
    """Returns (state, refusals). The harness never creates or upgrades an approval; it only reads one."""
    refusals: list[str] = []
    if not path:
        return {"approved": False, "problem": "no approved baseline was supplied"}, refusals
    target = Path(path)
    if not target.is_file():
        return {"approved": False, "problem": f"approved baseline file is missing: {path}"}, refusals
    raw = target.read_bytes()
    if not expected_sha256:
        refusals.append("approved_baseline_digest_not_bound")
    elif sha256_bytes(raw) != expected_sha256:
        refusals.append("approved_baseline_digest_mismatch")
    try:
        doc = json.loads(raw)
    except ValueError:
        refusals.append("approved_baseline_unreadable")
        return {"approved": False, "problem": "unreadable"}, refusals
    if doc.get("schema") != BASELINE_SCHEMA:
        refusals.append("approved_baseline_wrong_schema")
    status_ok = (doc.get("status") == "approved" and doc.get("approved_by") and doc.get("review_ref")
                 and doc.get("approved_at") and FULL_SHA.fullmatch(str((doc.get("subject") or {}).get("sha", "")))
                 and isinstance(doc.get("cohort_key"), dict))
    if not status_ok:
        return {"approved": False, "problem": "baseline file is not an approved, reviewed baseline",
                "doc": doc}, refusals
    return {"approved": True, "problem": None, "doc": doc, "sha256": sha256_bytes(raw)}, refusals


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


def apply_context_rules(decision: dict, baseline_state: dict, cohort: dict, lane: str, policy: dict) -> dict:
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
    return {**decision, "verdict": verdict, "reasons": reasons}


def build_receipt(*, run_id: str, mode: str, fake_model: bool, lane: str, bundle: dict, bundle_problems: list[str],
                  subjects: dict, environment: dict, protocol: dict, raw_files: dict, summary: dict,
                  decision: dict, baseline_state: dict, generated_at_ts: float, missing: list[dict],
                  aborted: bool, runtime: dict, evidence: dict) -> dict:
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
    }


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

    out = Path(opts["output_dir"])
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} already has content; evidence is never overwritten")
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    before = {side: deps.verify_subject(Path(opts[f"{side}_root"]), opts[f"{side}_sha"]) for side in SIDES}
    bad = {s: r["problems"] for s, r in before.items() if not subject_ok(r)}
    if bad:
        raise SubjectError(f"subjects are not clean and exact: {bad}")
    if opts["candidate_sha"] == opts["baseline_sha"] or before["candidate"]["tree"] == before["baseline"]["tree"]:
        raise SubjectError("candidate and baseline must be different subjects")

    run_id = uuid.uuid4().hex
    mode = ACTUAL_MODE if getattr(deps, "is_real", False) else OFFLINE_MODE
    fake_model = not getattr(deps, "is_real", False)
    baseline_state, baseline_refusals = load_baseline_approval(opts.get("approved_baseline_path"),
                                                               opts.get("approved_baseline_sha256"))
    scenario_variants = {s: len(specs[s]["variants"]) for s in selected}
    schedule = build_schedule(selected, scenario_variants, policy["warmup_samples"], measured_n)
    protocol = {"warmups": policy["warmup_samples"], "measured": measured_n,
                "min_samples": policy["min_measured_samples"], "order": policy["order"],
                "schedule_sha256": canonical_sha(schedule), "scenario_ids": selected,
                "required_scenario_ids": required_ids, "partial": partial}
    started_ts = deps.now()
    missing = list(deps.preflight(lane, opts["model_id"]))
    environment = deps.collect_environment(opts["model_id"], lane, policy["warmup_samples"]) if not missing else {}
    backends: dict[str, Any] = {}
    homes: dict[str, Path] = {}
    samples: list[dict] = []
    aborted = False
    runtime: dict[str, Any] = {}
    try:
        if not missing:
            import tempfile
            for side in SIDES:
                homes[side] = Path(tempfile.mkdtemp(prefix=f"wisp-release-{side}-"))
                (homes[side] / "config.yaml").write_text(render_role_config(opts["model_id"]))
                backends[side] = deps.spawn_backend(side, Path(opts[f"{side}_root"]), homes[side],
                                                    raw_dir / f"instrumentation.{side}.jsonl")
            for side, backend in backends.items():
                if not backend.wait_ready():
                    missing.append({"id": "backend_not_ready", "detail": f"{side} backend did not become ready"})
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
            with (raw_dir / "samples.jsonl").open("a", buffering=1) as samples_file, \
                    (raw_dir / "events.jsonl").open("a", buffering=1) as events_file:
                for item in schedule:
                    spec = specs[item["scenario"]]
                    prompt = spec["variants"][item["variant"]]
                    for slot, side in enumerate(item["order"]):
                        backend, driver = backends[side], drivers[side]
                        sink = HttpTurnDriver.new_sink()
                        cpu_before = backend.cpu_seconds()
                        started_ns = deps.clock_ns()
                        try:
                            if spec["kind"] == "http":
                                await driver.run_http(spec, sink)
                            else:
                                await driver.run_turn(spec, prompt, sink)
                        finally:
                            ended_ns = deps.clock_ns()
                        cpu_after = backend.cpu_seconds()
                        records = tails[side].refresh()
                        header = instrument_header(records)
                        unavailable = header["unavailable"] if header else \
                            ["engine_http", "authority_load", "process_check", "binding_check", "peer_check"]
                        window = window_events(records, sink["abs_start_ns"] or started_ns,
                                               sink["abs_end_ns"] or ended_ns)
                        identity = {"run_id": run_id, "id": sample_id(side, item["scenario"], item["phase"], item["rep"]),
                                    "side": side, "scenario": item["scenario"], "phase": item["phase"],
                                    "rep": item["rep"], "variant": item["variant"], "order_slot": slot,
                                    "started_ns": started_ns, "ended_ns": ended_ns,
                                    "prompt_sha256": sha256_bytes(prompt.encode())}
                        record, _analysis = finalize_sample(
                            spec=spec, corpus=corpus, driver_sink=sink,
                            instrument={"window": window, "unavailable": unavailable}, identity=identity)
                        record["diagnostics"]["backend_cpu_s"] = (
                            UNKNOWN if UNKNOWN in (cpu_before, cpu_after) else round(cpu_after - cpu_before, 3))
                        events_file.write(json.dumps({"id": identity["id"], "events": sink["events"],
                                                      "driver": {k: sink[k] for k in
                                                                 ("approvals", "deadline", "cancelled", "transport_error",
                                                                  "http", "latency_ns", "stream_closed_ns")},
                                                      "instrument": {"unavailable": unavailable, "window": window}},
                                                     sort_keys=True) + "\n")
                        samples_file.write(json.dumps(record, sort_keys=True) + "\n")
                        samples.append(record)
                        progress({"sample": identity["id"], "outcome": record["outcome"]})
    except (KeyboardInterrupt, asyncio.CancelledError):
        aborted = True
    finally:
        for backend in backends.values():
            backend.stop()

    after = {side: deps.verify_subject(Path(opts[f"{side}_root"]), opts[f"{side}_sha"]) for side in SIDES}
    write_json(raw_dir / "provenance.json", {
        "run_id": run_id, "started_at": iso(started_ts), "schedule": schedule,
        "preflight_missing": missing, "subjects_before": before, "subjects_after": after,
        "baseline_refusals": baseline_refusals, "harness_version": HARNESS_VERSION,
        "scenario_variants": {s: specs[s]["variants"] for s in selected},
        "note": "Throwaway state only. No user data, no secrets, no engine settings were read into this record."})
    summary = summarize(samples, bundle) if samples else {}
    if samples and not aborted:
        decision = decide(summary, bundle, baseline_state) if not partial else \
            {"verdict": "INCONCLUSIVE", "reasons": [{"code": "partial_run", "detail": "not a full release measurement"}],
             "comparison": {}}
    else:
        reasons = [{"code": "prerequisite_missing", "detail": m["id"] + ": " + m["detail"]} for m in missing]
        if aborted:
            reasons.append({"code": "aborted", "detail": "the run was interrupted"})
        decision = {"verdict": "INCONCLUSIVE", "reasons": reasons, "comparison": {}}
    cohort = cohort_key({"lane": lane, "execution_mode": mode, "environment": environment,
                         "harness": {"script_sha256": harness_sha256()},
                         "corpus": {"sha256": bundle["corpus_sha256"]},
                         "policy": {"sha256": bundle["policy_sha256"]}})
    if samples and not aborted and not partial:
        decision = apply_context_rules(decision, baseline_state, cohort, lane, policy)
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
        aborted=aborted, runtime=runtime, evidence={"attributed_engine_samples": chats})
    receipt_path = out / "receipt.json"
    write_json(receipt_path, receipt)
    return VERDICT_EXIT[decision["verdict"]], receipt_path


# --------------------------------------------------------------------------
# The receipt checker: every conclusion is re-derived from the raw files
# --------------------------------------------------------------------------

def load_raw(raw_dir: Path) -> tuple[list[dict], dict[str, dict], dict[str, list[dict]]]:
    def lines(path: Path) -> list[dict]:
        out = []
        for line in path.read_text().splitlines() if path.is_file() else []:
            if line.strip():
                out.append(json.loads(line))
        return out
    samples = lines(raw_dir / "samples.jsonl")
    events = {r["id"]: r for r in lines(raw_dir / "events.jsonl")}
    instrumentation = {side: lines(raw_dir / f"instrumentation.{side}.jsonl") for side in SIDES}
    return samples, events, instrumentation


_IDENTITY_FIELDS = ("run_id", "id", "side", "scenario", "phase", "rep", "variant", "order_slot",
                    "started_ns", "ended_ns", "prompt_sha256")
_DERIVED_COUNTS = ("engine_http_calls", "chat_calls", "engine_state_changes", "authority_loads",
                   "process_checks", "binding_checks", "peer_checks", "blocked_effects")


def verify_derivations(samples: list[dict], events: dict[str, dict], instrumentation: dict[str, list[dict]],
                       bundle: dict) -> list[str]:
    problems: list[str] = []
    specs, corpus = scenario_by_id(bundle), bundle["corpus"]
    file_lines = {side: {canonical_json(r) for r in records} for side, records in instrumentation.items()}
    for sample in samples:
        sid = sample.get("id")
        spec, raw = specs.get(sample.get("scenario")), events.get(sid)
        if spec is None:
            problems.append(f"unknown_scenario:{sid}")
            continue
        if raw is None:
            problems.append(f"events_missing:{sid}")
            continue
        sink = {"events": raw["events"], **raw["driver"]}
        window = raw["instrument"]["window"]
        for event in window:
            if canonical_json(event) not in file_lines.get(sample["side"], set()):
                problems.append(f"window_event_not_in_instrumentation:{sid}")
                break
        identity = {k: sample.get(k) for k in _IDENTITY_FIELDS}
        recomputed, _ = finalize_sample(spec=spec, corpus=corpus, driver_sink=sink, identity=identity,
                                        instrument={"window": window, "unavailable": raw["instrument"]["unavailable"]})
        for field in ("outcome", "grade", "metrics_ns"):
            if recomputed[field] != sample.get(field):
                problems.append(f"derivation_mismatch:{sid}:{field}")
        recorded = sample.get("diagnostics") or {}
        if any(recorded.get(k) != recomputed["diagnostics"].get(k) for k in _DERIVED_COUNTS):
            problems.append(f"derivation_mismatch:{sid}:diagnostics")
    return problems


def check_receipt(opts: argparse.Namespace, *, runner: Callable = run_cmd) -> tuple[int, dict]:
    """Verify a receipt against the raw evidence and the coordinator's expectations.

    Exit codes: 0 PASS, 1 BLOCK, 2 INCONCLUSIVE, 3 refused evidence, 4 usage.
    Nothing is accepted on the receipt's word: sample grades, timings, summaries and the verdict are
    recomputed from the raw files, and the raw files are bound by digest.
    """
    refusals: list[dict] = []
    result: dict[str, Any] = {"receipt": str(opts.receipt), "refusals": refusals, "verdict": None, "reasons": []}

    def refuse(code: str, detail: str = "") -> None:
        refusals.append({"code": code, "detail": detail})

    def finish() -> tuple[int, dict]:
        if refusals:
            result["verdict"] = "REFUSED"
            return EXIT_REFUSED, result
        return VERDICT_EXIT[result["verdict"]], result

    receipt_path = Path(opts.receipt)
    try:
        raw_bytes = receipt_path.read_bytes()
        receipt = json.loads(raw_bytes)
    except OSError:
        refuse("receipt_missing", str(receipt_path))
        return finish()
    except ValueError:
        refuse("receipt_unreadable")
        return finish()
    if opts.expect_receipt_sha256 and sha256_bytes(raw_bytes) != opts.expect_receipt_sha256:
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
    age = now - float(receipt.get("generated_at_ts") or 0)
    if age > policy["max_receipt_age_s"]:
        refuse("stale_receipt", f"{int(age)}s old")
    if age < -300:
        refuse("receipt_from_the_future")

    raw_dir = receipt_path.parent / "raw"
    declared = (receipt.get("raw") or {}).get("files") or {}
    actual = raw_manifest(raw_dir) if raw_dir.is_dir() else {}
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
        samples, events, instrumentation = load_raw(raw_dir)
    except (OSError, ValueError, KeyError):
        refuse("raw_unreadable")
        return finish()
    for problem in verify_alternation(samples, scenario_ids, policy["warmup_samples"], measured_n):
        refuse("incomparable_or_incomplete_samples", problem)
    if any(s.get("run_id") != receipt.get("run_id") for s in samples):
        refuse("sample_from_another_run")
    if refusals:
        return finish()

    for side in SIDES:
        header = next((r for r in instrumentation[side] if r.get("kind") == "header"), None)
        subject_root = (subjects[side].get("verified_before") or {}).get("root")
        if (not header or header.get("effect_guard") is not True or header.get("root") != subject_root
                or "engine_http" not in (header.get("available") or [])):
            refuse("instrumentation_header_invalid", side)
        model_samples = [s for s in samples if s["side"] == side and s["phase"] == "measured"
                         and ((scenario_by_id(bundle)[s["scenario"]].get("expect") or {}).get("model_calls") or {}).get("min")]
        if not any(isinstance(s["diagnostics"].get("chat_calls"), int) and s["diagnostics"]["chat_calls"] > 0
                   for s in model_samples):
            refuse("no_attributed_engine_evidence", side)
    for problem in verify_derivations(samples, events, instrumentation, bundle):
        refuse("raw_derivation_mismatch", problem)
    if refusals:
        return finish()

    summary = summarize(samples, bundle)
    if summary != receipt.get("summary"):
        refuse("summary_mismatch")
    baseline_state, baseline_refusals = load_baseline_approval(opts.approved_baseline,
                                                               opts.approved_baseline_sha256)
    for code in baseline_refusals:
        refuse(code)
    if baseline_state.get("approved"):
        approval = baseline_state["doc"]
        if approval["subject"]["sha"] != subjects["baseline"].get("sha"):
            refuse("baseline_subject_not_the_approved_one")
        if approval["subject"]["sha"] == subjects["candidate"].get("sha"):
            refuse("baseline_equals_candidate")
    if refusals:
        return finish()
    decision = apply_context_rules(decide(summary, bundle, baseline_state), baseline_state,
                                   cohort_key(receipt), receipt["lane"], policy)
    if decision["verdict"] != receipt.get("verdict"):
        refuse("receipt_verdict_mismatch", f"receipt says {receipt.get('verdict')!r}, evidence says {decision['verdict']!r}")
    result.update({"verdict": decision["verdict"], "reasons": decision["reasons"],
                   "comparison": decision["comparison"]})
    return finish()


def propose_baseline(receipt_path: Path, output: Path) -> Path:
    """Write a PROPOSAL. It is never approved, and it refuses to overwrite anything."""
    if Path(output).exists():
        raise FileExistsError(f"{output} exists; a baseline file is never overwritten")
    receipt_bytes = Path(receipt_path).read_bytes()
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
    check.add_argument("--expect-receipt-sha256")
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
