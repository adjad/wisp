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


def engine_settings(port: int, api_key: str, model_dirs: Sequence[Path]) -> dict:
    """The keys Wisp owns. oMLX fills in every other default on first start."""
    dirs = [str(Path(d)) for d in model_dirs]
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
                "--base-path", str(self.layout.base_path),
                "--model-dir", str(self.layout.models_dir)]

    def environment(self) -> dict[str, str]:
        return {"HOME": str(self.layout.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONPATH": self.layout.pythonpath, "PYTHONDONTWRITEBYTECODE": "1",
                "LANG": "en_US.UTF-8", "TMPDIR": str(self.layout.data_dir / "tmp")}

    # -- lifecycle -----------------------------------------------------------------
    def prepare(self) -> None:
        for directory in (self.layout.data_dir, self.layout.home, self.layout.base_path,
                          self.layout.models_dir, self.layout.data_dir / "tmp"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        merged = merge_owned(read_settings(self.layout.settings_path),
                             engine_settings(self.port, self.api_key, self.model_dirs()))
        _write_private(self.layout.settings_path, json.dumps(merged, indent=2))

    def _reap_stale(self) -> None:
        """Kill an engine orphaned by a crashed Wisp; never anything else."""
        try:
            pid = int(self.layout.pid_path.read_text().strip())
        except (OSError, ValueError):
            return
        if pid > 1 and str(self.layout.base_path) in _command_of(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass
        try:
            self.layout.pid_path.unlink()
        except OSError:
            pass

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running():
            return
        if not self.launcher and not self.layout.python.exists():
            raise EngineError("The bundled engine is not installed")
        self._reap_stale()
        self.prepare()
        if self._port_in_use():
            raise EngineError(f"Port {self.port} is already in use by another process")
        log = open(self.layout.log_path, "ab")
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
        deadline = time.monotonic() + timeout
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=2.0) as http:
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    return False
                try:
                    reply = await http.get(f"http://127.0.0.1:{self.port}/v1/models",
                                           headers=headers)
                    if reply.status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.4)
        return False

    def stop(self, grace: float = 10.0) -> None:
        proc, self._proc, self._incarnation = self._proc, None, None
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
