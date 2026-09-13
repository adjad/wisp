"""Synthetic ASGI/HTTP transports: no network listener or real inference."""
import asyncio
import json

import httpx
import pytest
from tests.test_mini_resources import synthetic_capacity, synthetic_process

from mini.gateway import Gateway as ProductionGateway, UPSTREAM
from tests.test_mini_resources import fixture_guard, fixture_configuration

def Gateway(*args, **kwargs):
    config = fixture_configuration()
    config["models"].append({**config["models"][0], "model_id": "fixture"})
    kwargs.setdefault("resources", fixture_guard(config))
    return ProductionGateway(*args, **kwargs)

TOKEN = "a" * 64
UPSTREAM_TOKEN = "b" * 64
CHAT = {"model": "fixture-model", "messages": [{"role": "user", "content": "synthetic prompt"}], "stream": False, "max_tokens": 1024}


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, stall=False, error=False):
        self.chunks, self.stall, self.error = chunks, stall, error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise httpx.ReadError("secret upstream error " + UPSTREAM_TOKEN)
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def response(data=None, *, stream=None, status=200, headers=None):
    return httpx.Response(status, headers=headers or {"content-type": "application/json"},
                          stream=stream or Stream([json.dumps(data).encode()]))


async def invoke(app, path="/v1/chat/completions", *, method="POST", body=None,
                 headers=None, query=b"", raw_path=None, receive_hook=None, send_hook=None):
    events = []
    first = True
    body = json.dumps(CHAT).encode() if body is None and method == "POST" else body or b""
    async def receive():
        nonlocal first
        if receive_hook:
            return await receive_hook()
        if first:
            first = False
            return {"type": "http.request", "body": body}
        await asyncio.Event().wait()
    async def send(event):
        if send_hook:
            await send_hook(event)
        events.append(event)
    await app({"type": "http", "method": method, "path": path,
               "raw_path": raw_path if raw_path is not None else path.encode(),
               "query_string": query,
               "headers": headers if headers is not None else [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")]}, receive, send)
    status = next((e["status"] for e in events if e["type"] == "http.response.start"), None)
    return status, b"".join(e.get("body", b"") for e in events), events


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Basic " + TOKEN, "bearer " + TOKEN, "Bearer " + UPSTREAM_TOKEN])
async def test_credentials_denied_before_transport(auth):
    calls = []
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r)))
    headers = [] if auth is None else [(b"authorization", auth.encode())]
    status, body, _ = await invoke(app, "/health", method="GET", headers=headers)
    assert status == 401 and not calls and TOKEN.encode() not in body


async def test_valid_auth_and_header_scrubbing():
    def handle(request):
        assert str(request.url) == UPSTREAM + "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer " + UPSTREAM_TOKEN
        assert request.headers["host"] == "127.0.0.1:8000"
        assert not any(k in request.headers for k in ("forwarded", "x-forwarded-for", "tailscale-user-login", "cookie", "proxy-authorization"))
        assert json.loads(request.content) == CHAT
        return response({"choices": [{"message": {"content": "fixture"}, "finish_reason": "stop"}]}, headers={"content-type": "application/json", "set-cookie": TOKEN, "x-forwarded-for": TOKEN})
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(handle))
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")]
    headers += [(k, b"spoof") for k in (b"forwarded", b"x-forwarded-for", b"tailscale-user-login", b"cookie", b"host", b"proxy-authorization")]
    status, body, events = await invoke(app, headers=headers)
    assert status == 200 and b"fixture" in body
    assert TOKEN not in str(events) and UPSTREAM_TOKEN not in str(events)
    assert app.active == 0


async def test_duplicate_authorization_and_header_bounds():
    app = Gateway(TOKEN, UPSTREAM_TOKEN)
    auth = ("Bearer " + TOKEN).encode()
    assert (await invoke(app, headers=[(b"authorization", auth), (b"Authorization", auth)]))[0] == 401
    assert (await invoke(app, headers=[(b"authorization", auth), (b"x", b"x" * 20_000)]))[0] == 431


