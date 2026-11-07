#!/usr/bin/env python3
"""Turn base's answers into r5's final build, on the Mac. Copies the build, splices chosen.jsonl, removes the rows that
stayed the BASE_WRITES placeholder (drop.ids) or ask something too close to a test question, and refuses a build where
the placeholder survives. Then run the builder's
validate.py on --out, and pack_r4.py --data <out>.

    finish_r5.py --build BUILD --chosen chosen.jsonl --drop drop.ids --out FINAL
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
sys.path.insert(0, str(TRAINING / "ask-lora-r2" / "builder"))
os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")
SPLICE = TRAINING / "ask-lora-r4" / "distill" / "splice.py"
PLACEHOLDER = "Base writes this answer."    # scen_r5_debuglog.BASE_WRITES; test_finish_r5.py compares them
SPLITS = {"all": "meta.jsonl", "train": "train.meta.jsonl", "valid": "valid.meta.jsonl"}    # split -> its meta file


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


def leaking_turns(out: Path) -> set:
    """Row ids of every turn whose question is too close to a test question. The build checks this against the pinned
    toolbench, which lacks this branch's Brief and session cases, so it runs again here against all of them."""
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
        if hit and not meta.get("leak_ok"):
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", required=True)
    ap.add_argument("--chosen", required=True)
    ap.add_argument("--drop", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists():
        sys.exit(f"finish_r5: {out} already exists")
    r = subprocess.run([sys.executable, str(SPLICE), "--build", a.build, "--chosen", a.chosen, "--out", str(out)])
    if r.returncode:
        sys.exit(f"finish_r5: splice failed with exit {r.returncode}")
    drop = {line.strip() for line in open(a.drop) if line.strip()}
    leaking = leaking_turns(out)
    removed = drop_rows(out, drop | leaking)
    left = placeholders_left(out)
    if left:
        sys.exit(f"finish_r5: {len(left)} rows still hold the placeholder, such as {left[:5]}")
    print(json.dumps({"placeholders_dropped": len(drop), "close_to_test_questions": sorted(leaking), "removed": removed, "rows": {s: sum(1 for _ in open(out / f"{s}.jsonl")) for s in SPLITS}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
