#!/usr/bin/env python3
"""Step-zero parity, no training: the r5.1 launch path (train.py's load_model + --init-adapter) against the original
step-1800 adapter loaded the ordinary way (PeftModel.from_pretrained). Same fixed rows, eval mode, no grad, no
optimizer is ever built. Also scores the base (LoRA off) so a no-op load can't pass. Runs on the GPU box.

    python parity_step0.py --train-py PATCHED/train.py --data UNZIPPED --base BF16 --adapter STEP1800 --sha SHA \
        --ids id1,id2,... --out parity.json
"""
from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import sys
from pathlib import Path


def load_train_py(path: str):
    spec = importlib.util.spec_from_file_location("train_r51", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["train_r51"] = m
    spec.loader.exec_module(m)
    return m


def score(T, model, rows, dev=0):
    import torch
    import torch.nn.functional as F
    model.eval()
    out = {}
    with torch.no_grad():
        for rid, p, c in rows:
            logits, _ = T._logits(model, p, c, dev)
            tgt = torch.tensor(c, device=logits.device)
            out[rid] = {"logits": logits.cpu(), "ce": F.cross_entropy(logits, tgt).item(),
                        "argmax": logits.argmax(-1).cpu()}
    return out


def compare(a, b):
    import torch
    res = {}
    for rid in a:
        x, y = a[rid]["logits"], b[rid]["logits"]
        res[rid] = {"positions": int(x.shape[0]), "max_abs_dlogit": float((x - y).abs().max()),
                    "top1_agree": int((a[rid]["argmax"] == b[rid]["argmax"]).sum()),
                    "ce_a": round(a[rid]["ce"], 6), "ce_b": round(b[rid]["ce"], 6), "identical": bool(torch.equal(x, y))}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-py", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--attn", default="sdpa")
    ap.add_argument("--out", required=True)
    o = ap.parse_args(argv)
    import torch
    T = load_train_py(o.train_py)
    want = o.ids.split(",")
    raw = {json.loads(l)["id"]: json.loads(l) for l in open(Path(o.data) / "train.jsonl")}
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(o.base, local_files_only=True)
    rows = [(i, tok(raw[i]["prompt"], add_special_tokens=False).input_ids,
             tok(raw[i]["completion"], add_special_tokens=False).input_ids) for i in want]

    # A: the launch path. load_model(for_training=True) builds the fresh LoRA exactly as `train` does, then the init.
    a = argparse.Namespace(base=o.base, attn=o.attn, qlora=False, rank=16, alpha=16, seed=20261029,
                           init_adapter=o.adapter, init_adapter_sha256=o.sha)
    _, model = T.load_model(a)
    fresh = score(T, model, rows[:1])                       # untrained LoRA (B = 0) == base on this row
    info = T.load_init_adapter(model, Path(o.adapter), a)
    launch = score(T, model, rows)
    with model.disable_adapter():
        base = score(T, model, rows)
    del model
    gc.collect(); torch.cuda.empty_cache()

    # B: the reference load of the same files.
    from peft import PeftModel
    ref_base = T._model_class().from_pretrained(o.base, dtype=torch.bfloat16, attn_implementation=T.attn_implementation(o.attn),
                                                device_map={"": 0})
    ref = PeftModel.from_pretrained(ref_base, o.adapter)
    reference = score(T, ref, rows)

    result = {"init": info, "rows": want, "attn": o.attn,
              "launch_vs_reference": compare(launch, reference),
              "launch_vs_base": compare(launch, base),
              "fresh_lora_vs_base_row0": compare(fresh, {k: base[k] for k in fresh}),
              "optimizer_built": False, "optimizer_steps": 0}
    Path(o.out).write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    ok = all(v["max_abs_dlogit"] == 0 for v in result["launch_vs_reference"].values()) and \
        all(v["max_abs_dlogit"] > 0 for v in result["launch_vs_base"].values())
    print("PARITY", "PASS" if ok else "CHECK")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
