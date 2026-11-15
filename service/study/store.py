"""School data on this Mac: courses, assignments, study materials, quiz attempts and
sources (text the student chose to study from: Google Drive files and school pages).

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
from datetime import datetime
from pathlib import Path

from service.study.course_names import clean_name, course_key, merge_key, same_course
from service.study.plan import title_key

_COURSE_FIELDS = ("name", "code", "grade", "score", "url", "source", "sample", "merge_key")
_ASSIGNMENT_FIELDS = ("course_id", "title", "kind", "due_ts", "all_day", "points", "score",
                      "status", "details", "url", "source", "sample")
_BOOL_FIELDS = ("all_day", "sample")
DETAILS_MAX = 4000
SOURCE_CHARS = 400_000      # a source's text is cut to this before it is saved
# "drive" and "page" are the Drive picker's and the school-page import's; the rest are what
# Find materials adds (service/study/find.py).
SOURCE_KINDS = ("drive", "page", "slides", "doc", "pdf", "notebook", "article", "discussion", "file",
                "video", "other")
_SOURCE_TITLE = 300
_SOURCE_URL = 4000
_NUMERIC_ID = re.compile(r"[a-z]+:(\d+)")
_WS = re.compile(r"\s+")


# Work already handed in: no later page can make it due again.
_DONE_STATUSES = ("submitted", "graded")

def _numeric(cid, url=None) -> bool:
    """A Canvas numeric course id. A 12-digit id could also be a name hash that happens to be
    all digits, so that length only counts when the row's url names the same course."""
    match = _NUMERIC_ID.fullmatch(cid) if isinstance(cid, str) else None
    if match is None:
        return False
    digits = match.group(1)
    return len(digits) != 12 or bool(url and str(url).rstrip("/").endswith("/courses/" + digits))


