"""/resetwisp's service side (service/wisp_reset.py).

Everything runs on a scratch home under pytest's temp dir: a fake user home with
a fake ~/.moe seeded with every kind of Wisp state plus model files. Nothing
touches the real ~/.moe, Google, the Keychain, TCC or a live service.
"""
from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import wisp_reset as R

# Every kind of state Wisp keeps in its home, as top-level names.
DELETED = {
    # caches: mail, messages, notes, calendar/reminders sync, contacts, brief
    "cache",
    # chats and sessions, memories and facts, research, Today's store
    "sessions.db", "sessions.db-wal", "sessions.db-shm", "facts.db", "facts.db-wal",
    "assistant.db", "assistant.db-wal", "assistant.db-shm",
    "research.db", "research.db-wal", "research.db-shm", "research_cache", "knowledge",
    # learned behavior and routines
    "behavior.db", "behavior.db-wal", "behavior.db-shm", "behavior_runtime.db",
    # About you, check-ins, brief state, daily workspace, attention, proactive
    "profile.json", "profile.json.bad-20261026", "checkins.json", "brief_state.json",
    "daily-workspace", "attention", "proactive",
    # outbox receipts, audit log, diagnostics, debug exports, corpus runs
    "assistant-delivery-receipts.json", "audit.jsonl", "diagnostics", "debug-exports",
    "corpus-runs", "corpus-prompts.json",
    # Google: tokens, workspace-mcp credentials and its uv install
    "gmail.json", "google_workspace", "uv",
    # permission grants, user MCP servers, skills
    "grants.json", "mcp.json", "skills",
    # everything else Wisp writes, and whatever a later version adds
    "backend-runtime.json", "codex_monitor_state.json", "node-inbox.db", "phone-gateway",
    "something-a-later-version-adds.json",
    # links: removed as links, their targets untouched
    "linked-out", "linked-dir",
}
KEPT = {"engine", "config.yaml", "provisioning", ".credential-generation", ".helper-transaction.json",
        "omlx-runtime-authorization.json", ".provisioning.lock", ".config.lock", ".engine-mode.lock"}
MODEL = "engine/models/Jundot/gemma-4-E4B-it-oQ4e-mtp/model.safetensors"
CONFIG = {
    "engine": {"mode": "bundled", "port": 8791},
    "installed_models": ["gemma-4-E4B-it-oQ4e-mtp"],
    "roles": {"general": "gemma-4-E4B-it-oQ4e-mtp", "agent": "gemma-4-E4B-it-oQ4e-mtp"},
    "inference": {
        "bindings": {
            "general": {"endpoint": "local", "model_id": "gemma-4-E4B-it-oQ4e-mtp"},
            "reasoning": {"endpoint": "cloud", "model_id": "frontier-model"},
        },
        "endpoints": {"cloud": {"enabled": True, "base_url": "https://api.example.invalid"}},
    },
    "remote_parity": True,
    "daily_summary_times": {"morning": 7, "evening": 21},
    "behavior": {"enabled": True},
    "proactive_auto_add": True,
}


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def world(tmp_path):
    user = tmp_path / "user"
    home = user / ".moe"
    outside = tmp_path / "outside"
    _write(outside / "precious.txt", "not Wisp's")
    _write(outside / "dir" / "inner.txt", "not Wisp's either")
    for name in DELETED - {"linked-out", "linked-dir"}:
        if "." in name and not name.startswith("cache"):
            _write(home / name)
        else:
            _write(home / name / "item.json")
    _write(home / "cache" / "mail_gists.json")
    _write(home / "cache" / "messages.txt")
    _write(home / "cache" / "notes.txt")
    (home / "cache" / "escape").symlink_to(outside / "precious.txt")
    (home / "diagnostics" / "escape-dir").symlink_to(outside / "dir", target_is_directory=True)
    (home / "linked-out").symlink_to(outside / "precious.txt")
    (home / "linked-dir").symlink_to(outside / "dir", target_is_directory=True)
    _write(home / MODEL, "weights")
    _write(home / "engine" / "base" / "settings.json", "{}")
    _write(home / "engine" / "base" / "model_settings.json", "{}")
    _write(home / "engine" / "home" / "state.json", "{}")
    _write(home / "engine" / "memory_guard.json", "{}")
    _write(home / "engine" / "engine.log", "request text")
    _write(home / "provisioning" / "endpoints.json", "{}")
    for name in (".credential-generation", ".helper-transaction.json", "omlx-runtime-authorization.json",
                 ".provisioning.lock", ".config.lock", ".engine-mode.lock"):
        _write(home / name, "")
    (home / "config.yaml").write_text(yaml.safe_dump(CONFIG))
    return {"user": user, "home": home, "outside": outside}


