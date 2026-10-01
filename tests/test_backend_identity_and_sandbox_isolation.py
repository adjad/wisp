"""H-16 (backend side) and M-8.

The app's backend port is hard-coded and the app used to trust whatever answered there,
running its "send this email / create this event" requests against real accounts. A
sandbox backend on 8765, a sandbox server aimed at the real backend, or any program on
that port could make the real app act. Here: the backend says which mode it is in, a
sandbox backend refuses a production port, and the sandbox refuses to be pointed at one.
"""
import importlib
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sandbox import guard
from sandbox.guard import SandboxIsolationError, port_of, refuse_production
from service import identity, main

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------ backend identity

def test_identity_reports_the_service_mode_and_pid():
    body = TestClient(main.app).get("/identity").json()
    assert body["service"] == "wisp-backend" and body["pid"] == os.getpid()
    assert body["mode"] in {"production", "sandbox"}


def test_a_backend_not_on_the_real_wisp_home_is_a_sandbox(monkeypatch, tmp_path):
    monkeypatch.delenv("WISP_SANDBOX_HOME", raising=False)
    monkeypatch.setattr(identity, "MOE_DIR", tmp_path / "elsewhere")
    assert identity.mode() == "sandbox"


def test_the_sandbox_home_marker_alone_makes_it_a_sandbox(monkeypatch):
    monkeypatch.setenv("WISP_SANDBOX_HOME", "/tmp/anything")
    monkeypatch.setattr(identity, "MOE_DIR", Path.home() / ".moe")
    assert identity.mode() == "sandbox"


def test_only_the_real_wisp_home_without_markers_is_production(monkeypatch):
    monkeypatch.delenv("WISP_SANDBOX_HOME", raising=False)
    monkeypatch.setattr(identity, "MOE_DIR", Path.home() / ".moe")
    assert identity.mode() == "production"
    assert identity.payload()["mode"] == "production"


def test_an_unresolvable_home_is_never_claimed_to_be_production(monkeypatch):
    class Broken:
        def resolve(self):
            raise OSError("boom")
    monkeypatch.delenv("WISP_SANDBOX_HOME", raising=False)
    monkeypatch.setattr(identity, "MOE_DIR", Broken())
    assert identity.is_sandbox() is True


@pytest.mark.parametrize("argv,expected", [
    (["uvicorn", "service.main:app", "--port", "8765"], 8765),
    (["uvicorn", "service.main:app", "--port=8775"], 8775),
    (["uvicorn", "--host", "127.0.0.1", "--port", "8765", "--reload"], 8765),
    (["uvicorn", "service.main:app"], None),
    (["uvicorn", "--port"], None),
    (["uvicorn", "--port", "abc"], None),
    (["uvicorn", "--port", "99999999"], None),
    ([], None),
])
def test_the_bind_port_is_read_from_uvicorns_arguments(argv, expected):
    assert identity.bind_port(argv) == expected


@pytest.mark.parametrize("port", ["8765", "8000"])
def test_a_sandbox_backend_refuses_a_production_port(port):
    with pytest.raises(SystemExit) as error:
        identity.refuse_sandbox_on_production_port(["uvicorn", "--port", port], sandbox=True)
    assert port in str(error.value) and "sandbox" in str(error.value)


@pytest.mark.parametrize("argv,sandbox", [
    (["uvicorn", "--port", "8775"], True),       # the sandbox's own port
    (["uvicorn", "--port", "8765"], False),      # the real backend on its own port
    (["uvicorn"], True),
])
def test_other_combinations_start_normally(argv, sandbox):
    identity.refuse_sandbox_on_production_port(argv, sandbox=sandbox)


def test_startup_runs_the_refusal():
    import inspect
    source = inspect.getsource(main.lifespan)
    assert "refuse_sandbox_on_production_port()" in source.split("quarantine.check()")[0]


# ------------------------------------------------------------- sandbox guard

