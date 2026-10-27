"""/resetwisp: erase Wisp's own state for a true first run, keeping the model.

The person types /resetwisp in the notch and confirms with an explicit Reset
Wisp button. The app then calls POST /assistant/reset_wisp, which:

1. seals the service (ResetGate): every other request is refused from here on,
   and requests already running are cancelled;
2. commits the reset: writes and fsyncs MARKER (``~/.moe/.reset-pending``)
   before anything destructive, so a reset interrupted from here on (a crash, a
   quit, a full disk) is finished by the next service start (resume_pending);
3. stops the service's background work (schedulers and their runs, syncs, MCP
   servers, the browser bridge, the behavior runtime);
4. disconnects every Google account (gmail_source.disconnect: local tokens and
   derived data go first, then each token is revoked with Google, best effort);
5. deletes everything in Wisp's home except KEEP_ENTRIES (the model, the
   engine and the plumbing that lets the engine start), and trims config.yaml
   to the keys that pick the engine and the model (trimmed_config).

The route ends with MARKER at phase "first-pass": it never claims the reset is
done. The app then stops the engine and this service and relaunches. The
backend the app launches (service/main.py, only with WISP_LAUNCH_NONCE) runs
resume_pending before any other import: with no live service and the engine
stopped, it erases the whole home once more (the restart pass, which catches
anything written after the first pass) together with the engine's prompt cache,
and only then writes phase "erased". Later starts retry only the names that
pass could not delete, at most MAX_ATTEMPTS times in all, and then write
"erased" with the leftovers as problems: a reset never loops, and never touches
data created after the restart pass. The relaunched app runs its own part
once per marker (Keychain provider keys, its defaults domain, its privacy
grants, notifications, its caches; APP_DONE records it) and removes MARKER last,
once it reads "erased". macOS accounts (System Settings ▸ Internet Accounts,
Mail, Calendar, Notes, Messages) are never touched: deleting a "connection"
means removing Wisp's access and every copy Wisp holds.

Never reachable by the model or the agent: it is not a tool, the shell policy
refuses commands that name the route (safety/policy._LOCAL_SEND_DENY), and the
route admits only the native app that launched this service (require_app_caller).
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import fcntl
import json
import os
import shutil
import stat
import secrets
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

APP_BUNDLE_ID = "com.wisp.assistant"
ROUTE = "/assistant/reset_wisp"
# The app sends exactly this; a generic {"confirm": true} from elsewhere is not enough.
CONFIRM_PHRASE = "reset wisp"

# Everything in Wisp's home that survives a reset. Anything else is deleted,
# including files a later version adds, so no state outlives a reset by being new.
# Written before the first destructive step, removed by the app last.
MARKER = ".reset-pending"
# The app's own record that it ran its part for one marker (holds the marker's id).
APP_DONE = ".reset-pending.app"
# Service starts that may work on one reset before it is declared done with leftovers.
MAX_ATTEMPTS = 3
KEEP_ENTRIES = frozenset({
    MARKER,                            # an unfinished reset: the next start finishes it
    APP_DONE,                          # the app ran its part for this marker
    "engine",                          # bundled engine: models/, base/ (its settings + key), home/
    "config.yaml",                     # trimmed to CONFIG_KEEP (engine mode + model choice)
    "provisioning",                    # keychain helper + reviewed endpoint bindings for the engine key
    ".credential-generation",          # Keychain credential generation; a mismatch quarantines the engine key
    ".helper-transaction.json",        # an in-flight keychain-helper transaction (recovery marker)
    "omlx-runtime-authorization.json", # attestation pin for the person's own oMLX runtime
    ".provisioning.lock", ".config.lock", ".engine-mode.lock",   # empty lock files other processes may hold
})
# Inside engine/: deleted even though the engine is kept (its log can hold request text).
ENGINE_DELETE = frozenset({"engine.log"})
# Inside engine/, deleted only while the engine is stopped (resume_pending): its
# prompt cache holds model state computed from past prompts, and its logs.
ENGINE_CACHES = ("base/cache", "base/logs")
# config.yaml keys that survive: the engine choice and the local model choice.
# Cloud and local-provider endpoints, keys and every other setting are dropped.
CONFIG_KEEP = ("engine.mode", "installed_models", "roles", "inference.bindings (local endpoint only)")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


class ResetRefused(Exception):
    """The reset cannot run safely here; nothing was deleted."""


# --------------------------------------------------------------------------
# The home and what is deleted from it
# --------------------------------------------------------------------------

def checked_home(home: Path, user_home: Path) -> Path:
    """Wisp's real home, or ResetRefused. Only ``<user home>/.moe`` itself
    qualifies: never ``/``, the user's home, a symlink, or anything outside the
    user's home (a WISP_HOME sandbox under $TMPDIR, another disk)."""
    home, user_home = Path(home), Path(user_home)
    if not home.is_absolute() or not user_home.is_absolute():
        raise ResetRefused("Wisp's home is not an absolute path")
    try:
        if home.is_symlink():
            raise ResetRefused("Wisp's home is a link; reset only works on a real folder")
        real = home.resolve(strict=True)
        real_user = user_home.resolve(strict=True)
        info = real.lstat()
    except FileNotFoundError:
        raise ResetRefused("Wisp's home does not exist") from None
    except OSError:
        raise ResetRefused("Wisp's home can't be read") from None
    if real == Path("/") or real == real_user or real_user == Path("/"):
        raise ResetRefused("Refusing to reset the whole disk or the home folder")
    if real_user not in real.parents:
        raise ResetRefused("Wisp's home is outside your home folder")
    if real != real_user / ".moe":
        raise ResetRefused("Reset only works on Wisp's own folder (~/.moe)")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ResetRefused("Wisp's home is not a folder you own")
    return real


