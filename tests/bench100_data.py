"""The full 100-problem suite: the original 50 basics (tests/bench50_data.py)
plus 50 advanced/agentic-flavored problems (tests/bench_advanced_data.py) —
OOP/data structures, generators/decorators, regex/parsing, graph & tree
algorithms, exception handling, and async. See tests/bench100_run.py.
"""
from __future__ import annotations

from tests.bench50_data import PROBLEMS as _BASIC
from tests.bench_advanced_data import PROBLEMS as _ADVANCED

PROBLEMS: list[dict] = [*_BASIC, *_ADVANCED]

assert len(PROBLEMS) == 100, f"expected 100 problems, got {len(PROBLEMS)}"
assert len({p["id"] for p in PROBLEMS}) == 100, "duplicate problem ids found"
