#!/usr/bin/env python3
"""Keep base's own thought at a tool-call step only where base's own call matches the scripted call.

The target's call stays the scripted one, so the row still teaches the right tool. Its thought comes from base only when
base reached the same call on its own: reasoning that led somewhere else can't explain this call. Everything else keeps what
the build gave it. No filler is invented, and no thought quota is applied. Crusoe's guide, tested on 31B and not on E4B, found
distilled traces held up where trace-free targets lost reasoning, which is why this exists. Whether it helps E4B is untested.

Matching (``call_problem``) is structural and typed: strings exactly, lists in order with their duplicates, ``False``, ``0``,
``""``, ``[]`` and ``null`` each a value of its own, and call text that can't be read unambiguously fails. Two exceptions,
both from the tool's own code (``tool_specs.json``, built by ``tool_specs.py`` from the pinned service): a value left out
matches the tool's own default, and the two arguments in ``CASE_FREE`` ignore case. Calls in one step may come in any order
only when all of them are reads. Fixed 2026-11-08 after review found ``["a,b"]`` matched ``["a", "b"]``, lists were
de-duplicated and their order ignored, case was ignored everywhere, and an empty value counted as unset.

    choose_traces.py --partial partial.jsonl --raw 'traces.*.jsonl' --out-chosen traces.chosen.jsonl --out-stats traces.stats.json
"""
from __future__ import annotations

import _paths  # noqa: F401

import argparse
import glob
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from gen import request_hash  # noqa: E402

Q = '<|"|>'                              # Gemma's string delimiter in call text
BARE = {"true": True, "false": False, "null": None}
MIN_WORDS, MAX_WORDS = 4, 300
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
SPECS = Path(__file__).with_name("tool_specs.json")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def canon(value):
    """One typed, structural form per value: nothing is joined into a string, so ``["a,b"]`` and ``["a", "b"]`` differ.
    Lists keep their order and duplicates; strings are compared exactly (case and spacing included); ``False``, ``0``,
    ``""``, ``[]`` and ``null`` are values, each its own. The only folding is JSON's own: ``3`` and ``3.0`` are one number.
    A bool is never a number. Anything that isn't a JSON value raises ``CallSyntaxError``."""
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise CallSyntaxError("non-finite number")
        return ("num", int(value) if float(value).is_integer() else float(value))
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(canon(v) for v in value))
    if isinstance(value, dict):
        return ("obj", tuple(sorted((k, canon(v)) for k, v in value.items())))
    raise CallSyntaxError(f"not a JSON value: {type(value).__name__}")


def load_specs(path=SPECS) -> dict:
    """``tool_specs.py``'s output: each tool's category and Python defaults, from the pinned service code."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


SPEC = load_specs()

# Arguments whose letter case the tool itself discards on every path, so "Tuesday" and "tuesday" run the same call and return
# the same text. Each is checked in the pinned service code; every other string compares exactly. Nine more looked case-free
# (get_upcoming.period, lookup_contact.name, view_emails.query, web_search.query, update_event/cancel_event/update_reminder/
# complete_reminder/append_note title) but echo the typed text back in no-match or error results, or pass it to a web engine or
# AppleScript as typed, so they stay exact.
CASE_FREE = {
    ("get_my_day_plan", "day"): "service/tools/proactive_tools.py:487 lowercases day before any use",
    ("find_free_time", "period"): "service/tools/schedule_extras.py:553 casefolds period; its errors are static",
}


def _same(name: str, key: str, a, b) -> bool:
    if a == b:
        return True
    return (name, key) in CASE_FREE and a[0] == b[0] == "str" and a[1].casefold() == b[1].casefold()


class CallSyntaxError(ValueError):
    pass


def _value(t: str, i: int):
    """One value of Gemma's call syntax at t[i] -> (python value, index after it)."""
    while i < len(t) and t[i].isspace():
        i += 1
    if t.startswith(Q, i):
        end = t.find(Q, i + len(Q))
        if end < 0:
            raise CallSyntaxError("unclosed string")
        return t[i + len(Q):end], end + len(Q)
    if i < len(t) and t[i] == '"':                       # JSON-style string, in case base writes one
        try:
            v, end = json.JSONDecoder().raw_decode(t, i)
        except json.JSONDecodeError as exc:
            raise CallSyntaxError(str(exc)) from exc
        return v, end
    if i < len(t) and t[i] in "[{":
        close, items, obj = ("]" if t[i] == "[" else "}"), [], {}
        i += 1
        while True:
            while i < len(t) and t[i].isspace():
                i += 1
            if i >= len(t):
                raise CallSyntaxError("unclosed " + close)
            if t[i] == close:
                return (items if close == "]" else obj), i + 1
            if close == "]":
                v, i = _value(t, i)
                items.append(v)
            else:
                k, i = _key(t, i)
                if k in obj:
                    raise CallSyntaxError(f"argument {k} given twice")
                v, i = _value(t, i)
                obj[k] = v
            while i < len(t) and t[i].isspace():
                i += 1
            if i < len(t) and t[i] == ",":
                i += 1
            elif i >= len(t) or t[i] != close:
                raise CallSyntaxError(f"expected , or {close} at {i}")
    m = re.compile(r"[^,\]}]*").match(t, i)
    word = m.group(0).strip()
    if not word:
        raise CallSyntaxError(f"no value at {i}")
    if word in BARE:
        return BARE[word], m.end()
    if NUMBER.fullmatch(word):
        return (float(word) if "." in word else int(word)), m.end()
    raise CallSyntaxError(f"unquoted text {word!r}: a string or a number?")       # ambiguous: fail closed


