"""Working and quiet hours as one setting (service/user_hours.py): defaults, midnight,
bad data, seeding from the old locations, skipping the question, and who reads it."""
from __future__ import annotations

import json
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service import user_hours as U
from service.assistant.store import AssistantStore
from service.assistant.today import default_preferences

ZONE = "America/New_York"
TZ = ZoneInfo(ZONE)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A whole Wisp home, so seeding finds the old files beside the new one."""
    base = tmp_path / "home"
    base.mkdir()
    monkeypatch.setattr(U, "path", lambda: base / "user_hours.json")
    return base


def old_quiet(base: Path, start: int, end: int) -> None:
    (base / "attention").mkdir(exist_ok=True)
    (base / "attention" / "settings.json").write_text(json.dumps(
        {"mode": "live", "quiet_start": start, "quiet_end": end}))


def day_rows(base: Path, *rows: tuple[str, int, int]) -> None:
    """Rows as the Day planner saves them, for an isolated assistant.db."""
    store = AssistantStore(base / "assistant.db")
    for day, start, end in rows:
        store.today_save_preferences(day, ZONE, {"start_minute": start, "end_minute": end}, 0)
    store._db.close()


# --- defaults ---------------------------------------------------------------


def test_defaults_when_nothing_is_set(home):
    assert U.working_hours() == U.Hours(540, 1020, (0, 1, 2, 3, 4))
    assert U.quiet_hours() == U.Hours(1320, 480, (0, 1, 2, 3, 4, 5, 6))
    assert U.offers_moves() is True and U.is_set() is False


def test_reading_writes_owner_only_and_keeps_no_values_when_nothing_was_set(home):
    U.working_hours()
    info = (home / "user_hours.json").stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert json.loads((home / "user_hours.json").read_text()) == {"version": 1}


# --- midnight ---------------------------------------------------------------


@pytest.mark.parametrize("minute,inside", [(1319, False), (1320, True), (1439, True), (0, True), (479, True),
                                           (480, False), (720, False)])
def test_quiet_hours_wrap_midnight(home, minute, inside):
    assert U.quiet_hours().contains(minute) is inside


def test_the_hour_after_midnight_belongs_to_the_shift_that_started_the_night_before():
    friday_night_shift = U.Hours(22 * 60, 6 * 60, (4,))
    assert friday_night_shift.contains(23 * 60, 4) and friday_night_shift.contains(3 * 60, 5)
    assert not friday_night_shift.contains(3 * 60, 4), "03:00 Friday belongs to Thursday's shift"
    assert not friday_night_shift.contains(23 * 60, 5)


def test_same_start_and_end_is_an_empty_window():
    assert not U.Hours(300, 300).contains(300)
    assert not U.Hours(0, 0).contains(0)


def test_day_span_never_wraps():
    assert U.Hours(540, 1020).day_span() == (540, 1020)
    assert U.Hours(22 * 60, 6 * 60).day_span() == (0, 6 * 60), "the longer side of a night shift"
    assert U.Hours(19 * 60, 1 * 60).day_span() == (19 * 60, 1440)


def test_in_quiet_hours_reads_the_clock_face_of_the_time_given(home):
    assert U.in_quiet_hours(datetime(2026, 11, 9, 23, 30, tzinfo=TZ))
    assert not U.in_quiet_hours(datetime(2026, 11, 9, 12, 0, tzinfo=TZ))
    assert U.in_working_hours(datetime(2026, 11, 9, 12, 0, tzinfo=TZ)), "a Monday noon"
    assert not U.in_working_hours(datetime(2026, 11, 7, 12, 0, tzinfo=TZ)), "a Saturday noon"


# --- bad data never crashes -------------------------------------------------


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"working": 3}',
                                 '{"working": {"start_minute": "nine", "end_minute": 1020}}',
                                 '{"working": {"start_minute": 600, "end_minute": 600}}',
                                 '{"working": {"start_minute": -5, "end_minute": 99999}}',
                                 '{"working": {"start_minute": 600, "end_minute": 700, "days": []}}',
                                 '{"working": {"start_minute": 600, "end_minute": 700, "days": [9]}}',
                                 '{"working": {"start_minute": true, "end_minute": 700}}',
                                 '{"quiet": {"start_minute": 1440, "end_minute": 480}}',
                                 '{"quiet": []}', '[' * 100000])
def test_bad_files_read_as_the_default(home, raw):
    (home / "user_hours.json").write_text(raw)
    assert U.working_hours() == U.DEFAULT_WORKING
    assert U.quiet_hours() == U.DEFAULT_QUIET
    assert U.snapshot()["working"]["start_minute"] == 540


def test_one_bad_section_leaves_the_other(home):
    (home / "user_hours.json").write_text(json.dumps(
        {"working": {"start_minute": "x"}, "quiet": {"start_minute": 60, "end_minute": 120}}))
    assert U.working_hours() == U.DEFAULT_WORKING and U.quiet_hours().start_minute == 60


def test_unreadable_home_still_answers(tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    monkeypatch.setattr(U, "path", lambda: blocker / "user_hours.json")
    assert U.working_hours() == U.DEFAULT_WORKING and U.in_quiet_hours(datetime(2026, 11, 9, 23, tzinfo=TZ))


def test_a_bad_save_is_refused_and_changes_nothing(home):
    U.save(quiet={"start_minute": 60, "end_minute": 120})
    before = (home / "user_hours.json").read_text()
    for bad in ({"working": {"start_minute": 600, "end_minute": 600}},
                {"working": {"start_minute": 600, "end_minute": 700, "days": []}},
                {"quiet": {"start_minute": 2000, "end_minute": 0}}, {"offer_moves": "yes"}):
        with pytest.raises(ValueError):
            U.save(**bad)
    assert (home / "user_hours.json").read_text() == before


# --- seeding from the old locations -----------------------------------------


def test_existing_quiet_hours_are_kept(home):
    old_quiet(home, 23, 6)
    assert U.quiet_hours() == U.Hours(23 * 60, 6 * 60)
    assert U.is_set(), "they chose this"
    old = json.loads((home / "attention" / "settings.json").read_text())
    assert old["quiet_start"] == 23, "the old file is left alone, for rollback"


def test_attention_default_quiet_hours_are_not_taken_as_an_answer(home):
    old_quiet(home, 22, 8)
    assert U.quiet_hours() == U.DEFAULT_QUIET and not U.is_set()


def test_quiet_hours_turned_off_stay_off(home):
    old_quiet(home, 5, 5)
    assert U.quiet_hours().is_empty and not U.in_quiet_hours(datetime(2026, 11, 9, 5, tzinfo=TZ))


@pytest.mark.parametrize("content", ["not json", '{"quiet_start": "ten", "quiet_end": 8}', '{"quiet_start": 99, "quiet_end": 8}'])
def test_a_bad_attention_file_seeds_nothing(home, content):
    (home / "attention").mkdir()
    (home / "attention" / "settings.json").write_text(content)
    assert U.quiet_hours() == U.DEFAULT_QUIET


def test_the_day_planners_usual_hours_are_kept_not_a_one_off(home):
    day_rows(home, ("2026-11-02", 600, 960), ("2026-11-03", 600, 960), ("2026-11-04", 720, 840))
    assert (U.working_hours().start_minute, U.working_hours().end_minute) == (600, 960)
    assert U.working_hours().days == U.WEEKDAYS and U.is_set()


def test_plan_tonight_never_becomes_the_usual_hours(home):
    day_rows(home, ("2026-11-02", 600, 960), ("2026-11-03", 600, 1440), ("2026-11-04", 600, 1440))
    assert (U.working_hours().start_minute, U.working_hours().end_minute) == (600, 960)


def test_on_a_tie_the_most_recent_window_wins(home):
    day_rows(home, ("2026-11-02", 600, 960), ("2026-11-03", 480, 900))
    assert U.working_hours().start_minute == 480


def test_someone_on_the_planners_old_default_keeps_it_and_is_still_asked(home):
    day_rows(home, ("2026-11-02", 540, 1080))
    assert (U.working_hours().start_minute, U.working_hours().end_minute) == (540, 1080)
    assert not U.is_set(), "never chosen, so the question is still worth asking"


def test_seeding_happens_once(home):
    day_rows(home, ("2026-11-02", 600, 960))
    assert U.working_hours().start_minute == 600
    U.save(working={"start_minute": 480, "end_minute": 900})
    day_rows(home, ("2026-11-03", 700, 800))
    assert U.working_hours().start_minute == 480


def test_a_store_that_cannot_be_read_is_retried_not_forgotten(home):
    (home / "assistant.db").write_text("this is not a database, yet")
    assert U.working_hours() == U.DEFAULT_WORKING
    assert not (home / "user_hours.json").exists(), "not recorded as done"
    (home / "assistant.db").unlink()
    day_rows(home, ("2026-11-02", 600, 960))
    assert U.working_hours().start_minute == 600


def test_a_store_without_the_table_counts_as_nothing_saved(home):
    sqlite3.connect(home / "assistant.db").close()
    assert U.working_hours() == U.DEFAULT_WORKING and (home / "user_hours.json").exists()


# --- skipping, saving, erasing ----------------------------------------------


def test_skipping_the_step_saves_nothing_and_changes_nothing(home):
    """Skip in the flow makes no request at all, so this is the state it leaves."""
    assert U.snapshot() == {"working": {"start_minute": 540, "end_minute": 1020, "days": [0, 1, 2, 3, 4]},
                            "quiet": {"start_minute": 1320, "end_minute": 480},
                            "offer_moves": True, "is_set": False}


def test_saving_one_part_keeps_the_rest_and_counts_as_answered(home):
    old_quiet(home, 23, 6)
    result = U.save(working={"start_minute": 600, "end_minute": 900, "days": [0, 5, 5]}, now=100.0)
    assert result["working"] == {"start_minute": 600, "end_minute": 900, "days": [0, 5]}
    assert result["quiet"] == {"start_minute": 23 * 60, "end_minute": 6 * 60} and result["is_set"] is True
    assert stat.S_IMODE((home / "user_hours.json").stat().st_mode) == 0o600


def test_clear_goes_back_to_the_defaults_and_does_not_read_the_old_values_again(home):
    old_quiet(home, 23, 6)
    U.save(working={"start_minute": 600, "end_minute": 900})
    assert U.clear()["is_set"] is False
    assert U.working_hours() == U.DEFAULT_WORKING and U.quiet_hours() == U.DEFAULT_QUIET


def test_erasing_the_profile_erases_the_hours(home, tmp_path, monkeypatch):
    from service.memory import profile as P, profile_api
    monkeypatch.setattr(P, "PROFILE_PATH", tmp_path / "profile.json")
    U.save(working={"start_minute": 600, "end_minute": 900})
    app = FastAPI()
    app.include_router(profile_api.router)
    assert TestClient(app).delete("/profile").status_code == 200
    assert U.is_set() is False and U.working_hours() == U.DEFAULT_WORKING


# --- the Day planner reads it -----------------------------------------------


def test_a_day_nobody_adjusted_uses_the_usual_hours(home, tmp_path):
    store = AssistantStore(tmp_path / "day.db")
    assert store.today_snapshot("2026-11-09", ZONE)["preferences"]["end_minute"] == 1020
    U.save(working={"start_minute": 600, "end_minute": 900})
    prefs = store.today_snapshot("2026-11-09", ZONE)["preferences"]
    assert (prefs["start_minute"], prefs["end_minute"]) == (600, 900)


def test_a_day_someone_adjusted_keeps_its_own_hours(home, tmp_path):
    store = AssistantStore(tmp_path / "day.db")
    store.today_save_preferences("2026-11-09", ZONE, {"start_minute": 480, "end_minute": 720}, 0)
    U.save(working={"start_minute": 600, "end_minute": 900})
    assert store.today_snapshot("2026-11-09", ZONE)["preferences"]["end_minute"] == 720
    assert store.today_snapshot("2026-11-10", ZONE)["preferences"]["end_minute"] == 900


def test_new_usual_hours_reach_today_and_later_but_never_a_past_day(home, tmp_path):
    store = AssistantStore(tmp_path / "day.db")
    for day in ("2026-11-08", "2026-11-09", "2026-11-10"):
        store.today_save_preferences(day, ZONE, {"start_minute": 540, "end_minute": 1080, "plan_all_day": True}, 0)
    assert store.today_apply_working_hours("2026-11-09", 600, 900) == 2
    assert store.today_snapshot("2026-11-08", ZONE)["preferences"]["end_minute"] == 1080
    after = store.today_snapshot("2026-11-09", ZONE)
    assert after["preferences"]["end_minute"] == 900 and after["preferences"]["plan_all_day"] is True
    assert after["revision"] == 2, "an edit takes a new revision, so a stale screen can't overwrite it"
    assert store.today_apply_working_hours("2026-11-09", 600, 900) == 0, "nothing left to change"


def test_a_daylight_saving_day_that_loses_the_usual_start_still_plans(home):
    U.save(working={"start_minute": 150, "end_minute": 900})                # 02:30
    assert default_preferences("2026-03-08", ZONE)["start_minute"] == 540   # 02:30 doesn't exist that day
    assert default_preferences("2026-03-09", ZONE)["start_minute"] == 150


def test_new_hours_skip_a_saved_day_they_do_not_fit(home, tmp_path):
    store = AssistantStore(tmp_path / "day.db")
    store.today_save_preferences("2026-03-08", ZONE, {"start_minute": 540, "end_minute": 1080}, 0)
    assert store.today_apply_working_hours("2026-03-08", 150, 900) == 0
    assert store.today_snapshot("2026-03-08", ZONE)["preferences"]["start_minute"] == 540


def test_a_night_shift_plans_its_longer_side(home):
    U.save(working={"start_minute": 22 * 60, "end_minute": 6 * 60})
    prefs = default_preferences("2026-11-09", ZONE)
    assert (prefs["start_minute"], prefs["end_minute"]) == (0, 360)


def test_replan_without_hours_uses_the_usual_ones(home, tmp_path, monkeypatch):
    from service.assistant import today_api
    store = AssistantStore(tmp_path / "day.db")
    monkeypatch.setattr(today_api, "assistant_store", store)
    monkeypatch.setattr(today_api, "snapshot", lambda day, timezone: store.today_snapshot(day, timezone))
    U.save(working={"start_minute": 600, "end_minute": 900})
    today_api.replan(today_api.Replan(day="2026-11-09", timezone=ZONE, revision=0, plan_all_day=True))
    prefs = store.today_snapshot("2026-11-09", ZONE)["preferences"]
    assert (prefs["start_minute"], prefs["end_minute"], prefs["plan_all_day"]) == (600, 900, True)


# --- alerts respect quiet hours ---------------------------------------------


def test_nudges_follow_the_shared_quiet_hours(home):
    from service.behavior.nudges import FeedbackState, select
    from service.behavior.types import Nudge
    def nudge(id_):
        return Nudge(id=id_, kind="contact_window", title="t", body="b", because="w", created_at=0.0, priority=0.5)
    def chosen(hour):
        now = datetime(2026, 11, 9, hour, tzinfo=TZ).timestamp()
        return select([nudge("a")], now=now, tz=TZ, recent=[], fb=FeedbackState(), exists=lambda _: False)
    assert chosen(12) and not chosen(23) and not chosen(7), "22:00-08:00 by default"
    U.save(quiet={"start_minute": 10 * 60, "end_minute": 14 * 60})
    assert not chosen(12) and chosen(23)
    U.save(quiet={"start_minute": 0, "end_minute": 0})
    assert chosen(3), "no quiet hours means none"


def test_attention_settings_route_writes_through_to_the_one_setting(home, tmp_path, monkeypatch):
    from service.assistant import attention_api
    monkeypatch.setattr(attention_api, "MOE_DIR", tmp_path / "attn")
    app = FastAPI()
    app.include_router(attention_api.router)
    client = TestClient(app)
    prefix = attention_api.router.prefix
    assert client.put(prefix + "/settings", json={"quiet_start": 21}).status_code == 200
    assert U.quiet_hours() == U.Hours(21 * 60, 8 * 60)
    assert client.put(prefix + "/settings", json={"daily_cap": 2}).status_code == 200
    assert U.quiet_hours() == U.Hours(21 * 60, 8 * 60), "another setting leaves them alone"


# --- the route --------------------------------------------------------------


@pytest.fixture
def api(home, tmp_path, monkeypatch):
    from service.assistant import hours_api
    from service.assistant.hub import hub
    store = AssistantStore(tmp_path / "day.db")
    monkeypatch.setattr(hours_api, "assistant_store", store)
    published = []
    async def publish(event, **_):
        published.append(event)
    monkeypatch.setattr(hub, "publish", publish)
    app = FastAPI()
    app.include_router(hours_api.router)
    client = TestClient(app)
    client.store, client.published = store, published
    return client


def test_route_reads_and_saves(api):
    assert api.get("/assistant/hours").json()["is_set"] is False
    reply = api.patch("/assistant/hours", json={"working": {"start_minute": 600, "end_minute": 900, "days": [0, 1]},
                                                "quiet": {"start_minute": 0, "end_minute": 0}, "offer_moves": False})
    assert reply.status_code == 200
    body = reply.json()
    assert body["working"]["days"] == [0, 1] and body["quiet"]["end_minute"] == 0
    assert body["offer_moves"] is False and body["is_set"] is True and api.published == [{"type": "changed"}]


def test_route_carries_new_hours_onto_today(api):
    today = datetime.now(TZ).date().isoformat()
    api.store.today_save_preferences(today, ZONE, {"start_minute": 540, "end_minute": 1080}, 0)
    assert api.patch("/assistant/hours", json={"working": {"start_minute": 600, "end_minute": 900},
                                               "timezone": ZONE}).status_code == 200
    assert api.store.today_snapshot(today, ZONE)["preferences"]["end_minute"] == 900


@pytest.mark.parametrize("body", [{"working": {"start_minute": 600, "end_minute": 600}},
                                  {"working": {"start_minute": 600, "end_minute": 700, "days": []}},
                                  {"working": {"start_minute": "9", "end_minute": 700}},
                                  {"working": {"start_minute": 1440, "end_minute": 700}},
                                  {"quiet": {"start_minute": 0, "end_minute": 1440}},
                                  {"offer_moves": "yes"}, {"unknown": 1}, {"timezone": "Mars/Base"}])
def test_route_refuses_bad_values_and_saves_nothing(api, home, body):
    assert api.patch("/assistant/hours", json=body).status_code == 422
    assert U.is_set() is False


def test_route_refuses_a_web_page(api):
    assert api.patch("/assistant/hours", json={"offer_moves": False}, headers={"Origin": "null"}).status_code == 403
    assert api.patch("/assistant/hours", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert U.offers_moves() is True
