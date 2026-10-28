"""Wisp's typed, retained oMLX guard evidence; no settings or limit changes.

Installed only by the hash-bound packaging overlay. The endpoint reads the
snapshot actually propagated to schedulers; it never creates a health proof
by recalculating a preview. All unrecognized state becomes unqualified.
"""
from __future__ import annotations

import asyncio
import functools
import os
import sys
import threading
import time
import uuid

CLOCK_DOMAIN = "darwin-clock_gettime-CLOCK_MONOTONIC-ns-v1"
_CREATION_LOCK = threading.RLock()
_TIERS = {"safe", "balanced", "aggressive", "custom"}
_SOURCES = {"host_statistics64", "available_memory", "static_fallback", "custom", "disabled", "unknown"}


class GuardEvidenceUnavailable(RuntimeError):
    """Safe refusal, independent of vendor diagnostic/exception text."""

    def __init__(self):
        super().__init__("The AI engine's memory guard could not be verified. Try again after the engine is ready.")


class GuardCapacityChanged(GuardEvidenceUnavailable):
    def __init__(self):
        RuntimeError.__init__(self, "The model no longer fits the AI engine's current memory limit. Free memory, then try again.")


def _now_ns():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def _pid():
    return os.getpid()


def _clock_supported():
    return sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC")


def _uint(value):
    return value if type(value) is int and 0 <= value < 2**63 else 0


def _stamp():
    try:
        return _uint(_now_ns()) if _clock_supported() else 0
    except Exception:
        return 0


def _state(enforcer):
    with _CREATION_LOCK:
        state = getattr(enforcer, "_wisp_guard_evidence", None)
        if state is None:
            state = {"lock": threading.RLock(), "id": uuid.uuid4().hex,
                     "source": "unknown", "sample_seq": 0, "sample_ns": 0,
                     "candidate": None, "applied": None, "capture": None,
                     "iteration_state": "none", "iteration_seq": 0, "iteration_ns": 0,
                     "admission_seq": 0, "admission_ns": 0,
                     "admission_sample_seq": 0, "admission_cap_bytes": 0}
            enforcer._wisp_guard_evidence = state
        return state


def reset(enforcer):
    """A new guard incarnation cannot inherit a previous start's evidence."""
    with _CREATION_LOCK:
        enforcer._wisp_guard_evidence = None
        _state(enforcer)


def begin_sample(enforcer):
    s = _state(enforcer)
    with s["lock"]:
        s["sample_seq"] += 1
        s["sample_ns"] = _stamp()
        s["source"] = "unknown"


def note_source(enforcer, source):
    s = _state(enforcer)
    with s["lock"]:
        s["source"] = source if type(source) is str and source in _SOURCES else "unknown"


def record_breakdown(enforcer, breakdown):
    s = _state(enforcer)
    with s["lock"]:
        values = {k: _uint(breakdown.get(k)) for k in ("static", "dynamic", "metal_cap", "hard_limit")}
        valid = (set(breakdown) == set(values) and
                 all(type(breakdown[k]) is int and 0 <= breakdown[k] < 2**63 for k in values))
        row = dict(values, valid=valid, sample_seq=s["sample_seq"], sample_ns=s["sample_ns"], source=s["source"])
        s["candidate"] = row
        if s["capture"] is not None:
            s["capture"].append(row)
    return breakdown


def propagated(enforcer, breakdown):
    """Called only after the original synchronous propagation returns."""
    s = _state(enforcer)
    with s["lock"]:
        row = s["candidate"]
        if row is not None and all(row[k] == breakdown.get(k) for k in ("static", "dynamic", "metal_cap", "hard_limit")):
            s["applied"] = dict(row)
        else:
            s["applied"] = None


def sampled(function):
    @functools.wraps(function)
    def run(self, *args, **kwargs):
        s = _state(self)
        with s["lock"]:
            begin_sample(self)
            return record_breakdown(self, function(self, *args, **kwargs))
    return run