def test_reset_removes_exactly_the_delete_list_and_keeps_the_model(world):
    home, user = world["home"], world["user"]
    assert {p.name for p in home.iterdir()} == DELETED | KEPT
    result = R.erase_home(home, user)
    assert result["failed"] == []
    assert set(result["removed"]) == DELETED | {"engine/engine.log"}
    assert set(result["kept"]) == KEPT
    assert {p.name for p in home.iterdir()} == KEPT
    assert R.leftover_entries(home) == []
    # The model, the engine's settings and key, and its credential plumbing survive.
    assert (home / MODEL).read_text() == "weights"
    for kept in ("engine/base/settings.json", "engine/base/model_settings.json", "engine/home/state.json",
                 "engine/memory_guard.json", "provisioning/endpoints.json"):
        assert (home / kept).exists(), kept
    assert not (home / "engine" / "engine.log").exists()
    # Links were removed as links; what they pointed at outside is untouched.
    assert (world["outside"] / "precious.txt").read_text() == "not Wisp's"
    assert (world["outside"] / "dir" / "inner.txt").read_text() == "not Wisp's either"


def test_config_keeps_only_the_engine_and_model_choice(world):
    R.erase_home(world["home"], world["user"])
    assert yaml.safe_load((world["home"] / "config.yaml").read_text()) == {
        "engine": {"mode": "bundled"},
        "installed_models": ["gemma-4-E4B-it-oQ4e-mtp"],
        "roles": {"general": "gemma-4-E4B-it-oQ4e-mtp", "agent": "gemma-4-E4B-it-oQ4e-mtp"},
        "inference": {"bindings": {"general": {"endpoint": "local", "model_id": "gemma-4-E4B-it-oQ4e-mtp"}}},
    }
    assert (world["home"] / "config.yaml").stat().st_mode & 0o077 == 0


def test_the_keep_list_is_explicit():
    assert R.KEEP_ENTRIES == frozenset(KEPT)
    assert R.ENGINE_DELETE == frozenset({"engine.log"})
    assert R.trimmed_config(None) == {} and R.trimmed_config(["x"]) == {}


def test_a_linked_config_is_dropped_not_followed(world):
    home = world["home"]
    (home / "config.yaml").unlink()
    (home / "config.yaml").symlink_to(world["outside"] / "precious.txt")
    R.erase_home(home, world["user"])
    assert not (home / "config.yaml").exists() and not (home / "config.yaml").is_symlink()
    assert (world["outside"] / "precious.txt").read_text() == "not Wisp's"


def test_a_database_waits_for_its_write_ahead_log(world, monkeypatch):
    home = world["home"]
    real_remove = R._remove

    def remove(path):
        if path.name == "assistant.db-wal":
            raise PermissionError("held")
        real_remove(path)

    monkeypatch.setattr(R, "_remove", remove)
    result = R.erase_home(home, world["user"])
    # The log stayed, so the database stays with it: a fresh database never
    # replays an old log on the next launch.
    assert {"assistant.db", "assistant.db-wal"} <= set(result["failed"])
    assert (home / "assistant.db").exists() and (home / "assistant.db-wal").exists()
    assert not (home / "sessions.db").exists()


