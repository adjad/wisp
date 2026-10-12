"""FastAPI app wrapping one BehaviorEngine.

All engine calls (requests and the background miner/nudger) run on ONE
dedicated worker thread. That serializes access to the engine's SQLite
connection and keeps it on the thread that created it, whatever threading
model the engine uses internally.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import hmac
import logging
import math
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from service.behavior.types import (FEEDBACK_VERDICTS, OBSERVATION_KINDS, Feedback, Observation)

log = logging.getLogger("brain_server")

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS = 20000
PATTERN_STATUSES = ("active", "confirmed", "rejected")
MAX_HORIZON_S = 14 * 86400.0


# --------------------------------------------------------------------------
# Request body limit (pure ASGI: rejects before the app sees the body).
# --------------------------------------------------------------------------
class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    return await _send_json(send, 400, {"detail": "invalid content-length"})
                if declared > self.max_bytes:
                    return await _send_json(send, 413, {"detail": "request body too large"})
        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                return await _send_json(send, 413, {"detail": "request body too large"})
            chunks.append(body)
            more = message.get("more_body", False)
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replay_receive():
            if replay:
                return replay.pop()
            return await receive()

        return await self.app(scope, replay_receive, send)


async def _send_json(send, status: int, payload: dict) -> None:
    import json
    raw = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(raw)).encode())]})
    await send({"type": "http.response.body", "body": raw})


# --------------------------------------------------------------------------
# Engine host: one worker thread owns the engine.
# --------------------------------------------------------------------------
class EngineHost:
    def __init__(self, factory: Callable[[], Any], call_timeout: float = 30.0) -> None:
        self.call_timeout = call_timeout
        self._executor = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="brain-engine")
        self.engine: Any = None
        self.error: str | None = None
        try:
            self.engine = self._executor.submit(factory).result(timeout=120)
        except NotImplementedError:
            self.error = "behavior engine not implemented in this build"
        except Exception as exc:  # noqa: BLE001
            log.exception("behavior engine failed to start")
            self.error = f"behavior engine failed to start: {type(exc).__name__}"

    @property
    def ready(self) -> bool:
        return self.engine is not None

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.engine is None:
            raise HTTPException(503, self.error or "behavior engine unavailable")
        fn = getattr(self.engine, method)
        future = self._executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=self.call_timeout)
        except cf.TimeoutError:
            raise HTTPException(503, "behavior engine busy") from None

    def shutdown(self) -> None:
        engine = self.engine
        if engine is not None and callable(getattr(engine, "close", None)):
            try:
                self._executor.submit(engine.close).result(timeout=10)
            except Exception:  # noqa: BLE001
                log.warning("engine close failed", exc_info=True)
        self._executor.shutdown(wait=False, cancel_futures=True)


class BackgroundLoop:
    """Mines every ``mine_interval_s`` and generates nudges every ``nudge_interval_s``."""

    def __init__(self, host: EngineHost, mine_interval_s: float, nudge_interval_s: float) -> None:
        self.host = host
        self.mine_interval_s = mine_interval_s
        self.nudge_interval_s = nudge_interval_s
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_mined_at: float | None = None
        self.last_nudged_at: float | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="brain-background", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _step(self, method: str) -> bool:
        try:
            self.host.call(method)
            return True
        except HTTPException as exc:
            self.last_error = f"{method}: {exc.detail}"
        except Exception as exc:  # noqa: BLE001
            log.exception("background %s failed", method)
            self.last_error = f"{method}: {type(exc).__name__}"
        return False

    def _run(self) -> None:
        next_mine = time.monotonic() + 2.0
        next_nudge = time.monotonic() + min(self.nudge_interval_s, 10.0)
        while not self.stop_event.wait(1.0):
            if not self.host.ready:
                continue
            now = time.monotonic()
            if now >= next_mine:
                if self._step("mine"):
                    self.last_mined_at = time.time()
                next_mine = time.monotonic() + self.mine_interval_s
            if now >= next_nudge:
                if self._step("generate_nudges"):
                    self.last_nudged_at = time.time()
                next_nudge = time.monotonic() + self.nudge_interval_s


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _finite(value: Any, name: str, *, required: bool = False) -> float | None:
    if value is None:
        if required:
            raise HTTPException(422, f"{name} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(422, f"{name} must be a finite number")
    return float(value)


def _observations(rows: Any) -> list[Observation]:
    if not isinstance(rows, list):
        raise HTTPException(422, "observations must be a list")
    if len(rows) > MAX_OBSERVATIONS:
        raise HTTPException(413, "too many observations in one request")
    out: list[Observation] = []
    for row in rows:
        if not isinstance(row, dict):
            raise HTTPException(422, "each observation must be an object")
        try:
            obs = Observation.from_dict(row)
        except TypeError:
            raise HTTPException(422, "observation is missing required fields") from None
        if not isinstance(obs.id, str) or not obs.id or obs.kind not in OBSERVATION_KINDS:
            raise HTTPException(422, "observation has an invalid id or kind")
        _finite(obs.ts, "ts", required=True)
        _finite(obs.end_ts, "end_ts")
        if not isinstance(obs.attrs, dict):
            raise HTTPException(422, "observation attrs must be an object")
        out.append(obs)
    return out


def _now(payload: dict | None) -> float | None:
    return _finite((payload or {}).get("now"), "now")


def create_app(db_path: str | Path, token: str, phraser: Any = None, *,
               engine_factory: Callable[[], Any] | None = None, tz: str | None = None,
               background: bool = True, mine_interval_s: float = 900.0,
               nudge_interval_s: float = 60.0, max_body_bytes: int = MAX_BODY_BYTES,
               call_timeout: float = 30.0) -> FastAPI:
    """Build the brain server app.

    ``engine_factory`` (tests) returns an object with BehaviorEngine's public
    methods; by default a real ``BehaviorEngine(db_path, phraser=..., tz=...)``
    is built on the engine worker thread.
    """
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("brain server token must be a string of at least 16 characters")
    token_digest = hashlib.sha256(token.encode("utf-8")).digest()

    if engine_factory is None:
        db = Path(db_path).expanduser()

        def engine_factory() -> Any:
            from service.behavior.engine import BehaviorEngine
            db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            return BehaviorEngine(db, phraser=phraser, tz=tz)

    host = EngineHost(engine_factory, call_timeout=call_timeout)
    loop = BackgroundLoop(host, mine_interval_s, nudge_interval_s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background:
            loop.start()
        try:
            yield
        finally:
            loop.stop()
            host.shutdown()

    app = FastAPI(title="Wisp brain", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.host = host
    app.state.loop = loop
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_body_bytes)

    def require_token(request: Request) -> None:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        presented = hashlib.sha256(value.strip().encode("utf-8")).digest()
        if not (scheme.lower() == "bearer" and hmac.compare_digest(presented, token_digest)):
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def run(method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return host.call(method, *args, **kwargs)
        except HTTPException:
            raise
        except KeyError as exc:
            raise HTTPException(404, f"not found: {exc.args[0] if exc.args else ''}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)[:300] or "invalid request") from None
        except NotImplementedError:
            raise HTTPException(503, "behavior engine not implemented in this build") from None
        except Exception:  # noqa: BLE001
            log.exception("engine %s failed", method)
            raise HTTPException(500, "internal error") from None

    auth = [Depends(require_token)]

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        if not host.ready:
            return JSONResponse({"ok": False, "engine": "unavailable"}, status_code=503)
        return JSONResponse({"ok": True, "engine": "ready"})

    @app.post("/v1/observe", dependencies=auth)
    def observe(payload: dict[str, Any] = Body(...)) -> dict:
        obs = _observations(payload.get("observations"))
        return {"count": int(run("observe", obs))}

    @app.post("/v1/snapshot", dependencies=auth)
    def snapshot(payload: dict[str, Any] = Body(...)) -> dict:
        source = payload.get("source")
        if not isinstance(source, str) or not source:
            raise HTTPException(422, "source is required")
        obs = _observations(payload.get("observations"))
        result = run("observe_snapshot", source, obs,
                     window_start=_finite(payload.get("window_start"), "window_start", required=True),
                     window_end=_finite(payload.get("window_end"), "window_end", required=True),
                     taken_at=_finite(payload.get("taken_at"), "taken_at", required=True))
        return dict(result)

    @app.post("/v1/mine", dependencies=auth)
    def mine(payload: dict[str, Any] | None = Body(None)) -> dict:
        patterns = run("mine", _now(payload))
        loop.last_mined_at = time.time()
        return {"patterns": [p.to_dict() for p in patterns]}

    @app.get("/v1/patterns", dependencies=auth)
    def patterns(status: str | None = None, kind: str | None = None) -> dict:
        return {"patterns": [p.to_dict() for p in run("patterns", status=status, kind=kind)]}

    @app.post("/v1/patterns/{pattern_id}/status", dependencies=auth)
    def set_status(pattern_id: str, payload: dict[str, Any] = Body(...)) -> dict:
        status = payload.get("status")
        if status not in PATTERN_STATUSES:
            raise HTTPException(400, f"status must be one of {', '.join(PATTERN_STATUSES)}")
        return {"pattern": run("set_pattern_status", pattern_id, status).to_dict()}

    @app.get("/v1/predict", dependencies=auth)
    def predict(now: float | None = None,
                horizon_s: float | None = Query(None, gt=0, le=MAX_HORIZON_S)) -> dict:
        # Omitted horizon -> the engine default (>= 24h, through the end of tomorrow).
        _finite(now, "now")
        return {"predictions": [p.to_dict() for p in run("predict", now, horizon_s)]}

    @app.post("/v1/nudges/generate", dependencies=auth)
    def generate(payload: dict[str, Any] | None = Body(None)) -> dict:
        return {"nudges": [n.to_dict() for n in run("generate_nudges", _now(payload))]}

    @app.get("/v1/nudges", dependencies=auth)
    def nudges(cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)) -> dict:
        items, new_cursor = run("nudges_since", cursor, limit)
        return {"nudges": [n.to_dict() for n in items], "cursor": int(new_cursor)}

    @app.post("/v1/feedback", dependencies=auth)
    def feedback(payload: dict[str, Any] = Body(...)) -> dict:
        nudge_id, verdict = payload.get("nudge_id"), payload.get("verdict")
        if not isinstance(nudge_id, str) or not nudge_id or verdict not in FEEDBACK_VERDICTS:
            raise HTTPException(422, "feedback needs nudge_id and a valid verdict")
        ts = _finite(payload.get("ts"), "ts") or time.time()
        run("feedback", Feedback(nudge_id=nudge_id, verdict=verdict, ts=ts))
        return {"ok": True}

    @app.get("/v1/profile", dependencies=auth)
    def profile(now: float | None = None, max_bytes: int = Query(700, ge=1, le=16384)) -> dict:
        _finite(now, "now")
        return {"text": str(run("profile_card", now, max_bytes))}

    @app.get("/v1/describe", dependencies=auth)
    def describe(subject: str | None = Query(None, max_length=200), now: float | None = None) -> dict:
        _finite(now, "now")
        return dict(run("describe", subject, now))

    @app.get("/v1/status", dependencies=auth)
    def status() -> dict:
        data = dict(run("status"))
        data["brain_server"] = {
            "background": background, "last_mined_at": loop.last_mined_at,
            "last_nudged_at": loop.last_nudged_at, "last_error": loop.last_error,
            "phraser": type(phraser).__name__ if phraser is not None else None,
        }
        return data

    return app
