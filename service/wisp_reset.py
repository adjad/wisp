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
backend the app launches (service/main.py, only with WISP_LAUNCH_NONCE) calls
resume_or_exit before any other import. Each restart pass is recorded in MARKER
(attempts + 1, in_progress) BEFORE it deletes anything, so no pass runs that
the marker does not count, and at most MAX_ATTEMPTS ever run. With no live
service and the engine stopped, the first restart pass erases the whole home
once more (anything written after the route's pass) together with the engine's
prompt cache. An entry it cannot delete is renamed in place to TRASH_PREFIX
<id>-<name> (a rename in the same folder works even when the contents can't be
deleted), and later starts retry only those trash entries, so they can never
delete what a fresh service writes under the original name; an entry that
can't even be renamed is reported at once and never retried. The pass writes
"erased" once nothing is left, or after MAX_ATTEMPTS starts with the leftovers
listed as problems. The service refuses to start (SystemExit) whenever a pass
could not be recorded or the whole-home pass has not completed: it never
creates data that a later pass would erase. The relaunched app runs its own
part once per marker (Keychain provider keys, its defaults domain, its privacy
grants, notifications, its caches; APP_DONE records it), never once MARKER says
"erased", and removes MARKER once it reads "erased". macOS accounts (System Settings ▸ Internet Accounts,
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
# Restart passes one reset may run before it is declared done with leftovers.
MAX_ATTEMPTS = 3
# An entry the restart pass could not delete is renamed to TRASH_PREFIX<marker id>-<name>
# in its own folder; only these are ever retried.
TRASH_PREFIX = ".reset-trash-"
# The backend's exit status when it refuses to start under an unrecorded or unfinished reset.
REFUSED_EXIT = 75
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


def _trash_name(trash_id: str, name: str) -> str:
    return f"{TRASH_PREFIX}{trash_id}-{name}"


def display_name(path: str) -> str:
    """A trash path as the entry it was ("chats", "engine/base/cache")."""
    folder, _, name = path.rpartition("/")
    while name.startswith(TRASH_PREFIX) and "-" in name[len(TRASH_PREFIX):]:
        name = name[len(TRASH_PREFIX):].split("-", 1)[1]
    return f"{folder}/{name}" if folder else name


def _real_folder(path: Path, device: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_dev == device


def _delete_or_trash(folder: Path, name: str, device: int, trash_id: str | None, *,
                     skip_delete: bool = False) -> tuple[str, str]:
    """Delete ``folder/name``; when that fails and ``trash_id`` is given, rename
    it to its trash name in the same folder. Returns (outcome, name) with outcome
    "removed", "trashed" (name is then the trash name), or "failed"."""
    target = folder / name
    if not skip_delete:
        try:
            _remove(target, device)
            return "removed", name
        except FileNotFoundError:
            return "removed", name
        except OSError:
            pass
    if trash_id is None:
        return "failed", name
    if name.startswith(_trash_name(trash_id, "")):
        return "trashed", name          # already this reset's trash (an interrupted pass)
    trash = _trash_name(trash_id, name)
    try:
        if os.path.lexists(folder / trash):
            return "failed", name
        os.rename(target, folder / trash)   # never follows a link: renames the entry itself
        return "trashed", trash
    except FileNotFoundError:
        return "removed", name
    except OSError:
        return "failed", name


def _erase_names(folder: Path, names: list[str], device: int, trash_id: str | None,
                 prefix: str, out: dict) -> None:
    """Delete these entries of one folder, SQLite sidecars before their database.
    A database whose sidecar stays is never deleted before it: with ``trash_id``
    it is moved to the trash beside that sidecar, so the pair keeps its names;
    when a sidecar can be neither deleted nor moved, its database stays where it
    is too (reported, never retried), so a fresh database never sits beside an
    old write-ahead log."""
    entries = set(names)
    order = sorted(entries, key=lambda n: (_sidecar_owner(n, entries) is None, n))
    blocked: set[str] = set()
    pinned: set[str] = set()
    for name in order:
        owner = _sidecar_owner(name, entries)
        if owner is None and name in pinned:
            out["failed"].append(prefix + name)
            out["stuck"].append(prefix + name)
            continue
        outcome, kept_as = _delete_or_trash(folder, name, device, trash_id,
                                            skip_delete=owner is None and name in blocked)
        if outcome == "removed":
            out["removed"].append(prefix + name)
            continue
        if owner is not None:
            blocked.add(owner)
            if outcome != "trashed":
                pinned.add(owner)
        out["failed"].append(prefix + name)
        if outcome == "trashed":
            out["trashed"].append(prefix + kept_as)
        else:
            out["stuck"].append(prefix + name)


def _erase_engine(root: Path, device: int, doomed: list[str], trash_id: str | None, out: dict) -> None:
    """Delete these paths inside engine/ (``engine.log``, ``base/cache``...)."""
    engine = root / "engine"
    if not _real_folder(engine, device):
        return
    for rel in doomed:
        folder_rel, _, name = rel.rpartition("/")
        folder = engine / folder_rel if folder_rel else engine
        # Never through a link: base/ itself must be a real folder of this disk.
        if not _real_folder(folder, device) or not os.path.lexists(folder / name):
            continue
        _erase_names(folder, [name], device, trash_id, "engine/" + (folder_rel + "/" if folder_rel else ""), out)


def _engine_trash(root: Path, device: int, trash_id: str) -> list[str]:
    """This reset's trash entries in engine/ and engine/base/ (as paths inside engine/)."""
    found: list[str] = []
    for rel in ("", "base"):
        folder = root / "engine" / rel if rel else root / "engine"
        if not _real_folder(root / "engine", device) or not _real_folder(folder, device):
            continue
        with contextlib.suppress(OSError):
            found += [f"{rel}/{name}" if rel else name for name in sorted(os.listdir(folder))
                      if trash_path_ok(f"engine/{rel}/{name}" if rel else f"engine/{name}", trash_id)]
    return found


def erase_engine_caches(home: Path, user_home: Path, trash_id: str) -> dict:
    """Only the engine's prompt cache and logs (the engine was running at the
    first restart pass and has stopped since): nothing else in the home."""
    root = checked_home(home, user_home)
    out: dict[str, list[str]] = {"removed": [], "failed": [], "trashed": [], "stuck": []}
    _erase_engine(root, root.lstat().st_dev, list(ENGINE_CACHES), trash_id, out)
    return out


def erase_home(home: Path, user_home: Path, *, engine_caches: bool = False,
               trash_id: str | None = None) -> dict:
    """Delete every entry of Wisp's home outside KEEP_ENTRIES, trim config.yaml
    and drop the engine's log (and, with ``engine_caches``, its prompt cache and
    logs: only while the engine is stopped). Never follows a link out of the
    home and never enters another file system mounted inside it.

    Returns names only (Wisp's own file names, never contents): ``removed``,
    ``kept`` and ``failed`` (everything not deleted). With ``trash_id`` (the
    restart pass), each entry that could not be deleted is renamed to its trash
    name in its own folder: ``trashed`` lists those paths (the only ones a later
    pass retries) and ``stuck`` the entries that could not even be moved. A
    SQLite database is deleted only after its -wal/-shm/-journal files are, so a
    fresh database can never pick up an old write-ahead log on the next launch.
    """
    root = checked_home(home, user_home)
    device = root.lstat().st_dev
    out: dict[str, list[str]] = {"removed": [], "failed": [], "trashed": [], "stuck": []}
    names = [entry.name for entry in os.scandir(root) if entry.name not in KEEP_ENTRIES]
    _erase_names(root, names, device, trash_id, "", out)
    doomed = sorted(ENGINE_DELETE) + (list(ENGINE_CACHES) if engine_caches else [])
    if trash_id is not None:
        # Trash an earlier, interrupted pass of this reset left inside the engine.
        doomed += _engine_trash(root, device, trash_id)
    _erase_engine(root, device, doomed, trash_id, out)
    try:
        if _rewrite_config(root) == "removed":
            out["removed"].append("config.yaml")
    except Exception:  # noqa: BLE001 — reported, never fatal to the rest of the reset
        out["failed"].append("config.yaml")
        out["stuck"].append("config.yaml")     # never retried: a later trim would drop new settings
    kept = sorted(name for name in os.listdir(root) if name in KEEP_ENTRIES)
    return {"removed": sorted(out["removed"]), "failed": sorted(set(out["failed"])), "kept": kept,
            "trashed": sorted(out["trashed"]), "stuck": sorted(set(out["stuck"]))}


CONFIG_NOT_TRIMMED = "Couldn't trim config.yaml."


def cant_delete_lines(names) -> list[str]:
    """The problem lines for entries left behind (config.yaml is trimmed, not deleted)."""
    names = sorted(set(names))
    lines = [CONFIG_NOT_TRIMMED] if "config.yaml" in names else []
    rest = [n for n in names if n != "config.yaml"]
    return (["Couldn't delete: " + ", ".join(rest)] if rest else []) + lines


def trash_path_ok(path: str, trash_id: str) -> bool:
    """Whether ``path`` is one of this reset's trash entries: top level, in
    engine/ or in engine/base/, and nothing else."""
    if not isinstance(path, str):
        return False
    folder, _, name = path.rpartition("/")
    return (folder in ("", "engine", "engine/base") and name.startswith(_trash_name(trash_id, ""))
            and len(name) > len(_trash_name(trash_id, "")) and "/" not in name and name not in (".", ".."))


def retry_trash(home: Path, user_home: Path, paths: list[str], trash_id: str) -> dict:
    """Delete this reset's trash entries again (and nothing else): what a fresh
    service writes under the original names is never touched. Returns
    ``removed`` and ``pending`` (trash paths still there)."""
    root = checked_home(home, user_home)
    device = root.lstat().st_dev
    out: dict[str, list[str]] = {"removed": [], "failed": [], "trashed": [], "stuck": []}
    by_folder: dict[str, list[str]] = {}
    for path in paths:
        if trash_path_ok(path, trash_id):
            folder, _, name = path.rpartition("/")
            by_folder.setdefault(folder, []).append(name)
    for folder_rel, names in sorted(by_folder.items()):
        folder = root
        ok = True
        for part in folder_rel.split("/") if folder_rel else []:
            folder = folder / part
            ok = ok and _real_folder(folder, device)
        if not ok:
            out["failed"] += [f"{folder_rel}/{n}" for n in names]
            continue
        _erase_names(folder, names, device, None, f"{folder_rel}/" if folder_rel else "", out)
    return {"removed": sorted(out["removed"]), "pending": sorted(set(out["failed"]))}


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
            "in_progress": False, "full_pass": False, "pending": [], "engine_cache": False, "problems": [],
            "at": int(time.time())}


def write_marker(home: Path, doc: dict) -> None:
    """Atomically write MARKER (fsynced, and its folder too)."""
    body = json.dumps({**doc, "problems": list(doc.get("problems", []))[:20]}, sort_keys=True).encode()
    temp = home / (MARKER + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        temp.unlink()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, home / MARKER)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
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
            doc = parse_marker_json(source.read(65536))
    except (OSError, ValueError):
        return InvalidMarker(reason="not valid JSON")
    return marker_from_json(doc)


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def parse_marker_json(data: bytes) -> Any:
    """Strict JSON, as the app's JSONSerialization reads it: NaN and Infinity are refused."""
    return json.loads(data, parse_constant=_no_constant)


def marker_from_json(doc: Any) -> dict:
    """A parsed marker, or InvalidMarker. The app applies exactly these rules
    (ResetWisp.swift, WispReset.Marker.parse; tests/fixtures/reset_markers.json
    holds the cases both sides check), so the two never disagree on a marker.

    version is the integer 1, phase one of PHASES, id 1-64 ASCII letters and
    digits; when present, attempts is a non-negative integer, full_pass,
    engine_cache and in_progress are booleans, pending lists this reset's trash
    paths and problems lists strings."""
    checks = (isinstance(doc, dict) and type(doc.get("version")) is int and doc["version"] == 1
              and isinstance(doc.get("phase"), str) and doc["phase"] in PHASES and "id" in doc)
    if checks:
        doc = {**new_marker(), **doc}
        checks = (isinstance(doc["id"], str) and 0 < len(doc["id"]) <= 64
                  and doc["id"].isascii() and doc["id"].isalnum()
                  and type(doc["attempts"]) is int and doc["attempts"] >= 0
                  and type(doc["full_pass"]) is bool and type(doc["engine_cache"]) is bool
                  and type(doc["in_progress"]) is bool
                  and isinstance(doc["pending"], list) and all(trash_path_ok(n, doc["id"]) for n in doc["pending"])
                  and isinstance(doc["problems"], list) and all(isinstance(n, str) for n in doc["problems"]))
    if not checks:
        return InvalidMarker(reason="not a Wisp reset marker")
    return doc


def _command_of(pid: int) -> str | None:
    """A process's command line, or None when it can't be read."""
    import subprocess
    try:
        done = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode in (0, 1) else None


def _engine_running(home: Path) -> bool:
    """Whether the bundled engine recorded in engine/engine.pid is still alive.

    A live pid counts only while it is still that engine (its command ends with
    this home's ``--base-path``, as engine.manager.reap_orphan checks), so a
    recycled pid doesn't keep the prompt cache forever. A command that can't be
    read counts as the engine: the cache is kept rather than deleted under it."""
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
        pass
    command = _command_of(pid)
    if command is None:
        return True
    if not command:
        return False                 # gone between the two checks
    bases = {str(Path(home) / "engine" / "base")}
    with contextlib.suppress(OSError):
        bases.add(str((Path(home) / "engine" / "base").resolve()))
    return any(command.endswith(f"--base-path {base}") for base in bases)


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


class ResetIncomplete(Exception):
    """The restart pass could not run or could not be recorded: the service must
    not start, because anything it wrote could be erased by a later pass."""


def resume_pending(home: Path | None = None, user_home: Path | None = None) -> dict | None:
    """The restart pass: run by the backend the app launched, before any other
    import (service/main.py via resume_or_exit), so no store is open.

    Without MARKER, or with one at "erased", this does nothing. An invalid marker
    is ignored and reported. Otherwise the pass is first recorded (attempts + 1,
    in_progress) and only then run: the first one erases the whole home (what
    the route's pass missed or never reached, and the engine's prompt cache when
    the engine is stopped), moving what it can't delete to the trash; later ones
    delete only that trash. It writes "erased" once nothing is left, or after
    MAX_ATTEMPTS passes with the leftovers listed as problems.

    Raises ResetIncomplete when the pass can't run here (an older service still
    answers on the port, the marker is outside Wisp's home) or can't be
    recorded (before the pass: nothing is deleted). Returns the marker written."""
    if home is None:
        from service.paths import MOE_DIR
        home = MOE_DIR
    home, user_home = Path(home), Path.home() if user_home is None else Path(user_home)
    marker = read_marker(home)
    if marker is None or (not isinstance(marker, InvalidMarker) and marker["phase"] == "erased"):
        return None
    if isinstance(marker, InvalidMarker):
        _log(f"ignored a reset marker: {marker['reason']}")
        return None
    if _service_live():
        raise ResetIncomplete("an earlier Wisp service still answers on port 8765")
    try:
        root = checked_home(home, user_home)
    except ResetRefused as exc:
        raise ResetIncomplete(f"the reset marker is in a folder a reset never erases ({exc})") from None
    with _exclusive(root):
        marker = read_marker(root)
        if marker is None or isinstance(marker, InvalidMarker) or marker["phase"] == "erased":
            return None
        doc = dict(marker)
        problems = list(doc["problems"])

        def note(line: str) -> None:
            if line not in problems:
                problems.append(line)

        if doc["attempts"] < MAX_ATTEMPTS:
            # Recorded before anything is deleted: a pass the marker doesn't count never runs.
            doc["attempts"] += 1
            doc["in_progress"] = True
            try:
                write_marker(root, doc)
            except OSError as exc:
                raise ResetIncomplete(f"Wisp couldn't record the restart pass ({type(exc).__name__}), "
                                      "so it deleted nothing") from None
            try:
                if _google_still_connected(root):
                    note(GOOGLE_NOT_REVOKED)
                engine_stopped = not _engine_running(root)
                if doc["full_pass"]:
                    sweep = retry_trash(root, user_home, doc["pending"], doc["id"])
                    doc["pending"] = sweep["pending"]
                    if not doc["engine_cache"] and engine_stopped:
                        # The engine was running at the first pass; its cache is only a cache.
                        cache = erase_engine_caches(root, user_home, doc["id"])
                        doc["pending"] = sorted(set(doc["pending"]) | set(cache["trashed"]))
                        for line in cant_delete_lines(cache["stuck"]):
                            note(line)
                        doc["engine_cache"] = True
                else:
                    sweep = erase_home(root, user_home, engine_caches=engine_stopped, trash_id=doc["id"])
                    doc["full_pass"] = True
                    doc["pending"] = sweep["trashed"]
                    doc["engine_cache"] = engine_stopped
                    for line in cant_delete_lines(sweep["stuck"]):
                        note(line)
                _log(f"restart pass {doc['attempts']}: removed {len(sweep['removed'])}, "
                     f"{len(doc['pending'])} left")
            except Exception as exc:  # noqa: BLE001 — counted; it can't run past MAX_ATTEMPTS
                _log(f"restart pass {doc['attempts']} failed: {type(exc).__name__}")
            doc["in_progress"] = False
        finished = doc["full_pass"] and not doc["pending"] and doc["engine_cache"]
        if finished or doc["attempts"] >= MAX_ATTEMPTS:
            if not doc["full_pass"]:
                note("Wisp couldn't finish deleting its data.")
            for line in cant_delete_lines(display_name(n) for n in doc["pending"]):
                note(line)
            if doc["full_pass"] and not doc["engine_cache"]:
                note("The engine's prompt cache was kept because the engine was still running.")
            doc["phase"] = "erased"
            doc["in_progress"] = False
        else:
            doc["phase"] = "first-pass" if doc["full_pass"] else "started"
        doc["problems"] = problems
        try:
            write_marker(root, doc)
        except OSError as exc:
            raise ResetIncomplete(f"Wisp couldn't record the restart pass ({type(exc).__name__})") from None
        return doc


def unfinished_reason(home: Path | None = None) -> str | None:
    """Why the service must not start under the marker on disk, or None. It may
    start once the reset is "erased", or once the whole-home pass is recorded as
    complete (a later start only retries the trash, never anything new)."""
    if home is None:
        from service.paths import MOE_DIR
        home = MOE_DIR
    marker = read_marker(Path(home))
    if marker is None or isinstance(marker, InvalidMarker) or marker["phase"] == "erased":
        return None
    if marker["in_progress"]:
        return "the last restart pass was not recorded as finished"
    if not marker["full_pass"]:
        return "Wisp's data is not fully deleted yet"
    return None


def resume_or_exit(home: Path | None = None, user_home: Path | None = None) -> None:
    """service/main.py's first call in the backend the app launched: run the
    restart pass, then refuse to start (SystemExit, reason on stderr) while the
    reset is unrecorded or unfinished, so the service never writes data a later
    pass would erase. Each refusal costs at most one recorded pass."""
    try:
        resume_pending(home, user_home)
        reason = unfinished_reason(home)
    except ResetIncomplete as exc:
        reason = str(exc)
    except Exception as exc:  # noqa: BLE001 — unknown state: never start on it
        reason = f"the restart pass failed ({type(exc).__name__})"
    if reason:
        _log(f"Wisp's service is not starting: a reset is unfinished ({reason}). Quit and reopen Wisp "
             "to try again; if this keeps happening, delete ~/.moe/.reset-pending.")
        raise SystemExit(REFUSED_EXIT)


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
            problems.append({"part": "files", "detail": " ".join(cant_delete_lines(sweep["failed"]))})
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
