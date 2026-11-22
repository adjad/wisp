"""Settings › Models: two menus (General use, Coding) that save several roles at once, and a
built-in engine that lists every model already on the Mac. Invented model ids; no live server."""
from pathlib import Path

import pytest
import yaml

from service import config
from service.engine import manager as engine_manager

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "USER_CONFIG", path)
    config.models_config.cache_clear()
    yield path
    config.models_config.cache_clear()


@pytest.fixture
def wisp(monkeypatch):
    from starlette.testclient import TestClient
    from service import main
    monkeypatch.setattr(main, "_sync_keep_warm", lambda: None)
    return TestClient(main.app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def test_one_save_moves_every_role_in_the_general_menu(overlay, wisp):
    r = wisp.post("/config", json={"roles": {"general": "alpha-7b", "fast": "alpha-7b", "reasoning": "alpha-7b"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    roles = yaml.safe_load(overlay.read_text())["roles"]
    # general carries agent, fast carries router (set_roles' coupling).
    for role in ("general", "agent", "fast", "router", "reasoning"):
        assert roles[role] == "alpha-7b"
    assert "coding" not in roles


def test_a_coding_pick_leaves_general_alone(overlay, wisp):
    assert wisp.post("/config", json={"roles": {"coding": "Qwen3.5-9B-oQ4e-mtp"}}).json()["ok"] is True
    roles = yaml.safe_load(overlay.read_text())["roles"]
    assert roles == {"coding": "Qwen3.5-9B-oQ4e-mtp"}


@pytest.mark.parametrize("bad", [{"nonsense": "alpha"}, {"coding": ""}, {"coding": 3}])
def test_a_bad_group_saves_nothing(overlay, wisp, bad):
    r = wisp.post("/config", json={"roles": bad})
    assert r.json()["ok"] is False
    assert not overlay.exists()


def test_the_single_role_save_still_works(overlay, wisp):
    assert wisp.post("/config", json={"role": "coding", "model": "alpha-7b"}).json()["ok"] is True
    assert yaml.safe_load(overlay.read_text())["roles"]["coding"] == "alpha-7b"


def test_gemma_wisp_stays_default_and_no_thinking_capable():
    models = yaml.safe_load((ROOT / "service/config/models.yaml").read_text())
    assert "Gemma-Wisp" in models["no_thinking_capable"]
    text = (ROOT / "app/Sources/WispApp/QuickSettingsText.swift").read_text()
    assert 'static let codingFirst = ["Qwen3.5-9B-oQ4e-mtp"]' in text


def test_user_model_dirs_are_the_existing_real_folders(tmp_path):
    home = tmp_path / "home"
    (home / "Desktop/OMLX_Model_Files").mkdir(parents=True)
    (home / ".omlx/models").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "Documents").mkdir()
    (home / "Documents/OMLX_Model_Files").symlink_to(elsewhere)      # a shortcut: left out
    found = engine_manager.user_model_dirs(home)
    assert found == [home / "Desktop/OMLX_Model_Files", home / ".omlx/models"]
    # Wisp's own folder is never listed twice.
    assert engine_manager.user_model_dirs(home, own=home / ".omlx/models") == [home / "Desktop/OMLX_Model_Files"]
    assert engine_manager.user_model_dirs(tmp_path / "nobody") == []


def test_wisps_own_folder_comes_first_so_its_copy_wins(tmp_path):
    layout = engine_manager.EngineLayout(tmp_path / "code", tmp_path / "data")
    user = tmp_path / "Desktop/OMLX_Model_Files"
    m = engine_manager.EngineManager(layout, api_key="k" * 40, extra_model_dirs=[user])
    settings = engine_manager.engine_settings(m.port, m.api_key, m.model_dirs())
    assert settings["model"]["model_dirs"] == [str(layout.models_dir), str(user)]
    assert settings["model"]["model_dir"] == str(layout.models_dir)      # downloads still land in Wisp's


def test_the_runtime_engine_gets_the_user_folders_and_never_reads_omlx_settings():
    runtime = (ROOT / "service/engine/runtime.py").read_text()
    assert "extra_model_dirs=user_model_dirs(own=layout.models_dir)" in runtime
    source = (ROOT / "service/engine/manager.py").read_text()
    dirs = source[source.index("USER_MODEL_DIRS = "):source.index("def engine_settings(")]
    assert "settings.json\"" not in dirs and "read_settings" not in dirs and "open(" not in dirs