def propagating(function):
    @functools.wraps(function)
    def run(self, *args, **kwargs):
        s = _state(self)
        with s["lock"]:
            s["capture"] = []
            s["propagation_ok"] = True
            try:
                result = function(self, *args, **kwargs)
                # The pinned original starts with its applied breakdown. Later
                # abort-limit/preview calculations must not replace that sample.
                s["applied"] = dict(s["capture"][0]) if s["capture"] and s["propagation_ok"] else None
                return result
            except BaseException:
                s["applied"] = None
                raise
            finally:
                s["capture"] = None
    return run


def unresolved(enforcer):
    """The original guard could not reach a relevant loaded scheduler."""
    s = _state(enforcer)
    with s["lock"]:
        s["propagation_ok"] = False
        s["applied"] = None


def begin_iteration(enforcer):
    s = _state(enforcer)
    with s["lock"]:
        s["iteration_state"] = "pending"


def complete_iteration(enforcer):
    s = _state(enforcer)
    with s["lock"]:
        s["iteration_seq"] += 1
        s["iteration_ns"] = _stamp()
        s["iteration_state"] = "ok"


def fail_iteration(enforcer):
    s = _state(enforcer)
    with s["lock"]:
        s["iteration_state"] = "failed"


def _task_state(task):
    if task is None:
        return "missing"
    try:
        if task.cancelled() or task.cancelling():
            return "cancelled"
        if task.done():
            return "failed" if task.exception() is not None else "done"
        return "running"
    except BaseException:
        return "failed"


def snapshot(enforcer):
    if enforcer is None:
        # No fabricated enforcer or incarnation. This object is explicitly
        # unqualified and cannot pass the strict consumer.
        enforcer = type("MissingGuard", (), {})()
    s = _state(enforcer)
    with s["lock"]:
        applied = s["applied"] or {}
        tier = getattr(enforcer, "_memory_guard_tier", None)
        tier = tier if type(tier) is str and tier in _TIERS else "unknown"
        prefill = getattr(enforcer, "_prefill_memory_guard", None) is True
        interval = getattr(enforcer, "_current_poll_interval", 0)
        interval_ms = int(interval * 1000) if type(interval) in (int, float) and interval in (1, 10, 30) else 0
        now = _stamp()
        row = {"version": 1, "clock_domain": CLOCK_DOMAIN, "pid": _uint(_pid()), "guard_id": s["id"],
               "tier": tier, "prefill_guard": prefill,
               "running": getattr(enforcer, "_running", None) is True,
               "task_state": _task_state(getattr(enforcer, "_task", None)),
               "iteration_state": s["iteration_state"], "iteration_seq": s["iteration_seq"],
               "iteration_ns": s["iteration_ns"], "sample_seq": applied.get("sample_seq", 0),
               "sample_ns": applied.get("sample_ns", 0), "sample_source": applied.get("source", "unknown"),
               "static_bytes": applied.get("static", 0), "dynamic_bytes": applied.get("dynamic", 0),
               "metal_bytes": applied.get("metal_cap", 0), "cap_bytes": applied.get("hard_limit", 0),
               "poll_interval_ms": interval_ms, "observed_ns": now, "qualified": False, "reason": "missing",
               "admission_seq": s["admission_seq"], "admission_ns": s["admission_ns"],
               "admission_sample_seq": s["admission_sample_seq"], "admission_cap_bytes": s["admission_cap_bytes"]}
        reason = "healthy"
        if not now:
            reason = "clock_invalid"
        elif not row["pid"]:
            reason = "missing"
        elif tier == "unknown":
            reason = "cap_invalid"
        elif not prefill:
            reason = "disabled"
        elif tier == "custom":
            reason = "custom"
        elif not row["running"]:
            reason = "not_running"
        elif row["task_state"] != "running":
            reason = "task_stopped"
        elif s["iteration_state"] != "ok" or not s["iteration_seq"]:
            reason = "iteration_failed" if s["iteration_state"] == "failed" else "iteration_pending"
        elif not applied:
            reason = "unpropagated"
        elif not applied.get("valid"):
            reason = "cap_invalid"
        elif row["sample_source"] != "host_statistics64":
            reason = "sample_unavailable"
        elif not interval_ms or any(not 0 < stamp <= now or now - stamp > (interval_ms + 2000) * 1_000_000
                                    for stamp in (row["sample_ns"], row["iteration_ns"])):
            reason = "stale"
        else:
            caps = [row["static_bytes"], row["dynamic_bytes"]]
            if row["metal_bytes"]:
                caps.append(row["metal_bytes"])
            if any(not value for value in caps) or row["cap_bytes"] != min(caps):
                reason = "cap_invalid"
        row["reason"] = reason
        row["qualified"] = reason == "healthy"
        return row


