#!/usr/bin/env python3
"""r5's choose step over gen_finals.py's samples: r4's choose.py plus r5's own final checks.

A sample passes when r4's ``choose.reject_reasons`` finds nothing AND ``final_problems_r5`` finds nothing AND
``source_contract.final_problems`` finds nothing (the row's gold contract and the facts in its tool results) AND the
output carries no stray channel marker. On a row with a gold contract, an honest "I couldn't find any ..." after a read
isn't a stall: the contract decides whether it's true. The first passing sample by k is chosen per id, as in choose.py, and the
chosen file has the same format, so splice.py takes it unchanged.

Only rows whose target is a final answer are considered. Per family it reports how many ids passed and how many stay
scripted because no sample passed. A row whose scripted target is the BASE_WRITES placeholder with no passing sample
cannot stay (a final build must not contain the placeholder): those ids go to ``--out-drop-ids`` and the stats.

    choose_r5.py --partial partial.jsonl --raw 'raw/finals.*.jsonl' --out-chosen chosen.jsonl --out-stats stats.json \\
        [--out-nopass-ids need.ids] [--out-drop-ids drop.ids] [--k 6] [--stall-ok-after-tool] [--tool-names f.json]

``--out-nopass-ids`` lists every id with no passing sample (the top-up round's input). ``--stall-ok-after-tool``
stops "stall" from rejecting a final that follows a tool result (an honest "I couldn't find any ..." after an empty
search); it is off by default, and the stats always say how many no-pass ids failed on a stall alone after a tool.
"""
from __future__ import annotations

import _paths  # noqa: F401

import argparse
import glob
import json
import os
import re
import sys
import tempfile
from collections import Counter

import choose  # noqa: E402  (r4)
import source_contract  # noqa: E402  (shared with builder/validate.py)

BASE_WRITES = "Base writes this answer."          # scen_r5_debuglog.BASE_WRITES
# --- copied from builder/validate.py (final_problems_r5); test_gen_finals.py compares them when validate imports
R5_MORE = re.compile(r"(?i)\b(?:and|plus) \d+ more\b")
R5_MEMORY_STALL = re.compile(r"(?i)\b(?:nothing (?:was |is )?saved|didn['’]?t save|nothing(?:['’]s| is| was)? in your memory|"
                             r"we haven['’]?t talked about)\b")
R5_RECALL_QKINDS = frozenset({"rec_user", "rec_asst", "rec_both", "rec_none"})


def is_recall_row(meta) -> bool:
    meta = meta or {}
    return meta.get("qkind") in R5_RECALL_QKINDS or bool(meta.get("recall_refs")) or bool(meta.get("recall_absent"))


def final_problems_r5(text: str, meta=None, had_tool_call: bool = False) -> list[tuple[str, str]]:
    out = []
    m = R5_MORE.search(text or "")
    if m:
        out.append(("final:and_n_more", m.group(0)))
    m = R5_MEMORY_STALL.search(text or "")
    if m and not had_tool_call and not is_recall_row(meta):
        out.append(("final:memory_stall", m.group(0)))
    return out
# --- end copy

ORIG_REJECT = choose.reject_reasons


def is_final(row: dict) -> bool:
    last = (row.get("messages") or [{}])[-1]
    return last.get("role") == "assistant" and not last.get("tool_calls") and isinstance(last.get("content"), str)


def had_tool_call(row: dict) -> bool:
    """The final's turn called a tool: an assistant tool call after the last user message."""
    msgs = row["messages"][:-1]
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    return any(m.get("role") == "assistant" and m.get("tool_calls") for m in msgs[last_user + 1:])


REJECTIONS: list[dict] = []          # one line per rejected sample, for the rejection manifest


def make_reject(stall_ok_after_tool: bool = False):
    def reject(rec: dict, row: dict) -> list[str]:
        out = ORIG_REJECT(rec, row)
        tool = had_tool_call(row)
        gold = row["meta"].get("gold") or {}
        if tool and (stall_ok_after_tool or gold.get("kind") in ("all_sources", "memory_contrast")):
            out = [x for x in out if x != "stall"]
        if rec.get("channel_leak"):
            out.append("channel_leak")
        out += [k for k, _ in final_problems_r5(rec.get("content") or "", row["meta"], tool)]
        out += [k for k, _ in source_contract.final_problems(rec.get("content") or "", row["meta"], row["messages"][:-1])]
        if out:
            REJECTIONS.append({"id": row["meta"]["id"], "family": row["meta"]["family"], "k": rec.get("k"), "reasons": out})
        return out
    return reject


def gold_refs(rows: list[dict]) -> dict:
    """session_chat_r4's recall facts now travel in meta.gold, so r5's chooser checks them without refs.py's map."""
    refs = {r["meta"]["id"]: r["meta"]["gold"] for r in rows if (r["meta"].get("gold") or {}).get("ref") is not None}
    return {i: {"kind": g["kind"], "ref": list(g["ref"]), "ok": list(g.get("ok") or [])} for i, g in refs.items()}


