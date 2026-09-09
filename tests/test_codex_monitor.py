from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from service.codex_monitor import CodexMonitor


def _fixture(root: Path, now: int) -> tuple[Path, Path]:
    state = root / "state_1.sqlite"
    history = root / "thread_history_1.sqlite"
    with sqlite3.connect(state) as db:
        db.execute("""CREATE TABLE threads (
            id TEXT PRIMARY KEY, updated_at INTEGER, archived INTEGER,
            name TEXT, title TEXT, preview TEXT, thread_source TEXT,
            cwd TEXT, git_branch TEXT)""")
        db.executemany("INSERT INTO threads VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)", [
            ("running", now - 30, "Build monitor", "", "", "user", "/repo/wisp", "codex/monitor"),
            ("done", now - 90, "Fix tests", "", "", "user", "/repo/wisp", ""),
            ("child", now - 10, "Internal helper", "", "", "subagent", "/repo/wisp", ""),
        ])
    with sqlite3.connect(history) as db:
        db.execute("""CREATE TABLE thread_turns (
            thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
            status TEXT, started_at INTEGER, completed_at INTEGER)""")
        db.execute("""CREATE TABLE thread_items (
            thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER,
            created_at_ms INTEGER, item_json TEXT, item_type TEXT)""")
        db.executemany("INSERT INTO thread_turns VALUES (?, ?, 1, ?, ?, ?)", [
            ("running", "turn-r", "inProgress", now - 30, None),
            ("done", "turn-d", "completed", now - 120, now - 90),
            ("child", "turn-c", "inProgress", now - 10, None),
        ])
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?, 1, ?, ?, 'agentMessage')", [
            ("running", "turn-r", "msg-r", (now - 30) * 1000,
             json.dumps({"type": "agentMessage", "phase": "commentary",
                         "text": "Implementing the SQLite reader."})),
            ("done", "turn-d", "msg-d", (now - 90) * 1000,
             json.dumps({"type": "agentMessage", "phase": "final",
                         "text": "All tests pass."})),
        ])
    return state, history


def test_render_recent_excludes_subagents_and_shows_latest_update(tmp_path: Path):
    now = 2_000_000_000
    _fixture(tmp_path, now)
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    text = monitor.render(view="recent", now=now)
    assert "1 running, 1 finished" in text
    assert "Build monitor" in text
    assert "Implementing the SQLite reader" in text
    assert "Fix tests" in text
    assert "Internal helper" not in text


def test_active_and_attention_views(tmp_path: Path):
    now = 2_000_000_000
    _fixture(tmp_path, now)
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    assert "Build monitor" in monitor.render(view="active", now=now)
    assert "No Codex tasks are needing attention" in monitor.render(view="attention", now=now)
    assert "possibly stalled" in monitor.render(view="attention", now=now + 16 * 60)


def test_poll_baselines_then_reports_completion_once(tmp_path: Path):
    now = 2_000_000_000
    _, history = _fixture(tmp_path, now)
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    assert monitor.poll_events(now=now) == []
    with sqlite3.connect(history) as db:
        db.execute("UPDATE thread_turns SET status='completed', completed_at=? "
                   "WHERE thread_id='running'", (now + 20,))
    events = monitor.poll_events(now=now + 20)
    assert [(event["kind"], event["thread_id"]) for event in events] == [("completed", "running")]
    assert monitor.poll_events(now=now + 21) == []


def test_poll_reports_stall_once(tmp_path: Path):
    now = 2_000_000_000
    _fixture(tmp_path, now)
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    assert monitor.poll_events(now=now) == []
    events = monitor.poll_events(now=now + 16 * 60)
    assert [(event["kind"], event["thread_id"]) for event in events] == [("stalled", "running")]
    assert monitor.poll_events(now=now + 17 * 60) == []


def test_poll_catches_task_that_starts_and_finishes_between_polls(tmp_path: Path):
    now = 2_000_000_000
    state, history = _fixture(tmp_path, now)
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    assert monitor.poll_events(now=now) == []
    with sqlite3.connect(state) as db:
        db.execute("INSERT INTO threads VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)",
                   ("fast", now + 10, "Fast finished task", "", "", "user", "/repo/wisp", ""))
    with sqlite3.connect(history) as db:
        db.execute("INSERT INTO thread_turns VALUES (?, ?, 1, ?, ?, ?)",
                   ("fast", "turn-fast", "completed", now + 2, now + 10))
    events = monitor.poll_events(now=now + 20)
    assert [(event["kind"], event["thread_id"]) for event in events] == [("completed", "fast")]
    assert monitor.poll_events(now=now + 21) == []


def test_router_sends_codex_status_requests_directly_to_monitor():
    from service.router.router import rule_route

    active = rule_route("Which Codex chats are still running?")
    assert active is not None
    assert active.direct_calls == [("get_codex_updates", {"view": "active"})]
    attention = rule_route("Does any Codex task need me or look stalled?")
    assert attention is not None
    assert attention.direct_calls == [("get_codex_updates", {"view": "attention"})]
