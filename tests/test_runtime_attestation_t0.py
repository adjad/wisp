"""T0-pin: characterization of the desktop oMLX runtime-attestation path.

This file changes no behaviour.  It pins what ``service/inference/
attributed_transport.py`` does today so that the later transport performance
changes (T0-walk: a faster tree walker; T0-spawn: scheduling and
``proc_pidpath``) can be shown to be verdict-for-verdict equivalent.

What is here
------------
* ``LegacyWalk`` - the reference ORACLE: a verbatim copy of
  ``DesktopOmlx._trusted_group`` and ``DesktopOmlx._qualified_tree`` (including
  the inner ``snapshot()``) as of base a7071035.  It is deliberately kept in the
  test tree.  Never delete or edit it while a replacement walker exists; the
  frozen-hash test below fails if its text changes.
* A corpus of synthetic trees, one per input class of the plan, each asserted
  to produce a stated verdict (and refusal reason) from BOTH the production
  code and the oracle.
* 1,200 seeded randomized trees composing those mutations, asserted
  production == oracle.
* Call-shape tests for ``_listener``: the exact argv set and the data-dependency
  order that must survive any rescheduling.  (T0-spawn replaced the test that
  pinned the exact serial order; the serial order is now pinned for the fallback
  path only, and the section at the end of the file proves the overlap, the
  reason order, the absence of leaks and the equivalence to a frozen copy of the
  former serial ``_listener``.)
* Documented blind spots pinned as behaviour.  Their names start with
  ``test_PINNED_BLIND_SPOT_``: if one of them fails because a check became
  stricter, that is a deliberate security change that needs its own review,
  not a test to relax.
* A seam test for the instrumentation points the release benchmark relies on.

T0-walk (the pull request that replaced ``rglob`` with an ``os.scandir`` walk)
changed this file in exactly these ways, and in no other:

* ``test_oracle_is_verbatim_copy_of_current_production_walker`` was retired, as
  its own docstring prescribed.  From then on ``LegacyWalk`` is the only record
  of the old behaviour and ``test_oracle_is_frozen`` keeps it immutable.
* The tests that observed the walk by spying on ``Path.rglob`` for the PRODUCTION
  code now spy on ``os.scandir`` (the oracle is still observed through
  ``rglob``): ``after_first_pass``, the walk-count tests and the vanishing-entry
  test.  Their assertions are unchanged in meaning (two passes, none on a cache
  hit, a mutation placed between the passes, an entry that vanishes after it
  was listed).
* New: the T0-walk differential section at the end of the walker tests
  (same-tree inventory equality, a second set of seeded random trees, extra
  corpus classes, mutation during the first pass, scandir semantics).

T0-path (``proc_pidpath`` instead of ``lsof -d txt``) changed this file only in the ways listed
in the comment block headed "T0-path" near the end of the file, and added the foreign-uid,
reason-order and retry tests there.  The helper itself is tested in ``test_runtime_proc_path.py``.

Everything runs on disposable temporary directories and in-process fakes.  It
never touches /Applications, port 8000, the Keychain or any real process.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import itertools
import os
import pathlib
from pathlib import Path
import grp
import pwd
import random
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
from collections import Counter
from types import SimpleNamespace
from unittest import mock

import pytest

from service.inference import attributed_transport as at
from service.inference import local_peer
from service.inference.inference_errors import ModelLoadError
from service.inference.local_peer import AuthRefused, ManagedOmlx

UID = os.getuid()
GID = os.getgid()
ALT_GIDS = [g for g in os.getgroups() if g != GID]
IS_ROOT = os.geteuid() == 0

OK = ('accepted', None)
REFUSED = ('refused', 'desktop_runtime_unqualified')
CHANGED = ('refused', 'desktop_runtime_changed')

TRUST_ALL = lambda gid: True  # noqa: E731
TRUST_NONE = lambda gid: False  # noqa: E731
TRUST_PRIMARY = lambda gid: gid == GID  # noqa: E731


# --------------------------------------------------------------------------
# The ORACLE.  Methods below are copied byte-for-byte from
# service/inference/attributed_transport.py (DesktopOmlx._trusted_group and
# DesktopOmlx._qualified_tree) at base a7071035.  Do not edit.
# --------------------------------------------------------------------------
class LegacyWalk:
    """Reference implementation of the tree qualification (frozen oracle)."""

    def __init__(self, uid, app_root, trusted_group=None):
        self.uid = uid
        self.app_root = app_root
        self._group_cache = {}
        self._qualified_roots = set()
        if trusted_group is not None:
            self._trusted_group = trusted_group

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
                found = {}
                for path in [root, *sorted(root.rglob('*'))]:
                    if len(found) >= 50000:
                        raise AuthRefused('desktop_runtime_unqualified')
                    info = path.lstat()
                    target = None
                    if stat.S_ISLNK(info.st_mode):
                        qualify(info, allow_link=True)
                        target_path = path.resolve(strict=True)
                        if target_path != root and root not in target_path.parents:
                            raise AuthRefused('desktop_runtime_unqualified')
                        target = str(target_path)
                    else:
                        qualify(info)
                    found[path] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                                   info.st_gid, info.st_size, info.st_mtime_ns,
                                   info.st_ctime_ns, target)
                return found

            inventory = snapshot()
            if snapshot() != inventory:
                raise AuthRefused('desktop_runtime_changed')
            qualified.add(root)
        except AuthRefused:
            raise
        except (OSError, RuntimeError):
            raise AuthRefused('desktop_runtime_unqualified') from None


# Whitespace-normalised text of the two oracle methods; computed once so the
# freeze test and the verbatim test below share one definition.
def _method_text(function):
    return textwrap.dedent(inspect.getsource(function))


ORACLE_FROZEN_SHA256 = 'b81e8d2893064670bc6b8fbf996dc8ec8b0ad4858f0966b4f78853dc325701cc'


def test_oracle_is_frozen():
    """Nobody may edit the oracle to make a replacement walker pass."""
    text = _method_text(LegacyWalk._trusted_group) + _method_text(LegacyWalk._qualified_tree)
    assert hashlib.sha256(text.encode()).hexdigest() == ORACLE_FROZEN_SHA256


# RETIRED in T0-walk: test_oracle_is_verbatim_copy_of_current_production_walker.
# It compared the source text of ``DesktopOmlx._qualified_tree`` with the oracle
# and could only hold while production still contained the old walker.  The
# oracle is now immutable through ``test_oracle_is_frozen`` above, and every
# behavioural claim is carried by the differential tests below.


# --------------------------------------------------------------------------
# Synthetic trees
# --------------------------------------------------------------------------
class Tree:
    """base/oMLX.app/Contents/Resources/Python plus a sibling ``outside``."""

    def __init__(self, base):
        self.base = base
        self.app_root = base / 'oMLX.app'
        self.root = self.app_root / 'Contents/Resources/Python'
        self.outside = base / 'outside'
        self.sealed = []
        self.root.mkdir(parents=True)
        self.outside.mkdir()

    def put(self, rel, mode=0o644, data=b'v'):
        path = rel if isinstance(rel, Path) else self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def mkdir(self, rel, mode=0o755):
        path = rel if isinstance(rel, Path) else self.root / rel
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        return path

    def seal(self, path, mode=0o000):
        os.chmod(path, mode)
        self.sealed.append(path)

    def socket(self, path):
        cwd = os.getcwd()
        os.chdir(path.parent)
        try:
            try:
                listener = socket.socket(socket.AF_UNIX)
                try:
                    listener.bind(path.name)
                finally:
                    listener.close()
            except PermissionError:
                # The simulation-QA child runs under a sandbox that denies creating
                # sockets. A FIFO is the same class of entry for the check under test
                # (neither a regular file, a directory nor a symlink), so the verdict
                # being pinned is unchanged.
                os.mkfifo(path.name)
        finally:
            os.chdir(cwd)

    def destroy(self):
        for path in self.sealed:
            with contextlib.suppress(OSError):
                os.chmod(path, 0o755)
        for dirpath, dirnames, _files in os.walk(self.base):
            for name in dirnames:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(dirpath, name), 0o755)
        os.chmod(self.base, 0o755)
        shutil.rmtree(self.base, ignore_errors=True)


@pytest.fixture
def trees(tmp_path):
    made = []
    counter = itertools.count()
    base = tmp_path.resolve()

    def factory():
        path = base / f't{next(counter)}'
        path.mkdir()
        tree = Tree(path)
        made.append(tree)
        return tree
    yield factory
    for tree in made:
        tree.destroy()


def make_production(tree, trusted, uid_shift=0):
    authority = object.__new__(at.DesktopOmlx)
    authority.uid = UID + uid_shift
    authority.app_root = tree.app_root
    authority._group_cache = {}
    authority._qualified_roots = set()
    authority._trusted_group = trusted
    return authority


def make_oracle(tree, trusted, uid_shift=0):
    return LegacyWalk(UID + uid_shift, tree.app_root, trusted)


def verdict(action):
    try:
        action()
    except AuthRefused as exc:
        return ('refused', str(exc))
    return OK


def _is_root(path, root):
    return isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(root)


@contextlib.contextmanager
def observe_walk(kind, root, on_pass_start=None):
    """Call ``on_pass_start(n)`` as the n-th pass over ``root`` (1-based) is about to begin.

    The oracle starts a pass by calling ``Path.rglob`` on the root, before anything is
    inspected.  The production walker starts one with ``os.lstat(root)`` called directly
    from ``snapshot()`` (``Path.resolve`` of a symlink and the boundary check also lstat
    the root, so the caller is checked).  Both hooks fire before any entry of that pass
    has been inspected, including the root itself.
    ``calls['listings']`` counts ``os.scandir`` calls on the root (production only).
    """
    calls = {'n': 0, 'listings': 0}

    def start():
        calls['n'] += 1
        if on_pass_start is not None:
            on_pass_start(calls['n'])
    if kind == 'oracle':
        original = pathlib.Path.rglob

        def rglob(self, *args, **kwargs):
            if self == root:
                start()
            return original(self, *args, **kwargs)
        with mock.patch.object(pathlib.Path, 'rglob', rglob):
            yield calls
    else:
        original_lstat, original_scandir = os.lstat, os.scandir

        def lstat(path, *args, **kwargs):
            if _is_root(path, root) and sys._getframe(1).f_code.co_name == 'snapshot':
                start()
            return original_lstat(path, *args, **kwargs)

        def scandir(path='.'):
            if _is_root(path, root):
                calls['listings'] += 1
            return original_scandir(path)
        with mock.patch.object(os, 'lstat', lstat), mock.patch.object(os, 'scandir', scandir):
            yield calls


@contextlib.contextmanager
def after_first_pass(root, mutate, kind='oracle'):
    """Run ``mutate`` after the first full walk of ``root``, before the second."""
    if mutate is None:
        yield
        return
    with observe_walk(kind, root, lambda n: mutate() if n == 2 else None):
        yield


def run_both(trees, build, *, trusted=TRUST_ALL, uid_shift=0):
    """Build the same tree twice; run production on one and the oracle on the other."""
    results = []
    for kind, factory in (('production', make_production), ('oracle', make_oracle)):
        tree = trees()
        mutate = build(tree)
        authority = factory(tree, trusted, uid_shift)
        with after_first_pass(tree.root, mutate, kind):
            results.append(verdict(lambda: authority._qualified_tree(tree.root)))
    return results[0], results[1]


def check(trees, build, expected, **kwargs):
    production, oracle = run_both(trees, build, **kwargs)
    assert oracle == expected, f'oracle {oracle} != expected {expected}'
    assert production == expected, f'production {production} != expected {expected}'


def base_content(tree):
    tree.put('module.py')
    tree.put('pkg/__init__.py')
    tree.put('pkg/sub/data.txt')
    tree.put('bin/tool', 0o755)


# --------------------------------------------------------------------------
# Corpus: one stated verdict per input class (production and oracle both)
# --------------------------------------------------------------------------
def _clean(tree):
    base_content(tree)


def _empty(tree):
    pass


def _dotfiles(tree):
    base_content(tree)
    tree.put('.hidden')
    tree.put('.dir/.inner')


def _dot_world_writable(tree):
    base_content(tree)
    tree.put('.dir/.secret', 0o666)


def _file_mode(mode):
    def build(tree):
        base_content(tree)
        tree.put('pkg/sub/w.py', mode)
    return build


def _dir_mode(mode):
    def build(tree):
        base_content(tree)
        tree.put('pkg/sub/inner/f.py')
        tree.mkdir('pkg/sub/inner', mode)
    return build


def _boundary_mode(which, mode):
    def build(tree):
        base_content(tree)
        target = {'root': tree.root, 'resources': tree.root.parent, 'contents': tree.root.parent.parent,
                  'app': tree.app_root, 'app_parent': tree.base}[which]
        os.chmod(target, mode)
    return build


def _group_file(mode):
    def build(tree):
        base_content(tree)
        tree.put('pkg/g.py', mode)
    return build


def _group_dir(tree):
    base_content(tree)
    tree.mkdir('pkg/gdir', 0o775)


def _hardlink_inside(tree):
    base_content(tree)
    os.link(tree.root / 'module.py', tree.root / 'pkg/module-copy.py')


def _hardlink_from_outside(tree):
    base_content(tree)
    outside = tree.put(tree.outside / 'x.py')
    os.link(outside, tree.root / 'pkg/linked.py')


def _hardlink_to_outside(tree):
    base_content(tree)
    os.link(tree.root / 'module.py', tree.outside / 'alias.py')


def _external_symlink_file(tree):
    base_content(tree)
    target = tree.put(tree.outside / 'x.py')
    os.symlink(target, tree.root / 'pkg/ext.py')


def _external_symlink_dir(tree):
    base_content(tree)
    os.symlink(tree.outside, tree.root / 'pkg/ext-dir')


def _relative_escape(tree):
    base_content(tree)
    tree.put(tree.outside / 'x.py')
    os.symlink('../../../../../../../outside/x.py', tree.root / 'pkg/up.py')


def _internal_symlink_file(tree):
    base_content(tree)
    os.symlink(tree.root / 'module.py', tree.root / 'pkg/link.py')


def _internal_symlink_relative(tree):
    base_content(tree)
    os.symlink('../module.py', tree.root / 'pkg/rel.py')


def _internal_symlink_dir(tree):
    base_content(tree)
    os.symlink(tree.root / 'pkg', tree.root / 'pkg-link')


def _internal_symlink_chain(tree):
    base_content(tree)
    os.symlink(tree.root / 'module.py', tree.root / 'a.py')
    os.symlink(tree.root / 'a.py', tree.root / 'b.py')


def _symlink_to_root(tree):
    base_content(tree)
    os.symlink(tree.root, tree.root / 'pkg/self')


def _dangling_symlink(tree):
    base_content(tree)
    os.symlink(tree.root / 'missing.py', tree.root / 'pkg/dangling.py')


def _symlink_loop(tree):
    base_content(tree)
    os.symlink('loop', tree.root / 'loop')


def _fifo(tree):
    base_content(tree)
    os.mkfifo(tree.root / 'pkg/pipe')


def _socket(tree):
    base_content(tree)
    tree.socket(tree.root / 'pkg/sock')


def _root_is_symlink(tree):
    base_content(tree)
    real = tree.root.parent / 'PythonReal'
    tree.root.rename(real)
    os.symlink(real, tree.root)


def _root_missing(tree):
    shutil.rmtree(tree.root)


def _root_is_file(tree):
    shutil.rmtree(tree.root)
    tree.root.write_bytes(b'not a directory')
    os.chmod(tree.root, 0o644)


def _root_outside_app(tree):
    other = tree.outside / 'Python'
    other.mkdir()
    (other / 'm.py').write_bytes(b'v')
    tree.root = other


def _root_is_app_root(tree):
    base_content(tree)
    tree.root = tree.app_root


def _deep(world_writable):
    def build(tree):
        base_content(tree)
        path = tree.root
        for index in range(45):
            path = path / f'd{index:02d}'
        tree.mkdir(path)
        tree.put(path / 'leaf.py', 0o666 if world_writable else 0o644)
    return build


UNICODE_NAMES = ['sp ace', 'ünï', '日本語', 'emoji-😀', 'a\nb', '-dash', ' leading', 'trailing ', 'tab\there']


def _unicode(world_writable):
    def build(tree):
        base_content(tree)
        for index, name in enumerate(UNICODE_NAMES):
            tree.put(f'{name}/{name}.py', 0o666 if world_writable and index == 5 else 0o644)
    return build


CORPUS = [
    pytest.param(_clean, OK, {}, id='clean'),
    pytest.param(_empty, OK, {}, id='clean-root-only'),
    pytest.param(_dotfiles, OK, {}, id='dotfiles-accepted'),
    pytest.param(_dot_world_writable, REFUSED, {}, id='dotfile-world-writable'),
    *[pytest.param(_file_mode(mode), REFUSED, {}, id=f'world-writable-file-{mode:04o}')
      for mode in (0o666, 0o602, 0o646, 0o777)],
    *[pytest.param(_dir_mode(mode), REFUSED, {}, id=f'world-writable-dir-{mode:04o}')
      for mode in (0o777, 0o757, 0o1777)],
    pytest.param(_dir_mode(0o700), OK, {}, id='private-dir-accepted'),
    *[pytest.param(_boundary_mode(which, 0o777), REFUSED, {}, id=f'world-writable-boundary-{which}')
      for which in ('root', 'resources', 'contents', 'app', 'app_parent')],
    pytest.param(_group_file(0o664), OK, {'trusted': TRUST_ALL}, id='group-writable-file-trusted-gid'),
    pytest.param(_group_file(0o664), REFUSED, {'trusted': TRUST_NONE}, id='group-writable-file-untrusted-gid'),
    pytest.param(_group_file(0o620), REFUSED, {'trusted': TRUST_NONE}, id='group-write-only-file-untrusted-gid'),
    pytest.param(_group_dir, OK, {'trusted': TRUST_ALL}, id='group-writable-dir-trusted-gid'),
    pytest.param(_group_dir, REFUSED, {'trusted': TRUST_NONE}, id='group-writable-dir-untrusted-gid'),
    pytest.param(_boundary_mode('contents', 0o775), OK, {'trusted': TRUST_ALL}, id='group-writable-boundary-trusted'),
    pytest.param(_boundary_mode('contents', 0o775), REFUSED, {'trusted': TRUST_NONE},
                 id='group-writable-boundary-untrusted'),
    pytest.param(_boundary_mode('app_parent', 0o775), REFUSED, {'trusted': TRUST_NONE},
                 id='group-writable-app-parent-untrusted'),
    pytest.param(_clean, REFUSED, {'uid_shift': 1}, id='foreign-uid-every-entry',
                 marks=pytest.mark.skipif(IS_ROOT, reason='uid 0 is always trusted')),
    pytest.param(_empty, REFUSED, {'uid_shift': 1}, id='foreign-uid-boundary',
                 marks=pytest.mark.skipif(IS_ROOT, reason='uid 0 is always trusted')),
    pytest.param(_hardlink_inside, REFUSED, {}, id='hardlink-inside'),
    pytest.param(_hardlink_from_outside, REFUSED, {}, id='hardlink-from-outside'),
    pytest.param(_hardlink_to_outside, REFUSED, {}, id='hardlink-to-outside'),
    pytest.param(_external_symlink_file, REFUSED, {}, id='external-symlink-file'),
    pytest.param(_external_symlink_dir, REFUSED, {}, id='external-symlink-dir'),
    pytest.param(_relative_escape, REFUSED, {}, id='relative-symlink-escapes-root'),
    pytest.param(_internal_symlink_file, OK, {}, id='internal-symlink-file'),
    pytest.param(_internal_symlink_relative, OK, {}, id='internal-symlink-relative'),
    pytest.param(_internal_symlink_dir, OK, {}, id='internal-symlink-dir-not-descended'),
    pytest.param(_internal_symlink_chain, OK, {}, id='internal-symlink-chain'),
    pytest.param(_symlink_to_root, OK, {}, id='symlink-to-root-itself'),
    pytest.param(_dangling_symlink, REFUSED, {}, id='dangling-symlink'),
    pytest.param(_symlink_loop, REFUSED, {}, id='symlink-loop'),
    pytest.param(_fifo, REFUSED, {}, id='fifo'),
    pytest.param(_socket, REFUSED, {}, id='unix-socket'),
    pytest.param(_root_is_symlink, REFUSED, {}, id='root-is-symlink'),
    pytest.param(_root_missing, REFUSED, {}, id='root-missing'),
    pytest.param(_root_is_file, REFUSED, {}, id='root-is-file'),
    pytest.param(_root_outside_app, REFUSED, {}, id='root-outside-app-root'),
    pytest.param(_root_is_app_root, REFUSED, {}, id='root-is-app-root'),
    pytest.param(_deep(False), OK, {}, id='deep-nesting-45'),
    pytest.param(_deep(True), REFUSED, {}, id='deep-nesting-world-writable-leaf'),
    pytest.param(_unicode(False), OK, {}, id='unicode-spaces-newline-names'),
    pytest.param(_unicode(True), REFUSED, {}, id='unicode-names-world-writable'),
]


@pytest.mark.parametrize('build,expected,options', CORPUS)
def test_corpus_verdict_is_pinned_for_production_and_oracle(trees, build, expected, options):
    check(trees, build, expected, **options)


def _mixed_gids(tree):
    base_content(tree)
    tree.put('pkg/primary.py', 0o664)
    other = tree.put('pkg/other.py', 0o664)
    os.chown(tree.root / 'pkg/primary.py', -1, GID)
    os.chown(other, -1, ALT_GIDS[0])


@pytest.mark.skipif(not ALT_GIDS, reason='needs a second supplementary group')
def test_group_trust_is_decided_per_gid(trees):
    check(trees, _mixed_gids, OK, trusted=TRUST_ALL)
    check(trees, _mixed_gids, REFUSED, trusted=TRUST_PRIMARY)
    check(trees, _mixed_gids, REFUSED, trusted=lambda gid: gid == ALT_GIDS[0])
    check(trees, _mixed_gids, REFUSED, trusted=TRUST_NONE)


@pytest.mark.skipif(not IS_ROOT, reason='chown to another account needs root')
def test_real_foreign_owner_is_refused(trees):
    def build(tree):
        base_content(tree)
        os.chown(tree.root / 'module.py', 12345, -1)
    check(trees, build, REFUSED)


# --------------------------------------------------------------------------
# Change between the two passes
# --------------------------------------------------------------------------
def _between(setup, mutation):
    def build(tree):
        base_content(tree)
        setup(tree) if setup else None
        return lambda: mutation(tree)
    return build


def _hop_add_plain(tree): tree.put('pkg/new.py')
def _hop_add_world_writable(tree): tree.put('pkg/new.py', 0o666)
def _hop_add_dir(tree): tree.mkdir('pkg/newdir')
def _hop_add_dotfile(tree): tree.put('.late')
def _hop_add_external_link(tree): os.symlink(tree.put(tree.outside / 'x.py'), tree.root / 'pkg/ext.py')
def _hop_add_internal_link(tree): os.symlink(tree.root / 'module.py', tree.root / 'pkg/late-link.py')
def _hop_add_fifo(tree): os.mkfifo(tree.root / 'pkg/pipe')
def _hop_hardlink_inside(tree): os.link(tree.root / 'module.py', tree.root / 'pkg/copy.py')
def _hop_hardlink_outside(tree): os.link(tree.root / 'module.py', tree.outside / 'alias.py')
def _hop_remove_file(tree): os.unlink(tree.root / 'pkg/sub/data.txt')
def _hop_remove_dir(tree): shutil.rmtree(tree.root / 'pkg/sub')
def _hop_chmod_benign(tree): os.chmod(tree.root / 'module.py', 0o600)
def _hop_chmod_world_writable(tree): os.chmod(tree.root / 'module.py', 0o666)


def _hop_chmod_and_revert(tree):
    os.chmod(tree.root / 'module.py', 0o600)
    os.chmod(tree.root / 'module.py', 0o644)


def _hop_touch(tree): os.utime(tree.root / 'module.py', ns=(1_000_000_000, 1_000_000_000))
def _hop_append(tree): (tree.root / 'module.py').write_bytes(b'longer content')


def _hop_replace_same_content(tree):
    path = tree.root / 'module.py'
    data = path.read_bytes()
    os.unlink(path)
    tree.put(path, 0o644, data)


def _hop_rename(tree): os.rename(tree.root / 'module.py', tree.root / 'renamed.py')
def _hop_chgrp(tree): os.chown(tree.root / 'module.py', -1, ALT_GIDS[0])


def _setup_link(tree):
    tree.put('other.py')
    os.symlink(tree.root / 'module.py', tree.root / 'pkg/link.py')


def _hop_retarget_internal(tree):
    os.unlink(tree.root / 'pkg/link.py')
    os.symlink(tree.root / 'other.py', tree.root / 'pkg/link.py')


def _hop_retarget_external(tree):
    os.unlink(tree.root / 'pkg/link.py')
    os.symlink(tree.put(tree.outside / 'x.py'), tree.root / 'pkg/link.py')


def _hop_link_target_removed(tree): os.unlink(tree.root / 'module.py')
def _hop_seal_dir(tree): tree.seal(tree.root / 'pkg/sub')


BETWEEN = [
    pytest.param(None, _hop_add_plain, CHANGED, id='add-file'),
    pytest.param(None, _hop_add_world_writable, REFUSED, id='add-world-writable-file'),
    pytest.param(None, _hop_add_dir, CHANGED, id='add-dir'),
    pytest.param(None, _hop_add_dotfile, CHANGED, id='add-dotfile'),
    pytest.param(None, _hop_add_external_link, REFUSED, id='add-external-symlink'),
    pytest.param(None, _hop_add_internal_link, CHANGED, id='add-internal-symlink'),
    pytest.param(None, _hop_add_fifo, REFUSED, id='add-fifo'),
    pytest.param(None, _hop_hardlink_inside, REFUSED, id='add-hardlink-inside'),
    pytest.param(None, _hop_hardlink_outside, REFUSED, id='add-hardlink-from-outside-the-tree'),
    pytest.param(None, _hop_remove_file, CHANGED, id='remove-file'),
    pytest.param(None, _hop_remove_dir, CHANGED, id='remove-dir'),
    pytest.param(None, _hop_chmod_benign, CHANGED, id='chmod-benign'),
    pytest.param(None, _hop_chmod_world_writable, REFUSED, id='chmod-world-writable'),
    pytest.param(None, _hop_chmod_and_revert, CHANGED, id='chmod-and-revert-seen-through-ctime'),
    pytest.param(None, _hop_touch, CHANGED, id='touch-mtime'),
    pytest.param(None, _hop_append, CHANGED, id='content-grows'),
    pytest.param(None, _hop_replace_same_content, CHANGED, id='replace-with-identical-content'),
    pytest.param(None, _hop_rename, CHANGED, id='rename'),
    pytest.param(None, _hop_chgrp, CHANGED, id='chgrp',
                 marks=pytest.mark.skipif(not ALT_GIDS, reason='needs a second supplementary group')),
    pytest.param(_setup_link, _hop_retarget_internal, CHANGED, id='retarget-symlink-inside'),
    pytest.param(_setup_link, _hop_retarget_external, REFUSED, id='retarget-symlink-outside'),
    pytest.param(_setup_link, _hop_link_target_removed, REFUSED, id='symlink-target-removed'),
    pytest.param(None, _hop_seal_dir, CHANGED, id='seal-dir-after-first-pass',
                 marks=pytest.mark.skipif(IS_ROOT, reason='root reads mode-000 directories')),
]


@pytest.mark.parametrize('setup,mutation,expected', BETWEEN)
def test_change_between_the_two_passes_is_pinned(trees, setup, mutation, expected):
    check(trees, _between(setup, mutation), expected)


def test_every_change_class_is_seen_only_when_a_second_pass_runs(trees):
    """The second pass is what produces 'changed': one more listing of the root per pass."""
    tree = trees()
    base_content(tree)
    authority = make_production(tree, TRUST_ALL)
    with observe_walk('production', tree.root) as calls:
        authority._qualified_tree(tree.root)
    assert calls['n'] == 2 and calls['listings'] == 2


def test_entry_vanishing_between_listing_and_lstat_is_refused(trees):
    def run(kind):
        tree = trees()
        base_content(tree)
        authority = (make_production if kind == 'production' else make_oracle)(tree, TRUST_ALL)
        victim = tree.root / 'pkg/sub/data.txt'
        state = {'done': False}
        if kind == 'oracle':
            original = pathlib.Path.lstat

            def lstat(self, *args, **kwargs):
                if self == tree.root / 'bin/tool' and not state['done']:   # sorts before the victim
                    state['done'] = True
                    os.unlink(victim)
                return original(self, *args, **kwargs)
            with mock.patch.object(pathlib.Path, 'lstat', lstat):
                return verdict(lambda: authority._qualified_tree(tree.root))
        original = os.scandir

        class Listed:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *exc):
                return False

        def scandir(path='.'):
            if _is_root(path, victim.parent) and not state['done']:
                with original(path) as listing:
                    entries = list(listing)
                state['done'] = True
                os.unlink(victim)                         # listed, then gone before it is stat'ed
                return Listed(entries)
            return original(path)
        with mock.patch.object(os, 'scandir', scandir):
            outcome = verdict(lambda: authority._qualified_tree(tree.root))
        assert state['done']
        return outcome
    assert run('production') == run('oracle') == REFUSED


# --------------------------------------------------------------------------
# Entry-count limit: 49,999 / 50,000 / 50,001 entries including the root
# --------------------------------------------------------------------------
def _fill(tree, children):
    """root plus exactly ``children`` entries: 100 directories, the rest files."""
    dirs = []
    for index in range(100):
        path = tree.root / f'd{index:03d}'
        path.mkdir()
        dirs.append(path)
    files = []
    per = -(-(children - 100) // 100)
    for path in dirs:
        for index in range(per):
            if len(files) == children - 100:
                break
            file = path / f'f{index:04d}'
            os.close(os.open(file, os.O_CREAT | os.O_WRONLY, 0o644))
            files.append(file)
    return files


def test_entry_count_limit_counts_the_root_and_refuses_the_50001st(trees):
    """Entries are counted including the root: 49,999 and 50,000 pass, 50,001 is refused."""
    pairs = []
    for factory in (make_production, make_oracle):
        tree = trees()
        pairs.append((factory, tree, _fill(tree, 50000)))
    assert 1 + sum(1 for _ in pairs[0][1].root.rglob('*')) == 50001
    for total, expected in ((50001, REFUSED), (50000, OK), (49999, OK)):
        for factory, tree, files in pairs:
            assert verdict(lambda: factory(tree, TRUST_ALL)._qualified_tree(tree.root)) == expected, (
                factory.__name__, total)
        for _factory, _tree, files in pairs:
            os.unlink(files.pop())


# --------------------------------------------------------------------------
# Seeded randomized trees: production == oracle
# --------------------------------------------------------------------------
NAME_POOL = ['alpha.py', 'beta.txt', '.hidden', 'sp ace', 'ünï', '日本語', 'emoji-😀', 'a\nb', '-dash', 'n' * 40]
RANDOM_SEEDS = range(1000, 2200)       # 1,200 fixed seeds; never derive from time


def _under(path, roots):
    return any(root in path.parents or root == path for root in roots)


def _uid(r):
    return r.randrange(10 ** 6)


def _op_add_external_file_link(t, r, dirs, files):
    os.symlink(t.put(t.outside / f'x{_uid(r)}.py'), r.choice(dirs) / f'ext-file-link-{_uid(r)}')


def _op_add_external_dir_link(t, r, dirs, files):
    os.symlink(t.outside, r.choice(dirs) / f'ext-dir-link-{_uid(r)}')


def _op_add_dangling(t, r, dirs, files):
    os.symlink(t.root / 'nowhere', r.choice(dirs) / f'dangling-{_uid(r)}')


def _op_add_internal_link(t, r, dirs, files):
    os.symlink(r.choice(files), r.choice(dirs) / f'int-link-{_uid(r)}')


def _op_add_internal_dir_link(t, r, dirs, files):
    os.symlink(r.choice(dirs), r.choice(dirs) / f'int-dir-link-{_uid(r)}')


def _op_add_loop(t, r, dirs, files):
    name = f'loop-{_uid(r)}'
    os.symlink(name, r.choice(dirs) / name)


def _op_add_fifo(t, r, dirs, files):
    os.mkfifo(r.choice(dirs) / f'pipe-{_uid(r)}')


def _op_add_socket(t, r, dirs, files):
    t.socket(r.choice(dirs) / f's{_uid(r)}')


def _op_hardlink_inside(t, r, dirs, files):
    os.link(r.choice(files), r.choice(dirs) / f'hard-inside-{_uid(r)}')


def _op_hardlink_outside(t, r, dirs, files):
    os.link(r.choice(files), t.outside / f'hard-outside-{_uid(r)}')


def _op_hardlink_symlink(t, r, dirs, files):
    where = r.choice(dirs)
    name = _uid(r)
    os.symlink(r.choice(files), where / f'sl-{name}')
    os.link(where / f'sl-{name}', where / f'sl2-{name}', follow_symlinks=False)


def _op_world_writable_file(t, r, dirs, files):
    os.chmod(r.choice(files), r.choice((0o666, 0o602, 0o646, 0o777)))


def _op_group_writable_file(t, r, dirs, files):
    os.chmod(r.choice(files), r.choice((0o664, 0o620, 0o660)))


def _op_chgrp(t, r, dirs, files):
    target = r.choice(files)
    if ALT_GIDS:
        with contextlib.suppress(OSError):
            os.chown(target, -1, r.choice(ALT_GIDS))


def _op_special_bits(t, r, dirs, files):
    os.chmod(r.choice(files), r.choice((0o4755, 0o2755, 0o1755)))


def _op_world_writable_dir(t, r, dirs, files):
    os.chmod(r.choice(dirs), r.choice((0o777, 0o1777, 0o757)))


def _op_group_writable_dir(t, r, dirs, files):
    os.chmod(r.choice(dirs), 0o775)


def _op_boundary(t, r, dirs, files):
    target = r.choice((t.root, t.root.parent, t.root.parent.parent, t.app_root, t.base))
    os.chmod(target, r.choice((0o777, 0o775, 0o755)))


def _op_seal(t, r, dirs, files):
    victim = r.choice(dirs[1:]) if len(dirs) > 1 else t.mkdir(t.root / 'sealed-late')
    if _under(victim, t.sealed):
        return
    if not any(victim.iterdir()):
        t.put(victim / 'hidden.py', 0o666)
    t.seal(victim, r.choice((0o000, 0o100)))


# (phase, operation): creators run first, directory modes next, sealing last.
RANDOM_OPS = [(0, _op_add_external_file_link), (0, _op_add_external_dir_link), (0, _op_add_dangling),
              (0, _op_add_internal_link), (0, _op_add_internal_dir_link), (0, _op_add_loop),
              (0, _op_add_fifo), (0, _op_add_socket), (0, _op_hardlink_inside), (0, _op_hardlink_outside),
              (0, _op_hardlink_symlink), (1, _op_world_writable_file), (1, _op_group_writable_file),
              (1, _op_group_writable_file), (1, _op_chgrp), (1, _op_special_bits),
              (2, _op_world_writable_dir), (2, _op_group_writable_dir), (2, _op_boundary), (3, _op_seal)]


def _hop_choices(t, r, target):
    def add(): t.put(t.root / f'late{_uid(r)}.py', r.choice((0o644, 0o666)))
    def remove(): os.unlink(target)
    def chmod(): os.chmod(target, r.choice((0o600, 0o666, 0o644)))
    def touch(): os.utime(target, ns=(2_000_000_000, 2_000_000_000))
    def append(): target.write_bytes(b'grown')
    def rename(): os.rename(target, target.with_name('renamed-' + target.name))
    def replace():
        data = target.read_bytes()
        os.unlink(target)
        t.put(target, 0o644, data)
    def revert():
        os.chmod(target, 0o600)
        os.chmod(target, 0o644)
    def adddir(): t.mkdir(t.root / f'latedir{_uid(r)}')
    def addlink(): os.symlink(t.put(t.outside / f'late{_uid(r)}'), t.root / f'late-ext-link-{_uid(r)}')
    def hardout(): os.link(target, t.outside / 'late-hard')
    return add, remove, chmod, touch, append, rename, replace, revert, adddir, addlink, hardout


def build_random(seed):
    def build(t):
        r = random.Random(seed)
        dirs = [t.root]
        for index in range(r.randint(0, 5)):
            dirs.append(t.mkdir(r.choice(dirs) / f'{r.choice(NAME_POOL)}.d{index}'))
        files = []
        for index in range(r.randint(1, 10)):
            mode = r.choice((0o644, 0o644, 0o600, 0o755, 0o664))
            files.append(t.put(r.choice(dirs) / f'{r.choice(NAME_POOL)}.{index}', mode))
        chosen = [r.choice(RANDOM_OPS) for _ in range(r.choice((0, 0, 1, 1, 2, 3)))]
        for _phase, op in sorted(chosen, key=lambda item: item[0]):
            op(t, r, dirs, files)
        if r.random() < 0.35:
            live = [f for f in files if not _under(f, t.sealed) and f.exists()]
            if live:
                target = r.choice(live)
                return r.choice(_hop_choices(t, r, target))
        return None
    return build


def build_options(seed):
    r = random.Random(f'options-{seed}')
    return {'trusted': r.choice((TRUST_ALL, TRUST_NONE, TRUST_PRIMARY)),
            'uid_shift': 1 if (r.random() < 0.02 and not IS_ROOT) else 0}


def test_randomized_trees_production_equals_oracle(tmp_path):
    base = tmp_path.resolve()
    outcomes = Counter()
    mismatches = []
    for seed in RANDOM_SEEDS:
        made = []

        def factory():
            made.append(Tree(base / f'r{seed}-{len(made)}'))
            return made[-1]
        try:
            production, oracle = run_both(factory, build_random(seed), **build_options(seed))
        finally:
            for tree in made:
                tree.destroy()
        outcomes[production] += 1
        if production != oracle:
            mismatches.append((seed, production, oracle))
    assert not mismatches, mismatches[:10]
    assert sum(outcomes.values()) == len(RANDOM_SEEDS) >= 1000
    # The generator must exercise every verdict class, or equality proves little.
    assert outcomes[OK] >= 80, outcomes
    assert outcomes[REFUSED] >= 150, outcomes
    assert outcomes[CHANGED] >= 5, outcomes
    assert set(outcomes) <= {OK, REFUSED, CHANGED}, outcomes


def test_randomized_corpus_is_deterministic_and_varied(tmp_path):
    """The same seed builds the same tree shape every run; different seeds differ."""
    base = tmp_path.resolve()
    counter = itertools.count()

    def shape(seed):
        tree = Tree(base / f'shape{next(counter)}')
        try:
            build_random(seed)(tree)
            return tuple(sorted(str(p.relative_to(tree.root)) for p in tree.root.rglob('*')))
        finally:
            tree.destroy()
    assert shape(1234) == shape(1234)
    assert len({shape(seed) for seed in range(1000, 1012)}) >= 8


# --------------------------------------------------------------------------
# T0-walk differential: the os.scandir walker against the frozen oracle
#
# What "same" means here.  Both walkers return a dict path -> 9-tuple and compare
# the two passes with ``!=``.  A dict comparison does not depend on insertion
# order, so the ``sorted()`` of the old walker was never part of the verdict.  The
# tests below nevertheless compare the INVENTORIES themselves (every key and every
# tuple), not just the verdicts, on one physical tree, so that equality of the
# keys, of every tuple field and of the symlink targets is checked directly.
# --------------------------------------------------------------------------
def capture_inventories(action):
    """Run ``action``; return (verdict, [every inventory ``snapshot()`` returned]).

    Both the oracle and the production walker name their per-pass function
    ``snapshot``; a profile hook reads its return value, so neither needs a seam.
    """
    captured = []

    def profiler(frame, event, arg):
        if event == 'return' and frame.f_code.co_name == 'snapshot' and isinstance(arg, dict):
            captured.append({os.fspath(key): value for key, value in arg.items()})
    sys.setprofile(profiler)
    try:
        outcome = verdict(action)
    finally:
        sys.setprofile(None)
    return outcome, captured


def on_same_tree(trees, build, *, trusted=TRUST_ALL, uid_shift=0):
    """Build ONE tree; run production and then the oracle on it."""
    tree = trees()
    build(tree)
    results = {}
    for kind, factory in (('production', make_production), ('oracle', make_oracle)):
        authority = factory(tree, trusted, uid_shift)
        results[kind] = capture_inventories(lambda: authority._qualified_tree(tree.root))
    return tree, results


def assert_same_inventory(results):
    production, oracle = results['production'], results['oracle']
    assert production[0] == oracle[0], f'verdict: production {production[0]} != oracle {oracle[0]}'
    assert len(production[1]) == len(oracle[1]), (len(production[1]), len(oracle[1]))
    for index, (new, old) in enumerate(zip(production[1], oracle[1])):
        assert new.keys() == old.keys(), (index, sorted(new.keys() ^ old.keys())[:5])
        assert new == old, (index, [key for key in new if new[key] != old[key]][:5])


@pytest.mark.parametrize('build,expected,options', CORPUS)
def test_walker_inventory_equals_oracle_on_the_whole_corpus(trees, build, expected, options):
    _tree, results = on_same_tree(trees, build, **options)
    assert_same_inventory(results)
    assert results['production'][0] == expected
    if expected == OK:
        assert len(results['production'][1]) == 2                  # two passes, equal
        assert results['production'][1][0] == results['production'][1][1]


def _symlink_to_parent_dir(tree):
    base_content(tree)
    os.symlink('..', tree.root / 'pkg/up')                         # resolves to the root itself


def _symlink_to_root_parent(tree):
    base_content(tree)
    os.symlink('../..', tree.root / 'pkg/up')                      # one level above the root


def _symlink_to_ancestor_of_root(tree):
    base_content(tree)
    os.symlink(tree.app_root, tree.root / 'pkg/up')


def _mutual_symlink_dirs(tree):
    base_content(tree)
    os.symlink('b', tree.root / 'a')
    os.symlink('a', tree.root / 'b')


def _nested_symlinked_dirs(tree):
    base_content(tree)
    os.symlink(tree.root / 'pkg', tree.root / 'one')
    os.symlink(tree.root / 'one', tree.root / 'two')               # a symlink to a symlinked directory


def _symlink_trailing_slash(tree):
    base_content(tree)
    os.symlink('../module.py/', tree.root / 'pkg/slash')


def _symlink_via_outside_and_back(tree):
    base_content(tree)
    tree.put(tree.outside / 'x.py')
    os.symlink('../../../../../outside/../oMLX.app/Contents/Resources/Python/module.py', tree.root / 'pkg/bounce')


def _symlink_through_symlinked_dir(tree):
    base_content(tree)
    os.symlink(tree.root / 'pkg', tree.root / 'pkg-link')
    os.symlink(tree.root / 'pkg-link/sub/data.txt', tree.root / 'through-link')


def _long_names(tree):
    base_content(tree)
    long = 'n' * 252
    tree.put(f'{long}/{long}/{long}.py')
    tree.put('ü' * 127)                                            # 254 bytes of UTF-8


def _long_name_world_writable(tree):
    base_content(tree)
    tree.put(f'{"n" * 252}/{"m" * 252}.py', 0o666)


def _beyond_path_max(tree):
    """A directory chain whose full path exceeds PATH_MAX, built through dir_fd."""
    base_content(tree)
    fd = os.open(tree.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        name = 'p' * 200
        for _ in range(7):                                         # 7 * 201 > 1024 bytes
            os.mkdir(name, 0o755, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def _non_utf8_name(tree):
    base_content(tree)
    try:
        os.close(os.open(os.fsencode(tree.root) + b'/bad-\xff\xfe', os.O_CREAT | os.O_WRONLY, 0o644))
    except OSError:
        pytest.skip('this filesystem rejects non-UTF-8 names')


def _many_siblings(tree):
    base_content(tree)
    for index in range(300):
        tree.put(f'bulk/f{index:04d}.py')


def _root_unreadable(tree):
    base_content(tree)
    tree.seal(tree.root, 0o000)


def _dir_listable_but_not_searchable(tree):
    base_content(tree)
    tree.put('r/child.py')
    tree.seal(tree.root / 'r', 0o400)                              # names can be read, nothing can be stat'ed


def _dir_read_execute_only(tree):
    base_content(tree)
    tree.put('r/child.py')
    tree.seal(tree.root / 'r', 0o500)


def _dir_write_only(tree):
    base_content(tree)
    tree.put('r/child.py')
    tree.seal(tree.root / 'r', 0o200)


def _nested_sealed_dirs(tree):
    base_content(tree)
    tree.put('a/b/c/world-writable.py', 0o666)
    tree.seal(tree.root / 'a/b', 0o000)


def _empty_dirs_everywhere(tree):
    base_content(tree)
    for index in range(20):
        tree.mkdir(f'empty{index}/inner/deeper')


def _names_that_look_like_globs(tree):
    base_content(tree)
    tree.put('**')
    tree.put('*')
    tree.put('[a-z]/x')
    tree.put('?')


NOT_ROOT = pytest.mark.skipif(IS_ROOT, reason='root reads mode-000 directories')

CORPUS_B = [
    pytest.param(_symlink_to_parent_dir, OK, {}, id='symlink-to-parent-dir-not-descended'),
    pytest.param(_symlink_to_root_parent, REFUSED, {}, id='symlink-to-the-roots-parent'),
    pytest.param(_symlink_to_ancestor_of_root, REFUSED, {}, id='symlink-to-ancestor-of-root'),
    pytest.param(_mutual_symlink_dirs, REFUSED, {}, id='mutual-symlink-loop'),
    pytest.param(_nested_symlinked_dirs, OK, {}, id='symlink-to-symlinked-dir'),
    pytest.param(_symlink_trailing_slash, REFUSED, {}, id='symlink-target-trailing-slash'),
    pytest.param(_symlink_via_outside_and_back, OK, {}, id='symlink-leaves-and-returns'),
    pytest.param(_symlink_through_symlinked_dir, OK, {}, id='symlink-through-symlinked-dir'),
    pytest.param(_long_names, OK, {}, id='255-byte-names'),
    pytest.param(_long_name_world_writable, REFUSED, {}, id='255-byte-name-world-writable'),
    pytest.param(_beyond_path_max, REFUSED, {}, id='path-beyond-path-max'),
    pytest.param(_non_utf8_name, OK, {}, id='non-utf8-name'),
    pytest.param(_many_siblings, OK, {}, id='300-siblings'),
    pytest.param(_empty_dirs_everywhere, OK, {}, id='empty-directories'),
    pytest.param(_names_that_look_like_globs, OK, {}, id='names-that-look-like-globs'),
    pytest.param(_root_unreadable, OK, {}, id='root-unreadable-blind-spot', marks=NOT_ROOT),
    pytest.param(_dir_listable_but_not_searchable, REFUSED, {}, id='dir-0400-names-listed-but-not-stat-able',
                 marks=NOT_ROOT),
    pytest.param(_dir_read_execute_only, OK, {}, id='dir-0500'),
    pytest.param(_dir_write_only, OK, {}, id='dir-0200-blind-spot', marks=NOT_ROOT),
    pytest.param(_nested_sealed_dirs, OK, {}, id='nested-sealed-directory-blind-spot', marks=NOT_ROOT),
]


@pytest.mark.parametrize('build,expected,options', CORPUS_B)
def test_extra_corpus_verdict_and_inventory_match_the_oracle(trees, build, expected, options):
    check(trees, build, expected, **options)
    _tree, results = on_same_tree(trees, build, **options)
    assert_same_inventory(results)
    assert results['production'][0] == expected


def test_symlink_inventory_records_the_resolved_target(trees):
    tree, results = on_same_tree(trees, _internal_symlink_relative)
    assert_same_inventory(results)
    first = results['production'][1][0]
    link_key = str(tree.root / 'pkg/rel.py')
    assert first[link_key][8] == str(tree.root / 'module.py')
    assert all(entry[8] is None for key, entry in first.items() if key != link_key)
    root_info = os.lstat(tree.root)
    assert first[str(tree.root)][:2] == (root_info.st_dev, root_info.st_ino)


def test_walker_never_lists_a_symlinked_directory_and_lists_every_real_one_once_per_pass(trees):
    tree = trees()
    base_content(tree)
    os.symlink(tree.root / 'pkg', tree.root / 'pkg-link')
    os.symlink('..', tree.root / 'pkg/up')
    listed = []
    original = os.scandir

    def scandir(path='.'):
        listed.append(os.fspath(path))
        return original(path)
    with mock.patch.object(os, 'scandir', scandir):
        assert verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root)) == OK
    real = [str(tree.root), str(tree.root / 'pkg'), str(tree.root / 'pkg/sub'), str(tree.root / 'bin')]
    assert sorted(listed) == sorted(real * 2)


@NOT_ROOT
def test_scandir_semantics_the_walker_depends_on(trees):
    """The os.scandir facts that stand beside test_rglob_semantics_the_walker_depends_on."""
    tree = trees()
    base_content(tree)
    tree.put('.dot')
    tree.put('real/inner.py')
    os.symlink(tree.root / 'real', tree.root / 'linkdir')
    tree.put('sealed/child.py')
    tree.seal(tree.root / 'sealed')
    with os.scandir(tree.root) as listing:
        entries = {entry.name: entry for entry in listing}
    assert '.dot' in entries                                       # dotfiles are listed
    assert entries['linkdir'].is_dir() and entries['linkdir'].is_symlink()
    assert not entries['linkdir'].is_dir(follow_symlinks=False)    # a symlinked dir is not a directory entry
    assert stat.S_ISLNK(entries['linkdir'].stat(follow_symlinks=False).st_mode)
    assert entries['sealed'].is_dir(follow_symlinks=False)
    with pytest.raises(PermissionError):
        os.scandir(tree.root / 'sealed')                           # the walker must catch exactly this and skip
    assert entries['.dot'].stat(follow_symlinks=False) == os.lstat(tree.root / '.dot')


# Fresh seeds, disjoint from RANDOM_SEEDS (1000..2199), and a richer generator.
RANDOM_SEEDS_B = range(70000, 72400)           # 2,400 fixed seeds; never derive from time
NAME_POOL_B = NAME_POOL + ['x' * 200, 'ü' * 100, 'q' * 248, '.dots', '*', '[z]', 'tab\tname']


def _op_seal_b(t, r, dirs, files):
    victim = r.choice(dirs[1:]) if len(dirs) > 1 else t.mkdir(t.root / 'sealed-late')
    if _under(victim, t.sealed):
        return
    if not any(victim.iterdir()):
        t.put(victim / 'hidden.py', 0o666)
    t.seal(victim, r.choice((0o000, 0o100, 0o200, 0o300, 0o400, 0o500)))


def _op_symlink_to_parent(t, r, dirs, files):
    os.symlink(r.choice(('..', '../..', '.', 'sub/..')), r.choice(dirs) / f'up-{_uid(r)}')


def _op_symlink_chain(t, r, dirs, files):
    where = r.choice(dirs)
    uid = _uid(r)
    os.symlink(r.choice(files), where / f'c1-{uid}')
    os.symlink(where / f'c1-{uid}', where / f'c2-{uid}')


def _op_symlink_dir_chain(t, r, dirs, files):
    where = r.choice(dirs)
    uid = _uid(r)
    os.symlink(r.choice(dirs), where / f'd1-{uid}')
    os.symlink(where / f'd1-{uid}', r.choice(dirs) / f'd2-{uid}')


def _op_empty_tree_of_dirs(t, r, dirs, files):
    t.mkdir(r.choice(dirs) / f'e{_uid(r)}/f/g')


def _op_nonwritable_dir(t, r, dirs, files):
    if len(dirs) > 1:                                              # never the root: later operations create in it
        os.chmod(r.choice(dirs[1:]), r.choice((0o555, 0o755, 0o700, 0o750)))


RANDOM_OPS_B = RANDOM_OPS + [(0, _op_symlink_to_parent), (0, _op_symlink_chain), (0, _op_symlink_dir_chain),
                             (0, _op_empty_tree_of_dirs), (2, _op_nonwritable_dir), (3, _op_seal_b),
                             (3, _op_seal_b)]


def _hop_choices_b(t, r, target, dirs):
    base = _hop_choices(t, r, target)
    live_dirs = [d for d in dirs if not _under(d, t.sealed) and d.is_dir()]

    def seal_dir():
        victim = r.choice(live_dirs)
        if victim != t.root and not _under(victim, t.sealed):
            t.seal(victim, 0o000)

    def unseal_dir():
        for path in list(t.sealed):
            if os.path.lexists(path):
                os.chmod(path, 0o755)
                if (path / 'hidden.py').exists():
                    os.chmod(path / 'hidden.py', r.choice((0o644, 0o666)))
                return

    def add_in_dir():
        t.put(r.choice(live_dirs) / f'late-in-dir{_uid(r)}.py', r.choice((0o644, 0o666)))

    def dir_chmod():
        os.chmod(r.choice(live_dirs), r.choice((0o755, 0o777, 0o700)))
    return base + (seal_dir, unseal_dir, add_in_dir, dir_chmod)


def build_random_b(seed):
    def build(t):
        r = random.Random(f'b-{seed}')
        dirs = [t.root]
        for index in range(r.randint(0, 12)):
            parent = r.choice([d for d in dirs if len(os.fsencode(d)) < 560])
            dirs.append(t.mkdir(parent / f'{r.choice(NAME_POOL_B)}.d{index}'))
        files = []
        for index in range(r.randint(1, 30)):
            mode = r.choice((0o644, 0o644, 0o600, 0o755, 0o664))
            files.append(t.put(r.choice(dirs) / f'{r.choice(NAME_POOL_B)}.{index}', mode))
        chosen = [r.choice(RANDOM_OPS_B) for _ in range(r.choice((0, 0, 1, 1, 2, 3, 4, 6)))]
        for _phase, op in sorted(chosen, key=lambda item: item[0]):
            op(t, r, dirs, files)
        if r.random() < 0.5:
            live = [f for f in files if not _under(f, t.sealed) and f.exists()]
            if live:
                return r.choice(_hop_choices_b(t, r, r.choice(live), dirs))
        return None
    return build


def build_options_b(seed):
    r = random.Random(f'options-b-{seed}')
    return {'trusted': r.choice((TRUST_ALL, TRUST_NONE, TRUST_PRIMARY)),
            'uid_shift': 1 if (r.random() < 0.02 and not IS_ROOT) else 0}


def test_walker_equals_oracle_on_fresh_random_trees_with_changes_between_passes(tmp_path):
    assert not set(RANDOM_SEEDS_B) & set(RANDOM_SEEDS) and len(RANDOM_SEEDS_B) >= 2000
    base = tmp_path.resolve()
    outcomes = Counter()
    mismatches = []
    for seed in RANDOM_SEEDS_B:
        made = []

        def factory():
            made.append(Tree(base / f'b{seed}-{len(made)}'))
            return made[-1]
        try:
            production, oracle = run_both(factory, build_random_b(seed), **build_options_b(seed))
        finally:
            for tree in made:
                tree.destroy()
        outcomes[production] += 1
        if production != oracle:
            mismatches.append((seed, production, oracle))
    assert not mismatches, mismatches[:10]
    assert sum(outcomes.values()) == len(RANDOM_SEEDS_B)
    assert outcomes[OK] >= 150 and outcomes[REFUSED] >= 300 and outcomes[CHANGED] >= 20, outcomes
    assert set(outcomes) <= {OK, REFUSED, CHANGED}, outcomes


def test_walker_inventory_equals_oracle_on_fresh_random_trees(tmp_path):
    """Same physical tree for both walkers, so every tuple (ino, ctime, ...) is comparable."""
    base = tmp_path.resolve()
    outcomes = Counter()
    entries = symlinks = 0
    mismatches = []
    for seed in RANDOM_SEEDS_B:
        tree = Tree(base / f's{seed}')
        try:
            build_random_b(seed)(tree)                              # the 'between passes' hop is not run here
            options = build_options_b(seed)
            results = {}
            for kind, factory in (('production', make_production), ('oracle', make_oracle)):
                authority = factory(tree, options['trusted'], options['uid_shift'])
                results[kind] = capture_inventories(lambda: authority._qualified_tree(tree.root))
            try:
                assert_same_inventory(results)
            except AssertionError as exc:
                mismatches.append((seed, str(exc)[:200]))
            outcomes[results['production'][0]] += 1
            for inventory in results['production'][1][:1]:
                entries += len(inventory)
                symlinks += sum(1 for value in inventory.values() if value[8] is not None)
        finally:
            tree.destroy()
    assert not mismatches, mismatches[:10]
    assert outcomes[OK] >= 150 and outcomes[REFUSED] >= 300, outcomes
    assert entries > 3000 and symlinks >= 50, (entries, symlinks)   # the comparison is not vacuous


def test_entry_limit_in_one_flat_directory_matches_the_oracle(trees):
    """Shape differs from the 100-directory limit test: all 50,000 children in the root itself."""
    tree = trees()
    files = []
    for index in range(50000):
        path = tree.root / f'f{index:05d}'
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o644))
        files.append(path)
    for expected in (REFUSED, OK, OK):                             # 50,001 / 50,000 / 49,999 entries with the root
        for factory in (make_production, make_oracle):
            assert verdict(lambda: factory(tree, TRUST_ALL)._qualified_tree(tree.root)) == expected, (
                factory.__name__, len(files))
        os.unlink(files.pop())


def test_defect_in_the_last_visited_directory_of_a_large_tree_is_refused(trees):
    """The real runtime tree has ~47,000 entries: a defect in the directory the walker reaches
    LAST in a tree that large must still be found by both walkers (guards against a per-walk
    entry cap that stops qualifying late entries)."""
    tree = trees()
    for directory in range(30):                                      # 30 x 1,000 = 30,000 regular files
        folder = tree.root / f'd{directory:02d}'
        folder.mkdir()
        for index in range(1000):
            os.close(os.open(folder / f'f{index:04d}', os.O_CREAT | os.O_WRONLY, 0o644))
    # Learn which directory production lists last on THIS filesystem (listing order is not
    # something to assume), then plant the defect in it.
    listed = []
    original = os.scandir

    def recording(path='.'):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(str(tree.root)):
            listed.append(os.fspath(path))
        return original(path)
    with mock.patch.object(os, 'scandir', recording):
        assert verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root)) == OK
    last_dir = Path(listed[-1])
    assert last_dir != tree.root, listed[-3:]
    target = last_dir / 'late.py'
    os.close(os.open(target, os.O_CREAT | os.O_WRONLY, 0o644))
    os.chmod(target, 0o666)                                          # world-writable
    for factory in (make_production, make_oracle):
        assert verdict(lambda: factory(tree, TRUST_ALL)._qualified_tree(tree.root)) == REFUSED, factory.__name__
    os.chmod(target, 0o644)
    for factory in (make_production, make_oracle):
        assert verdict(lambda: factory(tree, TRUST_ALL)._qualified_tree(tree.root)) == OK, factory.__name__


def test_a_non_oserror_from_the_directory_listing_escapes_and_is_never_accepted(trees):
    """Only OSError is treated as an unreadable directory; anything else must not be swallowed."""
    tree = trees()
    base_content(tree)
    authority = make_production(tree, TRUST_ALL)
    original = os.scandir

    def broken(path='.'):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(str(tree.root)):
            raise ValueError('listing exploded')
        return original(path)
    with mock.patch.object(os, 'scandir', broken):
        with pytest.raises(ValueError):
            authority._qualified_tree(tree.root)


# --------------------------------------------------------------------------
# The tree changing while it is being walked (not only between the passes)
# --------------------------------------------------------------------------
def _mutating_during_a_walk(tree, mutation, trigger):
    """Run production, calling ``mutation`` as the ``trigger``-th directory listing begins."""
    authority = make_production(tree, TRUST_ALL)
    original = os.scandir
    seen = {'n': 0}

    def scandir(path='.'):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(str(tree.root)):
            seen['n'] += 1
            if seen['n'] == trigger:
                mutation(tree)
        return original(path)
    with mock.patch.object(os, 'scandir', scandir):
        return verdict(lambda: authority._qualified_tree(tree.root)), seen['n']


HARMFUL_DURING_WALK = [
    ('world-writable-file-added', lambda t: t.put('pkg/sub/bad.py', 0o666)),
    ('world-writable-file-added-at-root', lambda t: t.put('bad.py', 0o666)),
    ('existing-file-made-world-writable', lambda t: os.chmod(t.root / 'module.py', 0o666)),
    ('hardlink-to-existing-file', lambda t: os.link(t.root / 'bin/tool', t.outside / 'alias')),
    ('external-symlink-added', lambda t: os.symlink(t.put(t.outside / 'x.py'), t.root / 'pkg/ext')),
    ('fifo-added', lambda t: os.mkfifo(t.root / 'pkg/sub/pipe')),
    ('directory-made-world-writable', lambda t: os.chmod(t.root / 'pkg/sub', 0o777)),
]
BENIGN_DURING_WALK = [
    ('file-added', lambda t: t.put('pkg/sub/new.py')),
    ('file-removed', lambda t: os.unlink(t.root / 'pkg/sub/data.txt')),
    ('directory-removed', lambda t: shutil.rmtree(t.root / 'pkg/sub')),
    ('chmod-benign', lambda t: os.chmod(t.root / 'module.py', 0o600)),
    ('rename', lambda t: os.rename(t.root / 'bin/tool', t.root / 'bin/tool2')),
]
DIRECTORIES_PER_PASS = 4        # base_content: root, pkg, pkg/sub, bin


@pytest.mark.parametrize('trigger', range(1, DIRECTORIES_PER_PASS + 1))
@pytest.mark.parametrize('name,mutation', HARMFUL_DURING_WALK, ids=[m[0] for m in HARMFUL_DURING_WALK])
def test_a_harmful_change_made_during_the_first_pass_is_always_refused(trees, name, mutation, trigger):
    """Wherever it lands in pass one, pass two qualifies it.  Never an accept."""
    tree = trees()
    base_content(tree)
    outcome, listings = _mutating_during_a_walk(tree, mutation, trigger)
    assert listings >= trigger
    assert outcome == REFUSED, (name, trigger, outcome)


@pytest.mark.parametrize('trigger', range(1, 2 * DIRECTORIES_PER_PASS + 1))
@pytest.mark.parametrize('name,mutation', BENIGN_DURING_WALK, ids=[m[0] for m in BENIGN_DURING_WALK])
def test_a_benign_change_made_during_a_walk_is_accepted_or_reported_changed(trees, name, mutation, trigger):
    tree = trees()
    base_content(tree)
    outcome, _listings = _mutating_during_a_walk(tree, mutation, trigger)
    if name in ('file-removed', 'directory-removed', 'rename'):
        assert outcome in (OK, CHANGED, REFUSED), (name, trigger, outcome)   # an entry listed, then gone
    else:
        assert outcome in (OK, CHANGED), (name, trigger, outcome)


@pytest.mark.parametrize('name,mutation', HARMFUL_DURING_WALK + BENIGN_DURING_WALK,
                         ids=[m[0] for m in HARMFUL_DURING_WALK + BENIGN_DURING_WALK])
def test_a_change_made_before_the_first_listing_gives_the_oracle_verdict(trees, name, mutation):
    """When the change lands before either walker has listed anything, the verdicts are identical."""
    verdicts = []
    for kind, factory in (('production', make_production), ('oracle', make_oracle)):
        tree = trees()
        base_content(tree)
        authority = factory(tree, TRUST_ALL)
        with observe_walk(kind, tree.root, lambda n: mutation(tree) if n == 1 else None):
            verdicts.append(verdict(lambda: authority._qualified_tree(tree.root)))
    assert verdicts[0] == verdicts[1]


@NOT_ROOT
def test_a_permission_flip_between_the_passes_matches_the_oracle(trees):
    """Invisible while sealed (pass one), visible once opened (pass two)."""
    def opened(child_mode):
        def build(tree):
            base_content(tree)
            tree.put('hole/child.py')
            tree.seal(tree.root / 'hole')

            def mutate():
                os.chmod(tree.root / 'hole', 0o755)
                os.chmod(tree.root / 'hole/child.py', child_mode)
            return mutate
        return build

    def sealed_after_pass_one(tree):
        base_content(tree)
        tree.put('hole/child.py')
        return lambda: tree.seal(tree.root / 'hole')
    check(trees, opened(0o666), REFUSED)
    check(trees, opened(0o644), CHANGED)
    check(trees, sealed_after_pass_one, CHANGED)


def test_threaded_churn_never_leaks_anything_but_a_refusal(trees):
    """Real concurrent mutation: every outcome is a clean verdict, never a stray exception."""
    import threading
    tree = trees()
    base_content(tree)
    for index in range(150):
        tree.put(f'bulk/f{index}.py')
    stop = threading.Event()

    def churn():
        counter = 0
        while not stop.is_set():
            counter += 1
            name = tree.root / f'bulk/churn{counter % 7}'
            try:
                name.write_bytes(b'x')
                os.chmod(name, 0o644)
                os.unlink(name)
                os.chmod(tree.root / 'bulk/f1.py', 0o600 if counter % 2 else 0o644)
            except OSError:
                pass
    worker = threading.Thread(target=churn)
    worker.start()
    try:
        outcomes = Counter(verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root))
                           for _ in range(25))
    finally:
        stop.set()
        worker.join()
    assert set(outcomes) <= {OK, CHANGED, REFUSED}, outcomes
    os.chmod(tree.root / 'bulk/f1.py', 0o666)                      # what is left behind must be found
    assert verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root)) == REFUSED


# --------------------------------------------------------------------------
# Failure handling: exactly the oracle's exception types and reasons
# --------------------------------------------------------------------------
class _Listed:
    """Stands in for the object os.scandir() returns, serving prepared entries."""

    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return iter(self.entries)

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.entries)


class _BrokenEntry:
    def __init__(self, real, error):
        self.real, self.error = real, error
        self.name, self.path = real.name, real.path

    def stat(self, *args, **kwargs):
        raise self.error

    def is_dir(self, *args, **kwargs):
        return self.real.is_dir(*args, **kwargs)

    def is_symlink(self):
        return self.real.is_symlink()

    def is_file(self, *args, **kwargs):
        return self.real.is_file(*args, **kwargs)

    def inode(self):
        return self.real.inode()


def _break_listing_of(tree, directory, error):
    original = os.scandir

    def scandir(path='.'):
        if os.fspath(path) == str(directory):
            with original(path) as listing:
                return _Listed([_BrokenEntry(entry, error) for entry in listing])
        return original(path)
    return mock.patch.object(os, 'scandir', scandir)


@pytest.mark.parametrize('error', [PermissionError(13, 'denied'), FileNotFoundError(2, 'gone'),
                                   OSError(5, 'io'), RuntimeError('loop')])
def test_a_failing_stat_is_refused_with_the_unqualified_reason(trees, error):
    tree = trees()
    base_content(tree)
    with _break_listing_of(tree, tree.root / 'pkg', error):
        assert verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root)) == REFUSED


def _as_foreign_owner(info):
    values = list(info)
    values[4] = info.st_uid + 1
    return os.stat_result(values)


class _ForeignOwnerEntry(_BrokenEntry):
    """Reports the entry as owned by another account (chown needs root, so it is simulated)."""

    def stat(self, *args, **kwargs):
        return _as_foreign_owner(self.real.stat(*args, **kwargs))


@pytest.mark.parametrize('kind', ['production', 'oracle'])
@pytest.mark.parametrize('victim,expected', [('pkg/link.py', REFUSED), ('pkg/sub', REFUSED), ('module.py', REFUSED)])
def test_an_entry_owned_by_another_account_is_refused_even_when_it_is_a_symlink(trees, kind, victim, expected):
    """The uid test runs on symlinks too (the symlink branch of qualify), for both walkers."""
    tree = trees()
    base_content(tree)
    os.symlink(tree.root / 'module.py', tree.root / 'pkg/link.py')
    authority = (make_production if kind == 'production' else make_oracle)(tree, TRUST_ALL)
    target = tree.root / victim
    if kind == 'oracle':
        original = pathlib.Path.lstat

        def lstat(self, *args, **kwargs):
            info = original(self, *args, **kwargs)
            return _as_foreign_owner(info) if self == target else info
        patch = mock.patch.object(pathlib.Path, 'lstat', lstat)
    else:
        original = os.scandir

        def scandir(path='.'):
            if os.fspath(path) == str(target.parent):
                with original(path) as listing:
                    return _Listed([_ForeignOwnerEntry(e, None) if e.path == str(target) else e for e in listing])
            return original(path)
        patch = mock.patch.object(os, 'scandir', scandir)
    with patch:
        assert verdict(lambda: authority._qualified_tree(tree.root)) == expected


def test_a_failing_listing_is_skipped_like_rglob_does(trees):
    """F3: a directory whose listing raises OSError is skipped silently, as rglob does."""
    tree = trees()
    base_content(tree)
    tree.put('pkg/sub/world-writable.py', 0o666)
    original = os.scandir

    def scandir(path='.'):
        if os.fspath(path) == str(tree.root / 'pkg/sub'):
            raise OSError(5, 'listing failed')
        return original(path)
    with mock.patch.object(os, 'scandir', scandir):
        assert verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root)) == OK


def test_an_unexpected_exception_is_not_swallowed_and_is_not_an_accept(trees):
    """Like the oracle, a non-OSError escapes unchanged (CheckedBackend turns it into refused())."""
    tree = trees()
    base_content(tree)
    authority = make_production(tree, TRUST_ALL)
    with _break_listing_of(tree, tree.root / 'pkg', ValueError('boom')):
        with pytest.raises(ValueError):
            authority._qualified_tree(tree.root)
    assert authority._qualified_roots == set()


def test_a_refused_walk_does_not_populate_the_tree_cache(trees):
    tree = trees()
    base_content(tree)
    tree.put('pkg/bad.py', 0o666)
    authority = make_production(tree, TRUST_ALL)
    assert verdict(lambda: authority._qualified_tree(tree.root)) == REFUSED
    assert authority._qualified_roots == set()
    os.chmod(tree.root / 'pkg/bad.py', 0o644)
    assert verdict(lambda: authority._qualified_tree(tree.root)) == OK
    assert authority._qualified_roots == {tree.root}


def test_the_walker_never_holds_more_than_one_directory_listing_open(trees):
    """A deep tree must not exhaust file descriptors: a listing is closed before the next is opened."""
    tree = trees()
    base_content(tree)
    path = tree.root
    for index in range(120):
        path = path / f'd{index}'
        tree.mkdir(path)
    open_now, peak = [0], [0]
    original = os.scandir

    class Counted:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            open_now[0] += 1
            peak[0] = max(peak[0], open_now[0])
            return self.inner.__enter__()

        def __exit__(self, *exc):
            open_now[0] -= 1
            return self.inner.__exit__(*exc)

        def __iter__(self):
            return iter(self.inner)

        def close(self):
            open_now[0] -= 1
            self.inner.close()

    with mock.patch.object(os, 'scandir', lambda p='.': Counted(original(p))):
        assert verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root)) == OK
    assert peak[0] == 1 and open_now[0] == 0


# --------------------------------------------------------------------------
# Interpreter behaviour the walker depends on
# --------------------------------------------------------------------------
@pytest.mark.skipif(IS_ROOT, reason='root reads mode-000 directories')
def test_rglob_semantics_the_walker_depends_on(trees):
    tree = trees()
    base_content(tree)
    tree.put('.dot')
    tree.put('real/inner.py')
    os.symlink(tree.root / 'real', tree.root / 'linkdir')
    tree.put('sealed/child.py')
    tree.seal(tree.root / 'sealed')
    listed = {str(p.relative_to(tree.root)) for p in tree.root.rglob('*')}
    assert '.dot' in listed                                   # dotfiles are yielded
    assert 'linkdir' in listed and 'linkdir/inner.py' not in listed   # symlinked dir: entry, not descended
    assert 'sealed' in listed and 'sealed/child.py' not in listed     # unreadable dir: entry only, no error


# --------------------------------------------------------------------------
# Documented blind spots, pinned as behaviour.  A failure here means a check
# became STRICTER (or looser).  That is a deliberate security change: update
# the test only in a change that is reviewed as such.
# --------------------------------------------------------------------------
@pytest.mark.skipif(IS_ROOT, reason='root reads mode-000 directories')
@pytest.mark.parametrize('mode', [0o000, 0o100, 0o300])
def test_PINNED_BLIND_SPOT_unreadable_directory_contents_are_silently_skipped(trees, mode):
    """Finding F3.  rglob skips a directory it cannot list, so everything beneath it,
    including a world-writable file, a hardlink, a FIFO and an external symlink, is never
    qualified, and the verdict is ACCEPT.  Do not 'fix' this inside a performance change."""
    def build(tree):
        base_content(tree)
        outside = tree.put(tree.outside / 'x.py')
        tree.put('hole/world-writable.py', 0o666)
        tree.put('hole/first.py')
        os.link(tree.root / 'hole/first.py', tree.root / 'hole/hardlink.py')
        os.mkfifo(tree.root / 'hole/pipe')
        os.symlink(outside, tree.root / 'hole/external.py')
        tree.put('hole/deeper/also-bad.py', 0o666)
        tree.seal(tree.root / 'hole', mode)
    check(trees, build, OK)


@pytest.mark.skipif(IS_ROOT, reason='root reads mode-000 directories')
def test_PINNED_BLIND_SPOT_changes_beneath_an_unreadable_directory_are_not_seen_between_passes(trees):
    def build(tree):
        base_content(tree)
        tree.put('hole/child.py')
        tree.seal(tree.root / 'hole')

        def mutate():
            os.chmod(tree.root / 'hole', 0o755)
            os.chmod(tree.root / 'hole/child.py', 0o666)
            os.chmod(tree.root / 'hole', 0o000)
        return mutate
    # The directory's own mode/ctime changed (chmod round trip), so this IS seen as a change,
    # but never as the world-writable file: the reason is 'changed', not 'unqualified'.
    check(trees, build, CHANGED)


def test_PINNED_BLIND_SPOT_nlink_is_not_checked_for_symlinks(trees):
    def build(tree):
        base_content(tree)
        os.symlink(tree.root / 'module.py', tree.root / 'pkg/sl')
        os.link(tree.root / 'pkg/sl', tree.root / 'pkg/sl2', follow_symlinks=False)
        assert os.lstat(tree.root / 'pkg/sl').st_nlink == 2
    check(trees, build, OK)


def test_PINNED_BLIND_SPOT_symlink_chain_through_an_external_hop_is_accepted(trees):
    """Only the fully resolved target is tested for containment, not the hops."""
    def build(tree):
        base_content(tree)
        os.symlink(tree.root / 'module.py', tree.outside / 'back')
        os.symlink(tree.outside / 'back', tree.root / 'pkg/hop')
    check(trees, build, OK)


@pytest.mark.parametrize('mode', [0o4755, 0o2755, 0o1755])
def test_PINNED_BLIND_SPOT_setuid_setgid_and_sticky_bits_are_not_inspected(trees, mode):
    def build(tree):
        base_content(tree)
        tree.put('bin/special', mode)
    check(trees, build, OK)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS ACLs')
def test_PINNED_BLIND_SPOT_extended_acls_are_not_inspected(trees):
    """Mode bits are the only write-permission test; an ACL granting 'everyone' write is invisible."""
    def build(tree):
        base_content(tree)
        target = tree.root / 'module.py'
        try:
            result = subprocess.run(['/bin/chmod', '+a', 'everyone allow write', str(target)],
                                    capture_output=True)
        except PermissionError:
            pytest.skip('the sandbox does not allow running /bin/chmod here')
        if result.returncode:
            pytest.skip('cannot set an ACL here: ' + result.stderr.decode()[:80])
    check(trees, build, OK)


def test_PINNED_BLIND_SPOT_tree_verdict_is_cached_for_the_instance_lifetime(trees):
    tree = trees()
    base_content(tree)
    authority = make_production(tree, TRUST_ALL)
    authority._qualified_tree(tree.root)
    tree.put('pkg/late-world-writable.py', 0o666)
    authority._qualified_tree(tree.root)                      # same instance: cache hit, no walk
    assert verdict(lambda: make_production(tree, TRUST_ALL)._qualified_tree(tree.root)) == REFUSED


def _fake_accounts(monkeypatch, members, primary_members=()):
    group = SimpleNamespace(gr_mem=list(members))
    current = SimpleNamespace(pw_name='current', pw_uid=UID, pw_gid=80)
    accounts = {'current': current,
                'setup': SimpleNamespace(pw_name='_setup', pw_uid=248, pw_gid=248),
                'human': SimpleNamespace(pw_name='human', pw_uid=502, pw_gid=502),
                'daemon': SimpleNamespace(pw_name='daemon', pw_uid=1, pw_gid=1),
                'root': SimpleNamespace(pw_name='root', pw_uid=0, pw_gid=0),
                'bigsys': SimpleNamespace(pw_name='_big', pw_uid=600, pw_gid=600)}
    monkeypatch.setattr(grp, 'getgrgid', lambda _gid: group)
    def primaries():
        return [SimpleNamespace(**{**vars(accounts[name]), 'pw_gid': 80}) for name in primary_members]
    monkeypatch.setattr(pwd, 'getpwall', primaries)
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: accounts[name])
    return group


SCENARIOS = [
    ([], ['current'], True),
    (['current'], [], True),
    (['root'], [], True),
    ([], ['root'], True),
    (['setup'], [], True),
    (['human'], [], False),
    ([], ['human'], False),
    (['daemon'], [], False),
    (['bigsys'], [], False),
    (['current', 'human'], [], False),
    (['current'], ['human'], False),
    (['missing'], [], False),
]


@pytest.mark.parametrize('named,primary,expected', SCENARIOS)
def test_trusted_group_production_equals_oracle(monkeypatch, named, primary, expected):
    _fake_accounts(monkeypatch, named, primary)
    production = object.__new__(at.DesktopOmlx)
    production.uid, production._group_cache = UID, {}
    oracle = LegacyWalk(UID, Path('/x'))
    assert production._trusted_group(80) is expected
    assert oracle._trusted_group(80) is expected


def test_trusted_group_failures_are_untrusted(monkeypatch):
    def missing(_gid):
        raise KeyError(_gid)

    def broken():
        raise OSError

    production = object.__new__(at.DesktopOmlx)
    production.uid, production._group_cache = UID, {}
    monkeypatch.setattr(grp, 'getgrgid', missing)
    assert production._trusted_group(80) is False
    monkeypatch.setattr(grp, 'getgrgid', lambda _gid: SimpleNamespace(gr_mem=[]))
    monkeypatch.setattr(pwd, 'getpwall', broken)
    production._group_cache = {}
    assert production._trusted_group(80) is False


def test_PINNED_BLIND_SPOT_group_verdict_is_cached_per_instance_even_after_membership_changes(monkeypatch):
    group = _fake_accounts(monkeypatch, ['current'])
    first = object.__new__(at.DesktopOmlx)
    first.uid, first._group_cache = UID, {}
    assert first._trusted_group(80) is True
    group.gr_mem.append('human')                              # membership changes afterwards
    assert first._trusted_group(80) is True                   # stale for this instance's lifetime
    fresh = object.__new__(at.DesktopOmlx)
    fresh.uid, fresh._group_cache = UID, {}
    assert fresh._trusted_group(80) is False                  # a new instance re-resolves


def test_group_cache_is_created_lazily_when_the_instance_has_none(monkeypatch):
    _fake_accounts(monkeypatch, ['current'])
    bare = object.__new__(at.DesktopOmlx)
    bare.uid = UID
    assert bare._trusted_group(80) is True and bare._group_cache == {80: True}


# --------------------------------------------------------------------------
# Call shape of DesktopOmlx._listener, on a real synthetic application tree
# with fake process inspection (same style as test_web_response_followup.py)
# --------------------------------------------------------------------------
SERVER_PID, PARENT_PID = 321, 77
LISTEN_ARGV = ('/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:LISTEN', '-Fpufn')
PS_ARGV = {pid: ('/bin/ps', '-ww', '-p', str(pid), '-o', 'ppid=,uid=,comm=') for pid in (SERVER_PID, PARENT_PID)}
# T0-path: the executable path is no longer an `lsof -d txt` spawn.  It is the module-level
# seam `at.process_path(pid)` (libproc proc_pidpath), recorded as the event ('path', pid).
PATH_EVENTS = {pid: ('path', pid) for pid in (SERVER_PID, PARENT_PID)}
OLD_TXT_ARGV = {pid: ('/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-d', 'txt', '-Fn') for pid in (SERVER_PID, PARENT_PID)}
EXPECTED_ARGVS = {LISTEN_ARGV, *PS_ARGV.values()}


# Captured at import, before any fixture can wrap it.
PRISTINE_QUALIFIED_TREE = at.DesktopOmlx._qualified_tree


class Host:
    """A synthetic oMLX application bundle plus fake process inspection."""

    def __init__(self, tmp_path, monkeypatch, name):
        base = tmp_path.resolve() / name
        self.base = base
        self.app_root = base / 'oMLX.app'
        self.app_executable = self.app_root / 'Contents/MacOS/oMLX'
        self.python_root = self.app_root / 'Contents/Resources/Python'
        self.server_entry = self.app_root / 'Contents/Resources/omlx/server.py'
        for path, mode in ((self.app_executable, 0o755), (self.python_root / 'cpython/bin/python3.11', 0o755),
                           (self.python_root / 'lib/module.py', 0o644), (self.server_entry, 0o644),
                           (self.server_entry.parent / 'helper.py', 0o644), (base / 'outside/python3.11', 0o755),
                           (self.python_root / 'lib/other-regular.py', 0o644)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'v')
            os.chmod(path, mode)
        self.events = []
        self.listen = f'p{SERVER_PID}\nu{UID}\nf4\nn127.0.0.1:8000\n'.encode()
        self.listeners = [('127.0.0.1', 8000)]
        self.ps = {str(SERVER_PID): f'{PARENT_PID} {UID} omlx-server\n',
                   str(PARENT_PID): f'1 {UID} {self.app_executable}\n'}
        self.txt = {str(SERVER_PID): str(self.python_root / 'cpython/bin/python3.11'),
                    str(PARENT_PID): str(self.app_executable)}
        self.signature_failures = {}
        self.manifest = base / 'absent-manifest.json'
        host = self

        def traced_tree(instance, root):
            host.events.append(('tree', root))
            return PRISTINE_QUALIFIED_TREE(instance, root)
        monkeypatch.setattr(at, 'inspect_command', self.inspect)
        monkeypatch.setattr(at, 'tcp_listeners', self.tcp_listeners)
        monkeypatch.setattr(at, 'process_path', self.process_path)
        monkeypatch.setattr(at.DesktopOmlx, '_signed_process',
                            lambda instance, pid, identity: host.signed(pid, identity))
        monkeypatch.setattr(at.DesktopOmlx, '_qualified_tree', traced_tree)
        for attribute in ('app_root', 'app_executable', 'python_root', 'server_entry'):
            monkeypatch.setattr(at.DesktopOmlx, attribute, getattr(self, attribute))

    def inspect(self, argv):
        self.events.append(('spawn', tuple(argv)))
        if '-iTCP:8000' in argv:
            return self.listen
        if argv[0] == '/bin/ps':
            return self.ps[argv[3]].encode()
        raise AssertionError(argv)      # T0-path: `lsof -d txt` (or anything else) is never spawned for the executable

    def process_path(self, pid):
        self.events.append(('path', pid))
        return self.txt[str(pid)]

    def tcp_listeners(self):
        self.events.append(('tcp_listeners',))
        return self.listeners

    def signed(self, pid, identity):
        self.events.append(('signed', pid, identity))
        if pid in self.signature_failures:
            raise AuthRefused(self.signature_failures[pid])

    def new(self):
        return at.DesktopOmlx(self.manifest)


@pytest.fixture
def make_host(tmp_path, monkeypatch):
    counter = itertools.count()
    return lambda: Host(tmp_path, monkeypatch, f'host{next(counter)}')


@pytest.fixture
def host(make_host):
    return make_host()


def spawns(host):
    return [event[1] for event in host.events if event[0] == 'spawn']


SEQUENTIAL_SUCCESS = [
    ('spawn', LISTEN_ARGV), ('tcp_listeners',),
    ('spawn', PS_ARGV[SERVER_PID]), PATH_EVENTS[SERVER_PID], ('signed', SERVER_PID, 'python3'),
    ('spawn', PS_ARGV[PARENT_PID]), PATH_EVENTS[PARENT_PID], ('signed', PARENT_PID, 'app.omlx'),
]


def test_listener_issues_exactly_the_reviewed_set_of_spawns(host):
    authority = host.new()
    assert authority._identity[0] == SERVER_PID and authority._identity[2] == PARENT_PID
    assert Counter(spawns(host)) == Counter(EXPECTED_ARGVS)      # three spawns, each exactly once
    assert Counter(e for e in host.events if e[0] == 'path') == Counter(PATH_EVENTS.values())   # T0-path: libproc, twice
    assert [e for e in host.events if e[0] == 'tcp_listeners'] == [('tcp_listeners',)]
    assert sorted(e[1:] for e in host.events if e[0] == 'signed') == [(PARENT_PID, 'app.omlx'), (SERVER_PID, 'python3')]
    assert [e[1] for e in host.events if e[0] == 'tree'] == [host.python_root, host.server_entry.parent]


def test_listener_data_dependencies_hold_in_any_scheduling(host):
    """True data dependencies; any rescheduling must keep every one of them."""
    host.new()
    order = [e for e in host.events if e[0] != 'tree']
    at_ = {event: index for index, event in enumerate(order)}
    listen, ps_server, ps_parent = ('spawn', LISTEN_ARGV), ('spawn', PS_ARGV[SERVER_PID]), ('spawn', PS_ARGV[PARENT_PID])
    txt_server, txt_parent = PATH_EVENTS[SERVER_PID], PATH_EVENTS[PARENT_PID]
    # T0-spawn changed this test: the former lines also pinned orderings that are NOT data
    # dependencies (a step's ps before its own lsof -d txt before its signature check), which
    # the serial code merely happened to have.  Only the real ones are asserted here; the
    # T0-spawn section proves them again with start and end timestamps under random timing.
    assert at_[listen] < at_[ps_server] < at_[ps_parent]          # pid -> ppid -> parent
    assert at_[listen] < at_[txt_server] and at_[listen] < at_[('signed', SERVER_PID, 'python3')]
    assert at_[ps_server] < at_[txt_parent] and at_[ps_server] < at_[('signed', PARENT_PID, 'app.omlx')]


def test_listener_serial_fallback_order_is_exactly_the_former_serial_order(host, monkeypatch):
    """T0-spawn changed this test (it pinned the only possible order before).

    The inspections now overlap, so no order is pinned for the normal path (see the
    T0-spawn section below).  What stays pinned is the order of the serial fallback,
    taken when the private pool cannot accept work: it must be the former serial
    order, spawn for spawn.
    """
    monkeypatch.setattr(at._POOL, 'submit', mock.Mock(side_effect=RuntimeError('cannot schedule new futures')))
    host.new()
    assert host.events == [*SEQUENTIAL_SUCCESS, ('tree', host.python_root), ('tree', host.server_entry.parent)]


def test_binding_repeats_every_spawn_but_walks_no_tree_on_the_same_instance(host):
    authority = host.new()
    walks = []
    original = os.scandir

    def scandir(path='.'):
        walks.append(path)
        return original(path)
    host.events.clear()
    with mock.patch.object(os, 'scandir', scandir):
        assert authority.binding() == SERVER_PID
    assert Counter(spawns(host)) == Counter(EXPECTED_ARGVS)
    assert walks == []                                            # tree cache hit on this instance
    assert [e[1] for e in host.events if e[0] == 'tree'] == [host.python_root, host.server_entry.parent]


def test_every_new_authority_walks_each_tree_twice(host):
    walks = Counter()
    original = os.scandir
    roots = {str(host.python_root): host.python_root, str(host.server_entry.parent): host.server_entry.parent}

    def scandir(path='.'):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in roots:
            walks[roots[os.fspath(path)]] += 1             # one listing of the root per pass
        return original(path)
    with mock.patch.object(os, 'scandir', scandir):
        host.new()
        host.new()
    assert walks == {host.python_root: 4, host.server_entry.parent: 4}


def test_binding_checks_manifest_absence_before_and_after_the_listener(host, monkeypatch):
    authority = host.new()
    log = []
    monkeypatch.setattr(at.DesktopOmlx, '_manifest_absent', lambda self: log.append('manifest'))
    original = at.DesktopOmlx._listener
    monkeypatch.setattr(at.DesktopOmlx, '_listener', lambda self: log.append('listener') or original(self))
    authority.binding()
    assert log == ['manifest', 'listener', 'manifest']
    assert pytest.raises(AuthRefused, authority.binding, SERVER_PID + 1).value.args == ('desktop_listener_changed',)


def _set(**changes):
    def apply(host):
        for key, value in changes.items():
            setattr(host, key, value)
    return apply


def _txt(pid, attribute):
    return lambda h: h.txt.__setitem__(str(pid), str(attribute(h)))


def _writable(attribute, mode):
    return lambda h: os.chmod(attribute(h), mode)


def _plant(attribute, name):
    def apply(host):
        path = attribute(host) / name
        path.write_bytes(b'bad')
        os.chmod(path, 0o666)
    return apply


# (name, apply-fault, reason, events issued before the refusal, in the current serial order)
FAULTS = [
    ('listener-malformed', _set(listen=b'p321\n'), 'desktop_listener_unqualified', 1),
    ('listener-extra-binder', _set(listeners=[('127.0.0.1', 8000), ('0.0.0.0', 8000)]),
     'desktop_listener_unqualified', 2),
    ('listeners-unavailable', _set(listeners=None), 'desktop_listener_unqualified', 2),
    ('server-command', lambda h: h.ps.__setitem__(str(SERVER_PID), f'{PARENT_PID} {UID} python3.11\n'),
     'desktop_process_unqualified', 3),
    ('server-executable-outside', _txt(SERVER_PID, lambda h: h.base / 'outside/python3.11'),
     'desktop_executable_unqualified', 4),
    ('server-signature', lambda h: h.signature_failures.__setitem__(SERVER_PID, 'desktop_signature_unqualified'),
     'desktop_signature_unqualified', 5),
    ('parent-command', lambda h: h.ps.__setitem__(str(PARENT_PID), f'1 {UID} /tmp/oMLX\n'),
     'desktop_parent_unqualified', 6),
    ('parent-not-launchd-child', lambda h: h.ps.__setitem__(str(PARENT_PID), f'2 {UID} {h.app_executable}\n'),
     'desktop_parent_unqualified', 6),
    ('parent-executable', _txt(PARENT_PID, lambda h: h.python_root / 'lib/other-regular.py'),
     'desktop_executable_unqualified', 7),
    ('parent-signature', lambda h: h.signature_failures.__setitem__(PARENT_PID, 'desktop_signature_unavailable'),
     'desktop_signature_unavailable', 8),
    ('server-entry-writable', _writable(lambda h: h.server_entry, 0o664), 'desktop_executable_unqualified', 8),
    ('python-tree', _plant(lambda h: h.python_root / 'lib', 'bad.py'), 'desktop_runtime_unqualified', 9),
    ('entry-tree', _plant(lambda h: h.server_entry.parent, 'bad.py'), 'desktop_runtime_unqualified', 10),
]


@pytest.mark.parametrize('name,apply,reason,issued', FAULTS, ids=[f[0] for f in FAULTS])
def test_each_fault_alone_is_refused_with_its_reason(host, name, apply, reason, issued):
    apply(host)
    with pytest.raises(AuthRefused) as caught:
        host.new()
    assert str(caught.value) == reason
    # T0-spawn changed this assertion: independent steps are started speculatively, so
    # at least the former serial prefix was issued and never anything outside the
    # reviewed set (the exact serial count is pinned by the serial-fallback test below).
    assert len(host.events) >= issued, host.events
    assert set(spawns(host)) <= EXPECTED_ARGVS
    assert len([e for e in host.events if e[0] == 'tree']) <= 2


@pytest.mark.parametrize('name,apply,reason,issued', FAULTS, ids=[f[0] for f in FAULTS])
def test_each_fault_alone_in_the_serial_fallback_issues_exactly_the_former_events(host, monkeypatch, name, apply, reason, issued):
    monkeypatch.setattr(at._POOL, 'submit', mock.Mock(side_effect=RuntimeError('cannot schedule new futures')))
    apply(host)
    with pytest.raises(AuthRefused) as caught:
        host.new()
    assert str(caught.value) == reason
    assert len(host.events) == issued, host.events


def test_simultaneous_faults_report_the_earliest_sequential_reason(make_host):
    """The reason order a rescheduled _listener must reproduce (plan stage 3)."""
    for i, j in itertools.combinations(range(len(FAULTS)), 2):
        host = make_host()
        FAULTS[i][1](host)
        FAULTS[j][1](host)
        with pytest.raises(AuthRefused) as caught:
            host.new()
        assert str(caught.value) == FAULTS[i][2], (FAULTS[i][0], FAULTS[j][0], str(caught.value))


def test_binding_refuses_when_the_qualified_identity_changed_since_construction(host):
    authority = host.new()
    original = host.txt[str(SERVER_PID)]
    host.txt[str(SERVER_PID)] = str(host.python_root / 'lib/module.py')    # another qualifying executable
    with pytest.raises(AuthRefused) as caught:
        authority.binding()
    assert str(caught.value) == 'desktop_listener_changed'
    host.txt[str(SERVER_PID)] = original
    assert authority.binding() == SERVER_PID                              # the original identity is accepted again


def test_runtime_authority_builds_a_new_desktop_authority_for_every_load(host, tmp_path, monkeypatch):
    monkeypatch.setattr(at.Path, 'home', lambda: tmp_path / 'no-home')
    first = at.RuntimeAuthority().load()
    second = at.RuntimeAuthority().load()
    assert type(first) is type(second) is at.DesktopOmlx and first is not second
    assert [e for e in host.events if e[0] == 'tree'].count(('tree', host.python_root)) == 2


def test_desktop_connected_peer_delegates_to_managed_connected_peer(monkeypatch):
    seen = []
    monkeypatch.setattr(ManagedOmlx, 'connected_peer',
                        lambda self, sock, pid, incarnation: seen.append((self, sock, pid, incarnation)) or 'result')
    authority = object.__new__(at.DesktopOmlx)
    assert authority.connected_peer('sock', 7, 'inc') == 'result' and seen == [(authority, 'sock', 7, 'inc')]


# --------------------------------------------------------------------------
# Connection and write discipline (invariants I1, I2, I3, I6)
# --------------------------------------------------------------------------
class FakeStream(at.httpcore.AsyncNetworkStream):
    def __init__(self, sock, log):
        self.sock, self.log = sock, log

    async def read(self, max_bytes, timeout=None):
        return b''

    async def write(self, buffer, timeout=None):
        self.log.append(('wire', bytes(buffer)))

    async def aclose(self):
        self.log.append('closed')

    async def start_tls(self, *args, **kwargs):
        raise AssertionError('no TLS on loopback')

    def get_extra_info(self, info):
        return self.sock if info == 'socket' else None


class FakeAuthority:
    uid = UID

    def __init__(self, log, fail_on=()):
        self.log, self.fail_on, self.calls = log, set(fail_on), 0

    def load(self):
        self.log.append('load')
        return self

    def binding(self, expected=None):
        self.log.append('binding')
        return SERVER_PID

    def connected_peer(self, sock, pid, identity):
        self.calls += 1
        self.log.append('peer')
        if self.calls in self.fail_on:
            raise AuthRefused('connected_peer_unqualified')
        return 'owner'


@pytest.fixture
def wire(monkeypatch):
    log = []
    sock = object()

    async def connect_tcp(self, host, port, **kwargs):
        return FakeStream(sock, log)
    monkeypatch.setattr(at.AutoBackend, 'connect_tcp', connect_tcp)
    monkeypatch.setattr(at, 'process_identity', lambda pid, uid: (pid, uid, 1, 2))
    monkeypatch.setattr(at.time, 'sleep', lambda _seconds: None)
    return log


def test_nothing_is_written_before_verification_and_every_write_is_rechecked(wire):
    async def scenario():
        backend = at.CheckedBackend(FakeAuthority(wire), 8000)
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        assert wire == ['load', 'binding', 'peer']                # connect-time verification, no bytes yet
        await stream.write(b'')                                   # empty write: no check, no bytes
        await stream.write(b'POST header')
        await stream.write(b'body')
    asyncio.run(scenario())
    assert wire == ['load', 'binding', 'peer', 'peer', ('wire', b'POST header'), 'peer', ('wire', b'body')]


def test_a_new_authority_is_loaded_for_every_connection(wire):
    async def scenario():
        backend = at.CheckedBackend(FakeAuthority(wire), 8000)
        await backend.connect_tcp('127.0.0.1', 8000)
        await backend.connect_tcp('127.0.0.1', 8000)
    asyncio.run(scenario())
    assert wire.count('load') == 2 and wire.count('binding') == 2


def test_failed_connect_verification_closes_the_stream_and_sends_nothing(wire):
    async def scenario():
        backend = at.CheckedBackend(FakeAuthority(wire, fail_on={1}), 8000)
        with pytest.raises(ModelLoadError):
            await backend.connect_tcp('127.0.0.1', 8000)
    asyncio.run(scenario())
    assert 'closed' in wire and not any(isinstance(e, tuple) for e in wire)


def test_epoch_change_between_header_and_body_write_sends_only_the_header(wire):
    async def scenario():
        backend = at.CheckedBackend(FakeAuthority(wire), 8000)
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        await stream.write(b'header')
        backend.epoch += 1
        with pytest.raises(ModelLoadError):
            await stream.write(b'body')
    asyncio.run(scenario())
    assert [e for e in wire if isinstance(e, tuple)] == [('wire', b'header')] and wire[-1] == 'closed'
    # The epoch is compared before the (expensive) peer inspection: no third inspection ran.
    assert wire.count('peer') == 2


def test_peer_check_failing_on_the_body_write_sends_only_the_header(wire):
    async def scenario():
        backend = at.CheckedBackend(FakeAuthority(wire, fail_on={3}), 8000)   # connect, header, body
        stream = await backend.connect_tcp('127.0.0.1', 8000)
        await stream.write(b'header')
        with pytest.raises(ModelLoadError):
            await stream.write(b'body')
    asyncio.run(scenario())
    assert [e for e in wire if isinstance(e, tuple)] == [('wire', b'header')]


class ScriptedPeer:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def connected_peer(self, sock, pid, identity):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def transient():
    return AuthRefused('connection_inspection_unavailable')


@pytest.mark.parametrize('outcomes,calls,result', [
    ([transient(), 'ok'], 2, 'ok'),
    ([AuthRefused('native_inspection_unavailable'), transient(), 'ok'], 3, 'ok'),
    ([subprocess.TimeoutExpired('lsof', 10), 'ok'], 2, 'ok'),
])
def test_retry_recovers_only_from_transient_inspection_failures(outcomes, calls, result):
    peer = ScriptedPeer(*outcomes)
    assert at.connected_peer_with_retry(peer, 's', 1, 'i', pause=0) == result and peer.calls == calls


@pytest.mark.parametrize('failure', [
    AuthRefused('connected_peer_unqualified'), AuthRefused('connection_kernel_unqualified'),
    AuthRefused('process_identity_changed'), AuthRefused('desktop_listener_changed'),
    AuthRefused('desktop_signature_unqualified'), AuthRefused('connected_peer_changed')])
def test_retry_never_retries_a_finding_about_the_peer(failure):
    peer = ScriptedPeer(failure, 'ok')
    with pytest.raises(AuthRefused) as caught:
        at.connected_peer_with_retry(peer, 's', 1, 'i', pause=0)
    assert caught.value is failure and peer.calls == 1


@pytest.mark.parametrize('failure', [transient, lambda: subprocess.TimeoutExpired('lsof', 10)])
def test_retry_gives_up_after_three_attempts_with_the_last_failure(failure):
    peer = ScriptedPeer(*[failure() for _ in range(3)], 'never reached')
    with pytest.raises((AuthRefused, subprocess.TimeoutExpired)):
        at.connected_peer_with_retry(peer, 's', 1, 'i', pause=0)
    assert peer.calls == 3


def test_transient_reason_set_is_exactly_the_two_inspection_reasons():
    assert at.TRANSIENT_INSPECTION_REASONS == {'native_inspection_unavailable', 'connection_inspection_unavailable'}


# --------------------------------------------------------------------------
# Subprocess hygiene (invariant I7)
# --------------------------------------------------------------------------
def test_inspect_command_keeps_fixed_environment_timeout_and_output_limits(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return SimpleNamespace(returncode=seen.get('rc', 0), stderr=seen.get('err', b''), stdout=seen.get('out', b'ok'))
    monkeypatch.setattr(subprocess, 'run', run)
    assert at.inspect_command(['/bin/x', 'y']) == b'ok'
    assert seen['argv'] == ['/bin/x', 'y'] and seen['timeout'] == 10
    assert seen['env'] == {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}
    assert seen['stdout'] == subprocess.PIPE and seen['stderr'] == subprocess.PIPE
    seen['err'] = b'\n  '                                          # whitespace-only stderr is tolerated
    assert at.inspect_command(['/bin/x']) == b'ok'
    seen['out'] = b'x' * (1024 * 1024)                            # exactly the cap is accepted
    assert len(at.inspect_command(['/bin/x'])) == 1024 * 1024
    for key, value in (('rc', 1), ('err', b'warning'), ('out', b'x' * (1024 * 1024 + 1))):
        seen.update(rc=0, err=b'', out=b'ok')
        seen[key] = value
        with pytest.raises(AuthRefused) as caught:
            at.inspect_command(['/bin/x'])
        assert str(caught.value) == 'native_inspection_unavailable'


def test_tcp_listeners_keeps_its_own_fixed_argv_environment_and_failure_mapping(monkeypatch):
    seen = {}
    result = SimpleNamespace(returncode=0, stderr=b'', stdout=b'p321\nu501\nf4\nn127.0.0.1:8000\n')

    def run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(subprocess, 'run', run)
    assert local_peer.tcp_listeners() == [('127.0.0.1', 8000)]
    assert seen['argv'] == ['/usr/sbin/lsof', '-nP', '-a', '-iTCP', '-sTCP:LISTEN', '-Fpufn']
    assert seen['env'] == {'PATH': '/usr/bin:/bin:/usr/sbin', 'LC_ALL': 'C'} and seen['timeout'] == 10
    for broken in (SimpleNamespace(returncode=1, stderr=b'', stdout=b''),
                   SimpleNamespace(returncode=0, stderr=b'warn', stdout=b'p1\nu1\nf1\nn127.0.0.1:1\n'),
                   OSError(), subprocess.TimeoutExpired('lsof', 10)):
        result = broken
        assert local_peer.tcp_listeners() is None


# --------------------------------------------------------------------------
# Instrumentation seams (the release benchmark patches exactly these)
# --------------------------------------------------------------------------
SEAMS = [
    ('CredentialTransport.handle_async_request', at.CredentialTransport.handle_async_request, '(self, request)', True),
    ('attributed_transport.inspect_command', at.inspect_command, '(argv)', False),
    ('attributed_transport.tcp_listeners', at.tcp_listeners, '()', False),
    ('RuntimeAuthority.load', at.RuntimeAuthority.load, '(self)', False),
    ('DesktopOmlx.binding', at.DesktopOmlx.binding, '(self, expected_pid=None)', False),
    ('ManagedOmlx.connected_peer', ManagedOmlx.connected_peer, '(self, sock, pid, incarnation)', False),
]
CONTRACT = [
    ('DesktopOmlx.connected_peer', at.DesktopOmlx.connected_peer, '(self, sock, pid, incarnation)'),
    ('DesktopOmlx._listener', at.DesktopOmlx._listener, '(self)'),
    ('DesktopOmlx._process', at.DesktopOmlx._process, '(self, pid)'),
    ('DesktopOmlx._executable', at.DesktopOmlx._executable, '(self, pid)'),
    ('DesktopOmlx._signed_process', at.DesktopOmlx._signed_process, '(self, pid, identity)'),
    ('DesktopOmlx._qualified_tree', at.DesktopOmlx._qualified_tree, '(self, root)'),
    ('DesktopOmlx._manifest_absent', at.DesktopOmlx._manifest_absent, '(self)'),
    ('DesktopOmlx._trusted_group', at.DesktopOmlx._trusted_group, '(self, gid)'),
    ('DesktopOmlx._qualified_file', at.DesktopOmlx._qualified_file,
     '(self, path, *, exact=None, parent=None, strict_permissions=False)'),
    ('connected_peer_with_retry', at.connected_peer_with_retry,
     '(authority, sock, pid, identity, *, attempts=3, pause=0.1)'),
    ('local_peer.process_identity', local_peer.process_identity, '(pid, uid)'),
    ('local_peer.process_path', local_peer.process_path, '(pid)'),
    ('local_peer.tcp_listeners', local_peer.tcp_listeners, '()'),
    ('CheckedBackend.connect_tcp', at.CheckedBackend.connect_tcp, '(self, host, port, **kwargs)'),
    ('CheckedStream.write', at.CheckedStream.write, '(self, buffer, timeout=None)'),
    ('CredentialTransport.invalidate', at.CredentialTransport.invalidate, '(self)'),
]


@pytest.mark.parametrize('name,function,signature,is_async', SEAMS, ids=[s[0] for s in SEAMS])
def test_benchmark_instrumentation_seams_exist_with_current_signatures(name, function, signature, is_async):
    assert str(inspect.signature(function)) == signature
    assert inspect.iscoroutinefunction(function) is is_async


@pytest.mark.parametrize('name,function,signature', CONTRACT, ids=[c[0] for c in CONTRACT])
def test_monkeypatched_contract_names_keep_their_signatures(name, function, signature):
    assert str(inspect.signature(function)) == signature


def test_tcp_listeners_is_looked_up_as_a_module_global_at_call_time(host):
    """Tests and the benchmark replace attributed_transport.tcp_listeners; it must be honoured."""
    assert at.tcp_listeners == host.tcp_listeners             # the fixture patched the module global
    host.new()
    assert ('tcp_listeners',) in host.events


def test_inspect_command_is_looked_up_as_a_module_global_when_the_authority_is_built(host):
    authority = host.new()
    assert authority.prep.run == host.inspect


@pytest.mark.skipif(sys.platform != 'darwin', reason='libproc')
def test_process_identity_returns_the_four_tuple_and_refuses_everything_else():
    identity = local_peer.process_identity(os.getpid(), UID)
    assert isinstance(identity, tuple) and len(identity) == 4
    assert identity[:2] == (os.getpid(), UID) and identity[2] > 0 and 0 <= identity[3] < 1000000
    assert local_peer.process_identity(os.getpid(), UID) == identity
    for pid, uid in ((os.getpid(), UID + 1), (2 ** 30, UID)):
        with pytest.raises(AuthRefused) as caught:
            local_peer.process_identity(pid, uid)
        assert str(caught.value) == 'process_identity_unqualified'


# ==========================================================================
# T0-spawn: the independent inspections of DesktopOmlx._listener overlap
# --------------------------------------------------------------------------
# Scheduling only.  Every fact, every refusal reason and the evaluation order
# stay those of the former serial code, which is kept below as LegacyListener
# and compared with the production method on every scenario.  Everything runs
# on in-process fakes: no socket, no network, no /Applications, no chmod.
# ==========================================================================
import concurrent.futures
import contextvars
import re
import threading
import time
import warnings

ORDER = ['listen', 'tcp', 'ps-server', 'txt-server', 'sig-server', 'ps-parent', 'txt-parent', 'sig-parent']
POSITION = {name: index for index, name in enumerate(ORDER)}
# Where in the former serial order each entry of FAULTS is evaluated (a step's own
# result is evaluated at its integer position, the check on that result just after).
FAULT_POSITION = [0.5, 1.5, 1.5, 2.5, 3.5, 4, 5.5, 5.5, 6.5, 7, 8, 9, 10]
PROCESS_FAULTS = list(range(10))          # the faults that need no change on disk


def step_name(argv):
    if '-iTCP:8000' in argv:
        return 'listen'
    if argv[0] == '/bin/ps':
        return 'ps-server' if argv[3] == str(SERVER_PID) else 'ps-parent'
    raise AssertionError(argv)          # T0-path: the executable path is `at.process_path`, never a spawn


def path_step_name(pid):
    return 'txt-server' if pid == SERVER_PID else 'txt-parent'


class LegacyListener:
    """The ORACLE: DesktopOmlx._listener as of base c6bcfe78, before T0-spawn.

    Verbatim except that the two module globals it reads are spelled ``at.``.
    Never edit it while a scheduler exists; the frozen-hash test fails if it changes.
    """

    def _listener(self):
        raw = at.inspect_command(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000',
                                  '-sTCP:LISTEN', '-Fpufn']).decode('ascii')
        rows = raw.splitlines()
        if (len(rows) != 4 or not re.fullmatch(r'p[1-9][0-9]*', rows[0])
                or rows[1] != 'u' + str(self.uid)
                or not re.fullmatch(r'f[0-9]+', rows[2])
                or rows[3] != 'n127.0.0.1:8000'):
            raise AuthRefused('desktop_listener_unqualified')
        listeners = at.tcp_listeners()
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
        self._qualified_tree(self.python_root)
        self._qualified_tree(self.server_entry.parent)
        return pid, executable, parent_pid, parent_executable


LEGACY_LISTENER_FROZEN_SHA256 = '8fee98d255b35b9a2b74b05c257bed622b90eb8470a64008620298d477065269'


def test_legacy_listener_oracle_is_frozen():
    text = textwrap.dedent(inspect.getsource(LegacyListener._listener))
    assert hashlib.sha256(text.encode()).hexdigest() == LEGACY_LISTENER_FROZEN_SHA256


class StepError(Exception):
    def __init__(self, step):
        super().__init__(step)
        self.step = step


class StepStop(BaseException):
    """A BaseException that is not an Exception (as KeyboardInterrupt is not)."""


def delays_for(profile, seed=0):
    """Per-step sleeps in seconds that shape which inspection finishes first."""
    unit = 0.002
    if profile == 'none':
        return {}
    if profile == 'forward':                                   # later steps are slower
        return {name: unit * POSITION[name] for name in ORDER}
    if profile == 'reverse':                                   # later steps finish first
        return {name: unit * (len(ORDER) - POSITION[name]) for name in ORDER}
    rng = random.Random(f'{profile}-{seed}')
    return {name: rng.random() * 3 * unit for name in ORDER}


class Probe:
    """Wraps one Host's fakes: in-flight accounting, timing log, delays, hooks, injected errors."""

    def __init__(self, host, monkeypatch=None, *, delays=None, hooks=None, errors=None):
        self.host, self.delays, self.hooks, self.errors = host, delays or {}, hooks or {}, errors or {}
        self.lock = threading.Lock()
        self.inflight = self.peak = 0
        self.log = []
        self.threads = set()
        if monkeypatch is not None:
            monkeypatch.setattr(at, 'inspect_command', lambda argv: self.step(step_name(argv), self.host.inspect, argv))
            monkeypatch.setattr(at, 'tcp_listeners', lambda: self.step('tcp', self.host.tcp_listeners))
            monkeypatch.setattr(at, 'process_path', lambda pid: self.step(path_step_name(pid), self.host.process_path, pid))
            monkeypatch.setattr(at.DesktopOmlx, '_signed_process',
                                lambda instance, pid, identity: self.step(
                                    'sig-server' if pid == SERVER_PID else 'sig-parent', self.host.signed, pid, identity))

    def step(self, name, call, *args):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.log.append((name, 'start', time.monotonic_ns()))
            self.threads.add(threading.current_thread().name)
        try:
            if name in self.hooks:
                self.hooks[name]()
            if self.delays.get(name):
                time.sleep(self.delays[name])
            if name in self.errors:
                raise self.errors[name]
            return call(*args)
        finally:
            with self.lock:
                self.inflight -= 1
                self.log.append((name, 'end', time.monotonic_ns()))

    def started(self):
        return [name for name, kind, _ in self.log if kind == 'start']

    def times(self):
        return {(name, kind): stamp for name, kind, stamp in self.log}


