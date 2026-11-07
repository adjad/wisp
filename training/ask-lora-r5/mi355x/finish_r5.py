#!/usr/bin/env python3
"""Turn base's answers into r5's final build, on the Mac. Then run the builder's validate.py on --out, and pack_r4.py --data <out>.

    finish_r5.py --build BUILD --chosen chosen.jsonl --drop drop.ids --out FINAL [--traces traces.chosen.jsonl]
                 --choose-stats stats.json [--rejections rejections.jsonl] [--allow-incomplete]

Steps, in order:
1. Copy the build and splice ``chosen.jsonl`` (base's answers) into it.
2. Splice ``--traces``: base's own thought at tool-call steps where base's call matched the script. The call stays scripted.
3. Mark every row's origin (see ``r5_stats``).
4. Remove rows, each with its reason in ``drop_manifest.jsonl``: answers still the placeholder, questions close to a test
   question (no exemption), an empty thought after a tool result with nothing to replace it (a tool step takes its whole turn with
   it), and any chosen answer that fails ``source_contract`` here even though it passed the chooser (which means the two have
   drifted).
5. Refuse a build that still holds a placeholder, and recount ``stats.json`` from the rows left.

**A build is ``complete`` only when every final was actually sampled.** Without ``--allow-incomplete``, finals that base never
sampled stop the run. With it, the output's status is ``incomplete_no_base_answers``, ``pack_r4.py`` refuses to pack it, and it must
never be called the dataset: it exists to check the pipeline.

The empty thought: after a tool result, the served prompt ends inside the thought channel, so a row with no thought supervises
``\\n<channel|>``, which trains the model to close it at once. Base nearly always thinks there (0.999 in r4's gate). So such a row
gets base's own thought or it leaves the build. First-step rows are different: Gemma's template renders nothing when there's no
thought, so they stay, and they're counted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
sys.path.insert(0, str(TRAINING / "ask-lora-r2" / "builder"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TRAINING / "ask-lora-r5" / "gpu"))
os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import r5_stats  # noqa: E402
import source_contract  # noqa: E402

SPLICE = TRAINING / "ask-lora-r4" / "distill" / "splice.py"
PLACEHOLDER = r5_stats.PLACEHOLDER    # scen_r5_debuglog.BASE_WRITES; test_finish_r5.py compares them
SPLITS = {"all": "meta.jsonl", "train": "train.meta.jsonl", "valid": "valid.meta.jsonl"}    # split -> its meta file
REUSE_MODEL = "r4-served-base"


def drop_rows(out: Path, drop: set) -> dict:
    """Remove the dropped ids from each split and its meta, keeping the two line-aligned. Returns rows removed per split."""
    removed = {}
    for split, meta_name in SPLITS.items():
        rows, metas = out / f"{split}.jsonl", out / meta_name
        keep_r, keep_m, n = [], [], 0
        with open(rows) as fr, open(metas) as fm:
            for line, meta in zip(fr, fm, strict=True):
                if json.loads(meta)["id"] in drop:
                    n += 1
                else:
                    keep_r.append(line)
                    keep_m.append(meta)
        for path, lines in ((rows, keep_r), (metas, keep_m)):
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(lines))
            os.replace(tmp, path)
        removed[split] = n
    return removed


def rewrite_metas(out: Path, change) -> None:
    """Apply ``change(meta)`` (in place) to every split's meta lines."""
    for meta_name in SPLITS.values():
        path = out / meta_name
        lines = []
        with open(path) as f:
            for line in f:
                m = json.loads(line)
                change(m)
                lines.append(json.dumps(m, ensure_ascii=False) + "\n")
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines))
        os.replace(tmp, path)


def leaking_turns(out: Path) -> set:
    """Row ids of every turn whose question is too close to a test question. seal_r5.py removes these before base writes; this
    is the last check, against this checkout's toolbench, and leak_ok is no exemption."""
    import leak
    import writes
    index = leak.LeakIndex()
    prefixes = set()
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        rows = [(json.loads(a), json.loads(b)) for a, b in zip(fr, fm, strict=True)]
    for ex, meta in rows:
        msgs, roles = ex["messages"], [m["role"] for m in ex["messages"]]
        i = roles.index("user")
        if meta.get("family") in writes.MULTITURN:
            i = max(j for j, r in enumerate(roles) if r == "user")
        hit, _score, _q = index.leaks(msgs[i].get("content") or "")
        if hit:
            prefixes.add(meta["id"].rsplit("-s", 1)[0])
    return {meta["id"] for _ex, meta in rows if meta["id"].rsplit("-s", 1)[0] in prefixes}