def require_admission(enforcer, required=0, current=0):
    if enforcer is None:
        raise GuardEvidenceUnavailable()
    with _state(enforcer)["lock"]:
        return _require_admission(enforcer, required, current)


def _require_admission(enforcer, required, current):
    """No waits, retry, fallback or profile changes inside the pool lock."""
    if enforcer is None:
        raise GuardEvidenceUnavailable()
    # An explicitly disabled or custom user profile retains upstream policy.
    # It is reported as unqualified and cannot certify fresh balanced defaults.
    prefill = getattr(enforcer, "_prefill_memory_guard", None)
    tier = getattr(enforcer, "_memory_guard_tier", None)
    if type(tier) is str and tier in _TIERS and (prefill is False or (prefill is True and tier == "custom")):
        return snapshot(enforcer)
    if not snapshot(enforcer)["qualified"]:
        raise GuardEvidenceUnavailable()
    # Refresh actual propagation, not a separate preview, immediately before
    # allocation. A healthy background iteration must already have completed.
    enforcer._propagate_memory_limit()
    row = snapshot(enforcer)
    if not row["qualified"]:
        raise GuardEvidenceUnavailable()
    if type(required) is not int or type(current) is not int or min(required, current) < 0:
        raise GuardEvidenceUnavailable()
    if required + current > row["cap_bytes"]:
        raise GuardCapacityChanged()
    s = _state(enforcer)
    with s["lock"]:
        s["admission_seq"] += 1
        s["admission_ns"] = _stamp()
        s["admission_sample_seq"] = row["sample_seq"]
        s["admission_cap_bytes"] = row["cap_bytes"]
    result = snapshot(enforcer)
    if (not result["qualified"] or not row["sample_ns"] <= result["admission_ns"] <= result["observed_ns"]
            or result["admission_sample_seq"] != result["sample_seq"]):
        raise GuardEvidenceUnavailable()
    return result


async def wait_initial(enforcer):
    """Bounded startup handshake, before preload and outside the pool lock."""
    deadline = asyncio.get_running_loop().time() + 3.0
    while _state(enforcer)["iteration_state"] in ("none", "pending"):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.01, remaining))
    row = snapshot(enforcer)
    return row["qualified"] or row["reason"] in ("disabled", "custom")


def loaded_state(engine):
    """Residency conventions of the pinned pool constructors, without loading."""
    name = type(engine).__name__
    if name in {"BatchedEngine", "VLMBatchedEngine", "DistributedBatchedEngine"}:
        value = getattr(engine, "_loaded", None)
        if type(value) is bool:
            return value
    if name in {"EmbeddingEngine", "RerankerEngine", "TTSEngine", "STTEngine", "STSEngine"} and hasattr(engine, "_model"):
        return engine._model is not None
    raise GuardEvidenceUnavailable()


def require_usable(engine):
    """A failed post-load handoff cannot become a reusable resident engine."""
    if (getattr(engine, "_wisp_guard_load_rejected", False)
            or getattr(engine, "_wisp_guard_unloading", False)):
        raise GuardEvidenceUnavailable()


