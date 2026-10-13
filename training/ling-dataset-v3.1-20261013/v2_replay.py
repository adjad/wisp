#!/usr/bin/env python3
"""Build V2 REPLAY rows for the Ling V3 fine-tune (deterministic, stdlib only).

Selects 600 train / 60 dev rows from the Ling V2 dataset, stratified by V2's primary
competency (provenance `primary_group`), screens user text against V3's held-out eval
shingles (generate.max_heldout_overlap / OVERLAP_LIMIT; generate.main() is never called),
and writes v2_replay.{train,dev,provenance}.jsonl. V2 messages are kept byte-for-byte;
rows gain "tools": [], ids v2r-<split>-NNNN and conversation_id == id.

Usage: python3 v2_replay.py [--v2-dir DIR] [--out-dir DIR]
"""
import argparse, json, random, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate  # noqa: E402  (import only; do not call generate.main())

SEED = 20261012
QUOTA = {"train": 600, "dev": 60}
DEFAULT_V2 = HERE.parent / "ling-dataset-v2-20261006"


def read_jsonl(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x]


def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def allocate(counts, total):
    """Largest-remainder allocation proportional to counts (ties broken by name)."""
    n = sum(counts.values())
    exact = {k: total * v / n for k, v in counts.items()}
    alloc = {k: int(e) for k, e in exact.items()}
    left = total - sum(alloc.values())
    for k in sorted(counts, key=lambda k: (-(exact[k] - alloc[k]), k))[:left]:
        alloc[k] += 1
    return alloc


def user_texts(row):
    return [m["content"] for m in row["messages"] if m["role"] == "user"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--v2-dir", type=Path, default=DEFAULT_V2)
    ap.add_argument("--out-dir", type=Path, default=HERE)
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    prov_out, summary = [], {}
    for split in ("train", "dev"):
        rows = read_jsonl(a.v2_dir / f"{split}.jsonl")
        prov = {p["id"]: p for p in read_jsonl(a.v2_dir / f"{split}.provenance.jsonl")}
        groups = {}
        for r in rows:
            groups.setdefault(prov[r["id"]]["primary_group"], []).append(r)
        alloc = allocate({k: len(v) for k, v in groups.items()}, QUOTA[split])
        chosen, rejected = [], 0
        for comp in sorted(groups):
            cand = sorted(groups[comp], key=lambda r: r["id"])
            random.Random(f"{SEED}:v2-replay:{split}:{comp}").shuffle(cand)
            got = []
            for r in cand:  # next stratified candidate replaces a rejected one
                if len(got) == alloc[comp]:
                    break
                if max(generate.max_heldout_overlap(t) for t in user_texts(r)) >= generate.OVERLAP_LIMIT:
                    rejected += 1
                    continue
                got.append(r)
            assert len(got) == alloc[comp], (split, comp)
            chosen += [(comp, r) for r in got]
        chosen.sort(key=lambda cr: cr[1]["id"])
        out = []
        for i, (comp, r) in enumerate(chosen, 1):
            rid = f"v2r-{split}-{i:04d}"
            out.append(dumps({"id": rid, "conversation_id": rid, "messages": r["messages"], "tools": []}))
            prov_out.append(dumps({"id": rid, "source_v2_id": r["id"], "split": split, "competency": comp,
                                   "task": prov[r["id"]]["task"], "category": "v2_replay"}))
        (a.out_dir / f"v2_replay.{split}.jsonl").write_text("\n".join(out) + "\n")
        summary[split] = {"allocation": alloc, "heldout_rejections": rejected, "rows": len(out)}
    (a.out_dir / "v2_replay.provenance.jsonl").write_text("\n".join(prov_out) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
