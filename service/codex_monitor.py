"""Read-only overview and change monitor for local Codex tasks.

Codex keeps task metadata and projected turn history in SQLite databases under
``~/.codex``.  Reading those projections is intentionally less invasive than
resuming a task through app-server: Wisp can answer "what are my Codex tasks
doing?" without loading a thread, subscribing to it, or changing any state.

The public App Server protocol remains the source of truth for the data model
(`thread/list`, `thread/read`, and turn lifecycle status).  The local database
reader is a pragmatic desktop integration: it is defensive about absent/newer
schemas and fails closed with a useful availability message.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any


RUNNING = {"inProgress", "running", "active"}
FAILED = {"failed", "error", "systemError", "cancelled", "interrupted"}
COMPLETED = {"completed"}
USER_THREAD_SOURCES = {"user", "cli", "vscode", "appServer", "exec", "unknown", ""}
STALL_AFTER_S = 15 * 60


class CodexMonitorUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexTask:
    id: str
    title: str
    status: str
    updated_at: float
    cwd: str = ""
    branch: str = ""
    latest_update: str = ""
    update_phase: str = ""

    @property
    def running(self) -> bool:
        return self.status in RUNNING

    @property
    def failed(self) -> bool:
        return self.status in FAILED


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _age(timestamp: float, now: float) -> str:
    seconds = max(0, int(now - timestamp))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


class CodexMonitor:
    def __init__(self, codex_dir: Path | None = None,
                 state_path: Path | None = None) -> None:
        configured = os.environ.get("CODEX_HOME")
        self.codex_dir = Path(codex_dir or configured or (Path.home() / ".codex"))
        if state_path is None:
            from service.paths import MOE_DIR
            state_path = MOE_DIR / "codex_monitor_state.json"
        self.state_path = Path(state_path)

    def _latest_db(self, pattern: str) -> Path:
        candidates = [path for path in self.codex_dir.glob(pattern)
                      if path.is_file() and not path.name.endswith(("-wal", "-shm"))]
        if not candidates:
            raise CodexMonitorUnavailable(
                f"Codex's local {pattern} database was not found in {self.codex_dir}")
        def rank(path: Path) -> tuple[int, float]:
            try:
                version = int(path.stem.rsplit("_", 1)[-1])
            except ValueError:
                version = -1
            return version, path.stat().st_mtime
        return max(candidates, key=rank)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        # URI read-only mode prevents an accidental journal/schema write.  A
        # short busy timeout lets Codex finish a commit instead of making a
        # transient WAL lock look like a missing task list.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def tasks(self, *, include_subagents: bool = False) -> list[CodexTask]:
        state_db = self._latest_db("state_*.sqlite")
        history_db = self._latest_db("thread_history_*.sqlite")
        try:
            with self._connect(state_db) as state:
                columns = self._columns(state, "threads")
                required = {"id", "updated_at", "archived"}
                if not required.issubset(columns):
                    raise CodexMonitorUnavailable("Codex's task index has an unsupported schema")
                title_expr = ("COALESCE(NULLIF(name, ''), NULLIF(title, ''), "
                              "NULLIF(preview, ''), id)" if {"name", "title", "preview"}.issubset(columns)
                              else "id")
                source_expr = "COALESCE(thread_source, '')" if "thread_source" in columns else "''"
                cwd_expr = "COALESCE(cwd, '')" if "cwd" in columns else "''"
                branch_expr = "COALESCE(git_branch, '')" if "git_branch" in columns else "''"
                rows = state.execute(
                    f"SELECT id, {title_expr} AS display_title, updated_at, "
                    f"{source_expr} AS thread_source, {cwd_expr} AS cwd, "
                    f"{branch_expr} AS branch FROM threads WHERE archived = 0 "
                    "ORDER BY updated_at DESC"
                ).fetchall()

            with self._connect(history_db) as history:
                turns = history.execute(
                    "SELECT t.thread_id, t.status, COALESCE(t.completed_at, t.started_at, 0) AS activity "
                    "FROM thread_turns t JOIN (SELECT thread_id, MAX(rollout_ordinal) AS ordinal "
                    "FROM thread_turns GROUP BY thread_id) latest "
                    "ON latest.thread_id=t.thread_id AND latest.ordinal=t.rollout_ordinal"
                ).fetchall()
                turn_by_thread = {row["thread_id"]: row for row in turns}
                messages = history.execute(
                    "SELECT i.thread_id, i.item_json, i.created_at_ms FROM thread_items i "
                    "JOIN (SELECT thread_id, MAX(rollout_ordinal) AS ordinal FROM thread_items "
                    "WHERE item_type='agentMessage' GROUP BY thread_id) latest "
                    "ON latest.thread_id=i.thread_id AND latest.ordinal=i.rollout_ordinal"
                ).fetchall()
                message_by_thread = {row["thread_id"]: row for row in messages}
        except sqlite3.Error as exc:
            raise CodexMonitorUnavailable(f"Codex's task index could not be read: {exc}") from exc

        result: list[CodexTask] = []
        for row in rows:
            source = str(row["thread_source"] or "")
            if not include_subagents and source not in USER_THREAD_SOURCES:
                continue
            turn = turn_by_thread.get(row["id"])
            status = str(turn["status"] if turn else "unknown")
            updated = float(row["updated_at"] or 0)
            if turn:
                updated = max(updated, float(turn["activity"] or 0))
            latest_text = phase = ""
            message = message_by_thread.get(row["id"])
            if message:
                updated = max(updated, float(message["created_at_ms"] or 0) / 1000)
                try:
                    payload = json.loads(message["item_json"])
                    latest_text = _clip(payload.get("text") or "", 280)
                    phase = str(payload.get("phase") or "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            result.append(CodexTask(
                id=str(row["id"]), title=_clip(row["display_title"], 100),
                status=status, updated_at=updated, cwd=str(row["cwd"] or ""),
                branch=str(row["branch"] or ""), latest_update=latest_text,
                update_phase=phase,
            ))
        return sorted(result, key=lambda task: task.updated_at, reverse=True)

    def render(self, *, view: str = "recent", hours: int = 24,
               limit: int = 20, include_subagents: bool = False,
               now: float | None = None) -> str:
        now = now or time.time()
        tasks = self.tasks(include_subagents=include_subagents)
        cutoff = now - max(1, hours) * 3600
        if view == "active":
            shown = [task for task in tasks if task.running]
        elif view == "attention":
            shown = [task for task in tasks if task.failed or
                     (task.running and now - task.updated_at >= STALL_AFTER_S)]
        elif view == "all":
            shown = tasks
        else:
            shown = [task for task in tasks if task.running or task.updated_at >= cutoff]
        shown = shown[:max(1, min(int(limit), 100))]
        if not shown:
            labels = {"active": "running", "attention": "needing attention",
                      "recent": f"updated in the last {max(1, hours)} hours"}
            return f"No Codex tasks are {labels.get(view, 'in this view')}."

        running = sum(task.running for task in shown)
        failed = sum(task.failed for task in shown)
        complete = sum(task.status in COMPLETED for task in shown)
        other = len(shown) - running - failed - complete
        counts = [f"{running} running", f"{complete} finished",
                  f"{failed} failed or interrupted"]
        if other:
            counts.append(f"{other} status unknown")
        lines = [f"Codex task overview: {', '.join(counts)} ({len(shown)} shown)."]
        for task in shown:
            if task.running and now - task.updated_at >= STALL_AFTER_S:
                status = "RUNNING · possibly stalled"
            elif task.running:
                status = "RUNNING"
            elif task.failed:
                status = task.status.upper()
            elif task.status in COMPLETED:
                status = "FINISHED"
            else:
                status = task.status.upper()
            location = Path(task.cwd).name if task.cwd else ""
            context = " · ".join(part for part in (location, task.branch) if part)
            suffix = f" · {context}" if context else ""
            lines.append(f"\n[{status} · {_age(task.updated_at, now)}{suffix}] {task.title}")
            if task.latest_update:
                label = "Latest" if task.running else "Result"
                lines.append(f"  {label}: {task.latest_update}")
            lines.append(f"  Task ID: {task.id}")
        return "\n".join(lines)

    def _load_snapshot(self) -> tuple[dict[str, dict[str, Any]], float, bool]:
        try:
            with self.state_path.open() as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), dict):
                return {}, 0, False
            return payload["tasks"], float(payload.get("saved_at") or 0), True
        except (OSError, ValueError, TypeError):
            return {}, 0, False

    def _save_snapshot(self, tasks: list[CodexTask], *, now: float,
                       previous: dict[str, dict[str, Any]]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        task_rows = {}
        for task in tasks:
            row = asdict(task)
            row["stalled_alerted"] = bool(
                task.running and now - task.updated_at >= STALL_AFTER_S)
            task_rows[task.id] = row
        if task_rows == previous:
            return
        payload = {"version": 1, "saved_at": now, "tasks": task_rows}
        fd, raw_path = tempfile.mkstemp(prefix="codex-monitor-", suffix=".json",
                                        dir=self.state_path.parent)
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2)
            path.replace(self.state_path)
        finally:
            path.unlink(missing_ok=True)

    def poll_events(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Return meaningful changes since the previous poll, then checkpoint.

        First use establishes a baseline. Commentary deltas are deliberately
        quiet; the user gets an alert only for a terminal transition or once
        when an active task crosses the stalled threshold.
        """
        now = now or time.time()
        tasks = self.tasks(include_subagents=False)
        previous, saved_at, has_baseline = self._load_snapshot()
        events: list[dict[str, Any]] = []
        if has_baseline:
            for task in tasks:
                old = previous.get(task.id)
                if not old:
                    # A short task can start and finish entirely between two
                    # 30-second polls. Surface new terminal rows created after
                    # the checkpoint, but never replay old work on first use.
                    if ((task.status in COMPLETED or task.failed)
                            and task.updated_at >= saved_at):
                        kind = "failed" if task.failed else "completed"
                        events.append({
                            "type": "codex_task_update", "kind": kind,
                            "thread_id": task.id, "title": task.title,
                            "status": task.status, "latest_update": task.latest_update,
                        })
                    continue
                old_status = str(old.get("status") or "unknown")
                kind = ""
                if (old_status in RUNNING
                        and (task.status in COMPLETED or task.failed)):
                    kind = "failed" if task.failed else "completed"
                elif (task.running and now - task.updated_at >= STALL_AFTER_S
                      and not bool(old.get("stalled_alerted"))):
                    kind = "stalled"
                if kind:
                    events.append({
                        "type": "codex_task_update", "kind": kind,
                        "thread_id": task.id, "title": task.title,
                        "status": task.status, "latest_update": task.latest_update,
                    })
        self._save_snapshot(tasks, now=now, previous=previous)
        return events


codex_monitor = CodexMonitor()
