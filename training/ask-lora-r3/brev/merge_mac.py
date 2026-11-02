"""Mac: fold a PEFT LoRA adapter into the bf16 Gemma 4 E4B safetensors without torch or a GPU.

train.py's `merge` needs CUDA/ROCm. This does the same arithmetic on the CPU with numpy, one tensor at a time:
W += (alpha / r) * B @ A in float32, rounded to bf16 (round-to-nearest-even). The output file keeps the base's
header byte for byte (same names, shapes, dtypes and offsets), so no vision/audio tensor can be renamed or dropped,
and only the LoRA target matrices change. Peak memory is a few hundred MB.

    python merge_mac.py --base <bf16 dir> --adapter <step dir with adapter_config.json> --out <NEW dir>

It refuses to write into an existing folder. Then quantize the output with quantize.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import sys
from pathlib import Path

import numpy as np

PREFIX = "base_model.model."


def header(path: Path) -> tuple[dict, int]:
    with path.open("rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        return json.loads(f.read(n)), 8 + n


def read(f, start: int, info: dict) -> np.ndarray:
    a, b = info["data_offsets"]
    f.seek(start + a)
    raw = f.read(b - a)
    if info["dtype"] == "F32":
        return np.frombuffer(raw, dtype="<f4").reshape(info["shape"])
    if info["dtype"] == "BF16":
        return (np.frombuffer(raw, dtype="<u2").astype(np.uint32) << 16).view(np.float32).reshape(info["shape"])
    sys.exit(f"unsupported adapter dtype {info['dtype']}")


def to_bf16(x: np.ndarray) -> bytes:
    u = np.ascontiguousarray(x, dtype=np.float32).view(np.uint32)
    u = u + (np.uint32(0x7FFF) + ((u >> 16) & np.uint32(1)))
    return (u >> 16).astype("<u2").tobytes()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, type=Path)
    ap.add_argument("--adapter", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    if a.out.exists():
        sys.exit(f"{a.out} exists; refusing to overwrite")
    cfg = json.loads((a.adapter / "adapter_config.json").read_text())
    if cfg.get("use_dora") or cfg.get("use_rslora") or cfg.get("rank_pattern") or cfg.get("alpha_pattern"):
        sys.exit("only plain LoRA (no DoRA, rsLoRA or per-module rank/alpha) is supported")
    if cfg.get("fan_in_fan_out") or cfg.get("bias", "none") != "none" or cfg.get("modules_to_save"):
        sys.exit("fan_in_fan_out, LoRA biases and modules_to_save are not supported")
    scale = cfg["lora_alpha"] / cfg["r"]

    src = a.base / "model.safetensors"
    bh, bstart = header(src)
    ah, astart = header(a.adapter / "adapter_model.safetensors")
    pairs: dict[str, tuple[str, str]] = {}
    for k in ah:
        if k.endswith(".lora_A.weight"):
            if not k.startswith(PREFIX):
                sys.exit(f"unexpected adapter tensor {k}")
            name = k[len(PREFIX):].replace(".lora_A.weight", ".weight")
            pairs[name] = (k, k.replace(".lora_A.", ".lora_B."))
    extra = {k for k in ah if k != "__metadata__"} - {x for p in pairs.values() for x in p}
    if extra:
        sys.exit(f"adapter has tensors that are not LoRA A/B pairs: {sorted(extra)[:3]}")
    missing = [n for n in pairs if n not in bh]
    if missing:
        sys.exit(f"{len(missing)} adapter targets have no base tensor, e.g. {missing[:3]}")
    target = re.compile(cfg["target_modules"]) if isinstance(cfg["target_modules"], str) else None
    if target and any(not target.fullmatch(n[: -len(".weight")]) for n in pairs):
        sys.exit("an adapter tensor is outside target_modules")

    a.out.mkdir(parents=True)
    order = sorted((v["data_offsets"][0], k) for k, v in bh.items() if k != "__metadata__")
    changed, max_delta = 0, 0.0
    sha = hashlib.sha256()
    with src.open("rb") as fb, (a.adapter / "adapter_model.safetensors").open("rb") as fa, \
            (a.out / "model.safetensors").open("wb") as fo:
        fb.seek(0)
        head = fb.read(bstart)
        fo.write(head)
        pos = 0
        for off, name in order:
            info = bh[name]
            s, e = info["data_offsets"]
            if s != pos:
                sys.exit(f"base tensors are not contiguous at {name}")
            if name in pairs:
                if info["dtype"] != "BF16":
                    sys.exit(f"{name} is {info['dtype']}, expected BF16")
                ka, kb = pairs[name]
                A, B = read(fa, astart, ah[ka]), read(fa, astart, ah[kb])
                W = read(fb, bstart, info)
                if B.shape[0] != W.shape[0] or A.shape[1] != W.shape[1]:
                    sys.exit(f"{name}: LoRA {B.shape}x{A.shape} does not fit {W.shape}")
                delta = scale * (B @ A)
                max_delta = max(max_delta, float(np.abs(delta).max()))
                data = to_bf16(W + delta)
                changed += 1
            else:
                fb.seek(bstart + s)
                data = fb.read(e - s)
            if len(data) != e - s:
                sys.exit(f"{name}: wrote {len(data)} bytes, expected {e - s}")
            fo.write(data)
            sha.update(data)
            pos = e
    for f in sorted(a.base.iterdir()):
        if f.name != "model.safetensors" and f.is_file():
            shutil.copy2(f, a.out / f.name)
    if changed != len(pairs) or max_delta == 0.0:
        sys.exit(f"merged {changed}/{len(pairs)} tensors, max |delta| {max_delta}: the adapter had no effect")
    info = {"base": str(a.base), "adapter": str(a.adapter), "scale": scale, "merged_tensors": changed,
            "max_abs_delta": max_delta, "tensor_data_sha256": sha.hexdigest()}
    (a.out / "merge-info.json").write_text(json.dumps(info, indent=1) + "\n")
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
