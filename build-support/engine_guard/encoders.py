"""Wisp: Gemma 4's audio and vision encoders load on demand, not with the model.

Installed only by the hash-bound packaging overlay as omlx/wisp_encoders.py.
Gemma 4 E4B carries a 610 MB audio tower and a 335 MB vision tower that stay
resident beside the language model even when no clip or picture ever arrives.
Here they are released once the model has loaded, loaded again from the
checkpoint the first time a request (or a pre-warm from Wisp's app on a mic or
Look press) needs one, and released after `IDLE_RELEASE_S` without use.

Rules:
- IMPORTANT: a request never fails or runs on missing weights. A request that
  carries audio or pixels loads the tower it needs before encoding, on the
  same engine thread, so a pre-warm still loading finishes first and the
  request waits for it.
- IMPORTANT: one engine thread. Loads and releases run on the engine's own
  single-worker MLX executor (the request gate is already on it), so they never
  run beside a scheduler step on the same model.
- IMPORTANT: bit-exact or resident. Lazy mode turns on only after every
  released tensor, read back from the checkpoint the same way mlx-vlm read it,
  matched the resident weights exactly. Any other model, engine setting,
  sidecar or mismatch keeps today's behavior: everything resident.
- A checkpoint changed on disk since the model loaded is never read into the
  live model; the load refuses until the model is reloaded.
- The language model, its KV/prefix cache and the vision feature cache are
  untouched: only the tower weights come and go.
- The pool's admission estimate still counts the towers (it reads file sizes),
  so a reload never needs memory the guard did not already admit.
"""
from __future__ import annotations

import asyncio
import functools
import gc
import json
import logging
import os
import threading
import time
import weakref
from pathlib import Path

logger = logging.getLogger(__name__)

IDLE_RELEASE_S = 300.0
POLL_S = 15.0
VERSION = 1
MODALITIES = {
    "audio": ("audio_tower.", "embed_audio."),
    "vision": ("vision_tower.", "embed_vision."),
}
# mlx-vlm's Gemma 4 model class; Bolt E4B and the other E2B/E4B builds share it.
SUPPORTED_CLASSES = {"mlx_vlm.models.gemma4.gemma4.Model"}
_DISABLE_ENV = "WISP_ENGINE_LAZY_ENCODERS"
# A tower whose checkpoint-backed tensors are less than this share of its bytes
# is not a plain checkpoint tower (computed tables, a sidecar): keep it resident.
_MIN_COVERAGE = 0.99
_COMPARE_BATCH = 64
# Checkpoints whose read-back already matched bit for bit in this engine process,
# keyed on the files' identity: a Save memory reload skips the second read.
_VERIFIED = set()


class EncoderUnavailable(RuntimeError):
    """A tower could not be loaded; the request fails rather than run without it."""


def _modality_of(key):
    for name, prefixes in MODALITIES.items():
        if key.startswith(prefixes):
            return name
    return None


def _now():
    return time.monotonic()


def _shards(model_dir):
    """The safetensors files mlx-vlm read, in its order."""
    index = model_dir / "model.safetensors.index.json"
    if index.is_file():
        weight_map = json.loads(index.read_text()).get("weight_map") or {}
        names = sorted(set(weight_map.values()))
        files = [model_dir / name for name in names if (model_dir / name).exists()]
        if files:
            return files
    return sorted(p for p in model_dir.glob("*.safetensors") if not p.name.endswith("consolidated.safetensors"))


def _identity(files):
    out = []
    for path in files:
        info = path.stat()
        out.append((str(path), info.st_size, info.st_mtime_ns, info.st_ino))
    return tuple(out)


class _Tower:
    def __init__(self, name):
        self.name = name
        self.keys = []          # parameter paths released and reloaded
        self.specs = {}         # path -> (shape, dtype)
        self.bytes = 0
        self.state = "resident"
        self.last_used = _now()
        self.loads = 0
        self.last_load_ms = None
        self.last_error = None

    def row(self, now):
        return {"state": self.state, "bytes": self.bytes, "loads": self.loads,
                "last_load_ms": self.last_load_ms,
                "idle_s": round(max(0.0, now - self.last_used), 1),
                "error": self.last_error}


