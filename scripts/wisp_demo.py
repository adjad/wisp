#!/usr/bin/env python3
"""Start or end a fresh-install demo of Wisp. See docs/fresh-demo.md and service/demo_mode.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.demo_mode import main  # noqa: E402  (stdlib only; touches no ~/.moe state)

if __name__ == "__main__":
    sys.exit(main())
