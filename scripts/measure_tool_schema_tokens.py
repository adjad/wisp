#!/usr/bin/env python3
"""Measure what the built-in tool schemas cost in prompt context, full vs terse.

    .venv/bin/python scripts/measure_tool_schema_tokens.py [-v]

Sizes are the compact JSON the model receives, per tool and in total, with
tokens approximated as chars/4 (no tokenizer is loaded). -v lists every tool.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import service.tools  # noqa: E402,F401  (registers every tool)
import os  # noqa: E402

from service.tools.registry import REGISTRY, model_schema  # noqa: E402


def _sizes(terse: bool) -> dict[str, int]:
    # Render straight from the registry so the unavailable update_event is
    # counted too; tool_schemas() would skip it.
    previous = os.environ.get("WISP_TERSE_TOOL_SCHEMAS")
    os.environ["WISP_TERSE_TOOL_SCHEMAS"] = "1" if terse else "0"
    try:
        return {n: len(json.dumps(model_schema(t.name, t.description, t.parameters),
                                  separators=(",", ":"))) for n, t in REGISTRY.items()}
    finally:
        if previous is None:
            os.environ.pop("WISP_TERSE_TOOL_SCHEMAS", None)
        else:
            os.environ["WISP_TERSE_TOOL_SCHEMAS"] = previous


def main(argv: list[str]) -> int:
    full, terse = _sizes(False), _sizes(True)
    names = sorted(n for n, t in REGISTRY.items()
                   if getattr(t.func, "__module__", "").startswith("service.tools."))
    if "-v" in argv:
        print(f"{'tool':28} {'full':>7} {'terse':>7}  (chars)")
        for n in names:
            print(f"{n:28} {full[n]:7} {terse[n]:7}")
    f, t = sum(full[n] for n in names), sum(terse[n] for n in names)
    print(f"tools measured: {len(names)}")
    print(f"full : {f:6} chars  ~{f // 4:5} tokens  (avg {f // len(names)} chars/tool)")
    print(f"terse: {t:6} chars  ~{t // 4:5} tokens  (avg {t // len(names)} chars/tool)")
    print(f"saved: {f - t:6} chars  ~{(f - t) // 4:5} tokens  ({100 * (f - t) / f:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
