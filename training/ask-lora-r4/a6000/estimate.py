#!/usr/bin/env python3
"""Time and cost of r4 on ONE RTX A6000, from what was measured on an A6000 and on r3's data. Pure python.

    python estimate.py                                   # the r4 planning table (5,500-6,500 text examples)
    python estimate.py --train-rows 5400 --mean-tokens 6650 --usd-per-hour 0.60
    python estimate.py --zip out/ask-lora-data.zip --tokenizer <gemma dir>   # exact token counts from the packed zip

EVERYTHING HERE IS AN ESTIMATE. The one measured anchor is r2's A6000 run (Hyperstack, SDPA, checkpointing on):
85.6 s per 8-row step at ~6.7k tokens per row = 624 tok/s, peak 21.6 GiB (ask-lora-r3/PLAN.md, "Measured basis").
Nothing in r3/r4 has run on an A6000. r3 on the MI355X node cannot be scaled to it (math attention, 8 GPUs).

What the model assumes:
  * training speed is tokens/s, flat in the row length (rows are 6.5k tokens on average, so the GPU is already full);
  * a dev eval token costs a third of a train token (forward only, no recompute; train.py's smoke uses the same rule);
  * picker generation = prefill at the forward speed + 30-55 ms per decoded token (plan.py's decode range).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---- measured / derived anchors (see the module docstring for where each comes from)
A6000_TRAIN_TOK_S = 624          # r2, A6000, SDPA + checkpointing, whole run (85.6 s per 8 rows of ~6.7k tokens)
A6000_PEAK_GIB = 21.6            # r2, same run
R3_MEAN_TEXT_TOKENS = 6549       # tokenizing r3's rendered train.jsonl (3,254 rows): prompt + completion, p50 6571, max 9158
R3_MEAN_COMPLETION_TOKENS = 41   # same file; r4's thoughts add maybe 40-60 tokens
VISION_MEAN_TOKENS = 680         # r3 vision_train.jsonl, 280 soft tokens per image counted in full (an upper bound)
AUDIO_MEAN_TOKENS = 260          # r3 audio_train.jsonl: ~90 text tokens + 25 audio tokens/s x 6.8 s mean clip
FWD_SPEEDUP = 3.0                # a forward-only token vs a train token (fwd + recompute + backward)
VISION_WEIGHT, AUDIO_WEIGHT = 0.12, 0.06      # train.py defaults, unchanged from r3
GLOBAL_BATCH = 8                 # unchanged from r3
SETUP_H = 0.75                   # venv + torch, 16 GB base download, uploads, preflight, smoke (plan.py's SETUP_H)
DECODE_S = (0.030, 0.040, 0.055)  # seconds per decoded token on the A6000, optimistic / middle / pessimistic (plan.py HW)
TRAIN_TOK_S = (700, A6000_TRAIN_TOK_S, 520)   # optimistic / measured / pessimistic (-17%: a hotter or slower host)


# ---- memory (GiB). Measured: weights 14.8 GiB (7.94B params, bf16; the r3 smoke log's `gpu_alloc`), 34,881,536 trainable LoRA
# parameters (r16 on the language model), r2's A6000 peak 21.6 GiB with SDPA, r3's MI355X peak 30.7 GiB at 9,938 tokens with MATH attention.
WEIGHTS_GIB = 14.8
LORA_STATE_GIB = 4 * 4 * 34_881_536 / 2**30     # fp32 weights + grads + two Adam moments (an upper bound: bf16 adapters halve it)
SDPA_PEAK_GIB, SDPA_PEAK_TOKENS = 21.6, 9500    # r2, A6000
MATH_PEAK_GIB, MATH_PEAK_TOKENS = 30.7, 9938    # r3, MI355X
CARD_GIB = 48.0


def peak_gib(tokens: int, attn: str = "sdpa") -> float:
    """Peak allocated GiB at the longest row. Everything above weights + LoRA state is activations: checkpointed layer inputs
    (linear in the row length) plus one layer's recompute working set; fused SDPA is linear in the length, math attention
    adds the L x L score matrices (quadratic). Scaled from the two measured peaks, so an extrapolation beyond ~10k is a guess."""
    base = WEIGHTS_GIB + LORA_STATE_GIB
    if attn == "math":
        return base + (MATH_PEAK_GIB - base) * (tokens / MATH_PEAK_TOKENS) ** 1.5
    return base + (SDPA_PEAK_GIB - base) * tokens / SDPA_PEAK_TOKENS


def pool_per_epoch(n_text: int, weight: float, total_weight: float) -> int:
    """train.pool_per_epoch with a non-empty pool: rows one multimodal pool adds to an epoch."""
    return round(weight / (1 - total_weight) * n_text) if weight > 0 else 0


def epoch_rows(n_text: int, vision_w: float = VISION_WEIGHT, audio_w: float = AUDIO_WEIGHT) -> dict:
    total_w = max(0.0, vision_w) + max(0.0, audio_w)
    if total_w >= 1:
        raise ValueError("vision + audio weights must stay below 1")
    v, a = pool_per_epoch(n_text, vision_w, total_w), pool_per_epoch(n_text, audio_w, total_w)
    return {"text": n_text, "vision": v, "audio": a, "total": n_text + v + a}


def epoch_tokens(rows: dict, mean_text: float, mean_vision: float = VISION_MEAN_TOKENS,
                 mean_audio: float = AUDIO_MEAN_TOKENS) -> float:
    return rows["text"] * mean_text + rows["vision"] * mean_vision + rows["audio"] * mean_audio


def sft_estimate(train_rows: int, mean_text: float = R3_MEAN_TEXT_TOKENS + 100, epochs: int = 2,
                 eval_every: int = 200, dev_text_rows: int = 150, dev_mm_rows: int = 130,
                 vision_w: float = VISION_WEIGHT, audio_w: float = AUDIO_WEIGHT,
                 tok_s: float = A6000_TRAIN_TOK_S, usd_h: float = 0.60, setup_h: float = SETUP_H) -> dict:
    """Hours and dollars of one SFT run: setup, epochs, the periodic dev evals (step 0 included)."""
    rows = epoch_rows(train_rows, vision_w, audio_w)
    steps = math.ceil(rows["total"] / GLOBAL_BATCH) * epochs
    tokens = epoch_tokens(rows, mean_text) * epochs
    train_h = tokens / tok_s / 3600
    n_evals = 1 + max(1, steps // eval_every)
    eval_tokens = dev_text_rows * mean_text + dev_mm_rows * (VISION_MEAN_TOKENS + AUDIO_MEAN_TOKENS) / 2
    eval_h = n_evals * eval_tokens / (tok_s * FWD_SPEEDUP) / 3600
    total = setup_h + train_h + eval_h
    return {"train_rows": train_rows, "epoch_rows": rows, "steps": steps, "tokens_m": round(tokens / 1e6, 1),
            "train_h": round(train_h, 1), "n_evals": n_evals, "eval_h": round(eval_h, 1),
            "total_h": round(total, 1), "usd": round(total * usd_h, 2), "tok_s": tok_s}


def picker_text_hours(n_steps: int, n_checkpoints: int, mean_prompt: float = R3_MEAN_TEXT_TOKENS,
                      mean_completion: float = 100, tok_s: float = A6000_TRAIN_TOK_S, decode_s: float = DECODE_S[1]) -> float:
    """pick_text.py generate: per step one prefill of the prompt plus the decoded tokens, per checkpoint."""
    per_step = mean_prompt / (tok_s * FWD_SPEEDUP) + mean_completion * decode_s
    return n_steps * n_checkpoints * per_step / 3600


def picker_mm_hours(n_checkpoints: int, rows: int = 230, secs_per_row: float = 4.0) -> float:
    """eval_mm.py: base + checkpoints on vision/reading/document/audio dev rows plus the report-only gate set, batch 1,
    greedy. A guess: r3's plan put the whole mm eval of base + 6 checkpoints at 1-2 h on an A6000."""
    return (n_checkpoints + 1) * rows * secs_per_row / 3600


