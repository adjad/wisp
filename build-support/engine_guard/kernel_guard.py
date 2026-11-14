# SPDX-License-Identifier: Apache-2.0
"""Wisp: a custom Metal kernel must never end a reply over a thread limit.

Metal caps the threads of one threadgroup per compiled pipeline
(``maxTotalThreadsPerThreadgroup``). The cap depends on the GPU and on the
kernel's register use: the same kernel that admits 1024 threads on an M5
admits 448 on an M1 Pro. MLX does not expose the cap to Python, and it checks
it only when the lazy graph is evaluated, so an oversized dispatch surfaces
far from the call that made it, as

    ValueError: Thread group size (512) is greater than  the maximum allowed
    threads per threadgroup (448)

and by then no caller can fall back.

``install()`` wraps ``mx.fast.metal_kernel``. Every kernel it returns checks
its own dispatches:

- The first dispatch of a pipeline (kernel + template) at a new threadgroup
  size is evaluated on the spot. If Metal refuses it, the error is raised at
  the call, where callers can fall back, and the real cap from MLX's message
  is remembered for that pipeline.
- Later dispatches cost one dictionary lookup: sizes already seen to work pass
  straight through, sizes above a learned cap raise at once.

Nothing is guessed: a size is trusted only after the GPU ran it, and a cap is
only ever the number Metal reported.

The second half is for Lightning MTP (``mlx_lm_mtp.batch_generator``): any
error inside a verify cycle is reported here, and the reply continues with
plain decoding.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading

logger = logging.getLogger(__name__)

_LIMIT_RE = re.compile(
    r"Thread group size \((\d+)\).*?threads per threadgroup \((\d+)\)", re.S
)
# mx.eval refuses to run while a function is being traced (mx.compile, vmap);
# the dispatch cannot be checked there and keeps upstream behaviour.
_TRACING_MARKERS = ("function transformation", "during compile", "while tracing")
_UNCHECKED = 1 << 30
# Tests and the hardware check set this to make every pipeline behave like a
# GPU with this cap. Never set in a release.
SIMULATED_CAP_ENV = "WISP_ENGINE_SIMULATED_THREAD_CAP"

_lock = threading.Lock()
_logged: set = set()
_failures: list = []  # (kernel name, threads asked, cap), for diagnostics


class ThreadgroupLimitError(ValueError):
    """A dispatch asks for more threads per threadgroup than its pipeline allows.

    Carries MLX's own wording, so upstream handlers that match the message
    keep working.
    """

    def __init__(self, kernel: str, threads: int, cap: int):
        super().__init__(
            f"Thread group size ({threads}) is greater than  the maximum allowed "
            f"threads per threadgroup ({cap})"
        )
        self.kernel = kernel
        self.threads = threads
        self.cap = cap


def limit_of(exc: BaseException):
    """``(threads asked, pipeline cap)`` when ``exc`` is a threadgroup-limit
    error, else None."""
    if isinstance(exc, ThreadgroupLimitError):
        return exc.threads, exc.cap
    match = _LIMIT_RE.search(str(exc))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def is_limit_error(exc: BaseException) -> bool:
    return limit_of(exc) is not None


def _simulated_cap():
    raw = os.environ.get(SIMULATED_CAP_ENV)
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError:
        return None
    return cap if cap > 0 else None


def _threads(threadgroup) -> int:
    total = 1
    for part in threadgroup:
        total *= int(part)
    return total


def _key(template):
    if not template:
        return ()
    try:
        key = tuple(tuple(item) for item in template)
        hash(key)
        return key
    except TypeError:
        return repr(template)


def failures() -> list:
    """Dispatches this process saw refused: ``(kernel, threads, cap)``."""
    with _lock:
        return list(_failures)


def _note_refused(kernel: str, threads: int, cap: int) -> None:
    mark = (kernel, threads, cap)
    with _lock:
        if mark in _logged:
            return
        _logged.add(mark)
        _failures.append(mark)
    logger.warning(
        "Wisp kernel guard: %s cannot run %d threads per threadgroup on this "
        "GPU (its pipeline allows %d); the caller falls back",
        kernel,
        threads,
        cap,
    )


class GuardedKernel:
    """A ``mx.fast.metal_kernel`` callable that checks its threadgroup sizes."""

    __slots__ = ("_kernel", "name", "_state")

    def __init__(self, kernel, name: str):
        self._kernel = kernel
        self.name = name
        # pipeline key -> [largest size seen to work, learned cap or 0]
        self._state: dict = {}

    def cap(self, template=None) -> int:
        """The pipeline's cap as Metal reported it, 0 while unknown."""
        state = self._state.get(_key(template))
        return state[1] if state else 0

    def __call__(self, *args, **kwargs):
        threadgroup = kwargs.get("threadgroup")
        if threadgroup is None and len(args) > 3:
            threadgroup = args[3]
        if threadgroup is None:
            return self._kernel(*args, **kwargs)
        template = kwargs.get("template")
        if template is None and len(args) > 1:
            template = args[1]
        threads = _threads(threadgroup)
        key = _key(template)
        state = self._state.get(key)
        if state is not None:
            if threads <= state[0]:
                return self._kernel(*args, **kwargs)
            if state[1] and threads > state[1]:
                raise ThreadgroupLimitError(self.name, threads, state[1])
        return self._first_use(key, threads, args, kwargs)

    def _learn(self, key, *, works: int = 0, cap: int = 0) -> None:
        with _lock:
            state = self._state.setdefault(key, [0, 0])
            if works > state[0]:
                state[0] = works
            if cap and (not state[1] or cap < state[1]):
                state[1] = cap

    def _first_use(self, key, threads: int, args, kwargs):
        simulated = _simulated_cap()
        if simulated is not None and threads > simulated:
            self._learn(key, cap=simulated)
            _note_refused(self.name, threads, simulated)
            raise ThreadgroupLimitError(self.name, threads, simulated)
        outputs = self._kernel(*args, **kwargs)
        try:
            import mlx.core as mx

            mx.eval(*outputs)
        except Exception as exc:
            limit = limit_of(exc)
            if limit is not None:
                self._learn(key, cap=limit[1])
                _note_refused(self.name, threads, limit[1])
                raise ThreadgroupLimitError(self.name, threads, limit[1]) from exc
            text = str(exc).lower()
            if any(marker in text for marker in _TRACING_MARKERS):
                self._learn(key, works=_UNCHECKED)
                return outputs
            # Not ours to judge (a failed command buffer, memory): never hide it.
            raise
        self._learn(key, works=threads)
        return outputs


