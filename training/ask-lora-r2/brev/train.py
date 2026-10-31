#!/usr/bin/env python3
"""Wisp Ask LoRA trainer for Gemma 4 E4B or Nemotron 3.5 Bolt E4B on one NVIDIA GPU (48 GB class).
Bolt is an RL post-train of Gemma 4 E4B with the same architecture and tokenizer, so one script
trains both; `--tag gemma|bolt` picks which (each run is a separate LoRA on the shared dataset).

    python train.py preflight --tag T --data DIR --base DIR            # template sha + render parity, lengths, stop tokens
    python train.py smoke     --tag T --data DIR --base DIR --out DIR  # the longest rows: peak memory + speed
    python train.py train     --tag T --data DIR --base DIR --out DIR [--resume DIR]
    python train.py merge     --tag T --base DIR --adapter DIR --data DIR --out DIR
    python train.py sample    --tag T --base DIR --adapter DIR --data DIR   # greedy decode of dev prompts, for a look

Rows are prompt/completion strings rendered on the Mac by brev/render.py with
Wisp's own chat template and each request's `chat_template_kwargs`, so the
prompt here is byte-for-byte what oMLX feeds the model in production. Loss is
on the completion only (an optional one-sentence thought, then the answer or
tool call, then its stop token).

Recipe (from Google's Gemma 4 QLoRA guide, Unsloth's Gemma 4 guide and the
community E4B write-ups; see brev/README.md for the sources):
- transformers >= 5.10.1 and peft >= 0.19. Before transformers 5.5.2,
  use_cache=False (which gradient checkpointing forces) broke E4B's 18
  KV-shared layers and gave garbage logits; smoke checks cache parity.
- LoRA r=16, alpha=16, dropout 0.05 on the language model's attention AND MLP
  projections. The vision/audio towers wrap their linears in
  Gemma4ClippableLinear, which PEFT cannot adapt, so the target regex is scoped
  to `language_model`.
- lr 2e-4, cosine, 3% warmup, grad-norm clip 0.3, 2 epochs, best-by-dev-loss.
- Train and merge with the same multimodal class (AutoModelForMultimodalLM),
  so adapter keys line up and the merged checkpoint keeps the vision and audio
  towers Wisp uses (image input, voice transcription). The merge refuses to
  save unless its tensor names equal the base's.
- Overfit a few rows before the real run (smoke does this).

Memory: Gemma's vocabulary is 262,144, so full logits for a 9.5k-token row are
~10 GiB in fp32 before their gradient. Each step asks for the completion
positions only (`logits_to_keep`), which is exact because the loss is
completion-only. Rows are weighted equally within an optimizer step, so the few
long answers don't drown the short tool calls.
"""
from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")  # before torch loads

import argparse
import hashlib
import inspect
import json
import math
import random
import shutil
import sys
import time
from pathlib import Path

STOP_IDS = {106, 50}             # <turn|>, <|tool_response>
N_LAYERS = 42
# Per-model template shas live in render.py (shipped in the data zip as render.py).
# Gemma's pinned sha equals the HF bf16 repo's, so a mismatch fails. Bolt's is the local quant's;
# the HF bf16 repo may differ, so there a mismatch warns and render parity decides.
STRICT_SHA_TAGS = {"gemma"}
# language-model attention + MLP only; the towers' ClippableLinear layers must not match
TARGET_RE = r".*language_model.*\.(self_attn\.(q_proj|k_proj|v_proj|o_proj)|mlp\.(gate_proj|up_proj|down_proj))"


def log(path: Path | None, **kv) -> None:
    line = json.dumps({"t": round(time.time(), 1), **kv})
    print(line, flush=True)
    if path:
        with path.open("a") as fh:
            fh.write(line + "\n")


# ------------------------------------------------------------------- data

def load_rows(data: Path, split: str) -> list[dict]:
    rows = [json.loads(l) for l in (data / f"{split}.jsonl").read_text().splitlines() if l.strip()]
    if not rows or not {"prompt", "completion"} <= set(rows[0]):
        sys.exit(f"{data}/{split}.jsonl: expected rows with prompt and completion")
    return rows