def bare():
    """A DesktopOmlx with no construction-time listener run (so _listener can be called directly)."""
    authority = object.__new__(at.DesktopOmlx)
    authority.uid, authority._group_cache, authority._qualified_roots = UID, {}, set()
    authority.prep = SimpleNamespace(run=at.inspect_command)
    return authority


def outcome(call):
    try:
        return ('ok', call(), None)
    except BaseException as error:      # noqa: BLE001 - the verdict includes the exception type
        return (type(error).__name__, error.args, error)


def same_outcome(new, old):
    if new[:2] != old[:2]:
        return False
    if isinstance(old[2], (StepError, StepStop)):
        return new[2] is old[2]                       # the very exception object that was injected
    return new[2] is None or type(new[2]) is type(old[2])


def legacy(authority):
    return LegacyListener._listener(authority)


def current(authority):
    return at.DesktopOmlx._listener(authority)


def thread_names():
    return {t.name for t in threading.enumerate()}


def pool_threads():
    return {name for name in thread_names() if name.startswith('wisp-inspect')}


@pytest.fixture(autouse=True)
def quiet_pool():
    """Every test starts and ends with an idle, empty private pool."""
    yield
    pool = getattr(at, '_POOL', None)
    if pool is not None:
        pool.shutdown()


# -- equivalence ------------------------------------------------------------
def test_listener_result_and_reviewed_calls_equal_the_oracle_without_faults(host):
    new = current(bare())
    new_events = list(host.events)
    host.events.clear()
    old = legacy(bare())
    assert new == old == (SERVER_PID, str(host.python_root / 'cpython/bin/python3.11'), PARENT_PID, str(host.app_executable))
    assert Counter(map(repr, new_events)) == Counter(map(repr, host.events))   # same calls, each exactly once


