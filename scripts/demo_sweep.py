#!/usr/bin/env python3
"""Ask-tab demo sweep: run scripts/demo_prompts.json against an ISOLATED Wisp
backend and print a mechanical scorecard.

    .venv/bin/python scripts/demo_sweep.py run   [--base URL] [--prompts FILE] [--out FILE]
                                                 [--only CAT,CAT] [--limit N] [--label NAME]
    .venv/bin/python scripts/demo_sweep.py score FILE.jsonl [BASELINE.jsonl]

SAFETY
  * Only an isolated backend (default http://127.0.0.1:8775, see sandbox/run.sh)
    is allowed. Port 8765 (the real Wisp) and 8000 (oMLX) are refused.
  * Every `confirm` event is auto-DENIED, so nothing is sent, deleted or changed
    through a confirmation card.
  * Prompts run strictly one after another: there is one resident model and
    concurrent generations contend for it and spike memory.

Each record (one JSON object per turn) holds the prompt, final answer, tools
with their results (capped), denied confirmations, error text, latency and the
reasoning the turn showed. `score` recomputes the scorecard from a saved file,
and with a second file prints the before/after comparison.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "http://127.0.0.1:8775"
REFUSED_PORTS = {8765, 8000}
RESULT_CAP = 3000

# --- mechanical checks (pure; tests/test_model_only_quality.py exercises them) ---

GUARD_TEXT = "haven't checked a current personal source"
FALLBACK_TEXTS = (
    "I couldn't produce a reliable answer",
    "I ran out of room working that one out",
    "I don't have a reliable way to do that",
    "(stopped after reaching the step limit)",
)
_MONOLOGUE = re.compile(
    r"^\s*(?:we need to|we should|the user (?:wants|is asking|asked|said)|i need to|"
    r"i should|let me (?:check|think|look|call)|okay,? so|first,? i|hmm|wait,)", re.I)


def is_refused_base(base: str) -> bool:
    parts = urlsplit(base)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return True
    return port in REFUSED_PORTS


def raw_dump(answer: str, tools: list[dict]) -> bool:
    """The answer is (mostly) a tool result pasted verbatim, or the labelled merge."""
    text = (answer or "").strip()
    if not text:
        return False
    if re.match(r"^\*\*(?:From |[a-z_]+:\*\*)", text) or re.search(r"^\*\*[a-z_]+:\*\*", text, re.M):
        return True
    for tool in tools or []:
        result = str(tool.get("result") or "").strip()
        if len(result) >= 200 and len(text) >= 200 and result[:200] in text:
            return True
    return False


def think_leak(answer: str) -> bool:
    text = answer or ""
    return bool("<think>" in text or "</think>" in text or _MONOLOGUE.match(text))


def score_record(rec: dict) -> dict:
    answer = rec.get("answer") or ""
    tools = rec.get("tools") or []
    personal = bool(rec.get("personal"))
    return {
        "empty": not answer.strip() and not rec.get("draft"),
        "error": bool(rec.get("error")),
        "raw_dump": raw_dump(answer, tools),
        "think_leak": think_leak(answer),
        "guard_overfire": GUARD_TEXT in answer and (bool(tools) or not personal),
        "guard_fired": GUARD_TEXT in answer,
        "fallback": any(t in answer for t in FALLBACK_TEXTS),
        "personal_no_tool": personal and not tools and not rec.get("denied") and not rec.get("draft"),
        "long": len(answer) > 900,
        "chars": len(answer),
        "secs": float(rec.get("secs") or 0.0),
    }


def scorecard(records: list[dict]) -> dict:
    rows = [score_record(r) for r in records]
    n = len(rows) or 1
    keys = ("empty", "error", "raw_dump", "think_leak", "guard_overfire", "guard_fired",
            "fallback", "personal_no_tool", "long")
    card = {k: sum(1 for r in rows if r[k]) for k in keys}
    card["turns"] = len(rows)
    card["bad_turns"] = sum(1 for r in rows if r["empty"] or r["error"] or r["raw_dump"]
                            or r["think_leak"] or r["guard_overfire"] or r["fallback"])
    chars = sorted(r["chars"] for r in rows) or [0]
    secs = sorted(r["secs"] for r in rows) or [0.0]
    card["chars_p50"] = chars[len(chars) // 2]
    card["chars_p90"] = chars[min(len(chars) - 1, int(len(chars) * 0.9))]
    card["secs_p50"] = round(secs[len(secs) // 2], 1)
    card["secs_p90"] = round(secs[min(len(secs) - 1, int(len(secs) * 0.9))], 1)
    card["secs_mean"] = round(statistics.mean(secs), 1) if rows else 0.0
    card["bad_rate"] = round(card["bad_turns"] / n, 3)
    return card


def print_card(card: dict, baseline: dict | None = None) -> None:
    order = ("turns", "bad_turns", "bad_rate", "empty", "error", "raw_dump", "think_leak",
             "guard_overfire", "guard_fired", "fallback", "personal_no_tool", "long",
             "chars_p50", "chars_p90", "secs_p50", "secs_p90", "secs_mean")
    for key in order:
        if baseline is None:
            print(f"  {key:18} {card[key]}")
        else:
            print(f"  {key:18} {baseline.get(key, '-')!s:>8} -> {card[key]!s:>8}")


def load(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# --- driving the isolated backend ---

def run_turn(client, base: str, prompt: str, sid: str | None) -> dict:
    out: dict = {"prompt": prompt, "answer": "", "tools": [], "denied": [], "error": "",
                 "session_id": sid, "reasoning": "", "route": "", "draft": None}
    deltas: list[str] = []
    body: dict = {"prompt": prompt, "debug": False}
    if sid:
        body["session_id"] = sid
    started = time.time()
    import httpx
    try:
        with client.stream("POST", f"{base}/agent", json=body,
                           timeout=httpx.Timeout(300.0)) as response:
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                kind = ev.get("type")
                if kind == "session":
                    out["session_id"] = ev.get("id") or out["session_id"]
                elif kind == "routed":
                    out["route"] = ev.get("route_source") or ev.get("reason", "")
                elif kind == "tool_call":
                    out["tools"].append({"name": ev.get("name"), "args": ev.get("args"),
                                         "decision": ev.get("decision")})
                elif kind == "tool_result" and out["tools"]:
                    out["tools"][-1]["result"] = str(ev.get("result", ""))[:RESULT_CAP]
                elif kind == "delta":
                    deltas.append(ev.get("text", ""))
                elif kind == "clear_answer":
                    deltas.clear()
                elif kind == "text":
                    out["answer"] = str(ev.get("text", ""))
                elif kind == "reasoning":
                    out["reasoning"] = str(ev.get("text", ""))
                elif kind == "message_draft":
                    out["draft"] = {"to": ev.get("to"), "text": ev.get("text")}
                elif kind == "confirm":
                    # NEVER approve: see the module docstring.
                    out["denied"].append({"tool": ev.get("tool"), "args": ev.get("args")})
                    client.post(f"{base}/agent/approve", json={
                        "session_id": out["session_id"], "action_id": ev.get("id"),
                        "approved": False, "scope": "once"}, timeout=30.0)
                elif kind == "error":
                    out["error"] = str(ev.get("message") or ev)
                elif kind == "done":
                    break
    except Exception as exc:  # noqa: BLE001 - a dead backend must not lose prior results
        out["error"] = f"{type(exc).__name__}: {exc}"
    if not out["answer"]:
        out["answer"] = "".join(deltas)
    out["secs"] = round(time.time() - started, 1)
    return out


def cmd_run(args) -> int:
    import httpx
    if is_refused_base(args.base):
        print(f"refusing {args.base}: ports {sorted(REFUSED_PORTS)} belong to the real Wisp/oMLX; "
              "use the isolated backend on 8775", file=sys.stderr)
        return 2
    spec = json.load(open(args.prompts))
    cases = spec["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["category"] in wanted or c["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]
    out_path = args.out or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"demo-sweep-{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    records: list[dict] = []
    with httpx.Client(trust_env=False) as client, open(out_path, "w") as sink:
        try:
            client.get(f"{args.base}/mode", timeout=10.0).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"isolated backend not reachable at {args.base}: {exc}", file=sys.stderr)
            return 1
        for case in cases:
            sid = None
            for turn, prompt in enumerate([case["prompt"], *case.get("followups", [])]):
                rec = run_turn(client, args.base, prompt, sid)
                sid = rec["session_id"]
                rec.update({"id": case["id"] + (f"#{turn}" if turn else ""), "category": case["category"],
                            "personal": bool(case.get("personal")), "variant_of": case.get("variant_of"),
                            "label": args.label})
                records.append(rec)
                sink.write(json.dumps(rec) + "\n")
                sink.flush()
                flags = [k for k, v in score_record(rec).items() if v is True]
                print(f"[{rec['id']:9}] {rec['secs']:5.1f}s {[t['name'] for t in rec['tools']]} "
                      f"{flags} :: {rec['answer'][:110]!r}", flush=True)
    print(f"\nwrote {len(records)} turns -> {out_path}")
    print_card(scorecard(records))
    return 0


def cmd_score(args) -> int:
    card = scorecard(load(args.file))
    baseline = scorecard(load(args.baseline)) if args.baseline else None
    print_card(card, baseline)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--base", default=DEFAULT_BASE)
    run.add_argument("--prompts", default=str(ROOT / "scripts" / "demo_prompts.json"))
    run.add_argument("--out")
    run.add_argument("--only")
    run.add_argument("--limit", type=int)
    run.add_argument("--label", default="sweep")
    run.set_defaults(func=cmd_run)
    score = sub.add_parser("score")
    score.add_argument("file")
    score.add_argument("baseline", nargs="?")
    score.set_defaults(func=cmd_score)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
