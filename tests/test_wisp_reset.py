"""/resetwisp's service side (service/wisp_reset.py).

Everything runs on a scratch home under pytest's temp dir: a fake user home with
a fake ~/.moe seeded with every kind of Wisp state plus model files. Nothing
touches the real ~/.moe, Google, the Keychain, TCC or a live service.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import wisp_reset as R

REAL_ERASE = R.erase_home

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


@pytest.fixture(autouse=True)
def no_live_service(monkeypatch):
    """Never probe the real service port from a test: no service is running."""
    monkeypatch.setattr(R, "_service_live", lambda port=8765: False)


def _marker(home, **fields):
    """Write a marker the way the route does, with any fields changed."""
    doc = {**R.new_marker(), **fields}
    R.write_marker(home, doc)
    return doc


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
    _write(home / "engine" / "base" / "cache" / "kv-0001.safetensors", "prompt cache")
    _write(home / "engine" / "base" / "logs" / "server.log", "request text")
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
                 "engine/memory_guard.json", "provisioning/endpoints.json",
                 # The running engine's prompt cache waits for the stopped engine (resume_pending).
                 "engine/base/cache/kv-0001.safetensors", "engine/base/logs/server.log"):
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
    assert R.KEEP_ENTRIES == frozenset(KEPT | {R.MARKER, R.APP_DONE})
    assert R.MARKER == ".reset-pending" and R.APP_DONE == ".reset-pending.app" and R.MAX_ATTEMPTS == 3
    assert R.ENGINE_DELETE == frozenset({"engine.log"})
    assert R.ENGINE_CACHES == ("base/cache", "base/logs")
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

    def remove(path, device=None):
        if path.name == "assistant.db-wal":
            raise PermissionError("held")
        real_remove(path, device)

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


def _flow(world, monkeypatch, erase=None):
    """The route on a scratch home, with Google and background work stubbed."""
    import service.paths
    monkeypatch.setattr(R, "state", R._State())
    monkeypatch.setattr(R, "caller_is_parent_app", lambda client, server: True)
    monkeypatch.setattr(service.paths, "MOE_DIR", world["home"])
    monkeypatch.setenv("HOME", str(world["user"]))
    order = []
    marker = lambda: R.read_marker(world["home"])  # noqa: E731

    async def stop(app, problems):
        assert R.state.sealed and marker()["phase"] == "started", "sealed and committed before any work stops"
        order.append("stop")

    async def google(problems):
        assert marker()["phase"] == "started", "the marker is on disk before anything destructive"
        order.append("google")
        problems.append({"part": "google_revoke", "detail": "Couldn't reach Google to revoke 1 sign-in."})
        return 2

    real_erase = R.erase_home

    def erase_step(home, user_home, **kwargs):
        assert R.state.sealed and not R.state.erased
        order.append("erase")
        if erase is not None:
            return erase(home, user_home)
        return real_erase(home, user_home, **kwargs)

    monkeypatch.setattr(R, "_stop_background", stop)
    monkeypatch.setattr(R, "_disconnect_google", google)
    monkeypatch.setattr(R, "erase_home", erase_step)
    app = _app()
    app.add_middleware(R.ResetGate)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/assistant/next")
    def other():
        return {"ok": True}

    return app, order


def test_the_app_flow_seals_commits_stops_disconnects_then_erases(world, monkeypatch):
    app, order = _flow(world, monkeypatch)
    assert _post(app, {"confirm": True}).status_code == 422
    assert order == [] and R.read_marker(world["home"]) is None and not R.state.sealed
    reply = _post(app, {"confirm": R.CONFIRM_PHRASE})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert order == ["stop", "google", "erase"]
    assert data["committed"] is True and data["erased"] is True and data["ok"] is False
    assert data["google_accounts"] == 2 and [p["part"] for p in data["problems"]] == ["google_revoke"]
    assert set(data["kept"]) == KEPT | {R.MARKER}
    assert {p.name for p in world["home"].iterdir()} == KEPT | {R.MARKER}
    # The route never says "erased": only the restart pass may (F1).
    marker = R.read_marker(world["home"])
    assert marker["phase"] == "first-pass" and marker["full_pass"] is False and marker["attempts"] == 0
    assert marker["problems"] == ["Couldn't reach Google to revoke 1 sign-in."]
    assert R.state.erased and not R.state.running
    # Sealed: "are you up" and the engine stop get through; everything else hears
    # the distinct "Wisp was reset.", and so does a second reset.
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    other = client.get("/assistant/next")
    assert other.status_code == 503 and other.json()["detail"] == R.ERASED
    again = _post(app, {"confirm": R.CONFIRM_PHRASE})
    assert again.status_code == 503 and again.json()["detail"] == R.ERASED


def test_a_failed_erase_is_reported_and_never_claimed_finished(world, monkeypatch):
    def broken(home, user_home):
        raise PermissionError("disk said no")

    app, order = _flow(world, monkeypatch, erase=broken)
    reply = _post(app, {"confirm": R.CONFIRM_PHRASE})
    assert reply.status_code == 200
    data = reply.json()
    assert data["committed"] is True and data["erased"] is False
    assert "Google was signed out, but its files were not deleted" in data["detail"]
    assert "Nothing was reset" not in json.dumps(data)
    # Not running, not erased: other requests hear "resetting", never "reset",
    # the marker still says the service's part is unfinished, and a retry runs again.
    assert not R.state.running and not R.state.erased and R.state.sealed
    assert R.read_marker(world["home"])["phase"] == "started"
    other = TestClient(app).get("/assistant/next")
    assert other.status_code == 503 and other.json()["detail"] == R.RESETTING
    assert (world["home"] / "assistant.db").exists()
    order.clear()
    monkeypatch.setattr(R, "_disconnect_google", lambda problems: asyncio.sleep(0, result=0))
    reply = _post(app, {"confirm": R.CONFIRM_PHRASE})
    assert reply.json()["erased"] is False and order == ["stop", "erase"]


def test_the_marker_is_written_before_anything_destructive_and_a_failure_to_write_refuses(world, monkeypatch):
    app, order = _flow(world, monkeypatch)

    def no_disk(home, doc):
        raise OSError("read-only")

    monkeypatch.setattr(R, "write_marker", no_disk)
    reply = _post(app, {"confirm": R.CONFIRM_PHRASE})
    assert reply.status_code == 500 and reply.json()["detail"].endswith("Nothing was reset.")
    assert order == [] and not R.state.sealed and not R.state.running
    assert (world["home"] / "assistant.db").exists()


def test_background_that_will_not_stop_is_reported(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(R, "STOP_TIMEOUT", 0.05)

    async def stubborn():
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue

    async def go():
        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        app = SimpleNamespace(state=SimpleNamespace(wisp_background_tasks=[task], wisp_bridge_link=None))
        problems = []
        from service.assistant import behavior_runtime
        monkeypatch.setattr(behavior_runtime, "stop_for_reset", lambda: None)
        await R._stop_background(app, problems)
        task.cancel()
        return problems, task

    loop = asyncio.new_event_loop()
    try:
        problems, task = loop.run_until_complete(go())
    finally:
        for pending in asyncio.all_tasks(loop):
            pending.cancel()
        loop.close()
    assert any(p["part"] == "background" and "didn't stop in time" in p["detail"] for p in problems)


# --------------------------------------------------------------------------
# An interrupted reset is finished by the next service start
# --------------------------------------------------------------------------

def test_the_route_then_the_restart_pass_reach_erased_only_after_resume(world, monkeypatch):
    """F1: the route ends at "first-pass"; the app waits; only the restart pass,
    with the old service gone and the engine stopped, writes "erased"."""
    app, order = _flow(world, monkeypatch)
    assert _post(app, {"confirm": R.CONFIRM_PHRASE}).json()["committed"] is True
    home, user = world["home"], world["user"]
    assert R.read_marker(home)["phase"] == "first-pass"
    monkeypatch.setattr(R, "erase_home", REAL_ERASE)     # the next start is a fresh process
    # A late writer after the route's pass (the old service's shutdown) and the
    # engine's cache, which the running engine kept, are both still there.
    _write(home / "sessions.db", "written while the old service stopped")
    assert (home / "engine" / "base" / "cache" / "kv-0001.safetensors").exists()
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "erased" and doc["attempts"] == 1 and doc["full_pass"] and doc["engine_cache"]
    assert {p.name for p in home.iterdir()} == KEPT | {R.MARKER}
    assert not (home / "engine" / "base" / "cache").exists() and not (home / "engine" / "base" / "logs").exists()
    assert (home / MODEL).read_text() == "weights"
    assert doc["problems"] == ["Couldn't reach Google to revoke 1 sign-in."]
    # Done: later starts leave everything alone.
    _write(home / "sessions.db", "a new chat after the reset")
    assert R.resume_pending(home, user) is None
    assert (home / "sessions.db").read_text() == "a new chat after the reset"


def test_the_next_start_finishes_a_reset_cut_short(world):
    home, user = world["home"], world["user"]
    _marker(home, problems=["Couldn't reach Google to revoke 1 sign-in."])     # still "started"
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "erased"
    assert {p.name for p in home.iterdir()} == KEPT | {R.MARKER}
    assert (world["outside"] / "precious.txt").read_text() == "not Wisp's"


def test_a_running_engine_keeps_its_cache_until_it_stops(world, monkeypatch):
    home, user = world["home"], world["user"]
    (home / "engine" / "engine.pid").write_text(str(os.getpid()))
    engine_command = f"python -m omlx.cli serve --base-path {home / 'engine' / 'base'}"
    monkeypatch.setattr(R, "_command_of", lambda pid: engine_command)
    _marker(home, phase="first-pass")
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "first-pass" and doc["full_pass"] and not doc["engine_cache"]
    assert (home / "engine" / "base" / "cache" / "kv-0001.safetensors").exists()
    assert not (home / "assistant.db").exists()
    assert R.unfinished_reason(home) is None, "the whole-home pass is done: the service may start"
    (home / "engine" / "engine.pid").unlink()
    _write(home / "assistant.db", "a chat after the restart pass")
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "erased" and doc["engine_cache"] and doc["attempts"] == 2
    assert not (home / "engine" / "base" / "cache").exists()
    # Only the cache was retried: data created after the restart pass survives.
    assert (home / "assistant.db").read_text() == "a chat after the restart pass"


def test_a_stuck_entry_ends_after_three_attempts_and_spares_new_data(world, monkeypatch):
    """F2: retries cover only what the restart pass couldn't delete, at most
    MAX_ATTEMPTS starts, and then the reset is declared done with leftovers."""
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    real_remove = R._remove
    tried = []

    def remove(path, device=None):
        if path.name.endswith("diagnostics"):
            tried.append(R.display_name(path.name))
            raise PermissionError("a 0500 folder")
        real_remove(path, device)

    monkeypatch.setattr(R, "_remove", remove)
    doc = R.resume_pending(home, user)
    trash = R.TRASH_PREFIX + doc["id"] + "-diagnostics"
    assert doc["phase"] == "first-pass" and doc["pending"] == [trash] and doc["attempts"] == 1
    assert (home / trash).is_dir() and not (home / "diagnostics").exists()
    assert R.unfinished_reason(home) is None
    # The person starts using Wisp again: new chats and a new Google sign-in.
    _write(home / "sessions.db", "new chat")
    _write(home / "gmail.json", json.dumps({"accounts": {"me@example.com": {"refresh_token": "new"}}}))
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "first-pass" and doc["attempts"] == 2
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "erased" and doc["attempts"] == 3
    assert doc["problems"][-1] == "Couldn't delete: diagnostics"
    assert tried == ["diagnostics"] * 3
    assert (home / "sessions.db").read_text() == "new chat"
    assert json.loads((home / "gmail.json").read_text())["accounts"]
    # And it stays done: a fourth start does nothing.
    assert R.resume_pending(home, user) is None and tried == ["diagnostics"] * 3


def test_a_restart_pass_that_keeps_failing_still_ends(world, monkeypatch):
    home, user = world["home"], world["user"]
    _marker(home)

    def broken(*a, **k):
        raise OSError("disk said no")

    monkeypatch.setattr(R, "erase_home", broken)
    phases = []
    for _ in range(3):
        phases.append(R.resume_pending(home, user)["phase"])
        if phases[-1] != "erased":
            # The whole-home pass never completed: the service must not start on it.
            assert R.unfinished_reason(home) == "Wisp's data is not fully deleted yet"
    assert phases == ["started", "started", "erased"]
    assert "Wisp couldn't finish deleting its data." in R.read_marker(home)["problems"]
    assert R.resume_pending(home, user) is None and R.unfinished_reason(home) is None


def test_unrevoked_google_access_is_reported(world):
    home, user = world["home"], world["user"]
    (home / "gmail.json").write_text(json.dumps({"client_id": "x", "accounts": {"me@example.com": {
        "refresh_token": "still here"}}}))
    _marker(home)
    doc = R.resume_pending(home, user)
    assert R.GOOGLE_NOT_REVOKED in doc["problems"] and not (home / "gmail.json").exists()


def test_signed_out_google_is_not_reported(world):
    home, user = world["home"], world["user"]
    (home / "gmail.json").write_text(json.dumps({"client_id": "own client", "accounts": {}}))
    _marker(home, phase="first-pass")
    assert R.GOOGLE_NOT_REVOKED not in R.resume_pending(home, user)["problems"]


def test_a_live_service_postpones_the_restart_pass_and_the_start(world, monkeypatch):
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    monkeypatch.setattr(R, "_service_live", lambda port=8765: True)
    with pytest.raises(R.ResetIncomplete, match="still answers on port 8765"):
        R.resume_pending(home, user)
    with pytest.raises(SystemExit) as refused:
        R.resume_or_exit(home, user)
    assert refused.value.code == R.REFUSED_EXIT
    assert R.read_marker(home)["attempts"] == 0 and (home / "sessions.db").exists()


def test_without_a_marker_the_start_touches_nothing(world):
    before = sorted(p.name for p in world["home"].iterdir())
    assert R.resume_pending(world["home"], world["user"]) is None
    assert sorted(p.name for p in world["home"].iterdir()) == before


def test_a_marker_outside_wisps_home_never_erases(tmp_path, world):
    sandbox = tmp_path / "sandbox"
    _write(sandbox / "assistant.db")
    _marker(sandbox)
    with pytest.raises(R.ResetIncomplete, match="a reset never erases"):
        R.resume_pending(sandbox, world["user"])
    with pytest.raises(SystemExit):
        R.resume_or_exit(sandbox, world["user"])
    assert (sandbox / "assistant.db").exists() and R.read_marker(sandbox)["attempts"] == 0


@pytest.mark.parametrize("content", ["", "not json", json.dumps({"phase": "started"}),
                                     json.dumps({"version": 2, "phase": "started", "id": "a"}),
                                     json.dumps({"version": 1, "phase": "done", "id": "a"}),
                                     json.dumps({"version": 1, "phase": "started", "id": "../x"}),
                                     json.dumps({"version": 1, "phase": "started", "id": "a", "attempts": "1"})])
def test_junk_markers_are_ignored(world, content):
    """N2: junk or empty content is ignored and reported, never treated as a reset."""
    home, user = world["home"], world["user"]
    (home / R.MARKER).write_text(content)
    assert isinstance(R.read_marker(home), R.InvalidMarker)
    before = sorted(p.name for p in home.iterdir())
    assert R.resume_pending(home, user) is None
    assert sorted(p.name for p in home.iterdir()) == before


def test_a_marker_someone_else_owns_or_a_link_is_ignored(world, monkeypatch):
    home, user = world["home"], world["user"]
    _marker(home)
    real_uid = os.getuid()
    monkeypatch.setattr(R.os, "getuid", lambda: real_uid + 1)
    assert R.read_marker(home)["reason"] == "not a regular file owned by this user"
    assert R.resume_pending(home, user) is None and (home / "sessions.db").exists()
    monkeypatch.undo()
    monkeypatch.setattr(R, "_service_live", lambda port=8765: False)
    (home / R.MARKER).unlink()
    (home / R.MARKER).symlink_to(world["outside"] / "precious.txt")
    assert isinstance(R.read_marker(home), R.InvalidMarker)
    assert R.resume_pending(home, user) is None and (home / "sessions.db").exists()
    assert (world["outside"] / "precious.txt").read_text() == "not Wisp's"


def test_importing_the_service_with_a_marker_does_nothing(world):
    """N1: only the backend the app launched runs the restart pass; importing the
    service (a script, a tool, a test) with the real HOME never erases."""
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    root = Path(__file__).resolve().parents[1]
    env = {"HOME": str(user), "WISP_HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-c", "import service, service.paths, service.wisp_reset"],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert (home / "sessions.db").exists() and R.read_marker(home)["phase"] == "first-pass"
    assert (root / "service" / "__init__.py").read_text() == ""


def test_the_restart_pass_runs_first_in_main_and_only_for_the_launched_backend():
    """service/main.py calls resume_pending before any other import, guarded by
    the app's launch nonce."""
    source = (Path(__file__).resolve().parents[1] / "service" / "main.py").read_text()
    body = source.split('"""', 2)[2]
    first = [line for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")][:5]
    assert first == ["from __future__ import annotations",
                     "import os as _reset_os",
                     'if _reset_os.environ.get("WISP_LAUNCH_NONCE"):',
                     "    from service.wisp_reset import resume_or_exit as _resume_or_exit",
                     "    _resume_or_exit()"]


def test_reset_logs_go_to_stderr_only(world, capsys):
    _marker(world["home"])
    R.resume_pending(world["home"], world["user"])
    out = capsys.readouterr()
    assert out.out == "" and "[reset] restart pass 1" in out.err


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
    # Percent-encoded, the way uvicorn would decode it, is refused too.
    assert policy.local_send_endpoint_hit("curl -X POST http://127.0.0.1:8765/assistant/%72eset_wisp")
    # Writing the commit marker always asks first: a shell command or a file tool naming it.
    assert policy.shell_secret_hit({"cmd": "touch ~/.moe/.reset-pending"})
    assert any(str(root).endswith(".reset-pending") for root in policy._credential_roots())
    # Mounted on the real app (and gated there): a native call that is not the
    # parent app reaches the route and is refused by it, not by a missing route.
    reply = TestClient(service.main.app).post(R.ROUTE, json={"confirm": R.CONFIRM_PHRASE})
    assert reply.status_code == 403 and reply.json()["detail"] == "Only the Wisp app can reset Wisp."
    assert any(m.cls is R.ResetGate for m in service.main.app.user_middleware)


# --------------------------------------------------------------------------
# B1: a restart pass is recorded before it runs; the service never starts on an
# unrecorded or unfinished pass
# --------------------------------------------------------------------------

def _counting_erase(monkeypatch):
    erases = []
    real = R.erase_home

    def erase(*a, **k):
        erases.append(1)
        return real(*a, **k)

    monkeypatch.setattr(R, "erase_home", erase)
    return erases


def _start(home, user):
    """One backend start, as service/main.py runs it: True when the service starts."""
    try:
        R.resume_or_exit(home, user)
    except SystemExit as exc:
        assert exc.code == R.REFUSED_EXIT
        return False
    return True


def test_a_marker_that_cannot_be_written_erases_nothing_and_refuses_every_start(world, monkeypatch, capsys):
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    erases = _counting_erase(monkeypatch)

    def no_disk(home, doc):
        raise PermissionError("the marker is locked")

    monkeypatch.setattr(R, "write_marker", no_disk)
    for start in range(5):
        assert _start(home, user) is False
        _write(home / f"would-be-user-data-{start}.json")     # never written: nothing started
    assert erases == [] and (home / "sessions.db").exists()
    assert R.read_marker(home)["attempts"] == 0
    err = capsys.readouterr().err
    assert "Wisp's service is not starting" in err and "so it deleted nothing" in err


def test_an_immutable_marker_on_disk_erases_nothing(world, monkeypatch):
    """The auditor's probe: `chflags uchg` on the marker, five starts."""
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    erases = _counting_erase(monkeypatch)
    import stat as st
    path = home / R.MARKER
    try:
        os.chflags(path, st.UF_IMMUTABLE)
    except OSError:
        pytest.skip("chflags is not available here")
    try:
        results = [_start(home, user) for _ in range(5)]
    finally:
        os.chflags(path, 0)
    assert results == [False] * 5 and erases == []
    assert (home / "sessions.db").exists() and not (home / (R.MARKER + ".tmp")).exists()


def test_a_pass_whose_result_cannot_be_recorded_refuses_the_start_and_stays_bounded(world, monkeypatch):
    """The pass is counted before it runs; if its result can't be written the
    service doesn't start, so nothing it would write can be erased later."""
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    erases = _counting_erase(monkeypatch)
    real_write = R.write_marker

    def write(home, doc):
        if not doc["in_progress"]:
            raise OSError("disk full")
        real_write(home, doc)

    monkeypatch.setattr(R, "write_marker", write)
    results = [_start(home, user) for _ in range(6)]
    assert not any(results)
    assert len(erases) <= R.MAX_ATTEMPTS
    marker = R.read_marker(home)
    assert marker["attempts"] == R.MAX_ATTEMPTS and marker["in_progress"] is True


def test_a_pass_killed_mid_erase_is_never_followed_by_a_service_start(world, monkeypatch):
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")

    def killed(*a, **k):
        raise KeyboardInterrupt      # the process dies inside the pass

    monkeypatch.setattr(R, "erase_home", killed)
    with pytest.raises(KeyboardInterrupt):
        R.resume_pending(home, user)
    marker = R.read_marker(home)
    assert marker["in_progress"] is True and marker["attempts"] == 1
    assert R.unfinished_reason(home) == "the last restart pass was not recorded as finished"
    monkeypatch.setattr(R, "erase_home", REAL_ERASE)
    # The next start finishes it (no service ran in between) and then may start.
    assert _start(home, user) is True
    marker = R.read_marker(home)
    assert marker["phase"] == "erased" and marker["attempts"] == 2 and not marker["in_progress"]


def test_the_backend_refuses_to_start_on_an_unrecorded_pass(world):
    """service/main.py itself exits before importing anything else."""
    home, user = world["home"], world["user"]
    _marker(home, phase="started", attempts=1, in_progress=True)
    root = Path(__file__).resolve().parents[1]
    env = {"HOME": str(user), "WISP_HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
           "WISP_LAUNCH_NONCE": "test-nonce"}
    # Never probe the real service port, and no recorded pass may run: the marker
    # can't be written (as in the chflags probe), so the start is refused untouched.
    script = ("from service import wisp_reset as R\n"
              "R._service_live = lambda port=8765: False\n"
              "def no_disk(home, doc): raise PermissionError('locked')\n"
              "R.write_marker = no_disk\n"
              "import service.main\n"
              "print('STARTED')\n")
    done = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == R.REFUSED_EXIT, done.stderr[-2000:]
    assert "STARTED" not in done.stdout and "Wisp's service is not starting" in done.stderr
    assert (home / "sessions.db").exists() and R.read_marker(home)["attempts"] == 1


# --------------------------------------------------------------------------
# B2: what the restart pass can't delete goes to the trash; only the trash is retried
# --------------------------------------------------------------------------

def test_a_stuck_folder_is_retried_as_trash_and_new_data_in_its_name_survives(world):
    """The auditor's probe: chats/stuck at 0500; the fresh service writes chats/after1.json."""
    home, user = world["home"], world["user"]
    _write(home / "chats" / "old.json", "old chat")
    _write(home / "chats" / "stuck" / "inner.txt", "old")
    os.chmod(home / "chats" / "stuck", 0o500)
    _marker(home, phase="first-pass")
    try:
        doc = R.resume_pending(home, user)
        trash = R.TRASH_PREFIX + doc["id"] + "-chats"
        assert doc["pending"] == [trash] and doc["phase"] == "first-pass"
        assert not (home / "chats").exists() and not (home / trash / "old.json").exists()
        assert R.unfinished_reason(home) is None, "the fresh service may start"
        for n in (1, 2):
            _write(home / "chats" / f"after{n}.json", "USER DATA")
            _write(home / "assistant.db", f"NEW DB {n}")
            doc = R.resume_pending(home, user)
        assert doc["phase"] == "erased" and doc["attempts"] == R.MAX_ATTEMPTS
        assert doc["problems"][-1] == "Couldn't delete: chats"
        assert sorted(p.name for p in (home / "chats").iterdir()) == ["after1.json", "after2.json"]
        assert (home / "assistant.db").read_text() == "NEW DB 2"
    finally:
        for folder in home.glob(R.TRASH_PREFIX + "*/stuck"):
            os.chmod(folder, 0o700)


def test_an_entry_that_cannot_even_be_renamed_is_reported_at_once_and_never_retried(world, monkeypatch):
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    real_remove, real_rename = R._remove, os.rename
    tried = []

    def remove(path, device=None):
        if path.name == "diagnostics":
            tried.append("delete")
            raise PermissionError("no")
        real_remove(path, device)

    def rename(src, dst):
        if Path(src).name == "diagnostics":
            tried.append("rename")
            raise PermissionError("no")
        real_rename(src, dst)

    monkeypatch.setattr(R, "_remove", remove)
    monkeypatch.setattr(R.os, "rename", rename)
    doc = R.resume_pending(home, user)
    assert doc["pending"] == [] and doc["phase"] == "erased" and doc["attempts"] == 1
    assert "Couldn't delete: diagnostics" in doc["problems"] and tried == ["delete", "rename"]


def test_a_stuck_database_moves_to_the_trash_with_its_write_ahead_log(world, monkeypatch):
    """A fresh assistant.db can never pick up the old WAL: the pair moves together."""
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    real_remove = R._remove

    def remove(path, device=None):
        if path.name == "assistant.db-wal":
            raise PermissionError("no")
        real_remove(path, device)

    monkeypatch.setattr(R, "_remove", remove)
    doc = R.resume_pending(home, user)
    prefix = R.TRASH_PREFIX + doc["id"] + "-"
    assert doc["pending"] == [prefix + "assistant.db", prefix + "assistant.db-wal"]
    assert not (home / "assistant.db").exists() and not (home / "assistant.db-wal").exists()
    assert (home / (prefix + "assistant.db")).exists() and (home / (prefix + "assistant.db-wal")).exists()
    monkeypatch.setattr(R, "_remove", real_remove)
    _write(home / "assistant.db", "fresh")
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "erased" and doc["pending"] == [] and doc["problems"] == []
    assert (home / "assistant.db").read_text() == "fresh"


def test_a_marker_whose_pending_names_are_not_its_trash_is_ignored(world):
    home, user = world["home"], world["user"]
    doc = _marker(home, phase="first-pass", full_pass=True)
    for pending in (["chats"], ["../outside"], [R.TRASH_PREFIX + "someoneelse-chats"],
                    ["cache/" + R.TRASH_PREFIX + doc["id"] + "-x"]):
        R.write_marker(home, {**doc, "pending": pending})
        assert isinstance(R.read_marker(home), R.InvalidMarker), pending
        assert R.resume_pending(home, user) is None and (home / "cache").exists()
    assert R.trash_path_ok("engine/base/" + R.TRASH_PREFIX + doc["id"] + "-cache", doc["id"])
    assert R.display_name("engine/base/" + R.TRASH_PREFIX + doc["id"] + "-cache") == "engine/base/cache"


# --------------------------------------------------------------------------
# N-c, N-d
# --------------------------------------------------------------------------

def test_mcp_servers_never_inherit_the_launch_nonce(monkeypatch):
    from service import mcp
    monkeypatch.setenv("WISP_LAUNCH_NONCE", "secret-nonce")
    monkeypatch.setenv("SOMETHING_ELSE", "kept")
    env = mcp._inherited_env({})
    assert "WISP_LAUNCH_NONCE" not in env and env["SOMETHING_ELSE"] == "kept"
    assert "WISP_LAUNCH_NONCE" not in mcp._inherited_env({"env_clear_prefixes": ["UV_"]})
    source = (Path(__file__).resolve().parents[1] / "service" / "mcp" / "__init__.py").read_text()
    assert "if str(k) not in _NEVER_INHERITED" in source, "a server's own env can't set it either"


def test_a_recycled_engine_pid_does_not_keep_the_cache(world, monkeypatch):
    home = world["home"]
    (home / "engine" / "engine.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(R, "_command_of", lambda pid: "/usr/bin/some-other-program --flag")
    assert R._engine_running(home) is False
    monkeypatch.setattr(R, "_command_of", lambda pid: None)
    assert R._engine_running(home) is True, "unreadable: keep the cache rather than delete it under the engine"
    monkeypatch.setattr(R, "_command_of", lambda pid: f"python serve --base-path {home / 'engine' / 'base'}")
    assert R._engine_running(home) is True
    monkeypatch.setattr(R, "_command_of", lambda pid: f"python serve --base-path {home / 'engine' / 'base2'}")
    assert R._engine_running(home) is False


# --------------------------------------------------------------------------
# Follow-up: engine trash, pinned databases, one marker definition, child envs
# --------------------------------------------------------------------------

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reset_markers.json"


@pytest.mark.parametrize("case", json.loads(FIXTURE.read_text())["cases"], ids=lambda c: c["name"])
def test_the_service_judges_every_shared_marker_case_like_the_app(case):
    """N4: tests/WispResetChecks.swift runs the same cases through the app's parser."""
    try:
        doc = R.marker_from_json(R.parse_marker_json(case["text"].encode()))
    except ValueError:
        doc = R.InvalidMarker(reason="not valid JSON")
    assert (not isinstance(doc, R.InvalidMarker)) is case["valid"], case["name"]


def test_engine_trash_left_by_a_killed_pass_is_collected(world):
    """N2: a pass killed after moving engine entries to the trash, before recording them."""
    home, user = world["home"], world["user"]
    doc = _marker(home, phase="first-pass", attempts=1, in_progress=True)
    prefix = R.TRASH_PREFIX + doc["id"] + "-"
    _write(home / "engine" / (prefix + "engine.log"), "request text")
    _write(home / "engine" / "base" / (prefix + "cache") / "kv.safetensors", "prompt cache")
    _write(home / "engine" / "base" / (R.TRASH_PREFIX + "otherreset-cache") / "x", "not this reset's")
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "erased" and doc["pending"] == [] and doc["problems"] == []
    assert not (home / "engine" / (prefix + "engine.log")).exists()
    assert not (home / "engine" / "base" / (prefix + "cache")).exists()
    assert (home / "engine" / "base" / (R.TRASH_PREFIX + "otherreset-cache") / "x").exists()
    assert (home / MODEL).read_text() == "weights"


def test_engine_trash_that_still_will_not_delete_is_kept_pending(world, monkeypatch):
    home, user = world["home"], world["user"]
    doc = _marker(home, phase="first-pass", attempts=1, in_progress=True)
    trash = R.TRASH_PREFIX + doc["id"] + "-cache"
    _write(home / "engine" / "base" / trash / "kv.safetensors", "prompt cache")
    real_remove = R._remove

    def remove(path, device=None):
        if path.name == trash:
            raise PermissionError("no")
        real_remove(path, device)

    monkeypatch.setattr(R, "_remove", remove)
    doc = R.resume_pending(home, user)
    assert doc["pending"] == ["engine/base/" + trash] and doc["phase"] == "first-pass"


def test_a_sidecar_that_cannot_move_keeps_its_database_beside_it(world, monkeypatch):
    """N3: never a fresh database beside an old write-ahead log."""
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")
    real_remove, real_rename = R._remove, os.rename

    def remove(path, device=None):
        if path.name == "assistant.db-wal":
            raise PermissionError("no")
        real_remove(path, device)

    def rename(src, dst):
        if Path(src).name == "assistant.db-wal":
            raise PermissionError("no")
        real_rename(src, dst)

    monkeypatch.setattr(R, "_remove", remove)
    monkeypatch.setattr(R.os, "rename", rename)
    doc = R.resume_pending(home, user)
    assert (home / "assistant.db").exists() and (home / "assistant.db-wal").exists()
    assert not any(R.display_name(p).startswith("assistant.db") for p in doc["pending"])
    assert "Couldn't delete: assistant.db, assistant.db-wal" in doc["problems"]
    assert not (home / "sessions.db").exists(), "other databases still go"


def test_a_config_that_cannot_be_trimmed_says_trim(world, monkeypatch):
    home, user = world["home"], world["user"]
    _marker(home, phase="first-pass")

    def broken(root):
        raise OSError("no")

    monkeypatch.setattr(R, "_rewrite_config", broken)
    doc = R.resume_pending(home, user)
    assert R.CONFIG_NOT_TRIMMED in doc["problems"]
    assert not any(p.startswith("Couldn't delete") and "config.yaml" in p for p in doc["problems"])
    assert R.cant_delete_lines(["config.yaml", "chats"]) == ["Couldn't delete: chats", "Couldn't trim config.yaml."]


def test_nothing_wisp_runs_inherits_the_launch_nonce(monkeypatch):
    """N6: the shell tool, skill scripts and MCP servers all drop it."""
    from service import identity
    monkeypatch.setenv("WISP_LAUNCH_NONCE", "secret-nonce")
    monkeypatch.setenv("SOMETHING_ELSE", "kept")
    env = identity.child_environment()
    assert "WISP_LAUNCH_NONCE" not in env and env["SOMETHING_ELSE"] == "kept"
    root = Path(__file__).resolve().parents[1]
    builtin = (root / "service" / "tools" / "builtin.py").read_text()
    assert "timeout=120, cwd=str(Path.home()), env=child_environment())" in builtin
    skills = (root / "service" / "skills" / "tools.py").read_text()
    assert 'env={**child_environment(), "WISP_SKILL_DIR": str(skill_dir)}' in skills
    assert "**os.environ" not in skills


def test_the_shell_tool_child_does_not_see_the_nonce(monkeypatch):
    from service.safety import policy
    from service.tools import builtin
    monkeypatch.setenv("WISP_LAUNCH_NONCE", "secret-nonce")
    monkeypatch.setattr(policy, "decide", lambda *a, **k: type("D", (), {"tier": policy.Tier.ALLOW,
                                                                         "shell_argv": None, "reason": ""})())
    out = builtin.run_shell("echo nonce=${WISP_LAUNCH_NONCE:-absent}")
    assert "nonce=absent" in str(out)


def test_the_app_knows_the_refusal_status():
    source = (Path(__file__).resolve().parents[1] / "app" / "Sources" / "WispApp" / "BackendManager.swift").read_text()
    assert f"static let resetUnfinishedStatus: Int32 = {R.REFUSED_EXIT}" in source


def test_engine_trash_from_a_killed_cache_only_pass_is_collected(world):
    """A cache-only pass (the engine ran during the first pass) killed after moving
    base/cache to the trash: the next pass deletes that trash too."""
    home, user = world["home"], world["user"]
    doc = _marker(home, phase="first-pass", attempts=1, full_pass=True, engine_cache=False, in_progress=True)
    trash = home / "engine" / "base" / (R.TRASH_PREFIX + doc["id"] + "-cache")
    (home / "engine" / "base" / "cache").rename(trash)       # moved, then the pass was killed
    _write(home / "assistant.db", "a chat after the first pass")
    doc = R.resume_pending(home, user)
    assert not trash.exists()
    assert doc["phase"] == "erased" and doc["pending"] == [] and doc["engine_cache"]
    assert (home / "assistant.db").read_text() == "a chat after the first pass"


def test_engine_trash_is_collected_even_while_the_engine_runs(world, monkeypatch):
    home, user = world["home"], world["user"]
    doc = _marker(home, phase="first-pass", attempts=1, full_pass=True, engine_cache=False)
    trash = home / "engine" / "base" / (R.TRASH_PREFIX + doc["id"] + "-cache")
    _write(trash / "kv.safetensors", "prompt-derived state")
    monkeypatch.setattr(R, "_engine_running", lambda home: True)
    R.resume_pending(home, user)
    assert not trash.exists() and (home / "engine" / "base" / "cache").exists()


def test_run_applescript_does_not_pass_the_nonce():
    source = (Path(__file__).resolve().parents[1] / "service" / "tools" / "automation_tools.py").read_text()
    assert 'timeout=_TIMEOUT, env=child_environment())' in source


def test_the_marker_json_rules_match_the_app():
    with pytest.raises(ValueError):
        R.parse_marker_json(b'{"a": 1, "a": 1}')
    with pytest.raises(ValueError):
        R.parse_marker_json(b'["\\udfff"]')
    assert R.parse_marker_json(b'["\\ud83d\\ude00"]') == ["\U0001F600"]
    with pytest.raises(ValueError):
        R.parse_marker_json(b'\xef\xbb\xbf{"version": 1, "id": "a", "phase": "erased"}')
    source = (Path(__file__).resolve().parents[1] / "app" / "Sources" / "WispApp" / "ResetWisp.swift").read_text()
    assert f"static let maxAttempts = {R.MAX_ATTEMPTS}" in source


@pytest.mark.parametrize("depth", [600, 1_000, 10_000, 32_000])
def test_a_deeply_nested_marker_file_is_ignored_not_a_crash(world, depth):
    """The service reads it at every start: refused cleanly, never RecursionError."""
    home, user = world["home"], world["user"]
    text = '{"version": 1, "id": "c0ffee12", "phase": "first-pass", "later": ' + "[" * depth + "]" * depth + "}"
    (home / R.MARKER).write_text(text)
    assert isinstance(R.read_marker(home), R.InvalidMarker)
    assert R.resume_pending(home, user) is None and (home / "sessions.db").exists()


def test_a_deep_document_never_recurses_in_the_surrogate_walk():
    deep = []
    for _ in range(50_000):
        deep = [deep]
    assert R._has_lone_surrogate(deep) is False
    assert R._has_lone_surrogate([[["ok", "\ud800"]]]) is True


def test_the_depth_and_number_limits_match_the_app():
    source = (Path(__file__).resolve().parents[1] / "app" / "Sources" / "WispApp" / "ResetWisp.swift").read_text()
    assert f"static let maxDepth = {R.MAX_JSON_DEPTH}" in source
    assert f"static let maxIntegerDigits = {R.MAX_INTEGER_DIGITS}" in source


def test_unrecorded_engine_trash_is_reported_at_the_attempt_limit(world):
    home, user = world["home"], world["user"]
    doc = _marker(home, phase="first-pass", attempts=R.MAX_ATTEMPTS, full_pass=True, engine_cache=True,
                  in_progress=True)
    _write(home / "engine" / "base" / (R.TRASH_PREFIX + doc["id"] + "-cache") / "kv", "prompt-derived")
    doc = R.resume_pending(home, user)
    assert doc["phase"] == "erased"
    assert doc["pending"] == ["engine/base/" + R.TRASH_PREFIX + doc["id"] + "-cache"]
    assert "Couldn't delete: engine/base/cache" in doc["problems"]
