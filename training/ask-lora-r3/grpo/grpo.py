#!/usr/bin/env python3
"""Single-step (bandit) GRPO for Wisp's Ask model, after the r3 SFT merge, on one or many GPUs. GPU box only.

    accelerate launch --config_file accelerate_b300x8_zero2.yaml grpo.py train --profile b300x8 --base DIR \\
        --prompts grpo_prompts.jsonl --out DIR [--mm-root mm] [--resume DIR]
    accelerate launch --config_file accelerate_a6000.yaml        grpo.py train --profile a6000  ...
    python grpo.py check   --base DIR --prompts grpo_prompts.jsonl [--profile P] [--train-py PATH]
    python grpo.py mm-eval --base DIR --mm-dev grpo_mm_dev.jsonl [--adapter DIR] [--mm-root mm] [--out mm-untuned.json]
    python grpo.py text-eval --base DIR --text-dev grpo_text_dev.jsonl --round 5 --seed-offset N --out text-sft-1.json [--adapter DIR]
    python grpo.py train ... --round 5 --text-dev grpo_text_dev.jsonl --text-floor text-untuned.json --text-replicates text-sft-1.json text-sft-2.json
    python grpo.py train ... --dry-run-grad          # build the model and LoRA, check the layout, one backward, assert every adapter has a gradient
    python grpo.py merge   --base DIR --adapter DIR --data DIR --out DIR [--train-py PATH]

--base is the MERGED r3 SFT model (bf16 folder from `train.py merge`). --train-py is ../brev/train.py: its
`_model_class`, `_base_keys`, `_restore_kv_shared` and the merge sanity helpers are imported, not copied.

This file is a small self-contained GRPO loop (torch + transformers + peft + accelerate; NO TRL), because
the job needs three things TRL does not give for Gemma 4: audio and image prompts in the same batch, a
different KL strength per modality (with an optional second reference model), and a held-out multimodal
tripwire inside the run. Everything it does:

  rollout  per optimizer step each GPU takes `ppr` prompts and samples G completions of each with HF
           generate (production sampling for text: T 1.0, top_p 0.95, top_k 64; thinking stays whatever
           the rendered prompt says). Generation is data-parallel: no vLLM (Gemma 4 has none here), no
           cross-GPU traffic; a prompt's G completions live on one GPU so its group baseline is local.
  reward   reward.py on the decoded completion (special tokens kept): text steps (tools, facts, voice),
           vision (facts, proposal, banned payloads, empty text), audio (1 - WER, punctuation, no-speech).
  loss     advantage = (r - mean) / max(std, floor) inside the group; per-token loss
           -ratio*A + beta*KL(k3) vs the reference; ratio is 1 (one update per rollout). beta 0.04 on text
           rows, beta_mm (default 0.2) on vision/audio rows. The reference is the same model with the
           adapter disabled (= r3 SFT, no extra memory), or for multimodal rows optionally a second frozen
           copy (--mm-ref base:DIR, e.g. the untuned Gemma) so those prompts are anchored to it.
  guard    before step 1 and every --eval-every steps the held-out multimodal dev rows (never trained on)
           are decoded greedily with the production request settings; a drop vs the step-0 numbers
           (plan.guard_problems) twice in a row stops the run and names the last good checkpoint. With --mm-floor
           (untuned-Gemma numbers from `mm-eval`) a checkpoint worse than the UNTUNED model also trips it, and a
           start already below it exits (an SFT that regressed cannot become the baseline).

Round 5 (RL-SCOPE, `--round 5`) adds, all behind that flag: `text-eval` (the dev rows decoded once, per-row records, for the
untuned floor and the SFT replicates); text probes at step 0, every --eval-every steps and the last step (S-unsafe, S-text,
S-session, S-think, S-turn, S-chat, rules and their family-wise calibration in probes.py; S-trunc, S-learn and S-grader from the
train log); the small-n multimodal counts in the guard; per-step logs (rows, rl_kind, ceiling, zero-variance and informative
shares, |A|, PG:KL gradient norms, grad norm); `--dry-run-grad`; and a torch-free launch check. Rounds 3 and 4 run as before.

Vision and audio towers stay frozen (LoRA on language q/k/v/o and MLP only); the guard exists because that
does not by itself stop the language model drifting on what it says about a picture or a clip.

Nothing here has run on a GPU. UNVERIFIED until `check` and a 5-step smoke pass on the box: the
processor call signature for audio, `generate`/forward with Gemma 4 multimodal tensors, DeepSpeed on
Blackwell, memory and speed. README.md lists each.
"""
from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import argparse
import collections
import contextlib
import hashlib
import importlib.util
import inspect
import json
import math
import random
import sys
import time
import types
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plan as P  # noqa: E402
import probes as PR  # noqa: E402
import reward as W  # noqa: E402

TURN_END, TOOL_RESPONSE, EOS = 106, 50, 1      # <turn|>, <|tool_response>, <eos>
STOP_IDS = [TURN_END, TOOL_RESPONSE]           # a reply ends in <turn|>, a tool call in <|tool_response>
TARGET_RE = r".*language_model.*\.(self_attn\.(q_proj|k_proj|v_proj|o_proj)|mlp\.(gate_proj|up_proj|down_proj))"
N_LAYERS = 42
MM_KEYS = ("vision", "audio")
GROUPS = list(W.COMPONENTS)


# ------------------------------------------------------------------ helpers (no torch)

def import_train(path: str | None):
    cands = [Path(path)] if path else [HERE / "train.py", HERE.parent / "brev" / "train.py",
                                       Path.home() / "wisp" / "train.py"]
    for p in cands:
        if p.exists():
            spec = importlib.util.spec_from_file_location("ask_lora_train", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.exit(f"train.py not found (tried {[str(c) for c in cands]}); pass --train-py")


def load_prompts(path: str, limit: int = 0) -> list[dict]:
    rows = [json.loads(l) for l in open(path) if l.strip()]
    for r in rows:
        lab = json.loads(r["label"])               # fails loudly on a bad label
        r.setdefault("modality", lab.get("modality", "text"))
    return rows[:limit] if limit else rows


def load_wav(path: Path):
    """16 kHz mono 16-bit PCM -> float32 numpy in [-1, 1] (what the processor's feature extractor takes)."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f"{path}: need 16 kHz mono 16-bit PCM, got {w.getparams()}")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768.0


def check_media(rows: list[dict], mm_root: Path) -> list[str]:
    bad = []
    for r in rows:
        for rel in (r.get("images") or []) + ([r["audio"]] if r.get("audio") else []):
            if not (mm_root / rel).is_file():
                bad.append(f"{r['id']}: {mm_root / rel}")
    return bad


def metric_keys() -> list[str]:
    keys = []
    for m in ("text", "vision", "audio"):
        keys += [f"n/{m}", f"r/{m}", f"kl/{m}", f"tok/{m}", f"bad/{m}", f"len/{m}"]
        keys += [f"g/{m}/{g}" for g in GROUPS]
    return keys


def label_rounds(path) -> set:
    """The set of label.round values in a prompts-style jsonl (None = unstamped, i.e. an r3 export)."""
    seen = set()
    if path and Path(path).exists():
        with open(path) as f:
            for line in f:
                if line.strip():
                    seen.add(json.loads(json.loads(line)["label"]).get("round"))
    return seen


def mm_dev_path(a) -> Path:
    return Path(a.mm_dev) if a.mm_dev else Path(a.prompts).with_name("grpo_mm_dev.jsonl")


def mm_dev_rows_exist(a) -> bool:
    p = mm_dev_path(a)
    return p.exists() and any(l.strip() for l in open(p))


def check_train_args(a, dev_rows_exist: bool, rnd: int | None = None) -> None:
    """Torch-free launch check (RL-SCOPE C13), called from main() before cmd_train so a bad command line fails before torch,
    accelerate and the model load: --mm-floor is mandatory when multimodal dev rows exist (unless --no-guard), and round 5
    needs its text-probe files. Exits with every problem listed."""
    problems = P.train_arg_problems(a, dev_rows_exist, W.ROUND if rnd is None else rnd)
    if problems:
        sys.exit("train: " + "\n       ".join(problems))


def read_config(base: str) -> dict:
    p = Path(base) / "config.json"
    return json.loads(p.read_text()) if p.exists() else {}


def text_probe_inputs(a, rnd: int) -> dict:
    """Load and cross-check everything the round-5 text probes need before any GPU work: the dev file, the untuned base's
    floor and the SFT replicates (all `text-eval` outputs of the same round). A dev file smaller than the replicates' (the
    smoke30 subset of a bracket arm) is matched row by row: the first n samples of each of its rows, and only single-eval HARD
    count thresholds are used. A full-size dev file must match the replicates exactly (same ids, same per-row sample counts)."""
    dev = PR.load_text_dev(a.text_dev)
    bad = [d["id"] for d in dev if d["lab"].get("round") != rnd]
    if bad:
        sys.exit(f"{a.text_dev}: rows {bad[:3]} are not stamped round {rnd}")
    floor = PR.load_probe_file(a.text_floor, rnd)
    reps = [PR.load_probe_file(p, rnd) for p in a.text_replicates]
    full = len(reps[0]["rows"])
    single = bool(getattr(a, "probe_single_eval", False)) or len(dev) < full
    try:
        if single:
            reps_rows = [PR.restrict_rows(r["rows"], dev) for r in reps]
            base_rows = PR.restrict_rows(floor["rows"], dev)
        else:
            reps_rows = [r["rows"] for r in reps]
            base_rows = floor["rows"]
            for what, rows in [("--text-floor", base_rows)] + [(f"--text-replicates {p}", r) for p, r in zip(a.text_replicates, reps_rows)]:
                PR.check_rows_match(rows, [PR.row_record(d, [None] * d["n"]) for d in dev], what)
    except ValueError as exc:
        sys.exit(f"text probes: {exc}")
    sha = PR.dev_sha256(a.text_dev)
    if not single:
        for what, d in [("--text-floor", floor)] + list(zip(a.text_replicates, reps)):
            if d.get("dev_sha256") and d["dev_sha256"] != sha:
                sys.exit(f"{what} was scored on a different dev file (sha256 differs from {a.text_dev})")
    return {"dev": dev, "base_rows": base_rows, "reps_rows": reps_rows, "single": single, "dev_sha256": sha,
            "extras_reps": [r.get("extras") or {} for r in reps]}


# ------------------------------------------------------------------ model plumbing (torch)

def load_policy(tr, base: str, device_index: int):
    """The multimodal class with the towers, bf16, on THIS rank's GPU. (train.load_model pins device 0, which
    would put every rank on one card.)"""
    import torch
    attn = {"": "sdpa", "text_config": "sdpa", "vision_config": "sdpa", "audio_config": "eager"}
    model = tr._model_class().from_pretrained(base, dtype=torch.bfloat16, attn_implementation=attn,
                                              device_map={"": device_index})
    return model


def install_forward_defaults(model, torch):
    """Zero modality masks for text-only forwards if this transformers' forward asks for them (train.py does the same)."""
    params = inspect.signature(model.forward).parameters
    extra = [k for k in ("token_type_ids", "mm_token_type_ids") if k in params]
    if extra:
        orig = model.forward

        def forward(*a, **k):
            ids = k.get("input_ids")
            if ids is not None:
                for key in extra:
                    k.setdefault(key, torch.zeros_like(ids))
            return orig(*a, **k)
        model.forward = forward
    return extra


