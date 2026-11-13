"""A small, authenticated model downloader for Wisp's private engine.

The browser dashboard needs the engine's private key. Keep that key and its
admin cookie here instead: the native setup guide can request only published
catalogue models, never an arbitrary URL, repository, token or destination.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import mode, runtime
from .manager import BUNDLED_PORT
from service.setup import catalog

router = APIRouter()
_ORIGIN = f"http://127.0.0.1:{BUNDLED_PORT}"
_MAX_BODY = 256 * 1024
_ACTIVE = {"pending", "downloading"}
_STATES = _ACTIVE | {"completed", "failed", "cancelled"}
_REQUEST_DEADLINE = 165.0  # includes queueing; native caller allows 180 seconds
_operation_lock = asyncio.Lock()
_START_FAILED = ("Wisp's built-in engine didn't start. Try again in a moment. "
                 "Details are in ~/.moe/engine/engine.log.")


class DownloadError(RuntimeError):
    pass


def _catalogue() -> dict[str, str]:
    return {m.id: m.repo for m in (*catalog.PRIMARY_MODELS, catalog.EMBEDDING_MODEL) if m.repo}


def _require_bundled() -> None:
    from service.inference.remote_policy import require_local_models_disabled
    require_local_models_disabled("Built-in model setup")
    if not mode.is_bundled():
        raise HTTPException(409, "Use your installed engine's downloader in external mode.")


def _request_origin(request: Request) -> None:
    # Host is controlled by the requester. Never use it to bless an Origin
    # (a rebound DNS name could otherwise bless itself).
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "Open model setup in Wisp.")
    origin = request.headers.get("origin")
    if origin is not None and origin != "http://127.0.0.1:8765":
        raise HTTPException(403, "Open model setup in Wisp.")


async def _body(request: Request, *, model: bool) -> str | None:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Send application/json")
    _request_origin(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(413, "Setup request is too large")
        body.extend(chunk)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Expected a setup JSON object") from None
    if not isinstance(value, dict) or set(value) != ({"model"} if model else set()):
        raise HTTPException(422, "Choose one of Wisp's downloadable models")
    if model:
        selected = value["model"]
        if not isinstance(selected, str) or selected not in _catalogue():
            raise HTTPException(422, "Choose one of Wisp's downloadable models")
        return selected
    return None


async def _ensure_engine() -> None:
    # Reuse the existing launch/readiness/identity path. Never start an
    # installed oMLX: the mode check precedes this call at every entry point.
    from service.inference.inference_errors import ModelLoadError
    from service.main import ensure_omlx
    try:
        async with asyncio.timeout(150):
            await ensure_omlx()
    except ModelLoadError as exc:
        # Two causes a retry cannot clear get a fixed sentence of their own. The raw text is
        # only matched, never shown: upstream errors can carry paths and tokens.
        text = str(exc)
        if "already in use" in text:
            raise DownloadError(f"Another program is using the built-in engine's port ({BUNDLED_PORT}). "
                                "Quit it, then try again.") from None
        if "not installed" in text:
            raise DownloadError("Wisp's built-in engine is missing from this copy.") from None
        raise DownloadError(_START_FAILED) from None
    except Exception:
        raise DownloadError(_START_FAILED) from None


@asynccontextmanager
async def _client():
    from service.config.quarantine import guard_client
    from service.inference.attributed_transport import bundled_transport
    engine = runtime._manager
    if engine is None or not engine.running() or not engine.verified:
        raise DownloadError("Start the built-in engine, then try again.")
    transport = bundled_transport(_ORIGIN, engine.api_key)
    if transport is None:
        raise DownloadError("The built-in engine could not be verified.")
    # Every request, including the login body and cookie, crosses the existing
    # process-attributed transport. No proxy, redirect, public key API or URL.
    async with guard_client(httpx.AsyncClient(
        base_url=_ORIGIN, transport=transport, trust_env=False,
        follow_redirects=False, timeout=15,
    )) as client:
        yield client, engine.api_key


async def _json(client, method: str, path: str, payload: dict | None = None) -> dict:
    async with client.stream(method, path, json=payload) as response:
        if not response.is_success:
            raise DownloadError("The engine couldn't complete that download request. Try again.")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > _MAX_BODY:
                raise DownloadError("The engine returned an invalid download status.")
            data.extend(chunk)
        result = json.loads(data)
        if not isinstance(result, dict):
            raise DownloadError("The engine returned an invalid download status.")
        return result


async def _call(method: str, path: str, payload: dict | None = None) -> dict:
    try:
        async with asyncio.timeout(25):
            async with _client() as (client, key):
                login = await _json(client, "POST", "/admin/api/login", {"api_key": key, "remember": False})
                if login.get("success") is not True or not client.cookies.get("omlx_admin_session"):
                    raise DownloadError("Wisp couldn't sign in to its built-in engine. Try starting it again.")
                return await _json(client, method, path, payload)
    except DownloadError:
        raise
    except Exception:
        # Upstream errors can contain local paths, tokens and repository text.
        raise DownloadError("Wisp couldn't reach its built-in downloader. Try again.") from None


def _number(value, ceiling: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return min(ceiling, max(0, value))
        except OverflowError:
            pass
    return 0


_FAILED = "Download failed. Check your connection and free disk space."
# The engine's error text is matched, never forwarded: it can carry paths, tokens and
# repository text. Each kind maps to a fixed sentence naming only catalogue values.
_OFFLINE_MARKERS = ("connecterror", "connectionerror", "connection error", "connection refused",
                    "connection reset", "connection aborted", "failed to resolve", "name resolution",
                    "nodename nor servname", "max retries exceeded", "network is unreachable",
                    "timed out", "timeout", "temporary failure", "offline")


def _failure(error, model_id: str) -> tuple[str, str]:
    """(reason, message) for a failed catalogue download, from the engine's error text."""
    text = error.lower() if isinstance(error, str) else ""
    model = catalog.by_id(model_id)
    name = model.label if model else "This model"
    page = f"huggingface.co/{model.repo}" if model and model.repo else "Hugging Face"
    if "is gated" in text or "gated repo" in text or ("401 client error" in text and "restricted" in text):
        return "gated", f"{page} asks for a Hugging Face sign-in, which Wisp's downloader doesn't have."
    if "repository not found" in text or "404 client error" in text:
        return "not_found", (f"{name} isn't available on Hugging Face right now ({page}). "
                             "It may not be published yet.")
    if "no space left" in text or "errno 28" in text or "enospc" in text:
        size = f" ({model.size_gb:.1f} GB)" if model and model.size_gb else ""
        return "no_space", f"Not enough free disk space for {name}{size}. Free some space, then try again."
    if any(marker in text for marker in _OFFLINE_MARKERS):
        return "offline", "Wisp couldn't reach huggingface.co. Check your internet connection."
    return "other", _FAILED


