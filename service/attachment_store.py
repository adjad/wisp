"""Local store of text extracted from email attachments.

A follow-up question about the same file needs no fetch and no re-read.
The text is private mail content, so erasing matters as much as reading:
the database is owner-only (0600 file, 0700 directory), ``secure_delete`` is
on, and ``VACUUM`` runs after anything is erased.

Ageing: a reading unused for ``MAX_AGE_S`` is dropped. ``get`` refuses and
deletes a stale row itself, so old text is never served even if no prune ran,
and pruning runs in exactly two places, at most hourly: ``put`` (after its own
write is committed) and ``prune_if_due``, which the backend starts as a
background task at launch. ``get`` and ``locate`` NEVER prune: a read does only
its own cheap work and never waits on a prune or its ``VACUUM``. A stale row
deleted by ``get`` is not vacuumed on the spot (that would rewrite the file on
a read): with ``secure_delete`` its bytes are already zeroed, and the next
prune does the ``VACUUM``.

"Pruned this hour" is recorded only when a prune succeeds, so a failed prune
(locked database, error) is retried by the next ``put``/``prune_if_due`` and a
pending ``vacuum_due`` is not delayed an hour. To keep two threads or
processes from pruning at once, a claim takes a short lease (``prune_lease``,
``_LEASE_S``) in the database; the claim is atomic (``BEGIN IMMEDIATE``), the
prune clears the lease when it ends either way, and a crashed pruner's lease
simply expires. This is a best-effort guard, not a lock.

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
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
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
_LEASE_S = 120  # how long a claimed prune blocks another one
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
CREATE TABLE IF NOT EXISTS seen(
    id INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL,
    source TEXT NOT NULL, account TEXT NOT NULL, name TEXT NOT NULL,
    sender TEXT NOT NULL, subject TEXT NOT NULL, ts REAL NOT NULL,
    UNIQUE(digest, source, account, name, sender, subject));
CREATE INDEX IF NOT EXISTS seen_digest ON seen(digest);
CREATE TABLE IF NOT EXISTS facts(
    digest TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tried(
    key TEXT PRIMARY KEY, outcome TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS indexed(
    rid INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL UNIQUE);
"""

# Full-text index: contentless (the words only; the text is stored once, in
# ``texts``) with deletes. Set to False when this SQLite has no FTS5; search
# then scans the texts instead.
FTS5 = True
_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS ft USING fts5(body, content='',"
    " contentless_delete=1, tokenize='unicode61 remove_diacritics 2')"
)


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
            _ensure_fts(conn)
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
        return result
    except (sqlite3.Error, OSError):
        return None


def _drop_digest(conn: sqlite3.Connection, digest: str, *, vacuum_later: bool) -> None:
    conn.execute("DELETE FROM texts WHERE digest=?", (digest,))
    conn.execute("DELETE FROM owners WHERE digest=?", (digest,))
    conn.execute("DELETE FROM locators WHERE digest=?", (digest,))
    _erase_extras(conn, digest)
    if vacuum_later:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('vacuum_due', 1)")


def _claim_prune(conn: sqlite3.Connection, now: float) -> bool:
    """True (and a short lease is taken) when a prune is due and none is running.

    Call inside ``BEGIN IMMEDIATE`` so two claimers cannot both win. The hour
    is NOT recorded here: ``prune`` records it when it succeeds.
    """
    row = conn.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
    due = row is None or now - row[0] >= _PRUNE_EVERY_S or row[0] > now
    if not due:
        return False
    lease = conn.execute("SELECT value FROM meta WHERE key='prune_lease'").fetchone()
    if lease is not None and now < lease[0] <= now + _LEASE_S:
        return False  # another thread or process is pruning
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('prune_lease', ?)",
        (now + _LEASE_S,),
    )
    return True


def _release_lease() -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with closing(conn):
            conn.execute("DELETE FROM meta WHERE key='prune_lease'")
    except (sqlite3.Error, OSError):
        pass


