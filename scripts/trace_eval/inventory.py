#!/usr/bin/env python3
"""Deduplicate Wisp debug exports into a content-free annotation queue.

This deliberately emits no prompt, answer, reasoning, tool arguments, tool
results, or raw model I/O. Originals stay at the supplied local paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from scripts.trace_eval.core import digest

KNOWN_MODELS = {"Ling-3.0-tiny-oQ6e", "Ling-3.0-tiny-oQ4e",
                "Agents-A1-4B-oQe6", "Huihui-Ornith-1.5-9B-abliterated-oQ6e"}


def build(paths: list[Path]) -> dict:
    seen: set[tuple[str, str]] = set()
    queue = []
    sessions = set()
    for path in paths:
        data = json.loads(path.read_text())
        if not isinstance(data.get("turns"), list):
            raise ValueError(f"invalid Wisp debug export: {path.name}")
        # Some older exports have an empty session ID. Keep them in separate
        # file-scoped groups rather than falsely merging unrelated turns.
        missing_session = not bool(data.get("session_id"))
        session = str(data.get("session_id") or path.name)
        session_hash = hashlib.sha256(session.encode()).hexdigest()[:16]
        sessions.add(session_hash)
        for index, turn in enumerate(data["turns"]):
            turn_hash = digest(turn)
            key = (session_hash, turn_hash)
            if key in seen:
                continue
            seen.add(key)
            calls = turn.get("tool_calls") or []
            role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "other"
            model = turn.get("model") if turn.get("model") in KNOWN_MODELS else "other_or_missing"
            queue.append({"source_file_sha256": hashlib.sha256(path.name.encode()).hexdigest()[:16],
                          "turn_index": index,
                          "session_hash": session_hash, "session_id_missing": missing_session,
                          "turn_sha256": turn_hash,
                          "role": role, "model": model,
                          "tool_count": len(calls) if isinstance(calls, list) else 0,
                          "has_raw_model_io": bool(turn.get("raw_model_io")),
                          "needs_human_label": role == "assistant"})
    return {"schema_version": 1, "source_files": len(paths),
            "unique_sessions": len(sessions), "unique_turns": len(queue),
            "models": dict(Counter(str(t["model"]) for t in queue
                                   if t["role"] == "assistant")),
            "queue": queue}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true",
                        help="replace an existing local annotation queue")
    args = parser.parse_args()
    private_root = (Path(__file__).resolve().parents[2] / ".evo/trace_eval").resolve()
    output = args.output.resolve()
    if not output.is_relative_to(private_root) or output == private_root:
        raise ValueError("output must be a child of the local .evo/trace_eval directory")
    if output.exists() and not args.replace:
        raise ValueError("output exists; use --replace to rebuild the local queue")
    result = build(args.paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "queue"}, indent=2))


if __name__ == "__main__":
    main()