def tokenize(tok, rows: list[dict], max_len: int) -> tuple[list[tuple], list[str]]:
    out, skipped = [], []
    for r in rows:
        p = tok(r["prompt"], add_special_tokens=False).input_ids
        c = tok(r["completion"], add_special_tokens=False).input_ids
        if len(p) + len(c) > max_len:
            skipped.append(r["id"])
            continue
        out.append((r["id"], p, c))
    return out, skipped


def _load_render(data: Path):
    """Import render.py from the unpacked data dir (it travels in the zip next to parity.jsonl)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ask_render", Path(data) / "render.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _template_check(a, tok) -> int:
    """Per-model template sha, then render parity against the downloaded repo's OWN template.

    The pinned sha is that of the local oQ4e build. For Gemma it is also the HF bf16 repo's
    (checked), so a different sha fails. For Bolt the bf16 repo's template was never compared
    with the quant's, so a different sha only warns and the parity check decides. Parity is
    the binding gate for both: sample rows are re-rendered from their raw messages with the
    template the training run will actually load, and must equal the trained rows byte for byte."""
    rd = _load_render(a.data)
    want = rd.TEMPLATE_SHAS[a.tag]
    tpl = Path(a.base) / "chat_template.jinja"
    if tpl.is_file():
        got, where = hashlib.sha256(tpl.read_bytes()).hexdigest(), "chat_template.jinja"
    elif getattr(tok, "chat_template", None):
        got, where = hashlib.sha256(tok.chat_template.encode()).hexdigest(), "tokenizer_config.json (inline)"
    else:
        print("PREFLIGHT FAILED: the base has no chat template (no chat_template.jinja, none inline)")
        return 1
    print(f"template [{a.tag}] {where}: sha256 {got[:16]} (pinned for the local {a.tag} quant: {want[:16]})")
    problems = 0
    if got != want:
        if a.tag in STRICT_SHA_TAGS:
            print(f"PREFLIGHT FAILED: the {a.tag} base's template differs from the pinned one")
            problems += 1
        else:
            print(f"WARNING: the downloaded {a.tag} repo's template differs from the local quant's. "
                  f"Record sha {got} in the run notes; the parity check below decides whether the data fits it.")
    rows = [json.loads(l) for l in (Path(a.data) / "parity.jsonl").read_text().splitlines() if l.strip()]
    if not rows:
        print("PREFLIGHT FAILED: parity.jsonl is empty")
        return 1
    by_split = {s: {r["id"]: r for r in load_rows(Path(a.data), s)} for s in ("train", "dev")}
    done = [by_split[p["split"]][p["meta"]["id"]] for p in rows]
    bad = rd.parity_mismatches(tok, done, [(p["example"], p["meta"]) for p in rows])
    print(f"render parity vs the {a.tag} base's own template: {len(rows) - len(bad)}/{len(rows)} rows byte-identical")
    if bad:
        print(f"PREFLIGHT FAILED: {len(bad)} rows render differently under this template, e.g. {bad[:3]}")
        problems += 1
    return problems


def _arch_check(a) -> int:
    cfg = json.loads((Path(a.base) / "config.json").read_text())
    layers = (cfg.get("text_config") or cfg).get("num_hidden_layers")
    arch = cfg.get("architectures")
    print(f"config: architectures {arch}, language layers {layers}")
    if layers != N_LAYERS or arch != ["Gemma4ForConditionalGeneration"]:
        print(f"PREFLIGHT FAILED: expected Gemma4ForConditionalGeneration with {N_LAYERS} layers "
              "(the LoRA target regex, stop ids and the cache check assume it)")
        return 1
    return 0


def cmd_preflight(a) -> int:
    if not (Path(a.base) / "config.json").is_file():
        print("PREFLIGHT FAILED: the base has no config.json")
        return 1
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    if _arch_check(a) + _template_check(a, tok):
        print("PREFLIGHT FAILED: the base does not match the data - do not train")
        return 1
    problems = 0
    for split in ("train", "dev"):
        rows = load_rows(Path(a.data), split)
        toks, skipped = tokenize(tok, rows, a.max_len)
        kept = [r for r in rows if r["id"] not in set(skipped)]
        lens = sorted(len(p) + len(c) for _, p, c in toks)
        bad_stop = [i for i, _, c in toks if c[-1] not in STOP_IDS]
        # The ids must decode back to the exact text, or training would see a
        # sequence the model is never served.
        roundtrip = sum(tok.decode(p + c) != r["prompt"] + r["completion"] for (_, p, c), r in zip(toks, kept))
        problems += len(bad_stop) + roundtrip + len(skipped)
        labeled = sorted(len(c) for _, _, c in toks)
        print(f"{split}: {len(rows)} rows, {len(skipped)} over --max-len {a.max_len}, tokens max {lens[-1]} "
              f"median {lens[len(lens) // 2]}, labeled total {sum(labeled)} (median {labeled[len(labeled) // 2]}), "
              f"with thought {sum('<|channel>thought' in r['prompt'][-20:] for r in rows)}, "
              f"bad stop token {len(bad_stop)}, decode mismatches {roundtrip}")
    print("PREFLIGHT OK" if not problems else f"PREFLIGHT FAILED ({problems} problems) - do not train")
    return 1 if problems else 0


# ------------------------------------------------------------------ model

def _model_class():
    import transformers
    for name in ("AutoModelForMultimodalLM", "AutoModelForImageTextToText"):
        if hasattr(transformers, name):
            return getattr(transformers, name)
    sys.exit("this transformers has no multimodal auto class - need >= 5.10.1")


def load_model(a, *, for_training: bool = True):
    import torch
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    kw = {}
    if getattr(a, "qlora", False) and for_training:
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_quant_storage=torch.bfloat16)
    model = _model_class().from_pretrained(a.base, dtype=torch.bfloat16, attn_implementation="sdpa",
                                           device_map={"": 0}, **kw)
    print(f"loaded {type(model).__name__} params={sum(p.numel() for p in model.parameters()) / 1e9:.2f}B "
          f"gpu_alloc={torch.cuda.memory_allocated() / 2**30:.1f}GiB qlora={bool(kw)}")
    if not for_training:
        return tok, model
    from peft import LoraConfig, get_peft_model
    if kw:
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=False)
    cfg = LoraConfig(r=a.rank, lora_alpha=a.alpha, lora_dropout=0.05, target_modules=TARGET_RE, bias="none")
    model = get_peft_model(model, cfg)
    names = [n for n, _ in model.named_modules() if n.endswith(".lora_A")]
    lm = [n for n in names if "language_model" in n]
    q = [n for n in lm if ".self_attn.q_proj." in n]
    if len(q) != N_LAYERS or len(lm) != len(names):
        sys.exit(f"LoRA targeting is wrong: {len(q)} language q_proj (want {N_LAYERS}), "
                 f"{len(names) - len(lm)} adapters outside the language model")
    model.print_trainable_parameters()
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model.train()  # checkpointing only runs in training mode
    return tok, model


_EXTRA: dict | None = None


def _forward_kwargs(model, n: int, dev) -> dict:
    """Text-only rows: zero modality masks if this transformers' forward asks for them."""
    global _EXTRA
    if _EXTRA is None:
        base = model.get_base_model() if hasattr(model, "get_base_model") else model
        params = inspect.signature(base.forward).parameters
        _EXTRA = {k for k in ("token_type_ids", "mm_token_type_ids") if k in params}
    import torch
    return {k: torch.zeros((1, n), dtype=torch.long, device=dev) for k in _EXTRA}


