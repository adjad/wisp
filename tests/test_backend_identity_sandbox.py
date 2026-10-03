"""F3: real launcher decisions and Uvicorn resolution, with inert host leaves."""
import asyncio
import importlib
import os
from pathlib import Path
import socket
import subprocess
import textwrap

import pytest
from uvicorn import Config, Server

from sandbox._backend_guard import SandboxBindError, canonical_port

ROOT = Path(__file__).resolve().parents[1]
BAD_TEXT = ["8765", "8000", "08765", "08000", "00000008765", "0000008000",
            " 8765", "8765 ", "\t8000\n", "+8765", "+8000", "8_765", "0x223d",
            "", "0", "-1", "65536", "999999999", "abc", "8765.0", "１２３"]


@pytest.mark.parametrize("value", BAD_TEXT + [None, True, False, 8765, 8000, 0, -1, 65536, 1.5])
def test_canonical_parser_refuses_unsafe_aliases_and_malformed_ports(value):
    with pytest.raises(SandboxBindError):
        canonical_port(value, what="fixture bind")


@pytest.mark.parametrize("value,expected", [("8775", 8775), ("8766", 8766), ("1", 1),
                                            ("65535", 65535), (8775, 8775), (8766, 8766)])
def test_canonical_parser_preserves_valid_isolated_ports(value, expected):
    assert canonical_port(value, what="fixture bind") == expected


def _shell(script, tmp_path, extra_env):
    # Every external leaf is denied or mocked by the scripts below. The only
    # real child is an inert /bin/bash interpreter, never a product process.
    return subprocess.run(["/bin/bash", "-c", script], check=False, capture_output=True,
                          text=True, timeout=10, env={
                              "PATH": "/nonexistent", "HOME": str(tmp_path),
                              "LAUNCHER": str(ROOT / "sandbox/run.sh"), **extra_env})


@pytest.mark.parametrize("name", ["WISP_BACKEND_PORT", "SANDBOX_PORT"])
@pytest.mark.parametrize("value", BAD_TEXT)
def test_full_launcher_refuses_before_directory_command_or_product_start(tmp_path, name, value):
    script = """
        forbidden() { builtin printf 'HOST_EFFECT:%s\n' "$1"; return 99; }
        dirname() { forbidden dirname; }
        python3() { forbidden python3; }
        mkdir() { forbidden mkdir; }
        lsof() { forbidden lsof; }
        curl() { forbidden curl; }
        open() { forbidden open; }
        source "$LAUNCHER"
    """
    result = _shell(script, tmp_path, {name: value})
    assert result.returncode == 1 and "refusing to run the sandbox" in result.stderr
    assert result.stdout == "", "even dirname/Python must not run before port refusal"
    assert list(tmp_path.iterdir()) == [], "refusal must not create state or logs"


@pytest.mark.parametrize("ports", [{}, {"WISP_BACKEND_PORT": "8775", "SANDBOX_PORT": "8766"},
                                  {"WISP_BACKEND_PORT": "9001", "SANDBOX_PORT": "9002"}])
def test_full_launcher_valid_ports_keep_both_bind_arguments_with_mocked_leaves(tmp_path, ports):
    state = tmp_path / "state"
    state.mkdir()  # disposable fixture only; launcher mkdir is an inert record
    script = r'''
        dirname() { builtin printf '%s\n' "$FIXTURE_ROOT/sandbox"; }
        python3() {
          case "$2" in
            *WISP_HOME*) builtin printf '%s\n' "$WISP_HOME" ;;
            *) builtin printf '%s/.moe\n' "$HOME" ;;
          esac
        }
        mkdir() { builtin printf 'MOCK_MKDIR\n'; }
        lsof() { return 1; }
        curl() { return 0; }
        seq() { builtin printf '1\n'; }
        sleep() { :; }
        open() { builtin printf 'MOCK_OPEN %s\n' "$1"; }
        kill() { builtin printf 'MOCK_KILL %s\n' "$1"; }
        function .venv/bin/uvicorn { builtin printf 'MOCK_UVICORN %s\n' "$*"; }
        source "$LAUNCHER"
    '''
    result = _shell(script, tmp_path, {**ports, "FIXTURE_ROOT": str(ROOT),
                                     "WISP_SANDBOX_HOME": str(state), "WISP_HOME": str(state / "moe")})
    assert result.returncode == 0, result.stderr
    backend = ports.get("WISP_BACKEND_PORT", "8775")
    sandbox = ports.get("SANDBOX_PORT", "8766")
    assert (state / "backend.log").read_text().strip() == f"MOCK_UVICORN service.main:app --port {backend}"
    assert (state / "sandbox.log").read_text().strip() == f"MOCK_UVICORN sandbox.server:app --factory --lifespan on --port {sandbox} --reload"
    assert "MOCK_MKDIR" in result.stdout and "MOCK_OPEN" in result.stdout
    assert not (state / "moe").exists()


