"""Exercise pinned Understudy CLI locally; synthetic data, no model inference.

Usage: python3 run_pilot.py /path/to/built/understudy-agent-tools
Only the standard library is required. Raw outputs stay in a fresh temp directory.
"""
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PIN = "c70c889e5167a6e7a5f94f901411d42108c4ef2e"
tools = Path(sys.argv[1]).resolve()
assert subprocess.check_output(["git", "-C", str(tools), "rev-parse", "HEAD"], text=True).strip() == PIN
root = Path(tempfile.mkdtemp(prefix="wisp-orchestra-run-", dir="/private/tmp")).resolve()
os.umask(0o077)
node = shutil.which("node")
assert node
env = {"PATH": os.environ["PATH"], "UNDERSTUDY_TELEMETRY": "0", "TZ": "UTC", "TMPDIR": str(root)}
# Defense in depth: deny common Node network entrypoints. This is a smoke-test
# guard, not a general security sandbox. Node permissions also bound file access
# and disallow child processes/native addons/workers for the selected CLI calls.
guard = root / "offline.cjs"
guard.write_text('''const deny = () => { throw new Error("PILOT_NETWORK_DENIED"); };
for (const name of ["http", "https"]) {
  const m = require("node:" + name); m.request = deny; m.get = deny;
}
for (const name of ["net", "tls"]) {
  const m = require("node:" + name); m.connect = deny; m.createConnection = deny;
}
require("node:net").Socket.prototype.connect = deny;
globalThis.fetch = deny;
''')
records = []

def cli(*args, expect=0):
    cmd = [node, "--permission", f"--allow-fs-read={tools}",
           f"--allow-fs-read={root}", f"--allow-fs-write={root}",
           "--require", str(guard), str(tools / "dist/bin.js"), *map(str, args)]
    result = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    records.append({"args": list(map(str, args)), "exit_code": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr})
    (root / "commands.json").write_text(json.dumps(records, indent=2) + "\n")
    assert result.returncode == expect, records[-1]
    return result.stdout

source = root / "synthetic-intents.csv"
labels = ["calendar_read", "reminder_create", "message_draft", "clarify"]
groups = ["amber", "birch", "cedar", "dahlia", "elm", "fir", "ginkgo", "hazel", "iris", "juniper"]
rows = []
for label in labels:
    for group in groups:
        for variant in range(3):
            rows.append([f"Synthetic {label} request for {group}, variant {variant}", label, f"{label}-{group}"])
# An exact duplicate and an unlabeled row exercise data hygiene.
rows += [rows[0], ["Synthetic incomplete example", "", "unlabeled"]]
with source.open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["request", "label", "scenario"])
    writer.writerows(rows)

compiled = json.loads(cli("capture-import", "compile", "--source", source, "--output-root", root / "artifacts", "--json"))
card_path = Path(compiled["workload_card_path"])
artifact = card_path.parent
assert compiled["payload_read"] is False
inspection = json.loads(cli("capture-import", "inspect-csv", "--source", source, "--artifact-root", artifact, "--json"))
args = ["capture-import", "prepare-classification", "--source", source, "--artifact-root", artifact,
        "--input-column", "request", "--label-column", "label", "--group-column", "scenario", "--json"]
dataset = json.loads(cli(*args))
again = json.loads(cli(*args))
assert dataset["row_count"] == 120
assert dataset["duplicate_rows_removed"] == 1
assert dataset["unusable_rows_removed"] == 1
sets = {}
for split, entry in dataset["splits"].items():
    content = Path(entry["path"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == entry["sha256"] == again["splits"][split]["sha256"]
    examples = [json.loads(line) for line in content.splitlines()]
    assert {x["label"] for x in examples} == set(labels)
    sets[split] = {x["group_id"] for x in examples}
assert not (sets["train"] & sets["dev"] or sets["train"] & sets["holdout"] or sets["dev"] & sets["holdout"])

# Reject label leakage and source mutation after inspection.
bad_args = args.copy()
bad_args[bad_args.index("--input-column") + 1] = "label"
cli(*bad_args, expect=1)
assert "label column cannot also be an input" in records[-1]["stderr"]
original = source.read_bytes()
source.write_bytes(original + b"changed,label,group\n")
cli(*args, expect=1)
assert "changed after inspection" in records[-1]["stderr"]
source.write_bytes(original)

# No route is applied. Compare default planning with explicit local constraints.
default_route = root / "route-default.json"
cli("route-decision", "plan", "--workload-card", card_path, "--output", default_route, "--json")
default = json.loads(default_route.read_text())
card = json.loads(card_path.read_text())
original_mode = card["mode"]
card["fallback_route"] = {"kind": "local", "provider": "synthetic-local", "model": "not-executed"}
card["route_requirements"]["privacy_boundary"] = "local-only; no external payloads"
local_card = root / "explicit-local-card.json"
local_card.write_text(json.dumps(card))
local_route = root / "route-local.json"
cli("route-decision", "plan", "--workload-card", local_card, "--output", local_route, "--json")
local = json.loads(local_route.read_text())
assert local["candidate_routes"][0]["kind"] == "local"
assert local["decision"] == default["decision"] == "evaluate-first"
cli("optimize-workload", "check", "--repo", root, expect=1)

summary = {
    "vendor_commit": PIN, "node_version": subprocess.check_output([node, "--version"], text=True).strip(),
    "type": "real CLI; synthetic dataset; no inference; no mock model results",
    "rows_submitted": len(rows), "rows_retained": dataset["row_count"],
    "splits": {k: v["row_count"] for k, v in dataset["splits"].items()},
    "duplicate_rows_removed": dataset["duplicate_rows_removed"], "unusable_rows_removed": dataset["unusable_rows_removed"],
    "deterministic_hashes": True, "cross_split_group_overlap": 0, "all_labels_in_each_split": True,
    "label_leakage_rejected": True, "changed_source_rejected": True,
    "original_card_mode": original_mode, "default_route": default["candidate_routes"][0],
    "explicit_local_route": local["candidate_routes"][0], "optimizer_missing_evidence_blocked": True,
    "cli_commands": len(records), "expected_rejections": sum(r["exit_code"] != 0 for r in records),
    "provider_calls": 0, "api_spend_usd": 0, "raw_evidence_directory": str(root),
}
(root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