@pytest.mark.parametrize('name,apply,reason,issued', FAULTS, ids=[f[0] for f in FAULTS])
@pytest.mark.parametrize('profile', ['none', 'forward', 'reverse'])
def test_each_fault_alone_has_the_oracle_verdict_under_every_timing(make_host, monkeypatch, name, apply, reason, issued, profile):
    host = make_host()
    apply(host)
    Probe(host, monkeypatch, delays=delays_for(profile))
    new, old = outcome(lambda: current(bare())), outcome(lambda: legacy(bare()))
    assert old[0] == 'AuthRefused' and old[1] == (reason,)
    assert same_outcome(new, old)


# -- the inspections really overlap -------------------------------------------
def must(event):
    assert event.wait(10), 'the inspections did not overlap'


def test_listener_overlaps_the_independent_inspections(host, monkeypatch):
    first, server, parent = threading.Barrier(2, timeout=10), threading.Barrier(3, timeout=10), threading.Barrier(3, timeout=10)
    parent_started = threading.Event()
    hooks = {
        'listen': first.wait, 'tcp': first.wait,                      # lsof LISTEN || tcp_listeners
        'ps-server': server.wait,                                      # the server's three steps together
        'txt-server': lambda: (server.wait(), must(parent_started)),   # and the server's txt / signature are
        'sig-server': lambda: (server.wait(), must(parent_started)),   # still running when the parent's ps starts
        'ps-parent': lambda: (parent_started.set(), parent.wait()),    # the parent's three steps together
        'txt-parent': parent.wait, 'sig-parent': parent.wait,
    }
    probe = Probe(host, monkeypatch, hooks=hooks)
    assert current(bare())[0] == SERVER_PID
    assert probe.peak >= 3 and probe.inflight == 0
    assert len(probe.started()) == 8