@pytest.fixture
def inert_server(monkeypatch):
    server = importlib.import_module("sandbox.server")
    events = []

    class World:
        def __init__(self):
            events.append("world")

        def snapshot(self):
            events.append("snapshot")
            return {"fixture": "synthetic"}

    class Client:
        async def aclose(self):
            events.append("client.close")

    def make_client():
        events.append("client")
        return Client()

    class Sync:
        def __init__(self, world, client):
            events.append("sync")

        def start(self):
            events.append("sync.start")

        async def stop(self):
            events.append("sync.stop")

    class Outbound:
        def __init__(self, world, client, *, sync):
            events.append("outbound")

        def start(self):
            events.append("outbound.start")

        async def stop(self):
            events.append("outbound.stop")

    monkeypatch.setattr(server, "World", World)
    monkeypatch.setattr(server.proxy, "make_client", make_client)
    monkeypatch.setattr(server, "SyncScheduler", Sync)
    monkeypatch.setattr(server, "OutboundConsumer", Outbound)

    async def startup(self, sockets=None):
        # Real Config.load, app factory, guarded ASGI and Uvicorn lifespan;
        # replace only the socket-opening runner leaf after lifecycle validation.
        if hasattr(self, "fixture_mutation"):
            self.fixture_mutation(self.config)
        await self.lifespan.startup()
        self.should_exit = self.lifespan.should_exit
        self.started = not self.should_exit

    async def main_loop(self):
        if hasattr(self, "fixture_request"):
            await self.fixture_request(self.config.loaded_app)

    async def shutdown(self, sockets=None):
        await self.lifespan.shutdown()

    monkeypatch.setattr(Server, "startup", startup)
    monkeypatch.setattr(Server, "main_loop", main_loop)
    monkeypatch.setattr(Server, "shutdown", shutdown)

    def run(port=8766, *, sockets=None, mutation=None, request=None, **kwargs):
        config = Config("sandbox.server:app", port=port, factory=True, lifespan="on",
                        http="h11", ws="none", loop="asyncio", log_config=None, **kwargs)
        runner = Server(config)
        if mutation:
            runner.fixture_mutation = mutation
        if request:
            runner.fixture_request = request
        asyncio.run(runner._serve(sockets=sockets))
        return runner

    return server, events, run


@pytest.mark.parametrize("port", [8000, 8765, "08765", " 8000 ", 0, -1, 65536, True, "", "abc"])
def test_programmatic_server_refuses_before_world_client_or_scheduler(inert_server, port):
    _, events, run = inert_server
    with pytest.raises(SandboxBindError):
        run(port)
    assert events == []


@pytest.mark.parametrize("kwargs", [{"uds": "/fixture.sock"}, {"fd": 3}])
def test_server_refuses_unknown_socket_bind_configuration(inert_server, kwargs):
    _, events, run = inert_server
    with pytest.raises(SandboxBindError):
        run(**kwargs)
    assert events == []


@pytest.mark.parametrize("port", [8775, 8766])
def test_programmatic_server_preserves_isolated_startup_and_shutdown(inert_server, port):
    _, events, run = inert_server
    runner = run(port)
    assert not runner.should_exit and runner.started
    assert events == ["world", "client", "sync", "outbound", "sync.start", "outbound.start",
                      "outbound.stop", "sync.stop", "client.close"]


def test_server_missing_actual_context_fails_even_with_safe_environment(inert_server, monkeypatch):
    server, events, _ = inert_server
    monkeypatch.setenv("SANDBOX_PORT", "8766")
    monkeypatch.setenv("UVICORN_PORT", "8766")
    with pytest.raises(SandboxBindError, match="Missing or ambiguous"):
        server.app()
    with pytest.raises(SandboxBindError, match="Missing or ambiguous"):
        Config("sandbox.server:app", port=8766, factory=True, log_config=None,
               ws="none", http="h11").load()
    async def unknown_lifespan():
        async with server.lifespan(server._app):
            pytest.fail("unknown startup may not yield")
    with pytest.raises(SandboxBindError, match="Missing or ambiguous"):
        asyncio.run(unknown_lifespan())
    assert events == []


def test_server_nested_configurations_are_ambiguous_not_assumed_safe(inert_server):
    _, events, run = inert_server
    def outer_factory():
        return run(8766)
    with pytest.raises(SandboxBindError, match="Missing or ambiguous"):
        Config(outer_factory, port=8766, factory=True, log_config=None,
               ws="none", http="h11").load()
    assert events == []


class Listener:
    def __init__(self, address, family=socket.AF_INET):
        self.address, self.family = address, family
    def getsockname(self):
        return self.address


@pytest.mark.parametrize("listeners", [[], [object()], [Listener(("127.0.0.1", 8765))],
                                       [Listener(("127.0.0.1", 8000))], [Listener(("127.0.0.1", 9000))],
                                       [Listener("unknown")], [Listener(("127.0.0.1", True))],
                                       [Listener("/fixture.sock", socket.AF_UNIX)],
                                       [Listener(("127.0.0.1", 8766)), Listener(("::1", 8765, 0, 0), socket.AF_INET6)]])
