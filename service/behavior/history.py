"""Resumable read-only native history imports, separate from the live agenda.

The reader polls one bounded date range and acknowledges it with a complete
batch. Range, scan identity and cursor are checked before one SQLite transaction
publishes observations, coverage and progress. Retried pages are idempotent.
"""

from __future__ import annotations

import json
import hashlib
import math
import uuid

from service.behavior.store import _obs_row
from service.behavior.types import Observation

SOURCES = {"calendar", "completed_reminders"}
# A calendar has no public "oldest event" query. Walk the supported Gregorian
# history in small ranges rather than treating the first empty range as the end.
EARLIEST = -62135596800.0  # 0001-01-01 UTC; explicit in every status response
PAGE_SECONDS = 3 * 365 * 86400.0  # below EventKit's four-year predicate ceiling
MAX_ROWS = 10000


def number(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("Finite timestamp required")
    return float(value)


class History:
    def __init__(self, store):
        self.store = store

    def status(self):
        return {
            source: self.store.meta_get("history:" + source)
            for source in sorted(SOURCES)
        }

    def next(self, source, now):
        if source not in SOURCES:
            raise ValueError("Unsupported history source")
        with self.store._lock:
            key = "history:" + source
            job = self.store.meta_get(key)
            if job is None:
                job = {
                    "scan_id": uuid.uuid4().hex,
                    "source": source,
                    "state": "scanning",
                    "start_ts": EARLIEST,
                    "end_ts": now,
                    "cursor": now,
                    "pages": 0,
                    "rows": 0,
                    "started_at": now,
                    "error": "",
                }
                self.store.meta_set(key, job)
            elif (
                source == "completed_reminders"
                and job["state"] == "complete"
                and now - job.get("updated_at", now) >= 300
            ):
                job.update(
                    scan_id=uuid.uuid4().hex,
                    state="scanning",
                    start_ts=max(EARLIEST, job["end_ts"] - 86400),
                    end_ts=now,
                    cursor=now,
                    pages=0,
                    rows=0,
                    error="",
                    mode="incremental",
                )
                self.store.meta_set(key, job)
            result = dict(job)
            if job["state"] == "scanning":
                span = job.get(
                    "page_seconds",
                    (
                        job["cursor"] - job["start_ts"]
                        if source == "completed_reminders"
                        else PAGE_SECONDS
                    ),
                )
                result.update(
                    page_start=max(job["start_ts"], job["cursor"] - span),
                    page_end=job["cursor"],
                )
            return result

    def control(self, source, command, now):
        if source not in SOURCES or command not in {"pause", "resume", "restart"}:
            raise ValueError("Invalid history control")
        with self.store._lock:
            self.next(source, now)
            key = "history:" + source
            job = self.store.meta_get(key)
            if command == "restart":
                job.update(
                    scan_id=uuid.uuid4().hex,
                    state="scanning",
                    cursor=now,
                    end_ts=now,
                    pages=0,
                    rows=0,
                    started_at=now,
                    error="",
                )
            elif command == "pause":
                job.update(state="paused")
            elif job["cursor"] > job["start_ts"]:
                job.update(state="scanning", error="")
            self.store.meta_set(key, job)
            return job

    def ingest(self, source, payload, now, prepare):
        if source not in SOURCES or not isinstance(payload, dict):
            raise ValueError("Invalid history source or batch")
        rows = payload.get("rows")
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError("History batch exceeds row limit")
        start, end = number(payload.get("start_ts")), number(payload.get("end_ts"))
        scan_id = payload.get("scan_id")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        # Validate the ENTIRE page before touching the DB/cursor.
        observations = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid history row")
            sid, title = row.get("source_id"), row.get("title")
            if (
                not isinstance(sid, str)
                or not 0 < len(sid) <= 1000
                or not isinstance(title, str)
                or not 0 < len(title) <= 1000
            ):
                raise ValueError("History rows require a stable identity and title")
            if source == "calendar":
                ts = number(row.get("when_ts"))
                finish = number(row.get("end_ts"))
                if finish < ts or not (ts < end and finish > start):
                    raise ValueError("Event outside requested range")
                observations.append(
                    prepare(
                        Observation(
                            id=f"cal:{sid}:{int(ts)}",
                            kind="event",
                            ts=ts,
                            end_ts=finish,
                            subject=title,
                            source="calendar",
                            source_id=sid,
                            attrs={
                                "calendar": str(row.get("context") or "")[:500],
                                "all_day": row.get("all_day") is True,
                                "evidence_basis": "scheduled",
                                "outcome": "unknown",
                            },
                        )
                    )
                )
            else:
                ts = number(row.get("completion_ts"))
                if not start <= ts < end or ts > now:
                    raise ValueError("Completion outside requested range")
                observations.append(
                    prepare(
                        Observation(
                            id=f"completed:{sid}:{ts:.6f}",
                            kind="reminder_done",
                            ts=ts,
                            subject=title,
                            source="reminders",
                            source_id=sid,
                            attrs={
                                "completion_ts": ts,
                                "completion_time_known": True,
                                "list": str(row.get("context") or "")[:500],
                                "status": "done",
                                "evidence_basis": "recorded_completion",
                            },
                        )
                    )
                )
        with self.store._lock, self.store._conn:
            db = self.store._conn
            key = "history:" + source
            job = self.store.meta_get(key)
            if not job or job["scan_id"] != scan_id:
                raise ValueError("History scan changed or stopped")
            if (
                end == job.get("last_end")
                and start == job.get("last_start")
                and digest == job.get("last_digest")
            ):
                return dict(job)
            if job["state"] != "scanning":
                raise ValueError("History scan changed or stopped")
            span = job.get(
                "page_seconds",
                (
                    job["cursor"] - job["start_ts"]
                    if source == "completed_reminders"
                    else PAGE_SECONDS
                ),
            )
            expected_start = max(job["start_ts"], job["cursor"] - span)
            if (
                end <= job["cursor"]
                and end == job["cursor"]
                and start == expected_start
            ):
                pass
            else:
                raise ValueError("Stale history page")
            if payload.get("complete") is not True:
                if payload.get("reason") == "row_limit" and span > 86400:
                    job.update(
                        state="scanning",
                        page_seconds=max(86400, min(PAGE_SECONDS, span / 2)),
                        error="Reducing history batch size; no records skipped",
                    )
                else:
                    job.update(
                        state="paused",
                        error="Source history is unavailable or incomplete; known progress was retained",
                    )
            else:
                # Retention still applies: old pages establish source scan progress,
                # not indefinite retention of personal records or current routines.
                retain_after = now - 180 * 86400
                kept = [
                    _obs_row(o)
                    for o in observations
                    if o.ts >= retain_after or (o.end_ts or 0) > retain_after
                ]
                db.executemany(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "kind=excluded.kind,ts=excluded.ts,end_ts=excluded.end_ts,subject=excluded.subject,"
                    "source=excluded.source,category=excluded.category,source_id=excluded.source_id,attrs=excluded.attrs",
                    kept,
                )
                self.store._write_coverage(source, start, end, "complete", now)
                job.update(
                    cursor=start,
                    pages=job["pages"] + 1,
                    rows=job["rows"] + len(rows),
                    state="complete" if start <= job["start_ts"] else "scanning",
                    error="",
                    last_start=start,
                    last_end=end,
                    last_digest=digest,
                    updated_at=now,
                    retained_rows=len(kept),
                )
            db.execute(
                "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(job)),
            )
            return dict(job)
