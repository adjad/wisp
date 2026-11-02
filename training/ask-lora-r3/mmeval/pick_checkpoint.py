#!/usr/bin/env python
"""Pick the LoRA checkpoint to merge: the lowest text dev loss among those whose multimodal metrics stay
within tolerance of the untuned base. Exit 0 and print the name (and path) when one qualifies, exit 2 when
none does, so a pipeline stops instead of shipping a regression.

    python pick_checkpoint.py --results RUN/results.json [--ckpt-root OUT] [--strict] [--tol vision_facts=0.01 ...]

Tolerances (defaults, in scoring.CHECKS): vision facts >= base-0.01, proposal F1 >= base-0.02, proposal
validity >= base-0.02, abstain >= base-0.02, empty replies <= base, no new injection leak (vision or audio),
audio WER (normalised) <= base+0.01, capitalised+punctuated >= base-0.02, no-speech empty >= base.
Reading (the READ call behind "pictures that lead to actions", rows of kind picture_read): exact TEXT:/DESCRIPTION:
format >= base-0.01, text recall of the visible lines >= base-0.01, empty replies <= base, and no new obeyed
injection (zero allowed); --require-reading refuses a run whose dev set has no reading rows.
A check whose metric is missing on either side (e.g. no audio rows) is skipped with a warning; --strict makes
a skipped check disqualify, which is the right setting once audio_rows.jsonl exists.
The text dev loss comes from train.log.jsonl (dev_loss_text, else dev_loss) at the checkpoint's step; a
checkpoint whose step has no logged eval cannot be ranked and is not picked.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

EVAL_EVENTS = {"eval", "stopped_at_deadline"}


def read_log(path) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def run_root(ckpt_root: Path) -> Path:
    """The training run dir (holds train.log.jsonl): ckpt_root itself, or its parent when it is OUT/ckpt."""
    if not (ckpt_root / "train.log.jsonl").is_file() and ckpt_root.name == "ckpt" \
            and (ckpt_root.parent / "train.log.jsonl").is_file():
        return ckpt_root.parent
    return ckpt_root


def adapter_dirs(ckpt_root: Path) -> dict[str, Path]:
    """{checkpoint name: adapter dir}. Scans OUT (step-* last three, best, final) and OUT/ckpt (the never-pruned
    --save-every snapshots); a step present in both resolves to the snapshot (adapter only, same weights)."""
    found: dict[str, Path] = {}
    for base in (ckpt_root, ckpt_root / "ckpt"):
        if base.is_dir():
            for p in sorted(base.iterdir()):
                if p.is_dir() and (p / "adapter_config.json").is_file():
                    found[p.name] = p
    return found


def _step_of(name: str) -> int | None:
    m = re.fullmatch(r"step-(\d+)", name)
    return int(m.group(1)) if m else None


def checkpoint_losses(train_log, names, ckpt_root=None) -> dict[str, dict]:
    """{checkpoint name: {"step", "dev_loss_text", "source"}}. 'best' resolves through the log's own
    `best` field ([dir name, loss]), 'final' to the last step trained; 'base' is not a checkpoint."""
    recs = read_log(train_log)
    evals = {r["step"]: r for r in recs if r.get("event") in EVAL_EVENTS and "dev_loss" in r and "step" in r}
    last_step = max([r["step"] for r in recs if "step" in r and "of" in r] or [0])
    best = None
    for r in recs:
        if isinstance(r.get("best"), list) and r["best"]:
            best = r["best"]
    out = {}
    for name in names:
        if name == "base":
            continue
        step = _step_of(name)
        if name == "best" and best:
            step = _step_of(str(best[0]))
        elif name == "final":
            step = last_step or None
        rec = evals.get(step) if step is not None else None
        if rec is None:
            out[name] = {"step": step, "dev_loss_text": None, "source": None}
        else:
            key = "dev_loss_text" if "dev_loss_text" in rec else "dev_loss"
            out[name] = {"step": step, "dev_loss_text": rec[key], "source": key}
    return out


def pick(results: dict, tol: dict | None = None, strict: bool = False, require_reading: bool = False):
    """(chosen name or None, table of {name, loss, ok, failed, skipped, reason})."""
    import scoring as S
    table = []
    for name, c in results["checkpoints"].items():
        verdict = S.compare(results["base"], c["metrics"], tol) if tol else c["verdict"]
        loss = c.get("dev_loss_text")
        reason = ""
        ok = verdict["ok"]
        if not ok:
            reason = "fails " + ", ".join(verdict["failed"])
        elif require_reading and not (results["base"].get("reading") or {}).get("n"):
            ok, reason = False, "no reading (picture_read) dev rows, under --require-reading"
        elif strict and verdict.get("no_data"):
            ok, reason = False, "no data for, under --strict: " + ", ".join(verdict["no_data"])
        elif loss is None:
            ok, reason = False, "no text dev loss logged for its step"
        table.append({"name": name, "loss": loss, "ok": ok, "failed": verdict["failed"],
                      "skipped": verdict["skipped"], "reason": reason, "step": c.get("step")})
    ranked = sorted((t for t in table if t["ok"]), key=lambda t: (t["loss"], t["step"] if t["step"] is not None else 1 << 30))
    return (ranked[0]["name"] if ranked else None), table


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="results.json from eval_mm.py / score_mm.py")
    ap.add_argument("--ckpt-root", help="adapter run dir; adds the chosen checkpoint's path to the output")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--require-reading", action="store_true",
                    help="refuse a run with no picture_read dev rows (the reading checks are otherwise skipped)")
    ap.add_argument("--tol", action="append", metavar="CHECK=VALUE", help="re-judge with different tolerances")
    ap.add_argument("--json", action="store_true", help="print the decision as JSON on stdout")
    a = ap.parse_args()
    import scoring as S
    tol = {}
    for it in a.tol or []:
        k, _, v = it.partition("=")
        if k not in S.DEFAULT_TOL:
            print(f"--tol {k}: unknown check; one of {sorted(S.DEFAULT_TOL)}", file=sys.stderr)
            return 3
        tol[k] = float(v)
    results = json.loads(Path(a.results).read_text())
    chosen, table = pick(results, tol or None, a.strict, a.require_reading)
    for t in sorted(table, key=lambda t: (t["loss"] is None, t["loss"] or 0)):
        loss = "-" if t["loss"] is None else f"{t['loss']:.4f}"
        print(f"{'PICK' if t['name'] == chosen else ('ok  ' if t['ok'] else 'FAIL')} {t['name']:<14} text dev loss {loss}"
              f"  {t['reason']}", file=sys.stderr)
        if t["skipped"] and not a.strict:
            print(f"     (skipped: {', '.join(t['skipped'])})", file=sys.stderr)
    if chosen is None:
        print("NO CHECKPOINT QUALIFIES: every candidate regresses vision or audio against the base "
              "(or cannot be ranked). Do not merge; see the table above and report.md.", file=sys.stderr)
        if a.json:
            print(json.dumps({"chosen": None, "table": table}))
        return 2
    path = str(adapter_dirs(Path(a.ckpt_root)).get(chosen, Path(a.ckpt_root) / chosen)) if a.ckpt_root else None
    if a.json:
        print(json.dumps({"chosen": chosen, "path": path, "table": table}))
    else:
        print(path or chosen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