async def accept_loaded(engine, check, cleanup):
    """Qualify the attached scheduler before the first caller can use it.

    The rejected instance stays unusable even if existing pool teardown fails.
    Cleanup failure never turns a refused first request into success.
    """
    try:
        require_usable(engine)
        check(engine)
    except GuardEvidenceUnavailable:
        engine._wisp_guard_load_rejected = True
        try:
            await cleanup()
        except Exception:
            pass
        raise


async def unload_bound(pool, model_id, busy_error, expected=None, *, local_only=False):
    """Retain the selected owner through deferred stop and cancellation.

    Normal duplicate unloads retain the vendor's busy result. Refusal cleanup
    joins a teardown of the same instance, even after a registry reset. A
    different instance gets its own task, serialized through the existing
    process-wide settlement policy rather than measuring two unloads at once.
    local_only is the vendor's distributed-teardown flag; a joined teardown
    keeps the flag of the task that started it.
    """
    if expected is None:
        if model_id in pool._unloading_models:
            raise busy_error(model_id, "unload while teardown is in progress")
        entry = pool._entries.get(model_id)
        engine = entry.engine if entry is not None else None
    else:
        entry, engine = expected
    if engine is None:
        return
    state = getattr(pool, "_wisp_unload_state", None)
    if state is None:
        state = {"tasks": {}, "markers": {}, "lock": asyncio.Lock()}
        pool._wisp_unload_state = state
    key = id(engine)
    record = state["tasks"].get(key)
    created = record is None
    if created:
        # A bound direct/lazy/child start must never restart this instance,
        # including after a failed stop. Explicit cleanup may retry stop().
        engine._wisp_guard_unloading = True
        marker = state["markers"].get(model_id)
        if marker is None:
            marker = {"count": 0, "owned": model_id not in pool._unloading_models}
            state["markers"][model_id] = marker
        marker["count"] += 1
        pool._unloading_models.add(model_id)

        async def teardown():
            async with state["lock"]:
                await pool._stop_and_unload_engine(
                    model_id, _wisp_expected=(entry, engine),
                    **({"local_only": True} if local_only else {}))

        task = asyncio.create_task(teardown())
        record = {"entry": entry, "engine": engine, "model_id": model_id, "task": task}
        state["tasks"][key] = record
    else:
        if record["engine"] is not engine or record["entry"] is not entry or record["model_id"] != model_id:
            raise GuardEvidenceUnavailable()
        task = record["task"]
    cancelled = False
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
    finally:
        if created and state["tasks"].get(key) is record:
            del state["tasks"][key]
            marker = state["markers"][model_id]
            marker["count"] -= 1
            if not marker["count"]:
                del state["markers"][model_id]
                if marker["owned"]:
                    pool._unloading_models.discard(model_id)
    if cancelled:
        raise asyncio.CancelledError


def bind_start(engine, check):
    """Keep lazy/direct starts behind the same pool-owned allocation gate."""
    if getattr(engine, "_wisp_guard_bound", False):
        return
    original = engine.start
    lock = asyncio.Lock()
    failed = False

    @functools.wraps(original)
    async def start(*args, **kwargs):
        nonlocal failed
        async with lock:
            require_usable(engine)
            loaded_state(engine)
            if failed:
                raise GuardEvidenceUnavailable()
            check(engine)
            try:
                return await original(*args, **kwargs)
            except BaseException:
                # A partially assigned nonstreaming _model is not success.
                # Pool recovery/discovery creates a fresh owned engine.
                failed = True
                raise

    engine.start = start
    engine._wisp_guard_bound = True
    engine._wisp_guard_check = check


async def start_bound(engine):
    if getattr(engine, "_wisp_guard_bound", None) is not True:
        raise GuardEvidenceUnavailable()
    return await engine.start()
