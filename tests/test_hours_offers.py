"""The offer to move an event outside working hours (service/assistant/hours_offers.py):
which events it skips, how rarely it speaks, and that nothing moves without the user."""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import user_hours as U
from service.assistant import hours_offers as O
from service.assistant.store import AssistantStore

ZONE = "America/New_York"
TZ = ZoneInfo(ZONE)
# A Monday. Events are tomorrow evening and the next day unless a test says otherwise.
NOON = datetime(2026, 11, 9, 12, tzinfo=TZ).timestamp()
SOURCE = Path(O.__file__).read_text()


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "path", lambda: tmp_path / "offers" / "hours_offers.json")


@pytest.fixture
def store(tmp_path):
    return AssistantStore(tmp_path / "assistant.db")


def own(sid="e1", title="Review draft", start=None, end=None, **values) -> dict:
    start = at(10, 19) if start is None else start
    return dict(id=sid, source_id=sid, source="calendar", title=title, kind="event", when_ts=start,
                end_ts=at(10, 20) if end is None else end, all_day=False, invitee_count=0,
                organizer_is_user=True, calendar_writable=True) | values


def sync(store, *rows):
    store.sync_source("calendar", list(rows))


def offer(store, now=NOON):
    return O.next_offer(store, ZONE, now=now)


# --- the offer itself -------------------------------------------------------


def test_an_own_evening_event_gets_a_named_free_slot_inside_working_hours(store):
    sync(store, own())
    got = offer(store)
    assert got["title"] == "Review draft" and got["side"] == "after" and got["source_id"] == "e1"
    assert got["duration_minutes"] == 60
    slot = datetime.fromtimestamp(got["slot"]["start_ts"], TZ)
    assert slot.weekday() < 5 and 9 <= slot.hour and slot.hour + 1 <= 17 and got["slot"]["label"]


def test_an_early_morning_event_is_before_working_hours(store):
    sync(store, own(start=at(10, 7), end=at(10, 8)))
    assert offer(store)["side"] == "before"


def test_the_slot_avoids_what_is_already_there(store):
    sync(store, own(), own("busy", "Standup", at(11, 9), at(11, 12), invitee_count=3))
    slot = datetime.fromtimestamp(offer(store)["slot"]["start_ts"], TZ)
    assert not (slot.day == 11 and slot.hour < 12)


def test_friday_nights_slot_skips_the_weekend(store):
    friday_noon = datetime(2026, 11, 13, 12, tzinfo=TZ).timestamp()
    sync(store, own(start=at(13, 19), end=at(13, 20)))
    slot = datetime.fromtimestamp(offer(store, friday_noon)["slot"]["start_ts"], TZ)
    assert slot.weekday() == 0, "Monday, not Saturday"


def test_no_free_slot_means_no_offer_and_no_offer_spent(store):
    sync(store, own(), *[own(f"w{d}", f"Full {d}", at(d, 9), at(d, 17), invitee_count=2) for d in range(10, 18)])
    assert offer(store) is None
    assert not O.path().exists()


def test_a_slot_outside_the_usual_hours_is_never_named(store, monkeypatch):
    """A day's own hours can stretch ("Plan tonight"), so its suggestions can't be trusted alone."""
    from service.assistant import checkin
    evening = {"start_ts": at(11, 19), "end_ts": at(11, 20), "label": "Tuesday 7:00 PM", "reason": "x"}
    monkeypatch.setattr(checkin, "move_suggestions", lambda *a, **k: {"suggestions": [evening]})
    sync(store, own())
    assert offer(store) is None


# --- what it skips ----------------------------------------------------------


@pytest.mark.parametrize("change", [
    {"all_day": True},
    {"invitee_count": 2},
    {"invitee_count": None},
    {"organizer_is_user": False},
    {"calendar_writable": False},
    {"calendar_writable": None},
    {"title": "Dinner with Sam"},
    {"title": "Gym"},
    {"end": at(10, 23, 30)},                       # too long
    {"start": at(10, 14), "end": at(10, 15)},      # inside working hours
    {"start": at(10, 16, 30), "end": at(10, 17, 30)},  # partly inside
    {"start": at(14, 11), "end": at(14, 12)},      # a Saturday
    {"start": at(9, 13), "end": at(9, 13, 30)},    # under two hours away
])
def test_skipped_events_get_no_offer(store, change):
    values = dict(change)
    kwargs = {k: values.pop(k) for k in ("start", "end", "title") if k in values}
    sync(store, own(**kwargs, **values))
    assert offer(store) is None


