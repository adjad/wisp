"""A linked Google account's past week reaches the judge, and the mail cap is per account."""
from datetime import datetime
from zoneinfo import ZoneInfo

from service.proactive import judge, sources

TZ = "America/Los_Angeles"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo(TZ)).timestamp()
JUDGE = "judge@example.com"


class Store:
    def __init__(self, rows):
        self.rows = rows

    def upcoming(self, now, days=7):
        return [r for r in self.rows if now - 300 <= r["when_ts"] <= now + days * 86400]

    def hidden_commitment_ids(self, now):
        return set()

    def undated_reminders(self):
        return []


def row(id_, title, days_ago, source_id):
    return {"id": id_, "title": title, "when_ts": NOW - days_ago * 86400, "source": "calendar",
            "kind": "event", "source_id": source_id, "all_day": False}


def test_past_week_is_kept_only_for_linked_google_calendars():
    rows = [row("g1", "Design review with Sam", 2, f"gcal:{JUDGE}:e1"),
            row("g2", "Old offsite", 10, f"gcal:{JUDGE}:e2"),
            row("m1", "My dentist", 2, "ek-1")]
    sched = sources.schedule(Store(rows), NOW, TZ, own="me", linked={JUDGE})
    assert [r["title"] for r in sched["past_calendar"]] == ["Design review with Sam"]
    assert all(r["account"] == JUDGE for r in sched["past_calendar"])
    assert not any(r["title"] == "My dentist" for r in sched["calendar"] + sched["past_calendar"])


def test_judge_sees_past_meetings_as_citable_calendar_sources():
    sched = {"calendar": [], "reminders": [],
             "past_calendar": [{"id": "g1", "title": "Design review with Sam", "when_ts": NOW - 2 * 86400,
                                "all_day": False, "account": JUDGE}]}
    material, ids = judge.build_input([], sched, NOW, TZ)
    assert "MEETINGS IN THE LAST 7 DAYS" in material and "[P1]" in material
    assert ids["P1"]["kind"] == "calendar" and ids["P1"]["row"]["id"] == "g1"
