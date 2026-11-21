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
from service.inference import engine_epoch, engine_hold
from service.config import omlx_api_key, omlx_base_url
from service.config.endpoints import EndpointConfigurationError, Target, endpoint, is_loopback


class IncompleteStreamError(RuntimeError):
    """The server ended generation without a complete, executable result."""


class SanitizedHTTPStatusError(httpx.HTTPStatusError):
    """Remote status failure with no provider-controlled request or response data."""


from .inference_errors import (EngineGuardUnavailableError, ModelLoadError, ModelMemoryLimitError,
                               ModelNotDownloadedError)


_OMLX_OTHERS_FULL = ("Wisp's model couldn't load, because other models in oMLX are using "
                     "the memory and are pinned or running. Unload one in oMLX, or wait for "
                     "it to finish, then try again.")

# The engine's prefill memory guard refused or stopped one request: its prompt needs more
# memory than is left. The model is loaded and fine, so this is not the "couldn't load" text.
REQUEST_MEMORY_MESSAGE = ("That request needed more memory than Wisp is allowed to "
                          "use. Try a shorter question or fewer attachments.")
# How much of the local engine's error body is kept to say why a streamed request was refused.
_ENGINE_ERROR_BODY_BYTES = 64 * 1024


def _stream_error_code(chunk: dict) -> str:
    """The engine's code on an in-stream error chunk (`error.code`, or oMLX's `omlx_code`)."""
    error = chunk.get("error")
    error = error if isinstance(error, dict) else {}
    for code in (error.get("code"), error.get("omlx_code"), chunk.get("omlx_code")):
        if isinstance(code, str) and code:
            return code
    return ""


def _model_label(model: str) -> str:
    """The catalog's display name for a model id ("Gemma 4 E4B"), else the id.
    Optional import: the mini bundle ships this module without service.setup."""
    try:
        from service.setup.catalog import by_id
        entry = by_id(model)
    except Exception:  # noqa: BLE001 - a label is cosmetic
        return model
    return entry.label if entry else model


async def _download_progress(model: str) -> float | None:
    """Percent done of an active built-in download of `model`, else None.

    Best effort and bounded: only the bundled engine has a download inventory,
    and a missing-model error must never wait long on it."""
    import asyncio
    try:
        from service.engine import downloads, mode
        if not mode.is_bundled():
            return None
        async with asyncio.timeout(3):
            tasks = await downloads._inventory()
        for _created, _task_id, task in tasks:
            if task.get("model") == model and task.get("state") in ("pending", "downloading"):
                return float(task.get("progress") or 0)
    except Exception:  # noqa: BLE001 - the plain "not downloaded" message still applies
        return None
    return None


# Remote output is untrusted and must stay bounded independently of timeouts.
# Sixteen bytes/token accommodates escaped JSON plus content/reasoning overhead.
REMOTE_OUTPUT_MIN_BYTES = 64 * 1024
REMOTE_OUTPUT_HARD_BYTES = 8 * 1024 * 1024
REMOTE_WIRE_HARD_BYTES = 16 * 1024 * 1024
REMOTE_BYTES_PER_TOKEN = 16
# A streamed token travels in its own SSE event. Measured on a real engine: ~240
# bytes of JSON envelope per token (687 events for ~3,000 characters of text).
# The wire budget must scale with max_tokens at that cost or any answer beyond
# ~1,100 tokens is refused as "larger than Wisp can safely hold".
REMOTE_WIRE_BYTES_PER_TOKEN = 512


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




class _RateLimitRetry(httpx.AsyncBaseTransport):
    """Remote providers only: a 429 (Gemini's per-minute limit) waits and
    re-sends the same request a few times instead of failing the turn."""
    DELAYS = (3.0, 6.0, 12.0)

    def __init__(self, inner):
        self._inner = inner

    async def handle_async_request(self, request):
        import asyncio
        for delay in (*self.DELAYS, None):
            response = await self._inner.handle_async_request(request)
            if response.status_code != 429 or delay is None:
                return response
            await response.aclose()
            await asyncio.sleep(delay)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()

class _ThoughtSplitter:
    """Moves a remote model's inline <thought>…</thought> text (Gemma on the
    Gemini API) out of the answer and into reasoning, even when a tag is split
    across stream chunks."""
    OPEN, CLOSE = "<thought>", "</thought>"

    def __init__(self) -> None:
        self.inside = False
        self.buffer = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buffer += text
        out: list[tuple[str, str]] = []
        while self.buffer:
            tag = self.CLOSE if self.inside else self.OPEN
            at = self.buffer.find(tag)
            if at >= 0:
                if at:
                    out.append(("reasoning" if self.inside else "content", self.buffer[:at]))
                self.buffer = self.buffer[at + len(tag):]
                self.inside = not self.inside
                continue
            # Keep a possible partial tag for the next chunk.
            keep = next((n for n in range(len(tag) - 1, 0, -1) if self.buffer.endswith(tag[:n])), 0)
            ready = self.buffer[:len(self.buffer) - keep]
            if ready:
                out.append(("reasoning" if self.inside else "content", ready))
            self.buffer = self.buffer[len(ready):]
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buffer = self.buffer, ""
        return [("reasoning" if self.inside else "content", rest)] if rest else []


class ReplayReasoning:
    """A remote provider's reasoning_details, held for the next request of one turn.

    Some providers need the reasoning that came with tool calls sent back unchanged
    with those calls (signed thinking, thought signatures). The list is kept exactly
    as received and is reachable only through `details`: it prints as a placeholder,
    and pickling or JSON-encoding it fails, so it cannot slip into a log, an event,
    a debug export or stored history by accident."""
    __slots__ = ("_details",)

    def __init__(self, details: list) -> None:
        self._details = details

    @property
    def details(self) -> list:
        return self._details

    def __repr__(self) -> str:
        return "<provider reasoning withheld>"

    __str__ = __repr__

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce_ex__(self, protocol):
        raise TypeError("provider reasoning is not serializable")


class ReasoningReplayTooLargeError(ModelLoadError):
    """A tool turn's replayed provider reasoning is past its byte cap. The turn stops
    with a plain message rather than continuing without the reasoning the provider
    needs back."""
    reason = "reasoning_replay_too_large"

    def __init__(self) -> None:
        from service.inference.remote_policy import REASONING_REPLAY_TOO_LARGE
        super().__init__(REASONING_REPLAY_TOO_LARGE)


