"""Lazy inference readiness scoped to one foreground request."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from service.config import role_to_model
from service.config.endpoints import role_target, Target
from service.inference import engine_epoch
from service.inference.omlx_client import ModelLoadError, OMLXClient

_CIRCUITS: dict[tuple, float] = {}

# Every engine call pays a full attestation, so a turn must not repeat a
# readiness read whose answer it already holds. A read is reused only while
# ALL of these hold (see _Residency); anything else runs the full check:
#   - engine_epoch is unchanged and nothing is in flight: no load, unload,
#     generation, embedding, rerank or connection reset by anyone in this
#     process has started, finished or is running since it was read;
#   - it is at most RESIDENCY_LEASE_SECONDS old, counted from the last read or
#     the last successful generation on that model;
#   - the last status read itself is at most RESIDENCY_MAX_UNVERIFIED_SECONDS
#     old, so a chain of quick steps cannot extend trust indefinitely (this
#     bounds changes made outside this process, which no epoch can see);
#   - the same model is requested and nothing needs evicting or reloading.
RESIDENCY_LEASE_SECONDS = 10.0
RESIDENCY_MAX_UNVERIFIED_SECONDS = 60.0
# Same budget the old health wait gave a stopped engine before it was started.
ENGINE_PROBE_SECONDS = 3.0


def _now() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class _Residency:
    """What one successful status read (or generation after it) established."""
    loaded: frozenset[str]
    epoch: int
    verified_at: float      # when the status was actually read
    renewed_at: float       # last read or successful generation on the model


class TurnInferenceClient:
    """Start inference only at a model operation, not at deterministic dispatch.

    Agent steps keep their per-step residency check, including after a tool may
    have used another model, but a step reuses the previous answer instead of
    asking the engine again when nothing could have changed it (see
    RESIDENCY_LEASE_SECONDS). A rolling-memory fold also gets a ready model
    when it is the first inference operation in an otherwise direct turn.
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
        self._residency: _Residency | None = None
        self._fallback_start = fallback_start
        self._fallback_client = None
        self._fallback_model = None

    async def ensure_engine(self) -> None:
        if self._engine_ready:
            return
        # An answering engine is proven by one status read, which also records
        # what is resident. Any failure falls back to the full start path.
        if await self._probe():
            return
        await self._start_engine()
        self._engine_ready = True

    async def _probe(self) -> bool:
        """One bounded status read that doubles as the engine-is-up check.

        Never raises and records nothing on failure (engine down, starting,
        slow, malformed answer, or a client without a status API): the caller
        then runs the unchanged health/start path.
        """
        client = self._client
        reader = getattr(client, "loaded_models", None)
        if reader is None or not getattr(client, "managed", True):
            return False
        before = engine_epoch.mark()
        try:
            async with asyncio.timeout(ENGINE_PROBE_SECONDS):
                loaded = await reader()
        except Exception:  # noqa: BLE001 - fail toward the full check
            return False
        if not isinstance(loaded, list) or any(type(m) is not str for m in loaded):
            return False
        self._engine_ready = True
        # Stamped with the epoch from BEFORE the read: if anything touched the
        # engine meanwhile, the record is already stale and is never reused.
        now = _now()
        self._residency = _Residency(frozenset(loaded), before, now, now)
        return True

    def _warm(self) -> set[str] | None:
        try:
            return {m for m in self._client.keep_warm()}
        except Exception:  # noqa: BLE001 - unknown keep-warm set: do not trust a proof
            return None

    def _fresh(self, proof: _Residency | None) -> bool:
        if proof is None or proof.epoch != engine_epoch.current() or not engine_epoch.quiet():
            return False
        now = _now()
        return (now - proof.renewed_at <= RESIDENCY_LEASE_SECONDS
                and now - proof.verified_at <= RESIDENCY_MAX_UNVERIFIED_SECONDS)

    def _covers(self, model: str, exclusive: bool) -> bool:
        """True only when a recent status read already satisfies ensure_only.

        Mirrors the no-op case of OMLXClient._ensure_managed exactly: the model
        is resident, nothing outside the keep set is, and no keep-warm model
        needs reloading. Anything else needs the real check.
        """
        proof = self._residency
        if not self._fresh(proof):
            return False
        warm = self._warm()
        if warm is None:
            return False
        keep = {model} if exclusive else warm | {model}
        if model not in proof.loaded or not proof.loaded <= keep:
            return False
        return exclusive or not (warm - proof.loaded - {model})

    def _remember(self, model: str, exclusive: bool, epoch_before: int) -> None:
        """Keep a proof after a check that changed nothing and pinned the set."""
        warm = self._warm()
        if warm is None or not (exclusive or warm <= {model}):
            self._residency = None
            return
        # Stamped with the epoch from before the check: a load/unload it made
        # (or anyone else's) leaves the record stale, so it is never reused.
        now = _now()
        self._residency = _Residency(frozenset({model}), epoch_before, now, now)

    async def ensure_only(self, model: str, **kwargs) -> None:
        if getattr(self._client, "managed", True):
            exclusive = bool(kwargs.get("exclusive", False))
            if not self._covers(model, exclusive):
                await self.ensure_engine()
                # The engine probe's own read may already answer the question.
                if not self._covers(model, exclusive):
                    self._residency = None
                    before = engine_epoch.mark()
                    await self._client.ensure_only(model, **kwargs)
                    self._remember(model, exclusive, before)
        else:
            async with asyncio.timeout(self._client.target.endpoint.readiness_timeout):
                await self.ensure_engine()
                await self._client.ensure_only(model, **kwargs)
        self._prepared_model = model

    def _generation_started(self) -> tuple[_Residency | None, int]:
        """Take a still-valid proof out for the duration of a generation.

        It is put back only if the generation completes; an error, an early
        close or a cancellation therefore leaves the next step to re-check.
        """
        proof, self._residency = self._residency, None
        return (proof if self._fresh(proof) else None), engine_epoch.mark()

    def _generation_finished(self, proof: _Residency | None, before: int,
                             model: str, active) -> None:
        """A completed generation on the proven model renews its proof.

        The client counts exactly two epoch ticks per generation (start and
        end), so the renewed record expects before + 2; any other tick (a
        foreign load, embedding or tool generation, even a concurrent one)
        leaves it stale and unused.
        """
        if (proof is None or before < 0 or active is not self._client
                or model not in proof.loaded):
            return
        self._residency = _Residency(proof.loaded, before + 2, proof.verified_at, _now())

    async def _ensure_generation(self, model: str, *, tools=None) -> None:
        if self._fallback_client is not None or self._prepared_model == model:
            return
        target = getattr(self._client, "target", None)
        # Frozen Target includes role, endpoint/credential reference, requested
        # model metadata, revision, context, capabilities and dimensions. Never
        # let a failure for one model/profile poison another on the same host.
        circuit = (target, model) if isinstance(target, Target) else (self._client, model)
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
        proof, before = self._generation_started()
        result = await active.chat(self._fallback_model or model, messages, **kwargs)
        self._generation_finished(proof, before, model, active)
        return result

    async def stream_events(self, model: str, messages: list[dict], **kwargs):
        await self._ensure_generation(model, tools=kwargs.get("tools"))
        active = self._fallback_client or self._client
        proof, before = self._generation_started()
        events = active.stream_events(self._fallback_model or model, messages, **kwargs)
        completed = False
        try:
            async for event in events:
                yield event
            completed = True
        finally:
            # Closing an outer async generator does not automatically close an
            # inner one suspended at yield. Release its HTTP stream and idle
            # tracking immediately on cancellation or early agent termination.
            await events.aclose()
            if completed:
                self._generation_finished(proof, before, model, active)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
