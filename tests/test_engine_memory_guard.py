"""The bundled engine's memory guard starts on "aggressive"; a tier the user picked is kept."""
from __future__ import annotations

import json
import stat

import pytest

from service.engine.manager import (EngineLayout, EngineManager, MEMORY_GUARD_TIER,
                                    memory_guard)


def _manager(tmp_path):
    return EngineManager(EngineLayout(tmp_path / "engine", tmp_path / "data"))


def _settings(manager):
    return json.loads(manager.layout.settings_path.read_text())


def _write(manager, doc):
    manager.layout.settings_path.write_text(json.dumps(doc))


def test_fresh_seed_is_aggressive(tmp_path):
    m = _manager(tmp_path)
    m.prepare()
    assert MEMORY_GUARD_TIER == "aggressive"
    assert _settings(m)["memory"] == {"memory_guard_tier": "aggressive"}
    record = m.layout.memory_guard_record
    assert json.loads(record.read_text()) == {"memory_guard_tier": "aggressive"}
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_engine_first_start_balanced_is_migrated_and_other_keys_kept(tmp_path):
    m = _manager(tmp_path)
    m.prepare()
    m.layout.memory_guard_record.unlink()        # an install from before this change
    doc = _settings(m)
    doc["memory"] = {"prefill_memory_guard": True, "memory_guard_tier": "balanced",
                     "memory_guard_custom_ceiling_gb": 0.0, "soft_threshold": 0.85,
                     "hard_threshold": 0.95}
    _write(m, doc)
    m.prepare()
    memory = _settings(m)["memory"]
    assert memory["memory_guard_tier"] == "aggressive"
    assert memory["prefill_memory_guard"] is True and memory["soft_threshold"] == 0.85
    assert memory["hard_threshold"] == 0.95 and memory["memory_guard_custom_ceiling_gb"] == 0.0


@pytest.mark.parametrize("memory", [
    {"memory_guard_tier": "custom", "memory_guard_custom_ceiling_gb": 14.5,
     "prefill_memory_guard": True},
    {"memory_guard_tier": "safe"},
    {"memory_guard_tier": "aggressive", "soft_threshold": 0.8},
])
def test_a_tier_the_user_picked_is_kept(tmp_path, memory):
    m = _manager(tmp_path)
    m.layout.base_path.mkdir(parents=True)
    _write(m, {"memory": memory})              # no record: the very first Wisp launch
    m.prepare()
    assert _settings(m)["memory"] == memory
    m.prepare()
    assert _settings(m)["memory"] == memory


def test_balanced_chosen_after_migration_is_kept(tmp_path):
    m = _manager(tmp_path)
    m.prepare()
    doc = _settings(m)
    doc["memory"]["memory_guard_tier"] = "balanced"   # picked later in the admin UI
    _write(m, doc)
    m.prepare()
    assert _settings(m)["memory"]["memory_guard_tier"] == "balanced"


def test_no_rewrite_loop(tmp_path):
    m = _manager(tmp_path)
    m.prepare()
    first = m.layout.settings_path.read_text()
    record = m.layout.memory_guard_record.read_text()
    for _ in range(3):
        m.prepare()
        assert m.layout.settings_path.read_text() == first
        assert m.layout.memory_guard_record.read_text() == record


def test_memory_guard_decision():
    assert memory_guard({}, already_set=False) == {"memory_guard_tier": "aggressive"}
    assert memory_guard({}, already_set=True) == {"memory_guard_tier": "aggressive"}
    assert memory_guard({"memory": "junk"}, already_set=False) == {"memory_guard_tier": "aggressive"}
    assert memory_guard({"memory": {"memory_guard_tier": "bogus", "x": 1}}, already_set=True) \
        == {"memory_guard_tier": "aggressive", "x": 1}
    assert memory_guard({"memory": {"memory_guard_tier": "Balanced"}}, already_set=False) \
        == {"memory_guard_tier": "aggressive"}
    assert memory_guard({"memory": {"memory_guard_tier": "balanced"}}, already_set=True) is None
    for tier in ("safe", "custom", "aggressive", "SAFE"):
        assert memory_guard({"memory": {"memory_guard_tier": tier}}, already_set=False) is None


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "", '{"memory": [1]}',
                                  '{"memory": {"memory_guard_tier": 5, "soft_threshold": 0.8}}'])
def test_a_corrupt_settings_file_gets_a_valid_aggressive_guard(tmp_path, text):
    m = _manager(tmp_path)
    m.prepare()
    m.layout.settings_path.write_text(text)          # the record already exists
    again = _manager(tmp_path)
    again.prepare()
    doc = _settings(again)
    assert doc["memory"]["memory_guard_tier"] == "aggressive"
    assert doc["auth"]["api_key"] == again.api_key and doc["server"]["port"] == again.port
    if "soft_threshold" in text:
        assert doc["memory"]["soft_threshold"] == 0.8
    again.prepare()
    assert _settings(again) == doc
