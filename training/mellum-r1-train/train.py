#!/usr/bin/env python3
"""Mellum R1 LoRA trainer for one NVIDIA A6000 48GB.

    python train.py preflight --data DIR --base DIR            # tokenizer only: lengths, mask, template
    python train.py smoke     --data DIR --base DIR --out DIR  # the longest rows: peak memory + speed
    python train.py train     --data DIR --base DIR --out DIR [--resume DIR]
    python train.py merge     --base DIR --adapter DIR --out DIR

Rows are the dataset's rendered `prompt` / `completion` strings (Mellum's own
chat template, built by training/mellum-dataset-r1-20261014/build.py). The
prompt and completion are tokenized separately, which is how inference sees
them: the prompt is the prefix and the model generates the completion.

Loss is on the completion tokens only, so labels are exact by construction.

Memory: the loss only needs logits for the completion (median 36 tokens).
Each step asks the model for those positions alone (`logits_to_keep`) instead
of the whole sequence. At 12k tokens x 98,304 vocab the full logits are 2.2 GiB
in bf16, 4.5 GiB in fp32, and the same again for their gradient; the suffix is
well under 0.5 GiB.

Each row's loss is the mean over its completion tokens, and rows are averaged
equally within an optimizer step. Token weighting would let the few long
reasoning rows dominate the short tool-call rows.
"""
from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")  # before torch loads

import argparse
import json
import math
import random
import shutil
import sys
import time
from pathlib import Path

EOS_ID = 28                      # <|im_end|>
TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj"]
N_LAYERS = 28


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


def cmd_preflight(a) -> int:
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base)
    problems = 0
    tpl = Path(a.base) / "chat_template.jinja"
    if tpl.exists():
        import hashlib
        sha = hashlib.sha256(tpl.read_bytes()).hexdigest()
        ok = sha == a.template_sha
        problems += not ok
        print(f"chat_template.jinja sha256 {sha[:12]}… {'matches' if ok else 'DIFFERS FROM'} the template the data was rendered with")
    for split in ("train", "dev"):
        rows = load_rows(Path(a.data), split)
        toks, skipped = tokenize(tok, rows, a.max_len)
        lens = sorted(len(p) + len(c) for _, p, c in toks)
        labeled = [len(c) for _, _, c in toks]
        bad_eos = [i for i, _, c in toks if c[-1] != EOS_ID]
        thinks = sum(r["completion"].startswith("<think>") for r in rows)
        # Decoding prompt+completion ids must give back the exact text, or the
        # model would be trained on something it never sees at inference.
        roundtrip = sum(tok.decode(p + c) != r["prompt"] + r["completion"]
                        for (_, p, c), r in zip(toks, [x for x in rows if x["id"] not in set(skipped)]))
        problems += len(bad_eos) + roundtrip + len(skipped)
        q = lambda f: lens[min(len(lens) - 1, int(f * len(lens)))]  # noqa: E731
        print(f"{split}: {len(rows)} rows, {len(skipped)} over --max-len {a.max_len}, "
              f"tokens max {lens[-1]} p95 {q(.95)} median {q(.5)}, labeled tokens total {sum(labeled)} "
              f"(median {sorted(labeled)[len(labeled) // 2]}), thinking rows {thinks}, "
              f"completion not ending in <|im_end|>: {len(bad_eos)}, decode mismatches: {roundtrip}")
        if skipped:
            print("  over length:", skipped[:5])
    print("PREFLIGHT OK" if not problems else f"PREFLIGHT FAILED ({problems} problems) - do not train")
    return 1 if problems else 0


# ------------------------------------------------------------------ model

