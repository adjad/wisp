"""service/engine — the private bundled-oMLX lifecycle manager.

What must keep holding:
  * the child never shares HOME, a port reserved for oMLX.app / Wisp, or Wisp's environment;
  * the API key is in a 0600 settings file and never on argv;
  * start/stop is clean, a busy port is refused, and a crashed Wisp's orphan is reaped.

    python tests/test_bundled_engine.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import stat
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from service.engine import (BUNDLED_PORT, EngineError, EngineLayout,  # noqa: E402
                            EngineManager, RESERVED_PORTS, engine_settings)
from service.engine.manager import merge_owned  # noqa: E402

PASS = FAIL = 0
KEY = "a" * 64

STUB = textwrap.dedent('''
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    base = args[args.index("--base-path") + 1]
    key = json.load(open(base + "/settings.json"))["auth"]["api_key"]
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            with open(base + "/seen.log", "a") as seen:
                seen.write((self.headers.get("Authorization") or "-") + "\\n")
            ok = self.headers.get("Authorization") == "Bearer " + key
            self.send_response(200 if ok else 401); self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *a): pass
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def make(tmp: Path, port: int | None = None, **kw) -> EngineManager:
    stub = tmp / "stub.py"
    stub.write_text(STUB)
    layout = EngineLayout(root=tmp / "engine", data_dir=tmp / "data")
    return EngineManager(layout, port=port or free_port(), api_key=KEY,
                         launcher=[sys.executable, str(stub)], **kw)


def test_builders() -> None:
    with tempfile.TemporaryDirectory() as t:
        os.environ["WISP_LOCAL_OMLX_KEY"] = "s" * 64
        try:
            m = make(Path(t))
            env, argv = m.environment(), m.command()
        finally:
            del os.environ["WISP_LOCAL_OMLX_KEY"]
        check("private HOME", env["HOME"] == str(m.layout.home))
        check("no Wisp env inherited", not any(k.startswith("WISP_") for k in env)
              and set(env) == {"HOME", "PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONSAFEPATH",
                                         "PYTHONNOUSERSITE", "LANG", "TMPDIR"})
        check("cwd and user site stay off sys.path", env["PYTHONSAFEPATH"] == "1" and env["PYTHONNOUSERSITE"] == "1")
        check("key never on argv", KEY not in " ".join(argv))
        check("loopback only", argv[argv.index("--host") + 1] == "127.0.0.1")
        check("own base path", argv[argv.index("--base-path") + 1] == str(m.layout.base_path))
        check("model dirs come from settings, not argv (a CLI dir would hide the extra ones)", "--model-dir" not in argv)
        check("default python is the bundled one",
              EngineManager(m.layout).command()[0] == str(m.layout.root / "py/bin/python3"))


def test_refusals() -> None:
    layout = EngineLayout(Path("/x"), Path("/y"))
    for port in sorted(RESERVED_PORTS) + [80, 70000]:
        try:
            EngineManager(layout, port=port)
            check(f"refuses port {port}", False)
        except EngineError:
            check(f"refuses port {port}", True)
    try:
        EngineManager(layout, api_key="short")
        check("refuses short key", False)
    except EngineError:
        check("refuses short key", True)
    check("default port is unreserved", BUNDLED_PORT not in RESERVED_PORTS)


def test_prepare_permissions() -> None:
    with tempfile.TemporaryDirectory() as t:
        m = make(Path(t), extra_model_dirs=[Path(t) / "shared"])
        m.prepare()
        mode = stat.S_IMODE(m.layout.settings_path.stat().st_mode)
        doc = json.loads(m.layout.settings_path.read_text())
        check("settings file is 0600", mode == 0o600, oct(mode))
        check("state dirs are 0700", stat.S_IMODE(m.layout.home.stat().st_mode) == 0o700)
        check("key stored in settings", doc["auth"]["api_key"] == KEY)
        check("unauthenticated inference off", doc["auth"]["allow_unauthenticated_inference"] is False)
        check("HF cache scan off", doc["huggingface"]["hf_cache_enabled"] is False)
        check("shared model dir added", str(Path(t) / "shared") in doc["model"]["model_dirs"])
    check("template port", engine_settings(9000, KEY, [])["server"]["port"] == 9000)


def test_key_and_settings_persistence() -> None:
    with tempfile.TemporaryDirectory() as t:
        layout = EngineLayout(Path(t) / "e", Path(t) / "d")
        first = EngineManager(layout)
        first.prepare()
        check("generated key is 64 hex", len(first.api_key) == 64)
        again = EngineManager(layout)
        check("restart keeps the key", again.api_key == first.api_key)
        doc = json.loads(layout.settings_path.read_text())
        doc["scheduler"] = {"max_concurrent_requests": 3}
        doc["server"]["log_level"] = "debug"
        layout.settings_path.write_text(json.dumps(doc))
        again.prepare()
        kept = json.loads(layout.settings_path.read_text())
        check("foreign sections survive prepare", kept["scheduler"] == {"max_concurrent_requests": 3})
        check("foreign keys inside owned sections survive", kept["server"]["log_level"] == "debug")
        check("owned keys are restored", kept["server"]["port"] == again.port
              and kept["auth"]["api_key"] == again.api_key)
        kept["model"]["model_dirs"].append("/Volumes/Models")
        layout.settings_path.write_text(json.dumps(kept))
        again.prepare()
        dirs = json.loads(layout.settings_path.read_text())["model"]["model_dirs"]
        check("a model folder added in the admin UI survives prepare",
              "/Volumes/Models" in dirs and dirs[0] == str(layout.models_dir), str(dirs))
        again.prepare()
        check("prepare does not duplicate folders",
              json.loads(layout.settings_path.read_text())["model"]["model_dirs"] == dirs)
    merged = merge_owned({"model": {"model_dirs": ["/old"], "x": 1}}, {"model": {"model_dirs": ["/new"]}})
    check("merge replaces owned keys only", merged == {"model": {"model_dirs": ["/new"], "x": 1}})
    layout = EngineLayout(Path("/nonexistent/e"), Path("/nonexistent/d"))
    check("unreadable settings are treated as empty", EngineManager(layout).api_key is not None)


def test_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as t:
        m = make(Path(t))
        try:
            m.start()
            check("ready", asyncio.run(m.wait_ready(20)))
            seen = (m.layout.base_path / "seen.log").read_text().split()
            check("readiness polling sent the API key to nobody", seen and all(h == "-" for h in seen), str(seen))
            check("pid recorded", m.layout.pid_path.exists())
            m.start()
            check("start is idempotent", m.running())
            stranger = EngineManager(m.layout, port=m.port, api_key="b" * 64, launcher=m.launcher)
            check("a manager that did not launch the child never reports ready",
                  not asyncio.run(_one_probe(stranger)))
            check("engine log is private", stat.S_IMODE(m.layout.log_path.stat().st_mode) == 0o600)
        finally:
            m.stop()
        check("stopped", not m.running() and not port_open(m.port))
        check("pid file removed", not m.layout.pid_path.exists())


async def _one_probe(manager: EngineManager) -> bool:
    return await manager.wait_ready(1.0)


def test_busy_port() -> None:
    with tempfile.TemporaryDirectory() as t, socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        m = make(Path(t), port=s.getsockname()[1])
        try:
            m.start()
            check("busy port refused", False)
        except EngineError:
            check("busy port refused", True)
        check("nothing started", not m.running())


def test_reaps_orphan() -> None:
    with tempfile.TemporaryDirectory() as t:
        first = make(Path(t))
        first.start()
        check("orphan up", asyncio.run(first.wait_ready(20)))
        orphan = first._proc
        first._proc = None  # simulate a crashed Wisp: pid file remains, no handle
        second = EngineManager(first.layout, port=first.port, api_key=KEY, launcher=first.launcher)
        try:
            second.start()
            time.sleep(0.2)
            check("orphan reaped", orphan.poll() is not None)
            check("replacement ready", asyncio.run(second.wait_ready(20)))
        finally:
            second.stop()
            if orphan.poll() is None:
                orphan.kill()


def test_reap_only_the_launched_engine() -> None:
    from service.engine.manager import reap_orphan
    with tempfile.TemporaryDirectory() as t:
        layout = EngineLayout(root=Path(t) / "engine", data_dir=Path(t) / "data")
        layout.base_path.mkdir(parents=True)
        check("no pid file: nothing to reap", reap_orphan(layout) is False)
        # A recycled pid: some other process that merely mentions the base folder.
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                      str(layout.base_path / "settings.json")])
        try:
            layout.pid_path.write_text(str(bystander.pid))
            check("a process that only mentions the folder is not killed", reap_orphan(layout) is False)
            time.sleep(0.2)
            check("bystander still running", bystander.poll() is None)
            check("stale pid file is removed", not layout.pid_path.exists())
        finally:
            bystander.kill()
            bystander.wait()
        (Path(t) / "m").mkdir()
        (Path(t) / "live").mkdir()
        m = make(Path(t) / "m")
        m.start()
        check("launched engine up", asyncio.run(m.wait_ready(20)))
        victim = m._proc
        m._proc = None                      # a previous backend's engine: pid file, no handle
        try:
            check("the launched engine is reaped", reap_orphan(m.layout) is True)
            time.sleep(0.3)
            check("engine gone", victim.poll() is not None)
        finally:
            if victim.poll() is None:
                victim.kill()
        live = make(Path(t) / "live")
        live.start()
        try:
            check("a manager never reaps its own live child", live.reap_orphan() is False and live.running())
        finally:
            live.stop()


def main() -> int:
    for fn in (test_builders, test_refusals, test_prepare_permissions, test_key_and_settings_persistence, test_lifecycle,
               test_busy_port, test_reaps_orphan, test_reap_only_the_launched_engine):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
