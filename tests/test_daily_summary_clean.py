"""The Daily Summary is a glanceable read, not a coverage report.

A 2026-10-17 problem report showed the chat brief ending in Mail-cache
freshness warnings, a "Mail scan incomplete" line, and an appended workspace
dump (Needs my attention / Suggested day plan / Source coverage). Those
caveats stay on the interactive email tools and the workspace API.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.assistant import brief as B                       # noqa: E402
from service.assistant import daily_workspace, daily_workspace_api  # noqa: E402
from service.tools import email_tools as E                     # noqa: E402

WARNING = ("Email is from Mail’s local cache. Newer messages may be missing; "
           "server freshness is unverified.")


def _stub_brief(monkeypatch):
    monkeypatch.setattr(B, "_render_brief", lambda now, messages: "BRIEF BODY")
    monkeypatch.setattr(B, "_messages_section", lambda now: "")
    monkeypatch.setattr(B, "_today_card", lambda now: "Today: nothing on your calendar.")
    monkeypatch.setattr(B, "_messages_card", lambda now: "")
    monkeypatch.setattr(daily_workspace_api, "current_workspace", lambda *a, **k: {})
    monkeypatch.setattr(daily_workspace, "render",
                        lambda *a, **k: "Needs my attention\n\nSource coverage")


def test_generated_brief_has_no_workspace_appendix(monkeypatch):
    _stub_brief(monkeypatch)
    full = asyncio.run(B._generate_brief("morning"))["FULL"]
    assert full == "BRIEF BODY"


def test_sections_add_no_mail_freshness_warning(monkeypatch):
    _stub_brief(monkeypatch)
    monkeypatch.setattr(E, "email_freshness_warning", lambda: WARNING)
    sections = asyncio.run(B._sections("morning", snapshot={"syncing": False}))
    assert sections["READY"] == "1"
    for key in ("FULL", "TODAY"):
        assert "local cache" not in sections[key]
        assert "may be missing" not in sections[key]