@pytest.mark.parametrize("method,path,raw,query", [
    ("GET", "/admin", None, b""), ("GET", "/v1/models/status", None, b""),
    ("POST", "/v1/models/foo/load", None, b""), ("POST", "/v1/models/foo/unload", None, b""),
    ("GET", "/health/", None, b""), ("GET", "//health", None, b""),
    ("HEAD", "/health", None, b""), ("OPTIONS", "/v1/models", None, b""),
    ("GET", "/health", b"/%68ealth", b""), ("GET", "/health", None, b"secret=1"),
    ("POST", "/v1/completions", None, b""),
])
async def test_path_allowlist(method, path, raw, query):
    calls = []
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r)))
    status, _, _ = await invoke(app, path, method=method, raw_path=raw, query=query)
    assert status in (400, 404) and not calls


async def test_health_and_models_remove_metadata():
    def handler(r):
        return response({"status": "ok", "database_path": TOKEN} if r.url.path == "/health" else
                        {"data": [{"id": "fixture", "path": TOKEN}], "private": TOKEN})
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(handler))
    for path in ("/health", "/v1/models"):
        status, body, _ = await invoke(app, path, method="GET")
        assert status == 200 and TOKEN.encode() not in body


@pytest.mark.parametrize("status", [301, 302, 400, 401, 500])
async def test_upstream_errors_are_sanitized(status):
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r:
        response({"error": UPSTREAM_TOKEN}, status=status, headers={"location": "https://example.invalid/" + TOKEN})))
    code, body, events = await invoke(app)
    assert code == 502 and TOKEN not in str(events) and UPSTREAM_TOKEN.encode() not in body


async def test_oversized_declared_chunked_and_mismatched_upload():
    app = Gateway(TOKEN, UPSTREAM_TOKEN, max_body=128)
    headers = [(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-length", b"129")]
    assert (await invoke(app, headers=headers))[0] == 413
    queue = asyncio.Queue()
    for event in [{"type": "http.request", "body": b"x" * 80, "more_body": True}, {"type": "http.request", "body": b"x" * 80}]:
        queue.put_nowait(event)
    assert (await invoke(app, receive_hook=queue.get))[0] == 413
    headers[-1] = (b"content-length", b"1")
    assert (await invoke(app, body=b"xx", headers=headers))[0] == 400


async def test_slow_upload_and_upstream_headers_deadline():
    app = Gateway(TOKEN, UPSTREAM_TOKEN, deadline=0.03)
    async def stall():
        await asyncio.Event().wait()
    assert (await invoke(app, receive_hook=stall))[0] == 504
    async def handler(r):
        await asyncio.Event().wait()
    app.transport = httpx.MockTransport(handler)
    assert (await invoke(app))[0] == 504
    assert app.active == 0


async def test_stream_bytes_preserved_and_failure_not_success():
    chunks = [b'data: {"choices":[{"delta":{"content":"fixture"},"finish_reason":null}]}\n\n',
              b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n', b"data: [DONE]\n\n"]
    stream = Stream(chunks)
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: response(stream=stream, headers={"content-type": "text/event-stream"})))
    status, body, _ = await invoke(app, body=json.dumps({**CHAT, "stream": True}).encode())
    assert status == 200 and body == b"".join(chunks) and stream.closed
    stream = Stream(chunks[:1], error=True)
    status, body, _ = await invoke(app, body=json.dumps({**CHAT, "stream": True}).encode())
    assert status == 200 and b'"error"' in body and b"[DONE]" not in body
    assert UPSTREAM_TOKEN.encode() not in body and stream.closed and app.active == 0


async def test_midstream_deadline_closes_upstream():
    stream = Stream([b'data: {"choices":[{"delta":{},"finish_reason":null}]}\n\n'], stall=True)
    app = Gateway(TOKEN, UPSTREAM_TOKEN, deadline=0.03, transport=httpx.MockTransport(lambda r: response(stream=stream, headers={"content-type": "text/event-stream"})))
    status, body, _ = await invoke(app, body=json.dumps({**CHAT, "stream": True}).encode())
    assert status == 200 and b"deadline_exceeded" in body and b"[DONE]" not in body
    assert stream.closed and app.active == 0


@pytest.mark.parametrize("after_chunk", [False, True])
async def test_disconnect_cancels_silent_and_active_streams(after_chunk):
    started = asyncio.Event()
    stream = Stream([b'data: {"choices":[{"delta":{},"finish_reason":null}]}\n\n'] if after_chunk else [], stall=True)
    def handler(request):
        started.set()
        return response(stream=stream, headers={"content-type": "text/event-stream"})
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(handler))
    queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": json.dumps({**CHAT, "stream": True}).encode()})
    task = asyncio.create_task(invoke(app, receive_hook=queue.get))
    await started.wait()
    await asyncio.sleep(0)
    queue.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(task, 1)
    assert stream.closed and app.active == 0


