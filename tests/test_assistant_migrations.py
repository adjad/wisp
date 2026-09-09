"""C-2 upgrade/recovery fixtures; never open the user's AssistantStore.

Run directly with Python or collect with pytest. Historical schemas are defined
independently of the production schema so a positional-copy regression fails.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

_scratch = tempfile.TemporaryDirectory(prefix="wisp-assistant-migrations-")
os.environ["WISP_HOME"] = _scratch.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.assistant import store as store_module  # noqa: E402
from service.assistant.reminders import due_reminders  # noqa: E402

AssistantStore = store_module.AssistantStore

BASE_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"), ("source", "TEXT NOT NULL"),
    ("source_id", "TEXT"), ("kind", "TEXT NOT NULL"),
    ("title", "TEXT NOT NULL"), ("context", "TEXT"),
    ("when_ts", "REAL"), ("all_day", "INTEGER DEFAULT 0"),
    ("location", "TEXT"), ("url", "TEXT"),
    ("status", "TEXT DEFAULT 'active'"), ("confidence", "REAL DEFAULT 1.0"),
    ("created_at", "REAL"), ("updated_at", "REAL"),
]
LAYOUTS = {
    "14": BASE_COLUMNS,
    "15-organizer-appended": BASE_COLUMNS + [("organizer", "TEXT")],
    "15-account-appended": BASE_COLUMNS + [("account", "TEXT")],
    "15-organizer-inline": BASE_COLUMNS[:6] + [("organizer", "TEXT")] + BASE_COLUMNS[6:],
    "16-appended": BASE_COLUMNS + [("organizer", "TEXT"), ("account", "TEXT")],
    "16-reverse-appended": BASE_COLUMNS + [("account", "TEXT"), ("organizer", "TEXT")],
    "16-inline": BASE_COLUMNS[:6] + [("organizer", "TEXT"), ("account", "TEXT")] + BASE_COLUMNS[6:],
}
NOW = 2_000_000_000.0


def fixture_rows(columns):
    names = {name for name, _ in columns}
    rows = []
    for i, status in enumerate(("active", "done", "dismissed", "active")):
        row = dict(
            id=f"original-{i}", source="manual", source_id=f"upstream-{i}",
            kind="reminder", title=f"Fixture {i} — café", context=f"Context {i}",
            organizer=f"Organizer {i}", account=f"Account {i}",
            when_ts=NOW + i * 3600, all_day=i % 2, location=f"Room {i}",
            url=f"https://example.invalid/{i}", status=status,
            confidence=0.25 + i * 0.125, created_at=NOW - 86400 + i,
            updated_at=NOW - 3600 + i,
        )
        if i == 3:
            for name in ("source_id", "context", "organizer", "account", "when_ts",
                         "all_day", "location", "url", "confidence", "created_at", "updated_at"):
                row[name] = None
        rows.append({name: value for name, value in row.items() if name in names})
    return rows


def insert_rows(db, table, rows):
    for row in rows:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()))


def create_fixture(path, layout, *, current_unique=False):
    columns = LAYOUTS[layout]
    rows = fixture_rows(columns)
    with sqlite3.connect(path) as db:
        unique = "source, source_id, when_ts" if current_unique else "source, source_id"
        db.execute("CREATE TABLE commitments (" + ", ".join(
            name + " " + kind for name, kind in columns) + f", UNIQUE({unique}))")
        db.execute("CREATE INDEX idx_commit_when ON commitments(status, when_ts)")
        db.execute("CREATE TABLE notify_log (commitment_id TEXT, stage TEXT, sent_at REAL, "
                   "PRIMARY KEY (commitment_id, stage))")
        insert_rows(db, "commitments", rows)
        db.executemany("INSERT INTO notify_log VALUES (?, ?, ?)", [
            (row["id"], stage, NOW - i - 1)
            for i, row in enumerate(rows) for stage in ("due", "T-30m")])
    db.close()
    return [{**row, "organizer": row.get("organizer"), "account": row.get("account")}
            for row in rows]


def snapshot(path):
    db = sqlite3.connect(path)
    try:
        return "\n".join(db.iterdump())
    finally:
        db.close()


class AssistantMigrations(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix="case-", dir=_scratch.name)))

    def open_store(self, path):
        store = AssistantStore(path)
        self.addCleanup(store._db.close)
        return store

    def assert_preserved(self, store, expected):
        rows = [dict(row) for row in store._db.execute("SELECT * FROM commitments ORDER BY id")]
        self.assertEqual(rows, sorted(expected, key=lambda row: row["id"]))
        self.assertEqual(store._db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(store._db.execute(
            "SELECT name FROM sqlite_master WHERE name='commitments_old_migrating'").fetchall(), [])
        index = store._db.execute(
            "SELECT tbl_name FROM sqlite_master WHERE name='idx_commit_when'").fetchone()
        self.assertIsNotNone(index)
        self.assertEqual(index[0], "commitments")
        self.assertEqual([row["name"] for row in store._db.execute(
            "PRAGMA index_info(idx_commit_when)")], ["status", "when_ts"])
        self.assertEqual([tuple(row) for row in store._db.execute(
            "SELECT * FROM notify_log ORDER BY commitment_id, stage")], sorted([
                (f"original-{i}", stage, NOW - i - 1)
                for i in range(4) for stage in ("due", "T-30m")]))
        self.assertTrue(store.already_notified("original-0", "due"))
        self.assertEqual(due_reminders(store, now=NOW), [])
        visible = sorted((row for row in expected
                          if row["status"] == "active" and row["when_ts"] is not None),
                         key=lambda row: row["when_ts"])
        self.assertEqual([row["id"] for row in store.upcoming(now=NOW)],
                         [row["id"] for row in visible])

    def test_historical_layouts_preserve_fields_and_repeated_startup(self):
        for layout in LAYOUTS:
            for current_unique in (False, True):
                with self.subTest(layout=layout, current_unique=current_unique):
                    path = self.root / f"{layout}-{current_unique}.db"
                    expected = create_fixture(path, layout, current_unique=current_unique)
                    for _ in range(3):
                        store = self.open_store(path)
                        self.assert_preserved(store, expected)
                        store._db.close()

    def test_recurring_occurrences_work_after_upgrade(self):
        path = self.root / "recurring.db"
        create_fixture(path, "14")
        store = self.open_store(path)
        store.sync_source("calendar", [
            dict(source_id="same-event", title=f"Occurrence {i}", kind="event", when_ts=NOW + i * 3600)
            for i in range(3)])
        rows = store._db.execute("SELECT * FROM commitments WHERE source='calendar'").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["when_ts"] for row in rows}, {NOW, NOW + 3600, NOW + 7200})
        with self.assertRaises(sqlite3.IntegrityError):
            store._db.execute("INSERT INTO commitments (id, source, source_id, kind, title, when_ts) "
                              "VALUES ('duplicate', 'calendar', 'same-event', 'event', 'Duplicate', ?)", (NOW,))
        store._db.rollback()

    def interrupted_fixture(self, path, layout, stage):
        expected = create_fixture(path, layout)
        with sqlite3.connect(path) as db:
            db.execute("ALTER TABLE commitments RENAME TO commitments_old_migrating")
            if stage != "renamed-only":
                db.execute("CREATE TABLE commitments (" + ", ".join(
                    name + " " + kind for name, kind in LAYOUTS["16-inline"])
                    + ", UNIQUE(source, source_id, when_ts))")
                if stage in ("partial-copy", "complete-copy"):
                    insert_rows(db, "commitments", expected[:2] if stage == "partial-copy" else expected)
                if stage == "new-live-row":
                    new_row = {**expected[0], "id": "new-live", "source_id": "new-source",
                               "title": "Later row", "when_ts": NOW + 7200}
                    insert_rows(db, "commitments", [new_row])
                    expected.append(new_row)
        db.close()
        return expected

    def test_interrupted_migration_recovers_every_layout_and_restarts(self):
        for layout in LAYOUTS:
            for stage in ("renamed-only", "empty-live", "partial-copy", "complete-copy", "new-live-row"):
                with self.subTest(layout=layout, stage=stage):
                    path = self.root / f"{layout}-{stage}.db"
                    expected = self.interrupted_fixture(path, layout, stage)
                    for _ in range(2):
                        store = self.open_store(path)
                        self.assert_preserved(store, expected)
                        store._db.close()

    def test_conflicting_recovery_keeps_both_tables_and_notifications(self):
        for conflict in ("same-id-edited", "same-key-other-id", "same-id-scrambled"):
            with self.subTest(conflict=conflict):
                path = self.root / f"{conflict}.db"
                expected = self.interrupted_fixture(path, "16-appended", "empty-live")
                row = dict(expected[0])
                if conflict == "same-id-edited":
                    row["status"] = "dismissed"
                elif conflict == "same-key-other-id":
                    row["id"] = "newer-id"
                else:
                    # The old SELECT * rebuild shifted these columns two places.
                    row = dict(zip((name for name, _ in LAYOUTS["16-inline"]),
                                   (row[name] for name, _ in LAYOUTS["16-appended"])))
                with sqlite3.connect(path) as db:
                    insert_rows(db, "commitments", [row])
                db.close()
                before = snapshot(path)
                for _ in range(2):
                    with self.assertRaises((RuntimeError, sqlite3.IntegrityError)):
                        self.open_store(path)
                    self.assertEqual(snapshot(path), before)

    def test_failed_copy_rolls_back_schema_and_releases_database(self):
        for layout in ("14", "16-appended"):
            with self.subTest(layout=layout):
                path = self.root / f"{layout}.db"
                expected = create_fixture(path, layout)
                before = snapshot(path)
                real_connect = sqlite3.connect

                class FailingConnection(sqlite3.Connection):
                    def execute(self, sql, parameters=()):
                        if sql.lstrip().upper().startswith("INSERT INTO COMMITMENTS"):
                            raise sqlite3.OperationalError("injected migration copy failure")
                        return super().execute(sql, parameters)

                def connect(*args, **kwargs):
                    return real_connect(*args, **kwargs, factory=FailingConnection)

                with patch.object(store_module.sqlite3, "connect", side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "injected migration"):
                        self.open_store(path)
                self.assertEqual(snapshot(path), before)
                self.assert_preserved(self.open_store(path), expected)

    def test_failure_after_each_ddl_or_copy_rolls_back_and_can_retry(self):
        for interrupted in (False, True):
            for failure in ("INSERT INTO commitments", "DROP TABLE commitments_old_migrating",
                            "CREATE INDEX IF NOT EXISTS idx_commit_when", "COMMIT"):
                with self.subTest(interrupted=interrupted, failure=failure):
                    path = self.root / f"{interrupted}-{failure.split()[0]}.db"
                    expected = (self.interrupted_fixture(path, "15-organizer-appended", "partial-copy")
                                if interrupted else create_fixture(path, "15-organizer-appended"))
                    before = snapshot(path)
                    real_connect = sqlite3.connect

                    class FailingConnection(sqlite3.Connection):
                        def execute(self, sql, parameters=()):
                            result = super().execute(sql, parameters)
                            if sql.startswith(failure):
                                raise sqlite3.OperationalError("injected post-statement failure")
                            return result

                    def connect(*args, **kwargs):
                        db = real_connect(*args, **kwargs, factory=FailingConnection)
                        if failure == "COMMIT":
                            db.set_authorizer(lambda action, arg, *rest:
                                              sqlite3.SQLITE_DENY
                                              if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                                              else sqlite3.SQLITE_OK)
                        return db

                    with patch.object(store_module.sqlite3, "connect", side_effect=connect):
                        with self.assertRaises(sqlite3.DatabaseError):
                            self.open_store(path)
                    self.assertEqual(snapshot(path), before)
                    self.assert_preserved(self.open_store(path), expected)

    def test_additive_migration_failure_is_atomic(self):
        path = self.root / "additive.db"
        expected = create_fixture(path, "14", current_unique=True)
        before = snapshot(path)
        real_connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql == "ALTER TABLE commitments ADD COLUMN account TEXT":
                    raise sqlite3.OperationalError("injected additive failure")
                return result

        with patch.object(store_module.sqlite3, "connect", side_effect=lambda *args, **kwargs:
                          real_connect(*args, **kwargs, factory=FailingConnection)):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected additive"):
                self.open_store(path)
        self.assertEqual(snapshot(path), before)
        self.assert_preserved(self.open_store(path), expected)

    def test_unrecognized_columns_fail_without_losing_data(self):
        for variant in ("unexpected", "missing"):
            with self.subTest(variant=variant):
                path = self.root / f"{variant}.db"
                create_fixture(path, "14")
                with sqlite3.connect(path) as db:
                    if variant == "unexpected":
                        db.execute("ALTER TABLE commitments ADD COLUMN extra TEXT DEFAULT 'preserve me'")
                    else:
                        db.execute("ALTER TABLE commitments DROP COLUMN location")
                db.close()
                before = snapshot(path)
                with self.assertRaisesRegex(RuntimeError, "unrecognized columns"):
                    self.open_store(path)
                self.assertEqual(snapshot(path), before)

    def test_two_legacy_tables_fail_without_choosing_a_winner(self):
        path = self.root / "two-legacy.db"
        self.interrupted_fixture(path, "14", "empty-live")
        with sqlite3.connect(path) as db:
            db.execute("CREATE UNIQUE INDEX old_live_constraint ON commitments(source, source_id)")
        db.close()
        before = snapshot(path)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "both legacy tables exist"):
                self.open_store(path)
            self.assertEqual(snapshot(path), before)

    def test_unique_index_detection_requires_the_complete_constraint(self):
        for extra in ("when-only", "both-constraints", "partial", "reversed-old"):
            with self.subTest(extra=extra):
                path = self.root / f"{extra}.db"
                expected = create_fixture(path, "14", current_unique=extra == "reversed-old")
                with sqlite3.connect(path) as db:
                    index = {"when-only": "when_ts", "reversed-old": "source_id, source"}.get(
                        extra, "source, source_id, when_ts")
                    where = " WHERE status='active'" if extra == "partial" else ""
                    db.execute(f'CREATE UNIQUE INDEX "odd index" ON commitments({index}){where}')
                db.close()
                store = self.open_store(path)
                self.assert_preserved(store, expected)
                store._db.execute("INSERT INTO commitments (id, source, source_id, kind, title, when_ts) "
                                  "VALUES ('next', 'manual', 'upstream-0', 'reminder', 'Next', ?)", (NOW + 600,))
                store._db.commit()

    def test_simultaneous_startups_serialize_migration_and_recovery(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                path = self.root / f"concurrent-{interrupted}.db"
                expected = (self.interrupted_fixture(path, "16-appended", "partial-copy")
                            if interrupted else create_fixture(path, "16-appended"))
                barrier = threading.Barrier(4)

                def start():
                    barrier.wait(timeout=10)
                    store = AssistantStore(path)
                    try:
                        return [dict(row) for row in store._db.execute("SELECT * FROM commitments ORDER BY id")]
                    finally:
                        store._db.close()

                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(lambda _: start(), range(4)))
                for rows in results:
                    self.assertEqual(rows, sorted(expected, key=lambda row: row["id"]))
                self.assert_preserved(self.open_store(path), expected)

    def test_null_id_multiplicity_is_preserved_or_recovery_refuses_overlap(self):
        for stage in ("fresh-upgrade", "empty-live", "partial-copy"):
            with self.subTest(stage=stage):
                path = self.root / f"null-id-{stage}.db"
                if stage == "fresh-upgrade":
                    expected = create_fixture(path, "14")
                    old_table = "commitments"
                else:
                    expected = self.interrupted_fixture(path, "14", "empty-live")
                    old_table = "commitments_old_migrating"
                nullable = {**expected[3], "id": None}
                old_row = {name: nullable[name] for name, _ in BASE_COLUMNS}
                with sqlite3.connect(path) as db:
                    insert_rows(db, old_table, [old_row, old_row])
                    if stage == "partial-copy":
                        insert_rows(db, "commitments", [nullable])
                db.close()
                if stage == "partial-copy":
                    before = snapshot(path)
                    for _ in range(2):
                        with self.assertRaisesRegex(RuntimeError, "overlapping NULL IDs"):
                            self.open_store(path)
                        self.assertEqual(snapshot(path), before)
                else:
                    for _ in range(2):
                        store = self.open_store(path)
                        self.assertEqual([dict(row) for row in store._db.execute(
                            "SELECT * FROM commitments WHERE id IS NULL")], [nullable, nullable])
                        self.assertEqual(store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0], 6)
                        store._db.close()


if __name__ == "__main__":
    unittest.main()
