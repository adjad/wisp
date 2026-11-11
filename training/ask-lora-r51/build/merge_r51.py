#!/usr/bin/env python3
"""Assemble the Ask LoRA r5.1 training set: (a) r5-final minus the audited DROP_TURN turns, plus (b) the finished rows of
the new family (safety_contrast_r51). Works with no --new directory (then r5.1 = r5-final minus dropped turns).

    merge_r51.py --r5 DIR --decisions FILE --labels FILE [--new DIR] --out DIR [--wisp-repo PIN] [--no-validate]

Rules
  drop      every row of each turn whose decision is DROP_TURN (turn = id minus its "-s<step>" suffix). Each dropped row goes
            to OUT/drop_manifest.jsonl (source "r51_audit", with the reason); r5's own drop manifest is carried over after
            them (source "r5").
  split     r5 rows keep their r5 train/valid side. New rows are placed by CONTRAST GROUP: rows are joined into one unit when
            they share meta.gold.group (fallback: their turn id), the same folded prompt (leak.fold), or the same
            meta.thread. A unit that touches an r5 row goes to that r5 row's side (an r5 unit on both sides is an error).
            Any other unit is valid when sha1(smallest group key) % 10 == 0, then every intent present among the new rows that
            has no valid unit gets its lowest-sha1 free unit. Final asserts: no group, no thread and no folded prompt is on
            both sides.
  intent    write rows (every r5 row the audit labelled): the audit label. New rows: meta.gold.intent. Other rows:
            "read_or_answer" for call/load targets and "answer" for finals, except a final whose family is in
            RESTRAINT_FAMILIES or whose meta.clarify is true (the right move is no tool): "leave_alone_or_clarify".
  stats     by family / target kind / target tool (messages[-1].tool_calls names) / intent, before (r5-final) and after.
Outputs: all/meta/train/valid(.meta).jsonl, drop_manifest.jsonl, stats.json, COUNTS.md, sample_labels.jsonl, validate.log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
BUILDER = TRAINING / "ask-lora-r2" / "builder"
sys.path.insert(0, str(BUILDER))
import leak  # noqa: E402  (fold only; the toolbench index is lazy)

NEW_FAMILY = "safety_contrast_r51"
RESTRAINT_FAMILIES = {"restraint_r4", "safe_restraint"}
DEFAULT_PIN = TRAINING / "ask-lora-r5" / "data" / "pin-7e442f2d"
FILES = ["all.jsonl", "meta.jsonl", "train.jsonl", "train.meta.jsonl", "valid.jsonl", "valid.meta.jsonl"]
SAMPLE_N, SAMPLE_SEED = 60, 51


def jl(path):
    """(raw line, parsed) pairs, blank lines skipped."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.strip():
                out.append((line, json.loads(line)))
    return out


def turn_of(id_):
    return id_.rsplit("-s", 1)[0]


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def h10(key):
    return int(hashlib.sha1(key.encode()).hexdigest(), 16)


def gold_of(m):
    g = m.get("gold")
    return g if isinstance(g, dict) else {}


def group_of(m):
    return gold_of(m).get("group") or "turn:" + turn_of(m["id"])


def target_of(ex):
    return ex["messages"][-1]


def tools_of(ex):
    return [c["function"]["name"] for c in (target_of(ex).get("tool_calls") or [])]


def last_user(ex):
    for msg in reversed(ex["messages"]):
        if msg["role"] == "user":
            c = msg.get("content")
            return c if isinstance(c, str) else json.dumps(c)
    return ""


def intent_of(m, labels):
    if m["id"] in labels:
        return labels[m["id"]]
    g = gold_of(m).get("intent")
    if g:
        return g
    if m["target_kind"] == "final":
        return "leave_alone_or_clarify" if (m["family"] in RESTRAINT_FAMILIES or m.get("clarify")) else "answer"
    return "read_or_answer"


# --------------------------------------------------------------------------------------------- split

