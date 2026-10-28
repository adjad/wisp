"""The bundled engine gets each catalog model's settings, once per profile version."""
from __future__ import annotations

import dataclasses
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
    assert not any(key.startswith("turboquant_") for key in entry)    # withdrawn: no TurboQuant
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


# The push after a download finishes belongs to the backend, not to a setup
# window that happens to be polling: closing the window mid-download must not
# leave Gemma on the engine's defaults (8k window, thinking off).

import asyncio
from types import SimpleNamespace

import pytest

from service.engine import downloads


@pytest.fixture
def engine(tmp_path, monkeypatch):
    layout = SimpleNamespace(base_path=tmp_path / "base", data_dir=tmp_path / "data")
    manager = SimpleNamespace(layout=layout, running=lambda: True, verified=True)
    monkeypatch.setattr(downloads.runtime, "_manager", manager)
    for name, value in [("_profiles_pushed", set()), ("_pushing", set()),
                        ("_push_failed_at", {}), ("_watch", None)]:
        monkeypatch.setattr(downloads, name, value)
    state = SimpleNamespace(puts=[], fail_put=False, inventories=[], inventory_calls=0,
                            manager=manager, settings=layout.base_path / "model_settings.json")

    async def call(method, path, payload=None):
        if path == "/openapi.json":
            raise downloads.DownloadError("no schema")
        assert method == "PUT" and path == f"/admin/api/models/{GEMMA.id}/settings"
        if state.fail_put:
            raise downloads.DownloadError("The engine couldn't complete that request.")
        state.puts.append(payload)
        return {"success": True}

    async def inventory():
        state.inventory_calls += 1
        return state.inventories.pop(0) if len(state.inventories) > 1 else state.inventories[0]

    monkeypatch.setattr(downloads, "_call", call)
    monkeypatch.setattr(downloads, "_inventory", inventory)
    return state


def _task(model_id, state):
    return (1.0, "task-1", {"model": model_id, "state": state})


def test_a_failed_push_is_retried_instead_of_counted(engine):
    engine.fail_put = True
    asyncio.run(downloads._push_profiles([_task(GEMMA.id, "completed")], now=lambda: 100.0))
    assert GEMMA.id not in downloads._profiles_pushed and downloads._push_failed_at == {GEMMA.id: 100.0}
    engine.fail_put = False
    # The UI polls every few seconds: no new try inside the retry interval.
    asyncio.run(downloads._push_profiles([_task(GEMMA.id, "completed")], now=lambda: 110.0))
    assert engine.puts == []
    asyncio.run(downloads._push_profiles([_task(GEMMA.id, "completed")], now=lambda: 131.0))
    assert GEMMA.id in downloads._profiles_pushed and downloads._push_failed_at == {}
    sent = engine.puts[0]
    assert sent["max_context_window"] == 16384 and sent["enable_thinking"] is True
    # Pushed once: later polls leave the engine alone.
    asyncio.run(downloads._push_profiles([_task(GEMMA.id, "completed")], now=lambda: 500.0))
    assert len(engine.puts) == 1


def test_only_a_profiled_download_still_owed_its_profile_is_awaited(engine):
    assert downloads._awaiting_profile([_task(GEMMA.id, "downloading")])
    assert downloads._awaiting_profile([_task(GEMMA.id, "pending")])
    assert downloads._awaiting_profile([_task(GEMMA.id, "completed")])
    assert not downloads._awaiting_profile([_task(GEMMA.id, "failed")])
    assert not downloads._awaiting_profile([_task(catalog.LING_Q4.id, "downloading")])
    downloads._profiles_pushed.add(GEMMA.id)
    assert not downloads._awaiting_profile([_task(GEMMA.id, "completed")])


