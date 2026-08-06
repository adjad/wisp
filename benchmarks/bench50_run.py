"""50-sample auto-graded coding benchmark: gpt-oss-20b-MXFP4-Q8 vs
Qwen3.6-27B-3bit-mlx (or any oMLX model given on the command line).

For each of the 50 problems in tests/bench50_data.py: sends the prompt,
extracts the returned Python function from a code fence, execs it in a
fresh namespace, and runs the fixed test cases against it. Records
pass/fail, timing, and token throughput. Writes raw per-problem results to
JSON plus a printed summary.

Usage:
    .venv/bin/python -m tests.bench50_run --model gpt-oss-20b-MXFP4-Q8
    .venv/bin/python -m tests.bench50_run --model Qwen3.6-27B-3bit-mlx --disable-thinking
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
from benchmarks.bench50_data import PROBLEMS, check

SYSTEM = ("You are an expert Python programmer. Write correct, working code. "
          "Follow the requested function name and signature exactly.")

CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
EXEC_TIMEOUT_S = 6
REQUEST_TIMEOUT_S = 90


class _ExecTimeout(Exception):
    pass


def _alarm_handler(signum, frame):  # noqa: ARG001
    raise _ExecTimeout()


def extract_code(text: str) -> str:
    m = CODE_FENCE_RE.search(text)
    if m:
        return m.group(1)
    # no fence found — fall back to the raw text, best effort
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
                        temperature=0.0, max_tokens=600, **extra),
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
            print(f"[{i:2d}/50] {mark:4s} {problem['id']:38s} "
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
    out_path = Path(args.out) if args.out else Path(f"tests/bench50_{safe_name}.json")
    await run_model(args.model, args.disable_thinking, out_path)


if __name__ == "__main__":
    asyncio.run(main())