def _key(t: str, i: int):
    m = re.compile(r'\s*(?:<\|"\|>([^<]*?)<\|"\|>|"([^"]*)"|([A-Za-z_][\w.-]*))\s*:').match(t, i)
    if not m:
        raise CallSyntaxError(f"no key at {i}")
    return next(g for g in m.groups() if g is not None), m.end()


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise CallSyntaxError("an argument given twice")
    return dict(pairs)


def parse_call_text(raw: str) -> dict:
    """Gemma's ``{key:<|"|>text<|"|>,n:3,flag:false,items:[<|"|>a<|"|>]}`` (or JSON) -> a dict of python values.
    Raises CallSyntaxError on anything else, so a malformed call can never match."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        v = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError:
        v, end = _value(text, 0)
        if text[end:].strip():
            raise CallSyntaxError("text after the arguments") from None
    if not isinstance(v, dict):
        raise CallSyntaxError("arguments are not an object")
    return v


def parse_args(raw) -> dict | None:
    """Each top-level argument to its typed form, empty values included; None when the arguments can't be read."""
    try:
        if not isinstance(raw, dict):
            raw = parse_call_text(str(raw or ""))
        return {k: canon(v) for k, v in raw.items()}
    except CallSyntaxError:
        return None


def scripted_calls(row: dict) -> list[tuple[str, dict]]:
    out = []
    for tc in row["messages"][-1].get("tool_calls") or []:
        fn = tc["function"]
        args = fn.get("arguments")
        args = json.loads(args, object_pairs_hook=_no_duplicate_keys) if isinstance(args, str) else (args or {})
        out.append((fn["name"], parse_args(args)))
    return out


def arg_problem(name: str, got: dict, want: dict, specs=None) -> str | None:
    """None when base's arguments run the scripted call exactly. Keys on both sides must hold the same typed value. A key on
    one side only is accepted only when its value there IS that tool's own Python default (``tool_specs.json``), because
    then leaving it out runs the same call: ``search_reminders()`` is ``search_reminders(query="")``. Otherwise a key the
    script has and base left out is ``call_missing_argument``, and one base added is ``call_extra_argument``: the schemas
    declare no defaults, so ``calendar_only``, ``unread``, ``count``, ``account`` or ``cc`` can't be shown to leave the
    call's scope alone. A tool with no spec gets no such equivalence."""
    defaults = {k: canon(v) for k, v in ((specs if specs is not None else SPEC).get(name) or {}).get("defaults", {}).items()}
    worst = None
    for k in sorted(set(got) | set(want)):
        if k in got and k in want:
            if not _same(name, k, got[k], want[k]):
                return "call_mismatch"
        elif k in want:
            if not (k in defaults and _same(name, k, defaults[k], want[k])):
                worst = worst or "call_missing_argument"
        elif not (k in defaults and _same(name, k, defaults[k], got[k])):
            worst = "call_extra_argument" if worst in (None, "call_extra_argument") else worst
    return worst


def call_problem(base_calls: list[dict], want: list[tuple[str, dict]], specs=None) -> str | None:
    """None when base made the scripted calls (see ``arg_problem``). Several calls in one step may come in any order only
    when every call on both sides is a read, so neither depends on the other; calls that write keep the script's order.
    A tool with no spec counts as a write. -> "call_mismatch" (other tools or values, or arguments that can't be read),
    "call_order", "call_missing_argument" or "call_extra_argument"."""
    specs = specs if specs is not None else SPEC
    got = [(c["function"]["name"], parse_args(c["function"].get("arguments"))) for c in base_calls or []]
    if any(a is None for _, a in got) or any(a is None for _, a in want):
        return "call_mismatch"
    if sorted(n for n, _ in got) != sorted(n for n, _ in want):
        return "call_mismatch"
    all_reads = all((specs.get(n) or {}).get("read") for n, _ in got + want)
    if not all_reads:
        if [n for n, _ in got] != [n for n, _ in want]:
            return "call_order"
        problems = [arg_problem(n, g, w, specs) for (n, g), (_, w) in zip(got, want)]
        return next((p for p in problems if p == "call_mismatch"), None) or next((p for p in problems if p), None)
    # Reads: each scripted call takes one distinct base call. Prefer an exact one, so a duplicate can't be matched twice.
    left, found = list(got), []
    for name, args in want:
        ranked = sorted((i for i, (n, _) in enumerate(left) if n == name), key=lambda i: arg_problem(name, left[i][1], args, specs) is not None)
        if not ranked:
            return "call_mismatch"
        i = ranked[0]
        found.append(arg_problem(name, left[i][1], args, specs))
        left.pop(i)
    return next((p for p in found if p == "call_mismatch"), None) or next((p for p in found if p), None)


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