def install(core=None) -> bool:
    """Wrap ``mx.fast.metal_kernel``. Idempotent; False when MLX is unusable
    (no Metal device), in which case nothing is changed."""
    try:
        if core is None:
            import mlx.core as core

        fast = core.fast
        original = fast.metal_kernel
    except Exception:
        return False
    if getattr(original, "_wisp_guarded", False):
        return True

    def metal_kernel(*args, **kwargs):
        kernel = original(*args, **kwargs)
        name = kwargs.get("name")
        if name is None and args:
            name = args[0]
        return GuardedKernel(kernel, str(name))

    metal_kernel._wisp_guarded = True
    metal_kernel._wisp_original = original
    metal_kernel.__doc__ = getattr(original, "__doc__", None)
    try:
        fast.metal_kernel = metal_kernel
    except Exception:
        logger.warning("Wisp kernel guard could not be installed", exc_info=True)
        return False
    return True


class _CoreLoader:
    """Runs ``install`` right after ``mlx.core`` finishes loading."""

    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        try:
            install(module)
        except Exception:
            logger.warning("Wisp kernel guard could not be installed", exc_info=True)


class _CoreFinder:
    def find_spec(self, name, path=None, target=None):
        if name != "mlx.core":
            return None
        try:
            sys.meta_path.remove(self)
        except ValueError:
            return None
        import importlib.util

        spec = importlib.util.find_spec(name)
        if spec is not None and spec.loader is not None:
            spec.loader = _CoreLoader(spec.loader)
        return spec


def install_when_ready() -> None:
    """Guard the kernels without importing MLX early.

    The engine sets Metal environment variables before its first MLX import
    (the GPU device is created during that import), so this must not move the
    import forward. If MLX is already loaded the guard goes in now; otherwise
    it goes in the moment ``mlx.core`` finishes loading, which is before any
    kernel can be built.
    """
    core = sys.modules.get("mlx.core")
    if core is not None and hasattr(core, "fast"):
        install(core)
        return
    if not any(isinstance(finder, _CoreFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _CoreFinder())


# ---------------------------------------------------------------------------
# Lightning MTP: a verify-cycle failure costs speed, never the reply.
# ---------------------------------------------------------------------------

_MTP_STRIKES = 3
_mtp_off_models: set = set()
_mtp_strikes: dict = {}
_mtp_logged: set = set()


def must_propagate(exc: BaseException) -> bool:
    """Errors a fallback would only hide: memory enforcement, aborts, and a
    failed command buffer (MLX reports that once; dropping it would keep the
    engine running on results the GPU discarded)."""
    if isinstance(exc, MemoryError):
        return True
    try:
        from omlx.exceptions import OMLXError

        if isinstance(exc, OMLXError):
            return True
    except Exception:
        pass
    text = str(exc)
    return ("Command buffer execution failed" in text or "kIOGPUCommandBuffer" in text
            # Upstream lets a cluster rank's failed rollback through on purpose.
            or "could not roll back its MTP cache" in text)


def _reply_key(gen_batch):
    """The requests a batch is serving. The batch object itself lives as long
    as the scheduler, so a mark on it alone would outlast the reply."""
    try:
        return tuple(getattr(gen_batch, "uids", None) or ())
    except Exception:
        return ()


def mtp_blocked(gen_batch) -> bool:
    """Whether this reply, or its model, already left MTP after a failure."""
    off = getattr(gen_batch, "_wisp_mtp_off", None)
    if off is not None and off == _reply_key(gen_batch):
        return True
    return id(getattr(gen_batch, "model", None)) in _mtp_off_models


def mtp_failure(gen_batch, exc: BaseException) -> bool:
    """Record an error raised inside an MTP step.

    True: the caller rebuilds the committed state and continues this reply
    with plain decoding. False: the error must propagate unchanged.

    A threadgroup-limit error is a property of this GPU, so the model stays on
    plain decoding for as long as it is loaded; so does a model that failed
    ``_MTP_STRIKES`` times for any other reason. Logged once per model.
    """
    if must_propagate(exc):
        return False
    model = getattr(gen_batch, "model", None)
    mark = id(model)
    try:
        gen_batch._wisp_mtp_off = _reply_key(gen_batch)
    except Exception:
        pass
    limit = limit_of(exc)
    with _lock:
        strikes = _mtp_strikes.get(mark, 0) + 1
        _mtp_strikes[mark] = strikes
        if limit is not None or strikes >= _MTP_STRIKES:
            _mtp_off_models.add(mark)
        first = mark not in _mtp_logged
        _mtp_logged.add(mark)
    if first:
        logger.warning(
            "Wisp: Lightning MTP failed (%s: %s); this reply continues with "
            "plain decoding%s",
            type(exc).__name__,
            exc,
            " and MTP stays off for this model" if mark in _mtp_off_models else "",
            exc_info=exc,
        )
    return True
