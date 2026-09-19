"""Offline OpenRouter contract prototype; no credentials, I/O, or tool execution.

This intentionally narrow prototype is NOT a production Wisp client. Inputs are
synthetic fixtures. SSE is buffered (bounded), not a live incremental transport.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_BYTES = 1_048_576


class ContractError(ValueError):
    """Safe fixed error text; never echo provider bodies or tool arguments."""


def request(model, provider, messages, tools, *, enabled=False, max_tokens=128):
    """Construct a non-streaming request, explicitly opted in and provider-pinned.

    No generic extra kwargs: local backend options cannot leak into the payload.
    Model/provider strings here are fixture IDs, not verified catalog entries.
    """
    if enabled is not True:
        raise ContractError("Cloud pilot is disabled")
    if (not isinstance(model, str) or "/" not in model
            or not isinstance(provider, str) or not provider.strip()):
        raise ContractError("Explicit model and provider required")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 256:
        raise ContractError("Output token limit outside pilot bounds")
    body = {
        "model": model, "messages": deepcopy(messages), "tools": deepcopy(tools),
        "tool_choice": "auto", "parallel_tool_calls": False,
        "max_tokens": max_tokens, "stream": False,
        "provider": {"only": [provider], "allow_fallbacks": False,
                     "require_parameters": True, "data_collection": "deny",
                     "zdr": True},
    }
    if len(json.dumps(body).encode()) > MAX_BYTES:
        raise ContractError("Request exceeds pilot byte limit")
    return {"url": ENDPOINT, "method": "POST",
            "headers": {"Content-Type": "application/json"}, "body": body}


def _json(raw):
    try:
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise ContractError("Invalid JSON") from None


def _usage(value):
    if value is None:
        return None  # Missing usage must never be represented as zero cost.
    if not isinstance(value, dict):
        raise ContractError("Invalid usage")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in value and (type(value[key]) is not int or value[key] < 0):
            raise ContractError("Invalid token count")
    if "cost" in value and value["cost"] is not None:
        try:
            cost = Decimal(str(value["cost"]))
        except InvalidOperation:
            raise ContractError("Invalid cost") from None
        if not cost.is_finite() or cost < 0:
            raise ContractError("Invalid cost")
    return deepcopy(value)


def _message(message, finish, allowed_tools):
    if finish not in ("stop", "tool_calls"):
        raise ContractError("Incomplete or filtered completion")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ContractError("Invalid assistant message")
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise ContractError("Invalid content")
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list) or bool(calls) != (finish == "tool_calls"):
        raise ContractError("Invalid tool completion")
    if not calls and not message.get("content"):
        raise ContractError("Empty completion")
    seen = set()
    for call in calls:
        if not isinstance(call, dict):
            raise ContractError("Invalid tool call")
        call_id, fn = call.get("id"), call.get("function")
        if (not isinstance(call_id, str) or not call_id or call_id in seen
                or call.get("type") != "function" or not isinstance(fn, dict)):
            raise ContractError("Invalid tool identity")
        seen.add(call_id)
        if not isinstance(fn.get("name"), str) or fn["name"] not in allowed_tools:
            raise ContractError("Unadvertised tool")
        if not isinstance(fn.get("arguments"), str) or not isinstance(_json(fn["arguments"]), dict):
            raise ContractError("Tool arguments must be a JSON object")
    # Retain provider reasoning_details for subsequent turns; never execute tools.
    return deepcopy(message)


def response(status, raw, *, allowed_tools=()):
    """Decode a synthetic HTTP response; redirects/errors never become success."""
    if status != 200:
        raise ContractError(f"Provider HTTP status {status}; no automatic retry")
    if len(raw) > MAX_BYTES:
        raise ContractError("Response exceeds pilot byte limit")
    data = _json(raw)
    if not isinstance(data, dict) or "error" in data:
        raise ContractError("Provider error or invalid envelope")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ContractError("Expected one choice")
    choice = choices[0]
    return {"message": _message(choice.get("message"), choice.get("finish_reason"), allowed_tools),
            "usage": _usage(data.get("usage"))}


def stream_response(chunks, *, allowed_tools=()):
    """Reassemble synthetic SSE byte chunks; emit nothing before valid completion.

    Bounded whole-stream buffering tests framing and tool reconstruction, not
    live latency, cancellation, backpressure, or production streaming behavior.
    """
    raw = bytearray()
    for chunk in chunks:
        if len(raw) + len(chunk) > MAX_BYTES:
            raise ContractError("Stream exceeds pilot byte limit")
        raw.extend(chunk)
    try:
        wire = raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeError:
        raise ContractError("Invalid stream encoding") from None
    if not wire.endswith("\n\n"):
        raise ContractError("Incomplete SSE frame")
    content, calls, finish, usage, done = [], {}, None, None, False
    for frame in wire.split("\n\n"):
        payload = "\n".join(line[5:].lstrip(" ") for line in frame.split("\n") if line.startswith("data:"))
        if not payload:
            continue
        if done:
            raise ContractError("Data after stream terminator")
        if payload == "[DONE]":
            done = True
            continue
        event = _json(payload)
        if not isinstance(event, dict) or "error" in event:
            raise ContractError("Provider stream error")
        if "usage" in event:
            usage = _usage(event["usage"])
        choices = event.get("choices")
        if choices == []:
            continue  # Also tolerate OpenAI-style usage-only frames.
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ContractError("Invalid stream choice")
        choice = choices[0]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ContractError("Invalid stream delta")
        # Reasoning streams need additional normalization; explicitly unsupported.
        if any(delta.get(k) for k in ("reasoning", "reasoning_content", "reasoning_details")):
            raise ContractError("Reasoning streams outside pilot scope")
        text = delta.get("content")
        if text is not None and not isinstance(text, str):
            raise ContractError("Invalid stream content")
        if finish is not None and (text or delta.get("tool_calls")):
            raise ContractError("Content after terminal choice")
        if text:
            content.append(text)
        fragments = delta.get("tool_calls")
        if fragments is None:
            fragments = []
        if not isinstance(fragments, list):
            raise ContractError("Invalid tool fragments")
        for fragment in fragments:
            if not isinstance(fragment, dict) or type(fragment.get("index")) is not int or fragment["index"] < 0:
                raise ContractError("Invalid tool index")
            call = calls.setdefault(fragment["index"], {"id": "", "type": "function",
                                                       "function": {"name": "", "arguments": ""}})
            if fragment.get("type", "function") != "function":
                raise ContractError("Invalid tool type")
            fn = fragment.get("function", {})
            if not isinstance(fn, dict):
                raise ContractError("Invalid function fragment")
            for source, dest, keys in ((fragment, call, ("id",)), (fn, call["function"], ("name", "arguments"))):
                for key in keys:
                    if key in source:
                        if not isinstance(source[key], str):
                            raise ContractError("Invalid string fragment")
                        dest[key] += source[key]
        terminal = choice.get("finish_reason")
        if terminal is not None:
            if finish is not None and finish != terminal:
                raise ContractError("Conflicting terminal choices")
            finish = terminal
    if not done:
        raise ContractError("Missing stream terminator")
    message = {"role": "assistant", "content": "".join(content) or None}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {"message": _message(message, finish, allowed_tools), "usage": usage}


def synthetic_followup(messages, assistant, tool_results, tools):
    """Append prewritten fixture results. No dispatch, callbacks, or tool execution."""
    names = {t["function"]["name"] for t in tools}
    checked = _message(assistant, "tool_calls", names)
    ids = {c["id"] for c in checked["tool_calls"]}
    if set(tool_results) != ids or any(not isinstance(v, str) for v in tool_results.values()):
        raise ContractError("Tool result IDs must match exactly")
    return deepcopy(messages) + [checked] + [
        {"role": "tool", "tool_call_id": c["id"], "content": tool_results[c["id"]]}
        for c in checked["tool_calls"]]
