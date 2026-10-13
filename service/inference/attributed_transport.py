"""Credentials and request bytes cross only an attributed established socket."""
import asyncio
import concurrent.futures
import contextvars
import ctypes
import grp
import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
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

from .attestation_lease import ClockSample, QualificationKey, QualificationLease, lease_usable
from .attestation_store import AttestationStore, StoreLimits
from .local_peer import (AuthRefused, ManagedOmlx, NativeAnomaly, native_connection_owner, process_bsdinfo,
                         process_identity, process_path, read_private, tcp_listeners)


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
# lookups; then ps, the executable path and the signature check for the server and for
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


def _close_inspection_pool():
    """Release the inspection pool's threads WITHOUT suspending the caller.

    Closing a transport runs inside cancellation and deadline scopes (a connection
    test that times out, a cancelled turn). An `await` here is a point where those
    scopes can interrupt the close before its caller's bookkeeping runs, so this is
    deliberately synchronous: `shutdown(wait=False)` only detaches the executor.
    Running and queued inspections (of this or any other transport) still finish,
    and each `_listener` run waits for its own futures in `_Inspections.__exit__`;
    the next inspection simply recreates the pool.
    """
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
        # The kernel's answer (libproc proc_pidpath) replaces the `lsof -d txt` spawn.
        # Same path for every real process, the same refusals, never more permissive than
        # lsof (see local_peer.process_path); `_qualified_file` still decides on the Path.
        return Path(process_path(pid))

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
            # Only a pid that passed both listener checks is inspected.  Its ps, executable
            # path and signature check are independent of each other.
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

    # -- exec-aware incarnation facts (P1/P2). Read-only; used only by the reuse path. --
    def _incarnation_facts(self, pid, signing_identity):
        """Exec-sensitive facts of one process, bracketed by two bsdinfo reads.

        ``(pid, ppid, uid, ruid, svuid, comm, name, start_s, start_us, path,
        cs_flags, cdhash, signing identifier, team)``. Exec into another binary
        changes comm/name/path/cdhash/identifier (and usually team); a change
        while these calls run is refused rather than returning a torn mixture.
        Any failure raises; ``signing_identity`` is only checked by
        ``_facts_qualified``.
        """
        before = process_bsdinfo(pid)
        path = process_path(pid)
        flags, = struct.unpack('=I', self._csops(pid, 0, 4))
        cdhash = self._csops(pid, 5, 20)          # CS_OPS_CDHASH
        identifier = self._csops_string(pid, 11)  # CS_OPS_IDENTITY
        team = self._csops_string(pid, 14)        # CS_OPS_TEAMID
        if process_bsdinfo(pid) != before:
            raise AuthRefused('desktop_incarnation_unstable')
        return (*before, path, int(flags), bytes(cdhash), identifier, team)

    def _boundary_fingerprints(self, executable, parent_executable):
        """lstat equality of both executables, server_entry and every boundary
        directory from the two qualified trees up to the app's parent."""
        paths = [Path(executable), Path(parent_executable), self.server_entry]
        stop = self.app_root.parent
        for root in (self.python_root, self.server_entry.parent):
            boundary = root
            while True:
                if boundary not in paths:
                    paths.append(boundary)
                if boundary == stop or boundary == boundary.parent:
                    break
                boundary = boundary.parent
        rows = []
        for path in paths:
            info = os.lstat(path)
            rows.append((str(path), info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                         info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        return tuple(rows)

    def _facts_qualified(self, server, parent):
        """The binding predicates that native facts can express (argv excluded: R2/D2)."""
        try:
            (s_pid, s_ppid, s_uid, s_ruid, s_svuid, _c, _n, _s, _u, s_path, s_flags, s_cdhash,
             s_identifier, s_team) = server
            (p_pid, p_ppid, p_uid, p_ruid, p_svuid, _pc, _pn, _ps, _pu, p_path, p_flags, p_cdhash,
             p_identifier, p_team) = parent
        except (TypeError, ValueError):
            return False
        return bool(
            (s_uid, s_ruid, s_svuid) == (self.uid,) * 3 and (p_uid, p_ruid, p_svuid) == (self.uid,) * 3
            and s_ppid == p_pid and p_ppid == 1 and s_pid != p_pid
            and s_flags & 0x00010001 == 0x00010001 and p_flags & 0x00010001 == 0x00010001
            and len(s_cdhash) == 20 and len(p_cdhash) == 20
            and s_identifier == 'python3' and p_identifier == 'app.omlx'
            and s_team == self.team_id and p_team == self.team_id
            and self.python_root in Path(s_path).parents and p_path == str(self.app_executable))

    def _observe(self, server_pid, parent_pid, executable, parent_executable):
        """One observation: (server facts, parent facts, boundary fingerprints)."""
        return (self._incarnation_facts(server_pid, 'python3'),
                self._incarnation_facts(parent_pid, 'app.omlx'),
                self._boundary_fingerprints(executable, parent_executable))


# ===========================================================================
# Attestation reuse: P1 per-write fence, P2 per-incarnation lease, P3 shadow.
#
# What is reused: only the facts that change when oMLX restarts or its files
# change (the constructor's two tree walks and group lookups, its listener
# identity). Which facts are checked is unchanged; every use re-reads the
# exec-aware incarnation facts natively and compares them with the ones
# bracketed around a full qualification. Decisions recorded in the PR:
#   D1  W_hard = 60 s: no write uses a tree/group audit that started >= 60 s ago.
#   D2  R2 accepted: argv[0] / exec into the same binary is not re-read per write
#       (still read per connection by the full binding()).
#   D3  native socket-owner lookup runs in SHADOW only; lsof decides.
#   D4  the connect-time binding() stays a full per-connection check.
# Every failure, ambiguity, expiry, counter exhaustion or clock problem falls
# back to the complete legacy check; nothing here can turn a refusal into an
# accept. WISP_ATTESTATION_LEGACY=1 restores exactly the former behaviour.
# ===========================================================================
LEGACY_ENV = 'WISP_ATTESTATION_LEGACY'
SHADOW_ENV = 'WISP_ATTESTATION_NATIVE_SHADOW'
_SECOND = 1_000_000_000
W_HARD_NS = 60 * _SECOND             # D1: absolute lease length from qualification start
W_SOFT_NS = 40 * _SECOND             # a use after this age starts one background re-walk
CONNECT_MARGIN_NS = 10 * _SECOND     # a NEW connection needs this much lease left
T_BUILD_NS = 30 * _SECOND            # a build/refresh loses publication authority after this
CLOCK_DOMAIN = 'darwin-CLOCK_MONOTONIC-sleep-inclusive'
QUALIFICATION_RULE = 'desktop-omlx-exec-facts-v1'
REUSE_POLICY = 'attestation-reuse-p1p2-v1'
_BUILDER = 'process-qualification-builder'
_LIMITS = StoreLimits(key_capacity=8, transport_capacity=4, nonce_capacity=2 ** 40,
                      counter_ceiling=2 ** 40, record_units=16384, fact_depth=6)
_log = logging.getLogger('wisp.attestation')


def _sleep_inclusive_ns():
    """Darwin CLOCK_MONOTONIC keeps counting while the Mac sleeps (clock_gettime(3));
    time.monotonic() does not, so it must never bound freshness."""
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class _FenceUnavailable(Exception):
    """The optimized path cannot decide (expiry, revocation, native error, policy).

    Never an AuthRefused: it is not retried, not a finding, and always sends the
    caller to the complete legacy check."""


class _DesktopReuse:
    """Process-wide qualified-authority lease for the desktop oMLX (P2).

    Locks: ``_lock`` is a short metadata lock (clock sample + store call + map
    update); it is never held across I/O, a native call, a subprocess or an
    await. Lock order is always ``_lock`` then the store's own lock.
    """

    def __init__(self, clock=None):
        self._clock_override = clock
        self._reset()

    def _reset(self):
        self._lock = threading.Lock()
        self.lifetime = secrets.token_hex(16)
        self.store = AttestationStore(process_lifetime=self.lifetime, policy_revision=REUSE_POLICY,
                                      clock_domain_id=CLOCK_DOMAIN, limits=_LIMITS)
        self.store.register_transport(_BUILDER)
        self._authorities = {}
        self._last_ns = 0
        self._rolled_back = False
        self._native_latched = False
        self.counters = {}

    # -- bookkeeping ------------------------------------------------------
    def _count(self, name):
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + 1

    def _clock_supported(self):
        return self._clock_override is not None or (
            sys.platform == 'darwin' and hasattr(time, 'clock_gettime_ns') and hasattr(time, 'CLOCK_MONOTONIC'))

    def _sample_locked(self):
        if not self._clock_supported():
            raise _FenceUnavailable('clock_unsupported')
        try:
            now = (self._clock_override or _sleep_inclusive_ns)()
        except Exception:  # noqa: BLE001 - any clock failure disables the optimized path
            raise _FenceUnavailable('clock_error') from None
        if type(now) is not int or now < 0:
            raise _FenceUnavailable('clock_error')
        if now < self._last_ns:
            self.store.disable('rollback')      # one-way: a regressing clock never certifies again
            self._authorities.clear()
            raise _FenceUnavailable('clock_regression')
        sample = ClockSample(CLOCK_DOMAIN, now, self._last_ns)
        self._last_ns = now
        return sample

    def legacy(self):
        """Kill switch. One-way: once seen, optimized admission stays off for this process."""
        if self._rolled_back:
            return True
        if os.environ.get(LEGACY_ENV, '') not in ('', '0'):
            with self._lock:
                self._rolled_back = True
                self.store.disable('rollback')
                self._authorities.clear()
            _log.info('attestation_reuse_rolled_back')
            return True
        return False

    @staticmethod
    def manifest_path():
        return Path.home() / '.moe/omlx-runtime-authorization.json'

    @staticmethod
    def key_for(manifest):
        return QualificationKey(
            uid=os.getuid(), port=DesktopOmlx.port, app_root=str(DesktopOmlx.app_root),
            app_executable=str(DesktopOmlx.app_executable), python_root=str(DesktopOmlx.python_root),
            server_entry=str(DesktopOmlx.server_entry), manifest_path=str(manifest), manifest_absent=True,
            qualification_rule_revision=QUALIFICATION_RULE, policy_revision=REUSE_POLICY)

    def revoke(self, key, reason):
        with self._lock:
            self.store.revoke_key(key)
            self._authorities.clear()
        self._count('revoke:' + reason)
        _log.debug('attestation_revoked reason=%s', reason)

    def invalidate(self):
        """CredentialTransport.invalidate(): conservatively revoke the current lease."""
        try:
            self.revoke(self.key_for(self.manifest_path()), 'invalidate')
        except Exception:  # noqa: BLE001 - a failure here must not mask the caller's invalidate
            with self._lock:
                self.store.disable('rollback')
                self._authorities.clear()

    def usable(self, lease):
        """LP-W / post-await check: unexpired immutable lease of the current generation."""
        if self.legacy():
            return False
        try:
            with self._lock:
                sample = self._sample_locked()
                snapshot = self.store.snapshot(lease.key)
                return snapshot is not None and lease_usable(lease, snapshot, sample)
        except Exception:  # noqa: BLE001 - never usable on doubt
            return False

    # -- connect-time acquisition ------------------------------------------
    def acquire(self, runtime):
        """Return ``(authority, lease)`` for a new connection, ``(authority, None)``
        after a fresh full load that could not be published, or ``None`` (caller
        runs the unchanged legacy load). Never waits for a background build."""
        if self.legacy():
            return None
        manifest = self.manifest_path()
        key = self.key_for(manifest)
        try:
            read_private(manifest)
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - unsafe/unreadable state: legacy load decides (refuses)
            self.revoke(key, 'manifest')
            return None
        else:
            self.revoke(key, 'manifest')        # managed authority: unchanged legacy path
            return None
        hit = self._reuse(runtime, key)
        if hit is not None:
            return hit
        return self._build(runtime, key)

    def _reuse(self, runtime, key):
        try:
            with self._lock:
                sample = self._sample_locked()
                lease = self.store.lookup(key, sample)
                authority = None if lease is None else self._authorities.get(lease.authority_reference)
        except _FenceUnavailable:
            return None
        if lease is None or authority is None:
            self._count('miss')
            return None
        if sample.now_ns >= lease.expiry_ns - CONNECT_MARGIN_NS:
            self._count('miss_margin')
            return None
        server_pid, parent_pid = lease.server_facts[0], lease.parent_facts[0]
        try:
            observed = authority._observe(server_pid, parent_pid, lease.server_facts[9],
                                          lease.parent_facts[9])
        except Exception:  # noqa: BLE001 - restart/vanished pid/native error: requalify from scratch
            self.revoke(key, 'revalidate_unavailable')
            return None
        if (observed != (lease.server_facts, lease.parent_facts, lease.boundary_fingerprints)
                or not authority._facts_qualified(observed[0], observed[1])):
            self.revoke(key, 'revalidate_changed')
            return None
        if sample.now_ns - lease.qualification_start_ns >= W_SOFT_NS:
            self._refresh(runtime, key)
        self._count('reuse')
        return authority, lease

    def _reserve(self, key):
        try:
            with self._lock:
                sample = self._sample_locked()
                reservation = self.store.reserve(key, _BUILDER, sample,
                                                 build_deadline_ns=sample.now_ns + T_BUILD_NS)
        except _FenceUnavailable:
            return None, None
        if reservation is None or not reservation.started:
            return None, None       # budget exhausted or another job owns the key: never wait for it
        return reservation.token, sample.now_ns

    def _settle(self, token, *, failed):
        with self._lock:
            if failed:
                self.store.fail(token)
            self.store.job_finished(token)

    def _build(self, runtime, key):
        token, start_ns = self._reserve(key)   # start is read before any qualification input
        if token is None:
            return None
        failed = True
        try:
            authority = runtime.load()             # the full, unchanged legacy qualification
            if not isinstance(authority, DesktopOmlx):
                return authority, None
            lease = self._qualify(authority, key, token, start_ns)
            failed = lease is None
            self._count('build' if lease is not None else 'build_unpublished')
            return authority, lease
        finally:
            self._settle(token, failed=failed)

    def _refresh(self, runtime, key):
        token, start_ns = self._reserve(key)
        if token is None:
            return
        thread = threading.Thread(target=self._run_refresh, args=(runtime, key, token, start_ns),
                                  name='wisp-attest-refresh', daemon=True)
        try:
            thread.start()
        except Exception:  # noqa: BLE001 - could not start: no job exists
            self._settle(token, failed=True)

    def _run_refresh(self, runtime, key, token, start_ns):
        failed = True
        try:
            authority = runtime.load()
            if isinstance(authority, DesktopOmlx):
                failed = self._qualify(authority, key, token, start_ns) is None
            self._count('refresh_ok' if not failed else 'refresh_refused')
        except BaseException as error:  # noqa: BLE001 - a failed refresh revokes; it never extends freshness
            self._count('refresh_error:' + type(error).__name__)
        finally:
            self._settle(token, failed=failed)

    def _qualify(self, authority, key, token, start_ns):
        """Bracket a full listener re-check with two equal native observations, then publish."""
        pid, executable, parent_pid, parent_executable = authority._identity
        try:
            before = authority._observe(pid, parent_pid, executable, parent_executable)
            authority.binding()
            after = authority._observe(pid, parent_pid, executable, parent_executable)
        except Exception:  # noqa: BLE001 - unpublishable; the connection's own checks decide
            return None
        server, parent, prints = before
        if (before != after or server[0] != pid or server[1] != parent_pid or server[9] != executable
                or parent[0] != parent_pid or parent[9] != parent_executable
                or not authority._facts_qualified(server, parent)):
            return None
        reference = secrets.token_hex(16)
        try:
            lease = QualificationLease(
                key=key, qualification_id=reference, global_generation=token.global_generation,
                key_generation=token.key_generation, clock_domain_id=CLOCK_DOMAIN,
                process_lifetime=self.lifetime, server_facts=server, parent_facts=parent,
                boundary_fingerprints=prints, authority_reference=reference,
                qualification_start_ns=start_ns, expiry_ns=start_ns + W_HARD_NS)
            with self._lock:
                sample = self._sample_locked()
                if not self.store.publish(token, lease, sample):
                    return None
                self._authorities = {reference: authority}
        except (_FenceUnavailable, ValueError):
            return None
        return lease

    # -- per-write fence (P1) ------------------------------------------------
    def fence(self, authority, lease, expected_pid):
        """Replaces the two per-write binding() calls on an optimized stream."""
        server_pid, parent_pid = lease.server_facts[0], lease.parent_facts[0]
        if expected_pid != server_pid:
            raise AuthRefused('desktop_listener_changed')
        if not self.usable(lease):
            raise _FenceUnavailable('lease')
        try:
            authority._manifest_absent()
        except AuthRefused:
            self.revoke(lease.key, 'manifest')
            raise
        try:
            server, parent, prints = authority._observe(server_pid, parent_pid, lease.server_facts[9],
                                                         lease.parent_facts[9])
        except Exception:  # noqa: BLE001 - cannot read: the full legacy check decides
            raise _FenceUnavailable('facts') from None
        if server != lease.server_facts or parent != lease.parent_facts:
            self.revoke(lease.key, 'incarnation_changed')
            raise AuthRefused('desktop_incarnation_changed')
        if not authority._facts_qualified(server, parent):
            self.revoke(lease.key, 'incarnation_unqualified')
            raise AuthRefused('desktop_incarnation_changed')
        if prints != lease.boundary_fingerprints:
            self.revoke(lease.key, 'fingerprint_changed')
            raise _FenceUnavailable('fingerprint')
        return expected_pid

    # -- P3 shadow ----------------------------------------------------------
    def shadow(self, pid, result):
        """Compare the native owner with the lsof decision; never changes it (D3)."""
        if self._native_latched or os.environ.get(SHADOW_ENV, '1') == '0' or self.legacy():
            return
        try:
            _incarnation, server_fd, endpoint = result
            client_fd, local, remote = endpoint
            native = native_connection_owner(pid, client_fd, local, remote)
            self._count('shadow_agree' if native[0] == server_fd else 'shadow_disagree')
            if native[0] != server_fd:
                _log.warning('attestation_shadow_disagreement class=server_fd')
        except NativeAnomaly as anomaly:
            reason = str(anomaly)
            self._count('shadow_anomaly:' + reason)
            if reason in ('size', 'oracle', 'port_high_bits'):
                self._native_latched = True     # layout doubt: native lookup off for this process
                _log.warning('attestation_shadow_latched class=%s', reason)
        except Exception as error:  # noqa: BLE001 - shadow can never affect the decision
            self._count('shadow_error:' + type(error).__name__)


class _FencedPeer:
    """Runs the unchanged ManagedOmlx.connected_peer (same lsof inventories, parser,
    reasons, bracket order) with the native fence in place of binding()."""

    def __init__(self, authority, lease, reuse):
        self.authority, self.lease, self.reuse = authority, lease, reuse
        self.port, self.uid, self.prep = authority.port, authority.uid, authority.prep

    def binding(self, expected_pid=None):
        return self.reuse.fence(self.authority, self.lease, expected_pid)

    def connected_peer(self, sock, pid, incarnation):
        result = ManagedOmlx.connected_peer(self, sock, pid, incarnation)
        self.reuse.shadow(pid, result)
        return result


_REUSE = _DesktopReuse()
if hasattr(os, 'register_at_fork'):
    # A forked child has no parent workers and a new process lifetime: every
    # inherited lease fails its lifetime check and nothing is waited on.
    os.register_at_fork(after_in_child=lambda: _REUSE._reset())


class CheckedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream, authority, epoch, backend, identity, owner, sock, *, lease=None, reuse=None):
        self.stream, self.authority, self.epoch, self.backend = stream, authority, epoch, backend
        self.identity, self.owner, self.sock = identity, owner, sock
        self.lease, self.reuse = lease, reuse

    async def _legacy_peer(self):
        if self.lease is None:
            return await asyncio.to_thread(connected_peer_with_retry, self.authority, self.sock,
                                           self.identity[0], self.identity)
        # An optimized stream that can no longer use its lease: a fresh, complete
        # qualification (new walks), bound to this stream's identity, then the
        # full legacy peer check with real binding() calls. Never weaker than legacy.
        fresh = await asyncio.to_thread(self.backend.authority.load)
        if not isinstance(fresh, DesktopOmlx) or fresh._identity != self.authority._identity:
            raise AuthRefused('desktop_listener_changed')
        return await asyncio.to_thread(connected_peer_with_retry, fresh, self.sock,
                                       self.identity[0], self.identity)

    async def check(self):
        try:
            if self.backend.epoch != self.epoch or self.stream.get_extra_info('socket') is not self.sock:
                raise AuthRefused('connection_invalidated')
            fenced = self.lease is not None and self.reuse.usable(self.lease)
            if fenced:
                try:
                    actual = await asyncio.to_thread(
                        connected_peer_with_retry, _FencedPeer(self.authority, self.lease, self.reuse),
                        self.sock, self.identity[0], self.identity)
                except _FenceUnavailable:
                    fenced = False
                    actual = await self._legacy_peer()
            else:
                actual = await self._legacy_peer()
            if (actual != self.owner or self.backend.epoch != self.epoch
                    or self.stream.get_extra_info('socket') is not self.sock):
                raise AuthRefused('connected_peer_changed')
            if fenced and not self.reuse.usable(self.lease):
                # LP-W: revoked or expired during the await. Decide again with the
                # complete legacy path before any byte of this write is released.
                actual = await self._legacy_peer()
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


class _EpochChanged(Exception):
    """The transport was invalidated during an optimized connect: refuse, do not retry."""


class CheckedBackend(AutoBackend):
    def __init__(self, authority, port):
        self.authority, self.port, self.epoch = authority, port, 0

    async def connect_tcp(self, host, port, **kwargs):
        epoch = self.epoch
        if host != '127.0.0.1' or port != self.port:
            raise refused()
        stream = await super().connect_tcp(host, port, **kwargs)
        try:
            authority = None
            if isinstance(self.authority, RuntimeAuthority) and not _REUSE.legacy():
                acquired = await asyncio.to_thread(_REUSE.acquire, self.authority)
                if acquired is not None:
                    authority, lease = acquired
                    if lease is not None:
                        try:
                            return await self._connect_reused(stream, epoch, authority, lease)
                        except _EpochChanged:
                            raise AuthRefused('connection_invalidated') from None
                        except Exception as error:  # noqa: BLE001 - fail closed to the full legacy check
                            if isinstance(error, AuthRefused):
                                _REUSE.revoke(lease.key, 'connect_refused')
                            authority = None
            if authority is None:
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

    async def _connect_reused(self, stream, epoch, authority, lease):
        """Optimized connect: full binding() (D4), incarnation bound to the lease,
        then the unchanged peer check with the native fence instead of binding()."""
        pid = await asyncio.to_thread(authority.binding)
        identity = await asyncio.to_thread(process_identity, pid, authority.uid)
        server = lease.server_facts
        if (pid, authority.uid, server[7], server[8]) != tuple(identity) or pid != server[0]:
            raise AuthRefused('process_identity_changed')
        sock = stream.get_extra_info('socket')
        owner = await asyncio.to_thread(connected_peer_with_retry, _FencedPeer(authority, lease, _REUSE),
                                        sock, pid, identity)
        if self.epoch != epoch:
            raise _EpochChanged()
        if not _REUSE.usable(lease):
            raise _FenceUnavailable('lease')
        return CheckedStream(stream, authority, epoch, self, identity, owner, sock, lease=lease, reuse=_REUSE)


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
            if isinstance(self.backend.authority, RuntimeAuthority):
                _REUSE.invalidate()     # network error / engine restart: requalify from scratch

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
        # Closing ends this transport's connections only. The process-wide lease
        # is not transport-scoped, so a routine per-turn client close keeps it.
        if self.backend:
            self.backend.epoch += 1
        try:
            await self.pool.aclose()
        finally:
            _close_inspection_pool()
