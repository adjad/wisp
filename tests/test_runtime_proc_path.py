"""T0-path: ``DesktopOmlx._executable`` reads the executable with libproc ``proc_pidpath``.

``lsof -d txt`` (about 21 ms and one spawn per call) is replaced by the in-process
``local_peer.process_path`` (about 0.04 ms).  This is security-boundary code, so
the replacement must give the SAME verdict and the SAME refusal reason as the
old code for every input, except for the stricter outcomes listed in
``STRICTER`` below, and it must never be more permissive than ``lsof`` was.

What is here
------------
* ``LegacyExecutable`` - the reference ORACLE: a verbatim copy of the previous
  ``DesktopOmlx._executable`` (the ``lsof -d txt`` spawn and its parsing).  A
  frozen-hash test fails if its text changes.
* A differential corpus on DISPOSABLE REAL CHILD PROCESSES (a copy of the running
  interpreter under a temporary directory, blocked on stdin): plain path, space,
  leading dash, symlinked launch path, deleted-and-replaced and deleted
  executables, a renamed directory, very long paths, every escaped class of
  character (non-ASCII, backslash, tab, newline, DEL, control characters) and a
  seeded random name fuzz.  New is compared with the real ``lsof`` oracle on each.
  These tests skip (never pass vacuously) where this machine denies running a
  copied interpreter or ``lsof``.
* Negative controls on real pids: nonexistent, reaped child, zombie child, own
  pid, pid 1 (another uid), out-of-range and wrongly typed pids.
* Fault injection into the raw libproc call: every errno, truncated, unterminated,
  oversized, NUL / newline / non-ASCII / backslash / relative / empty results,
  wrong result types and exceptions.
* A check of the libproc call itself (library, argtypes, restype, errno, buffer
  size) through a recording fake library, and thread-safety on real calls.
* Pins that the old ``lsof -d txt`` spawn is no longer issued.

The decisions recorded here (see also the comments in ``local_peer.process_path``):

1. NON-ASCII: ``lsof`` printed non-ASCII (and backslash, tab, newline, DEL and
   other control) bytes as literal escape text, so the path failed
   ``resolve(strict=True)`` and was refused with ``desktop_executable_unqualified``.
   ``process_path`` refuses every such path with that same reason.
2. FAILURE CLASSES: ``lsof`` rc 1 for a vanished pid or a zombie was the
   transient ``native_inspection_unavailable`` (retried).  ``proc_pidpath`` failing
   with ESRCH and other native inspection failures retains that bounded retry.
   ENOENT and malformed path/buffer results remain permanent refusals.
3. OTHER-UID PIDS: ``proc_pidpath`` succeeds for another uid's pid where ``lsof``
   refused.  The ``ps`` uid check in ``_listener`` runs first in the evaluated
   order, so the verdict is unchanged; the attestation file proves that under
   the Stage-3 scheduler.  The isolated seam difference is pinned here.

Everything runs on temporary directories, in-process fakes and children this
file starts itself.  It never touches /Applications, port 8000 or any other
process.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import inspect
import itertools
import os
import random
import re
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from service.inference import attributed_transport as at
from service.inference import local_peer
from service.inference.local_peer import AuthRefused

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='libproc proc_pidpath is Darwin only')

UNQUALIFIED = 'desktop_executable_unqualified'
TRANSIENT = 'native_inspection_unavailable'
UID = os.getuid()


# --------------------------------------------------------------------------
# The ORACLE: DesktopOmlx._executable before T0-path.  Copied from
# service/inference/attributed_transport.py at 5894368 (PR #155 head); the only
# changes are the module global spelled ``at.`` and the re-aligned continuation line.
# Do not edit.
# --------------------------------------------------------------------------
class LegacyExecutable:
    def _executable(self, pid):
        raw = at.inspect_command(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid),
                               '-d', 'txt', '-Fn'])
        try:
            rows = raw.decode('utf-8').splitlines()
        except UnicodeError:
            raise AuthRefused('desktop_executable_unqualified') from None
        paths = [row[1:] for row in rows if row.startswith('n')]
        if not paths:
            raise AuthRefused('desktop_executable_unqualified')
        return Path(paths[0])


LEGACY_EXECUTABLE_FROZEN_SHA256 = 'a8047ace30e6f4433b3ab9de51ccbbf46e5db5d3919336074148338acaa3a80e'


def test_legacy_executable_oracle_is_frozen():
    text = textwrap.dedent(inspect.getsource(LegacyExecutable._executable))
    assert hashlib.sha256(text.encode()).hexdigest() == LEGACY_EXECUTABLE_FROZEN_SHA256


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def authority():
    """A DesktopOmlx with no construction-time listener run."""
    instance = object.__new__(at.DesktopOmlx)
    instance.uid, instance._group_cache, instance._qualified_roots = UID, {}, set()
    return instance


def old_executable_once(pid):
    return LegacyExecutable._executable(authority(), pid)


def old_executable(pid):
    """Characterize a live process with at most three seam probes.

    This is not a complete listener retry. The synthetic listener tests below
    exercise production's three complete verifications and permanent refusals.
    """
    for attempt in range(3):
        try:
            return old_executable_once(pid)
        except AuthRefused as error:
            alive = type(pid) is int and 0 < pid <= 2 ** 31 - 1 and independent_probe(pid)[1] != errno.ESRCH
            if str(error) != TRANSIENT or not alive or attempt == 2:
                raise
            time.sleep(0.1)


def new_executable(pid):
    return at.DesktopOmlx._executable(authority(), pid)


def settle(executable, pid, expected):
    """The verdict of looking the pid up and then qualifying it as ``_listener`` does.

    ``expected`` is the one path that may be accepted (the child's real image), so a
    path that is merely escaped, truncated or unrelated is refused exactly as it is
    in ``_listener`` (``_qualified_file`` is untouched by T0-path).
    """
    try:
        path = executable(pid)
    except AuthRefused as error:
        return ('refused', str(error))
    except BaseException as error:  # noqa: BLE001 - the verdict includes the type
        return ('error', type(error).__name__)
    assert isinstance(path, Path)
    try:
        return ('accepted', authority()._qualified_file(path, exact=Path(expected)))
    except AuthRefused as error:
        return ('refused', str(error))


def lookup(executable, pid):
    try:
        return ('path', os.fspath(executable(pid)))
    except AuthRefused as error:
        return ('refused', str(error))
    except BaseException as error:  # noqa: BLE001
        return ('error', type(error).__name__)


def interpreter_image():
    """The file the kernel reports as the image of THIS process: sys.executable itself, or, for a
    framework build whose bin/python is only a launcher, the Python.app binary it executes."""
    image, _ = independent_probe(os.getpid())
    return os.fsdecode(image) if image else os.path.realpath(sys.executable)


def independent_probe(pid):
    """proc_pidpath by an independent route: (bytes or None, errno).  Never the code under test."""
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    ctypes.set_errno(0)
    length = library.proc_pidpath(pid, buffer, 4096)
    return (buffer.raw[:length], 0) if length > 0 else (None, ctypes.get_errno())


@pytest.fixture(scope='module')
def lsof_works():
    try:
        at.inspect_command(['/usr/sbin/lsof', '-nP', '-a', '-p', str(os.getpid()), '-d', 'txt', '-Fn'])
    except (OSError, AuthRefused, subprocess.SubprocessError) as error:
        pytest.skip(f'lsof cannot run here ({type(error).__name__}); the oracle needs it')


class Children:
    """Disposable children, each a copy of the interpreter blocked on stdin; all ended at teardown."""

    def __init__(self):
        self.procs, self.dirs = [], []

    def directory(self):
        # Not pytest's tmp_path: a copied interpreter is large and pytest keeps old trees.
        path = tempfile.TemporaryDirectory(prefix='t0path-')
        self.dirs.append(path)
        return Path(os.path.realpath(path.name))

    def copy(self, directory, name='py'):
        target = directory / name
        shutil.copy2(interpreter_image(), target)
        return target

    def start(self, launch, image):
        """Run ``launch`` (a path, possibly a symlink); return once it is fully initialised and the
        kernel reports ``image`` (a child changed while Python is still starting would die)."""
        try:
            proc = subprocess.Popen([os.fspath(launch), '-S', '-c',
                                     'import sys\nprint("ready", flush=True)\nsys.stdin.read()'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env={'PYTHONHOME': sys.base_prefix})
        except OSError as error:
            pytest.skip(f'cannot run a copied interpreter here: {error}')
        self.procs.append(proc)
        if not select.select([proc.stdout], [], [], 15)[0] or proc.stdout.readline() != b'ready\n':
            pytest.skip('the copied interpreter did not come up on this machine')
        want = os.fsencode(os.fspath(image))
        assert independent_probe(proc.pid)[0] == want, (want, independent_probe(proc.pid))
        return proc

    def close(self):
        for proc in self.procs:
            with contextlib.suppress(OSError):
                proc.stdin.close()
            with contextlib.suppress(OSError):
                proc.stdout.close()
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for path in self.dirs:
            path.cleanup()


@pytest.fixture
def children():
    group = Children()
    yield group
    group.close()


def run_child(children, *parts, name='py'):
    """Copy the interpreter to ``<tmp>/<parts...>/<name>``, run it, return (process, path)."""
    root = children.directory()
    directory = root.joinpath(*parts) if parts else root
    directory.mkdir(parents=True, exist_ok=True)
    target = children.copy(directory, name)
    return children.start(target, target), target


# --------------------------------------------------------------------------
# Differential corpus on real children: new == old (the lsof oracle)
# --------------------------------------------------------------------------
# (id, directory components, executable name, accepted by the old code?)  Names are chosen so
# that the OLD code decides the expectation: lsof prints these classes as escape text.
PLAIN = ('plain', ('plain',), 'py', True)
NAMES = [
    PLAIN,
    ('space', ('with space',), 'py', True),
    ('trailing-space-dir', ('trail ',), 'py', True),
    ('trailing-space-exe', ('plain',), 'py ', True),
    ('leading-dash', ('-dash',), 'py', True),
    ('leading-dot', ('.hidden',), 'py', True),
    ('dots', ('a..b',), 'py', True),
    ('punctuation', ('a;b$c&d(e)',), 'py', True),
    ('star-percent-quote', ('a*b%41"c',), 'py', True),
    ('caret', ('a^Ab',), 'py', True),
    ('nested', ('one', 'two', 'three'), 'py', True),
    ('non-ascii-dir', ('t\u00fcn\u00ef',), 'py', False),
    ('non-ascii-exe', ('plain',), 'p\u00fc', False),
    ('cjk', ('\u65e5\u672c\u8a9e',), 'py', False),
    ('emoji', ('emoji-\U0001f600',), 'py', False),
    ('nbsp', ('a\u00a0b',), 'py', False),
    ('backslash', ('a\\b',), 'py', False),
    ('backslash-x', ('a\\x41b',), 'py', False),
    ('tab', ('a\tb',), 'py', False),
    ('newline', ('a\nb',), 'py', False),
    ('carriage-return', ('a\rb',), 'py', False),
    ('del', ('a\x7fb',), 'py', False),
    ('ctrl-a', ('a\x01b',), 'py', False),
    ('escape-char', ('a\x1bb',), 'py', False),
    ('newline-then-fake-row', ('a\nn', 'bin'), 'sh', False),
]


@pytest.mark.parametrize('name,parts,exe,accepted', NAMES, ids=[n[0] for n in NAMES])
def test_real_child_new_has_the_stated_verdict(children, name, parts, exe, accepted):
    """Needs no lsof: the verdict the oracle gives is stated per case, so a sandbox that denies lsof still checks it."""
    proc, target = run_child(children, *parts, name=exe)
    assert settle(new_executable, proc.pid, target) == (('accepted', os.fspath(target)) if accepted
                                                        else ('refused', UNQUALIFIED))


@pytest.mark.parametrize('name,parts,exe,accepted', NAMES, ids=[n[0] for n in NAMES])
def test_real_child_new_equals_the_lsof_oracle(children, lsof_works, name, parts, exe, accepted):
    proc, target = run_child(children, *parts, name=exe)
    old = settle(old_executable, proc.pid, target)
    new = settle(new_executable, proc.pid, target)
    assert new == old
    assert new == (('accepted', os.fspath(target)) if accepted else ('refused', UNQUALIFIED))


@pytest.mark.parametrize('name,parts,exe,accepted', [n for n in NAMES if n[3]], ids=[n[0] for n in NAMES if n[3]])
def test_real_child_new_returns_the_same_path_the_oracle_returned(children, lsof_works, name, parts, exe, accepted):
    proc, target = run_child(children, *parts, name=exe)
    assert lookup(new_executable, proc.pid) == lookup(old_executable, proc.pid) == ('path', os.fspath(target))


def test_a_symlinked_launch_path_gives_the_real_target(children, lsof_works):
    root = children.directory()
    target = children.copy(root, 'real py')
    link = root / 'launch link'
    link.symlink_to(target)
    proc = children.start(link, target)
    assert lookup(new_executable, proc.pid) == lookup(old_executable, proc.pid) == ('path', os.fspath(target))
    assert settle(new_executable, proc.pid, target) == settle(old_executable, proc.pid, target) == ('accepted', os.fspath(target))


def test_a_symlinked_directory_in_the_launch_path_gives_the_real_target(children, lsof_works):
    root = children.directory()
    (root / 'real dir').mkdir()
    target = children.copy(root / 'real dir', 'py')
    (root / 'via').symlink_to(root / 'real dir')
    proc = children.start(root / 'via' / 'py', target)
    assert lookup(new_executable, proc.pid) == lookup(old_executable, proc.pid) == ('path', os.fspath(target))


def test_an_executable_deleted_and_replaced_while_running(children, lsof_works):
    proc, target = run_child(children, 'swap')
    target.unlink()
    target.write_bytes(b'replacement')
    os.chmod(target, 0o755)
    assert lookup(new_executable, proc.pid) == lookup(old_executable, proc.pid) == ('path', os.fspath(target))
    # Both then qualify the replacement's file, exactly as before.
    assert settle(new_executable, proc.pid, target) == settle(old_executable, proc.pid, target)


def test_an_executable_deleted_while_running_is_refused_like_the_oracle(children, lsof_works):
    """lsof still printed the old name (which then failed resolve); proc_pidpath fails with ENOENT."""
    proc, target = run_child(children, 'gone')
    target.unlink()
    old = settle(old_executable, proc.pid, target)
    new = settle(new_executable, proc.pid, target)
    assert old == ('refused', UNQUALIFIED)
    assert new == old


def test_a_renamed_directory_follows_the_process_like_the_oracle(children, lsof_works):
    root = children.directory()
    (root / 'before').mkdir()
    target = children.copy(root / 'before')
    proc = children.start(target, target)
    (root / 'before').rename(root / 'after')
    moved = root / 'after' / 'py'
    assert lookup(new_executable, proc.pid) == lookup(old_executable, proc.pid)
    assert settle(new_executable, proc.pid, moved) == settle(old_executable, proc.pid, moved)


@pytest.mark.parametrize('target_length', [700, 950, 1000])
def test_a_very_long_path_equals_the_oracle(children, lsof_works, target_length):
    """The kernel's PATH_MAX (1024) bounds a runnable image; the 4096 buffer is four times that."""
    root = children.directory()
    directory = root
    while len(os.fspath(directory)) < target_length - 12:
        directory = directory / ('d' * min(200, target_length - 12 - len(os.fspath(directory)) - 1))
    directory.mkdir(parents=True)
    target = children.copy(directory)
    assert len(os.fspath(target)) < 1013
    try:
        proc = children.start(target, target)
    except OSError:
        pytest.skip('this filesystem cannot run so long a path')
    assert lookup(new_executable, proc.pid) == lookup(old_executable, proc.pid) == ('path', os.fspath(target))
    assert settle(new_executable, proc.pid, target) == settle(old_executable, proc.pid, target)


FUZZ_POOL = ([chr(c) for c in range(0x20, 0x7f)] + [chr(c) for c in range(1, 0x20)] + ['\x7f']
             + ['\u00fc', '\u00e9', '\u65e5', '\U0001f600', '\u00a0', '\u200b'])


def fuzz_name(seed):
    rng = random.Random(f'proc-path-{seed}')
    while True:
        name = ''.join(rng.choice(FUZZ_POOL) for _ in range(rng.randint(1, 10)))
        if '/' not in name and name not in ('.', '..'):
            return name


def printable_ascii_without_backslash(text):
    return all(0x20 <= ord(c) < 0x7f and c != '\\' for c in text)


@pytest.mark.parametrize('seed', range(40))
def test_seeded_random_directory_names_have_the_rule_based_verdict(children, seed):
    name = fuzz_name(seed)
    proc, target = run_child(children, name)
    expected = ('accepted', os.fspath(target)) if printable_ascii_without_backslash(name) else ('refused', UNQUALIFIED)
    assert settle(new_executable, proc.pid, target) == expected, repr(name)


@pytest.mark.parametrize('seed', range(40))
def test_seeded_random_directory_names_equal_the_oracle(children, lsof_works, seed):
    name = fuzz_name(seed)
    proc, target = run_child(children, name)
    old = settle(old_executable, proc.pid, target)
    new = settle(new_executable, proc.pid, target)
    assert new == old, repr(name)
    expected = ('accepted', os.fspath(target)) if printable_ascii_without_backslash(name) else ('refused', UNQUALIFIED)
    assert new == expected, repr(name)


# --------------------------------------------------------------------------
# Negative controls on real pids
# --------------------------------------------------------------------------
def reaped_pid():
    proc = subprocess.Popen([sys.executable, '-S', '-c', 'pass'], env={'PYTHONHOME': sys.base_prefix},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    return proc.pid


def test_a_nonexistent_or_zombie_pid_is_the_transient_reason_without_lsof():
    assert lookup(new_executable, reaped_pid()) == ('refused', TRANSIENT)
    proc = subprocess.Popen(['/bin/sh', '-c', 'exit 0'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        assert lookup(new_executable, proc.pid) == ('refused', TRANSIENT)
    finally:
        proc.wait()


def test_a_nonexistent_pid_is_the_transient_reason_like_the_oracle(lsof_works):
    pid = reaped_pid()
    assert independent_probe(pid) == (None, errno.ESRCH)
    assert lookup(old_executable, pid) == ('refused', TRANSIENT)
    assert lookup(new_executable, pid) == ('refused', TRANSIENT)


def test_a_zombie_is_the_transient_reason_like_the_oracle(lsof_works):
    proc = subprocess.Popen(['/bin/sh', '-c', 'exit 0'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)         # exited, deliberately not reaped
        assert independent_probe(proc.pid) == (None, errno.ESRCH)
        assert lookup(old_executable, proc.pid) == ('refused', TRANSIENT)
        assert lookup(new_executable, proc.pid) == ('refused', TRANSIENT)
    finally:
        proc.wait()


def test_our_own_pid_equals_the_oracle(lsof_works):
    assert lookup(new_executable, os.getpid()) == lookup(old_executable, os.getpid())
    assert lookup(new_executable, os.getpid()) == ('path', os.fsdecode(independent_probe(os.getpid())[0]))


def test_PINNED_DELTA_another_uid_pid_is_looked_up_by_the_seam_where_lsof_refused(lsof_works):
    """proc_pidpath answers for pid 1 (root) where lsof exited 1 and so refused.

    This is NOT the verdict of `_listener`: its `ps` uid check runs first in the evaluated order
    (see the foreign-uid tests in test_runtime_attestation_t0.py, under the real Stage-3 scheduler),
    so `_executable` is never the only gate.  If a defence-in-depth uid check is ever added to the
    seam, this test must change deliberately.
    """
    assert lookup(old_executable_once, 1) == ('refused', TRANSIENT)
    kind, path = lookup(new_executable, 1)
    assert kind == 'path' and path == os.fsdecode(independent_probe(1)[0]) and path.startswith('/')


PID_BAD_BOTH = [0, -1, -5, 2 ** 31 - 1, 2 ** 31, 2 ** 40, None, True, False, 1.5, 'abc', b'12', [], float('nan')]


@pytest.mark.parametrize('pid', PID_BAD_BOTH, ids=[repr(p) for p in PID_BAD_BOTH])
def test_an_unusable_pid_is_the_transient_reason_like_the_oracle(lsof_works, pid):
    assert lookup(old_executable, pid) == ('refused', TRANSIENT)
    assert lookup(new_executable, pid) == ('refused', TRANSIENT)


# The ONLY inputs on which the new code is STRICTER than the oracle (listed in the handoff).
# Each is accepted by the old code and refused (as a nonexistent pid) by the new one.
STRICTER = {
    'pid beyond 32 bits that lsof silently truncates to a live pid': lambda: 2 ** 32 + os.getpid(),
    'pid given as a decimal string': lambda: str(os.getpid()),
}


@pytest.mark.parametrize('label', list(STRICTER))
def test_STRICTER_inputs_are_refused_by_the_new_code_and_were_accepted_by_the_old(lsof_works, label):
    pid = STRICTER[label]()
    assert lookup(old_executable, pid)[0] == 'path', 'the oracle no longer accepts it: drop it from STRICTER'
    assert lookup(new_executable, pid) == ('refused', TRANSIENT)


# --------------------------------------------------------------------------
# Fault injection into the raw libproc call
# --------------------------------------------------------------------------
SIZE = 4096


def raw_of(path: bytes, size=SIZE):
    return path + b'\0' * (size - len(path))


@pytest.fixture
def libproc(monkeypatch):
    """Replace the raw libproc call; returns the list of (pid, size) it was called with."""
    calls = []

    def program(length, raw=None, error=0):
        def fake(pid, size):
            calls.append((pid, size))
            return length, (raw if raw is not None else b'\0' * size), error
        monkeypatch.setattr(local_peer, '_proc_pidpath', fake)
        return calls
    return program


def refused_with(call):
    with pytest.raises(AuthRefused) as caught:
        call()
    return str(caught.value)


GOOD = b'/Applications/oMLX.app/Contents/MacOS/oMLX'


def test_a_good_result_is_returned_as_text(libproc):
    calls = libproc(len(GOOD), raw_of(GOOD))
    result = local_peer.process_path(4321)
    assert result == GOOD.decode() and type(result) is str
    assert calls == [(4321, 4096)]


def test_the_stale_errno_of_a_successful_call_is_ignored(libproc):
    libproc(len(GOOD), raw_of(GOOD), errno.ESRCH)
    assert local_peer.process_path(4321) == GOOD.decode()


def test_the_longest_path_that_fits_is_returned(libproc):
    path = b'/' + b'a' * (SIZE - 2)
    assert len(path) == SIZE - 1
    libproc(len(path), raw_of(path))
    assert local_peer.process_path(7) == path.decode()


@pytest.mark.parametrize('error', [errno.ESRCH, errno.ENOMEM, errno.EINVAL, errno.EPERM, errno.EACCES,
                                   errno.EIO, errno.EBADF, errno.ENAMETOOLONG, errno.EFAULT, errno.EAGAIN,
                                   errno.EINTR, 99999, -1])
@pytest.mark.parametrize('length', [0, -1])
def test_native_inspection_failures_preserve_bounded_retry(libproc, length, error):
    libproc(length, None, error)
    assert refused_with(lambda: local_peer.process_path(7)) == TRANSIENT


@pytest.mark.parametrize('length', [0, -1])
@pytest.mark.parametrize('error', [0, errno.ENOENT])
def test_missing_executable_is_refused_without_a_retry(libproc, length, error):
    libproc(length, None, error)
    assert refused_with(lambda: local_peer.process_path(7)) == UNQUALIFIED


@pytest.mark.parametrize('length', [SIZE, SIZE + 1, 2 ** 31 - 1, 10 ** 9])
def test_a_length_that_does_not_fit_the_buffer_is_refused(libproc, length):
    libproc(length, raw_of(GOOD))
    assert refused_with(lambda: local_peer.process_path(7)) == UNQUALIFIED


def test_a_full_buffer_without_terminator_is_refused(libproc):
    libproc(SIZE - 1, b'/' + b'a' * (SIZE - 1))                     # raw[length] is not NUL
    assert refused_with(lambda: local_peer.process_path(7)) == UNQUALIFIED


def test_a_path_not_terminated_at_the_reported_length_is_refused(libproc):
    libproc(len(GOOD), raw_of(GOOD + b'x'))                          # a byte follows the reported end
    assert refused_with(lambda: local_peer.process_path(7)) == UNQUALIFIED


@pytest.mark.parametrize('size', [0, 1, 8, len(GOOD), len(GOOD) + 1, SIZE - 1, SIZE + 1, 2 * SIZE])
def test_a_buffer_that_is_not_the_requested_size_is_refused(libproc, size):
    libproc(len(GOOD), (GOOD + b'\0' * size)[:size])
    assert refused_with(lambda: local_peer.process_path(7)) == UNQUALIFIED


def test_a_tiny_requested_buffer_as_the_kernel_reports_it_is_refused(libproc):
    """What proc_pidpath really does for a buffer that is too small: 0 and ENOMEM."""
    libproc(0, b'\0' * 8, errno.ENOMEM)
    assert refused_with(lambda: local_peer.process_path(7)) == UNQUALIFIED


@pytest.mark.parametrize('path', [
    b'', b'relative/py', b'./py', b'py', b' /abs', b'../x', b'C:/x', b'~/x',
    b'/a\0b', b'/a\nb', b'/a\rb', b'/a\tb', b'/a\x01b', b'/a\x1fb', b'/a\x7fb', b'/a\\b', b'/a\\\\b',
    '/t\u00fc'.encode(), '/\u65e5'.encode(), '/\U0001f600'.encode(), b'/a\xc3', b'/a\xff', b'/a\x80b', b'/a\xc2\xa0b',
], ids=repr)
def test_a_path_that_lsof_would_have_escaped_or_that_is_not_absolute_is_refused(libproc, path):
    libproc(len(path), raw_of(path))
    assert refused_with(lambda: local_peer.process_path(7)) == UNQUALIFIED


@pytest.mark.parametrize('path', [
    b'/', b'/a', b'/a b', b'/a/b c/d', b'/-dash', b'/a^b', b'/a"b', b"/a'b", b'/a*b?c[d]', b'/a%41', b'/a;b$c&d',
    b'/a/../b', b'/a//b', b'/a/', b'/ ' + b'x', b'/a~', b'/a{b}|c`d', b'/a<b>c', b'/a=b+c,d@e!f#g:h',
    bytes(range(0x20, 0x5c)) .join([b'/', b'']), bytes(range(0x5d, 0x7f)).join([b'/', b'']),
], ids=repr)
def test_every_printable_ascii_character_except_backslash_is_accepted_by_the_helper(libproc, path):
    libproc(len(path), raw_of(path))
    assert local_peer.process_path(7) == path.decode()


def test_the_character_class_is_exactly_printable_ascii_without_backslash(libproc):
    accepted = set()
    for code in range(256):
        path = b'/a' + bytes([code]) + b'b'
        libproc(len(path), raw_of(path))
        with contextlib.suppress(AuthRefused):
            local_peer.process_path(7)
            accepted.add(code)
    assert accepted == set(range(0x20, 0x7f)) - {0x5c}


@pytest.mark.parametrize('pid', [0, -1, 2 ** 31, 2 ** 32 + 5, '7', None, True, 1.0, b'7'], ids=repr)
def test_an_unusable_pid_never_reaches_libproc(libproc, pid):
    calls = libproc(len(GOOD), raw_of(GOOD))
    assert refused_with(lambda: local_peer.process_path(pid)) == TRANSIENT
    assert calls == []


@pytest.mark.parametrize('pid', [1, 2, 2 ** 31 - 1, 99999])
def test_a_usable_pid_reaches_libproc_unchanged(libproc, pid):
    calls = libproc(len(GOOD), raw_of(GOOD))
    assert local_peer.process_path(pid) == GOOD.decode()
    assert calls == [(pid, 4096)]


@pytest.mark.parametrize('result', [
    ('5', raw_of(GOOD), 0), (None, raw_of(GOOD), 0), (len(GOOD) + 0.0, raw_of(GOOD), 0), (True, raw_of(b'/'), 0),
    (len(GOOD), raw_of(GOOD).decode(), 0), (len(GOOD), bytearray(raw_of(GOOD)), 0), (len(GOOD), None, 0),
    (0, None, '3'), (0, None, None), (0, None, 3.0),
    (0, raw_of(b''), '3'), (0, raw_of(b''), None), (0, raw_of(b''), 3.0), (-1, raw_of(b''), 3.0),
    (len(GOOD), raw_of(GOOD), '0'), (len(GOOD), raw_of(GOOD), None),
], ids=repr)
def test_a_result_of_the_wrong_type_is_refused(monkeypatch, result):
    monkeypatch.setattr(local_peer, '_proc_pidpath', lambda pid, size: result)
    with pytest.raises(AuthRefused) as caught:
        local_peer.process_path(7)
    assert str(caught.value) == UNQUALIFIED


@pytest.mark.parametrize('error', [OSError('no libproc'), AttributeError('no symbol'), ctypes.ArgumentError('arg'),
                                   ValueError('v'), TypeError('t'), RuntimeError('r'), MemoryError(), IndexError('i'),
                                   ZeroDivisionError('z')], ids=lambda e: type(e).__name__)
def test_any_exception_from_the_native_call_is_a_refusal_never_an_acceptance(monkeypatch, error):
    def explode(pid, size):
        raise error
    monkeypatch.setattr(local_peer, '_proc_pidpath', explode)
    assert refused_with(lambda: local_peer.process_path(7)) == TRANSIENT


@pytest.mark.parametrize('error', [KeyboardInterrupt(), SystemExit(1)], ids=lambda e: type(e).__name__)
def test_an_interrupt_is_not_swallowed(monkeypatch, error):
    def interrupt(pid, size):
        raise error
    monkeypatch.setattr(local_peer, '_proc_pidpath', interrupt)
    with pytest.raises(type(error)):
        local_peer.process_path(7)


def test_a_failing_native_call_is_never_a_path_for_the_executable_seam(libproc):
    libproc(0, None, errno.ENOMEM)
    with pytest.raises(AuthRefused) as caught:
        new_executable(7)
    assert str(caught.value) == TRANSIENT


# --------------------------------------------------------------------------
# The raw call itself, through a recording fake library
# --------------------------------------------------------------------------
class FakeFunction:
    def __init__(self, library):
        self.library = library
        self.argtypes = self.restype = None

    def __call__(self, pid, buffer, size):
        self.library.events.append(('call', pid, size, self.argtypes, self.restype, ctypes.get_errno()))
        path = self.library.path
        if path is None:
            ctypes.set_errno(self.library.error)
            return 0
        ctypes.memmove(buffer, path + b'\0', len(path) + 1)
        return len(path)


class FakeLibrary:
    def __init__(self, path=None, error=errno.ESRCH):
        self.path, self.error, self.events = path, error, []
        self.proc_pidpath = FakeFunction(self)


@pytest.fixture
def fake_library(monkeypatch):
    def install(path=None, error=errno.ESRCH):
        library = FakeLibrary(path, error)

        def cdll(name, **kwargs):
            library.events.append(('cdll', name, kwargs))
            return library
        monkeypatch.setattr(local_peer.ctypes, 'CDLL', cdll)
        return library
    return install


def test_the_raw_call_loads_libproc_with_errno_and_declares_its_types(fake_library):
    library = fake_library(GOOD)
    ctypes.set_errno(errno.EPERM)              # a stale errno must be cleared before the call
    assert local_peer.process_path(4321) == GOOD.decode()
    assert library.events[0] == ('cdll', '/usr/lib/libproc.dylib', {'use_errno': True})
    kind, pid, size, argtypes, restype, errno_before = library.events[1]
    assert (kind, pid, size) == ('call', 4321, 4096)
    assert argtypes == [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32] and restype is ctypes.c_int
    assert errno_before == 0
    assert len(library.events) == 2


def test_the_raw_call_reports_errno_of_a_failed_call(fake_library):
    fake_library(None, errno.ESRCH)
    assert refused_with(lambda: local_peer.process_path(4321)) == TRANSIENT
    fake_library(None, errno.ENOMEM)
    assert refused_with(lambda: local_peer.process_path(4321)) == TRANSIENT


def test_a_missing_library_or_symbol_is_a_refusal(monkeypatch):
    monkeypatch.setattr(local_peer.ctypes, 'CDLL', lambda *a, **k: (_ for _ in ()).throw(OSError('dlopen')))
    assert refused_with(lambda: local_peer.process_path(7)) == TRANSIENT
    monkeypatch.setattr(local_peer.ctypes, 'CDLL', lambda *a, **k: SimpleLibraryWithoutSymbol())
    assert refused_with(lambda: local_peer.process_path(7)) == TRANSIENT


class SimpleLibraryWithoutSymbol:
    def __getattr__(self, name):
        raise AttributeError(name)


def test_the_buffer_constant_is_the_kernels_proc_pidpathinfo_maxsize():
    assert local_peer.PROC_PIDPATHINFO_MAXSIZE == 4096 == 4 * 1024      # <sys/proc_info.h>: 4 * MAXPATHLEN


# --------------------------------------------------------------------------
# The seam in attributed_transport
# --------------------------------------------------------------------------
def test_executable_returns_a_path_object_built_from_the_helper(monkeypatch):
    monkeypatch.setattr(at, 'process_path', lambda pid: f'/x/{pid}')
    result = new_executable(5)
    assert type(result) is type(Path('/')) and result == Path('/x/5')


def test_the_helper_is_looked_up_as_a_module_global_at_call_time(monkeypatch):
    seen = []
    monkeypatch.setattr(at, 'process_path', lambda pid: seen.append(pid) or '/x')
    new_executable(11)
    monkeypatch.setattr(at, 'process_path', lambda pid: seen.append(('second', pid)) or '/y')
    assert new_executable(12) == Path('/y')
    assert seen == [11, ('second', 12)]


def test_the_transport_imports_the_helper_it_is_tested_with():
    assert at.process_path is local_peer.process_path


def test_a_relative_path_from_the_helper_is_still_refused_by_the_qualification(monkeypatch):
    """Defence in depth that already existed and is untouched: _qualified_file demands resolved == path."""
    monkeypatch.setattr(at, 'process_path', lambda pid: 'relative/py')
    with pytest.raises(AuthRefused) as caught:
        authority()._qualified_file(new_executable(5), exact=Path('/x'))
    assert str(caught.value) == UNQUALIFIED


def test_a_relative_path_that_exists_relative_to_the_cwd_is_not_resolved_into_an_acceptance(monkeypatch, tmp_path):
    """`_executable` must not turn a relative result into an absolute one: Path.resolve() would make
    `py` match the file under the cwd and so be accepted by the equality in `_qualified_file`."""
    target = tmp_path.resolve() / 'py'
    target.write_bytes(b'x')
    os.chmod(target, 0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(at, 'process_path', lambda pid: 'py')
    assert new_executable(5) == Path('py') and not new_executable(5).is_absolute()
    with pytest.raises(AuthRefused) as caught:
        authority()._qualified_file(new_executable(5), exact=target)
    assert str(caught.value) == UNQUALIFIED


def test_executable_issues_no_spawn_and_never_runs_lsof(monkeypatch):
    def forbidden(argv):
        raise AssertionError(f'a process was spawned: {argv}')
    monkeypatch.setattr(at, 'inspect_command', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(at, 'process_path', lambda pid: '/x/py')
    assert new_executable(5) == Path('/x/py')
    code = at.DesktopOmlx._executable.__code__
    assert not {'inspect_command', 'subprocess'} & set(code.co_names)
    assert not any(isinstance(c, str) and ('lsof' in c or c in ('-d', 'txt')) for c in code.co_consts)


def test_executable_keeps_its_signature_and_has_no_other_effect():
    assert str(inspect.signature(at.DesktopOmlx._executable)) == '(self, pid)'
    assert str(inspect.signature(local_peer.process_path)) == '(pid)'
    assert str(inspect.signature(local_peer.process_identity)) == '(pid, uid)'


def test_process_identity_is_unchanged_in_behaviour():
    identity = local_peer.process_identity(os.getpid(), UID)
    assert isinstance(identity, tuple) and len(identity) == 4 and identity[:2] == (os.getpid(), UID)


# --------------------------------------------------------------------------
# Real calls from many threads (the pool calls the helper concurrently)
# --------------------------------------------------------------------------
def test_concurrent_real_calls_share_no_buffer_and_no_errno(children):
    proc, target = run_child(children, 'threads')
    gone = reaped_pid()
    own = os.fsdecode(independent_probe(os.getpid())[0])
    expected = {proc.pid: os.fspath(target), os.getpid(): own, gone: TRANSIENT}
    pids = list(expected)
    errors, barrier = [], threading.Barrier(16, timeout=30)

    def work(index):
        barrier.wait()
        rng = random.Random(index)
        for _ in range(300):
            pid = rng.choice(pids)
            try:
                got = local_peer.process_path(pid)
            except AuthRefused as error:
                got = str(error)
            if got != expected[pid]:
                errors.append((pid, got))
    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors, errors[:5]


def test_the_helper_runs_on_the_inspection_pool_threads(monkeypatch, children):
    proc, target = run_child(children, 'pool')
    future = at._POOL.submit(local_peer.process_path, proc.pid)
    assert future.result(30) == os.fspath(target)
    at._POOL.shutdown()


# --------------------------------------------------------------------------
# The stage's own source pins
# --------------------------------------------------------------------------
def test_the_module_still_has_exactly_one_process_spawning_path():
    source = inspect.getsource(at)
    assert source.count('subprocess.run(') == 1 and 'Popen' not in source
    assert 'lsof' in inspect.getsource(at.DesktopOmlx._listener)          # the LISTEN lookup is still lsof
    assert "'-d'" not in source and '"-d"' not in source and "'txt'" not in source


def test_the_helper_does_not_spawn_or_read_the_environment():
    source = inspect.getsource(local_peer.process_path) + inspect.getsource(local_peer._proc_pidpath)
    for forbidden in ('subprocess', 'os.environ', 'getenv', 'Popen', 'system(', 'open('):
        assert forbidden not in source


@pytest.mark.parametrize('error', [errno.ENOMEM, errno.EINTR, errno.EAGAIN, errno.EPERM])
@pytest.mark.parametrize('recover', [True, False])
def test_native_failure_retries_complete_listener_at_most_three_times(tmp_path, monkeypatch, error, recover):
    from tests.test_runtime_attestation_t0 import Host, bare
    host = Host(tmp_path, monkeypatch, 'host')
    state = {'attempts': 0}
    original = host.inspect

    def inspect(argv):
        if '-iTCP:8000' in argv:
            state['attempts'] += 1
        return original(argv)

    def raw(pid, size):
        if pid == 321 and (not recover or state['attempts'] < 3):
            return 0, b'\0' * size, error
        path = host.txt[str(pid)].encode()
        return len(path), path + b'\0' * (size - len(path)), 0

    monkeypatch.setattr(at, 'inspect_command', inspect)
    monkeypatch.setattr(local_peer, '_proc_pidpath', raw)
    monkeypatch.setattr(at, 'process_path', local_peer.process_path)
    authority = bare()

    class Peer:
        def connected_peer(self, *args):
            return authority._listener()

    try:
        if recover:
            at.connected_peer_with_retry(Peer(), None, 321, None, pause=0)
        else:
            assert refused_with(lambda: at.connected_peer_with_retry(Peer(), None, 321, None, pause=0)) == TRANSIENT
        assert state['attempts'] == 3
    finally:
        at._POOL.shutdown()


def test_transport_timing_ratios_use_unrounded_samples():
    from scripts import bench_transport_overhead as bench
    assert bench.speedups([20.0, 20.0], [0.04, 0.04]) == {
        'speedup_p50': 500.0, 'speedup_p95': 500.0, 'speedup_min': 500.0}
    assert bench.summarize([0.04, 0.04])['p50'] == 0.04
    assert all(value is None for value in bench.speedups([1.0], [0.0]).values())


@pytest.mark.parametrize('fail', [False, True])
def test_executable_benchmark_cleans_pools_on_success_and_failure(monkeypatch, fail):
    from types import SimpleNamespace
    from scripts import bench_transport_overhead as bench
    stopped = []

    def load():
        if fail:
            raise RuntimeError('synthetic inspection failure')
        return SimpleNamespace(_identity=(321, '/exe', 1, '/parent'))

    def module(label):
        return SimpleNamespace(RuntimeAuthority=lambda: SimpleNamespace(load=load),
                               DesktopOmlx=SimpleNamespace(_executable=lambda authority, pid: Path('/exe')),
                               _POOL=SimpleNamespace(shutdown=lambda: stopped.append(label)))

    monkeypatch.setattr(bench, 'alternate', lambda *args: ([20.0], [0.04]))
    if fail:
        with pytest.raises(RuntimeError):
            bench.executable_benchmark(module('old'), module('new'), 1)
    else:
        monkeypatch.setattr(bench, 'fresh', lambda module: None)
        rows = bench.executable_benchmark(module('old'), module('new'), 1)
        assert rows['server']['speedup_p50'] == 500.0
    assert stopped == ['old', 'new']


# Frozen pre-native ps command oracle. No changes to LegacyExecutable above.
class LegacyProcess:
    def _process(self, pid):
        raw = at.inspect_command(['/bin/ps', '-ww', '-p', str(pid),
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

LEGACY_PROCESS_SHA256 = 'e5ae7bb294ca1bd2e09655a9fff9981581a13f277334a47292764ae6fc614686'

def test_legacy_process_oracle_is_frozen():
    assert hashlib.sha256(textwrap.dedent(inspect.getsource(LegacyProcess._process)).rstrip().encode()).hexdigest() == LEGACY_PROCESS_SHA256

# Native argv[0] unit corpus: every syscall below is an in-process recording fake.
# No real process, copied image, ps invocation, socket, or native query is needed.
def procargs_image(argv=(b'omlx-server',), path=b'/synthetic/python', env=(b'WISP_CANARY=private-synthetic',)):
    import struct
    saved = path + b'\0'
    padding = b'\0' * (-len(saved) % 8)
    return struct.pack('=i', len(argv)) + saved + padding + b''.join(arg + b'\0' for arg in argv + env)


def parse_image(payload, capacity=1024):
    buffer = bytearray(capacity)
    buffer[:len(payload)] = payload
    return local_peer._parse_procargs(buffer, len(payload))


@pytest.mark.parametrize('path', [b'/' + b'x' * n for n in range(1, 34)])
def test_native_command_parser_uses_exact_target_alignment(path):
    assert parse_image(procargs_image(path=path)) == 'omlx-server'


@pytest.mark.parametrize('argv,env', [
    ((b'', b'omlx-server'), (b'WISP_CANARY=omlx-server',)),
    ((b'',), (b'omlx-server',)),
    ((b'', b''), (b'omlx-server', b'another-env-string-without-equals')),
    ((), (b'omlx-server',)),
])
def test_native_command_never_substitutes_a_later_argument_or_environment(argv, env):
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        parse_image(procargs_image(argv, env=env))


@pytest.mark.parametrize('command', [b'bad\ncommand', b'bad\tcommand', b'bad\\command',
    b'bad\x7fcommand', b'bad\xffcommand', b' omlx-server', b'omlx-server '])
def test_native_command_refuses_ps_escaping_or_whitespace_normalization(command):
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        parse_image(procargs_image((command,)))


def test_native_command_does_not_copy_or_select_other_arguments_or_environment():
    assert parse_image(procargs_image((b'omlx-server', b'', b'--api-key=synthetic-private'),
                                     env=(b'omlx-server', b'KEY=synthetic-private'))) == 'omlx-server'


@pytest.mark.parametrize('mutation', ['zero_argc', 'negative_argc', 'huge_argc', 'missing_path',
    'relative_path', 'padding_nonzero', 'extra_padding', 'truncated_argv', 'truncated_later_arg',
    'saturated', 'wrong_length_type', 'oversize_length'])
def test_native_command_parser_rejects_incomplete_ambiguous_and_malformed_data(mutation):
    import struct
    payload = procargs_image((b'omlx-server', b'arg'), env=())
    buffer = bytearray(1024)
    buffer[:len(payload)] = payload
    length = len(payload)
    path_end = payload.index(0, 4)
    offset = 4 + ((path_end - 4 + 1 + 7) // 8) * 8
    if mutation == 'zero_argc': struct.pack_into('=i', buffer, 0, 0)
    elif mutation == 'negative_argc': struct.pack_into('=i', buffer, 0, -1)
    elif mutation == 'huge_argc': struct.pack_into('=i', buffer, 0, 65537)
    elif mutation == 'missing_path': buffer[4:length] = b'x' * (length - 4)
    elif mutation == 'relative_path': buffer[4] = ord('x')
    elif mutation == 'padding_nonzero': buffer[path_end + 1] = ord('x')
    elif mutation == 'extra_padding': buffer[offset] = 0
    elif mutation == 'truncated_argv': length = offset + 3
    elif mutation == 'truncated_later_arg': length -= 1
    elif mutation == 'saturated': length = len(buffer)
    elif mutation == 'wrong_length_type': length = True
    elif mutation == 'oversize_length': length = len(buffer) + 1
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        local_peer._parse_procargs(buffer, length)


class NativeCommandRig:
    def __init__(self):
        self.payload = procargs_image()
        self.argmax = 1024
        self.flags = 0x10
        self.pid = 4321
        self.uid = UID
        self.ruid = self.svuid = UID
        self.error = 0
        self.returncode = 0
        self.used = None
        self.calls = []
        self.buffers = []
        self.count = 136
        self.argmax_size = 4

    def cdll(self, name, **kwargs):
        from types import SimpleNamespace
        self.calls.append(('library', name, kwargs))
        if name == '/usr/lib/libproc.dylib':
            def info(pid, flavor, argument, pointer, size):
                self.calls.append(('bsd', pid, flavor, argument, size))
                row = pointer._obj
                row.pid, row.uid, row.ruid, row.svuid, row.flags = self.pid, self.uid, self.ruid, self.svuid, self.flags
                return self.count
            return SimpleNamespace(proc_pidinfo=info)
        assert name == '/usr/lib/libSystem.B.dylib'
        def sysctl(mib, count, buffer, size, newp, newlen):
            args = tuple(mib)
            self.calls.append(('sysctl', args, newp, newlen, size._obj.value, ctypes.get_errno()))
            assert newp is None and newlen == 0
            if args == (1, 8):
                buffer._obj.value = self.argmax
                size._obj.value = self.argmax_size
                return 0
            assert args == (1, 49, self.pid)
            self.buffers.append(buffer)
            ctypes.memmove(buffer, self.payload, min(len(self.payload), len(buffer)))
            size._obj.value = len(self.payload) if self.used is None else self.used
            ctypes.set_errno(self.error)
            return self.returncode
        return SimpleNamespace(sysctl=sysctl)


@pytest.fixture
def native_command(monkeypatch):
    rig = NativeCommandRig()
    monkeypatch.setattr(local_peer.ctypes, 'CDLL', rig.cdll)
    return rig


def test_native_command_reads_only_pid_after_same_uid_lp64_and_clears_buffer(native_command):
    assert local_peer._proc_argv0(4321, UID) == 'omlx-server'
    queries = [row for row in native_command.calls if row[0] == 'sysctl']
    assert queries == [('sysctl', (1, 8), None, 0, 4, 0),
                       ('sysctl', (1, 49, 4321), None, 0, 1028, 0)]
    assert native_command.buffers and not any(native_command.buffers[0].raw)


@pytest.mark.parametrize('error', [errno.EPERM, errno.EACCES, errno.ESRCH, errno.EINVAL, errno.EINTR, errno.EIO])
def test_native_command_denial_is_transient_without_raw_exception_or_data(native_command, error):
    native_command.returncode, native_command.error = -1, error
    with pytest.raises(AuthRefused) as caught:
        local_peer._proc_argv0(4321, UID)
    assert caught.value.args == ('native_inspection_unavailable',)
    assert not any(native_command.buffers[0].raw)


@pytest.mark.parametrize('change', ['pid', 'uid', 'ruid', 'svuid', 'flags'])
def test_native_command_rechecks_uid_pid_and_target_bitness_before_argument_access(native_command, change):
    setattr(native_command, change, 0)
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        local_peer._proc_argv0(4321, UID)
    assert not any(row[0] == 'sysctl' for row in native_command.calls)


@pytest.mark.parametrize('argmax', [0, -1, 2 ** 30])
def test_native_command_refuses_unbounded_allocation(native_command, argmax):
    native_command.argmax = argmax
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        local_peer._proc_argv0(4321, UID)
    assert not native_command.buffers


@pytest.mark.parametrize('used', [0, 3, 1028, 1029])
def test_native_command_refuses_short_or_saturated_return_and_clears(native_command, used):
    native_command.used = used
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        local_peer._proc_argv0(4321, UID)
    assert not any(native_command.buffers[0].raw)


@pytest.mark.parametrize('pid', [True, None, '4321', 0, -1, 2 ** 31])
def test_native_process_row_bad_pid_cannot_reach_any_inspection(monkeypatch, pid):
    monkeypatch.setattr(local_peer, 'process_bsdinfo', lambda pid: pytest.fail('inspected invalid PID'))
    with pytest.raises(AuthRefused, match='^native_inspection_unavailable$'):
        local_peer.process_command(pid, UID)


def test_native_process_row_brackets_command_read_and_keeps_argv_not_names(monkeypatch):
    facts = (4321, 77, UID, UID, UID, b'python3', b'python3', 123, 456)
    calls = []
    monkeypatch.setattr(local_peer, 'process_bsdinfo', lambda pid: calls.append(('bsd', pid)) or facts)
    monkeypatch.setattr(local_peer, '_proc_argv0', lambda pid, uid: calls.append(('argv', pid, uid)) or 'omlx-server')
    assert local_peer.process_command(4321, UID) == (77, UID, 'omlx-server')
    assert calls == [('bsd', 4321), ('argv', 4321, UID), ('bsd', 4321)]


@pytest.mark.parametrize('field', [2, 3, 4])
def test_native_process_row_other_uid_has_no_argument_access(monkeypatch, field):
    facts = [4321, 77, UID, UID, UID, b'python3', b'python3', 123, 456]
    facts[field] = UID + 1
    monkeypatch.setattr(local_peer, 'process_bsdinfo', lambda pid: tuple(facts))
    monkeypatch.setattr(local_peer, '_proc_argv0', lambda *args: pytest.fail('read foreign arguments'))
    assert local_peer.process_command(4321, UID) == (77, facts[2], '')


@pytest.mark.parametrize('field,value', [(0, 4322), (1, 78), (2, UID + 1), (3, UID + 1),
    (4, UID + 1), (5, b'other'), (6, b'other'), (7, 124), (8, 457)])
def test_native_process_row_rejects_changed_incarnation(monkeypatch, field, value):
    before = (4321, 77, UID, UID, UID, b'python3', b'python3', 123, 456)
    after = list(before)
    after[field] = value
    snapshots = iter([before, tuple(after)])
    monkeypatch.setattr(local_peer, 'process_bsdinfo', lambda pid: next(snapshots))
    monkeypatch.setattr(local_peer, '_proc_argv0', lambda *args: 'omlx-server')
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        local_peer.process_command(4321, UID)


def test_desktop_process_uses_native_seam_and_never_ps(monkeypatch):
    monkeypatch.setattr(at, 'process_command', lambda pid, uid: (77, uid, 'omlx-server'))
    monkeypatch.setattr(at, 'inspect_command', lambda argv: pytest.fail('spawned ps'))
    assert at.DesktopOmlx._process(authority(), 4321) == (77, UID, 'omlx-server')


@pytest.mark.parametrize('count', [0, -1, 135, 137, None, False])
def test_native_command_partial_bsd_record_never_reaches_arguments(native_command, count):
    native_command.count = count
    with pytest.raises(AuthRefused, match='^native_inspection_unavailable$'):
        local_peer._proc_argv0(4321, UID)
    assert not any(row[0] == 'sysctl' for row in native_command.calls)


@pytest.mark.parametrize('size', [0, 3, 5, 1024])
def test_native_command_argmax_requires_exact_size(native_command, size):
    native_command.argmax_size = size
    with pytest.raises(AuthRefused, match='^desktop_process_unqualified$'):
        local_peer._proc_argv0(4321, UID)
    assert not native_command.buffers


@pytest.mark.parametrize('result', [None, False, '', 0.0])
def test_native_command_falsey_malformed_native_result_is_not_success(native_command, result):
    native_command.returncode = result
    with pytest.raises(AuthRefused, match='^native_inspection_unavailable$'):
        local_peer._proc_argv0(4321, UID)
    assert not any(native_command.buffers[0].raw)


@pytest.mark.parametrize('error', [OSError('synthetic-private'), AttributeError('synthetic-private'),
                                 RuntimeError('synthetic-private')])
def test_native_command_sanitizes_library_failures(monkeypatch, error):
    def missing(*args, **kwargs):
        raise error
    monkeypatch.setattr(local_peer.ctypes, 'CDLL', missing)
    with pytest.raises(AuthRefused) as caught:
        local_peer._proc_argv0(4321, UID)
    assert caught.value.args == ('native_inspection_unavailable',)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('error', [RuntimeError('synthetic-private'), KeyboardInterrupt()])
def test_native_command_clears_buffer_even_when_parser_raises(native_command, monkeypatch, error):
    def explode(*args):
        raise error
    monkeypatch.setattr(local_peer, '_parse_procargs', explode)
    with pytest.raises(AuthRefused if isinstance(error, Exception) else type(error)):
        local_peer._proc_argv0(4321, UID)
    assert not any(native_command.buffers[0].raw)


def test_native_process_row_sanitizes_unexpected_bsd_error(monkeypatch):
    def explode(pid):
        raise RuntimeError('synthetic-private')
    monkeypatch.setattr(local_peer, 'process_bsdinfo', explode)
    with pytest.raises(AuthRefused) as caught:
        local_peer.process_command(4321, UID)
    assert caught.value.args == ('native_inspection_unavailable',)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('port', [8791, 18791])
def test_other_port_subclasses_keep_the_preexisting_ps_reader(monkeypatch, port):
    class Other(at.DesktopOmlx):
        pass
    instance = object.__new__(Other)
    instance.port, instance.uid = port, UID
    calls = []
    monkeypatch.setattr(at, 'process_command', lambda *args: pytest.fail('changed another mode'))
    monkeypatch.setattr(at, 'inspect_command', lambda argv: calls.append(argv) or f'77 {UID} command with spaces\n'.encode())
    assert instance._process(4321) == (77, UID, 'command with spaces')
    assert calls == [['/bin/ps', '-ww', '-p', '4321', '-o', 'ppid=,uid=,comm=']]


def test_actual_bundled_subclass_retains_inherited_legacy_row_without_initialization(monkeypatch):
    # Passive source module import, NOT test_bundled_attribution's native setup.
    from service.inference.bundled_peer import BundledOmlx
    instance = object.__new__(BundledOmlx)
    instance.uid = UID
    monkeypatch.setattr(at, 'process_command', lambda *args: pytest.fail('native command used in bundled mode'))
    monkeypatch.setattr(at, 'inspect_command', lambda argv: f'77 {UID} ignored bundled command\n'.encode())
    assert instance._process(4321) == (77, UID, 'ignored bundled command')


def test_desktop_native_refusal_never_falls_back_to_ps(monkeypatch):
    def refuse(*args):
        raise AuthRefused('native_inspection_unavailable')
    monkeypatch.setattr(at, 'process_command', refuse)
    monkeypatch.setattr(at, 'inspect_command', lambda argv: pytest.fail('native failure fell back to ps'))
    with pytest.raises(AuthRefused, match='^native_inspection_unavailable$'):
        authority()._process(4321)
