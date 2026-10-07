"""Summarize existing model timings without publishing prompts or running models."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics


def summarize(root):
    arms = {}
    ids = None
    for arm in ("base", "v1", "v2"):
        folder = Path(root) / arm
        raw = folder / "raw.jsonl"
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        manifest = json.loads((folder / "manifest.json").read_text())
        if manifest["descriptor"].get("simulation") is not False:
            raise ValueError("Only archived actual inference is eligible")
        selected = [r for r in rows if r["lane"] == "routing" and not r["first_request_after_load"]]
        if any(r["status"] != "ok" or not r["usable_completion"] for r in selected):
            raise ValueError("Archive contains failed/incomplete routing trials; report separately")
        current_ids = [r["id"] for r in selected]
        if ids is not None and current_ids != ids:
            raise ValueError("Arms do not contain the same warm routing requests")
        ids = current_ids
        values = sorted(r["wall_s"] for r in selected)
        if any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("Invalid wall-clock measurements")
        arms[arm] = {"n": len(values), "p50_s": statistics.median(values),
            "p95_s": values[math.ceil(.95 * len(values))-1],
            "excluded_first_after_load": sum(r["lane"] == "routing" and r["first_request_after_load"] for r in rows),
            "backend": manifest["descriptor"]["backend"],
            "quantization": manifest["descriptor"]["quantization"],
            "policy": manifest["policy"],
            "inputs": {name: hashlib.sha256((folder/name).read_bytes()).hexdigest()
                       for name in ("raw.jsonl", "manifest.json", "runtime-0.json")}}
    return {"actual_archived_inference": True, "new_inference": False,
            "percentile_method": "median; p95 nearest rank", "arms": arms,
            "limits": ["Sequential cloud HF/PEFT runs, not paired Wisp latency",
                       "Output length and correctness differ between arms",
                       "No V2 Mac merged/quantized serving measurement", "No prompt contents exported"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.archive_root)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
