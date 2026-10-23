"""SessionStore.list_chats: the sidebar list for the desktop chat window."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from service.memory.store import SessionStore  # noqa: E402


def _store(tmp_path) -> SessionStore:
    return SessionStore(Path(tmp_path) / "sessions.db")


def _touch(store: SessionStore, sid: str, last_used: float) -> None:
    with store._lock:
        store._db.execute("UPDATE sessions SET last_used=? WHERE id=?", (last_used, sid))
        store._db.commit()


def test_empty_store(tmp_path):
    assert _store(tmp_path).list_chats() == []


def test_assistant_only_session_excluded(tmp_path):
    store = _store(tmp_path)
    store.add_turn(store.create_session(), "assistant", "hello there")
    store.create_session()
    assert store.list_chats() == []


def test_title_truncation_and_whitespace(tmp_path):
    store = _store(tmp_path)
    exact = store.create_session()
    store.add_turn(exact, "user", "x" * 60)
    long_ = store.create_session()
    store.add_turn(long_, "user", "y" * 200)
    spaced = store.create_session()
    store.add_turn(spaced, "user", "  what\n\n is   the\tweather  ")
    by_id = {c["id"]: c for c in store.list_chats()}
    assert by_id[exact]["title"] == "x" * 60
    assert len(by_id[long_]["title"]) == 60 and by_id[long_]["title"].endswith("…")
    assert by_id[spaced]["title"] == "what is the weather"


def test_title_is_first_user_turn_and_preview_latest_assistant(tmp_path):
    store = _store(tmp_path)
    sid = store.create_session()
    store.add_turn(sid, "user", "first question")
    store.add_turn(sid, "assistant", "first answer")
    store.add_turn(sid, "user", "second question")
    store.add_turn(sid, "assistant", "line one\n\n  line   two")
    chat = store.list_chats()[0]
    assert chat["title"] == "first question"
    assert chat["preview"] == "line one line two"
    assert chat["turn_count"] == 4
    assert set(chat) == {"id", "title", "preview", "created_at", "last_used", "turn_count"}


def test_preview_truncated_and_empty_without_assistant(tmp_path):
    store = _store(tmp_path)
    sid = store.create_session()
    store.add_turn(sid, "user", "hi")
    assert store.list_chats()[0]["preview"] == ""
    store.add_turn(sid, "assistant", "z" * 500)
    assert store.list_chats()[0]["preview"] == "z" * 120


def test_ordering_and_limit(tmp_path):
    store = _store(tmp_path)
    ids = []
    for n in range(5):
        sid = store.create_session()
        store.add_turn(sid, "user", f"chat {n}")
        ids.append(sid)
    # Oldest-created gets the newest last_used, so ordering cannot be insertion order.
    for rank, sid in enumerate(reversed(ids)):
        _touch(store, sid, 1000.0 + rank)
    chats = store.list_chats()
    assert [c["last_used"] for c in chats] == sorted((c["last_used"] for c in chats), reverse=True)
    assert chats[0]["id"] == ids[0]
    limited = store.list_chats(limit=2)
    assert [c["id"] for c in limited] == [ids[0], ids[1]]


def test_credential_like_string_still_lists(tmp_path):
    store = _store(tmp_path)
    sid = store.create_session()
    store.add_turn(sid, "user", "my key is sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD please save it")
    store.add_turn(sid, "assistant", "password=hunter2hunter2hunter2 stored")
    chat = store.list_chats()[0]
    assert chat["id"] == sid and chat["turn_count"] == 2
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD" not in chat["title"]


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory(prefix="chat-list-") as tmp:
            try:
                fn(Path(tmp))
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())


def _pre_title_db(path: Path) -> None:
    """A sessions.db as Wisp wrote it before sessions had a title column."""
    import sqlite3
    db = sqlite3.connect(str(path))
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at REAL, last_used REAL,
            summary TEXT DEFAULT '', summarized_idx INTEGER DEFAULT 0, pinned_role TEXT,
            pinned_model TEXT, active_skill TEXT DEFAULT '');
        CREATE TABLE turns (session_id TEXT, idx INTEGER, role TEXT, content TEXT, tool_digest TEXT,
            display_content TEXT, display_kind TEXT, created_at REAL, PRIMARY KEY (session_id, idx));
        INSERT INTO sessions (id, created_at, last_used) VALUES ('old', 1, 2);
        INSERT INTO turns VALUES ('old', 0, 'user', 'what is on my calendar', NULL, NULL, NULL, 1);
        INSERT INTO turns VALUES ('old', 1, 'assistant', 'Two meetings.', NULL, NULL, NULL, 2);
    """)
    db.commit()
    db.close()


def test_title_migration_is_additive_and_keeps_old_titles(tmp_path):
    import shutil
    import sqlite3
    fixture = Path(tmp_path) / "fixture.db"
    _pre_title_db(fixture)
    copy = Path(tmp_path) / "sessions.db"
    shutil.copyfile(fixture, copy)  # the migration only ever sees a copy
    store = SessionStore(copy)
    columns = {row[1]: row for row in store._db.execute("PRAGMA table_info(sessions)")}
    assert "title" in columns and columns["title"][3] == 0 and columns["title"][4] is None  # nullable, no default
    assert store._db.execute("SELECT title FROM sessions WHERE id='old'").fetchone()[0] is None
    chat = store.list_chats()[0]
    assert chat["title"] == "what is on my calendar" and chat["turn_count"] == 2
    store.set_title("old", "  Corpus ·  LIVE-001 · what is on my calendar ")
    assert store.list_chats()[0]["title"] == "Corpus · LIVE-001 · what is on my calendar"
    store.set_title("old", "")
    assert store.list_chats()[0]["title"] == "what is on my calendar"
    store.set_title("old", "Corpus · LIVE-001")
    # Idempotent: reopening migrates nothing and keeps the title.
    again = SessionStore(copy)
    assert [r[1] for r in again._db.execute("PRAGMA table_info(sessions)")].count("title") == 1
    assert again.list_chats()[0]["title"] == "Corpus · LIVE-001"
    # An older Wisp reading the migrated file: its own list query still works.
    old = sqlite3.connect(str(copy))
    rows = old.execute(
        "SELECT s.id, (SELECT t.content FROM turns t WHERE t.session_id=s.id AND t.role='user' "
        "ORDER BY t.idx ASC LIMIT 1) FROM sessions s ORDER BY s.last_used DESC").fetchall()
    assert rows == [("old", "what is on my calendar")]
    # The fixture itself was never touched.
    assert "title" not in {r[1] for r in sqlite3.connect(str(fixture)).execute("PRAGMA table_info(sessions)")}


def test_a_store_without_the_column_lists_by_first_turn(tmp_path):
    store = _store(tmp_path)
    sid = store.create_session()
    store.add_turn(sid, "user", "hello there")
    store._has_title = False  # a read-only opener of an older file
    store.set_title(sid, "ignored")
    assert store.list_chats()[0]["title"] == "hello there"
