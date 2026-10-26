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
    from service.main import ensure_omlx
    try:
        async with asyncio.timeout(150):
            await ensure_omlx()
    except Exception:
        raise DownloadError("Wisp's built-in engine didn't start. Try again in a moment.") from None


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
            "message": ("Download failed. Check your connection and free disk space, then choose Retry."
                        if state == "failed" else None),
        }
        created = _number(row.get("created_at"), 10**12)
        old = latest.get(repo)
        if old is None or created >= old[0]:
            latest[repo] = (created, task_id, task)
    return list(latest.values())


async def _inventory() -> list:
    return _tasks(await _call("GET", "/admin/api/hf/tasks"))


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
                if previous is not None and previous[1]["state"] in {"failed", "cancelled"}:
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
