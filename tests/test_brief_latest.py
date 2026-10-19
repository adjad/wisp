"""The kept Daily Summary: private, two days, served after a relaunch. No models or native sources."""
import asyncio
import os
import tempfile
from types import SimpleNamespace

_scratch = tempfile.TemporaryDirectory(prefix="wisp-brief-latest-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service.assistant import brief_latest as L


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "_PATH", Path(tmp_path) / "cache" / "daily_brief_latest.json")
    return tmp_path


def test_record_keeps_the_latest_per_day_privately_and_only_two_days(isolated):
    L.record("morning text", "morning", trace_id="t1", day="2026-10-16", now=1.0)
    L.record("morning text", "morning", day="2026-10-17", now=2.0)
    L.record("first", "morning", day="2026-10-18", now=3.0)
    L.record("second", "evening", day="2026-10-18", now=4.0)
    assert L.latest("2026-10-18") == {"text": "second", "composed_at": 4.0, "part_of_day": "evening"}
    assert L.latest("2026-10-17")["text"] == "morning text"
    assert L.latest("2026-10-16") is None, "only today's and yesterday's are kept"
    assert oct(L._PATH.stat().st_mode & 0o777) == "0o600"
    assert oct(L._PATH.parent.stat().st_mode & 0o777) == "0o700"


def test_blank_text_bad_days_and_a_corrupt_file_are_ignored(isolated):
    L.record("   ", "morning", day="2026-10-18")
    L.record("x", "morning", day="../../etc")
    assert not L._PATH.exists() and L.latest("2026-10-18") is None and L.latest("nonsense") is None
    L._PATH.parent.mkdir(parents=True)
    L._PATH.write_text("{not json")
    assert L.latest("2026-10-18") is None
    L.record("ok", "noon", day="2026-10-18", now=5.0)
    assert L.latest("2026-10-18") == {"text": "ok", "composed_at": 5.0, "part_of_day": ""}


def test_the_endpoint_serves_the_days_brief_or_404(isolated):
    from service import main
    client = TestClient(main.app)
    assert client.get("/assistant/daily_summary/latest", params={"day": "2026-10-18"}).status_code == 404
    L.record("**📧 Inbox — 3 emails**", "morning", day="2026-10-18", now=7.0)
    reply = client.get("/assistant/daily_summary/latest", params={"day": "2026-10-18", "timezone": "America/Los_Angeles"})
    assert reply.status_code == 200
    assert reply.json() == {"text": "**📧 Inbox — 3 emails**", "composed_at": 7.0, "part_of_day": "morning"}
    L.record("today's", "evening", now=8.0)
    assert client.get("/assistant/daily_summary/latest").json()["text"] == "today's", "day defaults to today"


def test_the_scheduled_brief_is_kept_only_when_real(isolated, monkeypatch):
    from service.assistant import brief
    from service.assistant.hub import hub
    published = []

    async def publish(event, **kwargs):
        published.append(event)

    monkeypatch.setattr(hub, "publish", publish)
    monkeypatch.setattr(brief.assistant_store, "event_by_key", lambda key: None)
    sections = {"READY": False, "FULL": "Still syncing"}

    async def fake_sections(part, **kwargs):
        return dict(sections)

    monkeypatch.setattr(brief, "_sections", fake_sections)
    assert asyncio.run(brief.run_scheduled_brief("morning", day="2026-10-18")) is False
    assert L.latest("2026-10-18") is None, "a hold-back is never kept"
    sections.update(READY=True, FULL="**🧭 Keeping in mind**\n- PA 2")
    assert asyncio.run(brief.run_scheduled_brief("morning", day="2026-10-18")) is True
    assert L.latest("2026-10-18")["text"] == "**🧭 Keeping in mind**\n- PA 2" and len(published) == 1


def test_bulk_mail_calls_to_action_do_not_make_it_need_you():
    """Build 2042: event promos with "RSVP"/"register" lines counted among the
    emails that need the user."""
    from service.assistant.daily_workspace import important_email_rows

    def row(n, **fields):
        return dict(subject=f"Subject {n}", sender=f"Ann Lee {n}", sender_address=f"ann{n}@example.com",
                    ts=float(n), body="", unread=False) | fields

    rows = [
        row(1, subject="See you there", sender="Katia Ameri", sender_address="katia@example.org",
            body="Could you submit your RSVP by Friday?\n\nUnsubscribe from these emails"),
        row(2, subject="Ship SF Wrapped", sender="Vercel", sender_address="news@vercel.com",
            body="Could you register for the event by Friday?"),
        row(3, subject="Draft", sender="Alice", sender_address="alice@example.test", body="Could you submit the project by Friday?"),
    ]
    picked = [r["subject"] for r, _ in important_email_rows(SimpleNamespace(rows=rows))]
    assert picked == ["Draft"]
    # Without the unsubscribe line, a person's request still counts.
    rows[0]["body"] = "Could you submit your RSVP by Friday?"
    assert [r["subject"] for r, _ in important_email_rows(SimpleNamespace(rows=rows))] == ["Draft", "See you there"]
