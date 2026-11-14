"""Mail closed: the Brief still reads. Freshness window, the on-disk index counted as
current, the Brief's one-sentence cause, and the Swift contract for when Mail is
opened in the background (the decision table itself is in
tests/MailDBReaderRegression.swift, run by the native mail-db gate)."""
from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "app" / "Sources" / "WispApp"

from service.tools import cache_store, email_tools as E

FDA_SENTENCE = ("Wisp can't read Apple Mail: open Mail once, or allow Full Disk Access for Wisp in "
                "System Settings > Privacy & Security.")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    scratch = tempfile.TemporaryDirectory(prefix="wisp-mail-closed-")
    monkeypatch.setattr(cache_store, "CACHE_DIR", Path(scratch.name))
    monkeypatch.setattr(cache_store, "save", lambda *a, **k: None)
    for name, value in dict(_headers="", _history="", _raw_emails="", _swift_headers="", _swift_history="",
                            _swift_raw="", _external_accounts={}, _headers_sync_generation=0,
                            _email_available=None, _email_read_source="", _email_reason="",
                            _email_reason_kind="", _email_sync_pending=False, _native_headers_at=0.0,
                            _native_raw_at=0.0, _native_history_at=0.0, _native_source="",
                            _mail_automation_refused=False).items():
        monkeypatch.setattr(E, name, value)
    yield
    scratch.cleanup()


def read_mail(source: str, *, age: float = 0.0, history: bool = False):
    now = time.time()
    row = f"{now - 3600} | Home | Sender | Subject"
    E.cache_emails(row)
    if history:
        E.cache_history_headers(row)
    E.set_email_availability(True, read_source=source)
    E._native_headers_at = now - age
    if history:
        E._native_history_at = now - age


def headers_row():
    return E.email_source_details(part="headers")[0]


# --- freshness window ---------------------------------------------------------

def test_window_has_margin_over_every_timer():
    timer = int(re.search(r"withTimeInterval: (\d+), repeats: true\) \{ _ in\s+self\?\.sync\(\)",
                          (SWIFT / "MailReader.swift").read_text()).group(1))
    assert timer == E._HEADERS_INTERVAL == 300
    assert E._EXTERNAL_MAX_AGE >= 2 * timer
    assert E._EXTERNAL_PART_MAX_AGE["headers"] == E._EXTERNAL_MAX_AGE   # Gmail and Mail agree
    assert E._SYNC_REQUEST_AGE == timer          # a question still asks for mail newer than one interval


@pytest.mark.parametrize("source", ["mail_app", "local_index"])
def test_a_late_sync_still_reads_current_and_a_missed_one_does_not(source):
    read_mail(source, age=E._HEADERS_INTERVAL + 90)     # a tick 90 s late
    assert headers_row()["fresh"] is True
    read_mail(source, age=E._EXTERNAL_MAX_AGE + 5)
    assert headers_row()["fresh"] is False


# --- the on-disk index counts as current ----------------------------------------

def test_index_read_is_current_for_headers_and_history_but_not_bodies():
    read_mail("local_index", age=30, history=True)
    assert headers_row()["fresh"] and headers_row()["source"] == "local_index"
    assert E.email_source_details(part="history")[0]["fresh"] is True
    assert not any(r["fresh"] for r in E.email_source_details(part="raw"))   # headers only: no bodies
    assert headers_row()["saved_copy"] is True
    read_mail("mail_app", age=30)
    assert headers_row()["saved_copy"] is False


def test_a_failed_index_read_is_not_current():
    read_mail("local_index", age=30)
    E.set_email_availability(False, FDA_SENTENCE, reason_kind="full_disk_access")
    assert headers_row()["fresh"] is False


def test_brief_header_gate_takes_an_index_read():
    from service.assistant.daily_workspace import fresh_header_rows
    read_mail("local_index", age=60)
    rows, eligible = fresh_header_rows(E, time.time())
    assert eligible and len(rows) == 1 and rows[0]["subject"] == "Subject"


@pytest.mark.parametrize("age, asks", [(100, False), (E._SYNC_REQUEST_AGE + 60, True)])
def test_a_question_asks_for_new_mail_only_after_one_interval(age, asks):
    read_mail("local_index", age=age)
    seen = []

    async def publish(event):
        seen.append(event)
    with patch("service.assistant.hub.hub.publish", publish):
        asyncio.run(E._ensure_email_cache(timeout_seconds=0))
    assert bool(seen) is asks


# --- the Brief's cause and fix ---------------------------------------------------

def test_brief_names_the_cause_in_one_sentence_with_its_fix_kind():
    from service.assistant import daily_workspace
    E.set_email_availability(False, FDA_SENTENCE, reason_kind="full_disk_access")
    result = {"email": {"ready": False}}
    daily_workspace._annotate_access(result, time.time())
    info = result["email"]
    assert info["reason"] == FDA_SENTENCE and info["blocked"] == "full_disk_access"
    assert info["state"] == "unavailable"
    assert FDA_SENTENCE.count(".") == 1 and "\n" not in FDA_SENTENCE


# --- Swift contract: where Mail may be opened ---------------------------------------

def test_swift_sentence_matches_the_service_text():
    assert FDA_SENTENCE in (SWIFT / "MailDBReader.swift").read_text().replace('"\n        + "', "")


def test_mail_opens_hidden_only_through_the_plan():
    reader = (SWIFT / "MailReader.swift").read_text()
    plan = (SWIFT / "MailDBReader.swift").read_text()
    # hidden: no window, not brought forward, not in Recents
    body = reader[reader.index("private func openMailHidden"):reader.index("private final class LaunchedApp")]
    assert "config.activates = false" in body and "config.hides = true" in body
    assert "addsToRecentItems = false" in body and "NSWorkspace.shared.openApplication" in body
    # one launcher, called only after a plan of .openHiddenThenAppleScript
    calls = [m.start() for m in re.finditer(r"(?<!func )openMailHidden\(\)", reader)]
    assert len(calls) == 2
    for at in calls:
        before = reader[max(0, at - 400):at]
        assert "openHiddenThenAppleScript" in before
    # never asks, and never opens Mail on a guess: an unknown answer is not a yes
    assert "ask" not in body.split("automationState")[0].lower().replace("asked", "")
    assert "static let openHiddenWithFullDiskAccess = false" in plan
    assert "if !mailOpen && automation == .granted { return .openHiddenThenAppleScript }" in plan
    assert plan.count("return .openHiddenThenAppleScript") == 2     # granted, no FDA; granted + switch
