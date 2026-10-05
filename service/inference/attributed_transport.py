"""Credentials and request bytes cross only an attributed established socket."""
import asyncio
import concurrent.futures
import contextvars
import ctypes
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import ssl
import stat
import struct
import sys
import threading
import time
from types import SimpleNamespace

import httpcore
import httpx
import certifi
from httpcore._backends.auto import AutoBackend

from .local_peer import AuthRefused, ManagedOmlx, process_identity, read_private, tcp_listeners


# Plain-language headline for every attribution refusal. The precise reason
# code stays on `.reason` for the debug export; it is never the headline.
REFUSED_MESSAGE = ("Wisp couldn't verify the local AI engine (oMLX) just now. "
                   "This is usually momentary \u2014 try again. If it keeps happening, "
                   "reopen oMLX.")

# Failures of the inspection itself (lsof exiting non-zero, printing a
# warning, or timing out under load), not findings about the peer. Only these
# are retried; a changed owner, pid, signature or socket is never retried.
TRANSIENT_INSPECTION_REASONS = frozenset({
    'native_inspection_unavailable', 'connection_inspection_unavailable',
})


# The independent inspections of one `DesktopOmlx._listener` run (two listener
# lookups; then ps, lsof -d txt and the signature check for the server and for
# its parent) wait on child processes, so they are overlapped on a small private
# pool.  This is scheduling only: every call still goes through the module-level
# `inspect_command` / `tcp_listeners` (or the instance's own `_process`,
# `_executable`, `_signed_process`), and the results are evaluated afterwards in
# the former serial order, so the first failure raised is the one the serial code
# raised.  The pool's tasks are leaf calls that never wait on the pool, so a full
# pool only delays callers and cannot deadlock them.
_INSPECTION_WORKERS = 8
_worker = threading.local()


def _mark_worker():
    _worker.inside = True


