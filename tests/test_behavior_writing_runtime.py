"""Writing profile through the runtime and the /behavior API: the switch, what is measured, what is
never kept, the style card gates, usage counters and reminder facts.

A REAL BehaviorEngine runs under a tmp_path home (no FakeEngine: the privacy claims are about what
the real databases hold). Synthetic data only (Sam Example, example.test); sentinel words stand in
for message text so a leak shows up as a byte match. No listener is opened: FastAPI TestClient only.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import behavior_api
from service.assistant import behavior_runtime as br
from service.behavior.engine import BehaviorEngine
from service.behavior.types import PROFILE_ONLY_KINDS
from tests.test_behavior_runtime import FakeHub

# Monday 12:00 UTC. The engine runs in UTC so hour-of-day patterns do not depend on this Mac's zone.
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400.0
NEW_KINDS = ("writing_style", "send_timing", "reminder_habit", "usage_stat")

# One sentinel stem per channel (none is in the greeting/sign-off lexicon, so even a leaked
# "phrase" could not be one of these).
S_MSG, S_IN, S_NOTE, S_REM, S_MAIL = "zorblax", "snorkelfin", "plinthwick", "gribbleton", "vandermoot"
SENTINELS = (S_MSG, S_IN, S_NOTE, S_REM, S_MAIL)


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from service import config
    for key in ("WISP_BEHAVIOR", "WISP_QA_SEED", "WISP_BEHAVIOR_PHRASER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "wisp_home"))
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "cfg" / "config.yaml")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    monkeypatch.setattr(behavior_api, "_settings_updates", behavior_api._SettingsUpdates())
    monkeypatch.setattr(br.BehaviorRuntime, "auto_actions_held", lambda self, store, now: False)
    monkeypatch.setattr(br, "_last_age_out", None)
    br.set_runtime(None)
    yield
    br.set_runtime(None)
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


@pytest.fixture
def store(tmp_path):
    from service.assistant.store import AssistantStore
    return AssistantStore(tmp_path / "assistant.db")


@pytest.fixture
def writes_allowed():
    from service.safety import policy
    before = (policy.read_only(), policy.full_access())
    policy.set_read_only(False)
    policy.set_full_access(False)
    yield policy
    policy.set_read_only(before[0])
    policy.set_full_access(before[1])


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(behavior_api.router)
    return TestClient(app)


@pytest.fixture
def clock():
    return Clock()


def make_real(tmp_path, clock, records=None):
    """A runtime over the real engine. ``rt.box['snap']`` is what the Messages reader returns."""
    box = {"snap": {"state": "ready", "records": list(records or [])}}
    rt = br.BehaviorRuntime(
        tmp_path / "home",
        local_factory=lambda path: BehaviorEngine(path, tz="UTC", clock=clock),
        messages_fn=lambda: box["snap"],
        contact_name_fn=lambda handle: "Sam Example",
        calendar_sync_fn=lambda: {"available": False},
        session_db_fn=lambda: (_ for _ in ()).throw(RuntimeError("no sessions in tests")),
        clock=clock)
    rt.box = box
    return rt


@pytest.fixture
def rt(tmp_path, clock):
    runtime = make_real(tmp_path, clock)
    br.set_runtime(runtime)
    yield runtime
    br.set_runtime(None)
    try:
        runtime.close()
    except Exception:  # noqa: BLE001
        pass


def writing_on(rt):
    rt.call("configure_writing", True)
    rt.writing_changed()


def msg_text(i: int, stem: str = S_MSG) -> str:
    return f"hey {stem}{i} is running late today! I'll call you after lunch, okay?"


def mail_text(i: int) -> str:
    return f"Hi Sam,\n\nCould you send the {S_MAIL}{i} figures over? I can't find them.\n\nThanks,\nPat"


def message_records(n_out=25, n_in=25, now=NOW):
    out = [{"identity": f"out{i}", "timestamp": now - 600 - i * 900, "direction": "outgoing",
            "conversation": "any;-;+15555550101", "text": msg_text(i)} for i in range(n_out)]
    inc = [{"identity": f"in{i}", "timestamp": now - 300 - i * 900, "direction": "incoming",
            "sender": "+15555550101", "text": msg_text(i, S_IN)} for i in range(n_in)]
    return out + inc


def notes(n=21, now=NOW):
    return [{"ts": now - 3600 * (i + 1), "title": f"{S_NOTE} list {i} apples", "folder": "Notes",
             "body": f"Remember the {S_NOTE}{i} order. It is not urgent."} for i in range(n)]


def add_reminders(store, n=22, now=NOW):
    for i in range(n):
        store.add_manual(f"Renew the {S_REM}{i} permit", now - (i + 1) * 3600)


def eml(body: str) -> bytes:
    return ("From: Sam Example <sam@example.test>\r\nTo: Pat Example <pat@example.test>\r\n"
            "Subject: Figures\r\nDate: Mon, 12 Oct 2026 08:00:00 +0000\r\n"
            "Message-ID: <fixed@example.test>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body).encode()


def sent_rows(n=30, inbox=3, now=NOW):
    rows = [{"rowid": 100 + i, "box": "sent", "ts": now - (i + 1) * 3600.0,
             "rfc_message_id": f"<s{i}@example.test>"} for i in range(n)]
    rows += [{"rowid": 900 + i, "box": "inbox", "ts": now - (i + 1) * 1800.0,
              "rfc_message_id": f"<in{i}@example.test>"} for i in range(inbox)]
    return rows


class FakeMail:
    """Stands in for service.mail_memory.bridge: records every ask, never touches the app."""

    def __init__(self, monkeypatch, rows=None, bodies=None):
        from service.mail_memory import bridge
        self.rows = rows if rows is not None else sent_rows()
        self.bodies = bodies or {}
        self.index_calls = 0
        self.body_calls: list[int] = []
        self.index_error: Exception | None = None
        self.body_errors: set[int] = set()
        self.missing: set[int] = set()
        self.BridgeError = bridge.BridgeError

        async def read_index(days, timeout=60.0):
            self.index_calls += 1
            if self.index_error is not None:
                raise self.index_error
            return list(self.rows), False

        async def read_body(rowid, timeout=15.0):
            self.body_calls.append(rowid)
            if rowid in self.body_errors:
                raise bridge.BridgeError("body_timeout")
            if rowid in self.missing:
                return None, "not_on_disk"
            return eml(self.bodies.get(rowid) or mail_text(rowid)), ""

        monkeypatch.setattr(bridge, "read_index", read_index)
        monkeypatch.setattr(bridge, "read_body", read_body)


def home_bytes(home: Path) -> bytes:
    """Every byte under the behavior home (databases, WAL, SHM, card cache), lower-cased."""
    return b"\n".join(p.read_bytes().lower() for p in sorted(Path(home).rglob("*")) if p.is_file())


def engine_of(rt):
    return rt.local_engine()


def seed_each_kind(rt, now=NOW):
    """One pattern of every new kind, mined from stored counts and metadata."""
    writing_on(rt)
    rt.call("observe_writing", "message", [(f"m{i}", now - 3600 * (i + 1), msg_text(i)) for i in range(25)])
    day0 = now - (now % DAY)
    rt.call("observe_email_sent", [(f"e{i}", day0 - (i + 1) * DAY + 10 * 3600) for i in range(25)])
    rt.call("observe_reminder_facts",
            [{"id": f"r{i}", "created_ts": now - (i + 2) * DAY, "due_ts": now - (i + 2) * DAY + 2 * 86400 // 2,
              "completed_ts": None, "repeating": False} for i in range(12)], now)
    for i in range(12):
        rt.call("record_usage", "tab_opened", ("day", "brief")[i % 2], 0, now - DAY * (1 + i % 3))
    rt.call("mine", now)
    return {p.kind: p for p in rt.call("patterns")}


# --------------------------------------------------------------------------------------------
# (1) The switch is OFF by default and nothing is measured or asked while it is off
# --------------------------------------------------------------------------------------------

def test_switch_defaults_off_in_engine_and_api(rt, client):
    assert rt.call("writing_enabled") is False
    body = client.get("/behavior/signals").json()
    assert body["settings"]["writing_profile"] is False
    assert rt.call("writing_stats") == {} and rt.style_card() == ""


def test_off_measures_nothing_and_never_reads_text(rt, store, monkeypatch, writes_allowed):
    """Messages, notes, reminders and sent mail: nothing is read, measured or asked while off."""
    rt.box["snap"] = {"state": "ready", "records": message_records()}
    mail = FakeMail(monkeypatch)
    add_reminders(store)
    asked = []

    def parsed_notes():
        asked.append("notes")
        return notes()
    monkeypatch.setattr("service.tools.notes_tools.parsed_notes", parsed_notes)
    reads = []
    original = rt._messages_fn
    rt._messages_fn = lambda: reads.append(1) or original()

    assert rt._ingest_writing(store, NOW) is False
    asyncio.run(rt._writing_email_step(NOW, force=True))

    assert reads == [] and asked == [], "the text sources were read although the switch is off"
    assert mail.index_calls == 0 and mail.body_calls == [], "the Mail bridge was asked while off"
    eng = engine_of(rt)
    assert all(eng._store.writing_rows(c) == [] for c in ("email", "message", "note", "reminder"))
    assert eng._store.observations(kinds=["email_out"]) == []
    # The engine refuses direct measurement too, and reports nothing unseen to read.
    assert rt.call("observe_writing", "message", [("x", NOW - 60, msg_text(1))]) == 0
    assert rt.call("writing_unseen", "message", ["x"]) == []
    assert rt.call("observe_email_sent", [("x", NOW - 60)]) == 0


def test_a_full_tick_with_the_switch_off_never_measures(rt, store, monkeypatch, writes_allowed):
    rt.box["snap"] = {"state": "ready", "records": message_records()}
    mail = FakeMail(monkeypatch)
    add_reminders(store)
    monkeypatch.setattr("service.tools.notes_tools.parsed_notes",
                        lambda: pytest.fail("notes were read while the switch is off"))
    asyncio.run(rt.tick(store, FakeHub(), now=NOW, force=True))
    assert mail.index_calls == 0 and mail.body_calls == []
    assert engine_of(rt)._store.writing_rows("message") == []
    assert b"zorblax" not in home_bytes(rt.home)


# --------------------------------------------------------------------------------------------
# (2) Switch on: outgoing is measured, incoming is not; no text anywhere on disk
# --------------------------------------------------------------------------------------------

def test_on_measures_outgoing_messages_only(rt, store):
    writing_on(rt)
    rt.box["snap"] = {"state": "ready", "records": message_records(n_out=25, n_in=40)}
    assert rt._ingest_writing(store, NOW) is True
    rows = engine_of(rt)._store.writing_rows("message")
    assert len(rows) == 25, "incoming messages must not be measured"
    stats = rt.call("writing_stats")["message"]
    assert stats["samples"] == 25
    # A second pass over the same snapshot adds nothing (already-measured ids are skipped).
    assert rt._ingest_writing(store, NOW) is False
    assert len(engine_of(rt)._store.writing_rows("message")) == 25


def assert_no_text_on_disk(home, sentinels):
    data = home_bytes(home)
    files = {p.name for p in Path(home).rglob("*") if p.is_file()}
    assert "behavior.db" in files and "behavior_runtime.db" in files
    for sentinel in sentinels:
        assert sentinel.encode() not in data, f"text of a channel ({sentinel}) was written under the home"
    # Not even ordinary words of the messages: only integers and lexicon phrases are kept.
    for phrase in (b"running late", b"after lunch", b"figures over", b"not urgent"):
        assert phrase not in data, phrase


def test_no_channel_text_reaches_any_file_under_the_home(rt, store, monkeypatch, writes_allowed):
    """All four channels, ingested the way the tick does it, then every byte under the home is read
    (databases, WAL, SHM, card cache). The reminder channel is fed through its own step: the tick's
    older Reminders reader (_ingest_reminders) keeps a title as an observation subject, which is
    not part of the writing profile (see test_tick_keeps_message_note_and_mail_text_out)."""
    writing_on(rt)
    rt.box["snap"] = {"state": "ready", "records": message_records()}
    add_reminders(store)
    monkeypatch.setattr("service.tools.notes_tools.parsed_notes", lambda: notes())
    FakeMail(monkeypatch)
    assert rt._ingest_writing(store, NOW) is True
    asyncio.run(rt._writing_email_step(NOW, force=True))
    eng = engine_of(rt)
    # Every channel really was measured, or the grep below would prove nothing.
    for channel, expected in (("message", 25), ("note", 21), ("reminder", 22), ("email", 25)):
        assert len(eng._store.writing_rows(channel)) == expected, channel
    rt.refresh_card(NOW)
    rt.refresh_style_card()
    assert_no_text_on_disk(rt.home, SENTINELS)


def test_tick_keeps_message_note_and_mail_text_out(rt, store, monkeypatch):
    writing_on(rt)
    rt.box["snap"] = {"state": "ready", "records": message_records()}
    monkeypatch.setattr("service.tools.notes_tools.parsed_notes", lambda: notes())
    FakeMail(monkeypatch)
    asyncio.run(rt.tick(store, FakeHub(), now=NOW, force=True))
    asyncio.run(rt.tick(store, FakeHub(), now=NOW + 700, force=True))
    eng = engine_of(rt)
    assert len(eng._store.writing_rows("message")) == 25 and len(eng._store.writing_rows("note")) == 21
    assert len(eng._store.writing_rows("email")) == 30
    assert_no_text_on_disk(rt.home, (S_MSG, S_IN, S_NOTE, S_MAIL))


def test_reminder_titles_are_measured_not_stored_by_the_writing_step(rt, store, writes_allowed):
    """The writing step itself keeps only counts for reminder titles."""
    writing_on(rt)
    add_reminders(store)
    assert rt._ingest_writing_reminders(store, NOW) == 22
    rows = engine_of(rt)._store.writing_rows("reminder")
    assert len(rows) == 22 and all(isinstance(v, (int, float)) for r in rows for k, v in r.items()
                                   if k not in ("greeting", "signoff"))
    assert S_REM.encode() not in home_bytes(rt.home)
    assert rt._ingest_writing_reminders(store, NOW) == 0


def test_stored_sample_rows_hold_only_integers_and_lexicon_phrases(rt, store):
    writing_on(rt)
    rt.box["snap"] = {"state": "ready", "records": message_records(n_in=0)}
    rt._ingest_writing(store, NOW)
    for row in engine_of(rt)._store.writing_rows("message"):
        for key, value in row.items():
            assert isinstance(value, (int, float)) or key in ("greeting", "signoff"), (key, value)
        assert row.get("greeting", "") in ("", "hey")


# --------------------------------------------------------------------------------------------
# (3) The sent-mail step: batch cap, no repeats, email_out times, BridgeError
# --------------------------------------------------------------------------------------------

def test_email_step_reads_at_most_one_batch_per_pass_and_never_repeats(rt, monkeypatch):
    writing_on(rt)
    mail = FakeMail(monkeypatch, sent_rows(n=30, inbox=3))
    asyncio.run(rt._writing_email_step(NOW, force=True))
    first = list(mail.body_calls)
    assert len(first) == br.WRITING_EMAIL_BATCH == 25
    assert all(100 <= r < 130 for r in first), "inbox rows must never be read"
    assert first == [100 + i for i in range(25)], "newest sent mail is read first"
    eng = engine_of(rt)
    assert len(eng._store.writing_rows("email")) == 25

    asyncio.run(rt._writing_email_step(NOW + 1, force=True))
    second = mail.body_calls[len(first):]
    assert sorted(second) == [125 + i for i in range(5)], "only the five unread bodies are asked for"
    assert not set(first) & set(second)
    assert len(eng._store.writing_rows("email")) == 30

    asyncio.run(rt._writing_email_step(NOW + 2, force=True))
    assert len(mail.body_calls) == 30, "an already-measured message is never asked for again"
    assert mail.index_calls == 3


def test_email_step_is_throttled_without_force(rt, monkeypatch):
    writing_on(rt)
    mail = FakeMail(monkeypatch, sent_rows(n=30))
    asyncio.run(rt._writing_email_step(NOW))
    n = len(mail.body_calls)
    assert n == 25
    asyncio.run(rt._writing_email_step(NOW + 60))           # a backlog is retried every 10 minutes
    assert len(mail.body_calls) == n and mail.index_calls == 1
    asyncio.run(rt._writing_email_step(NOW + br.WRITING_BACKLOG_EVERY_S + 1))
    assert len(mail.body_calls) == 30


def test_email_step_records_sent_times_as_email_out(rt, monkeypatch):
    writing_on(rt)
    rows = sent_rows(n=30, inbox=3)
    FakeMail(monkeypatch, rows)
    asyncio.run(rt._writing_email_step(NOW, force=True))
    out = engine_of(rt)._store.observations(kinds=["email_out"])
    assert sorted(o.ts for o in out) == sorted(r["ts"] for r in rows if r["box"] == "sent")
    assert all(o.subject == "" and o.source == "mail" for o in out)
    # Sent times are recorded for all 30 even though only 25 bodies were read this pass.
    assert len(out) == 30 and len(engine_of(rt)._store.writing_rows("email")) == 25


def test_email_step_survives_a_failing_index_read(rt, monkeypatch):
    writing_on(rt)
    mail = FakeMail(monkeypatch)
    mail.index_error = mail.BridgeError("app_not_connected")
    asyncio.run(rt._writing_email_step(NOW, force=True))          # must not raise
    assert "writing_email" in rt.errors and "app_not_connected" in rt.errors["writing_email"]
    assert mail.body_calls == [] and engine_of(rt)._store.writing_rows("email") == []
    mail.index_error = None                                        # the next pass recovers
    asyncio.run(rt._writing_email_step(NOW + 1, force=True))
    assert "writing_email" not in rt.errors and len(mail.body_calls) == 25


def test_email_step_skips_a_failed_body_and_retries_it_later(rt, monkeypatch):
    writing_on(rt)
    mail = FakeMail(monkeypatch, sent_rows(n=10, inbox=0))
    mail.body_errors = {103}
    asyncio.run(rt._writing_email_step(NOW, force=True))
    assert len(engine_of(rt)._store.writing_rows("email")) == 9, "one timeout must not drop the others"
    mail.body_errors = set()
    asyncio.run(rt._writing_email_step(NOW + 1, force=True))
    assert mail.body_calls.count(103) == 2 and mail.body_calls.count(100) == 1
    assert len(engine_of(rt)._store.writing_rows("email")) == 10


def test_email_not_on_disk_is_counted_read_and_not_asked_again(rt, monkeypatch):
    writing_on(rt)
    mail = FakeMail(monkeypatch, sent_rows(n=5, inbox=0))
    mail.missing = {101}
    asyncio.run(rt._writing_email_step(NOW, force=True))
    asyncio.run(rt._writing_email_step(NOW + 1, force=True))
    assert mail.body_calls.count(101) == 1
    # It is stored as a skip marker: it does not become a sample in the statistics.
    from service.behavior import writing
    rows = engine_of(rt)._store.writing_rows("email")
    assert len(rows) == 5 and sum(1 for r in rows if r.get("skip")) == 1
    assert writing.summarize("email", rows) is None          # under MIN_SAMPLES either way


def test_email_step_does_nothing_when_learning_is_off(rt, monkeypatch):
    writing_on(rt)
    mail = FakeMail(monkeypatch)
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    asyncio.run(rt._writing_email_step(NOW, force=True))
    assert mail.index_calls == 0


def test_email_text_is_dropped_after_measuring(rt, monkeypatch):
    writing_on(rt)
    FakeMail(monkeypatch)
    asyncio.run(rt._writing_email_step(NOW, force=True))
    assert S_MAIL.encode() not in home_bytes(rt.home)
    assert rt.call("writing_stats")["email"]["samples"] == 25


# --------------------------------------------------------------------------------------------
# (4) Switching off through PUT /behavior/signals erases samples and writing patterns
# --------------------------------------------------------------------------------------------

def test_put_writing_profile_does_not_rotate_the_epoch(rt, client):
    before = client.get("/behavior/signals").json()
    on = client.put("/behavior/signals", json={"writing_profile": True})
    assert on.status_code == 200 and on.json()["settings"]["writing_profile"] is True
    assert on.json()["epoch"] == before["epoch"]
    again = client.get("/behavior/signals").json()
    assert again["settings"]["writing_profile"] is True and again["epoch"] == before["epoch"]
    off = client.put("/behavior/signals", json={"writing_profile": False})
    assert off.json()["settings"]["writing_profile"] is False and off.json()["epoch"] == before["epoch"]


@pytest.mark.parametrize("bad", ["yes", 1, 0, None, [], {}])
def test_put_writing_profile_must_be_a_boolean(rt, client, bad):
    response = client.put("/behavior/signals", json={"writing_profile": bad})
    assert response.status_code == 422
    assert rt.call("writing_enabled") is False


def test_put_signals_reports_409_while_learning_is_off(client, monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    assert client.put("/behavior/signals", json={"writing_profile": True}).status_code == 409


def test_put_off_erases_samples_timing_and_writing_patterns(rt, client):
    seeded = seed_each_kind(rt)
    eng = engine_of(rt)
    assert set(NEW_KINDS) <= set(seeded), f"setup did not mine every kind: {sorted(seeded)}"
    assert eng._store.writing_rows("message") and eng._store.observations(kinds=["email_out"])
    eng.observe_note_edits([("n1", NOW - 60, "Groceries")])
    assert eng._store.observations(kinds=["note"])
    assert eng._store.meta_get("writing_salt")
    pattern_ids = {p.id for p in rt.call("patterns", kind="writing_style")}
    assert pattern_ids

    off = client.put("/behavior/signals", json={"writing_profile": False})
    assert off.status_code == 200 and off.json()["settings"]["writing_profile"] is False

    assert rt.call("writing_enabled") is False
    assert all(eng._store.writing_rows(c) == [] for c in ("email", "message", "note", "reminder"))
    assert eng._store.observations(kinds=["email_out"]) == []
    assert [o for o in eng._store.observations(kinds=["note"]) if o.source == "notes"] == []
    assert eng._store.meta_get("writing_salt") is None
    remaining = rt.call("patterns")
    assert not [p for p in remaining if p.kind == "writing_style"]
    assert not [p for p in remaining if p.kind == "send_timing" and p.subject in ("email", "note")]
    assert rt.call("writing_stats") == {} and rt.style_card() == ""
    assert client.get("/behavior/patterns?kind=writing_style").json()["patterns"] == []


def test_off_stays_off_when_the_same_data_is_offered_again(rt, client, store):
    writing_on(rt)
    rt.box["snap"] = {"state": "ready", "records": message_records()}
    rt._ingest_writing(store, NOW)
    client.put("/behavior/signals", json={"writing_profile": False})
    assert rt._ingest_writing(store, NOW) is False
    assert engine_of(rt)._store.writing_rows("message") == []


# --------------------------------------------------------------------------------------------
# (5) Forget: DELETE /behavior/data
# --------------------------------------------------------------------------------------------

def test_forget_removes_writing_data_and_resets_the_switch(rt, client, store, tmp_path, clock, monkeypatch):
    writing_on(rt)
    rt.box["snap"] = {"state": "ready", "records": message_records()}
    rt._ingest_writing(store, NOW)
    assert engine_of(rt)._store.writing_rows("message")
    assert (rt.home / "behavior.db").exists()
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, FakeHub()))

    response = client.request("DELETE", "/behavior/data", json={"confirm": True})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert not (rt.home / "behavior.db").exists() and not (rt.home / "behavior_runtime.db").exists()
    assert not any(p.name.startswith("behavior.db") for p in Path(rt.home).glob("*"))

    fresh = make_real(tmp_path, clock)                       # a brand-new runtime on the same home
    try:
        assert fresh.call("writing_enabled") is False, "forget must switch the writing profile back off"
        eng = fresh.local_engine()
        assert all(eng._store.writing_rows(c) == [] for c in ("email", "message", "note", "reminder"))
        assert fresh.call("writing_stats") == {} and fresh.call("patterns") == []
        br.set_runtime(fresh)
        assert client.get("/behavior/signals").json()["settings"]["writing_profile"] is False
    finally:
        br.set_runtime(None)
        fresh.close()


def test_forget_while_the_switch_is_on_leaves_no_sentinel_behind(rt, client, store, monkeypatch):
    writing_on(rt)
    rt.box["snap"] = {"state": "ready", "records": message_records()}
    rt._ingest_writing(store, NOW)
    home = rt.home
    monkeypatch.setattr(behavior_api, "_store_hub", lambda: (store, FakeHub()))
    client.request("DELETE", "/behavior/data", json={"confirm": True})
    assert home_bytes(home) == b"" or all(s.encode() not in home_bytes(home) for s in SENTINELS)


# --------------------------------------------------------------------------------------------
# (6) The style card and where it may go
# --------------------------------------------------------------------------------------------

def fill_writing(rt, n=25):
    writing_on(rt)
    rt.call("observe_writing", "email", [(f"e{i}", NOW - 3600 * (i + 1), mail_text(i)) for i in range(n)])
    rt.call("observe_writing", "message", [(f"m{i}", NOW - 3600 * (i + 1), msg_text(i)) for i in range(n)])
    rt.writing_changed()


def test_style_card_is_bounded_and_built_from_numbers(rt):
    fill_writing(rt)
    card = rt.style_card()
    assert card.startswith("How the user writes")
    assert 0 < len(card.encode("utf-8")) <= br.STYLE_CARD_BYTES == 300
    assert "Emails:" in card or "Messages:" in card
    for sentinel in SENTINELS:
        assert sentinel not in card.lower()


def test_style_card_is_empty_under_the_minimum_sample_count(rt):
    writing_on(rt)
    rt.call("observe_writing", "message", [(f"m{i}", NOW - 3600 * (i + 1), msg_text(i)) for i in range(19)])
    assert rt.refresh_style_card() == ""


def test_refresh_style_card_trims_an_oversized_card_to_whole_lines(rt, monkeypatch):
    fat = "\n".join(["How the user writes (measured, not guessed):"] + [f"Line {i}: " + "x" * 90 for i in range(6)])
    assert len(fat.encode()) > 300
    real_call = rt.call
    monkeypatch.setattr(rt, "call", lambda method, *a, **k: fat if method == "style_card" else real_call(method, *a, **k))
    card = rt.refresh_style_card()
    assert len(card.encode()) <= 300 and set(card.split("\n")) <= set(fat.split("\n"))


def test_style_card_is_cached_for_the_ttl_and_cleared_by_a_switch_change(rt):
    fill_writing(rt)
    first = rt.style_card()
    assert first
    rt.call("configure_writing", False)
    assert rt.style_card() == first, "the cached card is served until the switch change is announced"
    rt.writing_changed()
    assert rt.style_card() == ""


def test_style_card_on_a_running_loop_refreshes_in_the_background(rt):
    fill_writing(rt)

    async def scenario():
        assert rt.style_card() == ""                      # cold cache: no engine work on the loop
        deadline = time.monotonic() + 5
        while rt._style_refreshing and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return rt.style_card()
    assert asyncio.run(scenario()).startswith("How the user writes")


def test_module_style_card_and_record_usage_are_safe_without_a_runtime():
    br.set_runtime(None)
    assert br.style_card() == ""
    br.record_usage("tool_used", "send_message")             # no runtime: a silent no-op


def endpoint(**kw):
    return SimpleNamespace(managed=kw.get("managed", False), credential_ref=kw.get("credential_ref"),
                           base_url=kw.get("base_url", "https://api.example.test/v1"))


@pytest.mark.parametrize("learning,writing,remote,may_go,expect_card", [
    (True, True, False, True, True),
    (True, False, False, True, False),     # the writing switch is off
    (False, True, False, True, False),     # learning is off
    (True, True, True, True, False),       # remote inference is pinned
    (True, True, False, False, False),     # the model is a cloud endpoint
])
def test_style_block_gating_matrix(rt, monkeypatch, learning, writing, remote, may_go, expect_card):
    from service.memory.prompt_blocks import style_block
    fill_writing(rt)
    if not writing:
        rt.call("configure_writing", False)
        rt.writing_changed()
    if not learning:
        monkeypatch.setenv("WISP_BEHAVIOR", "0")
    seen = []
    monkeypatch.setattr("service.behavior_switch.remote_active", lambda: remote)
    monkeypatch.setattr("service.memory.profile.mail_facts_may_go",
                        lambda ep=None, *, managed=False: seen.append((ep, managed)) or may_go)
    ep = endpoint()
    block = style_block(ep, managed=True)
    if expect_card:
        assert block.startswith("\n\nHow the user writes")
        assert len(block.strip().encode("utf-8")) <= 300
        assert seen == [(ep, True)], "the endpoint and the managed flag must reach the privacy predicate"
    else:
        assert block == ""


def test_style_block_never_reaches_a_cloud_model_even_under_cloud_everything(rt, monkeypatch):
    from service.memory import profile
    from service.memory.prompt_blocks import style_block
    fill_writing(rt)
    monkeypatch.setattr("service.wisp_about.cloud_detail", lambda: {
        "mode": "everything", "known": True, "pending": False, "host": "cloud.example.test"})
    cloud = endpoint()
    # The general profile does go to a cloud endpoint under Everything; mail-derived facts do not.
    assert profile.may_send_to(cloud) is True
    assert profile.mail_facts_may_go(cloud) is False
    assert style_block(cloud) == ""
    assert style_block(cloud, managed=False) == ""
    assert style_block(None) == ""
    # The same card is served to the endpoint Wisp manages on this Mac.
    assert style_block(cloud, managed=True).strip().startswith("How the user writes")
    assert style_block(SimpleNamespace(managed=True)).strip().startswith("How the user writes")


def test_style_block_empty_without_a_runtime_or_while_remote_is_pinned(rt, monkeypatch):
    from service.memory.prompt_blocks import style_block
    fill_writing(rt)
    assert style_block(None, managed=True) != ""
    monkeypatch.setattr("service.behavior_switch.remote_active", lambda: True)
    assert style_block(None, managed=True) == ""
    monkeypatch.undo()
    br.set_runtime(None)
    assert style_block(None, managed=True) == ""


def test_style_block_ignores_a_rejected_channel(rt, client):
    """A user who marks the message pattern "not true" no longer has it in the card."""
    from service.memory.prompt_blocks import style_block
    fill_writing(rt)
    rt.call("mine", NOW)
    assert "Messages:" in style_block(None, managed=True)
    pid = next(p.id for p in rt.call("patterns", kind="writing_style") if p.subject == "message")
    assert client.post(f"/behavior/patterns/{pid}", json={"status": "rejected"}).status_code == 200
    rt.writing_changed()
    block = style_block(None, managed=True)
    assert "Messages:" not in block and "Emails:" in block


def test_draft_prompt_includes_the_card_only_when_style_block_returns_it(monkeypatch):
    from service.assistant import email_reply_api
    row = {"sender": "Sam Example <sam@example.test>", "subject": "Figures", "body": "Can you send them?",
           "from": "sam@example.test", "date": NOW}
    monkeypatch.setattr("service.config.endpoints.role_target", lambda role: SimpleNamespace(endpoint=endpoint(managed=True)))
    calls = []
    marker = "\n\nHow the user writes (measured, not guessed):\nEmails: short (~30 words)"
    monkeypatch.setattr("service.memory.prompt_blocks.style_block",
                        lambda ep=None, **kw: calls.append(ep) or marker)
    system, material = email_reply_api.draft_prompt(row)
    assert marker in system and marker not in material and len(calls) == 1
    monkeypatch.setattr("service.memory.prompt_blocks.style_block", lambda ep=None, **kw: "")
    system, _ = email_reply_api.draft_prompt(row)
    assert "How the user writes" not in system
    monkeypatch.setattr("service.memory.prompt_blocks.style_block",
                        lambda ep=None, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    system, _ = email_reply_api.draft_prompt(row)            # the rules alone still draft
    assert "How the user writes" not in system and system.startswith(email_reply_api.DRAFT_RULES)


def test_draft_prompt_end_to_end_with_a_managed_endpoint(rt, monkeypatch):
    from service.assistant import email_reply_api
    fill_writing(rt)
    row = {"sender": "Sam Example <sam@example.test>", "subject": "Figures", "body": "Can you send them?"}
    monkeypatch.setattr("service.config.endpoints.role_target", lambda role: SimpleNamespace(endpoint=endpoint(managed=True)))
    system, _ = email_reply_api.draft_prompt(row)
    assert "How the user writes" in system
    monkeypatch.setattr("service.config.endpoints.role_target", lambda role: SimpleNamespace(endpoint=endpoint()))
    system, _ = email_reply_api.draft_prompt(row)
    assert "How the user writes" not in system


# --------------------------------------------------------------------------------------------
# Agent loop hooks and the notes reader
# --------------------------------------------------------------------------------------------

def test_drafting_tools_and_tool_use_counter(rt):
    from service.agent import loop
    assert isinstance(loop._DRAFTING_TOOLS, frozenset)
    assert loop._DRAFTING_TOOLS == {"send_message", "draft_message", "send_email", "draft_email", "reply_to_email"}
    loop._count_tool_use("send_email")
    loop._count_tool_use("not a label!")                       # invalid labels are dropped, never raised
    queued = list(rt._pending_usage)
    assert [(e, d, ms) for e, d, ms, _ in queued] == [("tool_used", "send_email", 0)]
    rt._flush_usage()
    assert not rt._pending_usage
    rows = engine_of(rt)._store.usage_rows("2000-01-01")
    assert [(r["event"], r["detail"], r["n"]) for r in rows] == [("tool_used", "send_email", 1)]


def test_count_tool_use_never_raises(monkeypatch):
    from service.agent import loop
    monkeypatch.setattr(br, "record_usage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    loop._count_tool_use("send_message")


def test_parsed_notes_reads_the_cache_without_a_snapshot_and_respects_expiry(monkeypatch):
    from service.tools import notes_tools
    fs, rs = "\x01", "\x02"
    raw = rs.join([f"1760000000{fs}Groceries{fs}Notes{fs}milk and eggs", f"bad{fs}x{fs}y{fs}z", "broken record",
                   f"1760000100{fs} Plan {fs}Work{fs} body "])
    monkeypatch.setattr(notes_tools, "_notes", raw)
    monkeypatch.setattr(notes_tools, "_notes_at", time.time())
    issued = notes_tools._issued_snapshot
    parsed = notes_tools.parsed_notes()
    assert parsed == [{"ts": 1760000000.0, "title": "Groceries", "folder": "Notes", "body": "milk and eggs"},
                      {"ts": 1760000100.0, "title": "Plan", "folder": "Work", "body": "body"}]
    assert notes_tools._issued_snapshot is issued, "a plain read must not issue a publication snapshot"
    monkeypatch.setattr(notes_tools, "_notes_at", time.time() - 25 * 3600)
    assert notes_tools.parsed_notes() == [], "an expired cache is empty"


# --------------------------------------------------------------------------------------------
# (7) POST /behavior/usage and the weekly report
# --------------------------------------------------------------------------------------------

def test_usage_endpoint_accepts_only_tab_opened_with_a_label(rt, client):
    ok = client.post("/behavior/usage", json={"event": "tab_opened", "detail": "day"})
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert [(e, d) for e, d, _, _ in rt._pending_usage] == [("tab_opened", "day")]
    bad = [{"event": "ask", "detail": "local"}, {"event": "tool_used", "detail": "send_email"},
           {"event": "approval", "detail": "send_email"}, {"event": "denial", "detail": "x"},
           {"event": "bogus", "detail": "day"}, {"event": "tab_opened"}, {"event": "tab_opened", "detail": ""},
           {"event": "tab_opened", "detail": "my day!"}, {"event": "tab_opened", "detail": "a/b"},
           {"event": "tab_opened", "detail": "x" * 49},
           {"event": "tab_opened", "detail": None}, {"event": None, "detail": "day"}, {}]
    for payload in bad:
        response = client.post("/behavior/usage", json=payload)
        assert response.status_code == 422, payload
    assert len(rt._pending_usage) == 1, "a rejected request must not queue anything"
    assert client.post("/behavior/usage", json=["tab_opened", "day"]).status_code == 422


def test_usage_endpoint_stores_a_label_never_anything_else(rt, client):
    assert client.post("/behavior/usage", json={"event": "tab_opened", "detail": "Brief"}).status_code == 422
    assert client.post("/behavior/usage", json={"event": "tab_opened", "detail": 5.5}).status_code == 422
    client.post("/behavior/usage", json={"event": "tab_opened", "detail": "brief", "prompt": "secret words",
                                         "title": "private"})
    rt._flush_usage()
    rows = engine_of(rt)._store.usage_rows("2000-01-01")
    assert [(r["event"], r["detail"]) for r in rows] == [("tab_opened", "brief")]
    assert b"secret words" not in home_bytes(rt.home) and b"private" not in home_bytes(rt.home)


def test_usage_endpoint_is_409_while_learning_is_off(client, monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    assert client.post("/behavior/usage", json={"event": "tab_opened", "detail": "day"}).status_code == 409
    # Validation still comes first: a malformed request is the caller's fault whatever the switch says.
    assert client.post("/behavior/usage", json={"event": "ask", "detail": "local"}).status_code == 422


def test_usage_counters_appear_in_the_weekly_report(rt, client, clock):
    clock.t = NOW - 2 * DAY                                    # counted two days ago, reported today
    for tab in ("day", "day", "brief"):
        assert client.post("/behavior/usage", json={"event": "tab_opened", "detail": tab}).status_code == 200
    br.record_usage("tool_used", "send_email")
    br.record_usage("ask", "local", 1500)
    br.record_usage("ask", "cloud", 3500)
    rt.record_decision("send_message", True)
    rt.record_decision("send_message", False)
    rt._flush_usage()
    clock.t = NOW
    report = client.get("/behavior/weekly").json()
    usage = report["usage"]
    assert usage["available"] is True and usage["end_date_exclusive"] == "2026-10-12"
    lines = "\n".join(usage["lines"])
    assert "Questions asked: 2" in lines and "Answered on this Mac: 1 of 2; by a cloud model: 1" in lines
    assert "Average response time: 2.5 seconds" in lines
    assert "Tabs opened: 3 (Day 2, Brief 1)" in lines
    assert "Tool uses: 1 (send email 1)" in lines
    assert "Actions approved: 1; declined: 1" in lines
    assert "Wisp usage" not in report["summary"]          # the text summary never carries the usage block


def test_weekly_usage_is_unavailable_before_the_first_full_day(rt, client):
    client.post("/behavior/usage", json={"event": "tab_opened", "detail": "day"})
    rt._flush_usage()                                           # counted today: not a completed day yet
    usage = client.get("/behavior/weekly").json()["usage"]
    assert usage["available"] is False and usage["lines"] == [] and usage["note"]


def test_tick_flushes_the_usage_queue(rt, store, clock):
    clock.t = NOW - 2 * DAY
    br.record_usage("tab_opened", "day")
    clock.t = NOW
    assert len(rt._pending_usage) == 1
    asyncio.run(rt.tick(store, FakeHub(), now=NOW, force=True))
    assert not rt._pending_usage
    assert [r["detail"] for r in engine_of(rt)._store.usage_rows("2000-01-01")] == ["day"]


# --------------------------------------------------------------------------------------------
# (8) POST /behavior/reminder-facts
# --------------------------------------------------------------------------------------------

def facts(n, now=NOW, **extra):
    return [{"id": f"rem-{i}", "created_ts": now - (i + 1) * DAY, "due_ts": now - i * DAY, "completed_ts": None,
             "repeating": i % 5 == 0, **extra} for i in range(n)]


def test_reminder_facts_are_stored_as_dates_and_flags(rt, client):
    response = client.post("/behavior/reminder-facts",
                           json={"reminders": facts(3, title=f"{S_REM} dentist", notes=f"{S_REM} call"),
                                 "snapshot_at": NOW})
    assert response.status_code == 200 and response.json() == {"ok": True, "stored": 3}
    stored = engine_of(rt)._store.observations(kinds=["reminder_fact"])
    assert len(stored) == 3
    assert all(o.subject == "" and set(o.attrs) == {"created_ts", "due_ts", "completed_ts", "repeating"} for o in stored)
    assert sum(1 for o in stored if o.attrs["repeating"]) == 1
    assert S_REM.encode() not in home_bytes(rt.home), "a reminder title in the body must never be stored"
    # Posting the same snapshot again changes nothing.
    assert client.post("/behavior/reminder-facts", json={"reminders": facts(3), "snapshot_at": NOW}).json()["stored"] == 0


def test_reminder_facts_replace_the_open_set_and_keep_completed(rt, client):
    done = {"id": "rem-done", "created_ts": NOW - 5 * DAY, "due_ts": NOW - 4 * DAY, "completed_ts": NOW - 4 * DAY}
    client.post("/behavior/reminder-facts", json={"reminders": facts(3) + [done], "snapshot_at": NOW})
    assert len(engine_of(rt)._store.observations(kinds=["reminder_fact"])) == 4
    client.post("/behavior/reminder-facts", json={"reminders": facts(1), "snapshot_at": NOW})
    kept = engine_of(rt)._store.observations(kinds=["reminder_fact"])
    # The two open reminders that left the snapshot were deleted and stop being counted; the
    # finished one stays (its completion is history).
    assert sorted(o.attrs["completed_ts"] is not None for o in kept) == [False, True]


def test_reminder_facts_drop_malformed_rows(rt, client):
    rows = [{"id": "ok", "created_ts": NOW - DAY, "due_ts": NOW + DAY},
            {"id": "", "created_ts": NOW - DAY}, {"created_ts": NOW - DAY}, {"id": 5, "created_ts": NOW - DAY},
            {"id": "future", "created_ts": NOW + 30 * DAY}, {"id": "nodates"},
            {"id": "nan", "created_ts": float("nan")}, "not a dict", None, 7]
    import json
    response = client.post("/behavior/reminder-facts",
                           content=json.dumps({"reminders": rows, "snapshot_at": NOW}, allow_nan=True),
                           headers={"content-type": "application/json"})
    assert response.status_code == 200 and response.json()["stored"] == 1


def test_reminder_facts_accept_exactly_2000_rows_and_refuse_more(rt, client):
    ok = client.post("/behavior/reminder-facts", json={"reminders": facts(2000), "snapshot_at": NOW})
    assert ok.status_code == 200 and ok.json()["stored"] == 2000
    over = client.post("/behavior/reminder-facts", json={"reminders": facts(2001), "snapshot_at": NOW})
    assert over.status_code == 422
    assert len(engine_of(rt)._store.observations(kinds=["reminder_fact"])) == 2000, "a refused post changes nothing"


@pytest.mark.parametrize("body", [{}, {"reminders": None}, {"reminders": "rem-1"}, {"reminders": {"id": "a"}},
                                  {"reminders": 3}, {"rows": []}])
def test_reminder_facts_refuse_a_non_list(rt, client, body):
    assert client.post("/behavior/reminder-facts", json=body).status_code == 422


def test_reminder_facts_refuse_a_non_object_body(rt, client):
    assert client.post("/behavior/reminder-facts", json=[{"id": "a"}]).status_code == 422
    assert client.post("/behavior/reminder-facts", json="x").status_code == 422


def test_reminder_facts_is_409_while_learning_is_off(client, monkeypatch):
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    assert client.post("/behavior/reminder-facts", json={"reminders": []}).status_code == 409


# --------------------------------------------------------------------------------------------
# (9) New pattern kinds through GET /behavior/patterns and POST /behavior/patterns/{id}
# --------------------------------------------------------------------------------------------

@pytest.fixture
def seeded(rt):
    found = seed_each_kind(rt)
    missing = set(NEW_KINDS) - set(found)
    assert not missing, f"the fixture data did not produce patterns of kind {sorted(missing)}"
    return found


@pytest.mark.parametrize("kind", NEW_KINDS)
def test_patterns_endpoint_returns_each_new_kind(client, seeded, kind):
    response = client.get(f"/behavior/patterns?kind={kind}")
    assert response.status_code == 200
    patterns = response.json()["patterns"]
    assert patterns and {p["kind"] for p in patterns} == {kind}
    for p in patterns:
        assert p["statement"] and p["status"] == "active" and p["id"]
        assert p["stats"].get("basis") == "observed"
    unfiltered = {p["kind"] for p in client.get("/behavior/patterns").json()["patterns"]}
    assert kind in unfiltered


@pytest.mark.parametrize("kind", NEW_KINDS)
@pytest.mark.parametrize("status", ["confirmed", "rejected", "active"])
def test_patterns_of_the_new_kinds_accept_a_verdict(client, rt, seeded, kind, status):
    pid = seeded[kind].id
    response = client.post(f"/behavior/patterns/{pid}", json={"status": status})
    assert response.status_code == 200
    assert response.json()["pattern"]["id"] == pid and response.json()["pattern"]["status"] == status
    assert [p.status for p in rt.call("patterns", kind=kind) if p.id == pid] == [status]
    listed = client.get(f"/behavior/patterns?kind={kind}&status={status}").json()["patterns"]
    assert pid in [p["id"] for p in listed]
    # A verdict survives the next mining pass.
    rt.call("mine", NOW + 60)
    assert [p.status for p in rt.call("patterns", kind=kind) if p.id == pid] == [status]


def test_unknown_pattern_and_bad_verdict_for_a_new_kind(client, seeded):
    pid = seeded["writing_style"].id
    assert client.post(f"/behavior/patterns/{pid}", json={"status": "maybe"}).status_code == 422
    assert client.post("/behavior/patterns/nope", json={"status": "confirmed"}).status_code == 404
    assert client.get("/behavior/patterns?kind=writing_stylez").status_code == 422


def test_rejecting_a_writing_style_removes_that_channel_from_the_measurements(client, rt, seeded):
    pid = seeded["writing_style"].id
    assert "message" in rt.call("writing_stats")
    client.post(f"/behavior/patterns/{pid}", json={"status": "rejected"})
    assert "message" not in rt.call("writing_stats")
    assert "message" in rt.call("writing_stats", include_rejected=True)


# --------------------------------------------------------------------------------------------
# (10) The new kinds stay out of the profile card and every surface that can reach a remote model
# --------------------------------------------------------------------------------------------

def test_new_kinds_are_profile_only_and_excluded_from_the_card(client, rt, seeded):
    assert set(NEW_KINDS) == set(PROFILE_ONLY_KINDS)
    statements = [p.statement for p in seeded.values() if p.kind in NEW_KINDS]
    assert len(statements) == 4
    body = client.get("/behavior/profile").json()
    assert not [p for p in body.get("patterns", []) if p["kind"] in NEW_KINDS]
    haystack = body["card"] + "\n" + body.get("summary", "")
    for statement in statements:
        assert statement not in haystack and statement[:30] not in haystack
    for fragment in ("Your sent messages", "cluster on weekdays", "You usually set a reminder",
                     "tabs you open most", "words in"):
        assert fragment.lower() not in haystack.lower(), fragment
    assert "How the user writes" not in haystack
    # Searching the profile for a phrase that only a writing pattern contains finds nothing.
    narrowed = client.get("/behavior/profile?subject=sent messages").json()
    assert not [p for p in narrowed.get("patterns", []) if p["kind"] in NEW_KINDS]


def test_new_kinds_never_reach_the_cached_card_or_the_prompt_block(rt, seeded):
    from service.memory.prompt_blocks import profile_block
    card = rt.refresh_card(NOW)
    for p in seeded.values():
        assert p.statement not in card
    assert profile_block().count("How the user writes") == 0
    assert (rt.home / "behavior_profile_card.txt").exists() is False or \
        "How the user writes" not in (rt.home / "behavior_profile_card.txt").read_text()
    # They are never offered to the nudge/prediction engine either.
    assert not [p for p in engine_of(rt)._live_patterns() if p.kind in NEW_KINDS]
    own = {q.id for q in seeded.values() if q.kind in NEW_KINDS}
    assert not [p for p in rt.call("predict", NOW) if getattr(p, "pattern_id", "") in own]
    # ...while the Learned window's own list still has them.
    assert {p.kind for p in rt.call("patterns")} >= set(NEW_KINDS)


def test_signals_payload_keeps_the_other_settings(rt, client):
    body = client.get("/behavior/signals").json()
    assert {"settings", "epoch", "activity", "seed"} <= set(body)
    assert "writing_profile" in body["settings"] and len(body["settings"]) > 1
