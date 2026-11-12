#!/usr/bin/env python3
"""Summary for compare_backends.sh: the same prompts' oQ4e and bf16 samples after the same filter and choose steps."""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

CALL_BAD = {"call_mismatch", "no_call", "call_missing_argument", "call_extra_argument", "text_with_call"}


def jl(p):
    p = Path(p)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def ids(p):
    return [l.strip() for l in open(p) if l.strip()]


def arm(out: Path, name: str, meta: dict, fin: list, cal: list, old_drops: set) -> dict:
    w = out / name
    raw = jl(w / "raw" / "base.jsonl")
    chosen = {r["id"] for r in jl(w / "choose" / "chosen.jsonl")}
    rej = jl(w / "choose" / "rejections.jsonl")
    trej = jl(w / "choose" / "trace_rejections.jsonl")
    filt = json.loads((w / "choose" / "raw_filter.log").read_text().strip().splitlines()[-1])
    safety = json.loads((w / "choose" / "safety_check.log").read_text().strip().splitlines()[-1])
    tstats = json.loads((w / "choose" / "traces.stats.json").read_text())
    fset, cset = set(fin), set(cal)
    fs = [d for d in raw if d["id"] in fset]
    cs = [d for d in raw if d["id"] in cset]
    passed = sorted(i for i in fin if i in chosen)
    by_intent = collections.defaultdict(lambda: [0, 0])
    for i in fin:
        it = (meta[i].get("gold") or {}).get("intent")
        by_intent[it][1] += 1
        by_intent[it][0] += i in chosen
    bad_call = {(r["id"], r["k"]) for r in trej if r["id"] in cset and set(r["reasons"]) & CALL_BAD}
    call_ok = [(d["id"], d["k"]) for d in cs if (d["id"], d["k"]) not in bad_call]
    return {
        "final_samples": len(fs), "call_samples": len(cs),
        "finals_prompts_passed": len(passed), "finals_prompts": len(fin),
        "old_drops_passed": sum(1 for i in old_drops if i in chosen), "old_drops": len(old_drops),
        "finals_pass_by_intent": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_intent.items())},
        "final_reject_reasons": dict(collections.Counter(x for r in rej if r["id"] in fset for x in r["reasons"]).most_common()),
        "raw_filter_cut": filt.get("raw_cut"), "raw_filter_cut_by": filt.get("cut_by"),
        "safety_backstop_rejected": safety.get("rejected"),
        "final_length_cut": sum(1 for d in fs if d.get("finish_reason") == "length"),
        "final_mean_completion_tokens": round(sum(d["usage"]["completion_tokens"] for d in fs) / max(1, len(fs)), 1),
        "call_samples_matching_script": len(call_ok),
        "call_prompts_with_a_match": len({i for i, _ in call_ok}), "call_prompts": len(cal),
        "call_reject_reasons": dict(collections.Counter(x for r in trej if r["id"] in cset for x in r["reasons"]).most_common()),
        "thoughts_kept": tstats.get("kept"),
        "_passed": passed, "_call_ok_prompts": sorted({i for i, _ in call_ok}),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sealed", required=True)
    ap.add_argument("--finals", required=True)
    ap.add_argument("--calls", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--old-drops", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    meta = {m["id"]: m for m in jl(Path(a.sealed) / "meta.jsonl")}
    fin, cal = ids(a.finals), ids(a.calls)
    old = set(ids(a.old_drops)) & set(fin)
    res = {n: arm(out, n, meta, fin, cal, old) for n in ("oq4e", "bf16")}
    pa, pb = set(res["oq4e"].pop("_passed")), set(res["bf16"].pop("_passed"))
    ca, cb = set(res["oq4e"].pop("_call_ok_prompts")), set(res["bf16"].pop("_call_ok_prompts"))
    res["paired"] = {"finals_both": len(pa & pb), "finals_only_oq4e": len(pa - pb), "finals_only_bf16": len(pb - pa),
                     "finals_neither": len(set(fin) - pa - pb),
                     "calls_both": len(ca & cb), "calls_only_oq4e": len(ca - cb), "calls_only_bf16": len(cb - ca)}
    (out / "comparison.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
