"""Reads the 2026-10-29 Ask eval found missing, each on an existing tool so the
registry stays at its count: sent mail (view_emails mailbox='sent'), folder
sizes and dates (list_dir details), ranked file search with the true total
(find_files), linked account addresses and what's new (wisp_status topics),
and Focus and volume (system_status sections). All are reads."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from service import gmail_source
from service.safety import policy
from service.tools import builtin, files_tools, misc_t1, system_extras
from service.tools import email_tools
from service.tools.registry import REGISTRY


def test_the_new_reads_extend_existing_read_tools():
    schema = lambda name: REGISTRY[name].parameters["properties"]  # noqa: E731
    assert schema("view_emails")["mailbox"]["enum"] == ["inbox", "sent"]
    assert {"details", "sort"} <= set(schema("list_dir"))
    assert {"accounts", "whats_new"} <= set(schema("wisp_status")["topic"]["enum"])
    assert {"focus", "sound"} <= set(schema("system_status")["section"]["enum"])
    # Each stays in a category every mode allows to read.
    for name, category in [("view_emails", "email_read"), ("list_dir", "fs_read"), ("find_files", "fs_read"),
                           ("wisp_status", "wisp_admin"), ("system_status", "system_read")]:
        assert REGISTRY[name].category == category


# -- sent mail ---------------------------------------------------------------

def _sent(monkeypatch, rows, accounts=("me@gmail.com",)):
    from service.mail_memory import bridge
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda cfg=None: {a: {} for a in accounts})
    monkeypatch.setattr(gmail_source, "sent_rows", lambda: [dict(r) for r in rows])

    async def no_app(days, timeout=8.0):
        raise bridge.BridgeError("app_not_connected")
    monkeypatch.setattr(bridge, "read_sent", no_app)


def test_sent_mail_lists_the_user_s_own_mail_newest_first(monkeypatch):
    now = time.time()
    _sent(monkeypatch, [
        {"ts": now - 7200, "to": "ana@example.com", "subject": "Lease", "account": "me@gmail.com"},
        {"ts": now - 60, "to": "bo@example.com", "subject": "Dinner Friday", "account": "me@gmail.com"},
    ])
    import asyncio
    out = asyncio.run(email_tools.sent_emails_view())
    assert out.index("bo@example.com") < out.index("ana@example.com")
    assert "2 found" in out and "headers only" in out
    only = asyncio.run(email_tools.sent_emails_view(query="ana"))
    assert "ana@example.com" in only and "bo@example.com" not in only


def test_sent_mail_without_gmail_or_the_app_says_why(monkeypatch):
    import asyncio
    _sent(monkeypatch, [], accounts=())
    out = asyncio.run(email_tools.sent_emails_view())
    assert "couldn't read your sent mail" in out and "isn't connected" in out and "Gmail" in out


def test_view_emails_routes_mailbox_sent(monkeypatch):
    import asyncio
    _sent(monkeypatch, [{"ts": time.time(), "to": "c@example.com", "subject": "Hi", "account": "me@gmail.com"}])
    assert "c@example.com" in asyncio.run(email_tools.view_emails(mailbox="sent"))


# -- folder sizes and dates --------------------------------------------------

def test_list_dir_details_gives_folder_totals_and_dates(tmp_path):
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "a.bin").write_bytes(b"x" * 5000)
    (tmp_path / "big" / "deep").mkdir()
    (tmp_path / "big" / "deep" / "b.bin").write_bytes(b"x" * 3000)
    (tmp_path / "small.txt").write_text("hi")
    old = time.time() - 86400 * 30
    os.utime(tmp_path / "small.txt", (old, old))
    out = builtin.list_dir(str(tmp_path), details=True, sort="size")
    first, second = out.splitlines()[:2]
    assert first.startswith("big/") and "8.0 KB" in first and "modified" in first
    assert second.startswith("small.txt") and "2 bytes" in second
    newest = builtin.list_dir(str(tmp_path), details=True, sort="modified")
    assert newest.splitlines()[0].startswith("big/")


def test_list_dir_details_marks_a_total_it_couldnt_finish(tmp_path, monkeypatch):
    (tmp_path / "f").mkdir()
    for i in range(5):
        (tmp_path / "f" / f"{i}").write_text("x")
    monkeypatch.setattr(builtin, "_DETAIL_MAX_FILES", 2)
    out = builtin.list_dir(str(tmp_path), details=True)
    assert "at least" in out


# -- file search ranking -----------------------------------------------------

def test_find_files_ranks_the_named_file_in_the_user_s_folders_first():
    home = str(Path.home())
    hits = [f"{home}/code/x/old_lease_notes.txt", "/Volumes/Backup/lease.pdf",
            f"{home}/Documents/lease.pdf", f"{home}/Downloads/leaseholder.doc"]
    ranked = files_tools.rank_hits(hits, "lease")
    assert ranked[0] == f"{home}/Documents/lease.pdf"
    assert ranked[1] == "/Volumes/Backup/lease.pdf"          # an exact name beats a partial one
    assert ranked[-1] == f"{home}/code/x/old_lease_notes.txt"


def test_find_files_says_the_real_total_past_the_cap(monkeypatch):
    names = [f"/tmp/report{i}.txt" for i in range(55)]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "\n".join(names), ""))
    out = files_tools.find_files("report")
    assert "+40 more" in out and "found 55 matches" in out and f"best {files_tools._DEFAULT_HITS}" in out


# -- wisp_status: accounts and what's new ------------------------------------

def test_accounts_names_each_google_address_and_its_uses(monkeypatch):
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda cfg=None: {
        "me@work.com": {"workspace": True}, "me@gmail.com": {"auth_expired": 1}})
    monkeypatch.setattr(gmail_source, "has_calendar", lambda email, item=None: email == "me@work.com")
    monkeypatch.setattr(email_tools, "native_email_accounts", lambda: [{"id": "1", "name": "iCloud"}])
    out = misc_t1._accounts_report()
    assert "me@work.com: work or school (Google Workspace) account; Gmail, Calendar, Drive, Docs and Sheets" in out
    assert "me@gmail.com: personal Google account; Gmail (sign-in expired" in out
    assert "Apple Mail that Wisp reads: iCloud" in out


_CHANGELOG = """# Changelog