def _replayable_details(value) -> bool:
    return type(value) is list and bool(value) and all(type(item) is dict for item in value)


def _hold_provider_reasoning(message: dict, details, *, required: bool = False) -> None:
    """A remote reply with tool calls and reasoning_details: keep the details (exactly
    as received) under the private replay key, and remove every reasoning field the
    rest of Wisp reads (structured details, native and OpenAI-style text), so none of
    it is shown, logged, remembered or stored. A malformed list is not kept at all.
    Content and tool calls are untouched and still go through the normal checks."""
    from service.inference.remote_policy import REPLAY_REASONING_KEY
    for key in ("reasoning_details", "reasoning"):
        message.pop(key, None)
    message["reasoning_content"] = ""
    if _replayable_details(details):
        message[REPLAY_REASONING_KEY] = ReplayReasoning(details)
    elif required:
        # A model that needs its reasoning back cannot continue without it.
        raise IncompleteStreamError("Inference provider returned unusable reasoning")


def replay_reasoning_details(client, message: dict) -> list | None:
    """The reasoning_details to send back with this reply's tool calls on the next
    request of the same turn: only a remote client's, only when the reply held them."""
    from service.inference.remote_policy import REPLAY_REASONING_KEY
    if getattr(client, "managed", True) is not False or not isinstance(message, dict):
        return None
    held = message.get(REPLAY_REASONING_KEY)
    return held.details if isinstance(held, ReplayReasoning) else None


def without_replay_reasoning(value):
    """A copy of a message, or a list of messages, with no held or replayed provider
    reasoning (nor the Gemini API's per-call thought signatures): for everything that
    is shown, exported or kept."""
    from service.inference.remote_policy import REPLAY_REASONING_KEY, REPLAY_TOOL_EXTRA_KEY
    if isinstance(value, list):
        return [without_replay_reasoning(item) for item in value]
    if not isinstance(value, dict):
        return value
    private = (REPLAY_REASONING_KEY, REPLAY_TOOL_EXTRA_KEY, "reasoning_details")
    calls = value.get("tool_calls")
    signed = isinstance(calls, list) and any(isinstance(c, dict) and "extra_content" in c for c in calls)
    if not signed and not any(key in value for key in private):
        return value
    out = {k: v for k, v in value.items() if k not in private}
    if signed:
        out["tool_calls"] = [{k: v for k, v in c.items() if k != "extra_content"}
                             if isinstance(c, dict) else c for c in calls]
    return out


def _hold_tool_extras(message: dict) -> None:
    """A Gemini API reply's tool calls: take each call's `extra_content` (its thought
    signature) off the call and keep the valid ones, exactly as received, under the
    private replay key, paired with their call ids. Nothing of it stays readable on
    the message itself."""
    from service.inference.remote_policy import REPLAY_TOOL_EXTRA_KEY, _valid_tool_extra
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return
    pairs = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or "extra_content" not in call:
            continue
        extra = call.pop("extra_content")
        if _valid_tool_extra(extra):
            pairs.append((index, call.get("id"), extra))
    if pairs:
        message[REPLAY_TOOL_EXTRA_KEY] = ReplayReasoning(pairs)


def replay_tool_calls(client, message: dict, tool_calls):
    """`tool_calls` for the call row the next request of this turn sends: copies of
    the calls that came with a held Gemini API `extra_content` carry it back,
    unchanged. Only a remote client's, only when the reply held one; otherwise
    `tool_calls` itself."""
    from service.inference.remote_policy import REPLAY_TOOL_EXTRA_KEY
    if (getattr(client, "managed", True) is not False or not isinstance(message, dict)
            or not isinstance(tool_calls, list)):
        return tool_calls
    held = message.get(REPLAY_TOOL_EXTRA_KEY)
    if not isinstance(held, ReplayReasoning):
        return tool_calls
    by_id = {call_id: extra for _index, call_id, extra in held.details if call_id}
    if not by_id:
        return tool_calls
    return [{**call, "extra_content": by_id[call["id"]]}
            if isinstance(call, dict) and call.get("id") in by_id else call
            for call in tool_calls]


def _gemini_wire_messages(messages):
    """The Gemini API needs a thought signature on the first call of every tool step
    of the current turn. A step whose calls carry none (one Wisp made up, or a path
    that rebuilt the calls) gets Google's documented skip value on its first call
    instead of a 400. Copies only the rows it changes."""
    from service.inference.remote_policy import GEMINI_SKIP_SIGNATURE
    out = None
    for index, row in enumerate(messages or []):
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        calls = row.get("tool_calls")
        if not isinstance(calls, list) or not calls or not isinstance(calls[0], dict):
            continue
        if any(isinstance(c, dict) and "extra_content" in c for c in calls):
            continue
        if out is None:
            out = list(messages)
        first = {**calls[0], "extra_content": {"google": {"thought_signature": GEMINI_SKIP_SIGNATURE}}}
        out[index] = {**row, "tool_calls": [first, *calls[1:]]}
    return messages if out is None else out


def _without_tool_extras(messages):
    """`messages` with no tool-call `extra_content`: never sent to another provider."""
    if not any(isinstance(row, dict) and isinstance(row.get("tool_calls"), list)
               and any(isinstance(c, dict) and "extra_content" in c for c in row["tool_calls"])
               for row in messages or []):
        return messages
    return [{**row, "tool_calls": [{k: v for k, v in c.items() if k != "extra_content"}
                                   if isinstance(c, dict) else c for c in row["tool_calls"]]}
            if isinstance(row, dict) and isinstance(row.get("tool_calls"), list) else row
            for row in messages]


def _cache_marked_system(messages):
    """OpenRouter's Anthropic models: one prompt-cache breakpoint at the end of the
    first (stable) system message, so the tools and rules before it are reused from
    the cache on every later step and turn. Copies that one row only."""
    if (not messages or not isinstance(messages[0], dict) or messages[0].get("role") != "system"
            or not isinstance(messages[0].get("content"), str) or not messages[0]["content"]):
        return messages
    marked = {**messages[0], "content": [{"type": "text", "text": messages[0]["content"],
                                          "cache_control": {"type": "ephemeral"}}]}
    return [marked, *messages[1:]]


# Under cloud-everything a provider's readiness (its model list answered with this
# key) is proven once for a few minutes, not twice before every turn's first token.
# A provider that fails meanwhile still fails the request itself, with the same
# message; invalidate_connections and a 401/403 from the provider forget the proof.
REMOTE_READY_SECONDS = 300.0
_REMOTE_READY: dict[tuple, float] = {}


