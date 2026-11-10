"""Settings' "Auto-unload idle models" choice is saved and survives a relaunch,
and the endpoint refuses values the stepper can't send. No live server."""
import pytest

from service import config, idle


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "USER_CONFIG", path)
    monkeypatch.setattr(idle, "_idle_minutes", idle.get_idle_minutes())
    config.models_config.cache_clear()
    yield path
    config.models_config.cache_clear()


@pytest.fixture
def wisp():
    from starlette.testclient import TestClient
    from service import main
    return TestClient(main.app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def test_a_saved_choice_is_what_the_next_launch_starts_with(overlay, wisp):
    response = wisp.post("/idle_timeout", json={"idle_minutes": 30})
    assert response.status_code == 200 and response.json() == {"idle_minutes": 30.0}
    config.models_config.cache_clear()          # a relaunch re-reads the file
    assert idle._startup_minutes() == 30.0
    assert wisp.post("/idle_timeout", json={"idle_minutes": 0}).json() == {"idle_minutes": 0.0}
    config.models_config.cache_clear()
    assert idle._startup_minutes() == 0.0       # Off stays off


def test_no_saved_choice_uses_the_shipped_default(overlay):
    assert idle._startup_minutes() == float(config._packaged_config()["idle_unload_minutes"])


@pytest.mark.parametrize("text", ["idle_unload_minutes: lots\n", "idle_unload_minutes: true\n",
                                  "idle_unload_minutes: .nan\n"])
def test_a_hand_edited_bad_value_falls_back_to_the_default(overlay, text):
    overlay.write_text(text)
    config.models_config.cache_clear()
    assert idle._startup_minutes() == float(config._packaged_config()["idle_unload_minutes"])


def test_an_out_of_range_saved_value_is_clamped(overlay):
    overlay.write_text("idle_unload_minutes: -4\n")
    config.models_config.cache_clear()
    assert idle._startup_minutes() == 0.0
    overlay.write_text("idle_unload_minutes: 900\n")
    config.models_config.cache_clear()
    assert idle._startup_minutes() == 120.0


@pytest.mark.parametrize("value", ["soon", -1, 121, True, None, [5]])
def test_bad_input_is_refused_and_nothing_changes(overlay, wisp, value):
    before = idle.get_idle_minutes()
    response = wisp.post("/idle_timeout", json={"idle_minutes": value})
    assert response.status_code == 422
    assert idle.get_idle_minutes() == before
    assert not overlay.exists()


def test_not_a_number_is_refused(overlay, wisp):
    response = wisp.post("/idle_timeout", content=b'{"idle_minutes": NaN}',
                         headers={"content-type": "application/json"})
    assert response.status_code in (400, 422)
    assert not overlay.exists()


def test_a_body_without_the_key_just_reads_back(overlay, wisp):
    assert wisp.post("/idle_timeout", json={}).json() == {"idle_minutes": idle.get_idle_minutes()}
    assert not overlay.exists()
