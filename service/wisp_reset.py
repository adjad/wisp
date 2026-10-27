"""/resetwisp: erase Wisp's own state for a true first run, keeping the model.

The person types /resetwisp in the notch and confirms with an explicit Reset
Wisp button. The app then calls POST /assistant/reset_wisp, which:

1. stops the service's own background work (schedulers, syncs, MCP servers);
2. disconnects every Google account (gmail_source.disconnect: local tokens and
   derived data go first, then each token is revoked with Google, best effort);
3. forgets what Wisp learned (behavior_runtime.forget_all);
4. refuses every later request (ResetGate), so nothing writes new state;
5. deletes everything in Wisp's home except KEEP_ENTRIES (the model, the
   engine and the plumbing that lets the engine start), and trims config.yaml
   to the keys that pick the engine and the model (trimmed_config).

The app then clears its own UserDefaults domain, resets its own privacy grants
and relaunches into first-run setup. macOS accounts (System Settings ▸ Internet
Accounts, Mail, Calendar, Notes, Messages) are never touched: deleting a
"connection" means removing Wisp's access and every copy Wisp holds.

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
import os
import shutil
import stat
import struct
import threading
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
KEEP_ENTRIES = frozenset({
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


def _remove(path: Path) -> None:
    """Delete one entry without following a link: a link (or file) is unlinked,
    a real folder is removed with everything in it, links inside unlinked."""
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise OSError(errno.ENOTSUP, "symlink-safe removal unavailable")
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


def erase_home(home: Path, user_home: Path) -> dict:
    """Delete every entry of Wisp's home outside KEEP_ENTRIES, trim config.yaml
    and drop the engine's log. Never follows a link out of the home.

    Returns names only (Wisp's own file names, never contents): ``removed``,
    ``kept`` and ``failed``. A SQLite database is deleted only after its
    -wal/-shm/-journal files are, so a fresh database can never pick up an old
    write-ahead log on the next launch.
    """
    root = checked_home(home, user_home)
    removed: list[str] = []
    failed: list[str] = []
    entries = {entry.name for entry in os.scandir(root)}
    # Sidecars first, then the rest; a database waits for its sidecars.
    order = sorted(entries, key=lambda n: (_sidecar_owner(n, entries) is None, n))
    blocked: set[str] = set()
    for name in order:
        if name in KEEP_ENTRIES:
            continue
        owner = _sidecar_owner(name, entries)
        if owner is None and name in blocked:
            failed.append(name)
            continue
        try:
            _remove(root / name)
            removed.append(name)
        except FileNotFoundError:
            removed.append(name)
        except OSError:
            failed.append(name)
            if owner is not None:
                blocked.add(owner)
    engine = root / "engine"
    try:
        if stat.S_ISDIR(engine.lstat().st_mode):
            for name in sorted(ENGINE_DELETE):
                try:
                    (engine / name).unlink()
                    removed.append(f"engine/{name}")
                except FileNotFoundError:
                    pass
                except OSError:
                    failed.append(f"engine/{name}")
    except FileNotFoundError:
        pass
    try:
        config = _rewrite_config(root)
        if config == "removed":
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
# While resetting: nothing else runs
# --------------------------------------------------------------------------

class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False      # a reset is in progress (one at a time)
        self.sealed = False       # files are going or gone: refuse everything else


state = _State()
# What the app still needs from a sealed service: is it up, and stop the engine on quit.
_SEALED_ALLOWED = frozenset({("GET", "/health"), ("GET", "/identity"), ("POST", "/shutdown_omlx")})


class ResetGate:
    """Pure ASGI middleware: once a reset has started deleting, every other
    request is refused, so no sync, chat or scheduler writes state back."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if state.sealed and scope.get("type") in ("http", "websocket"):
            method = scope.get("method", "WEBSOCKET")
            if (method, scope.get("path", "")) not in _SEALED_ALLOWED:
                if scope["type"] == "http":
                    await JSONResponse({"detail": "Wisp is resetting."}, status_code=503)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------

router = APIRouter()


async def _stop_background(app, problems: list[dict]) -> None:
    tasks = [t for t in getattr(app.state, "wisp_background_tasks", ()) or () if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=10)
    try:
        from service.mcp import manager as mcp_manager
        await asyncio.wait_for(mcp_manager.stop(), timeout=10)
    except Exception:  # noqa: BLE001
        problems.append({"part": "mcp", "detail": "A connected tool server did not stop cleanly."})


async def _disconnect_google(problems: list[dict]) -> int:
    from service import gmail_source
    try:
        accounts = len(gmail_source.connected_accounts())
    except Exception:  # noqa: BLE001
        accounts = 0
    try:
        result = await asyncio.wait_for(gmail_source.disconnect(""), timeout=30)
    except Exception:  # noqa: BLE001 — the files below still go
        problems.append({"part": "google", "detail": "Google sign-out didn't finish; Wisp's copies of the "
                                                     "tokens are deleted, but Google may still list Wisp as connected."})
        return accounts
    revoked = (result or {}).get("revoked") or {}
    offline = sum(1 for v in revoked.values() if v == "offline")
    if offline:
        problems.append({"part": "google_revoke",
                         "detail": f"Couldn't reach Google to revoke {offline} sign-in"
                                   f"{'' if offline == 1 else 's'}. Wisp's copy is deleted; remove Wisp at "
                                   "myaccount.google.com/permissions if you want Google to forget it too."})
    return accounts


async def _forget_learned(problems: list[dict]) -> None:
    from service.assistant import behavior_runtime
    from service.assistant.store import assistant_store
    try:
        behavior_runtime.invalidate_email_history("Wisp is being reset")
        result = await asyncio.wait_for(asyncio.to_thread(behavior_runtime.forget_all, assistant_store), 60)
        if isinstance(result, dict) and result.get("files_failed"):
            problems.append({"part": "learned", "detail": "Some reminders or holds Wisp created couldn't be removed."})
    except Exception:  # noqa: BLE001 — the learned files are deleted below either way
        problems.append({"part": "learned", "detail": "Reminders or holds Wisp created may remain in "
                                                      "Reminders or Calendar."})


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
        if state.running:
            raise HTTPException(409, "A reset is already running.")
        state.running = True
    problems: list[dict] = []
    await _stop_background(request.app, problems)
    accounts = await _disconnect_google(problems)
    await _forget_learned(problems)
    state.sealed = True
    try:
        sweep = await asyncio.to_thread(erase_home, home, user_home)
    except ResetRefused as exc:
        raise HTTPException(409, str(exc)) from None
    if sweep["failed"]:
        problems.append({"part": "files", "detail": "Couldn't delete: " + ", ".join(sweep["failed"])})
    # Console record only: counts and Wisp's own file names, never personal data.
    print(f"[reset] removed {len(sweep['removed'])} entries, kept {len(sweep['kept'])}, "
          f"google accounts {accounts}, problems {[p['part'] for p in problems]}", flush=True)
    return {"ok": not problems, "removed": len(sweep["removed"]), "kept": sweep["kept"],
            "google_accounts": accounts, "problems": problems}
