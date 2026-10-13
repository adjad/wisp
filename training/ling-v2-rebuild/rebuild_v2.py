#!/usr/bin/env python3
"""Rebuild Ling-3.0-tiny-Wisp-V2-merged from the original base and the V2 LoRA adapter.

Recipe (from MERGE_MANIFEST.json): for each of the 102 targeted weights,
delta = (B @ A) * alpha / r in FP32 on CPU, cast delta to BF16, add to the BF16 base weight.
Every other tensor is copied unchanged. Each output shard is then checked against the
manifest's sha256. A shard can differ byte-wise only if this safetensors/torch version
serializes differently; the script reports that per shard instead of hiding it.

  python rebuild_v2.py --out /path/to/Ling-3.0-tiny-Wisp-V2-merged
  python rebuild_v2.py --out OUT --base-dir /already/downloaded/Ling-3.0-tiny   # skip download

Needs: torch, safetensors, huggingface_hub (download only). ~16GB download, ~16GB output,
peak RAM ~ one shard (~1.2GB) plus the adapter.
"""
import argparse, hashlib, json, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_REPO = "inclusionAI/Ling-3.0-tiny"
ADAPTER_SHA256 = "d9c40b5635cbe44e57d90433704fba12168b4ddfcd9cd52192a169146d05b433"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 24), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--base-dir", type=Path, default=None, help="local copy of the base at the pinned revision")
    ap.add_argument("--adapter-dir", type=Path, default=HERE / "v2-adapter")
    ap.add_argument("--meta-dir", type=Path, default=HERE / "v2-merged-meta",
                    help="the V2 merged folder's non-weight files (config, tokenizer, index, manifest)")
    args = ap.parse_args()

    import torch
    from safetensors import safe_open
    from safetensors.torch import load_file, save_file

    manifest = json.loads((args.meta_dir / "MERGE_MANIFEST.json").read_text())
    adapter_file = args.adapter_dir / "adapter_model.safetensors"
    got = sha256(adapter_file)
    if got != manifest["adapter_sha256"] or got != ADAPTER_SHA256:
        sys.exit(f"adapter sha256 mismatch: {got}")
    cfg = json.loads((args.adapter_dir / "adapter_config.json").read_text())
    if cfg.get("use_rslora") or cfg.get("use_dora") or cfg.get("rank_pattern") or cfg.get("alpha_pattern"):
        sys.exit("unexpected adapter options; refusing to guess the scaling")
    scale = cfg["lora_alpha"] / cfg["r"]
    print(f"adapter OK  r={cfg['r']} alpha={cfg['lora_alpha']} scale={scale}")

    lora = load_file(str(adapter_file))
    deltas = {}
    for key in lora:
        if ".lora_A.weight" not in key:
            continue
        target = key.replace("base_model.model.", "", 1).replace(".lora_A.weight", ".weight")
        a = lora[key].float()
        b = lora[key.replace(".lora_A.", ".lora_B.")].float()
        deltas[target] = ((b @ a) * scale).to(torch.bfloat16)
    if set(deltas) != set(manifest["changed_targets"]):
        sys.exit("adapter targets differ from the manifest's changed_targets")
    print(f"{len(deltas)} merge targets")

    base = args.base_dir
    if base is None:
        from huggingface_hub import snapshot_download
        base = Path(snapshot_download(BASE_REPO, revision=manifest["base_revision"],
                                      allow_patterns=["*.safetensors", "*.json"]))
    print("base:", base)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    for f in args.meta_dir.iterdir():
        if f.is_file() and not f.name.endswith(".safetensors"):
            shutil.copy2(f, out / f.name)

    index = json.loads((args.meta_dir / "model.safetensors.index.json").read_text())
    shards = sorted(set(index["weight_map"].values()))
    expected = manifest["files"]
    merged, mismatched = 0, []
    for name in shards:
        src = base / name
        with safe_open(str(src), "pt") as f:
            metadata = f.metadata()
        tensors = load_file(str(src))
        for key in tensors:
            if key in deltas:
                w = tensors[key]
                if w.dtype != torch.bfloat16 or w.shape != deltas[key].shape:
                    sys.exit(f"{key}: base is {w.dtype} {tuple(w.shape)}, delta {tuple(deltas[key].shape)}")
                tensors[key] = w + deltas[key]
                merged += 1
        save_file(tensors, str(out / name), metadata=metadata)
        del tensors
        want = (expected.get(name) or {}).get("sha256")
        ok = want == sha256(out / name)
        if not ok:
            mismatched.append(name)
        print(f"{name}  {'sha256 OK' if ok else 'sha256 DIFFERS from V2 manifest'}")

    print(f"\nmerged {merged}/{len(deltas)} targets into {len(shards)} shards -> {out}")
    if merged != len(deltas):
        sys.exit("not every target was found in the base shards")
    if mismatched:
        print(f"{len(mismatched)} shard(s) differ byte-wise from the original V2 files: {mismatched}")
        print("All targets merged with the V2 recipe. A different CPU's FP32 matmul can round a handful of BF16 values\n"
              "differently (tested: fp64 vs fp32 changed 1 in 134M values by 2e-7), so this is usable for training;\n"
              "it is just not a bit-for-bit copy. A shard WITHOUT merge targets differing would mean a wrong base download.")
        sys.exit(2)
    print("EXACT MATCH: every shard is byte-identical to the original V2 merged weights.")


if __name__ == "__main__":
    main()
