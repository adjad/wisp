"""web_fetch's address filter.

web_fetch takes a URL the model chose, and the model can be steered by whatever
text it just read — a web page, an email, a message. Without this filter it is a
way to make Wisp probe its own machine and LAN: oMLX on 127.0.0.1:8000, Wisp's
own backend on :8765, a router admin page, cloud metadata at 169.254.169.254.

These tests exercise `_blocked_reason` directly rather than `web_fetch`, so they
make no network requests and run identically in CI.
"""
from __future__ import annotations

import pytest

from service.tools.web_tools import _blocked_reason


@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "localhost",
    "0.0.0.0",
    "::1",
    "10.0.0.1",
    "192.168.1.1",
    "172.16.0.1",
    "169.254.169.254",   # cloud instance metadata
])
def test_private_and_loopback_hosts_are_refused(host):
    assert _blocked_reason(host) is not None


@pytest.mark.parametrize("host", ["example.com", "wttr.in"])
def test_public_hosts_are_allowed(host):
    # Needs DNS; skip rather than fail when the sandbox has no resolver.
    reason = _blocked_reason(host)
    if reason and "could not resolve" in reason:
        pytest.skip(f"no DNS available for {host}")
    assert reason is None


def test_empty_host_is_refused():
    assert _blocked_reason("") is not None


def test_unresolvable_host_is_refused():
    """Failing closed matters here: a name that doesn't resolve must not be
    treated as "not private, therefore fine"."""
    assert _blocked_reason("this-host-does-not-exist.invalid") is not None
