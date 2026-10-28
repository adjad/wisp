"""Behavior-engine phraser over Wisp's EXISTING inference path (``fast`` role).

The engine composes every nudge/profile sentence itself; this adapter may only
reword one (``phrase``) or label ambiguous event titles from a fixed list
(``categorize``). It never raises and never waits long:

- skipped entirely while the user is waiting on a turn (``idle.foreground_busy``)
  or while any generation is in flight, so it cannot contend with foreground
  work for the single resident model;
- one bounded request (``TIMEOUT_S``) through ``OMLXClient`` bound to
  ``role_target("fast")``. Under remote inference that target IS the pinned
  remote profile and ``OMLXClient._payload`` validates the body
  against ``RemoteOnlyProfile.validate_payload`` (model/messages/max_tokens/
  stream/temperature only, exact model id). On Apple silicon it is the managed
  local oMLX model and is used only when already resident (never a cold load);
- any failure, timeout, empty answer, or an answer that appears to add facts
  returns ``None`` / ``{}`` so the engine keeps its template text.

The engine's ``Phraser`` protocol is synchronous and the engine runs on a
worker thread (see ``behavior_runtime``), so each call runs its own short-lived
event loop. Called on the event-loop thread it simply returns ``None``.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from service import idle

TIMEOUT_S = 6.0
# Phrasing is decoration: cap model calls so one mining/nudge pass can never
# queue minutes of generation behind it.
MAX_CALLS_PER_WINDOW = 4
WINDOW_S = 60.0
_MAX_FACTS_BYTES = 3000


def _event_loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class WispPhraser:
    """``service.behavior.engine.Phraser`` backed by Wisp's ``fast`` role."""

    def __init__(self, *, timeout: float = TIMEOUT_S, client_factory=None, clock=time.monotonic) -> None:
        self.timeout = float(timeout)
        self._client_factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: list[float] = []
        self.last_error = ""

    # ---- gating ---------------------------------------------------------------
    def _admit(self) -> bool:
        if idle.foreground_busy() or any(idle._in_flight.values()) or _event_loop_running():
            return False
        now = self._clock()
        with self._lock:
            self._calls = [t for t in self._calls if now - t < WINDOW_S]
            if len(self._calls) >= MAX_CALLS_PER_WINDOW:
                return False
            self._calls.append(now)
        return True

    # ---- transport ------------------------------------------------------------
    async def _complete_async(self, system: str, user: str, *, max_tokens: int,
                              temperature: float) -> str | None:
        from service.config.endpoints import role_target
        from service.inference.remote_policy import remote_only_profile

        target = role_target("fast")
        remote = remote_only_profile() is not None
        if self._client_factory is not None:
            client = self._client_factory(target)
        else:
            from service.inference.omlx_client import OMLXClient
            client = OMLXClient(target=target, timeout=self.timeout)
        try:
            extra: dict = {}
            if remote:
                # Qwen-family soft switch (other models ignore it); remote
                # payloads accept no template kwargs.
                user = user + "\n/no_think"
            else:
                if not getattr(client, "managed", True):
                    return None
                # Never cold-load or swap the resident model for decoration.
                if target.model not in await client.loaded_models():
                    return None
                from service.config import no_thinking_kwargs
                extra = no_thinking_kwargs(target.model)
            if idle.foreground_busy():
                return None
            from service.inference.power_tier import BACKGROUND, power_tier
            with power_tier(BACKGROUND):
                response = await client.chat(
                    target.model,
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=temperature, max_tokens=max_tokens, **extra)
            choice = (response.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content")
            if not isinstance(content, str):
                return None
            from service.behavior.phraser import strip_think
            text = strip_think(content).replace("/no_think", "").strip()
            return text or None
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

    def _complete(self, system: str, user: str, *, max_tokens: int, temperature: float) -> str | None:
        if not self._admit():
            return None
        try:
            return asyncio.run(asyncio.wait_for(
                self._complete_async(system, user, max_tokens=max_tokens, temperature=temperature),
                timeout=self.timeout))
        except BaseException as exc:  # noqa: BLE001 — decoration must never fail the engine
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.last_error = type(exc).__name__
            return None

    # ---- Phraser protocol -----------------------------------------------------
    def phrase(self, kind: str, facts: dict) -> str | None:
        try:
            from service.behavior.phraser import PHRASE_SYSTEM, _adds_facts, _clean_sentence
            facts_text = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
            if len(facts_text.encode("utf-8")) > _MAX_FACTS_BYTES:
                return None
            raw = self._complete(PHRASE_SYSTEM, f"Notification kind: {kind}\nFacts (JSON): {facts_text}",
                                 max_tokens=200, temperature=0.3)
            if not raw:
                return None
            text = _clean_sentence(raw)
            if not text or _adds_facts(text, facts_text, str(kind)):
                return None
            return text
        except Exception:  # noqa: BLE001
            return None

    def categorize(self, titles: list[str], categories: tuple[str, ...]) -> dict[str, str]:
        try:
            from service.behavior.phraser import CATEGORIZE_SYSTEM, _parse_mapping
            allowed = tuple(c for c in categories if isinstance(c, str) and c)
            batch: list[str] = []
            for t in titles or ():
                if isinstance(t, str) and t.strip() and t not in batch:
                    batch.append(t)
            batch = batch[:30]
            if not allowed or not batch:
                return {}
            numbered = "\n".join(f"{i}. {t[:120]}" for i, t in enumerate(batch, 1))
            raw = self._complete(CATEGORIZE_SYSTEM,
                                 f"Allowed categories: {', '.join(allowed)}\nTitles:\n{numbered}",
                                 max_tokens=min(1024, 16 * len(batch) + 64), temperature=0.0)
            return _parse_mapping(raw, batch, allowed)
        except Exception:  # noqa: BLE001
            return {}
