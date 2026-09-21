"""Credentials and request bytes cross only an attributed established socket."""
import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import ssl
import stat
import struct
from types import SimpleNamespace

import httpcore
import httpx
import certifi
from httpcore._backends.auto import AutoBackend

from .local_peer import AuthRefused, ManagedOmlx, process_identity, read_private, tcp_listeners


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
            return DesktopOmlx(path)
        doc = json.loads(raw)
        if not re.fullmatch('[0-9a-f]{40}', doc.get('source_commit', '')):
            raise AuthRefused('authorization_mismatch')
        return ManagedOmlx(SimpleNamespace(run=inspect_command), path,
                           hashlib.sha256(raw).hexdigest(), doc['source_commit'])


class DesktopOmlx:
    """Attribute the official desktop oMLX server without pinning its version.

    The desktop app updates independently and does not install Wisp's managed
    launchd authorization record.  Its listener is accepted only while it is
    the `omlx-server` child of the fixed oMLX application executable.  Both
    live executable paths are qualified on every binding check; the normal
    PID/incarnation/socket/four-tuple checks still run before request bytes.
    """
    port = 8000
    app_executable = Path('/Applications/oMLX.app/Contents/MacOS/oMLX')
    python_root = Path('/Applications/oMLX.app/Contents/Resources/Python')
    server_entry = Path('/Applications/oMLX.app/Contents/Resources/omlx/server.py')
    team_id = 'PSK5Q5T46L'

    def __init__(self, manifest=None):
        self.uid = os.getuid()
        self.prep = SimpleNamespace(run=inspect_command)
        self.manifest = manifest or Path.home() / '.moe/omlx-runtime-authorization.json'
        self._identity = self._listener()

    def _manifest_absent(self):
        try:
            read_private(self.manifest)
        except FileNotFoundError:
            return
        raise AuthRefused('desktop_authority_superseded')

    def _csops(self, pid, operation, size):
        try:
            library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
            call = library.csops
            call.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
            call.restype = ctypes.c_int
            output = ctypes.create_string_buffer(size)
            if call(pid, operation, output, size) != 0:
                raise AuthRefused('desktop_signature_unqualified')
            return output.raw
        except (OSError, AttributeError):
            raise AuthRefused('desktop_signature_unavailable') from None

    def _csops_string(self, pid, operation):
        raw = self._csops(pid, operation, 256)
        try:
            kind, length = struct.unpack('>II', raw[:8])
            if kind or not 9 <= length <= len(raw) or raw[length - 1] != 0:
                raise ValueError
            value = raw[8:length - 1]
            if not value or b'\0' in value:
                raise ValueError
            return value.decode('ascii')
        except (UnicodeError, ValueError, struct.error):
            raise AuthRefused('desktop_signature_unqualified') from None

    def _signed_process(self, pid, identity):
        flags, = struct.unpack('=I', self._csops(pid, 0, 4))
        # CS_VALID and CS_RUNTIME bind the running pages to the kernel-validated
        # hardened-runtime signature.  Static bundle verification is unsuitable:
        # oMLX legitimately mutates its separately shipped Python resources.
        if flags & 0x00010001 != 0x00010001:
            raise AuthRefused('desktop_signature_unqualified')
        if (self._csops_string(pid, 11) != identity
                or self._csops_string(pid, 14) != self.team_id):
            raise AuthRefused('desktop_signature_unqualified')

    def _process(self, pid):
        raw = inspect_command(['/bin/ps', '-ww', '-p', str(pid),
                               '-o', 'ppid=,uid=,comm='])
        try:
            rows = raw.decode('utf-8').splitlines()
            if len(rows) != 1:
                raise ValueError
            fields = rows[0].strip().split(None, 2)
            if (len(fields) != 3 or not re.fullmatch(r'[1-9][0-9]*', fields[0])
                    or not re.fullmatch(r'0|[1-9][0-9]*', fields[1])):
                raise ValueError
            return int(fields[0]), int(fields[1]), fields[2]
        except (UnicodeError, ValueError):
            raise AuthRefused('desktop_process_unqualified') from None

    def _executable(self, pid):
        raw = inspect_command(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid),
                               '-d', 'txt', '-Fn'])
        try:
            rows = raw.decode('utf-8').splitlines()
        except UnicodeError:
            raise AuthRefused('desktop_executable_unqualified') from None
        paths = [row[1:] for row in rows if row.startswith('n')]
        if not paths:
            raise AuthRefused('desktop_executable_unqualified')
        return Path(paths[0])

    def _qualified_file(self, path, *, exact=None, parent=None, strict_permissions=False):
        try:
            resolved = path.resolve(strict=True)
            info = resolved.stat()
        except (OSError, RuntimeError):
            raise AuthRefused('desktop_executable_unqualified') from None
        if (resolved != path or (exact is not None and resolved != exact)
                or (parent is not None and parent not in resolved.parents)
                or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, self.uid)
                or info.st_mode & (0o022 if strict_permissions else 0o002)):
            raise AuthRefused('desktop_executable_unqualified')
        return str(resolved)

    def _listener(self):
        raw = inspect_command(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000',
                               '-sTCP:LISTEN', '-Fpufn']).decode('ascii')
        rows = raw.splitlines()
        if (len(rows) != 4 or not re.fullmatch(r'p[1-9][0-9]*', rows[0])
                or rows[1] != 'u' + str(self.uid)
                or not re.fullmatch(r'f[0-9]+', rows[2])
                or rows[3] != 'n127.0.0.1:8000'):
            raise AuthRefused('desktop_listener_unqualified')
        listeners = tcp_listeners()
        if (listeners is None
                or [(host, port) for host, port in listeners if port == self.port]
                    != [('127.0.0.1', self.port)]):
            raise AuthRefused('desktop_listener_unqualified')
        pid = int(rows[0][1:])
        parent_pid, uid, command = self._process(pid)
        if uid != self.uid or command != 'omlx-server':
            raise AuthRefused('desktop_process_unqualified')
        executable = self._qualified_file(self._executable(pid), parent=self.python_root)
        self._signed_process(pid, 'python3')

        grandparent_pid, parent_uid, parent_command = self._process(parent_pid)
        if (grandparent_pid != 1 or parent_uid != self.uid
                or parent_command != str(self.app_executable)):
            raise AuthRefused('desktop_parent_unqualified')
        parent_executable = self._qualified_file(
            self._executable(parent_pid), exact=self.app_executable,
            strict_permissions=True)
        self._signed_process(parent_pid, 'app.omlx')
        self._qualified_file(self.server_entry, exact=self.server_entry,
                             strict_permissions=True)
        return pid, executable, parent_pid, parent_executable

    def binding(self, expected_pid=None):
        self._manifest_absent()
        identity = self._listener()
        self._manifest_absent()
        if ((expected_pid is not None and identity[0] != expected_pid)
                or identity != self._identity):
            raise AuthRefused('desktop_listener_changed')
        return identity[0]

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
