"""Why Messages can't be read reaches the user, and only a real Full Disk Access
denial sends them to that setting. No native reader, model or network."""
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-messages-read-failure-")
os.environ.setdefault("WISP_HOME", _scratch.name)

import asyncio

import pytest

from service.assistant import sync_status
from service.tools import imessage_tools as M


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for name, value in (("_lines", ""), ("_available", False), ("_unavailable_reason", ""),
                        ("_unavailable_kind", ""), ("_sync_completed", False)):
        monkeypatch.setattr(M, name, value)
    monkeypatch.setattr(M.cache_store, "save", lambda *a, **k: None)


def messages_row():
    return sync_status.sources_snapshot(["messages"])["sources"][0]


def test_a_full_disk_access_denial_says_how_to_fix_it():
    """Installed build 2203: Messages read "Could not read this source", with no
    reason and no way to fix it."""
    M.cache_messages("", available=False, reason="anything the app sent", kind="full_disk_access")
    row = messages_row()
    assert row["state"] == "unavailable" and row["reason_kind"] == "full_disk_access"
    assert row["reason"] == row["progress_detail"] == M.FULL_DISK_ACCESS_REASON
    assert "Full Disk Access" in M.FULL_DISK_ACCESS_REASON and "turn on Wisp" in M.FULL_DISK_ACCESS_REASON
    assert M._unavailable_message() == f"({M.FULL_DISK_ACCESS_REASON})"


@pytest.mark.parametrize("kind,reason", [
    ("missing", "No Messages history was found on this Mac (chat.db is missing)."),
    ("unreadable", "Couldn't read Messages: no such table: message."),
])
def test_other_failures_keep_their_own_reason(kind, reason):
    M.cache_messages("", available=False, reason=reason, kind=kind)
    row = messages_row()
    assert row["reason_kind"] == kind and row["reason"] == row["progress_detail"] == reason
    assert "Full Disk Access" not in M._unavailable_message()


def test_an_unknown_kind_or_a_guess_never_claims_full_disk_access():
    # An unrecognized kind is dropped; a reason that merely mentions Full Disk
    # Access (the old reader's guess) isn't a classified denial.
    M.cache_messages("", available=False, reason="chat.db not readable (Full Disk Access?)", kind="tcc")
    assert M.messages_unavailable_kind() == "" and messages_row()["reason_kind"] == ""
    assert messages_row()["reason"] == "chat.db not readable (Full Disk Access?)"
    M.cache_messages("", available=False)
    assert M._unavailable_message() == "(Can't read Messages right now. Try again in a moment.)"
    assert messages_row()["progress_detail"] == "Could not read this source"


def test_a_successful_read_clears_the_reason_and_kind():
    M.cache_messages("", available=False, reason="x", kind="full_disk_access")
    M.cache_messages("V2 | 1 | U | chat:1 | Ann | Ann: hi", available=True, reason="stale", kind="full_disk_access")
    row = messages_row()
    assert row["state"] == "ready" and row["reason"] == "" and row["reason_kind"] == ""
    assert M.messages_unavailable_kind() == "" and M.messages_unavailable_reason() == ""


def test_still_syncing_has_no_failure_reason():
    row = messages_row()
    assert row["state"] == "syncing" and row["reason"] == "" and row["reason_kind"] == ""


def test_the_sync_endpoint_carries_the_readers_kind():
    from fastapi.testclient import TestClient
    from service import main
    client = TestClient(main.app)
    reply = client.post("/assistant/sync/messages", json={"lines": "", "diagnostics": {
        "available": False, "reason": "ignored for this kind", "reason_kind": "full_disk_access"}})
    assert reply.status_code == 200
    status = client.get("/assistant/sync/status", params={"sources": "messages"}).json()["sources"][0]
    assert status["reason_kind"] == "full_disk_access" and status["progress_detail"] == M.FULL_DISK_ACCESS_REASON
    client.post("/assistant/sync/messages", json={"lines": "", "diagnostics": {
        "available": False, "reason": "Couldn't read Messages: disk I/O error.", "reason_kind": "unreadable"}})
    status = client.get("/assistant/sync/status", params={"sources": "messages"}).json()["sources"][0]
    assert status["reason_kind"] == "unreadable" and status["progress_detail"] == "Couldn't read Messages: disk I/O error."


def test_the_messages_tools_say_the_same_thing():
    M.cache_messages("", available=False, reason="x", kind="full_disk_access")
    result = asyncio.run(M.view_messages(unread=True))
    assert M.FULL_DISK_ACCESS_REASON in result
