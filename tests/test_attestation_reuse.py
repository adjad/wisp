"""Attestation reuse (P1 fence, P2 per-incarnation lease, P3 native shadow).

Synthetic only: an in-process fake desktop authority and a fake sleep-inclusive
clock; real-kernel checks use this process's own loopback sockets and, where the
sandbox allows it, a disposable bash child. Never port 8000/8765, the installed
apps, real oMLX, credentials or user data.
"""
import asyncio
import ctypes
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from service.inference import attributed_transport as at
from service.inference import local_peer
from service.inference.inference_errors import ModelLoadError
from service.inference.local_peer import AuthRefused, ManagedOmlx

SECOND = 1_000_000_000
UID = os.getuid()
SERVER_PID, PARENT_PID = 4321, 4300
D = at.DesktopOmlx
SERVER_EXE = str(D.python_root / 'cpython/bin/python3.11')
PARENT_EXE = str(D.app_executable)


def server_facts(**changes):
    row = dict(pid=SERVER_PID, ppid=PARENT_PID, uid=UID, ruid=UID, svuid=UID, comm=b'python3.11' + b'\0' * 6,
               name=b'python3.11' + b'\0' * 22, start_s=1_700_000_000, start_us=1234, path=SERVER_EXE,
               flags=0x2010301, cdhash=b'\x11' * 20, identifier='python3', team=D.team_id)
    row.update(changes)
    return tuple(row.values())


def parent_facts(**changes):
    row = dict(pid=PARENT_PID, ppid=1, uid=UID, ruid=UID, svuid=UID, comm=b'oMLX' + b'\0' * 12,
               name=b'oMLX' + b'\0' * 28, start_s=1_699_999_000, start_us=99, path=PARENT_EXE,
               flags=0x2010301, cdhash=b'\x22' * 20, identifier='app.omlx', team=D.team_id)
    row.update(changes)
    return tuple(row.values())


FIELDS = ['pid', 'ppid', 'uid', 'ruid', 'svuid', 'comm', 'name', 'start_s', 'start_us', 'path',
          'flags', 'cdhash', 'identifier', 'team']
# A changed value for every fact field. Where a predicate also pins the field
# (uid, identifier, team, ppid, ...) the change is refused either way.
CHANGED = {'pid': 999, 'ppid': 5, 'uid': UID + 1, 'ruid': UID + 1, 'svuid': UID + 1, 'comm': b'bash' + b'\0' * 12,
           'name': b'bash' + b'\0' * 28, 'start_s': 1_800_000_000, 'start_us': 77,
           'flags': 0x2010311, 'cdhash': b'\x33' * 20, 'identifier': 'com.apple.bash', 'team': 'OTHERTEAM1'}
CHANGED_PATH = {'server': str(D.python_root / 'cpython/bin/python3.12'), 'parent': '/Applications/Other.app/x'}


class World:
    """The fake machine: what the native facts and the binding() inspection observe."""

    def __init__(self):
        self.server, self.parent = server_facts(), parent_facts()
        self.prints = (('fingerprint', 1, 2, 3),)
        self.identity = (SERVER_PID, SERVER_EXE, PARENT_PID, PARENT_EXE)
        self.command = 'omlx-server'          # ps comm/argv[0]: only binding() reads it (R2)
        self.manifest = False
        self.observe_error = None
        self.load_error = None
        self.load_gate = None                 # threading.Event that load() waits on
        self.load_advance = None              # (clock, seconds) the fake load takes
        self.on_binding = None                # called inside binding() (e.g. a change mid-qualification)
        self.log = []
        self.lock = threading.Lock()

    def record(self, event):
        with self.lock:
            self.log.append(event)

    def count(self, event):
        with self.lock:
            return self.log.count(event)


class FakeDesktop(at.DesktopOmlx):
    def __init__(self, world):
        self.world, self.uid, self.prep = world, UID, None
        self._identity = world.identity        # what the full constructor qualified

    def _manifest_absent(self):
        if self.world.manifest:
            raise AuthRefused('desktop_authority_superseded')

    def binding(self, expected_pid=None):
        self.world.record('binding')
        if self.world.on_binding is not None:
            self.world.on_binding()
        self._manifest_absent()
        if self.world.command != 'omlx-server':
            raise AuthRefused('desktop_process_unqualified')
        if (expected_pid is not None and self.world.identity[0] != expected_pid) or self.world.identity != self._identity:
            raise AuthRefused('desktop_listener_changed')
        return self.world.identity[0]

    def _observe(self, server_pid, parent_pid, executable, parent_executable):
        self.world.record('observe')
        if self.world.observe_error is not None:
            raise self.world.observe_error
        return self.world.server, self.world.parent, self.world.prints


class FakeRuntime(at.RuntimeAuthority):
    def __init__(self, world):
        self.world = world

    def load(self):
        gate = self.world.load_gate           # holds background re-walks only
        if gate is not None and threading.current_thread().name == 'wisp-attest-refresh':
            assert gate.wait(10)
        self.world.record('load')
        if self.world.load_advance is not None:
            clock, seconds = self.world.load_advance
            clock.advance(seconds)
        if self.world.load_error is not None:
            raise self.world.load_error
        return FakeDesktop(self.world)


class FakeStream(at.httpcore.AsyncNetworkStream):
    def __init__(self, sock, world):
        self.sock, self.world = sock, world

    async def read(self, max_bytes, timeout=None):
        return b''

    async def write(self, buffer, timeout=None):
        self.world.record(('wire', bytes(buffer)))

    async def aclose(self):
        self.world.record('closed')

    async def start_tls(self, *args, **kwargs):
        raise AssertionError('no TLS')

    def get_extra_info(self, info):
        return self.sock if info == 'socket' else None


