#!/usr/bin/env python3
"""Build the inert, allowlisted Ling V3 train/dev upload ZIP + upload-zip.json (reproducible headers).

Refuses to package unless validation.json passed and preflight.json is PASS for the exact train/dev bytes.
"""
import hashlib, json, platform, zipfile, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "ling-train-dev-v3-20261012.zip"
MEMBERS = ["train.jsonl", "dev.jsonl", "train.provenance.jsonl", "dev.provenance.jsonl", "conversations.jsonl", "stats.json",
           "assets/tools.json", "assets/system_parts.json", "assets/heldout_shingles.json",
           "README.md", "DATA_CARD.md", "preflight.py", "check_labels.py", "axolotl-a6000.yaml",
           "validate.py", "validation.json", "preflight.json"]
# Generator sources: generate.py plus every add-on module it loads (ext_*.py) and v2_replay.py, so the data is reproducible from the zip.
MEMBERS += ["generate.py"] + sorted(p.name for p in ROOT.glob("ext_*.py")) + (["v2_replay.py"] if (ROOT / "v2_replay.py").exists() else [])
# V2-replay provenance travels separately from train/dev.provenance.jsonl; required once v2r rows are appended to train/dev.
if (ROOT / "v2_replay.provenance.jsonl").exists():
    MEMBERS.append("v2_replay.provenance.jsonl")
else:
    assert b'"id":"v2r-' not in (ROOT / "train.jsonl").read_bytes(), "train.jsonl has v2r rows but v2_replay.provenance.jsonl is missing"
MEMBERS = sorted(dict.fromkeys(MEMBERS))


def sha(p):
    return hashlib.sha256((ROOT / p).read_bytes()).hexdigest()


def main():
    for n in MEMBERS:
        assert (ROOT / n).is_file(), "missing " + n
    assert json.loads((ROOT / "validation.json").read_text())["passed"] is True, "validation.json not passed"
    pf = json.loads((ROOT / "preflight.json").read_text())
    assert pf["status"] == "PASS" and not pf["rows_over_max_len"], "preflight not PASS"
    for n in ["train.jsonl", "dev.jsonl"]:
        assert pf["data_sha256"][n] == sha(n), "preflight.json is stale for " + n
    meta = {n: {"sha256": sha(n), "bytes": (ROOT / n).stat().st_size} for n in MEMBERS}
    path = ROOT / NAME
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for n in sorted(MEMBERS):
            info = zipfile.ZipInfo(n, date_time=(2026, 10, 12, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            z.writestr(info, (ROOT / n).read_bytes(), compresslevel=9)
    with zipfile.ZipFile(path) as z:
        assert set(z.namelist()) == set(MEMBERS) and z.testzip() is None
        for n, m in meta.items():
            assert hashlib.sha256(z.read(n)).hexdigest() == m["sha256"]
    receipt = {"archive": NAME, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
               "members": len(MEMBERS), "python": platform.python_version(), "zlib": zlib.ZLIB_VERSION,
               "static_data_only": True, "no_model_execution": True, "preflight_status": pf["status"],
               "axolotl_prepared_label_audit_done": False}
    (ROOT / "upload-zip.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
