"""Isolated metadata journal and offline replay checks. No live state or tools."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import AsyncMock

# Even direct invocation cannot write the user's journal.
_scratch = tempfile.TemporaryDirectory(prefix="wisp-diagnostic-tests-")
os.environ["WISP_HOME"] = _scratch.name

import pytest
from service import diagnostics


def read(root, trace):
    return json.loads((root / "diagnostics" / (trace.id + ".json")).read_text())


def test_content_allowlist_and_terminal_failure(tmp_path):
    trace = diagnostics.Trace("agent", root=tmp_path)
    secret = "private-prompt-Bearer-sk-do-not-save"
    trace.observe({"type": "routed", "role": "agent", "route_source": "rules", "reason": secret})
    trace.observe({"type": "tool_call", "name": secret, "args": {"secret": secret}})
    trace.observe({"type": "error", "message": secret, "detail": secret})
    trace.event("plan", tasks=2, revision=3, calendar_ready=False, title=secret)
    trace.finish()
    trace.finish("completed")
    payload = read(tmp_path, trace)
    assert secret not in json.dumps(payload)
    assert payload["status"] == "failed"
    assert payload["events"][-1]["stage"] == "failed"
    assert payload["events"][1]["route_role"] == "agent"
    assert (tmp_path / "diagnostics").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "diagnostics" / (trace.id + ".json")).stat().st_mode & 0o777 == 0o600


def test_disabled_trace_and_nested_span_never_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    trace = diagnostics.Trace("agent", enabled=False, root=tmp_path)
    with diagnostics.use(trace):
        with diagnostics.span("native"):
            diagnostics.event("result", ok=True)
    trace.finish()
    assert not list(tmp_path.iterdir())


def test_bounds_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_TRACES", 3)
    for _ in range(5):
        trace = diagnostics.Trace("today", root=tmp_path)
        trace.finish()
    assert len(list((tmp_path / "diagnostics").glob("*.json"))) == 3
    for _ in range(140):
        trace.event("tool_result")
    payload = read(tmp_path, trace)
    assert len(payload["events"]) == 128
    assert payload["events"][0]["stage"] == "started"
    assert payload["events_dropped"] > 0
    # Data is complete JSON on disk, requiring neither the Trace nor service.
    assert json.loads(json.dumps(payload))["trace_id"] == trace.id


def test_disk_failure_and_symlink_are_fail_open(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("occupied")
    trace = diagnostics.Trace("agent", root=bad)
    trace.finish("cancelled")
    assert trace.status == "cancelled"
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "diagnostics").symlink_to(outside, target_is_directory=True)
    diagnostics.Trace("agent", root=root).finish()
    assert not list(outside.iterdir())


def test_async_context_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    async def work(index):
        trace = diagnostics.Trace("agent", root=tmp_path)
        with diagnostics.use(trace):
            await asyncio.sleep(0)
            with diagnostics.span("native"):
                diagnostics.event("result", revision=index)
        trace.finish()
        return read(tmp_path, trace)
    async def run():
        return await asyncio.gather(*(work(i) for i in range(6)))
    payloads = asyncio.run(run())
    assert len({p["trace_id"] for p in payloads}) == 6
    for i, p in enumerate(payloads):
        assert [e["revision"] for e in p["events"] if "revision" in e] == [i]
        assert any(e.get("component") == "native" for e in p["events"])


def offline_module():
    script = Path(__file__).resolve().parents[1] / "scripts/replay_diagnostic.py"
    spec = importlib.util.spec_from_file_location("fixture_replay", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_replay_fixed_clock_without_effects(tmp_path, monkeypatch):
    import socket
    import subprocess
    def forbidden(*args, **kw):
        raise AssertionError("Replay attempted an external effect")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    module = offline_module()
    from service.assistant.today import build_plan
    capture = dict(schema_version=1, planner_fingerprint=diagnostics.planner_fingerprint(), day="2026-10-04",
                   timezone="UTC", now=1791072000, tasks=[], commitments=[],
                   preferences={}, sources={})
    capture["expected_plan"] = build_plan(capture["day"], capture["timezone"], [], [], {}, {}, now=capture["now"])
    report = {"schema_version": 1, "details": {"replay": capture}}
    assert module.replay(report)["matches_capture"] is True
    capture["expected_plan"]["warnings"] = []
    assert module.replay(report)["matches_capture"] is False
    capture["planner_fingerprint"] = "wrong"
    with pytest.raises(ValueError, match="version differs"):
        module.replay(report)
    capture["planner_fingerprint"] = diagnostics.planner_fingerprint()
    capture["tasks"] = [{}] * 1001
    with pytest.raises(ValueError, match="1000"):
        module.replay(report)


def test_timeline_playback_is_data_only():
    result = offline_module().replay({"schema_version": 1, "events": [{"stage": "send_email"}], "status": "failed"})
    assert result["mode"] == "timeline"
    assert "execution is unavailable" in result["notice"]


def test_cancelled_background_span(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    with pytest.raises(asyncio.CancelledError):
        with diagnostics.span("daily_brief") as trace:
            raise asyncio.CancelledError()
    assert read(tmp_path, trace)["status"] == "cancelled"


def test_native_unknown_and_failed_are_not_success(tmp_path, monkeypatch):
    from service.assistant import outbox
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    async def fixture(*args, **kw):
        return {"ok": False, "status": "unknown", "error": "PRIVATE-NATIVE-DETAIL"}
    monkeypatch.setattr(outbox, "_request", fixture)
    result = asyncio.run(outbox.request("create_calendar_event", {"title": "PRIVATE-NATIVE-TITLE"}))
    assert result["status"] == "unknown"
    payload = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text())
    assert payload["status"] == "unknown"
    assert any(e.get("native_outcome") == "unknown" for e in payload["events"])
    assert "PRIVATE" not in json.dumps(payload)
    async def disconnected(*args, **kw):
        return {"ok": False, "error": "App disconnected"}
    monkeypatch.setattr(outbox, "_request", disconnected)
    asyncio.run(outbox.request("create_calendar_event", {}))
    payloads = [json.loads(p.read_text()) for p in (tmp_path / "diagnostics").glob("*.json")]
    assert any(p["status"] == "failed" for p in payloads)


def test_unavailable_sources_not_reported_ready(tmp_path, monkeypatch):
    from service.assistant import sync_status
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    async def fixture(*args, **kw):
        return {"syncing": False, "sources": [{"id": "calendar", "state": "unavailable", "reason": "PRIVATE-SOURCE-DETAIL"},
                                               {"id": "email", "state": "disabled"}]}
    monkeypatch.setattr(sync_status, "_ensure_sources", fixture)
    asyncio.run(sync_status.ensure_sources(["calendar", "email"]))
    payload = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text())
    assert any(e.get("source_state") == "unavailable" for e in payload["events"])
    assert any(e.get("source_state") == "disabled" for e in payload["events"])
    assert payload["events"][-2]["ok"] is False
    assert "PRIVATE" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Retention: polling kinds must never evict interactive traces.
# ---------------------------------------------------------------------------

def journal_files(root):
    return sorted((root / "diagnostics").glob("*.json"), key=lambda p: p.stat().st_mtime)


def kinds(root):
    return [json.loads(p.read_text())["kind"] for p in journal_files(root)]


def test_retention_constants_document_the_real_rule():
    assert diagnostics.MAX_TRACES == 100
    assert diagnostics.MAX_BACKGROUND_TRACES == 20
    assert diagnostics.MAX_EVENTS == 128
    assert diagnostics.MAX_AGE == 7 * 86400
    assert diagnostics.BACKGROUND_KINDS == {"today", "source_sync", "daily_brief"}


def test_interactive_traces_keep_the_documented_100_and_evict_oldest(tmp_path):
    first = diagnostics.Trace("agent", root=tmp_path)
    first.finish()
    ids = [first.id]
    for _ in range(100):
        trace = diagnostics.Trace("agent", root=tmp_path)
        trace.finish()
        ids.append(trace.id)
    present = {p.stem for p in (tmp_path / "diagnostics").glob("*.json")}
    assert len(present) == 100
    assert first.id not in present            # oldest evicted, not newest
    assert ids[-1] in present and ids[1] in present


def test_polling_kinds_have_their_own_small_cap(tmp_path):
    for kind in ("today", "source_sync", "daily_brief"):
        for _ in range(25):
            diagnostics.Trace(kind, root=tmp_path).finish()
    counts = {kind: kinds(tmp_path).count(kind) for kind in ("today", "source_sync", "daily_brief")}
    assert counts == {"today": 20, "source_sync": 20, "daily_brief": 20}


def test_today_polling_never_evicts_chat_or_native_traces(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    from service.assistant import today_api
    agent = diagnostics.Trace("agent", root=tmp_path)
    agent.event("tool_call", tool_name="run_shell")
    agent.finish()
    native = diagnostics.Trace("native", root=tmp_path)
    native.finish()
    latest = None
    for _ in range(1000):
        latest = today_api.snapshot("2026-10-05", "UTC")["trace_id"]
    directory = tmp_path / "diagnostics"
    assert (directory / (agent.id + ".json")).exists()
    assert (directory / (native.id + ".json")).exists()
    assert kinds(tmp_path).count("today") == 20
    assert len(list(directory.iterdir())) == 22          # nothing else accumulates
    # "Explain this Today plan" still works from the newest polls.
    newest = json.loads((directory / (latest + ".json")).read_text())
    assert newest["kind"] == "today" and any(e["stage"] == "plan" for e in newest["events"])


def test_the_trace_being_written_is_never_evicted_by_its_own_persist(tmp_path):
    # A clock far ahead of the file timestamps makes every file look expired.
    skewed = diagnostics.Trace("agent", root=tmp_path, clock=lambda: time.time() + 30 * 86400)
    skewed.event("result")
    other = diagnostics.Trace("agent", root=tmp_path, clock=lambda: time.time() + 30 * 86400)
    other.finish()
    names = {p.stem for p in (tmp_path / "diagnostics").glob("*.json")}
    assert names == {other.id}                   # others age out; the writer keeps its own file
    skewed.event("result")
    assert skewed.id in {p.stem for p in (tmp_path / "diagnostics").glob("*.json")}


def test_age_rule_removes_only_expired_traces(tmp_path):
    expired = diagnostics.Trace("agent", root=tmp_path)
    expired.finish()
    recent = diagnostics.Trace("agent", root=tmp_path)
    recent.finish()
    now = time.time()
    os.utime(tmp_path / "diagnostics" / (expired.id + ".json"), (now - 8 * 86400, now - 8 * 86400))
    os.utime(tmp_path / "diagnostics" / (recent.id + ".json"), (now - 6 * 86400, now - 6 * 86400))
    diagnostics.Trace("agent", root=tmp_path).finish()
    names = {p.stem for p in (tmp_path / "diagnostics").glob("*.json")}
    assert expired.id not in names
    assert recent.id in names


def test_retention_never_touches_files_it_did_not_write(tmp_path):
    directory = tmp_path / "diagnostics"
    directory.mkdir(mode=0o700)
    foreign = [directory / ("note-%d.json" % index) for index in range(150)]
    for path in foreign:
        path.write_text("{}")
    (directory / ("a" * 32 + ".json")).write_text("not json")
    for _ in range(3):
        diagnostics.Trace("agent", root=tmp_path).finish()
    assert all(path.exists() for path in foreign)


def test_kinds_are_recovered_from_journals_written_by_an_earlier_process(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_TRACES", 1000)
    monkeypatch.setattr(diagnostics, "MAX_BACKGROUND_TRACES", 1000)
    for _ in range(30):
        diagnostics.Trace("today", root=tmp_path).finish()
    chats = [diagnostics.Trace("agent", root=tmp_path) for _ in range(30)]
    for chat in chats:
        chat.finish()
    monkeypatch.undo()                              # production caps again
    diagnostics._known.clear()                      # a restart forgets everything in memory
    diagnostics.Trace("today", root=tmp_path).finish()
    found = kinds(tmp_path)
    assert found.count("today") == 20
    assert found.count("agent") == 30               # unknown history is never mistaken for polling


def test_equal_timestamps_evict_the_oldest_write_first(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_TRACES", 4)
    real = os.replace
    def same_instant(source, target):
        real(source, target)
        os.utime(target, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    monkeypatch.setattr(diagnostics.os, "replace", same_instant)
    monkeypatch.setattr(diagnostics.time, "time", lambda: 1_700_000_000.0)   # nothing is expired
    ids = []
    for _ in range(12):
        trace = diagnostics.Trace("agent", root=tmp_path, clock=lambda: 1_700_000_000.0)
        trace.finish()
        ids.append(trace.id)
    assert {p.stem for p in (tmp_path / "diagnostics").glob("*.json")} == set(ids[-4:])


# ---------------------------------------------------------------------------
# Input validation and write safety.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../etc/passwd", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n", "g" * 32, "a" * 31 + "/"])
def test_trace_ids_must_be_exactly_32_lowercase_hex(tmp_path, bad):
    with pytest.raises(ValueError):
        diagnostics.Trace("agent", bad, root=tmp_path)
    assert not tmp_path.exists() or not list(tmp_path.rglob("*.json"))
    good = "0123456789abcdef" * 2
    assert diagnostics.Trace("agent", good, root=tmp_path).id == good


def test_counts_accept_only_bounded_ints_and_bools(tmp_path):
    trace = diagnostics.Trace("today", root=tmp_path)
    trace.event("plan", tasks="PRIVATE-STRING", blocks=2.5, warnings=[1], revision=10**12,
                unscheduled=True, calendar_ready=False, reminders_ready=None)
    row = read(tmp_path, trace)["events"][-1]
    assert "PRIVATE" not in json.dumps(row)
    assert "tasks" not in row and "blocks" not in row and "warnings" not in row
    assert "revision" not in row and "reminders_ready" not in row
    assert row["unscheduled"] is True and row["calendar_ready"] is False


@pytest.mark.parametrize("stage", ["send_email", "PRIVATE", "", None, 7, "agent ", "Started"])
def test_unknown_stages_are_never_recorded(tmp_path, stage):
    trace = diagnostics.Trace("agent", root=tmp_path)
    trace.event(stage)
    assert [e["stage"] for e in read(tmp_path, trace)["events"]] == ["started"]


ENUMS = {
    "source": ("calendar", "reminders", "email", "messages", "notes", "browser_history"),
    "source_state": ("ready", "syncing", "unavailable", "disabled"),
    "error_kind": ("TimeoutError", "ConnectionError", "PermissionError", "ValueError", "RuntimeError", "OSError", "KeyError", "HTTPError", "CancelledError"),
    "native_operation": ("create_calendar_event", "delete_calendar_event", "create_reminder", "update_reminder", "complete_reminder", "delete_reminder"),
    "native_outcome": ("succeeded", "failed", "unknown"),
    "component": ("agent", "today", "daily_brief", "source_sync", "native"),
    "route_role": ("fast", "agent", "general", "coding", "reasoning"),
    "route_source": ("rules", "fallback", "default", "semantic", "model"),
}


@pytest.mark.parametrize("key", sorted(ENUMS))
def test_enumerated_fields_accept_only_their_fixed_vocabulary(tmp_path, key):
    trace = diagnostics.Trace("agent", root=tmp_path)
    for bad in ("PRIVATE-VALUE", "", "Ready", 7, None, ["ready"]):
        trace.event("result", **{key: bad})
    for good in ENUMS[key]:
        trace.event("result", **{key: good})
    rows = read(tmp_path, trace)["events"][1:]
    assert "PRIVATE" not in json.dumps(rows)
    assert [r.get(key) for r in rows] == [None] * 6 + list(ENUMS[key])


def test_unknown_kind_is_normalized_to_agent(tmp_path):
    assert diagnostics.Trace("PRIVATE-KIND", root=tmp_path).kind == "agent"


def test_temporary_file_is_removed_even_when_the_replace_fails(tmp_path, monkeypatch):
    def refuse(*args, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(diagnostics.os, "replace", refuse)
    trace = diagnostics.Trace("agent", root=tmp_path)
    trace.event("result")
    directory = tmp_path / "diagnostics"
    assert not list(directory.glob("*.tmp")) and not list(directory.glob("*.json"))


def test_serialization_failure_leaves_no_temporary_file(tmp_path, monkeypatch):
    def refuse(*args, **kw):
        raise ValueError("not serializable")
    monkeypatch.setattr(diagnostics.json, "dump", refuse)
    diagnostics.Trace("agent", root=tmp_path).event("result")
    assert not list((tmp_path / "diagnostics").iterdir())


def test_journal_is_created_exclusively_nofollow_and_private(tmp_path, monkeypatch):
    seen = []
    real = os.open
    def spy(path, flags, mode=0o777, *args, **kw):
        if str(path).endswith(".tmp"):
            seen.append((flags, mode))
        return real(path, flags, mode, *args, **kw)
    monkeypatch.setattr(diagnostics.os, "open", spy)
    diagnostics.Trace("agent", root=tmp_path).finish()
    assert seen
    for flags, mode in seen:
        assert flags & os.O_EXCL and flags & os.O_NOFOLLOW and flags & os.O_CREAT
        assert mode == 0o600


def test_journal_files_are_private_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0)
    try:
        trace = diagnostics.Trace("agent", root=tmp_path)
        trace.finish()
    finally:
        os.umask(old)
    assert (tmp_path / "diagnostics" / (trace.id + ".json")).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "diagnostics").stat().st_mode & 0o777 == 0o700


def test_directory_must_be_a_private_real_directory_owned_by_the_user(tmp_path, monkeypatch):
    for index, mode in enumerate((0o770, 0o707, 0o750, 0o701, 0o755)):
        root = tmp_path / ("loose%d" % index)
        (root / "diagnostics").mkdir(parents=True)
        os.chmod(root / "diagnostics", mode)
        with pytest.raises(OSError):
            diagnostics._directory(root)
        diagnostics.Trace("agent", root=root).finish()
        assert not list((root / "diagnostics").iterdir())
    # A symlink is refused even when its target looks perfectly private.
    target = tmp_path / "private-target"
    target.mkdir(mode=0o700)
    root = tmp_path / "linked"
    root.mkdir()
    (root / "diagnostics").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        diagnostics._directory(root)
    diagnostics.Trace("agent", root=root).finish()
    assert not list(target.iterdir())
    # Not a directory at all.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "diagnostics").write_text("x")
    os.chmod(plain / "diagnostics", 0o600)       # private and owned, but not a directory
    with pytest.raises(OSError):
        diagnostics._directory(plain)
    # A directory owned by someone else is refused.
    good = tmp_path / "owned"
    assert diagnostics._directory(good).is_dir()
    monkeypatch.setattr(diagnostics.os, "getuid", lambda: os.geteuid() + 1)
    with pytest.raises(OSError):
        diagnostics._directory(good)


# ---------------------------------------------------------------------------
# Tool-name lookup must never raise into emit() or finish().
# ---------------------------------------------------------------------------

def test_weird_registry_entries_never_raise_into_the_trace(tmp_path, monkeypatch):
    from service.tools import registry
    shell = types.SimpleNamespace(func=types.SimpleNamespace(__module__="service.tools.shell"))
    monkeypatch.setitem(registry.REGISTRY, "no_module", types.SimpleNamespace(func=types.SimpleNamespace(__module__=None)))
    monkeypatch.setitem(registry.REGISTRY, "no_func", types.SimpleNamespace())
    monkeypatch.setitem(registry.REGISTRY, "plain_object", object())
    monkeypatch.setitem(registry.REGISTRY, "Bad-Name", shell)
    monkeypatch.setitem(registry.REGISTRY, "outside_module", types.SimpleNamespace(func=types.SimpleNamespace(__module__="evil.tools")))
    monkeypatch.setitem(registry.REGISTRY, "good_tool", shell)
    trace = diagnostics.Trace("agent", root=tmp_path)
    for name in ("no_module", "no_func", "plain_object", "Bad-Name", "outside_module", "missing", None, 7, ["x"]):
        trace.observe({"type": "tool_call", "name": name})
    trace.observe({"type": "tool_call", "name": "good_tool"})
    trace.finish()
    rows = [e for e in read(tmp_path, trace)["events"] if e["stage"] == "tool_call"]
    assert [r.get("tool_name") for r in rows] == [None] * 9 + ["good_tool"]


def test_a_broken_registry_cannot_fail_a_turn(tmp_path, monkeypatch):
    from service.tools import registry
    class Hostile(dict):
        def get(self, *args):
            raise RuntimeError("registry exploded")
    monkeypatch.setattr(registry, "REGISTRY", Hostile())
    trace = diagnostics.Trace("agent", root=tmp_path)
    trace.event("tool_call", tool_name="run_shell")
    trace.finish()
    assert trace.status == "completed"
    assert "tool_name" not in json.dumps(read(tmp_path, trace))


# ---------------------------------------------------------------------------
# Offline replay input limits.
# ---------------------------------------------------------------------------

def run_replay(monkeypatch, capsys, path):
    monkeypatch.setattr(sys, "argv", ["replay_diagnostic.py", str(path)])
    code = offline_module().main()
    return code, json.loads(capsys.readouterr().out)


def test_replay_rejects_oversized_reports_before_parsing(tmp_path, monkeypatch, capsys):
    big = tmp_path / "big.json"
    big.write_text('{"schema_version": 1, "pad": "' + "x" * (2 * 1024 * 1024) + '"}')
    code, out = run_replay(monkeypatch, capsys, big)
    assert code == 2 and "2 MB" in out["error"]
    exact = tmp_path / "ok.json"
    exact.write_text(json.dumps({"schema_version": 1, "events": [], "status": "failed"}))
    assert run_replay(monkeypatch, capsys, exact)[0] == 0


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_replay_rejects_non_finite_json(tmp_path, monkeypatch, capsys, literal):
    path = tmp_path / "nan.json"
    path.write_text('{"schema_version": 1, "events": [{"stage": "done", "elapsed_ms": %s}], "status": "failed"}' % literal)
    code, out = run_replay(monkeypatch, capsys, path)
    assert code == 2 and "Non-finite" in out["error"]


def test_replay_timeline_is_limited_to_128_events():
    module = offline_module()
    event = {"stage": "done"}
    assert module.replay({"schema_version": 1, "events": [event] * 128})["mode"] == "timeline"
    with pytest.raises(ValueError, match="Invalid trace timeline"):
        module.replay({"schema_version": 1, "events": [event] * 129})
    with pytest.raises(ValueError, match="Invalid trace timeline"):
        module.replay({"schema_version": 1, "events": "not-a-list"})


# ---------------------------------------------------------------------------
# The /agent route: observation, tool-name mapping and approval decisions.
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_env(monkeypatch, tmp_path):
    from service import main
    main.SESSIONS.clear()
    monkeypatch.setattr(diagnostics, "MOE_DIR", tmp_path)
    monkeypatch.setattr(main, "client", AsyncMock(), raising=False)
    monkeypatch.setattr(main, "ensure_omlx", AsyncMock())
    monkeypatch.setattr(main, "maybe_summarize", AsyncMock())
    def forbidden(*args, **kw):
        raise AssertionError("fixture must not open sockets or processes")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    async def fake_agent(client, model, messages, emit, approver, **kw):
        await emit({"type": "routed", "role": "agent", "route_source": "rules", "reason": "PRIVATE-REASON"})
        await emit({"type": "tool_call", "id": "call_0", "name": "run_shell", "args": {"cmd": "PRIVATE-ARGS"}})
        await approver.confirm({"id": "call_0", "tool": "run_shell", "args": {}, "reason": "PRIVATE-REASON"})
        await emit({"type": "tool_result", "id": "call_0", "result": "PRIVATE-RESULT"})
        return "done"
    monkeypatch.setattr(main, "run_agent", fake_agent, raising=False)
    yield main, tmp_path
    main.SESSIONS.clear()


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("by_request", [True, False])
def test_agent_route_journals_observed_events_tool_names_and_the_decision(agent_env, approved, by_request):
    main, root = agent_env
    async def scenario():
        response = await main.agent({"prompt": "PRIVATE-PROMPT"})
        iterator = response.body_iterator
        seen = []
        while not seen or seen[-1]["type"] != "confirm":
            seen.append(json.loads((await asyncio.wait_for(iterator.__anext__(), 20))[6:]))
        session = seen[0]
        body = {"session_id": session["id"], "action_id": "call_0", "approved": approved}
        if by_request:
            body["request_id"] = session["trace_id"]
        assert (await main.approve(body))["ok"] is True
        async for _ in iterator:
            pass
        return session["trace_id"]
    trace_id = asyncio.run(scenario())
    payload = json.loads((root / "diagnostics" / (trace_id + ".json")).read_text())
    stages = [e["stage"] for e in payload["events"]]
    assert "PRIVATE" not in json.dumps(payload)
    assert stages.index("routed") < stages.index("tool_call") < stages.index("confirm")
    assert payload["events"][stages.index("routed")]["route_role"] == "agent"
    assert ("approved" if approved else "denied") in stages
    assert ("denied" if approved else "approved") not in stages
    # The result carries only an id, so its name is mapped from the call.
    result = payload["events"][stages.index("tool_result")]
    assert result["tool_name"] == "run_shell"
    assert payload["events"][stages.index("tool_call")]["tool_name"] == "run_shell"
    assert payload["status"] == "completed"
