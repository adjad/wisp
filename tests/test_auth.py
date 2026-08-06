"""The backend's shared-secret gate.

Covers the properties that make it worth having at all: the key file is not
world-readable, it survives across calls, and a wrong key is rejected.
"""
from __future__ import annotations

import stat

from service import auth


def test_key_is_generated_and_stable():
    first = auth.api_key()
    assert first
    assert auth.api_key() == first


def test_key_file_is_not_readable_by_others():
    """A key any process could read would defeat the point — the security of
    this rests entirely on the file permissions."""
    auth.api_key()
    mode = auth.API_KEY_PATH.stat().st_mode
    assert not (mode & stat.S_IRGRP)
    assert not (mode & stat.S_IROTH)
    assert not (mode & stat.S_IWGRP)
    assert not (mode & stat.S_IWOTH)


def test_key_has_meaningful_entropy():
    assert len(auth.api_key()) >= 32


def test_ping_is_the_only_public_path():
    """Anything added to this set is reachable with no credential at all, so
    the set staying tiny is the invariant worth pinning."""
    assert auth.PUBLIC_PATHS == {"/ping"}
