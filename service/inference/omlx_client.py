"""Thin async client for the local oMLX server (OpenAI-compatible API).

oMLX owns model serving, switching, batching and the SSD KV cache. This client
just talks to its /v1/chat/completions endpoint, with streaming support.
"""
from __future__ import annotations

import json
import math
from typing import Any, Awaitable, Callable, AsyncIterator

import httpx

from service.config.quarantine import guard_client

from service import idle
from service.config import omlx_api_key, omlx_base_url
from service.config.endpoints import EndpointConfigurationError, Target, endpoint, is_loopback


class IncompleteStreamError(RuntimeError):
    """The server ended generation without a complete, executable result."""


class SanitizedHTTPStatusError(httpx.HTTPStatusError):
    """Remote status failure with no provider-controlled request or response data."""


from .inference_errors import ModelLoadError


# Remote output is untrusted and must stay bounded independently of timeouts.
# Sixteen bytes/token accommodates escaped JSON plus content/reasoning overhead.
REMOTE_OUTPUT_MIN_BYTES = 64 * 1024
REMOTE_OUTPUT_HARD_BYTES = 8 * 1024 * 1024
REMOTE_WIRE_HARD_BYTES = 16 * 1024 * 1024
REMOTE_BYTES_PER_TOKEN = 16


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
                 timeout: float = 600.0, *, target: Target | None = None) -> None:
        self.target = target
        ep = target.endpoint if target else (endpoint() if base_url is None else None)
        self.base_url = (ep.base_url if ep else base_url).rstrip("/")
        self.managed = ep.managed if ep else is_loopback(self.base_url)
        self.endpoint_name = ep.name if ep else ("local" if self.managed else "remote")
        from .providers import provider
        self.provider = provider(ep.provider if ep else "omlx")
        self.api_prefix = ep.api_prefix if ep else self.provider.api_prefix
        key_loader = None
        if api_key is None:
            if ep and ep.credential_ref.startswith("keychain:"):
                key_loader = ep.api_key
            elif ep:
                api_key = ep.api_key()
            elif self.managed:
                api_key = omlx_api_key()
            else:
                raise EndpointConfigurationError("An explicit remote URL requires its own credential")
        self.api_key = api_key
        from .attributed_transport import CredentialTransport
        self._credential_transport = CredentialTransport(self.base_url, api_key,
            managed=self.managed, key_loader=key_loader)
        self._client = guard_client(httpx.AsyncClient(
            base_url=self.base_url,
            transport=self._credential_transport,
            timeout=httpx.Timeout(timeout, connect=5.0),
            trust_env=False, follow_redirects=False,
        ))
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

    def invalidate_connections(self):
        self._credential_transport.invalidate()

    def _check_response(self, response):
        if not self.managed and not response.is_success:
            # Provider-controlled redirect locations and error bodies may echo
            # private data. Do not retain the real request either: it contains
            # the prompt. Shared error handling receives only this synthetic
            # status envelope, regardless of the remote protocol profile.
            request = httpx.Request("POST", "https://inference.invalid/request")
            safe_response = httpx.Response(response.status_code, request=request)
            raise SanitizedHTTPStatusError(
                f"Remote inference returned HTTP {response.status_code}",
                request=request, response=safe_response,
            ) from None
        response.raise_for_status()

    @staticmethod
    def _remote_limits(max_tokens: int) -> tuple[int, int]:
        output = min(REMOTE_OUTPUT_HARD_BYTES,
                     max(REMOTE_OUTPUT_MIN_BYTES, max_tokens * REMOTE_BYTES_PER_TOKEN))
        wire = min(REMOTE_WIRE_HARD_BYTES,
                   max(REMOTE_OUTPUT_MIN_BYTES, output * 2 + REMOTE_OUTPUT_MIN_BYTES))
        return output, wire

    @staticmethod
    def _check_remote_headers(response, maximum: int) -> None:
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise IncompleteStreamError("Remote inference response encoding is not allowed")
        length = response.headers.get("content-length")
        if length is not None and (
                not length.isascii() or not length.isdigit() or int(length) > maximum):
            raise IncompleteStreamError("Remote inference response exceeded the allowed size")

    async def _remote_body(self, response, maximum: int) -> bytes:
        self._check_remote_headers(response, maximum)
        body = bytearray()
        async for part in response.aiter_bytes(chunk_size=8192):
            if len(body) + len(part) > maximum:
                raise IncompleteStreamError("Remote inference response exceeded the allowed size")
            body.extend(part)
        return bytes(body)

    async def _remote_lines(self, response, maximum: int):
        """Yield UTF-8 lines while bounding the entire decoded remote stream."""
        self._check_remote_headers(response, maximum)
        pending = bytearray()
        received = 0
        async for part in response.aiter_bytes(chunk_size=8192):
            received += len(part)
            if received > maximum:
                raise IncompleteStreamError("Remote inference stream exceeded the allowed size")
            pending.extend(part)
            while (newline := pending.find(b"\n")) >= 0:
                raw = bytes(pending[:newline])
                del pending[:newline + 1]
                try:
                    yield raw.removesuffix(b"\r").decode("utf-8")
                except UnicodeDecodeError:
                    raise IncompleteStreamError("Invalid inference stream data") from None
        if pending:
            try:
                yield bytes(pending).removesuffix(b"\r").decode("utf-8")
            except UnicodeDecodeError:
                raise IncompleteStreamError("Invalid inference stream data") from None

    @staticmethod
    def _completion_data(response_or_body):
        try:
            data = (json.loads(response_or_body)
                    if isinstance(response_or_body, (bytes, bytearray))
                    else response_or_body.json())
        except (ValueError, TypeError, RecursionError):
            raise IncompleteStreamError("Invalid inference response") from None
        if not isinstance(data, dict) or "error" in data:
            # A provider's HTTP-200 error object is still an error. Reject it
            # before choice normalization or debug capture and retain no body.
            raise IncompleteStreamError("Inference provider returned an error") from None
        return data

    @staticmethod
    def _bounded_remote_value(total: int, value: Any, maximum: int) -> int:
        if value is None:
            return total
        if type(value) is not str:
            raise IncompleteStreamError("Invalid inference response data")
        try:
            total += len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise IncompleteStreamError("Invalid inference response data") from None
        if total > maximum:
            raise IncompleteStreamError("Remote inference output exceeded the allowed size")
        return total

    @staticmethod
    def _bounded_remote_json(total: int, value: Any, maximum: int) -> int:
        """Charge nested JSON content to one output budget without recursion."""
        active: set[int] = set()
        stack: list[tuple[str, Any]] = [("value", value)]

        def charge(size: int) -> None:
            nonlocal total
            total += size
            if total > maximum:
                raise IncompleteStreamError(
                    "Remote inference output exceeded the allowed size")

        while stack:
            action, current = stack.pop()
            if action == "leave":
                active.remove(current)
                continue
            if action == "list":
                try:
                    item = next(current)
                except StopIteration:
                    continue
                stack.append(("list", current))
                stack.append(("value", item))
                continue
            if action == "dict":
                try:
                    key, item = next(current)
                except StopIteration:
                    continue
                if type(key) is not str:
                    raise IncompleteStreamError("Invalid inference response data")
                # Quotes and a colon are charged along with the UTF-8 key.
                try:
                    charge(len(key.encode("utf-8")) + 3)
                except UnicodeEncodeError:
                    raise IncompleteStreamError("Invalid inference response data") from None
                stack.append(("dict", current))
                stack.append(("value", item))
                continue

            if type(current) is str:
                try:
                    charge(len(current.encode("utf-8")))
                except UnicodeEncodeError:
                    raise IncompleteStreamError("Invalid inference response data") from None
            elif current is None:
                charge(4)
            elif type(current) is bool:
                charge(4 if current else 5)
            elif type(current) is int:
                try:
                    charge(len(str(current)))
                except (ValueError, OverflowError):
                    raise IncompleteStreamError("Invalid inference response data") from None
            elif type(current) is float:
                if not math.isfinite(current):
                    raise IncompleteStreamError("Invalid inference response data")
                charge(len(json.dumps(current)))
            elif type(current) is list:
                identity = id(current)
                if identity in active:
                    raise IncompleteStreamError("Invalid inference response data")
                active.add(identity)
                charge(2 + max(0, len(current) - 1))
                stack.append(("leave", identity))
                stack.append(("list", iter(current)))
            elif type(current) is dict:
                identity = id(current)
                if identity in active:
                    raise IncompleteStreamError("Invalid inference response data")
                active.add(identity)
                charge(2 + max(0, len(current) - 1))
                stack.append(("leave", identity))
                stack.append(("dict", iter(current.items())))
            else:
                raise IncompleteStreamError("Invalid inference response data")
        return total

    @classmethod
    def _check_remote_completion_output(cls, data: dict[str, Any], maximum: int) -> None:
        total = 0
        metadata_total = 0
        choices = data.get("choices")
        # Managed oMLX historically degrades a missing choices array into an
        # empty completion. A remote provider is a different trust boundary:
        # accepting that malformed shape would preserve arbitrary provider
        # fields when `_ensure_choices` synthesizes its fallback response.
        # Reject it before normalization or debug capture instead.
        if not isinstance(choices, list) or not choices:
            raise IncompleteStreamError("Invalid inference response data")
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise IncompleteStreamError("Invalid inference response data")
            message = choice["message"]
            total = cls._bounded_remote_value(total, message.get("content"), maximum)
            # Providers can return the same reasoning in both their native
            # `reasoning` field and OpenAI-compatible `reasoning_content`.
            # Wisp consumes one of those representations, so charging both
            # falsely halves the usable answer budget.
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            total = cls._bounded_remote_value(total, reasoning, maximum)
            if "reasoning_details" in message:
                # Structured reasoning metadata often mirrors the reasoning
                # text. Bound it independently while the whole response stays
                # protected by the stricter wire-size ceiling.
                metadata_total = cls._bounded_remote_json(
                    metadata_total, message["reasoning_details"], maximum)
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise IncompleteStreamError("Invalid inference response data")
            for call in tool_calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise IncompleteStreamError("Invalid inference response data")
                total = cls._bounded_remote_value(
                    total, call["function"].get("arguments"), maximum)

    async def _readiness_mapping(self, path, maximum):
        # Bound the wire stream before JSON parsing. Reject compression rather
        # than allocating an unbounded decoded chunk from a small gzip body.
        import asyncio
        import json
        deadline = self.target.endpoint.readiness_timeout if self.target else 5.0
        try:
            async with asyncio.timeout(deadline):
                async with self._client.stream("GET", path, headers={"Accept-Encoding": "identity"}) as response:
                    self._check_response(response)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isascii() or not length.isdigit() or int(length) > maximum):
                        raise ValueError
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(body) + len(chunk) > maximum:
                            raise ValueError
                        body.extend(chunk)
                    data = json.loads(body)
                    if not isinstance(data, dict) or "error" in data:
                        raise ValueError
                    return data
        except EndpointConfigurationError:
            # Credential/configuration refusal must not become an availability
            # failure eligible for local fallback.
            raise
        except (ValueError, TypeError, RecursionError, TimeoutError):
            raise ModelLoadError("Invalid or unavailable inference readiness response") from None

    @staticmethod
    def _model_rows(data, key, maximum=1000):
        try:
            rows = data[key]
            if not isinstance(rows, list) or len(rows) > maximum:
                raise ValueError
            ids = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError
                model = row["id"]
                if (not isinstance(model, str) or not model or len(model) > 200
                        or model.strip() != model or any(ord(c) < 32 for c in model)):
                    raise ValueError
                if key == "models" and type(row.get("loaded", False)) is not bool:
                    raise ValueError
                ids.append(model)
            if len(set(ids)) != len(ids):
                raise ValueError
            return rows
        except (KeyError, ValueError, TypeError):
            raise ModelLoadError("Invalid inference model inventory") from None

    async def health(self) -> dict[str, Any]:
        if not self.provider.omlx_health:
            await self.models()
            return {"status": "ok"}
        data = await self._readiness_mapping("/health", 64 * 1024)
        if data.get("status") not in ("ok", "healthy"):
            raise ModelLoadError("Inference health unavailable")
        return {"status": "ok"}

    async def models(self) -> list[str]:
        data = await self._readiness_mapping(self.api_prefix + "/models", self.provider.inventory_bytes)
        return [m["id"] for m in self._model_rows(data, "data", self.provider.inventory_rows)]

    async def status(self) -> dict[str, Any]:
        if not self.provider.omlx_health:
            raise EndpointConfigurationError("Provider has no oMLX residency API")
        data = await self._readiness_mapping("/v1/models/status", 1024 * 1024)
        self._model_rows(data, "models")
        return data

    async def loaded_models(self) -> list[str]:
        s = await self.status()
        return [m["id"] for m in s["models"] if m.get("loaded", False)]

    async def unload(self, model: str) -> None:
        if not self.managed:
            raise EndpointConfigurationError("Only managed local models can be unloaded")
        r = await self._client.post(f"/v1/models/{model}/unload")
        r.raise_for_status()

    async def load(self, model: str) -> None:
        if not self.managed:
            raise EndpointConfigurationError("Only managed local models can be loaded")
        r = await self._client.post(f"/v1/models/{model}/load")
        r.raise_for_status()

    async def ensure_only(self, model: str, *, settle_timeout: float = 60.0,
                          exclusive: bool = False, emit=None) -> None:
        import asyncio
        # Include status/load network waits in the total deadline.
        deadline = settle_timeout if self.managed else min(settle_timeout,
            self.target.endpoint.readiness_timeout if self.target else 5.0)
        try:
            async with asyncio.timeout(deadline):
                if not self.managed:
                    # Remote inference auto-loads on demand. The Pro does not own
                    # remote model admission, eviction, or administrative APIs.
                    if self.provider.omlx_health:
                        await self.health()
                    if model not in await self.models():
                        raise ModelLoadError(f"Model {model!r} is unavailable on {self.endpoint_name}")
                    return
                await self._ensure_managed(model, settle_timeout=settle_timeout,
                                           exclusive=exclusive, emit=emit)
        except TimeoutError as exc:
            raise ModelLoadError(f"Inference readiness timed out on {self.endpoint_name}") from exc

    async def _ensure_managed(self, model: str, *, settle_timeout: float = 60.0,
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
        # role pointing at `Qwen3.6-27B-oQ3` (installed id is `Qwen3.6-27B-oQ3.5e`)
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
                try:
                    await self.load(k)
                except httpx.HTTPError:
                    pass  # optional keep-warm reload cannot fail the requested model

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
        model, messages, tools, max_tokens = self._fit_request(model, messages, tools, max_tokens)
        payload = self._payload(model, messages, tools, tool_choice,
                                temperature, max_tokens, stream=False, **extra)
        idle.begin(self.activity_key(model))
        try:
            if self.managed:
                r = await self._client.post(self.api_prefix + "/chat/completions", json=payload)
                self._check_response(r)
                data = self._completion_data(r)
            else:
                output_limit, wire_limit = self._remote_limits(max_tokens)
                async with self._client.stream("POST", self.api_prefix + "/chat/completions",
                        json=payload, headers={"Accept-Encoding": "identity"}) as r:
                    self._check_response(r)
                    data = self._completion_data(await self._remote_body(r, wire_limit))
                self._check_remote_completion_output(data, output_limit)
            data = _ensure_choices(data)
            # Applied here, not per-caller: an unclosed think block is a model
            # property, so every non-streaming caller (summaries, briefs,
            # codegen, router) would otherwise need its own copy of this guard.
            for choice in data.get("choices") or []:
                if isinstance(choice.get("message"), dict):
                    message = choice["message"]
                    if (not self.managed and message.get("reasoning_details")
                            and message.get("tool_calls")):
                        raise IncompleteStreamError("Provider reasoning tool replay is not supported")
                    if self.provider.name != "omlx":
                        if message.get("reasoning") and not message.get("reasoning_content"):
                            message["reasoning_content"] = message["reasoning"]
                    if choice["message"].get("tool_calls") and choice.get("finish_reason") != "tool_calls":
                        raise IncompleteStreamError("Tool generation did not finish successfully")
                    if self.managed:
                        _demote_unclosed_think(choice["message"], choice.get("finish_reason"))
            return data
        finally:
            idle.end(self.activity_key(model))

    async def stream(self, model: str, messages: list[dict[str, Any]], **kwargs) -> AsyncIterator[str]:
        events = self.stream_events(model, messages, **kwargs)
        try:
            async for event in events:
                if event["kind"] == "content":
                    yield event["text"]
        finally:
            await events.aclose()

    async def stream_events(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        temperature: float | None = None,
        max_tokens: int = 2048,
        use_remaining_context: bool = False,
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
        model, messages, tools, max_tokens = self._fit_request(
            model, messages, tools, max_tokens,
            use_remaining_context=use_remaining_context)
        payload = self._payload(model, messages, tools, tool_choice,
                                temperature, max_tokens, stream=True, **extra)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        done = False
        has_reasoning_details = False
        output_size = 0
        metadata_size = 0
        output_limit, wire_limit = self._remote_limits(max_tokens)
        idle.begin(self.activity_key(model))
        try:
            headers = None if self.managed else {"Accept-Encoding": "identity"}
            async with self._client.stream("POST", self.api_prefix + "/chat/completions",
                    json=payload, headers=headers) as r:
                self._check_response(r)
                lines = (r.aiter_lines() if self.managed
                         else self._remote_lines(r, wire_limit))
                async for line in lines:
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        raise IncompleteStreamError("Invalid inference stream data") from None
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise IncompleteStreamError("Inference stream returned an error")
                    choices = chunk.get("choices")
                    if choices == [] and "usage" in chunk:
                        continue
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        raise IncompleteStreamError("Invalid inference stream choices")
                    choice = choices[0]
                    # Carried to the final event so the same unclosed-<think>
                    # correction the non-streaming path applies can run here.
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta", {})
                    if not self.managed and "reasoning_details" in delta:
                        metadata_size = self._bounded_remote_json(
                            metadata_size, delta["reasoning_details"], output_limit)
                    has_reasoning_details |= bool(delta.get("reasoning_details"))
                    if (t := delta.get("reasoning_content") or (
                            delta.get("reasoning") if self.provider.name != "omlx" else None)):
                        if not self.managed:
                            output_size = self._bounded_remote_value(
                                output_size, t, output_limit)
                        reasoning_parts.append(t)
                        yield {"kind": "reasoning", "text": t}
                    if (t := delta.get("content")):
                        if not self.managed:
                            output_size = self._bounded_remote_value(
                                output_size, t, output_limit)
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
                            if not self.managed:
                                output_size = self._bounded_remote_value(
                                    output_size, fn["arguments"], output_limit)
                            slot["arguments"] += fn["arguments"]
        finally:
            idle.end(self.activity_key(model))
        if finish_reason not in {"stop", "length", "tool_calls"}:
            raise IncompleteStreamError("Inference stream ended before completion; no actions were executed from it")
        if not calls and finish_reason not in {"stop", "length"}:
            raise IncompleteStreamError("Plain generation did not finish successfully")
        if calls:
            if not self.managed and has_reasoning_details:
                raise IncompleteStreamError("Provider reasoning tool replay is not supported")
            if finish_reason != "tool_calls":
                raise IncompleteStreamError("Tool generation did not finish successfully")
            seen_ids = set()
            for call in calls.values():
                try:
                    args = json.loads(call["arguments"])
                except ValueError as exc:
                    raise IncompleteStreamError("Incomplete tool arguments") from exc
                if not call["id"] or call["id"] in seen_ids or not call["name"] or not isinstance(args, dict):
                    raise IncompleteStreamError("Invalid completed tool call")
                seen_ids.add(call["id"])
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
        if self.managed:
            _demote_unclosed_think(final_message, finish_reason)
        yield {"kind": "final", "message": final_message}

    def activity_key(self, model: str) -> str:
        return model if self.managed else f"{self.endpoint_name}:{self.base_url}:{model}"

    def _fit_request(self, model, messages, tools, max_tokens, *,
                     use_remaining_context: bool = False):
        if self.target is None:
            return model, messages, tools, max_tokens
        if model != self.target.model:
            raise EndpointConfigurationError("A bound inference client cannot change model identity")
        if tools and not self.managed and "tools" not in self.target.capabilities:
            raise EndpointConfigurationError("Remote tool calling has not been qualified for this target")
        from service.agent.loop import _fit_window, _est_tokens
        messages, fitted_tools, max_tokens = _fit_window(
            messages, tools or [], max_tokens, model, None,
            context_window=self.target.context_window,
            protected_prefix_count=next((i for i, m in enumerate(messages)
                                         if m.get("role") != "system"), len(messages)))
        # Never silently drop required tool schemas or send an overfull prompt.
        if len(fitted_tools) != len(tools or []):
            raise EndpointConfigurationError("Target context is too small for the requested tools")
        cost = sum(_est_tokens(m.get("content") or "") + _est_tokens(m.get("tool_calls") or [])
                   for m in messages) + _est_tokens(fitted_tools)
        if use_remaining_context and not self.managed:
            # Direct cloud answers should not inherit Wisp's historical fixed
            # 8k generation ceiling. After fitting the prompt, offer the model
            # every token left in the user-configured context window. Internal
            # structured calls retain their explicit bounded budgets.
            max_tokens = self.target.context_window - cost
        if cost + max_tokens > self.target.context_window:
            raise EndpointConfigurationError("Request cannot fit the target context window")
        return model, messages, tools, max_tokens

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
