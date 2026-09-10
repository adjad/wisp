"""Repository-wide pytest safety bootstrap.

This file is imported before test modules, which is early enough to keep
service modules with import-time stores away from the user's real Wisp state.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

import pytest


_REAL_WISP_HOME = (Path.home() / ".moe").resolve()
_PYTEST_STATE = tempfile.TemporaryDirectory(prefix="wisp-pytest-state-")
_ISOLATED_ROOT = Path(_PYTEST_STATE.name).resolve()

# Never inherit an opt-in that could activate live tests, host credentials, or
# installed templates. Tests that need one set it explicitly with monkeypatch.
for _key in tuple(os.environ):
    if _key.startswith("WISP_"):
        os.environ.pop(_key, None)

os.environ.update({
    "WISP_HOME": str(_ISOLATED_ROOT / "wisp"),
    "WISPAIR_HOME": str(_ISOLATED_ROOT / "air"),
    "WISP_TEST_PYTHON": sys.executable,
    "PYTHONDONTWRITEBYTECODE": "1",
})


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail closed if Wisp storage escaped the disposable test directory."""
    from service.paths import MOE_DIR

    resolved = MOE_DIR.resolve()
    if resolved == _REAL_WISP_HOME or not resolved.is_relative_to(_ISOLATED_ROOT):
        raise pytest.UsageError(
            f"refusing to run tests with Wisp state at {resolved}; "
            f"expected a path under {_ISOLATED_ROOT}"
        )


def pytest_unconfigure(config: pytest.Config) -> None:
    _PYTEST_STATE.cleanup()
