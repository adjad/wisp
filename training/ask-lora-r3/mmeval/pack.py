#!/usr/bin/env python
"""Pack the evaluator and its held-out data into ONE small zip for the GPU box (the Wisp repo is not there).

    python pack.py --vision-rows ../vision/vision_rows.jsonl [--audio-rows ../audio/audio_rows.jsonl] \
        [--vision-cases ../gate/vision/cases.json --audio-truth ../gate/audio/truth.json] \
        --out mmeval-data.zip

On the box:  unzip -d ~/wisp/mmeval mmeval-data.zip   ->  ~/wisp/mmeval/{*.py, data/}
             python ~/wisp/mmeval/eval_mm.py --data ~/wisp/mmeval/data ...
Only the dev rows and the media they name are packed (the zip never carries training images). Gate sets, when
given, are packed under data/gate_vision/ and data/gate_audio/ and evaluated alongside (ids are prefixed gate_).
Pure python; runs on the Mac.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rows as R  # noqa: E402

CODE = ["scoring.py", "rows.py", "serving.py", "score_mm.py", "eval_mm.py", "pick_checkpoint.py", "README.md"]


def _media(zf: zipfile.ZipFile, root: Path, rel: str, dest: str, seen: set) -> None:
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts:
        raise SystemExit(f"unsafe media path {rel!r}")
    src = root / rel
    if not src.is_file():
        raise SystemExit(f"missing media file {src}")
    name = f"{dest}/{pp.as_posix()}"
    if name not in seen:
        seen.add(name)
        zf.write(src, name)


def die(msg: str):
    sys.exit(f"pack: {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vision-rows")
    ap.add_argument("--audio-rows")
    ap.add_argument("--vision-cases")
    ap.add_argument("--audio-truth")
    ap.add_argument("--no-gate", action="store_true",
                    help="deliberately pack without the Mac gate's images and clips (otherwise both are required)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not a.no_gate:
        for flag, val, make in (("--vision-cases", a.vision_cases,
                                 "cd gate/vision && $PY make_images_more.py && $PY make_documents.py"),
                                ("--audio-truth", a.audio_truth, "cd gate/audio && $PY make_audio.py")):
            if not val:
                die(f"{flag} is required (the gate set rides along report-only); generate it with `{make}` "
                    f"(see gate/README.md) or pass --no-gate on purpose")
            if not Path(val).is_file():
                die(f"{flag} {val} does not exist; generate it with `{make}` (see gate/README.md)")
    seen: set = set()
    stats = {"vision": 0, "audio": 0, "gate_vision": 0, "gate_audio": 0, "files": 0}
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in CODE:
            if (HERE / name).is_file():
                zf.write(HERE / name, name)
        if a.vision_rows:
            root = Path(a.vision_rows).resolve().parent
            rows = R.load_vision_dev(a.vision_rows, "dev")
            print("vision dev rows:", R.vision_stats(rows))
            for r in rows:
                for rel in r["images"]:
                    _media(zf, root, rel, "data", seen)
            zf.writestr("data/vision_rows.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            stats["vision"] = len(rows)
        if a.audio_rows:
            root = Path(a.audio_rows).resolve().parent
            rows = R.load_audio_dev(a.audio_rows)
            for r in rows:
                _media(zf, root, R.audio_path(r) or "", "data", seen)
            zf.writestr("data/audio_rows.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            stats["audio"] = len(rows)
        if a.vision_cases:
            root = Path(a.vision_cases).resolve().parent
            zf.write(a.vision_cases, "data/gate_vision/cases.json")
            rows = R.vision_from_cases(a.vision_cases)
            for r in rows:
                for rel in r["images"]:
                    _media(zf, root, rel, "data/gate_vision", seen)
            stats["gate_vision"] = len(rows)
        if a.audio_truth:
            root = Path(a.audio_truth).resolve().parent
            zf.write(a.audio_truth, "data/gate_audio/truth.json")
            for r in R.audio_from_truth(a.audio_truth):
                _media(zf, root, R.audio_path(r), "data/gate_audio", seen)
                stats["gate_audio"] += 1
        stats["files"] = len(zf.namelist())
        def fail(msg):
            Path(a.out).unlink(missing_ok=True)       # never leave a zip that looks complete
            die(msg)
        if a.vision_rows and not stats["vision"]:
            fail("--vision-rows gave 0 dev rows; refusing to pack an eval with no vision regression data")
        if not a.no_gate:
            if not stats["gate_vision"] or not stats["gate_audio"]:
                fail(f"packed {stats['gate_vision']} gate images and {stats['gate_audio']} gate clips; "
                    "an empty gate set means the make scripts were not run (see gate/README.md)")
            if not any(r["kind"] == "document" for r in R.vision_from_cases(a.vision_cases)):
                fail("the gate cases.json has no scanned-document cases; run gate/vision/make_documents.py "
                    "after make_images_more.py")
    print(json.dumps(stats), "->", a.out, f"{Path(a.out).stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
