"""BehaviorEngine facade: ingest, snapshot lifecycle diffing, pattern status,
phraser fallbacks, cursors. Synthetic observations on a temp SQLite file."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from service.behavior.engine import BehaviorEngine
from tests.fixtures.behavior_persona import generate_persona
from service.behavior.types import Feedback, Nudge, Observation

TZ = ZoneInfo("America/Los_Angeles")
H = 3600.0
D = 86400.0
T0 = datetime(2026, 10, 13, 8, 0, tzinfo=TZ).timestamp()        # Tuesday 8am


@pytest.fixture
def eng(tmp_path):
    e = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles", clock=lambda: T0)
    yield e
    e.close()


def ev(oid, ts, subject="Pipeline review", sid=None, dur=30 * 60, **attrs):
    return Observation(id=oid, kind="event", ts=ts, end_ts=ts + dur, subject=subject, source="calendar",
                       source_id=sid or oid, attrs=attrs)


def snap(e, items, taken_at, start=None, end=None):
    return e.observe_snapshot("calendar", items, window_start=start if start is not None else taken_at - 7 * D,
                              window_end=end if end is not None else taken_at + 14 * D, taken_at=taken_at)


def kinds(e, kind):
    return {o.id: o for o in e._store.observations(kinds=[kind])}


# ------------------------------------------------------------------ ingest

def test_observe_is_idempotent_and_categorizes(eng):
    obs = [ev("a", T0 + H, "Morning lift"), ev("b", T0 + 2 * H, "Hiring sync")]
    assert eng.observe(obs) == 2
    assert eng.observe(obs) == 0                                   # identical re-send: no change
    assert eng.observe([ev("a", T0 + 3 * H, "Morning lift")]) == 1
    cats = {o.id: o.category for o in kinds(eng, "event").values()}
    assert cats == {"a": "fitness", "b": "meeting"}
    assert eng.status()["observations"] == {"event": 2}


# --------------------------------------------------------------- snapshots

def test_first_snapshot_has_nothing_to_diff(eng):
    assert snap(eng, [ev("a", T0 + D)], T0) == {"upserted": 1, "cancelled": 0, "moved": 0}


def test_vanished_upcoming_event_is_cancelled(eng):
    snap(eng, [ev("a", T0 + D, attendees=["Robin", "Dev"]), ev("b", T0 + 2 * D)], T0)
    out = snap(eng, [ev("b", T0 + 2 * D)], T0 + H)
    assert out["cancelled"] == 1 and out["moved"] == 0
    c = kinds(eng, "event_cancelled")["cancel:a"]
    assert c.ts == T0 + D and c.subject == "Pipeline review" and c.category == "meeting"
    assert c.attrs["start_ts"] == T0 + D and c.attrs["end_ts"] == T0 + D + 1800
    assert c.attrs["source_id"] == "a" and c.attrs["attendees"] == ["Robin", "Dev"]
    assert "a" not in kinds(eng, "event")                         # no longer part of the plan


def test_aging_out_and_started_events_are_not_cancellations(eng):
    past, started = ev("past", T0 - 3 * D), ev("started", T0 + 30 * 60)
    snap(eng, [past, started, ev("future", T0 + D)], T0)
    # A day later the past event left the window and "started" already began.
    out = snap(eng, [ev("future", T0 + D)], T0 + D - H, start=T0 - 2 * D)
    assert out["cancelled"] == 0 and out["moved"] == 0
    assert {"past", "started", "future"} <= set(kinds(eng, "event"))   # history is kept


def test_events_beyond_a_shrunken_window_are_not_cancelled(eng):
    snap(eng, [ev("far", T0 + 20 * D)], T0, end=T0 + 30 * D)
    assert snap(eng, [], T0 + H, end=T0 + 14 * D)["cancelled"] == 0


def test_one_off_move_by_source_id(eng):
    snap(eng, [ev("cal:x:1", T0 + D, sid="x")], T0)
    out = snap(eng, [ev("cal:x:2", T0 + 9 * D, sid="x")], T0 + H)      # moved 8 days: still a move
    assert out == {"upserted": 1, "cancelled": 0, "moved": 1}
    m = kinds(eng, "event_moved")["move:cal:x:1"]
    assert m.attrs["from_ts"] == T0 + D and m.attrs["to_ts"] == T0 + 9 * D
    assert m.attrs["new_id"] == "cal:x:2"
    assert set(kinds(eng, "event")) == {"cal:x:2"}


def test_same_id_new_time_is_a_move(eng):
    snap(eng, [ev("stable", T0 + D)], T0)
    out = snap(eng, [ev("stable", T0 + D + 2 * H)], T0 + H)
    assert out["moved"] == 1 and out["cancelled"] == 0
    (m,) = kinds(eng, "event_moved").values()
    assert m.attrs["from_ts"] == T0 + D and m.attrs["to_ts"] == T0 + D + 2 * H


def test_recurring_series_pairs_nearest_occurrence(eng):
    daily = [ev(f"s:{i}", T0 + i * D, sid="standup") for i in range(1, 6)]
    snap(eng, daily, T0)
    # Occurrence 3 moved two hours later; the others are untouched.
    moved = [o for o in daily if o.id != "s:3"] + [ev("s:3b", T0 + 3 * D + 2 * H, sid="standup")]
    out = snap(eng, moved, T0 + H)
    assert out == {"upserted": 1, "cancelled": 0, "moved": 1}
    (m,) = kinds(eng, "event_moved").values()
    assert m.id == "move:s:3" and m.attrs["to_ts"] == T0 + 3 * D + 2 * H


def test_recurring_deletion_while_window_slides_is_a_cancel(eng):
    weekly = [ev(f"w:{i}", T0 + i * 7 * D, sid="sync") for i in range(1, 3)]
    snap(eng, weekly, T0, end=T0 + 15 * D)
    # w:2 deleted; the slid window reveals w:3, which the old snapshot could not see.
    later = [weekly[0], ev("w:3", T0 + 21 * D, sid="sync")]
    out = snap(eng, later, T0 + 7 * H, end=T0 + 22 * D)
    assert out["cancelled"] == 1 and out["moved"] == 0
    assert "cancel:w:2" in kinds(eng, "event_cancelled")


def test_reappearing_event_clears_its_cancellation(eng):
    snap(eng, [ev("a", T0 + D)], T0)
    snap(eng, [], T0 + H)
    assert "cancel:a" in kinds(eng, "event_cancelled")
    snap(eng, [ev("a", T0 + D)], T0 + 2 * H)
    assert "cancel:a" not in kinds(eng, "event_cancelled") and "a" in kinds(eng, "event")


def test_snapshot_cancellations_feed_cancel_propensity(tmp_path):
    """Weekly back-to-back runs where the last meeting keeps vanishing are learned
    end to end through observe_snapshot (not just from persona lifecycle rows)."""
    e = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles")
    base = datetime(2026, 8, 4, 14, 0, tzinfo=TZ).timestamp()         # a Tuesday
    for week in range(8):
        day = base + week * 7 * D
        run = [ev(f"r{week}:{i}", day + i * 1800, f"Review {i}", sid=f"rev{i}:{week}") for i in range(3)]
        e.observe_snapshot("calendar", run, window_start=day - D, window_end=day + 7 * D, taken_at=day - D)
        keep = run if week in (2, 5) else run[:2]                     # the last one cancelled 6 of 8 weeks
        e.observe_snapshot("calendar", keep, window_start=day - D, window_end=day + 7 * D, taken_at=day - H)
    pats = e.mine(base + 60 * D)
    (b2b,) = [p for p in pats if p.subject == "back_to_back_last"]
    assert (b2b.support, b2b.opportunities) == (6, 8)
    e.close()


# ------------------------------------------------------------ pattern status

def test_status_survives_remine_and_rejected_is_silenced(eng):
    persona = generate_persona(T0)
    eng.observe(persona.observations + persona.upcoming)
    pats = eng.mine(T0)
    gym = next(p for p in pats if p.kind == "habit" and p.subject == "fitness")
    prot = next(p for p in pats if p.kind == "habit_protection")
    eng.set_pattern_status(gym.id, "confirmed")
    eng.set_pattern_status(prot.id, "rejected")
    again = {p.id: p for p in eng.mine(T0 + H)}
    assert again[gym.id].status == "confirmed"
    assert prot.id not in again                                       # mine() returns active+confirmed
    assert eng.patterns(status="rejected")[0].id == prot.id
    card = eng.profile_card(T0)
    assert "(you confirmed)" in card and prot.statement not in card
    assert all(p["id"] != prot.id for p in eng.describe()["patterns"])
    with pytest.raises(ValueError):
        eng.set_pattern_status(gym.id, "maybe")
    with pytest.raises(KeyError):
        eng.set_pattern_status("nope", "active")


def test_status_and_cursor(eng):
    persona = generate_persona(T0)
    eng.observe(persona.observations + persona.upcoming)
    assert eng.status()["last_mined_at"] is None
    created = eng.generate_nudges(T0)                                  # mines on first use
    st = eng.status()
    assert st["last_mined_at"] == T0 and st["patterns"] > 10
    assert st["nudge_cursor"] == len(created) == st["nudges"] and st["schema_version"] == 1
    first, cur = eng.nudges_since(0, limit=1)
    assert len(first) == 1 and cur == first[0].seq == 1
    rest, cur2 = eng.nudges_since(cur)
    assert [n.seq for n in rest] == list(range(2, len(created) + 1)) and cur2 == len(created)
    assert eng.nudges_since(cur2) == ([], cur2)


def test_feedback_records_and_rejects_unknown_verdicts(eng):
    eng.feedback(Feedback("n_missing", "useful", T0))
    assert eng.status()["observations"]["nudge_feedback"] == 1
    with pytest.raises(ValueError):
        eng.feedback(Feedback("n_missing", "meh", T0))


def test_empty_engine_knows_nothing(eng):
    assert eng.profile_card(T0) == ""
    assert eng.mine(T0) == [] and eng.predict(T0) == [] and eng.generate_nudges(T0) == []
    d = eng.describe()
    assert d["patterns"] == [] and "haven't learned" in d["summary"]


# ------------------------------------------------------------------ phraser

class Phraser:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def phrase(self, kind, facts):
        if self.fail:
            raise RuntimeError("model down")
        return f"[{kind}] {facts['title']}"

    def categorize(self, titles, categories):
        self.calls.append(list(titles))
        if self.fail:
            raise RuntimeError("model down")
        return {t: "fitness" for t in titles if t == "Ironwood session"} | {"Bogus": "not-a-category"}


def _ironwood(i):
    day = datetime(2026, 9, 7, 6, 45, tzinfo=TZ).timestamp() + i * 7 * D
    return ev(f"iw{i}", day, "Ironwood session", dur=3600)


def test_phraser_labels_leftover_titles_once_and_rewrites_bodies(tmp_path):
    ph = Phraser()
    e = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles", phraser=ph)
    e.observe([_ironwood(i) for i in range(5)] + [ev("x", T0 - D, "Bogus")])
    pats = e.mine(T0)
    assert any(p.kind == "habit" and p.subject == "fitness" for p in pats)
    e.mine(T0)
    assert ph.calls == [["Bogus", "Ironwood session"]]                # cached, incl. the bad answer
    assert e._store.meta_get("category_cache") == {"Ironwood session": "fitness", "Bogus": "other"}
    n = e._phrase(Nudge(id="n1", kind="day_preview", title="T", body="code body", because="b", created_at=T0))
    assert n.body == "[day_preview] T" and n.because == "b"
    e.close()


def test_failing_phraser_keeps_code_text(tmp_path):
    e = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles", phraser=Phraser(fail=True))
    e.observe([_ironwood(i) for i in range(5)])
    assert not any(p.subject == "fitness" for p in e.mine(T0))        # still "other": no habit claimed
    persona = generate_persona(T0)
    e.observe(persona.observations + persona.upcoming)
    e.mine(T0)
    nudges = e.generate_nudges(T0)
    assert nudges and all(not n.body.startswith("[") for n in nudges)
    e.close()


# ------------------------------------------------------------------ profile

@pytest.mark.parametrize("cap", [0, 40, 60, 120, 300, 700, 2000])
def test_profile_card_respects_byte_cap_on_whole_lines(eng, cap):
    persona = generate_persona(T0)
    eng.observe(persona.observations + persona.upcoming)
    pats = eng.mine(T0)
    card = eng.profile_card(T0, max_bytes=cap)
    assert len(card.encode("utf-8")) <= cap
    if not card:
        assert cap < 60
        return
    lines = card.split("\n")
    assert lines[0] in ("About you (learned from your behavior):", "Today:") and len(lines) >= 2
    statements = {p.statement for p in pats}
    section = None
    for line in lines:
        if line in ("About you (learned from your behavior):", "Today:"):
            section = "about" if line.startswith("About") else "today"
            continue
        assert line.startswith("- ")
        if section == "about":
            assert line[2:].removesuffix(" (you confirmed)") in statements   # never a cut-off sentence
    if cap >= 700:
        assert "Today:" in lines and "work out" in card and "Dev Malik" in card


def test_describe_filters_by_subject(eng):
    persona = generate_persona(T0)
    eng.observe(persona.observations + persona.upcoming)
    eng.mine(T0)
    d = eng.describe("dev malik", now=T0)
    assert d["patterns"] and all("dev malik" in (p["subject"] + p["statement"]).lower() for p in d["patterns"])
    assert d["summary"].startswith("From what you do: ")
    assert eng.describe("zebra", now=T0)["summary"] == "I haven't noticed a pattern about zebra yet."


def test_verdict_follows_a_pattern_whose_window_drifted(tmp_path):
    from service.behavior.store import Store
    from service.behavior.types import Pattern, Window
    s = Store(tmp_path / "s.db")
    old = Pattern(id="old", kind="habit", subject="fitness", statement="a", window=Window((0, 2, 4), 405, 465))
    other = Pattern(id="other", kind="habit", subject="focus", statement="f")
    s.replace_patterns([old, other], T0)
    s.set_pattern_status("old", "confirmed")
    s.set_pattern_status("other", "rejected")
    new = Pattern(id="new", kind="habit", subject="fitness", statement="b", window=Window((0, 2, 3, 4), 405, 465))
    out = {p.id: p.status for p in s.replace_patterns([new], T0 + D)}
    assert out == {"new": "confirmed"}
    assert {p.id: p.status for p in s.patterns()} == {"new": "confirmed", "other": "rejected"}
    s.close()


# ------------------------------------------------- review regressions (repair pass)

def test_subsecond_id_drift_is_neither_a_move_nor_a_cancel(eng):
    """Finding 5: ids embed int(ts); 0.0002s of drift across an integer boundary
    renamed the event and was reported as a move."""
    t = T0 + D - 0.0001
    snap(eng, [ev(f"cal:x:{int(t)}", t, sid="x")], T0)
    t2 = T0 + D + 0.0001
    out = snap(eng, [ev(f"cal:x:{int(t2)}", t2, sid="x")], T0 + 60)
    assert out == {"upserted": 1, "cancelled": 0, "moved": 0}
    assert not kinds(eng, "event_moved") and not kinds(eng, "event_cancelled")
    assert set(kinds(eng, "event")) == {f"cal:x:{int(t2)}"}
    # Same id, 59s later: same event; 60s: a move.
    snap(eng, [ev("stable", T0 + 2 * D)], T0 + 120)
    assert snap(eng, [ev("stable", T0 + 2 * D + 59)], T0 + 180)["moved"] == 0
    assert snap(eng, [ev("stable", T0 + 2 * D + 120)], T0 + 240)["moved"] == 1


def test_reappearing_event_clears_its_move_row_too(eng):
    """Finding 5: moved away and back -> the original id's move: row goes, like cancel:."""
    snap(eng, [ev("cal:m:1", T0 + D, sid="m")], T0)
    assert snap(eng, [ev("cal:m:2", T0 + D + H, sid="m")], T0 + 10)["moved"] == 1
    assert "move:cal:m:1" in kinds(eng, "event_moved")
    snap(eng, [ev("cal:m:1", T0 + D, sid="m")], T0 + 20)
    moved = kinds(eng, "event_moved")
    assert "move:cal:m:1" not in moved and "move:cal:m:2" in moved     # only the move back remains
    assert set(kinds(eng, "event")) == {"cal:m:1"}


