"""Small ASGI boundary with bounded intake, admission and disconnect handling."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hmac
import json
import math


class Rejected(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def credential(value: str) -> bytes:
    # Tokens are opaque printable ASCII, never interpreted or logged.
    if not isinstance(value, str) or not 32 <= len(value) <= 512 or any(
        ord(c) < 33 or ord(c) > 126 for c in value
    ):
        raise ValueError("Invalid credential configuration")
    return value.encode("ascii")


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


class Reply:
    def __init__(self, send):
        self.send, self.started, self.ended, self.sse = send, False, False, False

    async def start(self, status=200, content_type=b"application/json"):
        self.sse = content_type == b"text/event-stream"
        self.started = True
        await self.send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", content_type), (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]})

    async def body(self, body: bytes, *, more=False):
        await self.send({"type": "http.response.body", "body": body, "more_body": more})
        self.ended = not more

    async def json(self, value, status=200):
        body = encode(value)
        await self.start(status)
        await self.body(body)

    async def error(self, status, code):
        if not self.started:
            await self.json({"error": {"code": code}}, status)
        elif not self.ended and self.sse:
            # Fail visibly to OMLXClient; never invent a success terminal / DONE.
            await self.body(b"\n\ndata: " + encode({"error": {"code": code}}) + b"\n\n")


class Boundary:
    routes: frozenset[tuple[str, str]] = frozenset()

    def __init__(self, token: str, *, concurrency=4, deadline=120.0, max_body=2_000_000):
        self._credential = b"Bearer " + credential(token)
        if type(concurrency) is not int or not 1 <= concurrency <= 64:
            raise ValueError("Invalid concurrency")
        if not math.isfinite(deadline) or not 0 < deadline <= 600:
            raise ValueError("Invalid deadline")
        if type(max_body) is not int or not 1 <= max_body <= 2_000_000:
            raise ValueError("Invalid request limit")
        self.concurrency, self.deadline, self.max_body = concurrency, deadline, max_body
        self.active = 0

    def headers(self, scope):
        pairs = scope.get("headers", [])
        if len(pairs) > 64 or sum(len(k) + len(v) for k, v in pairs) > 16_384:
            raise Rejected(431, "headers_too_large")
        headers = {}
        for key, value in pairs:
            key = key.lower()
            headers.setdefault(key, []).append(value)
        if headers.get(b"authorization") is None or len(headers[b"authorization"]) != 1:
            raise Rejected(401, "unauthorized")
        if not hmac.compare_digest(headers[b"authorization"][0], self._credential):
            raise Rejected(401, "unauthorized")
        for key in (b"content-length", b"content-type", b"transfer-encoding"):
            if len(headers.get(key, [])) > 1:
                raise Rejected(400, "invalid_headers")
        if b"content-encoding" in headers or (
            b"transfer-encoding" in headers and b"content-length" in headers
        ):
            raise Rejected(400, "invalid_headers")
        return {k: v[0] for k, v in headers.items()}

    async def read_body(self, scope, receive, headers):
        length = headers.get(b"content-length")
        if length is not None:
            if not length.isdigit() or len(length) > 10:
                raise Rejected(400, "invalid_length")
            if int(length) > self.max_body:
                raise Rejected(413, "request_too_large")
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                raise asyncio.CancelledError
            if event["type"] != "http.request":
                raise Rejected(400, "invalid_request")
            body.extend(event.get("body", b""))
            if len(body) > self.max_body:
                raise Rejected(413, "request_too_large")
            if not event.get("more_body", False):
                break
        if length is not None and len(body) != int(length):
            raise Rejected(400, "invalid_length")
        if scope["method"] == "GET" and body:
            raise Rejected(400, "unexpected_body")
        return bytes(body)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        reply = Reply(send)
        admitted = False
        try:
            headers = self.headers(scope)
            path = scope.get("path", "")
            if ((scope["method"], path) not in self.routes
                    or scope.get("raw_path", path.encode()) != path.encode()):
                raise Rejected(404, "not_found")
            if len(scope.get("query_string", b"")) > 2048:
                raise Rejected(400, "invalid_query")
            if self.active >= self.concurrency:
                raise Rejected(429, "busy")
            self.active += 1  # no await between checking and reserving a slot
            admitted = True
            async with asyncio.timeout(self.deadline):
                body = await self.read_body(scope, receive, headers)
                await self.with_disconnect(scope, body, headers, receive, reply)
        except asyncio.CancelledError:
            raise
        except Rejected as exc:
            await self.report(reply, exc.status, exc.code)
        except TimeoutError:
            await self.report(reply, 504, "deadline_exceeded")
        except Exception:
            # Neither upstream exceptions nor user data enter logs/responses.
            await self.report(reply, 503, "unavailable")
        finally:
            if admitted:
                self.active -= 1

    async def report(self, reply, status, code):
        with suppress(Exception):
            async with asyncio.timeout(1):
                await reply.error(status, code)

    async def with_disconnect(self, scope, body, headers, receive, reply):
        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass
        work = asyncio.create_task(self.handle(scope, body, headers, reply))
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
        finally:
            for task in (work, watcher):
                if not task.done():
                    task.cancel()
            # A bounded cleanup grace also covers an unresponsive transport close.
            finished, pending = await asyncio.wait((work, watcher), timeout=1)
            await asyncio.gather(*finished, return_exceptions=True)
            for task in pending:
                task.cancel()
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def handle(self, scope, body, headers, reply):
        raise NotImplementedError