class Clock:
    def __init__(self):
        self.now = 1_000 * SECOND

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * SECOND)


def fake_connected_peer(self, sock, pid, incarnation):
    """Stands in for ManagedOmlx.connected_peer with its bracket order: binding,
    two lsof inventories, binding. self.binding is the fence on an optimized stream."""
    world = (self.authority if isinstance(self, at._FencedPeer) else self).world
    self.binding(pid)
    world.record('lsof')
    world.record('lsof')
    self.binding(pid)
    return incarnation, 9, (7, ('127.0.0.1', 50000), ('127.0.0.1', 8000))


@pytest.fixture
def env(monkeypatch, tmp_path):
    world = World()
    clock = Clock()
    reuse = at._DesktopReuse(clock=clock)
    monkeypatch.setattr(at, '_REUSE', reuse)
    monkeypatch.delenv(at.LEGACY_ENV, raising=False)
    monkeypatch.setenv(at.SHADOW_ENV, '0')
    monkeypatch.setattr(at.Path, 'home', lambda: tmp_path / 'home')
    sock = object()

    async def connect_tcp(self, host, port, **kwargs):
        return FakeStream(sock, world)
    monkeypatch.setattr(at.AutoBackend, 'connect_tcp', connect_tcp)
    monkeypatch.setattr(at, 'process_identity',
                        lambda pid, uid: (pid, uid, world.server[7], world.server[8]))
    monkeypatch.setattr(ManagedOmlx, 'connected_peer', fake_connected_peer)
    monkeypatch.setattr(at.time, 'sleep', lambda _s: None)
    backend = at.CheckedBackend(FakeRuntime(world), 8000)
    yield world, clock, reuse, backend
    for thread in threading.enumerate():
        if thread.name == 'wisp-attest-refresh':
            if world.load_gate is not None:
                world.load_gate.set()
            thread.join(5)


def run(coroutine):
    return asyncio.run(coroutine)


async def request(backend, *payloads):
    stream = await backend.connect_tcp('127.0.0.1', 8000)
    for payload in payloads:
        await stream.write(payload)
    return stream


def wires(world):
    return [event[1] for event in world.log if isinstance(event, tuple)]


def join_refresh():
    for thread in threading.enumerate():
        if thread.name == 'wisp-attest-refresh':
            thread.join(5)
            assert not thread.is_alive()


# ---------------------------------------------------------------------------
# P2: one qualified authority per incarnation
# ---------------------------------------------------------------------------
def test_second_connection_reuses_the_qualified_authority(env):
    world, clock, reuse, backend = env
    run(request(backend, b'h', b'b'))
    clock.advance(1)
    run(request(backend, b'h2', b'b2'))
    assert world.count('load') == 1                          # one tree-walking qualification
    assert wires(world) == [b'h', b'b', b'h2', b'b2']
    assert reuse.counters.get('reuse') == 1 and reuse.counters.get('build') == 1


def test_connect_keeps_one_full_binding_per_connection_D4(env):
    world, clock, reuse, backend = env
    run(request(backend))                     # build: load + bracketed binding; connect binding
    first = world.count('binding')
    world.log.clear()
    run(request(backend))
    assert world.count('binding') == 1 and world.count('load') == 0