def test_the_backend_pushes_when_the_download_finishes_with_no_window_open(engine, monkeypatch):
    monkeypatch.setattr(downloads, "_WATCH_EVERY_S", 0)
    engine.inventories = [[_task(GEMMA.id, "downloading")], [_task(GEMMA.id, "downloading")],
                          [_task(GEMMA.id, "completed")]]
    asyncio.run(asyncio.wait_for(downloads._watch_loop(), 5))
    assert len(engine.puts) == 1 and GEMMA.id in downloads._profiles_pushed
    assert engine.inventory_calls == 3        # stopped once nothing was owed
    assert _entries(engine.settings)[GEMMA.id]["max_context_window"] == 16384


def test_the_watch_retries_a_failed_push_until_the_engine_takes_it(engine, monkeypatch):
    monkeypatch.setattr(downloads, "_WATCH_EVERY_S", 0)
    monkeypatch.setattr(downloads, "_PUSH_RETRY_S", 0)
    engine.fail_put = True
    engine.inventories = [[_task(GEMMA.id, "completed")]]

    async def run():
        watch = asyncio.create_task(downloads._watch_loop())
        while engine.inventory_calls < 3:
            await asyncio.sleep(0)
        engine.fail_put = False
        await asyncio.wait_for(watch, 5)
    asyncio.run(run())
    assert len(engine.puts) == 1 and GEMMA.id in downloads._profiles_pushed


def test_the_watch_stops_with_the_engine(engine, monkeypatch):
    monkeypatch.setattr(downloads, "_WATCH_EVERY_S", 0)
    engine.manager.running = lambda: False
    asyncio.run(asyncio.wait_for(downloads._watch_loop(), 5))
    assert engine.inventory_calls == 0 and engine.puts == []


def test_a_watch_starts_once_and_only_for_a_profiled_download(engine):
    async def run():
        downloads._watch_downloads([_task(catalog.LING_Q4.id, "downloading")])
        assert downloads._watch is None
        downloads._watch_downloads([_task(GEMMA.id, "downloading")])
        first = downloads._watch
        downloads._watch_downloads([_task(GEMMA.id, "downloading")])
        assert first is not None and downloads._watch is first
        first.cancel()
    asyncio.run(run())


def _seeded_with_turboquant(tmp_path, kv_bits=6):
    """An install that seeded the briefly shipped TurboQuant 6-bit profile."""
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    withdrawn = {**GEMMA.engine_profile, "turboquant_kv_enabled": True, "turboquant_kv_bits": 6}
    record.write_text(json.dumps({GEMMA.id: profiles.fingerprint(withdrawn)}))
    entry = {**withdrawn, "turboquant_kv_bits": kv_bits, "is_favorite": True}
    settings.write_text(json.dumps({"version": 1, "models": {GEMMA.id: entry}}))
    return settings, record


def test_an_install_that_got_turboquant_has_it_removed_once(tmp_path):
    settings, record = _seeded_with_turboquant(tmp_path)
    assert profiles.seed(settings, record) == [GEMMA.id]
    entry = _entries(settings)[GEMMA.id]
    assert not any(key.startswith("turboquant_") for key in entry)
    assert entry["is_favorite"] is True and entry["max_context_window"] == 16384
    # The record now holds the current profile: nothing is written again.
    assert profiles.seed(settings, record) == []


def test_turboquant_the_person_chose_themselves_is_kept(tmp_path):
    settings, record = _seeded_with_turboquant(tmp_path, kv_bits=4)   # retuned in the admin UI
    profiles.seed(settings, record)
    entry = _entries(settings)[GEMMA.id]
    assert entry["turboquant_kv_enabled"] is True and entry["turboquant_kv_bits"] == 4


def _seeded_with_power_cap(tmp_path, watts=20, *, turboquant_done=False):
    """An install that seeded 8bff39bc's profile, which carried a model-level 20 W cap."""
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    withdrawn = {**GEMMA.engine_profile, "gpu_power_limit_w": 20}
    marks = {GEMMA.id: profiles.fingerprint(withdrawn)}
    if turboquant_done:   # an older record: one string mark for the TurboQuant cleanup
        marks[profiles.RETIRED_KEY] = {GEMMA.id: profiles.fingerprint(
            {"turboquant_kv_enabled": True, "turboquant_kv_bits": 6})}
    record.write_text(json.dumps(marks))
    entry = {**withdrawn, "gpu_power_limit_w": watts}
    settings.write_text(json.dumps({"version": 1, "models": {GEMMA.id: entry}}))
    return settings, record


