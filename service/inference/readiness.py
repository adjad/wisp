"""Lazy inference readiness scoped to one foreground request."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

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
                 start_engine: Callable[[], Awaitable[None]], *, emit=None):
        self._client = client
        self._start_engine = start_engine
        self._emit = emit
        self._engine_ready = False
        self._prepared_model: str | None = None

    async def ensure_engine(self) -> None:
        if not self._engine_ready:
            await self._start_engine()
            self._engine_ready = True

    async def ensure_only(self, model: str, **kwargs) -> None:
        await self.ensure_engine()
        await self._client.ensure_only(model, **kwargs)
        self._prepared_model = model

    async def _ensure_generation(self, model: str) -> None:
        if self._prepared_model != model:
            await self.ensure_only(
                model, exclusive=model == role_to_model("agent"), emit=self._emit)

    async def chat(self, model: str, messages: list[dict], **kwargs) -> dict:
        await self._ensure_generation(model)
        return await self._client.chat(model, messages, **kwargs)

    async def stream_events(self, model: str, messages: list[dict], **kwargs):
        await self._ensure_generation(model)
        events = self._client.stream_events(model, messages, **kwargs)
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
