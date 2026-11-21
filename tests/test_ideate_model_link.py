"""Ideate finding a copy of its model already on this Mac, and linking it into Wisp's own
engine folder with an APFS clone. A fake HOME and engine folder under tmp_path; no engine,
no model, no network. The stand-in Residency is the one the bridge tests use."""
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.ideate import api, config, engine, runner, store
from tests.test_ideate_bridge import FakeAdapter, FakeResidency, FAKE_PI
from service.ideate import harness


def make_model(folder: Path, config_json=True, weights=True) -> Path:
    folder.mkdir(parents=True)
    if config_json:
        (folder / "config.json").write_text("{}")
    if weights:
        (folder / "model-00001.safetensors").write_bytes(b"w" * 4096)
    (folder / "tokenizer.json").write_text("{}")
    return folder


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    models = tmp_path / "engine" / "models"
    monkeypatch.setattr(engine, "_home", lambda: home)
    monkeypatch.setattr(engine, "models_dir", lambda: models)
    state = {"bundled": True}
    monkeypatch.setattr(engine, "bundled", lambda: state["bundled"])
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI)
    residency = FakeResidency(listed=False)
    manager = runner.Manager(
        store.Store(tmp_path / "projects"), FakeAdapter(script), residency,
        sandbox_usable=lambda: True,
        engine_target=lambda: harness.EngineTarget("http://127.0.0.1:8000/v1", 8000),
        api_key=lambda: "", contain=lambda argv, profile: list(argv))
    monkeypatch.setattr(runner, "_manager", manager)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield home, models, state, client


def post(client):
    return client.post("/ideate/model/link", json={})


def test_nothing_on_this_mac(world):
    _, _, _, client = world
    body = client.get("/ideate/status").json()
    assert body["available"] is False and body["can_link"] is False and body["model_found_at"] == ""
    assert body["reason"] == "Inkling 2 Mini isn't on this Mac."
    assert post(client).status_code == 409


def test_finds_a_copy_in_the_omlx_folder(world):
    home, _, _, client = world
    found = make_model(home / "Desktop/OMLX_Model_Files/mlx-community" / config.MODEL_ID)
    make_model(home / "Desktop/OMLX_Model_Files/mlx-community/Inkling-2-Mini-oQ4e-mtp")
    body = client.get("/ideate/status").json()
    assert body["model_found_at"] == str(found) and body["can_link"] is True
    assert body["reason"] == "Inkling 2 Mini is on this Mac but not in Wisp's model folder."


def test_incomplete_folders_are_not_found(world):
    home, _, _, client = world
    make_model(home / "Desktop/OMLX_Model_Files/mlx-community" / config.MODEL_ID, weights=False)
    assert client.get("/ideate/status").json()["can_link"] is False


def test_finds_a_hugging_face_snapshot(world):
    home, _, _, client = world
    hub = home / ".cache/huggingface/hub" / f"models--{config.MODEL_ORG}--{config.MODEL_ID}"
    blobs = hub / "blobs"
    blobs.mkdir(parents=True)
    snap = hub / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (blobs / "b1").write_text("{}")
    (blobs / "b2").write_bytes(b"w" * 2048)
    (snap / "config.json").symlink_to("../../blobs/b1")
    (snap / "model.safetensors").symlink_to("../../blobs/b2")
    assert client.get("/ideate/status").json()["model_found_at"] == str(snap)
    try:
        final = engine.link_model()
    except engine.LinkRefused as exc:
        pytest.skip(f"no clone on this volume: {exc}")
    assert not (final / "config.json").is_symlink()
    assert (final / "model.safetensors").read_bytes() == b"w" * 2048


def test_external_mode_says_to_add_the_folder(world):
    home, _, state, client = world
    make_model(home / "Models" / config.MODEL_ID)
    state["bundled"] = False
    body = client.get("/ideate/status").json()
    assert body["can_link"] is False and "Add the folder in oMLX" in body["reason"]
    assert post(client).status_code == 409
    assert "oMLX" in post(client).json()["detail"]


def test_link_happy_path_clones_and_leaves_the_source_alone(world):
    home, models, _, client = world
    src = make_model(home / "Desktop/OMLX_Model_Files/mlx-community" / config.MODEL_ID)
    before = {p.name: p.stat().st_mtime for p in src.iterdir()}
    reply = post(client)
    if reply.status_code == 409 and "without copying" in reply.json()["detail"]:
        pytest.skip("this volume has no clonefile support")
    assert reply.status_code == 200, reply.text
    body = reply.json()
    final = models / config.MODEL_ORG / config.MODEL_ID
    assert body["linked_at"] == str(final) and body["engine_restart_needed"] is True
    assert (final / "config.json").is_file() and (final / "model-00001.safetensors").stat().st_size == 4096
    assert {p.name: p.stat().st_mtime for p in src.iterdir()} == before
    assert not [p for p in final.parent.iterdir() if "linking" in p.name]
    status = client.get("/ideate/status").json()
    assert status["link_pending_restart"] is True and status["can_link"] is False
    # A second link is refused, not redone.
    again = post(client)
    assert again.status_code == 409 and "already" in again.json()["detail"]


def test_link_refuses_a_symlinked_source(world, tmp_path):
    home, models, _, _ = world
    real = make_model(tmp_path / "elsewhere" / config.MODEL_ID)
    link = home / "Models" / config.MODEL_ID
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    with pytest.raises(engine.LinkRefused):
        engine.link_model(link)
    assert not (models / config.MODEL_ORG / config.MODEL_ID).exists()


def test_link_refuses_a_shortcut_inside_the_copy(world, tmp_path):
    home, models, _, _ = world
    src = make_model(home / "Models" / config.MODEL_ID)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    (src / "extra.bin").symlink_to(outside)
    with pytest.raises(engine.LinkRefused, match="shortcut"):
        engine.link_model(src)
    assert not (models / config.MODEL_ORG / config.MODEL_ID).exists()
    assert not models.exists() or not any(models.rglob("*linking*"))


def test_link_refuses_a_missing_source_and_an_existing_target(world):
    home, models, _, _ = world
    with pytest.raises(engine.LinkRefused):
        engine.link_model(home / "nope" / config.MODEL_ID)
    src = make_model(home / "Models" / config.MODEL_ID)
    make_model(models / config.MODEL_ORG / config.MODEL_ID)
    with pytest.raises(engine.LinkRefused, match="already"):
        engine.link_model(src)


def test_link_refuses_another_volume(world, monkeypatch):
    home, models, _, _ = world
    src = make_model(home / "Models" / config.MODEL_ID)
    monkeypatch.setattr(engine, "_same_volume", lambda a, b: False)
    with pytest.raises(engine.LinkRefused, match="another drive"):
        engine.link_model(src)
    assert not any(models.rglob("*linking*")) if models.exists() else True


def test_a_failed_clone_cleans_up(world, monkeypatch):
    home, models, _, _ = world
    src = make_model(home / "Models" / config.MODEL_ID)
    def boom(a, b):
        raise OSError(errno.ENOTSUP, "no")
    monkeypatch.setattr(engine, "_clonefile", boom)
    with pytest.raises(engine.LinkRefused, match="without copying"):
        engine.link_model(src)
    assert list((models / config.MODEL_ORG).iterdir()) == []


def test_link_route_refuses_web_pages(world):
    _, _, _, client = world
    assert client.post("/ideate/model/link", json={}, headers={"Origin": "https://example.com"}).status_code == 403
