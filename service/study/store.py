"""School data on this Mac: courses, assignments, study materials, quiz attempts.

Its own SQLite file under MOE_DIR/study/ (dir 0700, file 0600), like the attention
ledger, so school pages never mix with assistant.db and "Clear" can drop all of it.

Upserts merge: a page that does not show a field (a dashboard has no grade, a grades
page has no description) never blanks what an earlier page saved.

Imports converge (``import_rows``): canvas_parse keys a course by its numeric
Canvas id on course pages but by a name hash on the Dashboard and the all-grades
page, so one course can arrive under two ids. Rows sharing a ``merge_key``
(normalised course name) become ONE course row, the numeric id winning, and the
same assignment seen under both becomes one row keyed by (course, title).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

_COURSE_FIELDS = ("name", "code", "grade", "score", "url", "source", "sample", "merge_key")
_ASSIGNMENT_FIELDS = ("course_id", "title", "kind", "due_ts", "all_day", "points", "score",
                      "status", "details", "url", "source", "sample")
_BOOL_FIELDS = ("all_day", "sample")
DETAILS_MAX = 4000
_NUMERIC_ID = re.compile(r"[a-z]+:(\d+)")
_WS = re.compile(r"\s+")


# Work already handed in: no later page can make it due again.
_DONE_STATUSES = ("submitted", "graded")

def merge_key(name) -> str:
    """Same normalisation as canvas_parse: lower-case, punctuation to spaces, single spaces."""
    return _WS.sub(" ", re.sub(r"[^0-9a-z]+", " ", str(name or "").lower())).strip()


def _numeric(cid, url=None) -> bool:
    """A Canvas numeric course id. A 12-digit id could also be a name hash that happens to be
    all digits, so that length only counts when the row's url names the same course."""
    match = _NUMERIC_ID.fullmatch(cid) if isinstance(cid, str) else None
    if match is None:
        return False
    digits = match.group(1)
    return len(digits) != 12 or bool(url and str(url).rstrip("/").endswith("/courses/" + digits))


_CODE_PREFIX = re.compile(r"[a-z]{2,6} ?\d{1,4}[a-z]?(?: \d{1,3})? (?=\S)")


def _name_keys(key, code=None) -> set[str]:
    """A course's merge_key, plus the same key without a leading course code: a course page
    says "CSE 20: Beginning Programming in Python" where the Dashboard says only the name."""
    key = merge_key(key)
    if not key:
        return set()
    keys = {key}
    prefix = merge_key(code)
    if prefix and key.startswith(prefix + " ") and len(key) > len(prefix) + 1:
        keys.add(key[len(prefix) + 1:])
    match = _CODE_PREFIX.match(key)
    if match:
        keys.add(key[match.end():])
    return keys


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _unsaid_status(status) -> bool:
    return status in (None, "", "upcoming")


def _extended(new_due, old_due) -> bool:
    """The deadline moved later (both known)."""
    return (isinstance(new_due, (int, float)) and isinstance(old_due, (int, float))
            and not isinstance(new_due, bool) and not isinstance(old_due, bool) and new_due > old_due)


def study_dir() -> Path:
    """MOE_DIR/study, resolved at call time so WISP_HOME (tests, the sandbox) is honoured.
    The one source for store, day_hook and connector settings."""
    from service.paths import moe_dir
    return moe_dir() / "study"


def study_path() -> Path:
    return study_dir() / "study.db"


def study_exists() -> bool:
    """Whether this Mac ever saved school data. Reading never creates the file."""
    return study_path().exists()


def _richness(row) -> int:
    return (sum(not _blank(row[k]) for k in ("points", "score", "due_ts"))
            + bool((row["details"] or "").strip()) + (row["status"] not in (None, "", "upcoming")))