def test_home_sanity_checks_refuse(tmp_path, world):
    user, home = world["user"], world["home"]
    for bad in (Path("/"), user, tmp_path / "elsewhere" / ".moe", user / "Documents"):
        bad.mkdir(parents=True, exist_ok=True) if bad != Path("/") else None
        with pytest.raises(R.ResetRefused):
            R.erase_home(bad, user)
    with pytest.raises(R.ResetRefused):
        R.checked_home(user / "missing" / ".moe", user)
    with pytest.raises(R.ResetRefused):
        R.checked_home(Path("relative/.moe"), user)
    # ~/.moe as a link to another place is refused, and nothing there is touched.
    other = tmp_path / "linked-user"
    other.mkdir()
    (other / ".moe").symlink_to(home, target_is_directory=True)
    with pytest.raises(R.ResetRefused):
        R.erase_home(other / ".moe", other)
    assert {p.name for p in home.iterdir()} == DELETED | KEPT
    assert R.checked_home(home, user) == home.resolve()


def test_after_a_reset_the_service_is_a_first_launch(world, monkeypatch):
    """Every first-run marker the service keeps lives in Wisp's home and goes."""
    from service.assistant import store as store_module
    home = world["home"]
    for name in ("assistant.db", "assistant.db-wal", "assistant.db-shm"):
        (home / name).unlink()
    onboarded = store_module.AssistantStore(home / "assistant.db")
    onboarded.record_completion(store_module.FIRST_LAUNCH_KEY, repr(1_700_000_000.0))
    onboarded.record_completion("daily_brief:day_one", '{"day": "2023-11-14"}')
    assert onboarded.first_launch_at() == 1_700_000_000.0
    onboarded._db.close()
    markers = ["profile.json", "checkins.json", "brief_state.json", "proactive", "grants.json"]
    assert all((home / m).exists() for m in markers)

    R.erase_home(home, world["user"])

    for marker in markers + ["assistant.db"]:
        assert not (home / marker).exists(), marker
    fresh = store_module.AssistantStore(home / "assistant.db")
    try:
        first = fresh.first_launch_at()
        # A new first launch: the day-one Daily Summary hold and the 3-day routine
        # trial (behavior_runtime.AUTO_ACTION_TRIAL_S) start over from now.
        assert first is not None and first > 1_700_000_000.0
        assert fresh.completion("daily_brief:day_one") is None
    finally:
        fresh._db.close()


# --------------------------------------------------------------------------
# Only the app may call it
# --------------------------------------------------------------------------

def _app():
    app = FastAPI()
    app.include_router(R.router)
    return app


