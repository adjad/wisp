"""Process-pinned, opt-in remote-inference policy; never resolves credentials.

Remote inference sends every generation role to ONE user-configured
OpenAI-compatible server while data, tools and approvals stay on the Mac. It is
off unless the process is launched with ``WISP_REMOTE_ONLY=1`` AND the user's
config (``~/.moe/config.yaml``, or ``$WISP_HOME/config.yaml``) carries a
complete ``inference.remote_only`` profile. Wisp ships no endpoint of its own:
a profile alone never moves inference off the Mac, and the launch flag without
a valid profile refuses to start. Settings cannot change the snapshot after
startup.

Profile (see docs/REMOTE_INFERENCE.md)::

    inference:
      remote_only:
        base_url: https://inference.example      # https origin, no path or slash
        model_id: example-model                  # sent as-is for every role
        credential_ref: keychain:example         # Keychain item, never a token
        context_window: 32000
        max_tokens: 8192
        request_bytes: 524288
        messages: 200
        aggregate_bytes: 131072
        tools: 32
        response_bytes: 4194304
        # optional: api_prefix (/v1), readiness_timeout (5, max 30), enabled (true)

The mode is read/write: every built-in tool that does not need a local model
or a Wisp-local extension runtime is offered, each decided by the normal
safety policy (service/safety/policy.py and grants.py) exactly as on a Mac
with local models: sends always confirm and are never grantable, writes are
once/always/never. Read-only use is Wisp's existing View Only mode, which
keeps denying writes here. The pinned origin, model, Keychain-only credential
and payload validation apply throughout.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import logging
import math
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from service.config.endpoints import Target

_FORCED_MODE = os.environ.get("WISP_REMOTE_ONLY")
# The retired WISP_REMOTE_FULL flag: the mode it selected is now the only one.
# Read once at import; never fatal, noted once in the log when a profile loads.
_RETIRED_FULL_FLAG = os.environ.get("WISP_REMOTE_FULL")
_log = logging.getLogger(__name__)
# Built-ins that remote mode never offers or runs. Each needs something
# this build does not have: create_tool generates code with the local coding
# model and installs it; use_skill/wisp_skills/wisp_mcp serve the Wisp-local
# skill and MCP runtimes, which never start in a remote-only build (and their
# content must not leave the Mac). Everything else is left to the normal
# safety policy, never to this list. Email attachments stay too: their contents
# must not leave the Mac.
_REMOTE_EXCLUDED = frozenset({"create_tool", "use_skill", "wisp_skills", "wisp_mcp",
                              "read_email_attachments", "read_message_attachments"})

# Every limit the user's profile must state, with the range Wisp accepts.
LIMITS = {
    "context_window": (2048, 1_048_576),
    "max_tokens": (1, 131_072),
    "request_bytes": (4096, 16 * 1024 * 1024),
    "messages": (1, 1000),
    "aggregate_bytes": (1024, 16 * 1024 * 1024),
    "tools": (1, 128),
    "response_bytes": (1024, 64 * 1024 * 1024),
}
REQUIRED_KEYS = frozenset({"base_url", "model_id", "credential_ref", *LIMITS})
OPTIONAL_KEYS = frozenset({"enabled", "api_prefix", "readiness_timeout"})
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
_BASE_URL_CHARS = re.compile(r"[\x21-\x7e]+")
_CREDENTIAL_REF = re.compile(r"keychain:[A-Za-z][A-Za-z0-9_-]{0,63}")
# The only models a request may ask to reason, and the one effort each gets. The
# value is sent as the top-level `reasoning` option; it comes from this constant
# alone, never from a prompt, a tool result or a model reply.
# Gemini always reasons; "low" bounds it so agent steps stay quick and leave room
# for the answer inside max_tokens.
# On OpenRouter the option is `reasoning: {effort}`; on the Gemini API (bare ids, see
# GEMINI_API_MODELS) it is `reasoning_effort` (see reasoning_fields). Gemini 3 and 4
# cannot turn thinking off there, so "low" is the least it can be asked for.
REASONING_EFFORT_MODELS = {
    "anthropic/claude-haiku-5.5": "medium",
    "google/gemini-3.8-flash": "low",
    "google/gemini-4-carbon": "low",
    "google/gemini-4-argon": "low",
    "gemini-3.8-flash": "low",
    "gemini-4-carbon": "low",
    "gemini-4-argon": "low",
}
# The models the app offers on the Gemini API itself, by their bare ids. They get the
# Gemini API's options only, and only on its origin; OpenRouter's ids never do.
GEMINI_API_MODELS = frozenset({"gemini-3.8-flash", "gemini-4-carbon", "gemini-4-argon"})
# The entries that apply on every remote-only path: the one that predates
# cloud-everything, and the Gemini API's own ids (that wire always sends its effort).
# OpenRouter's Gemini entries apply only under cloud-everything, so the
# WISP_REMOTE_ONLY launcher path without remote_parity sends what it did before.
_UNGATED_REASONING_MODELS = frozenset({"anthropic/claude-haiku-5.5", *GEMINI_API_MODELS})
# The provider sets the reasoning budget to half of max_tokens (at least 1024) and
# requires max_tokens strictly above it, so a reasoning request needs at least this.
REASONING_MIN_MAX_TOKENS = 2049
# Where the inference client keeps a reply's reasoning_details for the next request
# of the same turn. Private: it is never sent under this name, shown or stored.
REPLAY_REASONING_KEY = "_replay_reasoning_details"
# Models whose reasoning_details must go back unchanged with their tool calls, and
# that always (or, with the option above, usually) send them. Their replies are
# fetched whole, never streamed: a streamed reasoning sequence is only the chunks
# in arrival order and may hold redacted placeholders, so it cannot be replayed
# reliably. Every other remote model streams as before.
REPLAY_REASONING_MODELS = frozenset({
    "google/gemini-3.8-flash", "google/gemini-4-carbon", "google/gemini-4-argon",
    "anthropic/claude-haiku-5.5",
})
# Where the inference client keeps the Gemini API's per-call `extra_content` (its
# thought signatures) for the next request of the same turn. Private like the key above.
# The Gemini API's replies are streamed: each signature arrives whole on its call.
REPLAY_TOOL_EXTRA_KEY = "_replay_tool_call_extra"
# Google's documented stand-in signature for a function call the model did not make
# (one Wisp synthesizes, or one whose signature was lost): it tells the API to skip
# validating that step instead of refusing the whole request.
GEMINI_SKIP_SIGNATURE = "skip_thought_signature_validator"
# OpenRouter's provider routing under cloud-everything: the fastest-answering host.
OPENROUTER_PROVIDER_ROUTING = {"sort": "latency"}
REASONING_REPLAY_TOO_LARGE = ("This model's reasoning is too large to continue the answer; "
                              "try a shorter request or another model")

# What each offered cloud model takes as input, from OpenRouter's public model list
# (input_modalities, read 2026-11-13). The one place Wisp decides whether a picture
# or a recording may go to the chosen model; a model not listed is text only.
REMOTE_INPUT_MODALITIES = {
    "google/gemini-3.8-flash": frozenset({"text", "image", "video", "file", "audio"}),
    "google/gemini-4-carbon": frozenset({"text", "image", "video", "file", "audio"}),
    "google/gemini-4-argon": frozenset({"text", "image", "video", "file", "audio"}),
    "anthropic/claude-haiku-5.5": frozenset({"text", "image", "file"}),
    "anthropic/claude-sonnet-5.5": frozenset({"text", "image", "file"}),
    "openai/gpt-6-luna": frozenset({"text", "image", "file"}),
    # The same models on the Gemini API (bare ids).
    "gemini-3.8-flash": frozenset({"text", "image", "video", "file", "audio"}),
    "gemini-4-carbon": frozenset({"text", "image", "video", "file", "audio"}),
    "gemini-4-argon": frozenset({"text", "image", "video", "file", "audio"}),
}
_TEXT_ONLY = frozenset({"text"})
# The context window Wisp uses for each offered cloud model: OpenRouter lists every
# one at 1,000,000 tokens or more. Mirrors IntelCloudPreset.contextWindows in the
# app. An unlisted model keeps the window it was saved with.
REMOTE_CONTEXT_WINDOWS = {
    "google/gemini-3.8-flash": 1_000_000,
    "google/gemini-4-carbon": 1_000_000,
    "google/gemini-4-argon": 1_000_000,
    "anthropic/claude-haiku-5.5": 1_000_000,
    "anthropic/claude-sonnet-5.5": 1_000_000,
    "openai/gpt-6-luna": 1_000_000,
    "gemini-3.8-flash": 1_000_000,
    "gemini-4-carbon": 1_000_000,
    "gemini-4-argon": 1_000_000,
}
# The window older Intel builds saved for every model.
LEGACY_CLOUD_CONTEXT_WINDOW = 32_000
# Pictures and recordings in a request (cloud-everything mode only, see
# validate_payload). Each picture is a base64 data URL of a PNG, JPEG or WebP, never
# a link; each recording is base64 WAV or MP3. Their encoded bytes are left out of
# the transcript budget (aggregate_bytes) and counted against REMOTE_MEDIA_TOTAL_BYTES
# instead, so a screenshot never crowds out the conversation and the whole body stays
# below request_bytes + replayed reasoning (response_bytes) + REMOTE_MEDIA_TOTAL_BYTES.
REMOTE_IMAGE_MAX_BYTES = 5 * 1024 * 1024        # decoded, per picture
REMOTE_IMAGES_PER_REQUEST = 9                   # a large screen: overview plus 8 sections
REMOTE_AUDIO_MAX_BYTES = 10 * 1024 * 1024       # decoded, per recording (30 s WAV is ~1 MB)
REMOTE_AUDIO_PER_REQUEST = 1
REMOTE_AUDIO_FORMATS = frozenset({"wav", "mp3"})
REMOTE_MEDIA_TOTAL_BYTES = 16 * 1024 * 1024     # encoded, every picture and recording together
# The Gemini API's own cap on one inline request (prompt, history and every picture
# or recording together), with a little room under Google's 20 MB.
GEMINI_INLINE_REQUEST_BYTES = 19 * 1024 * 1024
GEMINI_REQUEST_TOO_LARGE = ("These pictures and recordings are too large to send to Gemini in one "
                            "request. Send fewer or smaller ones, or start a new chat.")
REMOTE_CONTENT_PARTS = 32
# The least output a picture, recording or document call asks the cloud model for:
# the default model always reasons first, and its reasoning counts against max_tokens,
# so the few hundred tokens a local model needs would end the reply before it starts.
REMOTE_MEDIA_MIN_TOKENS = 4096
_IMAGE_DATA_URL = re.compile(r"data:image/(?:png|jpeg|webp);base64,")
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


def remote_input_modalities(model_id) -> frozenset:
    """What `model_id` takes as input; text only for a model not in the table."""
    return REMOTE_INPUT_MODALITIES.get(model_id, _TEXT_ONLY) if isinstance(model_id, str) else _TEXT_ONLY


def remote_context_window(model_id) -> int | None:
    """The window Wisp uses for an offered cloud model, or None for an unlisted one."""
    return REMOTE_CONTEXT_WINDOWS.get(model_id) if isinstance(model_id, str) else None


def cloud_everything_active() -> bool:
    """All of Wisp runs on the cloud model (remote-only profile plus remote_parity).
    The on-device limits that only protect a small local model are lifted under it."""
    try:
        return remote_only_profile() is not None and remote_parity()
    except Exception:  # noqa: BLE001 — an unreadable profile keeps the local limits
        return False


def cloud_output_budget(asked: int) -> int:
    """max_tokens for a short text call (a summary, a JSON plan). Under
    cloud-everything it leaves room for the model's reasoning, which counts
    inside max_tokens; elsewhere it is `asked` unchanged."""
    return cloud_media_budget(asked) if cloud_everything_active() else asked


# The cloud model writes about 150 tokens a second; reasoning counts as output.
CLOUD_TOKENS_PER_SECOND = 150
_CLOUD_NETWORK_S = 15.0


# Reading the prompt (prefill) takes about a second per 25k input tokens (~4
# characters a token); a huge input (a whole document) adds at most this much.
CLOUD_PREFILL_TOKENS_PER_SECOND = 25_000
_CLOUD_CHARS_PER_TOKEN = 4
CLOUD_PREFILL_MAX_S = 120.0


def cloud_call_timeout(local_s: float, out_tokens: int, *, input_chars: int = 0) -> float:
    """A timeout for a call asking `out_tokens` from a prompt of `input_chars`
    characters: `local_s` unchanged, except under cloud-everything, where it covers
    reading the prompt (CLOUD_PREFILL_TOKENS_PER_SECOND, at most CLOUD_PREFILL_MAX_S),
    writing every output token, plus the network."""
    if not cloud_everything_active():
        return local_s
    chars = input_chars if type(input_chars) is int and input_chars > 0 else 0
    prefill = min(CLOUD_PREFILL_MAX_S,
                  chars / _CLOUD_CHARS_PER_TOKEN / CLOUD_PREFILL_TOKENS_PER_SECOND)
    return max(local_s, out_tokens / CLOUD_TOKENS_PER_SECOND + prefill + _CLOUD_NETWORK_S)


def cloud_media_allowed(kind: str) -> bool:
    """Whether a `kind` ("image" or "audio") may go to the cloud model right now:
    a remote-only profile is active, the owner chose cloud-everything mode
    (remote_parity) and the profile's model takes that input."""
    profile = remote_only_profile()
    return (profile is not None and remote_parity()
            and kind in remote_input_modalities(profile.model_id))


def cloud_media_budget(asked: int) -> int:
    """max_tokens for a picture, recording or document call on the cloud model."""
    profile = remote_only_profile()
    if profile is None:
        return asked
    return min(profile.max_tokens, max(asked, REMOTE_MEDIA_MIN_TOKENS))


def _b64_size(data, cap: int) -> int:
    """Decoded size of strict base64 `data` (at most `cap`), or refuse. Never logs it."""
    if type(data) is not str or not data or len(data) % 4:
        _refuse("Invalid remote-only media part")
    if len(data) > (cap * 4) // 3 + 4:
        _refuse("A picture or recording is too large for the cloud model")
    if not _BASE64.fullmatch(data):
        _refuse("Invalid remote-only media part")
    size = len(data) // 4 * 3 - (len(data) - len(data.rstrip("=")))
    if not 0 < size <= cap:
        _refuse("A picture or recording is too large for the cloud model")
    return size


def _content_parts(parts, model_id: str, counts: dict | None = None) -> int:
    """Check one user message's content parts; return the encoded media bytes in it.
    `counts` carries the picture and recording totals across the whole request."""
    if not remote_parity():
        _refuse("Remote-only inference accepts text and tool transcripts only")
    if type(parts) is not list or not 1 <= len(parts) <= REMOTE_CONTENT_PARTS:
        _refuse("Invalid remote-only content parts")
    accepts = remote_input_modalities(model_id)
    counts = {"images": 0, "audio": 0} if counts is None else counts
    media = 0
    images, audio = counts["images"], counts["audio"]
    for part in parts:
        kind = part.get("type") if type(part) is dict else None
        if kind == "text":
            if set(part) != {"type", "text"} or type(part["text"]) is not str:
                _refuse("Invalid remote-only content parts")
        elif kind == "image_url":
            if "image" not in accepts:
                _refuse("The chosen cloud model can't read pictures")
            inner = part.get("image_url")
            if (set(part) != {"type", "image_url"} or type(inner) is not dict
                    or set(inner) != {"url"} or type(inner["url"]) is not str):
                _refuse("Invalid remote-only content parts")
            head = _IMAGE_DATA_URL.match(inner["url"])
            if head is None:
                _refuse("Remote-only pictures must be PNG, JPEG or WebP data, never a link")
            images += 1
            if images > REMOTE_IMAGES_PER_REQUEST:
                _refuse("Too many pictures for one cloud request")
            _b64_size(inner["url"][head.end():], REMOTE_IMAGE_MAX_BYTES)
            media += len(inner["url"])
        elif kind == "input_audio":
            if "audio" not in accepts:
                _refuse("The chosen cloud model can't hear audio")
            inner = part.get("input_audio")
            if (set(part) != {"type", "input_audio"} or type(inner) is not dict
                    or set(inner) != {"data", "format"} or inner.get("format") not in REMOTE_AUDIO_FORMATS):
                _refuse("Invalid remote-only content parts")
            audio += 1
            if audio > REMOTE_AUDIO_PER_REQUEST:
                _refuse("Too many recordings for one cloud request")
            _b64_size(inner["data"], REMOTE_AUDIO_MAX_BYTES)
            media += len(inner["data"])
        else:
            _refuse("Unsupported remote-only content part")
    counts["images"], counts["audio"] = images, audio
    return media


def _without_media(row: dict) -> dict:
    """`row` with every picture and recording payload emptied, for the transcript budget."""
    content = row.get("content")
    if type(content) is not list:
        return row
    out = []
    for part in content:
        if part.get("type") == "image_url":
            part = {"type": "image_url", "image_url": {"url": ""}}
        elif part.get("type") == "input_audio":
            part = {"type": "input_audio", "input_audio": {"data": "", "format": part["input_audio"]["format"]}}
        out.append(part)
    return {**row, "content": out}


def reasoning_option(model_id: str) -> dict | None:
    """The `reasoning` option for this model, or None when it gets none. OpenRouter's
    Gemini entries apply only under cloud-everything (_UNGATED_REASONING_MODELS)."""
    effort = REASONING_EFFORT_MODELS.get(model_id)
    if effort and model_id not in _UNGATED_REASONING_MODELS and not cloud_everything_active():
        return None
    return {"effort": effort} if effort else None


def reasoning_fields(wire: str, model_id: str) -> dict:
    """The top-level reasoning option a request to `model_id` over `wire` carries:
    `reasoning_effort` on the Gemini API (its bare ids only), `reasoning: {effort}`
    elsewhere (never for a bare Gemini API id), or nothing."""
    option = reasoning_option(model_id)
    if option is None:
        return {}
    if wire == "gemini":
        return {"reasoning_effort": option["effort"]} if model_id in GEMINI_API_MODELS else {}
    return {} if model_id in GEMINI_API_MODELS else {"reasoning": option}


def _tool_extras(row) -> list:
    """The `extra_content` objects on an assistant row's tool calls, in order."""
    calls = row.get("tool_calls") if isinstance(row, dict) else None
    if not isinstance(calls, list):
        return []
    return [call["extra_content"] for call in calls
            if isinstance(call, dict) and "extra_content" in call]


def replay_reasoning_bytes(messages) -> int:
    """Wire bytes of every replayed reasoning_details list and every replayed tool-call
    `extra_content` (the Gemini API's thought signatures) in `messages`, encoded as
    the request body encodes them. Raises TypeError/ValueError for anything that
    cannot be sent as JSON."""
    total = 0
    for row in messages or []:
        if isinstance(row, dict) and "reasoning_details" in row:
            total += len(json.dumps(row["reasoning_details"], ensure_ascii=False,
                                    allow_nan=False).encode("utf-8"))
        for extra in _tool_extras(row):
            total += len(json.dumps(extra, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return total


def _valid_tool_extra(extra) -> bool:
    """The Gemini API's per-call `extra_content`: {"google": {...}} whose thought
    signature, when present, is a non-empty string. Nothing else is sent back."""
    if type(extra) is not dict or set(extra) != {"google"} or type(extra["google"]) is not dict:
        return False
    signature = extra["google"].get("thought_signature")
    return signature is None or (type(signature) is str and bool(signature))


def _without_replay(row: dict) -> dict:
    """`row` without its replayed reasoning or tool-call extras, for the transcript budget."""
    if "reasoning_details" in row:
        row = {k: v for k, v in row.items() if k != "reasoning_details"}
    if _tool_extras(row):
        row = {**row, "tool_calls": [{k: v for k, v in call.items() if k != "extra_content"}
                                     if isinstance(call, dict) else call
                                     for call in row["tool_calls"]]}
    return row


def _refuse(message: str):
    from service.config.endpoints import EndpointConfigurationError
    raise EndpointConfigurationError(message)


@dataclass(frozen=True)
class RemoteOnlyProfile:
    """One validated remote-inference profile. No field has a built-in
    endpoint, model or credential: they come only from the user's config."""
    base_url: str
    model_id: str
    credential_ref: str
    context_window: int
    max_tokens: int
    request_bytes: int
    messages: int
    aggregate_bytes: int
    tools: int
    response_bytes: int
    api_prefix: str = "/v1"
    readiness_timeout: float = 5.0
    endpoint_name: str = "remote_only"
    provider: str = "openai-compatible"
    generation_roles: tuple[str, ...] = ("fast", "router", "agent", "general", "reasoning", "coding")
    tool_capabilities: tuple[str, ...] = ("tools",)

    @property
    def wire(self) -> str:
        """The provider wire format, told from the exact origin (providers.wire_provider)."""
        from service.inference.providers import wire_provider
        return wire_provider(self.provider, self.base_url)

    def fit_output(self, value: int) -> int:
        if type(value) is not int or value <= 0:
            _refuse("Remote output budget must be a positive integer")
        return min(value, self.max_tokens)

    def validate_payload(self, payload: dict) -> None:
        wire = self.wire
        allowed = {"model", "messages", "max_tokens", "stream", "temperature", "tools", "tool_choice"}
        # Each provider's own options, on that provider only.
        allowed |= {"reasoning_effort"} if wire == "gemini" else {"reasoning"}
        if wire == "openrouter":
            allowed.add("provider")
        if not isinstance(payload, dict) or set(payload) - allowed:
            _refuse("Unsupported remote-only inference options")
        if payload.get("model") != self.model_id or type(payload.get("stream")) is not bool:
            _refuse("Remote-only inference requires its exact model and stream flag")
        value = payload.get("max_tokens")
        if type(value) is not int or not 1 <= value <= self.max_tokens:
            _refuse("Remote-only output budget exceeds the profile limit")
        if "reasoning" in payload:
            # Exactly the constant's option for this model, with room for its budget.
            wanted = reasoning_fields(wire, payload["model"]).get("reasoning")
            got = payload["reasoning"]
            if (wanted is None or type(got) is not dict or set(got) != {"effort"}
                    or type(got["effort"]) is not str or got != wanted
                    or value < REASONING_MIN_MAX_TOKENS):
                _refuse("Unsupported remote-only reasoning option")
        if "reasoning_effort" in payload:
            # The Gemini API's form of the same constant, for its bare ids only.
            wanted = reasoning_fields(wire, payload["model"]).get("reasoning_effort")
            got = payload["reasoning_effort"]
            if wanted is None or type(got) is not str or got != wanted or value < REASONING_MIN_MAX_TOKENS:
                _refuse("Unsupported remote-only reasoning option")
        if "provider" in payload and payload["provider"] != OPENROUTER_PROVIDER_ROUTING:
            _refuse("Unsupported remote-only provider routing")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= self.messages:
            _refuse("Remote-only message budget exceeded")
        media_bytes = 0
        media_counts = {"images": 0, "audio": 0}
        for row in messages:
            if not isinstance(row, dict) or not isinstance(row.get("role"), str):
                _refuse("Invalid remote-only message")
            if row["role"] not in {"system", "user", "assistant", "tool"}:
                _refuse("Unsupported remote-only message role")
            content = row.get("content")
            if (row["role"] == "system" and type(content) is list and wire == "openrouter"
                    and self.model_id.startswith("anthropic/")):
                # One prompt-cache breakpoint on a system message's text (OpenRouter's
                # Anthropic models): exactly one text part marked ephemeral, no more.
                part = content[0] if len(content) == 1 else None
                if (type(part) is not dict or set(part) != {"type", "text", "cache_control"}
                        or part["type"] != "text" or type(part["text"]) is not str
                        or part["cache_control"] != {"type": "ephemeral"}):
                    _refuse("Invalid remote-only system message")
            elif row["role"] == "user" and isinstance(content, list):
                # Pictures and recordings: cloud-everything mode, a model that takes
                # them, data only, bounded (see REMOTE_INPUT_MODALITIES and the caps).
                media_bytes += _content_parts(content, self.model_id, media_counts)
                if media_bytes > REMOTE_MEDIA_TOTAL_BYTES:
                    _refuse("The pictures and recordings are too large for one cloud request")
            elif not isinstance(content, str) and not (row["role"] == "assistant" and row.get("tool_calls") and content is None):
                _refuse("Remote-only inference accepts text and tool transcripts only")
            if REPLAY_REASONING_KEY in row or REPLAY_TOOL_EXTRA_KEY in row:
                _refuse("Invalid remote-only message")
            for call in row.get("tool_calls") or []:
                if not isinstance(call, dict) or REPLAY_TOOL_EXTRA_KEY in call:
                    _refuse("Invalid remote-only message")
                if "extra_content" in call and (row["role"] != "assistant" or wire != "gemini"
                                                or not _valid_tool_extra(call["extra_content"])):
                    # The Gemini API's thought signature, sent back unchanged on the call
                    # it came with (same turn only). Nowhere else, never to another host.
                    _refuse("Remote-only tool-call replay is allowed only on its own provider")
            if "reasoning_details" in row:
                # A provider's own reasoning, sent back unchanged with the tool calls it
                # came with (same turn only). Nowhere else, and only as a list of objects.
                details = row["reasoning_details"]
                if (row["role"] != "assistant" or not row.get("tool_calls")
                        or type(details) is not list or not details
                        or not all(type(item) is dict for item in details)):
                    _refuse("Remote-only reasoning replay is allowed only with its tool calls")
        tools = payload.get("tools")
        if tools is not None and (not isinstance(tools, list) or not 1 <= len(tools) <= self.tools):
            _refuse("Remote-only tool schema budget exceeded")
        if "temperature" in payload:
            t = payload["temperature"]
            if type(t) not in {int, float}:
                _refuse("Invalid remote-only temperature")
            try:
                valid = math.isfinite(t) and 0 <= t <= 2
            except OverflowError:
                valid = False
            if not valid:
                _refuse("Invalid remote-only temperature")
        # Replayed reasoning has its own cap (response_bytes: it is one reply's worth
        # of provider output) and is left out of the transcript budget, so a turn
        # that fits is never refused only because the model reasoned at length. The
        # whole body stays bounded: request_bytes plus at most response_bytes. The
        # same for pictures and recordings, capped above by REMOTE_MEDIA_TOTAL_BYTES.
        try:
            reasoning_bytes = replay_reasoning_bytes(messages)
            transcript = [_without_media(_without_replay(row)) for row in messages]
            wire = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            aggregate = json.dumps({"messages": transcript, "tools": tools}, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            _refuse("Invalid bounded remote-only payload")
        if reasoning_bytes > self.response_bytes:
            _refuse(REASONING_REPLAY_TOO_LARGE)
        if (len(wire) > self.request_bytes + reasoning_bytes + media_bytes
                or len(aggregate) > self.aggregate_bytes):
            _refuse("Remote-only transcript/schema byte budget exceeded")
        if media_bytes and self.wire == "gemini" and len(wire) > GEMINI_INLINE_REQUEST_BYTES:
            # Google refuses an inline request past about 20 MB; say so here, before
            # anything is sent, instead of a provider error after the upload.
            _refuse(GEMINI_REQUEST_TOO_LARGE)


def profile_from_config(raw) -> RemoteOnlyProfile:
    """Validate a user's ``inference.remote_only`` mapping; refuse anything
    incomplete, unknown, out of range or carrying a secret."""
    if not isinstance(raw, dict):
        _refuse("Remote inference is enabled (WISP_REMOTE_ONLY=1) but no inference.remote_only "
                "profile is configured; see docs/REMOTE_INFERENCE.md")
    keys = set(raw)
    if keys - REQUIRED_KEYS - OPTIONAL_KEYS:
        _refuse("Unknown remote-inference profile fields: " + ", ".join(sorted(map(str, keys - REQUIRED_KEYS - OPTIONAL_KEYS))))
    if REQUIRED_KEYS - keys:
        _refuse("Incomplete remote-inference profile; missing: " + ", ".join(sorted(REQUIRED_KEYS - keys)))
    if raw.get("enabled", True) is not True:
        _refuse("Remote inference is launched but its profile is disabled")
    base_url = raw["base_url"]
    if not isinstance(base_url, str) or not base_url or base_url != base_url.rstrip("/"):
        _refuse("Remote-inference base_url must be an https origin without a trailing slash")
    # Checked before any URL parsing: urlsplit silently strips leading/trailing
    # whitespace and control characters, which would let a padded or
    # newline-terminated value pass as a valid origin.
    if not _BASE_URL_CHARS.fullmatch(base_url):
        _refuse("Remote-inference base_url must be printable ASCII with no whitespace or control characters")
    model_id = raw["model_id"]
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        _refuse("Invalid remote-inference model_id")
    credential_ref = raw["credential_ref"]
    if not isinstance(credential_ref, str) or not _CREDENTIAL_REF.fullmatch(credential_ref):
        _refuse("Remote-inference credential_ref must be keychain:<name>")
    limits = {}
    for key, (low, high) in LIMITS.items():
        value = raw[key]
        if type(value) is not int or not low <= value <= high:
            _refuse(f"Remote-inference {key} must be an integer from {low} to {high}")
        limits[key] = value
    if limits["max_tokens"] > limits["context_window"]:
        _refuse("Remote-inference max_tokens cannot exceed context_window")
    if limits["aggregate_bytes"] > limits["request_bytes"]:
        _refuse("Remote-inference aggregate_bytes cannot exceed request_bytes")
    api_prefix = raw.get("api_prefix", "/v1")
    timeout = raw.get("readiness_timeout", 5.0)
    if type(timeout) not in {int, float} or not (math.isfinite(timeout) and 0 < timeout <= 30):
        _refuse("Remote-inference readiness_timeout must be between 0 and 30 seconds")
    profile = RemoteOnlyProfile(base_url=base_url, model_id=model_id, credential_ref=credential_ref,
                                api_prefix=api_prefix, readiness_timeout=float(timeout), **limits)
    # The endpoint rules every remote target obeys: authenticated HTTPS, an
    # origin with no path, credentials or query, and a clean API prefix.
    from service.config.endpoints import endpoint_from_config
    ep = endpoint_from_config(profile.endpoint_name, {
        "base_url": profile.base_url, "credential_ref": profile.credential_ref,
        "provider": profile.provider, "api_prefix": profile.api_prefix,
        "readiness_timeout": profile.readiness_timeout,
    })
    if ep.base_url != profile.base_url:
        _refuse("Remote-inference base_url must be an https origin without a path")
    return profile


@lru_cache(maxsize=1)
def remote_only_profile() -> RemoteOnlyProfile | None:
    """Validate one startup snapshot; deliberately not cleared by Settings."""
    if _FORCED_MODE not in {None, "1"}:
        _refuse("Invalid remote-only launch mode")
    if _RETIRED_FULL_FLAG is not None:
        _log.warning("WISP_REMOTE_FULL is deprecated and ignored; remote inference is "
                     "always full read/write mode now")
    # Only the launcher's WISP_REMOTE_ONLY=1 opts in. A profile left in
    # ~/.moe/config.yaml must never move a normal Mac's inference (and its
    # native reads) to a remote server on its own.
    from service.config import models_config
    if _FORCED_MODE is None:
        # Settings ▸ Cloud inference ▸ "Everything" is the other explicit opt-in:
        # it writes the profile AND remote_parity, then restarts the backend.
        cfg = models_config()
        profile = (cfg.get("inference") or {}).get("remote_only")
        if not (cfg.get("remote_parity") is True and isinstance(profile, dict)
                and profile.get("enabled", True) is True):
            return None
        # Opted in from Settings, not the launcher: a profile this build refuses (one
        # left by an older build, a hand edit) must not keep the service from starting,
        # or Settings could never reach it to reconnect. Run without the cloud model
        # and say why in the log. The launcher's WISP_REMOTE_ONLY=1 still refuses.
        from service.config.endpoints import EndpointConfigurationError
        from service.config import upgrade_cloud_profile
        # An older Intel build saved a 32k window and smaller limits: raise them once.
        profile = upgrade_cloud_profile() or profile
        try:
            return profile_from_config(profile)
        except EndpointConfigurationError as exc:
            _log.warning("Cloud model not used: its saved settings are invalid (%s). "
                         "Reconnect it in Settings > Models > Cloud.", exc)
            print(f"[remote] cloud model not used: saved settings are invalid ({exc})", flush=True)
            return None
    inference = models_config().get("inference", {})
    if not isinstance(inference, dict):
        _refuse("Invalid inference configuration")
    return profile_from_config(inference.get("remote_only"))


def launched_remote_only() -> bool:
    """Whether the launcher itself forced remote-only mode (WISP_REMOTE_ONLY=1), as
    opposed to the Settings opt-in. Only the forced mode fails closed at startup."""
    return _FORCED_MODE == "1"


def require_generation_role(role: str) -> None:
    profile = remote_only_profile()
    if profile is not None and (not isinstance(role, str) or role not in profile.generation_roles):
        _refuse("This role is unavailable in remote-only inference")


def remote_default_target(role: str = "fast") -> Target:
    profile = remote_only_profile()
    if profile is None:
        _refuse("Remote-only profile is not enabled")
    require_generation_role(role)
    from service.config.endpoints import Target, endpoint_from_config
    ep = endpoint_from_config(profile.endpoint_name, {
        "base_url": profile.base_url, "credential_ref": profile.credential_ref,
        "managed": False, "provider": profile.provider, "api_prefix": profile.api_prefix,
        "readiness_timeout": profile.readiness_timeout,
    })
    return Target(role, ep, profile.model_id, profile="remote-only",
                  context_window=profile.context_window, capabilities=profile.tool_capabilities)


def validate_remote_target(target: Target) -> None:
    if remote_only_profile() is None:
        return
    from service.config.endpoints import Target
    if not isinstance(target, Target):
        _refuse("Remote-only inference requires an explicit bound target")
    require_generation_role(target.role)
    if target != remote_default_target(target.role):
        _refuse("Remote-only inference refuses alternate targets or credentials")


def require_local_models_disabled(operation: str) -> None:
    if remote_only_profile() is not None:
        _refuse(f"{operation} is unavailable in remote-only inference")


def remote_parity() -> bool:
    """User opt-in: remote inference behaves exactly like local models.

    Set ``remote_parity: true`` in ~/.moe/config.yaml (or WISP_REMOTE_PARITY=1).
    It lifts the data-minimizing limits of remote mode: skills and MCP tools,
    model-led routing, the history cap, the behavior phraser and Gmail sync all
    run as they do locally, so their content reaches the remote server. The
    pinned origin/model/credential, payload validation and the normal safety
    policy (sends always confirm) are unchanged."""
    raw = os.environ.get("WISP_REMOTE_PARITY")
    if raw is not None:
        return raw == "1"
    try:
        from service.config import models_config
        return models_config().get("remote_parity") is True
    except Exception:  # noqa: BLE001 — unreadable config: keep the limits
        return False


def remote_tool_allowed(tool_name: str | None) -> bool:
    """Whether a remote-only build may offer/run this tool at all.

    Every built-in except _REMOTE_EXCLUDED; the normal safety policy then
    decides allow/confirm/deny for each call."""
    if not isinstance(tool_name, str) or not tool_name:
        return False
    if remote_parity():
        from service.tools.registry import REGISTRY
        return REGISTRY.get(tool_name) is not None
    if tool_name in _REMOTE_EXCLUDED:
        return False
    # Only tools registered by Wisp itself; skill and MCP runtimes never start
    # in a remote-only build, but refuse their names regardless.
    from service.tools.registry import REGISTRY
    tool = REGISTRY.get(tool_name)
    return (tool is not None and tool.category != "skill_tool"
            and not tool_name.startswith("mcp_"))