def load_model(a, *, for_training: bool = True):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    try:
        import flash_attn  # noqa: F401
        impl = "flash_attention_2"    # supports Mellum's 1024-token sliding window natively
    except ImportError:
        impl = "sdpa"
    tok = AutoTokenizer.from_pretrained(a.base)
    model = AutoModelForCausalLM.from_pretrained(a.base, dtype=torch.bfloat16, attn_implementation=impl,
                                                 device_map={"": 0})
    model.config.use_cache = False
    print(f"loaded {a.base} attn={impl} params={sum(p.numel() for p in model.parameters()) / 1e9:.2f}B "
          f"gpu_alloc={torch.cuda.memory_allocated() / 2**30:.1f}GiB")
    if not for_training:
        return tok, model
    from peft import LoraConfig, get_peft_model
    cfg = LoraConfig(r=a.rank, lora_alpha=a.alpha, lora_dropout=0.05, target_modules=TARGETS,
                     task_type="CAUSAL_LM", bias="none")
    model = get_peft_model(model, cfg)
    hit = [n for n, _ in model.named_modules() if n.endswith(tuple(f"self_attn.{t}" for t in TARGETS))]
    if len(hit) != N_LAYERS * len(TARGETS):
        sys.exit(f"expected {N_LAYERS * len(TARGETS)} attention projections, LoRA matched {len(hit)}: {hit[:4]}")
    model.print_trainable_parameters()
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    return tok, model


def row_loss(model, p, c, dev):
    """Mean CE over the completion, computing logits for its positions only."""
    import torch
    import torch.nn.functional as F
    ids = torch.tensor([p + c], device=dev)
    k = len(c) + 1                      # positions P-1 .. L-1; the last one predicts past the end
    out = model(input_ids=ids, logits_to_keep=k, use_cache=False)
    logits = out.logits[0, -k:-1].float()   # P-1 .. L-2 predict c[0..C-1]; works even if the model ignored k
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
    """The N longest training rows, forward+backward each, one optimizer step."""
    import torch
    tok, model = load_model(a)
    toks, _ = tokenize(tok, load_rows(Path(a.data), "train"), a.max_len)
    toks.sort(key=lambda t: -(len(t[1]) + len(t[2])))
    worst = toks[: a.smoke_rows]
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=a.lr, fused=True)
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    for i, (rid, p, c) in enumerate(worst):
        loss, _, kept = row_loss(model, p, c, 0)
        (loss / len(worst)).backward()
        print(f"  row {i + 1}/{len(worst)} {rid}: {len(p) + len(c)} tokens, loss {loss.item():.3f}, "
              f"peak {torch.cuda.max_memory_allocated() / 2**30:.1f}GiB"
              + ("" if kept else "  (WARNING: model ignored logits_to_keep - full logits computed)"))
    opt.step()
    dt = time.time() - t0
    peak, reserved = torch.cuda.max_memory_allocated() / 2**30, torch.cuda.max_memory_reserved() / 2**30
    total = torch.cuda.get_device_properties(0).total_memory / 2**30
    ntok = sum(len(p) + len(c) for _, p, c in worst)
    all_tok = sum(len(p) + len(c) for _, p, c in toks)
    est = dt / ntok * all_tok * a.epochs / 60
    print(f"SMOKE: {len(worst)} longest rows ({ntok} tokens) in {dt:.0f}s = {ntok / dt:.0f} tok/s; "
          f"peak allocated {peak:.1f}GiB, reserved {reserved:.1f}GiB of {total:.0f}GiB; "
          f"full run estimate ~{est:.0f} min for {a.epochs} epochs over {len(toks)} rows")
    headroom = total - reserved
    print("MEMORY OK" if headroom >= 4 else f"MEMORY TIGHT: {headroom:.1f}GiB headroom - lower --max-len (README)")
    return 0


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
    log(logf, event="start", rows=len(train), dev=len(dev), epochs=a.epochs, accum=a.accum, steps=total,
        lr=a.lr, rank=a.rank, alpha=a.alpha, max_len=a.max_len, skipped=len(skipped))

    def save(tag: str) -> Path:
        d = out / tag
        model.save_pretrained(d)
        torch.save({"opt": opt.state_dict(), "sched": sched.state_dict(), "state": state}, d / "trainer_state.pt")
        return d

    model.train()
    t0, tok_count, step0 = time.time(), 0, state["step"]
    while state["step"] < total:
        epoch = state["step"] // steps_per_epoch
        order = list(range(len(train)))
        random.Random(a.seed + epoch).shuffle(order)
        start = (state["step"] % steps_per_epoch) * a.accum
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
            gnorm = torch.nn.utils.clip_grad_norm_(params, 1.0).item()
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            state["step"] += 1
            el = time.time() - t0
            log(logf, step=state["step"], of=total, epoch=epoch + 1, loss=round(loss_sum / len(batch), 4),
                token_acc=round(acc_sum / len(batch), 4), grad_norm=round(gnorm, 3),
                lr=f"{sched.get_last_lr()[0]:.2e}", tok_s=round(tok_count / el),
                peak_gib=round(torch.cuda.max_memory_allocated() / 2**30, 1),
                eta_min=round(el / max(1, state["step"] - step0) * (total - state["step"]) / 60))
            if state["step"] % a.eval_every == 0 or state["step"] == total:
                ev = evaluate(model, dev, 0)
                d = save(f"step-{state['step']:05d}")
                if state["best"] is None or ev["dev_loss"] < state["best"][1]:
                    state["best"] = (d.name, ev["dev_loss"])
                    shutil.rmtree(out / "best", ignore_errors=True)
                    shutil.copytree(d, out / "best")
                log(logf, event="eval", step=state["step"], **ev, best=state["best"])
                steps = sorted(out.glob("step-*"))
                for old in steps[:-3]:
                    shutil.rmtree(old, ignore_errors=True)
            if state["step"] >= total:
                break
    save("final")
    log(logf, event="done", best=state["best"], minutes=round((time.time() - t0) / 60, 1))
    return 0


