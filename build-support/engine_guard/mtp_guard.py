"""Wisp's safety net around the engine's multi-token (MTP) decode step.

Installed into the bundled engine as ``omlx/wisp_mtp_guard.py`` and called
from ``omlx/patches/mlx_lm_mtp/batch_generator.py`` (see the overlay patch).

Why: MTP drafts a few tokens and verifies them in one multi-row forward. That
forward runs custom Metal kernels, and MLX checks a kernel's threadgroup size
against the compiled pipeline's ``maxTotalThreadsPerThreadgroup`` only when
the lazy graph is evaluated. The limit depends on the GPU and on the kernel,
so a dispatch that is fine on one Mac raises on another ("Thread group size
(512) is greater than the maximum allowed threads per threadgroup (448)" on
an M1 Pro), long after the code that chose the route has returned. Upstream
lets that error end the reply.

What this does:

- ``run()`` wraps one MTP step. An unexpected error becomes the engine's own
  step-fallback signal, so the existing handler rebuilds the cache from the
  tokens already streamed and the request carries on with plain decoding.
- After the first such error MTP is not started again in this engine process
  (``disabled()``): a retry would fail the same way and pay a full re-prefill
  each time. A request that already holds MTP state is left to finish or to
  fall back by itself.
- ``install()`` records, per custom kernel, the largest threadgroup it was
  asked to run. When a step fails on a threadgroup limit, the log names the
  kernels that asked for that many threads.

The engine's own deliberate errors (``OMLXError``: memory limits, aborts) and
``MemoryError`` pass through untouched.
"""
from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger("omlx.wisp_mtp_guard")

_LOCK = threading.Lock()
_failure: str | None = None
_installed = False
# kernel name -> (threads, threadgroup) of its largest dispatch so far.
_largest: dict[str, tuple[int, tuple]] = {}
_MAX_KERNELS = 4096
_LIMIT = re.compile(r"Thread group size \((\d+)\).*?threadgroup \((\d+)\)", re.S)


def disabled() -> bool:
    """True once an MTP step has failed unexpectedly in this process."""
    return _failure is not None


def failure() -> str | None:
    return _failure


def _passes_through(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError):
        return True
    return any(cls.__name__ == "OMLXError" for cls in type(exc).__mro__)


def kernels_asking_for(threads: int) -> list[str]:
    with _LOCK:
        rows = [(name, group) for name, (total, group) in _largest.items() if total == threads]
    return [f"{name} {group}" for name, group in sorted(rows)]


def describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    match = _LIMIT.search(str(exc))
    if match:
        asked, allowed = int(match.group(1)), int(match.group(2))
        names = kernels_asking_for(asked)
        text += (f" [threadgroup of {asked} threads, this GPU allows {allowed} for that kernel; "
                 f"asked by: {', '.join(names) if names else 'no recorded kernel'}]")
    return text


def note_failure(exc: BaseException) -> bool:
    """Turn MTP off for this process. Returns True the first time only."""
    global _failure
    summary = describe(exc)
    with _LOCK:
        first = _failure is None
        if first:
            _failure = summary
    if first:
        logger.warning(
            "MTP step failed; this request continues with plain decoding and MTP "
            "stays off until the engine restarts. %s", summary)
    else:
        logger.debug("MTP step failed again after MTP was turned off: %s", summary)
    return first


def run(step, fallback, *args, **kwargs):
    """Run one MTP step; an unexpected error is raised as ``fallback``."""
    try:
        return step(*args, **kwargs)
    except fallback:
        raise
    except Exception as exc:
        if _passes_through(exc):
            raise
        note_failure(exc)
        raise fallback(f"MTP step failed ({type(exc).__name__})") from exc


class _Recorded:
    """A custom kernel that remembers the largest threadgroup it was given."""

    __slots__ = ("_kernel", "_name")

    def __init__(self, kernel, name):
        self._kernel = kernel
        self._name = name

    def __call__(self, *args, **kwargs):
        try:
            group = kwargs.get("threadgroup")
            if group is not None:
                total = int(group[0]) * int(group[1]) * int(group[2])
                seen = _largest.get(self._name)
                if seen is None or total > seen[0]:
                    with _LOCK:
                        if seen is not None or len(_largest) < _MAX_KERNELS:
                            _largest[self._name] = (total, tuple(int(v) for v in group))
        except Exception:
            pass
        return self._kernel(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._kernel, item)


def install(fast=None) -> bool:
    """Record threadgroup sizes of kernels made through ``mx.fast.metal_kernel``.

    Idempotent. Never raises: without MLX (or if the hook cannot be set) the
    guard still works, only the kernel names are missing from the log.
    """
    global _installed
    if _installed and fast is None:
        return True
    try:
        if fast is None:
            import mlx.core as mx

            fast = mx.fast
        original = fast.metal_kernel
        if getattr(original, "_wisp_recorded", False):
            _installed = True
            return True

        def metal_kernel(*args, **kwargs):
            kernel = original(*args, **kwargs)
            try:
                name = kwargs.get("name", args[0] if args else "?")
                return _Recorded(kernel, str(name))
            except Exception:
                return kernel

        metal_kernel._wisp_recorded = True
        metal_kernel._wisp_original = original
        fast.metal_kernel = metal_kernel
        _installed = True
        return True
    except Exception:
        logger.debug("kernel threadgroup recording not installed", exc_info=True)
        return False


def _reset_for_tests() -> None:
    global _failure, _installed
    with _LOCK:
        _failure = None
        _largest.clear()
    _installed = False