# A finished download under this share of the catalogue size fetched a repo that doesn't
# hold the model yet (a README-only repo "completes" in a second, leaving no weights).
_INCOMPLETE_SHARE = 0.5
# Reasons Wisp gives a task the engine itself reports as completed.
_ENGINE_COMPLETED = {"incomplete", "mismatch"}


def _incomplete(model_id: str, downloaded: int, total: int) -> bool:
    model = catalog.by_id(model_id)
    return bool(model and model.size_gb) and max(downloaded, total) < _INCOMPLETE_SHARE * model.size_gb * 1e9


def _tasks(payload: dict) -> list[tuple[float, str, dict]]:
    rows = payload.get("tasks")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise DownloadError("The engine returned an invalid download status.")
    catalogue = {repo: model for model, repo in _catalogue().items()}
    latest = {}
    # Only bounded catalogue fields reach Wisp. In particular, never forward
    # raw error messages, cookies, tokens, paths or unrelated downloads.
    for row in rows:
        if not isinstance(row, dict):
            continue
        repo, task_id, state = row.get("repo_id"), row.get("task_id"), row.get("status")
        if (not isinstance(repo, str) or repo not in catalogue
                or not isinstance(task_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id) is None
                or not isinstance(state, str) or state not in _STATES):
            continue
        task = {
            "model": catalogue[repo], "state": state,
            "progress": _number(row.get("progress"), 100),
            "downloaded_bytes": int(_number(row.get("downloaded_size"), 10**13)),
            "total_bytes": int(_number(row.get("total_size"), 10**13)),
            "bytes_per_second": _number(row.get("speed_bps"), 10**12),
        }
        # No button name: each surface adds its own (Retry, Retry download, Try again).
        task["reason"], task["message"] = (_failure(row.get("error"), catalogue[repo])
                                           if state == "failed" else (None, None))
        if state == "completed" and _incomplete(task["model"], task["downloaded_bytes"], task["total_bytes"]):
            known = catalog.by_id(task["model"])
            task.update(state="failed", reason="incomplete", message=(
                f"{known.label}'s files aren't all on Hugging Face yet (huggingface.co/{known.repo}). "
                "Try again once they are."))
        created = _number(row.get("created_at"), 10**12)
        old = latest.get(repo)
        if old is None or created >= old[0]:
            latest[repo] = (created, task_id, task)
    return list(latest.values())


