"""Thin async client for the local oMLX server (OpenAI-compatible API).

oMLX owns model serving, switching, batching and the SSD KV cache. This client
just talks to its /v1/chat/completions endpoint, with streaming support.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, AsyncIterator

import httpx

from service import idle
from service.config import omlx_api_key, omlx_base_url


class ModelLoadError(RuntimeError):
    """A model a role points at could not be made resident.

    Raised instead of returning quietly, so a bad role assignment surfaces as
    itself rather than as whatever the next call does against a model that
    never loaded. See OMLXClient.ensure_only.
    """


def _demote_unclosed_think(message: dict[str, Any], finish_reason: str | None) -> None:
    """Move a truncated, never-closed <think> block out of `content`, in place.

    Models with an ALWAYS-ON think block (LFM2.5, the default for nearly every
    role as of 2026-08-06) open `<think>` before writing any answer. oMLX only
    routes that block to `reasoning_content` once it sees the CLOSING tag; if
    generation hits the token ceiling first, the opening tag is still consumed
    but the monologue it introduced is handed back as `content` — the answer
    field — with no tag left in it to recognize. `_strip_think`'s <think> regex
    cannot catch this: by the time we see the text, there is no tag to match.

    Verified live on LFM2.5 (2026-08-06): a summarize_emails call capped at 800
    tokens returned finish_reason="length", reasoning_content="", and a content
    field that opened "The user wants me to create a warm, helpful inbox
    summary for Adi Jain. Let me analyze..." — which the agent loop then used as
    the tool result, so the model's private planning was shown to the user as
    the actual inbox summary. profile.py::_clean_or_empty documents the same
    failure on the profile path and guards it locally; this is that guard moved
    to the one place EVERY caller goes through, since the leak is a property of
    the model, not of any single prompt.

    The tell is exact rather than heuristic: an always-thinking model that
    closed its block leaves reasoning_content non-empty, so an empty
    reasoning_content plus a truncated finish is precisely the case where the
    block never closed. A run that stopped cleanly is never touched.

    Reclassifying (rather than deleting) keeps the text visible in the UI's
    "Show reasoning" disclosure and the debug export, and leaves `content`
    empty — which every call site already treats as "no summary", falling back
    to raw inbox/message lines instead of printing a monologue.

    SECOND SHAPE — the monologue DUPLICATED into both fields. Verified live
    2026-08-08 on Agents-A1-4B: a step truncated at max_tokens=804 came back
    with content and reasoning_content byte-identical (3597 chars each), both
    holding the same "We need to answer the user's question..." planning text
    cut off mid-sentence. The empty-reasoning_content test above returns early
    on this — reasoning_content is very much non-empty — so the guard passed it
    straight through and the agent loop returned the monologue as the turn's
    answer. Equality is as exact a tell as the first shape: a model that
    actually produced an answer never emits it character-for-character
    identical to its own chain-of-thought, so this cannot fire on a real one.
    """
    if finish_reason != "length":
        return
    leaked = (message.get("content") or "").strip()
    if not leaked:
        return
    reasoning = (message.get("reasoning_content") or "").strip()
    if reasoning and reasoning != leaked:
        return
    message["content"] = ""
    message["reasoning_content"] = leaked
    # Flag it, don't just blank it. On the STREAMING paths the leaked text has
    # already gone out to the client token by token, so a caller that only
    # sees content=="" would quietly leave the monologue on screen as the
    # answer. This lets the agent loop retract what it streamed (see
    # `clear_answer`) instead of silently appending nothing to it.
    message["_think_leak"] = True


def _ensure_choices(data: dict[str, Any]) -> dict[str, Any]:
    """Guarantee a chat response has a usable `choices[0].message`.

    Under concurrent or heavy load oMLX can answer 200 with a body that has NO
    `choices` key at all — not an error status, just a missing key. Every caller
    indexes `resp["choices"][0]["message"]` directly (13 sites across 11 modules
    at the time of writing), so that body became a raw `KeyError: 'choices'`
    propagating out as an unexplained 500 with no useful message.

    Reproduced deliberately 2026-08-08 by issuing two `summarize_*` calls
    concurrently against the one resident model: `KeyError: 'choices'` out of
    imessage_tools._summarize. It had previously been seen in the wild from
    brief.py under load.

    Normalizing here rather than at each call site is what makes the fix
    complete: every one of those callers ALREADY handles an empty content
    string (falling back to raw inbox/message lines, or to "no summary"), so a
    well-formed empty response degrades gracefully everywhere, while a missing
    key crashes everywhere. `finish_reason="error"` keeps the cause visible in
    the debug export instead of looking like the model chose to say nothing.
    """
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return data
    return {**data, "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "", "reasoning_content": ""},
        "finish_reason": "error",
    }]}


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
        # Models to keep resident ("warm") across other models' loads, so a
        # frequently-used small model isn't evicted every time a heavier one
        # runs. The co-residency arithmetic that justified this was written for
        # the old two-model roster (a 5.5GB summarizer alongside a 12.7GB agent
        # model, under a 20.4GB cap) and no longer describes anything: every
        # text role resolves to one ~4.1GB resident model, and the budget is now
        # oMLX's 8.0GB process ceiling, not 20.4GB. Kept because the mechanism
        # is still correct and main.py still calls set_keep_warm; empty by
        # default → identical to plain exclusive residency.
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
                          exclusive: bool = False,
                          emit: Callable[[dict], Awaitable[None]] | None = None) -> None:
        """Make `model` resident, evicting everything EXCEPT keep-warm models.

        With no keep-warm set this is exactly the old behavior: `model` becomes
        the sole resident. With the summarizer kept warm, the summarizer survives the eviction and
        is reloaded if it had been dropped, so it's always ready for an instant
        summary while the heavy `model` (the agent model) still gets the memory it needs
        — AT REST. Verified in practice that this doesn't hold once the agent model is
        actively generating: its KV cache grows past the point where both fit,
        and oMLX evicts the summarizer anyway mid-turn — so trying to preserve it here
        just adds pointless load/poll overhead for a guarantee that doesn't
        actually hold. `exclusive=True` skips the keep-warm set entirely for
        this call (pure old single-resident behavior) — used for the agent model turns,
        where the caller separately reloads the summarizer in the background right
        after the turn ends (see main.py), instead of fighting for co-residency
        during the turn itself.

        Large models near the wired cap can't co-reside: oMLX returns 507 rather
        than auto-evicting. Unload returns before memory is actually freed, so we
        must POLL until evicted models are gone before loading — otherwise the
        next request races a half-freed allocation and 507s. The TARGET is
        loaded first and always wins; keep-warm reloads are best-effort so they
        can never starve the model the caller actually asked for.

        `emit`: optional SSE sink (see agent/loop.py's `Emit`). A cold model
        swap/load here can take anywhere from a few seconds to ~30s+, and it
        sits BETWEEN the `routed` event and the first token/tool-call — a gap
        with no client-visible signal otherwise, which reads as a hung
        request. When given, this sends `status` events naming what's
        actually happening ("Loading <model>… (Ns)") so the UI can show real
        progress instead of a static "Thinking…"/"Working…" the whole time.
        A no-op when nothing needs evicting/loading (the common case).

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
            start = loop.time()
            deadline = start + settle_timeout
            while loop.time() < deadline:
                loaded = set(await self.loaded_models())
                if not (loaded - keep):
                    break
                if emit:
                    await emit({"type": "status",
                                "text": f"Freeing memory… ({int(loop.time() - start)}s)"})
                await asyncio.sleep(1.0)

        # 2. Load the TARGET if it isn't already resident (it always wins).
        #
        # oMLX accepts POST /v1/models/<id>/load for an id it doesn't have and
        # reports no error, so a role pointing at a misspelled or uninstalled
        # model used to spend the full settle_timeout polling, fall out of the
        # loop, and return NORMALLY with nothing resident — every later call
        # then ran against a model that wasn't there. Verified live: a stale
        # `super_model: Qwen3.6-27B-oQ3` (installed id is `Qwen3.6-27B-oQ3.5e`)
        # burned 60s and returned "OK" with loaded == []. Failing loudly here
        # is what turns that from "Wisp is mysteriously broken" into one clear
        # message naming the model that couldn't load.
        if model not in loaded:
            if emit:
                await emit({"type": "status", "text": f"Loading {model}…"})
            await self.load(model)
            start = loop.time()
            deadline = start + settle_timeout
            while loop.time() < deadline:
                loaded = set(await self.loaded_models())
                if model in loaded:
                    break
                if emit:
                    await emit({"type": "status",
                                "text": f"Loading {model}… ({int(loop.time() - start)}s)"})
                await asyncio.sleep(1.0)
            else:
                installed = await self.models()
                why = ("it is not installed in oMLX"
                       if model not in installed
                       else f"it did not become resident within {settle_timeout:.0f}s")
                raise ModelLoadError(f"could not load {model!r}: {why}.")

        # 3. Best-effort: reload any keep-warm model missing from the status we
        #    already have — no extra fetch needed to check this. Skipped when
        #    exclusive: the whole point of that mode is NOT to have the summarizer
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
        temperature: float | None = None,
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
            data = r.json()
            data = _ensure_choices(data)
            # Applied here, not per-caller: an unclosed think block is a model
            # property, so every non-streaming caller (summaries, briefs,
            # codegen, router) would otherwise need its own copy of this guard.
            for choice in data.get("choices") or []:
                if isinstance(choice.get("message"), dict):
                    _demote_unclosed_think(choice["message"], choice.get("finish_reason"))
            return data
        finally:
            idle.end(model)

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
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
        temperature: float | None = None,
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
        finish_reason: str | None = None
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
                    choice = chunk.get("choices", [{}])[0]
                    # Carried to the final event so the same unclosed-<think>
                    # correction the non-streaming path applies can run here.
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta", {})
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
        final_message = {
            "role": "assistant",
            "content": "".join(content_parts),
            "reasoning_content": "".join(reasoning_parts),
            "tool_calls": tool_calls or None,
        }
        # Same correction as `chat()`. Live deltas for this case have already
        # gone out (nothing can un-send them), but every consumer of the FINAL
        # message — the agent loop's empty-content retry, main.py's
        # reasoning-as-answer fallback, session persistence — must not treat a
        # truncated monologue as the turn's answer, or it gets replayed as
        # assistant history on the next turn.
        _demote_unclosed_think(final_message, finish_reason)
        yield {"kind": "final", "message": final_message}

    @staticmethod
    def _payload(model, messages, tools, tool_choice, temperature, max_tokens,
                 stream, **extra) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # Omitted when None so oMLX falls back to the model's own configured
        # sampling (~/.omlx/model_settings.json, the admin UI's per-model
        # profile). Every model there has force_sampling=false, meaning a
        # request-level temperature WINS over the configured one — so sending
        # this key unconditionally silently discarded whatever was tuned in
        # oMLX. None is the default everywhere; pass a number only to
        # deliberately override the user's tuning for one call.
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        payload.update(extra)
        return payload
