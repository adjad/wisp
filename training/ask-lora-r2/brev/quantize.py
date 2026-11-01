"""Mac: oQ-quantize a Gemma 4 E4B bf16 folder with the installed oMLX and combine the shipped MTP head.

The gate compares the untuned bf16 and the merged LoRA, both quantized by this script with one recipe
(oQ level 4, enhanced, group 64, bfloat16) and both given the SAME MTP head: the one inside the shipped
`-mtp` build (`--mtp-from`), rebuilt byte-for-byte as a `gemma4_assistant` donor folder.

Run it with oMLX's own Python under the heavy lock, with nothing else quantizing:

    (the env omlx-cli uses; -s keeps a user-site mlx from loading twice; Metal needs an unsandboxed shell)
    R=/Applications/oMLX.app/Contents/Resources; P=$R/Python
    PYTHONHOME=$P/cpython-3.11 PYTHONPATH=$R:$P/framework-mlx-base/lib/python3.11/site-packages \
      $P/cpython-3.11/bin/python3 -s quantize.py --src <bf16 dir> --out <new dir ending -oQ4e-mtp> \
      --mtp-from ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp

It refuses to write into an existing folder, so it can never overwrite the shipped Gemma.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import tempfile
import time
from pathlib import Path


def build_donor(mtp_from: Path, dest: Path) -> str:
    """Rebuild the gemma4_assistant donor from a combined -mtp build: same tensor bytes, prefix stripped."""
    cfg = json.loads((mtp_from / "config.json").read_text())
    asst = cfg["text_config"]["mtp_assistant_config"]
    (dest / "config.json").write_text(json.dumps(asst, indent=1))
    raw = (mtp_from / "model-mtp.safetensors").read_bytes()
    n = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8:8 + n])
    prefix = "language_model.mtp."
    new = {}
    for k, v in header.items():
        if k == "__metadata__":
            new[k] = v
            continue
        if not k.startswith(prefix):
            sys.exit(f"unexpected tensor in the MTP shard: {k}")
        new[k[len(prefix):]] = v
    hb = json.dumps(new, separators=(",", ":")).encode()
    hb += b" " * (-len(hb) % 8)
    (dest / "model.safetensors").write_bytes(struct.pack("<Q", len(hb)) + hb + raw[8 + n:])
    return hashlib.sha256(raw[8 + n:]).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="bf16 Gemma 4 E4B folder (untuned or merged)")
    ap.add_argument("--out", required=True, type=Path, help="NEW output folder")
    ap.add_argument("--mtp-from", required=True, type=Path, help="shipped -mtp build whose head both builds get")
    ap.add_argument("--level", type=float, default=4)
    args = ap.parse_args()
    if args.out.exists():
        sys.exit(f"{args.out} exists - refusing to overwrite")
    from omlx import __version__ as omlx_version
    from omlx.oq import combine_mtp_into_output, quantize_oq_streaming

    t0 = time.time()

    def cb(phase, pct, detail="", meta=None):
        print(f"  [{time.time() - t0:6.0f}s] {pct:5.1f}% {phase} {detail}", flush=True)

    quantize_oq_streaming(str(args.src), str(args.out), args.level, 64, cb, False, None, None, "",
                          "bfloat16", False, True, enhanced=True)
    with tempfile.TemporaryDirectory(prefix="gemma4-mtp-donor-") as d:
        data_sha = build_donor(args.mtp_from, Path(d))
        combine_mtp_into_output(str(args.out), d)
    recipe = {"src": str(args.src), "omlx": omlx_version, "oq_level": args.level, "enhanced": True,
              "group_size": 64, "dtype": "bfloat16", "mtp_from": str(args.mtp_from),
              "mtp_tensor_bytes_sha256": data_sha, "minutes": round((time.time() - t0) / 60, 1),
              "written": time.strftime("%FT%T%z")}
    (args.out / "wisp-quant-recipe.json").write_text(json.dumps(recipe, indent=1) + "\n")
    print(json.dumps(recipe))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