def test_writes_use_the_fence_instead_of_binding_and_keep_bracket_order(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        world.log.clear()
        await stream.write(b'header')
    run(scenario())
    # I19: fence (observe), two lsof inventories, fence (observe), then bytes. No binding per write.
    assert world.log == ['observe', 'lsof', 'lsof', 'observe', ('wire', b'header')]


def test_empty_write_sends_nothing_and_checks_nothing(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        world.log.clear()
        await stream.write(b'')
    run(scenario())
    assert world.log == []


@pytest.mark.parametrize('side', ['server', 'parent'])
@pytest.mark.parametrize('field', FIELDS)
def test_any_changed_incarnation_fact_refuses_the_next_write(env, side, field):
    """Exec into another binary (or any other incarnation change) between connect and write."""
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        await stream.write(b'header')
        value = CHANGED_PATH[side] if field == 'path' else CHANGED[field]
        if side == 'server':
            world.server = server_facts(**{field: value})
        else:
            world.parent = parent_facts(**{field: value})
        with pytest.raises(ModelLoadError):
            await stream.write(b'body')
    run(scenario())
    assert wires(world) == [b'header'] and world.log[-1] == 'closed'
    key = reuse.key_for(reuse.manifest_path())
    assert reuse.store.lookup(key, at.ClockSample(at.CLOCK_DOMAIN, clock.now, 0)) is None   # revoked


@pytest.mark.parametrize('side', ['server', 'parent'])
@pytest.mark.parametrize('field', FIELDS)
def test_a_changed_fact_at_connect_requalifies_from_scratch(env, side, field):
    world, clock, reuse, backend = env
    run(request(backend, b'x'))
    value = CHANGED_PATH[side] if field == 'path' else CHANGED[field]
    if side == 'server':
        world.server = server_facts(**{field: value})
    else:
        world.parent = parent_facts(**{field: value})
    world.log.clear()
    try:
        run(request(backend, b'y'))
    except ModelLoadError:
        pass                                  # a predicate refusal of the new facts is also fine
    assert world.count('load') >= 1            # never served from the old lease


def test_PINNED_RESIDUAL_R2_same_binary_exec_or_argv_change_is_not_reread_per_write(env):
    """D2: exec into the same binary leaves every native fact equal; argv[0] is read
    only by the per-connection binding()."""
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        world.command = 'something-else'      # argv rewrite / same-binary exec
        await stream.write(b'header')         # accepted: R2
        return stream
    run(scenario())
    assert wires(world) == [b'header']
    with pytest.raises(ModelLoadError):
        run(request(backend))                 # the next connection's binding() refuses it


def test_oMLX_restart_is_detected_at_the_next_use(env):
    world, clock, reuse, backend = env

    async def scenario():
        old = await backend.connect_tcp('127.0.0.1', 8000)
        await old.write(b'old-header')
        # oMLX restarts: new pid, new start time, same binaries.
        world.server = server_facts(pid=5555, start_s=1_700_000_500)
        world.identity = (5555, SERVER_EXE, PARENT_PID, PARENT_EXE)
        with pytest.raises(ModelLoadError):
            await old.write(b'old-body')      # old stream bound to the dead incarnation
        world.log.clear()
        new = await backend.connect_tcp('127.0.0.1', 8000)
        await new.write(b'new-header')
    run(scenario())
    assert world.count('load') == 1 and wires(world) == [b'new-header']


def test_pid_reuse_with_a_new_start_time_refuses(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        world.server = server_facts(start_us=4321)
        with pytest.raises(ModelLoadError):
            await stream.write(b'header')
    run(scenario())
    assert wires(world) == []


# ---------------------------------------------------------------------------
# Clock and W_hard (D1)
# ---------------------------------------------------------------------------
def test_default_clock_is_the_sleep_inclusive_CLOCK_MONOTONIC(monkeypatch):
    seen = []
    monkeypatch.setattr(at.time, 'clock_gettime_ns', lambda clock_id: seen.append(clock_id) or 42)
    monkeypatch.setattr(at.time, 'monotonic_ns', lambda: pytest.fail('time.monotonic does not count sleep'))
    monkeypatch.setattr(at.time, 'monotonic', lambda: pytest.fail('time.monotonic does not count sleep'))
    assert at._sleep_inclusive_ns() == 42 and seen == [at.time.CLOCK_MONOTONIC]


def test_a_sleep_longer_than_W_hard_forces_requalification(env, monkeypatch):
    """The fake clock jumps (the Mac slept) while time.monotonic would not have moved."""
    world, clock, reuse, backend = env
    monkeypatch.setattr(at.time, 'monotonic', lambda: 0.0)
    run(request(backend, b'a'))
    clock.advance(61)
    run(request(backend, b'b'))
    assert world.count('load') == 2


@pytest.mark.parametrize('age,reused', [
    (at.W_HARD_NS - at.CONNECT_MARGIN_NS - 1, True),
    (at.W_HARD_NS - at.CONNECT_MARGIN_NS, False),
    (at.W_HARD_NS, False),
])
def test_new_connections_need_the_connect_margin_before_hard_expiry(env, age, reused):
    world, clock, reuse, backend = env
    world.load_error = None
    run(request(backend))
    clock.now += age
    world.load_gate = threading.Event()       # hold any refresh so only foreground loads count
    world.log.clear()
    run(request(backend))
    assert (reuse.counters.get('reuse') == 1) is reused
    assert (world.count('load') == 0) is reused
    world.load_gate.set()
    join_refresh()


def test_expiry_between_header_and_body_rechecks_with_a_fresh_full_qualification(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        await stream.write(b'header')
        clock.advance(60)                     # exactly W_hard after qualification start: expired
        world.log.clear()
        await stream.write(b'body')
    run(scenario())
    # Fresh constructor (tree walks) plus the full legacy peer check with real binding().
    assert world.log == ['load', 'binding', 'lsof', 'lsof', 'binding', ('wire', b'body')]


def test_expired_stream_is_refused_when_the_fresh_qualification_differs(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        clock.advance(61)
        world.identity = (SERVER_PID, str(D.python_root / 'other'), PARENT_PID, PARENT_EXE)
        with pytest.raises(ModelLoadError):
            await stream.write(b'header')
    run(scenario())
    assert wires(world) == []


def test_clock_regression_disables_reuse_for_the_process(env):
    world, clock, reuse, backend = env
    run(request(backend))
    clock.now -= 1
    run(request(backend))
    run(request(backend))
    assert world.count('load') == 3 and not reuse.store.status().admitted


def test_clock_failure_falls_back_to_legacy(env, monkeypatch):
    world, clock, reuse, backend = env
    reuse._clock_override = lambda: (_ for _ in ()).throw(OSError('clock'))
    run(request(backend, b'x'))
    run(request(backend, b'y'))
    assert world.count('load') == 2 and wires(world) == [b'x', b'y']


# ---------------------------------------------------------------------------
# Refresh, revocation and concurrency (F4)
# ---------------------------------------------------------------------------
def test_refresh_ahead_runs_in_the_background_and_never_blocks_a_connection(env):
    world, clock, reuse, backend = env
    run(request(backend))
    clock.advance(41)
    world.load_gate = threading.Event()       # the background re-walk hangs
    started = time.perf_counter()
    run(request(backend, b'live'))            # served from the still-valid lease
    assert time.perf_counter() - started < 2 and wires(world) == [b'live']
    assert reuse.store.status().outstanding_refreshes == 1
    world.load_gate.set()
    join_refresh()
    assert world.count('load') == 2 and reuse.counters.get('refresh_ok') == 1
    clock.advance(30)                         # 71 s after the first walk, 30 s after the refresh
    world.log.clear()
    run(request(backend))
    assert world.count('load') == 0


def test_failed_refresh_revokes_and_never_extends_freshness(env):
    world, clock, reuse, backend = env
    run(request(backend))
    clock.advance(41)
    world.load_gate = threading.Event()
    run(request(backend, b'served'))          # served from the lease; the refresh is held
    world.load_error = AuthRefused('desktop_runtime_unqualified')
    world.load_gate.set()                     # ...and then fails
    join_refresh()
    assert wires(world) == [b'served'] and reuse.counters.get('refresh_error:AuthRefused') == 1
    world.load_gate = None
    world.log.clear()
    with pytest.raises(ModelLoadError):
        run(request(backend))                 # revoked: foreground requalification refuses
    assert world.count('load') >= 1


def test_hung_refresh_ages_out_and_foreground_never_waits_for_it(env):
    world, clock, reuse, backend = env
    run(request(backend))
    clock.advance(41)
    world.load_gate = threading.Event()
    run(request(backend))                     # starts the hanging refresh
    clock.advance(20)                         # lease expired; refresh token still current
    gate, world.load_gate = world.load_gate, None
    world.log.clear()
    run(request(backend, b'x'))               # legacy load, no wait, not published
    assert world.count('load') == 1 and wires(world) == [b'x']
    clock.advance(10)                         # refresh token past T_build: retired
    run(request(backend, b'y'))
    assert reuse.store.status().outstanding and len(reuse.store.status().outstanding) == 1
    gate.set()
    join_refresh()                            # stale completion: a no-op
    assert reuse.store.status().outstanding == ()
    world.log.clear()
    run(request(backend))
    assert world.count('load') == 0           # the foreground lease survived the stale refresh


def test_invalidate_during_a_refresh_discards_the_late_result(env):
    world, clock, reuse, backend = env
    run(request(backend))
    clock.advance(41)
    world.load_gate = threading.Event()
    run(request(backend))
    transport = at.CredentialTransport('http://127.0.0.1:8000', 'k', managed=True, authority=backend.authority)
    transport.invalidate()
    world.load_gate.set()
    join_refresh()
    key = reuse.key_for(reuse.manifest_path())
    assert reuse.store.lookup(key, at.ClockSample(at.CLOCK_DOMAIN, clock.now, 0)) is None
    world.load_gate = None
    world.log.clear()
    run(request(backend))
    assert world.count('load') == 1


def test_transport_invalidate_revokes_but_close_keeps_the_lease(env):
    world, clock, reuse, backend = env
    run(request(backend))
    transport = at.CredentialTransport('http://127.0.0.1:8000', 'k', managed=True, authority=backend.authority)
    run(transport.aclose())
    world.log.clear()
    run(request(backend))
    assert world.count('load') == 0
    transport.invalidate()
    world.log.clear()
    run(request(backend))
    assert world.count('load') == 1


def test_revocation_during_the_write_check_is_caught_before_bytes(env, monkeypatch):
    """LP-W: a revocation landing while the check is awaited forces the full legacy path."""
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        original = at._FencedPeer.connected_peer

        def revoking(self, sock, pid, incarnation):
            result = original(self, sock, pid, incarnation)
            reuse.revoke(self.lease.key, 'test')          # another client's invalidate
            world.identity = (SERVER_PID, str(D.python_root / 'other'), PARENT_PID, PARENT_EXE)
            return result
        monkeypatch.setattr(at._FencedPeer, 'connected_peer', revoking)
        with pytest.raises(ModelLoadError):
            await stream.write(b'header')
    run(scenario())
    assert wires(world) == []


def test_concurrent_cold_connections_build_once_and_never_wait(env):
    world, clock, reuse, backend = env

    async def scenario():
        await asyncio.gather(*(request(backend, b'p') for _ in range(12)))
    run(scenario())
    assert wires(world) == [b'p'] * 12
    assert reuse.counters.get('build') == 1 and reuse.store.status().outstanding == ()


def test_counter_exhaustion_fails_closed_to_legacy(env, monkeypatch):
    world, clock, reuse, backend = env
    monkeypatch.setattr(at, '_LIMITS', at.StoreLimits(key_capacity=8, transport_capacity=4, nonce_capacity=16,
                                                      counter_ceiling=1, record_units=16384, fact_depth=6))
    reuse._reset()
    run(request(backend))
    key = reuse.key_for(reuse.manifest_path())
    reuse.revoke(key, 'test')
    reuse.revoke(key, 'test')                 # generation counter exhausted: store disabled
    world.log.clear()
    run(request(backend, b'a'))
    run(request(backend, b'b'))
    assert world.count('load') == 2 and wires(world) == [b'a', b'b']
    assert not reuse.store.status().admitted


def test_nonce_exhaustion_fails_closed_to_legacy(env, monkeypatch):
    world, clock, reuse, backend = env
    monkeypatch.setattr(at, '_LIMITS', at.StoreLimits(key_capacity=8, transport_capacity=4, nonce_capacity=1,
                                                      counter_ceiling=100, record_units=16384, fact_depth=6))
    reuse._reset()
    run(request(backend))
    reuse.revoke(reuse.key_for(reuse.manifest_path()), 'test')
    world.log.clear()
    run(request(backend, b'a'))
    run(request(backend, b'b'))
    assert world.count('load') == 2 and wires(world) == [b'a', b'b']


def test_fork_reset_invalidates_every_inherited_lease(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        reuse._reset()                        # what the at-fork child handler runs
        world.log.clear()
        await stream.write(b'x')
    run(scenario())
    assert world.log[0] == 'load'             # inherited lease unusable: fresh legacy check


def test_refresh_threads_do_not_leak(env):
    world, clock, reuse, backend = env
    run(request(backend))
    for _ in range(3):
        clock.advance(41)
        run(request(backend))
        join_refresh()
    assert not [t for t in threading.enumerate() if t.name == 'wisp-attest-refresh']
    assert reuse.store.status().outstanding == ()


# ---------------------------------------------------------------------------
# Manifest, fingerprints, unavailable facts
# ---------------------------------------------------------------------------
def test_manifest_appearing_between_writes_refuses_and_revokes(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        await stream.write(b'header')
        world.manifest = True
        with pytest.raises(ModelLoadError):
            await stream.write(b'body')
    run(scenario())
    assert wires(world) == [b'header']


def test_manifest_file_routes_new_connections_to_the_unchanged_legacy_load(env, tmp_path):
    world, clock, reuse, backend = env
    run(request(backend))
    manifest = reuse.manifest_path()
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b'{}')
    os.chmod(manifest, 0o600)
    world.log.clear()
    run(request(backend))
    assert world.count('load') == 1            # legacy load decides


def test_fingerprint_change_falls_back_to_a_fresh_full_check(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        world.prints = (('fingerprint', 1, 2, 4),)
        world.log.clear()
        await stream.write(b'x')
    run(scenario())
    assert world.log[0] == 'observe' and 'load' in world.log and wires(world) == [b'x']


def test_unreadable_facts_fall_back_to_the_full_check_never_to_trusted(env):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        world.observe_error = AuthRefused('native_inspection_unavailable')
        world.identity = (SERVER_PID, str(D.python_root / 'other'), PARENT_PID, PARENT_EXE)
        with pytest.raises(ModelLoadError):
            await stream.write(b'x')
    run(scenario())
    assert wires(world) == []


def test_unpublishable_build_still_serves_the_connection_with_legacy_checks(env):
    world, clock, reuse, backend = env
    world.observe_error = AuthRefused('native_inspection_unavailable')
    run(request(backend, b'x'))
    run(request(backend, b'y'))
    assert world.count('load') == 2 and wires(world) == [b'x', b'y']
    assert world.count('observe') == 2         # one failed bracket per build, none per write


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
LEGACY_CONNECT = ['load', 'binding', 'binding', 'lsof', 'lsof', 'binding']


def test_kill_switch_restores_exactly_the_legacy_sequence(env, monkeypatch):
    world, clock, reuse, backend = env
    monkeypatch.setenv(at.LEGACY_ENV, '1')

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        await stream.write(b'h')
        await backend.connect_tcp('127.0.0.1', 8000)
    run(scenario())
    assert world.log == (LEGACY_CONNECT + ['binding', 'lsof', 'lsof', 'binding', ('wire', b'h')]
                         + LEGACY_CONNECT)
    assert 'observe' not in world.log


def test_kill_switch_is_one_way_and_applies_to_open_streams(env, monkeypatch):
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        monkeypatch.setenv(at.LEGACY_ENV, '1')
        world.log.clear()
        await stream.write(b'h')
        assert world.log[0] == 'load'          # full legacy recheck, not the fence
        monkeypatch.delenv(at.LEGACY_ENV)
        world.log.clear()
        await backend.connect_tcp('127.0.0.1', 8000)
        assert world.log == LEGACY_CONNECT     # still legacy after the variable is removed
    run(scenario())


# ---------------------------------------------------------------------------
# P3 native socket-owner lookup: shadow only (D3)
# ---------------------------------------------------------------------------
def socket_fdinfo(*, status=2, kind=2, laddr='127.0.0.1', lport=50000, faddr='127.0.0.1', fport=8000,
                  state=4, family=2, protocol=6, sock_type=1, vflag=1, so=0xABC, gencnt=7):
    raw = bytearray(local_peer.SOCKET_FDINFO_SIZE)
    struct.pack_into('=I', raw, 4, status)
    struct.pack_into('=Q', raw, 160, so)
    struct.pack_into('=iii', raw, 176, sock_type, protocol, family)
    struct.pack_into('=i', raw, 256, kind)
    struct.pack_into('=ii', raw, 264, socket.htons(fport), socket.htons(lport))
    struct.pack_into('=Q', raw, 272, gencnt)
    raw[288] = vflag
    raw[308:312] = socket.inet_aton(faddr)
    raw[324:328] = socket.inet_aton(laddr)
    struct.pack_into('=i', raw, 344, state)
    return bytes(raw)


class FakeLibproc:
    """proc_pidinfo/proc_pidfdinfo over synthesized struct bytes."""

    def __init__(self, own, server_fds, *, listfds_return=None, size=None):
        self.own, self.server_fds = own, server_fds        # server_fds: {fd: bytes}
        self.listfds_return, self.size = listfds_return, size

    def proc_pidinfo(self, pid, flavor, arg, buffer, size):
        assert flavor == local_peer.PROC_PIDLISTFDS
        rows = b''.join(struct.pack('=iI', fd, 2) for fd in self.server_fds)
        ctypes.memmove(buffer, rows, len(rows))
        return self.listfds_return if self.listfds_return is not None else len(rows)

    def proc_pidfdinfo(self, pid, fd, flavor, buffer, size):
        raw = self.own if pid == os.getpid() and fd == 7 else self.server_fds[fd]
        ctypes.memmove(buffer, raw, len(raw))
        return self.size if self.size is not None else len(raw)


LOCAL, REMOTE = ('127.0.0.1', 50000), ('127.0.0.1', 8000)
SERVER_END = dict(laddr='127.0.0.1', lport=8000, faddr='127.0.0.1', fport=50000)


def owner(library):
    return local_peer.native_connection_owner(SERVER_PID, 7, LOCAL, REMOTE, library=library)


def test_native_owner_decodes_the_unshared_established_pair():
    library = FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END, so=0xDEF, gencnt=11)})
    assert owner(library) == (9, 0xDEF, 11, 0xABC, 7)


@pytest.mark.parametrize('library,reason', [
    (FakeLibproc(socket_fdinfo(lport=50001), {9: socket_fdinfo(**SERVER_END)}), 'oracle'),
    (FakeLibproc(socket_fdinfo(laddr='127.0.0.2'), {9: socket_fdinfo(**SERVER_END)}), 'oracle'),
    (FakeLibproc(socket_fdinfo(status=3), {9: socket_fdinfo(**SERVER_END)}), 'client_shared'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END, status=3)}), 'server_shared'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END), 10: socket_fdinfo(**SERVER_END)}),
     'server_match'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END, state=5)}), 'server_match'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END, family=30)}), 'server_match'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END, vflag=2)}), 'server_match'),
    (FakeLibproc(socket_fdinfo(state=5), {9: socket_fdinfo(**SERVER_END)}), 'client_state'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END)}, size=791), 'fdinfo_return'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END)}, listfds_return=7), 'listfds'),
    (FakeLibproc(socket_fdinfo(), {9: socket_fdinfo(**SERVER_END)}, listfds_return=0), 'listfds'),
])
def test_every_native_anomaly_is_reported_never_accepted(library, reason):
    with pytest.raises(local_peer.NativeAnomaly) as caught:
        owner(library)
    assert str(caught.value) == reason


