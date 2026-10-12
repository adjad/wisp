"""Miners recover the routines planted in the fictional demo persona.

Synthetic data only: ``generate_persona`` (Aria Solano, fictional) and small
hand-built timelines. The engine runs on a temp SQLite file.
"""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from service.behavior import miners
from service.behavior.engine import BehaviorEngine
from service.behavior.persona import generate_persona
from service.behavior.types import Observation

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=TZ).timestamp()      # a Tuesday morning


@pytest.fixture(scope="module")
def mined(tmp_path_factory):
    persona = generate_persona(NOW)
    eng = BehaviorEngine(tmp_path_factory.mktemp("beh") / "b.db", tz="America/Los_Angeles")
    eng.observe(persona.observations + persona.upcoming)
    patterns = eng.mine(NOW)
    yield persona, patterns
    eng.close()


def one(patterns, kind, subject):
    found = [p for p in patterns if p.kind == kind and p.subject == subject]
    assert found, f"no {kind} pattern for {subject!r}: {[(p.kind, p.subject) for p in patterns]}"
    return max(found, key=lambda p: p.confidence)


def test_gym_habit_window_and_weekdays(mined):
    persona, patterns = mined
    gym = one(patterns, "habit", "fitness")
    assert gym.window.weekdays == (0, 2, 4)                      # Mon/Wed/Fri; the odd Tuesday is ignored
    assert 6 * 60 + 30 <= gym.window.start_minute <= 7 * 60
    assert 7 * 60 + 30 <= gym.window.end_minute <= 8 * 60
    assert gym.confidence > 0.5 and gym.support >= 15
    assert "work out" in gym.statement and "Mon, Wed and Fri mornings" in gym.statement
    assert gym.stats["label"] == "Morning lift"
    assert gym.next_expected is not None and gym.next_expected > NOW


def test_focus_habit_weekday_mornings(mined):
    _, patterns = mined
    focus = one(patterns, "habit", "focus")
    assert focus.window.weekdays == (0, 1, 2, 3, 4)
    assert (focus.window.start_minute, focus.window.end_minute) == (9 * 60, 11 * 60)


def test_dev_afternoon_contact_rhythm(mined):
    _, patterns = mined
    dev = one(patterns, "contact_rhythm", "Dev Malik")
    assert dev.stats["daypart"] == "afternoon"
    assert dev.window.weekdays == (0, 1, 2, 3, 4)
    assert 13 * 60 <= dev.window.start_minute <= 15 * 60 and dev.window.end_minute <= 18 * 60
    assert "Dev Malik most weekday afternoons" in dev.statement
    assert dev.stats["reply_latency_s"] is not None and dev.stats["reply_latency_s"] < 3600
    assert dev.stats["calls"] >= 4                              # the Thursday "Dev / Aria 1:1"


def test_mom_sunday_evenings(mined):
    _, patterns = mined
    mom = one(patterns, "contact_rhythm", "Mom")
    assert mom.window.weekdays == (6,)
    assert mom.stats["daypart"] == "evening"
    assert mom.statement.startswith("You call Mom on Sunday evenings")


def test_back_to_back_last_cancel_rate(mined):
    persona, patterns = mined
    b2b = one(patterns, "cancel_propensity", "back_to_back_last")
    assert 0.4 <= b2b.stats["rate"] <= 0.8
    assert b2b.support == persona.expected["b2b_last"]["cancelled"]
    assert b2b.opportunities == persona.expected["b2b_last"]["opportunities"]
    assert b2b.stats["last_share"] >= 0.6 and b2b.stats["lift"] >= 1.5
    assert "usually the last one" in b2b.statement
    assert set(b2b.window.weekdays) <= {1, 3}
    # Precision: nothing else is claimed as a cancel habit from this history.
    cancel_subjects = {p.subject for p in patterns if p.kind == "cancel_propensity"
                       and p.stats["action"] == "cancel"}
    assert cancel_subjects == {"back_to_back_last"}


def test_meetings_over_gym_get_moved(mined):
    persona, patterns = mined
    prot = one(patterns, "habit_protection", "fitness")
    assert prot.stats["moved"] == persona.expected["gym_collisions"]["moved"] == 4
    assert prot.stats["kept"] == persona.expected["gym_collisions"]["kept"] == 1
    assert "move it (4 of 5 times)" in prot.statement


def test_wisp_brief_around_8_15(mined):
    _, patterns = mined
    brief = one(patterns, "wisp_usage", "brief")
    assert abs(brief.stats["around_minute"] - 8 * 60 - 15) <= 10
    assert brief.window.weekdays == (0, 1, 2, 3, 4)
    assert "8:15am" in brief.statement or "8:10am" in brief.statement or "8:20am" in brief.statement


def test_day_shape_and_evidence_contract(mined):
    persona, patterns = mined
    assert any(p.kind == "day_shape" and p.subject == "weekdays" for p in patterns)
    ids = {o.id for o in persona.observations}
    for p in patterns:
        assert len(p.id) == 16 and 0 < len(p.evidence) <= 5
        assert set(p.evidence) <= ids, p
        assert 0.0 <= p.confidence <= 1.0 and p.support <= p.opportunities


def test_mining_is_deterministic_and_fast(tmp_path):
    persona = generate_persona(NOW)
    eng = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles")
    eng.observe(persona.observations + persona.upcoming)
    t0 = time.perf_counter()
    first = eng.mine(NOW)
    assert time.perf_counter() - t0 < 2.0
    again = eng.mine(NOW)
    assert [(p.id, p.statement, p.confidence) for p in first] == [(p.id, p.statement, p.confidence) for p in again]
    eng.close()