@pytest.mark.parametrize("value,port", [
    ("http://127.0.0.1:8765", 8765), ("http://localhost:8765/", 8765), ("http://[::1]:8765", 8765),
    ("127.0.0.1:8765", 8765), ("localhost:8000", 8000), ("8765", 8765), (8765, 8765),
    ("http://127.0.0.1:8775", 8775), ("https://example.test", 443), ("http://example.test", 80),
    ("", None), (None, None), ("http://127.0.0.1:notaport", None),
])
def test_port_extraction_handles_every_spelling(value, port):
    assert port_of(value) == port


@pytest.mark.parametrize("value", ["http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765",
                                   "127.0.0.1:8765", "8765", 8765, "http://127.0.0.1:8000", "8000"])
def test_production_ports_are_refused_in_every_spelling(value):
    with pytest.raises(SandboxIsolationError) as error:
        refuse_production(value, what="WISP_BACKEND_URL")
    assert "real Wisp" in str(error.value)


@pytest.mark.parametrize("value", ["http://127.0.0.1:8775", "8766", 8775, "http://127.0.0.1:9000", None, ""])
def test_sandbox_ports_are_allowed(value):
    refuse_production(value, what="x")


def test_an_unparseable_value_is_refused_not_ignored():
    with pytest.raises(SandboxIsolationError):
        refuse_production("http://127.0.0.1:notaport", what="WISP_BACKEND_URL")


def test_the_sandbox_proxy_cannot_be_pointed_at_the_real_backend(monkeypatch):
    from sandbox import proxy
    try:
        monkeypatch.setenv("WISP_BACKEND_URL", "http://127.0.0.1:8765")
        with pytest.raises(SandboxIsolationError):
            importlib.reload(proxy)
        monkeypatch.setenv("WISP_BACKEND_URL", "http://127.0.0.1:8775")
        importlib.reload(proxy)
        assert proxy.BACKEND_URL.endswith(":8775")
    finally:
        monkeypatch.undo()
        importlib.reload(proxy)


# ---------------------------------------------------------------- launcher

def _bash(script, env=None, **kw):
    try:
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(kw.pop("home", "/tmp")), **(env or {})}, **kw)
    except (PermissionError, FileNotFoundError):
        pytest.skip("spawning a shell is forbidden in this sandbox")


def test_the_launcher_checks_ports_before_creating_or_starting_anything():
    text = (ROOT / "sandbox/run.sh").read_text()
    guard_at = text.index("8765|8000)")
    for later in ("mkdir -p", ".venv/bin/uvicorn", "WISP_SANDBOX_HOME=", "curl -sf"):
        assert guard_at < text.index(later), f"the port guard must come before {later!r}"


@pytest.mark.parametrize("env", [{"WISP_BACKEND_PORT": "8765"}, {"SANDBOX_PORT": "8765"},
                                 {"WISP_BACKEND_PORT": "8000"}, {"SANDBOX_PORT": "8000"}])
def test_the_launcher_refuses_production_ports_and_creates_nothing(tmp_path, env):
    result = _bash(f"bash {ROOT}/sandbox/run.sh", env=env, home=tmp_path)
    assert result.returncode == 1 and "belongs to the real Wisp" in result.stderr
    assert list(tmp_path.iterdir()) == [], "nothing may be created before the refusal"


def test_the_cleanup_trap_survives_an_unset_server_pid():
    """M-8: under `set -u`, cleaning up before the sandbox server started died on an
    unset variable and left the backend running."""
    text = (ROOT / "sandbox/run.sh").read_text()
    function = re.search(r"cleanup\(\) \{.*?\n\}", text, re.S).group(0)
    script = textwrap.dedent(f"""
        set -euo pipefail
        # Builtins only: the CI sandbox forbids executing external programs such as sleep.
        while :; do :; done & BACKEND_PID=$!
        SANDBOX_PID=""
        {function}
        cleanup
        wait "$BACKEND_PID" 2>/dev/null || true
        if kill -0 "$BACKEND_PID" 2>/dev/null; then echo STILL_RUNNING; else echo STOPPED; fi
    """)
    result = _bash(script)
    assert result.returncode == 0 and result.stdout.strip() == "STOPPED", result.stderr
    assert 'SANDBOX_PID=""' in text.split("cleanup()")[0], "must be initialised before the trap function"
