"""Local temporary mailboxes only; no models, native apps or real messages."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from scripts import wisp_cowork as mailbox


def post(conn, **extra):
    return mailbox.send(conn, sender="codex", recipient="claude", task="fixture",
                        kind="request", body={"challenge": "synthetic"}, **extra)


def test_round_trip_correlates_reply_and_acknowledges_request(tmp_path):
    conn = mailbox.connect(tmp_path)
    request = post(conn, key="first")
    assert mailbox.inbox(conn, "codex") == []
    assert mailbox.inbox(conn, "claude") == [request]
    reply = mailbox.send(conn, sender="claude", recipient="codex", task="fixture",
                         kind="reply", body={"challenge": "synthetic", "status": "received"},
                         reply_to=request["id"])
    assert mailbox.inbox(conn, "claude") == []
    assert mailbox.inbox(conn, "codex") == [reply]
    assert mailbox.inbox(conn, "claude", include_acknowledged=True) == [request]
    assert mailbox.inbox(conn, "codex", after=reply["seq"]) == []
    conn.close()


def test_duplicate_delivery_is_idempotent_and_changed_payload_rejected(tmp_path):
    conn = mailbox.connect(tmp_path)
    request = post(conn, key="same")
    assert post(conn, key="same") == request
    with pytest.raises(ValueError, match="different message"):
        mailbox.send(conn, sender="codex", recipient="claude", task="fixture",
                     kind="request", body={"different": True}, key="same")
    assert mailbox.inbox(conn, "claude") == [request]
    conn.close()


def test_wrong_recipient_or_task_cannot_reply_or_ack(tmp_path):
    conn = mailbox.connect(tmp_path)
    request = post(conn)
    for sender, recipient, task in (("other", "codex", "fixture"),
                                    ("claude", "other", "fixture"),
                                    ("claude", "codex", "other")):
        with pytest.raises(ValueError):
            mailbox.send(conn, sender=sender, recipient=recipient, task=task,
                         kind="reply", body={}, reply_to=request["id"])
    with pytest.raises(ValueError):
        with conn:
            mailbox._ack(conn, "other", request["id"])
    assert len(mailbox.inbox(conn, "claude")) == 1
    conn.close()


def test_concurrent_duplicate_senders_create_one_message(tmp_path):
    mailbox.connect(tmp_path).close()

    def run(_):
        conn = mailbox.connect(tmp_path)
        try:
            return post(conn, key="concurrent")["id"]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert len(set(workers.map(run, range(12)))) == 1
    conn = mailbox.connect(tmp_path)
    assert len(mailbox.inbox(conn, "claude")) == 1
    conn.close()


def test_message_bodies_are_data_not_commands_and_size_is_bounded(tmp_path):
    conn = mailbox.connect(tmp_path)
    target = tmp_path / "must-not-exist"
    result = mailbox.send(conn, sender="codex", recipient="claude", task="fixture",
                          kind="message", body={"text": f"touch {target}"})
    assert result["body"]["text"] == f"touch {target}"
    assert not target.exists()
    with pytest.raises(ValueError, match="64 KiB"):
        mailbox.send(conn, sender="codex", recipient="claude", task="fixture",
                     kind="message", body={"text": "x" * 65536})
    conn.close()


def test_cli_worktrees_share_a_mailbox(tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Synthetic",
        "-c", "user.email=synthetic@example.invalid", "-c", "commit.gpgSign=false",
        "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "-q",
        str(worktree)], check=True)
    assert mailbox.mailbox_root(repo) == mailbox.mailbox_root(worktree)
    payload = tmp_path / "request.json"
    payload.write_text(json.dumps({"challenge": "cli-round-trip"}))

    def cli(directory, actor, *args):
        return json.loads(subprocess.run([sys.executable, str(mailbox.__file__),
            "--repo", str(directory), "--actor", actor, *args],
            check=True, capture_output=True, text=True).stdout)

    request = cli(repo, "codex", "send", "--to", "claude", "--task", "fixture",
        "--body-file", str(payload), "--key", "cli-request")
    assert cli(worktree, "claude", "inbox") == [request]
    reply = cli(worktree, "claude", "reply", request["id"], "--body-file", str(payload))
    assert reply["reply_to"] == request["id"]
    assert cli(repo, "codex", "inbox") == [reply]
    assert cli(worktree, "claude", "inbox") == []


def test_private_database_creation_does_not_change_process_umask(tmp_path):
    with patch.object(mailbox.os, "umask", side_effect=AssertionError("process-global mutation")):
        mailbox.connect(tmp_path).close()
    assert (tmp_path / "mailbox.sqlite3").stat().st_mode & 0o777 == 0o600
