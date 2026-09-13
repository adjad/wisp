"""Entrypoint/bundle and Pro compatibility without installed apps or live data."""
import argparse
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from unittest.mock import AsyncMock

import httpx
import pytest

from mini.__main__ import ENV_KEYS, application, main
from mini.build_bundle import build
from mini.gateway import Gateway
from mini.node import Node
from mini.protocol import KINDS
from mini.store import Store
from tests.test_mini_http import TOKEN, UPSTREAM_TOKEN, Stream, response


async def test_node_auth_routes_disabled_jobs_and_sanitized_status(tmp_path):
    store = Store(tmp_path.resolve() / "private", "fixture-mini")
    app = Node(TOKEN, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://node.test") as client:
        for path in ("/healthz", "/v1/status", "/v1/results"):
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
        client.headers["Authorization"] = "Bearer " + TOKEN
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        status = (await client.get("/v1/status")).json()
        assert status["jobs_enabled"] is status["connectors_enabled"] is status["effects_enabled"] is False
        assert status["pending_occurrences"] == status["results"] == 0
        assert "fixture-mini" not in json.dumps(status) and str(tmp_path) not in json.dumps(status)
        assert (await client.post("/v1/jobs", json={"enabled": True})).status_code == 404
        assert (await client.get("/v1/results?cursor=&limit=100")).json() == {"schema_version": 1, "results": [], "next_cursor": ""}
        for query in ("limit=0", "limit=101", "limit=-1", "limit=1&limit=2", "x=1", "cursor=tampered", "limit=abc"):
            assert (await client.get("/v1/results?" + query)).status_code == 400
    restarted = Node(TOKEN, Store(store.path.parent, "fixture-mini"))
    assert restarted.store.status() == status
    with store.connect() as db:
        assert {r[0] for r in db.execute("SELECT kind FROM jobs")} == KINDS
        assert {r[0] for r in db.execute("SELECT enabled FROM jobs")} == {0}


async def test_pro_poll_and_effect_presentation_only(tmp_path, monkeypatch):
    monkeypatch.setenv("WISP_HOME", str(tmp_path.resolve() / "pro-state"))
    from service import nodes
    from service.config.endpoints import Endpoint
    store = Store(tmp_path.resolve() / "private", "fixture-mini")
    app = Node(TOKEN, store)
    for index, effect in enumerate(("email.send", "message.send", "calendar.write")):
        oid = store.stage_occurrence("effect.proposal", index * 3600)
        row = store.result("effect.proposal", oid, "effect.proposal", "Synthetic proposal", "Untrusted presentation",
                           {"effect_id": f"fixture-{index}", "kind": effect, "arguments": {"url": "file:///not-read", "approved": True}})
        store.complete(row)
    monkeypatch.setenv("SYNTHETIC_NODE_KEY", TOKEN)
    monkeypatch.setattr(nodes, "endpoint", lambda name: Endpoint(name, "https://node.test", "env:SYNTHETIC_NODE_KEY"))
    inbox = nodes.NodeInbox(tmp_path.resolve() / "pro-state" / "inbox.sqlite3")
    hub = type("Hub", (), {"publish": AsyncMock()})()
    await nodes.poll_once(inbox, hub, {"node_id": "fixture-mini", "endpoint": "mini-node"}, transport=httpx.ASGITransport(app))
    assert hub.publish.await_count == 3
    for call in hub.publish.call_args_list:
        event = call.args[0]
        assert event["type"] == "node_result" and "not executed" in event["text"]
        assert not {"arguments", "approved", "action_id"} & event.keys()
    await nodes.poll_once(inbox, hub, {"node_id": "fixture-mini", "endpoint": "mini-node"}, transport=httpx.ASGITransport(app))
    assert hub.publish.await_count == 3


async def test_actual_omlx_client_through_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("WISP_HOME", str(tmp_path.resolve() / "pro-state"))
    from service.inference.omlx_client import OMLXClient, IncompleteStreamError
    mode = "normal"
    def upstream(request):
        if request.url.path == "/health":
            return response({"status": "ok"})
        if request.url.path == "/v1/models":
            return response({"data": [{"id": "fixture"}]})
        if not json.loads(request.content).get("stream"):
            return response({"choices": [{"message": {"content": "fixture"}, "finish_reason": "stop"}]})
        delta = {"content": "fixture"} if mode != "tools" else {"tool_calls": [{"index": 0, "id": "fixture-call", "function": {"name": "synthetic", "arguments": "{}"}}]}
        lines = [b"data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": None}]}).encode() + b"\n\n"]
        if mode != "truncated":
            lines += [b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls" if mode == "tools" else "stop"}]}).encode() + b"\n\n", b"data: [DONE]\n\n"]
        return response(stream=Stream(lines), headers={"content-type": "text/event-stream"})
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(upstream))
    client = OMLXClient(base_url="https://mini.test", api_key=TOKEN)
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://mini.test", transport=httpx.ASGITransport(app), headers={"Authorization": "Bearer " + TOKEN})
    try:
        await client.ensure_only("fixture")
        assert (await client.chat("fixture", [{"role": "user", "content": "fixture"}]))["choices"][0]["message"]["content"] == "fixture"
        for selected in ("normal", "tools"):
            mode = selected
            events = [event async for event in client.stream_events("fixture", [{"role": "user", "content": "fixture"}])]
            assert events[-1]["kind"] == "final"
            if mode == "tools":
                assert events[-1]["message"]["tool_calls"][0]["function"]["name"] == "synthetic"
        mode = "truncated"
        with pytest.raises(IncompleteStreamError):
            _ = [event async for event in client.stream_events("fixture", [{"role": "user", "content": "fixture"}])]
    finally:
        await client.aclose()


