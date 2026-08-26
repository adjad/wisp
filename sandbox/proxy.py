"""Streaming reverse proxy from the sandbox server to the sandbox backend.

The browser never talks to the backend directly — partly because
service/main.py has zero CORS middleware (a browser page on :8766 calling
:8775 would just be blocked), and partly because the sandbox server IS the
fake Swift app: routing everything through one process keeps the /agent
stream, the sync pushes, and the outbound action loop all talking to the
same httpx client with the same timeouts.

Three details that are easy to get wrong and silently break SSE:
  1. `stream=True` + `r.aiter_raw()` — buffering here would turn /agent's
     token-by-token stream into one blob delivered after the whole turn
     finishes, which defeats the entire point of watching it think.
  2. `content-length` must NOT be forwarded on a chunked/streamed response —
     a stale content-length on an SSE response can hang a browser waiting
     for bytes that already arrived.
  3. `BackgroundTask(response.aclose)` — without it, closing the browser tab
     mid-turn leaks the upstream connection instead of cancelling it.
"""
from __future__ import annotations

import os

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse

BACKEND_URL = os.environ.get("WISP_BACKEND_URL", "http://127.0.0.1:8775")

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "host",
}


def make_client() -> httpx.AsyncClient:
    # 600s matches WispClient.swift's /agent timeout — the real client's
    # generous budget for a slow local-model turn.
    return httpx.AsyncClient(base_url=BACKEND_URL,
                             timeout=httpx.Timeout(600.0, connect=5.0))


def _forward_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


async def proxy_request(client: httpx.AsyncClient, path: str, request: Request) -> StreamingResponse:
    body = await request.body()
    req = client.build_request(
        request.method, "/" + path,
        params=request.query_params,
        content=body,
        headers=_forward_headers(request.headers))
    upstream = await client.send(req, stream=True)
    resp_headers = _forward_headers(upstream.headers)
    if "text/event-stream" in (upstream.headers.get("content-type") or ""):
        resp_headers["Cache-Control"] = "no-cache"
        resp_headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=resp_headers,
        background=BackgroundTask(upstream.aclose))
