#!/usr/bin/env python3
"""Small, local Codex/Claude mailbox shared by all Git worktrees.

Messages are data, never commands or permission grants. Nothing here launches
agents, runs message bodies, polls models, or changes repository source files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    task TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    reply_to TEXT REFERENCES messages(id),
    dedupe_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(sender, dedupe_key)
);
CREATE TABLE IF NOT EXISTS acknowledgments (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    recipient TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox ON messages(recipient, seq);
"""


def mailbox_root(repo: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo, check=True, text=True, capture_output=True)
    return Path(result.stdout.strip()) / "wisp-coworkers"


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = root / "mailbox.sqlite3"
    # Avoid changing process-global umask: concurrent callers would restore
    # each other's masks out of order. Create the empty DB privately instead.
    try:
        descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    conn = sqlite3.connect(database, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def message(row: sqlite3.Row) -> dict:
    return {**dict(row), "body": json.loads(row["body"])}


def send(conn: sqlite3.Connection, *, sender: str, recipient: str,
         task: str, kind: str, body: dict, key: str | None = None,
         reply_to: str | None = None) -> dict:
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode()) > 65536:
        raise ValueError("message body exceeds 64 KiB; link a local artifact instead")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if reply_to:
            original = conn.execute(
                "SELECT * FROM messages WHERE id=?", (reply_to,)).fetchone()
            if not original or original["recipient"] != sender:
                raise ValueError("only the original recipient can reply")
            if original["sender"] != recipient or original["task"] != task:
                raise ValueError("reply must preserve the task and return to its sender")
        if key:
            existing = conn.execute(
                "SELECT * FROM messages WHERE sender=? AND dedupe_key=?",
                (sender, key)).fetchone()
            if existing:
                expected = (recipient, task, kind, encoded, reply_to)
                actual = tuple(existing[k] for k in
                               ("recipient", "task", "kind", "body", "reply_to"))
                if actual != expected:
                    raise ValueError("dedupe key already names a different message")
                return message(existing)
        identity = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages(id,sender,recipient,task,kind,body,reply_to,"
            "dedupe_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (identity, sender, recipient, task, kind, encoded, reply_to, key, timestamp()))
        if reply_to:
            _ack(conn, sender, reply_to)
        return message(conn.execute(
            "SELECT * FROM messages WHERE id=?", (identity,)).fetchone())


def _ack(conn: sqlite3.Connection, actor: str, identity: str) -> None:
    original = conn.execute(
        "SELECT recipient FROM messages WHERE id=?", (identity,)).fetchone()
    if not original or original["recipient"] != actor:
        raise ValueError("only the addressed recipient can acknowledge a message")
    conn.execute("INSERT OR IGNORE INTO acknowledgments VALUES(?,?,?)",
                 (identity, actor, timestamp()))


def inbox(conn: sqlite3.Connection, actor: str, after: int = 0,
          include_acknowledged: bool = False) -> list[dict]:
    return [message(row) for row in conn.execute(
        "SELECT m.* FROM messages m LEFT JOIN acknowledgments a ON a.message_id=m.id "
        "WHERE m.recipient=? AND m.seq>? AND (? OR a.message_id IS NULL) ORDER BY m.seq",
        (actor, after, include_acknowledged))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--actor", required=True, help="stable session/agent label")
    commands = parser.add_subparsers(dest="command", required=True)
    posting = commands.add_parser("send")
    posting.add_argument("--to", required=True)
    posting.add_argument("--task", required=True)
    posting.add_argument("--kind", default="message")
    posting.add_argument("--body-file", required=True, type=Path)
    posting.add_argument("--key", help="idempotency key; reuse only for the identical payload")
    replying = commands.add_parser("reply")
    replying.add_argument("message_id")
    replying.add_argument("--body-file", required=True, type=Path)
    replying.add_argument("--key")
    reading = commands.add_parser("inbox")
    reading.add_argument("--after", type=int, default=0)
    reading.add_argument("--all", action="store_true")
    acknowledging = commands.add_parser("ack")
    acknowledging.add_argument("message_id")
    args = parser.parse_args()
    conn = connect(mailbox_root(args.repo))
    try:
        if args.command in {"send", "reply"}:
            body = json.loads(args.body_file.read_text())
            if not isinstance(body, dict):
                raise ValueError("message body must be a JSON object")
            if args.command == "reply":
                original = conn.execute("SELECT * FROM messages WHERE id=?",
                                        (args.message_id,)).fetchone()
                if not original:
                    raise ValueError("original message does not exist")
                result = send(conn, sender=args.actor, recipient=original["sender"],
                              task=original["task"], kind="reply", body=body,
                              key=args.key, reply_to=args.message_id)
            else:
                result = send(conn, sender=args.actor, recipient=args.to, task=args.task,
                              kind=args.kind, body=body, key=args.key)
        elif args.command == "inbox":
            result = inbox(conn, args.actor, args.after, args.all)
        else:
            with conn:
                _ack(conn, args.actor, args.message_id)
            result = {"acknowledged": args.message_id, "actor": args.actor}
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"mailbox: {exc}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
