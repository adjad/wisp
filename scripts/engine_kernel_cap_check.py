#!/usr/bin/env python3
"""Check the bundled engine's Metal threadgroup handling on this Mac's GPU.

Run with the engine's own Python against an engine tree that carries the Wisp
overlay (a built Wisp.app's Contents/Resources/engine):

    Wisp.app/Contents/Resources/engine/py/bin/python3 \\
        scripts/engine_kernel_cap_check.py \\
        --engine-root Wisp.app/Contents/Resources/engine

It loads no model and talks to no server. It needs a Metal device, so it
cannot run inside a sandbox that hides the GPU.

What it does:

1. Reports the GPU and the real threadgroup limit of the Gemma 4 verify
   kernel's two pipelines, as Metal states it.
2. Runs the fused verify attention against a plain reference at this GPU's
   own limits.
3. Repeats that as if every pipeline allowed only `--simulate-cap` threads
   (default 448, the limit an M1 Pro reported): no dispatch may exceed the
   cap, a refused size must shrink and retry, an impossible geometry must
   take the stock attention, and a failing MTP step must fall back to the
   standard step.

Exit status 0 when every check passes, 1 when one fails, 3 when this process
has no Metal device (a sandbox or a headless host). `--report-only` stops
after step 1.

Without a built app, `--from-upstream <oMLX.app/Contents/Resources>` copies
that pinned oMLX's `omlx` package to a temporary directory, applies the Wisp
overlay from build-support/engine_guard, and runs the checks with that app's
Python. `--fetch` does the same with the release pinned in
build-support/engine-source.json, downloading and verifying it first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _paths(engine_root: Path, site_packages: Path | None) -> None:
    if site_packages is None:
        site_packages = engine_root / "sp"
    for path in (site_packages, engine_root):
        sys.path.insert(0, str(path))


class Checks:
    def __init__(self):
        self.results = []

    def run(self, name, fn):
        try:
            detail = fn()
            self.results.append({"check": name, "ok": True, "detail": detail})
            print(f"PASS  {name}" + (f"  ({detail})" if detail else ""))
        except Exception as exc:  # noqa: BLE001 - every failure is reported
            import traceback

            traceback.print_exc()
            self.results.append({"check": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")

    @property
    def failed(self):
        return [r for r in self.results if not r["ok"]]


def reference(mx, q, k, v, n_keys, scale):
    """Row j attends keys [0 .. n_keys - L + j]; fp32 throughout."""
    hq, L = q.shape[1], q.shape[2]
    gqa = hq // k.shape[1]
    kk = mx.repeat(k[:, :, :n_keys].astype(mx.float32), gqa, axis=1)
    vv = mx.repeat(v[:, :, :n_keys].astype(mx.float32), gqa, axis=1)
    scores = (q.astype(mx.float32) * scale) @ kk.swapaxes(-1, -2)
    keys = mx.arange(n_keys)[None, :]
    horizon = (n_keys - L + mx.arange(L))[:, None]
    scores = mx.where(keys <= horizon, scores, -mx.inf)
    return mx.softmax(scores, axis=-1) @ vv


def inputs(mx, hq, hkv, L, n_keys, dtype, cap=None, seed=0):
    mx.random.seed(seed)
    cap = cap or n_keys + 5
    q = (mx.random.normal((1, hq, L, 512)) * 0.2).astype(dtype)
    k = (mx.random.normal((1, hkv, cap, 512)) * 0.2).astype(dtype)
    v = mx.random.normal((1, hkv, cap, 512)).astype(dtype)
    mx.eval(q, k, v)
    return q, k, v


def close(mx, got, want, tol=3e-2):
    err = float(mx.max(mx.abs(got.astype(mx.float32) - want)).item())
    if not err <= tol:
        raise AssertionError(f"max |difference| {err:.4f} exceeds {tol}")
    return err


def fresh_kernel(mx):
    """The Gemma 4 verify kernel module with every cache cleared."""
    from omlx import wisp_kernel_guard as guard
    from omlx.patches import gemma4_verify_kernel as kernel

    kernel._kernels.clear()
    kernel._tg_thread_cap.clear()
    kernel._pass2_in_ops.clear()
    kernel._availability = None
    kernel._fallback_logged = False
    if not guard.install():
        raise RuntimeError("the kernel guard did not install")
    return guard, kernel


def record(kernel_pair):
    """Count the threads of every dispatch that reaches the real kernels."""
    seen = {"p1": [], "p2": []}
    for label, guarded in zip(("p1", "p2"), kernel_pair):
        inner = guarded._kernel

        def call(*args, _inner=inner, _label=label, **kwargs):
            total = 1
            for part in kwargs["threadgroup"]:
                total *= part
            seen[_label].append(total)
            return _inner(*args, **kwargs)

        guarded._kernel = call
    return seen


def report_native(mx, out):
    """The real limits of both pipelines on this GPU."""
    guard, kernel = fresh_kernel(mx)
    info = dict(mx.device_info())
    out["gpu"] = {k: info.get(k) for k in ("device_name", "architecture")}
    out["mlx"] = mx.__version__
    print(f"GPU: {out['gpu']['device_name']} ({out['gpu']['architecture']}), MLX {mx.__version__}")
    limits = {}
    for dtype in (mx.float16, mx.bfloat16):
        p1, p2 = kernel._get_kernels(512)
        row = {}
        for label, call in (
            # 1024 threads: more than any pipeline is known to refuse below.
            ("pass1", lambda: kernel._dispatch(*inputs(mx, 8, 1, 8, 8, dtype, cap=8)[:3], 8)),
        ):
            try:
                mx.eval(call())
                row[label] = "1024 or more"
            except ValueError as exc:
                limit = guard.limit_of(exc)
                if limit is None:
                    raise
                row[label] = limit[1]
        row["pass2"] = p2.cap([("T", dtype)]) or ("folded in ops" if kernel._pass2_in_ops.get(dtype) else "1024 or more")
        row["pass1"] = p1.cap([("T", dtype), ("R", kernel._RPS)]) or row["pass1"]
        limits[str(dtype)] = row
        print(f"  {dtype}: pass 1 limit {row['pass1']}, pass 2 limit {row['pass2']}")
        kernel._pass2_in_ops.clear()
    out["gemma4_verify_kernel_limits"] = limits
    out["refused"] = [list(item) for item in guard.failures()]


def report_unguarded(mx, json_path):
    """GPU and Gemma 4 verify-kernel limits on an engine without the guard."""
    import re

    from omlx.patches import gemma4_verify_kernel as kernel

    info = dict(mx.device_info())
    out = {"gpu": {k: info.get(k) for k in ("device_name", "architecture")}, "mlx": mx.__version__,
           "guard": "absent", "gemma4_verify_kernel": {}}
    print(f"GPU: {out['gpu']['device_name']} ({out['gpu']['architecture']}), MLX {mx.__version__}")
    print("This engine has no Wisp kernel guard (Wisp 0.5.0 or earlier).")
    for dtype in (mx.float16, mx.bfloat16):
        row = {}
        # Pass 1 at 1024, 512 and 256 threads (gqa 8 x 4, 2, 1 row groups); pass 2 is always 1024.
        for groups in (4, 2, 1):
            q, k, v = inputs(mx, 8, 1, 2 * groups, 8, dtype, cap=8)
            try:
                mx.eval(kernel._dispatch(q, k, v, 8))
                row[str(256 * groups)] = "ran"
            except ValueError as exc:
                match = re.search(r"Thread group size \((\d+)\).*?threads per threadgroup \((\d+)\)", str(exc), re.S)
                row[str(256 * groups)] = f"refused {match.group(1)} threads, limit {match.group(2)}" if match else str(exc)
        out["gemma4_verify_kernel"][str(dtype)] = row
        print(f"  {dtype}: " + "; ".join(f"pass 1 at {size}: {result}" for size, result in row.items()))
    try:
        out["cap_probe"] = {str(d): kernel._probe_tg_cap(d) for d in (mx.float16, mx.bfloat16)}
        print(f"  the engine's own probe reports: {out['cap_probe']}")
    except Exception as exc:  # noqa: BLE001
        out["cap_probe"] = f"{type(exc).__name__}: {exc}"
    if json_path:
        json_path.write_text(json.dumps(out, indent=2, default=str) + "\n")


def check_parity(mx):
    _, kernel = fresh_kernel(mx)
    worst = 0.0
    for dtype in (mx.float16, mx.bfloat16):
        for hq, hkv in ((8, 2), (32, 4), (16, 8)):
            for L in (2, 3, 4, 7, 8, 12):
                q, k, v = inputs(mx, hq, hkv, L, 37 + L, dtype)
                got = kernel.fused_verify_sdpa(q, k, v, 37 + L, 0.5)
                worst = max(worst, close(mx, got, reference(mx, q, k, v, 37 + L, 0.5)))
    return f"worst difference {worst:.4f}"


def check_capped(mx, cap):
    """Both passes under the cap, with results unchanged."""
    _, kernel = fresh_kernel(mx)
    seen = record(kernel._get_kernels(512))
    worst = 0.0
    for dtype in (mx.float16, mx.bfloat16):
        for hq, hkv in ((8, 2), (32, 4)):
            for L in (2, 4, 8, 12):
                q, k, v = inputs(mx, hq, hkv, L, 41 + L, dtype)
                got = kernel.fused_verify_sdpa(q, k, v, 41 + L, 1.0)
                worst = max(worst, close(mx, got, reference(mx, q, k, v, 41 + L, 1.0)))
    over = [t for t in seen["p1"] + seen["p2"] if t > cap]
    if over:
        raise AssertionError(f"dispatches above the cap reached Metal: {over}")
    if not seen["p1"]:
        raise AssertionError("pass 1 never ran")
    if 32 * kernel._BLOCKS > cap and seen["p2"]:
        raise AssertionError("pass 2 ran although its threadgroup exceeds the cap")
    return f"pass 1 sizes {sorted(set(seen['p1']))}, pass 2 kernel runs {len(seen['p2'])}, worst difference {worst:.4f}"


def check_retry(mx, cap):
    """A probe that guessed too high is corrected by the real dispatch."""
    guard, kernel = fresh_kernel(mx)
    seen = record(kernel._get_kernels(512))
    dtype = mx.bfloat16
    kernel._tg_thread_cap[dtype] = 1024  # what a passing probe would have left
    q, k, v = inputs(mx, 8, 2, 8, 64, dtype)  # gqa 4: 8 rows ask for 512 threads
    got = kernel.fused_verify_sdpa(q, k, v, 64, 1.0)
    close(mx, got, reference(mx, q, k, v, 64, 1.0))
    if kernel._tg_thread_cap[dtype] != cap:
        raise AssertionError(f"budget is {kernel._tg_thread_cap[dtype]}, expected the reported cap {cap}")
    if any(t > cap for t in seen["p1"]):
        raise AssertionError(f"a refused size reached Metal: {seen['p1']}")
    if not any(name.startswith("omlx_g4_verify_sdpa_p1") and threads == 512 for name, threads, _ in guard.failures()):
        raise AssertionError("the 512-thread dispatch was not refused")
    return f"512 threads refused, retried at {sorted(set(seen['p1']))}"


def check_stock_fallback(mx, cap):
    """A geometry that cannot fit one row group takes the stock attention."""
    import mlx.nn as nn
    from mlx_lm.models.cache import KVCache
    from mlx_vlm.models.gemma4 import language as g4
    from mlx_vlm.models.gemma4.config import TextConfig

    _, kernel = fresh_kernel(mx)
    stock_call = g4.Attention.__call__
    from omlx.patches import gemma4_verify_attention as attention

    if not attention.apply():
        raise RuntimeError("the verify attention patch did not apply")
    # 32 query heads over 1 kv head: one row group needs 1024 threads.
    config = TextConfig(
        hidden_size=64, num_hidden_layers=1, num_attention_heads=32, num_key_value_heads=1,
        head_dim=256, global_head_dim=512, num_kv_shared_layers=0, layer_types=["full_attention"],
    )
    layer = g4.Attention(config, 0)
    layer.set_dtype(mx.bfloat16)
    mx.eval(layer.parameters())
    if 32 * 32 <= cap:
        raise AssertionError("the simulated cap is too high for this check")
    if not kernel.is_available():
        raise AssertionError("the kernel is not available under the cap")
    # What a probe that passed at another geometry would have left behind.
    kernel._tg_thread_cap[mx.bfloat16] = 1024
    mx.random.seed(3)
    prompt = mx.random.normal((1, 9, 64)).astype(mx.bfloat16)
    block = mx.random.normal((1, 4, 64)).astype(mx.bfloat16)

    def run(call):
        cache = KVCache()
        call(layer, prompt[:, :1], None, cache)
        for i in range(1, 9):
            call(layer, prompt[:, i:i + 1], None, cache)
        mask = nn.MultiHeadAttention.create_additive_causal_mask(4 + 9).astype(mx.bfloat16)[-4:]
        out, _, _ = call(layer, block, mask, cache)
        mx.eval(out)
        return out, cache.offset

    want, want_offset = run(stock_call)
    got, got_offset = run(g4.Attention.__call__)
    if got_offset != want_offset:
        raise AssertionError(f"cache offset {got_offset} differs from stock {want_offset}")
    err = close(mx, got, want.astype(mx.float32), tol=1e-3)
    if not kernel._fallback_logged:
        raise AssertionError("the fused route did not fall back")
    return f"stock attention used, difference from stock {err:.5f}"


def check_mtp_fallback(mx):
    """A verify-path error must not fail the request."""
    from collections import deque
    from types import SimpleNamespace

    import importlib

    from omlx import wisp_kernel_guard as guard

    lm_generate = importlib.import_module("mlx_lm.generate")

    calls = []
    lm_generate.GenerationBatch.next = lambda self, *a, **k: calls.append("standard") or "standard-step"
    from omlx.patches.mlx_lm_mtp import batch_generator as bg

    if not bg.apply():
        raise RuntimeError("the MTP patch did not apply")
    patched_next = lm_generate.GenerationBatch.next
    state = SimpleNamespace(queue=deque(), uid=7, reentry_probe=False, stats=None)
    originals = {name: getattr(bg, name) for name in (
        "_is_mtp_batch_eligible", "_mtp_common_eligible", "_allows_new_mtp_activation",
        "_singleton_mtp_handoff_ready", "_prepare_mtp_state_for_next", "_mtp_next",
        "_reconcile_mtp_to_standard", "_spec_command_buffers", "_mtp_park_state_for_batch")}
    common = originals["_mtp_common_eligible"]
    import contextlib

    def scenario(error, *, activated):
        batch = lm_generate.GenerationBatch.__new__(lm_generate.GenerationBatch)
        batch.model = SimpleNamespace()
        batch.uids = [7]
        if activated:
            batch._omlx_mtp_state = state
        rebuilt = []
        bg._is_mtp_batch_eligible = lambda b: False
        # The real check, minus the parts that need a loaded model.
        bg._mtp_common_eligible = lambda b: not guard.mtp_blocked(b)
        bg._allows_new_mtp_activation = lambda b, attr: True
        bg._singleton_mtp_handoff_ready = lambda b: False
        bg._mtp_park_state_for_batch = lambda b: None
        bg._spec_command_buffers = lambda model=None: contextlib.nullcontext()
        if activated:
            bg._prepare_mtp_state_for_next = lambda b: state
            bg._mtp_next = lambda b, s: (_ for _ in ()).throw(error)
        else:
            bg._prepare_mtp_state_for_next = lambda b: (_ for _ in ()).throw(error)
        bg._reconcile_mtp_to_standard = lambda b, s: rebuilt.append(s) or True
        return batch, rebuilt

    try:
        limit = ValueError("Thread group size (512) is greater than  the maximum allowed threads per threadgroup (448)")
        batch, rebuilt = scenario(limit, activated=True)
        calls.clear()
        if patched_next(batch) != "standard-step" or calls != ["standard"]:
            raise AssertionError("a threadgroup error in the verify cycle did not fall back to the standard step")
        if rebuilt != [state] or hasattr(batch, "_omlx_mtp_state"):
            raise AssertionError("the committed cache was not rebuilt, or MTP state was kept")
        if not guard.mtp_blocked(batch):
            raise AssertionError("MTP was not turned off after a threadgroup error")
        again = lm_generate.GenerationBatch.__new__(lm_generate.GenerationBatch)
        again.model, again.uids = batch.model, [8]
        bg._prepare_mtp_state_for_next = lambda b: (_ for _ in ()).throw(AssertionError("MTP re-entered"))
        if patched_next(again) != "standard-step":
            raise AssertionError("the next request of the same model did not decode normally")

        batch, rebuilt = scenario(RuntimeError("anything else"), activated=False)
        if patched_next(batch) != "standard-step" or len(rebuilt) != 1 or rebuilt[0].queue:
            raise AssertionError("a failure during MTP activation did not rebuild and fall back")
        if not guard.mtp_blocked(batch) or id(batch.model) in guard._mtp_off_models:
            raise AssertionError("one unrelated failure should stop MTP for the reply only")

        fatal = RuntimeError("[METAL] Command buffer execution failed: Insufficient Memory")
        batch, rebuilt = scenario(fatal, activated=True)
        try:
            patched_next(batch)
        except RuntimeError as exc:
            if exc is not fatal:
                raise
        else:
            raise AssertionError("a failed command buffer was swallowed")
        if rebuilt:
            raise AssertionError("a failed command buffer must not be reconciled")
    finally:
        for name, value in originals.items():
            setattr(bg, name, value)
    del common
    return "threadgroup error, activation error and command-buffer error handled"


NO_METAL = 3
BUILD_SUPPORT = Path(__file__).resolve().parents[1] / "build-support"


def run_from_upstream(resources: Path, passthrough: list[str]) -> int:
    """Overlay a copy of a pinned oMLX's `omlx` package and check it with its Python."""
    import shutil
    import subprocess
    import tempfile

    sys.path.insert(0, str(BUILD_SUPPORT))
    import engine_guard_overlay as overlay

    python = resources / "Python" / "cpython-3.11" / "bin" / "python3.11"
    site = resources / "Python" / "framework-mlx-base" / "lib" / "python3.11" / "site-packages"
    if not (python.is_file() and site.is_dir() and (resources / "omlx").is_dir()):
        print(f"{resources} is not an oMLX.app Resources directory", file=sys.stderr)
        return 2
    manifest = json.loads((BUILD_SUPPORT / "engine_guard" / "manifest.json").read_text())
    with tempfile.TemporaryDirectory(prefix="wisp-engine-check-") as temp:
        engine = Path(temp) / "engine"
        shutil.copytree(resources / "omlx", engine / "omlx", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        # Each patched file's input hash is checked; a different oMLX is refused.
        overlay.apply(engine, manifest["upstream_tree_sha256"])
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [str(python), "-B", str(Path(__file__).resolve()), "--engine-root", str(engine),
                   "--site-packages", str(site), *passthrough]
        try:
            return subprocess.run(command, env=env).returncode
        except OSError as exc:
            print(f"SKIP  this process may not run the engine's Python: {exc}")
            return NO_METAL


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--engine-root", type=Path, help="directory holding the overlaid omlx package")
    where.add_argument("--from-upstream", type=Path, metavar="RESOURCES",
                       help="a pinned oMLX.app/Contents/Resources to overlay and check")
    where.add_argument("--fetch", action="store_true", help="download the pinned oMLX release and check it")
    parser.add_argument("--cache", type=Path, default=Path(".wisp-build/engine-cache"),
                        help="where --fetch keeps the verified download")
    parser.add_argument("--site-packages", type=Path, help="MLX site-packages (default: <engine-root>/sp)")
    parser.add_argument("--simulate-cap", type=int, default=448)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--json", type=Path, help="write the report here")
    args = parser.parse_args()
    if args.engine_root is None:
        passthrough = ["--simulate-cap", str(args.simulate_cap)]
        if args.report_only:
            passthrough.append("--report-only")
        if args.json:
            passthrough += ["--json", str(args.json.resolve())]
        if args.from_upstream is not None:
            return run_from_upstream(args.from_upstream.resolve(), passthrough)
        sys.path.insert(0, str(BUILD_SUPPORT))
        import engine as engine_build

        with engine_build.fetched_app(args.cache.resolve()) as app:
            engine_build.verify_source_identity(app)
            return run_from_upstream(app / "Contents" / "Resources", passthrough)
    _paths(args.engine_root.resolve(), args.site_packages.resolve() if args.site_packages else None)
    os.environ.pop("WISP_ENGINE_SIMULATED_THREAD_CAP", None)

    import omlx  # noqa: F401 - installs the guard hook before MLX loads
    try:
        import mlx.core as mx
    except ImportError as exc:
        if "No Metal device" in str(exc):
            print(f"SKIP  no Metal device in this process: {exc}")
            if args.json:
                args.json.write_text(json.dumps({"skipped": "no Metal device"}) + "\n")
            return NO_METAL
        raise
    try:
        from omlx import wisp_kernel_guard as guard
    except ImportError:
        # An engine without the Wisp guard (0.5.0 and earlier): the real limits
        # can still be read from MLX's own error, which is all a report needs.
        report_unguarded(mx, args.json)
        if args.report_only:
            return 0
        print("FAIL  this engine has no Wisp kernel guard; only --report-only works on it")
        return 1

    report: dict = {}
    checks = Checks()
    if not getattr(mx.fast.metal_kernel, "_wisp_guarded", False):
        print("FAIL  importing omlx did not guard mx.fast.metal_kernel")
        return 1
    report_native(mx, report)
    if not args.report_only:
        checks.run("fused verify attention matches the reference at this GPU's limits", lambda: check_parity(mx))
        os.environ[guard.SIMULATED_CAP_ENV] = str(args.simulate_cap)
        cap = args.simulate_cap
        checks.run(f"both passes stay within a {cap}-thread cap", lambda: check_capped(mx, cap))
        checks.run("a refused dispatch shrinks its row groups and retries", lambda: check_retry(mx, cap))
        checks.run("an impossible geometry takes the stock attention", lambda: check_stock_fallback(mx, cap))
        os.environ.pop(guard.SIMULATED_CAP_ENV, None)
        checks.run("an MTP verify failure falls back to the standard step", lambda: check_mtp_fallback(mx))
    report["checks"] = checks.results
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
