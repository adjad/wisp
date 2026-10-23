"""Run with python -m scripts.ling_diagnostics. Offline by default."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from .core import (CORPUS, ROOT, SCHEMA, SYSTEMS, ReplayTransport, canonical,
                   digest, load_cases, markdown_report, run_case, summarize)


def select(cases, ids, categories, limit):
    wanted = set(filter(None, ids.split(",")))
    cats = set(filter(None, categories.split(",")))
    if wanted - {c["id"] for c in cases} or cats - {c["category"] for c in cases}:
        raise ValueError("Unknown case ID/category")
    result = [c for c in cases if (not wanted or c["id"] in wanted)
              and (not cats or c["category"] in cats)]
    if limit:
        result = result[:limit]
    if not result:
        raise ValueError("Selection is empty")
    return result


def source_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       text=True, stderr=subprocess.DEVNULL, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def prepare(args, cases):
    selected = select(cases, args.ids, args.categories, args.limit)
    variants = args.variants.split(",")
    if not variants or len(set(variants)) != len(variants) or set(variants) - SYSTEMS.keys():
        raise ValueError("Variants must be distinct baseline and/or grounded")
    if not 1 <= args.reps <= 5 or len(selected) * args.reps * len(variants) > 256:
        raise ValueError("Use 1–5 repetitions and at most 256 case/variant/repetition runs")
    if not 1 <= args.wall_timeout <= 3600:
        raise ValueError("Suite wall timeout must be 1–3600 seconds")
    metadata = {"schema": SCHEMA, "mode": args.command, "source_sha": source_sha(),
                "corpus_sha256": digest(cases), "selected_case_ids": [c["id"] for c in selected],
                "variants": variants, "repetitions": args.reps, "model": args.model,
                "max_tokens": args.max_tokens, "temperature": 0, "turn_timeout_s": args.timeout,
                "wall_timeout_s": args.wall_timeout, "max_steps": args.max_steps,
                "system_prompts": {v: SYSTEMS[v] for v in variants},
                "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "state": "prepared", "expected_rows": len(selected) * len(variants) * args.reps,
                "limitations": ["Synthetic direct model/fixture contract only; no actual Wisp router/tools.",
                                "Prose quality is ungraded; exact contract checks can be strict.",
                                "This held-out diagnostic is not a training corpus or a release gate."]}
    # New directory required; old evidence is never replaced or appended silently.
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return selected, variants, metadata


async def execute(args, cases):
    selected, variants, metadata = prepare(args, cases)
    records, transport = [], None
    try:
        if args.command == "replay":
            data = json.loads(args.responses.read_text())
            if set(data) != {"schema", "description", "runs"} or data["schema"] != SCHEMA:
                raise ValueError("Unsupported recording schema")
            recordings = {}
            for row in data["runs"]:
                if set(row) != {"case_id", "variant", "repetition", "responses"}:
                    raise ValueError("Malformed recording row")
                key = (row["case_id"], row["variant"], row["repetition"])
                if key in recordings or not isinstance(row["responses"], list):
                    raise ValueError("Duplicate recording or invalid responses")
                recordings[key] = row["responses"]
            metadata["recordings_sha256"] = hashlib.sha256(args.responses.read_bytes()).hexdigest()
        else:
            from .live import open_transport
            transport = await open_transport(args)
        started = time.monotonic()
        metadata["state"] = "running"
        with (args.out / "results.jsonl").open("x") as fh:
            # Rotate variant order each repetition to reduce simple ordering bias.
            for rep in range(1, args.reps + 1):
                for case in selected:
                    order = variants if rep % 2 else list(reversed(variants))
                    for variant in order:
                        remaining = args.wall_timeout - (time.monotonic() - started)
                        if remaining < 1:
                            raise TimeoutError("Suite wall budget exhausted; partial evidence retained")
                        active = transport
                        if args.command == "replay":
                            key = (case["id"], variant, rep)
                            if key not in recordings:
                                raise ValueError(f"Missing recording for {key}")
                            active = ReplayTransport(recordings[key])
                        row = await run_case(case, active, model=args.model, mode=args.command,
                                             variant=variant, repetition=rep, max_steps=args.max_steps,
                                             timeout=min(args.timeout, remaining), max_tokens=args.max_tokens)
                        records.append(row)
                        fh.write(canonical(row) + "\n")
                        fh.flush()
                        print(f"{len(records)}/{metadata['expected_rows']} {case['id']} "
                              f"{variant} rep={rep}: {row['status']}", flush=True)
                        if args.command == "replay" and active.responses:
                            raise ValueError("Unused recorded responses: recording does not match the case run")
        metadata["state"] = "complete"
    except BaseException as exc:
        metadata["state"] = "incomplete"
        metadata["error_type"] = type(exc).__name__
        raise
    finally:
        if transport is not None:
            await transport.aclose()
        metadata["completed_rows"] = len(records)
        metadata["ended_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (args.out / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (args.out / "summary.json").write_text(json.dumps(summarize(records), indent=2) + "\n")
        (args.out / "report.md").write_text(markdown_report(records, metadata))
    return 1 if any(r["status"] == "fail" for r in records) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "validate", "replay", "live", "report"])
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--ids", default="")
    parser.add_argument("--categories", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--variants", default="baseline")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--model", default="offline-fixture")
    parser.add_argument("--responses", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--wall-timeout", type=float, default=600)
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--original-home", type=Path,
                        help="Live-only: original home, denied by the outer OS sandbox except scoped engine auth")
    args = parser.parse_args(argv)
    try:
        if args.limit < 0 or not 1 <= args.timeout <= 60 or not 1 <= args.max_steps <= 4 or not 32 <= args.max_tokens <= 1024:
            raise ValueError("Invalid selection or runtime budget")
        if args.command == "report":
            if args.out is None:
                raise ValueError("--out is required")
            print((args.out / "report.md").read_text())
            return 0
        cases = load_cases(args.corpus)
        if args.command == "validate":
            print(f"Validated {len(cases)} synthetic cases; no inference or service imports.")
            return 0
        if args.command == "list":
            for case in select(cases, args.ids, args.categories, args.limit):
                print(f"{case['id']:16} {case['category']:18} {case['rubric']}")
            return 0
        if args.out is None or (args.command == "replay" and args.responses is None):
            raise ValueError("--out is required; replay also requires --responses")
        if args.command == "live" and (args.model == "offline-fixture" or "ling" not in args.model.lower()):
            raise ValueError("Live mode requires the exact already-resident Ling --model ID")
        return asyncio.run(execute(args, cases))
    except Exception as exc:
        print(f"Diagnostic stopped: {type(exc).__name__}", file=sys.stderr)
        # No raw authenticated transport exceptions may be printed.
        if isinstance(exc, (ValueError, FileExistsError)):
            print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
