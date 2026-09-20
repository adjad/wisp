#!/usr/bin/env python3
"""Reproducible, side-effect-free Wisp/Laya routing benchmark.

The script never dispatches a Wisp tool. Model commands require an already
downloaded local snapshot and run exactly one requested model per process.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any

import psutil


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
QUESTIONS = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
FIXTURES = json.loads((HERE / "fixtures.json").read_text(encoding="utf-8"))
PREFLIGHT = json.loads((HERE / "preflight.json").read_text(encoding="utf-8"))
LABELS = {
    key: set(value.get("criteria", []))
    for key, value in QUESTIONS["questions"].items()
    if value["type"] == "choice"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_text(value: str) -> str:
    return value.replace(str(REPO), "<repo>").replace(str(Path.home()), "<home>")


def display_path(path: Path) -> str:
    return sanitize_text(str(path.resolve()))


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - index) + ordered[hi] * (index - lo)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expand_prompt(case: dict[str, Any]) -> str:
    if "prompt" in case:
        return case["prompt"]
    repeat = case["prompt_repeat"]
    return repeat["text"] * int(repeat["count"]) + repeat.get("suffix", "")


def state_for_model(case: dict[str, Any]) -> str:
    fields = []
    if case.get("last_user"):
        fields.append("Previous user: " + case["last_user"])
    if case.get("last_assistant"):
        fields.append("Previous assistant: " + case["last_assistant"])
    if case.get("last_tools"):
        fields.append("Previous tools: " + case["last_tools"])
    fields.append("Current user: " + expand_prompt(case))
    return "\n".join(fields)


def fixture_hashes() -> dict[str, str]:
    return {name: sha256(HERE / name) for name in ("preflight.json", "questions.json", "fixtures.json")}


def machine_facts() -> dict[str, Any]:
    def command(*args: str) -> str | None:
        try:
            return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=10).strip()
        except Exception:
            return None

    vm = psutil.virtual_memory()
    return {
        "captured_at": utc_now(),
        "platform": platform.platform(),
        "macos": platform.mac_ver()[0],
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version,
        "python_executable": sanitize_text(sys.executable),
        "hardware_model": command("sysctl", "-n", "hw.model"),
        "cpu_brand": command("sysctl", "-n", "machdep.cpu.brand_string"),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "memory_total_bytes": vm.total,
        "git_head": command("git", "-C", str(REPO), "rev-parse", "HEAD"),
        "git_branch": command("git", "-C", str(REPO), "branch", "--show-current"),
    }


def validate_fixtures() -> dict[str, Any]:
    errors: list[str] = []
    ids: set[str] = set()
    required = {
        "id", "tags", "tool_needed", "domain", "operation", "risk", "disposition",
        "required_tool_groups", "forbidden_tools", "argument_required",
    }
    for case in FIXTURES["cases"]:
        missing = required - set(case)
        if missing:
            errors.append(f"{case.get('id', '<missing>')}: missing {sorted(missing)}")
        case_id = case.get("id")
        if case_id in ids:
            errors.append(f"duplicate id {case_id}")
        ids.add(case_id)
        if not isinstance(expand_prompt(case), str) or not expand_prompt(case).strip():
            errors.append(f"{case_id}: empty prompt")
        for field in ("domain", "operation", "risk", "disposition"):
            if case.get(field) not in LABELS[field]:
                errors.append(f"{case_id}: invalid {field}={case.get(field)!r}")
        if not all(isinstance(group, list) and group for group in case.get("required_tool_groups", [])):
            errors.append(f"{case_id}: required tool groups must be nonempty lists")
    if errors:
        raise ValueError("\n".join(errors))
    counts = Counter(tag for case in FIXTURES["cases"] for tag in case["tags"])
    return {"cases": len(ids), "tag_counts": dict(sorted(counts.items())), "hashes": fixture_hashes()}


def observed_rss() -> int:
    return psutil.Process().memory_info().rss


def max_rss_bytes() -> int:
    # macOS reports bytes; Linux reports KiB.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(raw if sys.platform == "darwin" else raw * 1024)


def model_spec(name: str) -> dict[str, Any]:
    for item in PREFLIGHT["models"]:
        if item["name"] == name:
            return item
    raise ValueError(f"Unknown pinned model {name!r}")


def verify_local_model(name: str, directory: Path) -> dict[str, Any]:
    spec = model_spec(name)
    if directory.name != spec["revision"]:
        raise ValueError(f"Snapshot directory {directory.name!r} is not pinned revision {spec['revision']}")
    config_path = directory / "coreml_config.json"
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("repository") != spec["repository"]:
        raise ValueError(f"Repository mismatch: {config.get('repository')} != {spec['repository']}")
    if config.get("package_sha256") != spec["package_sha256"]:
        raise ValueError("Package hash recorded in coreml_config.json does not match preflight")
    if config.get("shape") != spec["shape"]:
        raise ValueError("Export shape does not match preflight")
    checked = {}
    for relative, expected in config.get("files", {}).items():
        path = directory / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = {"bytes": path.stat().st_size, "sha256": sha256(path)}
        if actual != expected:
            raise ValueError(f"File mismatch for {relative}: {actual} != {expected}")
        checked[relative] = actual
    return {"config": config, "verified_files": checked}


def normalize_model_answer(result: dict[str, Any]) -> dict[str, Any]:
    answers = result["answers"]
    return {
        "tool_needed": answers["tool_needed"]["noul"] >= 0.5,
        "domain": answers["domain"]["choice"],
        "operation": answers["operation"]["choice"],
        "risk": answers["risk"]["choice"],
        "disposition": answers["disposition"]["choice"],
        "confidence": {key: value["confidence"] for key, value in answers.items()},
        "act_probability": {key: value["action"]["act_probability"] for key, value in answers.items()},
    }


MEMORY_TOOLS = {"remember", "recall", "forget", "clear_memory", "search_conversations"}
REMINDER_TOOLS = {
    "add_reminder", "update_reminder", "search_reminders", "complete_reminder",
    "clear_reminders", "clear_past_reminders", "schedule_task",
}
CONTACT_TOOLS = {"lookup_contact", "contact_dates"}
FINANCE_TOOLS = {"get_stock_price", "convert_currency"}
SHELL_TOOLS = {"run_shell", "create_tool"}
MEDIA_TOOLS = {
    "spotify", "play_media", "play_radio", "stop_radio", "play_podcast",
    "play_audiobook", "play_ambient", "text_to_speech",
}
DESTRUCTIVE_TOOLS = {
    "clear_memory", "forget", "clear_reminders", "clear_past_reminders", "cancel_event",
    "cancel_scheduled_send", "clear_clipboard", "delete_path", "trash_file", "uninstall_app",
}
OUTBOUND_TOOLS = {"send_message", "send_email", "reply_to_email", "forward_email", "schedule_send"}


def tool_domain(name: str, category: str) -> str:
    if name in MEMORY_TOOLS:
        return "memory"
    if name in REMINDER_TOOLS:
        return "reminders"
    if name in CONTACT_TOOLS:
        return "contacts"
    if name in FINANCE_TOOLS:
        return "finance"
    if name in SHELL_TOOLS:
        return "shell"
    if name in MEDIA_TOOLS:
        return "media"
    if "email" in name or category.startswith("email_") or name in {"triage_inbox", "unsubscribe"}:
        return "mail"
    if "message" in name or category.startswith("messages_") or name == "schedule_send":
        return "messages"
    if category.startswith("calendar_") or name in {"get_upcoming", "get_past_events", "find_free_time"}:
        return "calendar"
    if category.startswith("notes_"):
        return "notes"
    if category.startswith("fs_") or name in {"list_dir", "read_file", "write_file"}:
        return "files"
    if category == "web_read":
        return "web"
    if category == "compute":
        return "compute"
    if category.startswith("system_") or category in {"app_control", "browser_history_read", "timer_write"}:
        return "device"
    return "unknown"


def operation_allows(name: str, category: str, operation: str) -> bool:
    if operation == "read":
        return ("read" in category or category in {"compute", "wisp_admin"}
                or name in {"get_upcoming", "get_past_events", "find_free_time", "calculate"})
    if operation == "write":
        return any(token in category for token in ("write", "send", "draft", "triage")) or name == "remember"
    if operation == "delete":
        return name in DESTRUCTIVE_TOOLS or "delete" in category
    if operation == "execute":
        return category in {"app_control", "system_write", "timer_write", "shell"} or name in SHELL_TOOLS
    return operation in {"compound", "unknown", "clarify", "answer"}


def candidate_assessment(prediction: dict[str, Any], case: dict[str, Any], inventory: dict[str, str]) -> dict[str, Any]:
    if not case["required_tool_groups"]:
        return {"pass": not case["tool_needed"], "candidate_count": 0, "candidates": []}
    if prediction["domain"] in {"none", "unknown", "multi"}:
        return {"pass": False, "candidate_count": 0, "candidates": [],
                "reason": "domain does not identify a usable single-domain candidate set"}
    candidates = sorted(
        name for name, category in inventory.items()
        if tool_domain(name, category) == prediction["domain"]
        and operation_allows(name, category, prediction["operation"])
    )
    passed = all(set(group).intersection(candidates) for group in case["required_tool_groups"])
    return {"pass": passed, "candidate_count": len(candidates), "candidates": candidates}


def run_model(args: argparse.Namespace) -> dict[str, Any]:
    validation = validate_fixtures()
    directory = Path(args.local_dir).resolve()
    verified = verify_local_model(args.model_name, directory)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    before_rss = observed_rss()
    before_max = max_rss_bytes()
    load_started = time.perf_counter_ns()
    import coremltools
    import laya_coreml
    agent = laya_coreml.load(directory, local_files_only=True)
    cold_load_ms = (time.perf_counter_ns() - load_started) / 1_000_000
    after_load_rss = observed_rss()

    sys.path.insert(0, str(REPO))
    scratch = tempfile.TemporaryDirectory(prefix="wisp-laya-inventory-")
    os.environ["WISP_HOME"] = scratch.name
    import service.tools  # noqa: F401
    from service.tools.registry import REGISTRY, is_tool_routable
    inventory = {name: tool.category for name, tool in REGISTRY.items() if is_tool_routable(name)}

    cases = []
    peak_rss = after_load_rss
    for case in FIXTURES["cases"]:
        state = state_for_model(case)
        question_lengths = {}
        prepare_error = None
        try:
            items, _ = agent.prepare(state, QUESTIONS["questions"])
            question_lengths = {
                qid: len(item["ids"])
                for qid, item in zip(QUESTIONS["questions"], items)
            }
        except Exception as exc:  # retained as evidence; prediction still runs
            prepare_error = f"{type(exc).__name__}: {exc}"
        raw_state_tokens = len(agent.tok(state, add_special_tokens=False)["input_ids"])
        started = time.perf_counter_ns()
        try:
            raw = agent.predict(state, QUESTIONS["questions"])
            latency_ms = (time.perf_counter_ns() - started) / 1_000_000
            prediction = normalize_model_answer(raw)
            assessment = candidate_assessment(prediction, case, inventory)
            error = None
        except Exception as exc:
            latency_ms = (time.perf_counter_ns() - started) / 1_000_000
            raw = None
            prediction = None
            assessment = None
            error = {
                "type": type(exc).__name__, "message": str(exc),
                "traceback": sanitize_text(traceback.format_exc(limit=8)),
            }
        peak_rss = max(peak_rss, observed_rss())
        max_len = int(agent.cfg.get("max_len", agent.shape["max_length"]))
        if error and "supports at most" in error["message"]:
            capacity_outcome = "reject"
        elif raw_state_tokens + 8 > max_len:
            capacity_outcome = "truncate"
        else:
            capacity_outcome = "accept"
        cases.append({
            "id": case["id"], "tags": case["tags"], "state": state,
            "raw_state_tokens": raw_state_tokens, "prepared_question_tokens": question_lengths,
            "prepare_error": prepare_error, "latency_ms": latency_ms, "raw": raw,
            "prediction": prediction, "candidate_assessment": assessment, "error": error,
            "capacity_outcome": capacity_outcome,
            "capacity_expected": case.get("capacity_expectation", {}).get(args.model_name),
        })

    representative_ids = [
        "EN-001", "EN-003", "EN-009", "EN-016", "NT-002", "NG-001",
        "SF-002", "CP-002", "CX-001", "ML-002", "ML-005", "UN-002",
    ]
    by_id = {case["id"]: case for case in FIXTURES["cases"]}
    # One unrecorded warm-up per shape/content family before timing repetitions.
    for case_id in representative_ids:
        agent.predict(state_for_model(by_id[case_id]), QUESTIONS["questions"])
    latency_samples: list[dict[str, Any]] = []
    for repetition in range(args.warm_repeats):
        for case_id in representative_ids:
            started = time.perf_counter_ns()
            agent.predict(state_for_model(by_id[case_id]), QUESTIONS["questions"])
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            latency_samples.append({"case_id": case_id, "repetition": repetition, "ms": elapsed})
            peak_rss = max(peak_rss, observed_rss())

    values = [sample["ms"] for sample in latency_samples]
    result = {
        "schema_version": 1,
        "kind": "laya_coreml_model",
        "started_at": utc_now(),
        "model": args.model_name,
        "repository": model_spec(args.model_name)["repository"],
        "revision": model_spec(args.model_name)["revision"],
        "local_dir": display_path(directory),
        "compute_units": getattr(agent, "compute_units", None),
        "coremltools_version": coremltools.__version__,
        "laya_coreml_version": getattr(laya_coreml, "__version__", "unknown"),
        "validation": validation,
        "verified_model": verified,
        "machine": machine_facts(),
        "memory": {
            "rss_before_load_bytes": before_rss,
            "rss_after_load_bytes": after_load_rss,
            "rss_load_delta_bytes": after_load_rss - before_rss,
            "rss_observed_peak_bytes": peak_rss,
            "rss_peak_delta_bytes": peak_rss - before_rss,
            "ru_maxrss_before_bytes": before_max,
            "ru_maxrss_end_bytes": max_rss_bytes(),
            "scope": "Current benchmark process RSS; Core ML accelerator allocations may not be fully attributed.",
        },
        "cold_load_ms": cold_load_ms,
        "warm_latency": {
            "repeats_per_case": args.warm_repeats,
            "representative_case_ids": representative_ids,
            "samples": latency_samples,
            "p50_ms": percentile(values, 0.50),
            "p95_ms": percentile(values, 0.95),
            "mean_ms": statistics.fmean(values),
            "unit": "one complete five-question routing classification (five serial Core ML calls)",
        },
        "inventory": {"routable_tools": len(inventory), "categories": dict(sorted(Counter(inventory.values()).items()))},
        "cases": cases,
        "finished_at": utc_now(),
    }
    scratch.cleanup()
    return result


def route_tool_names(decision: Any) -> set[str]:
    names = set(decision.tool_subset or ())
    names.update(name for name, _ in decision.direct_calls)
    names.update(decision.tool_argument_bindings)
    names.update(name for group in decision.required_tool_groups for name in group)
    names.update(name for item in decision.conditional_tools for name in item[:2])
    names.update(decision.narration_after)
    if decision.force_first_tool:
        names.add(decision.force_first_tool)
    return names


def baseline_prediction(decision: Any, inventory: dict[str, str]) -> dict[str, Any]:
    names = route_tool_names(decision)
    focused = set(name for name, _ in decision.direct_calls)
    focused.update(name for group in decision.required_tool_groups for name in group)
    focused.update(decision.tool_argument_bindings)
    focused.update(name for item in decision.conditional_tools for name in item[:2])
    if decision.force_first_tool:
        focused.add(decision.force_first_tool)
    # The lexical fallback intentionally pins generic escape hatches. They are
    # availability, not evidence that the user's primary domain is shell/memory.
    classified_names = focused or (names - {"run_shell", "recall", "create_tool", "use_skill"})
    domains = {tool_domain(name, inventory.get(name, "unknown")) for name in classified_names}
    domains.discard("unknown")
    # Contact lookup is a support step for an outbound channel, not a second
    # requested domain (for example, lookup_contact -> send_message).
    if len(domains) > 1:
        domains.discard("contacts")
    reason = (decision.reason + " " + decision.resolved_request).lower()
    clarifies = decision.clarify_channel or decision.clarify_target or "clarif" in reason or "ask " in reason
    if len(domains) > 1:
        domain = "multi"
    elif domains:
        domain = next(iter(domains))
    else:
        domain = "none" if not decision.needs_tools and not clarifies else "unknown"
    if clarifies:
        operation = "clarify"
    elif "compound" in reason or len(decision.required_tool_groups) > 1 or len(domains) > 1:
        operation = "compound"
    elif classified_names & DESTRUCTIVE_TOOLS:
        operation = "delete"
    elif classified_names & SHELL_TOOLS or any(inventory.get(name) in {"app_control", "system_write", "timer_write"} for name in classified_names):
        operation = "execute"
    elif any(any(token in inventory.get(name, "") for token in ("write", "send", "draft", "triage")) for name in classified_names):
        operation = "write"
    elif classified_names:
        operation = "read"
    else:
        operation = "answer" if not clarifies else "clarify"
    if classified_names & SHELL_TOOLS:
        risk = "shell"
    elif classified_names & DESTRUCTIVE_TOOLS:
        risk = "destructive"
    elif classified_names & OUTBOUND_TOOLS:
        risk = "outbound_write"
    elif operation in {"write", "execute"}:
        risk = "reversible_write"
    elif domain in {"mail", "messages", "calendar", "reminders", "notes", "contacts", "files", "memory", "multi"} and classified_names:
        risk = "sensitive_read"
    else:
        risk = "none"
    if "unsupported" in reason or "unavailable" in reason:
        disposition = "unsupported"
    elif clarifies:
        disposition = "clarify"
    elif decision.needs_tools:
        disposition = "route"
    else:
        disposition = "no_tool"
    return {"tool_needed": bool(decision.needs_tools), "domain": domain, "operation": operation,
            "risk": risk, "disposition": disposition}


async def _baseline_cases(route: Any, inventory: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for case in FIXTURES["cases"]:
        started = time.perf_counter_ns()
        decision = await route(
            expand_prompt(case), last_user=case.get("last_user"),
            last_assistant=case.get("last_assistant"), last_tools=case.get("last_tools"),
        )
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        observed = route_tool_names(decision)
        groups_ok = all(set(group).intersection(observed) for group in case["required_tool_groups"])
        forbidden_hit = sorted(set(case["forbidden_tools"]).intersection(observed))
        if case["tool_needed"]:
            full_pass = bool(decision.needs_tools and groups_ok and not forbidden_hit)
        else:
            full_pass = bool(not decision.needs_tools and not forbidden_hit)
        rows.append({
            "id": case["id"], "tags": case["tags"], "latency_ms": latency_ms,
            "prediction": baseline_prediction(decision, inventory),
            "route_decision": decision.as_dict(),
            "route_metadata": {
                "tool_subset": decision.tool_subset, "force_first_tool": decision.force_first_tool,
                "expect_tool_first": decision.expect_tool_first, "multi_round": decision.multi_round,
                "narration_after": sorted(decision.narration_after),
                "clarify_channel": decision.clarify_channel, "clarify_target": decision.clarify_target,
            },
            "observed_tools": sorted(observed), "required_groups_reachable": groups_ok,
            "forbidden_hits": forbidden_hit, "full_route_pass": full_pass,
        })
    return rows


def run_baseline(args: argparse.Namespace) -> dict[str, Any]:
    validation = validate_fixtures()
    scratch = tempfile.TemporaryDirectory(prefix="wisp-laya-baseline-")
    os.environ["WISP_HOME"] = scratch.name
    sys.path.insert(0, str(REPO))
    before_rss = observed_rss()
    started = time.perf_counter_ns()
    import service.tools  # noqa: F401
    from service.router.router import route
    from service.tools.registry import REGISTRY, is_tool_routable
    cold_load_ms = (time.perf_counter_ns() - started) / 1_000_000
    after_load_rss = observed_rss()
    inventory = {name: tool.category for name, tool in REGISTRY.items() if is_tool_routable(name)}
    cases = asyncio.run(_baseline_cases(route, inventory))
    standard = [case for case in FIXTURES["cases"] if "over_capacity" not in case["tags"]]
    warm_samples = []
    for repetition in range(args.warm_repeats):
        for case in standard:
            tick = time.perf_counter_ns()
            asyncio.run(route(
                expand_prompt(case), last_user=case.get("last_user"),
                last_assistant=case.get("last_assistant"), last_tools=case.get("last_tools"),
            ))
            warm_samples.append({
                "case_id": case["id"], "repetition": repetition,
                "ms": (time.perf_counter_ns() - tick) / 1_000_000,
            })
    values = [row["ms"] for row in warm_samples]
    result = {
        "schema_version": 1, "kind": "wisp_baseline", "started_at": utc_now(),
        "name": "unchanged Wisp source router", "git_head": machine_facts()["git_head"],
        "validation": validation, "machine": machine_facts(), "cold_load_ms": cold_load_ms,
        "memory": {
            "rss_before_load_bytes": before_rss, "rss_after_load_bytes": after_load_rss,
            "rss_load_delta_bytes": after_load_rss - before_rss,
            "rss_observed_peak_bytes": observed_rss(),
            "scope": "Current benchmark process RSS; baseline uses no classifier model.",
        },
        "warm_latency": {
            "repeats_per_case": args.warm_repeats, "samples": warm_samples,
            "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
            "mean_ms": statistics.fmean(values), "unit": "one full source-router decision",
        },
        "inventory": {"routable_tools": len(inventory), "categories": dict(sorted(Counter(inventory.values()).items()))},
        "cases": cases, "finished_at": utc_now(),
    }
    scratch.cleanup()
    return result


def fraction(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def binary_auroc(positive: list[float], negative: list[float]) -> float | None:
    if not positive or not negative:
        return None
    wins = sum(a > b for a in positive for b in negative)
    ties = sum(a == b for a in positive for b in negative)
    return (wins + 0.5 * ties) / (len(positive) * len(negative))


def score_run(raw: dict[str, Any]) -> dict[str, Any]:
    truth = {case["id"]: case for case in FIXTURES["cases"]}
    rows = [row for row in raw["cases"] if "over_capacity" not in truth[row["id"]]["tags"]]
    available = [row for row in rows if row.get("prediction") is not None]
    tool_cases = [row for row in available if truth[row["id"]]["tool_needed"]]
    positive_predictions = [row for row in available if row["prediction"]["tool_needed"]]
    safety = [row for row in available if truth[row["id"]]["risk"] in {"sensitive_read", "outbound_write", "destructive", "shell"}]
    fallback = [row for row in available if truth[row["id"]]["disposition"] in {"clarify", "unsupported", "unknown"}]
    def exact(field: str, subset: list[dict[str, Any]] = available) -> dict[str, Any]:
        return fraction(sum(row["prediction"][field] == truth[row["id"]][field] for row in subset), len(subset))
    safety_errors = []
    dangerous_underclassifications = []
    risk_rank = {"none": 0, "unknown": 0, "sensitive_read": 1, "reversible_write": 2,
                 "outbound_write": 3, "destructive": 4, "shell": 5}
    for row in safety:
        expected = truth[row["id"]]
        if raw["kind"] == "wisp_baseline":
            if not row["full_route_pass"]:
                safety_errors.append(row["id"])
        elif (not row["prediction"]["tool_needed"]
              or row["prediction"]["risk"] != expected["risk"]
              or row["prediction"]["disposition"] != expected["disposition"]
              or not (row.get("candidate_assessment") or {}).get("pass", False)):
            safety_errors.append(row["id"])
        if (not row["prediction"]["tool_needed"]
                or risk_rank[row["prediction"]["risk"]] < risk_rank[expected["risk"]]):
            dangerous_underclassifications.append(row["id"])
    if raw["kind"] == "wisp_baseline":
        route_complete = fraction(sum(row["full_route_pass"] for row in rows), len(rows))
        candidate = fraction(sum(row["required_groups_reachable"] for row in tool_cases), len(tool_cases))
        # route() prepares availability/contracts; Ling generates remaining
        # arguments later. This router-only run does not measure that model step.
        argument = fraction(0, 0)
    else:
        candidate_rows = [row for row in tool_cases if row.get("candidate_assessment")]
        candidate = fraction(sum(row["candidate_assessment"]["pass"] for row in candidate_rows), len(candidate_rows))
        # Typed decisions cannot encode RouteDecision or generate arguments, even on a correct label.
        route_complete = fraction(0, len(rows))
        argument = fraction(0, sum(truth[row["id"]]["argument_required"] for row in rows))
    capacity = [
        {"id": row["id"], "outcome": row.get("capacity_outcome"),
         "expected": row.get("capacity_expected"), "pass": row.get("capacity_expected") == row.get("capacity_outcome"),
         "error": row.get("error")}
        for row in raw["cases"] if "over_capacity" in truth[row["id"]]["tags"]
    ] if raw["kind"] != "wisp_baseline" else []
    cohorts = {}
    for tag in ("english", "multilingual", "no_tool", "negation", "safety", "compound", "context", "unknown"):
        subset = [row for row in available if tag in truth[row["id"]]["tags"]]
        cohorts[tag] = {
            "cases": len(subset),
            "tool_needed_accuracy": exact("tool_needed", subset),
            "domain_accuracy": exact("domain", subset),
            "operation_accuracy": exact("operation", subset),
        }
    selective_domain = []
    tool_score_diagnostic = None
    confident_tool_intent = None
    if raw["kind"] != "wisp_baseline":
        for threshold in (0.0, 0.25, 0.5, 0.75, 0.9):
            retained = [row for row in available if row["prediction"]["confidence"]["domain"] >= threshold]
            selective_domain.append({
                "threshold": threshold,
                "coverage": len(retained) / len(available) if available else None,
                "accuracy": (sum(row["prediction"]["domain"] == truth[row["id"]]["domain"] for row in retained)
                             / len(retained) if retained else None),
            })
        positives, negatives, action_values = [], [], []
        for row in rows:
            if not row.get("raw"):
                continue
            score = row["raw"]["answers"]["tool_needed"]["noul"]
            (positives if truth[row["id"]]["tool_needed"] else negatives).append(score)
            action_values.extend(answer["action"]["act_probability"] for answer in row["raw"]["answers"].values())
        tool_score_diagnostic = {
            "auroc": binary_auroc(positives, negatives),
            "positive_min_median_max": [min(positives), statistics.median(positives), max(positives)],
            "negative_min_median_max": [min(negatives), statistics.median(negatives), max(negatives)],
            "action_probability_unique": sorted(set(action_values)),
        }
    else:
        confident_rows = [
            (row, bool(row["route_metadata"]["expect_tool_first"]
                       or row["route_decision"]["direct_calls"]
                       or row["route_decision"]["required_tool_groups"]
                       or row["route_decision"]["tool_argument_bindings"]
                       or row["route_metadata"]["force_first_tool"]))
            for row in rows
        ]
        confident_positives = [(row, predicted) for row, predicted in confident_rows if predicted]
        confident_tool_intent = {
            "accuracy": fraction(sum(predicted == truth[row["id"]]["tool_needed"] for row, predicted in confident_rows), len(confident_rows)),
            "recall": fraction(sum(predicted for row, predicted in confident_rows if truth[row["id"]]["tool_needed"]),
                               sum(truth[row["id"]]["tool_needed"] for row, _ in confident_rows)),
            "precision": fraction(sum(truth[row["id"]]["tool_needed"] for row, _ in confident_positives), len(confident_positives)),
            "note": "Confident tool intent uses expect_tool_first or a concrete execution contract; needs_tools itself also means tools are merely available for Ling to choose automatically.",
        }
    return {
        "name": raw.get("model", raw.get("name")), "kind": raw["kind"],
        "evaluated_cases": len(rows), "failed_cases": len(rows) - len(available),
        "tool_needed_accuracy": exact("tool_needed"),
        "tool_needed_recall": fraction(sum(row["prediction"]["tool_needed"] for row in tool_cases), len(tool_cases)),
        "tool_needed_precision": fraction(
            sum(truth[row["id"]]["tool_needed"] for row in positive_predictions), len(positive_predictions)),
        "domain_accuracy": exact("domain"), "operation_accuracy": exact("operation"),
        "risk_accuracy": exact("risk"), "disposition_accuracy": exact("disposition"),
        "fallback_accuracy": exact("disposition", fallback),
        "fallback_overroute": fraction(sum(row["prediction"]["disposition"] == "route" for row in fallback), len(fallback)),
        "safety_sensitive_errors": {"count": len(safety_errors), "denominator": len(safety), "ids": safety_errors},
        "dangerous_underclassifications": {"count": len(dangerous_underclassifications), "ids": dangerous_underclassifications},
        "candidate_required_group_recall": candidate,
        "full_route_decision_sufficiency": route_complete,
        "argument_generation_sufficiency": argument,
        "cold_load_ms": raw["cold_load_ms"],
        "warm_p50_ms": raw["warm_latency"]["p50_ms"], "warm_p95_ms": raw["warm_latency"]["p95_ms"],
        "memory": raw["memory"], "capacity": capacity,
        "cohorts": cohorts,
        "selective_domain_accuracy": selective_domain,
        "tool_score_diagnostic": tool_score_diagnostic,
        "baseline_confident_tool_intent": confident_tool_intent,
    }


def markdown_report(summary: dict[str, Any]) -> str:
    scored = summary["runs"]
    lines = [
        "# Laya Core ML models for Wisp routing — benchmark report", "",
        f"Generated `{summary['generated_at']}` from `{summary['git_head']}`. All prompts are synthetic; no tool was dispatched.", "",
        "## Results", "",
        "| System | Tool recall | Domain acc. | Operation acc. | Safety errors | Candidate recall | Full RouteDecision | Warm p50 / p95 | Cold load | RSS load delta |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in scored:
        pct = lambda item: "n/a" if item["rate"] is None else f"{100*item['rate']:.1f}%"
        safety = f"{run['safety_sensitive_errors']['count']}/{run['safety_sensitive_errors']['denominator']}"
        memory = run["memory"].get("rss_load_delta_bytes", 0) / (1024**2)
        lines.append(
            f"| {run['name']} | {pct(run['tool_needed_recall'])} | {pct(run['domain_accuracy'])} | "
            f"{pct(run['operation_accuracy'])} | {safety} | {pct(run['candidate_required_group_recall'])} | "
            f"{pct(run['full_route_decision_sufficiency'])} | {run['warm_p50_ms']:.2f} / {run['warm_p95_ms']:.2f} ms | "
            f"{run['cold_load_ms']:.1f} ms | {memory:.1f} MiB |"
        )
    lines += [
        "", "## Interpretation", "",
        "Laya outputs five independent typed answers. It does not emit Wisp's required tool groups, forbidden tools, direct calls, ordering, clarification flags, resolved follow-up, argument bindings, or grounded arguments. Consequently, raw label accuracy is not full routing sufficiency.", "",
        "The `multi` label also does not decompose a compound request into its component domains. Candidate recall therefore gives compound cases no credit unless a model actually identifies a usable single-domain candidate set; this is intentional because a broad all-tools menu would defeat Wisp's retrieval objective.", "",
        "See `summary.json` and the per-run raw JSON for exact case-level outputs, probabilities, token counts, errors, latency samples, memory scope, and capacity behavior.", "",
    ]
    return "\n".join(lines)


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    raw_runs = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.inputs]
    heads = {run.get("git_head") or run.get("machine", {}).get("git_head") for run in raw_runs}
    result = {
        "schema_version": 1, "generated_at": utc_now(), "git_head": sorted(heads)[0] if len(heads) == 1 else sorted(str(x) for x in heads),
        "fixture_hashes": fixture_hashes(), "runs": [score_run(run) for run in raw_runs],
        "limitations": [
            "Synthetic fixtures measure routing semantics, not end-to-end task success.",
            "Process RSS does not fully attribute Core ML accelerator/unified-memory allocations.",
            "Cold load is first load in a fresh process, but operating-system Core ML compilation caches were not purged.",
            "Laya probabilities were used as published; this benchmark did not fit thresholds or recalibrate on the evaluation set.",
            "The Wisp baseline classifier labels are derived from its full RouteDecision for comparison; full-route reachability is the authoritative baseline grade.",
        ],
    }
    if args.report:
        Path(args.report).write_text(markdown_report(result), encoding="utf-8")
    return result


def write_json(value: dict[str, Any], path: str | None) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--out")
    baseline = sub.add_parser("baseline")
    baseline.add_argument("--warm-repeats", type=int, default=25)
    baseline.add_argument("--out", required=True)
    model = sub.add_parser("model")
    model.add_argument("--model-name", required=True)
    model.add_argument("--local-dir", required=True)
    model.add_argument("--warm-repeats", type=int, default=25)
    model.add_argument("--out", required=True)
    aggregate = sub.add_parser("summarize")
    aggregate.add_argument("inputs", nargs="+")
    aggregate.add_argument("--out", required=True)
    aggregate.add_argument("--report")
    args = parser.parse_args()
    if args.command == "validate":
        result = validate_fixtures()
        write_json(result, args.out)
    elif args.command == "baseline":
        write_json(run_baseline(args), args.out)
    elif args.command == "model":
        write_json(run_model(args), args.out)
    else:
        write_json(summarize(args), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
