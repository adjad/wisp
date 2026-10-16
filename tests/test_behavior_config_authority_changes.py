"""Synthetic configuration changes withdraw existing history permission."""
import os

import pytest

from service import config
from service.assistant.gmail_history_runtime import HistoryHost
from tests.test_behavior_gmail_history import CoreBridgeBackend, NOW, command


@pytest.fixture
def configured(tmp_path, monkeypatch):
    path = tmp_path/"config.yaml"
    monkeypatch.setattr(config, "USER_CONFIG", path)
    config._user_overlay.cache_clear();config.models_config.cache_clear()
    config.models_config()
    backend = CoreBridgeBackend([NOW])
    host = HistoryHost(backend=backend)
    host.bind(enabled=True,settings={"email":True},epoch="email-consent",role="fake-local-role")
    host.control(command(host))
    assert len(host.cached_getters()) == 1 and backend.opened[0].status()["state"] == "running"
    yield host,backend,path
    host.invalidate("fixture complete",close=True)
    config._user_overlay.cache_clear();config.models_config.cache_clear()


def test_global_learning_change_withdraws_before_overlay_replace(configured, monkeypatch):
    host,backend,path = configured
    original = os.replace
    observed = []
    def replacement(source,destination):
        observed.append(True)
        assert host.cached_getters() == ()
        assert backend.opened[0].status()["state"] != "running"
        return original(source,destination)
    monkeypatch.setattr(os,"replace",replacement)
    config.set_behavior_enabled(False)
    assert observed and host.cached_getters() == ()


@pytest.mark.parametrize("setter", [lambda:config.set_roles({"fast":"synthetic-model"}),
                                    lambda:config.set_role("reasoning","synthetic-model")])
def test_direct_public_role_setter_withdraws_without_runtime_poll(configured, setter):
    host,backend,path = configured
    setter()
    assert host.cached_getters() == ()
    assert backend.opened[0].status()["state"] != "running"


def test_overlay_failure_does_not_preserve_a_previous_grant(configured, monkeypatch):
    host,backend,path = configured
    def fail(*args,**kwargs):
        assert host.cached_getters() == ()
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(os,"replace",fail)
    with pytest.raises(OSError):
        config.set_behavior_enabled(False)
    assert host.cached_getters() == () and backend.opened[0].status()["state"] != "running"


@pytest.mark.parametrize("setter", [
    lambda:config.set_local_provider({"provider":"openai-compatible", "base_url":"http://127.0.0.1:5555","api_prefix":"/v1",
        "credential_ref":"none"},"synthetic-model",8192,["reasoning"]),
    lambda:config.set_cloud_provider({"base_url":"https://provider.invalid","api_prefix":"/v1",
        "credential_ref":"env:SYNTHETIC_ONLY"},"synthetic-model",8192,["research"]),
])
def test_provider_config_setters_withdraw_existing_job(configured, setter):
    host,backend,path = configured
    setter()
    assert host.cached_getters() == () and backend.opened[0].status()["state"] != "running"


def test_lost_notification_cannot_preserve_or_mint_a_grant(configured, monkeypatch):
    from service.config import authority_changes as events
    from service.assistant.gmail_history_runtime import HistoryUnavailable
    host,backend,path = configured
    before = events.current()
    def broken(*args,**kwargs):
        raise RuntimeError("synthetic cleanup subscriber failure")
    monkeypatch.setattr(host,"invalidate",broken)
    config.set_behavior_enabled(False)
    assert events.current() is not before and not events.current().ready
    assert host.cached_getters() == () and backend.opened[0].snapshot() is None
    config.models_config()  # A fresh read may settle metadata, never old permission.
    assert events.current().ready
    with pytest.raises(HistoryUnavailable):
        host.control(command(host))
    monkeypatch.undo()


def test_nested_and_concurrent_writes_remain_unsettled(configured, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier,Event
    from service.config import authority_changes as events
    from service.assistant.gmail_history_runtime import HistoryUnavailable
    host,backend,path = configured
    entered = Barrier(3)
    release = Event()
    original = config._write_overlay
    def delayed(update):
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return original(update)
    monkeypatch.setattr(config,"_write_overlay",delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(config.set_role,"fast","synthetic-one")
        b = pool.submit(config.set_role,"reasoning","synthetic-two")
        entered.wait(timeout=5)
        try:
            assert events.current().pending == 2 and not events.current().ready
            host.bind(enabled=True,settings={"email":True},epoch="new",role="fake-local-role")
            with pytest.raises(HistoryUnavailable):
                host.control(command(host))
            assert host.cached_getters() == ()
        finally:
            release.set()
        a.result(timeout=5);b.result(timeout=5)
    assert events.current().pending == 0 and not events.current().ready
    cfg = config.models_config()
    assert cfg["roles"]["fast"] == "synthetic-one" and cfg["roles"]["reasoning"] == "synthetic-two"
    assert events.current().ready and host.cached_getters() == ()


def test_explicit_reload_and_invalid_overlay_withdraw_current_proof(configured):
    from service.config import authority_changes as events
    host,backend,path = configured
    path.write_text("behavior:\n  enabled: false\n")
    config.models_config.cache_clear()
    assert host.cached_getters() == () and not events.current().ready
    assert config.get_behavior_enabled() is False
    assert events.current().ready
    path.write_text("[not, a, settings, mapping]")
    config._user_overlay.cache_clear()
    assert config.get_behavior_enabled() is False and not events.current().ready


def test_unrelated_preference_does_not_reset_authority(configured):
    from service.config import authority_changes as events
    host,backend,path = configured
    before = events.current()
    config.set_daily_summary_hour(20)
    assert events.current() is before and len(host.cached_getters()) == 1


def test_history_admission_probe_reads_only_atomic_memory(configured, monkeypatch):
    host,backend,path = configured
    def forbidden(*args,**kwargs):
        raise AssertionError("Admission tried to resolve settings or I/O")
    monkeypatch.setattr(config,"models_config",forbidden)
    monkeypatch.setattr(config,"get_behavior_enabled",forbidden)
    # Actual source core invokes the installed host probe under its source lock.
    assert backend.opened[0].status()["state"] == "running"
    assert len(host.cached_getters()) == 1
    monkeypatch.undo()
