#!/usr/bin/env python3
"""Build ONLY the given families with r5's real builder and write a build directory in r5's layout.

    build_family.py --out DIR FAMILY [FAMILY ...]

Same environment as r5's build (ASK_LORA_ROUND=4 ASK_LORA_R5=1) plus ASK_LORA_R51=1, and WISP_REPO defaulting to the
pin r5 used (training/ask-lora-r5/data/pin-7e442f2d). Everything is build.py's own: ``main(only=...)`` runs the scenarios
(same seeds, leak check against the pinned toolbench, harness, thinking), ``write`` does for_training, the split, the
eval manifest and the adjacent-duplicate fix, ``write_stats`` records code provenance, and ``partial.jsonl`` is written as
build.py's ``__main__`` does. Nothing here re-implements those.

Differences from a whole build, all inherent to building a subset:
- ``write`` splits the subset on its own (train/valid rows are only a stand-in: the merge step re-splits the union by group).
- ``fix_adjacent`` only sees this subset's rows, so a duplicate thought that sits across a family boundary in the whole
  build is not seen here; the final validate.py on the merged rows catches it.
- Unknown family names stop the build with the list of registered ones.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINING = HERE.parent.parent
BUILDER = TRAINING / "ask-lora-r2" / "builder"
PIN = TRAINING / "ask-lora-r5" / "data" / "pin-7e442f2d"


def redact(why) -> str:
    """A leak failure quotes the eval question it matched: keep the eval id and score, never the wording."""
    why = str(why)
    return (why.split("): ", 1)[0] + "): <eval wording withheld>") if why.startswith("leaks eval") else why[:200]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("families", nargs="+")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    if (out / "all.jsonl").exists():
        sys.exit(f"build_family: {out} already holds a build")
    out.mkdir(parents=True, exist_ok=True)
    # Set before build/scen/harness are imported: they read these at import time.
    os.environ["ASK_LORA_ROUND"] = "4"
    os.environ["ASK_LORA_R5"] = "1"
    os.environ["ASK_LORA_R51"] = "1"
    os.environ["ASK_LORA_OUT"] = str(out)
    os.environ["WISP_REPO"] = str(Path(os.environ.get("WISP_REPO") or PIN).resolve())
    sys.path.insert(0, str(BUILDER))
    os.chdir(BUILDER)          # build.py is run from its own folder in r5
    import build               # noqa: E402
    import scen                # noqa: E402

    only = set(a.families)
    unknown = sorted(only - set(scen.FAMILIES))
    if unknown:
        sys.exit(f"build_family: not a registered family: {unknown}. Registered: {sorted(scen.FAMILIES)}")
    examples, metas, failures, per_scen = build.main(only)
    print("scenarios ok per family:", dict(per_scen))
    print("examples:", len(examples), "failures:", len(failures))
    for sid, _prompt, why in failures:
        print("FAIL", sid, redact(why))
    if any(str(f[2]).startswith("thought:") for f in failures):
        sys.exit("build_family: thought failures, as in build.py")
    if not examples:
        sys.exit("build_family: no examples built")
    tr, va = build.write(examples, metas)
    build.write_stats(metas, tr, va)
    with open(out / "partial.jsonl", "w") as f:
        for ex, m in zip(examples, metas):
            f.write(json.dumps({"meta": m, **ex}, ensure_ascii=False) + "\n")
    print("per family:", dict(Counter(m["family"] for m in metas)), "train", len(tr), "valid", len(va))
    (out / "failures.json").write_text(json.dumps([{"id": sid, "why": redact(why)} for sid, _p, why in failures], indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
