"""SQLite persistence for long-running research jobs."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from service.paths import MOE_DIR

DB_PATH = MOE_DIR / "research.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS research_jobs (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    state TEXT NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '{}',
    steering TEXT NOT NULL DEFAULT '',
    report_md TEXT NOT NULL DEFAULT '',
    report_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS research_sources (
    job_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'found',
    score REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    quality_class TEXT NOT NULL DEFAULT '',
    quality_reason TEXT NOT NULL DEFAULT '',
    fetched_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, source_id),
    UNIQUE (job_id, canonical_url)
);
CREATE TABLE IF NOT EXISTS research_chunks (
    job_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    text TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, source_id, chunk_id)
);
CREATE TABLE IF NOT EXISTS research_evidence (
    job_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    subquestion_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    quote TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    stance TEXT NOT NULL DEFAULT 'supports',
    confidence REAL NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS research_contradictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    subquestion_id TEXT NOT NULL,
    evidence_id_a TEXT NOT NULL,
    evidence_id_b TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS research_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS research_events_job_seq ON research_events(job_id, seq);
CREATE INDEX IF NOT EXISTS research_sources_job ON research_sources(job_id);
CREATE INDEX IF NOT EXISTS research_evidence_job ON research_evidence(job_id);
CREATE INDEX IF NOT EXISTS research_chunks_job_source ON research_chunks(job_id, source_id);
CREATE INDEX IF NOT EXISTS research_contradictions_job ON research_contradictions(job_id);
"""

# Column additions for databases created before this revision. CREATE TABLE IF
# NOT EXISTS above is a no-op against an existing table, so a live
# ~/.moe/research.db from an earlier MVP build needs these added explicitly.
_MIGRATIONS: dict[str, list[str]] = {
    "research_jobs": [
        "ALTER TABLE research_jobs ADD COLUMN stop_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE research_jobs ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE research_jobs ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0",
    ],
    "research_sources": [
        "ALTER TABLE research_sources ADD COLUMN quality_class TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE research_sources ADD COLUMN quality_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE research_sources ADD COLUMN fetched_at REAL NOT NULL DEFAULT 0",
    ],
    "research_evidence": [
        "ALTER TABLE research_evidence ADD COLUMN verdict TEXT NOT NULL DEFAULT ''",
    ],
}


class ResearchStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._db.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._lock = threading.RLock()

    def _migrate(self) -> None:
        """Add columns introduced after a DB already exists on disk."""
        for table, statements in _MIGRATIONS.items():
            existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for statement in statements:
                column = statement.split("ADD COLUMN", 1)[1].split()[0]
                if column not in existing:
                    self._db.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _decode_job(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("plan_json", "report_json"):
            try:
                out[key.removesuffix("_json")] = json.loads(out.pop(key) or "{}")
            except json.JSONDecodeError:
                out[key.removesuffix("_json")] = {}
        out["cancel_requested"] = bool(out["cancel_requested"])
        out["pause_requested"] = bool(out["pause_requested"])
        out["pinned"] = bool(out["pinned"])
        return out

    def create_job(self, prompt: str, plan: dict | None = None,
                   *, state: str = "planning") -> dict:
        jid = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO research_jobs (id,prompt,state,plan_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (jid, prompt, state, json.dumps(plan or {}), now, now))
            self._db.commit()
        self.event(jid, "created", {"state": state})
        return self.get_job(jid) or {}

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode_job(row)

    def list_jobs(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM research_jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [j for r in rows if (j := self._decode_job(r)) is not None]

    def update_job(self, job_id: str, **fields: Any) -> dict | None:
        allowed = {"state", "steering", "report_md", "error",
                   "cancel_requested", "pause_requested", "stop_reason",
                   "pinned", "model_calls"}
        bool_fields = {"cancel_requested", "pause_requested", "pinned"}
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "plan":
                values["plan_json"] = json.dumps(value)
            elif key == "report":
                values["report_json"] = json.dumps(value)
            elif key in allowed:
                values[key] = int(value) if key in bool_fields else value
        if not values:
            return self.get_job(job_id)
        values["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in values)
        with self._lock:
            self._db.execute(f"UPDATE research_jobs SET {cols} WHERE id=?",
                             (*values.values(), job_id))
            self._db.commit()
        return self.get_job(job_id)

    def event(self, job_id: str, event_type: str, payload: dict | None = None) -> int:
        data = dict(payload or {})
        data.setdefault("type", event_type)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO research_events (job_id,type,payload_json,created_at) VALUES (?,?,?,?)",
                (job_id, event_type, json.dumps(data), time.time()))
            self._db.commit()
            return int(cur.lastrowid)

    def events_after(self, job_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT seq,type,payload_json,created_at FROM research_events "
                "WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                (job_id, after, limit)).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {"type": row["type"]}
            payload.update({"seq": row["seq"], "created_at": row["created_at"]})
            out.append(payload)
        return out

    def event_payloads(self, job_id: str, event_type: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM research_events WHERE job_id=? AND type=? ORDER BY seq",
                (job_id, event_type)).fetchall()
        out = []
        for row in rows:
            try:
                out.append(json.loads(row["payload_json"]))
            except json.JSONDecodeError:
                continue
        return out

    def add_source(self, job_id: str, *, url: str, canonical_url: str,
                   title: str = "", snippet: str = "", query: str = "",
                   domain: str = "", score: float = 0) -> str:
        with self._lock:
            existing = self._db.execute(
                "SELECT source_id FROM research_sources WHERE job_id=? AND canonical_url=?",
                (job_id, canonical_url)).fetchone()
            if existing:
                return str(existing["source_id"])
            count = self._db.execute(
                "SELECT COUNT(*) AS n FROM research_sources WHERE job_id=?", (job_id,)).fetchone()["n"]
            sid = f"S{int(count) + 1}"
            self._db.execute(
                "INSERT INTO research_sources "
                "(job_id,source_id,query,url,canonical_url,domain,title,snippet,score,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, sid, query, url, canonical_url, domain, title, snippet, score, time.time()))
            self._db.commit()
            return sid

    def update_source(self, job_id: str, source_id: str, **fields: Any) -> None:
        allowed = {"url", "canonical_url", "domain", "title", "snippet",
                   "published_at", "content_type", "body", "status", "score", "error",
                   "quality_class", "quality_reason", "fetched_at"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if not values:
            return
        cols = ", ".join(f"{k}=?" for k in values)
        with self._lock:
            self._db.execute(
                f"UPDATE research_sources SET {cols} WHERE job_id=? AND source_id=?",
                (*values.values(), job_id, source_id))
            self._db.commit()

    def sources(self, job_id: str, *, statuses: set[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM research_sources WHERE job_id=?"
        args: list[Any] = [job_id]
        if statuses:
            marks = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({marks})"
            args.extend(sorted(statuses))
        sql += " ORDER BY score DESC, source_id"
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def add_evidence(self, job_id: str, *, source_id: str, subquestion_id: str,
                     claim: str, quote: str, start_offset: int, end_offset: int,
                     stance: str = "supports", confidence: float = 0.0) -> str:
        with self._lock:
            # Exact quote/source pairs are content identities. Do not make the
            # report pay twice because the model paraphrased the same passage.
            old = self._db.execute(
                "SELECT evidence_id FROM research_evidence WHERE job_id=? AND source_id=? AND quote=?",
                (job_id, source_id, quote)).fetchone()
            if old:
                return str(old["evidence_id"])
            count = self._db.execute(
                "SELECT COUNT(*) AS n FROM research_evidence WHERE job_id=?", (job_id,)).fetchone()["n"]
            eid = f"E{int(count) + 1}"
            self._db.execute(
                "INSERT INTO research_evidence "
                "(job_id,evidence_id,source_id,subquestion_id,claim,quote,start_offset,end_offset,stance,confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, eid, source_id, subquestion_id, claim, quote,
                 start_offset, end_offset, stance, confidence))
            self._db.commit()
            return eid

    def evidence(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT e.*,s.url,s.title,s.domain,s.published_at,s.quality_class,s.quality_reason FROM research_evidence e "
                "JOIN research_sources s ON s.job_id=e.job_id AND s.source_id=e.source_id "
                "WHERE e.job_id=? ORDER BY CAST(substr(e.evidence_id,2) AS INTEGER)", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_evidence_verdict(self, job_id: str, evidence_id: str, verdict: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE research_evidence SET verdict=? WHERE job_id=? AND evidence_id=?",
                (verdict, job_id, evidence_id))
            self._db.commit()

    # -- chunks: persisted normalized text spans with exact source offsets --

    def add_chunk(self, job_id: str, source_id: str, chunk_id: str, *,
                  start_offset: int, end_offset: int, text: str, score: float = 0.0) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO research_chunks "
                "(job_id,source_id,chunk_id,start_offset,end_offset,text,score) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_id, source_id, chunk_id, start_offset, end_offset, text, score))
            self._db.commit()

    def chunks(self, job_id: str, source_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM research_chunks WHERE job_id=? AND source_id=? "
                "ORDER BY score DESC, chunk_id", (job_id, source_id)).fetchall()
        return [dict(r) for r in rows]

    # -- contradictions: deterministic conflicts surfaced between sources --

    def add_contradiction(self, job_id: str, *, subquestion_id: str,
                          evidence_id_a: str, evidence_id_b: str, description: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO research_contradictions "
                "(job_id,subquestion_id,evidence_id_a,evidence_id_b,description,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, subquestion_id, evidence_id_a, evidence_id_b, description, time.time()))
            self._db.commit()
            return int(cur.lastrowid)

    def contradictions(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM research_contradictions WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def clear_contradictions(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM research_contradictions WHERE job_id=?", (job_id,))
            self._db.commit()

    # -- job lifecycle helpers --

    def set_pinned(self, job_id: str, pinned: bool) -> dict | None:
        return self.update_job(job_id, pinned=pinned)

    def increment_model_calls(self, job_id: str, by: int = 1) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE research_jobs SET model_calls=model_calls+?, updated_at=? WHERE id=?",
                (by, time.time(), job_id))
            self._db.commit()

    def jobs_in_states(self, states: set[str]) -> list[dict]:
        marks = ",".join("?" for _ in states)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM research_jobs WHERE state IN ({marks})", sorted(states)).fetchall()
        return [j for r in rows if (j := self._decode_job(r)) is not None]

    def delete_job(self, job_id: str) -> None:
        """Permanently remove a job and everything derived from it."""
        with self._lock:
            for table in ("research_jobs", "research_sources", "research_chunks",
                          "research_evidence", "research_contradictions", "research_events"):
                self._db.execute(f"DELETE FROM {table} WHERE {'id' if table == 'research_jobs' else 'job_id'}=?",
                                 (job_id,))
            self._db.commit()

    def purge_stale_bodies(self, older_than_days: float = 30) -> int:
        """Drop cached page bodies for old, unpinned jobs; keep evidence/quotes.

        Evidence quotes already carry the exact supporting passage, so a report's
        citations remain fully auditable after this runs — only the full fetched
        page text (needed for re-extraction, not for what's already cited) is
        reclaimed.
        """
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            stale_jobs = [r["id"] for r in self._db.execute(
                "SELECT id FROM research_jobs WHERE pinned=0 AND updated_at<?", (cutoff,))]
            if not stale_jobs:
                return 0
            marks = ",".join("?" for _ in stale_jobs)
            cur = self._db.execute(
                f"UPDATE research_sources SET body='' WHERE job_id IN ({marks}) AND body<>''",
                stale_jobs)
            self._db.commit()
            return cur.rowcount
