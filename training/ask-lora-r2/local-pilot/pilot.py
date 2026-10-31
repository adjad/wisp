"""Local LoRA pilot on Apple silicon: measure s/iter, peak memory and the loss trend
for the Ask LoRA, on the local oQ4e build of either round-2 model (so this is QLoRA).

    python pilot.py --tag gemma|bolt --model <dir> --data <unzipped brev/ask-lora-data.zip> --out <dir> [--iters 50]

Run it once per model: results land in <out>/<tag>. The Bolt build has 8-bit attention, so
expect a higher peak than Gemma; compare the two peaks before assuming one number covers both.

It uses the rows brev/render.py produced: the exact production prompts
(enable_thinking, tools, short thoughts) plus completions. mlx-lm's own
`mlx_lm.lora` cannot pass enable_thinking to the template and computes logits
for every position (Gemma's 262k vocabulary makes that ~5 GB at 9.5k tokens),
so this is a small uncompiled loop on mlx-lm's pieces instead, with the same
LoRA recipe as brev/train.py: r=16, alpha=16 (scale 1.0), dropout 0.05 on the
language model's q/k/v/o and gate/up/down in all 42 layers, lr 2e-4 AdamW,
gradient checkpointing, batch 1. Loss is on the completion only, with logits
computed for those positions alone.

It also saves the adapter in mlx-lm's format and reloads it with
`mlx_lm.load(..., adapter_path=...)` for one greedy generation. Pilot only:
it trains 50 single-row steps, not the full run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
from mlx.utils import tree_flatten
from mlx_lm import load
from mlx_lm.tuner.trainer import grad_checkpoint
from mlx_lm.tuner.utils import linear_to_lora_layers

KEYS = ["self_attn.q_proj", "self_attn.k_proj", "self_attn.v_proj", "self_attn.o_proj",
        "mlp.gate_proj", "mlp.up_proj", "mlp.down_proj"]


def rows(data: Path, split: str) -> list[dict]:
    return [json.loads(l) for l in (data / f"{split}.jsonl").read_text().splitlines() if l.strip()]


def encode(tok, r):
    p = tok.encode(r["prompt"], add_special_tokens=False)
    c = tok.encode(r["completion"], add_special_tokens=False)
    return p, c


def completion_loss(model, ids, n_prompt):
    """Mean CE over the completion; only its positions go through the vocab projection."""
    lm = model.language_model
    h = lm.model(ids[:, :-1])                       # hidden states, (1, L-1, d)
    h = h[:, n_prompt - 1:]                          # positions that predict completion tokens
    logits = lm.model.embed_tokens.as_linear(h) if lm.tie_word_embeddings else lm.lm_head(h)
    if lm.final_logit_softcapping is not None:
        cap = lm.final_logit_softcapping
        logits = mx.tanh(logits / cap) * cap
    targets = ids[:, n_prompt:]
    return nn.losses.cross_entropy(logits.astype(mx.float32), targets).mean()


def dev_loss(model, dev):
    model.eval()
    tot = 0.0
    for p, c in dev:
        ids = mx.array([p + c])
        tot += completion_loss(model, ids, len(p)).item()
    model.train()
    return tot / len(dev)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, choices=["gemma", "bolt"],
                    help="which model --model is; its chat_template.jinja must match the pinned sha and "
                         "results go to <out>/<tag> so the two pilots never overwrite each other")
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--iters", type=int, default=50)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--dev-rows", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261029)
    a = ap.parse_args()
    # the rows were rendered with Gemma's template; the pilot model must be the local build of the tagged model
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brev"))
    from render import TEMPLATE_SHAS
    sha = hashlib.sha256((Path(a.model) / "chat_template.jinja").read_bytes()).hexdigest()
    if sha != TEMPLATE_SHAS[a.tag]:
        raise SystemExit(f"--model {a.model} has template sha {sha[:16]}, not the {a.tag} build "
                         f"({TEMPLATE_SHAS[a.tag][:16]}): wrong --tag or wrong directory")
    out = Path(a.out) / a.tag
    out.mkdir(parents=True, exist_ok=True)
    logf = (out / "pilot.log.jsonl").open("w")

    def log(**kv):
        line = json.dumps({"t": round(time.time(), 1), **kv})
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    t0 = time.time()
    model, tok = load(a.model)
    mx.eval(model.parameters())
    log(event="loaded", seconds=round(time.time() - t0, 1), active_gb=round(mx.get_active_memory() / 2**30, 2))
    model.freeze()
    lora = {"rank": 16, "scale": 1.0, "dropout": 0.05, "keys": KEYS}
    n_layers = len(model.layers)
    linear_to_lora_layers(model, n_layers, lora)
    trainable = sum(v.size for _, v in tree_flatten(model.trainable_parameters()))
    log(event="lora", layers=n_layers, trainable_params=trainable)
    grad_checkpoint(model.layers[0])

    train = [encode(tok, r) for r in rows(Path(a.data), "train")]
    dev_all = [encode(tok, r) for r in rows(Path(a.data), "dev")]
    rng = random.Random(a.seed)
    dev = rng.sample(dev_all, a.dev_rows)
    order = rng.sample(range(len(train)), a.iters)

    # worst case first: one forward+backward on the longest row, no update
    mx.reset_peak_memory()
    lp, lc = max(train, key=lambda t: len(t[0]) + len(t[1]))
    tic = time.perf_counter()
    loss, _ = nn.value_and_grad(model, completion_loss)(model, mx.array([lp + lc]), len(lp))
    mx.eval(loss, _)
    del _
    log(event="probe_longest", tokens=len(lp) + len(lc), s=round(time.perf_counter() - tic, 2),
        peak_gb=round(mx.get_peak_memory() / 2**30, 2))
    mx.clear_cache()
    base = dev_loss(model, dev)
    log(event="eval", step=0, dev_loss=round(base, 4))

    opt = optim.AdamW(learning_rate=a.lr, weight_decay=0.0)
    step_fn = nn.value_and_grad(model, completion_loss)
    tokens, t_train = 0, 0.0
    for i, idx in enumerate(order, 1):
        p, c = train[idx]
        ids = mx.array([p + c])
        tic = time.perf_counter()
        loss, grads = step_fn(model, ids, len(p))
        opt.update(model, grads)
        mx.eval(model.parameters(), opt.state, loss)
        dt = time.perf_counter() - tic
        t_train += dt
        tokens += len(p) + len(c)
        log(step=i, tokens=len(p) + len(c), loss=round(loss.item(), 4), s=round(dt, 2),
            tok_s=round(tokens / t_train), peak_gb=round(mx.get_peak_memory() / 2**30, 2))
    after = dev_loss(model, dev)
    log(event="eval", step=a.iters, dev_loss=round(after, 4))

    # mlx-lm adapter format, then reload through mlx_lm.load for a generation
    adapter = out / "adapter"
    adapter.mkdir(exist_ok=True)
    mx.save_safetensors(str(adapter / "adapters.safetensors"), dict(tree_flatten(model.trainable_parameters())))
    (adapter / "adapter_config.json").write_text(json.dumps(
        {"fine_tune_type": "lora", "num_layers": n_layers, "lora_parameters": lora}, indent=1))
    mean_s = t_train / a.iters
    full_rows = 2 * len(train)
    summary = {"tag": a.tag, "iters": a.iters, "mean_s_per_iter": round(mean_s, 2), "tok_s": round(tokens / t_train),
               "mean_tokens_per_row": round(tokens / a.iters), "peak_gb": round(mx.get_peak_memory() / 2**30, 2),
               "dev_loss_before": round(base, 4), "dev_loss_after": round(after, 4),
               "projected_full_run_hours_2_epochs": round(full_rows * mean_s / 3600, 1),
               "train_rows": len(train)}
    log(event="summary", **summary)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
