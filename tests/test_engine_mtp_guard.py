"""The bundled engine's MTP safety net (build-support/engine_guard/mtp_guard.py)
and the overlay patch that wires it into the engine's MTP step.

Synthetic: a fake `mlx.core` whose custom kernels reject a threadgroup larger
than a per-pipeline limit when the lazy graph is evaluated, as Metal does on
an M1-class GPU ("Thread group size (512) is greater than the maximum allowed
threads per threadgroup (448)"). No engine, model or GPU.

The tests that read upstream oMLX source skip when it is not on this Mac
(set WISP_ENGINE_SOURCE to an oMLX.app's Contents/Resources to point at one).
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_DIR = ROOT / "build-support" / "engine_guard"
SOURCE = Path(os.environ.get("WISP_ENGINE_SOURCE", "/Applications/oMLX.app/Contents/Resources"))
BATCH_GENERATOR = "omlx/patches/mlx_lm_mtp/batch_generator.py"
LIMIT_ERROR = "Thread group size ({asked}) is greater than  the maximum allowed threads per threadgroup ({cap})."


def _there(path):
    try:
        return path.is_file()
    except OSError:
        return False


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard():
    module = _load("wisp_mtp_guard_under_test", GUARD_DIR / "mtp_guard.py")
    module._reset_for_tests()
    return module


class Fallback(RuntimeError):
    pass


# ------------------------------------------------------------------ fake mlx
class Lazy:
    """An unevaluated array: a shape, a dtype and the kernel launches behind it."""

    def __init__(self, shape=(), dtype="bfloat16", launches=()):
        self.shape = tuple(shape)
        self.dtype = dtype
        self.launches = list(launches)

    @property
    def ndim(self):
        return len(self.shape)

    def __mul__(self, other):
        return Lazy(self.shape, self.dtype, self.launches)

    def __getitem__(self, key):
        key = key if isinstance(key, tuple) else (key,)
        shape = list(self.shape)
        for axis, part in enumerate(key):
            if isinstance(part, slice):
                shape[axis] = len(range(*part.indices(shape[axis])))
        return Lazy(shape, self.dtype, self.launches)

    def tolist(self):
        FakeMX.current.eval(self)
        return [0]


class FakeMX(types.ModuleType):
    """`mlx.core` with Metal's per-pipeline threadgroup limit, checked at eval."""

    current = None
    float16, bfloat16, float32, int32 = "float16", "bfloat16", "float32", "int32"

    def __init__(self, caps):
        super().__init__("mlx.core")
        self.caps = caps                    # kernel-name prefix -> thread limit
        self.launched = []                  # (name, threads) of every evaluated launch
        self.array = Lazy
        self.fast = types.SimpleNamespace(metal_kernel=self._metal_kernel)
        self.metal = types.SimpleNamespace(is_available=lambda: True)
        FakeMX.current = self

    def _cap(self, name):
        return next((cap for prefix, cap in self.caps.items() if name.startswith(prefix)), 1024)

    def _metal_kernel(self, name, **_):
        def launch(inputs, threadgroup, output_shapes, output_dtypes, **__):
            threads = threadgroup[0] * threadgroup[1] * threadgroup[2]
            behind = [x for i in inputs if isinstance(i, Lazy) for x in i.launches]
            return [Lazy(shape, dtype, behind + [(name, threads)])
                    for shape, dtype in zip(output_shapes, output_dtypes)]
        return launch

    def zeros(self, shape, dtype=None):
        return Lazy(shape, dtype)

    def concatenate(self, arrays, axis=0):
        shape = list(arrays[0].shape)
        shape[axis] = sum(a.shape[axis] for a in arrays)
        return Lazy(shape, arrays[0].dtype, [x for a in arrays for x in a.launches])

    def eval(self, *arrays):
        for array in arrays:
            for name, threads in array.launches:
                if threads > self._cap(name):
                    raise ValueError(LIMIT_ERROR.format(asked=threads, cap=self._cap(name)))
                self.launched.append((name, threads))