async def test_concurrency_rejects_and_cancellation_releases():
    started = asyncio.Event()
    async def handler(request):
        started.set()
        await asyncio.Event().wait()
    app = Gateway(TOKEN, UPSTREAM_TOKEN, concurrency=1, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(invoke(app))
    await started.wait()
    assert (await invoke(app))[0] == 429
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.active == 0


async def test_downstream_send_failure_closes_upstream():
    stream = Stream([b'data: {"choices":[{"delta":{},"finish_reason":null}]}\n\n'], stall=True)
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: response(stream=stream, headers={"content-type": "text/event-stream"})))
    async def fail(event):
        if event["type"] == "http.response.body":
            raise BrokenPipeError("private downstream detail")
    await invoke(app, body=json.dumps({**CHAT, "stream": True}).encode(), send_hook=fail)
    assert stream.closed and app.active == 0


@pytest.mark.parametrize("body", [b"[]", b"invalid", json.dumps({**CHAT, "messages": [{"content": [{"type": "image_url", "image_url": {"url": "file:///secret"}}]}]}).encode()])
async def test_non_text_or_invalid_chat_never_reaches_upstream(body):
    calls = []
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r)))
    assert (await invoke(app, body=body))[0] == 400 and not calls


@pytest.mark.parametrize("path,payload", [("/v1/embeddings", {"model": "fixture", "input": ["text"]}),
    ("/v1/rerank", {"model": "fixture", "query": "fixture", "documents": ["text"], "top_n": 1, "return_documents": False})])
async def test_retrieval_routes_match_pro_callers(path, payload):
    def handler(request):
        assert json.loads(request.content) == payload
        return response({"data": [{"index": 0, "embedding": [0.1]}]} if path == "/v1/embeddings" else {"results": [{"index": 0, "relevance_score": 0.5}]})
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(handler))
    assert (await invoke(app, path, body=json.dumps(payload).encode()))[0] == 200
    assert (await invoke(app, path, body=json.dumps({**payload, "url": "https://example.invalid"}).encode()))[0] == 400


@pytest.mark.parametrize("payload", [
    {**CHAT, "documents": [{"url": "https://example.invalid"}]},
    {**CHAT, "messages": [{"role": "user", "content": "text", "images": ["file:///private"]}]},
    {**CHAT, "messages": [{"role": "user", "content": "text", "audio": {"url": "file:///private"}}]},
    {**CHAT, "chat_template_kwargs": {"remote_url": "https://example.invalid"}},
    {**CHAT, "temperature": float("nan")},
])
async def test_hidden_media_and_nonstandard_json_rejected(payload):
    calls = []
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r)))
    assert (await invoke(app, body=json.dumps(payload).encode()))[0] == 400 and not calls


async def test_duplicate_json_fields_rejected():
    app = Gateway(TOKEN, UPSTREAM_TOKEN)
    assert (await invoke(app, body=b'{"model":"a","model":"b","messages":[]}'))[0] == 400