def prune_if_due() -> int:
    """Run ``prune`` if an hour passed since the last successful one.

    Blocking (it may ``VACUUM``): run it in a thread or background task, never
    on a read path. Safe to call from any thread.
    """
    conn = _connect()
    if conn is None:
        return 0
    try:
        with closing(conn):
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            try:
                due = _claim_prune(conn, now)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
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
                try:
                    _index(conn, digest, text)
                except sqlite3.Error:
                    pass  # the lazy fill in search indexes it later
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
                changed += conn.execute(
                    "DELETE FROM seen WHERE account=?", (acct,)
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
                changed += _sweep(conn)
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
                changed += conn.execute("DELETE FROM seen").rowcount
                changed += conn.execute("DELETE FROM tried").rowcount
                changed += _sweep(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if changed:
                conn.execute("VACUUM")
            return max(removed, 0)
    except (sqlite3.Error, OSError):
        return 0


# ---------------------------------------------------------------------------
# Finding kept readings: who sent them, what is inside, facts, tried files.
#
# What the user's words are: DATA. They are cut into plain alphanumeric words
# and each word is quoted by us before it reaches the index; the user's text is
# never passed on as query syntax.
#
# The search index is a contentless FTS5 table (``ft``) holding only the words;
# the text itself lives once, in ``texts``. ``indexed`` maps a text to a stable
# index row id (rowids of ``texts`` can change on VACUUM). Index rows are
# written by ``put`` and removed by ``_erase_extras`` / ``_sweep``, explicitly,
# never by triggers. Texts that predate the index are filled in lazily by
# ``search``, and any not yet filled are scanned directly.
# ---------------------------------------------------------------------------

_SOURCES = ("mail", "gmail", "messages")
_MAX_SEEN = 8
_MAX_WORDS = 8
_MAX_FACTS_CHARS = 4000
_MAX_TRIED_KEY = 300
_TRIED_AGE_S = 30 * 86400
_SNIPPET_CHARS = 160
_MAX_SNIPPETS = 2
_MAX_CANDIDATES = 5000
_FILL_ROWS = 40
_FILL_BUDGET_S = 0.5
_CHUNK = 400
_WORD_RE = re.compile(r"[^\W_]{2,}")
_OUTCOME_RE = re.compile(r"[a-z_]{1,24}")


@dataclass(frozen=True)
class Hit:
    digest: str
    kind: str
    how: str
    note: str
    cut: bool
    name: str
    sender: str
    subject: str
    ts: float
    source: str
    account: str
    snippets: tuple[str, ...]
    all_words: bool


def _clean(value: object, limit: int) -> str:
    """One line, no control or bidi characters, cut to ``limit``."""
    if not isinstance(value, str):
        return ""
    out = []
    for ch in value[: limit * 4 + 64]:
        cat = unicodedata.category(ch)
        if cat in ("Zs", "Zl", "Zp") or ch in "\t\r\n\x0b\x0c":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return " ".join("".join(out).split())[:limit].strip()


def _has_ft(conn: sqlite3.Connection) -> bool:
    if not FTS5:
        return False
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name='ft'").fetchone()
        is not None
    )


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the index table; without FTS5 in this SQLite, switch it off."""
    global FTS5
    if not FTS5:
        return
    fresh = (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name='ft'").fetchone() is None
    )
    try:
        conn.execute(_FTS_DDL)
        if fresh:
            conn.execute("INSERT INTO ft(ft, rank) VALUES('secure-delete', 1)")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "no such module" in text or "unrecognized" in text or "unknown" in text:
            FTS5 = False
            return
        raise


def _index(conn: sqlite3.Connection, digest: str, text: str) -> None:
    """(Re)write the index row for one text. Call inside a transaction."""
    if not _has_ft(conn):
        return
    row = conn.execute("SELECT rid FROM indexed WHERE digest=?", (digest,)).fetchone()
    if row:
        rid = row[0]
        conn.execute("DELETE FROM ft WHERE rowid=?", (rid,))
    else:
        rid = conn.execute("INSERT INTO indexed(digest) VALUES(?)", (digest,)).lastrowid
    conn.execute("INSERT INTO ft(rowid, body) VALUES(?,?)", (rid, text))


def _erase_extras(conn: sqlite3.Connection, digest: str) -> None:
    """Remove everything this section keeps for one digest."""
    if _has_ft(conn):
        row = conn.execute(
            "SELECT rid FROM indexed WHERE digest=?", (digest,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM ft WHERE rowid=?", (row[0],))
            # The words stay in the index segments until the next prune merges
            # them and rewrites the file.
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('vacuum_due', 1)")
    conn.execute("DELETE FROM indexed WHERE digest=?", (digest,))
    conn.execute("DELETE FROM seen WHERE digest=?", (digest,))
    conn.execute("DELETE FROM facts WHERE digest=?", (digest,))


def _sweep(conn: sqlite3.Connection) -> int:
    """Remove index rows, seen rows and facts whose text is gone.

    Call inside a transaction, after the texts were deleted. Returns the
    number of rows removed. Index entries survive a plain delete inside the
    FTS segments, so when any were removed the index is merged (``optimize``)
    here; the caller's VACUUM then rewrites the file.
    """
    gone = "digest NOT IN (SELECT digest FROM texts)"
    removed = 0
    if _has_ft(conn):
        indexed_gone = conn.execute(
            f"SELECT COUNT(*) FROM indexed WHERE {gone}"
        ).fetchone()[0]
        if indexed_gone:
            conn.execute(
                f"DELETE FROM ft WHERE rowid IN (SELECT rid FROM indexed WHERE {gone})"
            )
            _compact(conn)
    removed += conn.execute(f"DELETE FROM indexed WHERE {gone}").rowcount
    removed += conn.execute(f"DELETE FROM seen WHERE {gone}").rowcount
    removed += conn.execute(f"DELETE FROM facts WHERE {gone}").rowcount
    return removed


def _compact(conn: sqlite3.Connection) -> None:
    """Merge the index so deleted words leave the file (best effort)."""
    try:
        if _has_ft(conn):
            conn.execute("INSERT INTO ft(ft) VALUES('optimize')")
    except sqlite3.Error:
        pass


def note_seen(
    digest: str,
    *,
    source: str,
    account: str = "",
    name: str = "",
    sender: str = "",
    subject: str = "",
    ts: float = 0.0,
) -> bool:
    """Remember where a kept text came from (who, which file name, when).

    Refused when the digest has no stored text. At most 8 rows per digest, the
    newest kept. Strings are cleaned and cut (name 200, sender 200, subject 300).
    """
    if not _valid_digest(digest) or source not in _SOURCES:
        return False
    try:
        when = float(ts)
    except (TypeError, ValueError):
        when = 0.0
    if not math.isfinite(when) or when < 0:
        when = 0.0
    row = (
        digest,
        source,
        _clean(_account(account), 200),
        _clean(name, 200),
        _clean(sender, 200),
        _clean(subject, 300),
        when,
    )
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM texts WHERE digest=?", (digest,)
                ).fetchone() is None:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    "INSERT INTO seen(digest, source, account, name, sender,"
                    " subject, ts) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(digest, source, account, name, sender, subject)"
                    " DO UPDATE SET ts=excluded.ts",
                    row,
                )
                conn.execute(
                    "DELETE FROM seen WHERE digest=? AND id NOT IN"
                    " (SELECT id FROM seen WHERE digest=?"
                    "  ORDER BY ts DESC, id DESC LIMIT ?)",
                    (digest, digest, _MAX_SEEN),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return True
    except (sqlite3.Error, OSError):
        return False


def count() -> int:
    """How many readings are kept (and still servable)."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        with closing(conn):
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM texts WHERE version=? AND last_used>=?",
                    (VERSION, time.time() - MAX_AGE_S),
                ).fetchone()[0]
            )
    except (sqlite3.Error, OSError):
        return 0


