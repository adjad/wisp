#!/usr/bin/env python3
"""Where do a real turn's prompt tokens go? Per model step: system rules, tool
schemas, conversation history, tool results, the user message.

Starts one isolated backend + sandbox server (same isolation as eval_arms.py:
never the real ports or ~/.moe), drives a few representative prompts through
/agent with Debug Mode on, and reads the `raw_model_io` event each step
emits. Token counts use the same ~4-chars-per-token estimate as the budgeter
(loop._est_tokens), so they are comparable to its reservations, not to oMLX's
exact `prompt:` figure — expect those to run ~10–20% higher.

    .venv/bin/python scripts/eval_prompt_composition.py
    .venv/bin/python scripts/eval_prompt_composition.py --prompts "what's in my inbox" "weather in Dublin"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402

from scripts.eval_arms import Arm, BACKEND_PORT, SANDBOX_PORT, _port_busy, _refuse_real_ports  # noqa: E402

DEFAULT_PROMPTS = [
    "What's in my inbox?",
    "What's on my calendar this week?",
    "Summarize my messages from today.",
    "What do I need to do today?",
    "What's the weather in Dublin right now?",
    "Do I have any notes about groceries?",
]


RAW: list[dict] = []   # system text per step, saved beside the report for re-attribution


def _tok(obj) -> int:
    return len(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) // 4


def _split_system(text: str) -> dict[str, int]:
    """The system message is rules + identity + memory + skills + date, joined
    in run_agent. Split on the markers those blocks start with; anything we
    can't attribute stays under 'rules'."""
    out = {"rules": 0, "identity": 0, "memory": 0, "skills": 0}
    markers = [("memory", "Remembered facts"), ("memory", "Things the user asked you to remember"),
               ("identity", "The user is"), ("identity", "USER IDENTITY"),
               ("skills", "Skill:"), ("skills", "SKILL")]
    cuts = sorted({(text.find(m), k) for k, m in markers if text.find(m) >= 0})
    prev, prev_key = 0, "rules"
    for pos, key in cuts:
        out[prev_key] += _tok(text[prev:pos])
        prev, prev_key = pos, key
    out[prev_key] += _tok(text[prev:])
    return out


async def run_prompt(client: httpx.AsyncClient, base: str, prompt: str) -> list[dict]:
    steps: list[dict] = []
    async with client.stream("POST", f"{base}/agent", json={"prompt": prompt, "debug": True},
                             timeout=240) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            kind = ev.get("type")
            if kind == "raw_model_io":
                req = ev.get("request") or {}
                msgs = req.get("messages") or []
                tools = req.get("tools") or []
                system = "\n".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
                last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
                history = [m for i, m in enumerate(msgs)
                           if m.get("role") != "system" and i < last_user]
                tool_results = [m for m in msgs if m.get("role") == "tool"]
                assistant_calls = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
                comp = _split_system(system)
                RAW.append({"prompt": prompt, "step": len(steps) + 1, "system": system,
                            "tools": [t["function"]["name"] for t in tools]})
                steps.append({
                    "prompt": prompt, "step": len(steps) + 1,
                    "tools_offered": len(tools), "schemas": _tok(tools),
                    **comp,
                    "history": sum(_tok(m.get("content") or "") for m in history),
                    "history_msgs": len(history),
                    "tool_results": sum(_tok(m.get("content") or "") for m in tool_results),
                    "tool_calls": sum(_tok(m.get("tool_calls")) for m in assistant_calls),
                    "user": _tok((msgs[last_user].get("content") or "") if last_user >= 0 else ""),
                    "max_tokens": req.get("max_tokens"),
                })
            elif kind == "confirm":
                await client.post(f"{base}/agent/approve", json={
                    "session_id": ev.get("session_id") or "", "action_id": ev.get("id"),
                    "approved": False, "scope": "once"}, timeout=20)
            elif kind == "done":
                break
    return steps


def report(rows: list[dict]) -> str:
    cols = ["step", "tools_offered", "schemas", "rules", "identity", "memory", "skills",
            "history", "tool_results", "tool_calls", "user"]
    lines = ["| prompt | " + " | ".join(cols) + " | total |", "|---|" + "---|" * (len(cols) + 1)]
    for r in rows:
        total = sum(r[c] for c in cols if c not in {"step", "tools_offered"})
        lines.append(f"| {r['prompt'][:34]} | " + " | ".join(str(r[c]) for c in cols) + f" | {total} |")
    # Share of prompt by component, over every step.
    agg = {c: sum(r[c] for r in rows) for c in cols if c not in {"step", "tools_offered"}}
    tot = sum(agg.values()) or 1
    lines += ["", "| component | tokens (all steps) | share |", "|---|---|---|"]
    for c, v in sorted(agg.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {c} | {v} | {v / tot:.0%} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompts", nargs="*", default=DEFAULT_PROMPTS)
    ap.add_argument("--port", type=int, default=BACKEND_PORT)
    ap.add_argument("--sandbox-port", type=int, default=SANDBOX_PORT)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--scratch", default=None)
    args = ap.parse_args()
    _refuse_real_ports(args.port, args.sandbox_port)
    for p in (args.port, args.sandbox_port):
        if _port_busy(p):
            sys.exit(f"port {p} is in use")
    scratch = Path(args.scratch) if args.scratch else Path(os.environ.get("TMPDIR", "/tmp")) / "wisp-composition"
    arm = Arm("baseline", scratch, args.port, args.sandbox_port, args.python)
    rows: list[dict] = []
    arm.start()
    try:
        async def go():
            async with httpx.AsyncClient() as client:
                await run_prompt(client, arm.base, "What time is it?")   # warm-up, not reported
                for prompt in args.prompts:
                    t = time.monotonic()
                    steps = await run_prompt(client, arm.base, prompt)
                    rows.extend(steps)
                    print(f"  {prompt[:40]:<40} steps={len(steps)} {time.monotonic() - t:5.1f}s")
        asyncio.run(go())
    finally:
        arm.stop()
    md = report(rows)
    out = Path.home() / ".moe" / "evals" / "arms" / f"composition-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md + "\n")
    out.with_suffix(".raw.json").write_text(json.dumps(RAW, ensure_ascii=False, indent=1))
    print("\n" + md + f"\n\n-> {out}")


if __name__ == "__main__":
    main()
