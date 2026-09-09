"""Run the complete isolated Python regression gate.

Usage: .venv/bin/python scripts/test_replay_failure_fixes.py
No live prompt replay or external send is part of this runner.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

# These legacy script-style contracts already fail on the branch base and are
# owned by PR #19. Keep running them so CI catches any additional regression,
# but ratchet the exact module baseline until that product fix lands. A passing
# module is also actionable: the stale entry must be removed instead of quietly
# turning into a permanent suppression.
KNOWN_BASELINE_FAILURES = frozenset({
    "tests/test_alias_reachability.py",
    "tests/test_forced_step_withholding.py",
    "tests/test_router_scoping.py",
    "tests/test_semantic_routing.py",
})


def _tests() -> list[Path]:
    """Discover every repository test module in the pytest-configured roots."""
    return sorted(
        (ROOT / "tests").glob("test_*.py")
    ) + sorted((ROOT / "air" / "tests").glob("test_*.py"))


def _command(path: Path) -> list[str]:
    """Use direct execution for legacy check-counter and unittest scripts."""
    source = path.read_text(encoding="utf-8")
    relative = str(path.relative_to(ROOT))
    if "if __name__ ==" in source or "sys.exit(" in source:
        return [sys.executable, relative]
    return [sys.executable, "-m", "pytest", "-q", "-rs", relative]


def _environment(state_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("WISP_"):
            env.pop(key, None)
    fake_home = Path(state_dir) / "home"
    fake_home.mkdir()
    env.update({
        "HOME": str(fake_home),
        "WISP_HOME": str(Path(state_dir) / "wisp"),
        "WISPAIR_HOME": str(Path(state_dir) / "air"),
        "WISP_TEST_PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONOPTIMIZE": "0",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    })
    return env


def _classify_failures(failed: set[str]) -> tuple[list[str], list[str]]:
    """Return new regressions and stale baseline entries, in stable order."""
    return (
        sorted(failed - KNOWN_BASELINE_FAILURES),
        sorted(KNOWN_BASELINE_FAILURES - failed),
    )


def main() -> int:
    tests = _tests()
    commands = [_command(path) for path in tests]
    failed: set[str] = set()
    for path, command in zip(tests, commands):
        print("Running:", " ".join(command), flush=True)
        with tempfile.TemporaryDirectory(prefix="wisp-regression-") as state_dir:
            env = _environment(state_dir)
            if subprocess.run(command, cwd=ROOT, env=env).returncode:
                failed.add(str(path.relative_to(ROOT)))
    regressions, resolved = _classify_failures(failed)
    print(
        f"Regression gate: {len(commands) - len(failed)}/{len(commands)} "
        "test modules passed",
        flush=True,
    )
    for path in sorted(failed & KNOWN_BASELINE_FAILURES):
        print(f"KNOWN FAILURE (PR #19): {path}", flush=True)
    for path in regressions:
        print(f"REGRESSION: {path}", flush=True)
    for path in resolved:
        print(f"BASELINE RESOLVED; remove suppression: {path}", flush=True)
    return 1 if regressions or resolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