# About one prompt token per 300 base64 characters of 16 kHz, 16-bit mono WAV
# (32 tokens a second of audio at the provider), with a floor for the part itself.
_AUDIO_B64_CHARS_PER_TOKEN = 300
_AUDIO_TOKEN_FLOOR = 16


def _audio_estimates(messages):
    """`messages` itself when no message carries a recording; else a copy in which each
    recording's base64 is replaced by text of about its token cost, for measuring the
    window only. The copy is never sent."""
    if not isinstance(messages, list) or not any(
            isinstance(m, dict) and isinstance(m.get("content"), list)
            and any(isinstance(p, dict) and p.get("type") == "input_audio" for p in m["content"])
            for m in messages):
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            parts = []
            for p in m["content"]:
                if isinstance(p, dict) and p.get("type") == "input_audio":
                    data = (p.get("input_audio") or {}).get("data")
                    tokens = (len(data) if isinstance(data, str) else 0) // _AUDIO_B64_CHARS_PER_TOKEN
                    p = {"type": "text", "text": "a" * (4 * (tokens + _AUDIO_TOKEN_FLOOR))}
                parts.append(p)
            m = {**m, "content": parts}
        out.append(m)
    return out


def _split_inline_thought(message: dict) -> None:
    content = message.get("content")
    if not isinstance(content, str) or _ThoughtSplitter.OPEN not in content:
        return
    splitter = _ThoughtSplitter()
    parts = splitter.feed(content) + splitter.flush()
    message["content"] = "".join(t for k, t in parts if k == "content").strip()
    thought = "".join(t for k, t in parts if k == "reasoning")
    message["reasoning_content"] = ((message.get("reasoning_content") or "") + thought).strip()

async def _power_fields(client) -> dict[str, int]:
    """The current power tier's GPU cap (service/inference/power_tier.py), after
    a fresh look at other apps' GPU use.

    Only the managed local engine gets it: a remote provider has no use for it
    and a remote-only profile refuses unknown payload keys. A module function,
    not a method, because stand-in clients borrow OMLXClient.chat.
    """
    if not getattr(client, "managed", False):
        return {}
    from service.inference.power_tier import checked_payload_fields
    return await checked_payload_fields(lambda: _lift_gpu_power(client))


