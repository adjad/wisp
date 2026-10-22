"""Debug helper: per-layer activation dump / diff against `ling-parity --trace`.

    python_ -I parity/trace.py dump <model-dir> <out-dir>   # writes ref_<prompt>.safetensors
    python_ -I parity/trace.py diff <out-dir>               # compares ref_* with swift_*
"""

import sys
sys.dont_write_bytecode = True
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import reference  # noqa: E402  (sets sys.path for the oMLX mlx stack)

import mlx.core as mx  # noqa: E402


def dump(model_dir: Path, out_dir: Path) -> None:
    from mlx_lm.models.base import create_causal_mask
    from mlx_lm.utils import load

    reference.apply_bailing_hybrid_patch()
    model, tokenizer = load(str(model_dir), tokenizer_config={"trust_remote_code": False})
    template = (model_dir / "chat_template.jinja").read_text()
    inner = model.model
    for name, messages in reference.PROMPTS:
        ids = tokenizer.apply_chat_template(
            messages, chat_template=template, add_generation_prompt=True,
            enable_thinking=False, tokenize=True,
        )
        if isinstance(ids, dict):
            ids = ids["input_ids"]
        cache = model.make_cache()
        h = inner.word_embeddings(mx.array(list(ids))[None])
        out = {"embed": h}
        mask = create_causal_mask(h.shape[1], 0)
        att = inner.layers[0].attention
        x = inner.layers[0].input_layernorm(h)
        B, L, _ = x.shape
        H, D = att.num_attention_heads, att.head_dim
        out["l0.norm"] = x
        qp = att.q_proj(x)
        out["l0.q_proj"] = qp
        qc, _ = att.q_conv1d(qp, None, None, None)
        kc, _ = att.k_conv1d(att.k_proj(x), None, None, None)
        vc, _ = att.v_conv1d(att.v_proj(x), None, None, None)
        out["l0.q_conv"], out["l0.k_conv"], out["l0.v_conv"] = qc, kc, vc
        f = att.f_proj(x).reshape(B, L, H, D)
        out["l0.f"] = f
        b = att.b_proj(x)
        out["l0.b"] = b
        mod = sys.modules["mlx_lm.models.bailing_hybrid"]
        o, st = mod.recurrent_kda(
            qc.reshape(B, L, H, D), kc.reshape(B, L, H, D), vc.reshape(B, L, H, D),
            f, b, att.A_log, att.dt_bias, None, safe_gate=att.safe_gate,
            lower_bound=att.lower_bound, mask=None,
        )
        out["l0.kda"] = o
        out["l0.state"] = st.swapaxes(-1, -2)
        normed = att.o_norm(o, att.g_proj(x).reshape(B, L, H, D))
        out["l0.onorm"] = normed
        out["l0.oproj"] = att.o_proj(normed.reshape(B, L, -1))
        for i, layer in enumerate(inner.layers):
            n = layer.input_layernorm(h)
            if layer.is_global:
                r = layer.attention(n, mask, cache[i])
            else:
                r = layer.attention(n, None, cache[i], offset=0)
            h = h + r
            m = layer.mlp(layer.post_attention_layernorm(h))
            h = h + m
            out[f"attn.{i}"], out[f"mlp.{i}"], out[f"h.{i}"] = r, m, h
            mx.eval(h)
        out["logits"] = model.lm_head(inner.norm(h))
        mx.save_safetensors(
            str(out_dir / f"ref_{name}.safetensors"),
            {k: v.astype(mx.float32) for k, v in out.items()},
        )


def diff(out_dir: Path) -> None:
    for name, _ in reference.PROMPTS:
        a = mx.load(str(out_dir / f"ref_{name}.safetensors"))
        b = mx.load(str(out_dir / f"swift_{name}.safetensors"))
        print(f"== {name}")
        keys = ["embed"] + sorted(k for k in a if k.startswith("l0.")) + [
            f"{k}.{i}" for i in range(len([x for x in a if x.startswith("h.")]))
            for k in ("attn", "mlp", "h")
        ] + ["logits"]
        for k in keys:
            d = mx.abs(a[k] - b[k])
            print(f"{k:10s} max|d|={d.max().item():.6g} mean|d|={d.mean().item():.3g} "
                  f"exact={bool(mx.array_equal(a[k], b[k]).item())}")


def optest(path: Path) -> None:
    """Compare `ling-parity --optest` output with the same ops in Python MLX."""
    sw = mx.load(str(path))
    x = sw["x"]
    xb = x.astype(mx.bfloat16)
    k0, k1 = mx.random.split(mx.random.key(0), 2)
    w = mx.random.uniform(-0.05, 0.05, (4608, 1536), key=k0).astype(mx.bfloat16)
    xq = mx.random.uniform(-1, 1, (27, 1536), key=k1).astype(mx.bfloat16)
    wq, s, b = mx.quantize(w, group_size=64, bits=4)
    ref = {
        "exp_f32": mx.exp(x), "sigmoid_f32": mx.sigmoid(x),
        "exp_bf16": mx.exp(xb), "sigmoid_bf16": mx.sigmoid(xb),
        "silu_bf16": xb * mx.sigmoid(xb), "mul_f32": -5 * x,
        "lingsig_f32": mx.sigmoid(x), "lingsig_bf16": mx.sigmoid(xb),
        "q_w": w, "q_x": xq,
        "qmm_bf16": mx.quantized_matmul(xq, wq, s, b, transpose=True, group_size=64, bits=4),
        "qmv_bf16": mx.quantized_matmul(xq[:1], wq, s, b, transpose=True, group_size=64, bits=4),
        "rms_bf16": mx.fast.rms_norm(xq, mx.ones((1536,), mx.bfloat16), 1e-6),
        "swiglu_bf16": __import__("mlx_lm.models.activations", fromlist=["swiglu"]).swiglu(
            xq, mx.random.uniform(-1, 1, (27, 1536), key=mx.random.key(1)).astype(mx.bfloat16)),
    }
    for k, v in ref.items():
        v = v.astype(mx.float32)
        d = mx.abs(v - sw[k])
        n = int((d > 0).sum().item())
        print(f"{k:13s} differing={n:8d}/{v.size} max|d|={d.max().item():.3g}")


if __name__ == "__main__":
    if sys.argv[1] == "optest":
        optest(Path(sys.argv[2]))
    elif sys.argv[1] == "dump":
        dump(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        diff(Path(sys.argv[2]))
