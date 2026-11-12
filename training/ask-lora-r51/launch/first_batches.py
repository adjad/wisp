#!/usr/bin/env python3
"""Offline: the exact rows the first N optimizer steps of an r5.1 run would see. No model, no torch, no training.

Uses train.py's own functions (tokenize via load_all, pool_sizes, epoch_order_pools) on the unpacked ask-lora-data.zip,
so the order is the trainer's, given the same tokenizer, --max-len, seed, weights and global batch.

    python first_batches.py --data UNZIPPED_ZIP --base BF16_BASE_DIR --meta r51-final/train.meta.jsonl \
        --labels ../audit/labels.jsonl [--steps 50] [--max-len 11264] [--json OUT.json]
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import sys
from pathlib import Path

NEW = "safety_contrast_r51"
# coverage buckets the user asked about, by intent label
BUCKETS = {
    "send_or_reply": {"direct_send", "reply", "schedule_send"},
    "draft": {"draft"},
    "read_only_or_leave_alone": {"read_only", "leave_alone"},
    "clarify": {"clarify"},
    "refused": {"refused"},
    "embedded_instruction": {"embedded_instruction"},
    "authorized_write_other": {"authorized_write"},
}


def load_train_py(data: Path):
    spec = importlib.util.spec_from_file_location("train_r51", data / "train.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["train_r51"] = m
    spec.loader.exec_module(m)
    return m


def r5_intent(meta: dict, labels: dict) -> str:
    """r5 replay rows: the audit's intent for write targets; otherwise a coarse label from the row's own fields."""
    if meta["id"] in labels:
        return labels[meta["id"]]
    if meta.get("clarify"):
        return "clarify"
    if not meta.get("turn_writes"):
        return "no_write_turn"           # reads, chat, restraint and refusals in r5's own families
    return "step_in_write_turn"          # a read or lookup step before the write, or the final after it


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--steps", type=int, default=50)
    ap.add_argument("--max-len", type=int, default=11264)
    ap.add_argument("--global-batch", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20261029)
    ap.add_argument("--vision-weight", type=float, default=0.12)
    ap.add_argument("--audio-weight", type=float, default=0.06)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    data = Path(a.data)
    T = load_train_py(data)
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    ns = argparse.Namespace(data=str(data), base=a.base, max_len=a.max_len, seed=a.seed, dev_text_max=150,
                            vision_weight=a.vision_weight, audio_weight=a.audio_weight, audio_serving_frac=T.AUDIO_SERVING_FRAC)
    train, _dev, counts, _mm = T.load_all(ns, tok)
    n_text, n_vis, n_aud = counts["text"], counts["vision"], counts["audio"]
    pools = [(n_vis, a.vision_weight), (n_aud, a.audio_weight)]
    sizes = T.pool_sizes(n_text, pools)
    per_epoch = n_text + sum(sizes)
    spe = math.ceil(per_epoch / a.global_batch)
    order = T.epoch_order_pools(n_text, pools, a.seed, 0)

    metas = {}
    for l in open(a.meta):
        m = json.loads(l)
        metas[m["id"]] = m
    labels = {}
    for l in open(a.labels):
        r = json.loads(l)
        labels[r["id"]] = r["intent"]

    steps = []
    for s in range(a.steps):
        rows = []
        for i in order[s * a.global_batch:(s + 1) * a.global_batch]:
            rid, p, c, info = train[i]
            if i >= n_text:
                rows.append({"id": rid, "src": "vision" if i < n_text + n_vis else "audio", "tokens": len(p) + len(c)})
                continue
            m = metas[rid]
            if m["family"] == NEW:
                g = m.get("gold") or {}
                rows.append({"id": rid, "src": "new", "intent": g.get("intent"), "group": g.get("group"),
                             "kind": m["target_kind"], "category": g.get("category"), "tokens": len(p) + len(c)})
            else:
                rows.append({"id": rid, "src": "r5", "family": m["family"], "intent": r5_intent(m, labels),
                             "kind": m["target_kind"], "tokens": len(p) + len(c)})
        steps.append(rows)

    flat = [r for st in steps for r in st]
    src = collections.Counter(r["src"] for r in flat)
    new = [r for r in flat if r["src"] == "new"]
    r5 = [r for r in flat if r["src"] == "r5"]
    out = {
        "counts": counts, "pool_sizes": sizes, "rows_per_epoch": per_epoch, "steps_per_epoch": spe,
        "total_steps_2_epochs": 2 * spe, "rows_in_first_steps": len(flat), "by_source": dict(src),
        "new_by_intent": dict(collections.Counter(r["intent"] for r in new).most_common()),
        "new_by_kind": dict(collections.Counter(r["kind"] for r in new)),
        "new_groups": len({r["group"] for r in new}),
        "new_groups_by_intent": {k: len({r["group"] for r in new if r["intent"] == k})
                                 for k in sorted({r["intent"] for r in new})},
        "r5_by_intent": dict(collections.Counter(r["intent"] for r in r5).most_common()),
        "r5_by_family_top": dict(collections.Counter(r["family"] for r in r5).most_common(15)),
        "new_family_share_of_text_train": round(sum(1 for m in metas.values() if m["family"] == NEW) / n_text, 4),
    }
    cov = {}
    for b, intents in BUCKETS.items():
        hit = [s + 1 for s, st in enumerate(steps) if any(r.get("intent") in intents for r in st)]
        hit_new = [s + 1 for s, st in enumerate(steps) if any(r["src"] == "new" and r.get("intent") in intents for r in st)]
        cov[b] = {"rows_all": sum(1 for r in flat if r.get("intent") in intents),
                  "rows_new": sum(1 for r in new if r.get("intent") in intents),
                  "steps_any": hit, "steps_new": hit_new}
    out["coverage"] = cov
    out["steps"] = steps
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "steps"}, indent=1))
    for s, st in enumerate(steps):
        cells = []
        for r in st:
            if r["src"] == "new":
                cells.append(f"NEW:{r['intent']}/{r['kind']}")
            elif r["src"] == "r5":
                cells.append(f"r5:{r['intent']}")
            else:
                cells.append(r["src"])
        print(f"step {s + 1:2d}: " + ", ".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
