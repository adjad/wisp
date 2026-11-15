"""The Day tab's week row: GET /assistant/today/week (read-only, invented data only)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.assistant import today_api
from service.assistant.store import AssistantStore

ZONE = "America/Los_Angeles"
START = "2026-09-21"   # a Monday
NOW = datetime(2026, 9, 21, 8, tzinfo=ZoneInfo(ZONE)).timestamp()


def at(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


def event(id, start, end, **values):
    return dict(id=id, source_id=id, source="calendar", title="Made-up seminar", kind="event",
                when_ts=start, end_ts=end, all_day=False) | values


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(today_api, "assistant_store", store)
    monkeypatch.setattr(today_api.time, "time", lambda: NOW)   # the same module the store's clock reads
    app = FastAPI()
    app.include_router(today_api.router)
    with TestClient(app) as c:
        yield c, store
    store._db.close()


COVER = dict(coverage_start=NOW - 86400 * 30, coverage_end=NOW + 86400 * 30)


def sync(store, source, items):
    store.sync_source(source, items, diagnostics=COVER)


def week(c, **params):
    return c.get("/assistant/today/week", params={"start": START, "timezone": ZONE, **params})


def test_seven_days_with_counts_from_the_calendar(client):
    c, store = client
    sync(store, "calendar", [
        event("e1", at(22, 9), at(22, 10)),
        event("e2", at(22, 9, 30), at(22, 11)),            # overlaps e1: minutes count once
        event("e3", at(23, 0), at(24, 0), all_day=True),
    ])
    sync(store, "reminders", [dict(id="r1", source_id="r1", source="reminders", title="Hand in lab 2",
                                         kind="reminder", when_ts=at(24, 17), end_ts=None, all_day=False)])
    got = week(c).json()
    assert [d["day"] for d in got["days"]] == [f"2026-09-{n}" for n in range(21, 28)]
    tue = got["days"][1]
    assert tue["calendar_ready"] is True
    assert (tue["on_clock"], tue["on_clock_minutes"], tue["due"], tue["all_day"]) == (2, 120, 0, 0)
    assert tue["spans"] == [[540, 600, "calendar"], [570, 660, "calendar"]]
    assert got["days"][2]["all_day"] == 1 and got["days"][2]["on_clock"] == 0
    assert got["days"][3]["due"] == 1


def test_a_day_without_a_current_calendar_has_no_numbers(client, monkeypatch):
    c, store = client
    sync(store, "calendar", [event("e1", at(22, 9), at(22, 10))])
    real = today_api.build_plan

    def stale_tuesday(day, *a, **kw):
        plan = real(day, *a, **kw)
        if day == "2026-09-22":
            plan["sources"]["calendar"]["ready"] = False
        return plan
    monkeypatch.setattr(today_api, "build_plan", stale_tuesday)
    days = week(c).json()["days"]
    assert days[1] == {"day": "2026-09-22", "calendar_ready": False}
    assert days[0]["calendar_ready"] is True


def test_a_day_that_cannot_be_read_has_no_numbers(client, monkeypatch):
    c, store = client
    real = store.today_snapshot

    def broken(day, tz):
        if day == "2026-09-25":
            raise RuntimeError("unreadable")
        return real(day, tz)
    monkeypatch.setattr(store, "today_snapshot", broken)
    days = week(c).json()["days"]
    assert days[4] == {"day": "2026-09-25", "calendar_ready": False}


def test_never_builds_the_full_snapshot_or_writes(client, monkeypatch):
    c, store = client
    sync(store, "calendar", [event("e1", at(22, 9), at(22, 10))])
    monkeypatch.setattr(today_api, "snapshot", lambda *a, **k: pytest.fail("full snapshot built"))
    import service.study.day_hook as day_hook
    monkeypatch.setattr(day_hook, "attach", lambda *a, **k: pytest.fail("study hook ran"))
    before = store._db.total_changes
    assert week(c).status_code == 200
    assert store._db.total_changes == before


@pytest.mark.parametrize("params", [{"start": "2026-9-21"}, {"start": "tomorrow"}, {"timezone": "Nowhere/X"},
                                    {"start": "9999-12-30"}])
def test_bad_queries_are_refused(client, params):
    c, _ = client
    assert week(c, **params).status_code == 422
