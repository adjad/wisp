"""The history wire MailReader now posts keeps the backend's coverage semantics.

MailReader posts history from two new places: Mail's local index while Mail is
running, and an INCREMENTAL AppleScript walk that stitches a fresh newest slice
onto the rows it already held (MailHistoryMerge.swift). Both emit ONE summed
C2 attempted/skipped marker per account instead of one per 500-message batch,
a separate C2 cap marker, and the same C3 failed/interrupted marker. These
fixtures are written in exactly that shape and check that the old-mail
disclosures (history cap, history incomplete, skipped headers) still fire.

Synthetic strings only; no Mail, network or user data.

    .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_mail_history_wire.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if not os.environ.get("WISP_HOME"):
    _scratch = tempfile.TemporaryDirectory(prefix="wisp-history-wire-")
    os.environ["WISP_HOME"] = _scratch.name

from service.tools import email_tools as E  # noqa: E402
from service.tools import cache_store  # noqa: E402

FS = "\x01"


def h2(ts: float, account: str, account_id: str, subject: str, ident: str) -> str:
    return FS.join(["H2", str(int(ts)), "R", account, account_id, "Sender",
                    "sender@example.test", f"<{ident}@example.test>", subject, f"mail:{ident}"])


def c2(account: str, account_id: str, attempted: int, skipped: int, cap: str = "0") -> str:
    return FS.join(["C2", account, account_id, str(attempted), str(skipped), cap])


def c3(account: str, account_id: str, reason: str) -> str:
    return FS.join(["C3", account, account_id, reason])


OLD_DAY = (datetime.now() - timedelta(days=60)).replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.fixture
def mail(monkeypatch):
    now = datetime.now().timestamp()
    monkeypatch.setattr(E, "_headers", h2(now - 60, "Work", "UUID-W", "Recent", "recent") + "\n")
    monkeypatch.setattr(E, "_headers_sync_generation", 1)
    monkeypatch.setattr(E, "_email_available", True)
    monkeypatch.setattr(E, "_email_sync_pending", False)
    monkeypatch.setattr(E, "_email_read_source", "mail_app")
    return OLD_DAY.timestamp()


def summarize(day_ts: float, account: str | None = None) -> str:
    day = datetime.fromtimestamp(day_ts).date().isoformat()
    return asyncio.run(E.summarize_inbox_for_day(day, account=account))


def test_summed_marker_matches_per_batch_markers():
    rows = [h2(OLD_DAY.timestamp() - i, "Work", "UUID-W", f"S{i}", f"i{i}") for i in range(3)]
    per_batch = "\n".join(rows + [c2("Work", "UUID-W", 500, 1), c2("Work", "UUID-W", 120, 2)])
    summed = "\n".join(rows + [c2("Work", "UUID-W", 620, 3)])
    old = E.header_scan_coverage([], raw_headers=per_batch, fallback_cap=False)
    new = E.header_scan_coverage([], raw_headers=summed, fallback_cap=False)
    assert old == new
    assert new["attempted"] == 620 and new["skipped"] == 3 and not new["cap_accounts"]


def test_complete_merged_history_answers_an_old_day_without_caveats(mail, monkeypatch):
    monkeypatch.setattr(E, "_history", "\n".join([
        h2(mail, "Work", "UUID-W", "Quarterly plan", "old1"),
        c2("Work", "UUID-W", 1, 0),
    ]) + "\n")
    text = summarize(mail)
    assert "Quarterly plan" in text
    assert "History scan" not in text


def test_failed_walk_keeps_old_rows_and_discloses_incomplete(mail, monkeypatch):
    # What an aborted incremental walk posts: the previously held old row is
    # retained, and the account carries a C3 marker.
    monkeypatch.setattr(E, "_history", "\n".join([
        h2(mail, "Work", "UUID-W", "Quarterly plan", "old1"),
        c2("Work", "UUID-W", 1, 0),
        c3("Work", "UUID-W", "failed"),
    ]) + "\n")
    text = summarize(mail)
    assert "Quarterly plan" in text
    assert "History scan did not complete for Work" in text


def test_interrupted_account_without_rows_is_still_disclosed(mail, monkeypatch):
    monkeypatch.setattr(E, "_history", "\n".join([
        h2(mail, "Work", "UUID-W", "Quarterly plan", "old1"),
        c2("Work", "UUID-W", 1, 0),
        c3("Home", "", "interrupted"),
    ]) + "\n")
    coverage = E.header_scan_coverage([], raw_headers=E._history, fallback_cap=False)
    assert coverage["incomplete_accounts"] == ["Home"]
    assert "History scan did not complete for Home" in summarize(mail)


def test_history_cap_marker_is_disclosed(mail, monkeypatch):
    monkeypatch.setattr(E, "_history", "\n".join([
        h2(mail, "Work", "UUID-W", "Quarterly plan", "old1"),
        c2("Work", "UUID-W", 12000, 0),
        c2("Work", "UUID-W", 0, 0, cap="1"),
    ]) + "\n")
    coverage = E.header_scan_coverage([], raw_headers=E._history, fallback_cap=False)
    assert coverage["cap_accounts"] == ["Work"] and coverage["attempted"] == 12000
    assert "History scan reached its limit for Work" in summarize(mail)


def test_total_row_cap_from_the_index_is_disclosed(mail, monkeypatch):
    monkeypatch.setattr(E, "_history", "\n".join([
        h2(mail, "Work", "UUID-W", "Quarterly plan", "old1"),
        c2("Work", "UUID-W", 1, 0),
        c2("Mail", "*", 0, 0, cap="1"),
    ]) + "\n")
    assert "History scan reached its limit for Mail" in summarize(mail, account="Work")


@pytest.fixture
def persisted_mail(mail, monkeypatch, tmp_path):
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path / "cache")
    # Restore every touched in-memory field after this test as well.
    for field in ("_history", "_history_at", "_headers_at", "_raw_emails", "_raw_emails_at"):
        monkeypatch.setattr(E, field, getattr(E, field))
    return mail


def test_authoritative_empty_post_clears_memory_and_persisted_history(persisted_mail):
    from service.main import assistant_sync_emails

    old = h2(persisted_mail, "Work", "UUID-W", "Obsolete account row", "old") + "\n"
    E.cache_history_headers(old)
    before = (E._headers, E._raw_emails, E._headers_sync_generation, E.email_sync_state())
    assert cache_store.load("email_history") == old
    assert "Obsolete account row" in summarize(persisted_mail, account="Work")
    assert asyncio.run(assistant_sync_emails({"history": ""})) == {"ok": True}
    assert E._history == "" and cache_store.load("email_history") == ""
    assert "Obsolete account row" not in summarize(persisted_mail, account="Work")
    assert (E._headers, E._raw_emails, E._headers_sync_generation, E.email_sync_state()) == before


@pytest.mark.parametrize("update", [
    {"headers": ""},
    {"diagnostics": {"available": False, "reason": "synthetic unavailable"}},
    {},
])
def test_updates_without_history_preserve_persisted_rows(persisted_mail, update, monkeypatch):
    from service.main import assistant_sync_emails

    for field in ("_email_available", "_email_reason", "_email_sync_pending", "_email_read_source"):
        if hasattr(E, field):
            monkeypatch.setattr(E, field, getattr(E, field))
    old = h2(persisted_mail, "Work", "UUID-W", "Retained history", "old") + "\n"
    E.cache_history_headers(old)
    assert asyncio.run(assistant_sync_emails(update)) == {"ok": True}
    assert E._history == old and cache_store.load("email_history") == old


@pytest.mark.parametrize("reason", ["failed", "interrupted"])
def test_persisted_partial_history_retains_rows_and_disclosure(persisted_mail, reason):
    from service.main import assistant_sync_emails

    # Same-ID failed merge: old rows survive with C3, never an empty clear.
    wire = "\n".join([h2(persisted_mail, "Work", "UUID-W", "Retained history", "old"),
                      c2("Work", "UUID-W", 1, 0), c3("Work", "UUID-W", reason)]) + "\n"
    assert asyncio.run(assistant_sync_emails({"history": wire})) == {"ok": True}
    assert E._history == wire and cache_store.load("email_history") == wire
    text = summarize(persisted_mail, account="Work")
    assert "Retained history" in text and "History scan did not complete for Work" in text