def row_loss(model, p, c, dev, use_cache=False):
    """Mean CE over the completion, computing logits for its positions only."""
    import torch
    import torch.nn.functional as F
    ids = torch.tensor([p + c], device=dev)
    k = len(c) + 1                      # positions P-1 .. L-1; the last one predicts past the end
    out = model(input_ids=ids, logits_to_keep=k, use_cache=use_cache, **_forward_kwargs(model, ids.shape[1], dev))
    logits = out.logits[0, -k:-1].float()   # P-1 .. L-2 predict c[0..C-1]; works even if k was ignored
    target = torch.tensor(c, device=dev)
    loss = F.cross_entropy(logits, target)
    acc = (logits.argmax(-1) == target).float().mean()
    return loss, acc, out.logits.shape[1] == k


def evaluate(model, rows, dev) -> dict:
    import torch
    model.eval()
    losses, accs, exact = [], [], 0
    with torch.no_grad():
        for _, p, c in rows:
            loss, acc, _ = row_loss(model, p, c, dev)
            losses.append(loss.item())
            accs.append(acc.item())
            exact += acc.item() == 1.0
    model.train()
    return {"dev_loss": round(sum(losses) / len(losses), 4), "dev_token_acc": round(sum(accs) / len(accs), 4),
            "dev_exact_rows": f"{exact}/{len(rows)}"}


