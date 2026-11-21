"""The bundled engine's memory guard: oMLX's "custom" tier with its ceiling at 90% of the
Mac's memory. Tiers Wisp wrote itself move to it; a tier the user picked is kept."""
from __future__ import annotations

import json
import stat

import pytest

from service.engine import manager as engine_manager
from service.engine.manager import (EngineLayout, EngineManager, MEMORY_GUARD_TIER,
                                    ceiling_gb, memory_guard, physical_memory_bytes, wisp_memory)

GB = 1024**3
OURS = wisp_memory(physical_memory_bytes())


def _manager(tmp_path):
    return EngineManager(EngineLayout(tmp_path / "engine", tmp_path / "data"))


def _settings(manager):
    return json.loads(manager.layout.settings_path.read_text())


def _write(manager, doc):
    manager.layout.settings_path.write_text(json.dumps(doc))


def test_ceiling_is_ninety_percent_rounded_down():
    assert MEMORY_GUARD_TIER == "custom"
    assert ceiling_gb(24 * GB) == 21.6 and ceiling_gb(16 * GB) == 14.4
    assert ceiling_gb(36 * GB) == 32.4 and ceiling_gb(64 * GB) == 57.6
    assert ceiling_gb(int(23.99 * GB)) == 21.5
    assert ceiling_gb(0) == 0 and ceiling_gb(-5) == 0
    assert wisp_memory(24 * GB) == {"memory_guard_tier": "custom",
                                    "memory_guard_custom_ceiling_gb": 21.6}
    # oMLX refuses custom with no ceiling, so an unreadable size falls back.
    assert wisp_memory(0) == {"memory_guard_tier": "aggressive"}
    assert physical_memory_bytes() > 0 and OURS["memory_guard_tier"] == "custom"


def test_fresh_seed_is_custom_at_ninety_percent(tmp_path):
    m = _manager(tmp_path)
    m.prepare()
    assert _settings(m)["memory"] == OURS
    record = m.layout.memory_guard_record
    assert json.loads(record.read_text()) == OURS
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_engine_first_start_balanced_is_migrated_and_other_keys_kept(tmp_path):
    m = _manager(tmp_path)
    m.prepare()
    m.layout.memory_guard_record.unlink()        # an install from before the record existed
    doc = _settings(m)
    doc["memory"] = {"prefill_memory_guard": True, "memory_guard_tier": "balanced",
                     "memory_guard_custom_ceiling_gb": 0.0, "soft_threshold": 0.85,
                     "hard_threshold": 0.95}
    _write(m, doc)
    m.prepare()
    memory = _settings(m)["memory"]
    assert memory["memory_guard_tier"] == "custom"
    assert memory["memory_guard_custom_ceiling_gb"] == OURS["memory_guard_custom_ceiling_gb"]
    assert memory["prefill_memory_guard"] is True and memory["soft_threshold"] == 0.85
    assert memory["hard_threshold"] == 0.95


def test_an_earlier_wisps_aggressive_moves_to_custom(tmp_path):
    m = _manager(tmp_path)
    m.layout.base_path.mkdir(parents=True)
    _write(m, {"memory": {"memory_guard_tier": "aggressive", "prefill_memory_guard": True}})
    m.layout.memory_guard_record.write_text(json.dumps({"memory_guard_tier": "aggressive"}))
    m.prepare()
    assert _settings(m)["memory"] == {**OURS, "prefill_memory_guard": True}
    assert json.loads(m.layout.memory_guard_record.read_text()) == OURS
    m.prepare()
    assert _settings(m)["memory"] == {**OURS, "prefill_memory_guard": True}


