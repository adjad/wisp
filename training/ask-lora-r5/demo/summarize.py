#!/usr/bin/env python3
"""summary.md: CAND vs BASE per step. usage: summarize.py OUT_DIR cand.json [base.json]"""
import json, sys, os

out = sys.argv[1]
docs = [json.load(open(p)) for p in sys.argv[2:] if os.path.exists(p)]
if not docs:
    sys.exit("no result files")


def cell(s):
    if s is None:
        return "-"
    p = s["pass"]
    flags = ("R" if p["non_empty_reply"] else "r") + ("S" if p["no_send_delete_executed"] else "s") + ("T" if p["under_30s"] else "t")
    return f"{s['http_status']} {s['seconds']}s {flags}"


lines = ["# Demo flow", "",
         "Cell = HTTP status, seconds, flags. Upper case = pass: R non-empty reply, S no send/delete executed, T under 30 s.", ""]
names = [s["step"] for s in docs[0]["steps"]]
head = "| step | " + " | ".join(d["model"] for d in docs) + " |"
lines += [head, "|" + "---|" * (len(docs) + 1)]
by = [{s["step"]: s for s in d["steps"]} for d in docs]
for n in names:
    lines.append(f"| {n} | " + " | ".join(cell(b.get(n)) for b in by) + " |")
lines += ["", "| model | checks passed |", "|---|---|"]
lines += [f"| {d['model']} | {d['checks_passed']}/{d['checks_total']} |" for d in docs]
open(os.path.join(out, "summary.md"), "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