def test_listener_walks_the_trees_last_on_the_calling_thread(host, monkeypatch):
    probe = Probe(host, monkeypatch, delays=delays_for('jitter', 1))
    caller = threading.current_thread().name
    walks = []
    monkeypatch.setattr(at.DesktopOmlx, '_qualified_tree',
                        lambda self, root: walks.append((threading.current_thread().name, probe.inflight)))
    current(bare())
    assert walks == [(caller, 0), (caller, 0)]


@pytest.mark.parametrize('seed', range(12))
def test_listener_keeps_every_data_dependency_in_start_and_end_times(host, monkeypatch, seed):
    probe = Probe(host, monkeypatch, delays=delays_for('jitter', seed))
    current(bare())
    t = probe.times()
    for dependent in ('ps-server', 'txt-server', 'sig-server'):       # the pid is used only after both listener checks
        assert t[(dependent, 'start')] >= t[('listen', 'end')] and t[(dependent, 'start')] >= t[('tcp', 'end')]
    for dependent in ('ps-parent', 'txt-parent', 'sig-parent'):       # the parent pid comes out of the server's ps
        assert t[(dependent, 'start')] >= t[('ps-server', 'end')]
    last_step = max(index for index, event in enumerate(host.events) if event[0] != 'tree')
    assert all(index > last_step for index, event in enumerate(host.events) if event[0] == 'tree')


