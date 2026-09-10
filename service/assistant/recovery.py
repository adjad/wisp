"""Explicit, lossless recovery for interrupted AssistantStore migrations.

This file is intentionally runnable by path.  ``python -m`` imports
``service.assistant`` first, which is exactly what cannot succeed while the
module-level AssistantStore is fail-closed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from typing import Sequence


LIVE_TABLE = "commitments"
LEGACY_TABLE = "commitments_old_migrating"
MERGED_TABLE = "commitments_recovery_merged"
LIVE_QUARANTINE_TABLE = "commitments_recovery_live"
LEGACY_QUARANTINE_TABLE = "commitments_recovery_legacy"

CURRENT_COLUMNS = (
    "id", "source", "source_id", "kind", "title", "context", "organizer",
    "account", "when_ts", "all_day", "location", "url", "status",
    "confidence", "created_at", "updated_at",
)
OPTIONAL_ADDITIVE_COLUMNS = {"organizer", "account"}


class RecoveryRefused(RuntimeError):
    """The database was left untouched because recovery was not provably safe."""


@dataclass(frozen=True)
class RecoveryReport:
    state: str
    database: Path
    live_rows: int
    legacy_rows: int
    recoverable_rows: int
    already_preserved_rows: int
    conflicting_rows: int
    backup: Path | None = None
    conflicts: tuple[str, ...] = ()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in db.execute(f"PRAGMA table_info({_quote(table)})"))


def _validate_columns(columns: Sequence[str], label: str) -> None:
    present = set(columns)
    expected = set(CURRENT_COLUMNS)
    missing = expected - present - OPTIONAL_ADDITIVE_COLUMNS
    unexpected = present - expected
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected " + ", ".join(sorted(unexpected)))
        raise RecoveryRefused(
            f"{label} has an unsupported schema ({'; '.join(details)}); database unchanged"
        )


def _mapped_row(row: sqlite3.Row, columns: Sequence[str]) -> tuple[object, ...]:
    available = set(row.keys())
    return tuple(row[column] if column in available else None for column in columns)


def _create_merged_table(db: sqlite3.Connection) -> None:
    db.execute(f"""
        CREATE TABLE {_quote(MERGED_TABLE)} (
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
    """)


def _insert_sql(table: str) -> str:
    names = ", ".join(_quote(name) for name in CURRENT_COLUMNS)
    placeholders = ", ".join("?" for _ in CURRENT_COLUMNS)
    return f"INSERT INTO {_quote(table)} ({names}) VALUES ({placeholders})"


def _rows_equal(left: sqlite3.Row, values: Sequence[object]) -> bool:
    return all(left[name] == value for name, value in zip(CURRENT_COLUMNS, values))


def _classify_legacy_row(
    db: sqlite3.Connection,
    values: tuple[object, ...],
    *,
    live_had_null_id: bool,
) -> tuple[str, str | None]:
    row = dict(zip(CURRENT_COLUMNS, values))
    if row["id"] is None:
        # Multiple NULL primary keys are legal in SQLite. If live data already
        # had one, there is no stable identity with which to prove a copy is
        # new rather than a duplicate from the interrupted migration.
        if live_had_null_id:
            return "conflict", "legacy row with NULL id cannot be matched to live NULL-id rows"
        return "recoverable", None

    same_id = db.execute(
        f"SELECT * FROM {_quote(MERGED_TABLE)} WHERE id IS ?", (row["id"],)
    ).fetchone()
    if same_id is not None:
        if _rows_equal(same_id, values):
            return "preserved", None
        return "conflict", f"id={row['id']!r} differs between live and legacy tables"

    if row["source_id"] is not None and row["when_ts"] is not None:
        same_key = db.execute(
            f"SELECT 1 FROM {_quote(MERGED_TABLE)} "
            "WHERE source IS ? AND source_id IS ? AND when_ts IS ? LIMIT 1",
            (row["source"], row["source_id"], row["when_ts"]),
        ).fetchone()
        if same_key is not None:
            key = (row["source"], row["source_id"], row["when_ts"])
            return "conflict", f"id={row['id']!r} duplicates live source key {key!r}"
    return "recoverable", None


def _logical_digest(db: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for statement in db.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".recovery-backup")


def _ensure_backup(db: sqlite3.Connection, path: Path) -> Path:
    expected = _logical_digest(db)
    backup_path = _backup_path(path)
    backup_exists = backup_path.exists() or backup_path.is_symlink()
    if backup_exists and (
        backup_path.is_symlink()
        or not backup_path.is_file()
        or os.path.samefile(path, backup_path)
    ):
        raise RecoveryRefused(
            f"backup path {backup_path} is not an independent regular file; database unchanged"
        )
    created = not backup_exists
    backup = sqlite3.connect(str(backup_path))
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if created:
            # A connection cannot back itself up while it owns BEGIN
            # IMMEDIATE: sqlite3_backup waits for that writer forever. A
            # separate reader sees the same snapshot while the writer lock
            # prevents the source from changing under it.
            source.backup(backup)
            backup.commit()
    except BaseException:
        source.close()
        backup.close()
        if created:
            backup_path.unlink(missing_ok=True)
        raise
    source.close()
    backup.close()
    verified = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        integrity = verified.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or _logical_digest(verified) != expected:
            raise RecoveryRefused(
                f"existing backup {backup_path} does not match the database; "
                "move it aside and inspect it before retrying"
            )
    except BaseException:
        verified.close()
        if created:
            backup_path.unlink(missing_ok=True)
        raise
    verified.close()
    return backup_path


def _prepare_merge(
    db: sqlite3.Connection,
) -> tuple[int, int, int, int, tuple[str, ...]]:
    live_columns = _columns(db, LIVE_TABLE)
    legacy_columns = _columns(db, LEGACY_TABLE)
    _validate_columns(live_columns, LIVE_TABLE)
    _validate_columns(legacy_columns, LEGACY_TABLE)

    _create_merged_table(db)
    insert = _insert_sql(MERGED_TABLE)
    live_rows = db.execute(f"SELECT * FROM {_quote(LIVE_TABLE)}").fetchall()
    legacy_rows = db.execute(
        f"SELECT * FROM {_quote(LEGACY_TABLE)} ORDER BY rowid"
    ).fetchall()
    for row in live_rows:
        try:
            db.execute(insert, _mapped_row(row, CURRENT_COLUMNS))
        except sqlite3.IntegrityError as exc:
            raise RecoveryRefused(
                "live commitments contain identities that cannot coexist under the current "
                "schema; database unchanged"
            ) from exc

    live_had_null_id = any(row["id"] is None for row in live_rows)
    recoverable = preserved = 0
    conflicts: list[str] = []
    for row in legacy_rows:
        values = _mapped_row(row, CURRENT_COLUMNS)
        classification, detail = _classify_legacy_row(
            db, values, live_had_null_id=live_had_null_id
        )
        if classification == "recoverable":
            db.execute(insert, values)
            recoverable += 1
        elif classification == "preserved":
            preserved += 1
        elif classification == "conflict":
            if detail is None:
                raise RuntimeError("recovery conflict was missing its diagnostic")
            conflicts.append(detail)

    return len(live_rows), len(legacy_rows), recoverable, preserved, tuple(conflicts)


def inspect(path: Path) -> RecoveryReport:
    """Inspect recovery state without creating or modifying a database."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise RecoveryRefused(f"database does not exist: {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryRefused("SQLite integrity check failed; database unchanged")
        has_live = _table_exists(db, LIVE_TABLE)
        has_legacy = _table_exists(db, LEGACY_TABLE)
        live = db.execute(f"SELECT COUNT(*) FROM {_quote(LIVE_TABLE)}").fetchone()[0] if has_live else 0
        legacy = db.execute(f"SELECT COUNT(*) FROM {_quote(LEGACY_TABLE)}").fetchone()[0] if has_legacy else 0
        if not has_legacy:
            return RecoveryReport("healthy", path, live, 0, 0, 0, 0)
        if not has_live:
            return RecoveryReport("restart_can_recover", path, 0, legacy, legacy, 0, 0)
        for name in (MERGED_TABLE, LIVE_QUARANTINE_TABLE, LEGACY_QUARANTINE_TABLE):
            if _table_exists(db, name):
                raise RecoveryRefused(
                    f"recovery table {name} already exists; database unchanged"
                )
    finally:
        db.close()

    # Classification uses a disposable in-memory copy so inspection stays
    # genuinely read-only while exercising the same merge rules as repair.
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    try:
        source.backup(scratch)
        scratch.execute("BEGIN IMMEDIATE")
        live, legacy, recoverable, preserved, conflicts = _prepare_merge(scratch)
        scratch.rollback()
        return RecoveryReport(
            "recovery_required", path, live, legacy, recoverable, preserved,
            len(conflicts), conflicts=conflicts,
        )
    finally:
        scratch.close()
        source.close()


def repair(path: Path, *, apply: bool = False) -> RecoveryReport:
    """Apply an explicit, backed-up recovery, retaining both source tables."""
    plan = inspect(path)
    if plan.state == "healthy":
        return plan
    if not apply:
        raise RecoveryRefused("inspection only; rerun repair with --apply to change the database")
    if plan.state == "restart_can_recover":
        raise RecoveryRefused("only the legacy table exists; restart Wisp to recover it automatically")

    path = plan.database
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    backup_path: Path | None = None
    try:
        db.execute("BEGIN IMMEDIATE")
        # Re-check after taking the writer lock so a concurrent startup cannot
        # invalidate the read-only plan.
        if not (_table_exists(db, LIVE_TABLE) and _table_exists(db, LEGACY_TABLE)):
            raise RecoveryRefused("database state changed during recovery; database unchanged")
        for name in (MERGED_TABLE, LIVE_QUARANTINE_TABLE, LEGACY_QUARANTINE_TABLE):
            if _table_exists(db, name):
                raise RecoveryRefused(f"recovery table {name} already exists; database unchanged")

        backup_path = _ensure_backup(db, path)
        live, legacy, recoverable, preserved, conflicts = _prepare_merge(db)
        db.execute(f"ALTER TABLE {_quote(LIVE_TABLE)} RENAME TO {_quote(LIVE_QUARANTINE_TABLE)}")
        db.execute(f"ALTER TABLE {_quote(LEGACY_TABLE)} RENAME TO {_quote(LEGACY_QUARANTINE_TABLE)}")
        db.execute(f"ALTER TABLE {_quote(MERGED_TABLE)} RENAME TO {_quote(LIVE_TABLE)}")
        db.execute("DROP INDEX IF EXISTS idx_commit_when")
        db.execute(
            "CREATE INDEX idx_commit_when ON commitments(status, when_ts)"
        )
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("recovery SQLite integrity check failed; database unchanged")
        db.commit()
        return RecoveryReport(
            "recovered", path, live, legacy, recoverable, preserved,
            len(conflicts), backup_path, conflicts,
        )
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def recovery_guidance(path: Path, reason: str) -> str:
    path = path.expanduser().resolve()
    script = Path(__file__).resolve()
    inspect_command = " ".join(
        map(shlex.quote, (sys.executable, str(script), "inspect", str(path)))
    )
    repair_command = " ".join(
        map(shlex.quote, (sys.executable, str(script), "repair", str(path), "--apply"))
    )
    return (
        f"AssistantStore startup stopped: {reason}. Data was retained at {path}. "
        f"Inspect the conflict with: {inspect_command}. If the report is expected, "
        f"run the explicit backed-up recovery: {repair_command}. Ambiguous rows are "
        "retained in commitments_recovery_legacy."
    )


def _format(report: RecoveryReport) -> str:
    fields = [
        f"state: {report.state}",
        f"database: {report.database}",
        f"live rows: {report.live_rows}",
        f"legacy rows: {report.legacy_rows}",
        f"recoverable legacy rows: {report.recoverable_rows}",
        f"already-preserved legacy rows: {report.already_preserved_rows}",
        f"conflicting legacy rows: {report.conflicting_rows}",
    ]
    if report.backup is not None:
        fields.append(f"verified backup: {report.backup}")
        fields.append(f"live quarantine: {LIVE_QUARANTINE_TABLE}")
        fields.append(f"legacy quarantine: {LEGACY_QUARANTINE_TABLE}")
    fields.extend(f"conflict: {detail}" for detail in report.conflicts)
    if report.state == "recovery_required":
        command = " ".join(map(shlex.quote, (
            sys.executable, str(Path(__file__).resolve()), "repair", str(report.database), "--apply",
        )))
        fields.append(
            f"next step: review the conflicts, then run {command}; recovery first creates "
            "a verified backup and retains both original tables"
        )
    elif report.state == "restart_can_recover":
        fields.append("next step: restart Wisp; its normal migration can recover this state")
    elif report.state == "recovered":
        fields.append("next step: restart Wisp")
    return "\n".join(fields)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("inspect", "repair"):
        child = subparsers.add_parser(command)
        child.add_argument("database", type=Path)
        if command == "repair":
            child.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = inspect(args.database) if args.command == "inspect" else repair(
            args.database, apply=args.apply
        )
    except (RecoveryRefused, sqlite3.DatabaseError) as exc:
        print(f"recovery refused: {exc}", file=sys.stderr)
        return 2
    print(_format(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
