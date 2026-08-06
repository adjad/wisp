"""A/B/N test harness: baseline (gemma-4-e4b-it-4bit) vs any number of small-
model candidates, on real data, using the SAME system prompts and tool
schemas Wisp actually uses in production (imported directly from
service/router/router.py, service/tools/email_tools.py, service/tools) so
this is a fair comparison, not synthetic.

Categories:
  router     - classification accuracy against the real classify prompt
  email      - summarization quality on a real ~40-message inbox
  calendar   - summarization quality on real synced events
  toolcall   - can the model be handed the REAL tool schemas (get_upcoming,
               summarize_emails) and correctly emit a valid tool call with
               the right function + args, the same way the agent loop uses
               gpt-oss? (uses tool_choice="auto", not forced)

Runs PER-MODEL (not per-category): each candidate is loaded ONCE, all
categories run in that single residency window, then it's unloaded for the
next one. Avoids the swap-thrashing bug from the first version of this script.

Usage:
    .venv/bin/python tests/ab_test_models.py --candidates Qwen3-1.7B-6bit,Llama-3.2-3B-Instruct-4bit
    .venv/bin/python tests/ab_test_models.py --candidates ALL_SMALL   # every <2.5GB model found
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.inference.omlx_client import OMLXClient
from service.router.router import _CLASSIFY_SYS
from service.tools import tool_schemas
from service.tools.email_tools import _SYS as EMAIL_SYS
from benchmarks.ab_test_data import (
    CALENDAR_EVENTS_RAW,
    EMAIL_HEADERS_RAW,
    EMAIL_SIGNAL_ITEMS,
    IMESSAGE_BLOCKED_REASON,
    ROUTER_CASES,
)

BASELINE = "gemma-4-e4b-it-4bit"

TOOLCALL_CASES = [
    # (prompt, expected_tool_name)
    ("what's on my calendar today?", "get_upcoming"),
    ("what's in my inbox?", "summarize_emails"),
    ("summarize my emails from yesterday", "summarize_emails"),
    ("do I have any meetings this week?", "get_upcoming"),
]
TOOLCALL_SYS = (
    "You are Wisp, a private assistant running locally on the user's Mac. "
    "You can use tools to inspect the user's calendar and email. To do "
    "something, CALL THE TOOL directly."
)


# --------------------------------------------------------------------------
async def classify(client: OMLXClient, model: str, prompt: str) -> tuple[str | None, bool, float]:
    t0 = time.perf_counter()
    try:
        resp = await client.chat(
            model, [{"role": "system", "content": _CLASSIFY_SYS}, {"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=40)
        dt = time.perf_counter() - t0
        content = resp["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0)) if m else {}
        role = data.get("role")
        tools = bool(data.get("needs_tools")) or role == "agent"
        return role, tools, dt
    except Exception:  # noqa: BLE001
        return None, False, time.perf_counter() - t0


async def summarize(client: OMLXClient, model: str, sys_prompt: str, user_content: str,
                    max_tokens: int = 400) -> tuple[str, float]:
    t0 = time.perf_counter()
    resp = await client.chat(
        model, [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}],
        temperature=0.3, max_tokens=max_tokens)
    dt = time.perf_counter() - t0
    return (resp["choices"][0]["message"].get("content") or "").strip(), dt


async def try_toolcall(client: OMLXClient, model: str, prompt: str) -> tuple[str | None, dict, float]:
    """Hands the model the REAL tool schemas with tool_choice=auto (not
    forced) — exactly how the production agent loop calls gpt-oss. Returns
    (called_tool_name_or_None, args, elapsed_s)."""
    t0 = time.perf_counter()
    try:
        resp = await client.chat(
            model,
            [{"role": "system", "content": TOOLCALL_SYS}, {"role": "user", "content": prompt}],
            tools=tool_schemas(["get_upcoming", "summarize_emails"]), tool_choice="auto",
            temperature=0.0, max_tokens=100)
        dt = time.perf_counter() - t0
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return None, {}, dt
        name = calls[0]["function"]["name"]
        try:
            args = json.loads(calls[0]["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        return name, args, dt
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}", {}, time.perf_counter() - t0


# --------------------------------------------------------------------------
async def run_router(client: OMLXClient, model: str) -> dict:
    correct = 0
    total_t = 0.0
    rows = []
    for prompt, exp_role, exp_tools in ROUTER_CASES:
        role, tools, dt = await classify(client, model, prompt)
        total_t += dt
        ok = (role == exp_role and tools == exp_tools)
        correct += ok
        rows.append((prompt, role, tools, ok))
    return {"correct": correct, "n": len(ROUTER_CASES), "avg_ms": total_t / len(ROUTER_CASES) * 1000, "rows": rows}


async def run_email(client: OMLXClient, model: str) -> dict:
    lines = []
    for line in EMAIL_HEADERS_RAW.splitlines():
        parts = line.split(" | ", 2)
        lines.append(f"{parts[1]} | {parts[2]}" if len(parts) == 3 else line)
    user_content = "Inbox for the last few days (sender | subject):\n" + "\n".join(lines)
    out, dt = await summarize(client, model, EMAIL_SYS, user_content)
    hits = [name for name in EMAIL_SIGNAL_ITEMS if name.split()[0] in out]
    return {"output": out, "time_s": dt, "signal_hits": hits}


async def run_calendar(client: OMLXClient, model: str) -> dict:
    sys_prompt = ("You are a scheduling assistant. Summarize the user's upcoming "
                 "calendar events clearly and concisely, in natural language.")
    user_content = "Upcoming events:\n" + CALENDAR_EVENTS_RAW
    out, dt = await summarize(client, model, sys_prompt, user_content, max_tokens=200)
    return {"output": out, "time_s": dt}


async def run_toolcall(client: OMLXClient, model: str) -> dict:
    correct = 0
    rows = []
    for prompt, exp_tool in TOOLCALL_CASES:
        name, args, dt = await try_toolcall(client, model, prompt)
        ok = (name == exp_tool)
        correct += ok
        rows.append((prompt, name, args, dt, ok))
    return {"correct": correct, "n": len(TOOLCALL_CASES), "rows": rows}


# --------------------------------------------------------------------------
def print_model_report(model: str, mem_gb: float, r: dict, e: dict, c: dict, tc: dict) -> None:
    print(f"\n{'#'*94}\n# {model}   (~{mem_gb:.2f} GB)\n{'#'*94}")

    print(f"\n[ROUTER]  {r['correct']}/{r['n']} correct, avg {r['avg_ms']:.0f}ms")
    for prompt, role, tools, ok in r["rows"]:
        mark = " " if ok else "X"
        print(f"  {mark} {prompt[:55]:55} -> {role}/{tools}")

    print(f"\n[TOOL CALLING]  {tc['correct']}/{tc['n']} correct (tool_choice=auto, real schemas)")
    for prompt, name, args, dt, ok in tc["rows"]:
        mark = " " if ok else "X"
        print(f"  {mark} {prompt[:45]:45} -> {name} {args} ({dt:.1f}s)")

    print(f"\n[EMAIL SUMMARY]  ({e['time_s']:.1f}s)  signal items surfaced: {e['signal_hits'] or 'NONE'}")
    print("  " + e["output"].replace("\n", "\n  ")[:900])

    print(f"\n[CALENDAR SUMMARY]  ({c['time_s']:.1f}s)")
    print("  " + c["output"].replace("\n", "\n  ")[:500])


async def run_baseline_reference(client: OMLXClient) -> None:
    print(f"\n{'='*94}\nBASELINE REFERENCE — {BASELINE}\n{'='*94}")
    await client.ensure_only(BASELINE)
    r = await run_router(client, BASELINE)
    tc = await run_toolcall(client, BASELINE)
    e = await run_email(client, BASELINE)
    c = await run_calendar(client, BASELINE)
    print_model_report(BASELINE, 5.5, r, e, c, tc)


async def run_candidate(client: OMLXClient, model: str, mem_gb: float) -> None:
    print(f"\n(loading {model}...)")
    await client.ensure_only(model)
    r = await run_router(client, model)
    tc = await run_toolcall(client, model)
    e = await run_email(client, model)
    c = await run_calendar(client, model)
    print_model_report(model, mem_gb, r, e, c, tc)


ALL_SMALL = [
    ("gemma-3-270m-it-MLX-bf16", 0.92),
    ("Qwen3-1.7B-MLX-4bit", 1.02),
    ("Qwen3-1.7B-6bit", 1.47),
    ("Qwen3.5-2B-4bit", 1.81),       # new (VLM-capable)
    ("Llama-3.2-3B-Instruct-4bit", 1.90),
    ("Phi-3-mini-4k-instruct-4bit", 2.26),
    ("Qwen3.5-2B-6bit", 2.30),       # new (VLM-capable)
    ("Ternary-Bonsai-8B-mlx-2bit", 2.42),
    # 270m variants already shown to fail badly — kept out of the long run;
    # add back with --candidates if you want to re-confirm.
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="ALL_SMALL",
                    help="comma-separated model ids, or ALL_SMALL")
    ap.add_argument("--skip-baseline", action="store_true")
    args = ap.parse_args()

    if args.candidates == "ALL_SMALL":
        candidates = ALL_SMALL
    else:
        candidates = [(c.strip(), 0.0) for c in args.candidates.split(",")]

    client = OMLXClient()
    try:
        if not args.skip_baseline:
            await run_baseline_reference(client)
        for model, mem_gb in candidates:
            await run_candidate(client, model, mem_gb)
    finally:
        await client.aclose()

    print(f"\n{'='*94}\nIMESSAGE — SKIPPED\n{'='*94}\n{IMESSAGE_BLOCKED_REASON}")


if __name__ == "__main__":
    asyncio.run(main())