def cmd_smoke(a) -> int:
    """Cache parity, then the N longest training rows (forward+backward each, one
    optimizer step) for peak memory and speed, then an overfit check on 2 rows.

    Writes OUT/smoke.json. Exit 0 = OK, 3 = memory tight, 4 = out of memory,
    5 = KV-sharing bug, 6 = cannot overfit.
    The first row is excluded from the speed figure (CUDA warm-up)."""
    import torch
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tok, model = load_model(a)
    toks, _ = tokenize(tok, load_rows(Path(a.data), "train"), a.max_len)
    dev, _ = tokenize(tok, load_rows(Path(a.data), "dev"), a.max_len)
    # 1. KV-sharing parity: E4B's shared layers read K/V through the cache, and
    #    old transformers broke them when use_cache=False. Loss must not depend on it.
    model.eval()
    with torch.no_grad():
        for _, p, c in dev[:3]:
            on, off = row_loss(model, p, c, 0, True)[0].item(), row_loss(model, p, c, 0, False)[0].item()
            print(f"  cache parity: loss use_cache=True {on:.4f} vs False {off:.4f}")
            if abs(on - off) > 0.05 or off > 8:
                print("SMOKE FAILED: use_cache changes the loss (KV-sharing bug) - upgrade transformers")
                return 5
    model.train()
    toks.sort(key=lambda t: -(len(t[1]) + len(t[2])))
    worst = toks[: a.smoke_rows]
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=a.lr, fused=True)
    torch.cuda.reset_peak_memory_stats()
    total = torch.cuda.get_device_properties(0).total_memory / 2**30
    res = {"max_len": a.max_len, "ok": False}
    t0, timed = time.time(), 0
    try:
        for i, (rid, p, c) in enumerate(worst):
            if i == 1:
                torch.cuda.synchronize()
                t0 = time.time()
            loss, _, kept = row_loss(model, p, c, 0)
            (loss / len(worst)).backward()
            if i >= 1:
                timed += len(p) + len(c)
            res["logits_to_keep"] = kept
            print(f"  row {i + 1}/{len(worst)} {rid}: {len(p) + len(c)} tokens, loss {loss.item():.3f}, "
                  f"peak {torch.cuda.max_memory_allocated() / 2**30:.1f}GiB"
                  + ("" if kept else "  (WARNING: model ignored logits_to_keep - full logits computed)"))
        opt.step()
        torch.cuda.synchronize()
    except torch.cuda.OutOfMemoryError:
        res.update(reason="oom", peak_gib=round(torch.cuda.max_memory_allocated() / 2**30, 1))
        (out / "smoke.json").write_text(json.dumps(res, indent=2))
        print(f"SMOKE: CUDA OUT OF MEMORY at --max-len {a.max_len}")
        return 4
    dt = time.time() - t0
    peak, reserved = torch.cuda.max_memory_allocated() / 2**30, torch.cuda.max_memory_reserved() / 2**30
    # 2. Overfit two short rows: the loss must collapse, or the pipeline is broken
    #    (masking, cache, targeting) and the real run would waste the GPU.
    tiny = sorted(toks, key=lambda t: len(t[1]) + len(t[2]))[:2]
    first = last = None
    for i in range(a.overfit_steps):
        opt.zero_grad(set_to_none=True)
        tot = 0.0
        for _, p, c in tiny:
            loss, _, _ = row_loss(model, p, c, 0)
            (loss / len(tiny)).backward()
            tot += loss.item() / len(tiny)
        opt.step()
        first = tot if first is None else first
        last = tot
    print(f"  overfit check: loss {first:.3f} -> {last:.3f} over {a.overfit_steps} steps on 2 rows")
    res["overfit"] = [round(first, 3), round(last, 3)]
    if not last < first * 0.5:
        (out / "smoke.json").write_text(json.dumps(res, indent=2))
        print("SMOKE FAILED: the model cannot overfit two rows - check masking and targeting before training")
        return 6
    tok_s = timed / dt
    all_tok = sum(len(p) + len(c) for _, p, c in toks)
    dev_tok = sum(len(p) + len(c) for _, p, c in dev)
    epoch_min = all_tok / tok_s / 60
    steps = math.ceil(len(toks) / a.accum)
    eval_min = dev_tok / tok_s / 60 / 3 * max(1, steps // a.eval_every)   # forward-only ≈ a third of a train token
    headroom = total - reserved
    res.update(ok=headroom >= 4, reason="" if headroom >= 4 else "tight", tok_s=round(tok_s),
               peak_gib=round(peak, 1), reserved_gib=round(reserved, 1), total_gib=round(total, 1),
               epoch_min=round(epoch_min, 1), eval_min_per_epoch=round(eval_min, 1), rows=len(toks))
    (out / "smoke.json").write_text(json.dumps(res, indent=2))
    print(f"SMOKE: {len(worst)} longest rows, {tok_s:.0f} tok/s; peak allocated {peak:.1f}GiB, reserved "
          f"{reserved:.1f}GiB of {total:.0f}GiB; one epoch ~{epoch_min + eval_min:.0f} min incl. evals "
          f"({a.epochs} epochs ~{(epoch_min + eval_min) * a.epochs:.0f} min) over {len(toks)} rows")
    print("MEMORY OK" if headroom >= 4 else f"MEMORY TIGHT: {headroom:.1f}GiB headroom - lower --max-len")
    return 0 if headroom >= 4 else 3


# ------------------------------------------------------------------ train

def cmd_train(a) -> int:
    import torch
    from transformers import get_cosine_schedule_with_warmup
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    logf = out / "train.log.jsonl"
    tok, model = load_model(a)
    train, skipped = tokenize(tok, load_rows(Path(a.data), "train"), a.max_len)
    dev, _ = tokenize(tok, load_rows(Path(a.data), "dev"), a.max_len)
    if skipped:
        print(f"skipping {len(skipped)} train rows over --max-len {a.max_len}")
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=0.0, fused=True)
    steps_per_epoch = math.ceil(len(train) / a.accum)
    total = steps_per_epoch * a.epochs
    sched = get_cosine_schedule_with_warmup(opt, max(1, round(0.03 * total)), total)
    state = {"step": 0, "best": None}
    if a.resume:
        from peft import set_peft_model_state_dict
        from safetensors.torch import load_file
        r = Path(a.resume)
        set_peft_model_state_dict(model, load_file(str(r / "adapter_model.safetensors")))
        ck = torch.load(r / "trainer_state.pt", map_location="cpu", weights_only=False)
        opt.load_state_dict(ck["opt"])
        sched.load_state_dict(ck["sched"])
        state = ck["state"]
        print(f"resumed from {r} at optimizer step {state['step']}")
    else:
        log(logf, event="eval", step=0, **evaluate(model, dev, 0), best=None)   # the baseline the run must beat
    log(logf, event="start", rows=len(train), dev=len(dev), epochs=a.epochs, accum=a.accum, steps=total,
        lr=a.lr, rank=a.rank, alpha=a.alpha, max_len=a.max_len, skipped=len(skipped))

    def save(tag: str) -> Path:
        d = out / tag
        model.save_pretrained(d)
        torch.save({"opt": opt.state_dict(), "sched": sched.state_dict(), "state": state}, d / "trainer_state.pt")
        return d

    def checkpoint_and_eval(event: str) -> None:
        ev = evaluate(model, dev, 0)
        d = save(f"step-{state['step']:05d}")
        if state["best"] is None or ev["dev_loss"] < state["best"][1]:
            state["best"] = (d.name, ev["dev_loss"])
            shutil.rmtree(out / "best", ignore_errors=True)
            shutil.copytree(d, out / "best")
        log(logf, event=event, step=state["step"], of=total, **ev, best=state["best"])
        for old in sorted(out.glob("step-*"))[:-3]:
            shutil.rmtree(old, ignore_errors=True)

    model.train()
    t0, tok_count, step0 = time.time(), 0, state["step"]
    while state["step"] < total:
        epoch = state["step"] // steps_per_epoch
        order = list(range(len(train)))
        random.Random(a.seed + epoch).shuffle(order)
        start = (state["step"] % steps_per_epoch) * a.accum
        stop = False
        for b in range(start, len(order), a.accum):
            batch = [train[i] for i in order[b:b + a.accum]]
            loss_sum = acc_sum = 0.0
            for rid, p, c in batch:
                try:
                    loss, acc, _ = row_loss(model, p, c, 0)
                except torch.cuda.OutOfMemoryError:
                    save("oom-recovery")
                    sys.exit(f"CUDA OOM on {rid} ({len(p) + len(c)} tokens). Adapter saved to {out}/oom-recovery; "
                             f"rerun with a lower --max-len and --resume {out}/oom-recovery")
                (loss / len(batch)).backward()
                loss_sum += loss.item()
                acc_sum += acc.item()
                tok_count += len(p) + len(c)
            gnorm = torch.nn.utils.clip_grad_norm_(params, 0.3).item()
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            state["step"] += 1
            el = time.time() - t0
            log(logf, step=state["step"], of=total, epoch=epoch + 1, loss=round(loss_sum / len(batch), 4),
                token_acc=round(acc_sum / len(batch), 4), grad_norm=round(gnorm, 3),
                lr=f"{sched.get_last_lr()[0]:.2e}", tok_s=round(tok_count / el), s_per_step=round(el / max(1, state["step"] - step0), 1),
                peak_gib=round(torch.cuda.max_memory_allocated() / 2**30, 1),
                eta_min=round(el / max(1, state["step"] - step0) * (total - state["step"]) / 60))
            if state["step"] % a.eval_every == 0 or state["step"] == total:
                checkpoint_and_eval("eval")
            if state["step"] >= total:
                break
            if a.stop_at and time.time() >= a.stop_at:
                checkpoint_and_eval("stopped_at_deadline")
                stop = True
                break
        if stop:
            break
    save("final")
    log(logf, event="done", best=state["best"], minutes=round((time.time() - t0) / 60, 1))
    return 0


