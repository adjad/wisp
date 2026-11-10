"""Local store of text extracted from email attachments.

A follow-up question about the same file needs no fetch and no re-read.
The text is private mail content, so erasing matters as much as reading:
the database is owner-only (0600 file, 0700 directory), ``secure_delete`` is
on, and ``VACUUM`` runs after anything is erased.

Ageing: a reading unused for ``MAX_AGE_S`` is dropped. ``get`` refuses and
deletes a stale row itself, so old text is never served even if no prune ran,
and ``get``/``locate``/``put`` all run ``prune`` at most hourly (callers may
also run ``prune_if_due`` at startup). A stale row deleted by ``get`` is not
vacuumed on the spot (that would rewrite the file on a read): with
``secure_delete`` its bytes are already zeroed, and the next prune does the
``VACUUM``.

Unknown owner: a reading stored with owner ``""`` (a mail row whose account
label was empty) is never removed by ``forget_account`` itself, and
``forget_account("")`` does nothing. But an unknown owner must not keep private
text alive after a known owner was erased: when ``forget_account(X)`` removes
X from a digest and only ``""`` owners remain, the text goes too. A digest
owned by X and Y stays until both go; one owned only by ``""`` is removed by
``forget_all`` or by ageing.

Every public function swallows sqlite and OS errors and returns its
"nothing" value (None / False / 0). Nothing here logs text, names or
locators.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from service import paths

VERSION = 1  # bump to invalidate every stored reading
MAX_TEXT_CHARS = 600_000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_AGE_S = 30 * 86400  # since last use

_MAX_LOCATOR = 300
_PRUNE_EVERY_S = 3600
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS texts(
    digest TEXT PRIMARY KEY, version INTEGER NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL, cut INTEGER NOT NULL, note TEXT NOT NULL,
    how TEXT NOT NULL, bytes INTEGER NOT NULL, created REAL NOT NULL,
    last_used REAL NOT NULL);
CREATE TABLE IF NOT EXISTS owners(
    digest TEXT NOT NULL, account TEXT NOT NULL,
    PRIMARY KEY(digest, account));
CREATE TABLE IF NOT EXISTS locators(
    locator TEXT PRIMARY KEY, digest TEXT NOT NULL, account TEXT NOT NULL,
    created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL NOT NULL);
"""


@dataclass
class Stored:
    digest: str
    kind: str
    text: str
    cut: bool
    note: str
    how: str


def digest(data: bytes) -> str:
    """SHA-256 hex of the file bytes."""
    return hashlib.sha256(bytes(data)).hexdigest()


def _db_path() -> Path:
    return paths.moe_dir() / "cache" / "attachment_text.db"


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _valid_locator(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_LOCATOR


def _account(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _connect() -> sqlite3.Connection | None:
    """Open the database (creating it owner-only), or None if unusable."""
    try:
        path = _db_path()
        folder = path.parent
        if os.path.islink(folder) or os.path.islink(path):
            return None
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        # Create the file with the right mode before sqlite touches it.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        conn = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
        try:
            conn.execute("PRAGMA secure_delete=ON")
            conn.executescript(_SCHEMA)
        except sqlite3.Error:
            conn.close()
            raise
        return conn
    except (sqlite3.Error, OSError):
        return None


def get(digest: str) -> Stored | None:
    """Stored reading for a file digest, touching its last-used time."""
    if not _valid_digest(digest):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT version, kind, text, cut, note, how, last_used"
                " FROM texts WHERE digest=?",
                (digest,),
            ).fetchone()
            now = time.time()
            due = _claim_prune(conn, now)
            result = None
            if row is not None:
                if row[0] != VERSION or row[6] < now - MAX_AGE_S:
                    _drop_digest(conn, digest, vacuum_later=row[0] == VERSION)
                else:
                    conn.execute(
                        "UPDATE texts SET last_used=? WHERE digest=?", (now, digest)
                    )
                    result = Stored(
                        digest, row[1], row[2], bool(row[3]), row[4], row[5]
                    )
        if due:
            prune(now)
        return result
    except (sqlite3.Error, OSError):
        return None


def _drop_digest(conn: sqlite3.Connection, digest: str, *, vacuum_later: bool) -> None:
    conn.execute("DELETE FROM texts WHERE digest=?", (digest,))
    conn.execute("DELETE FROM owners WHERE digest=?", (digest,))
    conn.execute("DELETE FROM locators WHERE digest=?", (digest,))
    if vacuum_later:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('vacuum_due', 1)")


def _claim_prune(conn: sqlite3.Connection, now: float) -> bool:
    """True (and the hour is claimed) when an opportunistic prune is due."""
    row = conn.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
    due = row is None or now - row[0] >= _PRUNE_EVERY_S or row[0] > now
    if due:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_prune', ?)", (now,)
        )
    return due