class Encoders:
    """Per-engine tower residency. `reason` is None only when lazy mode is on."""

    def __init__(self, model, model_dir, *, executor=None, idle_s=IDLE_RELEASE_S):
        self._model = weakref.ref(model) if model is not None else (lambda: None)
        self.model_dir = Path(model_dir) if model_dir else None
        self.executor = executor
        self.idle_s = float(idle_s)
        self.reason = "pending"
        self.towers = {}
        self._files = []
        self._identity = ()
        self._lock = threading.RLock()
        self._task = None
        self.attach_ms = None

    # ---------------------------------------------------------------- status
    @property
    def supported(self):
        return self.reason is None

    def status(self):
        # No lock: the event loop reads this while the engine thread may hold it
        # through a load. Each field is a plain read; a row may lag one transition.
        now = _now()
        towers = dict(self.towers)
        return {"version": VERSION, "supported": self.supported, "reason": self.reason,
                "idle_release_s": self.idle_s, "attach_ms": self.attach_ms,
                "resident_bytes": sum(t.bytes for t in towers.values() if t.state == "resident"),
                "towers": {name: t.row(now) for name, t in towers.items()}}

    # ------------------------------------------------------------ reading back
    def _read(self, modalities):
        """Checkpoint tensors for `modalities`, through the model's own sanitize."""
        import mlx.core as mx
        model = self._model()
        if model is None:
            raise EncoderUnavailable("The model is no longer loaded.")
        try:
            from mlx_vlm.utils import _load_safetensors as load
        except ImportError:  # pragma: no cover - older mlx-vlm
            load = mx.load
        prefixes = tuple(p for m in modalities for p in MODALITIES[m])
        raw = {}
        for path in self._files:
            for key, value in load(str(path)).items():
                bare = key[len("model."):] if key.startswith("model.") else key
                if bare.startswith(prefixes):
                    raw[key] = value
        sanitized = model.sanitize(raw) if hasattr(model, "sanitize") else raw
        return {k: v for k, v in sanitized.items() if k.startswith(prefixes)}

    # ----------------------------------------------------------------- attach
    def prepare(self):
        """Engine thread, once, right after load: qualify and release. Never raises."""
        started = _now()
        try:
            self.reason = self._qualify()
            if self.reason is None:
                for name in list(self.towers):
                    self._release(name)
        except Exception as exc:  # noqa: BLE001 - anything unexpected keeps everything resident
            logger.warning("wisp encoders: lazy mode off (%s)", type(exc).__name__, exc_info=True)
            self.reason = f"error:{type(exc).__name__}"
            self._restore()
        self.attach_ms = int((_now() - started) * 1000)
        return self.reason

    def _restore(self):
        """A release failed partway: put back every tower it touched. One that can't
        be put back is "unavailable", and its requests fail (ensure) rather than run
        on placeholder weights."""
        for tower in self.towers.values():
            if tower.state == "resident":
                continue           # its release never began: the weights are untouched
            try:
                self._load(tower)
            except BaseException as exc:  # noqa: BLE001 - fail closed for this tower only
                tower.state = "unavailable"
                tower.last_error = tower.last_error or type(exc).__name__
                logger.warning("wisp encoders: %s could not be restored (%s)", tower.name, type(exc).__name__)

    def broken(self):
        return any(t.state == "unavailable" for t in self.towers.values())

    def _qualify(self):
        import mlx.core as mx
        from mlx.utils import tree_flatten
        if os.environ.get(_DISABLE_ENV, "1") == "0":
            return "disabled"
        model = self._model()
        if model is None:
            return "no_model"
        name = f"{type(model).__module__}.{type(model).__qualname__}"
        if name not in SUPPORTED_CLASSES:
            return "unsupported_model"
        if self.executor is None:
            return "no_engine_thread"
        if self.model_dir is None or not self.model_dir.is_dir():
            return "no_checkpoint"
        present = [m for m in MODALITIES if getattr(model, MODALITIES[m][0].rstrip("."), None) is not None]
        if not present:
            return "no_encoders"
        self._files = _shards(self.model_dir)
        if not self._files:
            return "no_checkpoint"
        self._identity = _identity(self._files)
        resident = {k: v for k, v in tree_flatten(model.parameters()) if _modality_of(k) in present}
        reread = self._read(present)
        towers = {m: _Tower(m) for m in present}
        total = dict.fromkeys(present, 0)
        for key, value in resident.items():
            total[_modality_of(key)] += value.nbytes
        for key, value in reread.items():
            current = resident.get(key)
            if current is None or tuple(current.shape) != tuple(value.shape) or current.dtype != value.dtype:
                return "reload_mismatch"
            tower = towers[_modality_of(key)]
            tower.keys.append(key)
            tower.specs[key] = (tuple(value.shape), value.dtype)
            tower.bytes += current.nbytes
        for m, tower in towers.items():
            if not tower.keys or tower.bytes < _MIN_COVERAGE * total[m]:
                return "reload_incomplete"
        # Bit-exact: read back the way a later load will, compare to resident.
        keys = sorted(reread)
        proof = (name, self._identity, tuple(keys))
        for start in range(0, 0 if proof in _VERIFIED else len(keys), _COMPARE_BATCH):
            batch = keys[start:start + _COMPARE_BATCH]
            same = [mx.array_equal(resident[k], reread[k]) for k in batch]
            mx.eval(same)
            if not all(bool(s.item()) for s in same):
                return "reload_mismatch"
            for k in batch:
                reread[k] = None
        del reread, resident
        _VERIFIED.add(proof)
        self.towers = towers
        return None

    # ------------------------------------------------------------ load/release
    def ensure(self, modality):
        """Engine thread: make `modality` resident (no-op when it is, or off)."""
        if self.reason is not None:
            tower = self.towers.get(modality)
            if tower is not None and tower.state == "unavailable":
                raise EncoderUnavailable("This model's encoder couldn't be restored. Reload the model.")
            return
        with self._lock:
            tower = self.towers.get(modality)
            if tower is None:
                return
            tower.last_used = _now()
            if tower.state == "resident":
                return
            self._load(tower)

    def _load(self, tower):
        import mlx.core as mx
        model = self._model()
        if model is None:
            raise EncoderUnavailable("The model is no longer loaded.")
        if _identity(self._files) != self._identity:
            tower.last_error = "checkpoint_changed"
            raise EncoderUnavailable("The model's files changed on disk. Reload the model to use its encoders.")
        tower.state = "loading"
        started = _now()
        try:
            weights = self._read([tower.name])
            if set(weights) != set(tower.keys) or any(
                    (tuple(v.shape), v.dtype) != tower.specs[k] for k, v in weights.items()):
                raise EncoderUnavailable("The model's encoder weights don't match the loaded model.")
            model.load_weights(list(weights.items()), strict=False)
            mx.eval([weights[k] for k in tower.keys])
            del weights
        except BaseException as exc:
            tower.state = "released"
            tower.last_error = type(exc).__name__
            self._placeholders(model, tower)
            raise
        tower.state = "resident"
        tower.loads += 1
        tower.last_error = None
        tower.last_used = _now()
        tower.last_load_ms = int((tower.last_used - started) * 1000)
        logger.info("wisp encoders: %s loaded in %d ms", tower.name, tower.last_load_ms)

    def _placeholders(self, model, tower):
        import mlx.core as mx
        empty = [(k, mx.zeros((0,), dtype=tower.specs[k][1])) for k in tower.keys]
        model.load_weights(empty, strict=False)
        mx.eval([v for _, v in empty])

    def release(self, modality=None, *, idle_only=False):
        """Engine thread: drop resident towers (all when `modality` is None)."""
        if self.reason is not None:
            return []
        released = []
        with self._lock:
            now = _now()
            for name in ([modality] if modality else list(self.towers)):
                tower = self.towers.get(name)
                if tower is None or tower.state != "resident":
                    continue
                if idle_only and now - tower.last_used < self.idle_s:
                    continue
                self._release(name)
                released.append(name)
        return released

    def _release(self, name):
        import mlx.core as mx
        model = self._model()
        tower = self.towers[name]
        tower.state = "released"   # first: a placeholder write that fails partway still counts
        if model is not None:
            self._placeholders(model, tower)
        gc.collect()
        mx.clear_cache()

    def idle_due(self, now=None):
        now = _now() if now is None else now
        return [n for n, t in dict(self.towers).items()
                if t.state == "resident" and now - t.last_used >= self.idle_s]

    # ------------------------------------------------------------- async glue
    async def run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, functools.partial(fn, *args))

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(min(POLL_S, max(1.0, self.idle_s / 4)))
            if self.idle_due():
                try:
                    released = await self.run(functools.partial(self.release, idle_only=True))
                    if released:
                        logger.info("wisp encoders: released idle %s", ", ".join(released))
                except Exception:  # noqa: BLE001 - try again next poll
                    logger.debug("wisp encoders: idle release failed", exc_info=True)

    def start_idle(self):
        if self.supported and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._idle_loop())

    def stop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()