def _local_day(ts):
    """The Mac's local calendar day of a due time (None when it is not a usable time)."""
    try:
        return datetime.fromtimestamp(float(ts)).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


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
            CREATE TABLE IF NOT EXISTS sources (
                id         TEXT PRIMARY KEY,
                course_id  TEXT,
                kind       TEXT NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                url        TEXT,
                text       TEXT NOT NULL DEFAULT '',
                chars      INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                UNIQUE (kind, url)
            );
            CREATE INDEX IF NOT EXISTS sources_course ON sources(course_id);
            CREATE TABLE IF NOT EXISTS deck_pages (
                source_id TEXT NOT NULL,
                page      INTEGER NOT NULL,
                title     TEXT NOT NULL DEFAULT '',
                points    TEXT NOT NULL DEFAULT '[]',
                figure    TEXT,
                topics    TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (source_id, page)
            );
            CREATE TABLE IF NOT EXISTS misses (
                material_id TEXT NOT NULL,
                question    INTEGER NOT NULL,
                source_id   TEXT,
                page        INTEGER,
                topic       TEXT NOT NULL DEFAULT '',
                at          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS misses_material ON misses(material_id);
        """)
        self._migrate()
        self._repair_nameless()

    def _repair_nameless(self) -> None:
        """Open-time repair of a store split before nameless course pages could merge (see
        ``_adopt_nameless``), or before names with a course code, section or "&" could
        (``_fold_named``, ``_adopt_by_assignments``). Idempotent and never fatal: a store it
        cannot repair still opens."""
        with self._lock:
            try:
                self._db.execute("BEGIN")
                now = time.time()
                for canonical in self._adopt_nameless(now) | self._fold_named(now) | self._adopt_by_assignments(now):
                    self._dedupe_assignments(canonical, now)
                self._db.execute("COMMIT")
            except sqlite3.Error:
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

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
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(sources)")}
        if "deck_status" not in cols:
            try:
                self._db.execute("ALTER TABLE sources ADD COLUMN deck_status TEXT NOT NULL DEFAULT 'none'")
            except sqlite3.OperationalError:
                pass   # another opener added it first
        # A page read cut short by a restart never finishes: show it as failed, not "reading" forever.
        if "deck_status" in {r[1] for r in self._db.execute("PRAGMA table_info(sources)")}:
            self._db.execute("UPDATE sources SET deck_status='failed' WHERE deck_status='reading'")
        # Find materials' labels (a study.db from before them reads null).
        for column, kind in (("week", "INTEGER"), ("topic", "TEXT"), ("summary", "TEXT")):
            if column not in cols:
                try:
                    self._db.execute(f"ALTER TABLE sources ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError:
                    pass   # another opener added it first

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
                                                                c.get("code"), c.get("name"))
                    keep = [self._db.execute("SELECT * FROM courses WHERE id=?", (r,)).fetchone() for r in retired]
                    keep = [k for k in keep if k is not None]
                    stored = self._db.execute("SELECT name, code FROM courses WHERE id=?", (canonical,)).fetchone()
                    mine = [(c.get("name"), c.get("code"))]
                    theirs = [(stored["name"], stored["code"])] if stored is not None else []
                    # The merged course shows the name without a course code or section ("Writing and
                    # Rhetoric: ..." over "WRIT 1 Writing and Rhetoric: ... Section 09"), whichever
                    # page brought it, so no later read renames it back and forth.
                    shown = clean_name((mine + theirs if cid == canonical else theirs + mine)
                                       + [(k["name"], k["code"]) for k in keep])
                    if cid != canonical:
                        # A name-keyed copy of a course the store already has: its name, code and key
                        # only fill blanks, so "CS 101: Introduction to Programming" is not renamed back and forth.
                        names = {k: c.pop(k, None) for k in ("name", "code", "merge_key")}
                        keep.append({k: names.get(k) for k in _COURSE_FIELDS})
                    if shown and shown != (c.get("name") if cid == canonical else (stored["name"] if stored else None)):
                        c["name"], c["merge_key"] = shown, merge_key(shown)
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
                touched |= self._adopt_nameless(now)
                touched |= self._adopt_by_assignments(now)
                for course_id in touched:
                    self._dedupe_assignments(course_id, now)
                kept = sum(self._db.execute("SELECT 1 FROM assignments WHERE id=?", (a,)).fetchone() is not None
                           for a in assignment_ids)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return len({alias.get(c, c) for c in course_ids}), kept

    def _canonical_course(self, cid: str, url, key: str, code=None, name=None) -> tuple[str, list[str]]:
        """(the id to write, stored ids folded into it). Names match by ``course_names.same_course``."""
        mine = course_key(name or key, code)
        if not mine.keys:
            return cid, []
        rows = [r for r in self._db.execute(
                    "SELECT id, name, url, code, merge_key FROM courses WHERE sample=0 AND id<>? AND merge_key<>''"
                    " ORDER BY updated_at, id", (cid,)).fetchall()
                if same_course(mine, course_key(r["name"] or r["merge_key"], r["code"]))]
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

    def _adopt_nameless(self, now: float) -> set[str]:
        """A course page that showed no course name saves a nameless row under its numeric
        id and address, which no name can match to the Dashboard's row for the same course.
        Such a row takes in the one url-less named row with the same score: the numeric id
        survives (as in ``_canonical_course``), gaining the name and code; the named row is
        retired and its assignments, materials and sources move over. Nothing happens unless
        the match is one-to-one both ways and nothing contradicts it (two codes or two letter
        grades that differ). Returns the surviving ids."""
        rows = self._db.execute("SELECT * FROM courses WHERE sample=0 ORDER BY updated_at, id").fetchall()
        nameless = [r for r in rows if _blank(r["name"]) and r["score"] is not None and not _blank(r["url"])
                    and _numeric(r["id"], r["url"])]
        named = [r for r in rows if not _blank(r["name"]) and _blank(r["url"]) and r["score"] is not None]

        def agree(a, b) -> bool:
            if a["score"] != b["score"]:
                return False
            return not any(not _blank(a[k]) and not _blank(b[k]) and str(a[k]).strip() != str(b[k]).strip()
                           for k in ("code", "grade"))

        adopted: set[str] = set()
        for row in nameless:
            matches = [n for n in named if agree(row, n)]
            if len(matches) != 1 or sum(agree(other, matches[0]) for other in nameless) != 1:
                continue
            twin = matches[0]
            self._write("courses", _COURSE_FIELDS, {"id": row["id"]}, _COURSE_DEFAULTS, now, keep=(twin,))
            self._retire_course(twin["id"], row["id"])
            named.remove(twin)
            adopted.add(row["id"])
        return adopted

    def _fold_into(self, target, other, now: float) -> None:
        """Fold stored course ``other`` into stored course ``target`` (the id that survives):
        ``other`` fills blanks only, the display name is the one without a code or section."""
        row = {"id": target["id"]}
        shown = clean_name([(target["name"], target["code"]), (other["name"], other["code"])])
        if shown and shown != target["name"]:
            row |= {"name": shown, "merge_key": merge_key(shown)}
        self._write("courses", _COURSE_FIELDS, row, _COURSE_DEFAULTS, now, keep=(other,))
        self._retire_course(other["id"], target["id"])

    def _fold_named(self, now: float) -> set[str]:
        """Stored rows that ``_canonical_course`` now calls one course (a store split before
        names with a course code, section or "&" could match) become one, as an import would
        fold them. Only id-by-name rows are folded; numeric ids never fold into each other."""
        out: set[str] = set()
        for row in self._db.execute("SELECT * FROM courses WHERE sample=0 AND name<>'' ORDER BY updated_at, id"
                                    ).fetchall():
            if _numeric(row["id"], row["url"]):
                continue
            canonical, _ = self._canonical_course(row["id"], row["url"], row["merge_key"] or "", row["code"],
                                                  row["name"])
            target = self._db.execute("SELECT * FROM courses WHERE id=?", (canonical,)).fetchone()
            if canonical == row["id"] or target is None:
                continue
            self._fold_into(target, row, now)
            out.add(canonical)
        return out

    def _adopt_by_assignments(self, now: float) -> set[str]:
        """A url-less row (the Dashboard's, keyed by a name hash) whose name still matches no
        course page is that course page's row when EVERY one of its dated assignments (at least
        one) is also in the numeric row, by normalised title and local due day; the pairing is
        one-to-one across all candidate pairs (no other numeric row holds them all, no other
        url-less row is wholly held by that numeric row); and nothing contradicts it (codes naming
        another subject+number, two scores or two letter grades that differ, two names that
        both carry no course code or section and still differ). One shared
        assignment alone never decides it while the url-less row has others, and an assignment
        two courses share leaves both apart. Returns the surviving ids."""
        rows = self._db.execute("SELECT * FROM courses WHERE sample=0 ORDER BY updated_at, id").fetchall()
        numeric = [r for r in rows if not _blank(r["url"]) and _numeric(r["id"], r["url"])]
        urlless = [r for r in rows if _blank(r["url"]) and not _numeric(r["id"], r["url"])]
        if not numeric or not urlless:
            return set()
        dated: dict[str, set] = {}
        for a in self._db.execute("SELECT course_id, title, due_ts FROM assignments"
                                  " WHERE due_ts IS NOT NULL AND course_id IS NOT NULL").fetchall():
            key, day = merge_key(title_key(a["title"])), _local_day(a["due_ts"])
            if key and day is not None:
                dated.setdefault(a["course_id"], set()).add((key, day))

        def agree(a, b) -> bool:
            ca, cb = course_key(a["name"], a["code"]).course, course_key(b["name"], b["code"]).course
            if ca and cb and ca != cb:
                return False
            if a["score"] is not None and b["score"] is not None and a["score"] != b["score"]:
                return False
            if not _blank(a["grade"]) and not _blank(b["grade"]) and str(a["grade"]).strip() != str(b["grade"]).strip():
                return False
            ka, kb = course_key(a["name"], a["code"]), course_key(b["name"], b["code"])
            if ka.keys and kb.keys and ka.clean and kb.clean and not same_course(ka, kb):
                return False   # two plain names that differ: the course page already took a Dashboard name
            return _blank(a["url"]) or _blank(b["url"])

        pairs = [(u, n) for u in urlless for n in numeric
                 if dated.get(u["id"]) and dated[u["id"]] <= dated.get(n["id"], set()) and agree(u, n)]
        adopted: set[str] = set()
        for u, n in pairs:
            if sum(p[0]["id"] == u["id"] for p in pairs) != 1 or sum(p[1]["id"] == n["id"] for p in pairs) != 1:
                continue
            self._fold_into(n, u, now)
            adopted.add(n["id"])
        return adopted

    def _retire_course(self, old: str, canonical: str) -> None:
        self._db.execute("UPDATE assignments SET course_id=? WHERE course_id=?", (canonical, old))
        self._db.execute("UPDATE materials SET course_id=? WHERE course_id=?", (canonical, old))
        self._db.execute("UPDATE sources SET course_id=? WHERE course_id=?", (canonical, old))
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
            self._db.execute("DELETE FROM misses WHERE material_id=?", (material_id,))
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

    # --- sources ----------------------------------------------------------------

    def save_source(self, source: dict) -> dict:
        """Save text the student chose to study from. The same kind and url again replaces
        its text, title and course (and refreshes created_at) but keeps its id. Returns the
        summary (no text). The text is untrusted data: stored as-is, cut to SOURCE_CHARS."""
        kind = source.get("kind")
        if kind not in SOURCE_KINDS:
            raise ValueError("unknown source kind")
        text = source.get("text")
        text = text.replace("\x00", "")[:SOURCE_CHARS] if isinstance(text, str) else ""
        title = source.get("title")
        title = title.strip()[:_SOURCE_TITLE] if isinstance(title, str) else ""
        url = source.get("url")
        url = (url.strip()[:_SOURCE_URL] or None) if isinstance(url, str) else None
        course_id = source.get("course_id")
        course_id = course_id if isinstance(course_id, str) and course_id else None
        now = time.time()
        replace = source.get("id")
        cols = "id, deck_status, week, topic, summary"
        with self._lock:
            old = None
            if isinstance(replace, str) and replace:
                # Find materials re-adding a source it knows by Drive id or canonical url: that row
                # keeps its id even when the kind or the url's spelling changed.
                old = self._db.execute(f"SELECT {cols} FROM sources WHERE id=?", (replace,)).fetchone()
            if old is None and url is not None:
                old = self._db.execute(f"SELECT {cols} FROM sources WHERE kind=? AND url=?",
                                       (kind, url)).fetchone()
            sid = old["id"] if old else "src_" + secrets.token_hex(6)
            if url is not None:
                # Another row already holding this (kind, url) would be dropped by the REPLACE.
                twin = self._db.execute("SELECT id FROM sources WHERE kind=? AND url=? AND id<>?",
                                        (kind, url, sid)).fetchone()
                if twin is not None:
                    self._db.execute("DELETE FROM sources WHERE id=?", (twin["id"],))
                    self._db.execute("DELETE FROM deck_pages WHERE source_id=?", (twin["id"],))
            # Re-adding keeps the pages read earlier (and their status and labels) until new ones
            # replace them.
            deck_status = old["deck_status"] if old else "none"
            labels = (old["week"], old["topic"], old["summary"]) if old else (None, None, None)
            self._db.execute(
                "INSERT OR REPLACE INTO sources(id,course_id,kind,title,url,text,chars,created_at,deck_status,"
                "week,topic,summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, course_id, kind, title, url, text, len(text), now, deck_status, *labels))
            row = self._db.execute(_SOURCE_SELECT + " WHERE id=?", (sid,)).fetchone()
        return dict(row)

    def set_source_text(self, source_id: str, text: str) -> None:
        """Replace one source's text (a PDF's text built from its pages), cut to SOURCE_CHARS."""
        text = text.replace("\x00", "")[:SOURCE_CHARS] if isinstance(text, str) else ""
        with self._lock:
            self._db.execute("UPDATE sources SET text=?, chars=? WHERE id=?", (text, len(text), source_id))

    def set_labels(self, source_id: str, *, week=None, topic=None, summary=None) -> bool:
        """Find materials' labels for one source: week (1..60, else None), topic, one-line summary."""
        if isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 60:
            week = None
        topic = (_WS.sub(" ", topic).strip()[:_TOPIC] or None) if isinstance(topic, str) else None
        summary = (_WS.sub(" ", summary).strip()[:_SUMMARY] or None) if isinstance(summary, str) else None
        with self._lock:
            cur = self._db.execute("UPDATE sources SET week=?, topic=?, summary=? WHERE id=?",
                                   (week, topic, summary, source_id))
        return cur.rowcount > 0

    def sources(self, course_id: str | None = None) -> list[dict]:
        """Summaries (no text), newest first; course_id filters, None means all."""
        with self._lock:
            if course_id is None:
                rows = self._db.execute(_SOURCE_SELECT + " ORDER BY created_at DESC, id").fetchall()
            else:
                rows = self._db.execute(_SOURCE_SELECT + " WHERE course_id=? ORDER BY created_at DESC, id",
                                        (course_id,)).fetchall()
        out = [dict(r) for r in rows]
        if any(r.get("deck_status") == "failed" for r in out):
            errors = self.meta("deck_errors", {}) or {}
            for r in out:
                if r.get("deck_status") == "failed":
                    r["deck_error"] = errors.get(r["id"]) or DECK_ERROR
        return out

    def set_deck_error(self, source_id: str, message: str | None) -> None:
        """The plain-language reason a deck could not be read (key-free), or None to clear it."""
        errors = dict(self.meta("deck_errors", {}) or {})
        if message:
            errors[source_id] = str(message)[:300]
        else:
            errors.pop(source_id, None)
        self.set_meta("deck_errors", errors)

    def source(self, source_id: str) -> dict | None:
        """The full row, with "text"."""
        with self._lock:
            row = self._db.execute(_SOURCE_SELECT.replace(" FROM sources", ", text FROM sources") + " WHERE id=?",
                                   (source_id,)).fetchone()
        return dict(row) if row else None

    def delete_source(self, source_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM sources WHERE id=?", (source_id,))
            self._db.execute("DELETE FROM deck_pages WHERE source_id=?", (source_id,))
        return cur.rowcount > 0

    # --- deck pages -------------------------------------------------------------

    def save_pages(self, source_id: str, pages: list[dict]) -> int:
        """Replace a source's pages (deck.validate_pages' shape) and mark its deck "ready"
        (or "none" for an empty list). An unknown source saves nothing. Returns pages saved."""
        rows = []
        seen: set[int] = set()
        for item in pages or []:
            row = _page_in(item)
            if row is not None and row[0] not in seen and len(rows) < PAGE_MAX:
                seen.add(row[0])
                rows.append(row)
        with self._lock:
            if self._db.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone() is None:
                return 0
            self._db.execute("BEGIN")
            try:
                self._db.execute("DELETE FROM deck_pages WHERE source_id=?", (source_id,))
                self._db.executemany(
                    "INSERT INTO deck_pages(source_id,page,title,points,figure,topics) VALUES (?,?,?,?,?,?)",
                    [(source_id, *row) for row in rows])
                self._db.execute("UPDATE sources SET deck_status=? WHERE id=?",
                                 ("ready" if rows else "none", source_id))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return len(rows)

    def set_deck_status(self, source_id: str, status: str) -> None:
        """"none" | "reading" | "ready" | "failed". "failed" keeps any pages an earlier read saved."""
        if status not in DECK_STATUSES:
            raise ValueError("unknown deck status")
        with self._lock:
            self._db.execute("UPDATE sources SET deck_status=? WHERE id=?", (status, source_id))

    def pages(self, source_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM deck_pages WHERE source_id=? ORDER BY page",
                                    (source_id,)).fetchall()
        return [_page_out(r) for r in rows]

    def search_pages(self, query: str, *, course_id: str | None = None, limit: int = 8) -> list[dict]:
        """Local keyword search over every deck page: no model, no network. A query word in a
        page's title or topics counts 3, in the deck's title 2, in its points or figure 1;
        a word is matched lower-cased and lightly stemmed ("pointers" finds "pointer").
        Best first, then deck title and page order. A score of 0 is never returned."""
        words = set(_terms(query))
        if not words:
            return []
        limit = max(1, min(int(limit), 100))
        sql = ("SELECT p.*, s.title AS deck, s.course_id AS course_id FROM deck_pages p"
               " JOIN sources s ON s.id = p.source_id")
        with self._lock:
            if course_id is None:
                rows = self._db.execute(sql).fetchall()
            else:
                rows = self._db.execute(sql + " WHERE s.course_id=?", (course_id,)).fetchall()
        deck_terms: dict[str, set[str]] = {}
        found = []
        for row in rows:
            page = _page_out(row)
            deck = row["deck"] or ""
            if deck not in deck_terms:
                deck_terms[deck] = set(_terms(deck))
            fields = ((3, set(_terms(page["title"]))), (3, set(_terms(" ".join(page["topics"])))),
                      (2, deck_terms[deck]), (1, set(_terms(" ".join(page["points"])))),
                      (1, set(_terms(page["figure"] or ""))))
            score = sum(weight * len(words & terms) for weight, terms in fields)
            if score <= 0:
                continue
            found.append(page | {"source_id": row["source_id"], "deck": deck, "course_id": row["course_id"],
                                 "score": float(score)})
        found.sort(key=lambda p: (-p["score"], p["deck"].casefold(), p["source_id"], p["page"]))
        return found[:limit]

    # --- misses -----------------------------------------------------------------

    def record_misses(self, material_id: str, rows: list[dict]) -> int:
        """The questions a quiz attempt got wrong, with each one's slide and topic."""
        now = time.time()
        clean = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            question = row.get("question")
            if isinstance(question, bool) or not isinstance(question, int) or question < 0:
                continue
            source_id = row.get("source_id")
            source_id = source_id if isinstance(source_id, str) and source_id else None
            page = row.get("page")
            page = page if isinstance(page, int) and not isinstance(page, bool) and page >= 1 else None
            topic = row.get("topic")
            topic = topic.strip()[:_TOPIC] if isinstance(topic, str) else ""
            clean.append((material_id, question, source_id, page, topic, now))
        if not clean:
            return 0
        with self._lock:
            if self._db.execute("SELECT 1 FROM materials WHERE id=?", (material_id,)).fetchone() is None:
                return 0
            self._db.executemany(
                "INSERT INTO misses(material_id,question,source_id,page,topic,at) VALUES (?,?,?,?,?,?)", clean)
        return len(clean)

    def misses(self, course_id: str | None = None, since: float | None = None) -> list[dict]:
        """Missed questions, oldest first, each with its material's course_id."""
        sql = ("SELECT x.material_id, x.question, x.source_id, x.page, x.topic, x.at, m.course_id"
               " FROM misses x JOIN materials m ON m.id = x.material_id WHERE 1=1")
        args: list = []
        if course_id is not None:
            sql += " AND m.course_id=?"
            args.append(course_id)
        if since is not None:
            sql += " AND x.at>=?"
            args.append(float(since))
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY x.at, x.material_id, x.question", args).fetchall()
        return [dict(r) for r in rows]

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
                                   " DELETE FROM attempts; DELETE FROM meta; DELETE FROM sources;"
                                   " DELETE FROM deck_pages; DELETE FROM misses; COMMIT;")

    def clear_sample(self) -> None:
        """Only the built-in sample: real courses, sources and anything made from them stay."""
        with self._lock:
            self._db.executescript(
                "BEGIN;"
                " DELETE FROM attempts WHERE material_id IN (SELECT id FROM materials WHERE sample=1);"
                " DELETE FROM misses WHERE material_id IN (SELECT id FROM materials WHERE sample=1);"
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


_SOURCE_SELECT = ("SELECT id, course_id, kind, title, url, chars, created_at,"
                  " (SELECT COUNT(*) FROM deck_pages p WHERE p.source_id = sources.id) AS pages,"
                  " deck_status, week, topic, summary FROM sources")

DECK_STATUSES = ("none", "reading", "ready", "failed")
DECK_ERROR = "Wisp couldn't read the slides."
_SUMMARY = 300
PAGE_MAX = 500          # pages kept per source
_PAGE_TITLE = 200
_POINT = 500
_POINTS = 6
_FIGURE = 1000
_TOPIC = 80
_TOPICS = 5
_WORD = re.compile(r"[0-9a-z]+")
_STOP = frozenset("a an and are as at be by do for from how in is it of on or the to what when where which who"
                  " why with this that these those about me my i you your we our".split())


def _stem(word: str) -> str:
    """Stemming-lite: enough that plural, -ing and -ed forms meet ("pointers", "pointer")."""
    for suffix in ("ies", "ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
    return word


def _terms(text) -> list[str]:
    return [_stem(w) for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]


def _text_list(value, cap: int, size: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(_WS.sub(" ", item).strip()[:size])
    return out[:cap]


def _page_in(item) -> tuple | None:
    """(page, title, points json, figure, topics json), or None for a row that is not a page."""
    if not isinstance(item, dict):
        return None
    page = item.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10_000:
        return None
    title = item.get("title")
    title = _WS.sub(" ", title).strip()[:_PAGE_TITLE] if isinstance(title, str) else ""
    figure = item.get("figure")
    figure = (_WS.sub(" ", figure).strip()[:_FIGURE] or None) if isinstance(figure, str) else None
    points = _text_list(item.get("points"), _POINTS, _POINT)
    topics = _text_list(item.get("topics"), _TOPICS, _TOPIC)
    return (page, title, json.dumps(points, ensure_ascii=False), figure, json.dumps(topics, ensure_ascii=False))


def _page_out(row: sqlite3.Row) -> dict:
    return {"source_id": row["source_id"], "page": row["page"], "title": row["title"],
            "points": json.loads(row["points"]), "figure": row["figure"], "topics": json.loads(row["topics"])}


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