class StudyStore:
    def __init__(self, path: Path):
        path = Path(path)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fresh = not path.exists()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if fresh:
            os.chmod(path, 0o600)
        self._lock = threading.Lock()
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS courses (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL DEFAULT '',
                code       TEXT,
                grade      TEXT,
                score      REAL,
                url        TEXT,
                source     TEXT NOT NULL DEFAULT 'canvas',
                sample     INTEGER NOT NULL DEFAULT 0,
                merge_key  TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id         TEXT PRIMARY KEY,
                course_id  TEXT,
                title      TEXT NOT NULL DEFAULT '',
                kind       TEXT NOT NULL DEFAULT 'assignment',
                due_ts     REAL,
                all_day    INTEGER NOT NULL DEFAULT 0,
                points     REAL,
                score      REAL,
                status     TEXT NOT NULL DEFAULT 'upcoming',
                details    TEXT NOT NULL DEFAULT '',
                url        TEXT,
                source     TEXT NOT NULL DEFAULT 'canvas',
                sample     INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS materials (
                id            TEXT PRIMARY KEY,
                kind          TEXT NOT NULL,
                title         TEXT NOT NULL,
                course_id     TEXT,
                assignment_id TEXT,
                created_at    REAL NOT NULL,
                model         TEXT,
                sample        INTEGER NOT NULL DEFAULT 0,
                count         INTEGER NOT NULL DEFAULT 0,
                body          TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                material_id TEXT NOT NULL,
                at          REAL NOT NULL,
                correct     INTEGER NOT NULL,
                total       INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempts_material ON attempts(material_id);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self._migrate()

    def _migrate(self) -> None:
        """A study.db from before merge_key: add the column and fill it from the names."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(courses)")}
        if "merge_key" not in cols:
            try:
                self._db.execute("ALTER TABLE courses ADD COLUMN merge_key TEXT")
            except sqlite3.OperationalError:
                pass   # another opener added it first
        for row in self._db.execute("SELECT id, name FROM courses WHERE merge_key IS NULL").fetchall():
            self._db.execute("UPDATE courses SET merge_key=? WHERE id=?", (merge_key(row["name"]), row["id"]))
        self._db.execute("CREATE INDEX IF NOT EXISTS courses_merge_key ON courses(merge_key)")

    # --- courses and assignments ------------------------------------------------

    def upsert_courses(self, courses: list[dict]) -> int:
        return self._upsert("courses", _COURSE_FIELDS, [_course_in(c) for c in courses], _COURSE_DEFAULTS)

    def upsert_assignments(self, items: list[dict]) -> int:
        return self._upsert("assignments", _ASSIGNMENT_FIELDS, [_assignment_in(i) for i in items],
                            _ASSIGNMENT_DEFAULTS)

    def _upsert(self, table: str, fields: tuple[str, ...], rows: list[dict], defaults: dict) -> int:
        now = time.time()
        written = 0
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for row in rows:
                    written += self._write(table, fields, row, defaults, now)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return written

    def _write(self, table: str, fields: tuple[str, ...], row: dict, defaults: dict, now: float,
               keep: tuple[dict, ...] = ()) -> int:
        """Merge one row by id. ``keep`` are rows being folded into it: they fill blanks only."""
        rid = row.get("id")
        if not isinstance(rid, str) or not rid:
            return 0
        old = self._db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        merged = dict(defaults) | ({k: old[k] for k in fields} if old else {})
        for other in keep:
            for key in fields:
                unsaid = _blank(merged.get(key)) or (key == "status" and merged.get(key) == "upcoming")
                if (key == "status" and unsaid and other[key] not in _DONE_STATUSES
                        and _extended(merged.get("due_ts"), other["due_ts"])):
                    unsaid = False   # the surviving row's later deadline outranks an old "missing"
                if unsaid and not _blank(other[key]):
                    merged[key] = other[key]
        prior_due = merged.get("due_ts")
        for key in fields:
            value = row.get(key)
            # Missing, null or empty text means "this page did not say".
            if _blank(value):
                continue
            if (key == "status" and value == "upcoming" and not _unsaid_status(merged.get(key))
                    and (merged.get(key) in _DONE_STATUSES or not _extended(row.get("due_ts"), prior_due))):
                # Parsers fill "upcoming" when a page shows no status, so it never undoes a
                # stored status. A later deadline reopens missing or late work, but never
                # work already handed in: a date-only page reads as 23:59 and would look later.
                continue
            merged[key] = value
        values = [int(bool(merged[k])) if k in _BOOL_FIELDS else merged.get(k) for k in fields]
        cols = ",".join(("id",) + fields + ("updated_at",))
        marks = ",".join("?" * (len(fields) + 2))
        self._db.execute(f"INSERT OR REPLACE INTO {table}({cols}) VALUES ({marks})", [rid, *values, now])
        return 1

    # --- converging imports -----------------------------------------------------

    def import_rows(self, courses: list[dict], assignments: list[dict]) -> tuple[int, int]:
        """One import: courses converge on one row per merge_key, assignments on one row per
        (canonical course, normalised title). Returns (courses, assignments) distinct rows written."""
        now = time.time()
        alias: dict[str, str] = {}
        course_ids: set[str] = set()
        assignment_ids: set[str] = set()
        touched: set[str] = set()
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for c in map(_course_in, courses):
                    cid = c.get("id")
                    if not isinstance(cid, str) or not cid:
                        continue
                    canonical, retired = self._canonical_course(cid, c.get("url"), c.get("merge_key") or "",
                                                                c.get("code"))
                    keep = [self._db.execute("SELECT * FROM courses WHERE id=?", (r,)).fetchone() for r in retired]
                    keep = [k for k in keep if k is not None]
                    if cid != canonical:
                        # A name-keyed copy of a course the store already has: its name, code and key
                        # only fill blanks, so "CSE 20: Beginning Programming" is not renamed back and forth.
                        names = {k: c.pop(k, None) for k in ("name", "code", "merge_key")}
                        keep.append({k: names.get(k) for k in _COURSE_FIELDS})
                    self._write("courses", _COURSE_FIELDS, c | {"id": canonical}, _COURSE_DEFAULTS, now,
                                keep=tuple(keep))
                    for old in retired:
                        self._retire_course(old, canonical)
                    for old in (*retired, cid):
                        if old != canonical:
                            alias[old] = canonical
                    for old, target in list(alias.items()):   # an earlier alias may name a retired id
                        if target in retired:
                            alias[old] = canonical
                    course_ids.add(canonical)
                    if retired:
                        touched.add(canonical)
                for a in map(_assignment_in, assignments):
                    aid = a.get("id")
                    if not isinstance(aid, str) or not aid:
                        continue
                    course_id = alias.get(a.get("course_id"), a.get("course_id"))
                    a = a | {"course_id": course_id}
                    stored = self._db.execute("SELECT 1 FROM assignments WHERE id=?", (aid,)).fetchone()
                    twin = None if stored else self._twin(course_id, a.get("title"), aid)
                    if twin is not None:
                        a = a | {"id": twin["id"]}   # _write keeps the twin's grade (see _unsaid_status)
                    self._write("assignments", _ASSIGNMENT_FIELDS, a, _ASSIGNMENT_DEFAULTS, now)
                    assignment_ids.add(a["id"])
                    if course_id:
                        touched.add(course_id)
                for course_id in touched:
                    self._dedupe_assignments(course_id, now)
                kept = sum(self._db.execute("SELECT 1 FROM assignments WHERE id=?", (a,)).fetchone() is not None
                           for a in assignment_ids)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return len({alias.get(c, c) for c in course_ids}), kept

    def _canonical_course(self, cid: str, url, key: str, code=None) -> tuple[str, list[str]]:
        """(the id to write, stored ids folded into it)."""
        keys = _name_keys(key, code)
        if not keys:
            return cid, []
        rows = [r for r in self._db.execute(
                    "SELECT id, url, code, merge_key FROM courses WHERE sample=0 AND id<>? AND merge_key<>''"
                    " ORDER BY updated_at, id", (cid,)).fetchall()
                if keys & _name_keys(r["merge_key"], r["code"])]
        numeric = [r for r in rows if _numeric(r["id"], r["url"])]
        hashed = [r["id"] for r in rows if r not in numeric]
        stored = self._db.execute("SELECT url FROM courses WHERE id=?", (cid,)).fetchone()
        if _numeric(cid, url or (stored["url"] if stored else None)):
            # Two numeric ids with one name are two real courses (another term): never merged.
            return cid, hashed
        if len(numeric) > 1:
            # One name, several real courses: only an exact name picks one, else stay apart.
            exact = [r for r in numeric if r["merge_key"] == merge_key(key)]
            if len(exact) != 1:
                return cid, []
            numeric = exact
        if numeric:
            return numeric[0]["id"], [cid] if stored is not None else []
        if hashed:
            return hashed[0], [cid] if stored is not None else []
        return cid, []

    def _retire_course(self, old: str, canonical: str) -> None:
        self._db.execute("UPDATE assignments SET course_id=? WHERE course_id=?", (canonical, old))
        self._db.execute("UPDATE materials SET course_id=? WHERE course_id=?", (canonical, old))
        self._db.execute("DELETE FROM courses WHERE id=?", (old,))

    def _twin(self, course_id, title, aid: str):
        if not course_id:
            return None
        key = merge_key(title)
        for row in self._db.execute("SELECT * FROM assignments WHERE course_id=? AND id<>? ORDER BY updated_at, id",
                                    (course_id, aid)).fetchall():
            if key and merge_key(row["title"]) == key:
                return row
        return None

    def _dedupe_assignments(self, course_id: str, now: float) -> None:
        """Rows of one course with the same normalised title become the richest one."""
        groups: dict[str, list] = {}
        for row in self._db.execute("SELECT * FROM assignments WHERE course_id=? ORDER BY updated_at, id",
                                    (course_id,)).fetchall():
            key = merge_key(row["title"])
            if key:
                groups.setdefault(key, []).append(row)
        for rows in groups.values():
            if len(rows) < 2:
                continue
            survivor = max(rows, key=_richness)   # max keeps the first (oldest) on a tie
            rest = tuple(r for r in rows if r["id"] != survivor["id"])
            self._write("assignments", _ASSIGNMENT_FIELDS, {"id": survivor["id"]}, _ASSIGNMENT_DEFAULTS, now,
                        keep=rest)
            for r in rest:
                self._db.execute("UPDATE materials SET assignment_id=? WHERE assignment_id=?",
                                 (survivor["id"], r["id"]))
                self._db.execute("DELETE FROM assignments WHERE id=?", (r["id"],))

    def courses(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM courses ORDER BY sample, name COLLATE NOCASE").fetchall()
        return [_plain(r) for r in rows]

    def assignments(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM assignments ORDER BY due_ts IS NULL, due_ts, title COLLATE NOCASE").fetchall()
        return [_plain(r) for r in rows]

    # --- materials --------------------------------------------------------------

    def save_material(self, material: dict) -> dict:
        body = material.get("body") or {}
        mid = material.get("id") or "m_" + secrets.token_hex(6)
        created = material.get("created_at")
        created = float(created) if created is not None else time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO materials(id,kind,title,course_id,assignment_id,created_at,model,"
                "sample,count,body) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, material["kind"], material.get("title") or "", material.get("course_id"),
                 material.get("assignment_id"), created, material.get("model"),
                 int(bool(material.get("sample"))), _count(material["kind"], body),
                 json.dumps(body, ensure_ascii=False)))
        return self.material(mid)

    def material(self, material_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(_MATERIAL_SELECT + " WHERE m.id=?", (material_id,)).fetchone()
        if row is None:
            return None
        out = _material(row)
        out["model"] = row["model"]
        out["body"] = json.loads(row["body"])
        return out

    def materials(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(_MATERIAL_SELECT + " ORDER BY m.created_at DESC, m.id").fetchall()
        return [_material(r) for r in rows]

    def delete_material(self, material_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM materials WHERE id=?", (material_id,))
            self._db.execute("DELETE FROM attempts WHERE material_id=?", (material_id,))
        return cur.rowcount > 0

    def record_attempt(self, material_id: str, correct: int, total: int) -> float | None:
        """The best score so far (0..1), or None for an unknown material or an empty try."""
        if total <= 0 or correct < 0:
            return None
        correct = min(int(correct), int(total))
        with self._lock:
            if self._db.execute("SELECT 1 FROM materials WHERE id=?", (material_id,)).fetchone() is None:
                return None
            self._db.execute("INSERT INTO attempts(material_id,at,correct,total) VALUES (?,?,?,?)",
                             (material_id, time.time(), correct, int(total)))
            row = self._db.execute(
                "SELECT MAX(CAST(correct AS REAL)/total) FROM attempts WHERE material_id=?",
                (material_id,)).fetchone()
        return row[0]

    # --- meta and clearing ------------------------------------------------------

    def meta(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, json.dumps(value)))

    def has_sample(self) -> bool:
        with self._lock:
            return any(self._db.execute(f"SELECT 1 FROM {t} WHERE sample=1 LIMIT 1").fetchone()
                       for t in ("courses", "assignments", "materials"))

    def clear(self) -> None:
        with self._lock:
            self._db.executescript("BEGIN; DELETE FROM courses; DELETE FROM assignments; DELETE FROM materials;"
                                   " DELETE FROM attempts; DELETE FROM meta; COMMIT;")

    def clear_sample(self) -> None:
        """Only the built-in sample: real courses and anything made from them stay."""
        with self._lock:
            self._db.executescript(
                "BEGIN;"
                " DELETE FROM attempts WHERE material_id IN (SELECT id FROM materials WHERE sample=1);"
                " DELETE FROM materials WHERE sample=1;"
                " DELETE FROM assignments WHERE sample=1;"
                " DELETE FROM courses WHERE sample=1;"
                " COMMIT;")


_COURSE_DEFAULTS = {"name": "", "source": "canvas", "sample": False}
_ASSIGNMENT_DEFAULTS = {"title": "", "kind": "assignment", "all_day": False, "status": "upcoming",
                        "details": "", "source": "canvas", "sample": False}


def _course_in(course: dict) -> dict:
    course = dict(course)
    if _blank(course.get("merge_key")):
        course["merge_key"] = merge_key(course.get("name")) or None
    return course


def _assignment_in(item: dict) -> dict:
    item = dict(item)
    if isinstance(item.get("details"), str):
        item["details"] = item["details"].strip()[:DETAILS_MAX]
    return item


_MATERIAL_SELECT = (
    "SELECT m.id, m.kind, m.title, m.course_id, m.assignment_id, m.created_at, m.model, m.sample, m.count,"
    " m.body, (SELECT MAX(CAST(a.correct AS REAL)/a.total) FROM attempts a WHERE a.material_id=m.id)"
    " AS best_score FROM materials m")


def _count(kind: str, body: dict) -> int:
    key = {"guide": "sections", "quiz": "questions", "lesson": "steps", "flashcards": "cards"}.get(kind)
    value = body.get(key) if key else None
    return len(value) if isinstance(value, list) else 0


def _plain(row: sqlite3.Row) -> dict:
    out = dict(row)
    out.pop("updated_at", None)
    for key in _BOOL_FIELDS:
        if key in out:
            out[key] = bool(out[key])
    return out


def _material(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "title": row["title"], "course_id": row["course_id"],
            "assignment_id": row["assignment_id"], "created_at": row["created_at"],
            "count": row["count"], "best_score": row["best_score"], "sample": bool(row["sample"])}


_STORE: StudyStore | None = None
_STORE_LOCK = threading.Lock()


def study_store() -> StudyStore:
    """Opened (and created) on first use, so importing this module never touches MOE_DIR.
    Only routes that save something call this; readers check study_exists() first."""
    global _STORE
    path = study_path()
    with _STORE_LOCK:
        if _STORE is None or _STORE.path != path:
            _STORE = StudyStore(path)
        return _STORE