def get_facts(digest: str, version: int) -> dict | None:
    """Facts kept for a text, or None (also for another ``version``)."""
    if not _valid_digest(digest) or isinstance(version, bool) or not isinstance(version, int):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT f.body FROM facts f JOIN texts t ON t.digest=f.digest"
                " WHERE f.digest=? AND f.version=?",
                (digest, version),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def put_facts(digest: str, version: int, facts: dict) -> bool:
    """Keep facts (a JSON object of at most 4,000 characters) beside a text."""
    if not _valid_digest(digest) or isinstance(version, bool) or not isinstance(version, int):
        return False
    if not isinstance(facts, dict):
        return False
    try:
        body = json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        return False
    if len(body) > _MAX_FACTS_CHARS:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM texts WHERE digest=?", (digest,)
                ).fetchone() is None:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO facts(digest, version, body) VALUES(?,?,?)",
                    (digest, version, body),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return True
    except (sqlite3.Error, OSError):
        return False


def _tried_key(key: object) -> str | None:
    """Keys are stored as a hash: a message locator is never kept readable."""
    if not isinstance(key, str) or not 0 < len(key) <= _MAX_TRIED_KEY:
        return None
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()


def tried(key: str) -> bool:
    """True when ``mark_tried`` recorded this key in the last 30 days."""
    hashed = _tried_key(key)
    if hashed is None:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT 1 FROM tried WHERE key=? AND ts>=?",
                (hashed, time.time() - _TRIED_AGE_S),
            ).fetchone()
        return row is not None
    except (sqlite3.Error, OSError):
        return False


def mark_tried(key: str, outcome: str) -> bool:
    """Record that a file was tried; ``outcome`` is a short token."""
    hashed = _tried_key(key)
    if hashed is None:
        return False
    token = outcome if isinstance(outcome, str) and _OUTCOME_RE.fullmatch(outcome) else "other"
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute(
                "INSERT OR REPLACE INTO tried(key, outcome, ts) VALUES(?,?,?)",
                (hashed, token, time.time()),
            )
        return True
    except (sqlite3.Error, OSError):
        return False


