#!/usr/bin/env python3
"""Freeze the Mail/Messages caches and the commitments store for offline work.

Usage: python scripts/attention_snapshot.py [--out DIR]

Read-only on every source: the caches are copied and assistant.db is opened with
mode=ro. The snapshot holds real personal text, so it is written 0700/0600
under ~/.moe/attention/snapshots/ and the script refuses to write inside the
repository. Prints counts only, never message text.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.attention.corpus import ensure_private_tree, freeze_snapshot  # noqa: E402
from service.paths import MOE_DIR  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help="snapshot directory (default: a new dated one)")
    args = ap.parse_args(argv)
    out = args.out or MOE_DIR / "attention" / "snapshots" / time.strftime("%Y%m%dT%H%M%S")
    try:
        ensure_private_tree(MOE_DIR)
        freeze_snapshot(MOE_DIR, out)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    import json
    manifest = json.loads((out / "manifest.json").read_text())
    print(f"snapshot: {out}")
    print(f"counts:   {manifest['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
