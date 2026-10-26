"""A fresh install that already has the recommended model: the person's own oMLX
(served, or its folders on disk) or Wisp's engine folder. Setup must use it and never
offer a download. Synthetic: fake model folders under tmp_path, fake /v1/models
answers, stubbed oMLX CLI; no network, port, engine or real ~/.omlx is touched."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from service.inference.omlx_client import ModelLoadError
from service.setup import catalog, guide
from service.setup.hardware import Hardware
from service.setup.local_models import catalogue_models_on_disk, weights_complete

GEMMA = catalog.GEMMA_E4B_MTP.id
LING = catalog.LING_Q6.id


@pytest.fixture(autouse=True)
def _modern_host(monkeypatch):
    monkeypatch.setattr(guide, "host_macos_major", lambda: 26)


def _model(root: Path, *parts: str, shards: int = 1, present: int | None = None,
           index: bool = False, config: bool = True) -> Path:
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True)
    if config:
        (folder / "config.json").write_text("{}")
    if shards == 1:
        names = ["model.safetensors"]
    else:
        names = [f"model-{n:05d}-of-{shards:05d}.safetensors" for n in range(1, shards + 1)]
    for name in names[: len(names) if present is None else present]:
        (folder / name).write_bytes(b"weights")
    if index:
        (folder / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {f"w{i}": n for i, n in enumerate(names)}}))
    return folder


# -- what counts as a model already on disk --------------------------------------

def test_finds_catalogue_models_in_both_engine_layouts(tmp_path):
    _model(tmp_path, "Jundot", GEMMA)                  # organised: <dir>/<org>/<id>
    _model(tmp_path, LING, shards=2, index=True)        # flat: <dir>/<id>
    _model(tmp_path, "mlx-community", "Not-A-Wisp-Model")
    _model(tmp_path, ".hidden", catalog.LING_Q4.id)
    assert catalogue_models_on_disk([tmp_path]) == sorted([GEMMA, LING])


def test_an_interrupted_download_is_not_installed(tmp_path):
    partial = _model(tmp_path, "Jundot", GEMMA, shards=3, present=2)
    assert not weights_complete(partial)
    indexed = _model(tmp_path, "mlx-works", LING, shards=2, present=1, index=True)
    assert not weights_complete(indexed)
    assert catalogue_models_on_disk([tmp_path]) == []


def test_folders_without_loadable_weights_do_not_count(tmp_path):
    _model(tmp_path, "a", GEMMA, config=False)                    # no config.json
    adapter = _model(tmp_path, "b", LING)
    (adapter / "adapter_config.json").write_text("{}")           # a LoRA adapter
    empty = tmp_path / "c" / catalog.LING_Q4.id
    empty.mkdir(parents=True)
    (empty / "config.json").write_text("{}")                     # config only
    (empty / "model.safetensors").write_bytes(b"")                # zero bytes
    assert catalogue_models_on_disk([tmp_path]) == []


def test_missing_or_unreadable_folders_are_just_not_evidence(tmp_path):
    assert catalogue_models_on_disk([tmp_path / "nope", tmp_path / "file"]) == []
    (tmp_path / "file").write_text("x")
    assert catalogue_models_on_disk([tmp_path / "file"]) == []


# -- bounds ----------------------------------------------------------------------

def test_scan_examines_a_bounded_number_of_entries_and_roots(monkeypatch, tmp_path):
    from service.setup import local_models
    monkeypatch.setattr(local_models, "MAX_ENTRIES", 5)
    for n in range(10):
        (tmp_path / f"org{n:02d}").mkdir()
    _model(tmp_path, "zzz-last", GEMMA)                 # past the cap: never examined
    assert len(local_models._entries(tmp_path)) == 5
    roots = []
    for n in range(local_models.MAX_ROOTS + 1):
        roots.append(tmp_path / f"root{n}")
        roots[-1].mkdir()
    _model(roots[-1], GEMMA)                            # only in the root past the cap
    assert catalogue_models_on_disk(roots) == []


def test_an_oversized_index_is_not_read(monkeypatch, tmp_path):
    from service.setup import local_models
    folder = _model(tmp_path, LING, shards=2, index=True)
    assert weights_complete(folder)
    monkeypatch.setattr(local_models, "MAX_INDEX_BYTES", 10)
    monkeypatch.setattr(local_models.json, "loads", lambda *_: pytest.fail("index read"))
    assert weights_complete(folder)                     # numbering alone still decides


# -- which folders are looked at (N1: never a folder the person chose) -------------

def test_external_mode_never_lists_a_folder(monkeypatch):
    import service.main as main
    import service.setup.local_models as local_models
    monkeypatch.setattr(main, "is_bundled", lambda: False)
    monkeypatch.setattr(local_models, "catalogue_models_on_disk",
                        lambda *_a, **_k: pytest.fail("external mode listed a folder"))
    monkeypatch.setattr(Path, "iterdir", lambda self: pytest.fail(f"listed {self}"))
    assert main._models_on_disk() == []


def test_bundled_mode_looks_only_in_wisps_engine_folder(monkeypatch, tmp_path):
    import service.config as config
    import service.engine.mode as mode
    import service.main as main
    import service.setup.local_models as local_models
    monkeypatch.setattr(main, "is_bundled", lambda: True)
    monkeypatch.setattr(mode, "engine_data_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(config, "omlx_settings", lambda: {"model": {"model_dirs": [str(tmp_path / "Desktop")]}})
    seen = []
    monkeypatch.setattr(local_models, "catalogue_models_on_disk",
                        lambda dirs, *a, **k: seen.extend(map(str, dirs)) or [])
    main._models_on_disk()
    assert seen == [str(tmp_path / "engine" / "models")]


# -- /setup/status on a fresh install --------------------------------------------

class _FakeClient:
    """A fake engine answer: a /v1/models list, or nothing listening."""
    def __init__(self, models=None, fail=False):
        self._models, self._fail = models or [], fail

    async def models(self):
        if self._fail:
            raise httpx.ConnectError("nothing on the port")
        return list(self._models)


def _fresh(monkeypatch, main, *, client, bundled, model_dirs):
    async def no_externals():
        return []
    monkeypatch.setattr(main, "client", client, raising=False)
    monkeypatch.setattr(main, "detect_external_engines", no_externals)
    monkeypatch.setattr(main, "_omlx_installed", lambda: True)
    monkeypatch.setattr(main, "_omlx_model_dir", lambda: str(model_dirs[0]))
    monkeypatch.setattr(main, "is_bundled", lambda: bundled)
    import service.engine.mode as engine_mode
    monkeypatch.setattr(engine_mode, "engine_data_dir", lambda: Path(model_dirs[0]).parent)
    monkeypatch.setattr(main, "_omlx_admin_url", lambda: "http://127.0.0.1:8000/admin")
    monkeypatch.setattr(main, "current_hardware", lambda: Hardware("Apple M5 Pro", 24))
    monkeypatch.setattr(main, "models_config", lambda: {})          # fresh: nothing saved
    monkeypatch.setattr(main, "_setup_roles",
                        lambda: {r: {"model": GEMMA, "endpoint": "local"} for r in guide.TEXT_ROLES})
    monkeypatch.setattr(guide, "main_engine_profile",
                        lambda: guide.BUNDLED if bundled else guide.OMLX)
    import service.engine.mode as mode
    monkeypatch.setattr(mode, "bundled_compatibility", lambda *a, **k: {
        "available": True, "minimum_macos": "26.2", "unavailable_reason": None})
    monkeypatch.setattr("service.config.setup_configuration_token", lambda: "token")


def _check(status, name):
    return next(c for c in status["checks"] if c["id"] == name)


def test_omlx_already_serving_gemma_is_ready_with_no_download(monkeypatch, tmp_path):
    import service.main as main
    _fresh(monkeypatch, main, client=_FakeClient([GEMMA, LING]), bundled=False, model_dirs=[tmp_path])
    s = asyncio.run(main.setup_status())
    assert s["ready"] is True and s["omlx"]["bundled"] is False and s["omlx"]["running"] is True
    assert s["models"]["use"] == GEMMA and s["models"]["download"] is None


def test_omlx_server_off_is_a_start_and_reads_no_folder(monkeypatch, tmp_path):
    # Wisp's own quit stops oMLX.app's server, so a first run often finds it off. The
    # status must not list the person's model folder (it can be on the Desktop).
    import service.main as main
    _model(tmp_path / "models", "Jundot", GEMMA)
    _fresh(monkeypatch, main, client=_FakeClient(fail=True), bundled=False, model_dirs=[tmp_path / "models"])
    import service.setup.local_models as local_models
    monkeypatch.setattr(local_models, "catalogue_models_on_disk",
                        lambda *_a, **_k: pytest.fail("external mode scanned a folder"))
    s = asyncio.run(main.setup_status())
    assert s["omlx"] == {**s["omlx"], "installed": True, "running": False, "bundled": False}
    assert s["models"]["source"] == "saved" and s["models"]["installed"] == []
    assert _check(s, "engine")["action"] == "start_engine"
    assert s["ready"] is False                    # not until the server answers


def test_a_slow_disk_check_never_holds_the_status(monkeypatch, tmp_path):
    import time as clock
    import service.main as main
    _fresh(monkeypatch, main, client=_FakeClient(fail=True), bundled=True, model_dirs=[tmp_path / "models"])
    monkeypatch.setattr(main, "_DISK_SCAN_SECONDS", 0.2)
    monkeypatch.setattr(main, "_models_on_disk", lambda: clock.sleep(1.0) or [GEMMA])
    async def timed():
        started = clock.monotonic()
        status = await main.setup_status()
        return status, clock.monotonic() - started
    s, waited = asyncio.run(timed())
    assert waited < 0.9
    assert s["models"]["installed"] == []


def test_built_in_engine_finds_a_copied_model_before_it_starts(monkeypatch, tmp_path):
    import service.main as main
    engine_models = tmp_path / "engine" / "models"
    _model(engine_models, "Jundot", GEMMA)
    _model(tmp_path / "Desktop" / "OMLX_Model_Files", catalog.LING_Q6.id)   # never looked at
    _fresh(monkeypatch, main, client=_FakeClient(fail=True), bundled=True, model_dirs=[engine_models])
    s = asyncio.run(main.setup_status())
    assert s["omlx"]["bundled"] is True and s["models"]["use"] == GEMMA
    assert s["models"]["download"] is None
    assert _check(s, "engine")["action"] == "start_engine"


def test_no_model_anywhere_still_offers_the_download(monkeypatch, tmp_path):
    import service.main as main
    _fresh(monkeypatch, main, client=_FakeClient(fail=True), bundled=True, model_dirs=[tmp_path / "models"])
    s = asyncio.run(main.setup_status())
    assert s["models"]["download"]["model"] == GEMMA


# -- starting the person's oMLX ---------------------------------------------------

def _lifecycle(monkeypatch, main, *, comes_up: bool):
    calls = []

    async def cli(*args):
        calls.append(args)
        return True

    async def await_omlx(seconds):
        calls.append(("await", seconds))
        return comes_up and any(c[0] == "start" for c in calls)

    async def status():
        return {"ok": True}
    monkeypatch.setattr(main, "_omlx_cli", cli)
    monkeypatch.setattr(main, "_await_omlx", await_omlx)
    monkeypatch.setattr(main, "setup_status", status)
    monkeypatch.setattr(main, "is_bundled", lambda: False)
    monkeypatch.setattr(main, "require_local_models_disabled", lambda *_: None)
    monkeypatch.setattr(main, "client", type("C", (), {"managed": True,
                                                       "invalidate_connections": lambda self: None})(),
                        raising=False)
    monkeypatch.setattr(main, "_quitting", False)
    return calls


def test_automatic_start_only_starts_the_persons_omlx(monkeypatch):
    import service.main as main
    calls = _lifecycle(monkeypatch, main, comes_up=True)
    assert asyncio.run(main.setup_start_engine({"automatic": True})) == {"ok": True}
    assert ("start", "--no-wait") in calls
    assert not any(c[0] in ("restart", "stop") for c in calls)


def test_automatic_start_never_restarts_even_when_it_fails(monkeypatch):
    import service.main as main
    calls = _lifecycle(monkeypatch, main, comes_up=False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.setup_start_engine({"automatic": True}))
    assert e.value.status_code == 503
    assert [c[0] for c in calls if c[0] != "await"] == ["start"]


def test_an_already_running_omlx_is_left_alone(monkeypatch):
    import service.main as main
    calls = _lifecycle(monkeypatch, main, comes_up=True)

    async def up(seconds):
        calls.append(("await", seconds))
        return True
    monkeypatch.setattr(main, "_await_omlx", up)
    asyncio.run(main.setup_start_engine({"automatic": True}))
    assert all(c[0] == "await" for c in calls)


def test_a_click_keeps_the_existing_start_path(monkeypatch):
    import service.main as main
    _lifecycle(monkeypatch, main, comes_up=True)
    used = []

    async def ensure():
        used.append(True)
    monkeypatch.setattr(main, "ensure_omlx", ensure)
    asyncio.run(main.setup_start_engine({}))
    asyncio.run(main.setup_start_engine())
    assert used == [True, True]
    with pytest.raises(HTTPException):
        asyncio.run(main.setup_start_engine({"automatic": "yes"}))


def test_automatic_start_in_bundled_mode_uses_the_built_in_path(monkeypatch):
    import service.main as main
    _lifecycle(monkeypatch, main, comes_up=True)
    monkeypatch.setattr(main, "is_bundled", lambda: True)
    used = []

    async def ensure():
        used.append(True)
        raise ModelLoadError("synthetic")
    monkeypatch.setattr(main, "ensure_omlx", ensure)
    with pytest.raises(HTTPException):
        asyncio.run(main.setup_start_engine({"automatic": True}))
    assert used == [True]
