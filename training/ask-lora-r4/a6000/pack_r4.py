#!/usr/bin/env python3
"""Pack r4 for the Brev A6000 (the L40 profile still works): the SFT data zip, the mmeval zip and the text-picker zip. Runs on the Mac.

    PY=~/Desktop/MOE_Project/.venv/bin/python
    $PY training/ask-lora-r4/a6000/pack_r4.py --model ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp

Inputs (each is checked FIRST; the script stops before doing any work if one is missing or empty):
    ../data/train.jsonl valid.jsonl train.meta.jsonl valid.meta.jsonl stats.json     r4 text build (ASK_LORA_ROUND=4)
    ../vision/vision_rows.jsonl (+ images beside it)                                 r4 vision rows
    ../../ask-lora-r3/audio/audio_rows.jsonl (+ clips beside it)                     audio is reused from r3
    ../../ask-lora-r3/gate/{vision/cases.json,audio/truth.json}                      report-only gate set for mmeval
                                                                                     (--no-gate to leave it out)

Outputs in --out-dir (default ./out):
    ask-lora-data.zip     brev/render.py's zip (train/dev text, vision, audio, train.py, render.py, parity rows),
                          rendered with Gemma's template. Upload to ~/wisp/uploads; sha256 in upload-zip.json.
    mmeval-data.zip       mmeval/pack.py: the evaluator code, the vision/audio DEV rows and the gate set.
    pickers.zip           pick_text.py, reward.py and data/text-dev.jsonl (the valid text steps with reward labels).
    upload.json           sha256 and size of the three, the stats, and the estimate.
    upload-zip.json       render.py's own record of ask-lora-data.zip (r3's brev/upload-zip.json is NOT touched).

On the box:  unzip -d ~/wisp/mmeval mmeval-data.zip && unzip -o -d ~/wisp/mmeval pickers.zip
(both land in the same directory, which is what pick_text.py expects).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
R4 = HERE.parent
TRAINING = R4.parent
R3 = TRAINING / "ask-lora-r3"
FORCED = "<|channel>thought\n"       # brev/render.py FORCED: a prompt that already ends inside the thought channel
TEXT_FILES = ("train.jsonl", "valid.jsonl", "train.meta.jsonl", "valid.meta.jsonl", "stats.json")


def die(msg: str):
    sys.exit(f"pack_r4: {msg}")


def nonempty(p: Path) -> bool:
    return p.is_file() and p.stat().st_size > 0


def count_lines(p: Path) -> int:
    with open(p) as fh:
        return sum(1 for line in fh if line.strip())


def check_inputs(data: Path, vision_rows: Path, audio_rows: Path, gate: tuple | None, model: Path | None = None) -> list:
    """Every reason not to start. Cheap: existence, size and line counts only (the real validation is render.py's)."""
    bad = []
    for name in TEXT_FILES:
        if not nonempty(data / name):
            bad.append(f"missing or empty text data: {data / name}  (build it with ASK_LORA_ROUND=4 ASK_LORA_OUT={data} build.py)")
    if not bad:
        for a, b in (("train.jsonl", "train.meta.jsonl"), ("valid.jsonl", "valid.meta.jsonl")):
            na, nb = count_lines(data / a), count_lines(data / b)
            if na != nb:
                bad.append(f"{a} has {na} rows but {b} has {nb}")
        if count_lines(data / "valid.jsonl") == 0:
            bad.append("valid.jsonl has no rows: the picker needs the valid text steps")
    if not nonempty(vision_rows):
        bad.append(f"missing or empty vision rows: {vision_rows}")
    else:
        for i, line in enumerate(open(vision_rows)):
            if line.strip():
                r = json.loads(line)
                miss = [x for x in r.get("images", []) if not (vision_rows.parent / x).is_file()]
                if miss:
                    bad.append(f"vision row {r.get('id')}: image(s) not found beside the rows file: {miss[:2]}")
                    break
    if not nonempty(audio_rows):
        bad.append(f"missing or empty audio rows: {audio_rows}")
    if gate is not None:
        for g in gate:
            if not nonempty(Path(g)):
                bad.append(f"missing gate file {g} (build the gate set per ask-lora-r3/gate/README.md, or pass --no-gate)")
    if model is not None and not (model / "chat_template.jinja").is_file():
        bad.append(f"--model {model} has no chat_template.jinja (the Gemma 4 E4B-it oQ4e-mtp folder Wisp serves)")
    return bad


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def pick_grpo_dir(arg: str | None) -> Path:
    """The grpo code whose labels and reward the picker uses: --grpo-dir, else r4's adaptation when it exists."""
    for d in ([Path(arg)] if arg else []) + [R4 / "grpo", R3 / "grpo"]:
        if (d / "reward.py").is_file() and (d / "export_prompts.py").is_file():
            return d
    die("no grpo dir with reward.py and export_prompts.py (pass --grpo-dir)")


def build_text_dev(dev_rows: list, pairs: list, classify, rnd: int = 4, turns: dict | None = None, tool_names=(),
                   thought_words=None) -> list:
    """Join render.py's rendered dev rows with the builder's (example, meta) pairs by position (render.py renders
    valid.jsonl in file order) and attach the reward label. Raises on any id mismatch so a misaligned join cannot
    silently score the wrong labels."""
    if len(dev_rows) != len(pairs):
        raise ValueError(f"{len(dev_rows)} rendered dev rows but {len(pairs)} valid examples")
    out = []
    for row, (ex, meta) in zip(dev_rows, pairs):
        if row["id"] != meta.get("id"):
            raise ValueError(f"dev row {row['id']!r} does not line up with valid example {meta.get('id')!r}")
        label = classify(ex, meta, rnd, turns or {}, tool_names)       # round 4: the r4 forbidden list, chat rules and recall labels
        label["forced"] = row["prompt"].endswith(FORCED)
        label["think"] = bool(row["think"])
        if thought_words is not None and rnd >= 4:
            label["thought_words"] = thought_words(row["completion"], label["forced"])    # the thinking floor never exceeds the ideal
        label["ideal_completion"] = row["completion"]
        out.append({"id": row["id"], "family": row["family"], "prompt": row["prompt"], "label": label})
    return out


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_pickers_zip(dest: Path, text_dev: list, grpo_dir: Path) -> None:
    files = {"pick_text.py": (HERE / "pick_text.py").read_bytes(),
             "reward.py": (grpo_dir / "reward.py").read_bytes(),
             "data/text-dev.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in text_dev).encode()}
    files["PICKERS_MANIFEST.json"] = json.dumps({"files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
                                                 "grpo_dir": str(grpo_dir), "steps": len(text_dev)}, indent=1).encode()
    tmp = dest.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for k in sorted(files):
            z.writestr(zipfile.ZipInfo(k, date_time=(2026, 11, 2, 0, 0, 0)), files[k], zipfile.ZIP_DEFLATED)
    os.replace(tmp, dest)


def run(cmd: list, env: dict | None = None) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], env={**os.environ, **(env or {})})
    if r.returncode:
        die(f"{Path(str(cmd[1])).name} failed with exit {r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="Gemma 4 E4B-it oQ4e-mtp dir (tokenizer + chat_template.jinja)")
    ap.add_argument("--data", default=str(R4 / "data"))
    ap.add_argument("--vision-rows", default=str(R4 / "vision" / "vision_rows.jsonl"))
    ap.add_argument("--audio-rows", default=str(R3 / "audio" / "audio_rows.jsonl"))
    ap.add_argument("--vision-cases", default=str(R3 / "gate" / "vision" / "cases.json"))
    ap.add_argument("--audio-truth", default=str(R3 / "gate" / "audio" / "truth.json"))
    ap.add_argument("--no-gate", action="store_true", help="leave the report-only gate set out of mmeval-data.zip")
    ap.add_argument("--grpo-dir", help="dir with reward.py and export_prompts.py (default: r4's grpo/ if it exists, else r3's)")
    ap.add_argument("--out-dir", default=str(HERE / "out"))
    ap.add_argument("--check-only", action="store_true", help="validate the inputs and stop")
    ap.add_argument("--no-estimate", action="store_true", help="skip the exact token count (slow: tokenizes every row)")
    ap.add_argument("--gpu", default="a6000", help="card profile in estimate.GPUS (a6000 or l40)")
    ap.add_argument("--usd-per-hour", type=float, help="default: the card's planning rate in estimate.GPUS")
    a = ap.parse_args()

    data, vrows, arows = Path(a.data), Path(a.vision_rows), Path(a.audio_rows)
    gate = None if a.no_gate else (a.vision_cases, a.audio_truth)
    bad = check_inputs(data, vrows, arows, gate, Path(a.model))
    if bad:
        die("not packing; fix these first:\n  " + "\n  ".join(bad))
    print(f"inputs ok: {count_lines(data / 'train.jsonl')} train + {count_lines(data / 'valid.jsonl')} valid text rows, "
          f"{count_lines(vrows)} vision rows, {count_lines(arows)} audio rows")
    if a.check_only:
        return 0
    grpo = pick_grpo_dir(a.grpo_dir)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    zip_path, upload_json = out / "ask-lora-data.zip", out / "upload-zip.json"

    # 1. the SFT zip: brev/render.py reads the text rows from ASK_LORA_DATA and never writes into r3's upload-zip.json
    run([sys.executable, R3 / "brev" / "render.py", "--model", a.model, "--vision-rows", vrows, "--audio-rows", arows,
         "--out", zip_path, "--upload-json", upload_json], env={"ASK_LORA_DATA": str(data)})

    # 2. the valid text steps with their reward labels, from the SAME rendered dev rows the trainer evaluates on
    sys.path.insert(0, str(grpo))                     # export_prompts does `import reward`
    export = load_module(grpo / "export_prompts.py", "r4_export_prompts")
    with zipfile.ZipFile(zip_path) as z:
        dev_rows = [json.loads(l) for l in z.read("dev.jsonl").decode().splitlines() if l.strip()]
    pairs = [(json.loads(e), json.loads(m)) for e, m in zip(open(data / "valid.jsonl"), open(data / "valid.meta.jsonl"))]
    turns: dict = {}                                  # the same turn context export_prompts.py gives classify
    tool_names: set = set()
    for ex, meta in pairs:
        names = [tc["function"]["name"] for tc in (ex["messages"][-1].get("tool_calls") or ())]
        turns.setdefault(export.turn_key(meta), []).append((meta["step"], names))
        tool_names.update(t["function"]["name"] for t in ex.get("tools") or () if isinstance(t, dict) and "function" in t)
        tool_names.update(names)
    tool_names.discard(export.DISCOVERY)
    text_dev = build_text_dev(dev_rows, pairs, export.classify, 4, turns, sorted(tool_names), export.thought_words)
    pickers = out / "pickers.zip"
    write_pickers_zip(pickers, text_dev, grpo)

    # 3. the mmeval zip (evaluator code + vision/audio dev rows + gate set)
    cmd = [sys.executable, R3 / "mmeval" / "pack.py", "--vision-rows", vrows, "--audio-rows", arows, "--out", out / "mmeval-data.zip"]
    cmd += ["--no-gate"] if a.no_gate else ["--vision-cases", a.vision_cases, "--audio-truth", a.audio_truth]
    run(cmd)

    info = {"inputs": {"data": str(data), "vision_rows": str(vrows), "audio_rows": str(arows), "grpo_dir": str(grpo)},
            "text_dev_steps": len(text_dev)}
    for name in ("ask-lora-data.zip", "mmeval-data.zip", "pickers.zip"):
        p = out / name
        info[name] = {"sha256": sha256(p), "bytes": p.stat().st_size}
    if not a.no_estimate:
        sys.path.insert(0, str(HERE))
        import estimate as E
        t = E.tokens_from_zip(zip_path, Path(a.model))
        info["tokens"] = t
        g = E.GPUS[a.gpu]
        usd_h = g["usd_h"] if a.usd_per_hour is None else a.usd_per_hour
        info["estimate_sft"] = E.sft_estimate(t["train.jsonl"]["rows"], t["train.jsonl"]["mean"], tok_s=g["train_tok_s"][1], usd_h=usd_h)
        info["estimate_stage1"] = [E.stage1_hours(t["train.jsonl"]["rows"], t["train.jsonl"]["mean"], gpu=a.gpu, case=c, usd_h=usd_h)
                                   for c in (0, 1, 2)]
        print("estimate:", json.dumps(info["estimate_sft"]))
        print("stage 1 (optimistic / middle / pessimistic):", [(e["total_h"], e["usd"]) for e in info["estimate_stage1"]])
    (out / "upload.json").write_text(json.dumps(info, indent=1) + "\n")
    print(json.dumps({k: v for k, v in info.items() if k.endswith(".zip")}, indent=1))
    print(f"\nupload to ~/wisp/uploads: {zip_path.name}, mmeval-data.zip, pickers.zip, ../../ask-lora-r3/brev/vm-setup.sh, run_sft.sh"
          f"\nunpack sha256 for run.sh: {info['ask-lora-data.zip']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