async def test_split_upstream_sse_errors_are_sanitized():
    raw = b'data: {"error":{"message":"' + UPSTREAM_TOKEN.encode() + b'"}}\n\n'
    stream = Stream([raw[:15], raw[15:40], raw[40:]])
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: response(stream=stream, headers={"content-type": "text/event-stream"})))
    status, body, _ = await invoke(app, body=json.dumps({**CHAT, "stream": True}).encode())
    assert status == 200 and b"invalid_upstream_stream" in body
    assert UPSTREAM_TOKEN.encode() not in body and b"[DONE]" not in body and stream.closed


@pytest.mark.parametrize("streaming", [False, True])
async def test_upstream_response_capacity(streaming):
    line = b'data: {"choices":[{"delta":{},"finish_reason":null}],"fixture":"' + b"x" * 100_000 + b'"}\n\n'
    stream = Stream([line] * 161 if streaming else [b"x" * 1_000_000] * 17)
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: response(stream=stream, headers={"content-type": "text/event-stream" if streaming else "application/json"})))
    status, body, _ = await invoke(app, body=json.dumps({**CHAT, "stream": streaming}).encode())
    assert status == (200 if streaming else 502)
    assert b"upstream_response_too_large" in body and stream.closed


@pytest.mark.parametrize("headers", [
    {"content-type": "text/html"}, {"content-type": "application/json", "content-encoding": "gzip"},
])
async def test_upstream_type_and_encoding_rejected(headers):
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: response({}, headers=headers)))
    status, body, _ = await invoke(app)
    assert status == 502 and b"invalid_upstream_response" in body


async def test_slow_downstream_send_deadline():
    stream = Stream([b'data: {"choices":[{"delta":{},"finish_reason":null}]}\n\n'])
    app = Gateway(TOKEN, UPSTREAM_TOKEN, deadline=0.03, transport=httpx.MockTransport(lambda r: response(stream=stream, headers={"content-type": "text/event-stream"})))
    stalled = False
    async def send(event):
        nonlocal stalled
        if event["type"] == "http.response.body" and not stalled:
            stalled = True
            await asyncio.Event().wait()
    _, body, _ = await asyncio.wait_for(invoke(app, body=json.dumps({**CHAT, "stream": True}).encode(), send_hook=send), 2)
    assert b"deadline_exceeded" in body and stream.closed and app.active == 0


async def test_cleanup_has_finite_grace():
    class SlowClose(Stream):
        async def aclose(self):
            self.closed = True
            await asyncio.Event().wait()
    stream = SlowClose([], stall=True)
    app = Gateway(TOKEN, UPSTREAM_TOKEN, deadline=0.01, transport=httpx.MockTransport(lambda r: response(stream=stream, headers={"content-type": "text/event-stream"})))
    await asyncio.wait_for(invoke(app, body=json.dumps({**CHAT, "stream": True}).encode()), 2)
    await asyncio.sleep(0)
    assert stream.closed and app.active == 0


@pytest.mark.parametrize("extra", [
    [(b"content-length", b"0"), (b"content-length", b"0")],
    [(b"content-length", b"0"), (b"transfer-encoding", b"chunked")],
    [(b"content-encoding", b"gzip")],
])
async def test_ambiguous_or_encoded_request_headers(extra):
    app = Gateway(TOKEN, UPSTREAM_TOKEN)
    headers = [(b"authorization", ("Bearer " + TOKEN).encode())] + extra
    assert (await invoke(app, headers=headers))[0] == 400


async def test_get_body_rejected():
    app = Gateway(TOKEN, UPSTREAM_TOKEN)
    assert (await invoke(app, "/health", method="GET", body=b"unexpected"))[0] == 400


@pytest.mark.parametrize('field,value', [('max_tokens', True), ('max_tokens', -1), ('max_tokens', 32769),
    ('max_tokens', '20'), ('temperature', True), ('temperature', -0.1), ('temperature', 2.1),
    ('temperature', '1'), ('temperature', float('inf'))])
async def test_generation_controls_are_bounded_before_upstream(field, value):
    calls = []
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(lambda r: calls.append(r)))
    status, _, _ = await invoke(app, body=json.dumps({**CHAT, field:value}).encode())
    assert status == 400 and not calls


