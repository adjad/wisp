#!/usr/bin/env python3
"""Read-only r5 overlap rule of a build against a sealed toolbench private-format eval dir. Ids and scores only.

    check_r51_eval_overlap.py --build DIR --eval-dir DIR --out FILE.json

Rule (leak_check_r5.check_overlap / seal_r5): every user turn of a row (the current = last, and history turns of 4+ words) is
matched by leak.py's 0.7 character-trigram Jaccard against every user turn (prompt + history) of every eval case.
Reads all.jsonl + meta.jsonl. Never modifies the build; never prints or writes eval wording.
"""
import argparse, hashlib, json, os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
sys.path.insert(0, str(TRAINING / "ask-lora-r5" / "mi355x"))
sys.path.insert(0, str(TRAINING / "ask-lora-r2" / "builder"))
os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")
import leak, leak_check_r5 as lc  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--build", required=True); ap.add_argument("--eval-dir", required=True); ap.add_argument("--out", required=True)
a = ap.parse_args()
build, ev = Path(a.build), os.path.expanduser(a.eval_dir)
rows = []
with open(build / "all.jsonl") as fe, open(build / "meta.jsonl") as fm:
    for x, y in zip(fe, fm, strict=True):
        rows.append(("all", json.loads(x), json.loads(y)))
entries = lc.private_entries(ev)
idx = leak.LeakIndex(entries)
hits = []
for _s, ex, meta in rows:
    users = lc.user_turns(ex)
    for i, t in enumerate(users):
        last = i == len(users) - 1
        if not last and len(t.split()) < lc.HISTORY_MIN_WORDS:
            continue
        hit, score, q = idx.leaks(t)
        if hit:
            hits.append({"row_id": meta["id"], "case_id": q["id"].split(":", 2)[-1], "score": round(score, 3),
                         "where": "current" if last else "history"})
sha = {f: hashlib.sha256(open(os.path.join(ev, f), "rb").read()).hexdigest()
       for f in ("cases.yaml", "world.json", "worlds.json") if os.path.isfile(os.path.join(ev, f))}
out = {"build": str(build), "eval_dir_sha256": sha,
       "rule": f"r5 rule unchanged: leak.py trigram Jaccard >= {leak.THRESHOLD} on every user turn (current + history of 4+ words) "
               "vs every eval prompt + history user turn; no exemption",
       "eval_reference_turns": len(entries), "rows_checked": len(rows), "hits": hits,
       "hit_rows": len({h["row_id"] for h in hits})}
Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
print(json.dumps({k: out[k] for k in ("rows_checked", "eval_reference_turns", "hit_rows")}), "hits:", len(hits))
