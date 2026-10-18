"""Standalone engine API checks; fresh interpreters and synthetic homes only.

    python tests/test_bundled_engine_api.py
    python tests/test_bundled_engine_api.py --setup-guide  # existing pytest suite
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0

PROBE = r'''
import asyncio, io, json, logging, os, sys, threading
from pathlib import Path
from unittest.mock import patch
import httpx, yaml
from fastapi import FastAPI
options = json.load(sys.stdin)
home = Path(os.environ["WISP_HOME"])
before = (home / "config.yaml").exists()
real_exists = Path.exists
def presence(path):
    if str(path) == "/Applications/oMLX.app" or path == Path.home() / "Applications/oMLX.app":
        return options.get("installed", False)
    return real_exists(path)
with patch.object(Path, "exists", presence):
    from service.engine import api, mode
user_app_detected = mode.external_omlx_installed(present=lambda path: path == Path.home() / "Applications/oMLX.app")
# Replace the check after import with the same injected presence fact.
api.mode.external_omlx_installed = lambda: options.get("installed", False)
async def quiet():
    return options.get("external_running", False)
api.external_engine_running = quiet
log_text = io.StringIO()
handler = logging.StreamHandler(log_text)
api.log.addHandler(handler)
app = FastAPI()
app.include_router(api.router)
async def run():
    out = {"import_wrote_config": not before and (home / "config.yaml").exists(),
           "user_app_detected": user_app_detected, "responses": []}
    # Any accidental settings/key access or manager construction is a failure.
    from service import config
    def forbidden(*args, **kwargs):
        raise AssertionError("Status must not read engine settings or resolve keys")
    config.omlx_settings = config.omlx_api_key = api.runtime.manager = forbidden
    if options.get("race"):
        entered, release, saving, explicit_write = (threading.Event() for _ in range(4))
        real_write = api._write_mode
        errors = []
        def delayed_write(value):
            if value == "bundled":
                entered.set()
                assert release.wait(5)
            else:
                explicit_write.set()
            real_write(value)
        api._write_mode = delayed_write
        def first_launch():
            try:
                api.persist_default()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        def explicit_choice():
            saving.set()
            try:
                api._save_selected_mode("external-omlx")
            except BaseException as exc:
                errors.append(type(exc).__name__)
        default = threading.Thread(target=first_launch)
        selected = threading.Thread(target=explicit_choice)
        default.start()
        assert entered.wait(5)
        selected.start()
        assert saving.wait(5)
        assert not explicit_write.wait(0.05)
        release.set()
        default.join(5)
        selected.join(5)
        assert not default.is_alive() and not selected.is_alive() and not errors
        api._write_mode = real_write
    if options.get("guard_refuses"):
        from service.inference import remote_policy
        from service.config.endpoints import EndpointConfigurationError
        def refuse(operation):
            assert operation == "Inference settings changes"
            raise EndpointConfigurationError("FAKE-GUARD-SECRET")
        remote_policy.require_local_models_disabled = refuse
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for operation in options.get("operations", [{"method": "GET", "path": "/engine"}]):
                kwargs = {k: operation[k] for k in ("json", "content", "headers") if k in operation}
                response = await client.request(operation["method"], operation["path"], **kwargs)
                out["responses"].append({"status": response.status_code, "body": response.json()})
    from service.setup import guide
    from service.setup.engines import main_engine_profile
    from service.setup.hardware import Hardware
    setup = guide.build_status(hardware=Hardware("test", 24), omlx=guide.OmlxState(False, False),
                              installed=[], models_source="saved", roles={}, externals=[], tool_capable=[])
    out["setup_engine"] = next(c for c in setup["checks"] if c["id"] == "engine")
    out["setup_model"] = next(c for c in setup["checks"] if c["id"] == "model")
    out["profile"] = main_engine_profile().label
    try:
        out["overlay"] = yaml.safe_load((home / "config.yaml").read_text())
    except (FileNotFoundError, yaml.YAMLError):
        out["overlay"] = None
    out["logs"] = log_text.getvalue()
    out["mode"] = mode.MODE
    print(json.dumps(out))
asyncio.run(run())
'''


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def fixture(root: Path, *, present: bool = True, minimum: str = "14.0") -> Path:
    engine = root / "code"
    engine.mkdir()
    if present:
        (engine / "py" / "bin").mkdir(parents=True)
        (engine / "py" / "bin" / "python3").write_text("never execute this fixture")
        (engine / "ENGINE.json").write_text(json.dumps({"minimum_macos": minimum, "omlx_version": "0.7.1.dev1"}))
    return engine


def probe(root: Path, engine: Path, *, env: dict | None = None, **options) -> dict:
    home = root / "wisp"
    home.mkdir(exist_ok=True)
    isolated_home = root / "user"
    isolated_home.mkdir(exist_ok=True)
    full = {k: v for k, v in os.environ.items() if not k.startswith(("WISP_", "MOE_"))}
    full.update({"HOME": str(isolated_home), "WISP_HOME": str(home), "WISP_ENGINE_ROOT": str(engine),
                 "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})
    done = subprocess.run([sys.executable, "-c", PROBE], input=json.dumps(options), cwd=ROOT, env=full,
                          text=True, capture_output=True, timeout=60)
    if done.returncode:
        raise RuntimeError(done.stderr[-2000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def body(result: dict, index: int = 0) -> dict:
    return result["responses"][index]["body"]


def test_first_launch_and_stickiness() -> None:
    for installed, expected in ((False, "bundled"), (True, "external-omlx")):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            engine = fixture(root)
            first = probe(root, engine, installed=installed)
            check(f"fresh default with installed={installed}", body(first)["mode"] == expected)
            check("startup persists resolved default", first["overlay"] == {"engine": {"mode": expected}})
            check("import performs no persistence", not first["import_wrote_config"])
            check("injectable presence check detects per-user oMLX", first["user_app_detected"])
            later = probe(root, engine, installed=not installed)
            check("later install/removal does not flip mode", body(later)["mode"] == expected)
            check("stored choice has no restart flag", body(later)["configured_mode"] == expected
                  and body(later)["restart_required"] is False)
            if expected == "bundled":
                check("built-in setup offers start before process answers", first["setup_engine"]["state"] == "warn"
                      and first["setup_engine"].get("action") == "start_engine"
                      and first["profile"] == "Wisp's built-in engine")
                # A fresh Mac can't fetch the private Mellum build, so it is offered
                # the best public download rather than a folder it doesn't have.
                check("built-in setup offers a public model download on a fresh Mac",
                      first["setup_model"].get("action") == "download_model"
                      and "Mellum" not in first["setup_model"]["detail"])
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        engine = fixture(root, present=False)
        check("absent engine defaults external", body(probe(root, engine))["mode"] == "external-omlx")
        (engine / "py" / "bin").mkdir(parents=True)
        (engine / "py" / "bin" / "python3").touch()
        result = probe(root, engine, operations=[{"method": "POST", "path": "/engine/mode", "json": {"mode": "bundled"}}])
        check("interpreter without provenance is unavailable", result["responses"][0]["status"] == 409)


def test_unsupported_and_validation() -> None:
    # Invalid provenance cannot select the engine or persist a bundled default.
    # Keep ordinary fixtures compatible so mode/stickiness checks reach their
    # intended path instead of accidentally exercising this refusal path.
    for manifest in ({}, {"minimum_macos": ""}, {"minimum_macos": None},
                     {"minimum_macos": 14}, {"minimum_macos": "FAKE-INVALID-REQUIREMENT"}):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            engine = fixture(root)
            (engine / "ENGINE.json").write_text(json.dumps(manifest))
            result = probe(root, engine, operations=[
                {"method": "GET", "path": "/engine"},
                {"method": "POST", "path": "/engine/mode", "json": {"mode": "bundled"}},
            ])
            check("invalid requirement defaults external", body(result)["mode"] == "external-omlx"
                  and result["overlay"] == {"engine": {"mode": "external-omlx"}})
            check("invalid requirement refuses bundled selection", not body(result)["bundled_available"]
                  and result["responses"][1]["status"] == 409)
            check("invalid requirement is not echoed", "FAKE-INVALID-REQUIREMENT" not in json.dumps(result))
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        engine = fixture(root, minimum="999.0")
        result = probe(root, engine, operations=[
            {"method": "GET", "path": "/engine"},
            {"method": "POST", "path": "/engine/mode", "json": {"mode": "bundled"}},
        ])
        check("unsupported first launch defaults external", body(result)["mode"] == "external-omlx")
        check("unsupported status has reason", not body(result)["bundled_available"] and "macOS 999" in body(result)["unsupported_reason"])
        check("unsupported bundled request is 409", result["responses"][1]["status"] == 409)
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        engine = fixture(root)
        invalid = [{"mode": "bundled", "api_key": "FAKE-SECRET"}, {"mode": "FAKE-SECRET"},
                   {"mode": None}, {"mode": []}, {"mode": True}, {}, [], "FAKE-SECRET"]
        operations = [{"method": "POST", "path": "/engine/mode", "json": item} for item in invalid]
        operations.append({"method": "POST", "path": "/engine/mode", "content": '{"FAKE-SECRET"',
                           "headers": {"content-type": "application/json"}})
        result = probe(root, engine, operations=operations)
        check("extra fields, bad values and malformed bodies rejected", all(r["status"] == 422 for r in result["responses"]))
        simple = probe(root, engine, operations=[
            {"method": "POST", "path": "/engine/mode", "content": '{"mode": "external-omlx"}',
             "headers": {"content-type": "text/plain"}},
            {"method": "POST", "path": "/engine/mode", "content": '{"mode": "external-omlx"}',
             "headers": {"content-type": "application/x-www-form-urlencoded"}}])
        check("cross-site 'simple' POSTs are refused (415), so they cannot change the mode",
              all(r["status"] == 415 for r in simple["responses"]))
        check("validation never echoes secret input", "FAKE-SECRET" not in json.dumps(result))
        check("invalid requests never change stored mode", result["overlay"]["engine"]["mode"] == "bundled")


def test_switch_and_secrets() -> None:
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        engine = fixture(root)
        home = root / "wisp"
        (home / "engine" / "base").mkdir(parents=True)
        settings = home / "engine" / "base" / "settings.json"
        settings.write_text(json.dumps({"auth": {"api_key": "FAKE-PRIVATE-KEY"}, "secret": "FAKE-SETTINGS-CONTENTS"}))
        (home / "config.yaml").write_text("unrelated: FAKE-OVERLAY-SECRET\n")
        result = probe(root, engine, external_running=True, operations=[
            {"method": "GET", "path": "/engine"},
            {"method": "POST", "path": "/engine/mode", "json": {"mode": "external-omlx"}},
            {"method": "GET", "path": "/engine"},
            {"method": "POST", "path": "/engine/mode", "json": {"mode": "bundled"}},
        ])
        check("pending switch keeps running mode", body(result, 1)["mode"] == "bundled")
        check("switch reports saved mode and restart", body(result, 1)["configured_mode"] == "external-omlx"
              and body(result, 1)["restart_required"] and body(result, 2)["restart_required"])
        check("switch back clears restart flag", not body(result, 3)["restart_required"])
        check("writer preserves unrelated overlay", result["overlay"]["unrelated"] == "FAKE-OVERLAY-SECRET")
        check("status reports coexistence and warning", body(result)["external_engine_running"] is True and "two engines" in result["logs"])
        check("status reads provenance version", body(result)["omlx_version"] == "0.7.1.dev1")
        check("status does not construct manager", body(result)["running"] is False)
        emitted = json.dumps(result["responses"]) + result["logs"]
        check("responses/logs contain no key, key path or settings", not any(s in emitted for s in
              ("FAKE-PRIVATE-KEY", "FAKE-SETTINGS-CONTENTS", "FAKE-OVERLAY-SECRET", str(settings), "api_key")))
        expected = {"mode", "configured_mode", "bundled_available", "unsupported_reason", "running",
                    "omlx_version", "admin_url", "external_omlx_installed", "restart_required", "external_engine_running"}
        check("response exactly matches frozen fields", set(body(result)) == expected)
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        engine = fixture(root)
        result = probe(root, engine, guard_refuses=True, operations=[
            {"method": "POST", "path": "/engine/mode", "json": {"mode": "external-omlx"}}])
        check("existing inference-write guard refuses write", result["responses"][0]["status"] == 409 and result["overlay"] is None)
        check("guard errors do not leak", "FAKE-GUARD-SECRET" not in json.dumps(result))


def test_env_and_corrupt_overlay() -> None:
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        engine = fixture(root)
        result = probe(root, engine, env={"WISP_ENGINE_MODE": "external-omlx"})
        check("environment selection does not persist a default", result["overlay"] is None and result["mode"] == "external-omlx")
        check("external status does not claim bundled version", body(result)["omlx_version"] is None)
        home = root / "wisp"
        (home / "config.yaml").write_text("[invalid")
        result = probe(root, engine, operations=[{"method": "POST", "path": "/engine/mode", "json": {"mode": "bundled"}}])
        check("corrupt overlay is refused and preserved", result["responses"][0]["status"] == 409
              and (home / "config.yaml").read_text() == "[invalid")
        for text in ("[]", "false", "0", "''"):
            (home / "config.yaml").write_text(text)
            result = probe(root, engine)
            check("nonmapping overlay is never a fresh install", result["mode"] == "external-omlx"
                  and (home / "config.yaml").read_text() == text)
        (home / "config.yaml").write_text("engine: null\n")
        check("null engine section has no stored mode", probe(root, engine)["mode"] == "bundled")


def test_concurrent_selection() -> None:
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        engine = fixture(root)
        result = probe(root, engine, race=True)
        check("concurrent explicit selection wins over first launch", result["overlay"]["engine"]["mode"] == "external-omlx"
              and body(result)["restart_required"])


NETWORK_PROBE = r'''
import asyncio, json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from service.engine import api
seen = []
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        seen.append((self.path, dict(self.headers)))
        if self.path == "/health":
            self.send_response(404)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[]}')
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
assert server.server_port not in {8000, 8765, 8791}
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
async def run():
    healthy = await api.external_engine_running(port=server.server_port)
    redirected = []
    def redirect(request):
        redirected.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://example.invalid/secret"})
    redirects = await api.external_engine_running(port=server.server_port, transport=httpx.MockTransport(redirect))
    health = await api.external_engine_running(port=server.server_port, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "ok"})))
    oversized = await api.external_engine_running(port=server.server_port, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (api._MAX_PROBE_BODY + 1))))
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "
    started = time.monotonic()
    stalled = await api.external_engine_running(port=server.server_port, deadline=0.1,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip())))
    return {"healthy": healthy, "health": health, "seen": seen, "redirects": redirects,
            "redirected": redirected, "oversized": oversized, "stalled": stalled,
            "elapsed": time.monotonic()-started}
try:
    print(json.dumps(asyncio.run(run())))
finally:
    server.shutdown()
    server.server_close()
    thread.join()
'''


def test_credential_free_local_probe() -> None:
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        full = {k: v for k, v in os.environ.items() if not k.startswith(("WISP_", "MOE_"))}
        full.update({"HOME": t, "WISP_HOME": t, "WISP_ENGINE_ROOT": str(root / "absent"),
                     "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                     "HTTP_PROXY": "http://example.invalid:1", "HTTPS_PROXY": "http://example.invalid:1"})
        done = subprocess.run([sys.executable, "-c", NETWORK_PROBE], cwd=ROOT, env=full,
                              text=True, capture_output=True, timeout=60)
        if done.returncode:
            raise RuntimeError(done.stderr[-2000:])
        result = json.loads(done.stdout.strip().splitlines()[-1])
        check("random-port stub detected via models fallback", result["healthy"] and [r[0] for r in result["seen"]] == ["/health", "/v1/models"])
        check("health endpoint detected", result["health"])
        check("probe sends no credentials", all(not {"authorization", "cookie", "x-api-key"} & {k.lower() for k in headers}
              for _, headers in result["seen"]))
        check("probe never follows redirects", not result["redirects"] and len(result["redirected"]) == 2
              and all("127.0.0.1" in url for url in result["redirected"]))
        check("oversized response refused", not result["oversized"])
        check("hard deadline bounds dripping body", not result["stalled"] and result["elapsed"] < 1)


def existing_setup_suite() -> int:
    # The existing file is pytest-style, so expose a standalone entry point
    # without editing it. All import-time paths and recovery state are isolated.
    with tempfile.TemporaryDirectory() as t:
        for key in list(os.environ):
            if key.startswith(("WISP_", "MOE_")):
                del os.environ[key]
        os.environ.update({"HOME": t, "WISP_HOME": str(Path(t) / "wisp"),
                           "WISP_ENGINE_ROOT": str(Path(t) / "absent"), "PYTHONDONTWRITEBYTECODE": "1"})
        sys.path.insert(0, str(ROOT))
        import pytest
        class Counts:
            passed = failed = 0
            def pytest_runtest_logreport(self, report):
                if report.failed:
                    self.failed += 1
                elif report.when == "call" and report.passed:
                    self.passed += 1
        counts = Counts()
        result = pytest.main([str(ROOT / "tests/test_setup_guide.py"), "-q", "-p", "no:cacheprovider"], plugins=[counts])
        print(f"\n{counts.passed} passed, {counts.failed} failed")
        return int(result)


def sockets_allowed() -> bool:
    """The Simulation QA sandbox refuses loopback binds; the probe test below needs a real
    listener, so it is reported as skipped there and runs wherever sockets are allowed."""
    import socket
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


def main() -> int:
    if "--setup-guide" in sys.argv:
        return existing_setup_suite()
    skipped = 0
    for fn in (test_first_launch_and_stickiness, test_unsupported_and_validation,
               test_switch_and_secrets, test_env_and_corrupt_overlay, test_concurrent_selection,
               test_credential_free_local_probe):
        print(fn.__name__)
        if fn is test_credential_free_local_probe and not sockets_allowed():
            skipped += 1
            print("  skip needs loopback sockets (refused by this sandbox)")
            continue
        fn()
    print(f"\n{PASS} passed, {FAIL} failed, {skipped} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