def _base_keys(base: Path) -> set[str]:
    idx = base / "model.safetensors.index.json"
    if idx.exists():
        return set(json.load(open(idx))["weight_map"])
    from safetensors import safe_open
    keys = set()
    for f in base.glob("*.safetensors"):
        with safe_open(str(f), "pt") as fh:
            keys |= set(fh.keys())
    return keys


def cmd_merge(a) -> int:
    """Fold the adapter into the bf16 base and save the full multimodal model.

    Refuses to save when the adapter has no effect, when merging changes the
    output, or when the saved tensor names differ from the base's (a renamed or
    dropped vision/audio tensor would be silently re-initialized by a loader)."""
    import torch
    from peft import PeftModel
    tok, base = load_model(a, for_training=False)
    model = PeftModel.from_pretrained(base, a.adapter)
    model.eval()
    _, p, c = tokenize(tok, load_rows(Path(a.data), "dev"), a.max_len)[0][0]
    with torch.no_grad():
        with model.disable_adapter():
            plain = row_loss(model, p, c, 0)[0].item()
        before = row_loss(model, p, c, 0)[0].item()
    merged = model.merge_and_unload()
    with torch.no_grad():
        after = row_loss(merged, p, c, 0)[0].item()
    print(f"merge check: dev-row loss base {plain:.4f}, adapter {before:.4f}, merged {after:.4f}")
    if abs(before - plain) < 1e-4:
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
    want, got = _base_keys(Path(a.base)), _base_keys(out)
    if want != got:
        print(f"missing {sorted(want - got)[:5]} … extra {sorted(got - want)[:5]} …")
        sys.exit(f"merged tensor names differ from the base ({len(want - got)} missing, {len(got - want)} extra)")
    print(f"merged model saved to {out}: {len(got)} tensors, names identical to the base")
    return 0


