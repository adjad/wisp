#!/usr/bin/env python3
"""Re-score saved synthetic traces, including exact-hash human reviews."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.trace_eval.core import load_cases, score, verify_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--traces", type=Path, required=True,
                        help="directory containing cases/<id>.json")
    parser.add_argument("--reviews", type=Path,
                        help="local JSON object keyed by case ID; never inferred from logs")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.manifest:
        verify_manifest(args.cases, args.manifest)
    cases = load_cases(args.cases)
    reviews = json.loads(args.reviews.read_text()) if args.reviews else {}
    verdicts = []
    for case in cases:
        path = args.traces / "cases" / (case["id"] + ".json")
        if not path.exists():
            verdicts.append({"case_id": case["id"], "status": "MISSING",
                             "failures": ["trace missing"]})
            continue
        verdicts.append(score(case, json.loads(path.read_text()),
                              review=reviews.get(case["id"])))
    counts = {status: sum(v["status"] == status for v in verdicts)
              for status in ("PASS", "FAIL", "UNVERIFIED", "MISSING")}
    report = {"cases_sha256": __import__("hashlib").sha256(args.cases.read_bytes()).hexdigest(),
              "counts": counts, "cases": verdicts,
              "strict_pass_rate": round(counts["PASS"] / len(verdicts), 4) if verdicts else 0}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
