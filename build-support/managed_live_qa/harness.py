"""One-shot synthetic live qualification for PR #50."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import sys
import time
import types
from typing import Awaitable, Callable

REPORT_KEYS = frozenset({"schema_version", "manifest_sha256", "candidate_sha", "model",
                         "status", "reason_codes", "call_count", "selected_ids",
                         "predicates", "elapsed_ms"})
REASON_CODES = frozenset({"invalid_capability", "readiness_unproven", "bounded_run_failed",
                          "unknown_fixture_id", "predicate_failed"})
PREDICATE_KEYS = frozenset({"schemas_valid", "daily_model_free", "daily_ready",
                            "email_selected", "messages_selected", "source_hashes_valid"})
MANIFEST_KEYS = frozenset({"schema_version", "artifact_kind", "manifest_id", "bundle_id",
                           "model", "port", "keychain", "max_completion_calls",
                           "completion_timeout_seconds", "run_timeout_seconds", "fixtures",
                           "expected_selected_ids", "required_readiness",
                           "required_predicates", "report_keys"})
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[a-z][a-z0-9-]{2,63}")


class QAError(RuntimeError):
    """Fixed-code refusal; messages never contain credentials or fixture text."""


def canonical_manifest(path: Path) -> tuple[dict, str]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise QAError("invalid_manifest") from None
    fixtures = raw.get("fixtures") if isinstance(raw, dict) else None
    if (not isinstance(raw, dict) or set(raw) != MANIFEST_KEYS
            or raw.get("schema_version") != 1
            or raw.get("artifact_kind") != "wisp-managed-summary-qa-v1"
            or raw.get("manifest_id") != "pr50-summary-selection-v1"
            or raw.get("bundle_id") != "com.wisp.app.summary-qa"
            or raw.get("model") != "Ling-3.0-tiny-oQ4e"
            or raw.get("port") != 18765
            or raw.get("keychain") != {
                "service": "com.wisp.summary-qa.inference", "account": "local-omlx"}
            or raw.get("max_completion_calls") != 2
            or type(raw.get("completion_timeout_seconds")) is not int
            or not 1 <= raw["completion_timeout_seconds"] <= 30
            or type(raw.get("run_timeout_seconds")) is not int
            or not raw["completion_timeout_seconds"] * 2 <= raw["run_timeout_seconds"] <= 60
            or not isinstance(fixtures, dict)
            or set(fixtures) != {"email_ids", "message_ids", "calendar_ids"}):
        raise QAError("invalid_manifest")
    all_ids = []
    for key in ("email_ids", "message_ids", "calendar_ids"):
        values = fixtures[key]
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                       for value in values)
                or len(values) != len(set(values))):
            raise QAError("invalid_manifest")
        all_ids.extend(values)
    if (len(all_ids) != len(set(all_ids))
            or raw.get("expected_selected_ids")
            != [fixtures["email_ids"][0], fixtures["message_ids"][0]]
            or raw.get("required_readiness")
            != ["exclusive_session", "model_loaded", "server_idle"]
            or set(raw.get("required_predicates", [])) != PREDICATE_KEYS
            or len(raw.get("required_predicates", [])) != len(PREDICATE_KEYS)
            or set(raw.get("report_keys", [])) != REPORT_KEYS
            or len(raw.get("report_keys", [])) != len(REPORT_KEYS)):
        raise QAError("invalid_manifest")
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    return raw, hashlib.sha256(encoded).hexdigest()


def sanitized_report(*, manifest_sha: str, candidate_sha: str, model: str,
                     status: str, reason_codes: list[str], call_count: int,
                     selected_ids: list[str], predicates: dict[str, bool],
                     elapsed_ms: int) -> dict:
    if (not HEX64.fullmatch(manifest_sha) or not HEX40.fullmatch(candidate_sha)
            or model != "Ling-3.0-tiny-oQ4e" or status not in {"PASS", "FAIL", "BLOCK"}
            or type(call_count) is not int or call_count not in range(3)
            or not isinstance(reason_codes, list)
            or len(reason_codes) != len(set(reason_codes))
            or not set(reason_codes).issubset(REASON_CODES)
            or not isinstance(selected_ids, list)
            or len(selected_ids) != len(set(selected_ids))
            or any(not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                   for value in selected_ids)
            or not isinstance(predicates, dict) or set(predicates) != PREDICATE_KEYS
            or any(type(value) is not bool for value in predicates.values())
            or type(elapsed_ms) is not int or elapsed_ms < 0
            or not math.isfinite(elapsed_ms)):
        raise QAError("invalid_report")
    return {"schema_version": 1, "manifest_sha256": manifest_sha,
            "candidate_sha": candidate_sha, "model": model, "status": status,
            "reason_codes": reason_codes, "call_count": call_count,
            "selected_ids": selected_ids, "predicates": predicates,
            "elapsed_ms": min(elapsed_ms, 60_000)}


def _attach(name: str, module: types.ModuleType) -> types.ModuleType:
    sys.modules[name] = module
    if "." in name:
        parent_name, child = name.rsplit(".", 1)
        if parent_name in sys.modules:
            setattr(sys.modules[parent_name], child, module)
    return module


def _package(name: str, path: Path) -> None:
    module = sys.modules.get(name) or _attach(name, types.ModuleType(name))
    module.__path__ = [str(path)]
    module.__package__ = name


class _Store:
    def upcoming(self, *, now: float, days: int) -> list[dict]:
        return [{"id": "calendar-today", "title": "QA orientation",
                 "when_ts": now + 1800, "source": "calendar",
                 "account": "qa@example.invalid"}]

    def __getattr__(self, _name):
        return lambda *args, **kwargs: []


def install_synthetic_adapters(service_root: Path, *, omlx_key: str = "") -> dict:
    """Install a closed synthetic world before importing candidate summaries."""
    service_root = service_root.resolve()
    for name, path in (("service", service_root), ("service.tools", service_root / "tools"),
                       ("service.assistant", service_root / "assistant"),
                       ("service.inference", service_root / "inference"),
                       ("service.memory", service_root / "memory")):
        _package(name, path)
    installed = {}
    cache = types.ModuleType("service.tools.cache_store")
    allowed = {"messages", "email_headers", "email_history", "raw_emails",
               "identity_emails", "contacts", "contact_handles", "birthdays"}
    def load(name):
        if name not in allowed:
            raise QAError("synthetic_cache_refused")
        return ""
    def save(_name, _value):
        raise QAError("synthetic_cache_read_only")
    cache.load, cache.save = load, save
    installed[cache.__name__] = _attach(cache.__name__, cache)

    registry = types.ModuleType("service.tools.registry")
    registry.register = lambda *args, **kwargs: (lambda function: function)
    installed[registry.__name__] = _attach(registry.__name__, registry)
    timeranges = types.ModuleType("service.tools.timeranges")
    timeranges.PERIOD_ARG = {}
    timeranges.BadPeriod = type("BadPeriod", (ValueError,), {})
    timeranges.resolve_span = lambda *args, **kwargs: (0.0, time.time(), "QA fixture")
    installed[timeranges.__name__] = _attach(timeranges.__name__, timeranges)
    capture = types.ModuleType("service.debug_capture")
    capture.record = lambda *args, **kwargs: None
    installed[capture.__name__] = _attach(capture.__name__, capture)
    identity = types.ModuleType("service.memory.identity")
    identity.is_me = lambda *args, **kwargs: False
    identity.user_name = identity.get_user_name = lambda: "QA User"
    identity.__getattr__ = lambda _name: (lambda *args, **kwargs: False)
    installed[identity.__name__] = _attach(identity.__name__, identity)
    store = types.ModuleType("service.assistant.store")
    store.assistant_store = _Store()
    installed[store.__name__] = _attach(store.__name__, store)

    sync = types.ModuleType("service.assistant.sync_status")
    snapshot = {"syncing": [], "unavailable": [], "sources": {
        name: {"id": name, "label": name.title(), "state": "ready"}
        for name in ("calendar", "reminders", "mail", "messages")}}
    sync.source_status = lambda name: {"id": name, "label": name.title(), "state": "ready"}
    sync.summary_snapshot = lambda: dict(snapshot)
    sync.ensure_daily_sources = lambda **kwargs: asyncio.sleep(0, result=dict(snapshot))
    sync.ensure_sources = lambda *args, **kwargs: asyncio.sleep(0, result=dict(snapshot))
    sync.daily_syncing_message = lambda _snapshot: "QA sources are not ready."
    installed[sync.__name__] = _attach(sync.__name__, sync)
    tools = types.ModuleType("service.tools.assistant_tools")
    tools._without_holiday_calendars = lambda events, **kwargs: list(events)
    tools._fmt = lambda event, now, **kwargs: (
        f"TODAY {event.get('title', 'QA event')} at +30 minutes")
    installed[tools.__name__] = _attach(tools.__name__, tools)

    config = types.ModuleType("service.config")
    config.__path__ = []
    config.role_to_model = lambda _role: "Ling-3.0-tiny-oQ4e"
    config.user_facing_summary_kwargs = config.no_thinking_kwargs = lambda _model: {}
    config.omlx_api_key = lambda: omlx_key
    config.omlx_base_url = lambda: "http://127.0.0.1:8000"
    installed[config.__name__] = _attach(config.__name__, config)
    endpoints = types.ModuleType("service.config.endpoints")
    endpoints.EndpointConfigurationError = type("EndpointConfigurationError", (ValueError,), {})
    endpoints.Target = type("Target", (), {})
    endpoints.endpoint = lambda: None
    endpoints.is_loopback = lambda value: value in {
        "http://127.0.0.1:8000", "http://localhost:8000"}
    installed[endpoints.__name__] = _attach(endpoints.__name__, endpoints)
    quarantine = types.ModuleType("service.config.quarantine")
    quarantine.guard_client = lambda client: client
    installed[quarantine.__name__] = _attach(quarantine.__name__, quarantine)
    idle = types.ModuleType("service.idle")
    idle.begin = idle.end = lambda *args, **kwargs: None
    idle.in_flight = lambda *args, **kwargs: 0
    idle.foreground_busy = lambda: False
    idle.__getattr__ = lambda _name: (lambda *args, **kwargs: None)
    installed[idle.__name__] = _attach(idle.__name__, idle)
    return installed


class _CountingClient:
    def __init__(self, inner, manifest):
        self.inner, self.manifest = inner, manifest
        self.call_count = 0
        self.email_schema_valid = self.message_schema_valid = False
        self.selected_ids = []

    async def ensure_only(self, *args, **kwargs):
        return await self.inner.ensure_only(*args, **kwargs)

    async def chat(self, model, messages, **kwargs):
        if self.call_count >= self.manifest["max_completion_calls"]:
            raise QAError("completion_budget_exceeded")
        self.call_count += 1
        async with asyncio.timeout(self.manifest["completion_timeout_seconds"]):
            response = await self.inner.chat(model, messages, **kwargs)
        try:
            choice = response["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(content, str) or len(content) > 6000:
                return response
            payload = json.loads(content)
            system = str(messages[0].get("content", ""))
            candidates = json.loads(messages[1]["content"])
            if "Choose the most useful email IDs" in system:
                values = payload.get("prioritize") if isinstance(payload, dict) else None
                valid = (isinstance(values, list) and len(values) == len(set(values))
                         and all(isinstance(value, str) and value in candidates for value in values))
                self.email_schema_valid = valid
                if valid and values and values[0] == "0":
                    self.selected_ids.append(self.manifest["fixtures"]["email_ids"][0])
            elif "Select up to three useful" in system:
                valid = (isinstance(payload, dict) and set(payload) == set(candidates)
                         and all(isinstance(values, list) and 1 <= len(values) <= 3
                                 and len(values) == len(set(values))
                                 and all(value in candidates[key] for value in values)
                                 for key, values in payload.items()))
                self.message_schema_valid = valid
                if valid and "0" in payload:
                    self.selected_ids.append(self.manifest["fixtures"]["message_ids"][0])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return response


class LiveSummaryRunner:
    def __init__(self, manifest, credential, service_root, *, source_hashes_valid):
        if not HEX64.fullmatch(credential):
            raise QAError("invalid_credential")
        self.manifest = manifest
        self.source_hashes_valid = source_hashes_valid
        install_synthetic_adapters(service_root, omlx_key=credential)
        from service.inference.omlx_client import OMLXClient
        self.client = _CountingClient(OMLXClient(
            base_url="http://127.0.0.1:8000", api_key=credential,
            timeout=float(manifest["completion_timeout_seconds"])), manifest)
        self.modules = None

    @property
    def call_count(self):
        return self.client.call_count

    def _load_originals(self):
        if self.modules is None:
            for name in ("service.tools.email_tools", "service.tools.imessage_tools",
                         "service.assistant.brief"):
                sys.modules.pop(name, None)
            email = importlib.import_module("service.tools.email_tools")
            messages = importlib.import_module("service.tools.imessage_tools")
            brief = importlib.import_module("service.assistant.brief")
            email._client = messages._client = self.client
            self.modules = email, messages, brief
        return self.modules

    async def qualify(self):
        model = self.manifest["model"]
        try:
            await self.client.ensure_only(model, exclusive=True)
            health = await self.client.inner.health()
            status = await self.client.inner.status()
            loaded = await self.client.inner.loaded_models()
        except Exception:
            return {"exclusive_session": False, "model_loaded": False, "server_idle": False}
        return {"model_loaded": health == {"status": "ok"} and model in loaded,
                "exclusive_session": loaded == [model], "server_idle": _proven_idle(status)}

    async def run(self):
        email, messages, brief = self._load_originals()
        now = time.time()
        email_rows = [
            f"{now:.0f} | U | qa@example.invalid | Campus Office | "
            "Action required: confirm orientation by Friday",
            f"{now - 60:.0f} | R | qa@example.invalid | Store | Weekly offers"]
        message_rows = [(now - 30, "Mom", "Mom: Can you confirm dinner at 7 tonight?"),
                        (now - 90, "Study Group", "Alex: The notes are uploaded.")]
        email._headers, email._headers_at = "\n".join(email_rows), now
        email._headers_sync_generation, email._email_available = 1, True
        messages._lines = "\n".join(
            f"{stamp:.0f} | {context} | {text}" for stamp, context, text in message_rows)
        messages._available = messages._sync_completed = True
        email_result = await email._summarize(email_rows, "QA fixture")
        message_result = await messages._summarize(message_rows, "QA fixture")
        before_daily = self.call_count
        daily = await brief._sections("morning", snapshot={"syncing": []})
        expected = self.manifest["expected_selected_ids"]
        selected = list(dict.fromkeys(self.client.selected_ids))
        predicates = {
            "schemas_valid": self.client.email_schema_valid and self.client.message_schema_valid,
            "daily_model_free": self.call_count == before_daily,
            "daily_ready": (isinstance(daily, dict) and daily.get("READY") == "1"
                            and bool(str(daily.get("FULL", "")).strip())),
            "email_selected": expected[0] in selected and bool(email_result.strip()),
            "messages_selected": expected[1] in selected and bool(message_result.strip()),
            "source_hashes_valid": self.source_hashes_valid}
        return selected, predicates, self.call_count


def _proven_idle(status):
    numeric = {"active_requests", "queued_requests", "queue_depth", "in_flight", "running_requests"}
    boolean = {"is_generating", "is_loading", "loading", "busy"}
    observed = 0
    def visit(value):
        nonlocal observed
        if isinstance(value, dict):
            for key, item in value.items():
                if key in numeric and type(item) is int:
                    observed += 1
                    if item:
                        return False
                elif key in boolean and type(item) is bool:
                    observed += 1
                    if item:
                        return False
                if not visit(item):
                    return False
        elif isinstance(value, list):
            for item in value:
                if not visit(item):
                    return False
        return True
    return visit(status) and observed > 0


class OneShotHarness:
    def __init__(self, manifest, manifest_sha, candidate_sha):
        self.manifest, self.manifest_sha, self.candidate_sha = manifest, manifest_sha, candidate_sha
        self.state, self.lock = "ARMED", asyncio.Lock()

    def _report(self, status, reasons, calls, selected, predicates, started):
        return sanitized_report(manifest_sha=self.manifest_sha,
            candidate_sha=self.candidate_sha, model=self.manifest["model"], status=status,
            reason_codes=reasons, call_count=calls, selected_ids=selected,
            predicates=predicates, elapsed_ms=int((time.monotonic() - started) * 1000))

    async def consume(self, capability_valid: bool, readiness: dict[str, bool],
                      run: Callable[[], Awaitable[tuple[list[str], dict[str, bool], int]]]):
        started = time.monotonic()
        empty = {key: False for key in PREDICATE_KEYS}
        async with self.lock:
            if self.state != "ARMED":
                raise QAError("one_shot_spent")
            self.state = "RUNNING"
        try:
            if not capability_valid:
                return self._report("FAIL", ["invalid_capability"], 0, [], empty, started)
            if (not isinstance(readiness, dict)
                    or set(readiness) != set(self.manifest["required_readiness"])
                    or any(type(value) is not bool for value in readiness.values())
                    or not all(readiness.values())):
                return self._report("BLOCK", ["readiness_unproven"], 0, [], empty, started)
            try:
                selected, predicates, calls = await asyncio.wait_for(
                    run(), timeout=self.manifest["run_timeout_seconds"])
            except asyncio.CancelledError:
                raise
            except Exception:
                owner = getattr(run, "__self__", None)
                calls = getattr(owner, "call_count", 0)
                calls = calls if type(calls) is int and calls in range(3) else 0
                return self._report("FAIL", ["bounded_run_failed"], calls, [], empty, started)
            allowed = {value for values in self.manifest["fixtures"].values() for value in values}
            if (not isinstance(selected, list) or len(selected) != len(set(selected))
                    or any(value not in allowed for value in selected)):
                return self._report("FAIL", ["unknown_fixture_id"], calls, [], empty, started)
            valid = (isinstance(predicates, dict) and set(predicates) == PREDICATE_KEYS
                     and all(type(value) is bool for value in predicates.values()))
            passed = (calls == self.manifest["max_completion_calls"]
                      and selected == self.manifest["expected_selected_ids"]
                      and valid and all(predicates.values()))
            return self._report("PASS" if passed else "FAIL",
                                [] if passed else ["predicate_failed"], calls, selected,
                                predicates if valid else empty, started)
        finally:
            self.state = "TERMINAL"