# -------------------------------------------------------------- request gate
def _needs(args, kwargs):
    needs = []
    pixels = kwargs.get("pixel_values", args[1] if len(args) > 1 else None)
    if (pixels is not None and kwargs.get("cached_image_features") is None) or (
            kwargs.get("pixel_values_videos") is not None and kwargs.get("cached_video_features") is None):
        needs.append("vision")
    if any(kwargs.get(k) is not None for k in ("audio_features", "input_features")):
        needs.append("audio")
    return needs


def _install_gate(model, encoders):
    """Every encode in Gemma 4 goes through these two methods on the instance."""
    embed = model.get_input_embeddings
    encode_image = getattr(model, "encode_image", None)

    @functools.wraps(embed)
    def get_input_embeddings(*args, **kwargs):
        for modality in _needs(args, kwargs):
            encoders.ensure(modality)
        return embed(*args, **kwargs)

    model.get_input_embeddings = get_input_embeddings
    if encode_image is not None:
        @functools.wraps(encode_image)
        def gated_encode_image(*args, **kwargs):
            encoders.ensure("vision")
            return encode_image(*args, **kwargs)

        model.encode_image = gated_encode_image


# ---------------------------------------------------------- engine lifecycle
def _engine_executor(engine):
    core = getattr(engine, "_engine", None)
    return getattr(core, "_mlx_executor", None) if core is not None else None