# A completed catalogue download is checked on disk before Wisp calls it finished:
# every promised shard present, and each pinned file's sha256 as tested.
_MESSAGES_AFTER_COMPLETION = {
    "incomplete": "{label}'s download is missing files (huggingface.co/{repo}). Try again.",
    "mismatch": ("The files downloaded for {label} aren't the ones Wisp was tested with "
                 "(huggingface.co/{repo}), so Wisp won't use them."),
}


def _named(names: list[str]) -> str:
    """Up to three local file names, shortened and stripped to plain characters."""
    shown = [re.sub(r"[^A-Za-z0-9._ -]", "?", n)[:40] for n in names[:3]]
    return ", ".join(shown) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def models_dir():
    """Where the engine's downloader writes: oMLX's HFDownloader gets model_dir=dir_list[0],
    the first of the engine's model_dirs, which manager.engine_settings makes Wisp's own
    layout.models_dir (tests/test_engine_downloads.py pins this against oMLX's source)."""
    layout = getattr(runtime._manager, "layout", None)
    return layout.models_dir if layout is not None else mode.engine_data_dir() / "models"


def _model_folder(model: "catalog.CatalogModel"):
    return models_dir() / model.repo


def check_failed(model_id: str, *, cached_only: bool = False) -> str | None:
    """Built-in engine: why a pinned catalogue model's downloaded folder can't be used, or
    None (unpinned, not Wisp's download, or it passes). A folder placed elsewhere by hand
    is the person's own and stays trusted."""
    from service.setup.local_models import verify_download
    known = catalog.by_id(model_id)
    if not mode.is_bundled() or known is None or not known.pinned_files or not known.repo:
        return None
    folder = _model_folder(known)
    return verify_download(folder, known, cached_only=cached_only) if folder.is_dir() else None


def warm_checks() -> None:
    """Startup, in the background: hash pinned downloads once so setup's cached checks know."""
    for model in catalog.PRIMARY_MODELS:
        try:
            check_failed(model.id)
        except Exception:  # noqa: BLE001 — a check that can't run now runs at the next poll
            pass


def _check_finished(tasks) -> list:
    from service.setup.local_models import verify_download
    for _, _, task in tasks:
        known = catalog.by_id(task["model"])
        if task["state"] != "completed" or known is None or not known.repo:
            continue
        folder = _model_folder(known)
        if not folder.is_dir():
            if known.pinned_files:   # finished, yet not where the engine downloads: never usable
                task.update(state="failed", reason="incomplete",
                            message=_MESSAGES_AFTER_COMPLETION["incomplete"].format(label=known.label, repo=known.repo))
            continue      # unpinned: setup's "hasn't appeared" route covers it
        reason = verify_download(folder, known)
        if reason:
            message = _MESSAGES_AFTER_COMPLETION[reason].format(label=known.label, repo=known.repo)
            from service.setup.local_models import extra_files
            extras = extra_files(folder, known) if reason == "mismatch" else []
            # Retry unlinks plain files only (never a recursive delete), so a folder or link
            # the tested model didn't have is the user's to remove; saying so avoids a loop.
            by_hand = [n for n in extras if (folder / n).is_symlink() or not (folder / n).is_file()]
            files = [n for n in extras if n not in by_hand]
            if files:
                message += f" It has files the tested model didn't: {_named(files)}. Try again to remove them."
            if by_hand:
                one = len(by_hand) == 1
                where = str(folder).replace(str(Path.home()), "~", 1)
                message += (f" It has {'a folder' if one else 'folders'} the tested model didn't: "
                            f"{_named(by_hand)}. Remove {'it' if one else 'them'} in Finder "
                            f"(in {where}), then try again.")
            task.update(state="failed", reason=reason, message=message)
    return tasks


def _contained_folder(model: "catalog.CatalogModel"):
    """The catalogue folder, only when nothing between the engine's models folder and it is
    a symlink (so a deletion can't reach another model's files); else None."""
    root = models_dir()
    folder = _model_folder(model)
    try:
        if not folder.is_dir():
            return None
        rel = folder.relative_to(root)
        step = root
        for part in rel.parts:                      # Neo-Arid, then Gemma-Wisp
            step = step / part
            if step.is_symlink():
                return None
        if not folder.resolve().is_relative_to(root.resolve() / rel):
            return None
    except (OSError, ValueError):
        return None
    return folder


