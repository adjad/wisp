"""Shared fixtures.

The most important thing here is that no test ever touches the developer's real
assistant state. `WISP_HOME` is redirected to a temp directory for the whole
session before anything under `service/` is imported, so the modules that build
their paths at import time (see service/paths.py) pick up the scratch location.
"""
from __future__ import annotations

import os
import tempfile

# Must happen before the first `import service.*` anywhere in the suite.
_SCRATCH = tempfile.mkdtemp(prefix="wisp-tests-")
os.environ["WISP_HOME"] = _SCRATCH

import pytest  # noqa: E402

from service.safety import policy  # noqa: E402


@pytest.fixture
def modes():
    """Set read_only/full_access for one test and restore them afterwards.

    Both are process-global module state, so without this a test that flips a
    mode leaks into every test that runs after it.
    """
    original = (policy.read_only(), policy.full_access())

    def _set(*, read_only: bool = False, full_access: bool = False):
        policy.set_read_only(read_only)
        policy.set_full_access(full_access)

    yield _set

    policy.set_read_only(original[0])
    policy.set_full_access(original[1])