def test_an_install_that_got_the_20_w_model_cap_has_it_removed_once(tmp_path):
    settings, record = _seeded_with_power_cap(tmp_path)
    assert profiles.seed(settings, record) == [GEMMA.id]
    assert "gpu_power_limit_w" not in _entries(settings)[GEMMA.id]
    assert profiles.seed(settings, record) == []


def test_a_power_cap_the_person_chose_themselves_is_kept(tmp_path):
    settings, record = _seeded_with_power_cap(tmp_path, watts=15)
    profiles.seed(settings, record)
    assert _entries(settings)[GEMMA.id]["gpu_power_limit_w"] == 15


def test_the_power_cap_cleanup_runs_on_a_record_from_before_groups(tmp_path):
    settings, record = _seeded_with_power_cap(tmp_path, turboquant_done=True)
    assert profiles.seed(settings, record) == [GEMMA.id]
    assert "gpu_power_limit_w" not in _entries(settings)[GEMMA.id]
    marks = json.loads(record.read_text())[profiles.RETIRED_KEY][GEMMA.id]
    assert isinstance(marks, list) and len(marks) == 2


def test_a_never_turboquant_install_is_left_alone(tmp_path):
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    profiles.seed(settings, record)
    assert profiles.seed(settings, record) == []


def _turn_turboquant_on(settings):
    doc = json.loads(settings.read_text())
    doc["models"][GEMMA.id].update({"turboquant_kv_enabled": True, "turboquant_kv_bits": 6})
    settings.write_text(json.dumps(doc))


def test_the_turboquant_cleanup_never_fires_again_on_a_reseed(tmp_path):
    settings, record = _seeded_with_turboquant(tmp_path)
    profiles.seed(settings, record)                       # the one cleanup
    assert json.loads(record.read_text())[profiles.RETIRED_KEY][GEMMA.id]
    _turn_turboquant_on(settings)                         # the person turns it back on
    assert profiles.seed(settings, record, [GEMMA], force=[GEMMA.id]) == [GEMMA.id]
    entry = _entries(settings)[GEMMA.id]
    assert entry["turboquant_kv_enabled"] is True and entry["turboquant_kv_bits"] == 6
    assert entry["temperature"] == 0.8                    # the profile itself still applies


def test_a_fresh_install_marks_the_cleanup_done_too(tmp_path):
    settings, record = tmp_path / "model_settings.json", tmp_path / "seeded.json"
    profiles.seed(settings, record)                       # nothing to remove, marked anyway
    _turn_turboquant_on(settings)
    profiles.seed(settings, record, [GEMMA], force=[GEMMA.id])
    assert _entries(settings)[GEMMA.id]["turboquant_kv_bits"] == 6


def test_a_profile_change_does_not_rerun_the_cleanup(tmp_path):
    settings, record = _seeded_with_turboquant(tmp_path)
    profiles.seed(settings, record)
    _turn_turboquant_on(settings)
    changed = dataclasses.replace(GEMMA, engine_profile={**GEMMA.engine_profile, "top_k": 40})
    assert profiles.seed(settings, record, [changed]) == [GEMMA.id]
    assert _entries(settings)[GEMMA.id]["turboquant_kv_enabled"] is True


def test_the_watch_gives_up_at_its_deadline(engine, monkeypatch):
    # A download that never finishes (stalled, or left paused) is not watched forever.
    monkeypatch.setattr(downloads, "_WATCH_EVERY_S", 0.01)
    monkeypatch.setattr(downloads, "_WATCH_FOR_S", 0.1)
    engine.inventories = [[_task(GEMMA.id, "downloading")]]
    asyncio.run(asyncio.wait_for(downloads._watch_loop(), 5))
    assert 1 <= engine.inventory_calls <= 10 and engine.puts == []
    assert downloads._WATCH_FOR_S == 0.1 and GEMMA.id not in downloads._profiles_pushed
