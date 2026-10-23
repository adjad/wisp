"""Owned-root preflight and owned-child supervision for the evaluation host.

Stdlib only. Two rules:

* A root is accepted only when it is a narrow scratch directory under the
  system temporary area, owned by this user, not a link, not a protected
  location or an ancestor of one, and (for a launch) fresh: absent, or an
  empty private directory. Validation happens before anything is created,
  changed or removed.
* The supervisor starts the backend and the app as its own children and
  keeps their ``Popen`` handles. A child is signalled only through its handle
  while it is unreaped, so the pid cannot have been reused. ``stop`` asks the
  supervisor over a private Unix socket; no command ever signals a pid read
  from a file. When the supervisor is gone, stop reports the last recorded
  state and signals nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE = "lifecycle.json"
CONTROL = "control.sock"
STOP_GRACE_S = 5.0


class RootError(ValueError):
    """A root that is not a narrow, owned, fresh evaluation scratch directory."""


def _home() -> str:
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


# confstr(_CS_DARWIN_USER_TEMP_DIR): this user's system temporary folder.
_CS_DARWIN_USER_TEMP_DIR = 65537


def scratch_bases() -> tuple[str, ...]:
    """Where evaluation roots may live: the system temporary areas only.

    Asked of the OS, never read from TMPDIR or tempfile: the host redirects
    both into its owned root, and an inherited TMPDIR could name any folder.
    """
    bases = {os.path.realpath("/private/tmp")}
    try:
        user_temp = os.confstr(_CS_DARWIN_USER_TEMP_DIR)
    except (ValueError, OSError):
        user_temp = None
    if user_temp and os.path.isabs(user_temp):
        bases.add(os.path.realpath(user_temp))
    return tuple(sorted(bases))


def protected_paths() -> tuple[str, ...]:
    home = _home()
    return tuple(os.path.realpath(p) for p in (
        "/", "/Users", "/private", "/private/var", "/private/var/folders", "/Applications", "/Library",
        "/System", home, os.path.join(home, ".moe"), os.path.join(home, "Library"),
        os.path.join(home, "Applications"), os.path.join(home, "Desktop"), os.path.join(home, "Documents")))


def _inside(path: str, base: str) -> bool:
    return path != base and path.startswith(base.rstrip(os.sep) + os.sep)


def validate_root(path: str | os.PathLike, *, fresh: bool,
                  allowed_entries: frozenset[str] | None = frozenset()) -> str:
    """Return the canonical root, or raise RootError. Never writes anything.

    ``fresh`` requires an absent root or an empty one; otherwise only
    ``allowed_entries`` may already exist at its top level (an installed,
    separately verified snapshot layout). ``None`` checks the location and
    ownership only, for an existing run's root.
    """
    raw = os.fspath(path)
    if not os.path.isabs(raw) or "\x00" in raw:
        raise RootError("the root must be an absolute path")
    parent = os.path.dirname(raw.rstrip(os.sep)) or os.sep
    try:
        os.lstat(parent)
    except OSError:
        raise RootError("the root's parent must already exist") from None
    canonical_parent = os.path.realpath(parent)
    candidate = os.path.join(canonical_parent, os.path.basename(raw.rstrip(os.sep)))
    if not any(_inside(candidate, base) for base in scratch_bases()):
        raise RootError("the root must be inside the system temporary area")
    for protected in protected_paths():
        if candidate == protected or _inside(protected, candidate):
            raise RootError("the root is a protected location or one of its ancestors")
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        return candidate
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RootError("the root must be a real directory, not a link or file")
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RootError("the root must be owned by this user and not writable by others")
    entries = set(os.listdir(candidate))
    if fresh and entries:
        raise RootError("the root is not empty")
    if not fresh and allowed_entries is not None and entries - allowed_entries:
        raise RootError(f"the root holds unexpected entries: {sorted(entries - allowed_entries)[:5]}")
    return candidate


def create_root(path: str | os.PathLike) -> str:
    """Validate a fresh root, then create it 0700. Refuses before any write."""
    root = validate_root(path, fresh=True)
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return validate_root(root, fresh=True)


# --- supervision -----------------------------------------------------------

class Child:
    """One owned child. Signals go only through its unreaped Popen handle."""

    def __init__(self, role: str, process) -> None:
        self.role, self.process = role, process
        self.returncode: int | None = None
        self.signals: list[str] = []

    def alive(self) -> bool:
        if self.returncode is not None:
            return False
        code = self.process.poll()
        if code is None:
            return True
        self.returncode = code
        return False

    def signal(self, sig: int) -> bool:
        if not self.alive():
            return False
        self.process.send_signal(sig)
        self.signals.append(signal.Signals(sig).name)
        return True

    def retire(self, grace: float = STOP_GRACE_S, *, clock=time.monotonic, nap=time.sleep) -> dict:
        if self.signal(signal.SIGTERM):
            deadline = clock() + grace
            while self.alive() and clock() < deadline:
                nap(0.05)
            if self.alive():
                self.signal(signal.SIGKILL)
                self.process.wait()
        self.alive()
        return self.record()

    def record(self) -> dict:
        return {"role": self.role, "pid": self.process.pid, "returncode": self.returncode,
                "alive": self.returncode is None, "signals": list(self.signals)}


class Supervisor:
    """Owns the children of one run and its state file."""

    def __init__(self, root: str, *, spawn=subprocess.Popen) -> None:
        self.root, self.spawn = Path(root), spawn
        self.children: list[Child] = []
        self.state: dict = {"supervisor_pid": os.getpid(), "state": "starting", "children": []}

    def write(self, **fields) -> None:
        self.state.update(fields)
        self.state["children"] = [c.record() for c in self.children]
        self.state["updated_at"] = time.time()
        tmp = self.root / (STATE + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.root / STATE)

    def start(self, role: str, argv: list[str], *, env: dict, cwd: str, log: str) -> Child:
        fd = os.open(self.root / log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            process = self.spawn(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fd,
                                 stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
        finally:
            os.close(fd)
        child = Child(role, process)
        self.children.append(child)
        self.write()
        return child

    def stop_all(self, state: str, reason: str = "") -> dict:
        for child in reversed(self.children):
            child.retire()
        self.write(state=state, reason=reason)
        return self.state


def _identity(port: int, token: str, nonce: str, timeout: float) -> int | None:
    """The backend's pid once it answers with this run's nonce, else None."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/evaluation/identity",
                                             headers={"x-wisp-evaluation-token": token})
            with opener.open(request, timeout=2) as response:
                body = json.loads(response.read())
            if body.get("nonce") == nonce and isinstance(body.get("pid"), int):
                return body["pid"]
            return None
        except (OSError, ValueError):
            time.sleep(0.1)
    return None


