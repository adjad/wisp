"""Attribution of Wisp's bundled oMLX engine (design: docs/plans/2026-10-13-bundled-omlx-attribution-design.md).

Real processes, real sockets, real kernel queries. The engine is a stand-in HTTP server
run by the existing Python interpreter (in place) and launched by the real EngineManager,
so pid pinning, the parent check, the listener checks, the interpreter path and the
code-tree audit (on temp site-packages and code trees) are exercised for real. Every
refusal must happen before a byte (and so the bearer key) is released.

Why in place: the production layout (<root>/py/bin/python3 plus sp/ and omlx/, signed by
Wisp) cannot be reproduced from a copied system interpreter: a framework python re-execs
into another binary after spawn, which attribution correctly refuses, and on a host whose
code-signing services are stalled a newly created executable hangs in dyld. The audit of
the real py/ tree and the signed-engine path are therefore verified at the packaging step
(plan step 4); here the interpreter runs in place and the temp sp/ and omlx/ trees stand
in for the audited code trees.

    python tests/test_bundled_attribution.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from service.engine import BUNDLED_PORT as ENGINE_BUNDLED_PORT  # noqa: E402
from service.engine import EngineLayout, EngineManager  # noqa: E402
from service.engine.manager import Incarnation  # noqa: E402
from service.inference import attributed_transport as at  # noqa: E402
from service.inference import bundled_peer as bp  # noqa: E402
from service.inference.local_peer import (AuthRefused, process_identity,  # noqa: E402
                                          process_path)

PASS = FAIL = 0
KEY = "c" * 64

STUB = textwrap.dedent('''
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    base = args[args.index("--base-path") + 1]
    key = json.load(open(base + "/settings.json"))["auth"]["api_key"]
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.headers.get("Authorization") == "Bearer " + key
            self.send_response(200 if ok else 401); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a): pass
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')

IMPOSTOR = textwrap.dedent('''
    import socket, sys
    port, out = int(sys.argv[1]), sys.argv[2]
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port)); s.listen(8)
    while True:
        c, _ = s.accept(); c.settimeout(1.0)
        try: data = c.recv(65536)
        except OSError: data = b""
        open(out, "ab").write(data)
        c.close()
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


def wait_listening(port: int, seconds: float = 15.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def own_signing_identifier() -> str:
    """The signing identifier of this interpreter (which is also the engine's), else the constant."""
    try:
        probe = bp.BundledOmlx.__new__(bp.BundledOmlx)
        return probe._csops_string(os.getpid(), 11)
    except Exception:  # noqa: BLE001 - unsigned interpreter
        return bp.ENGINE_SIGNING_ID


SIGNING_ID = own_signing_identifier()


def _in_place_class(port: int, interpreter_dir: Path, **attrs):
    """BundledOmlx for the in-place interpreter: it lives outside the temp bundle, so its
    directory is the interpreter root and only the temp trees are walked."""
    class InPlace(bp.BundledOmlx):
        signing_id = SIGNING_ID

        @staticmethod
        def _python_root(root):
            return interpreter_dir

        def _tree_roots(self):
            return (self.site_root, self.code_root)
    return type("InPlace", (InPlace,), {"port": port, **attrs})


class Env:
    """A fake engine bundle + manager in a private directory."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.root = self.tmp / "engine"
        (self.root / "sp").mkdir(parents=True)
        (self.root / "omlx").mkdir()
        # The binary the kernel reports for this interpreter: a framework python's bin/python3.x
        # launcher re-execs into it, and attribution (correctly) refuses an engine whose
        # executable changes after spawn, so the engine is started from the final binary.
        self.python = Path(process_path(os.getpid()))
        self.interpreter_dir = self.python.parent
        self.stub = self.root / "omlx/stub.py"
        self.stub.write_text(STUB)
        self.port = free_port()
        self.layout = EngineLayout(self.root, self.tmp / "data")
        self.manager = EngineManager(self.layout, port=self.port, api_key=KEY,
                                     launcher=[str(self.python), str(self.stub)])
        self.procs: list[subprocess.Popen] = []

    def bundled(self, source=None, **attrs):
        """A BundledOmlx class bound to this env's port (the production port is fixed)."""
        cls = _in_place_class(self.port, self.interpreter_dir, **attrs)
        return cls(source or self.manager.incarnation)

    def authority(self, source=None, **attrs):
        env = self
        cls = _in_place_class(self.port, self.interpreter_dir, **attrs)

        class Auth:
            port = env.port

            def load(self_inner):
                return cls(source or env.manager.incarnation)
        return Auth()

    def request(self, authority=None, key: str = KEY):
        transport = at.CredentialTransport(f"http://127.0.0.1:{self.port}", key, managed=True,
                                           authority=authority or self.authority())

        async def go():
            async with httpx.AsyncClient(transport=transport, timeout=20) as http:
                return await http.get(f"http://127.0.0.1:{self.port}/v1/models")
        return asyncio.run(go())

    def impostor(self, port: int | None = None) -> tuple[subprocess.Popen, Path]:
        out = self.tmp / f"impostor-{len(self.procs)}.bin"
        script = self.tmp / "impostor.py"
        script.write_text(IMPOSTOR)
        proc = subprocess.Popen([sys.executable, str(script), str(port or self.port), str(out)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        assert wait_listening(port or self.port)
        return proc, out

    def foreign_incarnation(self, pid: int) -> Incarnation:
        _, uid, secs, micros = process_identity(pid, os.getuid())
        return Incarnation(pid, uid, secs, micros, Path(process_path(pid)), self.root)

    def close(self) -> None:
        self.manager.stop()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)


def refuses(call, reason: str | None = None) -> bool:
    try:
        call()
    except (AuthRefused, Exception) as exc:  # noqa: BLE001
        got = getattr(exc, "reason", None) or str(exc)
        return reason is None or reason in got
    return False


def test_selection_and_constants() -> None:
    check("engine and transport agree on the port", ENGINE_BUNDLED_PORT == at.BUNDLED_PORT == bp.BUNDLED_PORT == 8791)
    check("8791 selects the bundled authority", isinstance(at.authority_for(8791), bp.BundledAuthority))
    check("8000 selects the desktop authority", isinstance(at.authority_for(8000), at.RuntimeAuthority))
    check("any other managed port gets the desktop authority", isinstance(at.authority_for(9999), at.RuntimeAuthority))
    check("bundled authority is not a RuntimeAuthority (no reuse lease)",
          not isinstance(at.authority_for(8791), at.RuntimeAuthority))
    check("bundled class listens on its own port", bp.BundledOmlx.port == 8791 and at.DesktopOmlx.port == 8000)


def test_accepts_the_engine_wisp_launched() -> None:
    env = Env()
    try:
        env.manager.start()
        check("engine listening", wait_listening(env.port))
        reply = env.request()
        check("request through the real transport succeeds", reply.status_code == 200, str(reply.status_code))
        check("bearer key was accepted by the engine", reply.status_code == 200)
        authority = env.bundled()
        check("binding returns the launched pid", authority.binding() == env.manager.incarnation().pid)
        print(f"       (signature strength exercised: {authority.strength})")
    finally:
        env.close()


def test_refuses_without_a_launched_engine() -> None:
    env = Env()
    try:
        env.impostor()
        check("no manager child: construction refused", refuses(env.bundled, "bundled_engine_not_owned"))
        env.request_error = None
        try:
            env.request()
            check("request refused", False)
        except Exception as exc:  # noqa: BLE001
            check("request refused", type(exc).__name__ == "ModelLoadError", type(exc).__name__)
    finally:
        env.close()


def test_key_never_reaches_an_impostor() -> None:
    env = Env()
    try:
        _, out = env.impostor()
        for source in (lambda: None,):
            try:
                env.request(env.authority(source))
            except Exception:  # noqa: BLE001
                pass
        data = out.read_bytes() if out.exists() else b""
        check("impostor received no bytes at all", data == b"", repr(data[:80]))
        check("bearer key not seen by impostor", KEY.encode() not in data)
    finally:
        env.close()


def test_impostor_replaces_a_killed_engine() -> None:
    env = Env()
    try:
        env.manager.start()
        wait_listening(env.port)
        stale = env.manager.incarnation()
        os.kill(stale.pid, signal.SIGKILL)
        time.sleep(0.5)
        check("dead engine yields no incarnation", env.manager.incarnation() is None)
        _, out = env.impostor()
        check("live manager source: refused", refuses(env.bundled))
        check("stale incarnation: refused (pid/identity differs)",
              refuses(lambda: env.bundled(lambda: stale)))
        try:
            env.request(env.authority(lambda: stale))
        except Exception:  # noqa: BLE001
            pass
        check("impostor got nothing", (out.read_bytes() if out.exists() else b"") == b"")
    finally:
        env.close()


def test_restart_changes_incarnation() -> None:
    env = Env()
    try:
        env.manager.start()
        wait_listening(env.port)
        old = env.bundled()
        first_pid = old.binding()
        env.manager.stop()
        time.sleep(0.5)
        env.manager.start()
        wait_listening(env.port)
        check("new engine accepted", env.request().status_code == 200)
        check("old authority refuses the new process", refuses(old.binding))
        check("pid changed across restart", env.manager.incarnation().pid != first_pid)
    finally:
        env.close()


def test_wrong_parent() -> None:
    env = Env()
    try:
        base = env.layout.base_path
        base.mkdir(parents=True)
        (base / "settings.json").write_text(json.dumps({"auth": {"api_key": KEY}}))
        # A shell starts the stub in the background and exits: the stub is re-parented to launchd.
        subprocess.run(["/bin/sh", "-c", f'"{env.python}" "{env.stub}" --port {env.port} --base-path "{base}" '
                        f'>/dev/null 2>&1 &'], check=True, env={"PATH": "/usr/bin:/bin"})
        check("orphaned stub listening", wait_listening(env.port))
        listener = subprocess.run(["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{env.port}", "-sTCP:LISTEN"],
                                  capture_output=True, text=True).stdout.split()
        pid = int(listener[0])
        try:
            check("a process not our child is refused",
                  refuses(lambda: env.bundled(lambda: env.foreign_incarnation(pid)), "desktop_parent_unqualified"))
        finally:
            os.kill(pid, signal.SIGKILL)
    finally:
        env.close()


def test_other_listeners_on_the_port() -> None:
    env = Env()
    try:
        env.manager.start()
        wait_listening(env.port)
        extra = socket.socket(socket.AF_INET6)
        try:
            extra.bind(("::1", env.port))
            extra.listen()
            check("extra IPv6 listener refused", refuses(env.bundled, "desktop_listener_unqualified"))
        except OSError:
            print("       (skipped: cannot bind ::1)")
        finally:
            extra.close()
        wide = socket.socket()
        try:
            wide.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            wide.bind(("0.0.0.0", env.port))
            wide.listen()
            check("extra wildcard listener refused", refuses(env.bundled, "desktop_listener_unqualified"))
        except OSError:
            print("       (skipped: cannot share the port on 0.0.0.0)")
        finally:
            wide.close()
        check("clean again once the extra listeners close", env.bundled().binding() > 0)
    finally:
        env.close()


def test_interpreter_and_tree_tampering() -> None:
    env = Env()
    try:
        env.manager.start()
        wait_listening(env.port)
        inc = env.manager.incarnation()
        wrong = Incarnation(inc.pid, inc.uid, inc.start_seconds, inc.start_microseconds,
                            env.root / "omlx/python3-other", inc.root)
        check("interpreter path mismatch refused", refuses(lambda: env.bundled(lambda: wrong)))
        wrong_start = Incarnation(inc.pid, inc.uid, inc.start_seconds - 1, inc.start_microseconds, inc.exe, inc.root)
        check("start-time mismatch refused", refuses(lambda: env.bundled(lambda: wrong_start), "process_identity_changed"))
        check("clean tree is accepted first", env.bundled().binding() == inc.pid)

        os.chmod(env.root / "sp", 0o777)
        check("world-writable site-packages refused", refuses(env.bundled, "desktop_runtime_unqualified"))
        os.chmod(env.root / "sp", 0o755)

        os.symlink("/etc", env.root / "omlx/escape")
        check("symlink leaving the tree refused", refuses(env.bundled, "desktop_runtime_unqualified"))
        os.unlink(env.root / "omlx/escape")

        os.link(env.stub, env.root / "omlx/stub-hardlink.py")
        check("hard-linked file refused", refuses(env.bundled, "desktop_runtime_unqualified"))
        os.unlink(env.root / "omlx/stub-hardlink.py")

        (env.root / "omlx/extra.py").write_text("x = 1\n")
        check("new file in a clean tree is still accepted", env.bundled().binding() == inc.pid)
        os.chmod(env.root / "omlx/extra.py", 0o666)
        check("world-writable file refused", refuses(env.bundled, "desktop_runtime_unqualified"))
    finally:
        env.close()


def test_signature_policy() -> None:
    """Signed backend: engine must match; unsigned backend: structural rules only."""
    env = Env()
    try:
        env.manager.start()
        wait_listening(env.port)
        engine_pid = env.manager.incarnation().pid
        import struct

        def fake(backend_flags, backend_team, engine_flags=0x00010001, engine_id=bp.ENGINE_SIGNING_ID,
                 engine_team=None, team_error=False):
            class Fake(_in_place_class(env.port, env.interpreter_dir)):

                def _csops(self, pid, operation, size):
                    flags = backend_flags if pid == os.getpid() else engine_flags
                    return struct.pack("=I", flags)

                def _csops_string(self, pid, operation):
                    if pid == os.getpid():
                        if team_error:
                            raise AuthRefused("desktop_signature_unqualified")
                        return backend_team
                    return engine_id if operation == 11 else (engine_team or backend_team)
            return Fake(env.manager.incarnation)

        ok = fake(0x00010001, "TEAM123456", engine_id=SIGNING_ID)
        check("signed backend + matching engine accepted", ok.binding() == engine_pid and ok.strength == "signed")
        check("signed: engine without hardened runtime refused",
              refuses(lambda: fake(0x00010001, "TEAM123456", engine_flags=0x1), "desktop_signature_unqualified"))
        check("signed: wrong signing identifier refused",
              refuses(lambda: fake(0x00010001, "TEAM123456", engine_id="com.example.not-the-engine"),
                      "desktop_signature_unqualified"))
        check("signed: wrong team refused",
              refuses(lambda: fake(0x00010001, "TEAM123456", engine_team="OTHER00000"),
                      "desktop_signature_unqualified"))
        check("signed backend whose team cannot be read refuses (no downgrade)",
              refuses(lambda: fake(0x00010001, "TEAM123456", team_error=True)))
        dev = fake(0x0, "", engine_flags=0x0, engine_id="anything", engine_team="")
        check("unsigned backend: structural rules decide", dev.binding() == engine_pid and dev.strength == "unsigned-dev")
        check("backend with only CS_VALID (ad hoc) is not 'signed'", fake(0x1, "", engine_flags=0).strength == "unsigned-dev")
        check("hardened runtime without a valid signature is refused, not downgraded to unsigned-dev",
              refuses(lambda: fake(0x00010000, "TEAM123456", engine_flags=0), "desktop_signature_unqualified"))
    finally:
        env.close()


def test_messages_and_reasons() -> None:
    backend = at.CheckedBackend(at.authority_for(8791), 8791)
    try:
        asyncio.run(backend.connect_tcp("127.0.0.1", 9))     # wrong port: refused before any I/O
        check("wrong-port connect refused", False)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        check("bundled refusal says built-in engine", "built-in AI engine" in text and "restart Wisp" in text, text)
        check("bundled refusal does not mention oMLX", "oMLX" not in text, text)
    try:
        raise AuthRefused("bundled_engine_not_owned")
    except AuthRefused:
        error = at.refused(8791)
    check("reason code kept off the user message",
          "bundled_engine_not_owned" not in str(error) and error.reason == "bundled_engine_not_owned", error.reason)
    for url, expected in (("http://127.0.0.1:8791", True), ("http://localhost:8791", True),
                          ("http://[::1]:8791", True), ("http://127.0.0.2:8791", True),
                          ("http://127.0.0.1:8000", False), ("http://192.0.2.1:8791", False),
                          ("https://example.com", False)):
        got = at.bundled_transport(url, "k" * 64)
        check(f"bundled_transport({url}) is {'checked' if expected else 'left to the caller'}",
              isinstance(got, at.CredentialTransport) == expected and (got is None) == (not expected))
    desktop = at.refused(8000)
    check("desktop refusal still says oMLX", "oMLX" in str(desktop))
    check("default refused() unchanged", str(at.refused()) == at.REFUSED_MESSAGE)


def test_missing_module_refuses() -> None:
    saved = sys.modules.get("service.inference.bundled_peer")
    sys.modules["service.inference.bundled_peer"] = None   # simulates a bundle without the module
    try:
        authority = at.authority_for(8791)
        check("unavailable module yields a refusing authority", isinstance(authority, at._UnavailableAuthority))
        check("it refuses on load", refuses(authority.load, "bundled_authority_unavailable"))
    finally:
        if saved is None:
            del sys.modules["service.inference.bundled_peer"]
        else:
            sys.modules["service.inference.bundled_peer"] = saved


def main() -> int:
    for fn in (test_selection_and_constants, test_accepts_the_engine_wisp_launched,
               test_refuses_without_a_launched_engine, test_key_never_reaches_an_impostor,
               test_impostor_replaces_a_killed_engine, test_restart_changes_incarnation,
               test_wrong_parent, test_other_listeners_on_the_port,
               test_interpreter_and_tree_tampering, test_signature_policy,
               test_messages_and_reasons, test_missing_module_refuses):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
