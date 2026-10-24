#!/usr/bin/env python3
"""Build the Ling V4 train/dev upload ZIP + upload-zip.json (reproducible headers).

Refuses to package unless validation.json and preflight.json are PASS for the exact train/dev bytes.
"""
import hashlib, json, platform, zipfile, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "ling-train-dev-v4-20261024.zip"
MEMBERS = ["train.jsonl", "dev.jsonl", "v4.provenance.jsonl", "voice_rewrites.jsonl", "stats.json", "assets/voice_tail.txt",
           "README.md", "REWRITE_GUIDE.md", "preflight.py", "preflight.json", "check_labels.py", "axolotl-a6000.yaml",
           "merge_lora.py", "validate_v4.py", "validation.json", "build_v4.py", "ext_v4_personal.py", "check_rewrite.py"]
MEMBERS = sorted(dict.fromkeys(MEMBERS))


def sha(p):
    return hashlib.sha256((ROOT / p).read_bytes()).hexdigest()


def main():
    for n in MEMBERS:
        assert (ROOT / n).is_file(), "missing " + n
    val = json.loads((ROOT / "validation.json").read_text())
    assert val["status"] == "PASS" and val["problem_count"] == 0, "validation.json not PASS"
    pf = json.loads((ROOT / "preflight.json").read_text())
    assert pf["status"] == "PASS" and not pf["rows_over_max_len"], "preflight not PASS"
    for split in ("train", "dev"):
        n = split + ".jsonl"
        assert pf["data_sha256"][n] == sha(n), "preflight.json is stale for " + n
        assert val["sha256"][split] == sha(n), "validation.json is stale for " + n
    meta = {n: {"sha256": sha(n), "bytes": (ROOT / n).stat().st_size} for n in MEMBERS}
    path = ROOT / NAME
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for n in MEMBERS:
            info = zipfile.ZipInfo(n, date_time=(2026, 10, 24, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            z.writestr(info, (ROOT / n).read_bytes(), compresslevel=9)
    with zipfile.ZipFile(path) as z:
        assert set(z.namelist()) == set(MEMBERS) and z.testzip() is None
        for n, m in meta.items():
            assert hashlib.sha256(z.read(n)).hexdigest() == m["sha256"]
    receipt = {"archive": NAME, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
               "members": len(MEMBERS), "python": platform.python_version(), "zlib": zlib.ZLIB_VERSION, "files": meta}
    (ROOT / "upload-zip.json").write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps({k: v for k, v in receipt.items() if k != "files"}, indent=1))


if __name__ == "__main__":
    main()
