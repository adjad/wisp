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


def main():
    commands = [_command(path) for path in _tests()]
    failed = []
    for command in commands:
        print("Running:", " ".join(command), flush=True)
        with tempfile.TemporaryDirectory(prefix="wisp-regression-") as state_dir:
            env = _environment(state_dir)
            if subprocess.run(command, cwd=ROOT, env=env).returncode:
                failed.append(command)
    print(
        f"Regression gate: {len(commands) - len(failed)}/{len(commands)} "
        "test modules passed",
        flush=True,
    )
    for command in failed:
        print("FAILED:", " ".join(command), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
