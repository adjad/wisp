#!/usr/bin/env python3
"""r5.1 training copy with the new family's TRAIN rows repeated (user decision 2026-11-12: "Yes, repeat 2x").

Copies a finished build (train/valid jsonl + meta + stats) to OUT and appends each `--family` train row once more
(times-1 extra copies) with the id suffixed `-rep<N>`, so ids stay unique. Valid is untouched, no row's text changes,
and stats.json gets the new example count (pack_r4.py checks it) plus an `upsample` record.

    python upsample_r51.py --data data/r51-final --out data/r51-final-up2 [--family safety_contrast_r51] [--times 2]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--family", default="safety_contrast_r51")
    ap.add_argument("--times", type=int, default=2)
    a = ap.parse_args(argv)
    src, out = Path(a.data), Path(a.out)
    if out.exists():
        sys.exit(f"{out} exists")
    if a.times < 2:
        sys.exit("--times must be at least 2")
    out.mkdir(parents=True)
    for f in src.iterdir():
        if f.is_file() and f.name not in ("train.jsonl", "train.meta.jsonl", "stats.json"):
            shutil.copy2(f, out / f.name)
    rows = [json.loads(l) for l in (src / "train.jsonl").read_text().splitlines() if l.strip()]
    metas = [json.loads(l) for l in (src / "train.meta.jsonl").read_text().splitlines() if l.strip()]
    if len(rows) != len(metas):
        sys.exit("train.jsonl and train.meta.jsonl differ in length")
    extra_r, extra_m = [], []
    for r, m in zip(rows, metas):
        rid = r.get("id") or (r.get("meta") or {}).get("id")
        if rid is not None and rid != m["id"]:
            sys.exit(f"row/meta order differs at {m['id']}")
        if m["family"] != a.family:
            continue
        for n in range(1, a.times):
            m2 = {**m, "id": f"{m['id']}-rep{n}", "repeat_of": m["id"]}
            r2 = json.loads(json.dumps(r))
            if "id" in r2:
                r2["id"] = m2["id"]
            if isinstance(r2.get("meta"), dict) and "id" in r2["meta"]:
                r2["meta"]["id"] = m2["id"]
            extra_r.append(r2)
            extra_m.append(m2)
    with (out / "train.jsonl").open("w") as f:
        for r in rows + extra_r:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (out / "train.meta.jsonl").open("w") as f:
        for m in metas + extra_m:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    stats = json.loads((src / "stats.json").read_text())
    n_valid = sum(1 for l in (src / "valid.jsonl").read_text().splitlines() if l.strip())
    stats["upsample"] = {"family": a.family, "times": a.times, "extra_train_rows": len(extra_r),
                         "source": str(src), "source_train_sha256": hashlib.sha256((src / "train.jsonl").read_bytes()).hexdigest()}
    if isinstance(stats.get("train"), int):
        stats["train"] = len(rows) + len(extra_r)
    if "examples" in stats:
        stats["examples"] = len(rows) + len(extra_r) + n_valid
    (out / "stats.json").write_text(json.dumps(stats, indent=1))
    print(f"{out}: train {len(rows)} + {len(extra_r)} repeats = {len(rows) + len(extra_r)}, valid {n_valid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
