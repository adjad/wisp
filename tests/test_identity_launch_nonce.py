"""/identity echoes the app's launch nonce only when the app supplied one.

The app records a launch receipt (pid, kernel start time, nonce) for the backend it
starts and passes the nonce in WISP_LAUNCH_NONCE. /identity echoes it so the app can
tell its own launch from any other process. No environment variable: no field.
"""
from __future__ import annotations

from service import identity


def test_launch_nonce_echoed_when_set(monkeypatch):
    monkeypatch.setenv("WISP_LAUNCH_NONCE", "3F2A-launch")
    body = identity.payload()
    assert body["launch_nonce"] == "3F2A-launch"
    assert body["service"] == "wisp-backend"


def test_launch_nonce_absent_without_env(monkeypatch):
    monkeypatch.delenv("WISP_LAUNCH_NONCE", raising=False)
    assert "launch_nonce" not in identity.payload()


def test_empty_launch_nonce_is_absent(monkeypatch):
    monkeypatch.setenv("WISP_LAUNCH_NONCE", "")
    assert "launch_nonce" not in identity.payload()
