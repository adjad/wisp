#!/usr/bin/env python3
"""r5's stats.json, always counted from the rows on disk. The audit found 8,430 rows under a nested count of 8,873, because a
later step dropped rows and kept the build's stats. Anything that changes rows calls ``refresh``.

Origins:
- final rows: ``base_r4_reuse`` (r4 sampled it from the served base), ``base_generated`` (this round), ``scripted`` (the builder's
  text, kept on purpose or as a fallback), ``placeholder`` (still waiting for base: only allowed in a build marked incomplete).
- tool rows: the call is always ``scripted``. ``trace_origin`` says where its thought came from: ``base``, ``scripted`` or ``none``.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

PLACEHOLDER = "Base writes this answer."
SPLITS = {"all": "meta.jsonl", "train": "train.meta.jsonl", "valid": "valid.meta.jsonl"}
TEXT_FILES = ("all.jsonl", "meta.jsonl", "train.jsonl", "train.meta.jsonl", "valid.jsonl", "valid.meta.jsonl", "partial.jsonl")


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_forced(ex: dict, meta: dict) -> bool:
    """The served prompt ends inside the thought channel: the step follows a tool result with thinking on."""
    msgs = ex["messages"]
    return len(msgs) >= 2 and msgs[-2].get("role") == "tool" and bool((meta.get("chat_template_kwargs") or {}).get("enable_thinking", True))


def origin_of(ex: dict, meta: dict) -> str:
    last = ex["messages"][-1]
    if last.get("tool_calls"):
        return "scripted_call"
    if last.get("content") == PLACEHOLDER:
        return "placeholder"
    if meta.get("distilled"):
        return meta.get("origin") or "base_generated"
    return "scripted"


def trace_origin(meta: dict) -> str:
    if not meta.get("think"):
        return "none"
    return "base" if meta.get("distilled") or meta.get("trace_origin") == "base" else "scripted"


def counted(build: Path) -> dict:
    fam = defaultdict(Counter)
    origins, traces, kinds, shapes = Counter(), Counter(), Counter(), Counter()
    lens = defaultdict(list)
    splits = {}
    for split, meta_name in SPLITS.items():
        p = build / meta_name
        splits[split] = sum(1 for _ in open(p)) if p.is_file() else 0
    with open(build / "all.jsonl") as fe, open(build / "meta.jsonl") as fm:
        for a, b in zip(fe, fm, strict=True):
            ex, m = json.loads(a), json.loads(b)
            o, tr = origin_of(ex, m), trace_origin(m)
            forced = is_forced(ex, m)
            origins[o] += 1
            traces[tr] += 1
            kinds[m["target_kind"]] += 1
            shapes[("forced" if forced else "first") + ("_think" if m.get("think") else "_empty" if forced else "_no_think")] += 1
            f = fam[m["family"]]
            f["rows"] += 1
            f[f"target_{m['target_kind']}"] += 1
            f[f"origin_{o}"] += 1
            f[f"trace_{tr}"] += 1
            if forced and not m.get("think"):
                f["forced_empty"] += 1
            if not ex["messages"][-1].get("tool_calls") and o != "placeholder":
                lens[m["family"]].append(len(ex["messages"][-1].get("content") or ""))
    turns = set()
    with open(build / "meta.jsonl") as fm:
        for b in fm:
            turns.add(json.loads(b)["id"].rsplit("-s", 1)[0])
    all_lens = [x for v in lens.values() for x in v]
    return {"examples": splits["all"], "train": splits["train"], "valid": splits["valid"], "turns": len(turns), "families_n": len(fam),
            "by_target": dict(kinds), "by_origin": dict(origins), "by_trace_origin": dict(traces), "rendered_shapes": dict(shapes),
            "final_chars_median": statistics.median(all_lens) if all_lens else None,
            "families": {k: dict(v) | ({"final_chars_median": statistics.median(lens[k])} if lens[k] else {}) for k, v in sorted(fam.items())}}


def refresh(build, *, status: str, extra: dict | None = None) -> dict:
    """Recount stats.json from the rows, keep the build's own head fields, and bind it to the rows' hashes."""
    build = Path(build)
    path = build / "stats.json"
    old = json.loads(path.read_text()) if path.is_file() else {}
    fresh = counted(build)
    stats = {k: old[k] for k in ("code_head", "service_head") if k in old} | fresh | {"status": status} | (extra or {})
    if "thinking" in old:
        stats["builder_thinking_before_answers"] = old["thinking"]
    elif "builder_thinking_before_answers" in old:
        stats["builder_thinking_before_answers"] = old["builder_thinking_before_answers"]
    stats["files_sha256"] = {n: sha256(build / n) for n in TEXT_FILES if (build / n).is_file()}
    tmp = build / "stats.json.tmp"
    tmp.write_text(json.dumps(stats, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return stats