def grpo_hours_plan_py(rows: int = 1500):
    """grpo/plan.py's A6000 estimate (optimistic, middle, pessimistic), read from the module, not retyped.
    None when plan.py is missing or its API has moved (another agent is adapting it for r4)."""
    import importlib.util
    for d in (HERE.parent / "grpo", HERE.parent.parent / "ask-lora-r3" / "grpo"):
        path = d / "plan.py"
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("grpo_plan", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["grpo_plan"] = mod
            spec.loader.exec_module(mod)
            return tuple(round(mod.hours("a6000", rows, 1.0, c), 1) for c in (0, 1, 2))
        except Exception:  # noqa: BLE001
            continue
    return None


def grpo_hours_calibrated(rows: int = 1500, tok_s: float = A6000_TRAIN_TOK_S, decode_s: float = DECODE_S[1],
                          G: int = 6, mix=(1265, 134, 89)) -> float:
    """The same epoch re-derived from the MEASURED SFT speed instead of plan.py's 25-45% MFU.

    Per prompt: the policy forward+backward over G completions (each repeats the prompt) at the train speed, the
    reference forward at the forward speed, generation prefill (HF repeats the prompt per completion) at the forward
    speed, and 1.8 x the mean completion in decode steps. plan.py's own prompt/completion sizes."""
    fwd = tok_s * FWD_SPEEDUP
    kinds = ((6624, 130), (722, 70), (185, 22))
    secs = []
    for p, c in kinds:
        toks = G * (p + c)
        secs.append(toks / tok_s + toks / fwd + G * p / fwd + 1.8 * c * decode_s)
    mean = sum(s * m for s, m in zip(secs, mix)) / sum(mix)
    return rows * mean / 3600


def planning_table(usd_h: float = 0.60) -> str:
    out = ["| text examples (train rows) | steps | tokens | train h | evals h | SFT total h | $ at $%.2f/h |" % usd_h,
           "|---|---|---|---|---|---|---|"]
    for total_examples in (5500, 6000, 6500):
        train = round(total_examples * 0.9)          # r3 split: 3,254 train of 3,618 examples (90%)
        e = sft_estimate(train, usd_h=usd_h)
        out.append(f"| {total_examples} ({train}) | {e['steps']} | {e['tokens_m']} M | {e['train_h']} | {e['eval_h']} "
                   f"| **{e['total_h']}** | {e['usd']:.0f} |")
    return "\n".join(out)


def tokens_from_zip(zip_path: Path, tokenizer_dir: Path) -> dict:
    """Exact mean prompt+completion tokens of the packed rows (text from train.jsonl; vision/audio text part only)."""
    import zipfile
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(str(tokenizer_dir))
    out = {}
    with zipfile.ZipFile(zip_path) as z:
        for name in ("train.jsonl", "dev.jsonl"):
            lens = []
            for line in z.read(name).decode().splitlines():
                if line.strip():
                    r = json.loads(line)
                    lens.append(len(tok(r["prompt"], add_special_tokens=False)["input_ids"])
                                + len(tok(r["completion"], add_special_tokens=False)["input_ids"]))
            lens.sort()
            out[name] = {"rows": len(lens), "mean": sum(lens) / max(1, len(lens)),
                         "p50": lens[len(lens) // 2] if lens else 0, "max": lens[-1] if lens else 0}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train-rows", type=int, help="text TRAIN rows (default: the planning table for 5,500-6,500 examples)")
    ap.add_argument("--mean-tokens", type=float, default=R3_MEAN_TEXT_TOKENS + 100)
    ap.add_argument("--zip", help="packed ask-lora-data.zip: take row counts and token means from it")
    ap.add_argument("--tokenizer", help="Gemma tokenizer dir, needed with --zip")
    ap.add_argument("--usd-per-hour", type=float, default=0.60, help="r2 paid $0.60/h on Hyperstack; confirm the real rate")
    ap.add_argument("--eval-every", type=int, default=200)
    ap.add_argument("--dev-text-rows", type=int, default=150)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    train_rows, mean = a.train_rows, a.mean_tokens
    if a.zip:
        if not a.tokenizer:
            ap.error("--zip needs --tokenizer")
        t = tokens_from_zip(Path(a.zip), Path(a.tokenizer))
        train_rows, mean = t["train.jsonl"]["rows"], t["train.jsonl"]["mean"]
        print("packed rows:", json.dumps(t))
    res = {}
    if train_rows:
        res["sft"] = {n: sft_estimate(train_rows, mean, eval_every=a.eval_every, dev_text_rows=a.dev_text_rows, tok_s=s,
                                      usd_h=a.usd_per_hour)
                      for n, s in zip(("optimistic", "measured", "pessimistic"), TRAIN_TOK_S)}
    res["picker_text_h_per_360_steps_per_ckpt"] = round(picker_text_hours(360, 1), 2)
    res["grpo_plan_py_h"] = grpo_hours_plan_py()
    res["grpo_calibrated_h"] = round(grpo_hours_calibrated(), 1)
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    if train_rows:
        for n, e in res["sft"].items():
            print(f"SFT {n:11s} {e['tok_s']} tok/s: {e['steps']} steps, {e['tokens_m']} M tokens, train {e['train_h']} h + "
                  f"{e['n_evals']} evals {e['eval_h']} h + setup = {e['total_h']} h = ${e['usd']:.0f}")
    else:
        print(planning_table(a.usd_per_hour))
    print(f"text picker: {res['picker_text_h_per_360_steps_per_ckpt']} h per checkpoint per 360 dev steps")
    print(f"GRPO epoch (1,500 prompts): plan.py {res['grpo_plan_py_h']} h; re-derived from the measured SFT speed {res['grpo_calibrated_h']} h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
