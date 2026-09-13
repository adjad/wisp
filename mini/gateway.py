"""Authenticated, text-only inference proxy to one fixed loopback oMLX."""
from __future__ import annotations

import json
import math

import httpx

from mini.http import Boundary, Rejected, credential

UPSTREAM = "http://127.0.0.1:8000"
ROUTES = frozenset({("GET", "/health"), ("GET", "/v1/models"),
                    ("POST", "/v1/chat/completions"),
                    ("POST", "/v1/embeddings"), ("POST", "/v1/rerank")})


def strict_json(body):
    def invalid_constant(_):
        raise ValueError
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError
        return parsed
    return json.loads(body, parse_constant=invalid_constant, parse_float=finite_float, object_pairs_hook=unique_pairs)


def chat_request(body):
    try:
        data = strict_json(body)
        if not isinstance(data, dict) or not isinstance(data.get("model"), str):
            raise ValueError
        if set(data) - {"model", "messages", "max_tokens", "stream", "temperature", "tools", "tool_choice", "chat_template_kwargs"}:
            raise ValueError
        if not data["model"].strip() or len(data["model"]) > 200:
            raise ValueError
        if type(data.get("stream", False)) is not bool:
            raise ValueError
        if "max_tokens" in data and (type(data["max_tokens"]) is not int or not 1 <= data["max_tokens"] <= 32768):
            raise ValueError
        if "temperature" in data and (type(data["temperature"]) not in (int, float) or not 0 <= data["temperature"] <= 2 or not math.isfinite(data["temperature"])):
            raise ValueError
        data.setdefault("max_tokens", 32768)
        messages = data["messages"]
        if not isinstance(messages, list) or not messages:
            raise ValueError
        # Text and tool-call messages only. Inference must not fetch remote
        # image/audio URLs or read local files through multimodal content parts.
        for message in messages:
            if not isinstance(message, dict) or message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError
            if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"}:
                raise ValueError
            if message.get("role") not in ("system", "user", "assistant", "tool"):
                raise ValueError
            for field in ("name", "tool_call_id", "reasoning_content"):
                if field in message and message[field] is not None and not isinstance(message[field], str):
                    raise ValueError
            calls = message.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list):
                    raise ValueError
                for call in calls:
                    if not isinstance(call, dict) or set(call) != {"id", "type", "function"} or call["type"] != "function":
                        raise ValueError
                    fn = call["function"]
                    if not isinstance(call["id"], str) or not isinstance(fn, dict) or set(fn) != {"name", "arguments"} or not all(isinstance(v, str) for v in fn.values()):
                        raise ValueError
        if "chat_template_kwargs" in data:
            kwargs = data["chat_template_kwargs"]
            if not isinstance(kwargs, dict) or set(kwargs) - {"enable_thinking"} or any(type(v) is not bool for v in kwargs.values()):
                raise ValueError
        if "tools" in data:
            if not isinstance(data["tools"], list):
                raise ValueError
            for tool in data["tools"]:
                if not isinstance(tool, dict) or set(tool) != {"type", "function"} or tool["type"] != "function":
                    raise ValueError
                fn = tool["function"]
                if not isinstance(fn, dict) or set(fn) - {"name", "description", "parameters", "strict"} or not isinstance(fn.get("name"), str):
                    raise ValueError
        choice = data.get("tool_choice")
        if isinstance(choice, dict):
            if set(choice) != {"type", "function"} or choice["type"] != "function" or not isinstance(choice["function"], dict) or set(choice["function"]) != {"name"} or not isinstance(choice["function"]["name"], str):
                raise ValueError
        elif choice is not None and choice not in ("auto", "none", "required"):
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError, RecursionError):
        raise Rejected(400, "invalid_chat_request") from None


