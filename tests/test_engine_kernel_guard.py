"""The bundled engine's Metal threadgroup guard and MTP fallback.

Every Ask reply on an M1 Pro ended with "Thread group size (512) is greater
than the maximum allowed threads per threadgroup (448)": a custom kernel in the
MTP verify path (oMLX 0.7.3rc1's omlx_m1_verify_mma5, upstream issue #4913)
asked for more threads than that GPU's pipeline allows, MLX raised only when
the lazy graph was evaluated, and nothing could fall back.

The guard logic is tested here against a fake MLX with a 448-thread cap (no
MLX, no GPU, no model). The real kernels are exercised by
scripts/engine_kernel_cap_check.py, which the last test runs when a pinned
oMLX and a Metal device are available.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "build-support" / "engine_guard"
CAP = 448


class FakeMLX:
    """`mlx.core` as far as the guard uses it: lazy kernels checked at eval."""

    def __init__(self, cap=CAP, eval_error=None):
        self.cap = cap
        self.eval_error = eval_error
        self.dispatched = []   # threads of every dispatch that reached "Metal"
        self.evals = 0
        self.fast = types.SimpleNamespace(metal_kernel=self._metal_kernel)

    def _metal_kernel(self, name, input_names=None, output_names=None, source="", **_):
        def kernel(*, inputs, template=None, grid, threadgroup, output_shapes, output_dtypes):
            threads = threadgroup[0] * threadgroup[1] * threadgroup[2]
            self.dispatched.append(threads)
            return [types.SimpleNamespace(threads=threads)]

        return kernel

    def eval(self, *arrays):
        self.evals += 1
        if self.eval_error is not None:
            raise self.eval_error
        for array in arrays:
            if array.threads > self.cap:
                raise ValueError(f"Thread group size ({array.threads}) is greater than  the maximum "
                                 f"allowed threads per threadgroup ({self.cap}).")


@pytest.fixture
def guard(monkeypatch):
    spec = importlib.util.spec_from_file_location("wisp_kernel_guard_under_test", OVERLAY / "kernel_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv(module.SIMULATED_CAP_ENV, raising=False)
    return module


@pytest.fixture
def mx(guard, monkeypatch):
    fake = FakeMLX()
    package = types.ModuleType("mlx")
    package.core = fake
    monkeypatch.setitem(sys.modules, "mlx", package)
    monkeypatch.setitem(sys.modules, "mlx.core", fake)
    assert guard.install(fake)
    return fake


def dispatch(kernel, threadgroup, template=(("T", "bf16"),)):
    return kernel(inputs=[], template=list(template), grid=(1, 1, 1), threadgroup=threadgroup,
                  output_shapes=[(1,)], output_dtypes=[None])


def test_refused_size_raises_at_the_call_with_the_real_cap(guard, mx):
    kernel = mx.fast.metal_kernel(name="verify_p1", input_names=[], output_names=[], source="")
    with pytest.raises(guard.ThreadgroupLimitError) as caught:
        dispatch(kernel, (32, 4, 4))                       # 512 threads, as on the M1 Pro
    assert (caught.value.kernel, caught.value.threads, caught.value.cap) == ("verify_p1", 512, CAP)
    assert isinstance(caught.value, ValueError)            # upstream handlers catch ValueError
    assert guard.limit_of(caught.value) == (512, CAP)
    assert kernel.cap([("T", "bf16")]) == CAP
    assert guard.failures() == [("verify_p1", 512, CAP)]


def test_a_known_cap_refuses_without_reaching_metal(guard, mx):
    kernel = mx.fast.metal_kernel(name="k", input_names=[], output_names=[], source="")
    with pytest.raises(guard.ThreadgroupLimitError):
        dispatch(kernel, (512, 1, 1))
    before = (list(mx.dispatched), mx.evals)
    with pytest.raises(guard.ThreadgroupLimitError):
        dispatch(kernel, (1024, 1, 1))
    assert (mx.dispatched, mx.evals) == before


def test_a_size_that_worked_is_not_evaluated_again(guard, mx):
    kernel = mx.fast.metal_kernel(name="k", input_names=[], output_names=[], source="")
    dispatch(kernel, (32, 4, 3))                           # 384: first use, evaluated once
    assert mx.evals == 1
    for _ in range(5):
        dispatch(kernel, (32, 4, 3))
        dispatch(kernel, (32, 4, 2))                       # smaller than a size seen to work
    assert mx.evals == 1
    assert max(mx.dispatched) <= CAP


def test_nothing_above_the_cap_is_trusted_without_running_it(guard, mx):
    kernel = mx.fast.metal_kernel(name="k", input_names=[], output_names=[], source="")
    dispatch(kernel, (256, 1, 1))
    with pytest.raises(guard.ThreadgroupLimitError):
        dispatch(kernel, (480, 1, 1))                      # never seen: checked, refused
    dispatch(kernel, (448, 1, 1))                          # exactly the cap: checked, works
    assert mx.evals == 3


def test_caps_are_kept_per_pipeline(guard, mx):
    kernel = mx.fast.metal_kernel(name="k", input_names=[], output_names=[], source="")
    with pytest.raises(guard.ThreadgroupLimitError):
        dispatch(kernel, (512, 1, 1), template=(("T", "bf16"),))
    mx.cap = 1024                                          # another template, another pipeline
    dispatch(kernel, (512, 1, 1), template=(("T", "fp16"),))
    with pytest.raises(guard.ThreadgroupLimitError):
        dispatch(kernel, (512, 1, 1), template=(("T", "bf16"),))


def test_simulated_cap_refuses_before_metal(guard, mx, monkeypatch):
    mx.cap = 1024
    monkeypatch.setenv(guard.SIMULATED_CAP_ENV, "448")
    kernel = mx.fast.metal_kernel(name="k", input_names=[], output_names=[], source="")
    with pytest.raises(guard.ThreadgroupLimitError) as caught:
        dispatch(kernel, (512, 1, 1))
    assert caught.value.cap == 448 and mx.dispatched == []


def test_other_errors_are_never_hidden(guard, monkeypatch):
    failure = RuntimeError("[METAL] Command buffer execution failed: Insufficient Memory")
    fake = FakeMLX(eval_error=failure)
    monkeypatch.setitem(sys.modules, "mlx", types.SimpleNamespace(core=fake))
    monkeypatch.setitem(sys.modules, "mlx.core", fake)
    guard.install(fake)
    kernel = fake.fast.metal_kernel(name="k", input_names=[], output_names=[], source="")
    with pytest.raises(RuntimeError) as caught:
        dispatch(kernel, (64, 1, 1))
    assert caught.value is failure


def test_a_traced_function_keeps_upstream_behaviour(guard, monkeypatch):
    fake = FakeMLX(eval_error=ValueError(
        "[eval] Attempting to eval an array during function transformations like compile or vmap is not allowed."))
    monkeypatch.setitem(sys.modules, "mlx", types.SimpleNamespace(core=fake))
    monkeypatch.setitem(sys.modules, "mlx.core", fake)
    guard.install(fake)
    kernel = fake.fast.metal_kernel(name="k", input_names=[], output_names=[], source="")
    assert dispatch(kernel, (64, 1, 1))[0].threads == 64
    assert dispatch(kernel, (64, 1, 1))[0].threads == 64
    assert fake.evals == 1


def test_install_is_idempotent(guard, mx):
    wrapped = mx.fast.metal_kernel
    assert guard.install(mx) and mx.fast.metal_kernel is wrapped


def test_guard_goes_in_when_mlx_loads_not_before(guard, monkeypatch, tmp_path):
    """The engine sets Metal environment variables before its first MLX import,
    so importing the package must not import MLX."""
    (tmp_path / "mlx").mkdir()
    (tmp_path / "mlx" / "__init__.py").write_text("")
    (tmp_path / "mlx" / "core.py").write_text(
        "import types\nfast = types.SimpleNamespace(metal_kernel=lambda *a, **k: (lambda **kw: []))\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("mlx", "mlx.core"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    before = list(sys.meta_path)
    guard.install_when_ready()
    try:
        assert "mlx.core" not in sys.modules
        guard.install_when_ready()                          # one hook, however often it is asked
        assert len(sys.meta_path) == len(before) + 1
        core = importlib.import_module("mlx.core")
        assert getattr(core.fast.metal_kernel, "_wisp_guarded", False)
        assert sys.meta_path == before
    finally:
        sys.meta_path[:] = before
        for name in ("mlx", "mlx.core"):
            sys.modules.pop(name, None)


# --- Lightning MTP ---------------------------------------------------------

def batch(model=None, uids=(1,)):
    return types.SimpleNamespace(model=model if model is not None else object(), uids=list(uids))


def test_threadgroup_error_in_mtp_falls_back_and_keeps_the_model_off_mtp(guard):
    first = batch()
    error = ValueError("Thread group size (512) is greater than  the maximum allowed threads per threadgroup (448)")
    assert not guard.mtp_blocked(first)
    assert guard.mtp_failure(first, error) is True
    assert guard.mtp_blocked(first)
    assert guard.mtp_blocked(batch(first.model))            # the next reply of the same model
    assert not guard.mtp_blocked(batch())                   # another model is unaffected


def test_other_mtp_errors_cost_the_reply_mtp_and_the_model_after_three(guard):
    model = object()
    for attempt in range(3):
        current = batch(model)
        assert guard.mtp_failure(current, RuntimeError("anything")) is True
        assert guard.mtp_blocked(current)
        assert guard.mtp_blocked(batch(model)) is (attempt == 2)


def test_a_failed_reply_does_not_keep_the_next_reply_off_mtp(guard):
    # The scheduler keeps one batch object for every reply: the mark on it is
    # for the requests it was serving when MTP failed.
    current = batch(uids=(7,))
    assert guard.mtp_failure(current, RuntimeError("anything")) is True
    assert guard.mtp_blocked(current)
    current.uids = [8]
    assert not guard.mtp_blocked(current)


@pytest.mark.parametrize("error", [
    RuntimeError("cluster rank could not roll back its MTP cache"),
    MemoryError("out of memory"),
    RuntimeError("[METAL] Command buffer execution failed: Insufficient Memory"),
    RuntimeError("kIOGPUCommandBufferCallbackErrorSubmissionsIgnored"),
])
def test_errors_a_fallback_would_hide_still_propagate(guard, error):
    current = batch()
    assert guard.mtp_failure(current, error) is False
    assert not guard.mtp_blocked(current)


def test_engine_memory_errors_propagate(guard, monkeypatch):
    exceptions = types.ModuleType("omlx.exceptions")

    class OMLXError(Exception):
        pass

    exceptions.OMLXError = OMLXError
    monkeypatch.setitem(sys.modules, "omlx", types.ModuleType("omlx"))
    monkeypatch.setitem(sys.modules, "omlx.exceptions", exceptions)
    assert guard.mtp_failure(batch(), OMLXError("prefill memory exceeded")) is False


# --- The shipped patches ---------------------------------------------------

def patch(name):
    return json.loads((OVERLAY / f"{name}.patch.json").read_text())


def test_overlay_ships_the_guard_and_patches_the_four_files():
    sys.path.insert(0, str(ROOT / "build-support"))
    import engine_guard_overlay as overlay

    assert overlay.ADDITIONS["omlx/wisp_kernel_guard.py"] == "kernel_guard.py"
    for target in ("omlx/__init__.py", "omlx/patches/gemma4_verify_kernel.py",
                   "omlx/patches/gemma4_verify_attention.py", "omlx/patches/mlx_lm_mtp/batch_generator.py"):
        assert target in overlay.TARGETS
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    assert {p["path"] for p in manifest["patches"]} == overlay.TARGETS
    assert {a["path"] for a in manifest["additions"]} == set(overlay.ADDITIONS)


def test_package_init_installs_the_guard_without_importing_mlx():
    edits = patch("__init__.py")["edits"]
    assert len(edits) == 1
    body = edits[0]["new"]
    assert "_wisp_kernel_guard.install_when_ready()" in body
    assert "import mlx" not in body
    assert "except Exception" in body                       # the guard can never stop the engine


def test_mtp_step_catches_every_error_and_rebuilds_the_cache():
    edits = patch("batch_generator.py")["edits"]
    handlers = [e for e in edits if "except _MtpStepFallback as exc:" in e["old"]]
    assert len(handlers) == 2                               # batched rows and the single reply
    for handler in handlers:
        assert "except Exception as exc:" in handler["new"]
        assert "except _MtpStepFallback" not in handler["new"]
        assert "_wisp_kernel_guard.mtp_failure(" in handler["new"] and "raise" in handler["new"]
    single = [h["new"] for h in handlers if "_omlx_mtp_state" in h["new"]][0]
    # A forward that failed before MTP finished activating may have advanced
    # the cache: the committed state is rebuilt anyway.
    assert single.index("if active is None and wisp_failure:") < single.index("SimpleNamespace(")
    for handler in handlers:
        # A failed rebuild re-raises the original error, which the scheduler's
        # own corruption recovery knows how to handle.
        body = handler["new"]
        assert body.index("if wisp_failure:") < body.index("raise exc") < body.index("raise RuntimeError(")
    gates = [e["new"] for e in edits if "_wisp_kernel_guard.mtp_blocked(gen_batch)" in e["new"]]
    assert len(gates) == 1


def test_gemma_kernel_retries_smaller_and_folds_pass_two_in_ops():
    edits = patch("gemma4_verify_kernel.py")["edits"]
    body = "".join(e["new"] for e in edits)
    assert "rows_cap = _rows_after_refusal(exc, gqa, queries.dtype, rows_cap)" in body
    retry = body[body.index("def fused_verify_sdpa("):body.index("def is_available(")]
    assert retry.index("except ValueError as exc:") < retry.index("continue") < retry.index("outs.append(out)")
    assert "return _combine_blocks(partials, sums, maxs, q_rows.dtype)" in body
    assert "limit = _guard.limit_of(exc)" in body           # the cap comes from Metal, not a guess


def test_gemma_attention_takes_stock_attention_when_the_kernel_fails():
    edits = patch("gemma4_verify_attention.py")["edits"]
    assert len(edits) == 1
    body = edits[0]["new"]
    assert body.index("try:") < body.index("fused_verify_sdpa(") < body.index("except Exception as exc:")
    assert "sdpa(\n                    queries, big_k, big_v, cache=cache, scale=self.scale, mask=mask" in body


def test_real_kernels_on_this_gpu_with_a_448_thread_cap(tmp_path):
    """The real Gemma 4 verify kernels and the real MTP patch, on this GPU, as
    if every pipeline allowed 448 threads. Needs the pinned oMLX and Metal."""
    source = Path(os.environ.get("WISP_ENGINE_SOURCE", "/Applications/oMLX.app/Contents/Resources"))
    if not (source / "omlx").is_dir():
        pytest.skip(f"no upstream oMLX source at {source}")
    manifest = json.loads((OVERLAY / "manifest.json").read_text())
    sys.path.insert(0, str(ROOT / "build-support"))
    import engine_guard_overlay as overlay

    for row in manifest["patches"]:
        target = source / row["path"]
        if not target.is_file() or overlay._hash(target.read_bytes()) != row["input_sha256"]:
            pytest.skip(f"{source} is not the pinned oMLX ({row['path']} differs)")
    report = tmp_path / "report.json"
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "engine_kernel_cap_check.py"), "--from-upstream", str(source),
             "--simulate-cap", str(CAP), "--json", str(report)], capture_output=True, text=True, timeout=600)
    except OSError as exc:
        pytest.skip(f"this sandbox may not start a child Python: {exc}")
    if result.returncode == 3:
        pytest.skip("no Metal device in this process (sandbox or headless host)")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    checks = json.loads(report.read_text())["checks"]
    assert len(checks) == 5 and all(check["ok"] for check in checks)