# ---------------------------------------------------------------- the guard
def test_a_failed_step_becomes_the_engines_fallback_and_turns_mtp_off(guard, caplog):
    def step(batch, state):
        raise ValueError(LIMIT_ERROR.format(asked=512, cap=448))

    assert not guard.disabled()
    with caplog.at_level(logging.DEBUG, logger="omlx.wisp_mtp_guard"):
        with pytest.raises(Fallback) as caught:
            guard.run(step, Fallback, "batch", "state")
        with pytest.raises(Fallback):
            guard.run(step, Fallback, "batch", "state")
    assert isinstance(caught.value.__cause__, ValueError)
    assert guard.disabled()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1                       # logged once, not per step
    assert "plain decoding" in warnings[0].getMessage()
    assert "512" in guard.failure() and "448" in guard.failure()


def test_a_working_step_and_the_engines_own_fallback_change_nothing(guard):
    assert guard.run(lambda a, b=0: a + b, Fallback, 2, b=3) == 5

    def falls_back():
        raise Fallback("verify cycle produced no emit tokens")

    with pytest.raises(Fallback):
        guard.run(falls_back, Fallback)
    assert not guard.disabled()


def test_engine_memory_errors_and_interrupts_pass_through(guard):
    class OMLXError(Exception):
        pass

    class PrefillMemoryAbortedError(OMLXError):
        pass

    for error in (PrefillMemoryAbortedError("over the limit"), MemoryError(), KeyboardInterrupt()):
        def step(error=error):
            raise error

        with pytest.raises(type(error)):
            guard.run(step, Fallback)
    assert not guard.disabled()


def test_the_log_names_the_kernel_that_asked_for_too_many_threads(guard):
    mx = FakeMX({"too_wide": 448})
    assert guard.install(mx.fast)
    assert guard.install(mx.fast)                    # idempotent: wrapped once
    assert getattr(mx.fast.metal_kernel, "_wisp_original", None) is not None
    assert not hasattr(mx.fast.metal_kernel._wisp_original, "_wisp_recorded")
    wide = mx.fast.metal_kernel(name="too_wide_p1")
    fine = mx.fast.metal_kernel(name="fine")
    kwargs = dict(inputs=[], output_shapes=[(1,)], output_dtypes=["float32"])
    fine(threadgroup=(256, 1, 1), **kwargs)
    (out,) = wide(threadgroup=(32, 4, 4), **kwargs)

    with pytest.raises(Fallback):
        guard.run(out.tolist, Fallback)
    assert guard.kernels_asking_for(512) == ["too_wide_p1 (32, 4, 4)"]
    assert "asked by: too_wide_p1 (32, 4, 4)" in guard.failure()
    assert "allows 448" in guard.failure()


def test_recording_never_breaks_a_kernel_call(guard):
    mx = FakeMX({})
    guard.install(mx.fast)
    kernel = mx.fast.metal_kernel(name="odd")
    (out,) = kernel(inputs=[], threadgroup=(32, 1, 1), output_shapes=[(1,)], output_dtypes=["float32"])
    assert out.shape == (1,)

    def raw(*args, **kwargs):
        return "ran"

    recorded = guard._Recorded(raw, "raw")
    assert recorded(threadgroup=("x", None, 1)) == "ran"     # a bad size is ignored, not raised
    assert recorded() == "ran"


# -------------------------------------------------------- the shipped patch
def _patch():
    return json.loads((GUARD_DIR / "batch_generator.py.patch.json").read_text())


def test_the_patch_wraps_both_mtp_steps_and_gates_new_activations():
    patch = _patch()
    assert patch["path"] == BATCH_GENERATOR
    new = "\n".join(edit["new"] for edit in patch["edits"])
    assert "from omlx import wisp_mtp_guard as _wisp_mtp_guard" in new
    assert "_wisp_mtp_guard.install()" in new
    for step in ("_mtp_next", "_mtp_batch_next"):
        wrapped = [e for e in patch["edits"] if f"return {step}(" in e["old"]]
        assert len(wrapped) == 1
        assert "_wisp_mtp_guard.run(" in wrapped[0]["new"]
        assert "_MtpStepFallback" in wrapped[0]["new"]
        assert "_spec_command_buffers(model)" in wrapped[0]["new"]
    gate = [e for e in patch["edits"] if "_wisp_mtp_guard.disabled()" in e["new"]]
    assert len(gate) == 1
    # A request that already holds MTP state is not cut off without a reconcile.
    assert '"_omlx_mtp_state"' in gate[0]["new"] and '"_omlx_mtp_batch_state"' in gate[0]["new"]