def trimmed_config(doc: Any) -> dict:
    """config.yaml after a reset: only what picks the engine and the model."""
    if not isinstance(doc, dict):
        return {}
    out: dict = {}
    engine = doc.get("engine")
    if isinstance(engine, dict) and isinstance(engine.get("mode"), str):
        out["engine"] = {"mode": engine["mode"]}
    installed = doc.get("installed_models")
    if isinstance(installed, list) and all(isinstance(m, str) for m in installed):
        out["installed_models"] = list(installed)
    roles = doc.get("roles")
    if isinstance(roles, dict):
        kept = {k: v for k, v in roles.items() if isinstance(k, str) and isinstance(v, str)}
        if kept:
            out["roles"] = kept
    inference = doc.get("inference")
    bindings = inference.get("bindings") if isinstance(inference, dict) else None
    if isinstance(bindings, dict):
        local = {k: dict(v) for k, v in bindings.items()
                 if isinstance(k, str) and isinstance(v, dict) and v.get("endpoint", "local") == "local"}
        if local:
            out["inference"] = {"bindings": local}
    return out


def _remove(path: Path, device: int | None = None) -> None:
    """Delete one entry without following a link: a link (or file) is unlinked,
    a real folder is removed with everything in it, links inside unlinked. A
    folder holding another file system (a mount point) is refused, not entered."""
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise OSError(errno.ENOTSUP, "symlink-safe removal unavailable")
        if device is not None:
            for folder, dirs, _files in os.walk(path, followlinks=False):
                if os.lstat(folder).st_dev != device:
                    raise OSError(errno.EXDEV, "another file system is mounted inside")
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(folder, d))]
        shutil.rmtree(path)
    else:
        path.unlink()


def _sidecar_owner(name: str, names: set[str]) -> str | None:
    for suffix in _SQLITE_SIDECARS:
        if name.endswith(suffix) and name[: -len(suffix)] in names:
            return name[: -len(suffix)]
    return None