def test_parse_rejects_any_size_but_the_qualified_one():
    for size in (0, 791, 793):
        with pytest.raises(local_peer.NativeAnomaly):
            local_peer.parse_socket_fdinfo(b'\0' * size)


def _shadow_result(env, monkeypatch, native):
    world, clock, reuse, backend = env
    monkeypatch.setenv(at.SHADOW_ENV, '1')
    monkeypatch.setattr(at, 'native_connection_owner', native)
    run(request(backend, b'h'))
    return world, reuse


@pytest.mark.parametrize('native,counter', [
    (lambda *a, **k: (9, 1, 2, 3, 4), 'shadow_agree'),
    (lambda *a, **k: (10, 1, 2, 3, 4), 'shadow_disagree'),
    (lambda *a, **k: (_ for _ in ()).throw(local_peer.NativeAnomaly('server_shared')), 'shadow_anomaly:server_shared'),
    (lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')), 'shadow_error:RuntimeError'),
])
def test_shadow_never_changes_the_lsof_decision(env, monkeypatch, native, counter):
    world, reuse = _shadow_result(env, monkeypatch, native)
    assert wires(world) == [b'h'] and reuse.counters.get(counter, 0) >= 1


def test_shadow_layout_doubt_latches_native_off(env, monkeypatch):
    calls = []

    def native(*args, **kwargs):
        calls.append(1)
        raise local_peer.NativeAnomaly('oracle')
    world, reuse = _shadow_result(env, monkeypatch, native)
    run(request(env[3], b'again'))
    assert len(calls) == 1 and reuse._native_latched and wires(world) == [b'h', b'again']


def test_shadow_does_not_run_when_lsof_refuses(env, monkeypatch):
    world, clock, reuse, backend = env
    monkeypatch.setenv(at.SHADOW_ENV, '1')
    monkeypatch.setattr(at, 'native_connection_owner', lambda *a, **k: pytest.fail('shadow ran'))

    def refusing(self, sock, pid, incarnation):
        raise AuthRefused('connected_peer_unqualified')
    monkeypatch.setattr(ManagedOmlx, 'connected_peer', refusing)
    with pytest.raises(ModelLoadError):
        run(request(backend))


# ---------------------------------------------------------------------------
# Real kernel (this process's own loopback sockets; a disposable bash child)
# ---------------------------------------------------------------------------
darwin = pytest.mark.skipif(sys.platform != 'darwin', reason='libproc')


@pytest.fixture
def loopback():
    try:
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname())
        server, _ = listener.accept()
    except OSError as error:
        pytest.skip(f'loopback sockets unavailable here: {error}')
    yield listener, client, server
    for item in (client, server, listener):
        item.close()


