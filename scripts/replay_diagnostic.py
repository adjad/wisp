#!/usr/bin/env python3
"""Offline Today replay or metadata timeline playback. Never runs the agent.

Usage: python scripts/replay_diagnostic.py Wisp-problem-….json
No service import, database, network, model, subprocess or native bridge.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024


def replay(report):
    details = report.get("details", {})
    capture = details.get("replay") if isinstance(details, dict) else None
    if capture is None:
        trace = report.get("trace", report)
        if not isinstance(trace, dict) or trace.get("schema_version") != 1:
            raise ValueError("No supported trace or planner capture in this report")
        events = trace.get("events")
        if not isinstance(events, list) or len(events) > 128:
            raise ValueError("Invalid trace timeline")
        # Playback is inspection only; arbitrary event values are never executed.
        return {"mode": "timeline", "trace_id": trace.get("trace_id"),
                "status": trace.get("status"), "events": events,
                "notice": "Metadata playback only. Agent/model/tool execution is unavailable."}
    if not isinstance(capture, dict) or capture.get("schema_version") != 1:
        raise ValueError("Unsupported planner capture version")
    for key in ("tasks", "commitments"):
        values = capture.get(key)
        if not isinstance(values, list) or len(values) > 1000 or not all(isinstance(v, dict) for v in values):
            raise ValueError("Planner input must contain at most 1000 tasks/commitments")
    if type(capture.get("now")) not in (int, float) or not math.isfinite(capture["now"]):
        raise ValueError("Capture requires a finite fixed clock")
    planner_path = Path(__file__).resolve().parents[1] / "service/assistant/today.py"
    fingerprint = hashlib.sha256(planner_path.read_bytes()).hexdigest()
    if capture.get("planner_fingerprint") != fingerprint:
        raise ValueError("Planner version differs from capture; check out its matching source before replaying")
    # Import the pure file directly. Importing service.assistant opens live stores.
    spec = importlib.util.spec_from_file_location("offline_today_planner", planner_path)
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    plan = planner.build_plan(capture["day"], capture["timezone"], capture["tasks"],
                              capture["commitments"], capture["preferences"], capture["sources"],
                              now=capture["now"])
    matched = plan == capture.get("expected_plan")
    return {"mode": "today", "matches_capture": matched, "plan": plan}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        with args.report.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Report exceeds 2 MB")
        report = json.loads(data, parse_constant=lambda v: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            raise ValueError("Unsupported report version")
        result = replay(report)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result.get("matches_capture", True) else 1
    except (ValueError, KeyError, TypeError, OSError, RecursionError, OverflowError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
