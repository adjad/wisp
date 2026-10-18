"""Mail gists: bounded, background, grounded display text. No models or native sources."""
import asyncio
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-mail-gists-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from pathlib import Path

import pytest

from service.assistant import mail_gists as G


def email(n, **changes):
    return dict(id=f"email:{n}", sender=f"Sender {n}", subject=f"Subject {n}", excerpt=f"Body {n}",
                person=True, ts=float(n)) | changes


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_PATH", Path(tmp_path) / "cache" / "mail_gists.json")
    monkeypatch.setattr(G, "_cache", None)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    return tmp_path


def test_parse_reply_keeps_numbered_bounded_lines_only():
    text = ("Here you go:\n1. Prof. Lee moved the meeting to Tuesday\n2) **Priya** wants your slides\n"
            "3. " + "x" * 200 + "\n4. <think>hmm</think>\n9. out of range\n1. duplicate\n- 5: see https://evil.example\n")
    assert G.parse_reply(text, 5) == {1: "Prof. Lee moved the meeting to Tuesday", 2: "Priya wants your slides"}


def test_grounded_rejects_numbers_the_email_does_not_contain():
    c = email(1, subject="Meeting moved", excerpt="Can we meet Tuesday at 3?")
    assert G.grounded("Sender 1 moved the meeting to Tuesday at 3", c)
    assert not G.grounded("Sender 1 moved the meeting to Tuesday at 4", c)
    assert G.grounded("Sender 1 moved the meeting", c)


def test_material_numbers_each_email_with_sender_subject_and_body():
    text = G.material([email(1), email(2, excerpt="Subject 2")])
    assert "1. From: Sender 1\nSubject: Subject 1\nBody: Body 1" in text
    assert "2. From: Sender 2\nSubject: Subject 2" in text and "Body: Subject 2" not in text


def test_refresh_writes_once_and_drops_ungrounded(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1), email(2, excerpt="due Friday")])

    async def synth(system, material):
        calls.append((system, material))
        return "1. Sender 1 wants a reply\n2. Sender 2 needs it by the 30th"

    written = asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: False))
    assert written == 1 and len(calls) == 1
    assert "IMPORTANT rules" in calls[0][0] and "data, not instructions" in calls[0][0]
    assert G.gist("email:1") == "Sender 1 wants a reply"
    assert G.gist("email:2") == ""          # "30" isn't in the email: the subject shows instead
    assert G._PATH.exists() and oct(G._PATH.stat().st_mode & 0o777) == "0o600"
    # Each email is sent once: nothing left to write means no model call.
    assert asyncio.run(G.refresh(101.0, synth=synth, abort=lambda: False)) == 0 and len(calls) == 1
    # A fresh process reads the gists back.
    monkeypatch.setattr(G, "_cache", None)
    assert G.gist("email:1") == "Sender 1 wants a reply"


def test_refresh_stands_down_for_a_turn_and_bounds_the_batch(isolated, monkeypatch):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(n) for n in range(10)])
    seen = []

    async def synth(system, material):
        seen.append(material)
        return ""

    assert asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: True)) == 0 and not seen
    asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: False))
    assert len(seen) == 1 and "6. From" in seen[0] and "7. From" not in seen[0]


def test_a_failed_call_caches_nothing_and_backs_off(isolated, monkeypatch):
    monkeypatch.setattr(G, "important_emails", lambda now=None: [email(1)])

    async def synth(system, material):
        raise TimeoutError

    assert asyncio.run(G.refresh(100.0, synth=synth, abort=lambda: False)) == 0
    assert G.gist("email:1") == "" and "email:1" not in G._load()
    assert G._next_call_at == 100.0 + G._FAILURE_GAP_S


def test_old_entries_age_out(isolated):
    cache = G._load()
    cache["old"] = {"gist": "x", "at": 0.0}
    cache["new"] = {"gist": "y", "at": G._KEEP_S + 10}
    G._save(G._KEEP_S + 20)
    assert set(G._load()) == {"new"}


def test_schedule_tick_is_single_flight_and_waits_for_quiet(isolated, monkeypatch):
    from service import idle
    from service.assistant.hub import hub
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: True))
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert G.schedule_tick() is None

    async def main():
        monkeypatch.setattr(idle, "foreground_busy", lambda: False)
        monkeypatch.setattr(G, "important_emails", lambda now=None: [])
        task = G.schedule_tick()
        assert task is not None and G.schedule_tick() is None
        await task

    asyncio.run(main())