def test_server_refuses_ambiguous_or_unsafe_inherited_listener_before_start(inert_server, listeners):
    _, events, run = inert_server
    with pytest.raises(SandboxBindError):
        run(sockets=listeners)
    assert events == []


def test_server_safe_inherited_ipv4_and_ipv6_keep_reload_worker_flow(inert_server):
    _, events, run = inert_server
    runner = run(sockets=[Listener(("127.0.0.1", 8766)), Listener(("::1", 8766, 0, 0), socket.AF_INET6)])
    assert not runner.should_exit and "world" in events and events[-1] == "client.close"


def test_server_rechecks_actual_config_before_startup(inert_server):
    _, events, run = inert_server
    runner = run(mutation=lambda config: setattr(config, "port", 8765))
    assert runner.should_exit
    assert events == []


@pytest.mark.parametrize("setting,value", [("lifespan", "off"), ("fd", 3), ("uds", "/fixture.sock")])
def test_server_rechecks_configuration_changes_before_startup(inert_server, setting, value):
    _, events, run = inert_server
    runner = run(mutation=lambda config: setattr(config, setting, value))
    assert runner.should_exit and events == []


@pytest.mark.parametrize("args,env", [
    (["--port", "08765"], {}), (["--port=08000"], {}),
    (["--port", " 8765 "], {}), (["--port=+8000"], {}),
    ([], {"UVICORN_PORT": "00008765"}), ([], {"UVICORN_PORT": " 8000 "}),
    (["--port", "0"], {}), (["--port", "65536"], {}),
    (["--port", "abc"], {}), ([], {}),
    (["--port", "8766", "--lifespan", "off"], {}),
    (["--port", "8766", "--fd", "3"], {}),
    (["--port", "8766", "--uds", "/fixture.sock"], {})])
def test_real_uvicorn_cli_and_environment_refuse_actual_unsafe_or_unknown_bind(inert_server, monkeypatch, args, env):
    from click.testing import CliRunner
    module = importlib.import_module("uvicorn.main")
    _, events, run = inert_server
    def inert_run(app, **kwargs):
        return run(kwargs["port"], fd=kwargs["fd"], uds=kwargs["uds"]) if kwargs["lifespan"] != "off" else Config(
            app, port=kwargs["port"], lifespan="off", factory=True, log_config=None, ws="none", http="h11").load()
    monkeypatch.setattr(module, "run", inert_run)
    result = CliRunner().invoke(module.main, ["sandbox.server:app", "--factory", *args], env=env)
    assert result.exit_code != 0
    assert events == []


@pytest.mark.parametrize("args,env", [(["--port", "8766"], {}), (["--port=8775"], {}),
                                     ([], {"UVICORN_PORT": "8766"}),
                                     (["--port", "8766"], {"UVICORN_PORT": "8765"})])
def test_real_uvicorn_cli_resolution_preserves_safe_port_and_precedence(inert_server, monkeypatch, args, env):
    from click.testing import CliRunner
    module = importlib.import_module("uvicorn.main")
    _, events, run = inert_server
    seen = []
    def inert_run(app, **kwargs):
        seen.append(kwargs["port"])
        return run(kwargs["port"])
    monkeypatch.setattr(module, "run", inert_run)
    result = CliRunner().invoke(module.main, ["sandbox.server:app", "--factory", *args], env=env)
    assert result.exit_code == 0, result.output
    assert seen == [8775 if args == ["--port=8775"] else 8766]
    assert events[-1] == "client.close"


@pytest.mark.parametrize("address", [None, ("127.0.0.1", 8765), ("127.0.0.1", 8000),
                                    ("127.0.0.1", 9000), ("127.0.0.1", "08766")])
def test_server_request_bind_cannot_bypass_startup_context(inert_server, address):
    _, events, run = inert_server
    async def request(app):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(event):
            pytest.fail("unsafe request must not reach any handler")
        with pytest.raises(SandboxBindError):
            await app({"type": "http", "server": address}, receive, send)
    runner = run(request=request)
    assert not runner.should_exit and "snapshot" not in events


def test_server_safe_request_reaches_original_route_without_a_real_socket(inert_server):
    _, events, run = inert_server
    messages = []
    async def request(app):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(event):
            messages.append(event)
        await app({"type": "http", "server": ("127.0.0.1", 8766), "client": ("127.0.0.1", 12345),
                   "http_version": "1.1", "scheme": "http", "method": "GET", "path": "/sandbox/world",
                   "root_path": "", "query_string": b"", "headers": []}, receive, send)
    runner = run(request=request)
    assert not runner.should_exit
    assert messages[0]["type"] == "http.response.start" and messages[0]["status"] == 200
    assert messages[1]["body"] == b'{"fixture":"synthetic"}'
    assert events.count("snapshot") == 1
