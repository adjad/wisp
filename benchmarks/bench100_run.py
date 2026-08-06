"""100-sample auto-graded coding benchmark: gpt-oss-20b-MXFP4-Q8 vs
Qwen3.6-27B-3bit-mlx (or any oMLX model given on the command line).

Extends tests/bench50_run.py to the full 100-problem suite in
tests/bench100_data.py (50 basics + 50 OOP/generator/regex/graph/exception/
async problems). Same exec-and-grade approach; see bench50_run.py's
docstring for the mechanics.

FAIRNESS NOTE ("equal leg up" for Qwen3.6-27B-3bit, 2026-07-17): the n=50
run capped every answer at 600 tokens, which cost gpt-oss 2 of its 3
failures to truncation/empty-output rather than wrong logic. This suite's
advanced half (classes, decorators, graph algorithms) needs more room to
write correctly regardless of model, so the cap is raised to 1200 tokens
for BOTH models — not just Qwen — so neither is punished for verbosity
before either even reaches wrong-vs-right code. The request timeout is
raised from 90s to 180s: Qwen runs ~4x slower per token (see bench50
results, ~18 vs ~73 tok/s), so at the new 1200-token cap it needs up to
~70s of pure generation time versus gpt-oss's ~16s — 90s left far too
little margin for network/oMLX overhead on Qwen's side specifically, which
would have shown up as spurious timeouts unrelated to code quality.

Usage:
    .venv/bin/python -m tests.bench100_run --model gpt-oss-20b-MXFP4-Q8
    .venv/bin/python -m tests.bench100_run --model Qwen3.6-27B-3bit-mlx --disable-thinking
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.inference.omlx_client import OMLXClient
from benchmarks.bench50_data import check
from benchmarks.bench100_data import PROBLEMS

SYSTEM = ("You are an expert Python programmer. Write correct, working code. "
          "Follow the requested function/class name and signature exactly.")

CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
EXEC_TIMEOUT_S = 10
MAX_TOKENS = 1200
REQUEST_TIMEOUT_S = 180


class _ExecTimeout(Exception):
    pass


def _alarm_handler(signum, frame):  # noqa: ARG001
    raise _ExecTimeout()


def extract_code(text: str) -> str:
    m = CODE_FENCE_RE.search(text)
    if m:
        return m.group(1)
    return text


def run_one_problem(problem: dict, content: str) -> tuple[bool, str]:
    code = extract_code(content)
    ns: dict = {}
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    try:
        signal.alarm(EXEC_TIMEOUT_S)
        try:
            exec(code, ns)  # noqa: S102 - trusted local-model output, dev benchmark
        except _ExecTimeout:
            return False, "exec() timed out (likely infinite loop)"
        except Exception as e:  # noqa: BLE001
            return False, f"exec() raised {type(e).__name__}: {e}"
        finally:
            signal.alarm(0)

        fn = None
        if "custom_check" not in problem:
            fn = ns.get(problem["fn_name"])
            if fn is None or not callable(fn):
                return False, f"no callable named `{problem['fn_name']}` found in output"

        signal.alarm(EXEC_TIMEOUT_S)
        try:
            return check(problem, fn, ns)
        except _ExecTimeout:
            return False, "test call timed out (likely infinite loop)"
        finally:
            signal.alarm(0)
    finally:
        signal.signal(signal.SIGALRM, old_handler)


async def run_problem(client: OMLXClient, model: str, problem: dict,
                       extra: dict) -> dict:
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            client.chat(model,
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": problem["prompt"]}],
                        temperature=0.0, max_tokens=MAX_TOKENS, **extra),
            timeout=REQUEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        dt = time.perf_counter() - t0
        return {"id": problem["id"], "passed": False, "error": "request timed out",
                "seconds": dt, "out_tokens": 0, "tok_per_s": 0.0, "content": ""}
    except Exception as e:  # noqa: BLE001
        dt = time.perf_counter() - t0
        return {"id": problem["id"], "passed": False, "error": f"request failed: {e}",
                "seconds": dt, "out_tokens": 0, "tok_per_s": 0.0, "content": ""}

    dt = time.perf_counter() - t0
    msg = resp["choices"][0]["message"]
    content = msg.get("content") or ""
    usage = resp.get("usage", {})
    out_tok = usage.get("completion_tokens", 0) or len(content.split())
    passed, error = run_one_problem(problem, content)
    return {
        "id": problem["id"], "passed": passed, "error": error,
        "seconds": dt, "out_tokens": out_tok,
        "tok_per_s": (out_tok / dt) if dt else 0.0,
        "content": content,
    }


async def run_model(model: str, disable_thinking: bool, out_path: Path) -> None:
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if disable_thinking else {}
    client = OMLXClient()
    results = []
    try:
        print(f"loading {model} ...", flush=True)
        t0 = time.perf_counter()
        await client.ensure_only(model)
        load_s = time.perf_counter() - t0
        print(f"  loaded in {load_s:.1f}s\n", flush=True)

        for i, problem in enumerate(PROBLEMS, 1):
            r = await run_problem(client, model, problem, extra)
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{i:3d}/100] {mark:4s} {problem['id']:38s} "
                  f"{r['seconds']:5.1f}s {r['tok_per_s']:5.1f} tok/s"
                  + ("" if r["passed"] else f"  -- {r['error'][:80]}"), flush=True)
    finally:
        await client.aclose()

    n = len(results)
    passed = sum(r["passed"] for r in results)
    total_s = sum(r["seconds"] for r in results)
    avg_tok_s = sum(r["tok_per_s"] for r in results if r["out_tokens"]) / max(
        1, sum(1 for r in results if r["out_tokens"]))
    summary = {
        "model": model, "n": n, "passed": passed, "pass_rate": passed / n,
        "total_seconds": total_s, "avg_seconds": total_s / n,
        "avg_tok_per_s": avg_tok_s, "load_seconds": load_s,
        "max_tokens": MAX_TOKENS, "request_timeout_s": REQUEST_TIMEOUT_S,
        "failures": [r["id"] for r in results if not r["passed"]],
        "results": results,
    }
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"\n{'='*70}\n{model}: {passed}/{n} passed ({passed/n:.0%})  "
          f"avg {avg_tok_s:.1f} tok/s  total {total_s:.0f}s\n"
          f"written to {out_path}\n{'='*70}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--disable-thinking", action="store_true",
                    help="pass chat_template_kwargs enable_thinking=False (Qwen3.6)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", args.model)
    out_path = Path(args.out) if args.out else Path(f"tests/bench100_{safe_name}.json")
    await run_model(args.model, args.disable_thinking, out_path)


if __name__ == "__main__":
    asyncio.run(main())
