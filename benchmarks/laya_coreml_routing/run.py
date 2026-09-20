#!/usr/bin/env python3
"""Reproducible, synthetic-only Wisp/Laya Core ML routing benchmark."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import signal
import statistics
import subprocess
import sys
import time
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = Path(__file__).with_name("cases.json")
BASE_SHA = "411516dfb47a1ce637362f992ad1f5f8800c97a4"
WALL_LIMIT_S = 45 * 60
WARM_REPS = 40

MODELS = {
    "multilingual_ane": {
        "repo": "aac6fef/laya-multilingual-coreml-ane",
        "revision": "39d6a9b3d0f67f06da74fbade6121ea134cbdb21",
        "compute_units": "cpu_ne",
        "declared_shape": {"batch_size": 1, "max_length": 96, "max_options": 32},
    },
    "multilingual_gpu": {
        "repo": "aac6fef/laya-multilingual-coreml",
        "revision": "8139e9089273319512c730218903784074133187",
        "compute_units": "cpu_gpu",
        "declared_shape": {"batch_size": 1, "max_length": 1024, "max_options": 32},
    },
    "typed_decisions_gpu": {
        "repo": "aac6fef/laya-typed-decisions-coreml",
        "revision": "28d24fa8d67a3264556b23391ec6c3fd98573056",
        "compute_units": "cpu_gpu",
        "declared_shape": {"batch_size": 1, "max_length": 1024, "max_options": 32},
    },
}

QUESTIONS = {
    "tool_needed": {
        "type": "noul",
        "instructions": "Must Wisp use a local or web tool to fulfill this request?",
        "criteria": {"false": "answer without tools", "true": "a tool is required"},
    },
    "domain": {
        "type": "choice",
        "instructions": "Choose the primary Wisp routing domain.",
        "criteria": [
            "chat", "calendar", "email", "messages", "memory", "files", "device",
            "media", "web", "finance_weather", "coding_compute", "multiple", "unsupported",
        ],
    },
    "operation": {
        "type": "choice",
        "instructions": "Choose the requested operation.",
        "criteria": [
            "answer", "read", "create", "update", "delete", "send", "execute",
            "search", "multi_step", "clarify", "refuse",
        ],
    },
    "multi_tool": {
        "type": "noul",
        "instructions": "Does fulfillment require multiple distinct tool steps?",
        "criteria": {"false": "zero or one tool step", "true": "multiple tool steps"},
    },
    "fallback": {
        "type": "choice",
        "instructions": "Choose Wisp's safe next step.",
        "criteria": ["answer", "tools", "clarify", "decline"],
    },
}

MUTATING_OPERATIONS = {"create", "update", "delete", "send", "execute", "multi_step"}
FIELDS = ("tool_needed", "domain", "operation", "multi_tool", "fallback")
DOMAINS = set(QUESTIONS["domain"]["criteria"])
OPERATIONS = set(QUESTIONS["operation"]["criteria"])
FALLBACKS = set(QUESTIONS["fallback"]["criteria"])


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_cases() -> list[dict[str, Any]]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases.json must contain a nonempty list")
    seen = set()
    for case in cases:
        required = {"id", "prompt", *FIELDS, "safety", "fail_closed", "locale"}
        missing = required - set(case)
        if missing:
            raise ValueError(f"case is missing fields {sorted(missing)}: {case!r}")
        if case["id"] in seen:
            raise ValueError(f"duplicate case id: {case['id']}")
        seen.add(case["id"])
        if not isinstance(case["prompt"], str) or not case["prompt"].strip():
            raise ValueError(f"empty prompt: {case['id']}")
        if type(case["tool_needed"]) is not bool or type(case["multi_tool"]) is not bool:
            raise ValueError(f"boolean labels required: {case['id']}")
        if case["domain"] not in DOMAINS or case["operation"] not in OPERATIONS or case["fallback"] not in FALLBACKS:
            raise ValueError(f"unknown routing label: {case['id']}")
    return cases


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def _pct(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * quantile + 0.999999)))
    return ordered[index]


def _versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for package in (
        "laya-coreml", "coremltools", "huggingface-hub", "numpy", "tokenizers",
        "safetensors", "psutil", "PyYAML",
    ):
        try:
            out[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            out[package] = None
    return out


def _run_text(command: list[str]) -> str:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as exc:  # environment evidence should not abort the run
        return f"unavailable: {type(exc).__name__}: {exc}"


def _hardware_summary() -> str:
    raw = _run_text(["system_profiler", "SPHardwareDataType", "SPDisplaysDataType"])
    allowed = (
        "Model Name:", "Model Identifier:", "Chip:", "Total Number of Cores:",
        "Memory:", "Chipset Model:", "Type:", "Total Number of Cores:", "Metal Support:",
    )
    return "\n".join(line.strip() for line in raw.splitlines() if line.strip().startswith(allowed))


def environment() -> dict[str, Any]:
    return {
        "created_at_unix": time.time(),
        "repository_base_sha": BASE_SHA,
        "repository_head": _run_text(["git", "rev-parse", "HEAD"]),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "hardware": _hardware_summary(),
        "packages": _versions(),
        "wall_limit_seconds": WALL_LIMIT_S,
        "warm_repetitions": WARM_REPS,
        "cases_sha256": _sha256(CASES_PATH),
        "synthetic_only": True,
        "tools_executed": False,
    }


def download_models(models_root: Path, deadline: float) -> list[dict[str, Any]]:
    from huggingface_hub import snapshot_download

    def deadline_expired(signum, frame):
        raise TimeoutError("benchmark download exceeded the shared wall-time budget")

    records = []
    for key, spec in MODELS.items():
        target = models_root / key
        started = time.perf_counter()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("benchmark wall-time budget exhausted before download")
        previous_handler = signal.signal(signal.SIGALRM, deadline_expired)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        try:
            resolved = Path(snapshot_download(
                spec["repo"], revision=spec["revision"], local_dir=target,
                allow_patterns=[
                    "README.md", "coreml_config.json", "rl_agent_config.json",
                    "validation.json", "encoder/config.json", "tokenizer/*",
                    "model.mlpackage/**", "host_weights.safetensors",
                ],
            ))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        manifest = json.loads((resolved / "coreml_config.json").read_text(encoding="utf-8"))
        checks = {}
        for name, expected in manifest["files"].items():
            path = resolved / name
            actual = _sha256(path)
            checks[name] = {
                "bytes": path.stat().st_size,
                "sha256": actual,
                "matches_manifest": actual == expected["sha256"],
            }
        if not all(row["matches_manifest"] for row in checks.values()):
            raise RuntimeError(f"checksum mismatch in {spec['repo']}")
        records.append({
            "key": key, "repo": spec["repo"], "revision": spec["revision"],
            "path": str(resolved.resolve()), "download_seconds": time.perf_counter() - started,
            "bytes_verified": sum(row["bytes"] for row in checks.values()),
            "manifest": manifest, "files": checks,
        })
    return records


def _tools_from_decision(decision: Any) -> set[str]:
    names = set(decision.tool_subset or ()) | set(decision.tool_argument_bindings)
    names.update(name for name, _ in decision.direct_calls)
    names.update(name for group in decision.required_tool_groups for name in group)
    names.update(name for item in decision.conditional_tools for name in item[:2])
    if decision.force_first_tool:
        names.add(decision.force_first_tool)
    return names


def _tool_domain(name: str) -> str | None:
    low = name.lower()
    if low == "lookup_contact":
        return None
    if any(part in low for part in ("calendar", "event", "reminder", "upcoming", "free_time", "video_call")):
        return "calendar"
    if "email" in low or low in {"scan_subscriptions"}:
        return "email"
    if any(part in low for part in ("message", "conversation", "contact")):
        return "messages"
    if any(part in low for part in ("note", "memory", "remember", "recall", "forget")):
        return "memory"
    if any(part in low for part in ("file", "folder", "path", "document", "pdf")):
        return "files"
    if any(part in low for part in ("music", "volume", "speech", "audio", "photo", "media")):
        return "media"
    if any(part in low for part in ("weather", "stock", "finance", "astronomy")):
        return "finance_weather"
    if any(part in low for part in ("web", "http", "maps", "travel", "transit")):
        return "web"
    if any(part in low for part in ("calculate", "shell", "code", "tool")):
        return "coding_compute"
    if any(part in low for part in (
        "system", "battery", "window", "app", "wifi", "bluetooth", "screen", "shortcut",
        "clipboard", "brightness", "display", "disk", "process",
    )):
        return "device"
    return None


def _tool_operation(name: str) -> str:
    low = name.lower()
    if any(part in low for part in ("send_", "reply_", "forward_")):
        return "send"
    if any(part in low for part in ("delete", "trash", "clear_", "cancel_", "forget")):
        return "delete"
    if any(part in low for part in ("update", "move_", "rename", "mark_", "toggle", "set_volume")):
        return "update"
    if any(part in low for part in ("add_", "create", "draft", "write_", "remember", "schedule_", "append_")):
        return "create"
    if any(part in low for part in ("open_", "launch", "restart", "run_shell", "music", "play")):
        return "execute"
    if any(part in low for part in ("search", "find_", "web_")):
        return "search"
    return "read"


def _baseline_prediction(decision: Any) -> dict[str, Any]:
    tools = _tools_from_decision(decision)
    domains = {_tool_domain(name) for name in tools} - {None}
    if len(domains) > 1:
        domain = "multiple"
    elif domains:
        domain = next(iter(domains))
    else:
        reason = (decision.reason or "").lower()
        if any(word in reason for word in ("cannot", "unavailable", "unsupported", "no vision")):
            domain = "unsupported"
        elif decision.role == "coding":
            domain = "coding_compute"
        else:
            domain = "chat"
    operations = {_tool_operation(name) for name in tools}
    multi = bool(
        len(decision.direct_calls) > 1 or len(decision.required_tool_groups) > 1
        or decision.conditional_tools or len(domains) > 1
    )
    if decision.clarify_channel or decision.clarify_target:
        operation, fallback = "clarify", "clarify"
    elif decision.needs_tools:
        if len(domains) > 1:
            operation = "multi_step"
        else:
            priority = ("delete", "send", "update", "create", "execute", "search", "read")
            operation = next((name for name in priority if name in operations), "read")
        fallback = "tools"
    elif domain == "unsupported":
        operation, fallback = "refuse", "decline"
    else:
        operation, fallback = "answer", "answer"
    return {
        "tool_needed": bool(decision.needs_tools), "domain": domain,
        "operation": operation, "multi_tool": multi, "fallback": fallback,
        "tools": sorted(tools), "route": decision.as_dict(),
    }


async def baseline_worker(output: Path) -> None:
    os.environ["WISP_HOME"] = str((output / "isolated_wisp_home").resolve())
    sys.path.insert(0, str(ROOT))
    process = psutil.Process()
    rss_before = process.memory_info().rss
    load_started = time.perf_counter()
    from service.router.router import route
    load_s = time.perf_counter() - load_started
    rss_after_load = process.memory_info().rss

    cases = load_cases()
    warmup_started = time.perf_counter()
    await route(cases[0]["prompt"])
    warmup_s = time.perf_counter() - warmup_started
    rows, quality_latencies, rss_samples = [], [], []
    for case in cases:
        started = time.perf_counter()
        decision = await route(case["prompt"])
        elapsed = time.perf_counter() - started
        quality_latencies.append(elapsed)
        rss_samples.append(process.memory_info().rss)
        rows.append({"case": case, "prediction": _baseline_prediction(decision), "latency_s": elapsed})
    warm_samples = []
    for _ in range(WARM_REPS):
        started = time.perf_counter()
        await route(cases[0]["prompt"])
        warm_samples.append(time.perf_counter() - started)
        rss_samples.append(process.memory_info().rss)
    peak_rss = max([rss_before, rss_after_load, *rss_samples])
    result = {
        "name": "wisp_baseline", "load_s": load_s, "warmup_s": warmup_s, "rows": rows,
        "memory": {
            "rss_before_load": rss_before, "rss_after_load": rss_after_load,
            "peak_observed_rss": peak_rss, "load_delta_rss": rss_after_load - rss_before,
            "peak_delta_rss": peak_rss - rss_before,
        },
        "latency": {
            "contract": "Wisp route() classification only; no agent/model/tool execution",
            "warm_samples": warm_samples, "warm_p50_s": statistics.median(warm_samples),
            "warm_p95_s": _pct(warm_samples, .95), "quality_samples": quality_latencies,
            "quality_p50_s": statistics.median(quality_latencies), "quality_p95_s": _pct(quality_latencies, .95),
        },
        "isolation": "WISP_HOME redirected; route() only; no agent loop or tool body invoked",
    }
    result["metrics"] = score_rows(rows)
    _json(output / "baseline.json", result)


def _device_name(device: Any) -> str:
    return type(device).__name__.removeprefix("ML").removesuffix("ComputeDevice") or repr(device)


def _walk_operations(block: Any):
    for operation in getattr(block, "operations", ()):
        yield operation
        for child in getattr(operation, "blocks", ()):
            yield from _walk_operations(child)


def compute_plan(agent: Any) -> dict[str, Any]:
    try:
        import coremltools as ct
        from coremltools.models.compute_plan import MLComputePlan

        compiled = agent.model.get_compiled_model_path()
        plan = MLComputePlan.load_from_path(compiled, compute_units=getattr(
            ct.ComputeUnit, {"cpu": "CPU_ONLY", "cpu_gpu": "CPU_AND_GPU", "cpu_ne": "CPU_AND_NE", "all": "ALL"}[agent.compute_units]
        ))
        program = plan.model_structure.program
        counts: Counter[str] = Counter()
        supported: Counter[str] = Counter()
        costs: Counter[str] = Counter()
        operations = 0
        for function in program.functions.values():
            for operation in _walk_operations(function.block):
                operations += 1
                usage = plan.get_compute_device_usage_for_mlprogram_operation(operation)
                if usage is not None:
                    preferred = _device_name(usage.preferred_compute_device)
                    counts[preferred] += 1
                    for device in usage.supported_compute_devices:
                        supported[_device_name(device)] += 1
                    cost = plan.get_estimated_cost_for_mlprogram_operation(operation)
                    if cost is not None:
                        costs[preferred] += float(cost.weight)
        return {
            "status": "ok", "compiled_path": compiled, "operations": operations,
            "preferred_operation_counts": dict(counts),
            "supported_operation_counts": dict(supported),
            "estimated_cost_weight_by_preferred_device": dict(costs),
            "available_devices": [_device_name(d) for d in agent.model.get_available_compute_devices()],
        }
    except Exception as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}


def _answer_value(answer: dict[str, Any]) -> Any:
    if answer["type"] == "noul":
        return answer["noul"] >= 0.5
    return answer["choice"]


def _capacity_probe(agent: Any) -> dict[str, Any]:
    question = {
        "probe": {
            "type": "noul", "instructions": "Tools needed?",
            "criteria": {"false": "no", "true": "yes"},
        }
    }
    empty_prepared, _ = agent.prepare("", question)
    prefix_and_final_sep_tokens = len(empty_prepared[0]["ids"])
    candidates = []
    for words in range(0, 1400):
        state = "x " * words
        raw_tokens = len(agent.tok(state, add_special_tokens=False)["input_ids"])
        prepared, _ = agent.prepare(state, question)
        untruncated_total = prefix_and_final_sep_tokens + raw_tokens
        candidates.append((words, raw_tokens, untruncated_total, len(prepared[0]["ids"]), state))
        if words > 1100 and len(prepared[0]["ids"]) == agent.cfg.get("max_len", 1024):
            break
    export_limit = int(agent.shape["max_length"])
    fitting = [row for row in candidates if row[2] <= export_limit]
    boundary = max(fitting, key=lambda row: (row[2], row[1]))
    over = next(row for row in candidates if row[2] > export_limit)

    def attempt(row: tuple[int, int, int, int, str]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            result = agent.predict(row[4], question)
            return {
                "status": "accepted", "source_words": row[0], "source_tokens": row[1],
                "untruncated_total_tokens": row[2], "prepared_tokens": row[3],
                "reported_usage_tokens": result["usage"]["input_tokens"],
                "elapsed_s": time.perf_counter() - started,
            }
        except Exception as exc:
            return {
                "status": "rejected", "source_words": row[0], "source_tokens": row[1],
                "untruncated_total_tokens": row[2], "prepared_tokens": row[3],
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": time.perf_counter() - started,
            }

    much_longer = "x " * 1600 + "TAIL_SENTINEL_A"
    same_prefix_other_tail = "x " * 1600 + "TAIL_SENTINEL_B"
    tail = []
    for state in (much_longer, same_prefix_other_tail):
        try:
            tail.append(agent.predict(state, question))
        except Exception as exc:
            tail.append({"error": f"{type(exc).__name__}: {exc}"})
    return {
        "export_limit_tokens": export_limit,
        "largest_constructed_fitting_input": attempt(boundary),
        "first_constructed_over_limit": attempt(over),
        "overlong_suffix_probe_identical": tail[0] == tail[1],
        "overlong_suffix_probe": tail,
        "interpretation": (
            "rejects over export shape" if export_limit < agent.cfg.get("max_len", export_limit)
            else "runtime truncates state to configured/exported maximum before inference"
        ),
    }


def model_worker(key: str, models_root: Path, output: Path) -> None:
    import laya_coreml as laya

    runtime_warnings: list[dict[str, Any]] = []
    def capture_warning(message, category, filename, lineno, file=None, line=None):
        runtime_warnings.append({
            "category": category.__name__, "message": str(message),
            "filename": filename, "lineno": lineno,
        })
    warnings.showwarning = capture_warning

    spec = MODELS[key]
    model_path = models_root / key
    process = psutil.Process()
    rss_before = process.memory_info().rss
    load_started = time.perf_counter()
    agent = laya.load(model_path, local_files_only=True, compute_units=spec["compute_units"])
    load_s = time.perf_counter() - load_started
    rss_after_load = process.memory_info().rss
    if {k: agent.shape[k] for k in spec["declared_shape"]} != spec["declared_shape"]:
        raise RuntimeError(f"loaded shape differs from pinned declaration for {key}: {agent.shape}")

    cases = load_cases()
    warmup_started = time.perf_counter()
    agent.predict("Show my calendar tomorrow.", QUESTIONS)
    warmup_s = time.perf_counter() - warmup_started
    rss_after_warmup = process.memory_info().rss
    rows, quality_latencies, cpu_samples, rss_samples = [], [], [], []
    for case in cases:
        cpu_started = time.process_time()
        started = time.perf_counter()
        raw = agent.predict(case["prompt"], QUESTIONS)
        elapsed = time.perf_counter() - started
        cpu_s = time.process_time() - cpu_started
        quality_latencies.append(elapsed)
        cpu_samples.append(cpu_s)
        rss_samples.append(process.memory_info().rss)
        answers = raw["answers"]
        prediction = {name: _answer_value(answer) for name, answer in answers.items()}
        rows.append({
            "case": case, "prediction": prediction, "answers": answers,
            "usage": raw["usage"], "latency_s": elapsed, "process_cpu_s": cpu_s,
        })

    latency_samples = []
    stable_reference = None
    stable_matches = 0
    representative = cases[0]["prompt"]
    for _ in range(WARM_REPS):
        started = time.perf_counter()
        repeated = agent.predict(representative, QUESTIONS)
        latency_samples.append(time.perf_counter() - started)
        rss_samples.append(process.memory_info().rss)
        if stable_reference is None:
            stable_reference = repeated
        if repeated == stable_reference:
            stable_matches += 1

    peak_rss = max([rss_before, rss_after_load, rss_after_warmup, *rss_samples])
    result = {
        "name": key, "repo": spec["repo"], "revision": spec["revision"],
        "model_path": str(model_path.resolve()), "compute_units_requested": spec["compute_units"],
        "loaded_shape": agent.shape, "load_s": load_s, "warmup_s": warmup_s,
        "memory": {
            "rss_before_load": rss_before, "rss_after_load": rss_after_load,
            "rss_after_warmup": rss_after_warmup, "peak_observed_rss": peak_rss,
            "load_delta_rss": rss_after_load - rss_before,
            "peak_delta_rss": peak_rss - rss_before,
        },
        "latency": {
            "contract": "five typed questions per routing request, batch size one",
            "warm_samples": latency_samples,
            "warm_p50_s": statistics.median(latency_samples), "warm_p95_s": _pct(latency_samples, .95),
            "quality_samples": quality_latencies,
            "quality_p50_s": statistics.median(quality_latencies), "quality_p95_s": _pct(quality_latencies, .95),
            "process_cpu_s_p50": statistics.median(cpu_samples), "process_cpu_s_p95": _pct(cpu_samples, .95),
            "input_tokens_p50": statistics.median(row["usage"]["input_tokens"] for row in rows),
            "input_tokens_max": max(row["usage"]["input_tokens"] for row in rows),
        },
        "stability": {"calls": WARM_REPS, "identical_results": stable_matches},
        "compute_plan": compute_plan(agent), "capacity": _capacity_probe(agent),
        "runtime_warnings": runtime_warnings, "rows": rows,
    }
    result["metrics"] = score_rows(rows)
    _json(output / f"{key}.json", result)


def score_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    positives = [row for row in rows if row["case"]["tool_needed"]]
    negatives = [row for row in rows if not row["case"]["tool_needed"]]
    multi_pos = [row for row in rows if row["case"]["multi_tool"]]
    multi_neg = [row for row in rows if not row["case"]["multi_tool"]]
    false_routes = [row for row in rows if row["case"]["safety"] == "false_route"]
    fail_closed = [row for row in rows if row["case"]["fail_closed"] and row["case"]["tool_needed"]]

    def accuracy(field: str, subset: list[dict[str, Any]] = rows) -> float:
        return sum(row["prediction"][field] == row["case"][field] for row in subset) / max(1, len(subset))

    safe_false_route = lambda row: (
        not row["prediction"]["tool_needed"]
        and row["prediction"]["operation"] not in MUTATING_OPERATIONS
        and row["prediction"]["fallback"] != "tools"
    )
    critical_miss = lambda row: (
        not row["prediction"]["tool_needed"] or row["prediction"]["fallback"] not in {"tools", "clarify"}
    )
    return {
        "cases": total,
        "tool_needed_accuracy": accuracy("tool_needed"),
        "tool_needed_recall": accuracy("tool_needed", positives),
        "tool_not_needed_specificity": accuracy("tool_needed", negatives),
        "domain_accuracy": accuracy("domain"),
        "operation_accuracy": accuracy("operation"),
        "fallback_accuracy": accuracy("fallback"),
        "multi_tool_recall": accuracy("multi_tool", multi_pos),
        "multi_tool_specificity": accuracy("multi_tool", multi_neg),
        "safety_false_routes_safe": sum(safe_false_route(row) for row in false_routes),
        "safety_false_routes_total": len(false_routes),
        "safety_critical_misses": sum(critical_miss(row) for row in fail_closed),
        "safety_critical_total": len(fail_closed),
        "errors": [
            {"id": row["case"]["id"], "expected": {k: row["case"][k] for k in ("tool_needed", "domain", "operation", "multi_tool", "fallback")},
             "predicted": {k: row["prediction"][k] for k in ("tool_needed", "domain", "operation", "multi_tool", "fallback")}}
            for row in rows if any(row["prediction"][k] != row["case"][k] for k in ("tool_needed", "domain", "operation", "multi_tool", "fallback"))
        ],
    }


def aggregate(output: Path) -> dict[str, Any]:
    results = [json.loads((output / "baseline.json").read_text(encoding="utf-8"))]
    results.extend(json.loads((output / f"{key}.json").read_text(encoding="utf-8")) for key in MODELS)
    summary = {
        "environment": json.loads((output / "environment.json").read_text(encoding="utf-8")),
        "models": json.loads((output / "model_manifests.json").read_text(encoding="utf-8")),
        "results": [{k: row[k] for k in row if k not in {"rows"}} for row in results],
    }
    _json(output / "summary.json", summary)
    return summary


def markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# Laya Core ML routing benchmark — generated summary", "",
        f"Base: `{summary['environment']['repository_base_sha']}`. Synthetic cases: "
        f"`{summary['environment']['cases_sha256']}`. No tools were executed.", "",
        "| Candidate | Tool recall | Domain | Operation | Safe false routes | Critical misses | Warm p50 / p95 | Peak RSS delta |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in summary["results"]:
        metrics = result["metrics"]
        latency = result["latency"]
        warm = f"{latency['warm_p50_s']*1000:.2f} / {latency['warm_p95_s']*1000:.2f} ms"
        memory = f"{result['memory']['peak_delta_rss']/1024**2:.1f} MiB"
        lines.append(
            f"| {result['name']} | {metrics['tool_needed_recall']:.1%} | {metrics['domain_accuracy']:.1%} | "
            f"{metrics['operation_accuracy']:.1%} | {metrics['safety_false_routes_safe']}/{metrics['safety_false_routes_total']} | "
            f"{metrics['safety_critical_misses']}/{metrics['safety_critical_total']} | {warm} | {memory} |"
        )
    lines.extend(["", "See `summary.json` and the per-candidate JSON files for complete raw evidence.", ""])
    return "\n".join(lines)


def orchestrate(output: Path, models_root: Path) -> None:
    started = time.monotonic()
    deadline = started + WALL_LIMIT_S
    output.mkdir(parents=True, exist_ok=True)
    head = _run_text(["git", "rev-parse", "HEAD"])
    if head != BASE_SHA:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", BASE_SHA, head], cwd=ROOT
        ).returncode == 0
        changed = _run_text(["git", "diff", "--name-only", BASE_SHA])
        allowed = ("benchmarks/laya_coreml_routing/", "docs/LAYA_COREML_ROUTING_BENCHMARK_20260920.md")
        disallowed = [path for path in changed.splitlines() if path and not path.startswith(allowed)]
        if not ancestor or disallowed:
            raise SystemExit(
                f"refusing benchmark: base {BASE_SHA} is not intact at {head}; "
                f"disallowed changes={disallowed}"
            )
    _json(output / "environment.json", environment())
    manifests = download_models(models_root, deadline)
    _json(output / "model_manifests.json", manifests)

    commands = [[sys.executable, __file__, "--worker", "baseline", "--output", str(output), "--models-root", str(models_root)]]
    commands.extend([
        [sys.executable, __file__, "--worker", key, "--output", str(output), "--models-root", str(models_root)]
        for key in MODELS
    ])
    for command in commands:
        remaining = WALL_LIMIT_S - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("benchmark wall-time budget exhausted")
        subprocess.run(command, check=True, timeout=remaining, cwd=ROOT)
    summary = aggregate(output)
    (output / "summary.md").write_text(markdown_summary(summary), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models-root", type=Path)
    parser.add_argument("--worker", choices=["baseline", *MODELS])
    args = parser.parse_args()
    output = args.output.resolve()
    models_root = (args.models_root or output / "models").resolve()
    if args.worker == "baseline":
        asyncio.run(baseline_worker(output))
    elif args.worker:
        model_worker(args.worker, models_root, output)
    else:
        orchestrate(output, models_root)


if __name__ == "__main__":
    main()
