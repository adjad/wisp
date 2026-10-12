"""Bounded user-managed snapshots. No automatic indexing or external effects.

Records are historical evidence, never instructions or proof of current state.
Source identity is resolved by code before saving, not accepted from a model.
Storage is separate JSON state: no existing database or migration is touched.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from service.file_organization import open_directory

MAX_TEXT_BYTES = 16_384
MAX_STATE_BYTES = 1_048_576
MAX_RECORDS = 128
KINDS = {"knowledge", "style_example", "followup_candidate"}
_ID = re.compile(r"[0-9a-f]{32}")


class KnowledgeWriteUnknown(OSError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bounded_text(value, name, maximum=MAX_TEXT_BYTES):
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value.encode("utf-8")) > maximum):
        raise ValueError(f"Provide nonempty {name} within {maximum} UTF-8 bytes.")
    return value


def revision(record):
    return digest(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False))


class KnowledgeStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def _locked(self):
        # Pinned, no-follow directory traversal; all filenames below are fixed.
        with open_directory(self.root, create=True) as (directory, _):
            if os.fstat(directory).st_uid != os.getuid():
                raise ValueError("Knowledge directory is not owned by this user.")
            os.fchmod(directory, 0o700)
            lock = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(lock).st_mode):
                    raise ValueError("Knowledge lock must be a regular file.")
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield directory
            finally:
                os.close(lock)

    def _load(self, directory):
        try:
            fd = os.open("records.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return []
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise ValueError("Knowledge state must be a regular file.")
            with os.fdopen(fd, "rb") as source:
                raw = source.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError("Knowledge state exceeds its size limit.")
            state = json.loads(raw)
            if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("records"), list):
                raise ValueError("Invalid knowledge state.")
            rows = state["records"]
            if len(rows) > MAX_RECORDS:
                raise ValueError("Knowledge state exceeds its record limit.")
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid knowledge record.")
                rid = row.get("id", "")
                if not isinstance(rid, str) or not _ID.fullmatch(rid) or rid in seen:
                    raise ValueError("Invalid or duplicate knowledge identity.")
                seen.add(rid)
                bounded_text(row.get("title"), "title", 200)
                bounded_text(row.get("text"), "text")
                if row.get("kind") not in KINDS:
                    raise ValueError("Invalid knowledge kind.")
                for key in ("created_at", "updated_at"):
                    stamp = row.get(key)
                    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
                        raise ValueError("Invalid knowledge timestamp.")
                provenance = row.get("provenance")
                if (not isinstance(provenance, dict) or provenance.get("quote") != row["text"]
                        or provenance.get("sha256") != digest(row["text"])
                        or provenance.get("kind") not in {"user_request", "email", "messages"}
                        or not isinstance(provenance.get("source_id"), str)
                        or not provenance["source_id"]):
                    raise ValueError("Invalid knowledge provenance.")
            return rows
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid knowledge state; nothing changed.") from exc

    def _write(self, directory, rows):
        raw = json.dumps({"version": 1, "records": rows}, ensure_ascii=False,
                         sort_keys=True, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("Knowledge state exceeds its size limit; nothing changed.")
        name = ".pending-" + secrets.token_hex(16)
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        committed = False
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, "records.json", src_dir_fd=directory, dst_dir_fd=directory)
            committed = True
            os.fsync(directory)
            # A pinned fd remains writable if another process relocates the
            # directory. Success must refer to the configured visible store.
            with open_directory(self.root, create=False) as (visible, _):
                pinned_stat, visible_stat = os.fstat(directory), os.fstat(visible)
                if (pinned_stat.st_dev, pinned_stat.st_ino) != (visible_stat.st_dev, visible_stat.st_ino):
                    raise OSError("Knowledge directory changed during publication.")
        except OSError as exc:
            if committed:
                raise KnowledgeWriteUnknown("Knowledge write outcome unknown; state may have changed. Review it before any retry.") from exc
            raise
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def read(self, query="", kind="", record_id="", limit=20):
        if not isinstance(query, str) or len(query) > 1000 or kind not in KINDS | {""}:
            raise ValueError("Invalid knowledge search.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be 1–50.")
        if record_id and (not isinstance(record_id, str) or not _ID.fullmatch(record_id)):
            raise ValueError("Use an exact returned knowledge ID.")
        terms = set(re.findall(r"\w+", query.casefold()))
        with self._locked() as directory:
            rows = self._load(directory)
        selected = [row for row in rows if (not kind or row["kind"] == kind)
                    and (not record_id or row["id"] == record_id)
                    and terms <= set(re.findall(r"\w+", (row["title"] + " " + row["text"]).casefold()))]
        selected.sort(key=lambda row: (-row["updated_at"], row["id"]))
        return {"records": [{**row, "revision": revision(row)} for row in selected[:limit]],
                "omitted": max(0, len(selected) - limit), "scope": "saved snapshots only; no live sources searched"}

    def save(self, *, title, text, kind, provenance, record_id="", expected_revision=""):
        bounded_text(title, "title", 200)
        bounded_text(text, "text")
        if (not isinstance(kind, str) or kind not in KINDS or not isinstance(provenance, dict)
                or provenance.get("kind") not in {"user_request", "email", "messages"}
                or not isinstance(provenance.get("source_id"), str) or not provenance["source_id"]
                or provenance.get("quote") != text or provenance.get("sha256") != digest(text)):
            raise ValueError("Invalid kind or source evidence.")
        if record_id and (not _ID.fullmatch(record_id) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision)):
            raise ValueError("An edit needs the exact ID and current revision.")
        with self._locked() as directory:
            rows = self._load(directory)
            old = next((row for row in rows if row["id"] == record_id), None) if record_id else None
            if record_id and (old is None or revision(old) != expected_revision):
                raise ValueError("Knowledge changed or disappeared; review it again before editing.")
            if not record_id:
                for row in rows:
                    if (row["title"] == title and row["text"] == text and row["kind"] == kind
                            and all(row["provenance"].get(key) == provenance.get(key)
                                    for key in ("kind", "source_id", "container_id", "session_id"))):
                        return {**row, "revision": revision(row)}
            if not old and len(rows) >= MAX_RECORDS:
                raise ValueError("Knowledge record limit reached; delete an explicitly chosen record first.")
            rid = record_id or secrets.token_hex(16)
            if not old and any(row["id"] == rid for row in rows):
                raise ValueError("Knowledge identity collision; nothing changed.")
            now = time.time()
            record = {"id": rid, "title": title, "text": text, "kind": kind,
                      "created_at": old["created_at"] if old else now, "updated_at": now,
                      "provenance": provenance}
            self._write(directory, [row for row in rows if row["id"] != rid] + [record])
        return {**record, "revision": revision(record)}

    def delete(self, record_id, expected_revision):
        if not isinstance(record_id, str) or not _ID.fullmatch(record_id):
            raise ValueError("Use an exact returned knowledge ID.")
        with self._locked() as directory:
            rows = self._load(directory)
            old = next((row for row in rows if row["id"] == record_id), None)
            if old is None or revision(old) != expected_revision:
                raise ValueError("Knowledge changed or disappeared; review it again before deleting.")
            self._write(directory, [row for row in rows if row["id"] != record_id])
        return {"id": record_id, "status": "deleted"}


def selected_source(kind, source_id, container_id, *, style=False):
    """Resolve one exact native cache identity; never follows links or refreshes."""
    bounded_text(source_id, "source ID", 500)
    bounded_text(container_id, "account/chat ID", 500)
    if kind == "email":
        if style:
            raise ValueError("This Mail cache cannot prove a writing example is user-authored.")
        from service.tools import email_tools as mail
        snapshot = mail.raw_email_snapshot()
        if snapshot is None:
            raise ValueError("A fresh complete Mail body snapshot is required.")
        rows = [row for row in snapshot.rows if row["message_id"] == source_id and row["account_id"] == container_id]
        if len(rows) != 1:
            raise ValueError("Exact account/message source is missing or ambiguous.")
        text = rows[0]["body"]
        note = "Historical cached Mail body; server completeness and current truth are not established."
    elif kind == "messages":
        from service.tools.imessage_tools import structured_messages_snapshot
        snapshot = structured_messages_snapshot()
        if snapshot["state"] != "ready" or snapshot["coverage"].get("malformed", 0):
            raise ValueError("Current structured Messages evidence is unavailable or malformed.")
        rows = [row for row in snapshot["records"] if row["guid"] == source_id and row["conversation"] == container_id]
        if len(rows) != 1 or rows[0]["status"] != "complete":
            raise ValueError("Exact complete chat/message source is missing or ambiguous.")
        if style and rows[0]["direction"] != "outgoing":
            raise ValueError("A writing example must be an outgoing user message.")
        text = rows[0]["text"]
        note = "Historical cached Messages record; conversation coverage may be partial."
    else:
        raise ValueError("This source has no validated knowledge identity adapter; nothing imported.")
    bounded_text(text, "source body")
    provenance = {"kind": kind, "source_id": source_id, "container_id": container_id,
                  "quote": text, "sha256": digest(text), "observed_at": time.time(), "note": note}
    if kind == "email":
        provenance.update(snapshot_generation=snapshot.generation,
                          snapshot_sha256=snapshot.raw_digest, snapshot_at=snapshot.synced_at)
    return text, provenance
