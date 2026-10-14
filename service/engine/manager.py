"""Start, watch and stop Wisp's private oMLX engine as a child process.

Isolation rules (each one was observed to matter on a scratch run of oMLX 0.7.1):
  * `--base-path` alone does NOT isolate: the server still rewrites
    ~/.omlx/bin/omlx-cluster-python and lists models found in ~/.cache/huggingface.
    The child therefore gets a private HOME and a minimal environment.
  * The API key lives in a 0600 settings.json, never on argv (visible to `ps`).
  * The child inherits nothing from Wisp's environment (WISP_* credentials, tokens).
"""
from __future__ import annotations

import asyncio
import json
import threading
import os
import secrets
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import httpx

# 8000 is a user's oMLX.app and 8765 is Wisp's own backend; neither may be used.
BUNDLED_PORT = 8791
RESERVED_PORTS = frozenset({8000, 8765})
_KEY_BYTES = 32


class EngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Incarnation:
    """Exactly which process the manager launched; the attribution check pins to it."""
    pid: int
    uid: int
    start_seconds: int
    start_microseconds: int
    exe: Path
    root: Path


@dataclass(frozen=True)
class EngineLayout:
    """Where the engine's code lives (read-only) and where its state lives (private).

    `root` holds `py/` (CPython), `sp/` (site-packages), `omlx/` and `omlx_web/`;
    in the packaged app that is Wisp.app/Contents/Resources/engine.
    """
    root: Path
    data_dir: Path

    @property
    def python(self) -> Path:
        return self.root / "py" / "bin" / "python3"

    @property
    def home(self) -> Path:
        return self.data_dir / "home"

    @property
    def base_path(self) -> Path:
        return self.data_dir / "base"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def settings_path(self) -> Path:
        return self.base_path / "settings.json"

    @property
    def pid_path(self) -> Path:
        return self.data_dir / "engine.pid"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "engine.log"

    @property
    def pythonpath(self) -> str:
        return f"{self.root}:{self.root / 'sp'}"


def new_api_key() -> str:
    return secrets.token_hex(_KEY_BYTES)