@pytest.mark.parametrize('path,data', [('/health', {}), ('/health', {'status':'starting'}),
    ('/health', {'status':'error'}), ('/health', {'status':'ok', 'error':'private'}),
    ('/v1/chat/completions', {'choices': [{'message':{'content':42}, 'finish_reason':'stop'}]}),
    ('/v1/chat/completions', {'choices': [{'message':{'content':'partial'}, 'finish_reason':'unknown'}]}),
    ('/v1/embeddings', {'data':[{'index':0, 'embedding':['private']}]}),
    ('/v1/embeddings', {'data':[{'index':0, 'embedding':[float('inf')]}]}),
    ('/v1/rerank', {'results':[{'index':True, 'relevance_score':0.1}]})])
def test_response_schema_fails_closed(path, data):
    from mini.gateway import route_response
    with pytest.raises((ValueError, TypeError, KeyError)):
        route_response(path, data)


@pytest.mark.parametrize('path,data', [
    ('/v1/chat/completions', {'choices':[{'message':{'content':'fixture','diagnostic':TOKEN}, 'finish_reason':'stop', 'log':TOKEN}], 'private':TOKEN}),
    ('/v1/embeddings', {'data':[{'index':0,'embedding':[0.1],'path':TOKEN}], 'private':TOKEN}),
    ('/v1/rerank', {'results':[{'index':0,'relevance_score':0.1,'document':TOKEN}], 'private':TOKEN})])
def test_response_reconstruction_never_copies_upstream_diagnostics(path, data):
    from mini.gateway import route_response
    assert TOKEN not in json.dumps(route_response(path, data))


async def test_stream_reconstructs_fields_and_requires_terminal_before_done():
    from mini.gateway import safe_sse
    from mini.http import Rejected
    chunks = [b'data: '+json.dumps({'choices':[{'delta':{'content':'partial','private':TOKEN},'finish_reason':None}], 'debug':TOKEN}).encode()+b'\n\n', b'data: [DONE]\n\n']
    class Upstream:
        async def aiter_raw(self):
            for chunk in chunks:
                yield chunk
    observed = []
    with pytest.raises(Rejected):
        async for chunk in safe_sse(Upstream()):
            observed.append(chunk)
    assert TOKEN.encode() not in b''.join(observed)
    assert b'[DONE]' not in b''.join(observed)


def test_exponent_overflow_controls_and_response_numbers_fail_closed():
    from mini.gateway import chat_request, number
    from mini.http import Rejected
    with pytest.raises(Rejected):
        chat_request(json.dumps({**CHAT, 'temperature':10**400}).encode())
    with pytest.raises(ValueError):
        number(10**400)


async def test_terminal_usage_trailer_is_strictly_reconstructed():
    from mini.gateway import safe_sse
    chunks = [b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
              b'data: '+json.dumps({'choices':[], 'usage':{'prompt_tokens':1,'completion_tokens':2,'private':TOKEN}, 'private':TOKEN}).encode()+b'\n\n',
              b'data: [DONE]\n\n']
    class Upstream:
        async def aiter_raw(self):
            for chunk in chunks: yield chunk
    result = b''.join([chunk async for chunk in safe_sse(Upstream())])
    assert b'[DONE]' in result and b'completion_tokens' in result and TOKEN.encode() not in result


@pytest.mark.parametrize("count,top_n,valid", [(1, 1, True), (2, 2, True), (1000, 1000, True),
    (1, 2, False), (1000, 1001, False), (1, 999999999, False), (1, True, False), (1, 0, False)])
async def test_rerank_top_n_bounded_before_forwarding(count, top_n, valid):
    calls = []
    def handler(request):
        calls.append(request)
        return response({"results": [{"index": 0, "relevance_score": 1.0}]})
    app = Gateway(TOKEN, UPSTREAM_TOKEN, transport=httpx.MockTransport(handler))
    payload = {"model": "fixture", "query": "fixture", "documents": ["text"] * count,
               "top_n": top_n, "return_documents": False}
    status, _, _ = await invoke(app, "/v1/rerank", body=json.dumps(payload).encode())
    assert status == (200 if valid else 400)
    assert len(calls) == int(valid)