def choose_r5(partial: str, raws: list[str], k=None, hash_check=True, refs=(), stall_ok_after_tool=False):
    """-> (final rows, chosen {id: record}, stats). choose.run's own stats plus the r5 report."""
    finals = [r for r in choose.read_jsonl(partial) if is_final(r)]
    REJECTIONS.clear()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    tmp_refs = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        for r in finals:
            tmp.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.close()
        json.dump(gold_refs(finals), tmp_refs)
        tmp_refs.close()
        choose.reject_reasons = make_reject(stall_ok_after_tool)
        try:
            _, chosen, stats = choose.run(tmp.name, raws, k, hash_check, [tmp_refs.name, *refs])
        finally:
            choose.reject_reasons = ORIG_REJECT
    finally:
        os.unlink(tmp.name)
        os.unlink(tmp_refs.name)
    stats["r5"] = report(finals, chosen, stats)
    return finals, chosen, stats


def report(finals: list[dict], chosen: dict, stats: dict) -> dict:
    fams: dict[str, dict] = {}
    all_drop, all_nopass = [], []
    for r in finals:
        m = r["meta"]
        f = fams.setdefault(m["family"], {"final_rows": 0, "passed": 0, "no_samples": [], "scripted_kept": [],
                                          "drop": [], "stall_only_after_tool": []})
        f["final_rows"] += 1
        if m["id"] in chosen:
            f["passed"] += 1
            continue
        all_nopass.append(m["id"])
        if r["messages"][-1]["content"] == BASE_WRITES:
            f["drop"].append(m["id"])
            all_drop.append(m["id"])
        else:
            f["scripted_kept"].append(m["id"])
        gfam = stats["families"].get(m["family"], {})
        np_ = (gfam.get("no_pass") or {}).get(m["id"])
        if np_ is None:
            f["no_samples"].append(m["id"])
        elif had_tool_call(r) and all(s["reasons"] == ["stall"] for s in np_):
            f["stall_only_after_tool"].append(m["id"])
    for name, f in fams.items():
        f["rejects_by_reason"] = (stats["families"].get(name) or {}).get("rejects_by_reason", {})
        f["still_scripted"] = len(f["scripted_kept"])
        f["to_drop"] = len(f["drop"])
    return {"families": fams, "final_rows": len(finals), "passed": len(chosen), "no_pass_ids": sorted(all_nopass),
            "drop_ids": sorted(all_drop),
            "rejects_by_reason": _sum_reasons(fams)}


def _sum_reasons(fams: dict) -> dict:
    c = Counter()
    for f in fams.values():
        c.update(f["rejects_by_reason"])
    return dict(c.most_common())


def summary(rep: dict) -> str:
    lines = [f"{'family':18} {'finals':>6} {'passed':>6} {'scripted':>8} {'drop':>5} {'no-samples':>10} {'stall-only':>10}"]
    for name, f in sorted(rep["families"].items()):
        lines.append(f"{name:18} {f['final_rows']:6} {f['passed']:6} {f['still_scripted']:8} {f['to_drop']:5} "
                     f"{len(f['no_samples']):10} {len(f['stall_only_after_tool']):10}")
    lines.append(f"{'ALL':18} {rep['final_rows']:6} {rep['passed']:6} "
                 f"{sum(f['still_scripted'] for f in rep['families'].values()):8} {len(rep['drop_ids']):5}")
    lines.append(f"rejects by reason: {rep['rejects_by_reason']}")
    if rep["drop_ids"]:
        lines.append(f"DROP {len(rep['drop_ids'])} rows (BASE_WRITES, no passing sample): "
                     + ", ".join(rep["drop_ids"][:20]) + (" ..." if len(rep["drop_ids"]) > 20 else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--partial", required=True)
    ap.add_argument("--raw", action="append", required=True, help="gen_finals.py output; repeatable, globs allowed")
    ap.add_argument("--out-chosen", required=True)
    ap.add_argument("--out-stats", required=True)
    ap.add_argument("--out-nopass-ids")
    ap.add_argument("--out-drop-ids")
    ap.add_argument("--out-rejections", help="jsonl: id, family, k, reasons of every rejected sample")
    ap.add_argument("--k", type=int, default=None, help="only samples with k < K")
    ap.add_argument("--tool-names", help="JSON list of every live tool name (refs.py --registry-out)")
    ap.add_argument("--refs", action="append", default=[])
    ap.add_argument("--no-hash-check", action="store_true")
    ap.add_argument("--stall-ok-after-tool", action="store_true")
    a = ap.parse_args(argv)
    if a.tool_names:
        choose.EXTRA_TOOL_NAMES.update(json.load(open(a.tool_names)))
    raws = sorted({p for pat in a.raw for p in (glob.glob(pat) or [pat])})
    finals, chosen, stats = choose_r5(a.partial, raws, a.k, not a.no_hash_check, a.refs, a.stall_ok_after_tool)
    with open(a.out_chosen, "w", encoding="utf-8") as f:
        for r in finals:
            if r["meta"]["id"] in chosen:
                f.write(json.dumps(chosen[r["meta"]["id"]], ensure_ascii=False) + "\n")
    with open(a.out_stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    if a.out_rejections:
        with open(a.out_rejections, "w", encoding="utf-8") as f:
            for r in REJECTIONS:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for path, ids in ((a.out_nopass_ids, stats["r5"]["no_pass_ids"]), (a.out_drop_ids, stats["r5"]["drop_ids"])):
        if path:
            with open(path, "w") as f:
                f.write("".join(i + "\n" for i in ids))
    print(summary(stats["r5"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
