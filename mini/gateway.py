"""Authenticated, text-only inference proxy to one fixed loopback oMLX."""
from __future__ import annotations

import json

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
    return json.loads(body, parse_constant=invalid_constant, object_pairs_hook=unique_pairs)


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


async def safe_sse(upstream):
    """Preserve successful SSE lines, withholding upstream diagnostics.

    OMLXClient itself consumes one JSON object per data line. Bound the pending
    line as well as the whole response; accept LF/CRLF without normalizing bytes.
    """
    pending = bytearray()
    size = 0
    async for chunk in upstream.aiter_raw():
        size += len(chunk)
        if size > 16_000_000:
            raise Rejected(502, "upstream_response_too_large")
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            if len(line) > 200_000:
                raise Rejected(502, "invalid_upstream_stream")
            stripped = line.rstrip(b"\r")
            if stripped.startswith(b"data:"):
                value = stripped[5:].strip()
                if value != b"[DONE]":
                    try:
                        data = strict_json(value)
                        if not isinstance(data, dict) or "error" in data or not isinstance(data.get("choices"), list):
                            raise ValueError
                    except (ValueError, TypeError, RecursionError):
                        raise Rejected(502, "invalid_upstream_stream") from None
            elif stripped and not stripped.startswith(b":"):
                raise Rejected(502, "invalid_upstream_stream")
            elif stripped.startswith(b":"):
                # Upstream comments are not needed by OMLXClient and may contain diagnostics.
                continue
            yield bytes(line) + b"\n"
        if len(pending) > 200_000:
            raise Rejected(502, "invalid_upstream_stream")
    if pending:
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
                wants_stream = chat_request(body).get("stream", False)
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
                    data = json.loads(result)
                    if not isinstance(data, dict):
                        raise ValueError
                    if scope["path"] == "/health":
                        data = {"status": "ok"}
                    elif scope["path"] == "/v1/models":
                        models = data["data"]
                        if not isinstance(models, list) or len(models) > 1000:
                            raise ValueError
                        if any(not isinstance(m, dict) or not isinstance(m.get("id"), str)
                               or not 1 <= len(m["id"]) <= 200 for m in models):
                            raise ValueError
                        data = {"object": "list", "data": [{"id": m["id"], "object": "model"} for m in models]}
                    elif data.get("error"):
                        raise ValueError
                    await reply.json(data)
                except (ValueError, KeyError, TypeError, RecursionError):
                    raise Rejected(502, "invalid_upstream_response") from None
