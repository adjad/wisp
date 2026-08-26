"""Qualitative agentic-coding round: 4 real-world, open-ended dev requests
(not auto-gradable like bench100) sent VERBATIM to both models so their raw
answers can be eyeballed side by side. Centerpiece is the HTML website task
the user asked for explicitly; the other 3 cover debugging, CLI-tool
building, and refactor-plus-new-feature — agentic flavors bench100's
isolated-function suite doesn't exercise.

Same "equal leg up" reasoning as bench100_run.py: identical prompt,
identical temperature, and a token budget / timeout generous enough that
Qwen's ~4x-slower tok/s (established in the n=50/n=100 runs) can't cause a
truncated or timed-out answer purely from being slower, not worse.

Usage:
    .venv/bin/python -m tests.agentic_qualitative_run --model gpt-oss-20b-MXFP4-Q8
    .venv/bin/python -m tests.agentic_qualitative_run --model Qwen3.6-27B-3bit-mlx --disable-thinking
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

MAX_TOKENS = 4000
REQUEST_TIMEOUT_S = 320
TEMPERATURE = 0.4  # identical for both models — not auto-graded, so a little
                   # creative headroom is fine as long as it's applied evenly

SYSTEM = ("You are an expert software engineer. Produce complete, working, "
          "production-quality output for exactly what's asked — no "
          "placeholders, no '...rest of the code here', no TODOs standing in "
          "for real logic.")

TASKS: list[tuple[str, str]] = [
    ("html_website",
     "Build a complete, single-file HTML website (inline CSS and JS only, no "
     "external dependencies or CDN links) for 'Aurora Coffee Roasters', a "
     "specialty coffee roastery. It must include: a hero section with the "
     "shop name and a tagline, an 'Our Story' section, a menu/products "
     "section listing at least 4 coffee products with prices, a visit-us / "
     "contact section with an address and hours, and a footer. Make it "
     "visually polished with a cohesive color palette and a responsive "
     "layout that works on mobile. Return ONLY the HTML in a single code "
     "block."),

    ("debug_multifunction",
     "This small inventory module has bugs. Find and fix ALL of them, and "
     "explain each bug you found:\n\n"
     "```python\n"
     "class Inventory:\n"
     "    def __init__(self):\n"
     "        self.items = {}\n"
     "\n"
     "    def add_stock(self, name, qty):\n"
     "        if name in self.items:\n"
     "            self.items[name] = qty\n"
     "        else:\n"
     "            self.items[name] = qty\n"
     "\n"
     "    def remove_stock(self, name, qty):\n"
     "        self.items[name] -= qty\n"
     "        return self.items[name]\n"
     "\n"
     "    def low_stock_items(self, threshold=5):\n"
     "        low = []\n"
     "        for name in self.items:\n"
     "            if self.items[name] < threshold:\n"
     "                low.append(name)\n"
     "        return low\n"
     "\n"
     "    def total_value(self, prices):\n"
     "        total = 0\n"
     "        for name, qty in self.items:\n"
     "            total += qty * prices[name]\n"
     "        return total\n"
     "```\n"),

    ("cli_tool",
     "Write a complete, runnable Python CLI tool (using argparse) called "
     "`wordcount.py` that counts lines, words, and characters in a text "
     "file. Requirements: positional `file` argument; flags `--lines`, "
     "`--words`, `--chars` to show only specific counts (if none given, show "
     "all three); support reading from stdin when `file` is '-'; print a "
     "clear error message and exit with a non-zero status if the file "
     "doesn't exist, instead of an unhandled traceback."),

    ("refactor_feature",
     "Refactor this TaskManager for readability and idiomatic Python, AND "
     "add the ability to mark a task complete and filter tasks by status "
     "(pending/complete), keeping the existing add/remove behavior intact:\n\n"
     "```python\n"
     "class TaskManager:\n"
     "    def __init__(self):\n"
     "        self.tasks = []\n"
     "\n"
     "    def add(self, t):\n"
     "        self.tasks.append(t)\n"
     "\n"
     "    def remove(self, t):\n"
     "        for i in range(len(self.tasks)):\n"
     "            if self.tasks[i] == t:\n"
     "                del self.tasks[i]\n"
     "                break\n"
     "\n"
     "    def list(self):\n"
     "        s = ''\n"
     "        for t in self.tasks:\n"
     "            s = s + t + ', '\n"
     "        return s\n"
     "```\n"),
]


async def run_task(client: OMLXClient, model: str, task_id: str, prompt: str,
                    extra: dict) -> dict:
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            client.chat(model,
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
                        temperature=TEMPERATURE, max_tokens=MAX_TOKENS, **extra),
            timeout=REQUEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        dt = time.perf_counter() - t0
        return {"id": task_id, "content": "", "error": "request timed out",
                "seconds": dt, "out_tokens": 0, "tok_per_s": 0.0}
    except Exception as e:  # noqa: BLE001
        dt = time.perf_counter() - t0
        return {"id": task_id, "content": "", "error": f"request failed: {e}",
                "seconds": dt, "out_tokens": 0, "tok_per_s": 0.0}

    dt = time.perf_counter() - t0
    msg = resp["choices"][0]["message"]
    content = msg.get("content") or ""
    usage = resp.get("usage", {})
    out_tok = usage.get("completion_tokens", 0) or len(content.split())
    return {
        "id": task_id, "content": content, "error": None,
        "seconds": dt, "out_tokens": out_tok,
        "tok_per_s": (out_tok / dt) if dt else 0.0,
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

        for i, (task_id, prompt) in enumerate(TASKS, 1):
            print(f"[{i}/{len(TASKS)}] {task_id} ...", flush=True)
            r = await run_task(client, model, task_id, prompt, extra)
            results.append(r)
            status = "ok" if not r["error"] else f"ERROR: {r['error']}"
            print(f"  -> {r['seconds']:.1f}s, {r['out_tokens']} tok, "
                  f"{r['tok_per_s']:.1f} tok/s ({status})", flush=True)
    finally:
        await client.aclose()

    out_path.write_text(json.dumps({"model": model, "load_seconds": load_s,
                                    "results": results}, indent=2))
    print(f"\nwritten to {out_path}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--disable-thinking", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", args.model)
    out_path = Path(args.out) if args.out else Path(f"tests/qualitative_{safe_name}.json")
    await run_model(args.model, args.disable_thinking, out_path)


if __name__ == "__main__":
    asyncio.run(main())
