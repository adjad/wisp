"""CPU-only development timing, called exclusively from isolated pytest state.

No model, endpoint, native integration or tool function executes. Model timings
must be reported separately; this is not an end-to-end latency benchmark.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
from datetime import datetime
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def quantiles(values):
    ordered = sorted(values)
    return {"n": len(values), "p50_ns": statistics.median(ordered),
            "p95_ns": ordered[math.ceil(.95 * len(ordered)) - 1]}


def capture(output):
    # Service imports may initialize stores; never permit a standalone invocation.
    home = Path(os.environ.get("WISP_HOME", "")).resolve()
    if not os.environ.get("PYTEST_CURRENT_TEST") or not any(
            part.startswith("wisp-pytest-state-") for part in home.parts):
        raise RuntimeError("CPU timing requires the repository's isolated pytest bootstrap")
    output = Path(output).resolve()
    if output != HERE / "artifacts/cpu-latency-2":
        raise ValueError("Output is outside the registered measurement directory")
    from service.router.router import rule_route
    from service.workflows.reads import compile_read
    from service.router.intent.request import build_messages, natural_context
    from service.router.intent.validation import InvalidIntent, validate_intent
    from service.router.intent.compiler import compile_intent
    rows = [json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines()]
    if len(rows) != 16 or not all(row["synthetic"] and row["development_only"] for row in rows):
        raise ValueError("Measurement inputs differ from the registered synthetic corpus")
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for case in rows:
        now = datetime.fromisoformat(case["now"])
        users = [m["content"] for m in case["history"] if m["role"] == "user"]
        raw = json.dumps(case["expected"])

        def deterministic():
            result = compile_read(case["prompt"], last_user=users[-1] if users else "",
                                  last_tools=", ".join(case["prior_tools"]), now=now)
            if result is not None:
                return "read_shortcut"
            result = rule_route(case["prompt"])
            return "rule" if result is not None else "unmatched"

        def intent_cpu():
            messages = build_messages(case["prompt"], context=case["history"],
                                      prior_tools=case["prior_tools"], now=now)
            try:
                intent = validate_intent(json.loads(raw), case["prompt"],
                    context=natural_context(messages[1:-1]),
                    prior_tools=case["prior_tools"], now=now)
                if intent.kind == "read":
                    compile_intent(intent, now=now)
                return "validated_" + intent.kind
            except InvalidIntent as exc:
                return "rejected: " + str(exc)

        class FixedCalendarClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        # Legacy calendar parsing has an implicit clock even when compile_read
        # receives now. Freeze only that isolated clock outside the timed calls.
        with patch("service.workflows.reads.datetime", FixedCalendarClock):
            for name, action in (("deterministic_selection", deterministic),
                                 ("intent_cpu_without_generation", intent_cpu)):
                warmup_status = action()
                values = []
                statuses = []
                for repeat in range(50):
                    start = time.perf_counter_ns()
                    status = action()
                    elapsed = time.perf_counter_ns() - start
                    values.append(elapsed)
                    statuses.append(status)
                    records.append({"id": case["id"], "path": name, "repeat": repeat,
                                    "elapsed_ns": elapsed, "status": status})
                if set(statuses) != {warmup_status}:
                    raise AssertionError("CPU case status changed during serialized measurement")
    paths = ["service/router/router.py", "service/workflows/reads.py",
             "service/router/intent/request.py", "service/router/intent/validation.py",
             "service/router/intent/grammar.py", "service/router/intent/compiler.py",
             "service/router/intent/schema.py", "service/inference/omlx_client.py",
             "eval/prompt-alignment-dev-20261007/cases.jsonl",
             "eval/prompt-alignment-dev-20261007/cpu_latency.py"]
    hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}
    summary = {"cpu_only": True, "model_inference": False, "tool_execution": False,
        "http": False, "cases": 16,
        "synthetic_clocks": {case["id"]: case["now"] for case in rows},
        "clock_policy": "Explicit now to compiler/validation; isolated reads.datetime frozen per case outside timing",
        "supersedes": "cpu-latency-1 (legacy calendar clock was not controlled)", "warmups_per_case": 1, "repetitions_per_case": 50,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_hashes": hashes, "platform": platform.platform(),
        "python": platform.python_version(), "timer": vars(time.get_clock_info("perf_counter")),
        "paths": {name: quantiles([r["elapsed_ns"] for r in records if r["path"] == name])
                  for name in {r["path"] for r in records}},
        "per_case": [{"id": case["id"], "path": name,
             "status": next(r["status"] for r in records if r["id"] == case["id"] and r["path"] == name),
             **quantiles([r["elapsed_ns"] for r in records if r["id"] == case["id"] and r["path"] == name])}
            for case in rows for name in {r["path"] for r in records}],
        "limits": ["Gold synthetic intent supplied; no Ling generation or status/transport cost",
                   "Deterministic selection can return unmatched; timings do not imply correctness",
                   "Unmerged code snapshot; source hashes identify working tree bytes",
                   "No live tool I/O, serving measurements, cold starts or V2-Mac qualification"]}
    (output / "raw.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in records))
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary
