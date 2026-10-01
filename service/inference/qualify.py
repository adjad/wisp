"""Decide whether a local inference app can drive Wisp's tool loop.

An OpenAI-compatible app answering a chat prompt proves nothing about tool
use. Two failures look like "the model is bad" but are really the engine:

* Silent context truncation. Ollama, for one, loads a model with a small
  default window (4k) and drops the START of an over-long prompt without an
  error. Wisp's system prompt and tool schemas live at the start, so the model
  is then asked to act without its instructions or its tools: it answers "I
  don't have access to your calendar" or produces unrelated text. The window
  the user types in a settings field is only a claim; the reported
  ``usage.prompt_tokens`` is the evidence.
* Tool calls that are not structured calls: the engine returns them as text
  (a template or parser is missing) or streams fragments Wisp cannot
  reassemble.

``qualify`` measures the first and exercises the second through the same
client Wisp uses in production. Nothing here reads or sends user data; every
prompt is synthetic. The caller (the server) records the result; a client can
never assert that a model is qualified.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from service.config.endpoints import Endpoint, EndpointConfigurationError, Target
from service.inference.omlx_client import OMLXClient

# Tool schemas for one scoped turn are ~3k tokens (worst case ~10k) before the
# system prompt and history, so 4k cannot work and 8k is the floor.
MIN_TOOL_CONTEXT = 8192
RECOMMENDED_CONTEXT = 16384
# Verifying more than this only adds minutes of prefill; Wisp does not need it.
PROBE_CONTEXT_CAP = RECOMMENDED_CONTEXT
QUALIFICATION_SCHEMA = 1
_PROBE_DEADLINE_SECONDS = 420.0
_TRUNCATION_TOLERANCE = 0.92
# The usage probe deliberately sends MORE than the cap: only an engine that
# reports holding at least ``cap`` prompt tokens has shown that the cap fits.
_PROBE_OVERSHOOT = 1.08
# An engine that refuses the oversize probe is re-tried at these fractions of
# the cap; the first prompt it accepts is the evidence.
_STEP_DOWN = (0.95, 0.85, 0.7, 0.5, 0.25, 0.125)

_TOOL = {"type": "function", "function": {
    "name": "probe_multiply",
    "description": "Multiply two whole numbers and return the product.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                   "required": ["a", "b"]}}}
_ASK = [{"role": "system", "content": "You are a careful assistant. When a tool can answer, call it."},
        {"role": "user", "content": "What is 17 times 23? Use the probe_multiply tool."}]


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    detail: str = ""
    required: bool = True  # advisory checks are reported but never block qualification

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "ok": self.ok, "detail": self.detail,
                "required": self.required}


@dataclass
class Report:
    qualified: bool = False
    effective_context: int = 0
    claimed_context: int = 0
    checks: list[Check] = field(default_factory=list)
    hint: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"qualified": self.qualified, "effective_context": self.effective_context,
                "claimed_context": self.claimed_context, "hint": self.hint,
                "seconds": round(self.seconds, 1), "checks": [c.as_dict() for c in self.checks]}


def context_hint(base_url: str, effective: int) -> str:
    """How to raise the window, for the engine most likely behind this port."""
    port = urlsplit(base_url).port
    wanted = RECOMMENDED_CONTEXT
    if port == 11434:
        return (f"Ollama is using a {effective:,}-token context. Quit Ollama, then start it with "
                f"`OLLAMA_CONTEXT_LENGTH={wanted} ollama serve` (or set `num_ctx {wanted}` in a "
                "Modelfile), and test again.")
    if port == 1234:
        return (f"LM Studio loaded this model with a {effective:,}-token context. Reload it with "
                f"Context Length set to {wanted} or more, then test again.")
    if port == 8080:
        return (f"The server is using a {effective:,}-token context. Restart it with `-c {wanted}` "
                "(llama.cpp) or the equivalent context option, then test again.")
    return (f"The app is only using a {effective:,}-token context. Restart it with a context length "
            f"of {wanted} or more, then test again.")


def _filler(tokens_per_char: float, tokens: int, needle: str) -> str:
    sentence = "The quick brown fox jumps over the lazy dog near the quiet river bank while item {} drifts by. "
    out, i = [f"The secret code is {needle}. "], 0
    chars = max(200, int(tokens / max(tokens_per_char, 0.05)))
    size = len(out[0])
    while size < chars:
        piece = sentence.format(i)
        out.append(piece); size += len(piece); i += 1
    return "".join(out)


async def _completion(http: httpx.AsyncClient, ep: Endpoint, model: str, content: str,
                      max_tokens: int = 4) -> dict[str, Any] | None:
    response = await http.post(
        ep.base_url + ep.api_prefix + "/chat/completions",
        json={"model": model, "max_tokens": max_tokens, "stream": False, "temperature": 0,
              "messages": [{"role": "user", "content": content}]},
        headers={"Accept-Encoding": "identity"})
    if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _prompt_tokens(data: dict[str, Any] | None) -> int | None:
    usage = (data or {}).get("usage")
    value = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _verified(tokens: int, cap: int) -> int:
    """The window to record: never more than the engine showed it held."""
    return (min(tokens, cap) // 256) * 256


async def measure_context(ep: Endpoint, model: str, claimed: int) -> tuple[int, str]:
    """(verified window in tokens, how it was established).

    The result is a conservative lower bound on the engine's real window, never
    the claimed number. An engine's ``usage.prompt_tokens`` counts what it
    actually held: a truncating engine reports its cut-down count, which can
    only be at or below its real window. So the probe is sized past the cap,
    and the recorded window is the reported count, capped and rounded down.
    The claimed cap is recorded only when the engine reports holding at least
    that many tokens.

    Character ratios only size the probe; they never verify a token count.
    Without usable reported prompt-token counts, the window is unmeasurable.

    ``how`` is "measured" (held the whole probe), "truncated" (cut a longer
    prompt), "rejected" (refused longer prompts) or "unmeasurable".
    """
    cap = min(claimed, PROBE_CONTEXT_CAP)
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as http:
        tiny = _prompt_tokens(await _completion(http, ep, model, "Say OK."))
        mid_text = _filler(0.25, 600, "CALIBRATE")
        mid = _prompt_tokens(await _completion(http, ep, model, mid_text))
        if tiny is None or mid is None or mid <= tiny:
            # Recall cannot establish a token lower bound for an unknown
            # tokenizer, even when the first line of a long prompt survives.
            return 0, "unmeasurable"
        per_char = (mid - tiny) / max(1, len(mid_text))
        target = int(cap * _PROBE_OVERSHOOT)
        code = "ZEBRA-" + str(int(time.time()) % 9000 + 1000)
        text = _filler(per_char, target, code)
        data = await _completion(http, ep, model, text + "\n\nReply with OK.")
        seen = _prompt_tokens(data)
        if seen is not None:
            # Fewer tokens than were sent means the engine cut the prompt (and
            # with it the start, where Wisp's instructions live) at ``seen``.
            cut = seen < cap and seen < target * _TRUNCATION_TOLERANCE
            return _verified(seen, cap), "truncated" if cut else "measured"
        # The engine refused the prompt outright (context exceeded): step down.
        # Whatever it then accepts and reports is what it has shown it holds.
        for fraction in _STEP_DOWN:
            size = int(cap * fraction)
            data = await _completion(http, ep, model, _filler(per_char, size, code) + "\n\nReply with OK.")
            seen = _prompt_tokens(data)
            if seen is not None:
                return _verified(seen, cap), "rejected"
        return 0, "unmeasurable"


def _parse_args(call: dict[str, Any]) -> dict[str, Any] | None:
    try:
        args = json.loads(call["function"]["arguments"])
    except (KeyError, TypeError, ValueError):
        return None
    return args if isinstance(args, dict) else None


def _is_multiply(call: dict[str, Any]) -> bool:
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        return False
    args = _parse_args(call)
    return (call["function"].get("name") == "probe_multiply" and args is not None
            and set(args) == {"a", "b"} and all(type(v) is int for v in args.values())
            and sorted(args.values()) == [17, 23])


_TEXT_CALL = re.compile(r"<tool_call>|\"name\"\s*:\s*\"probe_multiply\"|probe_multiply\s*\(", re.I)


async def _tool_checks(ep: Endpoint, model: str, window: int) -> list[Check]:
    checks: list[Check] = []
    target = Target("qualify", ep, model, context_window=max(window, MIN_TOOL_CONTEXT),
                    capabilities=("tools",))
    client = OMLXClient(target=target, timeout=180)
    try:
        async def first_call():
            return await client.chat(model, _ASK, tools=[_TOOL], max_tokens=512)

        data = await first_call()
        message = ((data.get("choices") or [{}])[0].get("message")) or {}
        calls = message.get("tool_calls") or []
        content = str(message.get("content") or "")
        if calls and _is_multiply(calls[0]):
            checks.append(Check("call", "Makes a structured tool call with correct arguments", True))
        elif not calls and _TEXT_CALL.search(content):
            checks.append(Check("call", "Makes a structured tool call with correct arguments", False,
                                "The model wrote the call as text instead of a structured tool call. "
                                "The app needs tool calling enabled for this model (for llama.cpp, start it "
                                "with `--jinja`), or choose a model that supports tools."))
            return checks
        else:
            checks.append(Check("call", "Makes a structured tool call with correct arguments", False,
                                "The model answered without calling the tool, or with wrong arguments."))
            return checks

        call = calls[0]
        history = _ASK + [
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call.get("id") or "call_0", "content": "391"}]
        follow = await client.chat(model, history, tools=[_TOOL], max_tokens=512)
        reply = ((follow.get("choices") or [{}])[0].get("message")) or {}
        text = str(reply.get("content") or "")
        ok = "391" in text.replace(",", "") and not reply.get("tool_calls") and "<think" not in text.lower()
        checks.append(Check("result", "Uses a tool result in its next answer", ok,
                            "" if ok else "After receiving the tool result the model did not answer with it "
                            "(or looped, or leaked its reasoning)."))

        streamed = None
        async for event in client.stream_events(model, _ASK, tools=[_TOOL], max_tokens=512):
            if event.get("kind") == "final":
                streamed = event.get("message") or {}
        s_calls = (streamed or {}).get("tool_calls") or []
        ok = bool(s_calls) and _is_multiply(s_calls[0])
        checks.append(Check("stream", "Streams a tool call Wisp can reassemble", ok,
                            "" if ok else "Streaming a tool call did not produce a complete, valid call."))

        plain = await client.chat(model, [{"role": "user", "content": "Reply with the single word: hello"}],
                                  tools=[_TOOL], max_tokens=64)
        p_msg = ((plain.get("choices") or [{}])[0].get("message")) or {}
        ok = not p_msg.get("tool_calls") and bool(str(p_msg.get("content") or "").strip())
        checks.append(Check("restraint", "Answers directly when no tool is needed", ok,
                            "" if ok else "The model called a tool for a plain greeting.", required=False))
    finally:
        await client.aclose()
    return checks


async def qualify(ep: Endpoint, model: str, claimed_context: int) -> Report:
    """Run the full qualification. Never raises for an engine's behavior."""
    started = time.monotonic()
    report = Report(claimed_context=claimed_context)
    try:
        async with asyncio.timeout(_PROBE_DEADLINE_SECONDS):
            effective, how = await measure_context(ep, model, claimed_context)
            report.effective_context = effective
            if effective <= 0 and how == "unmeasurable":
                report.checks.append(Check("context", "Reports a usable context window", False,
                                           "The app did not report usable prompt-token usage, so its "
                                           "context window cannot be verified."))
                report.hint = ("Use an app/model that reports prompt-token usage, then test again. "
                               "Without measured token counts Wisp cannot verify the context "
                               "required for tool workloads.")
            elif effective < MIN_TOOL_CONTEXT:
                if how == "rejected":
                    detail = (f"The app refuses prompts longer than about {effective:,} tokens; Wisp's "
                              f"tool instructions need at least {MIN_TOOL_CONTEXT:,}.")
                else:
                    detail = (f"The app is silently cutting prompts to about {effective:,} tokens; Wisp's "
                              f"tool instructions need at least {MIN_TOOL_CONTEXT:,}.")
                report.checks.append(Check(
                    "context", f"Context window of at least {MIN_TOOL_CONTEXT:,} tokens", False, detail))
                report.hint = context_hint(ep.base_url, effective)
            else:
                report.checks.append(Check(
                    "context", f"Context window of at least {MIN_TOOL_CONTEXT:,} tokens", True,
                    f"Verified {effective:,} tokens ({how})."))
                report.checks.extend(await _tool_checks(ep, model, effective))
    except TimeoutError:
        report.checks.append(Check("deadline", "Finishes the test in time", False,
                                   "The app was too slow to finish the test; try a smaller model."))
    except (EndpointConfigurationError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
        report.checks.append(Check("error", "Completes the test", False,
                                   f"The test could not finish ({type(error).__name__})."))
    except Exception as error:  # noqa: BLE001 - a misbehaving engine must not 500 the settings screen
        report.checks.append(Check("error", "Completes the test", False,
                                   f"The app returned something Wisp could not use ({type(error).__name__})."))
    report.qualified = bool(report.checks) and all(c.ok for c in report.checks if c.required) \
        and report.effective_context >= MIN_TOOL_CONTEXT
    report.seconds = time.monotonic() - started
    return report
