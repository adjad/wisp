"""Thin async client for the local oMLX server (OpenAI-compatible API).

oMLX owns model serving, switching, batching and the SSD KV cache. This client
just talks to its /v1/chat/completions endpoint, with streaming support.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from service import idle
from service.config import omlx_api_key, omlx_base_url


class OMLXClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: float = 600.0) -> None:
        self.base_url = (base_url or omlx_base_url()).rstrip("/")
        self.api_key = api_key or omlx_api_key()
        # generous read timeout: a cold model load can take tens of seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        # Models to keep resident ("warm") across other models' loads, so the
        # small always-on summarizer (gemma) doesn't get evicted every time the
        # heavy agent model (gpt-oss) runs. Any single keep-warm model co-fits
        # with any one other model under the 20.4GB cap (gemma 5.5 + gpt-oss
        # 12.7 = 15; gemma + vision 6 = 11.5), so ensure_only never has to
        # sacrifice the target to honor keep-warm. Empty by default → identical
        # to the old exclusive-residency behavior.
        self._keep_warm: set[str] = set()

    def set_keep_warm(self, models: set[str]) -> None:
        self._keep_warm = set(models)

    def keep_warm(self) -> set[str]:
        return set(self._keep_warm)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        r = await self._client.get("/health")
        r.raise_for_status()
        return r.json()

    async def models(self) -> list[str]:
        r = await self._client.get("/v1/models")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    async def status(self) -> dict[str, Any]:
        r = await self._client.get("/v1/models/status")
        r.raise_for_status()
        return r.json()

    async def loaded_models(self) -> list[str]:
        s = await self.status()
        return [m["id"] for m in s.get("models", []) if m.get("loaded")]

    async def unload(self, model: str) -> None:
        await self._client.post(f"/v1/models/{model}/unload")

    async def load(self, model: str) -> None:
        await self._client.post(f"/v1/models/{model}/load")

    async def ensure_only(self, model: str, *, settle_timeout: float = 60.0,
                          exclusive: bool = False) -> None:
        """Make `model` resident, evicting everything EXCEPT keep-warm models.

        With no keep-warm set this is exactly the old behavior: `model` becomes
        the sole resident. With gemma kept warm, gemma survives the eviction and
        is reloaded if it had been dropped, so it's always ready for an instant
        summary while the heavy `model` (gpt-oss) still gets the memory it needs
        — AT REST. Verified in practice that this doesn't hold once gpt-oss is
        actively generating: its KV cache grows past the point where both fit,
        and oMLX evicts gemma anyway mid-turn — so trying to preserve it here
        just adds pointless load/poll overhead for a guarantee that doesn't
        actually hold. `exclusive=True` skips the keep-warm set entirely for
        this call (pure old single-resident behavior) — used for gpt-oss turns,
        where the caller separately reloads gemma in the background right
        after the turn ends (see main.py), instead of fighting for co-residency
        during the turn itself.

        Large models near the wired cap can't co-reside: oMLX returns 507 rather
        than auto-evicting. Unload returns before memory is actually freed, so we
        must POLL until evicted models are gone before loading — otherwise the
        next request races a half-freed allocation and 507s. The TARGET is
        loaded first and always wins; keep-warm reloads are best-effort so they
        can never starve the model the caller actually asked for.

        Every call is preceded by exactly ONE loaded_models() fetch, reused for
        all three steps below (not re-fetched per step) — this is the common
        path (model + keep-warm already resident, nothing to do) and it's hit
        on EVERY tool-loop step, so minimizing round-trips to oMLX here matters:
        a redundant status poll is an extra chance to catch oMLX mid-hiccup on
        a call that didn't even need to happen.
        """
        import asyncio

        loop = asyncio.get_running_loop()
        keep = {model} if exclusive else (self._keep_warm | {model})
        loaded = set(await self.loaded_models())

        # 1. Evict everything not in the keep set (no-op if nothing to evict).
        to_evict = loaded - keep
        for m in to_evict:
            await self.unload(m)

        if to_evict:
            deadline = loop.time() + settle_timeout
            while loop.time() < deadline:
                loaded = set(await self.loaded_models())
                if not (loaded - keep):
                    break
                await asyncio.sleep(1.0)

        # 2. Load the TARGET if it isn't already resident (it always wins).
        if model not in loaded:
            await self.load(model)
            deadline = loop.time() + settle_timeout
            while loop.time() < deadline:
                loaded = set(await self.loaded_models())
                if model in loaded:
                    break
                await asyncio.sleep(1.0)

        # 3. Best-effort: reload any keep-warm model missing from the status we
        #    already have — no extra fetch needed to check this. Skipped when
        #    exclusive: the whole point of that mode is NOT to have gemma
        #    resident while this model is active (see the docstring).
        if not exclusive:
            for k in self._keep_warm - loaded - {model}:
                await self.load(k)

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        **extra: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat completion. Returns the raw oMLX response dict."""
        payload = self._payload(model, messages, tools, tool_choice,
                                temperature, max_tokens, stream=False, **extra)
        idle.begin(model)
        try:
            r = await self._client.post("/v1/chat/completions", json=payload)
            r.raise_for_status()
            return r.json()
        finally:
            idle.end(model)

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        **extra: Any,
    ) -> AsyncIterator[str]:
        """Stream assistant text deltas (SSE). Yields content chunks as they arrive."""
        payload = self._payload(model, messages, None, None,
                                temperature, max_tokens, stream=True, **extra)
        idle.begin(model)
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: "):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if (text := delta.get("content")):
                        yield text
        finally:
            idle.end(model)

    async def stream_events(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        **extra: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming chat that also carries reasoning and tool calls.

        Yields typed events as they arrive:
          {"kind": "reasoning", "text": <delta>}  — chain-of-thought token
          {"kind": "content",   "text": <delta>}  — answer token
        then a terminal event once the stream closes:
          {"kind": "final", "message": {content, reasoning_content, tool_calls}}

        The final message reassembles the fragmented streaming tool_calls (id +
        name arrive once, arguments stream across chunks) so the agent loop can
        act on them exactly as it did with the non-streaming `chat`.
        """
        payload = self._payload(model, messages, tools, tool_choice,
                                temperature, max_tokens, stream=True, **extra)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        idle.begin(model)
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: "):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if (t := delta.get("reasoning_content")):
                        reasoning_parts.append(t)
                        yield {"kind": "reasoning", "text": t}
                    if (t := delta.get("content")):
                        content_parts.append(t)
                        yield {"kind": "content", "text": t}
                    for tc in (delta.get("tool_calls") or []):
                        slot = calls.setdefault(tc.get("index", 0),
                                                {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        finally:
            idle.end(model)
        tool_calls = [
            {"id": s["id"], "type": "function",
             "function": {"name": s["name"], "arguments": s["arguments"]}}
            for _, s in sorted(calls.items()) if s["name"]
        ]
        yield {"kind": "final", "message": {
            "role": "assistant",
            "content": "".join(content_parts),
            "reasoning_content": "".join(reasoning_parts),
            "tool_calls": tool_calls or None,
        }}

    @staticmethod
    def _payload(model, messages, tools, tool_choice, temperature, max_tokens,
                 stream, **extra) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        payload.update(extra)
        return payload
