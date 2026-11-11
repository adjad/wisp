"""The background attachment reader (service/attachment_index.py) and its switch (service/attachment_api.py).

WISP_HOME is a tmp dir, the clock and every gate probe are controllable, and nothing sleeps. The bridge
listing and `mail_attachments.index_one` are faked for the gate and pacing tests; the last tests use the real
`index_one`, the real store and the real child process, with only Apple Mail's two bridge reads faked."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import attachment_api, attachment_index as ai, attachment_store, brief_pace, documents, idle, mail_attachments as ma, vision
from service.assistant.hub import hub
from service.mail_memory import bridge, runner
from tests.test_documents import pdf_with_text

def noon_today() -> float:
    """Local noon of the current day: the start of every test clock here.

    The tests move their clock forward by up to an hour or two and expect the reader's per-day count to hold.
    Started from the real time late in the evening, that step crossed local midnight and the count began again
    (seen in CI, where the runner's day ends at 00:00 UTC). Noon leaves twelve hours on both sides and stays
    within half a day of the real clock, which the store and the reader still read for themselves.
    """
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))


NOW = noon_today()
DAY = 86400.0
SECRET = "ZQXSECRETNAME"


def mrow(i, name="report.pdf", ts=None, size=50_000, on_disk=True, attachment_id=None, sender="Bob", subject="Report"):
    return {"rowid": i, "ts": (NOW - i * 60.0) if ts is None else ts, "account": "acct", "box": "other",
            "subject": subject, "sender": "bob@x.com", "sender_name": sender, "rfc_message_id": f"m{i}@x.com",
            "attachment_id": attachment_id or f"{i}.2", "name": name, "on_disk": on_disk, "size": size}


class W:
    """Every probe the reader looks at, set to "all clear"."""

    def __init__(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
        self.now = NOW
        self.busy = False
        self.may = True
        self.other = False
        self.mail_task = None
        self.subs = True
        self.power = (True, 100, False)
        self.hid = 0.0
        self.rows: list[dict] = []
        self.page_size = 1000
        self.list_calls: list[tuple] = []
        self.list_error: Exception | None = None
        self.read: list[str] = []           # names handed to index_one
        self.outcome = "kept"
        self.hold: asyncio.Event | None = None
        self.may_kinds: list[str] = []
        self.back = 0.0                      # how far the wall clock was moved back; the monotonic clock ignores it
        self.deadlines: list[float] = []

        monkeypatch.setattr(ai, "_clock", lambda: self.now)
        monkeypatch.setattr(ai, "_mono", lambda: self.now + self.back)
        monkeypatch.setattr(ai, "_last_prune", None)
        monkeypatch.setattr(ai, "_task", None)
        monkeypatch.setattr(ai, "_next_at", 0.0)
        monkeypatch.setattr(ai, "_last_run", None)
        monkeypatch.setattr(ai, "_last_skip", "")
        monkeypatch.setattr(idle, "foreground_busy", lambda: self.busy)

        def may_start(kind, mono=None):
            self.may_kinds.append(kind)
            return self.may
        monkeypatch.setattr(brief_pace, "may_start", may_start)
        monkeypatch.setattr(runner, "_other_background_work", lambda: self.other)
        monkeypatch.setattr(runner, "_task", None)
        monkeypatch.setattr(runner, "_cached", lambda name, fn: self.power if name == "power" else self.hid)
        monkeypatch.setattr(type(hub), "has_subscribers", property(lambda h: self.subs))

        async def index(terms, since, until, rfc_ids, limit=300, timeout=10.0):
            self.list_calls.append((list(terms), since, until, list(rfc_ids)))
            if self.list_error:
                raise self.list_error
            rows = sorted((r for r in self.rows if not until or r["ts"] < until), key=lambda r: -r["ts"])
            return rows[:self.page_size], len(rows) > self.page_size
        monkeypatch.setattr(bridge, "read_attachment_index", index)

        async def index_one(email, f, *, deadline):
            self.read.append(f.name)
            self.deadlines.append(deadline - time.monotonic())
            assert deadline - time.monotonic() <= ai.FILE_DEADLINE_S + 1
            if self.hold is not None:
                await self.hold.wait()
            return self.outcome
        monkeypatch.setattr(ma, "index_one", index_one)

    async def tick(self) -> bool:
        before = ai._task
        ai.schedule_tick()
        if ai._task is not None and ai._task is not before:
            await ai._task
            return True
        return False

    def after(self) -> float:
        return ai._next_at - (self.now + self.back)


@pytest.fixture
def w(monkeypatch, tmp_path):
    world = W(monkeypatch, tmp_path)
    ai.set_enabled(True)
    return world


def key_of(row) -> str:
    return f"mail:{row['rowid']}:{row['attachment_id']}:{row['size']}:{row['name']}"[:300]


# --- the switch ----------------------------------------------------------------------

def test_default_off_and_nothing_runs(monkeypatch, tmp_path):
    world = W(monkeypatch, tmp_path)
    world.rows = [mrow(1)]
    assert ai.enabled() is False
    assert ai.status()["enabled"] is False
    ai.schedule_tick()
    assert ai._task is None and world.list_calls == [] and ai._last_skip == ""


def test_switch_round_trip_and_modes(monkeypatch, tmp_path):
    W(monkeypatch, tmp_path)
    assert ai.set_enabled(True) is True and ai.enabled() is True
    folder = ai._dir()
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(folder / "settings.json").st_mode) == 0o600
    assert json.loads((folder / "settings.json").read_text()) == {"read_in_background": True}
    assert not list(folder.glob("*.tmp"))
    assert ai.set_enabled(False) is False and ai.enabled() is False


@pytest.mark.parametrize("bad", [1, 0, "true", None, [], "yes"])
def test_non_bool_refused(monkeypatch, tmp_path, bad):
    W(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        ai.set_enabled(bad)
    assert ai.enabled() is False and not (ai._dir() / "settings.json").exists()


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '{"read_in_background": "true"}',
                                  '{"read_in_background": 1}', '{"read_in_background": false}', "{}"])
def test_only_a_real_true_turns_it_on(monkeypatch, tmp_path, text):
    W(monkeypatch, tmp_path)
    ai._dir().mkdir(parents=True)
    (ai._dir() / "settings.json").write_text(text)
    assert ai.enabled() is False


# --- the gates, in order -------------------------------------------------------------

async def test_gates_in_order(w):
    w.rows = [mrow(1)]
    w.busy, w.may, w.other, w.subs = True, False, True, False
    w.mail_task = SimpleNamespace(done=lambda: False)
    runner._task = w.mail_task
    w.power = (False, 20, True)

    assert await w.tick() is False and ai._last_skip == "foreground" and w.may_kinds == []
    w.busy = False
    assert await w.tick() is False and ai._last_skip == "foreground" and w.may_kinds == ["attachments"]
    w.may = True
    assert await w.tick() is False and ai._last_skip == "other_background"
    w.other = False                                      # mail memory's own task still going
    assert await w.tick() is False and ai._last_skip == "other_background"
    runner._task = None
    assert await w.tick() is False and ai._last_skip == "no_subscribers"
    w.subs = True
    assert w.list_calls == []
    assert await w.tick() is True and ai._last_skip == "low_power" and w.list_calls == []
    w.power = (False, 29, False)
    assert await w.tick() is True and ai._last_skip == "battery_low" and w.list_calls == []
    w.power = (False, 30, False)                          # unplugged at 30% is enough
    assert await w.tick() is True and ai._last_skip == "" and w.read == ["report.pdf"]
    assert w.after() == ai.BETWEEN_GAP_S


async def test_not_due_is_silent_and_off_is_silent(w):
    w.rows = [mrow(1), mrow(2, "b.pdf")]
    assert await w.tick() is True
    w.busy = True
    w.now += ai.BETWEEN_GAP_S - 1
    assert await w.tick() is False and ai._last_skip == ""      # not due: the busy gate is not even reached
    w.busy = False
    ai.set_enabled(False)
    w.now += 10
    assert await w.tick() is False and len(w.read) == 1
    ai.set_enabled(True)
    assert await w.tick() is True and len(w.read) == 2


async def test_single_flight(w):
    w.rows = [mrow(1), mrow(2, "b.pdf")]
    w.hold = asyncio.Event()
    ai.schedule_tick()
    first = ai._task
    for _ in range(200):                    # the prune and the tried check run in threads first
        if w.read:
            break
        await asyncio.sleep(0.01)
    ai.schedule_tick()
    ai.schedule_tick()
    assert ai._task is first and w.read == ["report.pdf"]
    w.hold.set()
    await first
    assert w.read == ["report.pdf"]


# --- picking the file ---------------------------------------------------------------

async def test_candidate_filters_and_tried(w):
    big = ai.MAX_BYTES + 1
    w.rows = [mrow(1, "off.pdf", on_disk=False), mrow(2, "huge.pdf", size=big),
              mrow(3, "logo.png", size=5_000), mrow(4, "done.pdf"), mrow(5, "wanted.pdf"),
              mrow(6, "photo.png", size=30_000), mrow(7, "edge.pdf", size=ai.MAX_BYTES)]
    attachment_store.mark_tried(key_of(w.rows[3]), "kept")
    for _ in range(5):
        await w.tick()
        w.now += 2000
    assert w.read == ["wanted.pdf", "photo.png", "edge.pdf"]       # newest first; the rest never read
    assert w.list_calls[0][0] == [] and w.list_calls[0][3] == []        # no terms, no message ids
    assert abs(w.list_calls[0][1] - (w.now - 2000 * 5 - 30 * DAY)) < 11 * 2000


@pytest.mark.parametrize("outcome", ["kept", "known", "skipped", "unreadable"])
async def test_every_outcome_is_marked_tried(w, outcome):
    w.rows = [mrow(1), mrow(2, "b.pdf")]
    w.outcome = outcome
    assert attachment_store.tried(key_of(w.rows[0])) is False
    assert await w.tick() is True
    assert attachment_store.tried(key_of(w.rows[0])) is True
    w.now += 200
    assert await w.tick() is True and w.read == ["report.pdf", "b.pdf"]       # never the same file twice
    w.now += 200
    assert await w.tick() is True and w.read == ["report.pdf", "b.pdf"] and w.after() == ai.EMPTY_GAP_S


async def test_long_key_is_cut(w):
    long = mrow(1, "n" * 300 + ".pdf")
    w.rows = [long]
    assert await w.tick() is True
    assert attachment_store.tried(key_of(long)) is True and len(key_of(long)) == 300


async def test_paging_when_capped(w):
    w.page_size = 2
    w.rows = [mrow(1, "a.pdf", on_disk=False), mrow(2, "b.pdf", on_disk=False), mrow(3, "c.pdf", on_disk=False),
              mrow(4, "d.pdf", on_disk=False), mrow(5, "e.pdf")]
    assert await w.tick() is True and w.read == ["e.pdf"]
    assert len(w.list_calls) == 3
    untils = [c[2] for c in w.list_calls]
    assert untils[0] > NOW and untils[1] < untils[0] and untils[2] < untils[1]


async def test_at_most_three_pages(w):
    w.page_size = 2
    w.rows = [mrow(i, f"{i}.pdf", on_disk=False) for i in range(1, 7)] + [mrow(7, "late.pdf")]
    assert await w.tick() is True
    assert len(w.list_calls) == 3 and w.read == [] and w.after() == ai.EMPTY_GAP_S


# --- pacing -------------------------------------------------------------------------

async def test_pacing_after_a_file(w):
    w.rows = [mrow(i, f"{i}.pdf") for i in range(1, 5)]
    assert await w.tick() is True and w.after() == 90
    w.now += 89
    assert await w.tick() is False
    w.now += 2
    w.hid, w.power = 700.0, (True, 100, False)
    assert await w.tick() is True and w.after() == 30                 # away
    w.now += 31
    w.hid, w.power = 700.0, (False, 40, False)
    assert await w.tick() is True and w.after() == 90                 # away but low on battery: present
    w.now += 91
    w.hid = 599.0
    assert await w.tick() is True and w.after() == 90


async def test_pacing_after_empty_and_failure(w):
    assert await w.tick() is True and w.after() == 1800 and w.read == []
    w.now += 1799
    assert await w.tick() is False
    w.now += 2
    w.list_error = bridge.BridgeError("not_connected")
    assert await w.tick() is True
    assert w.after() == 900 and ai._last_skip == "read_failed" and w.read == []
    w.list_error = None
    w.rows = [mrow(1)]
    w.now += 901
    assert await w.tick() is True and w.read == ["report.pdf"] and ai._last_skip == ""


async def test_daily_cap_survives_a_restart(w, monkeypatch):
    w.rows = [mrow(i, f"{i}.pdf") for i in range(1, 60)]
    for _ in range(ai.MAX_PER_DAY):
        assert await w.tick() is True
        w.now += 100
    assert len(w.read) == 40 and ai.status()["today"] == 40
    assert await w.tick() is False and ai._last_skip == "daily_cap"
    # a restart: module state is gone, the day's file is not
    monkeypatch.setattr(ai, "_task", None)
    monkeypatch.setattr(ai, "_next_at", 0.0)
    monkeypatch.setattr(ai, "_last_skip", "")
    assert await w.tick() is False and ai._last_skip == "daily_cap" and len(w.read) == 40
    assert stat.S_IMODE(os.stat(ai._dir() / "state.json").st_mode) == 0o600
    w.now += 2 * DAY                                                   # another local day
    assert await w.tick() is True and len(w.read) == 41 and ai.status()["today"] == 1


# --- no file-derived text anywhere ----------------------------------------------------

async def test_status_and_logs_carry_nothing_from_a_file(w, caplog, capsys):
    w.rows = [mrow(1, f"{SECRET}.pdf", sender=SECRET, subject=SECRET)]
    caplog.set_level(logging.DEBUG)
    assert await w.tick() is True
    w.now += 5000
    w.rows = []
    w.list_error = RuntimeError(f"{SECRET} exploded")
    assert await w.tick() is True and ai._last_skip == "read_failed"
    st = ai.status()
    assert set(st) == {"enabled", "kept", "last_run", "last_skip", "today"}
    assert st["enabled"] is True and isinstance(st["kept"], int) and st["today"] == 1
    assert isinstance(st["last_run"], float) and st["last_skip"] == "read_failed"
    out = capsys.readouterr()
    for blob in (json.dumps(st), caplog.text, out.out, out.err):
        assert SECRET not in blob
    assert "RuntimeError" in caplog.text


# --- the API ------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    W(monkeypatch, tmp_path)
    app = FastAPI()
    app.include_router(attachment_api.router)
    return TestClient(app)


URL = "/assistant/attachments/background"


def test_api_get_and_put(client):
    got = client.get(URL)
    assert got.status_code == 200
    assert got.json() == {"enabled": False, "kept": 0, "last_run": None, "last_skip": "", "today": 0}
    on = client.put(URL, json={"enabled": True})
    assert on.status_code == 200 and on.json()["enabled"] is True and ai.enabled() is True
    assert client.get(URL).json()["enabled"] is True
    off = client.put(URL, json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False and ai.enabled() is False


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"enabled": 1}, {"enabled": None}, {},
                                  {"enabled": True, "extra": 1}, [True]])
def test_api_refuses_bad_bodies(client, body):
    assert client.put(URL, json=body).status_code == 422
    assert ai.enabled() is False


def test_api_refuses_a_browser_and_non_json(client):
    assert client.put(URL, json={"enabled": True}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.put(URL, content='{"enabled": true}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.put(URL, content='{"enabled": true}').status_code == 415
    assert ai.enabled() is False


# --- the real thing -----------------------------------------------------------------

def trip_models(monkeypatch):
    def trip(*a, **k):
        raise AssertionError("a model path was reached")

    async def atrip(*a, **k):
        raise AssertionError("a model path was reached")
    monkeypatch.setattr(documents, "local_model", trip)
    monkeypatch.setattr(documents, "local_chat", atrip)
    monkeypatch.setattr(vision, "read_images", atrip)


async def test_a_tick_completes_with_every_model_path_raising(w, monkeypatch):
    trip_models(monkeypatch)
    w.rows = [mrow(1)]
    assert await w.tick() is True and w.read == ["report.pdf"] and ai._last_skip == ""


async def test_end_to_end_real_index_one_store_and_child(monkeypatch, tmp_path):
    world = W(monkeypatch, tmp_path)
    monkeypatch.setattr(ma, "index_one", REAL_INDEX_ONE)
    trip_models(monkeypatch)
    ai.set_enabled(True)
    pdf = pdf_with_text("The zeppelin hangar lease ends on 2027-03-31 and the rent is 2100 dollars.")
    world.rows = [mrow(1, "hangar.pdf", size=len(pdf), attachment_id="1.2")]
    reads = []

    async def read(rowid, attachment_id, timeout=45.0):
        reads.append((rowid, attachment_id))
        return pdf, "hangar.pdf", ""
    monkeypatch.setattr(bridge, "read_attachment", read)

    assert attachment_store.search("zeppelin") == []
    assert await world.tick() is True
    assert reads == [(1, "1.2")]
    hits = attachment_store.search("zeppelin")
    assert len(hits) == 1 and hits[0].digest == attachment_store.digest(pdf)
    assert attachment_store.tried(key_of(world.rows[0])) is True
    assert ai.status()["kept"] == 1 and ai.status()["today"] == 1
    # the next tick finds nothing new and does not read it again
    world.now += 200
    assert await world.tick() is True and reads == [(1, "1.2")] and world.after() == ai.EMPTY_GAP_S


REAL_INDEX_ONE = ma.index_one


# --- the background reader stays out of the way of the foreground ----------------------

async def test_a_file_is_read_with_a_twenty_second_deadline(w):
    assert ai.FILE_DEADLINE_S == 20.0
    w.rows = [mrow(1)]
    assert await w.tick() is True
    assert w.deadlines and 18.0 < w.deadlines[0] <= 20.0


async def test_a_turn_that_begins_while_the_list_is_read_stops_the_read(w, monkeypatch):
    w.rows = [mrow(1)]
    answers = iter([False, True, True, True])                # the schedule gate, then the check before the read
    monkeypatch.setattr(idle, "foreground_busy", lambda: next(answers))
    assert await w.tick() is True
    assert w.read == [] and ai._last_skip == "foreground"
    assert attachment_store.tried(key_of(w.rows[0])) is False and ai._today(w.now) == 0
    assert w.after() == ai.FOREGROUND_GAP_S


async def test_the_page_is_checked_against_tried_in_one_call_off_the_loop(w, monkeypatch):
    import threading
    w.rows = [mrow(i, f"{i}.pdf") for i in range(1, 30)]
    calls = []
    real = attachment_store.tried_many

    def many(keys):
        calls.append((len(keys), threading.current_thread() is threading.main_thread()))
        return real(keys)

    def never(key):
        raise AssertionError("one call per candidate on the loop")
    monkeypatch.setattr(attachment_store, "tried_many", many)
    monkeypatch.setattr(attachment_store, "tried", never)
    assert await w.tick() is True and w.read == ["1.pdf"]
    assert calls == [(29, False)]


async def test_a_wall_clock_moved_back_does_not_stall_the_reader(w):
    w.rows = [mrow(i, f"{i}.pdf") for i in range(1, 5)]
    assert await w.tick() is True
    w.now += ai.BETWEEN_GAP_S + 1
    w.now -= 3 * 3600                  # the wall clock jumps back three hours...
    w.back += 3 * 3600                 # ...while real time moved on
    assert await w.tick() is True and len(w.read) == 2


async def test_a_wall_clock_moved_forward_does_not_hurry_the_reader(w):
    w.rows = [mrow(i, f"{i}.pdf") for i in range(1, 5)]
    assert await w.tick() is True
    w.now += 3600                      # the wall clock jumps ahead; no real time passed
    w.back -= 3600
    assert await w.tick() is False and len(w.read) == 1


async def test_the_daily_count_does_not_reset_when_the_clock_goes_back(w):
    w.rows = [mrow(i, f"{i}.pdf", ts=NOW - 5 * DAY - i) for i in range(1, 30)]
    for _ in range(3):
        assert await w.tick() is True
        w.now += 100
    assert ai.status()["today"] == 3
    w.now -= 2 * DAY                   # an earlier date: still the same day for the count
    w.back += 2 * DAY
    assert ai.status()["today"] == 3
    assert await w.tick() is True and ai.status()["today"] == 4
    w.now += 4 * DAY                   # forward past the highest day: a new day
    assert ai.status()["today"] == 0


async def test_the_cap_holds_when_the_clock_goes_back(w):
    w.rows = [mrow(i, f"{i}.pdf") for i in range(1, 60)]
    for _ in range(ai.MAX_PER_DAY):
        assert await w.tick() is True
        w.now += 100
    w.now -= DAY
    w.back += DAY
    assert await w.tick() is False and ai._last_skip == "daily_cap" and len(w.read) == 40


async def test_a_picture_skipped_while_text_recognition_rests_is_asked_again(w, monkeypatch):
    w.outcome = "resting"
    w.rows = [mrow(1, "scan.png", size=60_000), mrow(2, "notes.docx")]
    monkeypatch.setattr(ma, "_ocr_resting_until", time.monotonic() + 300, raising=False)
    assert await w.tick() is True and w.read == ["scan.png"]
    assert attachment_store.tried(key_of(w.rows[0])) is False        # not marked: tried again after the rest
    assert ai._today(w.now) == 0 and w.after() >= 290                # and not before the rest is over
    assert ai._last_skip == "ocr_resting"
    monkeypatch.setattr(ma, "_ocr_resting_until", 0.0, raising=False)
    w.outcome = "skipped"
    w.now += 400
    assert await w.tick() is True and w.read == ["scan.png", "scan.png"]
    assert attachment_store.tried(key_of(w.rows[0])) is True         # rested no more: skipped for good


async def test_a_skipped_file_that_needs_no_recognition_is_tried_even_while_it_rests(w, monkeypatch):
    w.outcome = "skipped"
    w.rows = [mrow(2, "notes.docx")]
    monkeypatch.setattr(ma, "_ocr_resting_until", time.monotonic() + 300, raising=False)
    assert await w.tick() is True
    assert attachment_store.tried(key_of(w.rows[0])) is True


async def test_the_store_is_compacted_about_hourly_and_never_during_a_turn(w, monkeypatch):
    w.rows = [mrow(i, f"{i}.pdf") for i in range(1, 30)]
    pruned = []
    monkeypatch.setattr(attachment_store, "prune_if_due", lambda: pruned.append(1) or 0)
    assert await w.tick() is True and len(pruned) == 1
    w.now += 200
    assert await w.tick() is True and len(pruned) == 1               # not again within the hour
    w.now += 3600
    w.busy = True
    assert await w.tick() is False and len(pruned) == 1              # a turn is running: nothing at all
    w.busy = False
    assert await w.tick() is True and len(pruned) == 2


async def test_a_failing_prune_does_not_stop_the_read(w, monkeypatch):
    w.rows = [mrow(1)]

    def boom():
        raise RuntimeError("locked")
    monkeypatch.setattr(attachment_store, "prune_if_due", boom)
    assert await w.tick() is True and w.read == ["report.pdf"]


# --- erasing reaches the reader and says when it failed --------------------------------

def _hold(mode="IMMEDIATE"):
    import sqlite3
    c = sqlite3.connect(str(attachment_store._db_path()), isolation_level=None, check_same_thread=False)
    c.execute(f"BEGIN {mode}")
    return c


def _forget_app(monkeypatch):
    from service.mail_memory import api
    from service.mail_memory import profile_facts

    async def changed():
        return None
    monkeypatch.setattr(api, "_changed", changed)
    monkeypatch.setattr(api, "store", lambda: None)
    monkeypatch.setattr(profile_facts, "forget_all", lambda st, facts: {"forgotten": 0})
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_forget_mail_memory_erases_and_stops_the_background_reader(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    client = _forget_app(monkeypatch)
    dig = attachment_store.digest(b"bytes")
    assert attachment_store.put(dig, kind="pdf", text="private words", cut=False, account="me@x.com")
    ai.set_enabled(True)
    assert attachment_store.mark_tried("mail:1:1.2:5:a.pdf", "kept")
    out = client.post("/assistant/mail_memory/forget_facts", json={}).json()
    assert out["attachment_text"] == "erased" and out["attachment_reading"] == "off" and out["enabled"] is False
    assert ai.enabled() is False                                       # it will not read the same files again
    assert attachment_store.get(dig) is None and attachment_store.tried("mail:1:1.2:5:a.pdf") is False


def test_forget_mail_memory_does_not_claim_an_erase_that_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(attachment_store, "_ERASE_WAIT_S", 0.2)
    client = _forget_app(monkeypatch)
    dig = attachment_store.digest(b"bytes")
    assert attachment_store.put(dig, kind="pdf", text="private words", cut=False, account="me@x.com")
    ai.set_enabled(True)
    c = _hold()
    try:
        out = client.post("/assistant/mail_memory/forget_facts", json={}).json()
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert out["attachment_text"] == "failed" and ai.enabled() is False
    assert attachment_store.get(dig) is not None                        # still there, and the reply says so


def test_disconnecting_gmail_says_when_the_attachment_text_was_not_erased(monkeypatch, tmp_path):
    from service import gmail_source
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(attachment_store, "_ERASE_WAIT_S", 0.2)
    dig = attachment_store.digest(b"file bytes")
    assert attachment_store.put(dig, kind="pdf", text="private words", cut=False, account="me@gmail.com")
    c = _hold()
    try:
        result = gmail_source.erase_account_data("me@gmail.com")
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert result["attachment_text"].startswith("failed") and attachment_store.get(dig) is not None
    assert gmail_source.erase_account_data("me@gmail.com")["attachment_text"] == "erased 1"


def test_an_account_whose_attachment_text_could_not_be_erased_stays_for_the_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(attachment_store, "_ERASE_WAIT_S", 0.2)
    from service.mail_memory import profile_facts
    meta = {}
    gone_account = "gone@x.com"
    now = 5_000_000.0
    meta["accounts_missing"] = json.dumps(
        {gone_account: [now - runner.GONE_SPAN_S - 10, runner.GONE_RUNS - 1]})
    st = SimpleNamespace(meta=lambda k: meta.get(k), set_meta=lambda k, v: meta.__setitem__(k, v),
                         account_newest=lambda: {gone_account: now - 3600},
                         candidate_accounts=lambda: {gone_account})
    erased = []
    monkeypatch.setattr(profile_facts, "forget_accounts", lambda s, f, accounts: erased.append(set(accounts)) or {"n": 1})
    dig = attachment_store.digest(b"bytes")
    assert attachment_store.put(dig, kind="pdf", text="private words", cut=False, account=gone_account)
    c = _hold()
    try:
        assert runner._forget_gone_accounts(st, [], now) is None
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert erased == [] and attachment_store.get(dig) is not None
    kept = json.loads(meta["accounts_missing"])
    assert gone_account in kept and kept[gone_account][1] >= runner.GONE_RUNS     # counted as gone again next time
    assert runner._forget_gone_accounts(st, [], now + 60) == {"n": 1}
    assert erased == [{gone_account}] and attachment_store.get(dig) is None


# --- review round 2: off the loop, fail closed, forget during a read, future dates -----------

import threading  # noqa: E402


async def test_mark_tried_runs_off_the_event_loop(w, monkeypatch):
    main = threading.get_ident()
    seen = {}
    real = attachment_store.mark_tried

    def spy(key, outcome):
        seen["thread"] = threading.get_ident()
        return real(key, outcome)

    monkeypatch.setattr(attachment_store, "mark_tried", spy)
    w.rows = [mrow(1)]
    assert await w.tick() is True
    assert seen["thread"] != main


async def test_a_busy_store_skips_the_run_instead_of_reading_the_same_file_again(w, monkeypatch):
    w.rows = [mrow(1)]

    def busy(keys):
        raise attachment_store.StoreBusy("locked")

    monkeypatch.setattr(attachment_store, "tried_many", busy)
    assert await w.tick() is True
    assert w.read == [] and ai._last_skip == "store_busy"
    assert w.after() == pytest.approx(ai.FAILURE_GAP_S)


async def test_a_file_that_could_not_be_marked_tried_says_so(w, monkeypatch):
    w.rows = [mrow(1)]
    monkeypatch.setattr(attachment_store, "mark_tried", lambda key, outcome: False)
    assert await w.tick() is True
    assert w.read == ["report.pdf"] and ai._last_skip == "store_busy"
    assert ai.status()["today"] == 1
    assert w.after() == pytest.approx(ai.FAILURE_GAP_S)


async def test_a_wrong_future_date_in_the_count_does_not_stop_the_reader(w):
    ai._write_json(ai._dir() / "state.json", {"day": "2099-01-01", "count": 40})
    assert ai.status()["today"] == 0
    w.rows = [mrow(1)]
    assert await w.tick() is True
    state = json.loads((ai._dir() / "state.json").read_text())
    assert state == {"day": ai._day(w.now), "count": 1}


async def test_the_count_of_today_and_of_a_clock_a_day_ahead_is_kept(w):
    today = ai._day(w.now)
    ai._write_json(ai._dir() / "state.json", {"day": today, "count": 7})
    assert ai.status()["today"] == 7
    ai._write_json(ai._dir() / "state.json", {"day": ai._day(w.now + DAY), "count": 7})
    assert ai.status()["today"] == 7           # the clock moved back a day
    ai._write_json(ai._dir() / "state.json", {"day": ai._day(w.now + 5 * DAY), "count": 7})
    assert ai.status()["today"] == 0           # far ahead: a wrong date, not a clock that went back


async def test_stop_cancels_a_read_in_flight_and_is_bounded(w):
    w.rows = [mrow(1)]
    w.hold = asyncio.Event()
    ai.schedule_tick()
    task = ai._task
    for _ in range(200):
        if w.read:
            break
        await asyncio.sleep(0.01)
    assert w.read and not task.done()
    await ai.stop()
    assert task.done()
    await ai.stop()                             # nothing in flight: returns at once


def _forget_stubs(monkeypatch):
    _forget_app(monkeypatch)
    from service.mail_memory import api
    return api


async def test_a_read_that_would_land_during_forget_mail_memory_stores_nothing(w, monkeypatch):
    """The real index_one, store and child: the erase runs after the epoch was read and before the keep."""
    monkeypatch.setattr(ma, "index_one", REAL_INDEX_ONE)
    trip_models(monkeypatch)
    ai.set_enabled(True)
    pdf = pdf_with_text("The zeppelin hangar lease ends on 2027-03-31 and the rent is 2100 dollars.")
    w.rows = [mrow(1, "hangar.pdf", size=len(pdf), attachment_id="1.2")]

    async def read(rowid, attachment_id, timeout=45.0):
        attachment_store.forget_all()          # "Forget mail memory" lands while the bytes are being fetched
        return pdf, "hangar.pdf", ""
    monkeypatch.setattr(bridge, "read_attachment", read)
    assert await w.tick() is True
    assert attachment_store.search("zeppelin") == [] and attachment_store.count() == 0
    assert attachment_store.get(attachment_store.digest(pdf)) is None
    assert ai.status()["kept"] == 0


async def test_forget_mail_memory_turns_the_reader_off_then_stops_it_then_erases(w, monkeypatch):
    api = _forget_stubs(monkeypatch)
    calls = []
    real_set, real_stop, real_erase = ai.set_enabled, ai.stop, attachment_store.forget_all_result

    def set_enabled(value):
        calls.append(("off", value))
        return real_set(value)

    async def stop():
        calls.append(("stop", ai.enabled()))
        await real_stop()

    def erase():
        calls.append(("erase", ai.enabled()))
        return real_erase()

    monkeypatch.setattr(ai, "set_enabled", set_enabled)
    monkeypatch.setattr(ai, "stop", stop)
    monkeypatch.setattr(attachment_store, "forget_all_result", erase)
    out = await api.forget_facts(api.Nothing())
    assert calls == [("off", False), ("stop", False), ("erase", False)]
    assert out["attachment_reading"] == "off" and out["complete"] is True


async def test_forget_mail_memory_says_still_on_when_the_switch_was_not_written(w, monkeypatch):
    api = _forget_stubs(monkeypatch)

    def refused(value):
        raise OSError("read-only")

    monkeypatch.setattr(ai, "set_enabled", refused)
    out = await api.forget_facts(api.Nothing())
    assert ai.enabled() is True
    assert out["attachment_reading"] == "still on" and out["complete"] is False
    assert out["attachment_text"] == "erased" and out["ok"] is True


async def test_forget_mail_memory_is_not_complete_when_the_erase_failed(w, monkeypatch):
    api = _forget_stubs(monkeypatch)
    monkeypatch.setattr(attachment_store, "_ERASE_WAIT_S", 0.2)
    assert attachment_store.put(attachment_store.digest(b"b"), kind="pdf", text="words", cut=False, account="me@x.com")
    c = _hold()
    try:
        out = await api.forget_facts(api.Nothing())
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert out["attachment_text"] == "failed" and out["attachment_reading"] == "off"
    assert out["ok"] is True and out["complete"] is False


def test_erasing_a_gmail_account_ignores_a_concurrent_erase_flipping_the_old_flag(monkeypatch, tmp_path):
    from service import gmail_source
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    dig = attachment_store.digest(b"file bytes")
    assert attachment_store.put(dig, kind="pdf", text="private words", cut=False, account="me@gmail.com")
    real = attachment_store._run_erase

    def racing(body):
        out = real(body)
        attachment_store._erase_state["failed"] = True      # another erase failed meanwhile
        return out

    monkeypatch.setattr(attachment_store, "_run_erase", racing)
    assert gmail_source.erase_account_data("me@gmail.com")["attachment_text"] == "erased 1"
    assert attachment_store.get(dig) is None


async def test_disconnecting_gmail_erases_the_attachment_text_off_the_event_loop(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock, patch
    import httpx
    from service import gmail_source
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    main = threading.get_ident()
    where = []
    real = attachment_store._run_erase

    def spy(body):
        where.append(threading.get_ident())
        return real(body)

    monkeypatch.setattr(attachment_store, "_run_erase", spy)
    dig = attachment_store.digest(b"file bytes")
    assert attachment_store.put(dig, kind="pdf", text="private words", cut=False, account="me@gmail.com")
    gmail_source._save_config({"client_id": "c", "client_secret": "s", "accounts": {
        "me@gmail.com": {"refresh_token": "r", "scope": gmail_source.SCOPE}}})
    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200))):
        result = await gmail_source.disconnect("me@gmail.com")
    assert result["erased"]["me@gmail.com"]["attachment_text"] == "erased 1"
    assert where and all(t != main for t in where)
    assert attachment_store.get(dig) is None


async def test_a_read_overtaken_by_an_erase_is_resting_so_the_file_is_tried_again(w, monkeypatch):
    """The store refused the write because an erase overtook the read. That is not a file with nothing
    to keep, so the real index_one says "resting" and the reader does not mark the file tried."""
    monkeypatch.setattr(ma, "index_one", REAL_INDEX_ONE)
    trip_models(monkeypatch)
    pdf = pdf_with_text("The glider trailer permit number is 4471 and it runs until 2028.")
    rows = [mrow(1, "permit.pdf", size=len(pdf), attachment_id="1.2")]
    email = ma.from_mail_rows(rows)[0]
    erased = []

    async def read(rowid, attachment_id, timeout=45.0):
        if not erased:
            erased.append(attachment_store.forget_account("someone-else@example.com"))
        return pdf, "permit.pdf", ""
    monkeypatch.setattr(bridge, "read_attachment", read)
    import time as _time
    first = await REAL_INDEX_ONE(email, email.files[0], deadline=_time.monotonic() + 30)
    assert first == "resting" and attachment_store.count() == 0
    again = await REAL_INDEX_ONE(email, email.files[0], deadline=_time.monotonic() + 30)
    assert again == "kept" and attachment_store.search("glider")