@darwin
def test_native_owner_equals_the_kernel_pair_on_a_real_loopback_connection(loopback):
    listener, client, server = loopback
    found = local_peer.native_connection_owner(os.getpid(), client.fileno(), client.getsockname(),
                                               client.getpeername())
    assert found[0] == server.fileno()


@darwin
@pytest.mark.parametrize('end,reason', [('server', 'server_shared'), ('client', 'client_shared')])
def test_real_fd_sharing_sets_FP_SHARED_and_is_an_anomaly(loopback, end, reason):
    listener, client, server = loopback
    duplicate = os.dup((server if end == 'server' else client).fileno())
    try:
        with pytest.raises(local_peer.NativeAnomaly) as caught:
            local_peer.native_connection_owner(os.getpid(), client.fileno(), client.getsockname(),
                                               client.getpeername())
        # A dup in the same process is also a second matching server fd.
        assert str(caught.value) in ({reason, 'server_match'} if end == 'server' else {reason})
        row = local_peer._socket_row(local_peer._libproc(), os.getpid(),
                                     (server if end == 'server' else client).fileno())
        assert row['status'] & local_peer.PROC_FP_SHARED     # the real kernel sets FP_SHARED
    finally:
        os.close(duplicate)


@darwin
def test_real_layout_oracle_catches_a_wrong_endpoint(loopback):
    listener, client, server = loopback
    local = client.getsockname()
    with pytest.raises(local_peer.NativeAnomaly) as caught:
        local_peer.native_connection_owner(os.getpid(), client.fileno(), (local[0], local[1] ^ 1),
                                           client.getpeername())
    assert str(caught.value) == 'oracle'


