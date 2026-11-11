#!/usr/bin/env python3
"""Why each base thought at a tool step was NOT kept. Ids and reason codes only; no thought or prompt text is written.

    trace_report.py --partial partial.jsonl --raw 'raw/*.jsonl' --out trace_rejections.jsonl

choose_traces.py keeps a thought only where base's own call matches the scripted call, and reports reject reasons as totals.
This reuses its ``reject_reasons`` (not a copy) to write the per-row reasons, so a tool step that keeps its scripted thought
(or, after a tool result, leaves the build) has its reason on record. A row with no sample at all is listed as ``no_sample``.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

GPU = Path(__file__).resolve().parents[2] / "ask-lora-r5" / "gpu"
sys.path.insert(0, str(GPU))
import choose_traces as CT  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--partial", required=True)
    ap.add_argument("--raw", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows = {r["meta"]["id"]: r for r in CT.read_jsonl(a.partial) if r["messages"][-1].get("tool_calls")}
    raws = sorted({p for pat in a.raw for p in (glob.glob(pat) or [pat])})
    seen, kept = set(), set()
    with open(a.out, "w", encoding="utf-8") as out:
        for path in raws:
            for rec in CT.read_jsonl(path):
                row = rows.get(rec.get("id"))
                if row is None or rec["id"] in kept:
                    continue
                seen.add(rec["id"])
                why = CT.reject_reasons(rec, row)
                if not why:
                    kept.add(rec["id"])
                    continue
                out.write(json.dumps({"id": rec["id"], "family": row["meta"]["family"], "k": rec.get("k"), "reasons": why}) + "\n")
        for i in sorted(set(rows) - seen):
            out.write(json.dumps({"id": i, "family": rows[i]["meta"]["family"], "k": None, "reasons": ["no_sample"]}) + "\n")
    print(json.dumps({"call_rows": len(rows), "kept": len(kept), "sampled": len(seen), "no_sample": len(rows) - len(seen)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
