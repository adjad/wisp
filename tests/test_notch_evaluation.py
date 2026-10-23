"""Offline boundary qualification for the contained Notch evaluation host.

The whole module runs under one latched boundary: the host's audit hook is
irreversible, so the guard is installed once, before any service module other
than the pytest bootstrap's ``service.paths`` is imported, and every test runs
inside it.

Inference is a SYNTHETIC OpenAI-compatible engine behind an httpx mock
transport. These are boundary tests of the real /agent route, policy,
confirmation and registry; they are never results for the user's corpus.
Nothing here starts a model, contacts a network host or touches real state.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from service.paths import MOE_DIR

ROOT = Path(__file__).resolve().parents[1]
OWNED = MOE_DIR.resolve().parent
PORT, ENGINE_PORT = 18891, 18892
TOKEN, NONCE = "t" * 40, "n" * 24
HOST_HEADER = f"127.0.0.1:{PORT}".encode()

from scripts.notch_evaluation import host as evaluation_host  # noqa: E402  (stdlib only at import)
from scripts.notch_evaluation import snapshots  # noqa: E402

SCRIPT: dict = {"tool": None, "args": {}}
ENGINE_LOG: list[tuple[str, str]] = []
MODELS: list[str] = []
NOT_RESIDENT: set[str] = set()


def _synthetic_client():
    """A real OMLXClient whose httpx transport is a scripted engine."""
    import httpx
    from service import main
    from service.config import role_to_model
    from service.inference.omlx_client import OMLXClient

    MODELS[:] = sorted({role_to_model(role) for role in ("agent", "fast", "general")})
    # The roster must be reviewed: the guard refuses any other completion.
    assert set(MODELS) <= set(CONFIG.allowed_models), (MODELS, CONFIG.allowed_models)

    def handler(request):
        path = request.url.path
        ENGINE_LOG.append((request.method, path))
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in MODELS]})
        if path == "/v1/models/status":
            return httpx.Response(200, json={"models": [{"id": m, "loaded": m not in NOT_RESIDENT} for m in MODELS]})
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        offered = [t["function"]["name"] for t in body.get("tools") or []]
        answered = any(m.get("role") == "tool" for m in body["messages"])
        if SCRIPT["tool"] in offered and not answered:
            delta = {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {
                "name": SCRIPT["tool"], "arguments": json.dumps(SCRIPT["args"])}}]}
            finish = "tool_calls"
        else:
            delta, finish = {"content": "SYNTHETIC ANSWER"}, "stop"
        chunks = [{"choices": [{"index": 0, "delta": delta}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
        if body.get("stream"):
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        message = {"role": "assistant", "content": delta.get("content", ""),
                   **({"tool_calls": delta["tool_calls"]} if "tool_calls" in delta else {})}
        return httpx.Response(200, json={"choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    class Synthetic(OMLXClient):
        def __init__(self, *args, **kwargs):
            super().__init__(base_url=f"http://127.0.0.1:{ENGINE_PORT}", api_key="synthetic")
            self._client = httpx.AsyncClient(base_url=self.base_url, transport=httpx.MockTransport(handler))

    # Per-turn clients are built from main.OMLXClient too.
    main.OMLXClient = Synthetic
    return Synthetic()


CONFIG = evaluation_host.HostConfig(
    run_id="qualify-offline", owned_root=str(OWNED), listen_port=PORT, token=TOKEN, nonce=NONCE,
    source_roots=(str(OWNED / "home"),), inference_url=f"http://127.0.0.1:{ENGINE_PORT}",
    # Empty synthetic state resolves every text role to the oQ4e default.
    allowed_models=("Ling-3.0-tiny-oQ4e", "Ling-3.0-tiny-oQ6e", "Ornith-1.0-9B"))
APP = evaluation_host.start(CONFIG, client_factory=_synthetic_client)

from service import evaluation_boundary as boundary  # noqa: E402
from service import main  # noqa: E402


def _scope(method: str, path: str, headers: list) -> dict:
    return {"type": "http", "method": method, "path": path, "headers": headers, "query_string": b"",
            "scheme": "http", "server": ("127.0.0.1", PORT), "client": ("127.0.0.1", 50000),
            "root_path": "", "http_version": "1.1"}


async def call(method: str, path: str, body=None, *, token: str = TOKEN, host: bytes = HOST_HEADER,
               disconnect: asyncio.Event | None = None, sink: dict | None = None) -> dict:
    headers = [(b"host", host), (b"content-type", b"application/json")]
    if token:
        headers.append((evaluation_host.TOKEN_HEADER.encode(), token.encode()))
    raw = json.dumps(body).encode() if body is not None else b""
    out = sink if sink is not None else {}
    out.update(status=None, headers={}, body=b"")
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        if disconnect is not None:
            await disconnect.wait()
            return {"type": "http.disconnect"}
        await asyncio.sleep(3600)

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await APP(_scope(method, path, headers), receive, send)
    return out


def events(result: dict) -> list[dict]:
    return [json.loads(line[6:]) for line in result["body"].decode().splitlines() if line.startswith("data: ")]


def lifespan():
    return main.app.router.lifespan_context(main.app)


def receipts(sink: str, tool: str | None = None) -> list[dict]:
    return [r for r in boundary.receipts() if r["sink"] == sink and (tool is None or r.get("tool") == tool)]


# --- latch, guard and seal -------------------------------------------------

def test_boundary_is_latched_and_cannot_be_reset():
    assert boundary.active()
    with pytest.raises(boundary.EvaluationBlockedError):
        boundary.latch(boundary.context())
    with pytest.raises(AttributeError):
        boundary.tool_dispatch_problem = lambda *a: None
    with pytest.raises(AttributeError):
        del boundary._state
    with pytest.raises(boundary.EvaluationBlockedError):
        boundary.seal_reads({})
    host_record = json.loads((OWNED / "evidence" / "host.json").read_text())
    assert set(host_record["sealed_reads"]) == set(evaluation_host.REVIEWED_READS)


def test_guard_refuses_process_network_and_foreign_files():
    with pytest.raises(PermissionError):
        subprocess.run(["/bin/echo", "never"], check=False)
    with pytest.raises(PermissionError):
        os.system("/usr/bin/true")
    probe = socket.socket()
    try:
        with pytest.raises(PermissionError):
            probe.connect(("192.0.2.1", 9))           # TEST-NET; refused before any packet
        with pytest.raises(PermissionError):
            probe.connect(("127.0.0.1", 8765))        # the production backend port
    finally:
        probe.close()
    with pytest.raises(PermissionError):
        socket.getaddrinfo("example.com", 443)
    real_home = Path(evaluation_host._real_home())
    with pytest.raises(PermissionError):
        open(real_home / ".wisp-evaluation-probe-never-created", "w")
    with pytest.raises(PermissionError):
        open(real_home / ".ssh" / "config")
    with pytest.raises(PermissionError):
        open("/private/var/tmp/wisp-evaluation-probe", "w")
    import ctypes
    with pytest.raises(PermissionError):
        ctypes.CDLL(str(OWNED / "never.dylib"))
    import webbrowser
    with pytest.raises(PermissionError):
        webbrowser.open("https://example.com")
    owned_file = OWNED / "evidence" / "scratch.txt"
    owned_file.write_text("owned state is writable")
    assert owned_file.read_text() == "owned state is writable"


def test_guard_judges_entries_and_dir_fd_names_where_they_really_are():
    import shutil
    outside = ROOT / "README.md"                      # a real file outside the owned root; never touched
    before = outside.stat()
    work = OWNED / "guard-entries"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "escape.txt").symlink_to(outside)
    # Changing what a link names is refused, through the link or not.
    with pytest.raises(PermissionError):
        os.chmod(work / "sub" / "escape.txt", 0o600)
    with pytest.raises(PermissionError):
        os.utime(work / "sub" / "escape.txt")
    # Removing the link entry itself is inside the root and leaves the target alone.
    fd = os.open(work / "sub", os.O_RDONLY)
    try:
        (work / "sub" / "plain.txt").write_text("synthetic")
        os.unlink("plain.txt", dir_fd=fd)              # resolved against the directory, not the cwd
        os.unlink("escape.txt", dir_fd=fd)
    finally:
        os.close(fd)
    assert not (work / "sub" / "escape.txt").exists() and outside.stat().st_mtime == before.st_mtime
    # A dir_fd naming a directory outside the root is refused, whatever the name.
    fd = os.open(ROOT, os.O_RDONLY)
    try:
        with pytest.raises(PermissionError):
            os.unlink("README.md", dir_fd=fd)
    finally:
        os.close(fd)
    assert outside.exists()
    # The fd-based tree removal pytest and tempfile use at exit works inside the root.
    (work / "tree" / "deep").mkdir(parents=True)
    (work / "tree" / "deep" / "link").symlink_to(outside)
    shutil.rmtree(work / "tree")
    assert not (work / "tree").exists() and outside.exists()
    with pytest.raises(PermissionError):
        shutil.rmtree(ROOT / "no-such-tree-outside")


def test_roster_is_pinned_to_the_reviewed_model_before_import():
    from service.config import role_to_model
    pinned = json.loads((OWNED / "wisp" / "config.yaml").read_text())
    assert pinned == {"roles": {role: CONFIG.allowed_models[0] for role in evaluation_host.TEXT_ROLES}}
    assert oct((OWNED / "wisp" / "config.yaml").stat().st_mode & 0o777) == "0o600"
    assert all(role_to_model(role) == CONFIG.allowed_models[0] for role in evaluation_host.TEXT_ROLES)


def test_http_guard_allows_only_reviewed_engine_operations():
    import httpx
    seen = []
    transport = httpx.MockTransport(lambda request: seen.append(request.url.path) or httpx.Response(200, json={}))

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(f"http://127.0.0.1:{ENGINE_PORT}/v1/models")
            for method, url in (("POST", f"http://127.0.0.1:{ENGINE_PORT}/v1/models/x/load"),
                                ("POST", f"http://127.0.0.1:{ENGINE_PORT}/v1/models/x/unload"),
                                ("POST", f"http://127.0.0.1:{ENGINE_PORT}/admin/settings"),
                                ("GET", "https://example.com/?q=private"),
                                ("POST", f"http://127.0.0.1:{PORT}/agent")):
                with pytest.raises(PermissionError):
                    await client.request(method, url)
    asyncio.run(run())
    assert seen == ["/v1/models"]
    assert receipts("http")


def test_model_lifecycle_is_refused():
    from service.inference.omlx_client import ModelLoadError

    async def run():
        client = main.OMLXClient()
        with pytest.raises(ModelLoadError, match="EVALUATION_BLOCKED"):
            await client.load("Ling-3.0-tiny-oQ6e")
        with pytest.raises(ModelLoadError, match="EVALUATION_BLOCKED"):
            await client.unload("Ling-3.0-tiny-oQ6e")
        with pytest.raises(ModelLoadError, match="not a reviewed"):
            await client.ensure_only("Some-Other-Model")
        await client.aclose()
    asyncio.run(run())


# --- final effect sinks ----------------------------------------------------

def _tool(name):
    from service.tools.registry import REGISTRY
    return REGISTRY[name]


def test_effects_are_blocked_with_full_access_and_a_standing_grant():
    from service.safety import grants, policy
    from service.tools.registry import run_tool
    policy.set_full_access(True)
    try:
        args = {"title": "Synthetic", "body": "never written"}
        grants.grant("create_note", args, decision="allow")
        decision = policy.decide("create_note", args)
        assert getattr(decision, "verdict", getattr(decision, "decision", decision)) is not None
        result = asyncio.run(run_tool(_tool("create_note"), args))
        assert result.startswith("(error: EVALUATION_BLOCKED")
        for name, call_args in (("send_message", {"to": "+15550100", "text": "never sent"}),
                                ("run_shell", {"cmd": "ls"}), ("web_search", {"query": "private canary"}),
                                ("write_file", {"path": str(OWNED / "home" / "x.txt"), "content": "x"})):
            assert asyncio.run(run_tool(_tool(name), call_args)).startswith("(error: EVALUATION_BLOCKED")
    finally:
        policy.set_full_access(False)
        grants.revoke("create_note")
    assert not (OWNED / "home" / "x.txt").exists()


def test_mislabeled_or_forged_reads_are_blocked():
    from service.tools.registry import Tool, run_tool
    real = _tool("read_file")

    def impostor(path: str) -> str:
        return "forged result"

    forged_same_name = Tool(name="read_file", description=real.description, parameters=real.parameters,
                            category="fs_read", func=impostor)
    write_labelled_read = Tool(name="send_email", description="reads mail", parameters={"type": "object",
                               "properties": {}}, category="email_read", func=lambda: "sent")
    copied_code = Tool(name="read_file", description="", parameters=real.parameters, category="fs_read",
                       func=type(real.func)(real.func.__code__, real.func.__globals__, "read_file"))
    for tool, args in ((forged_same_name, {"path": "x"}), (write_labelled_read, {}), (copied_code, {"path": "x"})):
        result = asyncio.run(run_tool(tool, args))
        assert result.startswith("(error: EVALUATION_BLOCKED"), result
    reasons = {r["reason"] for r in receipts("registry") if r["outcome"] == "blocked"}
    assert "implementation identity does not match the reviewed read" in reasons
    assert "not a reviewed read" in reasons


def test_reads_are_bound_to_the_source_scope():
    from service.tools.registry import run_tool
    home = OWNED / "home"
    (home / "Documents").mkdir(parents=True, exist_ok=True)
    (home / "Documents" / "plan.txt").write_text("SYNTHETIC-PLAN-42")
    (home / "Documents" / "escape.txt").symlink_to(ROOT / "README.md")
    allowed = asyncio.run(run_tool(_tool("read_file"), {"path": "~/Documents/plan.txt"}))
    assert "SYNTHETIC-PLAN-42" in allowed
    for path in ("/etc/hosts", str(ROOT / "README.md"), "~/Documents/escape.txt", "~/../wisp/assistant.db",
                 str(OWNED / "wisp" / "assistant.db")):
        assert asyncio.run(run_tool(_tool("read_file"), {"path": path})).startswith("(error: EVALUATION_BLOCKED")
    assert asyncio.run(run_tool(_tool("calculate"), {"expression": "6*7"})).strip().endswith("42")


def test_outbox_never_publishes_or_claims():
    from service.assistant import outbox
    from service.assistant.store import assistant_store
    before = len(assistant_store.pending_events())
    for event_type, payload in (("send_email", {"to": "a@example.com", "subject": "s", "body": "b"}),
                                ("send_message", {"to": "+15550100", "text": "t"}),
                                ("create_calendar_event", {"title": "t", "start_ts": 1.0}),
                                ("create_reminder", {"title": "t"}), ("unknown_native_action", {})):
        for entry in (outbox.request, outbox._request):
            result = asyncio.run(entry(event_type, payload))
            assert result["status"] == "blocked" and result["dispatch_started"] is False
            assert result["evaluation_blocked"] is True and result["ok"] is False
    assert len(assistant_store.pending_events()) == before
    assert not outbox._pending


def test_hub_refuses_every_publication_variant():
    from service.assistant.hub import hub
    from service.assistant.store import assistant_store
    before = len(assistant_store.pending_events())
    queue = hub.subscribe()
    try:
        with pytest.raises(boundary.EvaluationBlockedError):
            hub.publish_sync({"type": "send_email", "to": "a@example.com"})
        with pytest.raises(boundary.EvaluationBlockedError):
            asyncio.run(hub.publish({"type": "create_calendar_event"}, durable=False))
        with pytest.raises(boundary.EvaluationBlockedError):
            hub.publish_committed_sync(None, None, None, None)
        with pytest.raises(boundary.EvaluationBlockedError):
            asyncio.run(_prepare_nudge(hub, assistant_store))
        with pytest.raises(boundary.EvaluationBlockedError):
            asyncio.run(_first_event(hub))
        assert hub.publish_sync({"type": "changed"}) == {"type": "changed"}
        assert queue.empty()
    finally:
        hub.unsubscribe(queue)
    assert len(assistant_store.pending_events()) == before


async def _prepare_nudge(hub, store):
    hub.prepare_nudge_delivery(store)


async def _first_event(hub):
    async for event in hub.events():
        return event


def test_due_scheduled_sends_cannot_dispatch():
    """The scheduler never starts here; even if called, its sends hit the outbox latch."""
    from service.assistant import scheduler
    from service.assistant.outbox import request
    result = asyncio.run(request("send_email", {"to": "a@example.com", "subject": "s", "body": "b"}))
    assert result["status"] == "blocked"
    assert "_fire_scheduled_sends" in dir(scheduler)  # the reviewed sink still exists and is latched


def test_every_effect_entry_goes_through_a_latched_sink():
    """Static call-graph check: no module calls a tool implementation directly,
    and every hub method that enqueues or fans out consults the latch."""
    direct = []
    for path in (ROOT / "service").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.func\(\s*\*\*|to_thread\(\s*[\w.]+\.func\b", text):
            if path.name != "registry.py":
                direct.append(f"{path.relative_to(ROOT)}:{text[:match.start()].count(chr(10)) + 1}")
    assert direct == []
    hub_source = (ROOT / "service" / "assistant" / "hub.py").read_text(encoding="utf-8")
    for block in re.split(r"\n    (?:async )?def ", hub_source)[1:]:
        if "put_nowait" in block or "enqueue_event" in block:
            assert "publication_allowed" in block or block.startswith(("subscribe", "events")), block[:40]
    outbox_source = (ROOT / "service" / "assistant" / "outbox.py").read_text(encoding="utf-8")
    body = outbox_source.split("async def _request(", 1)[1]
    assert body.index("native_request_blocked") < body.index("hub.")


# --- the normal Ask route --------------------------------------------------

def test_endpoint_identity_fails_closed():
    async def run():
        assert (await call("GET", "/chats", token="")).get("status") == 401
        assert (await call("GET", "/chats", token="w" * 40))["status"] == 401
        assert (await call("GET", "/chats", host=b"127.0.0.1:8765"))["status"] == 421
        assert (await call("GET", "/chats", host=b"evil.example:18891"))["status"] == 421
        for method, path in (("POST", "/unload_all"), ("POST", "/config"), ("GET", "/assistant/events"),
                             ("POST", "/corpus/run"), ("POST", "/assistant/send_message_draft"),
                             ("POST", "/shutdown_omlx"), ("DELETE", "/sessions/abcdef12"),
                             ("POST", "/sessions/abcdef12/effects/resolve"),
                             # Reader pushes exist only in an approved live run.
                             ("POST", "/assistant/sync/emails"), ("GET", "/assistant/sync/status"),
                             ("POST", "/behavior/history/calendar/batch")):
            result = await call(method, path, {})
            assert result["status"] == 403, (method, path, result["status"])
            assert result["headers"][evaluation_host.NONCE_HEADER] == NONCE
        for body in ({"prompt": "x", "test_mode": True}, {"case_id": "1", "token": "x"}, {"prompt": 3}):
            assert (await call("POST", "/agent", body))["status"] == 422
        ident = await call("GET", "/evaluation/identity")
        assert ident["status"] == 200
        payload = json.loads(ident["body"])
        assert payload["nonce"] == NONCE and payload["pid"] == os.getpid()
        assert ident["headers"][evaluation_host.PID_HEADER] == str(os.getpid())
    asyncio.run(run())


def test_minimal_lifespan_starts_no_background_work():
    async def run():
        async with lifespan():
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            assert others == []
    asyncio.run(run())


def test_ask_turns_read_continue_and_block():
    home = OWNED / "home" / "Documents"
    home.mkdir(parents=True, exist_ok=True)
    (home / "brief.txt").write_text("SYNTHETIC-BRIEF-7")

    async def run():
        async with lifespan():
            SCRIPT.update(tool="read_file", args={"path": "~/Documents/brief.txt"})
            first = events(await call("POST", "/agent", {"prompt": "what does the file ~/Documents/brief.txt say",
                                                         "debug": False}))
            kinds = [e["type"] for e in first]
            assert kinds[0] == "session" and kinds[-1] == "done"
            assert any(e["type"] == "tool_result" and "SYNTHETIC-BRIEF-7" in str(e.get("result")) for e in first), first
            sid = first[0]["id"]
            SCRIPT.update(tool="read_file", args={"path": "/etc/hosts"})
            second = events(await call("POST", "/agent", {"prompt": "now read /etc/hosts", "session_id": sid,
                                                          "debug": False}))
            assert second[0]["id"] == sid and second[-1]["type"] == "done"
            assert any("EVALUATION_BLOCKED" in str(e.get("result")) for e in second)
            history = await call("GET", f"/sessions/{sid}")
            assert history["status"] == 200 and len(json.loads(history["body"])["turns"]) == 4
            chats = json.loads((await call("GET", "/chats"))["body"])["chats"]
            assert sid in json.dumps(chats), chats
    asyncio.run(run())


async def _until(predicate, sink, task, seconds=10.0):
    for _ in range(int(seconds / 0.02)):
        if predicate(events(sink)) or task.done():
            return
        await asyncio.sleep(0.02)


def test_confirmation_is_presented_then_the_effect_is_blocked():
    from service.safety import policy
    policy.set_full_access(False)
    SCRIPT.update(tool="create_note", args={"title": "Groceries", "body": "milk"})

    async def run():
        async with lifespan():
            sink: dict = {"body": b""}
            task = asyncio.create_task(call("POST", "/agent", {"prompt": "create a note called Groceries "
                                                               "that says milk", "debug": False}, sink=sink))
            await _until(lambda e: any(x["type"] == "confirm" for x in e), sink, task)
            stream = events(sink)
            card = next((x for x in stream if x["type"] == "confirm"), None)
            assert card is not None, stream
            sid = stream[0]["id"]
            approved = await call("POST", "/agent/approve", {"session_id": sid, "action_id": card.get("id", ""),
                                                             "approved": True, "scope": "always",
                                                             "request_id": card["request_id"]})
            assert approved["status"] == 200
            await asyncio.wait_for(task, 30)
            final = events(sink)
            assert any(x["type"] == "tool_call" and x.get("decision") == "confirm" for x in final)
            assert any(x["type"] == "tool_result" and "EVALUATION_BLOCKED" in str(x.get("result")) for x in final)
            assert final[-1]["type"] == "done"
    asyncio.run(run())
    from service.safety import grants
    grants.revoke("create_note")
    assert receipts("registry", "create_note")[-1]["outcome"] == "blocked"


def test_cancelled_turn_neither_executes_nor_replays():
    from service.safety import policy
    policy.set_full_access(False)
    SCRIPT.update(tool="create_note", args={"title": "Cancelled", "body": "never"})
    before = len(receipts("registry", "create_note"))

    async def run():
        async with lifespan():
            sink: dict = {"body": b""}
            gone = asyncio.Event()
            task = asyncio.create_task(call("POST", "/agent", {"prompt": "create a note called Cancelled",
                                                               "debug": False}, sink=sink, disconnect=gone))
            await _until(lambda e: any(x["type"] == "confirm" for x in e), sink, task)
            card = next(x for x in events(sink) if x["type"] == "confirm")
            gone.set()
            await asyncio.wait_for(task, 30)
            # Approving the abandoned card afterwards must not run anything.
            await call("POST", "/agent/approve", {"session_id": events(sink)[0]["id"], "action_id": "",
                                                  "approved": True, "scope": "once",
                                                  "request_id": card["request_id"]})
            await asyncio.sleep(0.2)
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            assert others == [], others
    asyncio.run(run())
    assert len(receipts("registry", "create_note")) == before


# --- snapshots and the ledger ----------------------------------------------

def test_snapshot_capture_is_scoped_consistent_and_verified():
    import sqlite3
    source = OWNED / "synthetic-source"
    state = source / "moe"
    (state / "cache").mkdir(parents=True)
    (state / "cache" / "messages.txt").write_text("SYNTHETIC message cache")
    (state / "grants.json").write_text("{}")
    (source / "Documents").mkdir()
    (source / "Documents" / "plan.txt").write_text("SYNTHETIC document")
    db = sqlite3.connect(state / "assistant.db")
    db.execute("CREATE TABLE commitments (id TEXT, title TEXT)")
    db.execute("CREATE TABLE assistant_events (id TEXT, payload TEXT)")
    db.execute("INSERT INTO commitments VALUES ('c1', 'SYNTHETIC standup')")
    db.execute("INSERT INTO assistant_events VALUES ('e1', 'queued send')")
    db.commit()
    db.close()
    for bad in (snapshots.SourceScope(caches=("browser_history_privacy",)),
                snapshots.SourceScope(assistant_tables=("assistant_events",)),
                snapshots.SourceScope(files=("../escape.txt",)), snapshots.SourceScope(files=(".moe/grants.json",)),
                snapshots.SourceScope(files=("bin/tool.sh",)), snapshots.SourceScope(files=("/etc/hosts",))):
        with pytest.raises(snapshots.ScopeError):
            bad.validate()
    scope = snapshots.SourceScope(caches=("messages", "notes"), assistant_tables=("commitments",),
                                  files=("Documents/plan.txt",))
    out = OWNED / "synthetic-snapshot"
    manifest = snapshots.capture(source, state, out, scope)
    assert manifest["label"] == snapshots.LABEL
    assert manifest["coverage"]["cache:notes"] == "missing"
    assert manifest["coverage"]["assistant.db"]["tables"] == {"commitments": 1}
    copy = sqlite3.connect(out / "wisp" / "assistant.db")
    assert copy.execute("SELECT COUNT(*) FROM assistant_events").fetchone()[0] == 0
    copy.close()
    assert not (out / "wisp" / "grants.json").exists()
    assert snapshots.verify(out).ok
    (out / "home" / "Documents" / "plan.txt").write_text("altered")
    assert "home/Documents/plan.txt changed since capture" in snapshots.verify(out).problems
    (out / "home" / "Documents" / "plan.txt").write_text("SYNTHETIC document")
    (out / "home" / "extra.txt").write_text("planted")
    assert not snapshots.verify(out).ok
    (out / "home" / "extra.txt").unlink()
    assert not snapshots.verify(out, max_age_s=-1).ok
    with pytest.raises(snapshots.ScopeError):
        snapshots.capture(source, state, out, scope)


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


GOOD = {"input_delivered": "YES", "response_contract": "PASS", "factual_correctness": "UNVERIFIED",
        "requested_effect": "NOT_REQUESTED", "source_coverage": "COVERED", "safety": "PASS"}


def _ledger(name: str, tags) -> Path:
    cases = [{"case_id": i, "risk_tag": tag, "prompt_sha256": _sha(f"synthetic prompt {i}"), "status": "NOT_RUN",
              "attempts": []} for i, tag in enumerate(tags, start=1)]
    path = OWNED / "evidence" / name
    path.write_text(json.dumps({"schema_version": 1, "corpus_sha256": "c" * 64, "cases": cases}))
    return path


def test_ledger_binds_inputs_and_counts_dimensions_separately():
    sha = _sha
    path = _ledger("synthetic-ledger.json", ("R", "W", "R"))
    with pytest.raises(evaluation_host.LedgerError):
        evaluation_host.CaseLedger(path, corpus_sha256="d" * 64, expected_cases=3)
    ledger = evaluation_host.CaseLedger(path, corpus_sha256="c" * 64, expected_cases=3)
    good = GOOD
    ledger.record_attempt(1, run_id="r", attempt_id="1-a", delivered_sha256=sha("synthetic prompt 1"),
                          receipts={"session": "s1"}, status="PASSED", dimensions=good)
    with pytest.raises(evaluation_host.LedgerError):        # a terminal case takes no more attempts
        ledger.record_attempt(1, run_id="r", attempt_id="1-b", delivered_sha256=sha("synthetic prompt 1"),
                              receipts={}, status="PASSED", dimensions=good)
    with pytest.raises(evaluation_host.LedgerError):        # duplicate attempt id
        ledger.record_attempt(3, run_id="r", attempt_id="1-a", delivered_sha256=sha("synthetic prompt 3"),
                              receipts={}, status="PASSED", dimensions=good)
    with pytest.raises(evaluation_host.LedgerError):        # substituted input
        ledger.record_attempt(3, run_id="r", attempt_id="3-a", delivered_sha256=sha("rewritten prompt"),
                              receipts={}, status="PASSED", dimensions=good)
    with pytest.raises(evaluation_host.LedgerError):        # W case cannot claim no effect was requested
        ledger.record_attempt(2, run_id="r", attempt_id="2-a", delivered_sha256=sha("synthetic prompt 2"),
                              receipts={}, status="PASSED", dimensions=good)
    blocked = {**good, "requested_effect": "BLOCKED"}
    with pytest.raises(evaluation_host.LedgerError):        # a blocked request is never a passed task
        ledger.record_attempt(2, run_id="r", attempt_id="2-a", delivered_sha256=sha("synthetic prompt 2"),
                              receipts={}, status="PASSED", dimensions=blocked)
    ledger.record_attempt(2, run_id="r", attempt_id="2-a", delivered_sha256=sha("synthetic prompt 2"),
                          receipts={"confirmation": "presented"}, status="BLOCKED", dimensions=blocked)
    resumed = evaluation_host.CaseLedger(path, corpus_sha256="c" * 64, expected_cases=3)
    assert resumed.next_case() == 3
    counts = resumed.counts()
    assert counts["status"] == {"NOT_RUN": 1, "IN_PROGRESS": 0, "PASSED": 1, "FAILED": 0, "BLOCKED": 1}
    assert counts["dimensions"]["requested_effect"]["BLOCKED"] == 1 and counts["attempts"] == 2


def test_multiturn_segments_are_bound_once_and_complete_only_when_all_arrive():
    path = _ledger("synthetic-multiturn.json", ("R", "R", "R"))
    ledger = evaluation_host.CaseLedger(path, corpus_sha256="c" * 64, expected_cases=3)
    seg = [_sha("segment one"), _sha("segment two")]
    # A caller cannot opt a single-turn case into arbitrary hashes: the
    # parameter no longer exists, and only the approved record counts.
    with pytest.raises(TypeError):
        ledger.record_attempt(1, run_id="r", attempt_id="x", delivered_sha256=_sha("rewritten"),
                              receipts={}, status="PASSED", dimensions=GOOD, multiturn_hashes=(_sha("rewritten"),))
    with pytest.raises(evaluation_host.LedgerError):        # not bound to the original entry
        ledger.approve_segmentation(1, entry_sha256=_sha("another entry"), segment_hashes=seg, approved_by="coord")
    with pytest.raises(evaluation_host.LedgerError):        # one segment is not a segmentation
        ledger.approve_segmentation(1, entry_sha256=_sha("synthetic prompt 1"), segment_hashes=seg[:1],
                                    approved_by="coord")
    ledger.approve_segmentation(1, entry_sha256=_sha("synthetic prompt 1"), segment_hashes=seg, approved_by="coord")
    with pytest.raises(evaluation_host.LedgerError):        # approved once
        ledger.approve_segmentation(1, entry_sha256=_sha("synthetic prompt 1"), segment_hashes=seg[::-1],
                                    approved_by="coord")
    for index in (-1, 1, True, 0.0):                         # negative, out of order, non-int
        with pytest.raises(evaluation_host.LedgerError):
            ledger.record_attempt(1, run_id="r", attempt_id=f"bad-{index!r}", delivered_sha256=seg[0],
                                  receipts={}, status="PASSED", dimensions=GOOD, turn_index=index)
    with pytest.raises(evaluation_host.LedgerError):        # an altered segment
        ledger.record_attempt(1, run_id="r", attempt_id="alt", delivered_sha256=_sha("segment ONE"),
                              receipts={}, status="PASSED", dimensions=GOOD, turn_index=0)
    # An undelivered turn is retried at the same index.
    ledger.record_attempt(1, run_id="r", attempt_id="1-0a", delivered_sha256=_sha("nothing typed"), receipts={},
                          status="BLOCKED", dimensions={**GOOD, "input_delivered": "NO", "response_contract": "UNVERIFIED"},
                          turn_index=0)
    assert ledger.next_turn(1) == 0
    ledger.record_attempt(1, run_id="r", attempt_id="1-0b", delivered_sha256=seg[0], receipts={},
                          status="PASSED", dimensions=GOOD, turn_index=0)
    # A partial case is IN_PROGRESS and a resumed run continues it, at turn 1.
    resumed = evaluation_host.CaseLedger(path, corpus_sha256="c" * 64, expected_cases=3)
    assert resumed.by_id[1]["status"] == "IN_PROGRESS"
    assert resumed.next_case() == 1 and resumed.next_turn(1) == 1
    assert resumed.counts()["status"]["IN_PROGRESS"] == 1
    with pytest.raises(evaluation_host.LedgerError):        # turn 0 is done
        resumed.record_attempt(1, run_id="r2", attempt_id="1-0c", delivered_sha256=seg[0], receipts={},
                               status="PASSED", dimensions=GOOD, turn_index=0)
    resumed.record_attempt(1, run_id="r2", attempt_id="1-1", delivered_sha256=seg[1], receipts={},
                           status="FAILED", dimensions={**GOOD, "response_contract": "FAIL"}, turn_index=1)
    assert resumed.by_id[1]["status"] == "FAILED"            # the worst segment decides
    assert len(resumed.by_id[1]["attempts"]) == 3 and resumed.next_case() == 2
    # A case that cannot finish is abandoned explicitly, never PASSED.
    resumed.approve_segmentation(2, entry_sha256=_sha("synthetic prompt 2"),
                                 segment_hashes=[_sha("a"), _sha("b")], approved_by="coord")
    resumed.record_attempt(2, run_id="r2", attempt_id="2-0", delivered_sha256=_sha("a"), receipts={},
                           status="PASSED", dimensions=GOOD, turn_index=0)
    with pytest.raises(evaluation_host.LedgerError):
        resumed.abandon_case(2, status="PASSED", reason="x")
    assert resumed.abandon_case(2, status="BLOCKED", reason="second turn unavailable")["status"] == "BLOCKED"
    resumed.record_attempt(3, run_id="r2", attempt_id="3-0", delivered_sha256=_sha("nope"), receipts={},
                           status="BLOCKED", dimensions={**GOOD, "input_delivered": "NO"})
    with pytest.raises(evaluation_host.LedgerError):        # segmentation after attempts is refused
        resumed.approve_segmentation(3, entry_sha256=_sha("synthetic prompt 3"),
                                     segment_hashes=[_sha("a"), _sha("b")], approved_by="coord")
    # A tampered segmentation record no longer loads.
    data = json.loads(path.read_text())
    data["cases"][0]["segmentation"]["entry_sha256"] = _sha("forged")
    path.write_text(json.dumps(data))
    with pytest.raises(evaluation_host.LedgerError):
        evaluation_host.CaseLedger(path, corpus_sha256="c" * 64, expected_cases=3)


def test_profile_and_config_are_fail_closed():
    profile = evaluation_host.sandbox_profile(CONFIG, sys.executable)
    assert "(deny appleevent-send)" in profile and "(deny process-fork)" in profile
    assert "(deny network*)" in profile and f'localhost:{PORT}' in profile
    for bad in ({"listen_port": 8765}, {"listen_port": 8000}, {"token": "short"},
                {"inference_url": "http://example.com:8000"}, {"run_id": "../x"}):
        fields = {**CONFIG.__dict__, **bad}
        with pytest.raises(ValueError):
            evaluation_host.HostConfig(**fields).validate() if "inference_url" not in bad else \
                evaluation_host.HostConfig(**fields).inference_port
    config_path = OWNED / "evidence" / "config.json"
    config_path.write_text(json.dumps({**CONFIG.__dict__, "source_roots": list(CONFIG.source_roots)}))
    os.chmod(config_path, 0o644)
    with pytest.raises(PermissionError):
        evaluation_host.HostConfig.load(config_path)
    os.chmod(config_path, 0o600)
    assert evaluation_host.HostConfig.load(config_path).run_id == CONFIG.run_id


# --- snapshot readiness hydration ------------------------------------------
# Ordered: absent, then incomplete, then complete, because readiness is
# process state, exactly as native sync leaves it.

def _verified(**overrides) -> "snapshots.Verification":
    """A synthetic start-time verification of the payloads now in the root."""
    listed = {str(p.relative_to(OWNED)): snapshots.sha256_file(p)
              for p in sorted((OWNED / "sync").glob("*.json")) if p.is_file()}
    if (OWNED / "wisp" / "assistant.db").is_file():
        listed["wisp/assistant.db"] = snapshots.sha256_file(OWNED / "wisp" / "assistant.db")
    for rel, value in overrides.items():
        if value is None:
            listed.pop(rel, None)
        else:
            listed[rel] = value
    return snapshots.Verification(True, files=listed, manifest_sha256="synthetic")


def _hydrate(installed=None) -> dict:
    return asyncio.run(evaluation_host.hydrate_sources(CONFIG, installed=installed or _verified()))


def _header(ts: float, subject: str) -> str:
    return "\x01".join(["H2", str(ts), "U", "Work", "a", "Synthetic Sender", "sender@example.test",
                        f"id-{subject}", subject])


def _write_sync(source: str, body: dict, *, complete: bool, age: float = 30) -> None:
    import time
    record = snapshots.payload_record(source, body, captured_at=time.time() - age, reader="synthetic", complete=complete)
    target = OWNED / "sync" / f"{source}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record))


def _calendar(title: str, started: float, *, complete: bool) -> None:
    import time
    from service.assistant.store import assistant_store
    now = time.time()
    assistant_store.sync_source("calendar", [{
        "source_id": f"syn-{title}", "kind": "event", "title": title, "context": "Synthetic",
        "organizer": None, "account": None, "when_ts": now + 2 * 3600, "end_ts": now + 3 * 3600,
        "all_day": False, "location": None, "invitee_count": None, "organizer_is_user": None,
        "calendar_writable": None, "confidence": 1.0}],
        diagnostics={"authorized": True, "available": True, "syncing": False, "complete": complete,
                     "partial": not complete, "snapshot_started_at": started})


def test_hydration_marks_absent_sources_unavailable_not_ready():
    from service.tools.registry import run_tool
    report = _hydrate()
    for source, entry in report["sources"].items():
        assert entry["hydrated"] == "unavailable", (source, entry)
        assert entry["state_after"] == "unavailable", (source, entry)
    upcoming = asyncio.run(run_tool(_tool("get_upcoming"), {"days": 2}))
    assert "SYNTHETIC" not in upcoming
    assert receipts("hub") == [] or all(r["event_type"] in ("changed",) or r["outcome"] == "blocked"
                                        for r in receipts("hub"))


def test_incomplete_captures_are_never_hydrated_as_ready():
    import time
    from service.tools import email_tools
    from service.tools.registry import run_tool
    _write_sync("emails", {"headers": _header(time.time() - 60, "INCOMPLETE-SUBJECT"),
                           "diagnostics": {"available": True, "syncing": False}}, complete=False)
    _write_sync("messages", {"lines": "INCOMPLETE line", "diagnostics": {"available": True, "syncing": True}},
                complete=True)
    _calendar("INCOMPLETE-EVENT", time.time() - 100, complete=False)
    report = _hydrate()
    assert report["sources"]["emails"]["state_after"] == "unavailable"
    assert "the capturing reader did not observe a complete read" in report["sources"]["emails"]["problems"]
    assert report["sources"]["messages"]["state_after"] == "unavailable"
    assert report["sources"]["calendar"]["state_after"] == "unavailable"
    assert not any("INCOMPLETE" in row.get("subject", "") for row in email_tools.header_rows())
    assert "INCOMPLETE-EVENT" not in asyncio.run(run_tool(_tool("get_upcoming"), {"days": 2}))
    with pytest.raises(snapshots.ScopeError):
        snapshots.payload_record("messages", {"lines": "x", "contacts": []}, captured_at=1.0,
                                 reader="synthetic", complete=True)


def test_complete_synthetic_captures_feed_the_normal_reads():
    import time
    from service.tools import email_tools
    from service.tools.registry import run_tool
    _write_sync("emails", {"headers": _header(time.time() - 60, "SYNTHETIC-SUBJECT-9"),
                           "diagnostics": {"available": True, "syncing": False, "read_source": "mail_app"}},
                complete=True)
    _calendar("SYNTHETIC-EVENT-3", time.time() - 50, complete=True)
    report = _hydrate()
    assert report["sources"]["emails"]["hydrated"] == "ready"
    assert report["sources"]["emails"]["state_after"] == "ready"
    assert report["sources"]["calendar"]["state_after"] == "ready"
    assert report["sources"]["calendar"]["rows"] == 1
    assert "BLOCKED" in report["sources"]["reminders"]["note"]
    assert any(row.get("subject") == "SYNTHETIC-SUBJECT-9" for row in email_tools.header_rows())
    assert "SYNTHETIC-EVENT-3" in asyncio.run(run_tool(_tool("get_upcoming"), {"days": 2}))
    assert report["sources"]["emails"]["provenance"]["reader"] == "synthetic"


def test_stale_unlisted_or_altered_payloads_are_never_ready():
    import time
    emails = {"headers": _header(time.time() - 60, "SYNTHETIC-SUBJECT-9"),
              "diagnostics": {"available": True, "syncing": False, "read_source": "mail_app"}}
    # An old native read stays old, however fresh the outer snapshot.
    _write_sync("emails", emails, complete=True, age=CONFIG.snapshot_max_age_s + 3600)
    report = _hydrate()
    assert report["sources"]["emails"]["state_after"] == "unavailable"
    assert "the native read is older than the allowed age" in report["sources"]["emails"]["problems"]
    _write_sync("emails", emails, complete=True)
    # Not in the start-time verification: never read.
    report = _hydrate(_verified(**{"sync/emails.json": None}))
    assert report["sources"]["emails"]["state_after"] == "unavailable"
    # Changed after verification: never read.
    report = _hydrate(_verified(**{"sync/emails.json": "0" * 64}))
    assert report["sources"]["emails"]["state_after"] == "unavailable"
    now = time.time()
    record = {"schema_version": 1, "source": "messages", "body": {"lines": "x"},
              "provenance": {"captured_at": float("nan"), "reader": "synthetic", "complete": True}}
    assert snapshots.payload_shape_problems("messages", record)
    record["provenance"]["captured_at"] = now + 3600
    assert "the native read was captured in the future" in snapshots.payload_problems("messages", record, now=now)
    # Calendar readiness needs the verified assistant.db and a current receipt.
    report = _hydrate(_verified(**{"wisp/assistant.db": None}))
    assert report["sources"]["calendar"]["state_after"] == "unavailable"


def _installed_root(name: str, *, payload_age: float = 30.0) -> Path:
    import time
    source = OWNED / f"src-{name}"
    (source / "moe" / "cache").mkdir(parents=True)
    (source / "moe" / "cache" / "notes.txt").write_text("SYNTHETIC notes")
    payloads = source / "payloads"
    payloads.mkdir()
    record = snapshots.payload_record("messages", {"lines": "SYNTHETIC line",
                                                   "diagnostics": {"available": True, "syncing": False}},
                                      captured_at=time.time() - payload_age, reader="synthetic", complete=True)
    (payloads / "messages.json").write_text(json.dumps(record))
    snap = OWNED / f"snap-{name}"
    snapshots.capture(source, source / "moe", snap,
                      snapshots.SourceScope(caches=("notes",), sync_payloads=("messages",)), payload_dir=payloads)
    out = OWNED / f"inst-{name}"
    out.mkdir(mode=0o700)
    snapshots.install(snap, out)
    return out


def test_installed_layout_must_match_its_manifest_exactly():
    out = _installed_root("layout")
    verified = snapshots.verify_installed(out, max_age_s=3600)
    assert verified.ok and set(verified.files) == {"wisp/cache/notes.txt", "sync/messages.json"}
    digest = verified.manifest_sha256
    assert snapshots.verify_installed(out, expected_manifest_sha256=digest).ok
    assert not snapshots.verify_installed(out, expected_manifest_sha256="f" * 64).ok

    def refused(problem: str) -> None:
        result = snapshots.verify_installed(out, max_age_s=3600)
        assert not result.ok and any(problem in p for p in result.problems), result.problems
        assert result.files == {}

    (out / "home" / "Documents").mkdir(parents=True)
    (out / "home" / "Documents" / "planted.txt").write_text("unreviewed")
    refused("home/Documents/planted.txt is not in the installed manifest")
    (out / "home" / "Documents" / "planted.txt").unlink()
    (out / "sync" / "emails.json").write_text("{}")
    refused("sync/emails.json is not in the installed manifest")
    (out / "sync" / "emails.json").unlink()
    original = (out / "sync" / "messages.json").read_bytes()
    (out / "sync" / "messages.json").write_bytes(original.replace(b"SYNTHETIC line", b"ALTERED line"))
    refused("sync/messages.json changed since capture")
    (out / "sync" / "messages.json").write_bytes(original)
    (out / "wisp" / "linked.txt").symlink_to(OWNED / "evidence")
    refused("wisp/linked.txt is a link")
    (out / "wisp" / "linked.txt").unlink()
    os.chmod(out / "evidence" / snapshots.MANIFEST, 0o644)
    refused("installed manifest is not a private regular file")
    os.chmod(out / "evidence" / snapshots.MANIFEST, 0o600)
    assert snapshots.verify_installed(out, max_age_s=3600).ok
    (out / "evidence" / snapshots.MANIFEST).unlink()
    refused("source state is present without an installed manifest")
    # Stale native provenance under a fresh outer snapshot is refused.
    stale = _installed_root("stale", payload_age=2 * 24 * 3600)
    result = snapshots.verify_installed(stale, max_age_s=24 * 3600)
    assert not result.ok and "sync:messages is older than the allowed age" in result.problems
    # A root the coordinator declared empty: no files, no manifest.
    empty = OWNED / "inst-empty"
    for part in snapshots.INSTALLED_PARTS:
        (empty / part).mkdir(parents=True)
    result = snapshots.verify_installed(empty)
    assert result.ok and result.empty and result.files == {}
    assert not snapshots.verify_installed(empty, expected_manifest_sha256="f" * 64).ok


# --- launcher lifecycle and roots ------------------------------------------

class _FakeProcess:
    """A Popen stand-in: records signals, never touches a real process."""

    def __init__(self, pid: int, *, alive: bool = True, dies_on: tuple[str, ...] = ("SIGTERM",)):
        self.pid, self.code, self.sent, self.dies_on = pid, None if alive else 0, [], dies_on

    def poll(self):
        return self.code

    def send_signal(self, sig):
        import signal
        name = signal.Signals(sig).name
        self.sent.append(name)
        if name in self.dies_on:
            self.code = -int(sig)

    def wait(self):
        if self.code is None:
            self.code = -9
        return self.code


def test_supervisor_signals_only_its_own_live_children():
    from scripts.notch_evaluation import lifecycle
    live = lifecycle.Child("backend", _FakeProcess(5001))
    assert live.retire()["signals"] == ["SIGTERM"] and not live.alive()
    assert not live.signal(15) and live.process.sent == ["SIGTERM"]       # reaped: never again
    stubborn = _FakeProcess(5002, dies_on=())
    ticks = iter(range(100))
    record = lifecycle.Child("app", stubborn).retire(grace=1, clock=lambda: next(ticks), nap=lambda _: None)
    assert record["signals"] == ["SIGTERM", "SIGKILL"] and stubborn.code == -9
    exited = _FakeProcess(5003, alive=False)                             # natural exit
    assert lifecycle.Child("backend", exited).retire()["signals"] == [] and exited.sent == []
    # A partial launch: the second start fails, the first child is retired.
    root = OWNED / "lifecycle-run"
    root.mkdir(mode=0o700)
    spawned: list[_FakeProcess] = []

    def spawn(argv, **kwargs):
        assert kwargs["start_new_session"] and kwargs["close_fds"]
        if spawned:
            raise OSError("synthetic exec failure")
        spawned.append(_FakeProcess(6001))
        return spawned[-1]

    sup = lifecycle.Supervisor(str(root), spawn=spawn)
    sup.start("backend", ["/bin/true"], env={}, cwd=str(root), log="host.log")
    with pytest.raises(OSError):
        sup.start("app", ["/bin/true"], env={}, cwd=str(root), log="app.log")
    state = sup.stop_all("failed", "launch failed: OSError")
    assert spawned[0].sent == ["SIGTERM"] and state["state"] == "failed"
    assert oct((root / lifecycle.STATE).stat().st_mode & 0o777) == "0o600"
    # stop without a live supervisor signals nothing, whatever the record says.
    (root / lifecycle.STATE).write_text(json.dumps({"children": [{"pid": 1}, {"pid": os.getpid()}]}))
    outcome = lifecycle.stop(str(root))
    assert outcome["stopped_by"] == "none" and "nothing was signalled" in outcome["note"]
    (root / lifecycle.STATE).write_text("not json")
    assert lifecycle.stop(str(root))["last_recorded"] == {"unreadable": True}
    # Retire moves only a finished run, into a private archive; nothing is deleted.
    archive = OWNED / "archive"
    archive.mkdir(mode=0o700)
    with pytest.raises(lifecycle.RootError, match="not finished"):
        lifecycle.retire(str(root), str(archive))
    (root / lifecycle.STATE).write_text(json.dumps({"state": "stopped"}))
    moved = lifecycle.retire(str(root), str(archive))
    assert moved == str(archive / root.name) and not root.exists() and (archive / root.name / "host.log").exists()


def test_roots_are_validated_before_anything_is_written(monkeypatch):
    from scripts.notch_evaluation import lifecycle
    home = os.path.realpath(os.path.expanduser("~"))
    import pwd
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    for bad in (real_home, os.path.join(real_home, ".moe"), os.path.join(real_home, "Applications"),
                "/Applications", "/", "/private/tmp", "relative/root"):
        with pytest.raises(lifecycle.RootError):
            lifecycle.validate_root(bad, fresh=True)
    assert home  # HOME is the synthetic one here; the password database names the real one
    # The scratch area comes from the OS, not from the TMPDIR the host redirected.
    import tempfile
    assert os.path.realpath(tempfile.gettempdir()) not in lifecycle.scratch_bases()
    assert any(str(OWNED).startswith(base + os.sep) for base in lifecycle.scratch_bases())
    base = OWNED / "roots"
    base.mkdir(mode=0o700)
    # An ancestor of a protected location is refused.
    monkeypatch.setattr(lifecycle, "protected_paths", lambda: (str(base / "a" / "b"),))
    with pytest.raises(lifecycle.RootError, match="protected"):
        lifecycle.validate_root(str(base / "a"), fresh=True)
    monkeypatch.undo()
    real = base / "real"
    real.mkdir(mode=0o700)
    (base / "link").symlink_to(real)
    with pytest.raises(lifecycle.RootError, match="link"):
        lifecycle.create_root(str(base / "link"))
    occupied = base / "occupied"
    occupied.mkdir(mode=0o700)
    (occupied / "sentinel").write_text("keep")
    with pytest.raises(lifecycle.RootError, match="not empty"):
        lifecycle.create_root(str(occupied))
    assert (occupied / "sentinel").read_text() == "keep" and oct(occupied.stat().st_mode & 0o777) == "0o700"
    shared = base / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    with pytest.raises(lifecycle.RootError, match="writable"):
        lifecycle.validate_root(str(shared), fresh=True)
    assert oct(shared.stat().st_mode & 0o777) == "0o777"
    with pytest.raises(lifecycle.RootError, match="parent"):
        lifecycle.create_root(str(base / "missing-parent" / "root"))
    created = lifecycle.create_root(str(base / "fresh"))
    assert created == str((base / "fresh").resolve()) and oct(os.stat(created).st_mode & 0o777) == "0o700"
    # The host config refuses a root outside the scratch area or sources outside the root.
    for bad in ({"owned_root": real_home}, {"source_roots": ("/etc",)}):
        with pytest.raises(ValueError):
            evaluation_host.HostConfig(**{**CONFIG.__dict__, **bad}).validate()


# --- inference binding -----------------------------------------------------

def test_every_completion_names_a_resident_reviewed_model():
    import time
    from service.tools import imessage_tools
    msgs = [{"role": "user", "content": "SYNTHETIC"}]
    model = MODELS[0]

    def completions() -> int:
        return sum(1 for entry in ENGINE_LOG if entry == ("POST", "/v1/chat/completions"))

    async def drain(client, name):
        async for _ in client.stream(name, msgs):
            pass

    async def run():
        client = main.OMLXClient()
        before = completions()
        for call_ in (lambda: client.chat("Unlisted-Model", msgs), lambda: drain(client, "Unlisted-Model")):
            with pytest.raises(Exception):
                await call_()
        NOT_RESIDENT.add(model)
        saved = imessage_tools._client
        try:
            for call_ in (lambda: client.chat(model, msgs), lambda: drain(client, model)):
                with pytest.raises(Exception):
                    await call_()
            # The reviewed summarize_messages path, with its own nested client.
            imessage_tools._client = main.OMLXClient()
            now = time.time()
            rows = [(now - 3600 + i * 60, "Synthetic Friend", f"SYNTHETIC plan detail {i} about the trip")
                    for i in range(12)]
            await imessage_tools._summarize(rows, "today")
        finally:
            NOT_RESIDENT.discard(model)
            if imessage_tools._client is not saved:
                await imessage_tools._client.aclose()
            imessage_tools._client = saved
        assert completions() == before, "a refused completion was forwarded"
        reasons = [r.get("reason", "") for r in boundary.receipts() if r["sink"] == "inference"]
        assert any("not a reviewed evaluation model" in r for r in reasons)
        assert sum("not resident" in r for r in reasons) >= 3, reasons
        reply = await client.chat(model, msgs)                 # a resident reviewed model still answers
        assert reply["choices"][0]["message"]["content"] == "SYNTHETIC ANSWER"
        assert completions() == before + 1
        await client.aclose()
    asyncio.run(run())


# --- mandatory evidence (last: the failure is sticky) ----------------------

def test_receipt_destinations_must_be_private_files_inside_the_root():
    bad = OWNED / "evidence" / "bad-receipts"
    bad.mkdir()
    for path, label in ((str(bad), "a directory"), ("/private/tmp/x-receipts.jsonl", "outside the root")):
        with pytest.raises(ValueError):
            boundary._open_receipts(path, str(OWNED))
    shared = OWNED / "evidence" / "shared.jsonl"
    shared.write_text("")
    os.chmod(shared, 0o644)
    with pytest.raises(ValueError, match="private"):
        boundary._open_receipts(str(shared), str(OWNED))
    (OWNED / "evidence" / "linked.jsonl").symlink_to(shared)
    with pytest.raises(ValueError):
        boundary._open_receipts(str(OWNED / "evidence" / "linked.jsonl"), str(OWNED))


def test_zz_lost_evidence_stops_the_run():
    from service.tools.registry import run_tool
    assert boundary.evidence_failure() is None
    state = boundary._state
    # Retention bound reached: sticky failure, every dispatch blocked.
    state.written = boundary._MAX_RECEIPT_BYTES
    boundary._record("allowed", "probe", {})
    assert "retention bound" in (boundary.evidence_failure() or "")
    result = asyncio.run(run_tool(_tool("calculate"), {"expression": "2+2"}))
    assert "EVALUATION_BLOCKED" in result and "evidence" in result
    health = asyncio.run(call("GET", "/health"))
    assert health["status"] == 503
    identity = asyncio.run(call("GET", "/evaluation/identity"))
    assert identity["status"] == 200 and json.loads(identity["body"])["evidence_failure"]
    # A write that fails (a descriptor that cannot be written) is sticky too.
    state.failure, state.written = None, 0
    readonly = os.open(OWNED / "evidence" / "boundary.jsonl", os.O_RDONLY)
    saved, state.fd = state.fd, readonly
    try:
        boundary._record("allowed", "probe", {})
        assert "could not be persisted" in (boundary.evidence_failure() or "")
        boundary._record("allowed", "probe", {})
        assert boundary.evidence_failure()                    # still failed
    finally:
        state.fd = saved
        os.close(readonly)
    assert "EVALUATION_BLOCKED" in asyncio.run(run_tool(_tool("calculate"), {"expression": "2+2"}))