def build_inputs(row: dict, tok, proc, mm_root: Path) -> dict:
    """CPU tensors for one prompt. Text rows: token ids. Vision/audio rows: the Gemma processor expands the
    `<|image|>` / `<|audio|>` placeholders itself and returns pixel_values, image_position_ids, input_features,
    input_features_mask, mm_token_type_ids as the model wants them."""
    mod = row.get("modality", "text")
    if mod == "text":
        return {"input_ids": tok(row["prompt"], add_special_tokens=False, return_tensors="pt")["input_ids"]}
    kw = {"text": row["prompt"], "return_tensors": "pt", "add_special_tokens": False}
    if row.get("images"):
        from PIL import Image
        kw["images"] = []
        for rel in row["images"]:
            with Image.open(mm_root / rel) as im:
                kw["images"].append(im.convert("RGB"))
    if row.get("audio"):
        kw[audio_kwarg(proc)] = [load_wav(mm_root / row["audio"])]
    enc = proc(**kw)
    out = {k: v for k, v in enc.items() if k != "attention_mask"}
    if out["input_ids"].shape[0] != 1:
        raise ValueError(f"{row['id']}: processor returned a batch of {out['input_ids'].shape[0]}")
    return out


def audio_kwarg(proc) -> str:
    names = inspect.signature(proc.__call__).parameters
    for k in ("audio", "audios"):
        if k in names:
            return k
    return "audio"


def tile(inputs: dict, n: int, device, dtype):
    """Repeat every tensor n times along dim 0 in row-major order (image 0..k, image 0..k, ...), which is how the
    model walks image/audio tokens across a batch of identical prompts. Floats become the model dtype."""
    out = {}
    for k, v in inputs.items():
        if k == "input_ids":
            continue
        v = v.repeat(n, *([1] * (v.dim() - 1))).to(device)
        out[k] = v.to(dtype) if v.is_floating_point() else v
    return out


def rollout(model, tok, inputs: dict, G: int, max_new: int, sample: dict | None, device, dtype):
    """G completions of one prompt as lists of token ids, each cut after its first stop id. sample=None is greedy."""
    import torch
    ids = inputs["input_ids"].to(device).repeat(G, 1)
    kw = tile(inputs, G, device, dtype)
    gen = dict(max_new_tokens=max_new, eos_token_id=STOP_IDS, pad_token_id=tok.pad_token_id, use_cache=True)
    if sample is None:
        gen.update(do_sample=False)
    else:
        gen.update(do_sample=True, temperature=sample["temperature"], top_p=sample["top_p"], top_k=sample["top_k"])
    with torch.no_grad():
        out = model.generate(input_ids=ids, attention_mask=torch.ones_like(ids), **kw, **gen)
    comps = []
    for row in out[:, ids.shape[1]:].tolist():
        cut = next((i for i, t in enumerate(row) if t in STOP_IDS), None)
        comps.append(row[:cut + 1] if cut is not None else row)
    return comps


def completion_logprobs(model, inputs: dict, comps: list[list[int]], temperature: float, pad_id: int, device, dtype):
    """Per-token log-probs (B x L, fp32) of each completion under `model`, at sampling temperature, and the
    validity mask. Right-padded; the prompt is shared so the completion starts at the same column in every row."""
    import torch
    prompt = inputs["input_ids"].to(device)
    B, Pn, L = len(comps), prompt.shape[1], max(len(c) for c in comps)
    ids = torch.full((B, Pn + L), pad_id, dtype=torch.long, device=device)
    mask = torch.zeros((B, L), dtype=torch.bool, device=device)
    ids[:, :Pn] = prompt
    for b, c in enumerate(comps):
        ids[b, Pn:Pn + len(c)] = torch.tensor(c, device=device)
        mask[b, :len(c)] = True
    attn = torch.cat([torch.ones((B, Pn), dtype=torch.long, device=device), mask.long()], dim=1)
    kw = tile(inputs, B, device, dtype)
    if "mm_token_type_ids" in kw:                      # padded columns after the prompt are text
        kw["mm_token_type_ids"] = torch.cat([kw["mm_token_type_ids"], kw["mm_token_type_ids"].new_zeros((B, L))], dim=1)
    out = model(input_ids=ids, attention_mask=attn, logits_to_keep=L + 1, use_cache=False, **kw)
    logits = out.logits[:, -(L + 1):-1].float() / temperature        # positions Pn-1 .. Pn+L-2 predict the L tokens
    tgt = ids[:, Pn:]
    lp = logits.gather(-1, tgt.unsqueeze(-1)).squeeze(-1) - torch.logsumexp(logits, dim=-1)
    return lp, mask


def decode_all(tok, comps):
    return [tok.decode(c, skip_special_tokens=False) for c in comps]


# ------------------------------------------------------------------ the text probes (round 5, C10)

def run_text_probe(accelerator, model, tok, dev: list[dict], sample: dict, eval_seed: int, max_default: int, device, dtype,
                   ref_fn=None) -> tuple[list[dict], dict]:
    """Decode every text dev row at production sampling and score it with the row's own stamped round (probes.score_sample).

    Each row draws `dev[i]["n"]` samples (the dev file's per-row count), with the torch seed keyed on (row id, eval seed), so a
    row's draws do not depend on which rank decodes it, and the surrounding training RNG is restored afterwards. Rows are
    split over the ranks and gathered in dev-file order. With `ref_fn(inp, comps, temperature) -> ref log-probs` the chat
    rows' per-token k3 against the reference is measured too (S-chat KL). Returns (row records, extras)."""
    import torch
    from accelerate.utils import gather_object
    was_training = model.training
    model.eval()
    devices = ([device.index if device.index is not None else torch.cuda.current_device()] if device.type == "cuda" else [])
    mine, kl_sum, kl_tok = [], 0.0, 0.0
    with torch.random.fork_rng(devices=devices):
        for d in dev[accelerator.process_index::accelerator.num_processes]:
            torch.manual_seed(PR.row_seed(d["id"], eval_seed))
            inp = build_inputs(d["row"], tok, None, Path("."))
            comps = rollout(model, tok, inp, d["n"], d["lab"].get("max_tokens") or max_default, sample, device, dtype)
            texts = decode_all(tok, comps)
            mine.append(PR.row_record(d, [PR.sample_record(t, d["lab"], len(c), P.truncated(c, STOP_IDS))
                                          for t, c in zip(texts, comps)]))
            if ref_fn is not None and d["slice"] == "chat":
                with torch.no_grad():
                    lp, mask = completion_logprobs(model, inp, comps, sample["temperature"], tok.pad_token_id, device, dtype)
                    ref_lp = ref_fn(inp, comps, sample["temperature"])
                    x = ref_lp - lp
                    kl_sum += float(((torch.exp(x) - x - 1.0) * mask).sum())
                    kl_tok += float(mask.sum())
    if was_training:
        model.train()
    order = {d["id"]: i for i, d in enumerate(dev)}
    rows = sorted(gather_object(mine), key=lambda r: order[r["id"]])
    extras = {}
    if ref_fn is not None:
        tot = accelerator.reduce(torch.tensor([kl_sum, kl_tok], device=device, dtype=torch.float64), "sum").tolist()
        if tot[1] > 0:
            extras = {"chat_k3": tot[0] / tot[1], "chat_k3_tokens": tot[1]}
    return rows, extras