def placeholders_left(out: Path) -> list:
    left = []
    for split, meta_name in SPLITS.items():
        with open(out / f"{split}.jsonl") as fr, open(out / meta_name) as fm:
            for line, meta in zip(fr, fm, strict=True):
                if json.loads(line)["messages"][-1].get("content") == PLACEHOLDER:
                    left.append(json.loads(meta)["id"])
    return left


def never_sampled_ids(stats_path) -> set:
    """Finals base never wrote a sample for, placeholders included. An unreadable report is an error: without it a row that was
    never sampled would look like one that was sampled and failed, and the build would call itself complete."""
    rep = (json.load(open(stats_path)).get("r5") or {}).get("families")
    if rep is None:
        sys.exit(f"finish_r5: {stats_path} has no r5 family report; pass choose_r5.py's --out-stats")
    return {i for f in rep.values() for i in f.get("no_samples", [])}


def load_chosen(path) -> dict:
    return {r["id"]: r for r in map(json.loads, open(path)) if r.get("id")}


def contract_failures(out: Path) -> dict:
    """id -> problem keys, for each base-written final that fails the contract here."""
    bad = {}
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        for a, b in zip(fr, fm, strict=True):
            ex, m = json.loads(a), json.loads(b)
            last = ex["messages"][-1]
            if m.get("distilled") and not last.get("tool_calls"):
                probs = source_contract.final_problems(last.get("content") or "", m, ex["messages"][:-1])
                if probs:
                    bad[m["id"]] = [k for k, _ in probs]
    return bad


def forced_empty_rows(out: Path) -> set:
    """Rows that would supervise an immediately closed thought."""
    ids = set()
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        for a, b in zip(fr, fm, strict=True):
            ex, m = json.loads(a), json.loads(b)
            if r5_stats.is_forced(ex, m) and not m.get("think") and ex["messages"][-1].get("content") != PLACEHOLDER:
                ids.add(m["id"])
    return ids


def turn_of(row_id: str) -> str:
    return row_id.rsplit("-s", 1)[0]


def target_kinds(out: Path) -> dict:
    with open(out / "meta.jsonl") as fm:
        return {m["id"]: m.get("target_kind") for m in map(json.loads, fm)}


def provenance_lines(out: Path, chosen: dict, traces: dict):
    with open(out / "all.jsonl") as fr, open(out / "meta.jsonl") as fm:
        for a, b in zip(fr, fm, strict=True):
            ex, m = json.loads(a), json.loads(b)
            c = chosen.get(m["id"]) or {}
            t = traces.get(m["id"]) or {}
            yield {"id": m["id"], "family": m["family"], "target_kind": m["target_kind"],
                   "origin": r5_stats.origin_of(ex, m), "trace_origin": r5_stats.trace_origin(m),
                   "sample_k": c.get("k", t.get("k")), "model": c.get("model") or t.get("model"),
                   "request_hash": c.get("request_hash") or t.get("request_hash"), "attn": c.get("attn"), "batch": c.get("batch")}