def _private_json(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def supervise(args) -> int:
    root = validate_root(args.root, fresh=False, allowed_entries=frozenset({
        "home", "wisp", "sync", "evidence", "host-config.json", "host.sb", "app.sb"}))
    sup = Supervisor(root)
    sup.write(state="starting")
    try:
        config = json.loads(Path(root, "host-config.json").read_text(encoding="utf-8"))
        host_env = {"LANG": "C.UTF-8", "PYTHONHASHSEED": "0"}
        backend = sup.start("backend", ["/usr/bin/sandbox-exec", "-f", str(Path(root, "host.sb")),
                                        args.python, "-I", "-B", args.host, "--config",
                                        str(Path(root, "host-config.json"))],
                            env=host_env, cwd=root, log="host.log")
        pid = _identity(config["listen_port"], config["token"], config["nonce"], args.ready_timeout)
        if pid is None or not backend.alive():
            sup.stop_all("failed", "backend did not answer with this run's identity")
            return 2
        # The backend runs under sandbox-exec, which execs the interpreter in
        # place, so the answering pid is the supervised child itself.
        if pid != backend.process.pid:
            sup.stop_all("failed", "the answering backend is not the supervised child")
            return 2
        _private_json(Path(root, "native-config.json"), {
            "run_id": config["run_id"], "owned_root": root, "listen_port": config["listen_port"],
            "token": config["token"], "nonce": config["nonce"], "backend_pid": pid,
            "source_roots": config.get("source_roots", []),
            "live_sources": config.get("source_mode") == "live"})
        tmp = Path(root, "tmp-app")
        os.mkdir(tmp, 0o700)
        sup.start("app", ["/usr/bin/sandbox-exec", "-f", str(Path(root, "app.sb")), args.app],
                  env={"HOME": _home(), "LANG": "C.UTF-8", "TMPDIR": str(tmp),
                       "WISP_NOTCH_EVALUATION_CONFIG": str(Path(root, "native-config.json"))},
                  cwd=root, log="app.log")
        sup.write(state="running")
    except Exception as exc:  # noqa: BLE001 — every partial launch is retired and reported
        sup.stop_all("failed", f"launch failed: {type(exc).__name__}")
        return 2
    return serve_control(sup)


def serve_control(sup: Supervisor) -> int:
    path = str(sup.root / CONTROL)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        server.bind(path)
    finally:
        os.umask(old)
    server.listen(1)
    server.settimeout(0.5)
    try:
        while True:
            for child in sup.children:
                if not child.alive():
                    # One side ended on its own: the run is not contained any more.
                    sup.stop_all("ended", f"{child.role} exited with {child.returncode}")
                    return 1
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                request = conn.recv(64).decode(errors="replace").strip()
                if request == "stop":
                    state = sup.stop_all("stopped", "stop requested")
                    conn.sendall(json.dumps(state).encode())
                    return 0
                conn.sendall(json.dumps(sup.state).encode())
    finally:
        server.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def stop(root: str, *, timeout: float = 30.0) -> dict:
    """Ask the supervisor to retire its children. Signals nothing itself."""
    canonical = validate_root(root, fresh=False, allowed_entries=None)
    control = Path(canonical, CONTROL)
    try:
        info = os.lstat(control)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RootError("the control socket is not this user's")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(control))
            conn.sendall(b"stop\n")
            chunks = []
            while chunk := conn.recv(65536):
                chunks.append(chunk)
        return {"stopped_by": "supervisor", **json.loads(b"".join(chunks))}
    except (FileNotFoundError, ConnectionRefusedError):
        state_path = Path(canonical, STATE)
        try:
            recorded = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except (OSError, ValueError):
            recorded = {"unreadable": True}   # a damaged record still signals nothing
        return {"stopped_by": "none", "note": "no live supervisor; nothing was signalled",
                "last_recorded": recorded}


