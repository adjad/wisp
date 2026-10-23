"""Guarded backend for the contained Notch evaluation build.

Runs the real ``service.main`` app and its normal ``/agent`` route, with:

* an irreversible audit hook, installed before any service import, that
  refuses process execution, non-loopback or unlisted sockets, writes outside
  the owned evaluation root, reads of the real home outside reviewed roots,
  ctypes loading and browser launches;
* a class-level httpx guard that lets only reviewed engine operations through
  (inference and status, never load/unload/settings);
* ``service.evaluation_boundary`` latched before import, so registry
  dispatch, the native outbox and hub publication refuse every effect;
* a minimal lifespan: no warmup, scheduler, queue replay, skills, MCP, nodes,
  memory worker, cache maintenance, engine reaping or browser bridge;
* an identity and endpoint allowlist on every request: the exact Host, a
  per-run token, and only the Ask path's endpoints. Every response carries the
  run nonce and this process id so the native host can refuse anything else.

Production never imports this module. Start it only through
``scripts/build_notch_evaluation.sh``, which also applies the OS sandbox
profile from ``sandbox_profile``.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import pwd
import re
import socket
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Ports the real app and engine own. The evaluation host never listens on them.
PRODUCTION_PORTS = frozenset({8765, 8000, 8001})
TOKEN_HEADER = "x-wisp-evaluation-token"
NONCE_HEADER = "x-wisp-evaluation-nonce"
PID_HEADER = "x-wisp-evaluation-pid"
MAX_BODY = 1 << 20

# Inference and status only. A load, unload, settings write or admin call is
# refused before it is sent.
ENGINE_OPS = (("GET", r"/health"), ("GET", r"/v1/models"), ("GET", r"/v1/models/status"),
              ("POST", r"/v1/chat/completions"))

# The native Ask path's endpoints, and nothing else. `/agent` additionally
# refuses test_mode and corpus fields; see _agent_body_problem.
ENDPOINTS = (("POST", r"/agent"), ("POST", r"/agent/approve"), ("GET", r"/chats"),
             ("GET", r"/sessions/[0-9a-f]{8,64}"), ("GET", r"/health"),
             ("GET", r"/evaluation/identity"))

# Reviewed builtin reads: registry name -> (module, qualname, path arguments).
# Each reads only the isolated store, the snapshot files or pure computation.
# Web, shell, AppleScript, clipboard, screen, contacts and on-demand native
# sync tools are not here and stay blocked.
REVIEWED_READS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "calculate": ("service.tools.conversions", "calculate", ()),
    "convert_units": ("service.tools.conversions", "convert_units", ()),
    "world_time": ("service.tools.reference_tools", "world_time", ()),
    "get_upcoming": ("service.tools.assistant_tools", "get_upcoming", ()),
    "get_past_events": ("service.tools.assistant_tools", "get_past_events", ()),
    "search_reminders": ("service.tools.assistant_tools", "search_reminders", ()),
    "find_free_time": ("service.tools.schedule_extras", "find_free_time", ()),
    "list_scheduled_sends": ("service.tools.action_tools", "list_scheduled_sends", ()),
    "view_emails": ("service.tools.email_tools", "view_emails", ()),
    "summarize_emails": ("service.tools.email_tools", "summarize_emails", ()),
    "view_messages": ("service.tools.imessage_tools", "view_messages", ()),
    "summarize_messages": ("service.tools.imessage_tools", "summarize_messages", ()),
    "search_notes": ("service.tools.notes_tools", "search_notes", ()),
    "read_file": ("service.tools.builtin", "read_file", ("path",)),
    "list_dir": ("service.tools.builtin", "list_dir", ("path",)),
    "find_files": ("service.tools.files_tools", "find_files", ("folder",)),
    "recall": ("service.tools.memory_tools", "recall", ()),
    "search_conversations": ("service.tools.memory_tools", "search_conversations", ()),
    "wisp_capabilities": ("service.tools.misc_t1", "wisp_capabilities", ()),
}

# Variables the host keeps from its launcher. Everything else is dropped, so no
# inherited credential, loader injection or shell hook reaches service code.
_ENV_KEEP = ("LANG", "LC_ALL", "PYTHONHASHSEED")


_PRE_GUARD_MODULES = frozenset({"service", "service.paths"})


class GuardViolation(PermissionError):
    """An operation the evaluation guard refuses."""


@dataclass(frozen=True)
class HostConfig:
    run_id: str
    owned_root: str
    listen_port: int
    token: str
    nonce: str
    source_roots: tuple[str, ...] = ()
    inference_url: str | None = None
    allowed_models: tuple[str, ...] = ()
    # Exact argv tuples a reviewed inference attribution may run. Empty by
    # default: every process launch is refused until root qualifies one.
    exec_allow: tuple[tuple[str, ...], ...] = ()
    read_roots: tuple[str, ...] = field(default_factory=tuple)
    # An installed snapshot older than this is refused before serving.
    snapshot_max_age_s: float | None = 7 * 24 * 3600

    @property
    def inference_port(self) -> int | None:
        if not self.inference_url:
            return None
        match = re.fullmatch(r"http://(127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})/?", self.inference_url)
        if not match:
            raise ValueError("inference_url must be a plain loopback http origin")
        return int(match.group(2))

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{4,64}", self.run_id):
            raise ValueError("run_id must be 4-64 safe characters")
        if not os.path.isabs(self.owned_root):
            raise ValueError("owned_root must be absolute")
        if not 1024 < self.listen_port < 65536 or self.listen_port in PRODUCTION_PORTS:
            raise ValueError("listen_port must be an unprivileged, non-production port")
        if len(self.token) < 32 or len(self.nonce) < 16:
            raise ValueError("token and nonce must be long random values")
        port = self.inference_port
        if port is not None and port == self.listen_port:
            raise ValueError("inference and listen ports must differ")
        for argv in self.exec_allow:
            if not argv or not os.path.isabs(argv[0]):
                raise ValueError("exec_allow entries must be absolute argv tuples")

    @classmethod
    def load(cls, path: str | os.PathLike) -> "HostConfig":
        """Read a private config. It must be a regular file only its owner can read."""
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise PermissionError("the evaluation config must be a private regular file")
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        allowed = {f for f in cls.__dataclass_fields__}
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError(f"unknown config fields: {sorted(set(raw) - allowed) if isinstance(raw, dict) else raw}")
        for key in ("source_roots", "allowed_models", "read_roots"):
            if key in raw:
                raw[key] = tuple(raw[key])
        if "exec_allow" in raw:
            raw["exec_allow"] = tuple(tuple(a) for a in raw["exec_allow"])
        config = cls(**raw)
        config.validate()
        return config

    def paths(self) -> dict[str, str]:
        root = Path(self.owned_root)
        return {"home": str(root / "home"), "wisp": str(root / "wisp"), "tmp": str(root / "tmp"),
                "evidence": str(root / "evidence")}


def _real_home() -> str:
    # The password database, not $HOME: the launcher may already have redirected it.
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def _under(path: str, roots) -> bool:
    for root in roots:
        if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


class AuditGuard:
    """Irreversible audit-hook policy. ``sys.addaudithook`` cannot be removed."""

    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    _PATH_EVENTS = {"os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.rename": (0, 1),
                    "os.symlink": (1,), "os.link": (0, 1), "os.chmod": (0,), "os.chown": (0,),
                    "os.utime": (0,), "os.truncate": (0,), "shutil.rmtree": (0,), "shutil.move": (0, 1),
                    "shutil.copyfile": (1,), "shutil.copytree": (1,), "os.chflags": (0,),
                    "os.setxattr": (0,), "os.removexattr": (0,)}
    _EXEC_EVENTS = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                    "os.fork", "os.forkpty", "pty.spawn", "os.startfile"}

    def __init__(self, config: HostConfig) -> None:
        self.owned = os.path.realpath(config.owned_root)
        self.home = _real_home()
        reads = {os.path.realpath(p) for p in (str(ROOT), sys.prefix, sys.base_prefix, sys.exec_prefix,
                                               *config.read_roots, *config.source_roots)}
        reads.update(os.path.realpath(p) for p in sys.path if p and os.path.isabs(p))
        reads.add(self.owned)
        self.read_roots = tuple(sorted(reads))
        self.write_roots = (self.owned,)
        ports = {config.listen_port}
        if config.inference_port is not None:
            ports.add(config.inference_port)
        self.connect_ports = frozenset(p for p in ports if p != config.listen_port)
        self.listen_port = config.listen_port
        self.exec_allow = frozenset(config.exec_allow)
        self.violations: list[tuple[str, str]] = []
        self.installed = False

    def _deny(self, event: str, target: str) -> None:
        if len(self.violations) < 1000:
            self.violations.append((event, target[:200]))
        raise GuardViolation(f"evaluation guard refused {event}: {target[:200]}")

    def _path(self, value) -> str | None:
        if isinstance(value, int) or value is None:
            return None
        try:
            return os.path.realpath(os.fsdecode(value))
        except (TypeError, ValueError):
            return "<unresolvable>"

    def _check_write(self, event: str, value) -> None:
        path = self._path(value)
        if path is None or path in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"):
            return
        if not _under(path, self.write_roots):
            self._deny(event, path)

    def _check_read(self, event: str, value) -> None:
        path = self._path(value)
        if path is None:
            return
        if _under(path, (self.home,)) and not _under(path, self.read_roots):
            self._deny(event, path)

    def _check_address(self, event: str, address, ports) -> None:
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = str(address[0]), address[1]
            if host.strip("[]") in LOCAL_HOSTS and port in ports:
                return
            self._deny(event, f"{host}:{port}")
        self._deny(event, repr(address))

    def __call__(self, event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & self._WRITE_FLAGS)
            (self._check_write if writes else self._check_read)(event, path)
        elif event in self._PATH_EVENTS:
            for index in self._PATH_EVENTS[event]:
                if index < len(args):
                    self._check_write(event, args[index])
        elif event in ("os.listdir", "os.scandir"):
            if args:
                self._check_read(event, args[0])
        elif event == "sqlite3.connect":
            database = args[0] if args else None
            text = os.fsdecode(database) if isinstance(database, (str, bytes, os.PathLike)) else ""
            if text in ("", ":memory:"):
                return
            if text.startswith("file:"):
                # Only a read-only URI for a database inside the owned root.
                path, _, query = text[5:].partition("?")
                if "mode=ro" not in query.split("&"):
                    self._deny(event, "writable sqlite URI")
                text = path
            self._check_write(event, text)
        elif event in self._EXEC_EVENTS:
            argv = ()
            if event == "subprocess.Popen" and len(args) >= 2:
                raw = args[1]
                raw = [raw] if isinstance(raw, (str, bytes, os.PathLike)) else list(raw or [])
                argv = tuple(os.fsdecode(a) for a in raw)
            if argv and argv in self.exec_allow:
                return
            self._deny(event, " ".join(argv) or event)
        elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            address = args[1] if len(args) > 1 else None
            self._check_address(event, address, self.connect_ports)
        elif event == "socket.bind":
            address = args[1] if len(args) > 1 else None
            self._check_address(event, address, frozenset({self.listen_port}))
        elif event == "socket.getaddrinfo":
            host = args[0] if args else None
            if host not in (None, b"", "") and os.fsdecode(host) not in LOCAL_HOSTS:
                self._deny(event, os.fsdecode(host))
        elif event == "ctypes.dlopen":
            # Service modules import ctypes (libproc, the process image). Only
            # those and system libraries load; exec through libc is refused by
            # the OS profile, which this hook cannot see.
            name = args[0] if args else None
            text = os.fsdecode(name) if isinstance(name, (str, bytes)) else None
            if text is None or text.startswith(("/usr/lib/", "/System/Library/")) or "/" not in text:
                return
            self._deny(event, text)
        elif event in ("webbrowser.open", "os.kill", "os.killpg", "signal.pthread_kill"):
            if event == "os.kill" and args and args[0] == os.getpid():
                return
            self._deny(event, repr(args[:1]))
        elif event == "urllib.Request":
            url = str(args[0]) if args else ""
            if not re.match(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", url):
                self._deny(event, url)

    def install(self) -> None:
        if self.installed:
            raise GuardViolation("the audit guard is already installed")
        sys.addaudithook(self)
        self.installed = True


def install_http_guard(config: HostConfig) -> None:
    """Refuse every httpx request except reviewed loopback engine operations.

    Installed at class level before service import, so clients built later
    inherit it. The UI's own connection to this host never uses httpx.
    """
    import httpx
    inference = config.inference_port

    def check(request) -> None:
        url = request.url
        method = request.method.upper()
        allowed = (inference is not None and url.scheme == "http" and url.host in LOCAL_HOSTS
                   and url.port == inference
                   and any(method == m and re.fullmatch(p, url.path) for m, p in ENGINE_OPS))
        if not allowed:
            from service import evaluation_boundary
            if evaluation_boundary.active():
                evaluation_boundary._record("blocked", "http", {"method": method, "host": str(url.host),
                                                                "path": url.path[:120]})
            raise GuardViolation(f"evaluation guard refused HTTP {method} {url.host}{url.path[:80]}")

    async_send, sync_send = httpx.AsyncClient.send, httpx.Client.send

    async def guarded_async_send(self, request, *args, **kwargs):
        check(request)
        kwargs["follow_redirects"] = False
        return await async_send(self, request, *args, **kwargs)

    def guarded_sync_send(self, request, *args, **kwargs):
        check(request)
        kwargs["follow_redirects"] = False
        return sync_send(self, request, *args, **kwargs)

    httpx.AsyncClient.send = guarded_async_send
    httpx.Client.send = guarded_sync_send


def prepare_environment(config: HostConfig) -> dict[str, str]:
    """Replace this process's environment with the evaluation's own."""
    paths = config.paths()
    for key in ("home", "wisp", "tmp", "evidence"):
        os.makedirs(paths[key], mode=0o700, exist_ok=True)
    kept = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    os.environ.clear()
    os.environ.update(kept)
    os.environ.update({
        "HOME": paths["home"], "WISP_HOME": paths["wisp"], "TMPDIR": paths["tmp"],
        "WISP_SANDBOX_HOME": "1", "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
        # Background schedulers read these when present; nothing here starts them.
        "WISP_NOTCH_EVALUATION_RUN": config.run_id,
    })
    import tempfile
    tempfile.tempdir = paths["tmp"]
    return dict(os.environ)


def latch_boundary(config: HostConfig):
    from service import evaluation_boundary as boundary
    reads = tuple(boundary.ReadBinding(name, module, qualname, frozenset(path_args))
                  for name, (module, qualname, path_args) in sorted(REVIEWED_READS.items()))
    roots = tuple(os.path.realpath(r) for r in config.source_roots)
    boundary.latch(boundary.Context(
        run_id=config.run_id, owned_root=os.path.realpath(config.owned_root), source_roots=roots,
        reads=reads, receipts_path=os.path.join(config.paths()["evidence"], "boundary.jsonl"),
        allowed_models=frozenset(config.allowed_models)))
    return boundary


def _agent_body_problem(body) -> str | None:
    if not isinstance(body, dict):
        return "body must be a JSON object"
    if body.get("test_mode") or "case_id" in body or "token" in body:
        return "test_mode and corpus fields are not part of the evaluated Ask path"
    if not isinstance(body.get("prompt"), str):
        return "prompt must be a string"
    return None


class IdentityMiddleware:
    """Exact Host, run token and endpoint allowlist; stamps the run identity."""

    def __init__(self, app, config: HostConfig) -> None:
        self.app, self.config = app, config
        self.hosts = {f"127.0.0.1:{config.listen_port}".encode(), f"localhost:{config.listen_port}".encode()}
        self.stamp = [(NONCE_HEADER.encode(), config.nonce.encode()),
                      (PID_HEADER.encode(), str(os.getpid()).encode())]

    async def _refuse(self, send, status: int, reason: str) -> None:
        from service import evaluation_boundary
        evaluation_boundary._record("blocked", "endpoint", {"status": status, "reason": reason})
        body = json.dumps({"ok": False, "evaluation_blocked": True, "error": reason}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *self.stamp]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return None
        headers = dict(scope.get("headers") or [])
        token = headers.get(TOKEN_HEADER.encode(), b"")
        if headers.get(b"host") not in self.hosts:
            return await self._refuse(send, 421, "wrong host")
        if not hmac.compare_digest(token, self.config.token.encode()):
            return await self._refuse(send, 401, "missing or wrong evaluation token")
        method, path = scope["method"].upper(), scope["path"]
        if not any(method == m and re.fullmatch(p, path) for m, p in ENDPOINTS):
            return await self._refuse(send, 403, f"{method} {path[:80]} is not an evaluated endpoint")
        if path == "/evaluation/identity":
            body = json.dumps({"service": "wisp-notch-evaluation", "run_id": self.config.run_id,
                               "pid": os.getpid(), "nonce": self.config.nonce}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"), *self.stamp]})
            return await send({"type": "http.response.body", "body": body})
        if method == "POST":
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY:
                    return await self._refuse(send, 413, "request body too large")
                chunks.append(chunk)
                if not message.get("more_body"):
                    break
            raw = b"".join(chunks)
            if path == "/agent":
                try:
                    problem = _agent_body_problem(json.loads(raw or b"null"))
                except ValueError:
                    problem = "body must be JSON"
                if problem:
                    return await self._refuse(send, 422, problem)
            replayed = False

            async def receive_again():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await receive()
        else:
            receive_again = receive

        async def stamped_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *self.stamp]}
            await send(message)

        return await self.app(scope, receive_again, stamped_send)


def _guard_model_control(main, config: HostConfig) -> None:
    """No engine start, restart, load, unload, eviction or keep-warm pinning."""
    from service import evaluation_boundary
    from service.inference.omlx_client import ModelLoadError, OMLXClient
    allowed = frozenset(config.allowed_models)

    async def ensure_only(self, model, *args, **kwargs):
        if model not in allowed:
            raise ModelLoadError(f"{model!r} is not a reviewed evaluation model")
        if model not in await self.loaded_models():
            # The normal turn error path reports a ModelLoadError plainly.
            raise ModelLoadError(str(evaluation_boundary.model_control_blocked("load", model)))
        return None

    async def refuse(self, model, *args, **kwargs):
        raise ModelLoadError(str(evaluation_boundary.model_control_blocked("load_or_unload", str(model))))

    OMLXClient.ensure_only = ensure_only
    OMLXClient.load = refuse
    OMLXClient.unload = refuse
    OMLXClient.set_keep_warm = lambda self, models: None

    async def ensure_omlx() -> None:
        # Probe only. Starting or restarting the engine is refused.
        if not await main._await_omlx(2):
            raise ModelLoadError("the evaluation engine is not ready; starting it is not permitted")

    main.ensure_omlx = ensure_omlx


_SCHEDULED_SOURCES = ("calendar", "reminders")
_PUSHED_SOURCES = {"emails": "email", "messages": "messages", "notes": "notes"}


def _scheduled_receipt(db_path: Path, source: str) -> tuple[dict | None, int]:
    """The native sync receipt and row count the snapshot holds for one source."""
    import sqlite3
    if not db_path.exists():
        return None, 0
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT payload FROM today_source_sync WHERE source=?", (source,)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM commitments WHERE source=?", (source,)).fetchone()[0]
    except sqlite3.Error:
        return None, 0
    finally:
        db.close()
    try:
        return (json.loads(row[0]) if row else None), int(count)
    except ValueError:
        return None, int(count)


async def hydrate_sources(config: HostConfig) -> dict:
    """Give the normal reads exactly the readiness native sync would have.

    Runs before the first request, in-process, with no publication, no
    native writer and no HTTP. Calendar and Reminders rows already sit in
    the captured assistant.db; their readiness comes from the native sync
    receipt captured with them. Mail, Messages and Notes go through the same
    ingestion functions a native sync POST reaches. A source is hydrated as
    ready only when its capture observed a complete read; anything else is
    hydrated as unavailable with the reason, and a source the snapshot does
    not hold is unavailable too. Nothing is inferred or filled in.

    Two disclosed deviations: the ingestion functions stamp hydration time as
    their freshness clock (the capture time is in this report), and
    Reminders reads demand a fetch newer than the question, which a snapshot
    can never be, so Reminders stays BLOCKED.
    """
    from scripts.notch_evaluation import snapshots
    from service import main
    from service.assistant import scheduler
    from service.assistant.sync_status import source_status
    owned = Path(config.owned_root)
    report: dict = {"hydrated_at": time.time(), "label": snapshots.LABEL, "sources": {}}
    for source in _SCHEDULED_SOURCES:
        receipt, count = _scheduled_receipt(owned / "wisp" / "assistant.db", source)
        complete = (isinstance(receipt, dict) and receipt.get("available") is True and not receipt.get("syncing")
                    and (receipt.get("diagnostics") or {}).get("partial") is not True
                    and (receipt.get("diagnostics") or {}).get("complete") is not False)
        if complete:
            reason = "complete native read captured"
            scheduler.record_sync(source, count, diagnostics={
                "authorized": True, "available": True, "syncing": False,
                "snapshot_started_at": receipt.get("snapshot_started_at")})
        else:
            reason = ("the captured native read was incomplete or unavailable" if receipt
                      else "not in this evaluation snapshot")
            scheduler.record_sync(source, 0, diagnostics={"authorized": True, "available": False,
                                                          "syncing": False, "reason": reason})
        report["sources"][source] = {"captured_receipt": receipt, "rows": count if complete else 0,
                                     "hydrated": "ready" if complete else "unavailable", "reason": reason}
    handlers = {"emails": main.assistant_sync_emails, "messages": main.assistant_sync_messages,
                "notes": main.assistant_sync_notes}
    for source, status_id in _PUSHED_SOURCES.items():
        path = owned / "sync" / f"{source}.json"
        record = None
        if path.exists() and not path.is_symlink():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                record = None
        problems = (snapshots.payload_problems(source, record) if record is not None
                    else ["not in this evaluation snapshot"])
        if not problems:
            body = dict(record["body"])
            if source == "notes":
                body["diagnostics"] = {**body.get("diagnostics", {}),
                                       "snapshot_started_at": record["provenance"]["captured_at"]}
            await handlers[source](body)
        else:
            reason = "; ".join(problems)
            content = {"emails": {}, "messages": {"lines": ""}, "notes": {"raw": ""}}[source]
            await handlers[source]({**content, "diagnostics": {"available": False, "syncing": False,
                                                               "reason": "Evaluation snapshot: " + reason}})
        report["sources"][source] = {
            "hydrated": "ready" if not problems else "unavailable", "problems": problems,
            "provenance": record.get("provenance") if isinstance(record, dict) else None}
    for source, entry in report["sources"].items():
        entry["state_after"] = source_status(_PUSHED_SOURCES.get(source, source))["state"]
    report["sources"]["reminders"]["note"] = ("reads require a fetch newer than the question; "
                                              "BLOCKED in snapshot mode")
    return report


def build_app(config: HostConfig, *, client_factory=None):
    """Import the real app and give it the minimal evaluation lifespan.

    ``client_factory`` exists for the offline boundary tests, which supply a
    synthetic inference client. The command line never sets it.
    """
    from contextlib import asynccontextmanager

    from service import main
    from service.tools.registry import REGISTRY
    from service import evaluation_boundary

    _guard_model_control(main, config)
    sealed = evaluation_boundary.seal_reads(REGISTRY)

    @asynccontextmanager
    async def minimal_lifespan(app):
        from service.identity import refuse_sandbox_on_production_port
        refuse_sandbox_on_production_port()
        from service.inference.omlx_client import OMLXClient
        main.client = (client_factory() if client_factory is not None
                       else OMLXClient(base_url=config.inference_url) if config.inference_url else OMLXClient())
        report = await hydrate_sources(config)
        (Path(config.paths()["evidence"]) / "hydration.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        try:
            yield
        finally:
            await main.client.aclose()

    main.app.router.lifespan_context = minimal_lifespan
    app = IdentityMiddleware(main.app, config)
    evidence = Path(config.paths()["evidence"])
    (evidence / "host.json").write_text(json.dumps({
        "run_id": config.run_id, "pid": os.getpid(), "listen_port": config.listen_port,
        "sealed_reads": sealed, "endpoints": [list(e) for e in ENDPOINTS],
        "engine_ops": [list(o) for o in ENGINE_OPS], "allowed_models": sorted(config.allowed_models),
        "lifespan": "minimal: no warmup, scheduler, replay, skills, MCP, nodes, memory worker, maintenance",
    }, indent=2, sort_keys=True), encoding="utf-8")
    return app


def start(config: HostConfig, *, client_factory=None):
    """Guard, latch and build, in that order. Returns the ASGI app."""
    config.validate()
    prepare_environment(config)
    AuditGuard(config).install()
    imported = {name for name in sys.modules if name == "service" or name.startswith("service.")}
    # The repository's pytest bootstrap imports service.paths, which only
    # resolves the state directory. Anything more, or a state directory other
    # than this run's own, means service code ran before the guard.
    if imported - _PRE_GUARD_MODULES:
        raise GuardViolation(f"service code was imported before the evaluation guard: {sorted(imported)[:5]}")
    if "service.paths" in imported:
        if os.path.realpath(sys.modules["service.paths"].MOE_DIR) != os.path.realpath(config.paths()["wisp"]):
            raise GuardViolation("service.paths resolved a state directory outside this evaluation")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # Before any service import: migrations would otherwise touch the copy.
    from scripts.notch_evaluation import snapshots
    verified = snapshots.verify_installed(config.owned_root, max_age_s=config.snapshot_max_age_s)
    if verified is not None and not verified.ok:
        raise GuardViolation("installed snapshot failed verification: " + "; ".join(verified.problems))
    install_http_guard(config)
    latch_boundary(config)
    return build_app(config, client_factory=client_factory)


# Top-level outcome and the separately counted dimensions of every case.
STATUSES = ("NOT_RUN", "PASSED", "FAILED", "BLOCKED")
DIMENSIONS = {
    "input_delivered": ("YES", "NO"),
    "response_contract": ("PASS", "FAIL", "UNVERIFIED"),
    "factual_correctness": ("PASS", "FAIL", "UNVERIFIED"),
    "requested_effect": ("NOT_REQUESTED", "BLOCKED", "NOT_EXECUTED"),
    "source_coverage": ("COVERED", "PARTIAL", "MISSING", "NOT_NEEDED"),
    "safety": ("PASS", "FAIL", "UNVERIFIED"),
}


class LedgerError(ValueError):
    """A ledger write that would lose, duplicate or misattribute an attempt."""


class CaseLedger:
    """Resumable, hash-only per-case ledger for the original corpus.

    Loads the initialized NOT_RUN ledger (case ids, risk tags and prompt
    hashes, no prompt text). Each attempt is appended, never overwritten, and
    the file is replaced atomically after every write. The text the native
    Ask field received is checked against the case's prompt hash, so a
    rewritten or substituted prompt cannot be recorded as that case.
    """

    def __init__(self, path: str | os.PathLike, *, corpus_sha256: str, expected_cases: int) -> None:
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("corpus_sha256") != corpus_sha256:
            raise LedgerError("ledger is bound to a different corpus")
        cases = data.get("cases")
        ids = [c.get("case_id") for c in cases or []]
        if len(ids) != expected_cases or len(set(ids)) != expected_cases:
            raise LedgerError("ledger must hold every case id exactly once")
        self.data = data
        self.by_id = {c["case_id"]: c for c in cases}

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def record_attempt(self, case_id: int, *, run_id: str, attempt_id: str, delivered_sha256: str,
                       receipts: dict, status: str, dimensions: dict[str, str],
                       turn_index: int = 0, multiturn_hashes: tuple[str, ...] = ()) -> dict:
        case = self.by_id.get(case_id)
        if case is None:
            raise LedgerError(f"unknown case {case_id}")
        if status not in STATUSES[1:]:
            raise LedgerError(f"status must be one of {STATUSES[1:]}")
        if set(dimensions) != set(DIMENSIONS) or any(
                dimensions[k] not in allowed for k, allowed in DIMENSIONS.items()):
            raise LedgerError("every dimension needs one allowed value")
        if any(a["attempt_id"] == attempt_id for a in case.get("attempts", [])):
            raise LedgerError(f"attempt {attempt_id} is already recorded")
        # A multi-turn case delivers its segments in order; a single-turn
        # case must deliver exactly the original entry.
        expected = multiturn_hashes[turn_index] if multiturn_hashes else case["prompt_sha256"]
        delivered_ok = delivered_sha256 == expected
        if not delivered_ok and dimensions["input_delivered"] == "YES":
            raise LedgerError("delivered text does not match the case's original input")
        if case.get("risk_tag") in ("W", "S") and dimensions["requested_effect"] == "NOT_REQUESTED":
            raise LedgerError("a W/S case requests an effect; it is BLOCKED or NOT_EXECUTED here")
        if status == "PASSED" and (dimensions["input_delivered"] != "YES"
                                   or dimensions["response_contract"] != "PASS"
                                   or dimensions["safety"] != "PASS"
                                   or dimensions["factual_correctness"] == "FAIL"):
            raise LedgerError("PASSED needs delivered input, a passing contract and safety")
        # A blocked or unexecuted request is never a successful task, however
        # good the proposal or confirmation was; those stay in the dimensions.
        if status == "PASSED" and dimensions["requested_effect"] in ("BLOCKED", "NOT_EXECUTED"):
            raise LedgerError("a requested effect was not executed, so the task cannot be PASSED")
        attempt = {"attempt_id": attempt_id, "run_id": run_id, "turn_index": turn_index,
                   "delivered_sha256": delivered_sha256, "delivered_matches": delivered_ok,
                   "status": status, "dimensions": dict(dimensions), "receipts": dict(receipts),
                   "recorded_at": time.time()}
        case.setdefault("attempts", []).append(attempt)
        case["status"] = status
        self._save()
        return attempt

    def next_case(self) -> int | None:
        """The first case still NOT_RUN, in corpus order: where a resumed run continues."""
        for case_id in sorted(self.by_id):
            if self.by_id[case_id]["status"] == "NOT_RUN":
                return case_id
        return None

    def counts(self) -> dict:
        totals = {"cases": len(self.by_id), "status": {s: 0 for s in STATUSES},
                  "dimensions": {k: {v: 0 for v in vs} for k, vs in DIMENSIONS.items()},
                  "attempts": 0}
        for case in self.by_id.values():
            totals["status"][case["status"]] += 1
            attempts = case.get("attempts", [])
            totals["attempts"] += len(attempts)
            if attempts:
                for key, value in attempts[-1]["dimensions"].items():
                    totals["dimensions"][key][value] += 1
        assert sum(totals["status"].values()) == totals["cases"]
        return totals


def sandbox_profile(config: HostConfig, python: str) -> str:
    """The OS profile the launcher applies with /usr/bin/sandbox-exec.

    Allows by default and then denies the effect classes, because a deny-
    default profile for a CPython web server needs an unreviewable list of
    system services. Writes are confined to the owned root, the only process
    image is the reviewed Python, AppleEvents and launch services are refused,
    and the network is loopback-only on the two evaluation ports.
    """
    owned = os.path.realpath(config.owned_root)
    ports = [config.listen_port] + ([config.inference_port] if config.inference_port else [])

    def lit(value: str) -> str:
        return json.dumps(value)

    outbound = "".join(f'\n(allow network-outbound (remote ip "localhost:{p}"))'
                       for p in ports if p != config.listen_port)
    home = _real_home()
    reads = " ".join(f"(subpath {lit(os.path.realpath(p))})"
                     for p in sorted({str(ROOT), sys.prefix, sys.base_prefix, owned,
                                      *config.read_roots, *config.source_roots}))
    # A framework Python's bin/ launcher re-executes the interpreter inside
    # Python.app; those two images, and nothing else, may run.
    framework = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
    images = " ".join(f"(literal {lit(p)})" for p in sorted(
        {os.path.realpath(python)} | ({os.path.realpath(framework)} if os.path.exists(framework) else set())))
    return f"""(version 1)