def test_the_endpoint_refuses_callers_other_than_the_app(monkeypatch):
    erased = []
    monkeypatch.setattr(R, "erase_home", lambda *a: erased.append(a))
    client = TestClient(_app())
    body = {"confirm": R.CONFIRM_PHRASE}
    # A web page: any Origin is refused.
    assert client.post(R.ROUTE, json=body, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 403
    # A browser simple request (form body) is refused.
    assert client.post(R.ROUTE, data={"confirm": R.CONFIRM_PHRASE}).status_code == 415
    # A native JSON call from anything but the parent app is refused.
    assert client.post(R.ROUTE, json=body).status_code == 403
    assert erased == []


def test_the_parent_app_check_reads_the_real_socket_table(monkeypatch):
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", 0))
    except PermissionError:
        server.close()
        pytest.skip("this sandbox does not allow a loopback listener")
    server.listen(1)
    caller = socket.create_connection(server.getsockname())
    accepted, _ = server.accept()
    try:
        client_end, server_end = caller.getsockname(), server.getsockname()
        assert R._tcp_peers(os.getpid(), client_end, server_end) == 1
        assert R._tcp_peers(os.getpid(), server_end, client_end) == 1
        # pytest's parent (a shell or runner) holds no end of this connection.
        assert R.caller_is_parent_app(client_end, server_end) is False
        # The connection's owner passes only when it is the Wisp app's executable.
        from service.inference import local_peer
        monkeypatch.setattr(local_peer, "process_path", lambda pid: "/usr/local/bin/python3")
        assert R.caller_is_parent_app(client_end, server_end, parent=os.getpid()) is False
        monkeypatch.setattr(local_peer, "process_path", lambda pid: "/Applications/Wisp.app/Contents/MacOS/Wisp")
        assert R.caller_is_parent_app(client_end, server_end, parent=os.getpid()) is True
        # A connection the "parent" does not hold is refused.
        assert R.caller_is_parent_app(("127.0.0.1", 1), server_end, parent=os.getpid()) is False
        assert R.caller_is_parent_app(client_end, server_end, parent=1) is False
    finally:
        for s in (caller, accepted, server):
            s.close()


def _post(app, body, client=("127.0.0.1", 50123)):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as http:
            return await http.post(R.ROUTE, json=body)
    return asyncio.run(go())


def test_the_app_flow_stops_work_then_disconnects_then_seals_then_erases(world, monkeypatch):
    import service.paths
    monkeypatch.setattr(R, "state", R._State())
    monkeypatch.setattr(R, "caller_is_parent_app", lambda client, server: True)
    monkeypatch.setattr(service.paths, "MOE_DIR", world["home"])
    monkeypatch.setenv("HOME", str(world["user"]))
    order = []

    async def stop(app, problems):
        order.append("stop")

    async def google(problems):
        order.append("google")
        problems.append({"part": "google_revoke", "detail": "offline"})
        return 2

    async def learned(problems):
        order.append("learned")

    real_erase = R.erase_home

    def erase(home, user_home):
        assert R.state.sealed, "nothing else may run while files go"
        order.append("erase")
        return real_erase(home, user_home)

    monkeypatch.setattr(R, "_stop_background", stop)
    monkeypatch.setattr(R, "_disconnect_google", google)
    monkeypatch.setattr(R, "_forget_learned", learned)
    monkeypatch.setattr(R, "erase_home", erase)
    app = _app()
    app.add_middleware(R.ResetGate)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/assistant/next")
    def other():
        return {"ok": True}

    assert _post(app, {"confirm": True}).status_code == 422
    assert order == []
    reply = _post(app, {"confirm": R.CONFIRM_PHRASE})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert order == ["stop", "google", "learned", "erase"]
    assert data["ok"] is False and data["google_accounts"] == 2
    assert [p["part"] for p in data["problems"]] == ["google_revoke"]
    assert set(data["kept"]) == KEPT
    assert {p.name for p in world["home"].iterdir()} == KEPT
    # Sealed: only "are you up" and the engine stop get through; a second reset is refused.
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/assistant/next").status_code == 503
    assert _post(app, {"confirm": R.CONFIRM_PHRASE}).status_code == 503


def test_a_refused_home_deletes_nothing(world, monkeypatch, tmp_path):
    import service.paths
    monkeypatch.setattr(R, "state", R._State())
    monkeypatch.setattr(R, "caller_is_parent_app", lambda client, server: True)
    sandbox = tmp_path / "sandbox-home"
    sandbox.mkdir()
    (sandbox / "assistant.db").write_text("x")
    monkeypatch.setattr(service.paths, "MOE_DIR", sandbox)
    monkeypatch.setenv("HOME", str(world["user"]))
    called = []
    monkeypatch.setattr(R, "_stop_background", lambda *a: called.append("stop"))
    reply = _post(_app(), {"confirm": R.CONFIRM_PHRASE})
    assert reply.status_code == 409
    assert called == [] and (sandbox / "assistant.db").exists() and not R.state.sealed


def test_the_model_and_agent_cannot_reach_it():
    import service.main  # noqa: F401 — registers every tool and mounts every route
    from service.safety import policy
    from service.tools.registry import REGISTRY
    for name, tool in REGISTRY.items():
        text = f"{name} {tool.description}".lower()
        assert "reset_wisp" not in text and "resetwisp" not in text, name
    for command in ("curl -X POST -H 'Content-Type: application/json' "
                    "http://127.0.0.1:8765/assistant/reset_wisp -d '{\"confirm\": \"reset wisp\"}'",
                    "curl localhost:8765//assistant//reset_wisp"):
        assert policy.local_send_endpoint_hit(command)
    # Mounted on the real app (and gated there): a native call that is not the
    # parent app reaches the route and is refused by it, not by a missing route.
    reply = TestClient(service.main.app).post(R.ROUTE, json={"confirm": R.CONFIRM_PHRASE})
    assert reply.status_code == 403 and reply.json()["detail"] == "Only the Wisp app can reset Wisp."
    assert any(m.cls is R.ResetGate for m in service.main.app.user_middleware)