def place_new(new_metas, r5_side_by_row, r5_metas):
    """-> list of 'train'/'valid' per new row. r5_side_by_row: side per r5 kept row, aligned with r5_metas."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    def keys(m):
        ks = ["g:" + group_of(m), "f:" + leak.fold(m["prompt"])]
        if m.get("thread"):
            ks.append("t:" + m["thread"])
        return ks

    for m in new_metas + r5_metas:
        ks = keys(m)
        for k in ks[1:]:
            union(ks[0], k)
    forced = {}
    for m, side in zip(r5_metas, r5_side_by_row):
        root = find(keys(m)[0])
        if forced.setdefault(root, side) != side:
            raise SystemExit(f"merge_r51: an r5 unit ({root[:60]}) is on both train and valid")
    units = defaultdict(list)          # root -> new row indexes
    for i, m in enumerate(new_metas):
        units[find(keys(m)[0])].append(i)
    side = {}
    for root in units:
        if root in forced:
            side[root] = forced[root]
    free = sorted(r for r in units if r not in forced)
    for r in free:
        side[r] = "valid" if h10(r) % 10 == 0 else "train"
    intents_of = lambda root: {gold_of(new_metas[i]).get("intent") for i in units[root]} - {None}   # noqa: E731
    have = set().union(*[intents_of(r) for r in units if side[r] == "valid"]) if units else set()
    for intent in sorted(set().union(*[intents_of(r) for r in units]) if units else set()):
        if intent in have:
            continue
        cands = [r for r in free if side[r] == "train" and intent in intents_of(r)]
        if cands:
            pick = min(cands, key=lambda r: (h10(r), r))
            side[pick] = "valid"
            have |= intents_of(pick)
    return [side[find(keys(m)[0])] for m in new_metas]


def check_sides(metas, sides):
    seen = {"group": defaultdict(set), "thread": defaultdict(set), "fold": defaultdict(set)}
    for m, s in zip(metas, sides):
        seen["group"][group_of(m)].add(s)
        seen["fold"][leak.fold(m["prompt"])].add(s)
        if m.get("thread"):
            seen["thread"][m["thread"]].add(s)
    for kind, d in seen.items():
        bad = sorted(k for k, v in d.items() if len(v) > 1)
        assert not bad, f"{kind}s on both train and valid: {bad[:5]}"


# --------------------------------------------------------------------------------------------- stats

def tally(exs, metas, labels):
    c = {"rows": len(metas), "by_family": Counter(), "by_target_kind": Counter(), "by_target_tool": Counter(),
         "by_intent": Counter()}
    for ex, m in zip(exs, metas):
        c["by_family"][m["family"]] += 1
        c["by_target_kind"][m["target_kind"]] += 1
        for t in tools_of(ex):
            c["by_target_tool"][t] += 1
        c["by_intent"][intent_of(m, labels)] += 1
    return {k: (dict(sorted(v.items())) if isinstance(v, Counter) else v) for k, v in c.items()}


def counts_md(before, after, extra):
    lines = ["# r5.1 dataset counts (before = r5-final, after = r5.1)", ""]
    lines += [f"Rows: {before['rows']} -> {after['rows']} (dropped {extra['rows_dropped']} rows in "
              f"{extra['turns_dropped']} turns; new rows {extra['rows_new']}; train {extra['train']}, valid {extra['valid']}).", ""]
    for key, title in (("by_family", "Family"), ("by_target_kind", "Target kind"), ("by_target_tool", "Target tool"),
                       ("by_intent", "Intent")):
        lines += [f"## {title}", "", f"| {title} | before | after | delta |", "|---|---:|---:|---:|"]
        for k in sorted(set(before[key]) | set(after[key])):
            b, a = before[key].get(k, 0), after[key].get(k, 0)
            lines.append(f"| {k} | {b} | {a} | {a - b:+d} |")
        lines.append("")
    lines += ["Intent rule: write rows take the audit label; new rows their recorded gold intent; other rows are "
              "read_or_answer (call/load targets) or answer (finals), except restraint finals (family in "
              f"{sorted(RESTRAINT_FAMILIES)} or meta.clarify) which are leave_alone_or_clarify.",
              "Combined sha256 covers: " + ", ".join(extra["hashed_files"]) + " (not stats.json / COUNTS.md).", ""]
    return "\n".join(lines)


def sample_labels(exs, metas, labels, n=SAMPLE_N, seed=SAMPLE_SEED):
    by = defaultdict(list)
    for ex, m in zip(exs, metas):
        by[intent_of(m, labels)].append((ex, m))
    rng = random.Random(seed)
    quota = {k: 0 for k in sorted(by)}
    left = n
    while left and any(quota[k] < len(by[k]) for k in quota):      # round-robin: equal shares, leftovers spill over
        for k in quota:
            if left and quota[k] < len(by[k]):
                quota[k] += 1
                left -= 1
    out = []
    for k in sorted(by):
        for ex, m in rng.sample(by[k], quota[k]):
            t = target_of(ex)
            if t.get("tool_calls"):
                tgt = {"call": [{"name": c["function"]["name"], "args": c["function"].get("arguments")}
                                for c in t["tool_calls"]]}
            else:
                tgt = {"final": (t.get("content") or "")[:300]}
            out.append({"id": m["id"], "family": m["family"], "intent": k, "user": last_user(ex), "target": tgt})
    return out


# --------------------------------------------------------------------------------------------- main

def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


def run(a):
    r5, out = Path(a.r5), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    allr, metar = jl(r5 / "all.jsonl"), jl(r5 / "meta.jsonl")
    assert len(allr) == len(metar), "r5 all.jsonl and meta.jsonl differ in length"
    ids = [m["id"] for _, m in metar]
    assert len(set(ids)) == len(ids), "r5 ids are not unique"
    side_of = {m["id"]: "train" for _, m in jl(r5 / "train.meta.jsonl")}
    for _, m in jl(r5 / "valid.meta.jsonl"):
        assert m["id"] not in side_of, f"{m['id']} in both r5 splits"
        side_of[m["id"]] = "valid"
    assert set(side_of) == set(ids), "r5 train+valid ids differ from all ids"
    labels = {}
    for _, r in jl(a.labels):
        labels[r["id"]] = r["intent"]
    decisions = [r for _, r in jl(a.decisions)]
    drop_turns = {}
    for d in decisions:
        if d["decision"] == "DROP_TURN":
            drop_turns[d.get("turn") or turn_of(d["id"])] = d
    r5_turns = {turn_of(i) for i in ids}
    missing = sorted(t for t in drop_turns if t not in r5_turns)
    assert not missing, f"DROP_TURN turns not in r5: {missing[:5]}"

    keep = [k for k, i in enumerate(ids) if turn_of(i) not in drop_turns]
    manifest = []
    verdicts = Counter()    # rows dropped, by auditor_verdict (OVERLAP apart from label drops)
    for i in ids:
        t = turn_of(i)
        if t in drop_turns:
            d = drop_turns[t]
            manifest.append({"id": i, "turn": t, "source": "r51_audit", "reason": d.get("reason"),
                             "decision": d["decision"], "auditor_verdict": d.get("auditor_verdict")})
            verdicts[d.get("auditor_verdict") or "none"] += 1
    rows_dropped = len(manifest)
    for _, r in jl(r5 / "drop_manifest.jsonl"):
        manifest.append({**r, "source": "r5"})

    ex5 = [allr[k][1] for k in keep]
    me5 = [metar[k][1] for k in keep]
    raw_all5 = [allr[k][0] for k in keep]
    raw_meta5 = [metar[k][0] for k in keep]
    side5 = [side_of[ids[k]] for k in keep]

    exn, men, raw_alln, raw_metan = [], [], [], []
    if a.new:
        nd = Path(a.new)
        na, nm = jl(nd / "all.jsonl"), jl(nd / "meta.jsonl")
        assert len(na) == len(nm), "new all/meta differ in length"
        for (rl, e), (rm, m) in zip(na, nm):
            assert m["family"] == NEW_FAMILY, f"{m['id']}: family {m['family']} is not {NEW_FAMILY}"
        nids = [m["id"] for _, m in nm]
        assert len(set(nids)) == len(nids), "new ids are not unique"
        clash = set(nids) & set(ids)
        assert not clash, f"new ids collide with r5: {sorted(clash)[:5]}"
        exn, men = [e for _, e in na], [m for _, m in nm]
        raw_alln, raw_metan = [r for r, _ in na], [r for r, _ in nm]
    sidesn = place_new(men, side5, me5) if men else []

    metas, exs = me5 + men, ex5 + exn
    sides = side5 + sidesn
    check_sides(metas, sides)

    raw_all, raw_meta = raw_all5 + raw_alln, raw_meta5 + raw_metan
    # train/valid keep r5's order for r5 rows, then new rows in their own order
    write_lines(out / "all.jsonl", raw_all)
    write_lines(out / "meta.jsonl", raw_meta)
    for s in ("train", "valid"):
        sel = [k for k, x in enumerate(sides) if x == s]
        write_lines(out / f"{s}.jsonl", [raw_all[k] for k in sel])
        write_lines(out / f"{s}.meta.jsonl", [raw_meta[k] for k in sel])
    write_lines(out / "drop_manifest.jsonl", [json.dumps(r, ensure_ascii=False) for r in manifest])
    samples = sample_labels(exs, metas, labels)
    write_lines(out / "sample_labels.jsonl", [json.dumps(r, ensure_ascii=False) for r in samples])

    vcode = None
    if not a.no_validate:
        env = {**os.environ, "ASK_LORA_ROUND": "4", "ASK_LORA_R5": "1", "WISP_REPO": str(Path(a.wisp_repo).resolve())}
        with open(out / "validate.log", "w") as lg:
            r = subprocess.run([sys.executable, "validate.py", str(out / "all.jsonl"), str(out / "train.jsonl"),
                                str(out / "valid.jsonl")], cwd=BUILDER, env=env, stdout=lg, stderr=subprocess.STDOUT)
        vcode = r.returncode
        with open(out / "validate.log", "a") as lg:
            lg.write(f"validate exit {vcode}\n")

    hashed = [f for f in FILES + ["drop_manifest.jsonl", "sample_labels.jsonl", "validate.log"] if (out / f).exists()]
    shas = {f: sha256_file(out / f) for f in hashed}
    combined = hashlib.sha256("".join(f"{shas[f]}  {f}\n" for f in sorted(shas)).encode()).hexdigest()
    before_ex = [e for _, e in allr]
    before_me = [m for _, m in metar]
    before, after = tally(before_ex, before_me, labels), tally(exs, metas, labels)
    extra = {"rows_dropped": rows_dropped, "turns_dropped": len(drop_turns), "rows_new": len(men),
             "train": sides.count("train"), "valid": sides.count("valid"), "hashed_files": sorted(shas)}
    stats = {"before_r5_final": before, "after_r51": after, **{k: v for k, v in extra.items() if k != "hashed_files"},
             "dropped_rows_by_auditor_verdict": dict(sorted(verdicts.items())),
             "dropped_turns_by_auditor_verdict": dict(sorted(Counter(d.get("auditor_verdict") or "none" for d in drop_turns.values()).items())),
             "dropped_turns": sorted(drop_turns), "keep_after_check_turns": sorted(
                 d.get("turn") or turn_of(d["id"]) for d in decisions if d["decision"] == "KEEP_AFTER_CHECK"),
             "r5_manifest_rows_carried": len(manifest) - rows_dropped, "validate_exit": vcode,
             "files_sha256": shas, "combined_sha256": combined}
    (out / "stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n")
    (out / "COUNTS.md").write_text(counts_md(before, after, extra) + f"\nCombined sha256: {combined}\n")
    return stats, vcode


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--r5", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--new")
    ap.add_argument("--out", required=True)
    ap.add_argument("--wisp-repo", default=str(DEFAULT_PIN))
    ap.add_argument("--no-validate", action="store_true")
    a = ap.parse_args(argv)
    stats, vcode = run(a)
    print(f"rows {stats['before_r5_final']['rows']} -> {stats['after_r51']['rows']}; dropped {stats['rows_dropped']} rows / "
          f"{stats['turns_dropped']} turns; new {stats['rows_new']}; train {stats['train']} valid {stats['valid']}")
    print(f"validate exit: {vcode}")
    for f, s in stats["files_sha256"].items():
        print(f"{s}  {f}")
    print(f"{stats['combined_sha256']}  COMBINED")
    sys.exit(vcode or 0)


if __name__ == "__main__":
    main()