def _rewrite_config(home: Path) -> str:
    """Trim config.yaml in place under the config lock. Returns what happened."""
    import yaml
    path = home / "config.yaml"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISREG(info.st_mode):
        _remove(path)               # a link or anything odd: drop it, never follow it
        return "removed"
    lock_fd = os.open(home / ".config.lock", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as source:
            try:
                doc = yaml.safe_load(source.read())
            except yaml.YAMLError:
                doc = None
        kept = trimmed_config(doc)
        temp = home / ".config.yaml.reset"
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            if kept:
                yaml.safe_dump(kept, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    return "trimmed"


def erase_home(home: Path, user_home: Path, *, engine_caches: bool = False,
               only: set[str] | None = None) -> dict:
    """Delete every entry of Wisp's home outside KEEP_ENTRIES, trim config.yaml
    and drop the engine's log (and, with ``engine_caches``, its prompt cache and
    logs: only while the engine is stopped). Never follows a link out of the
    home and never enters another file system mounted inside it.

    Returns names only (Wisp's own file names, never contents): ``removed``,
    ``kept`` and ``failed``. A SQLite database is deleted only after its
    -wal/-shm/-journal files are, so a fresh database can never pick up an old
    write-ahead log on the next launch.

    ``only`` limits it to those names (top-level names, ``engine/...`` and
    ``config.yaml``): a retry of what an earlier pass could not delete, which
    never touches anything created since.
    """
    root = checked_home(home, user_home)
    device = root.lstat().st_dev
    removed: list[str] = []
    failed: list[str] = []
    entries = {entry.name for entry in os.scandir(root)}
    # Sidecars first, then the rest; a database waits for its sidecars.
    order = sorted(entries, key=lambda n: (_sidecar_owner(n, entries) is None, n))
    blocked: set[str] = set()
    for name in order:
        if name in KEEP_ENTRIES or (only is not None and name not in only):
            continue
        owner = _sidecar_owner(name, entries)
        if owner is None and name in blocked:
            failed.append(name)
            continue
        try:
            _remove(root / name, device)
            removed.append(name)
        except FileNotFoundError:
            removed.append(name)
        except OSError:
            failed.append(name)
            if owner is not None:
                blocked.add(owner)
    engine = root / "engine"
    try:
        engine_info = engine.lstat()
    except FileNotFoundError:
        engine_info = None
    if engine_info is not None and stat.S_ISDIR(engine_info.st_mode) and engine_info.st_dev == device:
        doomed = sorted(ENGINE_DELETE) + (list(ENGINE_CACHES) if engine_caches else [])
        if only is not None:
            doomed = [name for name in doomed if f"engine/{name}" in only]
        for name in doomed:
            target = engine / name
            try:
                # Never through a link: base/ itself must be a real folder of this disk.
                parent = target.parent.lstat()
                if not stat.S_ISDIR(parent.st_mode) or parent.st_dev != device:
                    continue
                _remove(target, device)
                removed.append(f"engine/{name}")
            except FileNotFoundError:
                pass
            except OSError:
                failed.append(f"engine/{name}")
    try:
        if only is None or "config.yaml" in only:
            if _rewrite_config(root) == "removed":
                removed.append("config.yaml")
    except Exception:  # noqa: BLE001 — reported, never fatal to the rest of the reset
        failed.append("config.yaml")
    kept = sorted(name for name in os.listdir(root) if name in KEEP_ENTRIES)
    return {"removed": sorted(removed), "failed": sorted(set(failed)), "kept": kept}


def leftover_entries(home: Path) -> list[str]:
    """Entries of the home that a reset would delete (empty right after one)."""
    return sorted(name for name in os.listdir(home) if name not in KEEP_ENTRIES)


# --------------------------------------------------------------------------
# Who may call it: only the app that launched this service
# --------------------------------------------------------------------------

def _tcp_peers(pid: int, local: tuple[str, int], remote: tuple[str, int]) -> int:
    """How many established IPv4 TCP sockets of ``pid`` run from ``local`` to ``remote``."""
    from service.inference import local_peer as lp
    library = lp._libproc()
    buffer = ctypes.create_string_buffer(8 * lp._MAX_FDS)
    returned = library.proc_pidinfo(pid, lp.PROC_PIDLISTFDS, 0, buffer, len(buffer))
    if returned <= 0 or returned % 8 or returned >= len(buffer):
        raise lp.NativeAnomaly("listfds")
    count = 0
    for offset in range(0, returned, 8):
        fd, fdtype = struct.unpack_from("=iI", buffer.raw, offset)
        if fdtype != lp.PROX_FDTYPE_SOCKET or fd < 0:
            continue
        try:
            row = lp._socket_row(library, pid, fd)
        except lp.NativeAnomaly:
            continue            # closed between the list and the lookup, or not TCP
        if row.get("kind") == lp.SOCKINFO_TCP and lp._established(row, local, remote):
            count += 1
    return count


def caller_is_parent_app(client: tuple[str, int], server: tuple[str, int], *, parent: int | None = None) -> bool:
    """True only when the process that launched this service holds the client
    end of this very connection. The app launches the backend (BackendManager),
    so it is the parent; a command the agent runs is a child of this service,
    never its parent, and cannot own the app's socket.

    Our own end of the connection must decode to exactly these endpoints first
    (the layout check local_peer uses), so a misread structure refuses."""
    parent = os.getppid() if parent is None else parent
    if parent <= 1:
        return False
    try:
        if _tcp_peers(os.getpid(), server, client) != 1:
            return False
        if _tcp_peers(parent, client, server) != 1:
            return False
        from service.inference.local_peer import process_path
        executable = Path(process_path(parent))
    except Exception:  # noqa: BLE001 — anything unreadable refuses
        return False
    return executable.name in {"Wisp", "WispApp"}


def require_app_caller(request: Request) -> None:
    """The app's own JSON request, from the app that launched this service.

    Like the other native-only routes, a browser can't reach it (any Origin is
    refused, and so is a non-JSON body). On top of that the caller must be the
    parent app process itself, proven from the kernel's socket table."""
    if "origin" in request.headers:
        raise HTTPException(403, "Reset Wisp from its notch with /resetwisp.")
    media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise HTTPException(415, "Content-Type must be application/json.")
    client, server = request.scope.get("client"), request.scope.get("server")
    if (not client or not server or client[0] != "127.0.0.1" or server[0] != "127.0.0.1"
            or not caller_is_parent_app((client[0], int(client[1])), (server[0], int(server[1])))):
        raise HTTPException(403, "Only the Wisp app can reset Wisp.")


# --------------------------------------------------------------------------
# The commit marker: an interrupted reset is finished by the next start
# --------------------------------------------------------------------------

def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _log(message: str) -> None:
    """A short line for the backend's log: stderr only, never personal data."""
    print(f"[reset] {message}", file=sys.stderr, flush=True)


PHASES = ("started", "first-pass", "erased")


def new_marker() -> dict:
    """The marker the route writes before anything destructive."""
    return {"version": 1, "id": secrets.token_hex(16), "phase": "started", "attempts": 0,
            "full_pass": False, "pending": [], "engine_cache": False, "problems": [], "at": int(time.time())}


def write_marker(home: Path, doc: dict) -> None:
    """Atomically write MARKER (fsynced, and its folder too)."""
    body = json.dumps({**doc, "problems": list(doc.get("problems", []))[:20]}, sort_keys=True).encode()
    temp = home / (MARKER + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        temp.unlink()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, home / MARKER)
    _fsync_dir(home)


class InvalidMarker(dict):
    """A MARKER that is not one Wisp wrote: ignored (never acted on) and reported."""


def read_marker(home: Path) -> dict | None:
    """MARKER's contents, None when there is none, or an InvalidMarker when it
    is not a regular file this user owns or not valid marker JSON. An invalid
    marker never counts as a reset."""
    path = Path(home) / MARKER
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return InvalidMarker(reason="unreadable")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return InvalidMarker(reason="not a regular file owned by this user")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            doc = json.loads(source.read(65536))
    except (OSError, ValueError):
        return InvalidMarker(reason="not valid JSON")
    checks = isinstance(doc, dict) and doc.get("version") == 1 and doc.get("phase") in PHASES
    if checks:
        doc = {**new_marker(), **doc}
        checks = (isinstance(doc["id"], str) and 0 < len(doc["id"]) <= 64 and doc["id"].isalnum()
                  and type(doc["attempts"]) is int and doc["attempts"] >= 0
                  and type(doc["full_pass"]) is bool and type(doc["engine_cache"]) is bool
                  and isinstance(doc["pending"], list) and all(isinstance(n, str) for n in doc["pending"])
                  and isinstance(doc["problems"], list) and all(isinstance(n, str) for n in doc["problems"]))
    if not checks:
        return InvalidMarker(reason="not a Wisp reset marker")
    return doc


def _engine_running(home: Path) -> bool:
    """Whether the bundled engine recorded in engine/engine.pid is still alive."""
    try:
        pid = int((home / "engine" / "engine.pid").read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _service_live(port: int = 8765) -> bool:
    """Whether a Wisp service already answers on its port (an old backend still
    stopping): the restart pass waits for the next start instead of racing it."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _google_still_connected(home: Path) -> bool:
    """gmail.json still lists a signed-in account: its tokens were never revoked."""
    try:
        fd = os.open(home / "gmail.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            doc = json.loads(source.read(1 << 20))
    except (OSError, ValueError):
        return False
    accounts = doc.get("accounts") if isinstance(doc, dict) else None
    return isinstance(accounts, dict) and any(isinstance(v, dict) and v.get("refresh_token")
                                              for v in accounts.values())


GOOGLE_NOT_REVOKED = "Google access was not revoked; remove Wisp at myaccount.google.com/permissions."


@contextlib.contextmanager
def _exclusive(home: Path):
    """One erase of a home at a time, across processes (flock on the folder)."""
    fd = os.open(home, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def resume_pending(home: Path | None = None, user_home: Path | None = None) -> dict | None:
    """The restart pass: run by the backend the app launched, before any other
    import (service/main.py), so no store is open.

    Without MARKER, or with one at "erased", this does nothing. An invalid marker
    is ignored and reported. With a live service on the port it waits for the
    next start. Otherwise, the first time it erases the whole home (whatever the
    route's pass missed or never reached, and the engine's prompt cache when the
    engine is stopped); later starts retry only the names that pass could not
    delete. It writes "erased" once nothing is left, or after MAX_ATTEMPTS
    starts with the leftovers listed as problems. Never raises: the service
    still starts. Returns the marker it wrote, or None."""
    if home is None:
        from service.paths import MOE_DIR
        home = MOE_DIR
    home, user_home = Path(home), Path.home() if user_home is None else Path(user_home)
    try:
        marker = read_marker(home)
        if marker is None or (not isinstance(marker, InvalidMarker) and marker["phase"] == "erased"):
            return None
        if isinstance(marker, InvalidMarker):
            _log(f"ignored a reset marker: {marker['reason']}")
            return None
        if _service_live():
            _log("a Wisp service is still running; the reset finishes at the next start")
            return None
        root = checked_home(home, user_home)
    except Exception as exc:  # noqa: BLE001 — the service must still start
        _log(f"could not check the reset marker: {type(exc).__name__}")
        return None
    with _exclusive(root):
        marker = read_marker(root)
        if marker is None or isinstance(marker, InvalidMarker) or marker["phase"] == "erased":
            return None
        doc = dict(marker)
        doc["attempts"] += 1
        problems = [p for p in doc["problems"] if not p.startswith("Couldn't delete: ")]
        try:
            if _google_still_connected(root) and GOOGLE_NOT_REVOKED not in problems:
                problems.append(GOOGLE_NOT_REVOKED)
            engine_stopped = not _engine_running(root)
            if doc["full_pass"]:
                only = set(doc["pending"])
                if not doc["engine_cache"]:
                    only |= {f"engine/{name}" for name in ENGINE_CACHES}
                sweep = erase_home(root, user_home, engine_caches=engine_stopped, only=only)
            else:
                sweep = erase_home(root, user_home, engine_caches=engine_stopped)
            doc["full_pass"] = True
            doc["pending"] = sweep["failed"]
            doc["engine_cache"] = doc["engine_cache"] or (
                engine_stopped and not any(n.startswith("engine/base/") for n in sweep["failed"]))
            _log(f"restart pass {doc['attempts']}: removed {len(sweep['removed'])}, "
                 f"{len(sweep['failed'])} left")
        except Exception as exc:  # noqa: BLE001 — counted; it can't loop past MAX_ATTEMPTS
            _log(f"restart pass {doc['attempts']} failed: {type(exc).__name__}")
        finished = doc["full_pass"] and not doc["pending"] and doc["engine_cache"]
        if finished or doc["attempts"] >= MAX_ATTEMPTS:
            if not doc["full_pass"]:
                problems.append("Wisp couldn't finish deleting its data.")
            if doc["pending"]:
                problems.append("Couldn't delete: " + ", ".join(doc["pending"]))
            if doc["full_pass"] and not doc["engine_cache"]:
                problems.append("The engine's prompt cache was kept because the engine was still running.")
            doc["phase"] = "erased"
        else:
            doc["phase"] = "first-pass" if doc["full_pass"] else "started"
        doc["problems"] = problems
        try:
            write_marker(root, doc)
        except OSError as exc:
            _log(f"could not record the restart pass: {type(exc).__name__}")
        return doc


# --------------------------------------------------------------------------
# While resetting: nothing else runs
# --------------------------------------------------------------------------

class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False      # a reset is in progress (one at a time)
        self.sealed = False       # a reset began: refuse everything else until restart
        self.erased = False       # the service's files are gone (set only after erase_home returns)


state = _State()
RESETTING = "Wisp is resetting."
# Distinct: the only 503 the app accepts as "the service's part is done".
ERASED = "Wisp was reset."
# What the app still needs from a sealed service: the reset itself, whether the
# service is up (BackendManager reads /mode, the launch receipt /identity), and
# stopping the engine on the way out.
_SEALED_ALLOWED = frozenset({("POST", ROUTE), ("GET", "/health"), ("GET", "/mode"), ("GET", "/identity"),
                             ("POST", "/shutdown_omlx")})


class ResetGate:
    """Pure ASGI middleware: from the moment a reset starts, every other request
    is refused, so no sync, chat or scheduler writes state back."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if state.sealed and scope.get("type") in ("http", "websocket"):
            method = scope.get("method", "WEBSOCKET")
            if (method, scope.get("path", "")) not in _SEALED_ALLOWED:
                if scope["type"] == "http":
                    detail = ERASED if state.erased else RESETTING
                    await JSONResponse({"detail": detail}, status_code=503)(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1013})
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------

router = APIRouter()
# How long background work gets to stop before the reset goes on without it.
STOP_TIMEOUT = 10.0


def _cancel_requests() -> None:
    """Cancel requests already running (a chat turn, a sync), except this one."""
    from service.config import quarantine
    me = asyncio.current_task()
    for task in list(quarantine._active_requests):
        if task is not me and not task.done():
            task.cancel()


async def _stop_background(app, problems: list[dict]) -> None:
    from service.assistant import scheduler
    tasks = [t for t in getattr(app.state, "wisp_background_tasks", ()) or () if not t.done()]
    # The scheduler's own runs: a Daily Summary (or its day-one recompose) and a behavior tick.
    tasks += [t for t in (getattr(scheduler, "_brief_task", None), getattr(scheduler, "_behavior_task", None))
              if t is not None and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        _done, alive = await asyncio.wait(tasks, timeout=STOP_TIMEOUT)
        if alive:
            problems.append({"part": "background", "detail": f"{len(alive)} background task"
                             f"{'' if len(alive) == 1 else 's'} didn't stop in time; the next start erases again."})
    bridge = getattr(app.state, "wisp_bridge_link", None)
    if bridge is not None:
        try:
            await asyncio.wait_for(asyncio.to_thread(bridge.stop), 10)
        except Exception:  # noqa: BLE001
            problems.append({"part": "browser", "detail": "The browser connection didn't stop cleanly."})
    try:
        from service.mcp import manager as mcp_manager
        await asyncio.wait_for(mcp_manager.stop(), timeout=10)
    except Exception:  # noqa: BLE001
        problems.append({"part": "mcp", "detail": "A connected tool server did not stop cleanly."})
    try:
        from service.assistant import behavior_runtime
        await asyncio.wait_for(asyncio.to_thread(behavior_runtime.stop_for_reset), 30)
    except Exception:  # noqa: BLE001 — its files are deleted below either way
        problems.append({"part": "learned", "detail": "Learning didn't stop in time; the next start erases again."})


async def _disconnect_google(problems: list[dict]) -> int:
    from service import gmail_source
    try:
        accounts = len(gmail_source.connected_accounts())
    except Exception:  # noqa: BLE001
        accounts = 0
    try:
        result = await asyncio.wait_for(gmail_source.disconnect(""), timeout=30)
    except Exception:  # noqa: BLE001 — the files below still go
        problems.append({"part": "google", "detail": "Google sign-out didn't finish. Wisp's copies of the tokens "
                                                     "are deleted with its files, but Google may still list Wisp "
                                                     "as connected (myaccount.google.com/permissions)."})
        return accounts
    revoked = (result or {}).get("revoked") or {}
    offline = sum(1 for v in revoked.values() if v == "offline")
    if offline:
        problems.append({"part": "google_revoke",
                         "detail": f"Couldn't reach Google to revoke {offline} sign-in"
                                   f"{'' if offline == 1 else 's'}. Wisp's copy is deleted; remove Wisp at "
                                   "myaccount.google.com/permissions if you want Google to forget it too."})
    return accounts


def _reply(*, erased: bool, accounts: int, problems: list[dict], removed: int = 0, kept=(),
           detail: str = "") -> dict[str, Any]:
    # committed: MARKER is on disk, so this reset finishes even if this process can't.
    return {"committed": True, "erased": erased, "ok": erased and not problems, "removed": removed,
            "kept": sorted(kept), "google_accounts": accounts, "problems": problems, "detail": detail}


@router.post(ROUTE)
async def reset_wisp(request: Request) -> dict[str, Any]:
    require_app_caller(request)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "JSON object body required") from exc
    if not isinstance(body, dict) or body.get("confirm") != CONFIRM_PHRASE:
        raise HTTPException(422, "confirm must be the reset phrase")
    from service.paths import MOE_DIR
    user_home = Path.home()
    try:
        home = checked_home(MOE_DIR, user_home)
    except ResetRefused as exc:
        raise HTTPException(409, str(exc)) from None
    with state.lock:
        if state.erased:
            raise HTTPException(503, ERASED)
        if state.running:
            raise HTTPException(409, "A reset is already running.")
        state.running = True
    # Nothing destructive before the marker is on disk; after it, the reset is committed.
    marker = new_marker()
    try:
        write_marker(home, marker)
    except OSError:
        with state.lock:
            state.running = False
        raise HTTPException(500, "Couldn't start the reset (Wisp's folder isn't writable). Nothing was reset.") from None
    state.sealed = True
    _cancel_requests()
    problems: list[dict] = []
    accounts = 0
    try:
        await _stop_background(request.app, problems)
        accounts = await _disconnect_google(problems)
        try:
            with _exclusive(home):
                sweep = await asyncio.to_thread(erase_home, home, user_home)
        except Exception as exc:  # noqa: BLE001 — reported exactly; MARKER finishes it next start
            done = "Google was signed out" if accounts else "Wisp's work was stopped"
            problems.append({"part": "files", "detail": f"{done}, but its files were not deleted "
                                                        f"({type(exc).__name__}). Quit and reopen Wisp to finish."})
            with contextlib.suppress(OSError):
                # Still "started": the restart pass erases the whole home.
                write_marker(home, {**marker, "problems": [p["detail"] for p in problems if p["part"] != "files"]})
            _log(f"erase failed: {type(exc).__name__}")
            return _reply(erased=False, accounts=accounts, problems=problems,
                          detail=problems[-1]["detail"])
        if sweep["failed"]:
            problems.append({"part": "files", "detail": "Couldn't delete: " + ", ".join(sweep["failed"])})
        with contextlib.suppress(OSError):
            # Never "erased" here: only the restart pass, with this service and the
            # engine stopped, may say that (resume_pending).
            write_marker(home, {**marker, "phase": "first-pass",
                                "problems": [p["detail"] for p in problems if p["part"] != "files"]})
        state.erased = not sweep["failed"]
    finally:
        with state.lock:
            state.running = False
    # Console record only: counts and Wisp's own file names, never personal data.
    _log(f"removed {len(sweep['removed'])} entries, kept {len(sweep['kept'])}, "
         f"google accounts {accounts}, problems {[p['part'] for p in problems]}")
    return _reply(erased=state.erased, accounts=accounts, problems=problems,
                  removed=len(sweep["removed"]), kept=sweep["kept"])
