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
* Call-shape tests for ``_listener``: the exact argv set, the data-dependency
  order that must survive any rescheduling, and (named "sequential") the exact
  current serial order that T0-spawn will deliberately change.
* Documented blind spots pinned as behaviour.  Their names start with
  ``test_PINNED_BLIND_SPOT_``: if one of them fails because a check became
  stricter, that is a deliberate security change that needs its own review,
  not a test to relax.
* A seam test for the instrumentation points the release benchmark relies on.

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


def test_oracle_is_verbatim_copy_of_current_production_walker():
    """Proves the oracle really is today's code.

    RETIRE (do not weaken) this one test in the pull request that replaces
    ``DesktopOmlx._qualified_tree``: from then on the oracle is the only record
    of the old behaviour and ``test_oracle_is_frozen`` keeps it immutable.
    """
    assert _method_text(LegacyWalk._qualified_tree) == _method_text(at.DesktopOmlx._qualified_tree)
    assert _method_text(LegacyWalk._trusted_group) == _method_text(at.DesktopOmlx._trusted_group)


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


@contextlib.contextmanager
def after_first_pass(root, mutate):
    """Run ``mutate`` after the first full walk of ``root``, before the second.

    Both the production code and the oracle enumerate with ``root.rglob('*')``
    once per pass, so the second call is the boundary between the two passes.
    """
    if mutate is None:
        yield
        return
    original = pathlib.Path.rglob
    calls = {'n': 0}

    def rglob(self, *args, **kwargs):
        if self == root:
            calls['n'] += 1
            if calls['n'] == 2:
                mutate()
        return original(self, *args, **kwargs)
    with mock.patch.object(pathlib.Path, 'rglob', rglob):
        yield


def run_both(trees, build, *, trusted=TRUST_ALL, uid_shift=0):
    """Build the same tree twice; run production on one and the oracle on the other."""
    results = []
    for factory in (make_production, make_oracle):
        tree = trees()
        mutate = build(tree)
        authority = factory(tree, trusted, uid_shift)
        with after_first_pass(tree.root, mutate):
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
    """The second pass is what produces 'changed': one extra rglob per tree."""
    tree = trees()
    base_content(tree)
    calls = []
    original = pathlib.Path.rglob

    def rglob(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)
    authority = make_production(tree, TRUST_ALL)
    with mock.patch.object(pathlib.Path, 'rglob', rglob):
        authority._qualified_tree(tree.root)
    assert calls == [tree.root, tree.root]


def test_entry_vanishing_between_listing_and_lstat_is_refused(trees):
    def run(factory):
        tree = trees()
        base_content(tree)
        authority = factory(tree, TRUST_ALL)
        victim = tree.root / 'pkg/sub/data.txt'
        original = pathlib.Path.lstat
        state = {'done': False}

        def lstat(self, *args, **kwargs):
            if self == tree.root / 'bin/tool' and not state['done']:   # sorts before the victim
                state['done'] = True
                os.unlink(victim)
            return original(self, *args, **kwargs)
        with mock.patch.object(pathlib.Path, 'lstat', lstat):
            return verdict(lambda: authority._qualified_tree(tree.root))
    assert run(make_production) == run(make_oracle) == REFUSED


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
TXT_ARGV = {pid: ('/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-d', 'txt', '-Fn') for pid in (SERVER_PID, PARENT_PID)}
EXPECTED_ARGVS = {LISTEN_ARGV, *PS_ARGV.values(), *TXT_ARGV.values()}


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
        if argv[0] == '/usr/sbin/lsof' and '-d' in argv:
            return f'p{argv[4]}\nftxt\nn{self.txt[argv[4]]}\n'.encode()
        raise AssertionError(argv)

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
    ('spawn', PS_ARGV[SERVER_PID]), ('spawn', TXT_ARGV[SERVER_PID]), ('signed', SERVER_PID, 'python3'),
    ('spawn', PS_ARGV[PARENT_PID]), ('spawn', TXT_ARGV[PARENT_PID]), ('signed', PARENT_PID, 'app.omlx'),
]


def test_listener_issues_exactly_the_reviewed_set_of_spawns(host):
    authority = host.new()
    assert authority._identity[0] == SERVER_PID and authority._identity[2] == PARENT_PID
    assert Counter(spawns(host)) == Counter(EXPECTED_ARGVS)      # five spawns, each exactly once
    assert [e for e in host.events if e[0] == 'tcp_listeners'] == [('tcp_listeners',)]
    assert sorted(e[1:] for e in host.events if e[0] == 'signed') == [(PARENT_PID, 'app.omlx'), (SERVER_PID, 'python3')]
    assert [e[1] for e in host.events if e[0] == 'tree'] == [host.python_root, host.server_entry.parent]


def test_listener_data_dependencies_hold_in_any_scheduling(host):
    """True data dependencies; any rescheduling must keep every one of them."""
    host.new()
    order = [e for e in host.events if e[0] != 'tree']
    at_ = {event: index for index, event in enumerate(order)}
    listen, ps_server, ps_parent = ('spawn', LISTEN_ARGV), ('spawn', PS_ARGV[SERVER_PID]), ('spawn', PS_ARGV[PARENT_PID])
    txt_server, txt_parent = ('spawn', TXT_ARGV[SERVER_PID]), ('spawn', TXT_ARGV[PARENT_PID])
    assert at_[listen] < at_[ps_server] < at_[ps_parent]          # pid -> ppid -> parent
    assert at_[listen] < at_[txt_server] < at_[('signed', SERVER_PID, 'python3')]
    assert at_[ps_parent] < at_[txt_parent] < at_[('signed', PARENT_PID, 'app.omlx')]


def test_listener_current_sequential_order_is_exactly_this(host):
    """The serial order T0-spawn will deliberately change; update it only there."""
    host.new()
    assert host.events == [*SEQUENTIAL_SUCCESS, ('tree', host.python_root), ('tree', host.server_entry.parent)]


def test_binding_repeats_every_spawn_but_walks_no_tree_on_the_same_instance(host):
    authority = host.new()
    walks = []
    original = pathlib.Path.rglob

    def rglob(self, *args, **kwargs):
        walks.append(self)
        return original(self, *args, **kwargs)
    host.events.clear()
    with mock.patch.object(pathlib.Path, 'rglob', rglob):
        assert authority.binding() == SERVER_PID
    assert Counter(spawns(host)) == Counter(EXPECTED_ARGVS)
    assert walks == []                                            # tree cache hit on this instance
    assert [e[1] for e in host.events if e[0] == 'tree'] == [host.python_root, host.server_entry.parent]


def test_every_new_authority_walks_each_tree_twice(host):
    walks = Counter()
    original = pathlib.Path.rglob

    def rglob(self, *args, **kwargs):
        walks[self] += 1
        return original(self, *args, **kwargs)
    with mock.patch.object(pathlib.Path, 'rglob', rglob):
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
    # Sequential today: nothing after the failing step has run.  T0-spawn may issue more.
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
