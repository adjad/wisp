"""Daily Summary and Mail readiness never depend on the 2-year mail history.

The history sync is the slow Mail read (a multi-minute AppleScript walk when
Mail's local index can't be used). Daily Summary, `/assistant/summary`
readiness and `email_sync_state()` must be satisfied by the recent-header
cache alone, so they can never wait on, or be changed by, that history. These
tests replace the history cache with an object that fails on ANY use and
assert the brief and readiness come out identical.

Synthetic in-memory state only; no Mail, no network, no user data.

    .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_daily_summary_history_independence.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if not os.environ.get("WISP_HOME"):
    _scratch = tempfile.TemporaryDirectory(prefix="wisp-history-independence-")
    os.environ["WISP_HOME"] = _scratch.name

from service.assistant import brief as B                          # noqa: E402
from service.assistant import scheduler                           # noqa: E402
from service.assistant import sync_status as S                    # noqa: E402
from service.assistant.hub import hub                             # noqa: E402
from service.tools import email_extras as X                       # noqa: E402
from service.tools import email_tools as E                        # noqa: E402
from service.tools import imessage_tools as M                     # noqa: E402


class _HistoryTouched(AssertionError):
    pass


class PoisonedHistory:
    """Stands in for `email_tools._history`; any use of it fails the test."""

    def _fail(self, *_args, **_kwargs):
        raise _HistoryTouched("Daily Summary or readiness read mail history")

    __bool__ = __len__ = __iter__ = __contains__ = __str__ = __repr__ = _fail
    __eq__ = __hash__ = __add__ = __radd__ = __format__ = __getitem__ = _fail

    def __getattribute__(self, name):
        if name.startswith("__") or name == "_fail":
            return object.__getattribute__(self, name)
        raise _HistoryTouched(f"Daily Summary or readiness read mail history ({name})")


def _boom(*_args, **_kwargs):
    raise _HistoryTouched("Daily Summary or readiness parsed mail history")


def _h2(ts: float, account: str, account_id: str, name: str, address: str,
        subject: str, read: str = "U", ident: str = "") -> str:
    return "\x01".join(["H2", str(int(ts)), read, account, account_id, name, address,
                        f"<{ident}@example.test>", subject, f"mail:{ident}"])


@pytest.fixture
def sources(monkeypatch):
    """All four Daily sources ready, mail from the recent-header cache only."""
    now = datetime(2026, 9, 8, 11, 46).timestamp()
    monkeypatch.setattr(scheduler, "_sync_status", {
        source: {"available": True, "count": 1, "last_sync": now,
                 "diagnostics": {"snapshot_started_at": 4_000_000_000}}
        for source in ("calendar", "reminders")})
    monkeypatch.setattr(B.assistant_store, "upcoming", lambda now=0, days=7: [
        {"source": "reminders", "kind": "reminder", "when_ts": now + 3_600,
         "title": "pick up the synthetic parcel", "account": "iCloud"},
    ])
    monkeypatch.setattr(E, "_headers", "\n".join([
        _h2(now - 1_000, "Work", "UUID-W", "Casey", "casey@example.test",
            "Are you free Thursday?", ident="r1"),
        _h2(now - 2_000, "Home", "UUID-H", "Shop", "orders@shop.example.test",
            "Your order shipped", read="R", ident="r2"),
        _h2(now - 5_000, "Work", "UUID-W", "Dana", "dana@example.test",
            "Notes from today's review", ident="r3"),
    ]) + "\n")
    monkeypatch.setattr(E, "_headers_sync_generation", 1)
    monkeypatch.setattr(E, "_email_available", True)
    monkeypatch.setattr(E, "_email_sync_pending", False)
    monkeypatch.setattr(E, "_email_read_source", "mail_app")
    monkeypatch.setattr(M, "_parse_lines", lambda: [
        (now - 500, "Casey", "Me: see you at noon"),
    ])
    monkeypatch.setattr(M, "_lines",
                        f"V2 | {now - 500} | R | chat:1 | Casey | Me: see you at noon")
    monkeypatch.setattr(M, "_sync_completed", True)
    monkeypatch.setattr(M, "_available", True)
    # A realistic history cache that DOES differ from the recent headers, so
    # a brief that read it would visibly change.
    monkeypatch.setattr(E, "_history", "\n".join([
        _h2(now - 60, "Work", "UUID-W", "History Only", "history@example.test",
            "HISTORY-ONLY SUBJECT", ident="h1"),
        _h2(now - 400 * 86_400, "Work", "UUID-W", "Old", "old@example.test",
            "Two-year-old message", ident="h2"),
        "\x01".join(["C3", "Work", "UUID-W", "failed"]),
    ]) + "\n")
    return now


def _poison(monkeypatch) -> None:
    monkeypatch.setattr(E, "_history", PoisonedHistory())
    monkeypatch.setattr(E, "_parse_history", _boom)
    monkeypatch.setattr(X, "_parse_history", _boom)
    monkeypatch.setattr(E, "sender_stats", _boom)
    monkeypatch.setattr(E, "cache_history_headers", _boom)


def _render(now: float) -> dict[str, str]:
    return {"TODAY": B._today_card(now), "MESSAGES": B._messages_card(now),
            "FULL": B._render_brief(now, B._messages_section(now)),
            "EMAIL_SECTION": B._email_section(now), "EMAIL_BLOCK": B._email_block(now)}


def test_poison_really_fails_on_use():
    poisoned = PoisonedHistory()
    for use in (lambda: poisoned.split("\n"), lambda: bool(poisoned),
                lambda: "x" in poisoned, lambda: str(poisoned), lambda: f"{poisoned}"):
        with pytest.raises(_HistoryTouched):
            use()


def test_brief_sections_are_identical_without_history(sources, monkeypatch):
    baseline = _render(sources)
    assert "Are you free Thursday?" in baseline["FULL"]
    assert "HISTORY-ONLY SUBJECT" not in baseline["FULL"]
    _poison(monkeypatch)
    assert _render(sources) == baseline


def test_sections_entry_point_never_reads_history(sources, monkeypatch):
    monkeypatch.setattr(B.time, "time", lambda: sources)
    snapshot = S.summary_snapshot()
    assert not snapshot["syncing"], snapshot
    baseline = asyncio.run(B._sections("morning", snapshot=snapshot))
    _poison(monkeypatch)
    poisoned = asyncio.run(B._sections("morning", snapshot=S.summary_snapshot()))
    assert poisoned.get("READY") == "1"
    assert poisoned == baseline
    assert "HISTORY-ONLY SUBJECT" not in poisoned["FULL"]


def test_daily_source_readiness_never_reads_history(sources, monkeypatch):
    _poison(monkeypatch)
    assert E.email_sync_state() == "ready"
    status = S.source_status("email")
    assert status["state"] == "ready" and status["warning"] == ""
    snapshot = S.summary_snapshot()
    assert not snapshot["syncing"] and snapshot["completed"] == 4, snapshot
    with patch.object(hub, "publish", new_callable=AsyncMock):
        ensured = asyncio.run(S.ensure_daily_sources(timeout_seconds=0.5))
    assert not ensured["syncing"], ensured
    assert {row["id"]: row["state"] for row in ensured["sources"]}["email"] == "ready"


def test_history_can_neither_make_mail_ready_nor_hold_it_back(sources, monkeypatch):
    # No live header sync yet: a full history cache must not count as ready.
    monkeypatch.setattr(E, "_headers_sync_generation", 0)
    E.cache_history_headers(E._history)
    assert E._headers_sync_generation == 0
    assert E.email_sync_state() == "syncing"
    # A live header sync makes Mail ready with NO history at all — empty,
    # incomplete (C3) or poisoned.
    monkeypatch.setattr(E, "_headers_sync_generation", 1)
    for history in ("", "\x01".join(["C3", "Work", "UUID-W", "interrupted"]) + "\n"):
        monkeypatch.setattr(E, "_history", history)
        assert E.email_sync_state() == "ready"
        assert S.source_status("email")["state"] == "ready"
    _poison(monkeypatch)
    assert E.email_sync_state() == "ready"


def test_brief_module_does_not_reference_history():
    # Word-bounded so unrelated names such as browser_history don't match.
    for module, names in ((B, ("_history", "_parse_history", "sender_stats",
                               "cache_history_headers")),
                          (S, ("_history", "_parse_history", "cache_history_headers"))):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for name in names:
            assert not re.search(rf"\b{name}\b", source), \
                f"{Path(module.__file__).name} references {name}"
