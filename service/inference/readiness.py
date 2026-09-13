"""Lazy inference readiness scoped to one foreground request."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import httpx

from service.config.endpoints import role_target
from service.inference.omlx_client import ModelLoadError

_CIRCUITS: dict[str, float] = {}

from service.config import role_to_model
from service.inference.omlx_client import OMLXClient


class TurnInferenceClient:
    """Start inference only at a model operation, not at deterministic dispatch.

    Agent steps keep their existing per-step residency checks, including after
    a tool may have used another model. A rolling-memory fold also gets a ready
    model when it is the first inference operation in an otherwise direct turn.
    The underlying client is shared; readiness state belongs only to this turn.
    """

    def __init__(self, client: OMLXClient,
                 start_engine: Callable[[], Awaitable[None]], *, emit=None,
                 fallback_start=None):
        self._client = client
        self._start_engine = start_engine
        self._emit = emit
        self._engine_ready = False
        self._prepared_model: str | None = None
        self._fallback_start = fallback_start
        self._fallback_client = None
        self._fallback_model = None

    async def ensure_engine(self) -> None:
        if not self._engine_ready:
            await self._start_engine()
            self._engine_ready = True

    async def ensure_only(self, model: str, **kwargs) -> None:
        if getattr(self._client, "managed", True):
            await self.ensure_engine()
            await self._client.ensure_only(model, **kwargs)
        else:
            async with asyncio.timeout(self._client.target.endpoint.readiness_timeout):
                await self.ensure_engine()
                await self._client.ensure_only(model, **kwargs)
        self._prepared_model = model

    async def _ensure_generation(self, model: str, *, tools=None) -> None:
        if self._fallback_client is not None or self._prepared_model == model:
            return
        circuit = getattr(self._client, "base_url", "")
        try:
            if self._fallback_start and not tools and _CIRCUITS.get(circuit, 0) > time.monotonic():
                raise ModelLoadError("Remote readiness circuit is temporarily open")
            await self.ensure_only(model, exclusive=model == role_to_model("agent"), emit=self._emit)
            _CIRCUITS.pop(circuit, None)
        except (ModelLoadError, httpx.HTTPError, TimeoutError):
            # Only readiness, before generation starts, and only tool-free work.
            # Never retry an HTTP generation or an effectful agent workflow.
            if self._fallback_start is None or tools:
                raise
            _CIRCUITS[circuit] = time.monotonic() + 30
            target = role_target("fast")
            if not target.endpoint.managed:
                raise ModelLoadError("Reflex fallback must be local")
            self._fallback_client = OMLXClient(target=target)
            self._fallback_model = target.model
            if self._emit:
                await self._emit({"type": "status", "text": "Remote inference unavailable; using local Reflex.",
                                  "endpoint": "local", "model": target.model, "fallback": True})
            await self._fallback_start()
            await self._fallback_client.ensure_only(target.model)

    async def close_fallback(self):
        if self._fallback_client is not None:
            await self._fallback_client.aclose()

    async def chat(self, model: str, messages: list[dict], **kwargs) -> dict:
        await self._ensure_generation(model, tools=kwargs.get("tools"))
        active = self._fallback_client or self._client
        return await active.chat(self._fallback_model or model, messages, **kwargs)

    async def stream_events(self, model: str, messages: list[dict], **kwargs):
        await self._ensure_generation(model, tools=kwargs.get("tools"))
        active = self._fallback_client or self._client
        events = active.stream_events(self._fallback_model or model, messages, **kwargs)
        try:
            async for event in events:
                yield event
        finally:
            # Closing an outer async generator does not automatically close an
            # inner one suspended at yield. Release its HTTP stream and idle
            # tracking immediately on cancellation or early agent termination.
            await events.aclose()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
