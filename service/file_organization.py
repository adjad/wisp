"""Bounded file organization plans and atomic no-replace moves.

No content model, native app, shell, or network is used. Hashes record the
regular-file bytes inspected for a preview. Moving those exact bytes requires
source names and contents to remain stable through the atomic rename: the
kernel operation selects a basename, not a previously approved inode. A late
source replacement may therefore move an unapproved entry before verification
reports unknown. Unknown receipts describe possible completed side effects;
they must never trigger automatic retry or rollback. No-replacement at the
destination is enforced by the kernel independently of this source limitation.
Cross-volume moves refuse rather than falling back to copy/delete.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
from datetime import date
import errno
import fnmatch
import hashlib
import os
from pathlib import Path
import re
import stat
import sys

MAX_FILES = 1000
MAX_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: str


@dataclass(frozen=True)
class FileMove:
    source: str
    destination: str
    identity: FileIdentity


@dataclass(frozen=True)
class OrganizationPlan:
    folder: str
    destination: str
    pattern: str
    group_by: str
    moves: tuple[FileMove, ...]
    skipped: tuple[tuple[str, str], ...]
    source_state: tuple
    destination_states: tuple[tuple[str, tuple], ...]
    # A newly appearing target is safely skipped by the kernel. Its presence
    # changes the preview note, not the already-approved source/target set.
    existing_targets: tuple[str, ...] = field(default=(), compare=False)


def absolute_path(value: str) -> Path:
    # abspath normalizes '.'/'..' without following a symlink before admission.
    return Path(os.path.abspath(os.path.expanduser(value)))


@contextmanager
def open_directory(path: Path, *, create: bool = False, allow_missing: bool = False,
                   expected: tuple | None = None):
    """Walk from / using pinned descriptors; never traverse a symlink."""
    path = absolute_path(str(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    state = []
    try:
        root = os.fstat(fd)
        state.append((path.anchor, root.st_dev, root.st_ino))
        if expected and state[0] != expected[0]:
            raise OSError(errno.ESTALE, "directory changed since preview", str(path))
        missing = False
        for index, part in enumerate(path.parts[1:], 1):
            if missing:
                state.append((part, None, None))
                continue
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if create:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass  # Another creator must still pass O_NOFOLLOW below.
                    child = os.open(part, flags, dir_fd=fd)
                elif allow_missing:
                    missing = True
                    state.append((part, None, None))
                    continue
                else:
                    raise
            info = os.fstat(child)
            entry = (part, info.st_dev, info.st_ino)
            if expected and expected[index][1] is not None and entry != expected[index]:
                os.close(child)
                raise OSError(errno.ESTALE, "directory changed since preview", str(path))
            os.close(fd)
            fd = child
            state.append(entry)
        yield (None if missing else fd), tuple(state)
    finally:
        os.close(fd)


def directory_state(path: Path, *, allow_missing: bool = False) -> tuple:
    with open_directory(path, allow_missing=allow_missing) as (_, state):
        return state


def _stat_identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_identity(directory: int, name: str) -> FileIdentity:
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise OSError(errno.EINVAL, "only regular files can move; symlinks are excluded", name)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(before) != _stat_identity(opened):
            raise OSError(errno.ESTALE, "source changed while inspecting it", name)
        digest = hashlib.sha256()
        remaining = MAX_BYTES
        while True:
            chunk = os.read(fd, min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("file exceeds the 512 MiB preview byte limit; narrow the request")
            digest.update(chunk)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _stat_identity(opened) != _stat_identity(after) or _stat_identity(after) != _stat_identity(current):
            raise OSError(errno.ESTALE, "source changed while inspecting it", name)
        return FileIdentity(*_stat_identity(after), digest.hexdigest())
    finally:
        os.close(fd)


def group_for_filename(name: str, group_by: str) -> str | None:
    """Pure policy/preview mapping: empty group, one category, or unclassified."""
    if not isinstance(group_by, str) or group_by not in {"", "extension", "debug_date"}:
        raise ValueError("semantic/content categorization is unsupported; group_by must be empty, extension, or debug_date")
    if group_by == "extension":
        extension = Path(name).suffix.lstrip(".")
        return extension.upper() if extension else "NO_EXTENSION"
    if group_by == "debug_date":
        match = re.match(r"^(?:wisp[-_])?debug[-_](\d{4}-\d{2}-\d{2})(?=[_.-]|$)", name, re.I)
        if match:
            try:
                return date.fromisoformat(match[1]).isoformat()
            except ValueError:
                pass
        return None
    return ""


def build_plan(pattern: str, folder: str, destination: str, group_by: str = "") -> OrganizationPlan:
    if not isinstance(group_by, str) or group_by not in {"", "extension", "debug_date"}:
        raise ValueError("semantic/content categorization is unsupported; group_by must be empty, extension, or debug_date")
    if not all(isinstance(value, str) and value.strip() for value in (pattern, folder, destination)):
        raise ValueError("pattern, folder and destination are required")
    source = absolute_path(folder)
    target = Path(destination).expanduser()
    target = absolute_path(str(target if target.is_absolute() else source / target))
    if source == target and not group_by:
        raise ValueError("source and destination are the same folder")
    if "/" in pattern:
        qualified = absolute_path(pattern)
        if qualified.parent != source:
            raise ValueError("pattern must be a filename glob inside the source folder")
        pattern = qualified.name
    moves, skipped, states, conflicts = [], [], {}, []
    with open_directory(source) as (src_fd, source_state):
        names = sorted(name for name in os.listdir(src_fd) if fnmatch.fnmatch(name, pattern))
        files, total_bytes = [], 0
        for name in names:
            info = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                continue  # Immediate regular files only; never recurse.
            if not stat.S_ISREG(info.st_mode):
                skipped.append((str(source / name), "not a regular file; symlink/special file left untouched"))
                continue
            files.append((name, info))
            total_bytes += info.st_size
        if len(files) > MAX_FILES or total_bytes > MAX_BYTES:
            raise ValueError("preview is limited to 1000 files and 512 MiB; narrow the pattern or folder")
        # Check destination even for an empty match; a symlink is not an
        # acceptable organization root and never becomes a preview capability.
        directory_state(target, allow_missing=True)
        for name, _ in files:
            group = group_for_filename(name, group_by)
            if group is None:
                skipped.append((str(source / name), "no valid debug date in filename; left untouched"))
                continue
            parent = target / group if group else target
            if str(parent) not in states:
                states[str(parent)] = directory_state(parent, allow_missing=True)
            with open_directory(parent, allow_missing=True) as (dst_fd, _):
                if dst_fd is not None:
                    try:
                        os.stat(name, dir_fd=dst_fd, follow_symlinks=False)
                        conflicts.append(str(parent / name))
                    except FileNotFoundError:
                        pass
            moves.append(FileMove(str(source / name), str(parent / name), file_identity(src_fd, name)))
    return OrganizationPlan(str(source), str(target), pattern, group_by, tuple(moves),
                            tuple(skipped), source_state, tuple(sorted(states.items())), tuple(conflicts))


def atomic_move_no_replace(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
    """One rename with kernel-enforced exclusion, including dangling symlinks."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        flags = 4  # RENAME_EXCL, as in the repository's atomic backup publisher.
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        flags = 1  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace moves unavailable on this platform")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flags) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), destination)