def cmd_merge(a) -> int:
    """Fold the adapter into the bf16 base and save it; checks the merge on a dev row."""
    import torch
    from peft import PeftModel
    tok, base = load_model(a, for_training=False)
    model = PeftModel.from_pretrained(base, a.adapter)
    model.eval()
    probe = None
    if a.data:
        _, p, c = tokenize(tok, load_rows(Path(a.data), "dev"), a.max_len)[0][0]
        with torch.no_grad():
            probe = (p, c, row_loss(model, p, c, 0)[0].item())
    merged = model.merge_and_unload()
    if probe:
        with torch.no_grad():
            after = row_loss(merged, probe[0], probe[1], 0)[0].item()
        print(f"merge check: dev-row loss adapter {probe[2]:.4f} vs merged {after:.4f}")
        if abs(after - probe[2]) > 0.02:
            sys.exit("merge changed the model's output - not saving")
    merged.save_pretrained(a.out, max_shard_size="5GB", safe_serialization=True)
    tok.save_pretrained(a.out)
    for f in ("chat_template.jinja", "generation_config.json"):
        if (Path(a.base) / f).exists():
            shutil.copy(Path(a.base) / f, Path(a.out) / f)
    print(f"merged model saved to {a.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "smoke", "train", "merge"])
    ap.add_argument("--data", help="dir with train.jsonl and dev.jsonl")
    ap.add_argument("--base", required=True, help="Mellum2.1-12B-A2.5B-Thinking bf16 snapshot dir")
    ap.add_argument("--out")
    ap.add_argument("--adapter", help="merge: adapter dir (e.g. OUT/best)")
    ap.add_argument("--resume", help="train: a step-NNNNN or oom-recovery dir")
    ap.add_argument("--max-len", type=int, default=12288, help="rows longer than this are skipped")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--accum", type=int, default=16, help="rows per optimizer step (micro batch is always 1)")
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--eval-every", type=int, default=50, help="optimizer steps between dev evals")
    ap.add_argument("--smoke-rows", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20261015)
    ap.add_argument("--template-sha", default="4593a34d52f3364ac13ce57f4bea5688924e8942da6df8201e411db17e729f48",
                    help="sha256 of the chat_template.jinja the data was rendered with")
    a = ap.parse_args()
    return {"preflight": cmd_preflight, "smoke": cmd_smoke, "train": cmd_train, "merge": cmd_merge}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