class _CountingPhraser:
    def __init__(self, clock=None, cost=0.0):
        self.calls, self.clock, self.cost = 0, clock, cost

    def phrase(self, kind, facts):
        self.calls += 1
        if self.clock is not None:
            self.clock[0] += self.cost
        return f"phrased {facts['title']}"

    def categorize(self, titles, categories):
        return {}


def _three_nudge_engine(tmp_path, phraser):
    sun = datetime(2026, 10, 11, 13, 0, tzinfo=TZ).timestamp()
    p = generate_persona(sun)
    e = BehaviorEngine(tmp_path / "ph.db", tz="America/Los_Angeles", phraser=phraser)
    drop = [ev("cal:drop-gym:1", datetime(2026, 10, 12, 7, 0, tzinfo=TZ).timestamp(),
               "Foundry export check-in", dur=45 * 60)]
    for i, title in enumerate(("Pipeline review", "Security packet sync", "Hiring debrief", "Board deck prep")):
        drop.append(ev(f"cal:b2b{i}", datetime(2026, 10, 12, 14, 0, tzinfo=TZ).timestamp() + i * 1800, title))
    e.observe(p.observations + p.upcoming + drop)
    return e, sun


def test_phrasing_is_capped_per_generate_pass(tmp_path):
    """Finding 6: at most PHRASE_MAX_PER_PASS model calls per pass; the rest keep templates."""
    from service.behavior import engine as engine_mod
    ph = _CountingPhraser()
    e, sun = _three_nudge_engine(tmp_path, ph)
    made = e.generate_nudges(sun)
    assert len(made) >= 3 and ph.calls == engine_mod.PHRASE_MAX_PER_PASS == 2
    assert sum(n.body.startswith("phrased ") for n in made) == 2
    e.close()


