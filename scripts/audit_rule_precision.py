#!/usr/bin/env python3
"""Offline audit: how often do deterministic rules take control, and how hard?

Replays logged single-turn prompts through the workflow compiler and the rules
router (no model, no backend, no network tools) and records the STRONGEST action
each prompt triggered:

    CANNED   the workflow compiler answers with a canned clarification
    WORKFLOW the workflow compiler builds a typed plan
    PIN      the router pins an exact tool call (direct_calls / force_first_tool /
             required_tool_groups): the model can only summarize the result
    SCOPE    a rule narrows the tool set to a subset
    RETRIEVE the ambiguous fallback offers retrieved candidates (no rule claimed it)
    MODEL    no rule claimed the prompt

A rule that pins or answers directly on a prompt that did not need it is the
failure this audit hunts for ("what should I be doing today" -> web_search).
`--emit-pins` writes a stratified sample of PIN/CANNED prompts that
`eval_arms.py --arms model_led --prompts-file` can replay to find disagreements
with the model-led path.

    .venv/bin/python scripts/audit_rule_precision.py
    .venv/bin/python scripts/audit_rule_precision.py --emit-pins pins.json

PRIVACY. Prompts come from your local sessions.db and debug exports. Output goes
under ~/.moe/evals/rule-audit/ and is never meant to be committed.

LIMITS. Prompts are routed as single turns with no history, so follow-ups ("yes",
"the second one") are skipped or over-counted as canned clarifications. A PIN is
not proof of a false positive; it marks where to look.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import glob
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="rule-audit-"))

OUT = Path.home() / ".moe" / "evals" / "rule-audit"
_FOLLOWUP = re.compile(
    r"^\s*(?:yes|yeah|yep|no|nope|ok(?:ay)?|sure|thanks?|thank you|go ahead|do it|"
    r"cancel|never ?mind|stop|retry|try again|the (?:first|second|third|last) one)\W*$", re.I)
_CONTEXTUAL = re.compile(r"^(?:yes|ok(?:ay)?|just|send (?:it|this|all)|the name of it)\b", re.I)
_TIER_ORDER = ["CANNED", "WORKFLOW", "PIN", "SCOPE", "RETRIEVE", "MODEL"]


def load_prompts(sessions: Path, downloads: Path, extra: list[Path]) -> list[str]:
    seen: dict[str, None] = {}

    def add(text: object) -> None:
        if not isinstance(text, str):
            return
        text = " ".join(text.split())
        if 3 <= len(text) <= 400 and not _FOLLOWUP.match(text):
            seen.setdefault(text, None)

    if sessions.exists():
        con = sqlite3.connect(f"file:{sessions}?mode=ro", uri=True)
        try:
            for (content,) in con.execute("select content from turns where role='user'"):
                add(content)
        finally:
            con.close()
    for path in glob.glob(str(downloads / "wisp-debug-*.json")):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        for turn in data.get("turns", []):
            if turn.get("role") == "user":
                add(turn.get("text"))
    for path in extra:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for item in data if isinstance(data, list) else data.get("prompts", []):
            add(item if isinstance(item, str) else (item or {}).get("prompt"))
    return list(seen)


def rule_key(reason: str) -> str:
    """A stable rule label: the reason with counts, days and names stripped."""
    key = re.sub(r"\(\d+\)", "(N)", reason or "")
    key = re.sub(r"\b\d+\b", "N", key)
    key = re.sub(r"\{[^}]*\}", "{}", key)
    return key.strip()[:110]


async def classify(prompt: str, route, compile_new) -> dict:
    row = {"prompt": prompt, "tier": "MODEL", "rule": "", "tools": []}
    try:
        plan = compile_new(prompt)
    except Exception as exc:  # a crashing rule is itself a finding
        row.update(rule=f"compiler error: {type(exc).__name__}")
        plan = None
    if plan is not None:
        if plan.content_error:
            row.update(tier="CANNED", rule="workflow: clarification question",
                       detail=plan.content_error[:80])
        else:
            row.update(tier="WORKFLOW", rule=f"workflow: {plan.kind}",
                       tools=list(plan.sources))
        return row
    try:
        d = await asyncio.wait_for(route(prompt), timeout=30)
    except Exception as exc:
        row.update(rule=f"route error: {type(exc).__name__}")
        return row
    reason = getattr(d, "reason", "") or ""
    direct = [name for name, _ in (getattr(d, "direct_calls", None) or [])]
    groups = getattr(d, "required_tool_groups", None)
    if direct or d.force_first_tool or groups:
        if direct:
            tools = direct
        elif d.force_first_tool:
            tools = [d.force_first_tool]
        else:
            tools = sorted({t for g in groups for t in g})
        row.update(tier="PIN", rule=rule_key(reason), tools=tools)
    elif d.tool_subset and reason.startswith("ambiguous"):
        row.update(tier="RETRIEVE", rule=rule_key(reason), n_tools=len(d.tool_subset))
    elif d.tool_subset:
        row.update(tier="SCOPE", rule=rule_key(reason), tools=list(d.tool_subset)[:12],
                   n_tools=len(d.tool_subset))
    else:
        row.update(rule=rule_key(reason))
    return row


def stratified_pins(rows: list[dict], seed: int = 7) -> list[dict]:
    """Every web-search pin, plus samples of canned and other pinned prompts.

    Context-dependent canned prompts ("yes send this only") are dropped: without
    history they cannot be judged."""
    rng = random.Random(seed)
    web = [r for r in rows if r["tier"] == "PIN" and "current public" in r["rule"]]
    canned = [r for r in rows if r["tier"] == "CANNED" and not _CONTEXTUAL.match(r["prompt"])]
    other = [r for r in rows if r["tier"] == "PIN" and "current public" not in r["rule"]]
    return web + rng.sample(canned, min(40, len(canned))) + rng.sample(other, min(40, len(other)))


async def main_async(args) -> int:
    from service.router.router import route
    from service.workflows.compiler import compile_new
    prompts = load_prompts(Path(args.sessions).expanduser(), Path(args.downloads).expanduser(),
                           [Path(p) for p in args.extra])
    if args.limit:
        prompts = prompts[:args.limit]
    print(f"auditing {len(prompts)} distinct single-turn prompts", flush=True)
    rows, t0 = [], time.monotonic()
    for i, prompt in enumerate(prompts, 1):
        rows.append(await classify(prompt, route, compile_new))
        if i % 200 == 0:
            print(f"  {i}/{len(prompts)}  {time.monotonic() - t0:.0f}s", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    tiers = collections.Counter(r["tier"] for r in rows)
    print("\n| tier | prompts | share |\n|---|---|---|")
    for tier in _TIER_ORDER:
        print(f"| {tier} | {tiers[tier]} | {tiers[tier] / max(1, len(rows)):.0%} |")
    for tier in ("CANNED", "PIN", "SCOPE"):
        by_rule = collections.Counter(r["rule"] for r in rows if r["tier"] == tier)
        if not by_rule:
            continue
        print(f"\n### {tier} by rule (top {args.top})\n| rule | prompts |\n|---|---|")
        for rule, n in by_rule.most_common(args.top):
            print(f"| {rule} | {n} |")
    print(f"\nrows -> {path}")
    if args.emit_pins:
        pins = stratified_pins(rows)
        Path(args.emit_pins).write_text(json.dumps([r["prompt"] for r in pins], indent=1))
        Path(args.emit_pins).with_suffix(".meta.json").write_text(json.dumps(pins, indent=1))
        print(f"{len(pins)} PIN/CANNED prompts -> {args.emit_pins}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sessions", default="~/.moe/sessions.db")
    ap.add_argument("--downloads", default="~/Downloads")
    ap.add_argument("--extra", nargs="*",
                    default=[str(ROOT / "scripts" / "replay_prompts_regression.json")])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--emit-pins", default="")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