def _deletion_plan(model_id: str) -> list:
    """What a user's Retry removes before downloading again, worked out (and hashed) without
    touching anything: each pinned file that fails its check, and each top-level file the
    tested model didn't have, plus hub's metadata for them. Regular files only, never a
    symlink, never outside the contained catalogue folder or its hub metadata folder."""
    from service.setup.local_models import bad_pinned_files, extra_files
    known = catalog.by_id(model_id)
    if known is None or not known.pinned_files or not known.repo:
        return []
    folder = _contained_folder(known)
    if folder is None:
        return []
    meta = folder / ".cache" / "huggingface" / "download"
    meta_ok = all(not p.is_symlink() for p in (folder / ".cache", folder / ".cache" / "huggingface", meta))
    names = [*bad_pinned_files(folder, known), *extra_files(folder, known)]
    plan = []
    for name in names:
        path = folder / name
        if path.is_file() and not path.is_symlink() and path.parent == folder:
            plan.append(path)
        record = meta / f"{name}.metadata"
        if meta_ok and record.is_file() and not record.is_symlink():
            plan.append(record)
    return plan


def _delete(plan: list) -> list:
    """Quick: unlink what the plan named (no hashing here)."""
    gone = []
    for path in plan:
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
                gone.append(path.name)
        except OSError:
            pass
    return gone


async def _inventory() -> list:
    tasks = _tasks(await _call("GET", "/admin/api/hf/tasks"))
    return await asyncio.to_thread(_check_finished, tasks)


def _display(tasks) -> dict:
    return {"tasks": [task for _, _, task in tasks]}


@router.post("/start")
async def start_engine(request: Request):
    _require_bundled()
    await _body(request, model=False)
    try:
        await _ensure_engine()
        return {"running": True}
    except DownloadError as exc:
        raise HTTPException(503, str(exc)) from None


# Models whose profile reached the running engine after their download finished.
# Only a push the engine accepted lands here; a failed one is tried again.
_profiles_pushed: set[str] = set()
_pushing: set[str] = set()
_push_failed_at: dict[str, float] = {}
# The setup UI polls every few seconds; a failed push waits this long between tries.
_PUSH_RETRY_S = 30.0
# The backend's own watch over a profiled model's download, so its profile does
# not depend on a setup window being open when the download finishes.
_WATCH_EVERY_S = 10.0
_WATCH_FOR_S = 6 * 3600.0
_watch: asyncio.Task | None = None


def _awaiting_profile(tasks) -> bool:
    """A catalog download whose model has a profile the engine hasn't taken yet:
    still downloading, or finished and not pushed."""
    for _, _, task in tasks:
        model = catalog.by_id(task["model"])
        if model is None or not model.engine_profile or model.id in _profiles_pushed:
            continue
        if task["state"] in _ACTIVE or task["state"] == "completed":
            return True
    return False


async def _push_profiles(tasks, *, now=time.monotonic) -> None:
    """Hand a just-downloaded model its catalog profile without an engine restart.

    The engine read model_settings.json when it started, before this model
    existed. A model counts as pushed only once the engine accepted the PUT;
    until then it is tried again every _PUSH_RETRY_S, and the file still holds
    the profile (seeded in prepare()) for the engine's next start.
    """
    from . import profiles
    layout = getattr(runtime._manager, "layout", None)
    if layout is None:
        return
    path = layout.base_path / "model_settings.json"
    record = layout.data_dir / "seeded_profiles.json"
    for _, _, task in tasks:
        model = catalog.by_id(task["model"])
        if task["state"] != "completed" or model is None or not model.engine_profile \
                or model.id in _profiles_pushed or model.id in _pushing:
            continue
        failed_at = _push_failed_at.get(model.id)
        if failed_at is not None and now() - failed_at < _PUSH_RETRY_S:
            continue
        _pushing.add(model.id)
        try:
            profiles.seed(path, record, [model], force=[model.id])
            entry = profiles.entry_for(path, model.id) or dict(model.engine_profile)
            try:
                schema = await _call("GET", "/openapi.json")
                allowed = set(schema["components"]["schemas"]["ModelSettingsRequest"]["properties"])
            except (DownloadError, KeyError, TypeError):
                allowed = None
            await _call("PUT", f"/admin/api/models/{model.id}/settings", profiles.push_body(entry, allowed))
            # The PUT rewrites the entry from its schema; put back keys it dropped.
            profiles.seed(path, record, [model], force=[model.id])
            _profiles_pushed.add(model.id)
            _push_failed_at.pop(model.id, None)
        except (DownloadError, OSError):
            _push_failed_at[model.id] = now()
        finally:
            _pushing.discard(model.id)