def _words(query: object) -> list[str]:
    if not isinstance(query, str):
        return []
    found: list[str] = []
    for word in _WORD_RE.findall(query[:4000].lower()):
        if word not in found:
            found.append(word)
        if len(found) >= _MAX_WORDS:
            break
    return found


def _fill(conn: sqlite3.Connection) -> None:
    """Index some texts that predate the index (bounded in rows and time)."""
    if not _has_ft(conn):
        return
    rows = conn.execute(
        "SELECT digest FROM texts WHERE digest NOT IN (SELECT digest FROM indexed)"
        " LIMIT ?",
        (_FILL_ROWS,),
    ).fetchall()
    if not rows:
        return
    stop = time.monotonic() + _FILL_BUDGET_S
    for (dig,) in rows:
        if time.monotonic() > stop:
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT text FROM texts WHERE digest=?", (dig,)).fetchone()
            if row is not None:
                _index(conn, dig, row[0])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _chunks(items: list[str]):
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def _snippets(text: str, words: list[str]) -> tuple[str, ...]:
    """Up to two short passages around the matched words."""
    spots: list[tuple[int, int, str]] = []
    for word in words:
        m = re.search(re.escape(word), text, re.IGNORECASE)
        if m:
            spots.append((m.start(), m.end(), word))
    if not spots:
        return ()
    spots.sort()

    def around(start: int, end: int) -> str:
        return _clean(text[max(0, start - 60) : end + 100], _SNIPPET_CHARS)

    first = spots[0]
    out = [around(first[0], first[1])]
    second = None
    far = first[0] + _SNIPPET_CHARS
    for spot in spots[1:]:
        if spot[0] >= far:
            second = spot
            break
    if second is None:
        m = re.compile(re.escape(first[2]), re.IGNORECASE).search(text, far)
        if m:
            second = (m.start(), m.end(), first[2])
    if second is not None:
        extra = around(second[0], second[1])
        if extra and extra not in out:
            out.append(extra)
    return tuple(s for s in out if s)[:_MAX_SNIPPETS]


def search(
    query: str,
    *,
    limit: int = 8,
    since: float = 0.0,
    until: float = 0.0,
    account: str = "",
    source: str = "",
    name: str = "",
) -> list[Hit]:
    """Kept texts that hold the words of ``query`` (and/or whose name does).

    Up to 8 words of two or more characters. Texts holding ALL the words come
    first (and alone); when none does, texts holding ANY word. ``since`` and
    ``until`` bound the seen rows' ``ts``; ``name`` is a case-insensitive part
    of the file name. A text never seen with a sender still matches (empty
    name, sender and subject, ``ts`` 0) unless a filter rules it out. Does not
    touch last-used times. Never raises.
    """
    try:
        return _search(query, limit, since, until, account, source, name)
    except Exception:  # noqa: BLE001 - search must never raise
        return []


def _num(value: object) -> float:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) and n > 0 else 0.0


