#!/usr/bin/env python3
"""Reproducible synthetic comparison of Laya classifiers and Wisp routing.

Run one model per process so cold-load time and memory remain attributable:

  python benchmark.py model --name multilingual-ane --model-dir /path/to/model --output result.json
  python benchmark.py baseline --output baseline.json

The script never calls or executes a Wisp tool. The baseline invokes only the
router and records which tool schemas it would expose or pre-resolve.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = Path(__file__).with_name("corpus.json")

DOMAIN_CRITERIA = {
    "conversation": "text only",
    "calendar": "calendar",
    "reminders": "reminders",
    "email": "email",
    "messages": "texts contacts calls",
    "files": "files folders",
    "notes_memory": "notes memory",
    "system": "device settings",
    "apps_media": "apps media",
    "web_data": "web live data math",
    "travel": "maps travel transit",
    "compound": "multiple domains",
}

OPERATION_CRITERIA = {
    "converse": "answer rewrite explain",
    "read": "read report",
    "search": "find search",
    "create": "create add capture",
    "update": "change move archive complete",
    "delete": "delete cancel clear forget",
    "send": "send reply call",
    "control": "operate device app media",
    "calculate": "calculate convert",
    "compound": "multiple operations",
}

COMPACT_QUESTIONS = {
    "tool_needed": {
        "type": "choice",
        "instructions": "Does this request need external data or an action?",
        "criteria": {
            "tool": "data device app stored resource",
            "no_tool": "answer from text or knowledge",
        },
    },
    "domain": {
        "type": "choice",
        "instructions": "Choose the domain.",
        "criteria": DOMAIN_CRITERIA,
    },
    "operation": {
        "type": "choice",
        "instructions": "Choose the operation.",
        "criteria": OPERATION_CRITERIA,
    },
}

RICH_DOMAIN_CRITERIA = {
    "conversation": "Answer, explain, rewrite, summarize supplied text, translate, or brainstorm without external data.",
    "calendar": "Read, create, change, cancel, or search calendar events, agendas, and free time.",
    "reminders": "Read, search, create, change, complete, or delete reminders and tasks.",
    "email": "Read, search, summarize, draft, reply to, archive, or send email.",
    "messages": "Read or send text messages; search conversations; look up contacts; place calls.",
    "files": "Read, find, list, create, move, organize, convert, or delete files, folders, and documents.",
    "notes_memory": "Read, search, create, append, remember, recall, or forget notes and personal memory.",
    "system": "Read or control device state, settings, network, clipboard, screen, or hardware.",
    "apps_media": "Open, quit, switch, or control applications, windows, music, and media.",
    "web_data": "Search or fetch the web; weather, stocks, news, sports, time, definitions, math, and conversions.",
    "travel": "Find places, directions, traffic, travel time, local events, flights, and transit.",
    "compound": "The request requires two or more different domains or dependent tool steps.",
}

RICH_OPERATION_CRITERIA = {
    "converse": "Answer, explain, rewrite, translate, summarize supplied text, or brainstorm with no tool.",
    "read": "Read, summarize, or report current, external, device, or stored information.",
    "search": "Find, search, or look up an item, source, record, place, or conversation.",
    "create": "Create or add a new event, reminder, note, folder, file, capture, or stored fact.",
    "update": "Change, move, archive, complete, mark, organize, or edit an existing item.",
    "delete": "Delete, trash, cancel, clear, remove, or forget an existing item.",
    "send": "Send, draft, reply, forward, call, or communicate something to another person.",
    "control": "Operate a device, application, setting, window, network, or media playback.",
    "calculate": "Calculate arithmetic or convert numeric units, values, or currency.",
    "compound": "The request requires several different operations or dependent steps.",
}

RICH_QUESTIONS = {
    "tool_needed": {
        "type": "choice",
        "instructions": "Can this request be answered from supplied text and general knowledge alone, or must the assistant access external/current/stored data, an application, or a device action?",
        "criteria": {
            "tool": "Requires current or stored data, a user resource, an application, a device, or an external action.",
            "no_tool": "Can be completed entirely from supplied text and general knowledge with no external access or action.",
        },
    },
    "domain": {
        "type": "choice",
        "instructions": "Choose the single best request domain, or compound when several domains are required.",
        "criteria": RICH_DOMAIN_CRITERIA,
    },
    "operation": {
        "type": "choice",
        "instructions": "Choose the primary requested operation, or compound when several operations are required.",
        "criteria": RICH_OPERATION_CRITERIA,
    },
}

EFFECTS = {"create", "update", "delete", "send", "control"}
SAFE_OPERATIONS = {"converse", "read", "search", "calculate"}


def load_corpus() -> list[dict[str, Any]]:
    return json.loads(CORPUS_PATH.read_text())


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def machine() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def answer_choice(answer: dict[str, Any]) -> tuple[str, float]:
    probabilities = answer["probabilities"]
    choice = answer["choice"]
    return choice, float(probabilities[choice])


def aggregate_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if not r.get("error")]
    n = len(rows)
    def rate(predicate) -> float | None:
        return sum(1 for r in valid if predicate(r)) / n if n else None

    language_groups = {
        "english": [r for r in valid if r["language"] == "en"],
        "multilingual": [r for r in valid if r["language"] != "en"],
    }
    selective = {}
    for threshold in (0.5, 0.7, 0.9):
        covered = [r for r in valid if min(r["confidences"].values()) >= threshold]
        selective[str(threshold)] = {
            "coverage": len(covered) / n if n else None,
            "joint_accuracy_on_covered": (
                sum(1 for r in covered if r["joint_correct"]) / len(covered)
                if covered else None
            ),
        }
    return {
        "cases": n,
        "completed": len(valid),
        "capacity_or_runtime_errors": n - len(valid),
        "tool_needed_accuracy": rate(lambda r: r["tool_needed_correct"]),
        "domain_accuracy": rate(lambda r: r["domain_correct"]),
        "operation_accuracy": rate(lambda r: r["operation_correct"]),
        "joint_accuracy": rate(lambda r: r["joint_correct"]),
        "safe_request_effect_overtrigger_rate": rate(
            lambda r: r["expected_operation"] in SAFE_OPERATIONS
            and r["predicted_operation"] in EFFECTS
        ),
        "effect_request_downgrade_rate": rate(
            lambda r: r["expected_operation"] in EFFECTS
            and (r["predicted_operation"] not in EFFECTS or r["predicted_tool_needed"] is False)
        ),
        "english_joint_accuracy": (
            sum(1 for r in language_groups["english"] if r["joint_correct"])
            / len(language_groups["english"]) if language_groups["english"] else None
        ),
        "multilingual_joint_accuracy": (
            sum(1 for r in language_groups["multilingual"] if r["joint_correct"])
            / len(language_groups["multilingual"]) if language_groups["multilingual"] else None
        ),
        "latency_ms": {
            "p50": percentile([r["latency_ms"] for r in valid], 0.5),
            "p95": percentile([r["latency_ms"] for r in valid], 0.95),
            "mean": statistics.fmean(r["latency_ms"] for r in valid) if valid else None,
        },
        "selective": selective,
    }


def run_model(args: argparse.Namespace) -> dict[str, Any]:
    import psutil
    import laya_coreml as laya

    corpus = load_corpus()
    process = psutil.Process()
    rss_before = process.memory_info().rss
    started = time.perf_counter()
    agent = laya.load(args.model_dir, local_files_only=True)
    load_seconds = time.perf_counter() - started
    rss_after_load = process.memory_info().rss

    questions = COMPACT_QUESTIONS if args.schema == "compact" else RICH_QUESTIONS

    # One unrecorded warmup keeps first-compile cost out of warm measurements.
    agent.predict("What is on my calendar today?", questions)

    rows = []
    for case in corpus:
        state = case.get("laya_state", case["prompt"])
        started = time.perf_counter()
        try:
            result = agent.predict(state, questions)
            latency_ms = (time.perf_counter() - started) * 1000
            tool_choice, tool_conf = answer_choice(result["answers"]["tool_needed"])
            domain, domain_conf = answer_choice(result["answers"]["domain"])
            operation, operation_conf = answer_choice(result["answers"]["operation"])
            predicted_tool = tool_choice == "tool"
            row = {
                "id": case["id"],
                "language": case["language"],
                "expected_tool_needed": case["tool_needed"],
                "predicted_tool_needed": predicted_tool,
                "expected_domain": case["domain"],
                "predicted_domain": domain,
                "expected_operation": case["operation"],
                "predicted_operation": operation,
                "tool_needed_correct": predicted_tool == case["tool_needed"],
                "domain_correct": domain == case["domain"],
                "operation_correct": operation == case["operation"],
                "confidences": {
                    "tool_needed": tool_conf,
                    "domain": domain_conf,
                    "operation": operation_conf,
                },
                "usage": result.get("usage", {}),
                "latency_ms": latency_ms,
                "raw_answers": result["answers"],
            }
            row["joint_correct"] = (
                row["tool_needed_correct"] and row["domain_correct"]
                and row["operation_correct"]
            )
        except Exception as exc:  # capacity failures are part of the result
            row = {
                "id": case["id"],
                "language": case["language"],
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        rows.append(row)

    capacity = {}
    for label, words in (("medium", 160), ("long", 1400)):
        state = "Check my calendar after reading this context. " + "context " * words
        started = time.perf_counter()
        try:
            result = agent.predict(state, {"domain": questions["domain"]})
            capacity[label] = {
                "status": "completed",
                "latency_ms": (time.perf_counter() - started) * 1000,
                "usage": result.get("usage", {}),
                "answer": result["answers"]["domain"],
            }
        except Exception as exc:
            capacity[label] = {
                "status": "error",
                "latency_ms": (time.perf_counter() - started) * 1000,
                "error": f"{type(exc).__name__}: {exc}",
            }

    stability_cases = [corpus[i] for i in (0, 6, 18, 59, 70)]
    stability = {"repeats": args.stability_repeats, "cases": [], "exact_matches": 0,
                 "total_comparisons": 0}
    for case in stability_cases:
        state = case.get("laya_state", case["prompt"])
        try:
            reference = agent.predict(state, questions)["answers"]
            matches = 0
            for _ in range(args.stability_repeats):
                answers = agent.predict(state, questions)["answers"]
                matches += answers == reference
            stability["cases"].append({"id": case["id"], "matches": matches,
                                       "comparisons": args.stability_repeats})
            stability["exact_matches"] += matches
            stability["total_comparisons"] += args.stability_repeats
        except Exception as exc:
            stability["cases"].append({"id": case["id"],
                                       "error": f"{type(exc).__name__}: {exc}"})

    config = json.loads((Path(args.model_dir) / "coreml_config.json").read_text())
    output = {
        "kind": "laya_model",
        "name": args.name,
        "model_dir": str(Path(args.model_dir).resolve()),
        "model_config": config,
        "schema_variant": args.schema,
        "schema": questions,
        "machine": machine(),
        "load_seconds": load_seconds,
        "memory_bytes": {
            "rss_before_load": rss_before,
            "rss_after_load": rss_after_load,
            "rss_load_delta": rss_after_load - rss_before,
            "rss_final": process.memory_info().rss,
            "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "aggregate": aggregate_model(rows),
        "capacity": capacity,
        "stability": stability,
        "rows": rows,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    return output


def decision_tools(decision: Any) -> set[str]:
    names = set(decision.tool_subset or ())
    if decision.force_first_tool:
        names.add(decision.force_first_tool)
    names.update(name for name, _ in decision.direct_calls)
    names.update(name for group in decision.required_tool_groups for name in group)
    names.update(name for item in decision.conditional_tools for name in item[:2])
    names.update(decision.tool_argument_bindings)
    return names


async def baseline_rows() -> list[dict[str, Any]]:
    os.environ.setdefault("WISP_HOME", "/private/tmp/laya-routing-wisp-home")
    sys.path.insert(0, str(ROOT))
    import service.tools  # noqa: F401
    from service.router.router import route

    rows = []
    for case in load_corpus():
        started = time.perf_counter()
        try:
            decision = await route(
                case["prompt"],
                last_user=case.get("last_user"),
                last_assistant=case.get("last_assistant"),
                last_tools=case.get("last_tools"),
            )
            latency_ms = (time.perf_counter() - started) * 1000
            offered = decision_tools(decision)
            groups = case["expected_tool_groups"]
            group_hits = [bool(set(group) & offered) for group in groups]
            row = {
                "id": case["id"],
                "language": case["language"],
                "expected_tool_needed": case["tool_needed"],
                "predicted_tool_needed": bool(decision.needs_tools),
                "tool_needed_correct": bool(decision.needs_tools) == case["tool_needed"],
                "expected_tool_groups": groups,
                "offered_tools": sorted(offered),
                "group_hits": group_hits,
                "all_expected_groups_recalled": all(group_hits),
                "route": decision.as_dict(),
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            row = {
                "id": case["id"],
                "language": case["language"],
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        rows.append(row)
    return rows


def aggregate_baseline(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if not r.get("error")]
    tool_cases = [r for r in valid if r["expected_tool_needed"]]
    no_tool_cases = [r for r in valid if not r["expected_tool_needed"]]
    multi = [r for r in valid if r["language"] != "en"]
    effect_ids = {
        c["id"] for c in load_corpus() if c["operation"] in EFFECTS or c["operation"] == "compound"
    }
    effect_rows = [r for r in valid if r["id"] in effect_ids]
    return {
        "cases": len(rows),
        "completed": len(valid),
        "errors": len(rows) - len(valid),
        "tool_needed_accuracy": sum(r["tool_needed_correct"] for r in valid) / len(rows),
        "expected_tool_group_recall": (
            sum(r["all_expected_groups_recalled"] for r in tool_cases) / len(tool_cases)
        ),
        "no_tool_specificity": (
            sum(not r["predicted_tool_needed"] for r in no_tool_cases) / len(no_tool_cases)
        ),
        "effect_request_group_recall": (
            sum(r["all_expected_groups_recalled"] for r in effect_rows) / len(effect_rows)
        ),
        "multilingual_group_recall": (
            sum(r["all_expected_groups_recalled"] for r in multi if r["expected_tool_needed"])
            / sum(1 for r in multi if r["expected_tool_needed"])
        ),
        "latency_ms": {
            "p50": percentile([r["latency_ms"] for r in valid], 0.5),
            "p95": percentile([r["latency_ms"] for r in valid], 0.95),
            "mean": statistics.fmean(r["latency_ms"] for r in valid),
        },
    }


def run_baseline(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    rows = asyncio.run(baseline_rows())
    output = {
        "kind": "wisp_baseline",
        "machine": machine(),
        "elapsed_seconds": time.perf_counter() - started,
        "aggregate": aggregate_baseline(rows),
        "rows": rows,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("model")
    model.add_argument("--name", required=True)
    model.add_argument("--model-dir", required=True)
    model.add_argument("--output", required=True)
    model.add_argument("--schema", choices=("compact", "rich"), default="compact")
    model.add_argument("--stability-repeats", type=int, default=5)
    baseline = sub.add_parser("baseline")
    baseline.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run_model(args) if args.command == "model" else run_baseline(args)
    print(json.dumps(result["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