def write_jsonl(path: Path, rows) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", required=True)
    ap.add_argument("--chosen", required=True)
    ap.add_argument("--drop", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--traces")
    ap.add_argument("--rejections")
    ap.add_argument("--choose-stats", required=True, help="choose_r5.py's --out-stats: tells never sampled from sampled and failed")
    ap.add_argument("--allow-incomplete", action="store_true", help="for checking the pipeline only: the output can't be packed")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.exists():
        sys.exit(f"finish_r5: {out} already exists")
    r = subprocess.run([sys.executable, str(SPLICE), "--build", a.build, "--chosen", a.chosen, "--out", str(out)])
    if r.returncode:
        sys.exit(f"finish_r5: splice failed with exit {r.returncode}")
    chosen = load_chosen(a.chosen)
    traces = load_chosen(a.traces) if a.traces else {}

    def mark(m):
        c = chosen.get(m["id"])
        if c and m.get("distilled"):
            m["origin"] = "base_r4_reuse" if c.get("model") == REUSE_MODEL else "base_generated"
            m["trace_origin"] = "base"
        t = traces.get(m["id"])
        if t and m.get("target_kind") in ("call", "load"):
            m["think"], m["thought"], m["distilled"], m["trace_origin"] = True, t["reasoning"], True, "base"
            m["trace_k"] = t["k"]
    rewrite_metas(out, mark)

    # Never sampled is not the same as sampled and failed. choose_r5 lists both in drop.ids, so the stats tell them apart.
    never_sampled = never_sampled_ids(a.choose_stats)
    known_placeholders = set(placeholders_left(out))
    drops: dict[str, str] = {}
    for line in open(a.drop):
        i = line.strip()
        if i and i not in never_sampled:
            drops[i] = "no_base_answer_passed"
    for i in leaking_turns(out):
        drops.setdefault(i, "close_to_test_question")
    # A final base hasn't written yet has no thought *yet*: it only earns this drop once base has been asked and failed. A tool
    # step is the same in a pipeline check with no --traces. A dropped tool step takes its whole turn: a chain with a hole in it
    # isn't a chain, and the earlier steps' thoughts name dates only the missing call can vouch for.
    forced_empty = forced_empty_rows(out)
    tool_steps = target_kinds(out)
    awaiting_traces = {i for i in forced_empty if tool_steps.get(i) != "final"} if a.allow_incomplete and not a.traces else set()
    for i in forced_empty - never_sampled - awaiting_traces:
        drops.setdefault(i, "forced_empty_thought")
        if tool_steps.get(i) != "final":
            for other in tool_steps:
                if turn_of(other) == turn_of(i):
                    drops.setdefault(other, "turn_lost_a_tool_step")
    failures = contract_failures(out)
    for i in failures:
        drops.setdefault(i, "contract_failed_after_choose")

    # Rows nothing has answered block a complete build. In a pipeline check they stay visible and are counted.
    unsampled = sorted((known_placeholders & never_sampled) - set(drops))
    unsampled_scripted = len((never_sampled - known_placeholders) - set(drops))
    incomplete = bool(unsampled or unsampled_scripted)
    if incomplete and not a.allow_incomplete:
        shutil.rmtree(out)
        sys.exit(f"finish_r5: {len(unsampled)} placeholder rows and {unsampled_scripted} scripted finals have no base sample; "
                 "generate them first (--allow-incomplete only checks the pipeline)")
    removed = drop_rows(out, set(drops))
    left = placeholders_left(out)
    if left and not a.allow_incomplete:
        sys.exit(f"finish_r5: {len(left)} rows still hold the placeholder, such as {left[:5]}")

    write_jsonl(out / "drop_manifest.jsonl", ({"id": i, "reason": why, "problems": failures.get(i)} for i, why in sorted(drops.items())))
    write_jsonl(out / "provenance.jsonl", provenance_lines(out, chosen, traces))
    for src, name in ((a.rejections, "rejections.jsonl"), (a.choose_stats, "choose_stats.json"), (a.chosen, "chosen.jsonl"), (a.drop, "drop.ids"),
                      (a.traces, "traces.chosen.jsonl")):
        if src and os.path.isfile(src):
            shutil.copy2(src, out / name)
    drop_reasons = dict(Counter(drops.values()).most_common())
    status = "incomplete_no_base_answers" if incomplete else "complete"
    stats = r5_stats.refresh(out, status=status, extra={"drops": drop_reasons, "placeholders_left": len(set(left)),
                                                       "chosen_answers": len(chosen), "base_traces": len(traces),
                                                       "unsampled_scripted_finals": unsampled_scripted,
                                                       "forced_empty_awaiting_base": len((forced_empty & never_sampled) - set(drops)),
                                                       "forced_tool_steps_awaiting_traces": len(awaiting_traces - set(drops))})
    print(json.dumps({"status": status, "drops": drop_reasons, "removed": removed, "rows": {k: stats[k] for k in ("examples", "train", "valid")},
                      "by_origin": stats["by_origin"], "by_trace_origin": stats["by_trace_origin"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