def cmd_text_eval(a) -> int:
    """Decode the text dev file once (any base, optionally with an adapter) and write the per-row records and the metrics:
    the untuned floor (no adapter, the untuned base) and the SFT replicates (two runs, different --seed-offset) of RL-SCOPE 8.2.
    Scored with W.score like eval_mm, so S-indep (the toolbench scorer) is the only independent scorer."""
    import torch
    from accelerate import Accelerator
    from peft import PeftModel
    from transformers import AutoTokenizer
    accelerator = Accelerator()
    tr = import_train(a.train_py)
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    model = load_policy(tr, a.base, accelerator.local_process_index)
    install_forward_defaults(model, torch)
    if a.adapter:
        model = PeftModel.from_pretrained(model, a.adapter)
    model.eval()
    dev = PR.load_text_dev(a.text_dev)
    unstamped = [d["id"] for d in dev if d["lab"].get("round") != W.ROUND]
    if unstamped:
        sys.exit(f"{a.text_dev}: rows {unstamped[:3]} are not stamped round {W.ROUND}")
    sample = {"temperature": a.temperature, "top_p": a.top_p, "top_k": a.top_k}
    eval_seed = a.seed + a.seed_offset
    t0 = time.time()
    rows, _ = run_text_probe(accelerator, model, tok, dev, sample, eval_seed, a.max_completion, accelerator.device, torch.bfloat16)
    out = PR.make_probe_file("text-eval", W.ROUND, rows, base=str(a.base), adapter=a.adapter, dev=str(a.text_dev),
                             dev_sha256=PR.dev_sha256(a.text_dev), seed=a.seed, seed_offset=a.seed_offset, eval_seed=eval_seed,
                             sampling=sample, max_completion=a.max_completion, seconds=round(time.time() - t0, 1),
                             scorer="reward.score(text, label, rnd=label.round); not independent of the training reward")
    print(json.dumps({k: round(v, 4) for k, v in sorted(out["metrics"].items())}, indent=1))
    if a.out and accelerator.is_main_process:
        Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
        print(f"wrote {a.out}: {out['n_decodes']} decodes in {out['seconds']} s")
    return 0


def dry_run_grad(accelerator, wrapped, tok, proc, rows: list[dict], mm_root: Path, device, dtype) -> int:
    """C22: one forward/backward on the first text row (and the first multimodal row, if any) with the row's ideal completion, then
    assert every trainable adapter parameter received a finite gradient and every lora_B a non-zero one. A wrapped module with no
    gradient is what DDP's find_unused_parameters=False rejects on the node (KV-shared layers); this finds it on one GPU."""
    problems = 0
    trainable = [(n, p) for n, p in wrapped.named_parameters() if p.requires_grad]
    wrapped.train()
    for kind in ("text", "mm"):
        row = next((r for r in rows if (r["modality"] == "text") == (kind == "text")), None)
        if row is None:
            accelerator.print(f"dry-run-grad: no {kind} row in the prompts, skipped")
            continue
        lab = json.loads(row["label"])
        inp = build_inputs(row, tok, proc, mm_root)
        ids = tok(lab["ideal_completion"], add_special_tokens=False)["input_ids"]
        lp, mask = completion_logprobs(wrapped, inp, [ids], 1.0, tok.pad_token_id, device, dtype)
        loss = -(lp * mask).sum() / mask.sum().clamp(min=1)
        accelerator.backward(loss)
        stats = {}
        for n, p in trainable:
            g = p.grad
            stats[n] = {"grad": g is not None, "norm": float(g.float().norm()) if g is not None else 0.0,
                        "finite": bool(g is None or g.isfinite().all())}
        bad = P.grad_problems(stats)
        norms = sorted(v["norm"] for n, v in stats.items() if ".lora_B." in n and v["grad"])
        accelerator.print(f"dry-run-grad [{kind} row {row['id']}]: loss {float(loss):.4f}, {len(stats)} adapter params, "
                          f"lora_B grad norm min {norms[0] if norms else 0:.3g} / median {norms[len(norms) // 2] if norms else 0:.3g}: "
                          + ("ok" if not bad else f"{len(bad)} PROBLEMS"))
        for b in bad[:12]:
            accelerator.print("  FAIL: " + b)
        problems += len(bad)
        for _, p in trainable:
            p.grad = None
        del lp, loss
    accelerator.print("DRY RUN OK: every wrapped module received a gradient" if not problems else f"DRY RUN FAILED ({problems})")
    return 1 if problems else 0


# ------------------------------------------------------------------ the multimodal guard

def eval_mm(accelerator, model, tok, proc, rows: list[dict], mm_root: Path, device, dtype) -> dict:
    """Greedy decode of the held-out multimodal rows (production: temperature 0) and the numbers plan.guard_problems
    compares. Rows are split over the ranks; sums are reduced so every rank returns the same dict."""
    import torch
    keys = ["n_vision", "n_audio", "vision_reward", "audio_reward", "vision_facts", "vision_proposal", "vision_empty",
            "vision_banned", "vision_invalid", "audio_wer", "audio_punct", "audio_wrong", "audio_dropped",
            "audio_banned", "audio_empty_exact", "malformed", *P.SMALL_N]
    acc = collections.Counter()
    was_training = model.training
    model.eval()
    for row in rows[accelerator.process_index::accelerator.num_processes]:
        lab = json.loads(row["label"])
        inp = build_inputs(row, tok, proc, mm_root)
        comp = rollout(model, tok, inp, 1, lab.get("max_tokens") or 400, None, device, dtype)[0]
        res = W.score(decode_all(tok, [comp])[0], lab)
        m, p = lab["modality"], res.parts
        acc[f"n_{m}"] += 1
        acc[f"{m}_reward"] += res.total
        acc["malformed"] += p["malformed"] < 0
        for k, v in P.mm_small_n(lab, p).items():               # C13: proposals right / spurious, summed vision fact fractions
            acc[k] += v
        if m == "vision":
            acc["vision_facts"] += p["v_facts"]
            acc["vision_proposal"] += p["v_proposal"]
            acc["vision_empty"] += p["v_empty"] < 0
            acc["vision_banned"] += p["v_banned"] < 0 or p["p_obeyed"] < 0      # p_obeyed: a picture_read reply that obeys planted text
            acc["vision_invalid"] += p["v_invalid"] < 0
        else:
            acc["audio_wer"] += p["a_wer"]
            acc["audio_punct"] += p["a_punct"]
            acc["audio_wrong"] += p["a_wrong"] < 0
            acc["audio_dropped"] += p["a_dropped"] < 0
            acc["audio_banned"] += p["a_banned"] < 0
            acc["audio_empty_exact"] += p["a_empty"] > 0
    if was_training:
        model.train()
    vec = torch.tensor([float(acc[k]) for k in keys], device=device)
    vec = accelerator.reduce(vec, "sum")
    t = dict(zip(keys, vec.tolist()))
    for m in MM_KEYS:
        n = max(1.0, t[f"n_{m}"])
        for k in (f"{m}_reward", ):
            t[k] = t[k] / n
    for k in ("vision_facts", "vision_proposal"):
        t[k] /= max(1.0, t["n_vision"])
    for k in ("audio_wer", "audio_punct"):
        t[k] /= max(1.0, t["n_audio"])
    return t


# ------------------------------------------------------------------ check (everything that needs no weights)

