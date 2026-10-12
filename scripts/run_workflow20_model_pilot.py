#!/usr/bin/env python3
"""Offline preparation for an eventual model-assisted Workflow20 pilot.

Runs the candidate's real /agent function and streaming body with test_mode=False
in a disposable, guarded child. Model methods and explicitly declared tool
adapters are synthetic. This exercises request orchestration, not native tool
semantics, engine authentication, answer accuracy, or release performance.
Live execution is deliberately unavailable; an external grant cannot enable it
merely by being supplied to this preparation script.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import datetime as domain_datetime
import hashlib
import inspect
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "wisp.workflow20.model_pilot.preparation/1"
MODE = "offline_fake_transport_preparation"
MAX_CASES = 25
MAX_CALLS_PER_TURN = 2
MAX_TURN_SECONDS = 30
MAX_SUITE_SECONDS = 1200
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 1000
MAX_JSON_BYTES = 1024 * 1024
MAX_TEXT = 64 * 1024


class BoundaryViolation(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def execution_input(case, scenario):
    """Project execution inputs; oracle facts and expected outcomes never cross."""
    return {"case_id": case["id"], "turns": [dict(role=t["role"], text=t["text"]) for t in case["turns"]],
            "scenario": {"clock": scenario["clock"], "timezone": scenario["timezone"],
                         "sources": scenario["sources"]}}


def validate_input(payload):
    if not isinstance(payload, dict) or set(payload) - {
            "case_id", "turns", "scenario", "transport", "adapters", "approval", "max_calls"}:
        raise ValueError("Unknown execution input field; expectations are not execution inputs")
    if not isinstance(payload.get("case_id"), str) or not payload["case_id"]:
        raise ValueError("case_id is required")
    turns = payload.get("turns")
    if not isinstance(turns, list) or not 1 <= len(turns) <= 8 or turns[-1].get("role") != "user":
        raise ValueError("Provide 1–8 conversation turns ending with a user request")
    for turn in turns:
        if set(turn) != {"role", "text"} or turn["role"] not in {"user", "assistant"}:
            raise ValueError("Invalid conversation turn")
        if not isinstance(turn["text"], str) or not 0 < len(turn["text"]) <= MAX_TEXT:
            raise ValueError("Invalid conversation text")
    scenario = payload.get("scenario", {})
    if set(scenario) != {"clock", "timezone", "sources"}:
        raise ValueError("Scenario accepts clock/timezone/sources only, never oracle facts")
    instant = domain_datetime.datetime.fromisoformat(scenario["clock"])
    zone = ZoneInfo(scenario["timezone"])
    if instant.utcoffset() is None or instant.astimezone(zone).utcoffset() != instant.utcoffset():
        raise ValueError("Scenario clock must use its timezone's correct offset")
    if not isinstance(scenario["sources"], dict):
        raise ValueError("sources must be an object")
    calls = payload.get("max_calls", MAX_CALLS_PER_TURN)
    if type(calls) is not int or not 1 <= calls <= MAX_CALLS_PER_TURN:
        raise ValueError("Nested transport budget is 1–2 calls per turn")
    if payload.get("approval", "deny") not in {"deny", "approve_synthetic"}:
        raise ValueError("Only denial or exact synthetic effects can be approved")
    responses = payload.get("transport", [])
    if not isinstance(responses, list) or len(responses) > MAX_CALLS_PER_TURN * 8:
        raise ValueError("Fake responses must be a bounded list")
    for response in responses:
        if not isinstance(response, dict) or set(response) - {"message", "delay_seconds"}:
            raise ValueError("Invalid fake transport response")
        if not isinstance(response.get("message"), dict):
            raise ValueError("Fake response requires an explicit message")
        delay = response.get("delay_seconds", 0)
        if type(delay) not in (int, float) or not 0 <= delay <= MAX_TURN_SECONDS * 2:
            raise ValueError("Invalid synthetic transport delay")
    adapters = payload.get("adapters", {})
    if not isinstance(adapters, dict):
        raise ValueError("adapters must be an object")
    for name, adapter in adapters.items():
        if not isinstance(name, str) or not isinstance(adapter, dict):
            raise ValueError("Invalid adapter")
        if adapter.get("kind") == "source":
            if set(adapter) != {"kind", "source", "result"} or adapter["source"] not in scenario["sources"]:
                raise ValueError("Source adapter needs one declared synthetic source")
        elif adapter.get("kind") == "effect":
            if set(adapter) != {"kind", "args", "result"} or not isinstance(adapter["args"], dict):
                raise ValueError("Effect adapter requires exact synthetic arguments")
        else:
            raise ValueError("Adapter kind must be source or effect")
        if not isinstance(adapter["result"], str) or len(adapter["result"]) > MAX_TEXT:
            raise ValueError("Adapter result must be bounded text")
    if len(canonical(payload).encode()) > MAX_JSON_BYTES:
        raise ValueError("Execution input exceeds 1 MiB")
    return payload


class Ledger:
    """One child-local order for approvals, dispatches, receipts, and sentinels."""
    def __init__(self):
        self.events = []
        self._lock = threading.Lock()
        self._approved = {}

    def emit(self, kind, **values):
        with self._lock:
            if len(self.events) >= MAX_EVENTS:
                raise BoundaryViolation("Child event budget exceeded")
            row = {"sequence": len(self.events), "monotonic_ns": time.monotonic_ns(), "kind": kind, **values}
            self.events.append(row)
            return row

    def accepted(self, request_id, action_id, tool, args, approved):
        frozen = json.loads(canonical(args))
        row = self.emit("approval_accepted", request_id=request_id, action_id=action_id,
                        tool=tool, args=frozen, args_sha256=digest(frozen), approved=approved)
        if approved:
            with self._lock:
                self._approved.setdefault((tool, digest(frozen)), []).append(row)

    def claim(self, tool, args):
        with self._lock:
            choices = self._approved.get((tool, digest(args)), [])
            if not choices:
                raise BoundaryViolation("Synthetic effect lacks exact accepted approval")
            return choices.pop(0)


class FakeTransport:
    """All OMLX instances, including summaries, share one bounded counter."""
    def __init__(self, responses, ledger, maximum):
        self.responses = list(responses)
        self.ledger = ledger
        self.maximum = maximum
        self.turn_calls = 0
        self.total_calls = 0

    def begin_turn(self):
        self.turn_calls = 0

    async def message(self, surface, model, messages, **kwargs):
        self.turn_calls += 1
        self.total_calls += 1
        self.ledger.emit("fake_transport_call", surface=surface, model=model,
                         turn_call=self.turn_calls, total_call=self.total_calls,
                         input_sha256=digest({"messages": messages, "kwargs": kwargs}))
        if self.turn_calls > self.maximum:
            self.ledger.emit("boundary_blocked", surface="nested_transport_budget")
            raise BoundaryViolation("Nested transport call budget exceeded")
        if not self.responses:
            raise BoundaryViolation("No explicit fake response remains")
        response = self.responses.pop(0)
        await asyncio.sleep(response.get("delay_seconds", 0))
        return json.loads(canonical(response["message"]))

    @contextmanager
    def install(self, client_class):
        transport = self
        def init(client, *args, **kwargs):
            client.managed = True
            client.target = None
            client.base_url = "http://offline.invalid"
            client.endpoint_name = "offline_fake"
            client._keep_warm = set()
        async def chat(client, model, messages, **kwargs):
            from service.inference import engine_epoch
            engine_epoch.begin()
            try:
                message = await transport.message("chat", model, messages, **kwargs)
                return {"choices": [{"message": message, "finish_reason": "stop"}]}
            finally:
                engine_epoch.end()
        async def stream_events(client, model, messages, **kwargs):
            from service.inference import engine_epoch
            engine_epoch.begin()
            try:
                message = await transport.message("stream_events", model, messages, **kwargs)
                if message.get("content"):
                    yield {"kind": "content", "text": message["content"]}
                yield {"kind": "final", "message": message}
            finally:
                engine_epoch.end()
        async def loaded(client):
            from service.config import role_to_model
            return [role_to_model("fast")]
        async def unavailable(client, *args, **kwargs):
            transport.ledger.emit("boundary_blocked", surface="engine_lifecycle")
            raise BoundaryViolation("No load/unload or readiness fallback is permitted")
        async def close(client):
            pass
        with ExitStack() as stack:
            for name, value in {"__init__": init, "chat": chat, "stream_events": stream_events,
                                "loaded_models": loaded, "load": unavailable, "unload": unavailable,
                                "ensure_only": unavailable, "health": unavailable, "aclose": close}.items():
                stack.enter_context(patch.object(client_class, name, value))
            yield


@contextmanager
def domain_clock(scenario):
    """Patch only date/planning modules; never global time or engine/auth clocks."""
    instant = domain_datetime.datetime.fromisoformat(scenario["clock"])
    stamp, zone = instant.timestamp(), ZoneInfo(scenario["timezone"])
    real_datetime, real_date = domain_datetime.datetime, domain_datetime.date
    class DateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(stamp, tz or zone) if tz else cls.fromtimestamp(stamp, zone).replace(tzinfo=None)
        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(stamp, domain_datetime.timezone.utc).replace(tzinfo=None)
    class Date(real_date):
        @classmethod
        def today(cls):
            return DateTime.now().date()
    # A proxy preserves function-local `dt.datetime` users without modifying
    # the shared stdlib datetime module, which engine/auth also import.
    import types
    proxy = types.SimpleNamespace(**vars(domain_datetime))
    proxy.datetime, proxy.date = DateTime, Date
    names = {"service.router.router", "service.workflows.compiler", "service.workflows.reads",
             "service.workflows.workflow20", "service.tools.timeranges", "service.tasks.parser",
             "service.tasks.temporal", "service.tasks.reply_engine", "service.calendar_planning",
             "service.tools.schedule_extras", "service.tools.assistant_tools", "service.tools.web_tools"}
    with ExitStack() as stack:
        for name in names:
            module = sys.modules.get(name)
            if module is None:
                continue
            for attr, value in list(vars(module).items()):
                if value is real_datetime:
                    stack.enter_context(patch.object(module, attr, DateTime))
                elif value is real_date:
                    stack.enter_context(patch.object(module, attr, Date))
                elif value is domain_datetime:
                    stack.enter_context(patch.object(module, attr, proxy))
        yield


def _refuse(ledger, surface):
    def refused(*args, **kwargs):
        ledger.emit("boundary_blocked", surface=surface)
        raise BoundaryViolation(f"Offline pilot denied {surface}")
    return refused


@contextmanager
def sqlite_boundary(scratch, ledger):
    """SQLite opens in C: gate every connection before importing service state."""
    original = sqlite3.connect
    def connect(database, *args, **kwargs):
        if kwargs.get("uri") or not isinstance(database, (str, os.PathLike)):
            return _refuse(ledger, "sqlite_path")()
        path = Path(database).resolve()
        if not path.is_relative_to(scratch):
            return _refuse(ledger, "sqlite_path")()
        return original(database, *args, **kwargs)
    with patch.object(sqlite3, "connect", connect), patch.object(sqlite3.dbapi2, "connect", connect):
        yield


def _trace(stack, ledger, owner, name):
    original = getattr(owner, name)
    if inspect.iscoroutinefunction(original):
        async def wrapped(*args, **kwargs):
            ledger.emit("production_branch", function=f"{owner.__name__}.{name}")
            return await original(*args, **kwargs)
    else:
        def wrapped(*args, **kwargs):
            ledger.emit("production_branch", function=f"{owner.__name__}.{name}")
            return original(*args, **kwargs)
    stack.enter_context(patch.object(owner, name, wrapped))


@contextmanager
def synthetic_tools(adapters, ledger):
    from service.tools.registry import REGISTRY
    safe_reads = {"assistant_read", "fs_read", "email_read", "messages_read", "notes_read", "web_read"}
    replacements = {}
    for name, tool in REGISTRY.items():
        adapter = adapters.get(name)
        if adapter and (tool.unavailable_reason or name in {"route_to_arrive", "unsubscribe", "update_event"}):
            raise ValueError(f"Cannot simulate unavailable capability {name}")
        if adapter and adapter["kind"] == "source" and tool.category not in safe_reads:
            raise ValueError(f"Source adapter cannot bypass effect policy for {name}")
        if adapter and adapter["kind"] == "effect" and tool.category in safe_reads:
            raise ValueError(f"Read tool cannot be declared an approved effect: {name}")
        def invoke(*args, _name=name, _adapter=adapter, **kwargs):
            if args:
                return _refuse(ledger, f"positional_tool:{_name}")()
            if not _adapter:
                return _refuse(ledger, f"undeclared_tool:{_name}")()
            frozen = json.loads(canonical(kwargs))
            if _adapter["kind"] == "source":
                ledger.emit("synthetic_source_read", tool=_name, source=_adapter["source"],
                            args=frozen, result_sha256=digest(_adapter["result"]),
                            tool_semantics_qualified=False)
            else:
                if frozen != _adapter["args"]:
                    return _refuse(ledger, f"effect_payload:{_name}")()
                approved = ledger.claim(_name, frozen)
                ledger.emit("synthetic_effect_invoked", tool=_name, args=frozen,
                            approval_sequence=approved["sequence"], request_id=approved["request_id"],
                            action_id=approved["action_id"])
                ledger.emit("synthetic_effect_receipt", tool=_name, args=frozen,
                            receipt_id=f"fake:{len(ledger.events)}", native_effect=False)
            return _adapter["result"]
        replacements[name] = replace(tool, func=invoke)
    unknown = set(adapters) - set(REGISTRY)
    if unknown:
        raise ValueError(f"Unknown synthetic tool adapters: {sorted(unknown)}")
    with patch.dict(REGISTRY, replacements):
        yield


async def _consume(main, payload, ledger, transport):
    sid = main.store.create_session()
    for turn in payload["turns"][:-1]:
        main.store.add_turn(sid, turn["role"], turn["text"])
    transport.begin_turn()
    response = await main.agent({"prompt": payload["turns"][-1]["text"], "session_id": sid,
                                 "test_mode": False, "debug": False})
    events = []
    try:
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            for line in chunk.splitlines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if len(events) >= MAX_EVENTS or len(canonical(event)) > MAX_JSON_BYTES:
                    raise BoundaryViolation("SSE output budget exceeded")
                events.append(event)
                if event.get("type") == "confirm":
                    adapter = payload.get("adapters", {}).get(event.get("tool"), {})
                    approved = (payload.get("approval") == "approve_synthetic"
                                and adapter.get("kind") == "effect" and event.get("args") == adapter["args"])
                    ledger.emit("approval_requested", request_id=event.get("request_id"),
                                action_id=event.get("id"), tool=event.get("tool"), args=event.get("args"))
                    answer = await main.approve({"session_id": sid, "request_id": event.get("request_id"),
                                                 "action_id": event["id"], "approved": approved, "scope": "once"})
                    if answer.get("ok") is not True:
                        raise BoundaryViolation("Production approval endpoint refused matching request")
    finally:
        await response.body_iterator.aclose()
    if not any(e.get("type") == "done" for e in events):
        raise BoundaryViolation("Agent stream ended without done")
    return events


def child_run(payload, scratch):
    """Called only by the owned child, never in an already-imported service."""
    validate_input(payload)
    if any(name == "service" or name.startswith("service.") for name in sys.modules):
        raise RuntimeError("Service imported before isolation")
    scratch = Path(scratch).resolve()
    for directory in ("state", "codex", "air", "tmp"):
        (scratch / directory).mkdir(exist_ok=True)
    os.environ.update(WISP_HOME=str(scratch / "state"), CODEX_HOME=str(scratch / "codex"),
                      WISPAIR_HOME=str(scratch / "air"), TMPDIR=str(scratch / "tmp"),
                      TZ=payload["scenario"]["timezone"], PYTHONDONTWRITEBYTECODE="1")
    if hasattr(time, "tzset"):
        time.tzset()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    from scripts.release_performance import EffectGuard, FsPolicy
    ledger = Ledger()
    class PreparationFsPolicy(FsPolicy):
        def read_allowed(self, path, real=None):
            real = os.path.realpath(path) if real is None else real
            # The release guard only defaults to denying real HOME. This
            # preparation also denies external-volume and other-user data.
            return (self._under(real, self.read_roots) or self._under(real, self.write_roots)
                    or real in self.read_exact)
    fs = PreparationFsPolicy(home=Path.home(), read_roots=[ROOT, sys.prefix, sys.base_prefix, sys.exec_prefix,
                                                         "/usr/share/zoneinfo", "/usr/share/locale"],
                  read_exact=["/dev/null", "/etc/localtime", "/etc/timezone"],
                  write_roots=[scratch], labels={"read_roots": ["candidate", "interpreter"],
                                                "write_roots": ["disposable_child"]})
    guard = EffectGuard(ledger.emit, exec_grammar={}, exec_aliases={}, allowed_ports=set(), engine_ops=(), fs=fs)
    result = {"schema": SCHEMA, "mode": MODE, "case_id": payload["case_id"],
              "verdict": "PREPARATION_ONLY", "release_authorized": False,
              "answer_grade": "UNAVAILABLE", "engine_transport_qualified": False,
              "native_tools_qualified": False, "http_auth_boundary_qualified": False,
              "startup_qualified": False, "test_mode": False,
              "policy_fixture": "per_action_confirmation_with_isolated_empty_grants",
              "limitations": ["Fake model methods; no model inference occurred.",
                              "Declared tool functions are synthetic adapters; their production semantics are untested.",
                              "No independent prose-to-fact grader is implemented.",
                              "Endpoint function/body path only; ASGI middleware and startup were not exercised.",
                              "Python guard cannot sandbox arbitrary native extension I/O; only reviewed adapters execute."]}
    try:
        with guard, sqlite_boundary(scratch, ledger), ExitStack() as stack:
            # No exact real-home configuration/credential exception is allowed
            # in preparation. Import config only after the guard is installed.
            from service import config
            stack.enter_context(patch.object(config, "OMLX_SETTINGS", scratch / "absent-settings.json"))
            stack.enter_context(patch.object(config, "OMLX_MODEL_SETTINGS", scratch / "absent-models.json"))
            stack.enter_context(patch.object(config, "omlx_api_key", _refuse(ledger, "credential")))
            stack.enter_context(patch.object(config, "_credential", _refuse(ledger, "credential")))
            from service.config import credentials
            stack.enter_context(patch.object(credentials, "resolve", _refuse(ledger, "credential")))
            from service.inference.omlx_client import OMLXClient
            transport = FakeTransport(payload.get("transport", []), ledger, payload.get("max_calls", 2))
            stack.enter_context(transport.install(OMLXClient))
            from service import main
            main.client = OMLXClient()
            from service.safety import policy
            # Use the product's own mode setters in this disposable process.
            # Packaged full_access must not bypass the ordering control.
            policy.set_full_access(False)
            policy.set_read_only(False)
            from service.memory import identity
            stack.enter_context(patch.object(identity, "identity_prompt_block",
                                            lambda **kwargs: "Synthetic test user; no real account identity loaded."))
            # Lifespan is intentionally not started: no scheduler, unload,
            # warmup, MCP, memory daemon, or native bridge may run.
            from service.agent.approver import InteractiveApprover
            original_resolve = InteractiveApprover.resolve
            def resolve(approver, action_id, approved, scope="once"):
                action = json.loads(canonical(approver._actions.get(action_id, {})))
                accepted = original_resolve(approver, action_id, approved, scope)
                if accepted:
                    ledger.accepted(approver.request_id, action_id, action.get("tool"), action.get("args", {}), approved)
                return accepted
            stack.enter_context(patch.object(InteractiveApprover, "resolve", resolve))
            stack.enter_context(synthetic_tools(payload.get("adapters", {}), ledger))
            from service.workflows import reads
            from service.tasks import reply_engine
            for owner, name in ((main, "prepare_turn"), (main, "route"), (main, "run_agent"),
                                (reads, "compile_read"), (reply_engine, "prepare_task_turn_async")):
                _trace(stack, ledger, owner, name)
            stamp = domain_datetime.datetime.fromisoformat(payload["scenario"]["clock"]).isoformat()
            from service.memory import prompt_blocks
            def now_line(**kwargs):
                return f"\nCurrent local time: {stamp}"
            stack.enter_context(patch.object(main, "now_line", now_line))
            stack.enter_context(patch.object(prompt_blocks, "now_line", now_line))
            with domain_clock(payload["scenario"]):
                result["events"] = asyncio.run(_consume(main, payload, ledger, transport))
            result["fake_transport_calls"] = transport.total_calls
            result["orchestration_completed"] = not any(e.get("type") == "error" for e in result["events"])
    except BaseException as exc:
        result.update(orchestration_completed=False, error=f"{type(exc).__name__}: {exc}"[:1000])
    result["ledger"] = ledger.events
    result["boundary_clean"] = not any(e["kind"] in {"effect_blocked", "boundary_blocked"} for e in ledger.events)
    result["preparation_check_pass"] = result.get("orchestration_completed", False) and result["boundary_clean"]
    return result


def run_offline(payload, *, timeout=MAX_TURN_SECONDS):
    """External monotonic watchdog; kills/reaps only its task-owned process group."""
    validate_input(payload)
    if type(timeout) not in (int, float) or not 0 < timeout <= MAX_TURN_SECONDS:
        raise ValueError("Child deadline must be greater than zero and at most 30 seconds")
    with tempfile.TemporaryDirectory(prefix="wisp-model-pilot-") as directory:
        scratch = Path(directory).resolve()
        source = scratch / "input.json"
        source.write_text(canonical(payload))
        stdout, stderr = scratch / "stdout", scratch / "stderr"
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        started = time.monotonic()
        killed = None
        with stdout.open("wb") as out, stderr.open("wb") as err:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_child",
                                      "--input", str(source), "--scratch", str(scratch)],
                                     cwd=ROOT, env=env, stdout=out, stderr=err, start_new_session=True)
            try:
                while child.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        killed = "deadline"
                    elif stdout.stat().st_size + stderr.stat().st_size > MAX_OUTPUT_BYTES:
                        killed = "output_limit"
                    if killed:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        break
                    try:
                        child.wait(timeout=min(0.05, timeout - elapsed))
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait()
        if time.monotonic() - started >= timeout:
            killed = killed or "deadline"
        base = {"schema": SCHEMA, "mode": MODE, "case_id": payload["case_id"],
                "verdict": "PREPARATION_ONLY", "release_authorized": False,
                "elapsed_seconds": time.monotonic() - started,
                "child_pid": child.pid, "child_reaped": True, "child_returncode": child.returncode}
        if killed:
            return {**base, "preparation_check_pass": False, "orchestration_completed": False,
                    "watchdog": killed, "answer_grade": "UNAVAILABLE"}
        try:
            raw = stdout.read_bytes()
            if len(raw) > MAX_OUTPUT_BYTES:
                raise ValueError("Child output limit exceeded")
            result = json.loads(raw)
            if result.get("schema") != SCHEMA or result.get("case_id") != payload["case_id"]:
                raise ValueError("Child result identity mismatch")
            if child.returncode != 0:
                raise ValueError("Child did not exit successfully")
            return {**result, **base}
        except (ValueError, OSError) as exc:
            return {**base, "preparation_check_pass": False, "orchestration_completed": False,
                    "error": f"Invalid child result: {exc}", "stderr": stderr.read_bytes()[:2000].decode(errors="replace")}


def run_offline_suite(payloads, *, timeout=MAX_TURN_SECONDS, wall_timeout=MAX_SUITE_SECONDS):
    """Bound explicitly supplied fake cases; this is not a live-pilot entry."""
    if not isinstance(payloads, list) or not 1 <= len(payloads) <= MAX_CASES:
        raise ValueError("An offline suite contains 1–25 explicit synthetic cases")
    if type(wall_timeout) not in (int, float) or not 0 < wall_timeout <= MAX_SUITE_SECONDS:
        raise ValueError("Suite wall deadline must be greater than zero and at most 1200 seconds")
    if type(timeout) not in (int, float) or not 0 < timeout <= MAX_TURN_SECONDS:
        raise ValueError("Child deadline must be greater than zero and at most 30 seconds")
    for item in payloads:
        validate_input(item)
    if len({item["case_id"] for item in payloads}) != len(payloads):
        raise ValueError("Offline case identities must be unique")
    started, rows = time.monotonic(), []
    for item in payloads:
        remaining = wall_timeout - (time.monotonic() - started)
        if remaining <= 0:
            break
        rows.append(run_offline(item, timeout=min(timeout, remaining)))
    return {"schema": SCHEMA, "mode": MODE, "verdict": "PREPARATION_ONLY", "release_authorized": False,
            "answer_grade": "UNAVAILABLE", "requested_cases": len(payloads), "cases": rows,
            "unexecuted_case_ids": [item["case_id"] for item in payloads[len(rows):]],
            "preparation_check_pass": len(rows) == len(payloads) and all(row["preparation_check_pass"] for row in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "offline", "live", "_child"])
    parser.add_argument("--input", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--packet", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "live":
        parser.error("Live execution is disabled; preparation ACK is not a runtime/resource grant")
    if args.mode == "_child":
        if args.input is None or args.scratch is None:
            parser.error("Internal child needs input and scratch")
        payload = json.loads(args.input.read_text())
        print(canonical(child_run(payload, args.scratch)))
        return 0
    if args.mode == "offline":
        if args.input is None:
            parser.error("offline needs an explicit synthetic input file")
        data = json.loads(args.input.read_text())
        result = run_offline_suite(data) if isinstance(data, list) else run_offline(data)
        print(json.dumps(result, indent=2))
        return 0 if result["preparation_check_pass"] else 1
    if args.packet is None:
        parser.error("prepare needs --packet; no model is invoked")
    packet = json.loads(args.packet.read_text())
    cases = packet.get("cases", [])
    if not 1 <= len(cases) <= MAX_CASES:
        parser.error("Preparation packet must contain 1–25 cases")
    print(json.dumps({"schema": SCHEMA, "mode": "manifest_only", "verdict": "PREPARATION_ONLY",
                      "release_authorized": False, "packet_sha256": hashlib.sha256(args.packet.read_bytes()).hexdigest(),
                      "case_ids": [case["id"] for case in cases], "model_calls": 0,
                      "runtime_enabled": False, "answer_grade": "UNAVAILABLE"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