def cmd_sample(a) -> int:
    """Greedy-decode a few dev prompts with the adapter, next to the target."""
    import torch
    from peft import PeftModel
    tok, base = load_model(a, for_training=False)
    model = PeftModel.from_pretrained(base, a.adapter) if a.adapter else base
    model.eval()
    rows = load_rows(Path(a.data), "dev")
    random.Random(0).shuffle(rows)
    for r in rows[: a.n]:
        ids = tok(r["prompt"], add_special_tokens=False, return_tensors="pt").input_ids.to(0)
        with torch.no_grad():
            gen = model.generate(input_ids=ids, max_new_tokens=200, do_sample=False,
                                 eos_token_id=sorted(STOP_IDS | {1}), **_forward_kwargs(model, ids.shape[1], 0))
        text = tok.decode(gen[0, ids.shape[1]:])
        print(f"== {r['id']} ({r['family']}, {r['kind']})\n  want: {r['completion'][:300]!r}\n  got:  {text[:300]!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "smoke", "train", "merge", "sample"])
    ap.add_argument("--data", help="dir with train.jsonl and dev.jsonl (from ask-lora-data.zip)")
    ap.add_argument("--tag", required=True, choices=["gemma", "bolt"],
                    help="which model this run trains (run.sh passes it); selects the pinned template sha")
    ap.add_argument("--base", required=True, help="bf16 snapshot dir of that model (Gemma 4 E4B-it or Nemotron 3.5 Bolt E4B)")
    ap.add_argument("--out")
    ap.add_argument("--adapter", help="merge/sample: adapter dir (e.g. OUT/best)")
    ap.add_argument("--resume", help="train: a step-NNNNN or oom-recovery dir")
    ap.add_argument("--max-len", type=int, default=10240, help="rows longer than this are skipped (longest is ~9.5k)")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--accum", type=int, default=8, help="rows per optimizer step (micro batch is always 1)")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--eval-every", type=int, default=26, help="optimizer steps between dev evals (~4 per epoch)")
    ap.add_argument("--qlora", action="store_true", help="4-bit NF4 base (for a 24 GB GPU); merge stays bf16")
    ap.add_argument("--overfit-steps", type=int, default=15, help="smoke: steps for the 2-row overfit check")
    ap.add_argument("--smoke-rows", type=int, default=8)
    ap.add_argument("--stop-at", type=float, default=0, help="train: unix time to stop at (saves + evals first)")
    ap.add_argument("--n", type=int, default=8, help="sample: how many dev prompts")
    ap.add_argument("--seed", type=int, default=20261029)
    a = ap.parse_args()
    return {"preflight": cmd_preflight, "smoke": cmd_smoke, "train": cmd_train, "merge": cmd_merge,
            "sample": cmd_sample}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