def retrieval_request(path, body):
    try:
        data = strict_json(body)
        if not isinstance(data, dict) or not isinstance(data.get("model"), str) or not 1 <= len(data["model"]) <= 200:
            raise ValueError
        if path == "/v1/embeddings":
            if set(data) != {"model", "input"}:
                raise ValueError
            texts = data["input"]
        else:
            if set(data) != {"model", "query", "documents", "top_n", "return_documents"}:
                raise ValueError
            if not isinstance(data["query"], str) or type(data["top_n"]) is not int or data["top_n"] < 1 or data["return_documents"] is not False:
                raise ValueError
            texts = data["documents"]
        if not isinstance(texts, list) or not 1 <= len(texts) <= 1000 or any(not isinstance(t, str) for t in texts):
            raise ValueError
    except (ValueError, KeyError, TypeError, RecursionError):
        raise Rejected(400, "invalid_retrieval_request") from None


def number(value):
    if type(value) not in (int, float) or not -1e100 <= value <= 1e100 or not math.isfinite(value):
        raise ValueError
    return value


def bounded_text(value, maximum=200_000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError
    return value


def count(value):
    if type(value) is not int or not 0 <= value <= 2**53:
        raise ValueError
    return value


def chat_response(data, *, stream=False):
    """Reconstruct only fields consumed by the client; never copy diagnostics."""
    if not isinstance(data, dict) or "error" in data:
        raise ValueError
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError
    finish = choice.get("finish_reason")
    if finish not in ({None, "stop", "length", "tool_calls"} if stream else {"stop", "length", "tool_calls"}):
        raise ValueError
    key = "delta" if stream else "message"
    message = choice.get(key)
    if not isinstance(message, dict):
        raise ValueError
    clean = {}
    for field in ("content", "reasoning_content"):
        if field in message:
            clean[field] = None if message[field] is None else bounded_text(message[field])
    if "role" in message:
        if message["role"] != "assistant":
            raise ValueError
        clean["role"] = "assistant"
    calls = message.get("tool_calls")
    if calls is not None:
        if not isinstance(calls, list) or not 1 <= len(calls) <= 128:
            raise ValueError
        clean["tool_calls"] = []
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ValueError
            fn = call["function"]
            result = {"function": {k: bounded_text(fn[k]) for k in ("name", "arguments") if k in fn}}
            if not stream and set(result["function"]) != {"name", "arguments"}:
                raise ValueError
            if "id" in call:
                result["id"] = bounded_text(call["id"], 200)
            elif not stream:
                raise ValueError
            if stream:
                result["index"] = count(call["index"])
                if result["index"] >= 128:
                    raise ValueError
            if "type" in call:
                if call["type"] != "function":
                    raise ValueError
                result["type"] = "function"
            elif not stream:
                result["type"] = "function"
            clean["tool_calls"].append(result)
    out = {"choices": [{key: clean, "finish_reason": finish}]}
    if "usage" in data and data["usage"] is not None:
        if not isinstance(data["usage"], dict):
            raise ValueError
        out["usage"] = {k: count(data["usage"][k]) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in data["usage"]}
    return out


def route_response(path, data):
    if not isinstance(data, dict) or "error" in data:
        raise ValueError
    if path == "/health":
        if data.get("status") not in ("ok", "healthy"):
            raise ValueError
        return {"status": "ok"}
    if path == "/v1/chat/completions":
        return chat_response(data)
    if path == "/v1/models":
        models = data["data"]
        if not isinstance(models, list) or len(models) > 1000:
            raise ValueError
        if any(not isinstance(m, dict) or not m.get("id") for m in models):
            raise ValueError
        return {"object": "list", "data": [{"id": bounded_text(m["id"], 200), "object": "model"} for m in models]}

    if path == "/v1/embeddings":
        rows = data["data"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError
        out = []
        for row in rows:
            vector = row["embedding"]
            if not isinstance(vector, list) or not 1 <= len(vector) <= 65536:
                raise ValueError
            out.append({"object": "embedding", "index": count(row["index"]), "embedding": [number(v) for v in vector]})
        if len({r["index"] for r in out}) != len(out):
            raise ValueError
        return {"object": "list", "data": out}
    if path == "/v1/rerank":
        rows = data["results"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError
        out = [{"index": count(row["index"]), "relevance_score": number(row["relevance_score"])} for row in rows]
        if len({r["index"] for r in out}) != len(out):
            raise ValueError
        return {"results": out}
    raise ValueError


async def safe_sse(upstream):
    pending = bytearray()
    size = 0
    terminal = False
    done = False
    async for chunk in upstream.aiter_raw():
        size += len(chunk)
        if size > 16_000_000:
            raise Rejected(502, "upstream_response_too_large")
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            try:
                if len(line) > 200_000:
                    raise ValueError
                line = line.rstrip(b"\r")
                if not line or line.startswith(b":"):
                    continue
                if not line.startswith(b"data:") or done:
                    raise ValueError
                value = line[5:].strip()
                if value == b"[DONE]":
                    if not terminal:
                        raise ValueError
                    done = True
                    yield b"data: [DONE]\n\n"
                    continue
                raw = strict_json(value)
                if terminal:
                    if not isinstance(raw, dict) or raw.get("choices") != [] or not isinstance(raw.get("usage"), dict) or "error" in raw:
                        raise ValueError
                    usage = {k: count(raw["usage"][k]) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in raw["usage"]}
                    if not usage:
                        raise ValueError
                    yield b"data: " + json.dumps({"choices": [], "usage": usage}, separators=(",", ":")).encode() + b"\n\n"
                    continue
                data = chat_response(raw, stream=True)
                terminal = data["choices"][0]["finish_reason"] is not None
                yield b"data: " + json.dumps(data, separators=(",", ":"), allow_nan=False).encode() + b"\n\n"
            except (ValueError, TypeError, KeyError, RecursionError):
                raise Rejected(502, "invalid_upstream_stream") from None
        if len(pending) > 200_000:
            raise Rejected(502, "invalid_upstream_stream")
    if pending or not terminal or not done:
        raise Rejected(502, "invalid_upstream_stream")


class Gateway(Boundary):
    routes = ROUTES

    def __init__(self, token, upstream_token, *, transport=None, **limits):
        super().__init__(token, **limits)
        credential(upstream_token)
        if token == upstream_token:
            raise ValueError("Gateway and upstream credentials must differ")
        self._upstream_token, self.transport = upstream_token, transport

    async def handle(self, scope, body, headers, reply):
        if scope.get("query_string"):
            raise Rejected(400, "invalid_query")
        wants_stream = False
        if scope["method"] == "POST":
            if headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() != b"application/json":
                raise Rejected(415, "json_required")
            if scope["path"] == "/v1/chat/completions":
                request = chat_request(body)
                wants_stream = request.get("stream", False)
                body = json.dumps(request, allow_nan=False, separators=(",", ":")).encode()
            else:
                retrieval_request(scope["path"], body)
        # Construct every header afresh: no Host, Forwarded, X-Forwarded-*,
        # Tailscale identity, cookies, proxy auth, or caller auth crosses here.
        outgoing = {"Authorization": "Bearer " + self._upstream_token,
                    "Content-Type": "application/json", "Accept-Encoding": "identity",
                    "Accept": "text/event-stream" if wants_stream else "application/json"}
        async with httpx.AsyncClient(base_url=UPSTREAM, trust_env=False, follow_redirects=False,
                                     transport=self.transport, timeout=httpx.Timeout(self.deadline, connect=2)) as client:
            async with client.stream(scope["method"], scope["path"], content=body,
                                     headers=outgoing) as upstream:
                if upstream.status_code != 200:
                    raise Rejected(502, "upstream_error")
                if upstream.headers.get("content-encoding", "identity") != "identity":
                    raise Rejected(502, "invalid_upstream_response")
                content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if wants_stream:
                    if content_type != "text/event-stream":
                        raise Rejected(502, "invalid_upstream_response")
                    await reply.start(content_type=b"text/event-stream")
                    async for chunk in safe_sse(upstream):
                        await reply.body(chunk, more=True)
                    await reply.body(b"")
                    return
                if content_type != "application/json":
                    raise Rejected(502, "invalid_upstream_response")
                result = bytearray()
                async for chunk in upstream.aiter_raw():
                    result.extend(chunk)
                    if len(result) > 16_000_000:
                        raise Rejected(502, "upstream_response_too_large")
                try:
                    data = route_response(scope["path"], strict_json(result))
                    await reply.json(data)
                except (ValueError, KeyError, TypeError, RecursionError):
                    raise Rejected(502, "invalid_upstream_response") from None