def execute_plan(plan: OrganizationPlan) -> list[dict]:
    """Consume no preview here; caller claims it once before entering execution."""
    receipts = []
    states = dict(plan.destination_states)
    for move in plan.moves:
        source, target = Path(move.source), Path(move.destination)
        receipt = {"source": move.source, "destination": move.destination,
                   "expected_digest": move.identity.digest, "rename_performed": False}
        renamed = False
        try:
            with open_directory(Path(plan.folder), expected=plan.source_state) as (src_fd, _):
                if file_identity(src_fd, source.name) != move.identity:
                    raise OSError(errno.ESTALE, "source changed since preview", source.name)
                with open_directory(target.parent, create=True, expected=states[str(target.parent)]) as (dst_fd, _):
                    destination_info = os.fstat(dst_fd)
                    receipt["destination_directory_identity"] = {
                        "device": destination_info.st_dev, "inode": destination_info.st_ino}
                    # There is no conditional-inode rename here. Another stat
                    # cannot close the basename race; postverification must
                    # report an unknown effect if a replacement was moved.
                    atomic_move_no_replace(src_fd, source.name, dst_fd, target.name)
                    renamed = True
                    receipt["rename_performed"] = True
                    actual = file_identity(dst_fd, target.name)
                    receipt["observed_digest"] = actual.digest
                    # Rename changes ctime; all other identity/content evidence
                    # must still agree. A late edit is not a verified exact move.
                    if (actual.device, actual.inode, actual.size, actual.mtime_ns, actual.digest) != (
                            move.identity.device, move.identity.inode, move.identity.size,
                            move.identity.mtime_ns, move.identity.digest):
                        raise OSError(errno.ESTALE, "content changed during move; verify destination before retrying")
                    visible = directory_state(target.parent)
                    if visible[-1][1:] != (os.fstat(dst_fd).st_dev, os.fstat(dst_fd).st_ino):
                        raise OSError(errno.ESTALE, "destination directory moved; verify file location before retrying")
            receipt.update(status="moved", digest=actual.digest)
        except (OSError, ValueError) as exc:
            if renamed:
                receipt.update(status="unknown", reason=f"rename occurred but verification failed: {exc}; do not retry automatically")
            elif isinstance(exc, OSError) and exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                receipt.update(status="skipped", reason="destination already exists; nothing replaced")
            elif isinstance(exc, OSError) and exc.errno == errno.EXDEV:
                receipt.update(status="failed", reason="cross-volume no-replace move unsupported; source left untouched")
            else:
                receipt.update(status="failed", reason=str(exc))
        receipts.append(receipt)
    receipts.extend({"source": source, "status": "skipped", "reason": reason} for source, reason in plan.skipped)
    return receipts
