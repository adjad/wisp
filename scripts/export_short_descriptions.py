#!/usr/bin/env python3
"""Export the terse tool descriptions for dataset rendering.

    .venv/bin/python scripts/export_short_descriptions.py [out.json]

Writes {"source_sha": <git HEAD>, "tools": {name: short}} to the path, or to
stdout when none is given. The SHA lets a dataset worker prove its training rows
were rendered from the descriptions the runtime will actually send. Stdlib plus
the repo's own module only; it does not import the tool registry or any model.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.tools.short_descriptions import SHORT  # noqa: E402


def _head_sha() -> str:
    try:
        return subprocess.run(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str]) -> int:
    payload = json.dumps({"source_sha": _head_sha(), "tools": dict(sorted(SHORT.items()))},
                         indent=2, ensure_ascii=False) + "\n"
    if len(argv) > 1:
        Path(argv[1]).write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
