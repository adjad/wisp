"""Cloud settings contracts use synthetic configuration only; no Keychain or network."""
from service import config
from service.config.endpoints import endpoint_from_config, EndpointConfigurationError


def test_remote_endpoint_is_https_and_origin_bound():
    endpoint = endpoint_from_config("cloud", {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai",
        "api_prefix": "/api/v1",
        "credential_ref": "keychain:cloud",
    })
    assert endpoint.base_url == "https://openrouter.ai"
    assert endpoint.credential_ref == "keychain:cloud"
    assert not endpoint.managed


def test_cloud_settings_never_return_credentials(monkeypatch):
    monkeypatch.setattr(config, "models_config", lambda: {
        "inference": {
            "endpoints": {"cloud": {"provider": "openrouter",
                "base_url": "https://openrouter.ai", "api_prefix": "/api/v1",
                "credential_ref": "keychain:cloud"}},
            "bindings": {"reasoning": {"endpoint": "cloud",
                "model_id": "vendor/model", "context_window": 8192}},
        },
    })
    result = config.cloud_provider_settings()
    assert result["enabled"] is True
    assert result["roles"] == ["reasoning"]
    assert result["credential_name"] == "cloud"
    assert result["super_model_enabled"] is False
    assert not {"api_key", "token", "password", "credential_ref"} & result.keys()


def test_cloud_super_model_metadata_is_persisted_without_expanding_role_bindings(monkeypatch):
    saved = []
    monkeypatch.setattr(config, "models_config", lambda: {
        "default_role": "general",
        "roles": {"general": "local-general", "reasoning": "local-reasoning",
                  "coding": "local-coding", "research": "local-research"},
        "inference": {"bindings": {}},
    })
    monkeypatch.setattr(config, "_save_overlay", saved.append)
    endpoint_cfg = {"enabled": True, "provider": "openrouter",
                    "base_url": "https://openrouter.ai", "api_prefix": "/api/v1",
                    "credential_ref": "keychain:cloud"}
    config.set_cloud_provider(endpoint_cfg, "vendor/model", 65536, [],
                              super_model_enabled=True)
    update = saved[0]["inference"]
    assert update["super_model"] == {
        "enabled": True, "model_id": "vendor/model", "context_window": 65536}
    assert all(binding["endpoint"] == "local"
               for binding in update["bindings"].values())


def test_cloud_settings_expose_only_safe_credential_name(monkeypatch):
    monkeypatch.setattr(config, "models_config", lambda: {
        "inference": {
            "endpoints": {"cloud": {"credential_ref": "keychain:cloud-abc123"}},
            "bindings": {},
        },
    })
    result = config.cloud_provider_settings()
    assert result["credential_name"] == "cloud-abc123"
    assert "credential_ref" not in result


def test_cloud_role_assignment_is_explicit_and_local_by_default(monkeypatch):
    saved = []
    monkeypatch.setattr(config, "_save_overlay", saved.append)
    monkeypatch.setattr(config, "models_config", lambda: {
        "default_role": "reasoning",
        "roles": {"reasoning": "local-reasoning", "coding": "local-coding",
                  "research": "local-research"},
        "inference": {"bindings": {"reasoning": {
            "endpoint": "cloud", "model_id": "old-cloud/model"}}},
    })
    config.set_cloud_provider({"enabled": True}, "vendor/model", 16384, ["reasoning"])
    bindings = saved[0]["inference"]["bindings"]
    assert bindings["reasoning"]["endpoint"] == "cloud"
    assert bindings["coding"]["endpoint"] == "local"
    assert bindings["research"]["endpoint"] == "local"
    assert bindings["coding"]["model_id"] == "local-coding"
    assert bindings["reasoning"]["qualified_capabilities"] == []


def test_deselecting_cloud_role_restores_local_roster(monkeypatch):
    saved = []
    monkeypatch.setattr(config, "_save_overlay", saved.append)
    monkeypatch.setattr(config, "models_config", lambda: {
        "default_role": "reasoning",
        "roles": {"reasoning": "local-reasoning", "coding": "local-coding",
                  "research": "local-research"},
        "inference": {"bindings": {"reasoning": {
            "endpoint": "cloud", "model_id": "vendor/model"}}},
    })
    config.set_cloud_provider({"enabled": True}, "vendor/model", 16384, [])
    binding = saved[0]["inference"]["bindings"]["reasoning"]
    assert binding == {"endpoint": "local", "model_id": "local-reasoning",
        "revision": "", "profile": "", "context_window": None,
        "qualified_capabilities": [], "dimensions": 0}


def test_disconnect_restores_local_roster(monkeypatch):
    saved = []
    monkeypatch.setattr(config, "_save_overlay", saved.append)
    monkeypatch.setattr(config, "models_config", lambda: {
        "default_role": "reasoning",
        "roles": {"reasoning": "local-reasoning", "coding": "local-coding",
                  "research": "local-research"},
        "inference": {"bindings": {"coding": {
            "endpoint": "cloud", "model_id": "vendor/model"}}},
    })
    config.disable_cloud_provider()
    update = saved[0]["inference"]
    assert update["endpoints"]["cloud"] == {"enabled": False}
    assert update["bindings"]["coding"]["model_id"] == "local-coding"


def test_cloud_rejects_non_generation_role(monkeypatch):
    monkeypatch.setattr(config, "_save_overlay", lambda update: None)
    try:
        config.set_cloud_provider({"enabled": True}, "vendor/model", 8192, ["fast"])
    except ValueError as error:
        assert str(error) == "Unsupported cloud role"
    else:
        raise AssertionError("fast role must remain local")


def test_remote_http_is_rejected():
    try:
        endpoint_from_config("cloud", {"provider": "openai-compatible",
            "base_url": "http://provider.test", "credential_ref": "keychain:cloud"})
    except EndpointConfigurationError as error:
        assert "HTTPS" in str(error)
    else:
        raise AssertionError("remote HTTP must fail closed")
