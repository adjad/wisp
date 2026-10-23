"""Process-latched no-effect boundary for the contained Notch evaluation host.

Stdlib only, and inert unless ``scripts/notch_evaluation/host.py`` latches it
before importing any other service module. Production never latches it, so
every check below returns "not blocked" there and changes no behavior.

Once latched, the boundary cannot be turned off for the life of the process:
``latch`` refuses a second call, and the module refuses reassignment of its
state and functions. Normal policy and confirmation still run first; the
boundary sits at the final sinks (registry dispatch, the native outbox, hub
publication) and turns every effect into a structured blocked receipt.

Only reviewed builtin reads may run. A read is identified by the exact
function object and code object sealed from the registry after import, not by
its name, category or any metadata a tool declares about itself. Path
arguments must resolve inside the evaluation's source roots.

Receipts are mandatory evidence. Their file must be a private regular file
inside the owned root; if a receipt cannot be persisted, or the retention
bound is reached, the boundary records a sticky evidence failure and from then
on blocks every dispatch, so a run never continues without its evidence.

This is an in-process control. Arbitrary code in the same process could still
reach native I/O, which is why the host also runs under an OS sandbox profile
and an irreversible audit hook. The boundary never grants authority that
production policy would refuse.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import types
from dataclasses import dataclass, field

BLOCKED_MARKER = "EVALUATION_BLOCKED"
_MAX_RECEIPTS = 10_000             # in-memory copy only; the file is the record
_MAX_RECEIPT_BYTES = 64 * 1024 * 1024
# Hub events that carry no effect and no private content. Everything else is refused.
_INERT_EVENTS = frozenset({"changed", "heartbeat", "hello"})


class EvaluationBlockedError(RuntimeError):
    """An effect reached a final sink while the evaluation boundary is latched."""


@dataclass(frozen=True)
class ReadBinding:
    """One reviewed builtin read: its registry name and implementation module.

    ``path_args`` name the arguments that address files; each must resolve
    inside a source root. The function and code identity are captured by
    ``seal_reads`` from the imported registry, never supplied by a caller.
    """
    tool: str
    module: str
    qualname: str
    path_args: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Context:
    run_id: str
    owned_root: str
    source_roots: tuple[str, ...]
    reads: tuple[ReadBinding, ...]
    receipts_path: str | None = None
    allowed_models: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Sealed:
    binding: ReadBinding
    func: object
    code: object
    code_sha256: str


@dataclass
class _State:
    context: Context
    sealed: dict[str, _Sealed] | None = None
    receipts: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    fd: int | None = None
    written: int = 0
    failure: str | None = None


_state: _State | None = None


def _code_sha256(code) -> str:
    import marshal
    return hashlib.sha256(marshal.dumps(code)).hexdigest()


def latch(context: Context) -> None:
    """Activate the boundary for the rest of this process. Callable once."""
    global _state
    if _state is not None:
        raise EvaluationBlockedError("evaluation boundary is already latched")
    if not isinstance(context, Context) or not context.run_id or not os.path.isabs(context.owned_root):
        raise ValueError("an evaluation context needs a run id and an absolute owned root")
    fd = _open_receipts(context.receipts_path, context.owned_root) if context.receipts_path else None
    _state = _State(context=context, fd=fd)
    _Module._frozen = True
    _record("latched", "boundary", {"run_id": context.run_id,
                                    "reads": sorted(b.tool for b in context.reads)})


def _open_receipts(path: str, owned_root: str) -> int:
    """A private, regular, append-only receipt file inside the owned root."""
    import stat
    owned = os.path.realpath(owned_root)
    parent = os.path.realpath(os.path.dirname(path))
    if not os.path.isabs(path) or not (parent == owned or parent.startswith(owned.rstrip(os.sep) + os.sep)):
        raise ValueError("the receipt file must be inside the owned root")
    try:
        fd = os.open(os.path.join(parent, os.path.basename(path)),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError as exc:
        raise ValueError(f"the receipt file cannot be opened: {exc.strerror}") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("the receipt file must be a private regular file owned by this user")
    return fd


def evidence_failure() -> str | None:
    """Why mandatory evidence can no longer be kept, or None. Sticky once set."""
    return None if _state is None else _state.failure


def active() -> bool:
    return _state is not None


def context() -> Context | None:
    return None if _state is None else _state.context


def seal_reads(registry: dict) -> dict[str, str]:
    """Capture the reviewed reads' exact implementations. Callable once.

    A binding whose registry entry is missing, or whose function is not the
    reviewed module and qualname, is left unsealed: that tool stays blocked.
    Returns tool name -> code sha256 for the evidence ledger.
    """
    if _state is None:
        raise EvaluationBlockedError("the evaluation boundary is not latched")
    with _state.lock:
        if _state.sealed is not None:
            raise EvaluationBlockedError("reviewed reads are already sealed")
        sealed: dict[str, _Sealed] = {}
        for binding in _state.context.reads:
            tool = registry.get(binding.tool)
            func = getattr(tool, "func", None)
            code = getattr(func, "__code__", None)
            if (tool is None or code is None or getattr(tool, "name", None) != binding.tool
                    or getattr(func, "__module__", None) != binding.module
                    or getattr(func, "__qualname__", None) != binding.qualname):
                continue
            sealed[binding.tool] = _Sealed(binding, func, code, _code_sha256(code))
        _state.sealed = sealed
    identities = {name: s.code_sha256 for name, s in sorted(sealed.items())}
    _record("sealed", "reads", {"sealed": identities,
                                "unsealed": sorted({b.tool for b in _state.context.reads} - set(sealed))})
    return identities


def receipts() -> list[dict]:
    if _state is None:
        return []
    with _state.lock:
        return [dict(r) for r in _state.receipts]


def _digest(value) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=repr, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _record(outcome: str, sink: str, detail: dict) -> dict:
    """Keep a receipt. Values are never stored, only names and digests."""
    receipt = {"marker": BLOCKED_MARKER if outcome == "blocked" else "EVALUATION_BOUNDARY",
               "outcome": outcome, "sink": sink, "at": time.time(), **detail}
    if _state is None:
        return receipt
    line = (json.dumps(receipt, sort_keys=True, default=str) + "\n").encode("utf-8", "replace")
    with _state.lock:
        if len(_state.receipts) < _MAX_RECEIPTS:
            _state.receipts.append(receipt)
        elif _state.fd is None and _state.failure is None:
            _state.failure = "receipt retention is exhausted and there is no receipt file"
        if _state.fd is not None and _state.failure is None:
            if _state.written + len(line) > _MAX_RECEIPT_BYTES:
                _state.failure = "the receipt file reached its retention bound"
            else:
                try:
                    if os.write(_state.fd, line) != len(line):
                        raise OSError("short write")
                    _state.written += len(line)
                except OSError as exc:
                    _state.failure = f"a receipt could not be persisted ({exc})"[:200]
    return receipt


def _within(path: str, roots: tuple[str, ...]) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    resolved = os.path.realpath(os.path.expanduser(path))
    for root in roots:
        base = os.path.realpath(root)
        if resolved == base or resolved.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def tool_dispatch_problem(tool, args: dict) -> str | None:
    """None to let a registry dispatch run; otherwise the blocked tool result.

    Called after argument validation and before ``tool.func``. Inactive
    outside the evaluation host.
    """
    if _state is None:
        return None
    name = getattr(tool, "name", "?")
    reason = None
    sealed = (_state.sealed or {}).get(name)
    func = getattr(tool, "func", None)
    if _state.failure is not None:
        reason = "evaluation evidence can no longer be kept"
    elif _state.sealed is None:
        reason = "reviewed reads are not sealed"
    elif sealed is None:
        reason = "not a reviewed read"
    elif func is not sealed.func or getattr(func, "__code__", None) is not sealed.code:
        reason = "implementation identity does not match the reviewed read"
    else:
        for key in sealed.binding.path_args:
            if key in args and not _within(args[key], _state.context.source_roots):
                reason = f"argument {key!r} is outside the evaluation source scope"
                break
    keys = sorted(args) if isinstance(args, dict) else []
    if reason is None:
        _record("allowed", "registry", {"tool": name, "arg_keys": keys, "args_sha256": _digest(args)})
        # Admission requires this dispatch's own receipt to have survived.
        # Recording can itself set the sticky failure (including a short write).
        if _state.failure is None:
            return None
        reason = "evaluation evidence can no longer be kept"
    _record("blocked", "registry", {"tool": name, "reason": reason, "arg_keys": keys,
                                    "args_sha256": _digest(args)})
    return (f"(error: {BLOCKED_MARKER} — {name} was not run: {reason}. This is a contained "
            f"evaluation build; nothing was read, sent or changed.)")


def native_request_blocked(event_type: str, payload) -> dict | None:
    """None outside evaluation; otherwise a not-attempted result for the outbox."""
    if _state is None:
        return None
    _record("blocked", "outbox", {"event_type": str(event_type),
                                  "payload_keys": sorted(payload) if isinstance(payload, dict) else [],
                                  "payload_sha256": _digest(payload)})
    return {"ok": False, "status": "blocked", "dispatch_started": False, "evaluation_blocked": True,
            "error": f"{BLOCKED_MARKER}: the contained evaluation build never dispatches native actions; "
                     "nothing was sent or changed"}


def publication_allowed(event, variant: str) -> bool:
    """False for an inert event the hub should skip silently; raises for any other.

    Returns True only outside evaluation. Under evaluation nothing reaches the
    durable queue or a subscriber.
    """
    if _state is None:
        return True
    kind = event.get("type") if isinstance(event, dict) else None
    if kind in _INERT_EVENTS:
        _record("suppressed", "hub", {"variant": variant, "event_type": kind})
        return False
    _record("blocked", "hub", {"variant": variant, "event_type": str(kind),
                               "event_sha256": _digest(event)})
    raise EvaluationBlockedError(f"{BLOCKED_MARKER}: hub {variant} publication of {kind!r} refused")


def model_control_blocked(operation: str, model: str = "") -> EvaluationBlockedError | None:
    """An error for any engine lifecycle call; inference itself is not routed here."""
    if _state is None:
        return None
    _record("blocked", "model_control", {"operation": operation, "model": model})
    return EvaluationBlockedError(f"{BLOCKED_MARKER}: {operation} is not permitted in the evaluation build")


class _Module(types.ModuleType):
    """Refuses rebinding of the boundary's state and functions once latched."""
    _frozen = False

    def __setattr__(self, name, value):
        if type(self)._frozen:
            raise AttributeError(f"evaluation_boundary.{name} is immutable")
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if type(self)._frozen:
            raise AttributeError(f"evaluation_boundary.{name} is immutable")
        super().__delattr__(name)


sys.modules[__name__].__class__ = _Module
