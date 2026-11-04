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

import logging
import os
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


def private_sqlite_file(path: str | os.PathLike[str]) -> None:
    """Make a Wisp SQLite database owner-only (0600) before it is opened.

    These files hold the user's chats, remembered facts and synced mail and
    message caches, so another account on the Mac must not be able to read
    them. A missing file is created 0600 (never briefly world-readable); an
    existing file and its sidecars are tightened in place. Contents are never
    touched. A failure is logged, not raised: SQLite reports a file it truly
    cannot open, and a store must not refuse to start over its mode.
    """
    if str(path) == ":memory:":
        return
    main = Path(path)
    try:
        os.close(os.open(main, os.O_RDONLY | os.O_CREAT, 0o600))
    except OSError as exc:
        log.warning("could not create %s privately: %s", main, exc)
        return
    for candidate in (main, *(main.with_name(main.name + s) for s in _SQLITE_SIDECARS)):
        try:
            if candidate.stat().st_mode & 0o077:
                os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("could not make %s owner-only: %s", candidate, exc)
