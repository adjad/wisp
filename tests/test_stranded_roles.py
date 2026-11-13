"""0.5.0: code-default roles name base Gemma, which is no longer offered. A built-in-engine
Mac with no roles of its own and only Wisp's tuned Gemma on disk must not be pointed at base."""
import pytest

from service import config
from service.engine import mode
from service.setup import catalog

BASE, TUNED, PRE = catalog.GEMMA_E4B_MTP.id, catalog.GEMMA_E4B_WISP.id, catalog.GEMMA_E4B_WISP_PRERELEASE.id
ROLES = {"fast": BASE, "agent": BASE, "general": BASE, "vision": BASE, "coder": "Some-Coder"}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(mode, "is_bundled", lambda: True)
    monkeypatch.setattr(mode, "engine_data_dir", lambda: tmp_path)

    def put(model_id, owner="Neo-Arid"):
        folder = tmp_path / "models" / owner / model_id
        folder.mkdir(parents=True)
        (folder / "config.json").write_text("{}")
        (folder / "model.safetensors").write_bytes(b"x" * 16)
    return put


def _resolved(overlay=None):
    return config._stranded_roles({"roles": dict(ROLES)}, overlay or {})["roles"]


def test_only_the_tuned_gemma_on_disk_moves_the_base_roles_to_it(engine):
    engine(TUNED)
    roles = _resolved()
    assert {r: roles[r] for r in ("fast", "agent", "general", "vision")} == dict.fromkeys(
        ("fast", "agent", "general", "vision"), TUNED)
    assert roles["coder"] == "Some-Coder"            # a role that never named base is left alone


def test_the_pre_release_folder_counts_as_the_tuned_gemma(engine):
    engine(PRE, owner="local")
    assert _resolved()["fast"] == PRE


def test_base_on_disk_keeps_base(engine):
    engine(TUNED)
    engine(BASE, owner="Jundot")
    assert _resolved()["fast"] == BASE


def test_nothing_on_disk_keeps_base(engine):
    assert _resolved()["fast"] == BASE


def test_the_persons_own_roles_are_never_changed(engine):
    engine(TUNED)
    assert _resolved({"roles": {"fast": BASE}})["fast"] == BASE


def test_external_mode_never_looks(engine, monkeypatch):
    engine(TUNED)
    monkeypatch.setattr(mode, "is_bundled", lambda: False)
    assert _resolved()["fast"] == BASE


def test_a_weightless_folder_does_not_count(engine, tmp_path):
    (tmp_path / "models" / "Neo-Arid" / TUNED).mkdir(parents=True)   # a README-only "download"
    assert _resolved()["fast"] == BASE


def test_setup_token_and_the_apply_precondition_see_the_same_roles(engine, tmp_path, monkeypatch):
    # A stranded Mac: the token (from models_config) and the automatic apply's precondition
    # (from the file under the lock) must both see the roles resolved to Gemma Wisp, or the
    # first automatic apply after a cache clear is refused.
    import yaml
    engine(TUNED)
    overlay = tmp_path / "cfg" / "config.yaml"
    monkeypatch.setattr(config, "USER_CONFIG", overlay)
    config.models_config.cache_clear()
    try:
        expected = config.setup_configuration_token()
        assert config.models_config()["roles"]["fast"] == TUNED
        config.set_roles({"fast": TUNED, "general": TUNED}, expected_configuration=expected, available_models=[])
        assert yaml.safe_load(overlay.read_text())["roles"]["fast"] == TUNED      # written, not refused
        config.models_config.cache_clear()
        with pytest.raises(config.ConfigurationPreconditionFailed):               # a stale token still is
            config.set_roles({"fast": TUNED}, expected_configuration=expected, available_models=[])
    finally:
        config.models_config.cache_clear()