def _search(query, limit, since, until, account, source, name) -> list[Hit]:
    words = _words(query)
    if not words:
        return []
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 8
    since, until = _num(since), _num(until)
    acct = _account(account)
    src = source if isinstance(source, str) else ""
    part = _clean(name, 200).lower() if isinstance(name, str) else ""
    conn = _connect()
    if conn is None:
        return []
    with closing(conn):
        now = time.time()
        live = (VERSION, now - MAX_AGE_S)
        try:
            _fill(conn)
        except sqlite3.Error:
            pass
        use_ft = _has_ft(conn)
        unindexed = (
            [
                r[0]
                for r in conn.execute(
                    "SELECT digest FROM texts WHERE version=? AND last_used>=?"
                    " AND digest NOT IN (SELECT digest FROM indexed)",
                    live,
                ).fetchall()
            ]
            if use_ft
            else None  # no index: every text is scanned
        )
        matched: dict[str, set[str]] = {}

        def add(word: str, digests) -> None:
            for dig in digests:
                matched.setdefault(dig, set()).add(word)

        for word in words:
            if use_ft:
                try:
                    rows = conn.execute(
                        "SELECT digest FROM indexed WHERE rid IN"
                        " (SELECT rowid FROM ft WHERE ft MATCH ? LIMIT ?)",
                        ('"%s"*' % word, _MAX_CANDIDATES),
                    ).fetchall()
                    add(word, (r[0] for r in rows))
                except sqlite3.Error:
                    pass
                for chunk in _chunks(unindexed or []):
                    marks = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT digest FROM texts WHERE digest IN ({marks})"
                        " AND instr(lower(text), ?)>0",
                        (*chunk, word),
                    ).fetchall()
                    add(word, (r[0] for r in rows))
            else:
                rows = conn.execute(
                    "SELECT digest FROM texts WHERE version=? AND last_used>=?"
                    " AND instr(lower(text), ?)>0 LIMIT ?",
                    (*live, word, _MAX_CANDIDATES),
                ).fetchall()
                add(word, (r[0] for r in rows))
            rows = conn.execute(
                "SELECT DISTINCT digest FROM seen WHERE instr(lower(name), ?)>0"
                " LIMIT ?",
                (word, _MAX_CANDIDATES),
            ).fetchall()
            add(word, (r[0] for r in rows))
        if not matched:
            return []

        candidates = list(matched)
        info: dict[str, tuple] = {}
        seen_rows: dict[str, list[tuple]] = {}
        owners: dict[str, list[str]] = {}
        for chunk in _chunks(candidates):
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                "SELECT digest, kind, how, note, cut FROM texts WHERE digest IN"
                f" ({marks}) AND version=? AND last_used>=?",
                (*chunk, *live),
            ):
                info[r[0]] = r[1:]
            for r in conn.execute(
                "SELECT digest, source, account, name, sender, subject, ts"
                f" FROM seen WHERE digest IN ({marks})",
                chunk,
            ):
                seen_rows.setdefault(r[0], []).append(r[1:])
            for r in conn.execute(
                f"SELECT digest, account FROM owners WHERE digest IN ({marks})",
                chunk,
            ):
                owners.setdefault(r[0], []).append(r[1])

        best: dict[str, tuple] = {}
        for dig in candidates:
            if dig not in info:
                continue
            rows = seen_rows.get(dig)
            if rows is None:
                mine = sorted(owners.get(dig, []), reverse=True)
                rows = [("", mine[0] if mine else "", "", "", "", 0.0)]
                if acct and acct not in mine:
                    continue
            else:
                rows = [
                    r
                    for r in rows
                    if (not acct or r[1] == acct)
                ]
            keep = []
            for r in rows:
                r_src, _r_acct, r_name, _r_sender, _r_subj, r_ts = r
                if src and r_src != src:
                    continue
                if part and part not in r_name.lower():
                    continue
                if since and r_ts < since:
                    continue
                if until and r_ts > until:
                    continue
                keep.append(r)
            if keep:
                best[dig] = max(keep, key=lambda r: r[5])

        if not best:
            return []
        total = len(words)
        full = [d for d in best if len(matched[d]) == total]
        pool = full or list(best)
        pool.sort(key=lambda d: (-len(matched[d]), -best[d][5], d))
        out: list[Hit] = []
        for dig in pool[:limit]:
            kind, how, note, cut = info[dig]
            r_src, r_acct, r_name, r_sender, r_subj, r_ts = best[dig]
            row = conn.execute("SELECT text FROM texts WHERE digest=?", (dig,)).fetchone()
            snippets = _snippets(row[0], sorted(matched[dig])) if row else ()
            out.append(
                Hit(
                    digest=dig,
                    kind=kind,
                    how=how,
                    note=note,
                    cut=bool(cut),
                    name=r_name,
                    sender=r_sender,
                    subject=r_subj,
                    ts=float(r_ts),
                    source=r_src,
                    account=r_acct,
                    snippets=snippets,
                    all_words=bool(full),
                )
            )
        return out


def prune(now: float | None = None) -> int:
    """Drop unused readings (age first, then least recently used by size).

    On success it records "pruned this hour" and clears any claim lease; on
    failure it clears the lease only, so the next attempt is not held off.
    """
    now = time.time() if now is None else float(now)
    try:
        removed = _prune(now)
    except (sqlite3.Error, OSError):
        removed = None
    if removed is None:
        _release_lease()
        return 0
    return removed


def _prune(now: float) -> int:
    conn = _connect()
    if conn is None:
        raise OSError("attachment store unavailable")
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
            orphans += _sweep(conn)
            orphans += conn.execute(
                "DELETE FROM tried WHERE ts < ?", (now - _TRIED_AGE_S,)
            ).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        pending = conn.execute(
            "SELECT 1 FROM meta WHERE key='vacuum_due'"
        ).fetchone()
        if removed or orphans or pending:
            _compact(conn)
            conn.execute("VACUUM")
            conn.execute("DELETE FROM meta WHERE key='vacuum_due'")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_prune', ?)",
            (time.time(),),
        )
        conn.execute("DELETE FROM meta WHERE key='prune_lease'")
        return removed