def test_wisps_own_ceiling_follows_the_macs_memory(tmp_path, monkeypatch):
    m = _manager(tmp_path)
    monkeypatch.setattr(engine_manager, "physical_memory_bytes", lambda: 16 * GB)
    m.prepare()
    assert _settings(m)["memory"]["memory_guard_custom_ceiling_gb"] == 14.4
    monkeypatch.setattr(engine_manager, "physical_memory_bytes", lambda: 24 * GB)
    m.prepare()          # e.g. the data folder moved to a bigger Mac
    assert _settings(m)["memory"]["memory_guard_custom_ceiling_gb"] == 21.6
    assert json.loads(m.layout.memory_guard_record.read_text())["memory_guard_custom_ceiling_gb"] == 21.6


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


@pytest.mark.parametrize("change", [
    {"memory_guard_tier": "balanced"},
    {"memory_guard_tier": "aggressive"},
    {"memory_guard_tier": "safe"},
    {"memory_guard_custom_ceiling_gb": 12.0},
])
def test_a_choice_made_after_wisp_set_it_is_kept(tmp_path, change):
    m = _manager(tmp_path)
    m.prepare()
    doc = _settings(m)
    doc["memory"].update(change)                # picked later in the admin UI
    _write(m, doc)
    m.prepare()
    assert _settings(m)["memory"] == doc["memory"]


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
    t = 24 * GB
    ours = wisp_memory(t)
    assert memory_guard({}, already_set=False, total_bytes=t) == ours
    assert memory_guard({}, already_set=True, total_bytes=t) == ours
    assert memory_guard({"memory": "junk"}, already_set=False, total_bytes=t) == ours
    assert memory_guard({"memory": {"memory_guard_tier": "bogus", "x": 1}}, already_set=True,
                        total_bytes=t) == {**ours, "x": 1}
    assert memory_guard({"memory": {"memory_guard_tier": "Balanced"}}, already_set=False,
                        total_bytes=t) == ours
    assert memory_guard({"memory": {"memory_guard_tier": "balanced"}}, already_set=True,
                        recorded=ours, total_bytes=t) is None
    for tier in ("safe", "custom", "aggressive", "SAFE"):
        assert memory_guard({"memory": {"memory_guard_tier": tier}}, already_set=False,
                            total_bytes=t) is None
    # An earlier Wisp's record of "aggressive", still in force, moves on; without a record it stays.
    old = {"memory": {"memory_guard_tier": "aggressive"}}
    assert memory_guard(old, already_set=True, recorded={"memory_guard_tier": "aggressive"},
                        total_bytes=t) == ours
    assert memory_guard(old, already_set=True, recorded=None, total_bytes=t) is None
    assert memory_guard(old, already_set=True, recorded=ours, total_bytes=t) is None
    # Wisp's own ceiling: current → nothing; another Mac's → updated; the user's own → kept.
    assert memory_guard({"memory": dict(ours)}, already_set=True, recorded=ours, total_bytes=t) is None
    other = wisp_memory(16 * GB)
    assert memory_guard({"memory": dict(other)}, already_set=True, recorded=other, total_bytes=t) == ours
    mine = {**ours, "memory_guard_custom_ceiling_gb": 18.0}
    assert memory_guard({"memory": mine}, already_set=True, recorded=ours, total_bytes=t) is None


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "", '{"memory": [1]}',
                                  '{"memory": {"memory_guard_tier": 5, "soft_threshold": 0.8}}'])
def test_a_corrupt_settings_file_gets_a_valid_guard(tmp_path, text):
    m = _manager(tmp_path)
    m.prepare()
    m.layout.settings_path.write_text(text)          # the record already exists
    again = _manager(tmp_path)
    again.prepare()
    doc = _settings(again)
    assert doc["memory"]["memory_guard_tier"] == "custom"
    assert doc["memory"]["memory_guard_custom_ceiling_gb"] == OURS["memory_guard_custom_ceiling_gb"]
    assert doc["auth"]["api_key"] == again.api_key and doc["server"]["port"] == again.port
    if "soft_threshold" in text:
        assert doc["memory"]["soft_threshold"] == 0.8
    again.prepare()
    assert _settings(again) == doc
