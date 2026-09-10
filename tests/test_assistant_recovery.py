"""Lossless startup-recovery tests; all databases live in disposable state."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


_scratch = tempfile.TemporaryDirectory(prefix="wisp-assistant-recovery-")
os.environ["WISP_HOME"] = _scratch.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.assistant import recovery  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402


SCHEMA = """
CREATE TABLE {table} (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    context TEXT,
    organizer TEXT,
    account TEXT,
    when_ts REAL,
    all_day INTEGER DEFAULT 0,
    location TEXT,
    url TEXT,
    status TEXT DEFAULT 'active',
    confidence REAL DEFAULT 1.0,
    created_at REAL,
    updated_at REAL,
    UNIQUE(source, source_id, when_ts)
)
"""
OLD_COLUMNS = tuple(
    column for column in recovery.CURRENT_COLUMNS if column not in {"organizer", "account"}
)
OLD_SCHEMA = """
CREATE TABLE {table} (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT, kind TEXT NOT NULL,
    title TEXT NOT NULL, context TEXT, when_ts REAL, all_day INTEGER DEFAULT 0,
    location TEXT, url TEXT, status TEXT DEFAULT 'active', confidence REAL DEFAULT 1.0,
    created_at REAL, updated_at REAL, UNIQUE(source, source_id)
)
"""


def row(row_id: str, source_id: str, title: str, when_ts: float) -> tuple:
    return (
        row_id, "calendar", source_id, "event", title, "Work", "Alex", "iCloud",
        when_ts, 0, "Room 1", None, "active", 1.0, 100.0, 200.0,
    )


def insert(db: sqlite3.Connection, table: str, values: tuple) -> None:
    columns = ", ".join(recovery.CURRENT_COLUMNS)
    placeholders = ", ".join("?" for _ in recovery.CURRENT_COLUMNS)
    db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)


def snapshot(path: Path) -> str:
    db = sqlite3.connect(path)
    try:
        return "\n".join(db.iterdump())
    finally:
        db.close()


def create_conflict(path: Path) -> dict[str, tuple]:
    rows = {
        "live": row("shared", "shared-source", "Live version", 1_000.0),
        "key_live": row("key-live", "key-source", "Current keyed row", 4_000.0),
        "exact": row("exact", "exact-source", "Same version", 2_000.0),
        "recoverable": row("legacy-only", "legacy-only", "Recovered", 3_000.0),
        "same_id": row("shared", "shared-source", "Legacy edit", 1_000.0),
        "same_key": row("other-id", "key-source", "Other identity", 4_000.0),
    }
    with sqlite3.connect(path) as db:
        db.execute(SCHEMA.format(table="commitments"))
        db.execute(SCHEMA.format(table="commitments_old_migrating"))
        db.execute(
            "CREATE TABLE notify_log (commitment_id TEXT, stage TEXT, sent_at REAL, "
            "PRIMARY KEY (commitment_id, stage))"
        )
        insert(db, "commitments", rows["live"])
        insert(db, "commitments", rows["key_live"])
        insert(db, "commitments", rows["exact"])
        insert(db, "commitments_old_migrating", rows["exact"])
        insert(db, "commitments_old_migrating", rows["recoverable"])
        insert(db, "commitments_old_migrating", rows["same_id"])
        insert(db, "commitments_old_migrating", rows["same_key"])
        db.execute("INSERT INTO notify_log VALUES ('shared', 'due', 300)")
    return rows


class AssistantRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix="case-", dir=_scratch.name)))

    def test_startup_error_is_actionable_and_keeps_the_database_unchanged(self):
        path = self.root / "assistant conflict.db"
        create_conflict(path)
        before = snapshot(path)

        with self.assertRaises(RuntimeError) as raised:
            AssistantStore(path)

        message = str(raised.exception)
        self.assertIn("Data was retained", message)
        self.assertIn(str(path.resolve()), message)
        self.assertIn(sys.executable, message)
        self.assertIn("recovery.py inspect", message)
        self.assertIn("repair", message)
        self.assertIn("--apply", message)
        self.assertEqual(snapshot(path), before)

    def test_inspection_is_read_only_and_reports_conflict_counts(self):
        path = self.root / "inspect.db"
        create_conflict(path)
        before = snapshot(path)

        report = recovery.inspect(path)

        self.assertEqual(report.state, "recovery_required")
        self.assertEqual((report.live_rows, report.legacy_rows), (3, 4))
        self.assertEqual(
            (report.recoverable_rows, report.already_preserved_rows, report.conflicting_rows),
            (1, 1, 2),
        )
        self.assertEqual(snapshot(path), before)
        self.assertFalse(recovery._backup_path(path).exists())

    def test_explicit_repair_backs_up_merges_and_quarantines_idempotently(self):
        path = self.root / "repair.db"
        rows = create_conflict(path)
        before = snapshot(path)

        with self.assertRaisesRegex(recovery.RecoveryRefused, "--apply"):
            recovery.repair(path)
        self.assertEqual(snapshot(path), before)

        report = recovery.repair(path, apply=True)

        self.assertEqual(report.state, "recovered")
        self.assertEqual(
            (report.recoverable_rows, report.already_preserved_rows, report.conflicting_rows),
            (1, 1, 2),
        )
        self.assertIsNotNone(report.backup)
        self.assertEqual(snapshot(report.backup), before)
        with sqlite3.connect(path) as db:
            live = db.execute("SELECT id, title FROM commitments ORDER BY id").fetchall()
            self.assertEqual(live, [
                ("exact", "Same version"),
                ("key-live", "Current keyed row"),
                ("legacy-only", "Recovered"),
                ("shared", "Live version"),
            ])
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM commitments_recovery_live").fetchone()[0], 3)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM commitments_recovery_legacy").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT * FROM notify_log").fetchall(), [("shared", "due", 300.0)])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        store = AssistantStore(path)
        self.addCleanup(store._db.close)
        self.assertEqual(store.get("legacy-only")["title"], rows["recoverable"][4])
        after = snapshot(path)
        repeated = recovery.repair(path, apply=True)
        self.assertEqual(repeated.state, "healthy")
        self.assertEqual(snapshot(path), after)

    def test_unsafe_backup_and_unsupported_schema_are_refused(self):
        for case in ("backup", "symlink", "hardlink", "schema"):
            with self.subTest(case=case):
                path = self.root / f"{case}.db"
                create_conflict(path)
                backup_path = recovery._backup_path(path)
                if case == "backup":
                    other = sqlite3.connect(backup_path)
                    other.execute("CREATE TABLE unrelated (value TEXT)")
                    other.commit()
                    other.close()
                elif case == "symlink":
                    backup_path.symlink_to(path)
                elif case == "hardlink":
                    os.link(path, backup_path)
                else:
                    with sqlite3.connect(path) as db:
                        db.execute("ALTER TABLE commitments_old_migrating ADD COLUMN mystery TEXT")
                before = snapshot(path)
                with self.assertRaises(recovery.RecoveryRefused):
                    recovery.repair(path, apply=True)
                self.assertEqual(snapshot(path), before)
                if case in {"symlink", "hardlink"}:
                    self.assertTrue(os.path.samefile(path, backup_path))

    def test_failure_after_backup_rolls_back_and_verified_backup_can_be_reused(self):
        path = self.root / "retry.db"
        create_conflict(path)
        before = snapshot(path)
        original = recovery._prepare_merge
        calls = 0

        def fail_after_ddl(db):
            nonlocal calls
            calls += 1
            result = original(db)
            if calls == 1:  # read-only inspection uses a disposable copy
                return result
            raise sqlite3.OperationalError("injected failure")

        with patch.object(recovery, "_prepare_merge", side_effect=fail_after_ddl):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                recovery.repair(path, apply=True)
        self.assertEqual(snapshot(path), before)
        self.assertEqual(snapshot(recovery._backup_path(path)), before)

        report = recovery.repair(path, apply=True)
        self.assertEqual(report.state, "recovered")

    def test_repair_normalizes_two_old_constraint_tables(self):
        path = self.root / "two-old-tables.db"
        live = row("live", "live-source", "Live", 1_000.0)
        legacy = row("legacy", "legacy-source", "Legacy", 2_000.0)
        with sqlite3.connect(path) as db:
            db.execute(OLD_SCHEMA.format(table="commitments"))
            db.execute(OLD_SCHEMA.format(table="commitments_old_migrating"))
            for table, values in (("commitments", live), ("commitments_old_migrating", legacy)):
                mapped = dict(zip(recovery.CURRENT_COLUMNS, values))
                columns = ", ".join(OLD_COLUMNS)
                placeholders = ", ".join("?" for _ in OLD_COLUMNS)
                db.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    tuple(mapped[column] for column in OLD_COLUMNS),
                )

        with self.assertRaisesRegex(RuntimeError, "both legacy tables exist"):
            AssistantStore(path)
        report = recovery.repair(path, apply=True)
        self.assertEqual(report.state, "recovered")
        store = AssistantStore(path)
        self.addCleanup(store._db.close)
        self.assertEqual(
            [item["id"] for item in store._db.execute("SELECT id FROM commitments ORDER BY id")],
            ["legacy", "live"],
        )
        self.assertEqual(
            {item["name"] for item in store._db.execute("PRAGMA table_info(commitments)")},
            set(recovery.CURRENT_COLUMNS),
        )

    def test_cli_runs_by_path_without_importing_the_broken_store(self):
        path = self.root / "cli.db"
        create_conflict(path)
        script = Path(recovery.__file__).resolve()

        inspected = subprocess.run(
            [sys.executable, str(script), "inspect", str(path)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertIn("state: recovery_required", inspected.stdout)
        self.assertIn("next step:", inspected.stdout)
        self.assertIn("--apply", inspected.stdout)
        self.assertIn("id='shared' differs between live and legacy tables", inspected.stdout)
        self.assertIn("id='other-id' duplicates live source key", inspected.stdout)
        refused = subprocess.run(
            [sys.executable, str(script), "repair", str(path)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--apply", refused.stderr)


if __name__ == "__main__":
    unittest.main()