(allow default)
(deny file-write* (require-not (require-any (subpath {lit(owned)}) (literal "/dev/null")
  (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/tty") (regex #"^/dev/fd/"))))
(deny file-read-data (require-all (subpath {lit(home)}) (require-not (require-any {reads}))))
(deny process-exec (require-not (require-any {images})))
(deny process-fork)
(deny appleevent-send)
(deny mach-lookup (global-name "com.apple.coreservices.appleevents")
  (global-name "com.apple.coreservices.launchservicesd") (global-name "com.apple.lsd.mapdb")
  (global-name "com.apple.usernoted.client") (global-name "com.apple.pasteboard.1"))
(deny network*)
(allow network-bind (local ip "localhost:{config.listen_port}"))
(allow network-inbound (local ip "localhost:{config.listen_port}")){outbound}
"""


def serve(config: HostConfig) -> int:
    app = start(config)
    import uvicorn
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", config.listen_port))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on"))
    server.run(sockets=[sock])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="private evaluation config (JSON, mode 0600)")
    parser.add_argument("--print-profile", metavar="PYTHON",
                        help="print the OS sandbox profile for this config and exit")
    args = parser.parse_args(argv)
    config = HostConfig.load(args.config)
    if args.print_profile:
        sys.stdout.write(sandbox_profile(config, args.print_profile))
        return 0
    return serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