def test_nothing_is_started_from_data_that_failed_its_own_check(make_host, monkeypatch):
    """A pid is inspected only after the checks on the data it came from passed."""
    allowed = {'listener-malformed': {'listen', 'tcp'}, 'listener-extra-binder': {'listen', 'tcp'},
               'listeners-unavailable': {'listen', 'tcp'}, 'server-command': set(ORDER[:5])}
    for fault in FAULTS:
        if fault[0] not in allowed:
            continue
        for profile in ('forward', 'reverse', 'none'):
            host = make_host()
            fault[1](host)
            probe = Probe(host, monkeypatch, delays=delays_for(profile))
            with pytest.raises(AuthRefused):
                current(bare())
            assert set(probe.started()) <= allowed[fault[0]], (fault[0], profile, probe.started())


def test_every_call_goes_through_the_module_globals_and_the_instance_seams(host, monkeypatch):
    seen = []
    base = Probe(host, monkeypatch)
    real_inspect, real_tcp, real_path = at.inspect_command, at.tcp_listeners, at.process_path
    monkeypatch.setattr(at, 'inspect_command', lambda argv: seen.append(('inspect', step_name(argv))) or real_inspect(argv))
    monkeypatch.setattr(at, 'tcp_listeners', lambda: seen.append(('tcp',)) or real_tcp())
    monkeypatch.setattr(at, 'process_path', lambda pid: seen.append(('process_path', pid)) or real_path(pid))
    for name in ('_process', '_executable'):
        original = getattr(at.DesktopOmlx, name)
        monkeypatch.setattr(at.DesktopOmlx, name, lambda self, pid, original=original, name=name:
                            seen.append((name, pid)) or original(self, pid))
    current(bare())
    assert sorted(seen, key=repr) == sorted([
        ('inspect', 'listen'), ('tcp',), ('_process', SERVER_PID), ('inspect', 'ps-server'), ('_executable', SERVER_PID),
        ('process_path', SERVER_PID), ('_process', PARENT_PID), ('inspect', 'ps-parent'), ('_executable', PARENT_PID),
        ('process_path', PARENT_PID)], key=repr)
    assert base.started().count('sig-server') == base.started().count('sig-parent') == 1