def test_phrasing_stops_after_the_wall_time_budget(tmp_path, monkeypatch):
    from service.behavior import engine as engine_mod
    clock = [1000.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    ph = _CountingPhraser(clock, cost=engine_mod.PHRASE_BUDGET_S + 1)    # one slow call eats it all
    e, sun = _three_nudge_engine(tmp_path, ph)
    made = e.generate_nudges(sun)
    assert len(made) >= 3 and ph.calls == 1
    e.close()


def test_mine_prunes_old_observations_and_nudges(tmp_path):
    e = BehaviorEngine(tmp_path / "pr.db", tz="America/Los_Angeles")
    e.observe([ev("old", T0 - 181 * D), ev("keep", T0 - 179 * D)])
    e._store.insert_nudge(Nudge(id="n_old", kind="day_preview", title="t", body="b", because="c",
                                created_at=T0 - 31 * D))
    e._store.insert_nudge(Nudge(id="n_new", kind="day_preview", title="t", body="b", because="c",
                                created_at=T0 - 29 * D))
    e.mine(T0)
    assert set(kinds(e, "event")) == {"keep"}
    assert not e._store.has_nudge("n_old") and e._store.has_nudge("n_new")
    assert e.status()["nudge_cursor"] == 2                             # cursor never goes back
    e.close()


def test_newer_schema_db_is_moved_aside_not_crashed_on(tmp_path):
    from service.behavior.store import SCHEMA_VERSION, Store
    s = Store(tmp_path / "v.db")
    s.meta_set("schema_version", SCHEMA_VERSION + 1)
    s.upsert_observations([ev("x", T0)])
    s.close()
    s2 = Store(tmp_path / "v.db")
    assert s2.meta_get("schema_version") == SCHEMA_VERSION and s2.count_by_kind() == {}
    assert list(tmp_path.glob(f"v.db.schema-{SCHEMA_VERSION + 1}-*.bak"))
    s2.close()
    s3 = Store(tmp_path / "v.db")                                      # reopening is a no-op
    assert s3.meta_get("schema_version") == SCHEMA_VERSION
    s3.close()


@pytest.mark.parametrize("word", ["gym", "workout", "work out", "exercise", "fitness", "Lifting"])
def test_describe_matches_category_synonyms(eng, word):
    persona = generate_persona(T0)
    eng.observe(persona.observations + persona.upcoming)
    eng.mine(T0)
    d = eng.describe(word, now=T0)
    assert any("work out" in p["statement"] for p in d["patterns"]), word
    assert not any("Dev Malik" in p["statement"] for p in d["patterns"])


def test_low_confidence_patterns_stay_out_of_the_profile_card(tmp_path):
    from service.behavior import profile as profile_mod
    from service.behavior.types import Pattern
    weak = Pattern(id="w", kind="contact_rhythm", subject="Sam", statement="You talk to Sam a bit",
                   confidence=profile_mod.MIN_CONFIDENCE - 0.01)
    strong = Pattern(id="s", kind="habit", subject="fitness", statement="You work out", confidence=0.8)
    card = profile_mod.profile_card([weak, strong], [], T0, TZ)
    assert "You work out" in card and "Sam" not in card


def test_profile_leads_with_personal_habits_and_people_before_meetings(eng):
    """The card should make the user feel known: the gym and Dev come before a
    recurring "Team lunch", which comes after cancel habits, day shape and Wisp use."""
    persona = generate_persona(T0)
    eng.observe(persona.observations + persona.upcoming)
    eng.mine(T0)
    card = eng.profile_card(T0, max_bytes=4000)
    gym, dev, lunch = card.index("work out"), card.index("Dev Malik"), card.index("Team lunch")
    assert gym < dev < lunch
    assert card.index("back-to-back") < lunch
    statements = [p["statement"] for p in eng.describe()["patterns"]]
    first = lambda word: next(i for i, s in enumerate(statements) if word in s)   # noqa: E731
    assert first("work out") < first("Dev Malik") < first("Team lunch")
    # At the default cap the personal lines still lead.
    about = eng.profile_card(T0).split("\n")[1:3]
    assert "work out" in about[0] and "Dev Malik" in about[1]
