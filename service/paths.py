"""The single place ~/.moe is computed.

WISP_HOME redirects Wisp's OWN state (commitments, sessions, facts, caches,
grants, skills, config overlay) somewhere else — the sandbox uses it to get a
completely separate world without touching the user's real data.

Deliberately NOT $HOME: overriding HOME would also move ~/.omlx/settings.json
and ~/.omlx/model_settings.json (service/config/__init__.py), which is where
the model roster and the user's starred favorites live — the backend would
boot with no models.

Resolved once at import, matching the import-time constants it feeds.
"""
from __future__ import annotations

import glob
import logging
import os
import stat
import time
from pathlib import Path

log = logging.getLogger(__name__)


def moe_dir() -> Path:
    override = os.environ.get("WISP_HOME")
    return Path(override).expanduser() if override else Path.home() / ".moe"


MOE_DIR = moe_dir()


# SQLite's rollback journal and WAL files. SQLite gives a sidecar it creates the
# main file's mode, so a private main file keeps new sidecars private; sidecars
# left behind by an older build still need their own chmod.
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")

# A private_write_bytes temp file older than this was left by a write that died
# between create and rename (a crash or SIGKILL); a live write takes far less.
STALE_TEMP_S = 600


def tighten_private_file(path: str | os.PathLike[str]) -> None:
    """Make an existing regular file owner-only (0600), never through a link.

    lstat, then chmod with follow_symlinks=False: a symlink is refused (and a
    link raced in after the lstat only has the link's own mode changed), and
    the file is never opened, so a FIFO cannot hang it and no SQLite POSIX lock
    held elsewhere in this process is dropped by a close. A missing file is
    fine; any other failure is logged, not raised: a store or state file must
    not refuse to start over its mode.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("could not check %s: %s", path, exc)
        return
    if not stat.S_ISREG(info.st_mode):
        log.warning("not making %s owner-only: not a regular file", path)
        return
    if info.st_mode & 0o077:
        try:
            os.chmod(path, 0o600, follow_symlinks=False)
        except OSError as exc:
            log.warning("could not make %s owner-only: %s", path, exc)


def private_sqlite_file(path: str | os.PathLike[str]) -> None:
    """Make a Wisp SQLite database owner-only (0600) before it is opened.

    These files hold the user's chats, remembered facts and synced mail and
    message caches, so another account on the Mac must not be able to read
    them. A missing file is created 0600 (O_EXCL|O_NOFOLLOW: never briefly
    world-readable, never through a link); an existing file and its sidecars
    are tightened in place without being opened (tighten_private_file).
    Contents are never touched. A failure is logged, not raised: SQLite reports
    a file it truly cannot open, and a store must not refuse to start over its
    mode.
    """
    if str(path) == ":memory:":
        return
    main = Path(path)
    try:
        os.close(os.open(main, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    except FileExistsError:
        pass                                  # an existing file (or link): tightened below
    except OSError as exc:
        log.warning("could not create %s privately: %s", main, exc)
        return
    for candidate in (main, *(main.with_name(main.name + s) for s in _SQLITE_SIDECARS)):
        tighten_private_file(candidate)


def private_mkdirs(path: str | os.PathLike[str]) -> None:
    """Create `path` and every missing parent, each 0700.

    mkdir(mode=..., parents=True) gives the mode only to the last directory;
    the parents it creates get the umask's (0755). Directories that already
    exist are left as they are.
    """
    target = Path(path)
    missing = []
    while not os.path.lexists(target):
        missing.append(target)
        target = target.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass


def private_temp_files(path: str | os.PathLike[str]) -> list[Path]:
    """The temp files private_write_bytes uses for `path` that exist right now.

    A crash between create and rename leaves one behind (owner-only, but still
    a copy of the data), so code that deletes `path` for privacy deletes these
    too."""
    target = Path(path)
    return list(target.parent.glob(f".{glob.escape(target.name)}.*.tmp"))


def private_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Replace `path` with `data`, owner-only (0600) from the first byte.

    For state files of personal data under ~/.moe. The bytes go to a fresh
    temp file in the same directory, created 0600 with O_EXCL|O_NOFOLLOW (so
    it can be neither an existing file nor a link), then os.replace moves it
    over `path` atomically after an fsync: a reader sees the old file or the
    new one, never a torn write, and a file an older build left 0644 is
    replaced by a 0600 one. A symlink at `path` is replaced, not written
    through. Nothing is chmod-ed after data is written. Temp files an earlier
    write of `path` left behind by dying are removed first. Missing
    parent directories are created 0700 (private_mkdirs).
    Errors propagate, as from a plain write.
    """
    target = Path(path)
    private_mkdirs(target.parent)
    cutoff = time.time() - STALE_TEMP_S
    for stale in private_temp_files(target):
        try:
            if os.lstat(stale).st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass
    temp = target.with_name(f".{target.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)          # exactly 0600 whatever the umask, before any byte
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def private_append_text(path: str | os.PathLike[str], text: str) -> None:
    """Append `text` to a log of personal data, owner-only (0600) from the first byte.

    The file is created 0600 with O_NOFOLLOW (a symlink in its place is
    refused with ELOOP) and O_NONBLOCK (a FIFO cannot hang the open); anything
    but a regular file is refused. One an older build left 0644 is tightened
    through the same descriptor before anything more is written to it. Missing
    parent directories are created 0700 (private_mkdirs). Errors propagate, as from a
    plain write.
    """
    target = Path(path)
    private_mkdirs(target.parent)
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{target} is not a regular file")
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