def test_instrumentation_wrappers_installed_over_the_seams_count_every_call_once(host, monkeypatch):
    """The release benchmark wraps these seams with a lock-protected recorder; no call may be lost or doubled."""
    counts, lock = Counter(), threading.Lock()

    def wrap(label, function):
        def wrapper(*args, **kwargs):
            with lock:
                counts[label] += 1
            return function(*args, **kwargs)
        return wrapper
    monkeypatch.setattr(at, 'inspect_command', wrap('inspect', at.inspect_command))
    monkeypatch.setattr(at, 'tcp_listeners', wrap('tcp', at.tcp_listeners))
    monkeypatch.setattr(at, 'process_path', wrap('process_path', at.process_path))
    for _ in range(25):
        current(bare())
    # T0-path: three spawns (the listener lookup and two ps) plus two libproc lookups per run.
    assert counts == {'inspect': 25 * 3, 'tcp': 25, 'process_path': 25 * 2}


def test_context_variables_reach_the_worker_threads(host, monkeypatch):
    marker = contextvars.ContextVar('t0_marker')
    seen = set()
    real_inspect = host.inspect
    monkeypatch.setattr(at, 'inspect_command', lambda argv: seen.add(marker.get('lost')) or real_inspect(argv))
    marker.set('kept')
    current(bare())
    assert seen == {'kept'}


