from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from service.codex_monitor import CodexMonitor, CodexMonitorUnavailable


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


def _resume(state: Path, history: Path, thread: str, turn: str, start: int) -> None:
    with sqlite3.connect(history) as db:
        db.execute("INSERT INTO thread_turns VALUES (?, ?, 2, 'inProgress', ?, NULL)", (thread, turn, start))
    with sqlite3.connect(state) as db:
        db.execute("UPDATE threads SET updated_at=? WHERE id=?", (start, thread))


def _finish(state: Path, history: Path, thread: str, turn: str, at: int, status: str = "completed") -> None:
    with sqlite3.connect(history) as db:
        db.execute("UPDATE thread_turns SET status=?, completed_at=? WHERE thread_id=? AND turn_id=?",
                   (status, at, thread, turn))
    with sqlite3.connect(state) as db:
        db.execute("UPDATE threads SET updated_at=? WHERE id=?", (at, thread))


def test_a_resumed_task_announces_completion_once_per_cooldown(tmp_path: Path):
    """One long-lived task used to alert on every turn it finished (55 in three days)."""
    from service.codex_monitor import COMPLETED_COOLDOWN_S
    now = 2_000_000_000
    state, history = _fixture(tmp_path, now)
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    assert monitor.poll_events(now=now) == []
    _finish(state, history, "running", "turn-r", now + 20)
    assert [e["kind"] for e in monitor.poll_events(now=now + 20)] == ["completed"]
    # Resumed and finished again an hour later: the completion is quiet.
    _resume(state, history, "running", "turn-2", now + 3600)
    assert monitor.poll_events(now=now + 3600) == []
    _finish(state, history, "running", "turn-2", now + 3700)
    assert monitor.poll_events(now=now + 3700) == []
    # Past the cooldown it announces again.
    later = now + 20 + COMPLETED_COOLDOWN_S + 60
    _resume(state, history, "running", "turn-3", later - 30)
    assert monitor.poll_events(now=later - 30) == []
    _finish(state, history, "running", "turn-3", later)
    assert [e["kind"] for e in monitor.poll_events(now=later)] == ["completed"]


def test_cooldown_never_holds_back_a_failure(tmp_path: Path):
    now = 2_000_000_000
    state, history = _fixture(tmp_path, now)
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    assert monitor.poll_events(now=now) == []
    _finish(state, history, "running", "turn-r", now + 20)
    assert [e["kind"] for e in monitor.poll_events(now=now + 20)] == ["completed"]
    _resume(state, history, "running", "turn-2", now + 60)
    assert monitor.poll_events(now=now + 60) == []
    _finish(state, history, "running", "turn-2", now + 90, status="failed")
    assert [e["kind"] for e in monitor.poll_events(now=now + 90)] == ["failed"]


def test_history_selection_keeps_all_user_states_and_latest_ordinals(tmp_path: Path):
    now = 2_000_000_000
    state, history = _fixture(tmp_path, now)
    with sqlite3.connect(state) as db:
        db.execute("INSERT INTO threads VALUES (?, ?, 0, ?, '', '', 'user', '', '')",
                   ("missing' OR 1=1 --", now, "No history"))
        db.execute("INSERT INTO threads VALUES ('archived', ?, 1, 'Archived', '', '', 'user', '', '')",
                   (now,))
    with sqlite3.connect(history) as db:
        # The ordinal, not the most recent clock, determines the latest turn.
        db.execute("INSERT INTO thread_turns VALUES ('running', 'new', 2, 'failed', ?, ?)",
                   (now - 1000, now - 900))
        db.execute("INSERT INTO thread_items VALUES ('running', 'new', 'old-clock', 2, ?, ?, 'agentMessage')",
                   ((now - 1000) * 1000, json.dumps({"text": "Latest by ordinal", "phase": "commentary"})))
        db.execute("INSERT INTO thread_items VALUES ('running', 'new', 'tool', 3, ?, '{}', 'toolCall')",
                   (now * 1000,))
        db.execute("INSERT INTO thread_items VALUES ('done', 'turn-d', 'malformed', 2, ?, '{', 'agentMessage')",
                   ((now - 20) * 1000,))
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    tasks = {task.id: task for task in monitor.tasks()}
    assert set(tasks) == {"running", "done", "missing' OR 1=1 --"}
    assert tasks["running"].status == "failed"
    assert tasks["running"].latest_update == "Latest by ordinal"
    assert tasks["running"].update_phase == "commentary"
    assert tasks["done"].status == "completed"
    assert tasks["done"].latest_update == ""
    assert tasks["done"].updated_at == now - 20
    assert tasks["missing' OR 1=1 --"].status == "unknown"
    assert "child" in {task.id for task in monitor.tasks(include_subagents=True)}
    assert "archived" not in {task.id for task in monitor.tasks(include_subagents=True)}


