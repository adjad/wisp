"""Offline-testable core for the one-shot managed summary QA backend."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sys
import types
from typing import Awaitable, Callable

REPORT_KEYS = frozenset({"schema_version", "manifest_sha256", "candidate_sha", "model",
                         "status", "reason_codes", "call_count", "selected_ids",
                         "predicates", "elapsed_ms"})


class QAError(RuntimeError):
    """A fixed-code failure whose details are never copied to the report."""


def canonical_manifest(path: Path) -> tuple[dict, str]:
    raw = json.loads(path.read_text())
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    return raw, hashlib.sha256(encoded).hexdigest()


def install_synthetic_adapters(staged_service: Path) -> dict[str, types.ModuleType]:
    """Install source-read-free dependency shells before summary imports."""
    forbidden = set(sys.modules).intersection({"service.tools.email_tools", "service.tools.imessage_tools",
                                                "service.assistant.brief"})
    if forbidden:
        raise QAError("summary_modules_preimported")
    installed: dict[str, types.ModuleType] = {}
    for package, rel in (("service.tools", "tools"), ("service.memory", "memory"),
                         ("service.assistant", "assistant")):
        module = types.ModuleType(package)
        module.__path__ = [str(staged_service / rel)]
        sys.modules[package] = installed[package] = module

    cache = types.ModuleType("service.tools.cache_store")
    cache.CACHE_DIR = staged_service / "__qa_cache_forbidden__"
    cache.load = lambda name: "" if name in {"email_headers", "email_history", "email_raw",
                                             "messages", "contacts_privacy_revision"} else (_ for _ in ()).throw(QAError("unknown_cache_key"))
    cache.save = lambda *_args, **_kwargs: (_ for _ in ()).throw(QAError("cache_write"))
    sys.modules[cache.__name__] = installed[cache.__name__] = cache

    identity = types.ModuleType("service.memory.identity")
    identity.user_name = lambda: "QA User"
    sys.modules[identity.__name__] = installed[identity.__name__] = identity
    capture = types.ModuleType("service.debug_capture")
    capture.record = lambda *_args, **_kwargs: None
    sys.modules[capture.__name__] = installed[capture.__name__] = capture
    return installed


def sanitized_report(*, manifest_sha: str, candidate_sha: str, model: str,
                     status: str, reason_codes: list[str], call_count: int,
                     selected_ids: list[str], predicates: dict[str, bool], elapsed_ms: int) -> dict:
    if status not in {"PASS", "FAIL", "BLOCK"} or call_count not in range(3):
        raise QAError("invalid_report")
    if not isinstance(elapsed_ms, int) or elapsed_ms < 0 or not math.isfinite(elapsed_ms):
        raise QAError("invalid_report")
    if any(not isinstance(value, bool) for value in predicates.values()):
        raise QAError("invalid_report")
    report = {"schema_version": 1, "manifest_sha256": manifest_sha,
              "candidate_sha": candidate_sha, "model": model, "status": status,
              "reason_codes": sorted(set(reason_codes)), "call_count": call_count,
              "selected_ids": sorted(set(selected_ids)), "predicates": predicates,
              "elapsed_ms": elapsed_ms}
    if set(report) != REPORT_KEYS or len(json.dumps(report)) > 4096:
        raise QAError("invalid_report")
    return report


@dataclass
class OneShotHarness:
    manifest: dict
    manifest_sha: str
    candidate_sha: str
    state: str = "READY"
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def consume(self, capability_ok: bool, readiness: dict[str, bool],
                      run: Callable[[], Awaitable[tuple[list[str], dict[str, bool]]]]) -> dict:
        async with self._lock:
            if self.state != "READY":
                raise QAError("spent")
            self.state = "CONSUMED"
        if not capability_ok:
            return self._terminal("FAIL", ["invalid_capability"], 0, [], {})
        if any(readiness.get(key) is not True for key in self.manifest["required_readiness"]):
            return self._terminal("BLOCK", ["readiness_unproven"], 0, [], {})
        try:
            selected, predicates = await asyncio.wait_for(
                run(), timeout=self.manifest["run_timeout_seconds"])
        except asyncio.CancelledError:
            self.state = "TERMINAL"
            raise
        except (Exception, asyncio.TimeoutError):
            return self._terminal("FAIL", ["bounded_run_failed"], 0, [], {})
        allowed = set().union(*(self.manifest["fixtures"].values()))
        if not set(selected).issubset(allowed):
            return self._terminal("FAIL", ["unknown_fixture_id"], 2, [], predicates)
        status = "PASS" if all(predicates.values()) else "FAIL"
        return self._terminal(status, [] if status == "PASS" else ["predicate_failed"],
                              2, selected, predicates)

    def _terminal(self, status, reasons, calls, selected, predicates):
        self.state = "TERMINAL"
        return sanitized_report(manifest_sha=self.manifest_sha,
            candidate_sha=self.candidate_sha, model=self.manifest["model"], status=status,
            reason_codes=reasons, call_count=calls, selected_ids=selected,
            predicates=predicates, elapsed_ms=0)