async def attach(engine):
    """VLMBatchedEngine.start(), after every load-time transform. Never raises."""
    model = getattr(engine, "_vlm_model", None)
    encoders = Encoders(model, getattr(engine, "_model_name", None), executor=_engine_executor(engine))
    engine._wisp_encoders = encoders
    if model is None or encoders.executor is None:
        encoders.reason = "no_engine_thread" if model is not None else "no_model"
        return encoders
    try:
        reason = await encoders.run(encoders.prepare)
    except Exception as exc:  # noqa: BLE001 - executor trouble: keep everything resident
        encoders.reason = f"error:{type(exc).__name__}"
        return encoders
    if reason is not None and encoders.broken():
        _install_gate(model, encoders)     # fail closed: refuse that encoder's requests
        logger.warning("wisp encoders: %s unavailable for %s (%s)", ", ".join(
            n for n, t in encoders.towers.items() if t.state == "unavailable"), engine._model_name, reason)
    elif reason is None:
        _install_gate(model, encoders)
        encoders.start_idle()
        logger.info("wisp encoders: on demand for %s (%s)", engine._model_name,
                    ", ".join(f"{n} {t.bytes // 1_000_000} MB" for n, t in encoders.towers.items()))
    else:
        logger.info("wisp encoders: resident for %s (%s)", getattr(engine, "_model_name", "?"), reason)
    return encoders


def detach(engine):
    encoders = getattr(engine, "_wisp_encoders", None)
    if encoders is not None:
        encoders.stop()


# ----------------------------------------------------------------- endpoints
def _loaded_engines(pool):
    entries = getattr(pool, "_entries", None) or {}
    for model_id, entry in list(entries.items()):
        engine = getattr(entry, "engine", None)
        if engine is not None and getattr(engine, "_loaded", False):
            yield model_id, engine


def pool_status(pool):
    models = {}
    for model_id, engine in _loaded_engines(pool):
        encoders = getattr(engine, "_wisp_encoders", None)
        models[model_id] = encoders.status() if encoders is not None else {
            "version": VERSION, "supported": False, "reason": "not_attached", "towers": {}}
    return {"version": VERSION, "idle_release_s": IDLE_RELEASE_S, "models": models}


async def act(pool, model_id, modality, action):
    """(status_code, body) for POST /v1/wisp/encoders/{modality}/{action}."""
    if action not in ("prewarm", "release"):
        return 404, {"error": "unknown_action"}
    if modality not in MODALITIES and not (action == "release" and modality == "all"):
        return 422, {"error": "unknown_modality"}
    entry = pool.get_entry(model_id) if pool is not None and model_id else None
    engine = getattr(entry, "engine", None) if entry is not None else None
    if engine is None or not getattr(engine, "_loaded", False):
        return 409, {"error": "not_loaded", "model": model_id}
    encoders = getattr(engine, "_wisp_encoders", None)
    if encoders is None:
        return 200, {"model": model_id, "supported": False, "reason": "not_attached", "towers": {}}
    if encoders.supported:
        try:
            if action == "prewarm":
                await encoders.run(encoders.ensure, modality)
            else:
                await encoders.run(encoders.release, None if modality == "all" else modality)
        except EncoderUnavailable as exc:
            return 409, {"error": "encoder_unavailable", "message": str(exc), **encoders.status()}
    return 200, {"model": model_id, **encoders.status()}