@darwin
def test_process_bsdinfo_reads_this_process_and_refuses_bad_pids():
    facts = local_peer.process_bsdinfo(os.getpid())
    assert facts[0] == os.getpid() and facts[1] == os.getppid() and facts[2:5] == (UID,) * 3
    assert type(facts[5]) is bytes and facts[7] > 0
    assert facts[7:9] == local_peer.process_identity(os.getpid(), UID)[2:]
    for pid in (0, -1, 2 ** 31, '1', True):
        with pytest.raises(AuthRefused):
            local_peer.process_bsdinfo(pid)


@darwin
def test_real_exec_keeps_pid_and_start_but_changes_the_native_facts():
    """Invariant: (pid, start) is not an incarnation. Exec into another binary changes
    comm/path/cdhash; exec into the same binary (bash -> /bin/sh -> bash) changes none (R2)."""
    try:
        child = subprocess.Popen(['/bin/bash', '-c', 'read a; exec /bin/sh -c "read b; exec /bin/cat"'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        pytest.skip(f'cannot start a disposable bash here: {error}')
    authority = object.__new__(at.DesktopOmlx)
    # Apple platform binaries carry no team identifier (CS_OPS_TEAMID fails for
    # them); the facts compared here are the kernel's bsdinfo, path and cdhash.
    signed = at.DesktopOmlx._csops_string.__get__(authority)
    authority._csops_string = lambda pid, op: 'platform' if op == 14 else signed(pid, op)

    def facts():
        for _ in range(200):
            try:
                return authority._incarnation_facts(child.pid, 'n/a')
            except AuthRefused:
                time.sleep(0.01)
        pytest.skip('child facts unreadable here')

    def settle(predicate):
        for _ in range(300):
            current = facts()
            if predicate(current):
                return current
            time.sleep(0.01)
        return facts()
    try:
        first = settle(lambda f: f[9] == '/bin/bash')
        child.stdin.write(b'1\n')
        child.stdin.flush()
        time.sleep(0.3)
        same = facts()                          # bash exec'd /bin/sh, which runs bash again
        assert same == first                    # PINNED_RESIDUAL R2 on the real kernel
        child.stdin.write(b'2\n')
        child.stdin.flush()
        other = settle(lambda f: f[9] != '/bin/bash')
        if child.poll() is not None or other[9] == '/bin/bash':
            pytest.skip('exec of /bin/cat is not permitted in this sandbox')
        assert other[0] == first[0] and other[7:9] == first[7:9]          # pid and start time survive exec
        assert other[5] != first[5] and other[9] != first[9] and other[11] != first[11]
    finally:
        child.kill()
        child.wait()


# ---------------------------------------------------------------------------
# Additional controls added for the section 5.2 mutation set
# ---------------------------------------------------------------------------
def current_lease(reuse, clock):
    return reuse.store.lookup(reuse.key_for(reuse.manifest_path()), at.ClockSample(at.CLOCK_DOMAIN, clock.now, 0))


def test_unqualified_facts_are_never_published(env):
    world, clock, reuse, backend = env
    world.server = server_facts(identifier='com.example.other')      # fails the signing predicate
    run(request(backend, b'a'))
    run(request(backend, b'b'))
    assert world.count('load') == 2 and current_lease(reuse, clock) is None


def test_facts_changing_inside_the_qualification_bracket_are_not_published(env):
    world, clock, reuse, backend = env
    world.on_binding = lambda: setattr(world, 'server', server_facts(cdhash=b'\x44' * 20))
    run(request(backend))
    assert reuse.counters.get('build_unpublished') == 1 and 'build' not in reuse.counters


def test_facts_must_agree_with_the_constructor_identity(env):
    world, clock, reuse, backend = env
    world.server = server_facts(path=str(D.python_root / 'cpython/bin/python3.12'))   # qualified, but not _identity
    run(request(backend))
    assert current_lease(reuse, clock) is None


def test_lease_starts_before_the_qualification_reads_anything(env):
    world, clock, reuse, backend = env
    started = clock.now
    world.load_advance = (clock, 5)
    run(request(backend))
    lease = current_lease(reuse, clock)
    assert lease.qualification_start_ns == started and lease.expiry_ns == started + at.W_HARD_NS


def test_refresh_starts_exactly_at_W_soft(env):
    world, clock, reuse, backend = env
    run(request(backend))
    clock.now += at.W_SOFT_NS - 1
    run(request(backend))
    assert reuse.store.status().outstanding_refreshes == 0
    world.load_gate = threading.Event()
    clock.now += 1
    run(request(backend))
    assert reuse.store.status().outstanding_refreshes == 1
    world.load_gate.set()
    join_refresh()


def test_fence_rechecks_the_lease_after_the_inventories(env, monkeypatch):
    """Expiry inside the peer check: the closing fence itself refuses to vouch."""
    world, clock, reuse, backend = env

    async def scenario():
        stream = await backend.connect_tcp('127.0.0.1', 8000)

        def expiring(self, sock, pid, incarnation):
            self.binding(pid)
            world.record('lsof')
            clock.advance(61)
            world.record('lsof')
            self.binding(pid)
            return incarnation, 9, (7, ('127.0.0.1', 50000), ('127.0.0.1', 8000))
        monkeypatch.setattr(ManagedOmlx, 'connected_peer', expiring)
        world.log.clear()
        await stream.write(b'x')
    run(scenario())
    assert world.log[:4] == ['observe', 'lsof', 'lsof', 'load']


def test_fence_refuses_a_pid_other_than_the_leased_server(env):
    world, clock, reuse, backend = env
    run(request(backend))
    lease = current_lease(reuse, clock)
    with pytest.raises(AuthRefused):
        reuse.fence(FakeDesktop(world), lease, SERVER_PID + 1)


def test_connect_binds_the_process_identity_to_the_leased_incarnation(env, monkeypatch):
    world, clock, reuse, backend = env
    run(request(backend))
    monkeypatch.setattr(at, 'process_identity', lambda pid, uid: (pid, uid, world.server[7], 999))
    world.log.clear()
    run(request(backend))
    assert world.count('load') == 1           # refused on the lease, decided by the legacy path


def test_revocation_during_the_connect_check_falls_back_to_legacy(env, monkeypatch):
    world, clock, reuse, backend = env
    run(request(backend))
    original = at._FencedPeer.connected_peer

    def revoking(self, sock, pid, incarnation):
        result = original(self, sock, pid, incarnation)
        reuse.revoke(self.lease.key, 'test')
        return result
    monkeypatch.setattr(at._FencedPeer, 'connected_peer', revoking)
    world.log.clear()
    stream = run(request(backend))
    assert world.count('load') == 1 and stream.lease is None     # decided before connect returned


def test_an_optimized_connect_refusal_is_redecided_by_the_full_legacy_check(env, monkeypatch):
    world, clock, reuse, backend = env
    run(request(backend))
    calls = []

    def refusing_once(self, sock, pid, incarnation):
        calls.append(1)
        raise AuthRefused('connected_peer_unqualified')
    monkeypatch.setattr(at._FencedPeer, 'connected_peer', refusing_once)
    world.log.clear()
    run(request(backend, b'x'))
    assert calls == [1] and world.count('load') == 1 and wires(world) == [b'x']
    assert current_lease(reuse, clock) is None                       # and the lease was revoked


@pytest.mark.parametrize('change,counter', [
    (lambda world: setattr(world, 'server', server_facts(cdhash=b'\x55' * 20)), 'revoke:revalidate_changed'),
    (lambda world: setattr(world, 'observe_error', AuthRefused('process_identity_unqualified')),
     'revoke:revalidate_unavailable'),
])
def test_connect_revalidates_natively_before_serving_the_lease(env, change, counter):
    world, clock, reuse, backend = env
    run(request(backend))
    change(world)
    world.log.clear()
    try:
        run(request(backend))
    except ModelLoadError:
        pass
    assert reuse.counters.get(counter) == 1 and world.log[:2] == ['observe', 'load']


def test_incarnation_facts_refuse_a_change_while_they_are_read(monkeypatch):
    rows = iter([(1, 2, UID, UID, UID, b'a', b'a', 5, 6), (1, 2, UID, UID, UID, b'b', b'b', 5, 6)])
    monkeypatch.setattr(at, 'process_bsdinfo', lambda pid: next(rows))
    monkeypatch.setattr(at, 'process_path', lambda pid: '/x')
    authority = object.__new__(at.DesktopOmlx)
    monkeypatch.setattr(at.DesktopOmlx, '_csops', lambda self, pid, op, size: b'\x01' * size)
    monkeypatch.setattr(at.DesktopOmlx, '_csops_string', lambda self, pid, op: 'v')
    with pytest.raises(AuthRefused) as caught:
        authority._incarnation_facts(1, 'python3')
    assert str(caught.value) == 'desktop_incarnation_unstable'


def test_bundles_without_the_reuse_modules_import_and_stay_legacy(tmp_path, monkeypatch):
    """The mini bundle and staged live-QA packages ship only these three files."""
    import importlib
    import shutil
    package = tmp_path / 'wisp_mini_copy'
    package.mkdir()
    (package / '__init__.py').write_text('')
    source = os.path.dirname(at.__file__)
    for name in ('attributed_transport.py', 'local_peer.py', 'inference_errors.py'):
        shutil.copy(os.path.join(source, name), package / name)
    monkeypatch.syspath_prepend(str(tmp_path))
    copy = importlib.import_module('wisp_mini_copy.attributed_transport')
    try:
        assert copy.AttestationStore is None and copy._REUSE.legacy() and copy._REUSE.store is None
        copy._REUSE.invalidate()
        copy.CredentialTransport('http://127.0.0.1:8000', 'k', managed=True).invalidate()
    finally:
        for name in [n for n in sys.modules if n.startswith('wisp_mini_copy')]:
            del sys.modules[name]


def test_store_revoke_key_retires_the_published_lease_and_the_in_flight_build(env):
    world, clock, reuse, backend = env
    run(request(backend))
    key = reuse.key_for(reuse.manifest_path())
    lease = current_lease(reuse, clock)
    clock.advance(41)
    with reuse._lock:
        token = reuse.store.reserve(key, at._BUILDER, reuse._sample_locked(),
                                    build_deadline_ns=clock.now + at.T_BUILD_NS).token
    assert reuse.store.revoke_key(key)
    with reuse._lock:
        sample = reuse._sample_locked()
        assert reuse.store.lookup(key, sample) is None
        assert not reuse.store.publish(token, lease, sample)        # the old build lost authority
    assert not reuse.usable(lease)