def test_a_repeating_event_is_skipped(store):
    sync(store, own(), own(start=at(17, 19), end=at(17, 20)))
    assert offer(store) is None, "two occurrences share one calendar id"


def test_a_class_imported_as_separate_events_is_skipped_too(store):
    sync(store, own("a", "STAT 101"), own("b", "STAT 101", at(17, 19), at(17, 20)))
    assert offer(store) is None


def test_the_same_title_at_a_different_time_is_not_a_repeat(store):
    sync(store, own("a", "Review"), own("b", "Review", at(17, 7), at(17, 8)))
    assert offer(store)["source_id"] == "a"


def test_events_past_the_lookahead_wait(store):
    sync(store, own(start=at(14, 7), end=at(14, 8)), own("far", "Far off", at(16, 19), at(16, 20)))
    assert offer(store) is None


def test_a_working_saturday_counts(store):
    U.save(working={"start_minute": 540, "end_minute": 1020, "days": [0, 1, 2, 3, 4, 5]})
    friday = datetime(2026, 11, 13, 12, tzinfo=TZ).timestamp()
    sync(store, own(start=at(14, 19), end=at(14, 20)))
    assert offer(store, friday)["source_id"] == "e1"


# --- how rarely -------------------------------------------------------------


def test_one_a_day_across_many_events(store):
    sync(store, own("a", "First"), own("b", "Second", at(11, 19), at(11, 20)))
    assert offer(store)["source_id"] == "a"
    assert offer(store)["source_id"] == "a", "the same one on a refresh, not a second one"
    O.dismiss(O.offer_id("a"), now=NOON)
    assert offer(store) is None, "tomorrow, not straight after"
    assert offer(store, NOON + 86400)["source_id"] == "b"


def test_an_ignored_offer_does_not_come_back_on_another_day(store):
    sync(store, own(start=at(11, 19), end=at(11, 20)))
    assert offer(store)["source_id"] == "e1"
    assert offer(store, NOON + 86400) is None


def test_a_dismissal_is_final(store):
    sync(store, own(start=at(11, 19), end=at(11, 20)))
    got = offer(store)
    assert O.dismiss(got["id"], now=NOON)
    assert offer(store) is None and offer(store, NOON + 86400) is None


def test_an_accepted_offer_is_not_repeated(store):
    sync(store, own(start=at(11, 19), end=at(11, 20)))
    assert O.accept(offer(store)["id"], now=NOON)
    assert offer(store) is None and offer(store, NOON + 86400) is None


def test_three_dismissals_in_a_row_pause_offers(store):
    now = NOON
    for index in range(3):
        day = 10 + index
        sync(store, own(f"e{index}", f"Thing {index}", at(day, 19), at(day, 20)))
        got = offer(store, now)
        assert got["source_id"] == f"e{index}"
        O.dismiss(got["id"], now=now)
        now += 86400
    sync(store, own("later", "Another", at(13, 19), at(13, 20)))
    assert offer(store, now) is None, "paused"
    state = O._load()
    assert state["paused_until"] == pytest.approx(now - 86400 + O.DISMISS_PAUSE_DAYS * 86400)
    state["paused_until"] = now - 1
    O._save(state)
    assert offer(store, now)["source_id"] == "later", "and back after the pause"


def test_taking_one_resets_the_count(store):
    now = NOON
    for index, finish in enumerate((O.dismiss, O.dismiss, O.accept, O.dismiss)):
        day = 10 + index
        sync(store, own(f"e{index}", f"Thing {index}", at(day, 19), at(day, 20)))
        got = offer(store, now)
        assert got is not None, index
        finish(got["id"], now=now)
        now += 86400


def test_quiet_hours_hold_the_offer(store):
    sync(store, own())
    assert offer(store, at(9, 23)) is None
    assert not O.path().exists(), "held, not spent"
    assert offer(store, at(10, 12))["source_id"] == "e1"


def test_no_quiet_hours_means_no_hold(store):
    U.save(quiet={"start_minute": 0, "end_minute": 0})
    sync(store, own())
    assert offer(store, at(9, 23))["source_id"] == "e1"


def test_the_off_switch(store):
    U.save(offer_moves=False)
    sync(store, own())
    assert offer(store) is None