def test_entrypoints_fixed_loopback_and_secret_consumption(tmp_path, monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("WISP_MINI_INFERENCE_KEY", TOKEN)
    monkeypatch.setenv("WISP_LOCAL_OMLX_KEY", UPSTREAM_TOKEN)
    monkeypatch.setattr(sys, "argv", ["mini", "gateway"])
    assert main() == 0
    import os
    assert not any(key in os.environ for key in ENV_KEYS)
    monkeypatch.setenv("WISP_MINI_NODE_KEY", TOKEN)
    monkeypatch.setattr(sys, "argv", ["mini", "node", "--state-dir", str(tmp_path.resolve() / "private"), "--node-id", "fixture-mini"])
    assert main() == 0
    assert [call["port"] for call in calls] == [8765, 8766]
    assert all(call["host"] == "127.0.0.1" and call["workers"] == 1 and call["proxy_headers"] is False and call["access_log"] is False for call in calls)


@pytest.mark.parametrize("key", [None, "", "short", "x" * 32 + "\n", "\u20ac" * 32])
def test_invalid_config_fails_before_binding(key, monkeypatch, capsys):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    if key is not None:
        monkeypatch.setenv("WISP_MINI_INFERENCE_KEY", key)
    monkeypatch.setenv("WISP_LOCAL_OMLX_KEY", UPSTREAM_TOKEN)
    monkeypatch.setattr(sys, "argv", ["mini", "gateway"])
    assert main() == 1
    assert capsys.readouterr().err == "mini-runtime: startup or service failure\n"


def test_bundle_determinism_digests_and_no_secrets(tmp_path):
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    manifest = build(first)
    assert build(second) == manifest and first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        members = archive.getmembers()
        assert all(m.isfile() and m.name.startswith("mini/") and ".." not in m.name for m in members)
        files = {m.name: archive.extractfile(m).read() for m in members}
    assert set(files) == {entry["path"] for entry in manifest["files"]} | {"mini/bundle.json"}
    for entry in manifest["files"]:
        assert hashlib.sha256(files[entry["path"]]).hexdigest() == entry["sha256"]
    assert TOKEN.encode() not in b"".join(files.values())
    assert manifest["services"]["gateway"]["upstream"] == "http://127.0.0.1:8000"
    assert "service/" not in " ".join(files)
    with pytest.raises(FileExistsError):
        build(first)


def test_standalone_import_graph_cannot_load_effects(tmp_path):
    # Separate interpreter avoids backend imports performed by compatibility tests.
    code = """
import importlib.abc, sys, subprocess, socket
def forbidden(*args, **kwargs):
    raise AssertionError('forbidden external effect')
subprocess.Popen = forbidden
socket.create_connection = forbidden
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'service','sandbox','air'}:
            raise AssertionError('forbidden import')
sys.meta_path.insert(0, Block())
import mini.node, mini.protocol, mini.store
from mini.store import Store
from mini.node import Node
s=Store(sys.argv[1], 'synthetic')
n=Node('f'*64,s)
assert s.status()['pending_occurrences']==0
"""
    run = subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path.resolve() / "private")], capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr.decode()


def test_extracted_bundle_runs_without_repository_imports(tmp_path):
    archive_path = tmp_path / "runtime.tar.gz"
    build(archive_path)
    extracted = tmp_path.resolve() / "extracted"
    extracted.mkdir()
    with tarfile.open(archive_path) as archive:
        archive.extractall(extracted, filter="data")
    code = """
import argparse, importlib.abc, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'service','sandbox','air'}:
            raise AssertionError('repository dependency')
sys.meta_path.insert(0, Block())
from mini.__main__ import application
os.environ['WISP_MINI_INFERENCE_KEY']='a'*64
os.environ['WISP_LOCAL_OMLX_KEY']='b'*64
gateway,port=application(argparse.Namespace(service='gateway'))
assert port==8765
os.environ['WISP_MINI_NODE_KEY']='c'*64
node,port=application(argparse.Namespace(service='node',state_dir=sys.argv[2],node_id='synthetic'))
assert port==8766 and node.store.status()['jobs_enabled'] is False
import mini
assert Path(mini.__file__).is_relative_to(sys.argv[1])
assert not any(name.startswith('service') for name in sys.modules)
"""
    run = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(extracted), str(tmp_path.resolve() / "state")],
                         cwd=extracted, capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr.decode()
