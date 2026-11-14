"""The cloud key's Keychain read: one macOS question at a time, time to answer it, and
Connect (Everything) asks it once up front. Synthetic only: no Keychain, no network."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from service import main
from service.config import provider_credentials as secrets
from service.config.endpoints import EndpointConfigurationError

URL = "https://openrouter.ai"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(secrets, "_RECENT_FAILURE", {})
    monkeypatch.setattr(secrets, "_READ_LOCK", threading.Lock())
    monkeypatch.setattr(secrets.quarantine, "check", lambda: None)


def answer(value=b"k" * 32, code=0):
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=code, stdout=value + b"\n", stderr=b"")
    return calls, run


def test_read_waits_long_enough_for_the_password_question(monkeypatch):
    calls, run = answer()
    monkeypatch.setattr(secrets.subprocess, "run", run)
    assert secrets.resolve_keychain("cloud-a", URL) == "k" * 32
    assert calls[0]["timeout"] >= 60


def test_overlapping_silent_reads_wait_their_turn(monkeypatch):
    calls, run = answer()
    monkeypatch.setattr(secrets.subprocess, "run", run)
    secrets._READ_LOCK.acquire()      # another quick read is in progress
    threading.Timer(0.2, secrets._READ_LOCK.release).start()
    assert secrets.resolve_keychain("cloud-a", URL) == "k" * 32
    assert len(calls) == 1


def test_a_read_in_progress_is_never_joined_by_a_second_question(monkeypatch):
    calls, run = answer()
    monkeypatch.setattr(secrets.subprocess, "run", run)
    monkeypatch.setattr(secrets, "LOCK_WAIT_S", 0.1)
    secrets._READ_LOCK.acquire()      # another read is showing macOS's question
    try:
        with pytest.raises(EndpointConfigurationError, match="^Provider credential unavailable$"):
            secrets.resolve_keychain("cloud-a", URL)
    finally:
        secrets._READ_LOCK.release()
    assert calls == []


def test_a_refusal_is_not_asked_again_by_polls_but_connect_asks(monkeypatch):
    calls, run = answer(b"", code=128)       # Deny
    monkeypatch.setattr(secrets.subprocess, "run", run)
    with pytest.raises(EndpointConfigurationError):
        secrets.resolve_keychain("cloud-a", URL)
    with pytest.raises(EndpointConfigurationError):
        secrets.resolve_keychain("cloud-a", URL)
    assert len(calls) == 1                   # the poll did not put the question back
    _, run_ok = answer()
    monkeypatch.setattr(secrets.subprocess, "run", run_ok)
    assert secrets.resolve_keychain("cloud-a", URL, wait=True) == "k" * 32
    assert secrets.resolve_keychain("cloud-a", URL) == "k" * 32   # success clears the hold


def test_a_refusal_holds_only_its_own_account(monkeypatch):
    _, deny = answer(b"", code=128)
    monkeypatch.setattr(secrets.subprocess, "run", deny)
    with pytest.raises(EndpointConfigurationError):
        secrets.resolve_keychain("cloud-a", URL)
    _, ok = answer()
    monkeypatch.setattr(secrets.subprocess, "run", ok)
    assert secrets.resolve_keychain("cloud-b", URL) == "k" * 32


def test_the_lock_is_released_after_every_outcome(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("synthetic")
    monkeypatch.setattr(secrets.subprocess, "run", boom)
    with pytest.raises(EndpointConfigurationError):
        secrets.resolve_keychain("cloud-a", URL)
    assert secrets._READ_LOCK.acquire(blocking=False)
    secrets._READ_LOCK.release()


def everything_body(name="cloud-0123456789abcdef0123456789abcdef"):
    return {"provider": "openrouter", "base_url": URL, "api_prefix": "/api/v1",
            "model_id": "google/gemini-3.8-flash", "context_window": 32000,
            "roles": ["everything"], "super_model_enabled": False, "credential_name": name}


def test_connect_everything_reads_the_key_first_and_says_how_to_answer(monkeypatch):
    seen = []

    def refused(name, base_url, *, wait=False):
        seen.append((name, base_url, wait))
        raise EndpointConfigurationError("Provider credential unavailable")
    monkeypatch.setattr(secrets, "resolve_keychain", refused)
    monkeypatch.setattr(main, "remote_only_profile", lambda: None)
    saved = []
    import service.config as config
    monkeypatch.setattr(config, "set_cloud_everything", lambda *a, **k: saved.append(a))
    with pytest.raises(main.HTTPException) as raised:
        asyncio.run(main.connect_cloud_inference(everything_body()))
    assert raised.value.status_code == 400
    assert raised.value.detail == main.CLOUD_KEY_UNREADABLE and "Always Allow" in raised.value.detail
    assert seen == [("cloud-0123456789abcdef0123456789abcdef", URL, True)]
    assert saved == []                       # nothing moves to the cloud on an unread key


def test_connect_everything_still_reads_the_key_with_a_saved_profile(monkeypatch):
    seen = []
    monkeypatch.setattr(secrets, "resolve_keychain",
                        lambda name, base_url, *, wait=False: seen.append(wait) or "k" * 32)
    monkeypatch.setattr(main, "remote_only_profile", lambda: object())   # saved by an earlier Connect
    saved = []
    import service.config as config
    monkeypatch.setattr(config, "set_cloud_everything", lambda *a, **k: saved.append(a))

    async def settings():
        return {"enabled": True, "roles": ["everything"]}
    monkeypatch.setattr(main, "get_cloud_inference", settings)
    result = asyncio.run(main.connect_cloud_inference(everything_body()))
    assert result["roles"] == ["everything"]
    assert seen == [True] and len(saved) == 1
