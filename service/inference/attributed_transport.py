"""Credentials and request bytes cross only an attributed established socket."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import ssl
import stat
from types import SimpleNamespace

import httpcore
import httpx
import certifi
from httpcore._backends.auto import AutoBackend

from .local_peer import AuthRefused, ManagedOmlx, process_identity, read_private


def refused():
    from .inference_errors import ModelLoadError
    return ModelLoadError('Local inference peer attribution unavailable')


def inspect_command(argv):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                            env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
    if result.returncode or result.stderr.strip() or len(result.stdout) > 1024 * 1024:
        raise AuthRefused('native_inspection_unavailable')
    return result.stdout


class RuntimeAuthority:
    def load(self):
        # Installed only by an independently reviewed adapter qualification.
        # No development flag, guessed CLI process, or health response grants trust.
        path = Path.home() / '.moe/omlx-runtime-authorization.json'
        try:
            raw = read_private(path)
        except FileNotFoundError:
            return DesktopOmlx()
        doc = json.loads(raw)
        if not re.fullmatch('[0-9a-f]{40}', doc.get('source_commit', '')):
            raise AuthRefused('authorization_mismatch')
        return ManagedOmlx(SimpleNamespace(run=inspect_command), path,
                           hashlib.sha256(raw).hexdigest(), doc['source_commit'])


class DesktopOmlx:
    """Attribute the official desktop oMLX listener without pinning a release.

    oMLX.app updates independently and does not install Wisp's launchd
    qualification record.  We still fail closed before request bytes unless a
    single loopback listener belongs to this uid and its live executable is the
    regular Python runtime inside /Applications/oMLX.app.  The established
    socket then receives the same PID/UID/four-tuple checks as managed oMLX.
    """
    port = 8000

    def __init__(self):
        self.uid = os.getuid()
        self.prep = SimpleNamespace(run=inspect_command)
        self._pid, self._executable = self._listener()

    def _listener(self):
        raw = inspect_command(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000',
                               '-sTCP:LISTEN', '-Fpufn']).decode('ascii')
        rows = raw.splitlines()
        if (len(rows) != 4 or not re.fullmatch(r'p[1-9][0-9]*', rows[0])
                or rows[1] != 'u' + str(self.uid)
                or not re.fullmatch(r'f[0-9]+', rows[2])
                or rows[3] != 'n127.0.0.1:8000'):
            raise AuthRefused('desktop_listener_unqualified')
        pid = int(rows[0][1:])
        text = inspect_command(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid),
                                '-d', 'txt', '-Fn']).decode('utf-8').splitlines()
        paths = [line[1:] for line in text if line.startswith('n')]
        if not paths:
            raise AuthRefused('desktop_executable_unqualified')
        executable = Path(paths[0])
        prefix = Path('/Applications/oMLX.app/Contents/Resources/Python')
        info = executable.stat()
        if (executable.resolve(strict=True) != executable or prefix not in executable.parents
                or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, self.uid)
                or info.st_mode & 0o022):
            raise AuthRefused('desktop_executable_unqualified')
        return pid, str(executable)

    def binding(self, expected_pid=None):
        pid, executable = self._listener()
        if ((expected_pid is not None and pid != expected_pid)
                or pid != self._pid or executable != self._executable):
            raise AuthRefused('desktop_listener_changed')
        return pid

    def connected_peer(self, sock, pid, incarnation):
        return ManagedOmlx.connected_peer(self, sock, pid, incarnation)


class CheckedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, authority, epoch, backend, identity, owner, sock):
        self.stream, self.authority, self.epoch, self.backend = stream, authority, epoch, backend
        self.identity, self.owner, self.sock = identity, owner, sock

    async def check(self):
        try:
            if self.backend.epoch != self.epoch or self.stream.get_extra_info('socket') is not self.sock:
                raise AuthRefused('connection_invalidated')
            actual = await asyncio.to_thread(self.authority.connected_peer, self.sock,
                                            self.identity[0], self.identity)
            if (actual != self.owner or self.backend.epoch != self.epoch
                    or self.stream.get_extra_info('socket') is not self.sock):
                raise AuthRefused('connected_peer_changed')
        except Exception:
            await self.stream.aclose()
            raise refused() from None

    async def write(self, buffer, timeout=None):
        if not buffer:
            return
        await self.check()  # Includes every header/body write, including pooled requests.
        return await self.stream.write(buffer, timeout)

    async def read(self, max_bytes, timeout=None):
        return await self.stream.read(max_bytes, timeout)

    async def aclose(self):
        await self.stream.aclose()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    async def start_tls(self, *args, **kwargs):
        await self.aclose()
        raise refused()


class CheckedBackend(AutoBackend):
    def __init__(self, authority, port):
        self.authority, self.port, self.epoch = authority, port, 0

    async def connect_tcp(self, host, port, **kwargs):
        epoch = self.epoch
        if host != '127.0.0.1' or port != self.port:
            raise refused()
        stream = await super().connect_tcp(host, port, **kwargs)
        try:
            authority = await asyncio.to_thread(self.authority.load)
            pid = await asyncio.to_thread(authority.binding)
            identity = await asyncio.to_thread(process_identity, pid, authority.uid)
            sock = stream.get_extra_info('socket')
            owner = await asyncio.to_thread(authority.connected_peer, sock, pid, identity)
            if self.epoch != epoch:
                raise AuthRefused('connection_invalidated')
            return CheckedStream(stream, authority, epoch, self, identity, owner, sock)
        except Exception:
            await stream.aclose()
            raise refused() from None


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        try:
            async for part in self.stream:
                yield part
        except httpcore.NetworkError:
            raise httpx.ReadError('Inference connection unavailable') from None

    async def aclose(self):
        await self.stream.aclose()


class CredentialTransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url, key, *, managed, authority=None, key_loader=None):
        self.origin, self.key = httpx.URL(base_url), key
        self.key_loader = key_loader
        self.backend = CheckedBackend(authority or RuntimeAuthority(), self.origin.port) if managed else None
        self.pool = httpcore.AsyncConnectionPool(ssl_context=ssl.create_default_context(cafile=certifi.where()),
                                               network_backend=self.backend, retries=0,
                                               max_connections=10, max_keepalive_connections=0)

    def invalidate(self):
        if self.backend:
            self.backend.epoch += 1

    async def handle_async_request(self, request):
        if (request.url.scheme, request.url.host, request.url.port) != (
                self.origin.scheme, self.origin.host, self.origin.port):
            raise refused()
        if self.key_loader is not None:
            # Keychain may prompt/block. Cancellation must remain responsive;
            # no socket or request bytes exist until resolution succeeds.
            key = await asyncio.to_thread(self.key_loader)
            if not key:
                raise refused()
            self.key = key
            self.key_loader = None
        request.headers.pop('Authorization', None)
        headers = list(request.headers.raw)
        if self.key:
            headers.append((b'Authorization', ('Bearer ' + self.key).encode('ascii')))
        try:
            response = await self.pool.handle_async_request(httpcore.Request(
                method=request.method, url=httpcore.URL(scheme=request.url.raw_scheme,
                    host=request.url.raw_host, port=request.url.port, target=request.url.raw_path),
                headers=headers, content=request.stream, extensions=request.extensions))
            return httpx.Response(response.status, headers=response.headers,
                                  stream=ResponseStream(response.stream), extensions=response.extensions)
        except (httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError):
            self.invalidate()
            raise httpx.ConnectError('Inference connection unavailable') from None

    async def aclose(self):
        self.invalidate()
        await self.pool.aclose()
