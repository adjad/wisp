#!/usr/bin/env python3
"""Keep base's own thought at a tool-call step only where base's own call matches the scripted call.

The target's call stays the scripted one, so the row still teaches the right tool. Its thought comes from base only when
base reached the same call on its own: reasoning that led somewhere else can't explain this call. Everything else keeps what
the build gave it. No filler is invented, and no thought quota is applied. Crusoe's guide, tested on 31B and not on E4B, found
distilled traces held up where trace-free targets lost reasoning, which is why this exists. Whether it helps E4B is untested.

    choose_traces.py --partial partial.jsonl --raw 'traces.*.jsonl' --out-chosen traces.chosen.jsonl --out-stats traces.stats.json
"""
from __future__ import annotations

import _paths  # noqa: F401

import argparse
import glob
import json
import re
import sys
from collections import Counter

from gen import request_hash  # noqa: E402

_ARG = re.compile(r'([A-Za-z_][\w]*)\s*:\s*(?:<\|"\|>(.*?)<\|"\|>|([^,{}]+))', re.S)
MIN_WORDS, MAX_WORDS = 4, 300
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def fold(value) -> str:
    """One comparable form per value. ``False`` and ``0`` are values, not absence: ``calendar_only=false`` is a different read
    from ``calendar_only=true``. Booleans fold to true/false and numbers to one form, so ``3``, ``3.0`` and ``"3"`` agree."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = re.sub(r"\s+", " ", str(value).strip().strip('"').lower())
    if NUMBER.fullmatch(text):
        n = float(text)
        return str(int(n)) if n.is_integer() else repr(n)
    return text


def absent(value) -> bool:
    """Only an unset value: null, an empty string, an empty list. Never False or 0."""
    return value is None or (isinstance(value, (str, list, dict)) and not (value.strip() if isinstance(value, str) else value))


def parse_args(raw) -> dict:
    """Gemma's `{key:<|"|>value<|"|>,n:3}` call text, loosely: each top-level key to its folded value."""
    if isinstance(raw, dict):
        return {k: fold(v) for k, v in raw.items() if not absent(v)}
    out = {}
    for key, quoted, plain in _ARG.findall(str(raw or "")):
        value = quoted if quoted else plain
        if not absent(value):
            out[key] = fold(value)
    return out


def scripted_calls(row: dict) -> list[tuple[str, dict]]:
    out = []
    for tc in row["messages"][-1].get("tool_calls") or []:
        fn = tc["function"]
        args = fn.get("arguments")
        args = json.loads(args) if isinstance(args, str) else (args or {})
        out.append((fn["name"], {k: fold(v) for k, v in args.items() if not absent(v)}))
    return out


def call_problem(base_calls: list[dict], want: list[tuple[str, dict]]) -> str | None:
    """None when base made the scripted calls: the same tools, each with exactly the scripted values (False and 0 included)
    and no other set argument. An extra argument fails: the schemas declare no defaults, so ``calendar_only``, ``unread``,
    ``count``, ``account`` or ``cc`` can't be shown to leave the call's scope alone, and reasoning that chose them explains
    a different call. Order is free, because parallel reads have none.
    -> "call_mismatch" (other tools or values) or "call_extra_argument" (the scripted values plus more)."""
    got = [(c["function"]["name"], parse_args(c["function"].get("arguments"))) for c in base_calls or []]
    if sorted(n for n, _ in got) != sorted(n for n, _ in want):
        return "call_mismatch"
    left, extra = list(got), False
    for name, args in want:
        exact = [i for i, (n, a) in enumerate(left) if n == name and a == args]
        if exact:
            left.pop(exact[0])
            continue
        wider = [i for i, (n, a) in enumerate(left) if n == name and all(a.get(k) == v for k, v in args.items())]
        if not wider:
            return "call_mismatch"
        left.pop(wider[0])
        extra = True
    return "call_extra_argument" if extra else None


def calls_match(base_calls: list[dict], want: list[tuple[str, dict]]) -> bool:
    return call_problem(base_calls, want) is None


def reject_reasons(rec: dict, row: dict) -> list[str]:
    if rec.get("error"):
        return ["error"]
    out = []
    t = (rec.get("reasoning") or "").strip()
    if not t:
        out.append("no_thought")
    elif not MIN_WORDS <= len(t.split()) <= MAX_WORDS:
        out.append("thought_length")
    if rec.get("finish_reason") == "length":
        out.append("truncated")
    if rec.get("channel_leak"):
        out.append("channel_leak")
    if (rec.get("content") or "").strip():
        out.append("text_with_call")           # production's tool steps carry no text beside the call
    if not rec.get("tool_calls"):
        out.append("no_call")
    elif problem := call_problem(rec["tool_calls"], scripted_calls(row)):
        out.append(problem)
    if rec.get("request_hash") != request_hash(row["messages"][:-1], row.get("tools")):
        out.append("stale_hash")
    return out


def run(partial: str, raws: list[str]):
    rows = {r["meta"]["id"]: r for r in read_jsonl(partial) if r["messages"][-1].get("tool_calls")}
    chosen, reasons, by_family = {}, Counter(), {}
    for path in raws:
        for rec in read_jsonl(path):
            row = rows.get(rec.get("id"))
            if row is None or rec["id"] in chosen:
                continue
            fam = by_family.setdefault(row["meta"]["family"], Counter())
            fam["samples"] += 1
            why = reject_reasons(rec, row)
            if why:
                reasons.update(why)
                fam["rejected"] += 1
                continue
            fam["kept"] += 1
            chosen[rec["id"]] = {"id": rec["id"], "family": row["meta"]["family"], "k": rec["k"], "reasoning": rec["reasoning"].strip(),
                                 "request_hash": rec["request_hash"], "model": rec.get("model")}
    stats = {"call_rows": len(rows), "kept": len(chosen), "rejects_by_reason": dict(reasons.most_common()),
             "families": {k: dict(v) for k, v in sorted(by_family.items())}}
    return chosen, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--partial", required=True)
    ap.add_argument("--raw", action="append", required=True)
    ap.add_argument("--out-chosen", required=True)
    ap.add_argument("--out-stats", required=True)
    a = ap.parse_args(argv)
    raws = sorted({p for pat in a.raw for p in (glob.glob(pat) or [pat])})
    chosen, stats = run(a.partial, raws)
    with open(a.out_chosen, "w", encoding="utf-8") as f:
        for c in chosen.values():
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with open(a.out_stats, "w") as f:
        json.dump(stats, f, indent=1)
    print(json.dumps({k: stats[k] for k in ("call_rows", "kept", "rejects_by_reason")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
