"""The bundled engine gets each catalog model's settings, once per profile version."""
from __future__ import annotations

import json
import stat

from service.engine import profiles
from service.setup import catalog

GEMMA = catalog.GEMMA_E4B_MTP


def _entries(path):
    return json.loads(path.read_text())["models"]


def test_fresh_engine_gets_gemma_profile(tmp_path):
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    assert profiles.seed(settings, record) == [GEMMA.id]
    entry = _entries(settings)[GEMMA.id]
    assert entry["temperature"] == 0.8 and entry["top_p"] == 0.95 and entry["top_k"] == 64
    assert entry["enable_thinking"] is True and entry["mtp_enabled"] is True
    assert json.loads(settings.read_text())["version"] == 1
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


def test_existing_keys_are_kept_and_profile_keys_win(tmp_path):
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    settings.write_text(json.dumps({"version": 1, "models": {
        GEMMA.id: {"temperature": 1.0, "is_favorite": True},
        "other-model": {"temperature": 0.2}}}))
    profiles.seed(settings, record)
    entries = _entries(settings)
    assert entries[GEMMA.id]["temperature"] == 0.8 and entries[GEMMA.id]["is_favorite"] is True
    assert entries["other-model"] == {"temperature": 0.2}


def test_user_tuning_after_seeding_is_not_overwritten(tmp_path):
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    profiles.seed(settings, record)
    doc = json.loads(settings.read_text())
    doc["models"][GEMMA.id]["temperature"] = 0.9       # changed in the engine's admin UI
    settings.write_text(json.dumps(doc))
    assert profiles.seed(settings, record) == []
    assert _entries(settings)[GEMMA.id]["temperature"] == 0.9


def test_a_new_profile_version_is_applied_again(tmp_path):
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    profiles.seed(settings, record)
    changed = catalog.CatalogModel(GEMMA.id, GEMMA.label, GEMMA.quant, GEMMA.size_gb, GEMMA.min_ram_gb,
                                   GEMMA.repo, GEMMA.note, GEMMA.tested,
                                   engine_profile={**GEMMA.engine_profile, "temperature": 0.5})
    assert profiles.seed(settings, record, [changed]) == [GEMMA.id]
    assert _entries(settings)[GEMMA.id]["temperature"] == 0.5


def test_force_reapplies_after_an_admin_push(tmp_path):
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    profiles.seed(settings, record)
    doc = json.loads(settings.read_text())
    del doc["models"][GEMMA.id]["mtp_enabled"]          # dropped by the PUT's schema
    settings.write_text(json.dumps(doc))
    assert profiles.seed(settings, record, [GEMMA], force=[GEMMA.id]) == [GEMMA.id]
    assert _entries(settings)[GEMMA.id]["mtp_enabled"] is True


def test_push_body_sends_the_full_entry_filtered_by_schema():
    entry = {"temperature": 0.6, "mtp_enabled": True, "unknown": 1}
    assert profiles.push_body(entry, {"temperature", "mtp_enabled"}) == {"temperature": 0.6, "mtp_enabled": True}
    assert profiles.push_body(entry, None) == entry


def test_only_models_with_a_profile_are_seeded():
    assert [m.id for m in profiles.profiled_models()] == [GEMMA.id]