def test_history_queries_batch_large_eligible_sets(tmp_path: Path):
    now = 2_000_000_000
    state, history = _fixture(tmp_path, now)
    names = [f"extra-{i}" for i in range(1001)]
    with sqlite3.connect(state) as db:
        db.executemany("INSERT INTO threads VALUES (?, ?, 0, ?, '', '', 'user', '', '')",
                       [(name, now, name) for name in names])
    with sqlite3.connect(history) as db:
        db.executemany("INSERT INTO thread_turns VALUES (?, 'new', 3, 'completed', ?, ?)",
                       [(name, now - 10, now) for name in names])
        db.executemany("INSERT INTO thread_items VALUES (?, 'new', 'answer', 4, ?, ?, 'agentMessage')",
                       [(name, now * 1000, json.dumps({"text": name, "phase": "final"})) for name in names])
    tasks = {task.id: task for task in CodexMonitor(tmp_path, tmp_path / "snapshot.json").tasks()}
    assert len(tasks) == 1003
    assert all(tasks[name].status == "completed" and tasks[name].latest_update == name for name in names)


@pytest.mark.parametrize("damage", ["turn-table", "message-table", "turn-column", "message-column"])
def test_empty_selection_still_rejects_unsupported_history(tmp_path: Path, damage: str):
    now = 2_000_000_000
    state, history = _fixture(tmp_path, now)
    with sqlite3.connect(state) as db:
        db.execute("UPDATE threads SET archived=1")
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    assert monitor.tasks() == []
    with sqlite3.connect(history) as db:
        if damage == "turn-table":
            db.execute("DROP TABLE thread_turns")
        elif damage == "message-table":
            db.execute("DROP TABLE thread_items")
        elif damage == "turn-column":
            db.execute("ALTER TABLE thread_turns RENAME COLUMN status TO unsupported_status")
        else:
            db.execute("ALTER TABLE thread_items RENAME COLUMN item_json TO unsupported_json")
    with pytest.raises(CodexMonitorUnavailable):
        monitor.tasks()


def test_unselected_history_does_not_add_full_scan_work(tmp_path: Path, monkeypatch):
    now = 2_000_000_000
    _, history = _fixture(tmp_path, now)
    with sqlite3.connect(history) as db:
        db.execute("CREATE INDEX turn_lookup ON thread_turns(thread_id, rollout_ordinal)")
        db.execute("CREATE INDEX item_lookup ON thread_items(thread_id, rollout_ordinal)")
        records = [(f"archived-{i}", str(n), n, "completed", now, now)
                   for i in range(2500) for n in range(8)]
        db.executemany("INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?)", records)
        db.executemany("INSERT INTO thread_items VALUES (?, ?, 'm', ?, ?, '{}', 'agentMessage')",
                       [(row[0], row[1], row[2], now * 1000) for row in records])
    connect = CodexMonitor._connect
    instructions = 0

    def count_work():
        nonlocal instructions
        instructions += 1000
        return 0

    def observed_connect(path):
        connection = connect(path)
        if path == history:
            connection.set_progress_handler(count_work, 1000)
        return connection

    monkeypatch.setattr(CodexMonitor, "_connect", staticmethod(observed_connect))
    tasks = CodexMonitor(tmp_path, tmp_path / "snapshot.json").tasks()
    assert {task.id for task in tasks} == {"running", "done"}
    # SQLite instruction count is deterministic work, not a machine-speed
    # threshold. A full aggregation of these 40k irrelevant rows exceeds it.
    assert instructions < 50_000


@pytest.mark.parametrize("broken_history", [False, True])
def test_reader_closes_connections_on_success_and_error(tmp_path: Path, monkeypatch, broken_history: bool):
    _, history = _fixture(tmp_path, 2_000_000_000)
    if broken_history:
        with sqlite3.connect(history) as db:
            db.execute("DROP TABLE thread_items")
    connect = CodexMonitor._connect
    connections = []

    def observed_connect(path):
        connection = connect(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(CodexMonitor, "_connect", staticmethod(observed_connect))
    monitor = CodexMonitor(tmp_path, tmp_path / "snapshot.json")
    if broken_history:
        with pytest.raises(CodexMonitorUnavailable):
            monitor.tasks()
    else:
        monitor.tasks()
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
