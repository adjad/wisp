"""A hand-edited ~/.moe/config.yaml that no longer parses is never overwritten,
and saves fail with a plain reason instead of a bare 500. No live server."""
import pytest

from service import config

DAMAGED = "roles:\n\tfast: some-model\n"  # a tab indent is not valid YAML


@pytest.fixture
def damaged(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(DAMAGED)
    monkeypatch.setattr(config, "USER_CONFIG", path)
    config.models_config.cache_clear()
    yield path
    config.models_config.cache_clear()


@pytest.fixture
def wisp():
    from starlette.testclient import TestClient
    from service import main
    return TestClient(main.app, base_url="http://127.0.0.1", raise_server_exceptions=False)


@pytest.mark.parametrize("save", [
    lambda: config.save_installed_models(["model-a"]),
    lambda: config.set_roles({"coding": "model-a"}),
    lambda: config.set_daily_summary_times(morning=7),
])
def test_writers_refuse_a_damaged_file_with_a_typed_error(damaged, save):
    before = damaged.read_bytes()
    with pytest.raises(config.ConfigurationOverlayDamaged) as caught:
        save()
    assert "~/.moe/config.yaml" in str(caught.value)
    assert damaged.read_bytes() == before
    assert not list(damaged.parent.glob(".config-*"))


def test_a_non_mapping_file_is_refused_the_same_way(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n")
    monkeypatch.setattr(config, "USER_CONFIG", path)
    with pytest.raises(config.ConfigurationOverlayDamaged):
        config.save_installed_models(["model-a"])
    assert path.read_text() == "- just\n- a list\n"


def test_get_models_still_answers_with_a_damaged_file(damaged, wisp, monkeypatch):
    from service import main

    class Engine:
        async def models(self):
            return ["model-a", "model-b"]

    monkeypatch.setattr(main, "client", Engine(), raising=False)
    before = damaged.read_bytes()
    response = wisp.get("/models")
    assert response.status_code == 200
    body = response.json()
    assert body["installed"] == ["model-a", "model-b"]
    assert set(body["roles"]) == set(config.models_config()["roles"])
    assert damaged.read_bytes() == before


def test_a_settings_save_says_the_file_is_the_problem(damaged, wisp):
    before = damaged.read_bytes()
    response = wisp.post("/config", json={"role": "coding", "model": "model-a"})
    assert response.status_code == 409
    assert "config.yaml" in response.json()["detail"]
    assert damaged.read_bytes() == before


def test_a_readable_file_still_saves(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("daily_summary_hour: 8\n")
    monkeypatch.setattr(config, "USER_CONFIG", path)
    config.save_installed_models(["model-a"])
    assert "model-a" in path.read_text() and "daily_summary_hour" in path.read_text()
    config.models_config.cache_clear()