def retire(root: str, into: str) -> str:
    """Move a finished run's root into a private archive directory.

    Only a root whose supervisor is gone and whose last recorded state is
    terminal moves; nothing is deleted. ``into`` must be an existing private
    directory this user owns. Returns the new location.
    """
    canonical = validate_root(root, fresh=False, allowed_entries=None)
    control = Path(canonical, CONTROL)
    if control.exists() or control.is_symlink():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(2)
                conn.connect(str(control))
            raise RootError("the run's supervisor is still answering; stop it first")
        except (ConnectionRefusedError, FileNotFoundError):
            pass
    try:
        state = json.loads(Path(canonical, STATE).read_text(encoding="utf-8")).get("state")
    except (OSError, ValueError):
        state = None
    if state not in ("stopped", "ended", "failed"):
        raise RootError(f"the run is not finished (state: {state})")
    target_dir = os.path.realpath(into)
    info = os.lstat(target_dir)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RootError("the archive must be an existing private directory")
    target = os.path.join(target_dir, os.path.basename(canonical))
    if os.path.lexists(target):
        raise RootError("the archive already holds a root of that name")
    os.rename(canonical, target)
    return target


def install_snapshot(snapshot: str, root: str) -> str:
    """Install a verified snapshot into a fresh owned root; return its manifest digest.

    The digest goes into the host config, so the backend refuses any other
    installed state before it imports service code.
    """
    canonical = validate_root(root, fresh=True)
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.notch_evaluation import snapshots
    verified = snapshots.verify(snapshot)
    if not verified.ok:
        raise RootError("the snapshot failed verification: " + "; ".join(verified.problems))
    snapshots.install(snapshot, canonical)
    return verified.manifest_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("preflight", help="validate (and with --create, create) a fresh root")
    check.add_argument("root")
    check.add_argument("--create", action="store_true")
    run = sub.add_parser("supervise", help="launch and own the backend and app")
    run.add_argument("root")
    run.add_argument("--python", required=True)
    run.add_argument("--host", required=True)
    run.add_argument("--app", required=True)
    run.add_argument("--ready-timeout", type=float, default=30.0)
    end = sub.add_parser("stop", help="ask the supervisor to retire its children")
    end.add_argument("root")
    keep = sub.add_parser("retire", help="move a finished run's root into a private archive")
    keep.add_argument("root")
    keep.add_argument("--into", required=True)
    place = sub.add_parser("install-snapshot", help="install a verified snapshot into a fresh root")
    place.add_argument("snapshot")
    place.add_argument("root")
    secrets_cmd = sub.add_parser("secret", help="print a random token")
    secrets_cmd.add_argument("--bytes", type=int, default=32)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            print(create_root(args.root) if args.create else validate_root(args.root, fresh=True))
            return 0
        if args.command == "supervise":
            return supervise(args)
        if args.command == "stop":
            print(json.dumps(stop(args.root), indent=1, sort_keys=True))
            return 0
        if args.command == "install-snapshot":
            print(install_snapshot(args.snapshot, args.root))
            return 0
        if args.command == "retire":
            print(retire(args.root, args.into))
            return 0
        print(secrets.token_hex(args.bytes))
        return 0
    except RootError as exc:
        print(f"lifecycle: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
