"""Reload the pilot adapter through mlx_lm.load(adapter_path=...) and greedily
generate for a few dev prompts, next to the target, to show the adapter loads and runs.

    python gen_check.py --model <dir> --adapter <out>/adapter --data <dir> [--n 3]
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from mlx_lm import generate, load
from mlx_lm.sample_utils import make_sampler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--n", type=int, default=3)
    a = ap.parse_args()
    model, tok = load(a.model, adapter_path=a.adapter)
    rows = [json.loads(l) for l in (Path(a.data) / "dev.jsonl").read_text().splitlines() if l.strip()]
    rng = random.Random(1)
    picks = rng.sample(rows, a.n)
    for r in picks:
        ids = tok.encode(r["prompt"], add_special_tokens=False)
        text = generate(model, tok, prompt=ids, max_tokens=120, sampler=make_sampler(temp=0.0), verbose=False)
        print(json.dumps({"id": r["id"], "kind": r["kind"], "want": r["completion"][:240], "got": text[:240]},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