async def _lift_gpu_power(client) -> bool | None:
    """Ask the bundled engine to drop its GPU power cap now, mid-request too.
    True when it did, False when it can't (an engine without the route: 404
    or 405), None when it couldn't be asked or was too busy to answer (429,
    5xx, anything else), so it is asked again. A closed client (its call has
    ended) asks nothing and raises LiftClientClosed, so another capped call's
    is asked."""
    from service.inference.power_tier import _LIFT_TIMEOUT_S, LiftClientClosed
    if getattr(getattr(client, "_client", None), "is_closed", False):
        raise LiftClientClosed
    try:
        r = await client._client.post("/v1/wisp/gpu-power/lift", timeout=_LIFT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — unreachable, or a stand-in without an engine
        return None
    if r.status_code == 200:
        return True
    return False if r.status_code in (404, 405) else None


def _power_call_started(client, payload: dict) -> tuple[bool, int | None] | None:
    """Counts calls someone is waiting on, so Brief and background calls drop
    their cap while one runs (oMLX holds the lowest cap among active requests),
    and capped calls, which power_tier watches (and lifts through this client)
    while they run."""
    if not getattr(client, "managed", False):
        return None
    from service.inference.power_tier import call_started
    return call_started(payload.get("gpu_power_limit_w"), lambda: _lift_gpu_power(client))


def _power_call_ended(started: tuple[bool, int | None] | None) -> None:
    if started is not None:
        from service.inference.power_tier import call_ended
        call_ended(started)


class OMLXClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: float = 600.0, *, target: Target | None = None) -> None:
        from service.inference.remote_policy import remote_only_profile, remote_default_target, validate_remote_target
        if remote_only_profile() is not None:
            if base_url is not None or api_key is not None:
                raise EndpointConfigurationError("Remote-only inference refuses raw URL or credential overrides")
            if target is None:
                target = remote_default_target()
            validate_remote_target(target)
        self.target = target
        ep = target.endpoint if target else (endpoint() if base_url is None else None)
        self.base_url = (ep.base_url if ep else base_url).rstrip("/")
        self.managed = ep.managed if ep else is_loopback(self.base_url)
        self.endpoint_name = ep.name if ep else ("local" if self.managed else "remote")
        from .providers import provider
        self.provider = provider(ep.provider if ep else "omlx")
        self.api_prefix = ep.api_prefix if ep else self.provider.api_prefix
        from .providers import wire_provider
        # The provider's wire format ("gemini", "openrouter", ...), told from the exact
        # origin: a remote-only profile records no provider name.
        self.wire = wire_provider(self.provider.name, self.base_url)
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
        from service.inference.remote_policy import cloud_everything_active
        # Under cloud-everything a remote https provider's connections are kept alive
        # and shared by every client in the process (one TLS handshake, not one a turn).
        # The managed engine and every other path keep their own, unshared, pool.
        shared = (not self.managed and self.base_url.startswith("https://")
                  and cloud_everything_active())
        self._credential_transport = CredentialTransport(self.base_url, api_key,
            managed=self.managed, key_loader=key_loader, shared=shared)
        self._client = guard_client(httpx.AsyncClient(
            base_url=self.base_url,
            transport=(self._credential_transport if self.managed
                       else _RateLimitRetry(self._credential_transport)),
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
        # Residency is unknown after a connection reset (engine restart path).
        with engine_epoch.operation():
            self._credential_transport.invalidate()
        key = self._ready_key() if not self.managed else None
        if key is not None:
            _REMOTE_READY.pop(key, None)

    def _check_response(self, response):
        if self.managed and response.headers.get("x-wisp-guard-refusal") == "1":
            # The authenticated local producer supplies a fixed marker. Never
            # parse/retain its body or retry a refused model operation.
            if response.status_code == 503:
                raise EngineGuardUnavailableError() from None
            if response.status_code == 507:
                raise ModelMemoryLimitError() from None
        if not self.managed and not response.is_success:
            if response.status_code in (401, 403):
                # A key the provider now refuses: health must not keep vouching for it.
                key = self._ready_key()
                if key is not None:
                    _REMOTE_READY.pop(key, None)
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

    async def _check_stream_response(self, response):
        """`_check_response` for a response opened with `stream`, whose body is still unread.

        The local engine says why it refused in a small JSON body (its prefill memory guard,
        a prompt past the context window). That body is read first, so the error raised
        carries it and `service.errors.translate` can tell the person what to do; unread, it
        could only say "try again". A remote provider's body is never read or kept
        (`_check_response`), and neither is a guard refusal's.
        """
        if (self.managed and not response.is_success
                and response.headers.get("x-wisp-guard-refusal") != "1"):
            body = bytearray()
            try:
                async for part in response.aiter_bytes():
                    body += part
                    if len(body) >= _ENGINE_ERROR_BODY_BYTES:
                        break
            except httpx.HTTPError:
                pass                    # the status is still reported, without a reason
            httpx.Response(response.status_code, content=bytes(body[:_ENGINE_ERROR_BODY_BYTES]),
                           request=response.request).raise_for_status()
        self._check_response(response)

    @staticmethod
    def _remote_limits(max_tokens: int) -> tuple[int, int]:
        output = min(REMOTE_OUTPUT_HARD_BYTES,
                     max(REMOTE_OUTPUT_MIN_BYTES, max_tokens * REMOTE_BYTES_PER_TOKEN))
        wire = min(REMOTE_WIRE_HARD_BYTES,
                   max(REMOTE_OUTPUT_MIN_BYTES, output * 2 + REMOTE_OUTPUT_MIN_BYTES,
                       max_tokens * REMOTE_WIRE_BYTES_PER_TOKEN + REMOTE_OUTPUT_MIN_BYTES))
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
        try:
            async for part in response.aiter_bytes():
                if len(body) + len(part) > maximum:
                    raise IncompleteStreamError("Remote inference response exceeded the allowed size")
                body.extend(part)
        except httpx.HTTPError:
            raise IncompleteStreamError(
                "Remote inference response ended unexpectedly") from None
        return bytes(body)

    async def _remote_lines(self, response, maximum: int):
        """Yield UTF-8 lines while bounding the entire decoded remote stream."""
        self._check_remote_headers(response, maximum)
        pending = bytearray()
        received = 0
        try:
            async for part in response.aiter_bytes():
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
        except httpx.HTTPError:
            raise IncompleteStreamError(
                "Remote inference stream ended unexpectedly") from None
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
            # before choice normalization or debug capture and retain no body,
            # only its short machine-readable code/status for diagnosis.
            err = (data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data)
            err = err.get("error") if isinstance(err, dict) else None
            tag = ""
            if isinstance(err, dict):
                code, status = err.get("code"), err.get("status")
                tag = " ".join(str(v)[:40] for v in (code, status)
                               if isinstance(v, (int, str)) and str(v).replace("_", "").isalnum())
            raise IncompleteStreamError("Inference provider returned an error"
                                        + (f" ({tag})" if tag else "")) from None
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
                if "extra_content" in call:
                    # A per-call thought signature (the Gemini API): metadata, bounded
                    # with the structured reasoning above.
                    metadata_total = cls._bounded_remote_json(
                        metadata_total, call["extra_content"], maximum)

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

    def _ready_key(self) -> tuple | None:
        """The readiness-cache key, only for a remote client under cloud-everything."""
        if self.managed or self.target is None:
            return None
        from service.inference.remote_policy import cloud_everything_active
        if not cloud_everything_active():
            return None
        ep = self.target.endpoint
        return (ep.base_url, ep.api_prefix, ep.credential_ref)

    def _recently_ready(self) -> bool:
        import time
        key = self._ready_key()
        seen = _REMOTE_READY.get(key) if key is not None else None
        return seen is not None and time.monotonic() - seen < REMOTE_READY_SECONDS

    async def _remote_models_ready(self, *, always: bool = False) -> None:
        """The remote inventory read that proves endpoint and key, skipped while a
        recent one still stands (cloud-everything only) unless `always`."""
        if not always and self._recently_ready():
            return
        await self.models()
        key = self._ready_key()
        if key is not None:
            import time
            _REMOTE_READY[key] = time.monotonic()

    async def warm(self) -> None:
        """Open (or refresh) a kept-alive connection to the remote provider before a
        turn needs it. The answer does not matter, only the connection: the Gemini
        API's short model list (which also proves the key), else a HEAD on the API
        prefix (OpenRouter's model list is large). Remote clients only. The request
        is always sent: an idle kept-alive connection expires long before the
        readiness proof does (/inference/warm rate-limits the calls)."""
        if self.managed:
            return
        if self.wire == "gemini":
            await self._remote_models_ready(always=True)
            return
        response = await self._client.request("HEAD", self.api_prefix,
                                              headers={"Accept-Encoding": "identity"})
        await response.aclose()

    async def health(self) -> dict[str, Any]:
        if not self.provider.omlx_health:
            await self._remote_models_ready()
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

    async def engine_activity(self) -> dict[str, Any]:
        """oMLX's whole-server counters (GET /api/status), for every client and
        model: active_requests and waiting_requests. Read-only."""
        if not self.provider.omlx_health:
            raise EndpointConfigurationError("Provider has no oMLX residency API")
        return await self._readiness_mapping("/api/status", 64 * 1024)

    async def unload(self, model: str) -> None:
        if not self.managed:
            raise EndpointConfigurationError("Only managed local models can be unloaded")
        with engine_epoch.operation():
            r = await self._client.post(f"/v1/models/{model}/unload")
        r.raise_for_status()

    async def load(self, model: str) -> None:
        if not self.managed:
            raise EndpointConfigurationError("Only managed local models can be loaded")
        engine_hold.check(model)       # an Ideate run holds the engine: nothing loads beside it
        with engine_epoch.operation(), idle.own_load(self.activity_key(model)):
            r = await self._client.post(f"/v1/models/{model}/load")
        if r.status_code == 507:
            # The managed engine refused admission. Do not echo its diagnostics
            # or convert a refusal into a successful residency/automatic retry.
            raise ModelMemoryLimitError() from None
        if r.status_code == 404 and await self._not_installed(model):
            # The bundled engine (oMLX 0.7.0+) answers an unknown id with 404
            # at once. Confirmed against the inventory, so an engine without
            # the load route still reports its plain HTTP error instead.
            raise ModelNotDownloadedError(await _download_progress(model)) from None
        self._check_response(r)

    async def _not_installed(self, model: str) -> bool:
        try:
            return model not in await self.models()
        except Exception:  # noqa: BLE001 - unknown: keep the original failure
            return False

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
                    # The inventory read proves the endpoint and key answer. A model
                    # missing from it is not refused: Gemini's OpenAI-compatible
                    # list leaves Gemma out though it serves it, and a model the
                    # provider truly lacks fails on the request itself.
                    await self._remote_models_ready()
                    return
                await self._ensure_managed(model, settle_timeout=settle_timeout,
                                           exclusive=exclusive, emit=emit)
        except TimeoutError as exc:
            # An engine that accepts a load for an id it doesn't have (desktop
            # oMLX) never reports it; the wait just runs out. Say what is wrong.
            if self.managed:
                import asyncio
                try:
                    async with asyncio.timeout(3):
                        missing = await self._not_installed(model)
                except TimeoutError:
                    missing = False
                if missing:
                    raise ModelNotDownloadedError(await _download_progress(model)) from exc
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
        actually happening ("Loading <catalog label>… (Ns)") so the UI can show real
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

        # An Ideate run holds the engine for its model: another model is refused here,
        # before anything is unloaded, so the run keeps its model (engine_hold.py).
        engine_hold.check(model)
        # A prewarm load in flight (service/memory_saver.py) finishes first, so this
        # doesn't POST a second load of the same model beside it.
        from service import memory_saver
        if emit and memory_saver.prewarm_pending():
            await emit({"type": "status", "text": (
                f"Loading {_model_label(model)}… The first answer after a rest takes a little longer.")})
        await memory_saver.settle_prewarm()
        loop = asyncio.get_running_loop()
        keep = {model} if exclusive else (self._keep_warm | {model})
        # On the user's oMLX.app other apps may be using what's loaded, and a turn
        # never unloads there. oMLX evicts idle models itself when a load or a prefill
        # needs the room, and refuses (507, or 400 at prefill) rather than overcommit.
        on_omlx_app = memory_saver.omlx_app(self)
        loaded = set(await self.loaded_models())

        # 1. Evict everything not in the keep set (no-op if nothing to evict).
        to_evict = set() if on_omlx_app else loaded - keep
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
                                "text": (f"Making room for {_model_label(model)}… "
                                         f"({int(loop.time() - start)}s)")})
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
        #
        # The bundled engine (oMLX 0.7.0+) now refuses an unknown id with 404
        # straight away; load() turns that into the "finish setup" message.
        # The polling fallback below still covers an engine that accepts it.
        if model not in loaded:
            label = _model_label(model)
            if emit:
                await emit({"type": "status", "text": (
                    f"Loading {label}… The first answer after a rest takes a little longer.")})
            try:
                await self.load(model)
            except ModelMemoryLimitError:
                if on_omlx_app and loaded - {model}:
                    raise ModelMemoryLimitError(_OMLX_OTHERS_FULL) from None
                raise
            start = loop.time()
            deadline = start + settle_timeout
            while loop.time() < deadline:
                loaded = set(await self.loaded_models())
                if model in loaded:
                    break
                if emit:
                    await emit({"type": "status", "text": (
                        f"Loading {label}… ({int(loop.time() - start)}s) "
                        "The first answer after a rest takes a little longer.")})
                await asyncio.sleep(1.0)
            else:
                if model not in await self.models():
                    raise ModelNotDownloadedError(await _download_progress(model))
                raise ModelLoadError(f"Wisp's model ({label}) didn't finish loading within "
                                     f"{settle_timeout:.0f} seconds. Try again in a moment.")

        # 3. Best-effort: reload any keep-warm model missing from the status we
        #    already have — no extra fetch needed to check this. Skipped when
        #    exclusive: the whole point of that mode is NOT to have the summarizer
        #    resident while this model is active (see the docstring).
        #    Nor while another model is on the user's oMLX.app: a load can evict it.
        if not exclusive and not (on_omlx_app and loaded - self._keep_warm - {model}):
            for k in self._keep_warm - loaded - {model}:
                try:
                    await self.load(k)
                except (httpx.HTTPError, ModelMemoryLimitError, EngineGuardUnavailableError):
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
        engine_headers: dict[str, str] | None = None,
        use_remaining_context: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat completion. Returns the raw oMLX response dict.

        `engine_headers` go only to the local managed engine (for example
        oMLX's `X-oMLX-Cache-Store: off`); a remote provider never sees them.
        """
        model, messages, tools, max_tokens, reasoning = self._fit_with_reasoning(
            model, messages, tools, max_tokens, use_remaining_context=use_remaining_context)
        payload = self._payload(model, messages, tools, tool_choice,
                                temperature, max_tokens, stream=False,
                                **{**(await _power_fields(self)), **extra, **reasoning})
        # A generation can make the engine load its model, so it counts as
        # engine activity for every readiness proof (see engine_epoch).
        engine_epoch.begin()
        idle.begin(self.activity_key(model))
        power_call = _power_call_started(self, payload)
        try:
            if self.managed:
                if engine_headers:
                    r = await self._client.post(self.api_prefix + "/chat/completions", json=payload,
                                                headers=engine_headers)
                else:
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
                    # A provider's reasoning that comes with tool calls (OpenRouter's
                    # reasoning_details, often with reasoning text) is never refused or
                    # shown: the tool calls and content go on through the same checks, and
                    # the details are held only for the next request of this turn
                    # (`_hold_provider_reasoning`); the text is kept nowhere.
                    held_details = message.get("reasoning_details")
                    hold_reasoning = (not self.managed and bool(held_details)
                                      and bool(message.get("tool_calls")))
                    if self.provider.name != "omlx":
                        if message.get("reasoning") and not message.get("reasoning_content"):
                            message["reasoning_content"] = message["reasoning"]
                    if not self.managed:
                        _split_inline_thought(message)
                        if self.wire == "gemini":
                            # The Gemini API's thought signatures, held for this turn only.
                            _hold_tool_extras(message)
                    if hold_reasoning:
                        _hold_provider_reasoning(message, held_details,
                                                 required=self._replays_reasoning(model))
                    if (not self.managed and choice["message"].get("tool_calls")
                            and choice.get("finish_reason") in ("stop", None)):
                        choice["finish_reason"] = "tool_calls"   # Gemini's OpenAI-compatible API
                    if choice["message"].get("tool_calls") and choice.get("finish_reason") != "tool_calls":
                        raise IncompleteStreamError("Tool generation did not finish successfully")
                    if self.managed:
                        _demote_unclosed_think(choice["message"], choice.get("finish_reason"))
            return data
        finally:
            _power_call_ended(power_call)
            idle.end(self.activity_key(model))
            engine_epoch.end()

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

        A remote model in remote_policy.REPLAY_REASONING_MODELS is not streamed: its
        reply is fetched whole (`_whole_reply_events`), so the reasoning_details it
        returns with tool calls are exactly the provider's, ready to send back.
        """
        # Under cloud-everything a call that offers no tools can return no tool calls,
        # so nothing would be replayed: it streams, even for those models. Its
        # reasoning stays unshown, as it is when the reply is fetched whole.
        whole_reply = not self.managed and self._replays_reasoning(model)
        quiet_reasoning = False
        if whole_reply and not tools:
            from service.inference.remote_policy import cloud_everything_active
            if cloud_everything_active():
                whole_reply, quiet_reasoning = False, True
        if whole_reply:
            async for event in self._whole_reply_events(
                    model, messages, tools=tools, tool_choice=tool_choice,
                    temperature=temperature, max_tokens=max_tokens,
                    use_remaining_context=use_remaining_context, **extra):
                yield event
            return
        model, messages, tools, max_tokens, reasoning = self._fit_with_reasoning(
            model, messages, tools, max_tokens,
            use_remaining_context=use_remaining_context)
        payload = self._payload(model, messages, tools, tool_choice,
                                temperature, max_tokens, stream=True,
                                **{**(await _power_fields(self)), **extra, **reasoning})
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        thought_splitter = _ThoughtSplitter()
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        done = False
        has_reasoning_details = False
        # A remote provider's streamed reasoning_details: each delta carries a list of
        # detail objects, concatenated here in arrival order, never merged or changed.
        # One malformed delta means none of it is kept for replay. Best effort only:
        # the provider documents no merge rule and may stream redacted placeholders,
        # so a replay built from a stream is not guaranteed to be accepted. Models
        # that need it reliably are fetched whole instead (see above).
        replay_details: list[dict] = []
        replay_valid = True
        output_size = 0
        metadata_size = 0
        output_limit, wire_limit = self._remote_limits(max_tokens)
        engine_epoch.begin()
        idle.begin(self.activity_key(model))
        power_call = _power_call_started(self, payload)
        try:
            headers = None if self.managed else {"Accept-Encoding": "identity"}
            async with self._client.stream("POST", self.api_prefix + "/chat/completions",
                    json=payload, headers=headers) as r:
                await self._check_stream_response(r)
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
                    if (self.managed and isinstance(chunk, dict) and "error" in chunk
                            and _stream_error_code(chunk).startswith("prefill_memory_")):
                        # The local engine's memory guard, reported inside a 200 stream. Only
                        # its code is read; its message is never kept. Nothing from this
                        # stream is executed, as for every other error below.
                        raise ModelMemoryLimitError(REQUEST_MEMORY_MESSAGE)
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
                        piece = delta["reasoning_details"]
                        if type(piece) is list and all(type(item) is dict for item in piece):
                            replay_details.extend(piece)
                        elif piece is not None:
                            replay_valid = False
                    has_reasoning_details |= bool(delta.get("reasoning_details"))
                    if (t := delta.get("reasoning_content") or (
                            delta.get("reasoning") if self.provider.name != "omlx" else None)):
                        if not self.managed:
                            output_size = self._bounded_remote_value(
                                output_size, t, output_limit)
                        # Once a remote provider sends structured reasoning, its reasoning
                        # text is neither shown nor kept: it may end in tool calls, and
                        # then only the details are held, for replay (see the end of
                        # this stream).
                        if not (has_reasoning_details and not self.managed) and not quiet_reasoning:
                            reasoning_parts.append(t)
                            yield {"kind": "reasoning", "text": t}
                    if (t := delta.get("content")):
                        if not self.managed:
                            output_size = self._bounded_remote_value(
                                output_size, t, output_limit)
                            for kind, piece in thought_splitter.feed(t):
                                (reasoning_parts if kind == "reasoning" else content_parts).append(piece)
                                yield {"kind": kind, "text": piece}
                        else:
                            content_parts.append(t)
                            yield {"kind": "content", "text": t}
                    for tc in (delta.get("tool_calls") or []):
                        index = tc.get("index")
                        if index is None:
                            # Gemini sends each call whole, without an index: a
                            # new named call is a new slot, never appended.
                            index = max(calls) if calls else 0
                            if ((tc.get("function") or {}).get("name") and calls
                                    and calls[index]["name"]):
                                index += 1
                        slot = calls.setdefault(index, {"id": "", "name": "", "arguments": "",
                                                        "extra": None})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        if not self.managed and self.wire == "gemini" and "extra_content" in tc:
                            # The call's thought signature arrives whole, on one chunk.
                            from service.inference.remote_policy import _valid_tool_extra
                            metadata_size = self._bounded_remote_json(
                                metadata_size, tc["extra_content"], output_limit)
                            if _valid_tool_extra(tc["extra_content"]):
                                slot["extra"] = tc["extra_content"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            if not self.managed:
                                output_size = self._bounded_remote_value(
                                    output_size, fn["arguments"], output_limit)
                            slot["arguments"] += fn["arguments"]
        finally:
            _power_call_ended(power_call)
            idle.end(self.activity_key(model))
            engine_epoch.end()
        for kind, piece in thought_splitter.flush():
            (reasoning_parts if kind == "reasoning" else content_parts).append(piece)
            yield {"kind": kind, "text": piece}
        if not self.managed and done and finish_reason is None:
            # Gemini can close a complete stream ([DONE]) without a finish reason.
            finish_reason = "tool_calls" if calls else "stop"
        if calls and not self.managed and finish_reason == "stop":
            finish_reason = "tool_calls"   # Gemini's OpenAI-compatible API ends tool calls with "stop"
        if finish_reason not in {"stop", "length", "tool_calls"}:
            raise IncompleteStreamError("Inference stream ended before completion; no actions were executed from it")
        if not calls and finish_reason not in {"stop", "length"}:
            raise IncompleteStreamError("Plain generation did not finish successfully")
        if calls:
            if not self.managed and has_reasoning_details:
                # Held for replay, not refused or shown: the calls below are checked
                # exactly as without it.
                reasoning_parts.clear()
            if finish_reason != "tool_calls":
                raise IncompleteStreamError("Tool generation did not finish successfully")
            seen_ids = set()
            for index, call in calls.items():
                if not call["id"] and not self.managed:
                    call["id"] = f"call_{index}"      # Gemini may omit call ids
                try:
                    args = json.loads(call["arguments"])
                except ValueError as exc:
                    raise IncompleteStreamError("Incomplete tool arguments") from exc
                if not call["id"] or call["id"] in seen_ids or not call["name"] or not isinstance(args, dict):
                    raise IncompleteStreamError("Invalid completed tool call")
                seen_ids.add(call["id"])
        named = [s for _, s in sorted(calls.items()) if s["name"]]
        tool_calls = [
            {"id": s["id"], "type": "function",
             "function": {"name": s["name"], "arguments": s["arguments"]}}
            for s in named
        ]
        final_message = {
            "role": "assistant",
            "content": "".join(content_parts),
            "reasoning_content": "".join(reasoning_parts),
            "tool_calls": tool_calls or None,
        }
        if tool_calls and not self.managed and has_reasoning_details and replay_valid:
            # Only with tool calls: a final answer ends the turn, and its details go.
            _hold_provider_reasoning(final_message, replay_details)
        if tool_calls and not self.managed and any(s["extra"] is not None for s in named):
            from service.inference.remote_policy import REPLAY_TOOL_EXTRA_KEY
            final_message[REPLAY_TOOL_EXTRA_KEY] = ReplayReasoning(
                [(i, s["id"], s["extra"]) for i, s in enumerate(named) if s["extra"] is not None])
        # Same correction as `chat()`. Live deltas for this case have already
        # gone out (nothing can un-send them), but every consumer of the FINAL
        # message — the agent loop's empty-content retry, main.py's
        # reasoning-as-answer fallback, session persistence — must not treat a
        # truncated monologue as the turn's answer, or it gets replayed as
        # assistant history on the next turn.
        if self.managed:
            _demote_unclosed_think(final_message, finish_reason)
        # Cut off by max_tokens before any answer: lets the agent loop tell a
        # budget exhaustion (a retry reproduces it) from an unparseable reply.
        if finish_reason == "length" and not tool_calls and not final_message["content"].strip():
            final_message["_truncated"] = True
        yield {"kind": "final", "message": final_message}

    def activity_key(self, model: str) -> str:
        return model if self.managed else f"{self.endpoint_name}:{self.base_url}:{model}"

    def _replays_reasoning(self, model: str) -> bool:
        from service.inference.remote_policy import REPLAY_REASONING_MODELS
        return not self.managed and model in REPLAY_REASONING_MODELS

    async def _whole_reply_events(self, model, messages, *, tools, tool_choice, temperature,
                                  max_tokens, use_remaining_context, **extra):
        """`stream_events` for a model whose reasoning must be replayed: one
        non-streaming request (`chat`, with its size limits and checks), then the
        same events a stream ends with. Only the reply's content is yielded, never its
        reasoning; the final message holds the reply's reasoning_details (exactly as
        parsed from that one body) under the private replay key when it has tool
        calls. The calls get the same checks as streamed ones."""
        from service.inference.remote_policy import REPLAY_REASONING_KEY, REPLAY_TOOL_EXTRA_KEY
        data = await self.chat(model, messages, tools=tools, tool_choice=tool_choice,
                               temperature=temperature, max_tokens=max_tokens,
                               use_remaining_context=use_remaining_context, **extra)
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason")
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise IncompleteStreamError("Invalid inference response data")
        raw_calls = message.get("tool_calls") or []
        if finish_reason is None:
            finish_reason = "tool_calls" if raw_calls else "stop"
        if finish_reason not in {"stop", "length", "tool_calls"}:
            raise IncompleteStreamError("Inference stream ended before completion; no actions were executed from it")
        if not raw_calls and finish_reason not in {"stop", "length"}:
            raise IncompleteStreamError("Plain generation did not finish successfully")
        tool_calls = []
        seen_ids = set()
        for index, call in enumerate(raw_calls):
            fn = call.get("function") if isinstance(call, dict) else None
            if not isinstance(fn, dict):
                raise IncompleteStreamError("Invalid completed tool call")
            call_id = call.get("id") or f"call_{index}"      # some providers omit call ids
            name, arguments = fn.get("name"), fn.get("arguments")
            if not isinstance(arguments, str):
                raise IncompleteStreamError("Incomplete tool arguments")
            try:
                args = json.loads(arguments)
            except ValueError as exc:
                raise IncompleteStreamError("Incomplete tool arguments") from exc
            if (not isinstance(call_id, str) or call_id in seen_ids or not isinstance(name, str)
                    or not name or not isinstance(args, dict)):
                raise IncompleteStreamError("Invalid completed tool call")
            seen_ids.add(call_id)
            tool_calls.append({"id": call_id, "type": "function",
                               "function": {"name": name, "arguments": arguments}})
        if content:
            yield {"kind": "content", "text": content}
        final_message = {"role": "assistant", "content": content, "reasoning_content": "",
                         "tool_calls": tool_calls or None}
        if tool_calls and isinstance(message.get(REPLAY_REASONING_KEY), ReplayReasoning):
            final_message[REPLAY_REASONING_KEY] = message[REPLAY_REASONING_KEY]
        if tool_calls and isinstance(message.get(REPLAY_TOOL_EXTRA_KEY), ReplayReasoning):
            # Paired by position; a call whose id was filled in here takes that id.
            final_message[REPLAY_TOOL_EXTRA_KEY] = ReplayReasoning(
                [(i, tool_calls[i]["id"], extra) for i, _id, extra in message[REPLAY_TOOL_EXTRA_KEY].details
                 if i < len(tool_calls)])
        if finish_reason == "length" and not tool_calls and not content.strip():
            final_message["_truncated"] = True
        yield {"kind": "final", "message": final_message}

    def _check_replay_budget(self, messages) -> None:
        """Refuse, with a plain message, a request whose replayed reasoning is past its
        cap: the profile's response_bytes, or the hard output ceiling without one.
        Never sent without it instead: the provider needs it back."""
        if self.managed:
            return
        from service.inference.remote_policy import remote_only_profile, replay_reasoning_bytes
        try:
            size = replay_reasoning_bytes(messages)
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise EndpointConfigurationError("Invalid replayed provider reasoning") from None
        profile = remote_only_profile()
        if size > (profile.response_bytes if profile is not None else REMOTE_OUTPUT_HARD_BYTES):
            raise ReasoningReplayTooLargeError()

    def _fit_with_reasoning(self, model, messages, tools, max_tokens, *,
                            use_remaining_context: bool = False):
        """`_fit_request`, plus the remote profile's reasoning option when its model
        has one (remote_policy.REASONING_EFFORT_MODELS: `reasoning` on OpenRouter,
        `reasoning_effort` on the Gemini API), plus the provider's wire shape
        (`_wire_messages`, `_wire_fields`).

        The provider gives reasoning half of max_tokens (at least 1024) and needs
        max_tokens above that, so a reasoning request carries at least
        REASONING_MIN_MAX_TOKENS. A request asking for that much or more simply gets
        the option. A smaller one that is part of a tool turn (tools offered, or a
        transcript with tool calls or results) is raised to that floor, so every step
        of the turn reasons alike; if the raised request does not fit, it is sent as
        asked, without reasoning. A small tool-free call (a connection probe, a title,
        a background one-liner) keeps its budget and goes without reasoning: raising
        it would only add latency and cost to a call that needs a few words."""
        model, messages, tools, max_tokens, fields = self._fit_reasoning_only(
            model, messages, tools, max_tokens, use_remaining_context=use_remaining_context)
        return (model, self._wire_messages(model, messages), tools, max_tokens,
                {**fields, **self._wire_fields()})

    def _wire_fields(self) -> dict:
        """Provider options every request of this client carries: OpenRouter's
        latency-first provider routing, under cloud-everything only."""
        from service.inference.remote_policy import OPENROUTER_PROVIDER_ROUTING, cloud_everything_active
        if (not self.managed and getattr(self, "wire", None) == "openrouter"
                and cloud_everything_active()):
            return {"provider": dict(OPENROUTER_PROVIDER_ROUTING)}
        return {}

    def _wire_messages(self, model, messages):
        """The request's messages in this provider's wire shape (copies of changed
        rows only): the Gemini API's thought signatures on its tool steps, never on
        another provider's; one prompt-cache mark for OpenRouter's Anthropic models
        under cloud-everything. A managed client's messages go unchanged."""
        if self.managed:
            return messages
        wire = getattr(self, "wire", None)
        if wire == "gemini":
            return _gemini_wire_messages(messages)
        messages = _without_tool_extras(messages)
        if wire == "openrouter" and isinstance(model, str) and model.startswith("anthropic/"):
            from service.inference.remote_policy import cloud_everything_active
            if cloud_everything_active():
                return _cache_marked_system(messages)
        return messages

    def _fit_reasoning_only(self, model, messages, tools, max_tokens, *,
                            use_remaining_context: bool = False):
        from service.inference.remote_policy import (REASONING_MIN_MAX_TOKENS, reasoning_fields,
                                                     remote_only_profile)
        self._check_replay_budget(messages)
        profile = remote_only_profile()
        option = (reasoning_fields(getattr(self, "wire", profile.wire), profile.model_id) or None
                  if profile is not None and not self.managed and self.target is not None
                  and model == profile.model_id else None)
        if option is not None and profile.max_tokens >= REASONING_MIN_MAX_TOKENS:
            asked = max_tokens
            if type(max_tokens) is int and 0 < max_tokens < REASONING_MIN_MAX_TOKENS:
                tool_turn = bool(tools) or any(
                    isinstance(m, dict) and (m.get("tool_calls") or m.get("role") == "tool")
                    for m in messages or [])
                if tool_turn:
                    asked = REASONING_MIN_MAX_TOKENS
            if type(asked) is int and asked >= REASONING_MIN_MAX_TOKENS:
                try:
                    fitted = self._fit_request(model, messages, tools, asked,
                                               use_remaining_context=use_remaining_context)
                except EndpointConfigurationError:
                    fitted = None
                if fitted is not None and fitted[3] >= REASONING_MIN_MAX_TOKENS:
                    import copy
                    return (*fitted, copy.deepcopy(option))
        return (*self._fit_request(model, messages, tools, max_tokens,
                                   use_remaining_context=use_remaining_context), {})

    def _fit_request(self, model, messages, tools, max_tokens, *,
                     use_remaining_context: bool = False):
        from service.inference.remote_policy import remote_only_profile, validate_remote_target
        remote_profile = remote_only_profile()
        if remote_profile is not None:
            validate_remote_target(self.target)
            max_tokens = remote_profile.fit_output(max_tokens)
        if self.target is None:
            return model, messages, tools, max_tokens
        if model != self.target.model:
            raise EndpointConfigurationError("A bound inference client cannot change model identity")
        if tools and not self.managed and "tools" not in self.target.capabilities:
            raise EndpointConfigurationError("Remote tool calling has not been qualified for this target")
        from service.agent.loop import _fit_window, _est_tokens
        fit_options = ({"min_output_tokens": 1}
                       if self.endpoint_name == "local_provider" else {})
        original_messages = messages
        # A recording's base64 is not prompt text: the window is measured with it
        # stood in for by its rough token cost, and the real request is sent.
        estimated = _audio_estimates(messages)
        messages, fitted_tools, max_tokens = _fit_window(
            estimated, tools or [], max_tokens, model, None,
            context_window=self.target.context_window,
            protected_prefix_count=next((i for i, m in enumerate(estimated)
                                         if m.get("role") != "system"), len(estimated)),
            **fit_options)
        if estimated is not original_messages and messages != estimated:
            # A request carrying a recording is sent whole or not at all.
            raise EndpointConfigurationError("Request cannot fit the target context window")
        if remote_profile is not None and messages != estimated:
            raise EndpointConfigurationError("Remote-only context cannot discard selected messages or tool results")
        # Never silently drop required tool schemas or send an overfull prompt.
        if len(fitted_tools) != len(tools or []):
            raise EndpointConfigurationError("Target context is too small for the requested tools")
        cost = sum(_est_tokens(m.get("content") or "") + _est_tokens(m.get("tool_calls") or [])
                   for m in messages) + _est_tokens(fitted_tools)
        if estimated is not original_messages:
            messages = original_messages
        if remote_profile is None and use_remaining_context and not self.managed and self.endpoint_name != "local_provider":
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
        from service.inference.remote_policy import remote_only_profile
        remote_profile = remote_only_profile()
        if remote_profile is not None:
            remote_profile.validate_payload(payload)
        return payload