def stored_api_key(settings_path: Path) -> str | None:
    """The key a previous run wrote, so a Wisp restart does not rotate it needlessly."""
    try:
        key = json.loads(settings_path.read_text())["auth"]["api_key"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return key if isinstance(key, str) and len(key) >= 32 else None


def _saved_model_dirs(settings: dict) -> list[str]:
    """Model folders already in settings.json (added in the engine's admin UI)."""
    model = settings.get("model")
    saved = model.get("model_dirs") if isinstance(model, dict) else None
    return [d for d in saved if isinstance(d, str) and d] if isinstance(saved, list) else []


def engine_settings(port: int, api_key: str, model_dirs: Sequence[Path],
                    user_dirs: Sequence[str] = ()) -> dict:
    """The keys Wisp owns. oMLX fills in every other default on first start.

    Wisp's own folders come first; folders the user added in the admin UI are kept after them.
    """
    dirs = [str(Path(d)) for d in model_dirs]
    dirs += [d for d in user_dirs if d not in dirs]
    return {
        "version": "1.0",
        "server": {"host": "127.0.0.1", "port": port, "auto_start_on_launch": True},
        "model": {"model_dirs": dirs, "model_dir": dirs[0] if dirs else "",
                  "model_fallback": False},
        "auth": {"api_key": api_key, "skip_api_key_verification": False,
                 "allow_unauthenticated_inference": False},
        "huggingface": {"hf_cache_enabled": False},
    }


def read_settings(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def merge_owned(existing: dict, owned: dict) -> dict:
    """Overwrite only the keys Wisp owns; keep everything oMLX or the admin UI added."""
    merged = {**existing}
    for section, values in owned.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **values}
        else:
            merged[section] = values
    return merged


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def reap_orphan(layout: EngineLayout) -> bool:
    """Kill the engine recorded in engine.pid if it is still the engine Wisp launched.

    Wisp starts the engine in its own session, so a crash or a SIGKILL of the backend leaves it
    running. Only a process whose command line carries this layout's `--base-path` is killed,
    never a recycled pid that merely mentions the folder. True when a process was stopped.
    """
    try:
        pid = int(layout.pid_path.read_text().strip())
    except (OSError, ValueError):
        return False
    stopped = False
    # --base-path is the last argument of the engine command, so an exact suffix match keeps
    # `.../base` from matching `.../base2`.
    if pid > 1 and _command_of(pid).endswith(f"--base-path {layout.base_path}"):
        if _parent_of(pid) not in (1, os.getpid()):
            # Still a child of another live process: that backend owns it. Reaping is for an
            # engine orphaned to launchd (pid 1), or one this process launched and lost.
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
            for _ in range(50):
                time.sleep(0.1)
                os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
    try:
        layout.pid_path.unlink()
    except OSError:
        pass
    return stopped


def _parent_of(pid: int) -> int | None:
    try:
        out = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "ppid="],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _command_of(pid: int) -> str:
    try:
        return subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class EngineManager:
    layout: EngineLayout
    port: int = BUNDLED_PORT
    api_key: str | None = None  # None: keep the key already in settings.json, else make one
    extra_model_dirs: Sequence[Path] = ()
    # Replaces `python -m omlx.cli serve`; tests substitute a stub server.
    launcher: Sequence[str] | None = None
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    # Set by the caller once the checked client has proven the listener is the child. Cleared
    # whenever the child changes; code that cannot do the check itself (plain urllib calls)
    # sends the engine key only while this is True.
    verified: bool = field(default=False, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _incarnation: Incarnation | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.port in RESERVED_PORTS or not 1024 <= self.port <= 65535:
            raise EngineError(f"Port {self.port} is not usable for the bundled engine")
        if self.api_key is None:
            self.api_key = stored_api_key(self.layout.settings_path) or new_api_key()
        if len(self.api_key) < 32:
            raise EngineError("The engine API key is too short")

    # -- pure builders (unit-testable, no side effects) -------------------------------
    def model_dirs(self) -> list[Path]:
        return [self.layout.models_dir, *map(Path, self.extra_model_dirs)]

    def command(self) -> list[str]:
        prefix = list(self.launcher) if self.launcher else [
            str(self.layout.python), "-m", "omlx.cli", "serve"]
        return [*prefix, "--host", "127.0.0.1", "--port", str(self.port),
                "--base-path", str(self.layout.base_path)]   # model dirs come from settings.json

    def environment(self) -> dict[str, str]:
        # PYTHONSAFEPATH keeps the working directory off sys.path and PYTHONNOUSERSITE keeps the
        # user site under HOME off it, so the engine imports only the trees attribution audits.
        return {"HOME": str(self.layout.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONPATH": self.layout.pythonpath, "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1",
                "LANG": "en_US.UTF-8", "TMPDIR": str(self.layout.data_dir / "tmp")}

    # -- lifecycle -----------------------------------------------------------------
    def prepare(self) -> None:
        for directory in (self.layout.data_dir, self.layout.home, self.layout.base_path,
                          self.layout.models_dir, self.layout.data_dir / "tmp"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = read_settings(self.layout.settings_path)
        merged = merge_owned(existing, engine_settings(
            self.port, self.api_key, self.model_dirs(), user_dirs=_saved_model_dirs(existing)))
        _write_private(self.layout.settings_path, json.dumps(merged, indent=2))

    def _reap_stale(self) -> None:
        reap_orphan(self.layout)

    def reap_orphan(self) -> bool:
        """Stop an engine a previous Wisp left behind; never the child this manager owns."""
        return False if self.running() else reap_orphan(self.layout)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        """Launch the engine unless it is already running. True only when THIS call launched it,
        decided under the lock, so a caller can tell its own launch from a concurrent one."""
        # One launch at a time: two overlapping starts would each pass the busy-port check and
        # start an engine, leaving one untracked and holding the port.
        with self._lock:
            return self._start()

    def mark_verified(self, incarnation) -> None:
        """Record that the checked client verified `incarnation`. Ignored if the child has since
        changed, so a stop and restart during the check can never leave a new child 'verified'."""
        with self._lock:
            if incarnation is not None and incarnation is self._incarnation and self.running():
                self.verified = True

    def _start(self) -> bool:
        if self.running():
            return False
        self.verified = False
        if not self.launcher and not self.layout.python.exists():
            raise EngineError("The bundled engine is not installed")
        self._reap_stale()
        self.prepare()
        if self._port_in_use():
            raise EngineError(f"Port {self.port} is already in use by another process")
        fd = os.open(self.layout.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)          # a log from an earlier run may have been created looser
        log = open(fd, "ab")
        try:
            self._proc = subprocess.Popen(
                self.command(), env=self.environment(), cwd=str(self.layout.data_dir),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        finally:
            log.close()
        _write_private(self.layout.pid_path, str(self._proc.pid))
        try:
            self._incarnation = self._record_incarnation(self._proc.pid)
        except Exception as exc:  # noqa: BLE001 — an engine Wisp cannot identify must not run
            self.stop()
            raise EngineError("Could not identify the engine process") from exc
        return True

    def _record_incarnation(self, pid: int) -> Incarnation:
        from service.inference.local_peer import AuthRefused, process_identity, process_path
        # A freshly exec'd process can be briefly unreadable (a first-run code-signing
        # assessment holds it in dyld), so retry for a couple of seconds before giving up.
        for attempt in range(40):
            try:
                pid, uid, seconds, micros = process_identity(pid, os.getuid())
                return Incarnation(pid, uid, seconds, micros, Path(process_path(pid)), self.layout.root)
            except AuthRefused:
                if attempt == 39 or not self.running():
                    raise
                time.sleep(0.1)

    def incarnation(self) -> Incarnation | None:
        """The live child this manager launched, else None. Never read from disk."""
        return self._incarnation if self.running() else None

    def _port_in_use(self) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", self.port)) == 0

    async def wait_ready(self, timeout: float = 60.0) -> bool:
        """True once the child we launched answers HTTP on its port (any status).

        This sends no credential and follows no proxy: the port is not yet known to belong to
        the child (another process could bind it while the engine is still importing), so the
        API key must never be offered here. Identity is proven by the attributed transport on
        the first real request, which refuses anything that is not the recorded child.
        """
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as http:
            while time.monotonic() < deadline:
                if self._proc is None or self._proc.poll() is not None:
                    return False
                try:
                    await http.get(f"http://127.0.0.1:{self.port}/health")
                    return True      # any HTTP answer: oMLX returns 503 "loading" while it preloads models
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.4)
        return False

    def stop(self, grace: float = 10.0) -> None:
        with self._lock:
            self._stop(grace)

    def _stop(self, grace: float) -> None:
        proc, self._proc, self._incarnation = self._proc, None, None
        self.verified = False
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        try:
            self.layout.pid_path.unlink()
        except OSError:
            pass