class _InspectionPool:
    """A lazily created, bounded, restartable thread pool (one per process)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._executor = None

    def submit(self, call, *args):
        # Each task runs in a copy of the caller's context, as asyncio.to_thread does.
        context = contextvars.copy_context()
        with self._lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_INSPECTION_WORKERS, thread_name_prefix='wisp-inspect',
                    initializer=_mark_worker)
            return self._executor.submit(context.run, call, *args)

    def shutdown(self, wait=True):
        """Finish queued and running work, then join the workers; new work restarts the pool."""
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=wait and not getattr(_worker, 'inside', False))

    def _reset_in_forked_child(self):
        # The workers do not exist in a forked child; never wait on them there.
        self._lock = threading.Lock()
        self._executor = None


_POOL = _InspectionPool()
if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_POOL._reset_in_forked_child)


class _Deferred:
    """A call that runs when its result is first asked for (the serial fallback)."""

    def __init__(self, call, args):
        self._call, self._args, self._done = call, args, False

    def result(self):
        if not self._done:
            try:
                self._value, self._error = self._call(*self._args), None
            except BaseException as error:  # noqa: BLE001 - re-raised below, unchanged
                self._value, self._error = None, error
            self._done = True
        if self._error is not None:
            raise self._error
        return self._value


class _Inspections:
    """The inspections one `_listener` run started; none of them outlives the run."""

    def __init__(self):
        self._started = []

    def run(self, call, *args):
        # A pool worker asking for a nested `_listener`, or a pool that cannot take
        # work, gets the former serial behaviour: each call runs when evaluated.
        if getattr(_worker, 'inside', False):
            return _Deferred(call, args)
        try:
            future = _POOL.submit(call, *args)
        except RuntimeError:
            return _Deferred(call, args)
        self._started.append(future)
        return future

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        for future in self._started:
            future.cancel()
        concurrent.futures.wait(self._started)
        return False


async def _close_inspection_pool():
    try:
        await asyncio.to_thread(_POOL.shutdown)
    except RuntimeError:  # the loop's default executor is already closed
        _POOL.shutdown(wait=False)


def refused():
    from .inference_errors import ModelLoadError
    error = ModelLoadError(REFUSED_MESSAGE)
    active = sys.exc_info()[1]
    error.reason = str(active) if isinstance(active, AuthRefused) else type(active).__name__ if active else ''
    return error


def connected_peer_with_retry(authority, sock, pid, identity, *, attempts=3, pause=0.1):
    """Verify the connected peer, retrying only transient inspection failures.

    Each attempt is a complete verification; a retry can only turn an
    inspection hiccup into a clean pass, never accept a peer that failed.
    """
    for attempt in range(attempts):
        try:
            return authority.connected_peer(sock, pid, identity)
        except subprocess.TimeoutExpired:
            if attempt == attempts - 1:
                raise
        except AuthRefused as exc:
            if str(exc) not in TRANSIENT_INSPECTION_REASONS or attempt == attempts - 1:
                raise
        time.sleep(pause * (attempt + 1))


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
    app_root = Path('/Applications/oMLX.app')
    app_executable = Path('/Applications/oMLX.app/Contents/MacOS/oMLX')
    python_root = Path('/Applications/oMLX.app/Contents/Resources/Python')
    server_entry = Path('/Applications/oMLX.app/Contents/Resources/omlx/server.py')
    team_id = 'PSK5Q5T46L'

    def __init__(self, manifest=None):
        self.uid = os.getuid()
        self._group_cache = {}
        self._qualified_roots = set()
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

    def _trusted_group(self, gid):
        """Allow group writes only when no other local account is a member.

        The desktop adapter's documented trust boundary excludes compromise of
        Wisp's own login UID. It also treats root and underscore-prefixed macOS
        system UIDs below 500 as part of the platform boundary. This preserves
        oMLX's shipped 0664 Python files without extending trust to a second
        human account that shares the file's group.
        """
        cache = getattr(self, '_group_cache', None)
        if cache is None:
            cache = self._group_cache = {}
        if gid in cache:
            return cache[gid]
        try:
            named = set(grp.getgrgid(gid).gr_mem)
            members = [entry for entry in pwd.getpwall() if entry.pw_gid == gid]
            members.extend(pwd.getpwnam(name) for name in named)
            trusted = all(entry.pw_uid in (0, self.uid)
                          or (0 < entry.pw_uid < 500 and entry.pw_name.startswith('_'))
                          for entry in members)
        except (KeyError, OSError):
            trusted = False
        cache[gid] = trusted
        return trusted

    def _qualified_tree(self, root):
        """Qualify the complete interpreted-code tree and replacement boundaries."""
        try:
            root = Path(root)
            qualified = getattr(self, '_qualified_roots', None)
            if qualified is None:
                qualified = self._qualified_roots = set()
            if root in qualified:
                return
            app_parent = self.app_root.parent
            if (root.resolve(strict=True) != root or self.app_root not in root.parents
                    or app_parent not in root.parents):
                raise AuthRefused('desktop_runtime_unqualified')

            def qualify(info, *, allow_link=False):
                if info.st_uid not in (0, self.uid):
                    raise AuthRefused('desktop_runtime_unqualified')
                if allow_link:
                    if not stat.S_ISLNK(info.st_mode):
                        raise AuthRefused('desktop_runtime_unqualified')
                    return
                if (info.st_mode & 0o002
                        or info.st_mode & 0o020 and not self._trusted_group(info.st_gid)
                        or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                    raise AuthRefused('desktop_runtime_unqualified')
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise AuthRefused('desktop_runtime_unqualified')

            boundary = root
            while True:
                info = boundary.lstat()
                qualify(info)
                if not stat.S_ISDIR(info.st_mode):
                    raise AuthRefused('desktop_runtime_unqualified')
                if boundary == app_parent:
                    break
                boundary = boundary.parent

            def snapshot():
                # One os.scandir pass.  The rules are those of the former
                # `[root, *sorted(root.rglob('*'))]` loop: every entry (dotfiles too)
                # is lstat'ed, qualified and recorded; a symlinked directory is an
                # entry but is never descended; the 50,000 limit is checked before each
                # entry with the root counted; a directory that cannot be listed is
                # skipped, as rglob skipped it.  The inventory is compared as a dict,
                # so enumeration order cannot change the verdict.
                found = {}

                def record(path, info):
                    if len(found) >= 50000:
                        raise AuthRefused('desktop_runtime_unqualified')
                    target = None
                    if stat.S_ISLNK(info.st_mode):
                        qualify(info, allow_link=True)
                        target_path = Path(path).resolve(strict=True)
                        if target_path != root and root not in target_path.parents:
                            raise AuthRefused('desktop_runtime_unqualified')
                        target = str(target_path)
                    else:
                        qualify(info)
                    found[path] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                                   info.st_gid, info.st_size, info.st_mtime_ns,
                                   info.st_ctime_ns, target)

                root_text = str(root)
                record(root_text, os.lstat(root_text))
                pending = [root_text]
                while pending:
                    try:
                        with os.scandir(pending.pop()) as listing:
                            entries = list(listing)
                    except OSError:
                        continue
                    for entry in entries:
                        info = entry.stat(follow_symlinks=False)
                        record(entry.path, info)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(entry.path)
                return found

            inventory = snapshot()
            if snapshot() != inventory:
                raise AuthRefused('desktop_runtime_changed')
            qualified.add(root)
        except AuthRefused:
            raise
        except (OSError, RuntimeError):
            raise AuthRefused('desktop_runtime_unqualified') from None

    def _listener(self):
        with _Inspections() as inspections:
            # `lsof` for the listener and the whole-system listener table are independent.
            listen = inspections.run(lambda: inspect_command(
                ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:LISTEN', '-Fpufn']))
            table = inspections.run(lambda: tcp_listeners())
            raw = listen.result().decode('ascii')
            rows = raw.splitlines()
            if (len(rows) != 4 or not re.fullmatch(r'p[1-9][0-9]*', rows[0])
                    or rows[1] != 'u' + str(self.uid)
                    or not re.fullmatch(r'f[0-9]+', rows[2])
                    or rows[3] != 'n127.0.0.1:8000'):
                raise AuthRefused('desktop_listener_unqualified')
            listeners = table.result()
            if (listeners is None
                    or [(host, port) for host, port in listeners if port == self.port]
                        != [('127.0.0.1', self.port)]):
                raise AuthRefused('desktop_listener_unqualified')
            pid = int(rows[0][1:])
            # Only a pid that passed both listener checks is inspected.  Its ps, lsof -d txt
            # and signature check are independent of each other.
            server_process = inspections.run(self._process, pid)
            server_lookup = inspections.run(self._executable, pid)
            server_signature = inspections.run(self._signed_process, pid, 'python3')
            parent_pid, uid, command = server_process.result()
            if uid != self.uid or command != 'omlx-server':
                raise AuthRefused('desktop_process_unqualified')
            # The parent pid is used only after the server's ps row passed its checks.
            parent_process = inspections.run(self._process, parent_pid)
            parent_lookup = inspections.run(self._executable, parent_pid)
            parent_signature = inspections.run(self._signed_process, parent_pid, 'app.omlx')

            # Every result is evaluated in the former serial order.
            executable = self._qualified_file(server_lookup.result(), parent=self.python_root)
            server_signature.result()
            grandparent_pid, parent_uid, parent_command = parent_process.result()
            if (grandparent_pid != 1 or parent_uid != self.uid
                    or parent_command != str(self.app_executable)):
                raise AuthRefused('desktop_parent_unqualified')
            parent_executable = self._qualified_file(
                parent_lookup.result(), exact=self.app_executable,
                strict_permissions=True)
            parent_signature.result()
            self._qualified_file(self.server_entry, exact=self.server_entry,
                                 strict_permissions=True)
            self._qualified_tree(self.python_root)
            self._qualified_tree(self.server_entry.parent)
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
            actual = await asyncio.to_thread(connected_peer_with_retry, self.authority, self.sock,
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
        await self.check()  # Includes every request-bearing write, including pooled requests.
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
            owner = await asyncio.to_thread(connected_peer_with_retry, authority, sock, pid, identity)
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
        try:
            await self.pool.aclose()
        finally:
            await _close_inspection_pool()
