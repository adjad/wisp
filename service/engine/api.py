"""Engine selection and a credential-free, display-only coexistence probe."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import re
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request

from . import mode, runtime
from .manager import BUNDLED_PORT

log = logging.getLogger(__name__)
_selection_lock = threading.Lock()
_default_persisted = False
_MAX_PROBE_BODY = 64 * 1024


@contextmanager
def _selection_transaction():
    # An outer selection lock makes the absent-mode recheck and overlay write
    # one operation, even across two backend processes sharing WISP_HOME. The
    # existing overlay writer still owns all config locking/merge/replacement.
    with _selection_lock:
        mode.MOE_DIR.mkdir(parents=True, exist_ok=True)
        with (mode.MOE_DIR / ".engine-mode.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def _write_mode(value: str) -> None:
    # Identical guard to /config -> set_roles; RecoveryMiddleware also wraps
    # this router when it is included by service.main.
    from service.inference.remote_policy import require_local_models_disabled
    from service.config import _save_overlay
    require_local_models_disabled("Inference settings changes")
    _save_overlay({"engine": {"mode": value}})


def _save_selected_mode(value: str) -> None:
    # Serialize explicit choices with the first-launch recheck/write so a
    # concurrent first GET cannot overwrite a just-saved user selection.
    with _selection_transaction():
        _write_mode(value)


def persist_default() -> None:
    """Store the import-time first choice once, outside mode's import path.

    Recheck the overlay so a settings request made since startup wins. The
    existing writer preserves all unrelated settings and refuses corrupt files.
    """
    global _default_persisted
    if _default_persisted or not mode.selection_unset():
        return
    try:
        with _selection_transaction():
            if _default_persisted or not mode.selection_unset():
                return
            _write_mode(mode.MODE)
            _default_persisted = True
    except (OSError, ValueError, yaml.YAMLError):
        log.warning("Wisp could not save its first-launch engine selection")


@asynccontextmanager
async def _lifespan(app):
    await asyncio.to_thread(persist_default)
    yield


router = APIRouter(prefix="/engine", tags=["engine"], lifespan=_lifespan)
from .downloads import router as downloads_router
router.include_router(downloads_router)


@dataclass(frozen=True)
class ExternalEngine:
    running: bool = False
    # Whether it holds models, from its own /health pool counters; None when it
    # doesn't report them (an older oMLX, or only /v1/models answered). A running
    # process or a model list is never taken as memory in use.
    loaded: bool | None = None
    model_bytes: int | None = None


def _pool_residency(payload: dict) -> tuple[bool | None, int | None]:
    pool = payload.get("engine_pool")
    if not isinstance(pool, dict):
        return None, None
    count, size = pool.get("loaded_count"), pool.get("current_model_memory")
    if type(count) is not int or count < 0:
        return None, None
    measured = size if type(size) is int and size > 0 else None
    return count > 0 or measured is not None, measured


async def external_engine_probe(*, port: int = 8000, timeout: float = 0.3,
                                deadline: float = 0.8,
                                transport: httpx.AsyncBaseTransport | None = None) -> ExternalEngine:
    """Fixed loopback, no secrets, no redirects or proxy environment.

    Port injection is for tests only; it is never taken from an HTTP request or
    settings. A health JSON object or a valid model list proves availability,
    not identity, and never authorizes inference traffic. Read only: it never
    loads or unloads anything on the other engine.
    """
    try:
        async with asyncio.timeout(deadline):
            async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                         follow_redirects=False, transport=transport) as client:
                for path in ("/health", "/v1/models"):
                    try:
                        async with client.stream("GET", f"http://127.0.0.1:{port}{path}") as response:
                            if response.status_code != 200:
                                continue
                            body = b""
                            async for chunk in response.aiter_bytes():
                                body += chunk
                                if len(body) > _MAX_PROBE_BODY:
                                    break
                            else:
                                payload = json.loads(body)
                                if isinstance(payload, dict):
                                    if path == "/health":
                                        if payload.get("status") in ("ok", "healthy", "running"):
                                            loaded, size = _pool_residency(payload)
                                            return ExternalEngine(True, loaded, size)
                                    elif isinstance(payload.get("data"), list):
                                        return ExternalEngine(True)
                    except (httpx.HTTPError, OSError, ValueError, RecursionError):
                        continue
    except TimeoutError:
        pass
    return ExternalEngine()


async def external_engine_running(**kwargs: Any) -> bool:
    return (await external_engine_probe(**kwargs)).running


def _omlx_version() -> str | None:
    try:
        value = json.loads((mode.engine_code_root() / "ENGINE.json").read_text()).get("omlx_version")
    except (OSError, ValueError, AttributeError, RecursionError):
        return None
    # Provenance is the only source; never read settings or echo arbitrary text.
    if isinstance(value, str) and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){1,3}[A-Za-z0-9.+-]{0,40}", value):
        return value
    return None


async def _status() -> dict[str, Any]:
    reason = mode.unsupported_reason()
    available = mode.engine_present() and reason is None
    configured = mode.configured_mode()
    external = await external_engine_probe()
    external_running = external.running
    bundled = mode.is_bundled()
    if bundled and external.loaded:
        log.warning("An external engine also has a model loaded; two engines can consume extra memory")
    # Observe the existing child without constructing a manager (which resolves
    # an API key) or starting a process. External status never reads ~/.omlx.
    child = runtime._manager
    running = bool(child is not None and child.running()) if bundled else external_running
    installed = mode.external_omlx_installed()
    return {
        "mode": mode.MODE,
        "configured_mode": configured,
        "bundled_available": available,
        "unsupported_reason": reason,
        "running": running,
        "omlx_version": _omlx_version() if bundled and mode.engine_present() else None,
        "admin_url": (f"http://127.0.0.1:{BUNDLED_PORT}/admin" if bundled and available
                      else "http://127.0.0.1:8000/admin" if not bundled and (installed or running)
                      else None),
        "external_omlx_installed": installed,
        "restart_required": configured is not None and configured != mode.MODE,
        "external_engine_running": bundled and external_running,
        # Only about the other engine while Wisp runs on its own; null when unknown.
        "external_engine_loaded": external.loaded if bundled and external_running else None,
        "external_engine_model_bytes": external.model_bytes if bundled and external.loaded else None,
        # The Ask model's audio/vision encoders on Wisp's own engine (loaded on
        # demand); null on the user's oMLX, an older engine, or no answer.
        "encoders": await _encoders() if bundled and running else None,
    }


async def _encoders() -> dict[str, Any] | None:
    from service import encoders
    try:
        return await encoders.status()
    except Exception:  # noqa: BLE001 — display only
        return None


@router.get("")
async def get_engine() -> dict[str, Any]:
    await asyncio.to_thread(persist_default)
    return await _status()


@router.post("/mode")
async def set_engine_mode(request: Request) -> dict[str, Any]:
    # Manual validation avoids FastAPI's raw-input 422 errors echoing a secret
    # submitted in an extra field, invalid value, or malformed JSON document.
    # A cross-site page can send a "simple" POST to a loopback port without a preflight;
    # insisting on application/json forces one, which this backend never answers.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Send application/json")
    try:
        body = await request.json()
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(status_code=422, detail="Expected an engine mode JSON object") from None
    if (not isinstance(body, dict) or set(body) != {"mode"}
            or not isinstance(body["mode"], str) or body["mode"] not in (mode.BUNDLED, mode.EXTERNAL)):
        raise HTTPException(status_code=422, detail="mode must be bundled or external-omlx; no other fields are accepted")
    value = body["mode"]
    if value == mode.BUNDLED and (not mode.engine_present() or mode.unsupported_reason() is not None):
        raise HTTPException(status_code=409, detail="The built-in engine is unavailable on this Mac")
    try:
        await asyncio.to_thread(_save_selected_mode, value)
    except (OSError, ValueError, yaml.YAMLError):
        raise HTTPException(status_code=409, detail="Wisp could not save the engine selection") from None
    return await _status()
