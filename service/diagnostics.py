"""Bounded, content-free local traces. Never required for request correctness.

No HTTP reader: the desktop reads these same-user files even with the service
stopped. Text, prompts, tool arguments/results and exception messages are never
accepted here. Detailed evidence belongs in an explicitly selected report.
"""
from __future__ import annotations

import contextvars
import asyncio
import json
import hashlib
from functools import lru_cache
import os
import re
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from service.paths import MOE_DIR

MAX_TRACES = 100
MAX_EVENTS = 128
MAX_AGE = 7 * 86400
_current = contextvars.ContextVar("wisp_diagnostic_trace", default=None)
_disk_lock = threading.Lock()
_labels = {"agent", "today", "daily_brief", "source_sync", "native",
           "started", "completed", "failed", "cancelled", "routed", "tool_call",
           "tool_result", "confirm", "approved", "denied", "error", "done",
           "sources", "plan", "requested", "result", "waiting", "unavailable", "unknown"}
_counts = {"tasks", "blocks", "unscheduled", "warnings", "revision", "calendar_ready",
           "reminders_ready", "ok", "events_dropped", "elapsed_ms"}


def _directory(root):
    # Fail closed on a redirected/symlinked journal, but fail open for Wisp.
    root.mkdir(parents=True, exist_ok=True)
    path = root / "diagnostics"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("Unsafe diagnostics directory")
    return path


@lru_cache(maxsize=1)
def backend_build():
    root = Path(__file__).resolve().parent
    try:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
        result = {"source_fingerprint": digest.hexdigest(), "python_version": ".".join(map(str, sys.version_info[:3]))}
        info = root.parent.parent / "build-info.json"
        if info.is_file():
            commit = json.loads(info.read_text()).get("commit", "")
            if isinstance(commit, str) and re.fullmatch(r"[a-f0-9]{40}", commit):
                result["commit"] = commit
        return result
    except Exception:
        return {}


def planner_fingerprint():
    return hashlib.sha256((Path(__file__).parent / "assistant/today.py").read_bytes()).hexdigest()


class Trace:
    def __init__(self, kind, trace_id=None, *, enabled=True, root=None, clock=time.time):
        self.id = trace_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", self.id):
            raise ValueError("Invalid trace ID")
        self.kind = kind if kind in _labels else "agent"
        self.enabled, self.root, self.clock = enabled, Path(root or MOE_DIR), clock
        self.started = clock()
        self._started_monotonic = time.monotonic()
        self.status = "started"
        self.terminal_event = None
        self.events = []
        self.dropped = 0
        self._finished = False
        self._lock = threading.RLock()
        self.event("started")

    def event(self, stage, **fields):
        if not self.enabled or not isinstance(stage, str) or stage not in _labels:
            return
        with self._lock:
            item = {"stage": stage, "elapsed_ms": max(0, round((time.monotonic() - self._started_monotonic) * 1000))}
            # Only numbers/bools under known keys. Never arbitrary dictionaries.
            for key, value in fields.items():
                if key in _counts and type(value) in (bool, int) and abs(value) < 10**12:
                    item[key] = value
            if len(self.events) >= MAX_EVENTS:
                self.events.pop(1)  # retain start and the most recent evidence
                self.dropped += 1
            for key, choices in {
                "source": {"calendar", "reminders", "email", "messages", "notes", "browser_history"},
                "source_state": {"ready", "syncing", "unavailable", "disabled"},
                "error_kind": {"TimeoutError", "ConnectionError", "PermissionError", "ValueError", "RuntimeError", "OSError", "KeyError", "HTTPError", "CancelledError"},
                "native_operation": {"create_calendar_event", "delete_calendar_event", "create_reminder", "update_reminder", "complete_reminder", "delete_reminder"},
                "native_outcome": {"succeeded", "failed", "unknown"},
                "component": {"agent", "today", "daily_brief", "source_sync", "native"},
                "route_role": {"fast", "agent", "general", "coding", "reasoning"},
                "route_source": {"rules", "fallback", "default", "semantic", "model"},
            }.items():
                value = fields.get(key)
                if isinstance(value, str) and value in choices:
                    item[key] = value
            name = fields.get("tool_name")
            registry = getattr(sys.modules.get("service.tools.registry"), "REGISTRY", {})
            tool = registry.get(name) if isinstance(name, str) else None
            if tool and getattr(tool.func, "__module__", "").startswith("service.tools.") and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                item["tool_name"] = name
            self.events.append(item)
            self._persist()

    def observe(self, event):
        # Token streams and raw I/O never enter the journal, including in debug mode.
        stage = event.get("type")
        if isinstance(stage, str) and stage in {"routed", "tool_call", "tool_result", "confirm", "error", "done"}:
            if stage in {"done", "error"}:
                self.terminal_event = stage
            if stage == "error":
                self.status = "failed"
            self.event(stage, route_role=event.get("role"), route_source=event.get("route_source"),
                       tool_name=event.get("name") or event.get("tool"))

    def finish(self, status="completed"):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self.status != "failed":
                self.status = status if status in {"completed", "failed", "cancelled", "unknown"} else "failed"
            self.event(self.status)

    def payload(self):
        return {"schema_version": 1, "trace_id": self.id, "kind": self.kind,
                "started_at": self.started, "status": self.status, "backend_build": backend_build(),
                "client_terminal_event": self.terminal_event,
                "events_dropped": self.dropped, "events": list(self.events)}

    def _persist(self):
        try:
            with _disk_lock:
                directory = _directory(self.root)
                # Atomic replacement keeps readers from seeing partial JSON.
                temporary = directory / (self.id + "." + uuid.uuid4().hex + ".tmp")
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        json.dump(self.payload(), stream, allow_nan=False)
                    os.replace(temporary, directory / (self.id + ".json"))
                finally:
                    temporary.unlink(missing_ok=True)
                files = sorted((p for p in directory.glob("*.json")
                                if re.fullmatch(r"[a-f0-9]{32}\.json", p.name)),
                               key=lambda p: p.lstat().st_mtime, reverse=True)
                for index, path in enumerate(files):
                    if index >= MAX_TRACES or self.clock() - path.lstat().st_mtime > MAX_AGE:
                        path.unlink(missing_ok=True)
        except Exception:
            # Diagnostics must not replace a failure, interrupt a turn, or send data.
            pass


@contextmanager
def use(trace):
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


def event(stage, **fields):
    trace = _current.get()
    if trace is not None:
        trace.event(stage, **fields)


@contextmanager
def span(kind, *, enabled=True):
    """Nest in the request trace, or create a trace for background work."""
    existing = _current.get()
    trace = existing or Trace(kind, enabled=enabled)
    with use(trace):
        if existing:
            trace.event("requested", component=kind)
        try:
            yield trace
        except BaseException as error:
            status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            if existing:
                trace.event(status, component=kind)
            else:
                trace.finish(status)
            raise
        else:
            if existing:
                trace.event("result", component=kind)
            else:
                trace.finish()


def current_id():
    trace = _current.get()
    return trace.id if trace is not None and trace.enabled else None
