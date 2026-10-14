#!/usr/bin/env python3
"""Merge a PEFT LoRA adapter (e.g. the Axolotl Ling V3 output) into a BF16 safetensors model directory.

Same recipe as V2 (MERGE_MANIFEST.json): delta = (B @ A) * alpha / r in FP32 on CPU, cast to BF16, add to the BF16
weight. Every other tensor is copied unchanged; every non-weight file (config, tokenizer, chat template, modeling code)
is copied from --base-dir. Writes MERGE_MANIFEST.json with per-file sha256s.

  python merge_lora.py --base-dir Ling-3.0-tiny-Wisp-V2-merged --adapter-dir ling-v3-lora-out --out Ling-3.0-tiny-Wisp-V3-merged
"""
import argparse, hashlib, json, shutil, sys
from pathlib import Path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-dir", type=Path, required=True)
    ap.add_argument("--adapter-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    import torch
    from safetensors import safe_open
    from safetensors.torch import load_file, save_file

    cfg = json.loads((args.adapter_dir / "adapter_config.json").read_text())
    if cfg.get("use_rslora") or cfg.get("use_dora") or cfg.get("rank_pattern") or cfg.get("alpha_pattern"):
        sys.exit("unexpected adapter options; refusing to guess the scaling")
    scale = cfg["lora_alpha"] / cfg["r"]
    adapter_file = args.adapter_dir / "adapter_model.safetensors"
    lora = load_file(str(adapter_file))
    deltas = {}
    for key in lora:
        if ".lora_A." not in key:
            continue
        target = key.replace("base_model.model.", "", 1).replace(".lora_A.weight", ".weight").replace(".lora_A.default.weight", ".weight")
        b_key = key.replace(".lora_A.", ".lora_B.")
        deltas[target] = ((lora[b_key].float() @ lora[key].float()) * scale).to(torch.bfloat16)
    if not deltas:
        sys.exit("no lora_A/lora_B pairs found in the adapter")
    print(f"adapter r={cfg['r']} alpha={cfg['lora_alpha']} scale={scale}: {len(deltas)} targets")

    index = json.loads((args.base_dir / "model.safetensors.index.json").read_text())
    unknown = set(deltas) - set(index["weight_map"])
    if unknown:
        sys.exit(f"{len(unknown)} adapter targets are not weights of the base, e.g. {sorted(unknown)[:3]}")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    for f in args.base_dir.iterdir():
        if f.is_file() and not f.name.endswith(".safetensors") and f.name != "MERGE_MANIFEST.json":
            shutil.copy2(f, out / f.name)

    merged, max_rel = 0, 0.0
    for name in sorted(set(index["weight_map"].values())):
        src = args.base_dir / name
        with safe_open(str(src), "pt") as f:
            metadata = f.metadata()
        tensors = load_file(str(src))
        for key in tensors:
            if key in deltas:
                w = tensors[key]
                if w.dtype != torch.bfloat16 or w.shape != deltas[key].shape:
                    sys.exit(f"{key}: base is {w.dtype} {tuple(w.shape)}, delta {tuple(deltas[key].shape)}")
                max_rel = max(max_rel, (deltas[key].float().norm() / w.float().norm().clamp_min(1e-12)).item())
                tensors[key] = w + deltas[key]
                merged += 1
        save_file(tensors, str(out / name), metadata=metadata)
        del tensors
        print(f"{name} written")
    if merged != len(deltas):
        sys.exit(f"merged {merged} of {len(deltas)} targets")

    files = {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file() and p.name != "MERGE_MANIFEST.json"}
    (out / "MERGE_MANIFEST.json").write_text(json.dumps({
        "base_dir": str(args.base_dir), "adapter_sha256": sha256(adapter_file), "adapter_config": cfg,
        "method": "CPU FP32 B@A * alpha/r, cast delta to BF16, add to BF16 base", "merged_target_count": merged,
        "max_relative_delta_norm": max_rel, "torch": torch.__version__, "files": files}, indent=2))
    print(f"merged {merged} targets; largest ||delta||/||W|| = {max_rel:.4f}; wrote {out}")


if __name__ == "__main__":
    main()
