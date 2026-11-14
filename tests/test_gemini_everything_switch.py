"""The "Gemini handles everything" switch (Settings > Advanced).

Synthetic only: an in-memory config, no Keychain, no network, no key. Checks that the
switch turns on only over a saved Gemini API connection whose key was read and tested
at the click, that it writes the remote-only profile plus remote_parity, and that
turning it off removes both, restores the Super Model setting and keeps the Gemini
connection and its cloud roles.
"""
import asyncio
import copy

import pytest
from fastapi import HTTPException

from service import config, main
from service.config.endpoints import EndpointConfigurationError
from service.inference import remote_policy

GEMINI_ENDPOINT = {"enabled": True, "provider": "gemini",
                   "base_url": "https://generativelanguage.googleapis.com",
                   "api_prefix": "/v1beta/openai", "credential_ref": "keychain:cloud-test",
                   "readiness_timeout": 10}
RUNNING = {"profile": None}
OPENROUTER_ENDPOINT = {**GEMINI_ENDPOINT, "provider": "openrouter",
                       "base_url": "https://openrouter.ai", "api_prefix": "/api/v1"}


@pytest.fixture
def store(monkeypatch):
    cfg = {"inference": {
        "endpoints": {"cloud": copy.deepcopy(GEMINI_ENDPOINT)},
        "bindings": {"research": {"endpoint": "cloud", "model_id": "gemini-3.8-flash",
                                  "context_window": 32000}},
        "super_model": {"enabled": True, "model_id": "gemini-3.8-flash", "context_window": 32000},
    }}

    def save(update, **_kwargs):
        merged = config._deep_merge(cfg, update)
        cfg.clear()
        cfg.update(merged)
        remote_policy.remote_only_profile.cache_clear()

    monkeypatch.setattr(config, "models_config", lambda: copy.deepcopy(cfg))
    monkeypatch.setattr(config, "_save_overlay", save)
    reads, probes = [], []

    def resolve(name, base_url, *, wait=False):
        reads.append((name, base_url, wait))
        return "synthetic-test-value"

    async def probe(checked, model_id, key):
        probes.append((checked.base_url, model_id, key))

    monkeypatch.setattr("service.config.provider_credentials.resolve_keychain", resolve)
    monkeypatch.setattr(main, "_remote_only_switch_probe", probe)
    # What the running service answers with: the startup profile, not the saved config.
    RUNNING["profile"] = None
    monkeypatch.setattr(main, "remote_only_profile", lambda: RUNNING["profile"])
    return cfg, reads, probes


def post(enabled):
    return asyncio.run(main.set_gemini_everything({"enabled": enabled}))


def test_off_by_default_and_available_over_gemini(store):
    state = asyncio.run(main.get_gemini_everything())
    assert state["available"] is True and state["enabled"] is False
    assert state["restart_required"] is False


def test_turning_on_reads_and_tests_the_key_then_writes_the_profile(store):
    cfg, reads, probes = store
    state = post(True)
    assert reads == [("cloud-test", GEMINI_ENDPOINT["base_url"], True)]
    assert probes == [(GEMINI_ENDPOINT["base_url"], "gemini-3.8-flash", "synthetic-test-value")]
    assert cfg["remote_parity"] is True
    profile = cfg["inference"]["remote_only"]
    assert profile["enabled"] is True and profile["base_url"] == GEMINI_ENDPOINT["base_url"]
    assert profile["model_id"] == "gemini-3.8-flash"
    assert profile["context_window"] == remote_policy.remote_context_window("gemini-3.8-flash")
    assert "synthetic-test-value" not in repr(cfg)          # the key is never saved
    assert state["enabled"] is True and state["restart_required"] is True
    assert config.cloud_everything_enabled() is True


def test_turning_off_removes_everything_it_added_and_keeps_the_connection(store):
    cfg, _reads, _probes = store
    before = copy.deepcopy(cfg)
    post(True)
    post(False)
    assert cfg["remote_parity"] is False
    assert cfg["inference"]["remote_only"]["enabled"] is False
    assert config.cloud_everything_enabled() is False
    assert cfg["inference"]["endpoints"]["cloud"] == before["inference"]["endpoints"]["cloud"]
    assert cfg["inference"]["bindings"] == before["inference"]["bindings"]
    assert cfg["inference"]["super_model"] == before["inference"]["super_model"]
    assert config.cloud_provider_settings()["roles"] == ["research"]


def test_turning_off_asks_for_a_restart_while_the_service_still_runs_the_profile(store):
    post(True)
    # The service restarted onto the profile; remote_parity alone is not what it runs.
    RUNNING["profile"] = object()
    state = post(False)
    assert state["enabled"] is False and state["active"] is True
    assert state["restart_required"] is True                 # the app restarts at once
    RUNNING["profile"] = None                          # after that restart
    state = asyncio.run(main.get_gemini_everything())
    assert state["restart_required"] is False and state["active"] is False


def test_cloud_roles_are_reported_so_off_never_claims_everything_stays_here(store):
    cfg, _reads, _probes = store
    assert asyncio.run(main.get_gemini_everything())["cloud_roles_on"] is True
    cfg["inference"]["bindings"] = {}
    cfg["inference"]["super_model"]["enabled"] = False
    assert asyncio.run(main.get_gemini_everything())["cloud_roles_on"] is False


def test_disconnecting_while_on_leaves_no_super_model_to_restore(store):
    cfg, _reads, _probes = store
    post(True)
    config.disable_cloud_provider()                          # the key is disconnected
    post(False)                                              # a later off from the API
    assert cfg["inference"]["super_model"]["enabled"] is False


@pytest.mark.parametrize("endpoint", [None, OPENROUTER_ENDPOINT,
                                      {**GEMINI_ENDPOINT, "enabled": False}])
def test_no_gemini_connection_explains_and_reads_no_key(store, endpoint):
    cfg, reads, probes = store
    if endpoint is None:
        cfg["inference"]["endpoints"].pop("cloud")
    else:
        cfg["inference"]["endpoints"]["cloud"] = copy.deepcopy(endpoint)
    assert asyncio.run(main.get_gemini_everything())["available"] is False
    with pytest.raises(HTTPException) as raised:
        post(True)
    assert raised.value.status_code == 400 and raised.value.detail == main.GEMINI_EVERYTHING_NO_KEY
    assert reads == [] and probes == [] and "remote_only" not in cfg["inference"]


def test_an_unreadable_key_or_failed_test_saves_nothing(store, monkeypatch):
    cfg, _reads, _probes = store

    def refused(*_a, **_k):
        raise EndpointConfigurationError("Provider credential unavailable")
    monkeypatch.setattr("service.config.provider_credentials.resolve_keychain", refused)
    with pytest.raises(HTTPException) as raised:
        post(True)
    assert raised.value.detail == main.CLOUD_KEY_UNREADABLE
    assert "remote_only" not in cfg["inference"] and cfg.get("remote_parity") is not True

    monkeypatch.setattr("service.config.provider_credentials.resolve_keychain",
                        lambda *_a, **_k: "synthetic-test-value")

    async def bad_probe(*_a):
        raise HTTPException(status_code=400, detail="The provider rejected that key.")
    monkeypatch.setattr(main, "_remote_only_switch_probe", bad_probe)
    with pytest.raises(HTTPException):
        post(True)
    assert "remote_only" not in cfg["inference"] and cfg.get("remote_parity") is not True


def test_invalid_body_is_refused(store):
    for body in ({}, {"enabled": "yes"}, {"enabled": 1}):
        with pytest.raises(HTTPException):
            asyncio.run(main.set_gemini_everything(body))
