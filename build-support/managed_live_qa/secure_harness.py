"""Synthetic summary qualification core for the staged QA-only artifact."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import sys
import time
import types
from typing import Awaitable, Callable, Protocol

HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[a-z][a-z0-9-]{2,63}")
PREDICATE_KEYS = frozenset({"schemas_valid", "daily_model_free", "daily_ready",
    "email_selected", "messages_selected", "source_hashes_valid"})
READINESS_KEYS = frozenset({"exclusive_session", "model_loaded", "server_idle"})
REASON_CODES = frozenset({"invalid_capability", "readiness_unproven",
    "bounded_run_failed", "unknown_fixture_id", "predicate_failed",
    "external_exclusivity_required", "integrity_unproven"})
REPORT_KEYS = frozenset({"schema_version", "manifest_sha256", "production_sha",
    "artifact_sha", "build_manifest_sha256", "source_inventory_sha256",
    "runtime_inventory_sha256", "native_sha256", "launch_nonce", "child_pid",
    "ipc_authenticated", "model", "status", "reason_codes", "call_count",
    "selected_ids", "predicates", "readiness", "elapsed_ms"})
MANIFEST_KEYS = frozenset({"schema_version", "artifact_kind", "manifest_id",
    "bundle_id", "ipc_protocol", "model", "inference_endpoint",
    "exclusive_proof_protocol", "keychain", "max_completion_calls",
    "completion_timeout_seconds", "run_timeout_seconds", "fixtures",
    "expected_selected_ids", "required_readiness", "required_predicates",
    "report_keys"})


class QAError(RuntimeError):
    """Fixed-code refusal; never includes credentials or fixture text."""


@dataclass(frozen=True)
class ReportIdentity:
    manifest_sha256: str
    production_sha: str
    artifact_sha: str
    build_manifest_sha256: str
    source_inventory_sha256: str
    runtime_inventory_sha256: str
    native_sha256: str
    launch_nonce: str
    child_pid: int
    ipc_authenticated: bool


class ExclusiveLease(Protocol):
    async def assert_held(self) -> None: ...


def canonical_manifest(path: Path) -> tuple[dict, str]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise QAError("invalid_manifest") from None
    fixtures = raw.get("fixtures") if isinstance(raw, dict) else None
    if (not isinstance(raw, dict) or set(raw) != MANIFEST_KEYS
            or raw.get("schema_version") != 2
            or raw.get("artifact_kind") != "wisp-managed-summary-qa-v1"
            or raw.get("manifest_id") != "pr50-summary-selection-v1"
            or raw.get("bundle_id") != "com.wisp.app.summary-qa"
            or raw.get("ipc_protocol") != "anonymous-pipes-v1"
            or raw.get("model") != "Ling-3.0-tiny-oQ4e"
            or raw.get("inference_endpoint") != "http://127.0.0.1:8000"
            or raw.get("exclusive_proof_protocol") not in {
                "unavailable", "server-lease-v1"}
            or raw.get("keychain") != {"service": "com.wisp.summary-qa.inference",
                                       "account": "local-omlx"}
            or raw.get("max_completion_calls") != 2
            or type(raw.get("completion_timeout_seconds")) is not int
            or not 1 <= raw["completion_timeout_seconds"] <= 30
            or type(raw.get("run_timeout_seconds")) is not int
            or not 24 <= raw["run_timeout_seconds"] <= 60
            or not isinstance(fixtures, dict)
            or set(fixtures) != {"email_ids", "message_ids", "calendar_ids"}):
        raise QAError("invalid_manifest")
    all_ids = []
    for key in ("email_ids", "message_ids", "calendar_ids"):
        values = fixtures[key]
        if (not isinstance(values, list) or not values or len(values) != len(set(values))
                or any(not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                       for value in values)):
            raise QAError("invalid_manifest")
        all_ids.extend(values)
    if (len(all_ids) != len(set(all_ids))
            or raw.get("expected_selected_ids")
               != [fixtures["email_ids"][0], fixtures["message_ids"][0]]
            or set(raw.get("required_readiness", [])) != READINESS_KEYS
            or set(raw.get("required_predicates", [])) != PREDICATE_KEYS
            or set(raw.get("report_keys", [])) != REPORT_KEYS):
        raise QAError("invalid_manifest")
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    return raw, hashlib.sha256(encoded).hexdigest()


def sanitized_report(identity: ReportIdentity, *, model: str, status: str,
                     reason_codes: list[str], call_count: int,
                     selected_ids: list[str], predicates: dict[str, bool],
                     readiness: dict[str, bool], elapsed_ms: int) -> dict:
    hashes = (identity.manifest_sha256, identity.build_manifest_sha256,
              identity.source_inventory_sha256, identity.runtime_inventory_sha256,
              identity.native_sha256, identity.launch_nonce)
    if (any(not HEX64.fullmatch(value) for value in hashes)
            or not HEX40.fullmatch(identity.production_sha)
            or not HEX40.fullmatch(identity.artifact_sha)
            or type(identity.child_pid) is not int or identity.child_pid < 0
            or type(identity.ipc_authenticated) is not bool
            or model != "Ling-3.0-tiny-oQ4e" or status not in {"PASS", "FAIL", "BLOCK"}
            or type(call_count) is not int or call_count not in range(3)
            or not isinstance(reason_codes, list) or len(reason_codes) != len(set(reason_codes))
            or not set(reason_codes).issubset(REASON_CODES)
            or not isinstance(selected_ids, list) or len(selected_ids) != len(set(selected_ids))
            or any(not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                   for value in selected_ids)
            or not isinstance(predicates, dict) or set(predicates) != PREDICATE_KEYS
            or any(type(value) is not bool for value in predicates.values())
            or not isinstance(readiness, dict) or set(readiness) != READINESS_KEYS
            or any(type(value) is not bool for value in readiness.values())
            or type(elapsed_ms) is not int or elapsed_ms < 0 or not math.isfinite(elapsed_ms)):
        raise QAError("invalid_report")
    if status == "PASS" and (reason_codes or call_count != 2
            or not all(predicates.values()) or not all(readiness.values())
            or not identity.ipc_authenticated or identity.child_pid <= 0):
        raise QAError("invalid_report")
    if status == "BLOCK" and (call_count or selected_ids):
        raise QAError("invalid_report")
    return {"schema_version": 2, **identity.__dict__, "model": model,
        "status": status, "reason_codes": reason_codes, "call_count": call_count,
        "selected_ids": selected_ids, "predicates": predicates,
        "readiness": readiness, "elapsed_ms": min(elapsed_ms, 60_000)}


def _attach(name: str, module: types.ModuleType) -> types.ModuleType:
    sys.modules[name] = module
    if "." in name:
        parent_name, child = name.rsplit(".", 1)
        if parent_name in sys.modules:
            setattr(sys.modules[parent_name], child, module)
    return module


def _package(name: str, path: Path) -> None:
    module = sys.modules.get(name) or _attach(name, types.ModuleType(name))
    module.__path__, module.__package__ = [str(path)], name


class _Store:
    def upcoming(self, *, now: float, days: int) -> list[dict]:
        return [{"id": "calendar-today", "title": "QA orientation",
                 "when_ts": now + 1800, "source": "calendar",
                 "account": "qa@example.invalid"}]

    def event_by_key(self, _key):
        return None


def install_synthetic_adapters(service_root: Path, *, endpoint: str) -> dict:
    """Install an explicit read-only synthetic world before summary imports."""
    service_root = service_root.resolve()
    isolated = ("service.config.quarantine", "service.inference.omlx_client",
                "service.inference.attributed_transport", "service.inference.local_peer",
                "service.tools.email_tools", "service.tools.imessage_tools",
                "service.tools.message_digest", "service.assistant.brief")
    for name in isolated:
        sys.modules.pop(name, None)
        parent_name, child = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, child):
            delattr(parent, child)
    for name, path in (("service", service_root), ("service.tools", service_root / "tools"),
                       ("service.assistant", service_root / "assistant"),
                       ("service.inference", service_root / "inference"),
                       ("service.config", service_root / "config"),
                       ("service.memory", service_root / "memory")):
        _package(name, path)
    installed = {}
    cache = types.ModuleType("service.tools.cache_store")
    allowed = {"messages", "email_headers", "email_history", "email_raw",
               "identity_emails", "contacts", "contact_handles", "birthdays",
               "contacts_privacy_revision"}
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
    identity.is_me = lambda *_args, **_kwargs: False
    identity.user_name = identity.get_user_name = lambda: "QA User"
    identity.contact_name = lambda _value: ""
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
    async def forbidden_sync(*_args, **_kwargs):
        raise QAError("native_sync_forbidden")
    sync.ensure_daily_sources = sync.ensure_sources = forbidden_sync
    sync.daily_syncing_message = lambda _snapshot: "QA sources are not ready."
    installed[sync.__name__] = _attach(sync.__name__, sync)
    tools = types.ModuleType("service.tools.assistant_tools")
    tools._without_holiday_calendars = lambda events, **_kwargs: list(events)
    tools._fmt = lambda event, now, **_kwargs: (
        f"TODAY {event.get('title', 'QA event')} at +30 minutes")
    installed[tools.__name__] = _attach(tools.__name__, tools)
    config = sys.modules["service.config"]
    config.role_to_model = lambda _role: "Ling-3.0-tiny-oQ4e"
    config.user_facing_summary_kwargs = lambda model: (
        {} if model.casefold().startswith("ling-") else
        {"chat_template_kwargs": {"enable_thinking": False}})
    config.no_thinking_kwargs = lambda _model: {
        "chat_template_kwargs": {"enable_thinking": False}}
    config.omlx_api_key = lambda: ""
    config.omlx_base_url = lambda: endpoint
    endpoints = types.ModuleType("service.config.endpoints")
    endpoints.EndpointConfigurationError = type("EndpointConfigurationError", (ValueError,), {})
    endpoints.Target = type("Target", (), {})
    endpoints.endpoint = lambda: None
    endpoints.is_loopback = lambda value: value == endpoint
    installed[endpoints.__name__] = _attach(endpoints.__name__, endpoints)
    idle = types.ModuleType("service.idle")
    active = set()
    idle.begin = lambda key: active.add(key)
    idle.end = lambda key: active.discard(key)
    idle.in_flight = lambda *_args, **_kwargs: len(active)
    idle.foreground_busy = lambda: bool(active)
    installed[idle.__name__] = _attach(idle.__name__, idle)
    return installed


class CountingClient:
    def __init__(self, inner, manifest, lease: ExclusiveLease):
        self.inner, self.manifest, self.lease = inner, manifest, lease
        self.call_count = 0
        self.email_schema_valid = self.message_schema_valid = False
        self.selected_ids = []

    async def ensure_only(self, model, **_kwargs):
        await self.lease.assert_held()
        if model != self.manifest["model"] or await self.inner.loaded_models() != [model]:
            raise QAError("readiness_unproven")

    async def load(self, *_args, **_kwargs):
        raise QAError("model_mutation_forbidden")

    async def unload(self, *_args, **_kwargs):
        raise QAError("model_mutation_forbidden")

    async def chat(self, model, messages, **kwargs):
        await self.lease.assert_held()
        if model != self.manifest["model"] or self.call_count >= 2:
            raise QAError("completion_budget_exceeded")
        self.call_count += 1
        async with asyncio.timeout(self.manifest["completion_timeout_seconds"]):
            response = await self.inner.chat(model, messages, **kwargs)
        await self.lease.assert_held()
        try:
            choice = response["choices"][0]
            payload = json.loads(choice["message"]["content"])
            candidates = json.loads(messages[1]["content"])
            system = str(messages[0].get("content", ""))
            if choice.get("finish_reason") != "stop":
                return response
            if "Choose the most useful email IDs" in system:
                values = payload.get("prioritize") if isinstance(payload, dict) else None
                valid = isinstance(values, list) and values and all(v in candidates for v in values)
                self.email_schema_valid = bool(valid)
                if valid and values[0] == "0":
                    self.selected_ids.append(self.manifest["fixtures"]["email_ids"][0])
            elif "Select up to three useful" in system:
                valid = (isinstance(payload, dict) and set(payload) == set(candidates)
                         and all(isinstance(v, list) and v
                                 and all(item in candidates[k] for item in v)
                                 for k, v in payload.items()))
                self.message_schema_valid = bool(valid)
                if valid and "0" in payload:
                    self.selected_ids.append(self.manifest["fixtures"]["message_ids"][0])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return response


class LiveSummaryRunner:
    def __init__(self, manifest, credential, service_root, *, lease=None,
                 source_hashes_valid=False, generation="absent"):
        if lease is None:
            raise QAError("external_exclusivity_required")
        if not HEX64.fullmatch(credential):
            raise QAError("invalid_credential")
        self.manifest, self.lease = manifest, lease
        self.source_hashes_valid, self.modules = source_hashes_valid, None
        os.environ["WISP_CREDENTIAL_GENERATION"] = generation
        install_synthetic_adapters(service_root, endpoint=manifest["inference_endpoint"])
        from service.inference.omlx_client import OMLXClient
        self.client = CountingClient(OMLXClient(
            base_url=manifest["inference_endpoint"], api_key=credential,
            timeout=float(manifest["completion_timeout_seconds"])), manifest, lease)

    @property
    def call_count(self):
        return self.client.call_count

    def _load_originals(self):
        if self.modules is None:
            email = importlib.import_module("service.tools.email_tools")
            messages = importlib.import_module("service.tools.imessage_tools")
            brief = importlib.import_module("service.assistant.brief")
            email._client = messages._client = self.client
            self.modules = email, messages, brief
        return self.modules

    async def qualify(self):
        empty = {key: False for key in READINESS_KEYS}
        try:
            await self.lease.assert_held()
            model = self.manifest["model"]
            health = await self.client.inner.health()
            status = await self.client.inner.status()
            loaded = await self.client.inner.loaded_models()
            await self.lease.assert_held()
            idle = _proven_idle(status)
            return {"exclusive_session": True,
                    "model_loaded": health == {"status": "ok"} and loaded == [model],
                    "server_idle": idle}
        except Exception:
            return empty

    async def run(self):
        await self.lease.assert_held()
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
        await self.lease.assert_held()
        expected = self.manifest["expected_selected_ids"]
        selected = list(dict.fromkeys(self.client.selected_ids))
        predicates = {"schemas_valid": self.client.email_schema_valid
                          and self.client.message_schema_valid,
            "daily_model_free": self.call_count == before_daily,
            "daily_ready": isinstance(daily, dict) and daily.get("READY") == "1"
                           and bool(str(daily.get("FULL", "")).strip()),
            "email_selected": expected[0] in selected and bool(email_result.strip()),
            "messages_selected": expected[1] in selected and bool(message_result.strip()),
            "source_hashes_valid": self.source_hashes_valid}
        return selected, predicates, self.call_count


def _proven_idle(status):
    numeric = {"active_requests", "queued_requests", "queue_depth", "in_flight",
               "running_requests"}
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
            return all(visit(item) for item in value)
        return True
    return visit(status) and observed > 0


class OneShotHarness:
    def __init__(self, manifest, identity: ReportIdentity):
        self.manifest, self.identity = manifest, identity
        self.state, self.lock = "ARMED", asyncio.Lock()

    def _report(self, status, reasons, calls, selected, predicates, readiness, started):
        return sanitized_report(self.identity, model=self.manifest["model"], status=status,
            reason_codes=reasons, call_count=calls, selected_ids=selected,
            predicates=predicates, readiness=readiness,
            elapsed_ms=int((time.monotonic() - started) * 1000))

    async def consume(self, readiness: dict[str, bool],
                      run: Callable[[], Awaitable[tuple[list[str], dict[str, bool], int]]]):
        started = time.monotonic()
        empty = {key: False for key in PREDICATE_KEYS}
        async with self.lock:
            if self.state != "ARMED":
                raise QAError("one_shot_spent")
            self.state = "RUNNING"
        try:
            if set(readiness) != READINESS_KEYS or not all(readiness.values()):
                return self._report("BLOCK", ["readiness_unproven"], 0, [], empty,
                                    readiness, started)
            try:
                selected, predicates, calls = await asyncio.wait_for(
                    run(), timeout=self.manifest["run_timeout_seconds"])
            except asyncio.CancelledError:
                raise
            except Exception:
                return self._report("FAIL", ["bounded_run_failed"], 0, [], empty,
                                    readiness, started)
            allowed = {value for values in self.manifest["fixtures"].values()
                       for value in values}
            if any(value not in allowed for value in selected):
                return self._report("FAIL", ["unknown_fixture_id"], calls, [], empty,
                                    readiness, started)
            valid = set(predicates) == PREDICATE_KEYS and all(
                type(value) is bool for value in predicates.values())
            passed = (calls == 2 and selected == self.manifest["expected_selected_ids"]
                      and valid and all(predicates.values()))
            return self._report("PASS" if passed else "FAIL",
                [] if passed else ["predicate_failed"], calls, selected,
                predicates if valid else empty, readiness, started)
        finally:
            self.state = "TERMINAL"