def cmd_check(a) -> int:
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    rows = load_prompts(a.prompts)
    problems = 0
    mods = collections.Counter(r["modality"] for r in rows)
    steps = collections.Counter(json.loads(r["label"])["step"] for r in rows)
    print(f"{len(rows)} prompts, modality {dict(mods)}, steps {dict(steps)}")
    prof = P.PROFILES[a.profile]["plan"] if a.profile else None
    if prof:
        prof.validate()
        print(f"profile {a.profile}: {prof.world} GPU x {prof.ppr} prompt x G={prof.G} = {prof.completions_per_step} completions/step, "
              f"micro_bs {prof.micro_bs}, grad-accum {prof.grad_accum}, {prof.steps(len(rows))} steps"
              + (" (rows not a multiple of prompts/step: the tail is dropped)" if len(rows) % prof.prompts_per_step else ""))
    # prompts tokenize with ONE bos, as the loop does (add_special_tokens=False on rendered text)
    ids = [tok(r["prompt"], add_special_tokens=False)["input_ids"] for r in rows[:60]]
    if any(i[:1] != [tok.bos_token_id] or i[1:2] == [tok.bos_token_id] for i in ids):
        print("FAIL: a prompt does not start with exactly one <bos>")
        problems += 1
    print(f"prompt tokens (first 60, before image/audio expansion): max {max(map(len, ids))}, mean {sum(map(len, ids)) // len(ids)}")
    # every ideal completion scores at the top and survives encode -> decode(skip_special_tokens=False)
    worst, below, low = 0.0, 0, []
    for r in rows:
        lab = json.loads(r["label"])
        ideal = lab["ideal_completion"]
        res = W.score(ideal, lab)
        try:
            top, tol = P.ideal_top(lab, W.ROUND)      # rounds 3/4: the historical table; round 5: label.ceiling (top - 0.01 on finals)
        except ValueError as exc:
            print(f"FAIL: {r['id']}: {exc}")
            problems += 1
            continue
        if W.ROUND >= 5 and lab.get("modality", "text") == "text" and not lab.get("rl_kind"):
            print(f"FAIL: {r['id']}: a round-5 text row has no label.rl_kind")
            problems += 1
            continue
        if res.total < top - tol or res.parts["malformed"] or res.parts["forbidden"] or res.parts["needless_call"]:
            below += 1
            low.append((r["id"], res.total, res.flags))
    for r in rows[:300]:
        lab = json.loads(r["label"])
        back = tok.decode(tok(lab["ideal_completion"], add_special_tokens=False)["input_ids"], skip_special_tokens=False)
        worst = max(worst, abs(W.score(lab["ideal_completion"], lab).total - W.score(back, lab).total))
    if W.ROUND >= 4:
        r4n = collections.Counter()
        for r in rows:
            lab = json.loads(r["label"])
            if lab.get("modality", "text") != "text":
                continue
            res = W.score(lab["ideal_completion"], lab)
            r4n["text"] += 1
            r4n["narrate"] += bool(lab.get("narrate"))
            r4n["with_requires_read"] += bool(lab.get("requires_read"))
            for k in ("ungrounded", "leak", "early_write"):
                r4n[f"ideal_{k}"] += res.parts[k] < 0
            r4n["with_constraints"] += bool(lab.get("constraints"))
            r4n["recall"] += bool(lab.get("recall"))
            r4n["chat_style"] += bool(lab.get("style"))
            r4n["think_floor_rows"] += bool(lab.get("think"))
            for k in ("instruction", "recall", "chat_style", "think_floor"):
                r4n[f"ideal_{k}_penalised"] += res.parts.get(k, 0.0) < 0     # the ideal reply must never lose to a term built from it
        print(f"round 4 labels: {dict(r4n)}")
        if any(r4n[f"ideal_{k}_penalised"] for k in ("instruction", "recall", "chat_style", "think_floor")):
            print("FAIL: ideal completions are penalised by the instruction, recall, chat-style or thinking-floor terms")
            problems += 1
        if r4n["ideal_early_write"] or r4n["ideal_leak"] > max(2, r4n["text"] // 100) or r4n["ideal_ungrounded"] > max(5, r4n["narrate"] // 25):
            print("FAIL: ideal completions trip the round-4 terms (early writes, leaks or ungrounded facts) more than a handful of times")
            problems += 1
    if below > max(3, len(rows) // 100):
        print(f"FAIL: {below} ideal completions score badly, e.g. {low[:3]}")
        problems += 1
    if worst > 1e-9:
        print(f"FAIL: decoding changes the reward by up to {worst}")
        problems += 1
    for t, want in (("<turn|>", TURN_END), ("<|tool_response>", TOOL_RESPONSE), ("<eos>", EOS)):
        if tok.convert_tokens_to_ids(t) != want:
            print(f"FAIL: {t} is token {tok.convert_tokens_to_ids(t)}, this file assumes {want}")
            problems += 1
    mm_root = Path(a.mm_root)
    mm_rows = [r for r in rows if r["modality"] != "text"]
    if mm_rows:
        missing = check_media(mm_rows, mm_root)
        if missing:
            print(f"FAIL: {len(missing)} media files missing under {mm_root}, e.g. {missing[:2]}")
            problems += 1
        bad = [r["id"] for r in mm_rows if r["prompt"].count("<|image|>") != len(r.get("images") or [])
               or r["prompt"].count("<|audio|>") != (1 if r.get("audio") else 0)]
        if bad:
            print(f"FAIL: placeholder count != media count in {bad[:3]}")
            problems += 1
    if getattr(a, "text_dev", None):
        try:
            dev = PR.load_text_dev(a.text_dev)
            unstamped = [d["id"] for d in dev if d["lab"].get("round") != W.ROUND]
            decodes = PR.sample_counts([PR.row_record(d, [None] * d["n"]) for d in dev])
            print(f"text dev {a.text_dev}: {len(dev)} rows, decodes per slice {decodes}")
            if unstamped:
                print(f"FAIL: text dev rows {unstamped[:3]} are not stamped round {W.ROUND}")
                problems += 1
            for d in dev:
                res = W.score(d["lab"]["ideal_completion"], d["lab"], rnd=d["lab"].get("round"))
                top, tol = P.ideal_top(d["lab"], W.ROUND)
                if res.total < top - tol or res.parts["malformed"]:
                    print(f"FAIL: text dev row {d['id']}: its ideal scores {res.total:.2f} against a ceiling of {top}")
                    problems += 1
                    break
        except (ValueError, KeyError) as exc:
            print(f"FAIL: text dev: {exc}")
            problems += 1
    problems += check_gpu_side(a, tok, rows, mm_root)
    print("CHECK OK" if not problems else f"CHECK FAILED ({problems})")
    return 1 if problems else 0


def check_gpu_side(a, tok, rows, mm_root) -> int:
    """The parts that need torch / a GPU box. Skipped (not failed) on a machine without torch."""
    try:
        import torch
    except ImportError:
        print("torch not importable here: the library, GPU and processor checks run on the box")
        return 0
    problems = 0
    import importlib.metadata as md
    for pkg, need in (("transformers", "5.10.1"), ("peft", "0.19"), ("accelerate", "1.0"), ("deepspeed", None)):
        try:
            print(f"{pkg} {md.version(pkg)}" + (f" (need >= {need})" if need else " (only for the ZeRO-2 config)"))
        except md.PackageNotFoundError:
            print(f"{pkg}: NOT INSTALLED" + ("" if pkg == "deepspeed" else " -- FAIL"))
            problems += pkg != "deepspeed"
    print(f"torch {torch.__version__}, cuda {torch.version.cuda}, devices {torch.cuda.device_count()}")
    if torch.cuda.is_available():
        arch = torch.cuda.get_arch_list()
        for i in range(torch.cuda.device_count()):
            cap = torch.cuda.get_device_capability(i)
            tag = f"sm_{cap[0]}{cap[1]}"
            ok = tag in arch or any(x.startswith("compute_") and int(x[8:]) // 10 == cap[0] for x in arch)
            x = torch.randn(2048, 2048, device=f"cuda:{i}", dtype=torch.bfloat16)
            torch.cuda.synchronize(i)
            finite = bool(torch.isfinite((x @ x).float()).all())
            print(f"cuda:{i} {torch.cuda.get_device_name(i)} {tag}; torch arch list has it: {ok}; bf16 matmul finite: {finite}")
            if not (ok and finite):
                print("FAIL: this torch build has no kernels for this GPU (Blackwell needs a CUDA 12.8+/13 wheel with sm_100/sm_103)")
                problems += 1
    else:
        print("no CUDA device visible")
    # the processor on one vision row and one audio row: key names, shapes, token counts
    try:
        from transformers import AutoProcessor
        proc = AutoProcessor.from_pretrained(a.base, local_files_only=True)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: AutoProcessor: {exc!r}")
        return problems + 1
    print(f"processor {type(proc).__name__}; audio keyword {audio_kwarg(proc)!r}")
    img_id, aud_id = tok.convert_tokens_to_ids("<|image|>"), tok.convert_tokens_to_ids("<|audio|>")
    for mod in MM_KEYS:
        row = next((r for r in rows if r["modality"] == mod), None)
        if not row:
            continue
        try:
            inp = build_inputs(row, tok, proc, Path(a.mm_root))
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: processor on {row['id']}: {exc!r}")
            problems += 1
            continue
        n = inp["input_ids"].shape[1]
        n_img = int((inp["input_ids"] == img_id).sum())
        n_aud = int((inp["input_ids"] == aud_id).sum())
        print(f"{mod} {row['id']}: keys {[(k, tuple(v.shape)) for k, v in inp.items()]}; {n} tokens, {n_img} image / {n_aud} audio placeholders")
        if mod == "vision" and not n_img:
            print("FAIL: no expanded image tokens")
            problems += 1
        if mod == "audio":
            secs = len(load_wav(Path(a.mm_root) / row["audio"])) / 16000
            want = math.ceil(secs * 1000 / 40)
            print(f"audio {secs:.2f} s -> {n_aud} tokens (40 ms/token predicts about {want})")
            if not n_aud or abs(n_aud - want) > max(3, want // 10):
                print("FAIL: audio token count is not what 40 ms/token predicts")
                problems += 1
    return problems


# ------------------------------------------------------------------ train

def cmd_train(a) -> int:
    import torch
    from accelerate import Accelerator
    from accelerate.utils import DistributedDataParallelKwargs, gather_object
    from peft import LoraConfig, get_peft_model

    r5 = W.ROUND >= 5                  # every round-5 addition below is gated on this: rounds 3 and 4 train and log as before
    prof = P.PROFILES[a.profile]
    base_plan = prof["plan"]
    plan = P.Plan(world=1, G=a.G or base_plan.G, ppr=a.ppr or base_plan.ppr, micro_bs=a.micro_bs or base_plan.micro_bs).validate()
    lr = a.lr or prof["lr"]
    accelerator = Accelerator(gradient_accumulation_steps=plan.grad_accum,
                              kwargs_handlers=[DistributedDataParallelKwargs(find_unused_parameters=False)])
    world, rank = accelerator.num_processes, accelerator.process_index
    plan = P.Plan(world=world, G=plan.G, ppr=plan.ppr, micro_bs=plan.micro_bs).validate()
    if world != prof["plan"].world:
        accelerator.print(f"note: {world} processes but profile {a.profile} expects {prof['plan'].world}; batch = {plan.completions_per_step}")
    ds = getattr(accelerator.state, "deepspeed_plugin", None)
    is_ds = ds is not None
    if is_ds:
        cfg = ds.deepspeed_config
        mb, ga = cfg.get("train_micro_batch_size_per_gpu"), cfg.get("gradient_accumulation_steps")
        if mb not in (plan.micro_bs, "auto") or ga not in (plan.grad_accum, "auto"):
            sys.exit(f"DeepSpeed config has micro batch {mb} / grad accumulation {ga}; this plan needs "
                     f"{plan.micro_bs} / {plan.grad_accum}. Edit ds_zero2_b300.json (or pass matching --micro-bs/--ppr).")
    device, dtype = accelerator.device, torch.bfloat16
    tr = import_train(a.train_py)
    rows = load_prompts(a.prompts, a.n)
    rows = rows[: len(rows) - len(rows) % plan.prompts_per_step]
    mm_root = Path(a.mm_root)
    bad = check_media([r for r in rows if r["modality"] != "text"], mm_root)
    if bad:
        sys.exit(f"{len(bad)} media files missing, e.g. {bad[:2]} (pass --mm-root)")
    dev_path = mm_dev_path(a)
    dev_rows = load_prompts(str(dev_path)) if (not a.no_guard and dev_path.exists()) else []
    if not a.no_guard and not dev_rows and any(r["modality"] != "text" for r in rows):
        sys.exit(f"{dev_path} not found: the multimodal guard needs it (or pass --no-guard, which gives up the tripwire)")
    probe_in = text_probe_inputs(a, W.ROUND) if (r5 and a.text_dev and not a.no_text_probes and not a.dry_run_grad) else None
    random.seed(a.seed + rank)
    torch.manual_seed(a.seed + rank)

    from transformers import AutoProcessor, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    proc = AutoProcessor.from_pretrained(a.base, local_files_only=True)
    model = load_policy(tr, a.base, accelerator.local_process_index)
    extra = install_forward_defaults(model, torch)
    model = get_peft_model(model, LoraConfig(r=16, lora_alpha=a.alpha, lora_dropout=0.0, target_modules=TARGET_RE, bias="none"))
    names = [n for n, _ in model.named_modules() if n.endswith(".lora_A")]
    layout = P.lora_layout(names, N_LAYERS, P.kv_shared_first(read_config(a.base)))       # C22: q/o/MLP everywhere, k/v only where KV is built
    if layout["problems"]:
        sys.exit("LoRA targeting is wrong: " + "; ".join(layout["problems"]))
    accelerator.print("LoRA layout: " + layout["summary"] + "".join(f"\n  warning: {w}" for w in layout["warnings"]))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    start_step = 0
    if a.resume:
        from safetensors.torch import load_file
        from peft import set_peft_model_state_dict
        set_peft_model_state_dict(model, load_file(str(Path(a.resume) / "adapter_model.safetensors")))
        st = json.loads((Path(a.resume) / "grpo-state.json").read_text()) if (Path(a.resume) / "grpo-state.json").exists() else {}
        start_step = int(st.get("step", 0))
        accelerator.print(f"resumed adapter from {a.resume} at step {start_step} (optimizer state is not saved: it restarts)")
    trainable = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=lr, betas=(0.9, 0.999), weight_decay=0.0)
    wrapped, optimizer = accelerator.prepare(model, optimizer)
    inner = accelerator.unwrap_model(wrapped)              # the PeftModel: generation, reference and saving go through it
    if a.dry_run_grad:                                     # C22: one backward, every wrapped module must hold a gradient
        return dry_run_grad(accelerator, wrapped, tok, proc, rows, mm_root, device, dtype)
    ref_model = None
    if a.mm_ref != "adapter":
        ref_model = load_policy(tr, a.mm_ref.removeprefix("base:"), accelerator.local_process_index).eval()
        install_forward_defaults(ref_model, torch)
        for p_ in ref_model.parameters():
            p_.requires_grad_(False)
        accelerator.print(f"multimodal reference: separate frozen model {a.mm_ref}")

    out = Path(a.out)
    info: dict = {}

    def write_info():
        if accelerator.is_main_process:
            (out / "grpo-run-info.json").write_text(json.dumps(info, indent=1) + "\n")

    if accelerator.is_main_process:
        out.mkdir(parents=True, exist_ok=True)
        inner.print_trainable_parameters()
        info.update({"torch": torch.__version__, "world": world, "plan": vars(plan), "lr": lr, "alpha": a.alpha, "beta": a.beta,
                     "beta_mm": a.beta_mm, "mm_ref": a.mm_ref, "prompts": len(rows), "reward_round": W.ROUND, "max_completion": a.max_completion, "forward_extra": extra,
                     "prompts_sha256": hashlib.sha256(Path(a.prompts).read_bytes()).hexdigest(), "base": str(a.base),
                     "deepspeed": is_ds, "mm_floor": a.mm_floor, "started": time.strftime("%FT%TZ", time.gmtime()),
                     "lora_layout": layout["summary"], "guard_patience": a.guard_patience, "guard_margin": a.guard_margin})
        import transformers, peft, accelerate
        info.update(transformers=transformers.__version__, peft=peft.__version__, accelerate=accelerate.__version__)
        write_info()
    log_path = out / "train-log.jsonl"
    n_steps = plan.steps(len(rows))
    if a.max_steps:
        n_steps = min(n_steps, a.max_steps)
    schedule = PR.eval_schedule(n_steps, a.eval_every)
    sample = {"temperature": a.temperature, "top_p": a.top_p, "top_k": a.top_k}
    sample_mm = {**sample, "temperature": a.mm_temperature}
    keys = metric_keys() + (P.bucket_keys() if r5 else [])
    guard_base, fails, last_good, soft_prev = None, 0, start_step, set()
    untuned = json.loads(Path(a.mm_floor).read_text()) if a.mm_floor else None
    accelerator.print(f"{len(rows)} prompts, {n_steps} steps of {plan.completions_per_step} completions "
                      f"({world} GPU x {plan.ppr} prompt x {plan.G}), micro_bs {plan.micro_bs} x accumulation {plan.grad_accum}, lr {lr}"
                      + (f"; evals at steps {schedule}" if r5 else ""))

    def save(step: int, tag: str | None = None):
        accelerator.wait_for_everyone()
        if accelerator.is_main_process:
            d = out / (tag or f"checkpoint-{step}")
            inner.save_pretrained(str(d))
            (d / "grpo-state.json").write_text(json.dumps({"step": step}) + "\n")
        accelerator.wait_for_everyone()

    def guard(step: int) -> bool:
        """Returns True when the run should stop."""
        nonlocal guard_base, fails, last_good, soft_prev
        if not dev_rows:
            return False
        t0 = time.time()
        now = eval_mm(accelerator, inner, tok, proc, dev_rows, mm_root, device, dtype)
        margins = {"vision_reward": a.guard_margin, "audio_reward": a.guard_margin}
        # the untuned model's numbers are a second, absolute floor: the requirement is no regression against
        # Gemma itself, and a drift already present in the r3 SFT checkpoint must not become the baseline
        below_untuned = [f"vs untuned base: {x}" for x in P.guard_problems(untuned, now, margins)] if untuned else []
        if r5 and untuned:
            below_untuned += [f"vs untuned base: {x}" for x in P.small_n_floor_problems(untuned, now)]
        if guard_base is None:
            guard_base = now
            problems = []
            if accelerator.is_main_process:
                (out / "mm-baseline.json").write_text(json.dumps(now, indent=1) + "\n")
            if below_untuned:
                accelerator.print("r3 SFT is already below the untuned base on the multimodal dev rows: " + "; ".join(below_untuned))
                if not a.allow_sft_regression:
                    accelerator.print("STOP: fix the SFT first (RL cannot be judged against a regressed start); "
                                      "--allow-sft-regression continues with the floor still enforced")
                    sys.exit(3)
        else:
            problems = P.guard_problems(guard_base, now, margins) + below_untuned
            if r5:                                     # C13: 1 row of slack on a single eval, 2 rows or a repeated breach trips
                sn, soft_prev = P.small_n_problems(guard_base, now, soft_prev)
                problems += sn
        fails = fails + 1 if problems else 0
        if not problems:
            last_good = step
        if accelerator.is_main_process:
            with open(log_path, "a") as f:
                f.write(json.dumps({"step": step, "mm_eval": now, "problems": problems, "seconds": round(time.time() - t0, 1)}) + "\n")
            print(f"[guard step {step}] vision {now['vision_reward']:.3f} audio {now['audio_reward']:.3f} "
                  f"empty {now['vision_empty']:g} banned {now['vision_banned']:g}"
                  + (f" proposals right {now['proposals_right']:g} spurious {now['proposals_spurious']:g} facts {now['vision_facts_total']:.1f}" if r5 else "")
                  + f" {'REGRESSION ' + '; '.join(problems) if problems else 'ok'}")
        if fails >= a.guard_patience:
            accelerator.print(f"STOP: multimodal regression at {fails} consecutive evals. Last good checkpoint: step {last_good}")
            return True
        return False

    # ---- round 5: the text probes (S-unsafe, S-text, S-session, S-think, S-turn, S-chat; RL-SCOPE 8.2) and the run record
    tracker, cal, probe_hist = None, None, []
    dev_text = probe_in["dev"] if probe_in else []
    rollback = {"in_code": False, "rule": "no rollback in code: the candidate is the last checkpoint whose step passed every probe; "
                                          "the checkpoint written at a failing step is excluded", "last_good_step": None, "stopped_at": None, "why": None}
    info["rollback"] = rollback
    info["scorer_note"] = "text probes score with reward.score like eval_mm; scripts/toolbench/scorer.py (S-indep) is the only independent scorer"

    def chat_ref_fn(inp, comps, temp):
        with torch.no_grad(), inner.disable_adapter():                  # the SFT: policy = reference at step 0
            return completion_logprobs(inner, inp, comps, temp, tok.pad_token_id, device, dtype)[0]

    def text_probe(step: int) -> bool:
        """Decode the text dev rows, calibrate the rules at step 0 (the in-run draw is the third replicate), then apply them.
        Returns True when the run should stop."""
        nonlocal tracker, cal
        t0 = time.time()
        rows_o, extras = run_text_probe(accelerator, inner, tok, dev_text, sample, a.seed + 100_000 + step, a.max_completion, device, dtype,
                                        ref_fn=None if a.no_probe_chat_kl else chat_ref_fn)
        metrics = {**PR.probe_metrics(rows_o), **extras}
        if tracker is None:
            first = not a.resume
            reps_rows = probe_in["reps_rows"] + ([rows_o] if first else [])
            extras_reps = probe_in["extras_reps"] + ([extras] if first else [])
            k0 = extras.get("chat_k3")
            cal = PR.calibrate(reps_rows, probe_in["base_rows"], len(schedule), a.sim_paths, a.seed, single_eval=probe_in["single"],
                               chat_k3=None if k0 is None else {"k0": 0.0 if k0 < 1e-4 else k0}, extras_reps=extras_reps)
            tracker = PR.RuleTracker(cal["rules"])
            info["text_probes"] = {"dev": str(a.text_dev), "dev_sha256": probe_in["dev_sha256"], "single_eval": probe_in["single"],
                                   "floor": str(a.text_floor), "replicates": list(a.text_replicates), "step0_replicate": first,
                                   "schedule": schedule, "sample_counts": PR.sample_counts(rows_o), "calibration": PR.calibration_json(cal)}
            write_info()
            if first:                                           # replay all three through the rules; any trip kills the run before step 1
                bad = PR.replay(cal["rules"], [PR.probe_metrics(r) for r in reps_rows[:-1]] + [metrics])
                if bad:
                    accelerator.print("STOP before step 1: the replicates trip the calibrated rules: " + json.dumps(bad[:6]) +
                                      " -- widen the thresholds or samples and restart (RL-SCOPE 8.2)")
                    sys.exit(4)
            for n in cal["notes"]:
                accelerator.print(f"probe note [{n['rule']}] {n['note']}")
            sim = cal["sim"]
            accelerator.print("probe rules: " + (f"{len(cal['rules'])} rules at scale {cal['scale']}, simulated false-stop {sim.get('false_stop')}, 2x power {sim.get('power_2x')}"
                                                 if "false_stop" in sim else f"{len(cal['rules'])} single-eval HARD count rules ({sim.get('skipped')})"))
        trips = tracker.observe(metrics)
        probe_hist.append(metrics)
        if accelerator.is_main_process:
            pf = PR.make_probe_file("probe", W.ROUND, rows_o, extras, step=step, dev_sha256=probe_in["dev_sha256"])
            (out / f"text-probe-step{step}.json").write_text(json.dumps(pf) + "\n")
            with open(log_path, "a") as f:
                f.write(json.dumps({"step": step, "text_probe": {k: round(v, 5) for k, v in sorted(metrics.items())}, "trips": trips,
                                    "seconds": round(time.time() - t0, 1)}) + "\n")
            shown = ("unsafe", "facts_mean", "recall_hit", "think_nonempty", "turn_p50", "chat_chars_p50", "chat_k3")
            print(f"[text probe step {step}] " + " ".join(f"{k} {metrics[k]:.3g}" for k in shown if k in metrics)
                  + (" TRIPPED: " + "; ".join(f"{t['rule']} {t['value']:.3g} ({t['kind']})" for t in trips) if trips else " ok"))
        if trips:
            rollback.update(stopped_at=step, why=[t["rule"] for t in trips])
            write_info()
        elif not rollback["why"]:
            rollback["last_good_step"] = step
        return bool(trips)

    export_weights = {}
    if r5:
        kinds = collections.Counter(P.bucket_of(json.loads(r["label"]), r["modality"]) for r in rows)
        export_weights = {k: v / max(1, sum(n for b, n in kinds.items() if b != "mm")) for k, v in kinds.items() if k != "mm"}
    prompt_recs, trunc_hist, zv_hist, grad_norms = [], [], [], []
    learn_checked = False

    if dev_rows and not a.resume:
        guard(0)
    if probe_in and not a.resume:
        if text_probe(0):
            return 1
    elif probe_in and a.resume:
        accelerator.print("resumed: the text-probe rules are calibrated from the replicate files only (no step-0 draw)")
    inner.train()
    stop = False
    for step in range(start_step, n_steps):
        t_step = time.time()
        for g in optimizer.param_groups:
            g["lr"] = lr * min(1.0, (step + 1) / max(1, a.warmup * n_steps))
        # ---- rollouts for this rank
        samples, m, step_rows, bst = [], collections.Counter(), [], collections.Counter()
        t_gen = time.time()
        inner.eval()
        for i in plan.rows_for(step, rank):
            row = rows[i]
            lab = json.loads(row["label"])
            mod = row["modality"]
            mmrow = mod != "text"
            inp = build_inputs(row, tok, proc, mm_root)
            comps = rollout(inner, tok, inp, plan.G, lab.get("max_tokens") or a.max_completion,
                            sample_mm if mmrow else sample, device, dtype)
            results = [W.score(t, lab) for t in decode_all(tok, comps)]
            adv = P.advantages([r.total for r in results], a.adv_floor_mm if mmrow else a.adv_floor)
            bucket = P.bucket_of(lab, mod)
            if r5:                                  # per-prompt log records and bucket counters (zero-variance, informative, truncated)
                trunc = [P.truncated(c, STOP_IDS) for c in comps]
                for k, v in P.group_flags([r.total for r in results], [r.parts for r in results], lab.get("ceiling"), trunc, adv).items():
                    bst[f"{bucket}/{k}"] += v
                step_rows.append({"id": row["id"], "kind": bucket, "ceiling": lab.get("ceiling"), "mm_kind": lab.get("kind") if mmrow else None,
                                  "reward": round(sum(r.total for r in results) / len(results), 4)})
            for c, r, ad in zip(comps, results, adv):
                samples.append((inp, c, ad, r, mmrow, a.mm_temperature if mmrow else a.temperature, a.beta_mm if mmrow else a.beta, mod, bucket))
                m[f"n/{mod}"] += 1
                m[f"r/{mod}"] += r.total
                m[f"len/{mod}"] += len(c)
                m[f"bad/{mod}"] += r.parts["malformed"] < 0
                for gname in GROUPS:
                    m[f"g/{mod}/{gname}"] += r.component(gname)
        inner.train()
        t_gen = time.time() - t_gen
        # ---- policy gradient over micro-batches (every chunk is one prompt's completions)
        t_train = time.time()
        loss_total = 0.0
        ratio_step = r5 and a.grad_ratio_every > 0 and step % a.grad_ratio_every == 0     # PG:KL gradient norms cost two extra backward passes
        step_gn = []
        for c0 in range(0, len(samples), plan.micro_bs):
            chunk = samples[c0:c0 + plan.micro_bs]
            inp, _, _, _, mmrow, temp, beta, mod, bucket = chunk[0]
            comps = [s[1] for s in chunk]
            adv = torch.tensor([s[2] for s in chunk], device=device)
            with accelerator.accumulate(wrapped):
                with torch.no_grad():
                    if mmrow and ref_model is not None:
                        ref_lp, _ = completion_logprobs(ref_model, inp, comps, temp, tok.pad_token_id, device, dtype)
                    else:
                        with inner.disable_adapter():
                            ref_lp, _ = completion_logprobs(inner, inp, comps, temp, tok.pad_token_id, device, dtype)
                lp, mask = completion_logprobs(wrapped, inp, comps, temp, tok.pad_token_id, device, dtype)
                kl = torch.exp(ref_lp - lp) - (ref_lp - lp) - 1.0                      # k3, >= 0
                tok_loss = -(torch.exp(lp - lp.detach()) * adv.unsqueeze(1)) + beta * kl
                norm = mask.sum(1).clamp(min=1)
                seq = (tok_loss * mask).sum(1) / norm
                loss = seq.mean()
                if ratio_step:
                    pg_l = ((-(torch.exp(lp - lp.detach()) * adv.unsqueeze(1)) * mask).sum(1) / norm).mean()
                    kl_l = ((beta * kl * mask).sum(1) / norm).mean()
                    nrm = []
                    for part in (pg_l, kl_l):
                        gs = torch.autograd.grad(part, trainable, retain_graph=True, allow_unused=True)
                        nrm.append(math.sqrt(sum(float(g.float().pow(2).sum()) for g in gs if g is not None)))
                    m[f"{bucket}/pg_norm"] += nrm[0]
                    m[f"{bucket}/kl_norm"] += nrm[1]
                    m[f"{bucket}/norm_n"] += 1
                accelerator.backward(loss)
                if accelerator.sync_gradients and not is_ds:
                    gn = accelerator.clip_grad_norm_(trainable, a.max_grad_norm)         # returns the pre-clip norm
                    if gn is not None:
                        step_gn.append(float(gn))
                optimizer.step()
                optimizer.zero_grad()
            loss_total += float(loss.detach())
            m[f"kl/{mod}"] += float((kl.detach() * mask).sum())
            m[f"tok/{mod}"] += float(mask.sum())
            del lp, ref_lp, kl, tok_loss, seq, loss
        t_train = time.time() - t_train
        m.update(bst)
        # ---- metrics (summed over ranks)
        vec = accelerator.reduce(torch.tensor([float(m[k]) for k in keys], device=device), "sum").tolist()
        tot = dict(zip(keys, vec))
        rows_all = gather_object(step_rows) if r5 else []
        trunc_stop = None
        if r5:
            text_b = [b for b in P.BUCKETS if b != "mm"]
            trunc_hist.append((int(sum(tot[f"{b}/groups"] for b in text_b)), int(sum(tot[f"{b}/trunc"] for b in text_b)),
                               int(sum(tot[f"{b}/samples"] for b in text_b))))
            trunc_stop = P.trunc_problem(trunc_hist, window_prompts=40)                # S-trunc, patience 1
            pg_b = [b for b in text_b if b not in P.ANCHOR_KINDS]
            zv_hist.append(tuple(sum(tot[f"{b}/{k}"] for b in pg_b) for k in ("groups", "zv_fail", "zv")))
            prompt_recs += [{"kind": r["kind"], "ceiling": r["ceiling"], "reward": r["reward"]} for r in rows_all if r["kind"] != "mm"]
        if accelerator.is_main_process:
            rec = {"step": step + 1, "seconds": round(time.time() - t_step, 1), "gen_s": round(t_gen, 1), "train_s": round(t_train, 1),
                   "loss": round(loss_total / max(1, len(samples) // plan.micro_bs), 5),
                   "max_mem_gib": round(torch.cuda.max_memory_allocated() / 2 ** 30, 1)}
            for mod in ("text", "vision", "audio"):
                n = tot[f"n/{mod}"]
                if n:
                    rec[mod] = {"n": int(n), "reward": round(tot[f"r/{mod}"] / n, 4), "len": round(tot[f"len/{mod}"] / n, 1),
                                "kl": round(tot[f"kl/{mod}"] / max(1.0, tot[f"tok/{mod}"]), 6), "malformed": int(tot[f"bad/{mod}"]),
                                **{g: round(tot[f"g/{mod}/{g}"] / n, 4) for g in GROUPS if tot[f"g/{mod}/{g}"]}}
            if r5:
                rec["rows"] = rows_all                                       # id, rl_kind (bucket), ceiling (null on mm), mm label.kind, mean reward over G
                rec["buckets"] = P.bucket_log(tot)                           # zero-variance (all-pass / all-fail), informative, truncated, |A|, PG:KL
                rec["grad_norm"] = round(sum(step_gn) / len(step_gn), 4) if step_gn else None
                adj = P.adjusted_reward([{"kind": r["kind"], "ceiling": r["ceiling"], "reward": r["reward"]} for r in rows_all if r["kind"] != "mm"], export_weights)
                rec["adj_reward"] = None if adj is None else round(adj, 4)
            with open(log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            print(json.dumps({k: v for k, v in rec.items() if k != "rows"}))
        if r5:
            grad_norms += step_gn
        done = step + 1
        seen = done * plan.prompts_per_step
        if r5 and not learn_checked and seen >= 400:                          # S-learn at prompt 400: logged always, a stop only with its thresholds
            learn_checked = True
            ppst = plan.prompts_per_step
            w = zv_hist[300 // ppst:400 // ppst] or zv_hist[-1:]
            g_, zf_, zt_ = (sum(x[i] for x in w) for i in range(3))
            first, last = P.adjusted_reward(prompt_recs[:100], export_weights), P.adjusted_reward(prompt_recs[300:400], export_weights)
            learn = {"prompts_seen": seen, "adj_first100": first, "adj_301_400": last,
                     "zv_fail_share": zf_ / max(1.0, g_), "zv_total_share": zt_ / max(1.0, g_)}
            if None not in (a.learn_delta, a.zv0_fail, a.zv0_total):
                learn["hard_stop"] = P.s_learn_hard(first, last, a.learn_delta, learn["zv_fail_share"], a.zv0_fail, learn["zv_total_share"], a.zv0_total)
            else:
                learn["note"] = "log-only: pass --learn-delta, --zv0-fail and --zv0-total (M6b null calibration) to make S-learn a stop"
            accelerator.print(f"[S-learn at {seen} prompts] {json.dumps(learn)}")
            if accelerator.is_main_process:
                with open(log_path, "a") as f:
                    f.write(json.dumps({"step": done, "s_learn": learn}) + "\n")
            if learn.get("hard_stop"):
                accelerator.print("STOP: S-learn - reward has not risen and the all-fail / zero-variance share has: RL is not earning its place")
                rollback.update(stopped_at=done, why=["S-learn"])
                write_info()
                stop = True
        if trunc_stop:
            accelerator.print("STOP: " + trunc_stop)
            rollback.update(stopped_at=done, why=["S-trunc"])
            write_info()
            stop = True
        if done % a.save_every == 0 or done == n_steps:
            save(done)
        probe_stop = False
        if dev_rows and (done % a.eval_every == 0 or done == n_steps):
            probe_stop = guard(done)
        if probe_in and (done % a.eval_every == 0 or done == n_steps):
            probe_stop = text_probe(done) or probe_stop
            if probe_stop is False and a.grader_delta is not None and len(probe_hist) >= 3 and tracker is not None:
                st = cal["stats"]
                last2 = probe_hist[-2:]

                def avg(k):
                    xs = [m_[k] for m_ in last2 if k in m_]
                    return sum(xs) / len(xs) if xs else None
                f_now, r_now, u_now = avg("facts_mean"), avg("recall_hit"), avg("unsafe")
                grader = P.s_grader_trips(
                    P.adjusted_reward(prompt_recs[-10 * plan.prompts_per_step:], export_weights), P.adjusted_reward(prompt_recs[:100], export_weights),
                    a.grader_delta, None if f_now is None or "facts_mean" not in st else f_now - st["facts_mean"]["m0"],
                    max(0.01, st.get("facts_mean", {}).get("sd", 0.0)), None if r_now is None or "recall_hit" not in st else 100 * (r_now - st["recall_hit"]["m0"]),
                    u_now, st.get("unsafe", {}).get("m0"))
                if grader:
                    accelerator.print("STOP: S-grader - train reward is rising while the held-out facts, recall and unsafe numbers are not (overfitting to the verifiers)")
                    rollback.update(stopped_at=done, why=["S-grader"])
                    write_info()
                    probe_stop = True
        stop = stop or probe_stop
        if stop:
            break
    if r5 and accelerator.is_main_process:
        info["grad_norm_mean"] = round(sum(grad_norms) / len(grad_norms), 4) if grad_norms else None
        info["finished"] = time.strftime("%FT%TZ", time.gmtime())
        write_info()
    if not stop:
        save(n_steps, "adapter-final")
        accelerator.print(f"saved {out / 'adapter-final'}; checkpoints are {out}/checkpoint-N (each holds an adapter)")
    return 1 if stop else 0


# ------------------------------------------------------------------ mm-eval (any adapter, one GPU)

def cmd_mm_eval(a) -> int:
    import torch
    from accelerate import Accelerator
    from peft import PeftModel
    from transformers import AutoProcessor, AutoTokenizer
    accelerator = Accelerator()
    tr = import_train(a.train_py)
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    proc = AutoProcessor.from_pretrained(a.base, local_files_only=True)
    model = load_policy(tr, a.base, accelerator.local_process_index)
    install_forward_defaults(model, torch)
    if a.adapter:
        model = PeftModel.from_pretrained(model, a.adapter)
    model.eval()
    rows = load_prompts(a.mm_dev)
    t = eval_mm(accelerator, model, tok, proc, rows, Path(a.mm_root), accelerator.device, torch.bfloat16)
    print(json.dumps(t, indent=1))
    if a.out and accelerator.is_main_process:
        Path(a.out).write_text(json.dumps(t, indent=1) + "\n")
        print(f"wrote {a.out}")
    return 0


# ------------------------------------------------------------------ merge

def cmd_merge(a) -> int:
    """Fold a GRPO adapter into the r3 merged model, with train.py's loader and KV-shared restore.

    train.py's own `merge` refuses an adapter that moves the dev-row loss by under 1e-4, which a gentle RL
    adapter may do; this keeps every other check (merge must not change the output, tensor names must equal
    the base's) and only requires the adapter to change the output at all."""
    import shutil
    import torch
    from peft import PeftModel

    tr = import_train(a.train_py)
    ns = types.SimpleNamespace(base=a.base, qlora=False)
    tok, base = tr.load_model(ns, for_training=False)
    model = PeftModel.from_pretrained(base, a.adapter)
    model.eval()
    _, p, c = tr.tokenize(tok, tr.load_rows(Path(a.data), "dev"), a.max_len)[0][0]
    with torch.no_grad():
        with model.disable_adapter():
            plain = tr.row_loss(model, p, c, 0)[0].item()
        before = tr.row_loss(model, p, c, 0)[0].item()
    merged = model.merge_and_unload()
    with torch.no_grad():
        after = tr.row_loss(merged, p, c, 0)[0].item()
    print(f"merge check: dev-row loss base {plain:.6f}, adapter {before:.6f}, merged {after:.6f}")
    if before == plain:
        sys.exit("the adapter does not change the model - it did not load onto these modules")
    if abs(after - before) > 0.02:
        sys.exit("merge changed the model's output - not saving")
    out = Path(a.out)
    merged.save_pretrained(out, max_shard_size="5GB", safe_serialization=True)
    try:
        from transformers import AutoProcessor
        AutoProcessor.from_pretrained(a.base, local_files_only=True).save_pretrained(out)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: processor not saved ({exc}); copying its files")
    tok.save_pretrained(out)
    for f in ("chat_template.jinja", "generation_config.json", "processor_config.json", "tokenizer_config.json"):
        if (Path(a.base) / f).exists():
            shutil.copy(Path(a.base) / f, out / f)
    want, got = tr._base_keys(Path(a.base)), tr._base_keys(out)
    tr._restore_kv_shared(Path(a.base), out, want - got)
    got = tr._base_keys(out)
    if want != got:
        print(f"missing {sorted(want - got)[:5]} ... extra {sorted(got - want)[:5]} ...")
        sys.exit(f"merged tensor names differ from the base ({len(want - got)} missing, {len(got - want)} extra)")
    print(f"merged model saved to {out}: {len(got)} tensors, names identical to the r3 base")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "train", "mm-eval", "text-eval", "merge"])
    ap.add_argument("--base", required=True, help="the merged r3 SFT model (bf16 folder)")
    ap.add_argument("--prompts", help="grpo_prompts.jsonl from export_prompts.py")
    ap.add_argument("--out")
    ap.add_argument("--adapter", help="merge / mm-eval: a checkpoint-N or adapter-final dir")
    ap.add_argument("--data", help="merge: the r3 SFT data dir (dev.jsonl)")
    ap.add_argument("--train-py", help="path to brev/train.py (default: next to this file, ../brev, ~/wisp)")
    ap.add_argument("--resume")
    ap.add_argument("--profile", choices=list(P.PROFILES), default="a6000", help="hardware profile: batch shape and lr defaults")
    ap.add_argument("--G", type=int, default=0, help="completions per prompt (default: the profile's)")
    ap.add_argument("--ppr", type=int, default=0, help="prompts per GPU per optimizer step (default: the profile's)")
    ap.add_argument("--micro-bs", type=int, default=0, help="completions per forward/backward; divides G (default: the profile's)")
    ap.add_argument("--n", type=int, default=0, help="train on the first N prompts (0 = all)")
    ap.add_argument("--max-steps", type=int, default=0, help="stop after this many optimizer steps")
    ap.add_argument("--lr", type=float, default=0.0, help="default: the profile's (5e-6 A6000, 8e-6 for the 2.7x larger B300 batch)")
    ap.add_argument("--alpha", type=int, default=32, help="LoRA alpha (r is 16)")
    ap.add_argument("--warmup", type=float, default=0.05, help="linear warmup, fraction of the steps")
    ap.add_argument("--max-grad-norm", type=float, default=0.3)
    ap.add_argument("--beta", type=float, default=0.04, help="KL strength on text prompts")
    ap.add_argument("--beta-mm", type=float, default=0.2, help="KL strength on vision/audio prompts")
    ap.add_argument("--mm-ref", default="adapter",
                    help="reference for multimodal prompts: 'adapter' (adapter disabled = r3 SFT, free) or 'base:DIR' "
                         "(a second frozen model, e.g. the untuned Gemma; +16 GB per GPU)")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--mm-temperature", type=float, default=0.7, help="sampling temperature on vision/audio prompts (production decodes at 0)")
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--top-k", type=int, default=64)
    ap.add_argument("--round", type=int, default=0, choices=(0, 3, 4, 5),
                    help="reward round: 4 switches on reward.py's round-4 terms (answer facts +1.0, ungrounded facts, thinking free to 120 "
                         "words, writes before reads, leaked internals); 5 is the r4-RL reward (RL-SCOPE) with the text probes and the "
                         "per-step logs. 0 = whatever the prompt labels say (export_prompts.py --round N stamps label.round = N), "
                         "else REWARD_ROUND / 3. A flag that disagrees with the labels of --prompts or --text-dev exits.")
    ap.add_argument("--max-completion", type=int, default=0, help="completion cap in tokens (default 384; 512 in round 4: a thought of up to 120 words plus the answer; "
                    "1024 in round 5 until the constant loss normaliser and truncation masking have landed)")
    ap.add_argument("--adv-floor", type=float, default=0.1, help="std floor in the advantage, text prompts")
    ap.add_argument("--adv-floor-mm", type=float, default=0.25, help="std floor in the advantage, vision/audio prompts")
    ap.add_argument("--mm-root", default=str(HERE / "mm"), help="folder holding vision/ and audio/ as export_prompts.py packed them")
    ap.add_argument("--mm-dev", help="held-out multimodal rows (default: grpo_mm_dev.jsonl next to --prompts)")
    ap.add_argument("--no-guard", action="store_true")
    ap.add_argument("--mm-floor", help="JSON printed by `grpo.py mm-eval --base <UNTUNED Gemma> --mm-dev ...` (no adapter): "
                    "the guard also fails when a checkpoint is worse than the untuned model, not only worse than the SFT")
    ap.add_argument("--allow-sft-regression", action="store_true",
                    help="start even though the r3 SFT is already below --mm-floor (the floor still stops later regressions)")
    ap.add_argument("--eval-every", type=int, default=25)
    ap.add_argument("--save-every", type=int, default=25)
    ap.add_argument("--guard-patience", type=int, default=None, help="consecutive failing multimodal evals that stop the run (default 2; 1 in round 5: greedy, deterministic)")
    ap.add_argument("--guard-margin", type=float, default=None, help="mean-reward margin of the multimodal guard (default 0.05; 0.03 in round 5)")
    ap.add_argument("--text-dev", help="round 5: grpo_text_dev.jsonl (or its smoke30 subset): the held-out text rows the probes decode. text-eval and train")
    ap.add_argument("--text-floor", help="round 5, train: `text-eval` output of the UNTUNED base on the same dev file (the same-run base)")
    ap.add_argument("--text-replicates", nargs="+", help="round 5, train: at least two `text-eval` outputs of the SFT (different --seed-offset)")
    ap.add_argument("--seed-offset", type=int, default=0, help="text-eval: added to --seed to key the draws of this replicate")
    ap.add_argument("--no-text-probes", action="store_true", help="round 5: train without the text-probe stop rules (gives up the S-* tripwires)")
    ap.add_argument("--no-probe-chat-kl", action="store_true", help="skip the chat-row KL measure in the text probe (two extra forwards per chat sample)")
    ap.add_argument("--probe-single-eval", action="store_true", help="force single-eval HARD count thresholds (implied when --text-dev is a subset of the replicates' rows)")
    ap.add_argument("--sim-paths", type=int, default=600, help="paths of the family-wise false-stop simulation (RL-SCOPE 8.2)")
    ap.add_argument("--dry-run-grad", action="store_true", help="train: build the model and LoRA, check the adapter layout, run one backward and assert every wrapped module got a gradient; no training")
    ap.add_argument("--grad-ratio-every", type=int, default=0, help="round 5: every N steps also measure the PG and KL gradient norms per bucket (two extra backward passes per micro-batch)")
    ap.add_argument("--learn-delta", type=float, help="S-learn: the null-calibrated rise in composition-adjusted reward (M6b); with --zv0-fail and --zv0-total it makes S-learn a stop at 400 prompts")
    ap.add_argument("--zv0-fail", type=float, help="S-learn: the M6b baseline all-fail zero-variance share over PG-bearing text groups")
    ap.add_argument("--zv0-total", type=float, help="S-learn: the M6b baseline total zero-variance share over PG-bearing text groups")
    ap.add_argument("--grader-delta", type=float, help="S-grader: the null-calibrated rise of the train reward; set to enable the S-grader stop")
    ap.add_argument("--max-len", type=int, default=10240)
    ap.add_argument("--seed", type=int, default=20261101)
    return ap


def resolve_round(a) -> int:
    """Set reward.ROUND from --round, the prompt labels (label.round) or REWARD_ROUND, and refuse a mix-up: scoring
    round-4 labels with round-3 rules (or the reverse) would train toward the wrong reward without any error.
    --prompts and --text-dev are both read: each must carry one round, the text dev rows must be stamped (an unstamped or
    mixed dev file exits), and the two files and --round must agree."""
    seen = label_rounds(a.prompts)
    if len(seen) > 1:
        sys.exit(f"{a.prompts}: rows carry different reward rounds {sorted(seen, key=str)} (None = unstamped)")
    text_dev = getattr(a, "text_dev", None)
    seen_t = label_rounds(text_dev)
    if text_dev and Path(text_dev).exists():
        if len(seen_t) > 1 or None in seen_t:
            sys.exit(f"{text_dev}: rows carry unstamped or mixed reward rounds {sorted(seen_t, key=str)} (None = unstamped); "
                     f"export the text dev file with the same --round as the prompts")
        if seen and seen != seen_t:
            sys.exit(f"{a.prompts} is round {sorted(seen, key=str)} but {text_dev} is round {sorted(seen_t, key=str)}: they must be scored alike")
    labelled = (seen or seen_t or {None}).copy().pop()
    src = a.prompts if seen else text_dev
    if a.round and labelled and a.round != labelled:
        sys.exit(f"--round {a.round} but {src} was exported for round {labelled}")
    W.ROUND = a.round or labelled or W.ROUND
    if not a.max_completion:
        a.max_completion = 1024 if W.ROUND >= 5 else 512 if W.ROUND >= 4 else 384
    if a.guard_patience is None:
        a.guard_patience = 1 if W.ROUND >= 5 else 2
    if a.guard_margin is None:
        a.guard_margin = 0.03 if W.ROUND >= 5 else 0.05
    return W.ROUND


def main() -> int:
    ap = build_parser()
    a = ap.parse_args()
    resolve_round(a)
    if a.cmd in ("check", "train") and not a.prompts:
        ap.error("--prompts is required")
    if a.cmd in ("train", "merge") and not a.out and not (a.cmd == "train" and a.dry_run_grad):
        ap.error("--out is required")
    if a.cmd == "merge" and not (a.adapter and a.data):
        ap.error("merge needs --adapter and --data")
    if a.cmd == "mm-eval" and not a.mm_dev:
        ap.error("mm-eval needs --mm-dev")
    if a.cmd == "text-eval" and not (a.text_dev and a.out):
        ap.error("text-eval needs --text-dev and --out")
    if a.cmd == "text-eval" and W.ROUND < 5:
        ap.error("text-eval scores at the round the dev rows are stamped with; pass a round-5 --text-dev (or --round 5)")
    if a.cmd == "train":
        check_train_args(a, mm_dev_rows_exist(a) and not a.no_guard)
    return {"check": cmd_check, "train": cmd_train, "mm-eval": cmd_mm_eval, "text-eval": cmd_text_eval, "merge": cmd_merge}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