# -- reason order --------------------------------------------------------------
PROFILES = ['none', 'forward', 'reverse', 'jitter-a', 'jitter-b']


@pytest.mark.parametrize('profile', PROFILES)
def test_simultaneous_faults_report_the_earliest_sequential_reason_under_every_timing(make_host, monkeypatch, profile):
    for i, j in itertools.combinations(range(len(FAULTS)), 2):
        host = make_host()
        FAULTS[i][1](host)
        FAULTS[j][1](host)
        Probe(host, monkeypatch, delays=delays_for(profile, i * 31 + j))
        with pytest.raises(AuthRefused) as caught:
            host.new()
        assert str(caught.value) == FAULTS[i][2], (profile, FAULTS[i][0], FAULTS[j][0], str(caught.value))


def test_all_faults_at_once_report_the_first_reason(make_host, monkeypatch):
    for profile in PROFILES:
        host = make_host()
        for fault in FAULTS:
            fault[1](host)
        Probe(host, monkeypatch, delays=delays_for(profile))
        with pytest.raises(AuthRefused) as caught:
            host.new()
        assert str(caught.value) == FAULTS[0][2]


def test_random_fault_subsets_have_the_oracle_verdict(make_host, monkeypatch):
    rng = random.Random(20261004)
    for case in range(120):
        host = make_host()
        for index in rng.sample(range(len(FAULTS)), rng.randint(1, 5)):
            FAULTS[index][1](host)
        Probe(host, monkeypatch, delays=delays_for(rng.choice(PROFILES), case))
        new, old = outcome(lambda: current(bare())), outcome(lambda: legacy(bare()))
        assert same_outcome(new, old), (case, new[:2], old[:2])


def test_an_exception_from_any_step_is_the_one_the_serial_code_raises_first(make_host, monkeypatch):
    """Every step can fail with its own exception (any type, not only AuthRefused); when several fail,
    the earliest in the former serial order surfaces, whichever finished first."""
    for profile in ('forward', 'reverse', 'jitter-a'):
        for size in (1, 2, 3, 8):
            for combo in itertools.combinations(ORDER, size):
                host = make_host()
                errors = {step: StepError(step) for step in combo}
                Probe(host, monkeypatch, delays=delays_for(profile, size), errors=errors)
                with pytest.raises(StepError) as caught:
                    current(bare())
                assert caught.value is errors[min(combo, key=POSITION.get)], (profile, combo)


def test_a_base_exception_from_a_step_is_raised_unchanged_and_nothing_is_left_running(make_host, monkeypatch):
    for step in ORDER:
        host = make_host()
        stop = StepStop(step)
        probe = Probe(host, monkeypatch, delays=delays_for('reverse'), errors={step: stop})
        with pytest.raises(StepStop) as caught:
            current(bare())
        assert caught.value is stop and probe.inflight == 0, step


def test_a_step_exception_and_a_data_fault_follow_one_order(make_host, monkeypatch):
    """A raised error at a step beats a data fault evaluated after it and loses to one evaluated before it."""
    for index, fault in enumerate(FAULTS):
        for step in ORDER:
            if FAULT_POSITION[index] == POSITION[step]:
                continue                                  # the same evaluation point: nothing to order
            for profile in ('forward', 'reverse'):
                host = make_host()
                fault[1](host)
                error = StepError(step)
                Probe(host, monkeypatch, delays=delays_for(profile), errors={step: error})
                with pytest.raises(Exception) as caught:
                    current(bare())
                if POSITION[step] < FAULT_POSITION[index]:
                    assert caught.value is error, (fault[0], step, profile)
                else:
                    assert str(caught.value) == fault[2], (fault[0], step, profile)


def test_a_malformed_listener_row_is_reported_before_a_failing_tcp_listeners(host, monkeypatch):
    host.listen = b'p321\n'
    Probe(host, monkeypatch, delays={'listen': 0.05}, errors={'tcp': StepError('tcp')})
    with pytest.raises(AuthRefused) as caught:
        current(bare())
    assert str(caught.value) == 'desktop_listener_unqualified'


def test_undecodable_listener_output_is_the_unicode_error_the_serial_code_raised(host, monkeypatch):
    host.listen = b'p321\nu501\nf4\nn127.0.0.1:\xff\n'
    Probe(host, monkeypatch)
    new, old = outcome(lambda: current(bare())), outcome(lambda: legacy(bare()))
    assert new[0] == old[0] == 'UnicodeDecodeError' and new[1] == old[1]


# -- nothing outlives the call (I5) --------------------------------------------
def test_a_failing_listener_returns_only_after_every_started_inspection_ended(make_host, monkeypatch):
    for fault in FAULTS:
        host = make_host()
        fault[1](host)
        probe = Probe(host, monkeypatch, delays={name: 0.03 for name in ORDER})
        with pytest.raises(AuthRefused):
            current(bare())
        assert probe.inflight == 0, fault[0]
        assert Counter(name for name, kind, _ in probe.log if kind == 'end') == Counter(probe.started()), fault[0]


def test_an_error_while_waiting_does_not_leave_a_worker_running(host, monkeypatch):
    slow_running = threading.Event()
    probe = Probe(host, monkeypatch, delays={'sig-server': 0.25}, errors={'ps-server': StepError('ps')},
                  hooks={'sig-server': slow_running.set, 'ps-server': lambda: must(slow_running)})
    started = time.monotonic()
    with pytest.raises(StepError):
        current(bare())
    assert probe.inflight == 0 and time.monotonic() - started >= 0.24       # it waited for the slow one


def test_real_child_processes_do_not_outlive_a_failing_or_successful_listener(host, monkeypatch):
    """Every inspection runs a real child process (a short `sh`); when _listener returns or raises, each one
    that was ever started has been waited for (no `ps`: the CI sandbox does not allow it)."""
    children = []

    class Spawner(Probe):
        def step(self, name, call, *args):
            def spawn_then_call(*a):
                child = subprocess.Popen(['/bin/sh', '-c', 'sleep 0.05; sleep 0.001'])
                children.append(child)
                child.communicate()
                return call(*a)
            return super().step(name, spawn_then_call, *args)

    for number, fault in enumerate((None, FAULTS[3], FAULTS[0], FAULTS[1])):
        children.clear()
        fresh = Host(host.base.parent, monkeypatch, f'real-{number}')
        if fault:
            fault[1](fresh)
        Spawner(fresh, monkeypatch, errors={'ps-server': StepError('ps')} if number == 3 else None)
        outcome(lambda: current(bare()))
        assert children and all(child.returncode is not None for child in children), number


# -- the private pool ----------------------------------------------------------
def test_pool_is_private_bounded_and_never_the_loops_default_executor(host, monkeypatch):
    assert at._INSPECTION_WORKERS >= 6                       # one listener never needs more than six at once
    probe = Probe(host, monkeypatch, delays=delays_for('jitter-a', 3))
    with concurrent.futures.ThreadPoolExecutor(40) as callers:
        list(callers.map(lambda _: current(bare()), range(120)))
    assert 1 <= len(pool_threads()) <= at._INSPECTION_WORKERS
    assert probe.peak <= at._INSPECTION_WORKERS
    assert probe.threads and all(name.startswith('wisp-inspect') for name in probe.threads)


def test_shutdown_joins_every_worker_and_the_pool_is_reusable(host, monkeypatch):
    Probe(host, monkeypatch)
    current(bare())
    assert pool_threads()
    at._POOL.shutdown()
    assert not pool_threads()
    at._POOL.shutdown()                                      # idempotent
    assert current(bare())[0] == SERVER_PID                  # lazily recreated


def test_closing_a_transport_shuts_the_pool_down(host, monkeypatch):
    Probe(host, monkeypatch)

    async def scenario():
        transport = at.CredentialTransport('http://127.0.0.1:8000', 'k', managed=False)
        await asyncio.to_thread(lambda: current(bare()))
        assert pool_threads()
        await transport.aclose()
        assert not pool_threads()
    asyncio.run(scenario())


def test_work_in_flight_during_a_shutdown_still_completes(host, monkeypatch):
    gate = threading.Event()
    Probe(host, monkeypatch, hooks={'ps-server': lambda: gate.wait(10)})
    result = []
    runner = threading.Thread(target=lambda: result.append(outcome(lambda: current(bare()))))
    runner.start()
    time.sleep(0.1)
    closer = threading.Thread(target=at._POOL.shutdown)
    closer.start()
    time.sleep(0.1)
    gate.set()
    runner.join(15)
    closer.join(15)
    assert not runner.is_alive() and not closer.is_alive() and result[0][0] == 'ok'


def test_a_pool_that_cannot_schedule_falls_back_to_the_serial_code_with_the_same_verdicts(make_host, monkeypatch):
    for fault in FAULTS:
        host = make_host()
        fault[1](host)
        monkeypatch.setattr(at._POOL, 'submit', mock.Mock(side_effect=RuntimeError('cannot schedule new futures')))
        new, old = outcome(lambda: current(bare())), outcome(lambda: legacy(bare()))
        assert same_outcome(new, old) and new[1] == (fault[2],), fault[0]


