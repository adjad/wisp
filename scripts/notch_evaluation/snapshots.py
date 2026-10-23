"""Source-scope manifests and consistent read-only snapshot capture.

The contained evaluation host reads real records only from snapshots taken
here, never from the live ~/.moe or a native source. A capture is driven by an
explicit scope: which Wisp caches, which assistant tables and which document
files. Credentials, configuration, grants, queues, claims and executable
extensions are refused even when a scope names them. Each capture writes a
manifest with the capture time, selectors, row counts and a SHA-256 for every
file, labelled as a snapshot rather than a live sync. The host verifies the
manifest before serving, so a partial, stale or altered snapshot fails closed.

Qualification uses synthetic sources only. Capturing real sources needs a
scope reviewed by the evaluation coordinator; nothing here runs on import.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

LABEL = "snapshot: captured copy of selected records, not a live native sync"
MANIFEST = "snapshot-manifest.json"

# Wisp's synced-content caches (service/tools/cache_store.py), by name.
CACHE_NAMES = frozenset({"email_headers", "email_history", "email_sources", "email_raw", "messages",
                         "notes", "contacts", "contact_handles", "birthdays", "identity_emails"})
# assistant.db tables holding records a read answers from. Queues, claims,
# receipts and write state are never copied, whatever a scope asks for.
ASSISTANT_TABLES = frozenset({"commitments", "calendar_event_ends", "calendar_event_invitees",
                              "calendar_event_writable", "today_tasks", "assistant_undated_reminders",
                              "assistant_reminder_verified", "today_source_sync"})
REFUSED_TABLES = frozenset({"assistant_events", "assistant_reminder_writes", "assistant_behavior_action_receipts",
                            "assistant_reminder_terminals", "notify_log", "assistant_schedule_versions",
                            "assistant_stale_reminder_decisions"})
# File names and parts that never enter a snapshot.
REFUSED_NAMES = frozenset({"config.yaml", "grants.json", "mcp.json", "gmail.json", "profile.json",
                           "settings.json", "credentials", "keychain", ".ssh", ".gnupg", "skills",
                           "engine", "node-inbox.db", "codex_monitor_state.json"})
REFUSED_SUFFIXES = (".key", ".pem", ".p12", ".token", ".app", ".dylib", ".so", ".sh", ".command", ".py",
                    ".scpt", ".workflow", ".plist")
MAX_FILE_BYTES = 64 * 1024 * 1024
# The installed source layout: every file under these is a captured file.
INSTALLED_PARTS = ("wisp", "home", "sync")
# How far ahead of this clock a capture time may be before it is refused.
CLOCK_SKEW_S = 300.0

# Native sync push bodies a snapshot may hold, by source, with the fields each
# ingestion handler accepts. Contacts, browser history and any other field are
# refused. A body is hydrated as ready only when its record proves a complete
# native read (see payload_problems); otherwise it is hydrated as unavailable.
SYNC_PAYLOAD_FIELDS = {
    "emails": frozenset({"headers", "history", "raw", "raw_coverage", "identity_emails", "diagnostics"}),
    "messages": frozenset({"lines", "diagnostics"}),
    "notes": frozenset({"raw", "diagnostics"}),
}
SYNC_CONTENT_FIELD = {"emails": "headers", "messages": "lines", "notes": "raw"}


class ScopeError(ValueError):
    """A scope that would copy something a snapshot must never hold."""


@dataclass(frozen=True)
class SourceScope:
    """What one capture may copy. Every selector is explicit; nothing is implied."""
    caches: tuple[str, ...] = ()
    assistant_tables: tuple[str, ...] = ()
    # Paths relative to the source home, each a regular file.
    files: tuple[str, ...] = ()
    # Native sync push records (payload_record), by source name.
    sync_payloads: tuple[str, ...] = ()

    def validate(self) -> None:
        for name in self.caches:
            if name not in CACHE_NAMES:
                raise ScopeError(f"cache {name!r} is not a reviewed source cache")
        for table in self.assistant_tables:
            if table in REFUSED_TABLES or table not in ASSISTANT_TABLES:
                raise ScopeError(f"assistant table {table!r} may not be captured")
        for rel in self.files:
            _check_relative(rel)
        for source in self.sync_payloads:
            if source not in SYNC_PAYLOAD_FIELDS:
                raise ScopeError(f"sync payload {source!r} is not a reviewed source")


def payload_record(source: str, body: dict, *, captured_at: float, reader: str, complete: bool) -> dict:
    """Wrap one native sync push body with its observed provenance.

    ``complete`` is what the capturing reader observed, never an assumption:
    a capture that cannot prove its read finished passes False.
    """
    record = {"schema_version": 1, "source": source, "body": body,
              "provenance": {"captured_at": captured_at, "reader": reader, "complete": complete}}
    if problems := payload_shape_problems(source, record):
        raise ScopeError("; ".join(problems))
    return record


def payload_shape_problems(source: str, record) -> list[str]:
    if source not in SYNC_PAYLOAD_FIELDS:
        return [f"{source!r} is not a reviewed sync source"]
    if not isinstance(record, dict) or record.get("source") != source or record.get("schema_version") != 1:
        return ["record is not a sync payload for this source"]
    body, provenance = record.get("body"), record.get("provenance")
    if not isinstance(body, dict) or not isinstance(provenance, dict):
        return ["record needs a body and provenance"]
    problems = [f"field {key!r} is not accepted for {source}" for key in sorted(set(body) - SYNC_PAYLOAD_FIELDS[source])]
    content = body.get(SYNC_CONTENT_FIELD[source])
    if content is not None and not isinstance(content, str):
        problems.append(f"{SYNC_CONTENT_FIELD[source]} must be text")
    if not _timestamp(provenance.get("captured_at")) or not isinstance(provenance.get("reader"), str):
        problems.append("provenance needs a finite captured_at and a reader")
    if type(provenance.get("complete")) is not bool:
        problems.append("provenance.complete must be observed true or false")
    return problems


def _timestamp(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def freshness_problem(stamp, *, now: float, max_age_s: float | None, what: str) -> str | None:
    """Why ``stamp`` is not a current capture time, or None."""
    if not _timestamp(stamp):
        return f"{what} has no finite capture time"
    if stamp > now + CLOCK_SKEW_S:
        return f"{what} was captured in the future"
    if max_age_s is not None and now - stamp > max_age_s:
        return f"{what} is older than the allowed age"
    return None


def payload_problems(source: str, record, *, now: float | None = None,
                     max_age_s: float | None = None) -> list[str]:
    """Why this record may NOT be hydrated as a ready source ([] when it may).

    The native capture time is judged itself: a fresh outer snapshot never
    makes an old native read current.
    """
    problems = payload_shape_problems(source, record)
    if problems:
        return problems
    body, provenance = record["body"], record["provenance"]
    if problem := freshness_problem(provenance["captured_at"], now=time.time() if now is None else now,
                                    max_age_s=max_age_s, what="the native read"):
        problems.append(problem)
    diagnostics = body.get("diagnostics") if isinstance(body.get("diagnostics"), dict) else {}
    if provenance["complete"] is not True:
        problems.append("the capturing reader did not observe a complete read")
    if diagnostics.get("available") is not True:
        problems.append("the native read was not available")
    if diagnostics.get("syncing"):
        problems.append("the native read was still syncing")
    if not isinstance(body.get(SYNC_CONTENT_FIELD[source]), str):
        problems.append(f"no {SYNC_CONTENT_FIELD[source]} content was captured")
    if source == "emails" and isinstance(body.get("raw_coverage"), dict) and body["raw_coverage"].get("complete") is False:
        problems.append("mail body coverage was incomplete")
    return problems


def _check_relative(rel: str) -> None:
    path = Path(rel)
    if not rel or path.is_absolute() or ".." in path.parts or "\x00" in rel:
        raise ScopeError(f"file selector {rel!r} must be a plain relative path")
    lowered = [part.lower() for part in path.parts]
    if any(part in REFUSED_NAMES for part in lowered) or lowered[0] in {".moe", "library"}:
        raise ScopeError(f"file selector {rel!r} names refused state")
    if path.name.lower().endswith(REFUSED_SUFFIXES):
        raise ScopeError(f"file selector {rel!r} is an executable or credential type")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ScopeError(f"{path.name} is not a regular file (symlinks are refused)")
    if info.st_size > MAX_FILE_BYTES:
        raise ScopeError(f"{path.name} is larger than the snapshot limit")
    return info


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, OSError):
        return False


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(src, "rb") as reader, open(dest, "xb") as writer:
        shutil.copyfileobj(reader, writer)
    os.chmod(dest, 0o600)


def _capture_assistant(src_db: Path, dest_db: Path, tables: tuple[str, ...]) -> dict:
    """Copy one consistent state of assistant.db, keeping rows only in ``tables``.

    The sqlite backup API reads a single transaction-consistent state of a
    database opened read-only, including its WAL. The schema stays intact so
    the normal store opens it; every other table is emptied.
    """
    _regular(src_db)
    dest_db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    target = sqlite3.connect(dest_db)
    try:
        source.backup(target)
    finally:
        source.close()
    try:
        names = [row[0] for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {}
        for name in names:
            if name in tables:
                counts[name] = target.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            else:
                target.execute(f'DELETE FROM "{name}"')
        target.commit()
        target.execute("VACUUM")
        missing = sorted(set(tables) - set(names))
    finally:
        target.close()
    os.chmod(dest_db, 0o600)
    return {"tables": counts, "missing_tables": missing}


def capture(source_home: str | os.PathLike, source_state: str | os.PathLike,
            dest_root: str | os.PathLike, scope: SourceScope, *,
            payload_dir: str | os.PathLike | None = None) -> dict:
    """Capture ``scope`` into a fresh ``dest_root`` and write its manifest.

    ``source_state`` is the Wisp state directory (normally ~/.moe) and
    ``source_home`` the home that document selectors are relative to. The
    destination mirrors the evaluation host layout: ``wisp/`` for state and
    ``home/`` for documents.
    """
    scope.validate()
    home, state, dest = Path(source_home), Path(source_state), Path(dest_root)
    if dest.exists() and any(dest.iterdir()):
        raise ScopeError("a snapshot is captured into a new, empty directory")
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.time()
    entries: list[dict] = []
    coverage: dict[str, object] = {}
    for name in scope.caches:
        src = state / "cache" / f"{name}.txt"
        if not src.exists():
            coverage[f"cache:{name}"] = "missing"
            continue
        _regular(src)
        target = dest / "wisp" / "cache" / f"{name}.txt"
        _copy(src, target)
        coverage[f"cache:{name}"] = "captured"
    if scope.assistant_tables:
        src = state / "assistant.db"
        if src.exists():
            coverage["assistant.db"] = _capture_assistant(src, dest / "wisp" / "assistant.db",
                                                          scope.assistant_tables)
        else:
            coverage["assistant.db"] = "missing"
    for rel in scope.files:
        src = home / rel
        if not src.exists():
            coverage[f"file:{rel}"] = "missing"
            continue
        _regular(src)
        if not _inside(src, home):
            raise ScopeError(f"{rel!r} resolves outside the source home")
        _copy(src, dest / "home" / rel)
        coverage[f"file:{rel}"] = "captured"
    for source in scope.sync_payloads:
        src = Path(payload_dir) / f"{source}.json" if payload_dir is not None else None
        if src is None or not src.exists():
            coverage[f"sync:{source}"] = "missing"
            continue
        _regular(src)
        record = json.loads(src.read_text(encoding="utf-8"))
        if problems := payload_shape_problems(source, record):
            raise ScopeError(f"sync payload {source}: " + "; ".join(problems))
        _copy(src, dest / "sync" / f"{source}.json")
        complete = not payload_problems(source, record)
        coverage[f"sync:{source}"] = {"captured_at": record["provenance"]["captured_at"],
                                      "reader": record["provenance"]["reader"],
                                      "complete": complete}
    for path in sorted(p for p in dest.rglob("*") if p.is_file()):
        entries.append({"path": str(path.relative_to(dest)), "sha256": sha256_file(path),
                        "bytes": path.stat().st_size})
    manifest = {"schema_version": 1, "label": LABEL, "captured_at": started, "finished_at": time.time(),
                "scope": asdict(scope), "coverage": coverage, "files": entries}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps({k: v for k, v in manifest.items()}, sort_keys=True).encode()).hexdigest()
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(dest / MANIFEST, 0o600)
    return manifest


@dataclass
class Verification:
    ok: bool
    problems: list[str] = field(default_factory=list)
    manifest_sha256: str = ""
    age_s: float | None = None
    # Relative path -> sha256 of every verified captured file.
    files: dict[str, str] = field(default_factory=dict)
    # True only for a root that holds no source state and no manifest.
    empty: bool = False


def _provenance_problems(manifest: dict, *, now: float, max_age_s: float | None) -> list[str]:
    """The outer capture time and every recorded native capture time must be current."""
    problems = []
    if problem := freshness_problem(manifest.get("finished_at"), now=now, max_age_s=max_age_s, what="the snapshot"):
        problems.append(problem)
    coverage = manifest.get("coverage") if isinstance(manifest.get("coverage"), dict) else {}
    for key, value in sorted(coverage.items()):
        if key.startswith("sync:") and isinstance(value, dict):
            if problem := freshness_problem(value.get("captured_at"), now=now, max_age_s=max_age_s, what=key):
                problems.append(problem)
    return problems


def verify(dest_root: str | os.PathLike, *, max_age_s: float | None = None) -> Verification:
    """Recompute every hash; refuse extra, missing, linked or altered files."""
    dest = Path(dest_root)
    problems: list[str] = []
    try:
        manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Verification(False, ["manifest is missing or unreadable"])
    recorded = manifest.pop("manifest_sha256", "")
    if hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != recorded:
        problems.append("manifest was altered")
    if manifest.get("label") != LABEL:
        problems.append("manifest is not labelled as a snapshot")
    expected = {e["path"]: e for e in manifest.get("files", [])}
    present = {}
    for path in dest.rglob("*"):
        rel = str(path.relative_to(dest))
        if rel == MANIFEST:
            continue
        if path.is_symlink():
            problems.append(f"{rel} is a link")
        elif path.is_file():
            present[rel] = path
    for rel in sorted(set(present) - set(expected)):
        problems.append(f"{rel} is not in the manifest")
    for rel, entry in sorted(expected.items()):
        if rel not in present:
            problems.append(f"{rel} is missing")
        elif sha256_file(present[rel]) != entry["sha256"]:
            problems.append(f"{rel} changed since capture")
    now = time.time()
    problems += _provenance_problems(manifest, now=now, max_age_s=max_age_s)
    age = now - manifest["finished_at"] if _timestamp(manifest.get("finished_at")) else None
    files = {rel: e["sha256"] for rel, e in expected.items()}
    return Verification(not problems, problems, recorded, age, files if not problems else {})


def install(dest_root: str | os.PathLike, owned_root: str | os.PathLike) -> None:
    """Copy a verified snapshot into an empty evaluation host root."""
    result = verify(dest_root)
    if not result.ok:
        raise ScopeError("snapshot failed verification: " + "; ".join(result.problems))
    src, owned = Path(dest_root), Path(owned_root)
    for part in ("wisp", "home", "sync"):
        target = owned / part
        if target.exists() and any(target.iterdir()):
            raise ScopeError(f"{part}/ in the evaluation root is not empty")
    for path in sorted(p for p in src.rglob("*") if p.is_file() and p.name != MANIFEST):
        _copy(path, owned / path.relative_to(src))
    (owned / "evidence").mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(src / MANIFEST, owned / "evidence" / MANIFEST)
    os.chmod(owned / "evidence" / MANIFEST, 0o600)


def _installed_files(owned: Path) -> tuple[dict[str, Path], list[str]]:
    """Every file under the installed source layout, and any link found there."""
    files: dict[str, Path] = {}
    problems: list[str] = []
    for part in INSTALLED_PARTS:
        top = owned / part
        if top.is_symlink():
            problems.append(f"{part}/ is a link")
            continue
        if not top.exists():
            continue
        if not top.is_dir():
            problems.append(f"{part} is not a directory")
            continue
        for base, dirs, names in os.walk(top, followlinks=False):
            for name in [*dirs, *names]:
                path = Path(base) / name
                rel = str(path.relative_to(owned))
                if path.is_symlink():
                    problems.append(f"{rel} is a link")
                elif name in names:
                    if path.is_file():
                        files[rel] = path
                    else:
                        problems.append(f"{rel} is not a regular file")
    return files, problems


def verify_installed(owned_root: str | os.PathLike, *, max_age_s: float | None = None,
                     expected_manifest_sha256: str = "") -> Verification:
    """Check the complete installed source layout before any service import.

    Runs before the host imports or migrates anything, so every file under
    wisp/, home/ and sync/ must be a captured file listed in the installed
    manifest, unchanged and unlinked. A root with no manifest is accepted
    only when that layout holds no files at all (an explicitly empty
    synthetic root); populated state without a manifest is refused. When the
    coordinator bound a manifest digest, the installed one must match it.
    """
    owned = Path(owned_root)
    present, problems = _installed_files(owned)
    path = owned / "evidence" / MANIFEST
    if not path.exists() and not path.is_symlink():
        if expected_manifest_sha256:
            problems.append("the coordinator bound a snapshot but none is installed")
        if present:
            problems.append("source state is present without an installed manifest")
        return Verification(not problems, problems, "", None, {}, empty=not problems)
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            return Verification(False, problems + ["installed manifest is not a private regular file"])
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Verification(False, problems + ["installed manifest is unreadable"])
    recorded = manifest.pop("manifest_sha256", "")
    if hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != recorded:
        problems.append("installed manifest was altered")
    if expected_manifest_sha256 and recorded != expected_manifest_sha256:
        problems.append("installed manifest is not the one the coordinator bound")
    if manifest.get("label") != LABEL:
        problems.append("installed manifest is not labelled as a snapshot")
    expected = {e.get("path"): e.get("sha256") for e in manifest.get("files", []) if isinstance(e, dict)}
    for rel in sorted(set(present) - set(expected)):
        problems.append(f"{rel} is not in the installed manifest")
    for rel, digest in sorted(expected.items()):
        if rel not in present:
            problems.append(f"{rel} is missing or not a regular file")
        elif sha256_file(present[rel]) != digest:
            problems.append(f"{rel} changed since capture")
    now = time.time()
    problems += _provenance_problems(manifest, now=now, max_age_s=max_age_s)
    age = now - manifest["finished_at"] if _timestamp(manifest.get("finished_at")) else None
    return Verification(not problems, problems, recorded, age, dict(expected) if not problems else {})