def prune_if_due() -> int:
    """Run ``prune`` if an hour passed since the last one (call at startup)."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        with closing(conn):
            now = time.time()
            due = _claim_prune(conn, now)
        return prune(now) if due else 0
    except (sqlite3.Error, OSError):
        return 0


def put(
    digest: str,
    *,
    kind: str,
    text: str,
    cut: bool,
    note: str = "",
    how: str = "",
    account: str = "",
) -> bool:
    """Store a reading. Empty or whitespace-only text is refused."""
    if not _valid_digest(digest) or not isinstance(text, str) or not text.strip():
        return False
    if not isinstance(kind, str) or not isinstance(note, str) or not isinstance(how, str):
        return False
    cut = bool(cut)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        cut = True
    size = len(text.encode("utf-8", "replace"))
    now = time.time()
    acct = _account(account)
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO texts(digest, version, kind, text, cut, note, how,"
                    " bytes, created, last_used) VALUES(?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(digest) DO UPDATE SET version=excluded.version,"
                    " kind=excluded.kind, text=excluded.text, cut=excluded.cut,"
                    " note=excluded.note, how=excluded.how, bytes=excluded.bytes,"
                    " last_used=excluded.last_used",
                    (digest, VERSION, kind, text, int(cut), note, how, size, now, now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO owners(digest, account) VALUES(?,?)",
                    (digest, acct),
                )
                due = _claim_prune(conn, now)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        if due:
            prune(now)
        return True
    except (sqlite3.Error, OSError):
        return False


def locate(locator: str) -> str | None:
    """Digest remembered for a caller-chosen locator."""
    if not _valid_locator(locator):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT digest FROM locators WHERE locator=?", (locator,)
            ).fetchone()
            now = time.time()
            due = _claim_prune(conn, now)
        if due:
            prune(now)
        return row[0] if row else None
    except (sqlite3.Error, OSError):
        return None


def remember(locator: str, digest: str, *, account: str = "") -> bool:
    """Remember which digest a locator pointed at."""
    if not _valid_locator(locator) or not _valid_digest(digest):
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute(
                "INSERT OR REPLACE INTO locators(locator, digest, account, created)"
                " VALUES(?,?,?,?)",
                (locator, digest, _account(account), time.time()),
            )
        return True
    except (sqlite3.Error, OSError):
        return False


def forget_account(account: str) -> int:
    """Erase everything owned by an account; returns text rows removed.

    A text shared with another account stays until its last owner goes.
    """
    acct = _account(account)
    if not acct:
        return 0
    conn = _connect()
    if conn is None:
        return 0
    try:
        with closing(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                theirs = [
                    r[0]
                    for r in conn.execute(
                        "SELECT digest FROM owners WHERE account=?", (acct,)
                    ).fetchall()
                ]
                changed = conn.execute(
                    "DELETE FROM owners WHERE account=?", (acct,)
                ).rowcount
                removed = 0
                for dig in theirs:
                    # Only unknown ("") owners left: they cannot keep it alive.
                    if conn.execute(
                        "SELECT 1 FROM owners WHERE digest=? AND account<>''",
                        (dig,),
                    ).fetchone() is None:
                        removed += conn.execute(
                            "DELETE FROM texts WHERE digest=?", (dig,)
                        ).rowcount
                        conn.execute("DELETE FROM owners WHERE digest=?", (dig,))
                changed += conn.execute(
                    "DELETE FROM locators WHERE account=?", (acct,)
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM texts WHERE digest NOT IN"
                    " (SELECT digest FROM owners)"
                ).rowcount
                changed += removed
                changed += conn.execute(
                    "DELETE FROM locators WHERE digest NOT IN"
                    " (SELECT digest FROM texts)"
                ).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if changed:
                conn.execute("VACUUM")
            return max(removed, 0)
    except (sqlite3.Error, OSError):
        return 0


def forget_all() -> int:
    """Erase every stored reading; returns text rows removed."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        with closing(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                removed = conn.execute("DELETE FROM texts").rowcount
                changed = removed
                changed += conn.execute("DELETE FROM owners").rowcount
                changed += conn.execute("DELETE FROM locators").rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if changed:
                conn.execute("VACUUM")
            return max(removed, 0)
    except (sqlite3.Error, OSError):
        return 0


def prune(now: float | None = None) -> int:
    """Drop unused readings (age first, then least recently used by size)."""
    now = time.time() if now is None else float(now)
    conn = _connect()
    if conn is None:
        return 0
    try:
        with closing(conn):
            removed = 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                removed += conn.execute(
                    "DELETE FROM texts WHERE last_used < ?", (now - MAX_AGE_S,)
                ).rowcount
                total = conn.execute(
                    "SELECT COALESCE(SUM(bytes), 0) FROM texts"
                ).fetchone()[0]
                if total > MAX_TOTAL_BYTES:
                    rows = conn.execute(
                        "SELECT digest, bytes FROM texts ORDER BY last_used ASC"
                    ).fetchall()
                    for dig, size in rows:
                        if total <= MAX_TOTAL_BYTES:
                            break
                        conn.execute("DELETE FROM texts WHERE digest=?", (dig,))
                        total -= size
                        removed += 1
                conn.execute(
                    "DELETE FROM owners WHERE digest NOT IN (SELECT digest FROM texts)"
                )
                orphans = conn.execute(
                    "DELETE FROM locators WHERE digest NOT IN"
                    " (SELECT digest FROM texts)"
                ).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            pending = conn.execute(
                "SELECT 1 FROM meta WHERE key='vacuum_due'"
            ).fetchone()
            if removed or orphans or pending:
                conn.execute("VACUUM")
                conn.execute("DELETE FROM meta WHERE key='vacuum_due'")
            return removed
    except (sqlite3.Error, OSError):
        return 0