def test_a_queued_inspection_still_runs_when_the_pool_is_shut_down_meanwhile(host, monkeypatch):
    """shutdown() finishes queued work (it does not cancel it), so a run in progress is never failed by a close."""
    monkeypatch.setattr(at, '_INSPECTION_WORKERS', 1)
    at._POOL.shutdown()
    gate = threading.Event()
    Probe(host, monkeypatch, hooks={'listen': lambda: gate.wait(10)})
    result = []
    runner = threading.Thread(target=lambda: result.append(outcome(lambda: current(bare()))))
    runner.start()
    time.sleep(0.2)                                  # tcp_listeners is queued behind the blocked lsof
    closer = threading.Thread(target=at._POOL.shutdown)
    closer.start()
    time.sleep(0.2)
    gate.set()
    runner.join(15)
    closer.join(15)
    assert not runner.is_alive() and not closer.is_alive() and result[0][0] == 'ok', result


def test_a_deferred_inspection_runs_its_call_once_however_often_it_is_asked(monkeypatch):
    calls = []
    deferred = at._Deferred(lambda value: calls.append(value) or value, (7,))
    assert deferred.result() == 7 and deferred.result() == 7 and calls == [7]
    failing = at._Deferred(lambda: calls.append('boom') or (_ for _ in ()).throw(StepError('x')), ())
    for _ in range(2):
        with pytest.raises(StepError):
            failing.result()
    assert calls == [7, 'boom']


def test_listener_called_from_inside_a_pool_worker_runs_inline_and_cannot_deadlock(host, monkeypatch):
    monkeypatch.setattr(at, '_INSPECTION_WORKERS', 1)
    at._POOL.shutdown()
    Probe(host, monkeypatch)
    inner = []
    real_tcp = host.tcp_listeners

    def reentrant():
        if not inner:
            inner.append(None)
            inner[0] = current(bare())                        # a worker asks for a nested _listener
        return real_tcp()
    host.tcp_listeners = reentrant
    done = []
    runner = threading.Thread(target=lambda: done.append(outcome(lambda: current(bare()))), daemon=True)
    runner.start()
    runner.join(20)
    assert not runner.is_alive(), 'deadlock: a pool worker waited on its own pool'
    assert done[0][0] == 'ok' and inner[0][0] == SERVER_PID


def test_a_saturated_pool_only_slows_callers_down(host, monkeypatch):
    monkeypatch.setattr(at, '_INSPECTION_WORKERS', 2)
    at._POOL.shutdown()
    probe = Probe(host, monkeypatch, delays=delays_for('jitter-b', 4))
    with concurrent.futures.ThreadPoolExecutor(30) as callers:
        results = list(callers.map(lambda _: outcome(lambda: current(bare())), range(60)))
    assert all(r[0] == 'ok' for r in results) and probe.peak <= 2 and probe.inflight == 0


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='fork')
def test_a_forked_child_does_not_inherit_a_dead_pool(host, monkeypatch):
    Probe(host, monkeypatch)
    current(bare())                                          # the parent now owns live workers
    read, write = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        pid = os.fork()
    if pid == 0:                                             # child: must build its own workers, not hang
        try:
            os.close(read)
            os.write(write, b'1' if current(bare())[0] == SERVER_PID else b'0')
        finally:
            os._exit(0)
    os.close(write)
    deadline, finished = time.monotonic() + 30, None
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            finished = status
            break
        time.sleep(0.05)
    if finished is None:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    answer = os.read(read, 1)
    os.close(read)
    assert finished == 0 and answer == b'1'


def test_the_production_module_has_no_second_process_spawning_path():
    source = inspect.getsource(at)
    assert 'Popen' not in source
    assert source.count('subprocess.run(') == 1               # inspect_command, nowhere else
    assert 'subprocess.run(' in inspect.getsource(at.inspect_command)
    listener = inspect.getsource(at.DesktopOmlx._listener)
    assert 'subprocess' not in listener and 'Popen' not in listener


# -- connected_peer keeps its structure ------------------------------------------
def test_connected_peer_brackets_two_sequential_snapshots_with_the_two_bindings(monkeypatch):
    uid = os.getuid()
    local, remote = ('127.0.0.1', 54321), ('127.0.0.1', 8000)
    raw = (f'p321\nu{uid}\nf4\ntIPv4\nPTCP\nn127.0.0.1:8000->127.0.0.1:54321\nTST=ESTABLISHED\n'
           f'p{os.getpid()}\nu{uid}\nf9\ntIPv4\nPTCP\nn127.0.0.1:54321->127.0.0.1:8000\nTST=ESTABLISHED\n').encode()
    log, lock, active = [], threading.Lock(), {'now': 0, 'peak': 0}

    def traced(label, result):
        def call(*args):
            with lock:
                active['now'] += 1
                active['peak'] = max(active['peak'], active['now'])
                log.append((label, threading.current_thread().name))
            time.sleep(0.005)
            with lock:
                active['now'] -= 1
            return result
        return call
    authority = object.__new__(at.DesktopOmlx)
    authority.uid, authority.port = uid, 8000
    authority.binding = traced('binding', 321)
    authority.prep = SimpleNamespace(run=traced('lsof', raw))
    incarnation = (321, uid, 100, 200)
    monkeypatch.setattr(local_peer, 'process_identity', lambda pid, owner: incarnation)

    class Socket:
        def fileno(self): return 9
        def getsockname(self): return local
        def getpeername(self): return remote
    assert authority.connected_peer(Socket(), 321, incarnation) == (incarnation, 4, (9, local, remote))
    assert [label for label, _ in log] == ['binding', 'lsof', 'lsof', 'binding']      # bracket, two sequential snapshots
    assert active['peak'] == 1 and len({thread for _, thread in log}) == 1             # never overlapped, one thread


# -- stress: many concurrent runs against the serial oracle ----------------------
ACTIVE = contextvars.ContextVar('t0_active_probe')


class Fleet:
    """Routes the module-level fakes to a per-run Probe through a context variable."""

    def __init__(self, base, monkeypatch):
        self.base = base
        monkeypatch.setattr(at, 'inspect_command',
                            lambda argv: ACTIVE.get().step(step_name(argv), ACTIVE.get().host.inspect, argv))
        monkeypatch.setattr(at, 'tcp_listeners', lambda: ACTIVE.get().step('tcp', ACTIVE.get().host.tcp_listeners))
        monkeypatch.setattr(at, 'process_path',
                            lambda pid: ACTIVE.get().step(path_step_name(pid), ACTIVE.get().host.process_path, pid))
        monkeypatch.setattr(at.DesktopOmlx, '_signed_process',
                            lambda instance, pid, identity: ACTIVE.get().step(
                                'sig-server' if pid == SERVER_PID else 'sig-parent', ACTIVE.get().host.signed, pid, identity))

    def clone(self):
        import copy
        host = copy.copy(self.base)
        host.events, host.ps, host.txt = [], dict(self.base.ps), dict(self.base.txt)
        host.signature_failures, host.listeners = {}, list(self.base.listeners)
        return host


def run_case(fleet, case):
    faults, errors, profile, seed = case
    probes, results = [], []
    for runner in (current, legacy):
        host = fleet.clone()
        for index in faults:
            FAULTS[index][1](host)
        probe = Probe(host, delays=delays_for(profile, seed), errors=dict(errors))
        probes.append(probe)
        token = ACTIVE.set(probe)
        try:
            results.append(outcome(lambda: runner(bare())))
        finally:
            ACTIVE.reset(token)
    return results, probes


def test_stress_concurrent_listeners_equal_the_serial_oracle_without_leaks(make_host, monkeypatch):
    base = make_host()
    fleet = Fleet(base, monkeypatch)
    rng = random.Random(1004)
    cases = []
    for number in range(200):
        faults = rng.sample(PROCESS_FAULTS, rng.choice([0, 0, 1, 1, 2, 3]))
        errors = {step: StepError(step) for step in rng.sample(ORDER, rng.choice([0, 0, 0, 1, 2]))}
        cases.append((faults, errors, rng.choice(PROFILES), number))
    before = thread_names()
    with concurrent.futures.ThreadPoolExecutor(32) as callers:
        outcomes = list(callers.map(lambda case: run_case(fleet, case), cases))
    for case, ((new, old), (new_probe, old_probe)) in zip(cases, outcomes):
        assert same_outcome(new, old), (case[:2], new[:2], old[:2])
        assert new_probe.inflight == 0 and old_probe.inflight == 0
        new_steps, old_steps = Counter(new_probe.started()), Counter(old_probe.started())
        assert all(count == 1 for count in new_steps.values())            # nothing issued twice
        assert not old_steps - new_steps                                   # at least what the serial code issued
    assert len(pool_threads()) <= at._INSPECTION_WORKERS
    at._POOL.shutdown()
    assert thread_names() <= before                                        # no thread outlived the runs


# ==========================================================================
# T0-path: the executable path comes from libproc (proc_pidpath), not from lsof -d txt
# --------------------------------------------------------------------------
# What changed in this file, and why (nothing else was edited):
#   * TXT_ARGV became OLD_TXT_ARGV (kept only to assert it is never issued); EXPECTED_ARGVS
#     lost the two `lsof -d txt` argvs (five spawns became three).
#   * Host: `inspect` no longer answers `lsof -d txt` (it raises); new `Host.process_path` is
#     installed as the module seam `at.process_path` and records ('path', pid).  The
#     per-pid answer is still `host.txt`, so every FAULTS entry and the `_txt` helper are unchanged.
#   * SEQUENTIAL_SUCCESS, the data-dependency test and the spawn-set test: ('spawn', TXT_ARGV[pid])
#     events became PATH_EVENTS[pid].
#   * step_name no longer names a txt step (it raises); Probe and Fleet route `at.process_path`
#     through the same 'txt-server' / 'txt-parent' steps, so every timing, hook and reason-order
#     test still covers that step.
#   * The module-global seam test lists `process_path` and the instrumentation-count test expects
#     3 spawns + 2 libproc lookups per run (it was 5 spawns).
# LegacyListener (the frozen serial oracle) is untouched: it calls ``self._executable``.  The oracle
# for the executable lookup itself is LegacyExecutable in test_runtime_proc_path.py.
# ==========================================================================
from tests.test_runtime_proc_path import LegacyExecutable


def test_the_old_lsof_txt_spawn_is_no_longer_issued(make_host):
    for fault in [None] + [FAULTS[i] for i in range(len(FAULTS))]:
        host = make_host()
        if fault:
            fault[1](host)
        try:
            host.new()
        except AuthRefused:
            pass
        issued = spawns(host)
        assert not set(issued) & set(OLD_TXT_ARGV.values())
        assert not any('-d' in argv or 'txt' in argv for argv in issued), issued
        assert set(issued) <= EXPECTED_ARGVS


def test_the_executable_lookup_is_issued_once_per_pid_through_the_seam(host):
    host.new()
    assert sorted(e for e in host.events if e[0] == 'path') == sorted(PATH_EVENTS.values())


def lsof_txt_exits_one(host):
    """What the old `lsof -d txt` did: exit 1 (so refuse) for pid 1, which a non-root user may not inspect,
    and print the fake pids' executables as the former Host fake did."""
    real = host.inspect

    def inspect(argv):
        if '-d' in argv:
            if argv[4] == '1':
                raise AuthRefused('native_inspection_unavailable')
            return f'p{argv[4]}\nftxt\nn{host.txt[argv[4]]}\n'.encode()
        return real(argv)
    return inspect


def legacy_executable_authority(host, monkeypatch):
    """A bare authority whose `_executable` is the frozen lsof oracle (lsof exiting 1 for pid 1)."""
    monkeypatch.setattr(at, 'inspect_command', lsof_txt_exits_one(host))
    authority = bare()
    authority._executable = lambda pid: LegacyExecutable._executable(authority, pid)
    return authority


def real_process_path(host):
    """The REAL libproc helper for the one real pid in play (1); the fake pids keep their fake answers."""
    def lookup(pid):
        host.events.append(('path', pid))
        return local_peer.process_path(pid) if pid == 1 else host.txt[str(pid)]
    return lookup


@pytest.mark.skipif(sys.platform != 'darwin', reason='libproc')
@pytest.mark.parametrize('profile', ['none', 'forward', 'reverse', 'jitter-a'])
def test_a_foreign_uid_server_pid_1_is_refused_by_the_ps_uid_check_under_the_scheduler(make_host, monkeypatch, profile):
    """proc_pidpath answers for root's pid 1 where lsof exited 1; the verdict must not depend on it."""
    host = make_host()
    host.listen = f'p1\nu{UID}\nf4\nn127.0.0.1:8000\n'.encode()
    host.ps['1'] = f'{PARENT_PID} 0 omlx-server\n'                   # root's process: uid 0
    host.process_path = real_process_path(host)
    Probe(host, monkeypatch, delays=delays_for(profile, 5))
    new = outcome(lambda: current(bare()))
    assert ('path', 1) in host.events                                 # the helper really ran, speculatively...
    assert new[:2] == ('AuthRefused', ('desktop_process_unqualified',))      # ...and was not the gate
    old_host = make_host()
    old_host.listen, old_host.ps['1'] = host.listen, host.ps['1']
    old = outcome(lambda: legacy(legacy_executable_authority(old_host, monkeypatch)))
    assert same_outcome(new, old)


@pytest.mark.skipif(sys.platform != 'darwin', reason='libproc')
@pytest.mark.parametrize('profile', ['none', 'forward', 'reverse', 'jitter-a'])
def test_a_foreign_uid_parent_pid_1_is_refused_by_the_ps_uid_check_under_the_scheduler(make_host, monkeypatch, profile):
    host = make_host()
    host.ps[str(SERVER_PID)] = f'1 {UID} omlx-server\n'               # the server's parent is pid 1
    host.ps['1'] = f'1 0 {host.app_executable}\n'                      # root's launchd-like row: uid 0
    host.process_path = real_process_path(host)
    Probe(host, monkeypatch, delays=delays_for(profile, 6))
    new = outcome(lambda: current(bare()))
    assert ('path', 1) in host.events
    assert new[:2] == ('AuthRefused', ('desktop_parent_unqualified',))
    old_host = make_host()
    old_host.ps[str(SERVER_PID)], old_host.ps['1'] = host.ps[str(SERVER_PID)], host.ps['1']
    old = outcome(lambda: legacy(legacy_executable_authority(old_host, monkeypatch)))
    assert same_outcome(new, old)


@pytest.mark.skipif(sys.platform != 'darwin', reason='libproc')
def test_the_helper_is_not_the_only_gate_even_if_ps_were_to_lie_about_the_uid(make_host, monkeypatch):
    """Second and third lines of defence that already existed: the path must lie under the app and the
    file must be a regular, correctly owned, non-writable one.  pid 1 is /sbin/launchd, not oMLX."""
    host = make_host()
    host.listen = f'p1\nu{UID}\nf4\nn127.0.0.1:8000\n'.encode()
    host.ps['1'] = f'{PARENT_PID} {UID} omlx-server\n'               # a lying ps row: our uid
    host.process_path = real_process_path(host)
    Probe(host, monkeypatch)
    with pytest.raises(AuthRefused) as caught:
        current(bare())
    assert str(caught.value) == 'desktop_executable_unqualified'


@pytest.mark.parametrize('reason', ['desktop_executable_unqualified', 'native_inspection_unavailable'])
@pytest.mark.parametrize('which', ['txt-server', 'txt-parent'])
@pytest.mark.parametrize('profile', ['none', 'forward', 'reverse', 'jitter-b'])
def test_a_path_lookup_refusal_is_ordered_like_the_serial_code(make_host, monkeypatch, reason, which, profile):
    """With any other single fault present, the reason is the earliest one in the former serial order."""
    for index in [None, *PROCESS_FAULTS, 10, 11, 12]:
        host = make_host()
        if index is not None:
            FAULTS[index][1](host)
        Probe(host, monkeypatch, delays=delays_for(profile, index or 0), errors={which: AuthRefused(reason)})
        new, old = outcome(lambda: current(bare())), outcome(lambda: legacy(bare()))
        assert same_outcome(new, old), (index, which, reason, new[:2], old[:2])
        assert new[0] == 'AuthRefused'


@pytest.mark.parametrize('which', ['txt-server', 'txt-parent'])
def test_a_failing_lookup_does_not_leave_work_running(make_host, monkeypatch, which):
    host = make_host()
    probe = Probe(host, monkeypatch, delays=delays_for('forward', 2), errors={which: AuthRefused('desktop_executable_unqualified')})
    with pytest.raises(AuthRefused):
        current(bare())
    assert probe.inflight == 0


# -- retry discipline: only the vanished-process reason is retried ----------------------------------
class VanishingAuthority:
    """connected_peer reaches the executable lookup exactly as the real one does through binding()."""

    def __init__(self, script, monkeypatch):
        self.calls = 0
        script = list(script)

        def raw(pid, size):
            kind = script.pop(0)
            if kind == 'ok':
                return len(OK_PATH), OK_PATH + b'\0' * (size - len(OK_PATH)), 0
            return 0, b'\0' * size, kind
        monkeypatch.setattr(local_peer, '_proc_pidpath', raw)

    def connected_peer(self, sock, pid, identity):
        self.calls += 1
        at.DesktopOmlx._executable(bare(), 4242)
        return 'peer'


OK_PATH = b'/Applications/oMLX.app/Contents/MacOS/oMLX'


@pytest.mark.parametrize('error', ['ESRCH', 'EPERM', 'ENOMEM', 'EINVAL', 'EACCES', 'EIO', 'EINTR', 'EAGAIN'])
def test_native_inspection_is_retried_up_to_three_attempts_like_a_failing_lsof(monkeypatch, error):
    import errno
    error = getattr(errno, error)
    authority = VanishingAuthority([error, error, 'ok'], monkeypatch)
    assert at.connected_peer_with_retry(authority, None, 4242, None, pause=0) == 'peer' and authority.calls == 3
    authority = VanishingAuthority([error] * 3, monkeypatch)
    with pytest.raises(AuthRefused) as caught:
        at.connected_peer_with_retry(authority, None, 4242, None, pause=0)
    assert str(caught.value) == 'native_inspection_unavailable' and authority.calls == 3


@pytest.mark.parametrize('error', ['ENOENT'])
def test_missing_executable_is_never_retried(monkeypatch, error):
    import errno
    authority = VanishingAuthority([getattr(errno, error), 'ok', 'ok'], monkeypatch)
    with pytest.raises(AuthRefused) as caught:
        at.connected_peer_with_retry(authority, None, 4242, None, pause=0)
    assert str(caught.value) == 'desktop_executable_unqualified' and authority.calls == 1


def test_no_new_transient_reason_exists():
    assert at.TRANSIENT_INSPECTION_REASONS == {'native_inspection_unavailable', 'connection_inspection_unavailable'}