async def _watch_loop() -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WATCH_FOR_S
    while loop.time() < deadline:
        await asyncio.sleep(_WATCH_EVERY_S)
        engine = runtime._manager
        if engine is None or not engine.running() or not engine.verified:
            return  # A stopped engine reads the seeded file when it starts again.
        try:
            tasks = await _inventory()
        except DownloadError:
            continue
        await _push_profiles(tasks)
        if not _awaiting_profile(tasks):
            return


def _watch_downloads(tasks) -> None:
    """Watch a profiled model's download from the backend until its profile is
    in the engine. Before, only the setup UI's poll pushed it, so closing the
    window mid-download could leave the model on the engine's defaults."""
    global _watch
    if not _awaiting_profile(tasks) or (_watch is not None and not _watch.done()):
        return
    _watch = asyncio.get_running_loop().create_task(_watch_loop())


@router.get("/downloads")
async def download_status(request: Request):
    _require_bundled()
    _request_origin(request)
    engine = runtime._manager
    if engine is None or not engine.running() or not engine.verified:
        return {"tasks": []}
    try:
        tasks = await _inventory()
        await _push_profiles(tasks)
        _watch_downloads(tasks)
        return _display(tasks)
    except DownloadError as exc:
        raise HTTPException(503, str(exc)) from None


@router.post("/downloads")
async def download_model(request: Request):
    _require_bundled()
    model = await _body(request, model=True)
    try:
        async with asyncio.timeout(_REQUEST_DEADLINE):
            async with _operation_lock:
                await _ensure_engine()
                tasks = await _inventory()
                previous = next(((task_id, task) for _, task_id, task in tasks if task["model"] == model), None)
                if previous is not None and previous[1]["state"] in _ACTIVE:
                    return _display(tasks)  # double click/reopened setup never queues a duplicate
                if previous is not None and previous[1].get("reason") in _ENGINE_COMPLETED:
                    # The engine finished this task, so it refuses a retry (400): drop the row
                    # and download again, which fetches what is missing or changed. Hub skips a
                    # local file whose saved metadata matches the remote, without hashing it, so
                    # a pinned file damaged on disk is removed first or it would survive.
                    # Hash first (slow, changes nothing); a timeout here leaves the disk as it was.
                    plan = await asyncio.to_thread(_deletion_plan, model)
                    try:
                        await _call("DELETE", f"/admin/api/hf/task/{previous[0]}")
                    except DownloadError:
                        # Gone already, or still active ("still active" is a 404 too): look again,
                        # and never queue a second download beside an active one.
                        tasks = await _inventory()
                        if any(t["model"] == model and t["state"] in _ACTIVE for _, _, t in tasks):
                            return _display(tasks)
                    await asyncio.to_thread(_delete, plan)     # quick: unlinks only
                    await _call("POST", "/admin/api/hf/download", {"repo_id": _catalogue()[model]})
                elif previous is not None and previous[1]["state"] in {"failed", "cancelled"}:
                    await _call("POST", f"/admin/api/hf/retry/{previous[0]}", {})
                else:
                    await _call("POST", "/admin/api/hf/download", {"repo_id": _catalogue()[model]})
                tasks = await _inventory()
                _watch_downloads(tasks)
                return _display(tasks)
    except TimeoutError:
        raise HTTPException(504, "The engine took too long. Check again to see whether the download started.") from None
    except DownloadError as exc:
        raise HTTPException(503, str(exc)) from None


@router.post("/downloads/cancel")
async def cancel_download(request: Request):
    _require_bundled()
    model = await _body(request, model=True)
    try:
        async with asyncio.timeout(_REQUEST_DEADLINE):
            async with _operation_lock:
                # Cancellation/status must not resurrect a stopped engine.
                tasks = await _inventory()
                for _, task_id, task in tasks:
                    if task["model"] == model and task["state"] in _ACTIVE:
                        await _call("POST", f"/admin/api/hf/cancel/{task_id}", {})
                        break
                return _display(await _inventory())
    except TimeoutError:
        raise HTTPException(504, "The engine took too long. Check again to see the download's current state.") from None
    except DownloadError as exc:
        raise HTTPException(503, str(exc)) from None
