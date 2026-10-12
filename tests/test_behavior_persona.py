"""The fictional demo persona (Aria Solano): deterministic, clearly fictional,
consistent with founder-demo.json, and planted so a demo fires nudges."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from service.behavior.persona import generate_persona

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=TZ).timestamp()
DEMO = Path(__file__).resolve().parents[1] / "service" / "demo" / "founder-demo.json"


def _dump(p):
    return ([o.to_dict() for o in p.observations], [o.to_dict() for o in p.upcoming],
            p.calendar_events, p.reminders, p.notes, p.expected)


def test_deterministic_for_now_and_seed():
    assert _dump(generate_persona(NOW)) == _dump(generate_persona(NOW))
    assert _dump(generate_persona(NOW, seed=12)) != _dump(generate_persona(NOW))


def test_everything_is_marked_demo_and_split_at_now():
    p = generate_persona(NOW)
    assert p.observations and p.upcoming
    assert all(o.id.startswith("demo:") and o.source == "demo" for o in p.observations + p.upcoming)
    assert all(o.ts < NOW for o in p.observations)
    assert all(o.ts >= NOW and o.kind == "event" for o in p.upcoming)
    assert all(e["demo_id"].startswith("demo:") and e["calendar"] == "Wisp Demo" for e in p.calendar_events)
    assert all(r["demo_id"].startswith("demo:") and r["list"] == "Wisp Demo" for r in p.reminders)
    assert all(n["demo_id"].startswith("demo:") and n["folder"] == "Wisp Demo" and n["body"] for n in p.notes)
    assert len({o.id for o in p.observations + p.upcoming}) == len(p.observations) + len(p.upcoming)
    first = datetime.fromtimestamp(min(o.ts for o in p.observations), TZ).date()
    assert first >= datetime.fromtimestamp(NOW, TZ).date() - timedelta(weeks=8)
    assert max(o.ts for o in p.upcoming) < NOW + 14 * 86400


def test_calendar_seed_matches_kept_events():
    p = generate_persona(NOW)
    kinds = {o.kind for o in p.observations}
    assert {"event", "event_cancelled", "event_moved", "message_in", "message_out", "reminder",
            "reminder_done", "note", "wisp_ask"} <= kinds
    cal = {e["demo_id"] for e in p.calendar_events}
    events = {o.id for o in p.observations + p.upcoming if o.kind == "event"}
    assert cal == events                                     # cancelled ones are not seeded
    for m in (o for o in p.observations if o.kind == "event_moved"):
        assert m.attrs["new_id"] in cal and m.attrs["from_ts"] != m.attrs["to_ts"]
    for c in (o for o in p.observations if o.kind == "event_cancelled"):
        assert c.attrs["start_ts"] == c.ts and c.attrs["subject"] == c.subject
        assert c.id.removeprefix("demo:cancel:") not in cal
    assert all(e["end_ts"] > e["start_ts"] and e["title"] for e in p.calendar_events)


def test_people_match_founder_demo_contacts():
    contacts = {f"{c['given_name']} {c['family_name']}" for c in json.loads(DEMO.read_text())["contacts"]}
    p = generate_persona(NOW)
    people = {o.subject for o in p.observations if o.kind.startswith("message_")}
    assert people - {"Mom"} <= contacts
    attendees = {a for o in p.observations + p.upcoming for a in o.attrs.get("attendees", [])}
    assert attendees <= contacts


def test_upcoming_plants_a_gym_conflict_and_a_back_to_back_run():
    p = generate_persona(NOW)
    by_id = {o.id: o for o in p.upcoming}
    clash = by_id[p.expected["conflict_event_id"]]
    day = datetime.fromtimestamp(clash.ts, TZ)
    assert day.weekday() in (0, 2, 4) and day.date() > datetime.fromtimestamp(NOW, TZ).date()
    assert 6 * 60 + 45 <= day.hour * 60 + day.minute < 7 * 60 + 45
    last = by_id[p.expected["b2b_upcoming_last_event_id"]]
    run_day = datetime.fromtimestamp(last.ts, TZ).date()
    assert run_day.weekday() in (1, 3)
    run = sorted((o for o in p.upcoming if datetime.fromtimestamp(o.ts, TZ).date() == run_day
                  and o.attrs.get("location", "").startswith("Cedarloop office · ")), key=lambda o: o.ts)
    assert len(run) >= 3 and run[-1].id == last.id
    assert all(b.ts - a.end_ts <= 600 for a, b in zip(run, run[1:]))


def test_expected_ground_truth_shape():
    e = generate_persona(NOW).expected
    assert e["gym"]["weekdays"] == [0, 2, 4] and e["gym"]["start_minute"] == 405
    assert e["dev"]["daypart"] == "afternoon" and e["mom"]["weekday"] == 6
    assert 0.4 <= e["b2b_last"]["rate"] <= 0.8 and e["b2b_last"]["opportunities"] >= 14
    assert e["gym_collisions"] == {"moved": 4, "kept": 1} and e["brief_minute"] == 495


def test_other_weekdays_still_plant_nudge_material():
    """Whatever weekday 'now' falls on, the next gym day has a clash and a run lies ahead."""
    for offset in range(7):
        now = NOW + offset * 86400 + 5 * 3600
        p = generate_persona(now)
        assert p.expected["conflict_event_id"] in {o.id for o in p.upcoming}
        assert p.expected["b2b_upcoming_last_event_id"] in {o.id for o in p.upcoming}
