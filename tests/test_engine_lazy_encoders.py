"""The bundled engine's on-demand Gemma 4 encoders (build-support/engine_guard/encoders.py).

Synthetic: a fake mlx and a fake Gemma 4 model, never a real engine or GPU.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ fake mlx
class FakeArray:
    def __init__(self, shape, dtype="bfloat16", value=0.0, source=None):
        self.shape = tuple(shape)
        self.dtype = dtype
        self.value = value
        self.source = source            # a checkpoint read counts its evals

    @property
    def nbytes(self):
        n = 2
        for d in self.shape:
            n *= d
        return n


class Flag:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class FakeMX(types.ModuleType):
    def __init__(self):
        super().__init__("mlx.core")
        self.files = {}         # path -> {key: (shape, value)}
        self.load_delay = 0.0
        self.reads = 0
        self.cleared = 0

    def zeros(self, shape, dtype=None):
        return FakeArray(shape, dtype)

    def array_equal(self, a, b):
        return Flag(a.shape == b.shape and a.value == b.value)

    def eval(self, arrays):
        for a in arrays if isinstance(arrays, (list, tuple)) else [arrays]:
            if isinstance(a, FakeArray) and a.source is not None:
                self.reads += 1
                a.source = None

    def clear_cache(self):
        self.cleared += 1

    def load(self, path):
        if self.load_delay:
            time.sleep(self.load_delay)
        return {k: FakeArray(shape, value=value, source=path) for k, (shape, value) in self.files[path].items()}


def tree_flatten(tree, prefix=""):
    out = []
    for key, value in tree.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(tree_flatten(value, name + "."))
        else:
            out.append((name, value))
    return out


@pytest.fixture()
def mx(monkeypatch):
    fake = FakeMX()
    utils = types.ModuleType("mlx.utils")
    utils.tree_flatten = tree_flatten
    package = types.ModuleType("mlx")
    package.core = fake
    package.utils = utils
    monkeypatch.setitem(sys.modules, "mlx", package)
    monkeypatch.setitem(sys.modules, "mlx.core", fake)
    monkeypatch.setitem(sys.modules, "mlx.utils", utils)
    monkeypatch.delitem(sys.modules, "mlx_vlm.utils", raising=False)
    monkeypatch.delenv("WISP_ENGINE_LAZY_ENCODERS", raising=False)
    return fake


@pytest.fixture()
def enc(mx):
    spec = importlib.util.spec_from_file_location(
        "wisp_encoders_under_test", ROOT / "build-support/engine_guard/encoders.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------- fake Gemma 4 model
CHECKPOINT = {
    "language_model.model.embed_tokens.weight": ((8, 4), 1.0),
    "audio_tower.layers.0.weight": ((16, 16), 2.0),
    "embed_audio.embedding_projection.weight": ((4, 16), 3.0),
    "vision_tower.encoder.0.weight": ((8, 8), 4.0),
    "embed_vision.embedding_projection.weight": ((4, 8), 5.0),
}


class Model:
    def __init__(self):
        self.params = {}
        for key, (shape, value) in CHECKPOINT.items():
            self.params[key] = FakeArray(shape, value=value)
        self.audio_tower = object()
        self.vision_tower = object()
        self.encoded = []

    def parameters(self):
        return dict(self.params)

    def sanitize(self, weights):
        return dict(weights)

    def load_weights(self, items, strict=True):
        for key, value in items:
            self.params[key] = value

    def tower_ready(self, modality):
        prefixes = {"audio": ("audio_tower.", "embed_audio."), "vision": ("vision_tower.", "embed_vision.")}[modality]
        return all(v.shape != (0,) for k, v in self.params.items() if k.startswith(prefixes))

    def get_input_embeddings(self, input_ids=None, pixel_values=None, **kwargs):
        if pixel_values is not None:
            assert self.tower_ready("vision"), "vision tower ran with released weights"
            self.encoded.append("vision")
        if kwargs.get("input_features") is not None:
            assert self.tower_ready("audio"), "audio tower ran with released weights"
            self.encoded.append("audio")
        return "embeds"

    def encode_image(self, pixel_values, image_position_ids=None):
        assert self.tower_ready("vision")
        return "features"


Model.__module__ = "mlx_vlm.models.gemma4.gemma4"
Model.__qualname__ = "Model"


class OtherModel(Model):
    pass


OtherModel.__module__ = "mlx_vlm.models.qwen3_vl.qwen3_vl"


@pytest.fixture()
def checkpoint(tmp_path, mx):
    path = tmp_path / "model.safetensors"
    path.write_bytes(b"x")
    mx.files[str(path)] = dict(CHECKPOINT)
    return tmp_path


@pytest.fixture()
def executor():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    yield pool
    pool.shutdown(wait=True)


def make(enc, checkpoint, executor, model=None):
    model = model or Model()
    encoders = enc.Encoders(model, checkpoint, executor=executor)
    return model, encoders


# ----------------------------------------------------------------- the tests
def test_prepare_releases_both_towers_after_a_bit_exact_read_back(enc, checkpoint, executor, mx):
    model, encoders = make(enc, checkpoint, executor)
    assert encoders.prepare() is None
    status = encoders.status()
    assert status["supported"] and status["reason"] is None
    assert {n: t["state"] for n, t in status["towers"].items()} == {"audio": "released", "vision": "released"}
    assert status["towers"]["audio"]["bytes"] == 16 * 16 * 2 + 4 * 16 * 2
    assert status["resident_bytes"] == 0
    # The language model is never touched.
    assert model.params["language_model.model.embed_tokens.weight"].shape == (8, 4)
    assert not model.tower_ready("audio") and not model.tower_ready("vision")
    assert mx.cleared >= 2


def test_text_only_turn_never_loads_an_encoder(enc, checkpoint, executor, mx):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    enc._install_gate(model, encoders)
    reads = mx.reads
    assert model.get_input_embeddings([1, 2, 3]) == "embeds"
    assert model.get_input_embeddings(input_ids=[1], pixel_values=None, input_features=None) == "embeds"
    assert mx.reads == reads
    assert {t["state"] for t in encoders.status()["towers"].values()} == {"released"}


def test_audio_request_loads_only_the_audio_tower(enc, checkpoint, executor):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    enc._install_gate(model, encoders)
    model.get_input_embeddings([1], None, input_features="mel")
    towers = encoders.status()["towers"]
    assert towers["audio"]["state"] == "resident" and towers["audio"]["loads"] == 1
    assert towers["vision"]["state"] == "released"
    assert model.encoded == ["audio"]


def test_image_request_and_encode_image_load_vision(enc, checkpoint, executor):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    enc._install_gate(model, encoders)
    assert model.encode_image("pixels") == "features"
    assert encoders.status()["towers"]["vision"]["state"] == "resident"
    model.get_input_embeddings([1], "pixels")
    assert encoders.status()["towers"]["vision"]["loads"] == 1      # second use is instant


def test_cached_image_features_need_no_vision_tower(enc, checkpoint, executor):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    assert enc._needs(([1], "pixels"), {"cached_image_features": "f"}) == []
    assert enc._needs(([1],), {"pixel_values_videos": "v"}) == ["vision"]
    assert enc._needs(([1],), {"audio_features": "a"}) == ["audio"]


def test_request_arriving_mid_load_waits_for_it(enc, checkpoint, executor, mx):
    """A pre-warm still reading the checkpoint when the clip arrives: the request
    blocks until the load finishes and never sees released weights."""
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    enc._install_gate(model, encoders)
    mx.load_delay = 0.3
    prewarm = executor.submit(encoders.ensure, "audio")
    deadline = time.monotonic() + 2
    while encoders.status()["towers"]["audio"]["state"] != "loading" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert encoders.status()["towers"]["audio"]["state"] == "loading"
    # A different thread (status readers never block on the load) ...
    assert encoders.status()["towers"]["audio"]["state"] == "loading"
    # ... and the request itself, from another thread, waits on the lock.
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        "embeds", model.get_input_embeddings([1], input_features="mel")))
    started = time.monotonic()
    worker.start()
    worker.join(5)
    prewarm.result(5)
    assert result["embeds"] == "embeds"
    assert time.monotonic() - started >= 0.1
    assert encoders.status()["towers"]["audio"]["loads"] == 1


def test_request_queued_behind_a_prewarm_on_the_engine_thread(enc, checkpoint, executor, mx):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    enc._install_gate(model, encoders)
    mx.load_delay = 0.1
    prewarm = executor.submit(encoders.ensure, "vision")
    request = executor.submit(model.get_input_embeddings, [1], "pixels")
    assert request.result(5) == "embeds"
    prewarm.result(5)
    assert encoders.status()["towers"]["vision"]["loads"] == 1


def test_idle_release_after_the_window(enc, checkpoint, executor, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(enc, "_now", lambda: clock[0])
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    encoders.ensure("audio")
    encoders.ensure("vision")
    clock[0] += enc.IDLE_RELEASE_S - 1
    encoders.ensure("vision")                       # used again: its window restarts
    clock[0] += 2
    assert encoders.idle_due() == ["audio"]
    assert encoders.release(idle_only=True) == ["audio"]
    towers = encoders.status()["towers"]
    assert towers["audio"]["state"] == "released" and towers["vision"]["state"] == "resident"
    assert not model.tower_ready("audio") and model.tower_ready("vision")
    clock[0] += enc.IDLE_RELEASE_S
    assert encoders.release(idle_only=True) == ["vision"]


def test_default_idle_window_is_five_minutes(enc):
    assert enc.IDLE_RELEASE_S == 300.0


def test_idle_loop_releases_on_the_engine_thread(enc, checkpoint, executor, monkeypatch):
    monkeypatch.setattr(enc, "POLL_S", 0.01)
    model, encoders = make(enc, checkpoint, executor)
    encoders.idle_s = 0.05
    encoders.prepare()
    encoders.ensure("audio")
    threads = []
    real_release = encoders.release

    def release(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return real_release(*args, **kwargs)

    encoders.release = release

    async def run():
        encoders.start_idle()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if encoders.status()["towers"]["audio"]["state"] == "released":
                break
        encoders.stop()

    asyncio.run(run())
    assert encoders.status()["towers"]["audio"]["state"] == "released"
    assert threads and all(name != threading.main_thread().name for name in threads)


def test_unload_detaches_and_a_reload_starts_released(enc, checkpoint, executor):
    """Save memory parks the model: the engine stops, the idle task ends and the
    weights go with the model. The next load starts with both towers released."""

    class Engine:
        def __init__(self):
            self._vlm_model = Model()
            self._model_name = str(checkpoint)
            self._engine = types.SimpleNamespace(_mlx_executor=executor)
            self._loaded = True

    async def run():
        first = Engine()
        encoders = await enc.attach(first)
        assert encoders.supported and encoders._task is not None
        await encoders.run(encoders.ensure, "audio")
        enc.detach(first)
        assert encoders._task is None
        first._vlm_model = None
        second = Engine()
        again = await enc.attach(second)
        enc.detach(second)
        return encoders, again

    import gc
    encoders, again = asyncio.run(run())
    gc.collect()
    assert encoders._model() is None             # nothing here keeps a parked model alive
    assert {t["state"] for t in again.status()["towers"].values()} == {"released"}


@pytest.mark.parametrize("case, reason", [
    ("other_model", "unsupported_model"),
    ("disabled", "disabled"),
    ("mismatch", "reload_mismatch"),
    ("missing_tensor", "reload_incomplete"),
    ("no_engine_thread", "no_engine_thread"),
    ("no_checkpoint", "no_checkpoint"),
])
def test_fallback_keeps_everything_resident(enc, checkpoint, executor, mx, monkeypatch, case, reason):
    model = OtherModel() if case == "other_model" else Model()
    if case == "disabled":
        monkeypatch.setenv("WISP_ENGINE_LAZY_ENCODERS", "0")
    files = mx.files[str(checkpoint / "model.safetensors")]
    if case == "mismatch":
        files["vision_tower.encoder.0.weight"] = ((8, 8), 99.0)    # a sidecar or transform changed it
    if case == "missing_tensor":
        del files["audio_tower.layers.0.weight"]
    encoders = enc.Encoders(model, None if case == "no_checkpoint" else checkpoint,
                            executor=None if case == "no_engine_thread" else executor)
    assert encoders.prepare() == reason
    assert not encoders.supported
    assert model.tower_ready("audio") and model.tower_ready("vision")
    encoders.ensure("audio")                  # a no-op: nothing was released
    assert encoders.release() == []
    assert model.tower_ready("audio") and model.tower_ready("vision")


def test_unexpected_error_keeps_everything_resident(enc, checkpoint, executor, monkeypatch):
    model, encoders = make(enc, checkpoint, executor)
    monkeypatch.setattr(model, "sanitize", lambda weights: 1 / 0)
    assert encoders.prepare() == "error:ZeroDivisionError"
    assert model.tower_ready("audio") and model.tower_ready("vision")


def test_attach_installs_no_gate_when_unsupported(enc, checkpoint, executor):
    engine = types.SimpleNamespace(_vlm_model=OtherModel(), _model_name=str(checkpoint),
                                   _engine=types.SimpleNamespace(_mlx_executor=executor), _loaded=True)
    original = engine._vlm_model.get_input_embeddings
    encoders = asyncio.run(enc.attach(engine))
    assert encoders.reason == "unsupported_model"
    assert engine._vlm_model.get_input_embeddings == original
    assert encoders._task is None


def test_checkpoint_changed_on_disk_refuses_the_load(enc, checkpoint, executor):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    enc._install_gate(model, encoders)
    path = checkpoint / "model.safetensors"
    path.write_bytes(b"requantized in place")
    with pytest.raises(enc.EncoderUnavailable):
        model.get_input_embeddings([1], input_features="mel")
    assert not model.tower_ready("audio")
    assert encoders.status()["towers"]["audio"]["error"] == "checkpoint_changed"


def test_failed_load_leaves_placeholders_not_half_a_tower(enc, checkpoint, executor, mx):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    real = mx.load
    mx.load = lambda path: (_ for _ in ()).throw(OSError("disk"))
    with pytest.raises(OSError):
        encoders.ensure("vision")
    mx.load = real
    assert encoders.status()["towers"]["vision"]["state"] == "released"
    encoders.ensure("vision")
    assert encoders.status()["towers"]["vision"]["state"] == "resident"


def test_second_load_in_a_process_skips_the_bit_compare(enc, checkpoint, executor, mx, monkeypatch):
    compares = []
    real = mx.array_equal
    monkeypatch.setattr(mx, "array_equal", lambda a, b: compares.append(1) or real(a, b))
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    first = len(compares)
    assert first == 4                                  # every tower tensor, once
    model2, encoders2 = make(enc, checkpoint, executor)
    encoders2.prepare()
    assert encoders2.supported and len(compares) == first
    (checkpoint / "model.safetensors").write_bytes(b"other bytes")   # a new identity compares again
    model3, encoders3 = make(enc, checkpoint, executor)
    encoders3.prepare()
    assert len(compares) == 2 * first


# ----------------------------------------------------------------- endpoints
class Pool:
    def __init__(self, entries):
        self._entries = entries

    def get_entry(self, model_id):
        return self._entries.get(model_id)


def test_act_and_status_contract(enc, checkpoint, executor):
    model, encoders = make(enc, checkpoint, executor)
    encoders.prepare()
    engine = types.SimpleNamespace(_loaded=True, _wisp_encoders=encoders)
    pool = Pool({"gemma": types.SimpleNamespace(engine=engine),
                 "parked": types.SimpleNamespace(engine=None)})

    async def run():
        assert (await enc.act(pool, "parked", "audio", "prewarm"))[0] == 409
        assert (await enc.act(pool, "missing", "audio", "prewarm"))[0] == 409
        assert (await enc.act(pool, "gemma", "smell", "prewarm"))[0] == 422
        assert (await enc.act(pool, "gemma", "audio", "explode"))[0] == 404
        code, body = await enc.act(pool, "gemma", "audio", "prewarm")
        assert code == 200 and body["towers"]["audio"]["state"] == "resident"
        code, body = await enc.act(pool, "gemma", "all", "release")
        assert code == 200 and body["towers"]["audio"]["state"] == "released"
        return enc.pool_status(pool)

    status = asyncio.run(run())
    assert set(status["models"]) == {"gemma"}
    assert status["idle_release_s"] == enc.IDLE_RELEASE_S
    assert status["models"]["gemma"]["supported"] is True
