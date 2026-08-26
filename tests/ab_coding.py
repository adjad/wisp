"""A/B coding-quality harness: compare two oMLX models on real coding prompts.

Sends the same prompts to model A and model B, records each answer + timing,
and writes a side-by-side markdown report you can eyeball for quality.

Usage:
    .venv/bin/python -m tests.ab_coding \
        --a gpt-oss-20b-MXFP4-Q8 \
        --b Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from service.inference.omlx_client import OMLXClient

PROMPTS: list[tuple[str, str]] = [
    ("bugfix",
     "This Python function should return the second-largest unique number in a "
     "list, or None if there isn't one. It has a bug. Fix it and explain the bug:\n\n"
     "def second_largest(nums):\n"
     "    nums = sorted(nums)\n"
     "    return nums[-2]\n"),
    ("algorithm",
     "Write a Python function `merge_intervals(intervals)` that merges overlapping "
     "intervals given as a list of [start, end] pairs. Include time complexity and "
     "a couple of edge cases in comments."),
    ("refactor",
     "Refactor this for readability and to avoid the nested loop, keeping behavior "
     "identical:\n\n"
     "def common(a, b):\n"
     "    out = []\n"
     "    for x in a:\n"
     "        for y in b:\n"
     "            if x == y and x not in out:\n"
     "                out.append(x)\n"
     "    return out\n"),
    ("async_concurrency",
     "Write an async Python function that fetches a list of URLs concurrently with "
     "httpx, limits concurrency to 5, retries each URL up to 3 times with exponential "
     "backoff, and returns a dict of url -> status_code. Production quality."),
    ("edge_reasoning",
     "In Python, explain precisely why `[[]] * 3` followed by `a[0].append(1)` makes "
     "all three sublists contain 1, and show the correct way to build a list of 3 "
     "independent empty lists."),
]

SYSTEM = ("You are an expert software engineer. Write correct, idiomatic, "
          "production-quality code. Be concise.")


async def run_model(client: OMLXClient, model: str, prompt: str) -> dict:
    t0 = time.time()
    resp = await client.chat(
        model,
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=1200,
    )
    dt = time.time() - t0
    msg = resp["choices"][0]["message"]
    usage = resp.get("usage", {})
    out_tok = usage.get("completion_tokens", 0)
    return {
        "content": msg.get("content") or "(no content)",
        "seconds": dt,
        "out_tokens": out_tok,
        "tok_per_s": (out_tok / dt) if dt else 0,
        "load_s": usage.get("model_load_duration", 0),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="model A id")
    ap.add_argument("--b", required=True, help="model B id")
    ap.add_argument("--out", default="tests/ab_report.md")
    args = ap.parse_args()

    client = OMLXClient()
    lines = [f"# A/B coding comparison\n",
             f"- **A** = `{args.a}`\n- **B** = `{args.b}`\n"]
    summary = []
    try:
        # batch by model so oMLX swaps once per model, not once per prompt
        results: dict[str, dict] = {}
        for label, model in (("A", args.a), ("B", args.b)):
            # free memory for large models that can't co-reside under the cap
            await client.ensure_only(model)
            for name, prompt in PROMPTS:
                print(f"[{name}] {label}={model} ...", flush=True)
                try:
                    results[(label, name)] = await run_model(client, model, prompt)
                except Exception as e:  # record failure, keep going
                    results[(label, name)] = {
                        "content": f"(request failed: {e})", "seconds": 0,
                        "out_tokens": 0, "tok_per_s": 0, "load_s": 0}
        for name, prompt in PROMPTS:
            ra = results[("A", name)]
            rb = results[("B", name)]
            summary.append((name, ra, rb))
            lines += [
                f"\n---\n## {name}\n",
                f"**Prompt:**\n\n```\n{prompt.strip()}\n```\n",
                f"\n### A — {args.a}  ·  {ra['tok_per_s']:.1f} tok/s, "
                f"{ra['seconds']:.1f}s, load {ra['load_s']:.1f}s\n",
                f"\n````\n{ra['content'].strip()}\n````\n",
                f"\n### B — {args.b}  ·  {rb['tok_per_s']:.1f} tok/s, "
                f"{rb['seconds']:.1f}s, load {rb['load_s']:.1f}s\n",
                f"\n````\n{rb['content'].strip()}\n````\n",
            ]
        # speed summary table
        lines.append("\n---\n## Speed summary\n\n| task | A tok/s | B tok/s |\n|---|---|---|\n")
        for name, ra, rb in summary:
            lines.append(f"| {name} | {ra['tok_per_s']:.1f} | {rb['tok_per_s']:.1f} |\n")
    finally:
        await client.aclose()

    Path(args.out).write_text("".join(lines))
    print(f"\nReport written to {args.out}")
    for name, ra, rb in summary:
        print(f"  {name:18s} A {ra['tok_per_s']:5.1f} tok/s | B {rb['tok_per_s']:5.1f} tok/s")


if __name__ == "__main__":
    asyncio.run(main())