## [Unreleased]

### Added
- **Sent mail.** Long detail here.
- **Folder sizes.** More.

### Fixed
- **A crash.** Detail.

## [0.5.0-dev7] - 2026-10-28

### Added
- **Older thing.**
"""


def test_whats_new_reads_the_newest_changelog_section_only():
    out = misc_t1.changelog_headlines(_CHANGELOG)
    assert "Added:\n- Sent mail.\n- Folder sizes.\nFixed:\n- A crash." in out
    assert "Older thing" not in out and "Long detail" not in out
    assert "not yet released" in out


def test_whats_new_finds_the_changelog_from_source():
    assert misc_t1._changelog_path() == Path(misc_t1.__file__).resolve().parents[2] / "CHANGELOG.md"


# -- system_status: focus and sound ------------------------------------------

def _focus_db(tmp_path: Path, records: list[dict]) -> Path:
    (tmp_path / "Assertions.json").write_text(json.dumps({"data": [{"storeAssertionRecords": records}]}))
    (tmp_path / "ModeConfigurations.json").write_text(json.dumps({"data": [{"modeConfigurations": {
        "com.apple.focus.work": {"mode": {"name": "Work"}}}}]}))
    return tmp_path


def test_focus_names_the_focus_that_is_on(tmp_path):
    db = _focus_db(tmp_path, [{"assertionDetails": {"assertionDetailsModeIdentifier": "com.apple.focus.work"}}])
    assert system_extras.focus_status(db) == "Focus is on: Work."


def test_focus_off_says_a_scheduled_one_cant_be_read(tmp_path):
    assert "scheduled or automatic Focus" in system_extras.focus_status(_focus_db(tmp_path, []))


def test_sound_reads_output_mute_and_input(monkeypatch):
    monkeypatch.setattr(system_extras, "_osa",
                        lambda script, timeout=0: subprocess.CompletedProcess([], 0, "35|true|60\n", ""))
    assert system_extras.sound_status() == "Output volume: 35%, muted. Input (microphone) volume: 60%."
    monkeypatch.setattr(system_extras, "_osa",
                        lambda script, timeout=0: subprocess.CompletedProcess([], 0, "missing value|missing value|missing value\n", ""))
    assert "isn't controlled by macOS" in system_extras.sound_status()


def test_view_only_mode_allows_the_new_reads(monkeypatch):
    monkeypatch.setattr(policy, "_READ_ONLY", True)
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    assert policy.decide("system_read", {"section": "focus"}, tool="system_status").tier is policy.Tier.ALLOW
    assert policy.decide("wisp_admin", {"topic": "accounts"}, tool="wisp_status").tier is policy.Tier.ALLOW