def _patched_batch_generator_source():
    upstream = SOURCE / BATCH_GENERATOR
    if not _there(upstream):
        pytest.skip(f"no upstream oMLX source at {SOURCE}")
    text = upstream.read_text()
    for edit in _patch()["edits"]:
        if text.count(edit["old"]) != edit["count"]:
            pytest.skip("upstream oMLX source here is not the pinned version")
        text = text.replace(edit["old"], edit["new"])
    return text


class _Sibling(types.ModuleType):
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return None


@pytest.fixture
def engine(guard, monkeypatch):
    """The patched engine MTP module, loaded against stand-in packages."""
    source = _patched_batch_generator_source()
    package = "omlx.patches.mlx_lm_mtp"
    stubs = {name: types.ModuleType(name) for name in (
        "omlx", "omlx.patches", package, "omlx.prefill_progress", package + ".cache_rollback",
        package + ".context_copy", package + ".prompt_priming", "mlx_lm", "mlx_lm.generate")}
    for name in ("omlx", "omlx.patches", package, "mlx_lm"):
        stubs[name].__path__ = []
    for name in ("cache_rollback", "context_copy", "prompt_priming"):
        stubs[f"{package}.{name}"] = _Sibling(f"{package}.{name}")   # constants read at import
    stubs[package + ".prompt_priming"].decode_scope = lambda *a, **k: contextlib.nullcontext()
    stubs["omlx.prefill_progress"].get_prefill_tracker = lambda *a, **k: None
    stubs["omlx"].wisp_mtp_guard = guard
    stubs["omlx.wisp_mtp_guard"] = guard

    class GenerationBatch:
        def __init__(self, model=None, uids=()):
            self.model, self.uids, self.standard_steps = model, list(uids), 0

        def next(self):
            self.standard_steps += 1
            return "standard step"

        def filter(self, *a, **k):
            pass

        def extend(self, *a, **k):
            pass

    class BatchGenerator:
        def _next(self, *a, **k):
            pass

        def remove(self, *a, **k):
            pass

        def close(self):
            pass

    stubs["mlx_lm.generate"].GenerationBatch = GenerationBatch
    stubs["mlx_lm.generate"].BatchGenerator = BatchGenerator
    mx = FakeMX({})
    stubs["mlx"] = types.ModuleType("mlx")
    stubs["mlx"].__path__ = []
    stubs["mlx"].core = mx
    stubs["mlx.core"] = mx
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)

    module = types.ModuleType(package + ".batch_generator")
    module.__package__ = package
    module.__file__ = str(SOURCE / BATCH_GENERATOR)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    assert module.apply() is True

    module.reconciled = []
    monkeypatch.setattr(module, "_is_mtp_batch_eligible", lambda batch: False)
    monkeypatch.setattr(module, "_singleton_mtp_handoff_ready", lambda batch: False)
    monkeypatch.setattr(module, "_mtp_park_state_for_batch", lambda batch: None)
    monkeypatch.setattr(module, "_log_multirow_mtp_inactive_once", lambda batch: None)
    monkeypatch.setattr(module, "_mark_standard_multirow_decode", lambda batch: None)
    monkeypatch.setattr(module, "_drafter_for", lambda model: None)
    monkeypatch.setattr(module, "_drafter_scope", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(module, "_ineligibility_reason", lambda batch: None)
    monkeypatch.setattr(module, "_model_has_mtp_module", lambda model: True)
    monkeypatch.setattr(module, "_model_mtp_decode_enabled", lambda model: True)
    monkeypatch.setattr(module, "_has_grammar_processors", lambda batch: False)
    monkeypatch.setattr(module, "_has_xtc_sampler", lambda batch: False)

    def prepare(batch):
        batch._omlx_mtp_state = types.SimpleNamespace(uid=batch.uids[0], reentry_probe=False, stats=None)
        return batch._omlx_mtp_state

    def reconcile(batch, state):
        module.reconciled.append(state.uid)
        return True

    monkeypatch.setattr(module, "_prepare_mtp_state_for_next", prepare)
    monkeypatch.setattr(module, "_reconcile_mtp_to_standard", reconcile)
    module.GenerationBatch = GenerationBatch
    module.model = types.SimpleNamespace(mtp_forward=lambda *a, **k: None)
    return module


def test_the_engine_starts_recording_kernels_when_the_mtp_module_loads(engine):
    assert getattr(sys.modules["mlx.core"].fast.metal_kernel, "_wisp_recorded", False)


def test_a_verify_error_does_not_fail_the_request(engine, guard, monkeypatch, caplog):
    """The tester's failure: the verify cycle's one host sync raises Metal's
    threadgroup error. The reply must carry on with plain decoding."""
    verify_cycles = []

    def mtp_next(batch, state):
        verify_cycles.append(state.uid)
        wide = sys.modules["mlx.core"].fast.metal_kernel(name="verify_kernel")
        (result,) = wide(inputs=[], threadgroup=(32, 4, 4), output_shapes=[(4,)],
                         output_dtypes=["int32"])
        sys.modules["mlx.core"].caps["verify_kernel"] = 448
        return result.tolist()                      # host = result.tolist()

    monkeypatch.setattr(engine, "_mtp_next", mtp_next)
    batch = engine.GenerationBatch(engine.model, [7])

    with caplog.at_level(logging.WARNING, logger="omlx.wisp_mtp_guard"):
        assert batch.next() == "standard step"      # no exception reaches the engine loop
        assert batch.next() == "standard step"
        assert batch.next() == "standard step"
    assert verify_cycles == [7]                     # MTP is not tried again
    assert engine.reconciled == [7]                 # the cache was rebuilt once, from streamed tokens
    assert batch.standard_steps == 3
    assert getattr(batch, "_omlx_mtp_state", None) is None
    assert guard.disabled()
    assert len(caplog.records) == 1
    assert "verify_kernel (32, 4, 4)" in caplog.records[0].getMessage()

    # The next request in this engine process starts on plain decoding too.
    later = engine.GenerationBatch(engine.model, [8])
    assert later.next() == "standard step"
    assert verify_cycles == [7] and engine.reconciled == [7]


def test_a_reply_that_cannot_be_restored_still_fails_loudly(engine, monkeypatch):
    def mtp_next(batch, state):
        raise ValueError(LIMIT_ERROR.format(asked=512, cap=448))

    monkeypatch.setattr(engine, "_mtp_next", mtp_next)
    monkeypatch.setattr(engine, "_reconcile_mtp_to_standard", lambda batch, state: False)
    batch = engine.GenerationBatch(engine.model, [7])
    with pytest.raises(RuntimeError, match="could not restore the committed cache"):
        batch.next()


def test_a_request_already_in_mtp_keeps_its_state_after_another_one_failed(engine, guard):
    guard.note_failure(ValueError("boom"))
    fresh = engine.GenerationBatch(engine.model, [1])
    running = engine.GenerationBatch(engine.model, [2])
    running._omlx_mtp_state = object()
    assert engine._mtp_common_eligible(fresh) is False
    assert engine._mtp_common_eligible(running) is True


def test_mtp_runs_as_before_while_nothing_has_failed(engine, monkeypatch):
    monkeypatch.setattr(engine, "_mtp_next", lambda batch, state: "mtp step")
    batch = engine.GenerationBatch(engine.model, [7])
    assert batch.next() == "mtp step"
    assert batch.next() == "mtp step"
    assert batch.standard_steps == 0 and engine.reconciled == []


# ------------------------------------- upstream's fused Gemma verify kernel
@pytest.fixture
def kernel_under(monkeypatch):
    """Upstream gemma4_verify_kernel.py on a GPU with the given pipeline limits."""
    path = SOURCE / "omlx" / "patches" / "gemma4_verify_kernel.py"
    if not _there(path):
        pytest.skip(f"no upstream oMLX source at {SOURCE}")

    def load(pass1, pass2):
        mx = FakeMX({"omlx_g4_verify_sdpa_p1": pass1, "omlx_g4_verify_sdpa_p2": pass2})
        mlx = types.ModuleType("mlx")
        mlx.__path__ = []
        mlx.core = mx
        monkeypatch.setitem(sys.modules, "mlx", mlx)
        monkeypatch.setitem(sys.modules, "mlx.core", mx)
        return _load("gemma4_verify_kernel_under_test", path), mx

    return load


@pytest.mark.parametrize("gqa,rows_per_launch", [(8, 2), (4, 4), (2, 8)])
def test_fused_kernel_sizes_both_passes_to_a_448_thread_limit(kernel_under, gqa, rows_per_launch):
    """Pass 1 is the pipeline Metal limits to 448 on M1-class GPUs; pass 2
    always asks for 1024, so it runs only where that pipeline allows it."""
    kernel, mx = kernel_under(pass1=448, pass2=1024)
    kv_heads = 2
    assert kernel.kernel_max_rows(gqa, mx.bfloat16) == rows_per_launch
    mx.launched.clear()
    for length in range(2, 13):
        queries = mx.zeros((1, kv_heads * gqa, length, 512), dtype=mx.bfloat16)
        buffer = mx.zeros((1, kv_heads, 4096, 512), dtype=mx.bfloat16)
        out = kernel.fused_verify_sdpa(queries, buffer, buffer, 1000 + length, 1.0)
        mx.eval(out)                                 # raises if any launch is over its limit
        assert out.shape == (1, kv_heads * gqa, length, 512)
    by_pass = {"p1": [], "p2": []}
    for name, threads in mx.launched:
        by_pass["p1" if "_p1_" in name else "p2"].append(threads)
    assert by_pass["p1"] and max(by_pass["p1"]) <= 448
    assert by_pass["p2"] and max(by_pass["p2"]) <= 1024


def test_fused_kernel_is_not_used_when_pass_two_cannot_fit(kernel_under):
    kernel, mx = kernel_under(pass1=448, pass2=448)
    assert kernel.kernel_max_rows(4, mx.bfloat16) == 0
    assert kernel.is_available() is False            # the attention patch then keeps the stock route
    queries = mx.zeros((1, 8, 4, 512), dtype=mx.bfloat16)
    buffer = mx.zeros((1, 2, 64, 512), dtype=mx.bfloat16)
    with pytest.raises(ValueError):                  # refused while building, before any cache update
        kernel.fused_verify_sdpa(queries, buffer, buffer, 32, 1.0)
    assert all(threads <= 448 for _, threads in mx.launched)


def test_fused_kernel_is_not_used_when_even_one_row_group_is_too_wide(kernel_under):
    kernel, mx = kernel_under(pass1=200, pass2=1024)
    assert kernel.kernel_max_rows(8, mx.bfloat16) == 0
    assert kernel.is_available() is False


def test_small_gemma_models_never_reach_the_fused_kernel():
    """E2B/E4B share KV across layers; the attention patch leaves them on the
    stock route before it looks at the kernel at all. (This is why the fused
    kernel is not what fails for Wisp's default model.)"""
    path = SOURCE / "omlx" / "patches" / "gemma4_verify_attention.py"
    if not _there(path):
        pytest.skip(f"no upstream oMLX source at {SOURCE}")
    text = path.read_text()
    body = text[text.index("    def __call__(self, x, mask=None, cache=None"):]
    gate = body[:body.index("use_kernel = (")]
    assert 'getattr(self.config, "num_kv_shared_layers", 0)' in gate
    assert "return original_call(" in gate
    assert "gemma4_verify_kernel" not in gate
