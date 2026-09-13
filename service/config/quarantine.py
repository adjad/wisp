"""Process-latched recovery quarantine and credential-use/provisioning leases.

A successful provisioning writer must hold the same exclusive flock. Existing
requests drain before marker publication; new requests cannot cross that boundary.
Removing a marker never revives credentials cached by an invalidated process.
"""
from __future__ import annotations

from contextlib import contextmanager
import asyncio
import fcntl
import os
from pathlib import Path
import re
import stat
import weakref

import httpx


class CredentialQuarantined(RuntimeError):
    def __init__(self):
        super().__init__("Credential recovery required; restart backend after recovery")


class RecoveryGate:
    def __init__(self, home=None, expected=None):
        self.home = home or Path.home
        self.expected = expected
        self.blocked = False
        self.callbacks = []
        self.clients = weakref.WeakSet()
        self.active = set()

    @property
    def directory(self):
        return Path(self.home()) / ".moe"

    def invalidate(self):
        if not self.blocked:
            self.blocked = True
            for client in self.clients:
                client.headers.pop("Authorization", None)
            for callback in self.callbacks:
                callback()
        raise CredentialQuarantined()

    def generation(self):
        parent = self.directory
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return "absent"
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError("unsafe recovery directory")
        try:
            (parent / ".helper-transaction.json").lstat()
        except FileNotFoundError:
            pass
        else:
            raise OSError("recovery marker present")
        try:
            fd = os.open(parent / ".credential-generation", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return "absent"
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 64):
                raise OSError("unsafe credential generation")
            value = source.read(65)
        if not re.fullmatch(rb"[0-9a-f]{64}", value):
            raise OSError("invalid credential generation")
        return value.decode("ascii")

    def check(self):
        if self.blocked:
            raise CredentialQuarantined()
        try:
            current = self.generation()
        except OSError:
            self.invalidate()
        if self.expected is None:
            self.expected = current
        if self.expected != current:
            self.invalidate()

    @contextmanager
    def lease(self):
        self.check()
        fd = None
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(self.directory / ".provisioning.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                self.invalidate()
            # Never synchronously wait on a writer from an async event loop.
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                # Read-only provisioning status also holds this lock. Refuse
                # this dispatch, but latch only a marker/epoch/unsafe-state change.
                raise CredentialQuarantined() from None
            self.check()
            yield
            self.check()
        except OSError:
            self.invalidate()
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


_gate = RecoveryGate(expected=os.environ.pop("WISP_CREDENTIAL_GENERATION", None))


def check():
    _gate.check()


@contextmanager
def lease():
    with _gate.lease():
        yield


class _GuardedStream(httpx.AsyncByteStream):
    def __init__(self, stream, gate, held, task):
        self.stream, self.gate, self.held, self.task = stream, gate, held, task

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                self.gate.check()
                yield chunk
            self.gate.check()
        finally:
            await self.aclose()

    async def aclose(self):
        if self.held is not None:
            held, self.held = self.held, None
            try:
                await self.stream.aclose()
            finally:
                self.gate.active.discard(self.task)
                held.__exit__(None, None, None)


def guard_client(client):
    """Guard even clients whose Authorization header was cached before recovery."""
    gate = _gate
    original = client.send
    gate.check()
    gate.clients.add(client)
    async def send(request, *args, **kwargs):
        held = gate.lease()
        held.__enter__()
        task = asyncio.current_task()
        gate.active.add(task)
        response = None
        try:
            response = await original(request, *args, **kwargs)
            gate.check()
            if kwargs.get("stream", False):
                response.stream = _GuardedStream(response.stream, gate, held, task)
                held = None
            return response
        except BaseException:
            if response is not None:
                await response.aclose()
            raise
        finally:
            if held is not None:
                gate.active.discard(task)
                held.__exit__(None, None, None)
    client.send = send
    return client


_active_requests = set()


class RecoveryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        try:
            check()
        except CredentialQuarantined:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                body = b'{"error":"credential_recovery_required"}'
                await send({"type": "http.response.start", "status": 503,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
            return
        task = asyncio.current_task()
        _active_requests.add(task)
        async def guarded_send(message):
            check()
            await send(message)
        try:
            await self.app(scope, receive, guarded_send)
        finally:
            _active_requests.discard(task)


async def watch(tasks=(), close=None, *, interval=0.1):
    """Quarantine active work; keep the server unavailable until fresh restart."""
    while True:
        try:
            check()
        except CredentialQuarantined:
            victims = set(tasks) | _active_requests.copy() | _gate.active.copy()
            victims.discard(asyncio.current_task())
            for task in victims:
                task.cancel()
            await asyncio.gather(*victims, return_exceptions=True)
            if close is not None:
                await close()
            return
        await asyncio.sleep(interval)