def test_habit_window_survives_dst_change(tmp_path):
    """History crossing the Nov 1 2026 DST change keeps the 6:45 wall-clock window."""
    now = datetime(2026, 11, 17, 8, 0, tzinfo=TZ).timestamp()
    persona = generate_persona(now)
    eng = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles")
    eng.observe(persona.observations)
    gym = one(eng.mine(now), "habit", "fitness")
    assert gym.window.weekdays == (0, 2, 4) and gym.window.start_minute == 6 * 60 + 45
    eng.close()


# ------------------------------------------------------------- unit level

def _yoga(day, minute=7 * 60, n=0):
    start = datetime.combine(day, datetime.min.time(), TZ) + timedelta(minutes=minute)
    return Observation(id=f"y{n}", kind="event", ts=start.timestamp(), end_ts=start.timestamp() + 3600,
                       subject="Yoga", category="fitness")


def test_habit_needs_four_occurrences_over_three_weeks():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=TZ).timestamp()
    saturdays = [datetime(2026, 10, d, tzinfo=TZ).date() for d in (3, 10, 17, 24)]
    first = Observation(id="anchor", kind="event", ts=datetime(2026, 10, 1, 9, tzinfo=TZ).timestamp(),
                        end_ts=datetime(2026, 10, 1, 10, tzinfo=TZ).timestamp(), subject="Errand", category="errand")
    three = [_yoga(d, n=i) for i, d in enumerate(saturdays[:3])]
    assert not [p for p in miners.mine_all([first] + three, now, TZ) if p.subject == "fitness"]
    four = [_yoga(d, n=i) for i, d in enumerate(saturdays)]
    (p,) = [p for p in miners.mine_all([first] + four, now, TZ) if p.subject == "fitness"]
    assert p.window.weekdays == (5,) and p.support == 4 and p.opportunities == 4
    assert p.statement == "You usually work out on Saturday mornings (7–8am)"


def test_wilson_lower_bound_prefers_more_evidence():
    assert miners.wilson_lower(0, 0) == 0.0
    assert miners.wilson_lower(4, 4) < miners.wilson_lower(40, 40) < 1.0
    assert miners.wilson_lower(20, 24) < 20 / 24


def test_pattern_ids_are_stable_hashes():
    gym = miners.pattern_id("habit", "fitness", "0,2,4|morning")
    assert gym == miners.pattern_id("habit", "fitness", "0,2,4|morning") and len(gym) == 16
    assert gym != miners.pattern_id("habit", "fitness", "5|morning")


def test_back_to_back_runs_ignore_double_bookings():
    day = datetime(2026, 10, 6, tzinfo=TZ).date()

    def ev(i, minute, dur):
        start = datetime.combine(day, datetime.min.time(), TZ) + timedelta(minutes=minute)
        return Observation(id=f"e{i}", kind="event", ts=start.timestamp(),
                           end_ts=start.timestamp() + dur * 60, subject=f"M{i}", category="meeting")

    slots = miners.assign_features(miners.build_slots(
        [ev(1, 840, 30), ev(2, 875, 30), ev(3, 905, 30), ev(4, 950, 30), ev(5, 960, 60)], [], [], TZ))
    by_id = {s.loc.obs.id: s for s in slots}
    assert [by_id[i].position for i in ("e1", "e2", "e3")] == ["first", "middle", "last"]   # 5-min gaps join
    assert by_id["e4"].run_len == 1 and by_id["e5"].run_len == 1   # e4 after a 15 min gap; e5 overlaps e4


def test_categories_from_titles():
    from service.behavior.categorize import categorize
    assert categorize("Morning lift") == "fitness"
    assert categorize("Cash runway review") == "meeting"          # "run" is a whole word only
    assert categorize("Dev / Aria 1:1") == "one_on_one"
    assert categorize("Sync with Dev") == "one_on_one"
    assert categorize("Hiring sync") == "meeting"
    assert categorize("Call Mom") == "call"
    assert categorize("Customer call", {"attendees": ["a", "b", "c"]}) == "meeting"
    assert categorize("Focus block") == "focus" and categorize("Team lunch") == "meal"
    assert categorize("Dentist") == "health" and categorize("Flight to SFO") == "travel"
    assert categorize("Zzz") == "other"


def test_contact_spread_over_the_week_is_not_a_rhythm():
    """Finding 4: when no weekday qualifies the miner used to emit weekdays=()
    (= every day) with a bogus 0-60 minute window: "most afternoons (12–1am)"."""
    base = datetime(2026, 9, 7, 15, 0, tzinfo=TZ)                 # a Monday afternoon
    obs = []
    for i, dd in enumerate((0, 1, 2, 3, 4, 12)):                   # one per weekday + a Saturday
        t = (base + timedelta(days=dd)).timestamp()
        obs.append(Observation(id=f"m{i}a", kind="message_in", ts=t, subject="Sam"))
        if i < 3:
            obs.append(Observation(id=f"m{i}b", kind="message_out", ts=t + 600, subject="Sam"))
    pats = miners.mine_all(obs, (base + timedelta(days=20)).timestamp(), TZ)
    sam = [p for p in pats if p.kind == "contact_rhythm" and p.subject == "Sam"]
    assert sam == [] or all(p.window.weekdays and p.window.end_minute - p.window.start_minute > 0
                            and "12–1am" not in p.statement for p in sam)
    assert all(p.window.weekdays for p in pats if p.kind == "contact_rhythm")


def test_fmt_range_midnight_end():
    from service.behavior.timeutil import fmt_range
    assert fmt_range(1380, 1440) == "11pm–12am"
    assert fmt_range(14 * 60, 17 * 60) == "2–5pm" and fmt_range(405, 465) == "6:45–7:45am"
    assert fmt_range(690, 780) == "11:30am–1pm" and fmt_range(0, 60) == "12–1am"