def test_finishing_an_unknown_offer_is_refused(store):
    assert O.dismiss("0" * 24, now=NOON) is False
    assert O.accept("0" * 24, now=NOON) is False


def test_a_corrupt_state_file_reads_as_empty(store):
    O.path().parent.mkdir(parents=True)
    O.path().write_text("not json")
    sync(store, own())
    assert offer(store)["source_id"] == "e1"
    assert stat_mode(O.path()) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


# --- nothing moves without the user -----------------------------------------


def test_the_offer_changes_no_event_and_queues_no_action(store):
    sync(store, own())
    before = store.calendar_offer_rows(at(9, 0), at(20, 0))
    actions = store._db.execute("SELECT COUNT(*) FROM assistant_events").fetchone()[0]
    got = offer(store)
    O.accept(got["id"], now=NOON)
    O.dismiss(got["id"], now=NOON)
    assert store.calendar_offer_rows(at(9, 0), at(20, 0)) == before
    assert store._db.execute("SELECT COUNT(*) FROM assistant_events").fetchone()[0] == actions


def test_this_code_reaches_no_write_path():
    """The module and its route can read and record. Moving is Ask's: its confirmation card
    and bound, verified move. So neither may name any tool or queue that changes a calendar."""
    for text in (SOURCE, Path(O.__file__).with_name("hours_api.py").read_text()):
        names = {n.name if isinstance(n, ast.FunctionDef) else n.attr if isinstance(n, ast.Attribute)
                 else n.id if isinstance(n, ast.Name) else "" for n in ast.walk(ast.parse(text))}
        imported = {a.name for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names} | {n.module or "" for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ImportFrom)}
        assert not names & {"update_event", "execute_tool", "claim_calendar_action", "enqueue", "queue_event",
                            "create_calendar_event", "moveEvent", "prepare_event_move", "bind_event_move"}
        assert not any("tools" in module or "agent" in module for module in imported), imported


def test_taking_the_offer_goes_through_asks_confirmation():
    """In the app, Move it sends the Move panel's request to Ask and nothing else."""
    view = Path(__file__).resolve().parents[1] / "app/Sources/WispApp/NotchDayView.swift"
    source = view.read_text()
    block = source[source.index("HoursOfferRow(offer:"):source.index("if let movingEvent {")]
    assert "model.askAbout(request)" in block
    for write in ("moveEvent", "update_event", "CalendarReader", "CalendarMove"):
        assert write not in block
    row = (view.parent / "UserHoursView.swift").read_text()
    row = row[row.index("struct HoursOfferRow"):]
    assert "moveEvent" not in row and "Wisp confirms before the calendar changes" in row


# --- the route --------------------------------------------------------------


@pytest.fixture
def api(store, monkeypatch):
    from service.assistant import hours_api
    monkeypatch.setattr(hours_api, "assistant_store", store)
    monkeypatch.setattr(hours_api, "_availability", lambda: None)
    app = FastAPI()
    app.include_router(hours_api.router)
    client = TestClient(app)
    return client


def test_route_returns_finishes_and_validates(api, store, monkeypatch):
    calls = {}
    real = O.next_offer
    def fixed(store_, timezone, *, now, behavior=None):
        calls["timezone"] = timezone
        return real(store_, timezone, now=NOON, behavior=behavior)
    monkeypatch.setattr(O, "next_offer", fixed)
    sync(store, own())
    got = api.post("/assistant/hours/offer", json={"timezone": ZONE}).json()["offer"]
    assert got["source_id"] == "e1" and calls["timezone"] == ZONE
    assert api.post(f"/assistant/hours/offer/{got['id']}/dismiss", json={}).json() == {"ok": True}
    assert api.post("/assistant/hours/offer", json={"timezone": ZONE}).json() == {"offer": None}
    assert api.post("/assistant/hours/offer/not-an-id/dismiss", json={}).status_code == 422
    assert api.post("/assistant/hours/offer", json={"timezone": "Mars/Base"}).status_code == 422
    assert api.post("/assistant/hours/offer", json={"timezone": ZONE, "x": 1}).status_code == 422


def test_route_refuses_a_web_page(api):
    assert api.post("/assistant/hours/offer", json={"timezone": ZONE}, headers={"Origin": "null"}).status_code == 403
    assert api.post("/assistant/hours/offer/" + "0" * 24 + "/accept", content="{}",
                    headers={"Content-Type": "text/plain"}).status_code == 415
