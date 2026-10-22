"""Reference side of the WispLing parity check.

Runs the same three prompts as the Swift ``ling-parity`` tool through the
Python mlx-lm implementation that oMLX actually serves Ling-3.0 with, and
prints one JSON object per prompt (same schema as the Swift tool).

oMLX does not use mlx-lm's own ``bailing_hybrid`` module (that one is Ling 2.6:
GLA linear attention). ``omlx.patches.bailing_hybrid.apply_bailing_hybrid_patch``
replaces ``mlx_lm.models.bailing_hybrid`` with its vendored Ling 3.0 module
(KDA linear attention + MLA + MoE, with the trained SwiGLU clamp) and removes
mlx-lm's ``bailing_hybrid -> bailing_moe_v3`` remapping. This script applies
exactly that patch before loading.

Run with oMLX's bundled interpreter (it is the bare CPython; the mlx stack lives
in framework-mlx-base/site-packages, which this script puts on sys.path):

    /Applications/oMLX.app/Contents/Resources/Python/framework-mlx-base/bin/python_ -I \
        parity/reference.py /path/to/Ling-3.0-tiny-Wisp-V3-merged-oQ4e > parity/reference.jsonl

Procedure (identical in Swift): render the chat template with
enable_thinking=False and add_generation_prompt=True, run the whole prompt in a
single forward pass with a fresh cache, report the top-5 logits of the last
position, then greedily decode 32 tokens one at a time (no EOS stop, so the
two sides are always compared over the full 32 positions).
"""

import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path

# Defaults: the installed oMLX (mlx 0.32.4). LING_REF_OMLX / LING_REF_SITE
# select another oMLX engine layout, e.g. an older bundled engine whose mlx
# core matches the one mlx-swift vendors (0.32.2):
#   LING_REF_OMLX=<engine> LING_REF_SITE=<engine>/sp <engine>/py/bin/python3 -I ...
OMLX_RESOURCES = Path(os.environ.get("LING_REF_OMLX", "/Applications/oMLX.app/Contents/Resources"))
SITE_PACKAGES = Path(
    os.environ.get(
        "LING_REF_SITE",
        str(OMLX_RESOURCES / "Python/framework-mlx-base/lib/python3.11/site-packages"),
    )
)
sys.path.insert(0, str(SITE_PACKAGES))
sys.path.insert(1, str(OMLX_RESOURCES))

import mlx.core as mx  # noqa: E402
import mlx_lm  # noqa: E402
from mlx_lm.utils import load  # noqa: E402

from omlx.patches.bailing_hybrid import apply_bailing_hybrid_patch  # noqa: E402

# Keep in sync with Sources/LingParity/main.swift.
PROMPTS = [
    ("greeting", [{"role": "user", "content": "Hi! Who are you?"}]),
    (
        "system_math",
        [
            {"role": "system", "content": "You are Wisp, a concise assistant."},
            {"role": "user", "content": "What is 17 * 23? Answer with just the number."},
        ],
    ),
    (
        "multi_turn",
        [
            {"role": "user", "content": "Give me one tip for better sleep."},
            {"role": "assistant", "content": "Keep a consistent bedtime, even on weekends."},
            {"role": "user", "content": "Why does that help? Explain briefly."},
        ],
    ),
]
GEN_TOKENS = 32


def main() -> None:
    model_dir = Path(sys.argv[1])
    applied = apply_bailing_hybrid_patch()
    print(
        f"# mlx {mx.__version__}, mlx_lm {mlx_lm.__version__}, bailing_hybrid patch applied={applied}",
        file=sys.stderr,
    )
    module = sys.modules["mlx_lm.models.bailing_hybrid"]
    print(f"# model module: {module.__file__}", file=sys.stderr)

    t0 = time.perf_counter()
    # trust_remote_code=False: the tokenizer is a plain PreTrainedTokenizerFast;
    # never run the checkpoint's modeling code (and never prompt on stdout).
    model, tokenizer = load(str(model_dir), tokenizer_config={"trust_remote_code": False})
    load_s = time.perf_counter() - t0
    chat_template = (model_dir / "chat_template.jinja").read_text()

    total_gen = 0
    total_gen_s = 0.0
    for name, messages in PROMPTS:
        prompt_ids = tokenizer.apply_chat_template(
            messages,
            chat_template=chat_template,
            add_generation_prompt=True,
            enable_thinking=False,
            tokenize=True,
        )
        if isinstance(prompt_ids, dict):
            prompt_ids = prompt_ids["input_ids"]
        prompt_ids = list(prompt_ids)
        prompt_text = tokenizer.apply_chat_template(
            messages,
            chat_template=chat_template,
            add_generation_prompt=True,
            enable_thinking=False,
            tokenize=False,
        )

        cache = model.make_cache()
        logits = model(mx.array(prompt_ids)[None], cache=cache)[0, -1]
        logits32 = logits.astype(mx.float32)
        order = mx.argsort(-logits32)[:5]
        mx.eval(order, logits32)
        top5 = [[int(i), float(logits32[int(i)].item())] for i in order.tolist()]

        generated = []
        step_top5 = []
        token = mx.argmax(logits, axis=-1)
        t1 = time.perf_counter()
        for step in range(GEN_TOKENS):
            l32 = logits.astype(mx.float32)
            order5 = mx.argsort(-l32)[:5]
            mx.eval(order5, l32, token)
            step_top5.append([[int(i), float(l32[int(i)].item())] for i in order5.tolist()])
            tid = int(token.item())
            generated.append(tid)
            if step == GEN_TOKENS - 1:
                break
            logits = model(mx.array([[tid]]), cache=cache)[0, -1]
            token = mx.argmax(logits, axis=-1)
        gen_s = time.perf_counter() - t1
        total_gen += GEN_TOKENS
        total_gen_s += gen_s

        print(
            json.dumps(
                {
                    "prompt": name,
                    "prompt_tokens": len(prompt_ids),
                    "prompt_ids_sha256": hashlib.sha256(
                        ",".join(map(str, prompt_ids)).encode()
                    ).hexdigest(),
                    "prompt_text": prompt_text,
                    "top5": top5,
                    "greedy_ids": generated,
                    "text": tokenizer.decode(generated),
                    "step_top5": step_top5,
                    "decode_tok_per_s": round(GEN_TOKENS / gen_s, 2),
                }
            ),
            flush=True,
        )

    peak = mx.get_peak_memory() if hasattr(mx, "get_peak_memory") else mx.metal.get_peak_memory()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "summary": True,
                "load_s": round(load_s, 2),
                "decode_tok_per_s": round(total_gen / total_gen_s, 2),
                "mlx_peak_memory_gb": round(peak / 1e9, 3),
                "max_rss_gb": round(rss / 1e9, 3),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
