#!/usr/bin/env python3
"""Merge the harvested-simulation SFT set with the real-usage SFT set into
one train/valid/test split, dropping known-contaminated examples.

Contamination: a harvest flow's own approver (SelfTargetApprover) can deny a
call the MODEL got right (e.g. `mail_draft_then_send`'s expect only listed
`summarize_emails`, so a correctly self-targeted `send_email` in the same
turn was denied by the harness, not by policy) — any example whose messages
contain the resulting "The user denied this action." tool result is an
artifact of the harness's grading, not organic model behavior, and is
dropped rather than trained on.

Usage:
    python scripts/merge_lora_data.py --harvest ~/lora_data --real-usage ~/lora_data_real_usage --out ~/lora_data_merged
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def is_contaminated(example: dict) -> bool:
    return any("denied this action" in (m.get("content") or "") for m in example["messages"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harvest", default="~/lora_data")
    ap.add_argument("--real-usage", default="~/lora_data_real_usage")
    ap.add_argument("--out", default="~/lora_data_merged")
    ap.add_argument("--val-frac", type=float, default=0.1)
    args = ap.parse_args()

    harvest_dir = Path(args.harvest).expanduser()
    real_dir = Path(args.real_usage).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    harvest = load_jsonl(harvest_dir / "harvest_sft.jsonl")
    # NOT test.jsonl — build_lora_dataset.py's test.jsonl is a copy of a
    # slice of valid.jsonl (mlx_lm.lora requires the file to exist even when
    # --test isn't passed), so including it would double-count those examples.
    real = load_jsonl(real_dir / "train.jsonl") + load_jsonl(real_dir / "valid.jsonl")

    n_harvest_raw = len(harvest)
    harvest = [e for e in harvest if not is_contaminated(e)]
    dropped = n_harvest_raw - len(harvest)

    examples = harvest + real
    n_val = max(1, int(len(examples) * args.val_frac))
    val, train = examples[:n_val], examples[n_val:]

    (out_dir / "train.jsonl").write_text("\n".join(json.dumps(e) for e in train) + "\n")
    (out_dir / "valid.jsonl").write_text("\n".join(json.dumps(e) for e in val) + "\n")
    (out_dir / "test.jsonl").write_text(
        "\n".join(json.dumps(e) for e in val[: max(1, n_val // 2)]) + "\n")

    print(f"harvest: {len(harvest)} ({dropped} dropped as contaminated)")
    print(f"real usage: {len(real)}")
    print(f"total: {len(examples)} -> {len(train)} train / {len(val)} valid, written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
