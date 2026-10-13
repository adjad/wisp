"""Predictions and nudges over the fictional demo persona, plus the attention
budget (quiet hours, daily cap, cooldown, dedupe, mute/undo/useful feedback)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from service.behavior import nudges as N
from service.behavior.engine import BehaviorEngine
from tests.fixtures.behavior_persona import generate_persona
from service.behavior.types import Action, Feedback, Nudge, Observation

TZ = ZoneInfo("America/Los_Angeles")
D = 86400.0
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=TZ).timestamp()        # Tuesday 8am


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


@pytest.fixture(scope="module")
def persona():
    return generate_persona(NOW)


@pytest.fixture
def eng(tmp_path, persona):
    e = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles")
    e.observe(persona.observations + persona.upcoming)
    e.mine(NOW)
    yield e
    e.close()


# --------------------------------------------------------------- predictions

def test_predicts_conflict_and_cancel_risk_for_upcoming_week(eng, persona):
    preds = eng.predict(NOW, 24 * 3600)
    by_kind = {}
    for p in preds:
        by_kind.setdefault(p.kind, []).append(p)
    (conflict,) = by_kind["habit_conflict"]
    assert conflict.refs == (persona.expected["conflict_event_id"],)
    assert conflict.probability == 0.8 and "4 of the last 5" in conflict.because
    assert "20 of the last 24 Mon/Wed/Fri" in conflict.because
    (risk,) = by_kind["cancel_risk"]                                  # only the LAST of the run
    assert risk.refs == (persona.expected["b2b_upcoming_last_event_id"],)
    assert 0.4 <= risk.probability <= 0.8
    assert "of the last 16 meetings that ended a 3+ back-to-back run" in risk.because
    gym = [p for p in by_kind["activity"] if p.subject == "Morning lift"]
    assert gym and gym[0].probability > 0.5 and gym[0].refs          # it's on the calendar
    assert any(p.kind == "contact" and p.subject == "Dev Malik" for p in preds)
    assert preds == sorted(preds, key=lambda p: (p.ts, p.kind, p.subject))


def test_rejected_patterns_do_not_drive_predictions(eng):
    prot = next(p for p in eng.patterns(kind="habit_protection"))
    gym = next(p for p in eng.patterns(kind="habit") if p.subject == "fitness")
    eng.set_pattern_status(prot.id, "rejected")
    (conflict,) = [p for p in eng.predict(NOW) if p.kind == "habit_conflict"]
    assert conflict.probability == round(gym.confidence, 2)          # falls back to habit confidence
    eng.set_pattern_status(gym.id, "rejected")
    assert not [p for p in eng.predict(NOW) if p.kind in ("habit_conflict", "activity")
                and p.pattern_id == gym.id]


# -------------------------------------------------------------------- nudges

def test_demo_nudges_fire_with_actions(eng, persona):
    made = eng.generate_nudges(NOW)
    by_kind = {n.kind: n for n in made}
    assert {"habit_conflict", "cancel_likely", "day_preview"} <= set(by_kind)
    hc = by_kind["habit_conflict"]
    remind, draft = hc.actions
    assert remind.type == "add_reminder" and remind.auto
    assert remind.params["title"] == "Gym moved? — Harborline SSO check-in overlaps your usual workout"
    conflict_ts = at(14, 7)
    assert remind.params["when_ts"] == conflict_ts - 12 * 3600       # 12h before (later than now)
    assert draft.type == "open_prompt" and not draft.auto
    assert draft.params["prompt"] == "Draft a note to Tessa Cole asking to move Harborline SSO check-in to after 7:45am"
    assert hc.expires_at == conflict_ts and persona.expected["conflict_event_id"] in hc.evidence
    cl = by_kind["cancel_likely"]
    hold, resched = cl.actions
    assert hold.type == "add_hold" and hold.auto and hold.params["title"] == "Likely skip: Ops standup"
    assert hold.params["when_ts"] == at(13, 15, 45) and hold.params["end_ts"] == at(13, 16, 15)
    assert resched.params["prompt"] == "Draft a reschedule note for Ops standup" and not resched.auto
    assert by_kind["day_preview"].body.startswith("Likely today:")
    assert [n.seq for n in made] == list(range(1, len(made) + 1))
    assert all(0 <= n.priority <= 1 and n.because for n in made)
    event = hc.to_event()
    assert {"type", "nudge_id", "title", "body", "because"} <= set(event) and event["type"] == "nudge"
    # Regenerating the same situation creates nothing new (ids dedupe, cooldowns hold).
    assert eng.generate_nudges(NOW + 600) == []
    assert eng.generate_nudges(NOW + 4 * 3600) == []


def test_reminder_is_due_now_when_event_is_under_12h_away(eng):
    made = eng.generate_nudges(at(13, 20))                            # 11h before the Wed 7am conflict
    (hc,) = [n for n in made if n.kind == "habit_conflict"]
    assert hc.actions[0].params["when_ts"] == at(13, 20)


@pytest.mark.parametrize("when", [at(12, 23, 0), at(13, 6, 30), at(13, 22, 0)])
def test_quiet_hours(eng, when):
    assert eng.generate_nudges(when) == []


def test_habit_missed_suggests_a_hold_in_a_free_slot(tmp_path, persona):
    wed = datetime(2026, 10, 14, tzinfo=TZ).date()
    drop = {persona.expected["conflict_event_id"]} | {
        o.id for o in persona.upcoming
        if o.subject == "Morning lift" and datetime.fromtimestamp(o.ts, TZ).date() == wed}
    e = BehaviorEngine(tmp_path / "b.db", tz="America/Los_Angeles")
    e.observe(persona.observations + [o for o in persona.upcoming if o.id not in drop])
    e.mine(NOW)
    made = e.generate_nudges(at(14, 10, 30))
    (miss,) = [n for n in made if n.kind == "habit_missed"]
    (hold,) = miss.actions
    assert hold.type == "add_hold" and hold.auto and hold.params["title"] == "Morning lift (Wisp suggested)"
    assert hold.params["when_ts"] == at(14, 11) and hold.params["end_ts"] == at(14, 12)   # after the focus block
    assert miss.title == "No workout today?"
    e.close()


def test_contact_window_needs_an_unanswered_message(tmp_path, persona):
    def run(extra, now):
        e = BehaviorEngine(tmp_path / f"c{len(extra)}.db", tz="America/Los_Angeles")
        e.observe(persona.observations + persona.upcoming + extra)
        e.mine(NOW)
        made = [n for n in e.generate_nudges(now) if n.kind == "contact_window"]
        e.close()
        return made

    ask = Observation(id="m:in", kind="message_in", ts=at(13, 12, 10), subject="Dev Malik", source="messages")
    (cw,) = run([ask], at(13, 14, 45))
    assert cw.title == "Reply to Dev Malik?" and "2h ago" in cw.body
    assert cw.actions == (Action("open_prompt", {"prompt": "Draft a message to Dev Malik"},
                                 label="Draft a message to Dev Malik"),)
    assert cw.expires_at == at(13, 17)
    reply = Observation(id="m:out", kind="message_out", ts=at(13, 12, 30), subject="Dev Malik", source="messages")
    assert run([ask, reply], at(13, 14, 45)) == []                    # answered
    assert run([ask], at(13, 13, 0)) == []                            # outside the 2-5pm window


# --------------------------------------------------------------- the budget

def nudge(kind, i, created=NOW, expires=None, prio=0.9, pattern="p1", actions=()):
    return Nudge(id=f"{kind}-{i}", kind=kind, title="t", body="b", because="c", created_at=created,
                 expires_at=expires, priority=prio, pattern_ids=(pattern,), actions=actions)


def select(cands, recent=(), now=NOW + 3 * 3600, fb=None, exists=lambda _id: False):
    return N.select(list(cands), now=now, tz=TZ, recent=list(recent), fb=fb or N.FeedbackState(),
                    exists=exists)


def test_daily_budget_and_free_preview():
    recent = [nudge("habit_conflict", 1), nudge("cancel_likely", 1), nudge("contact_window", 1)]
    cands = [nudge("habit_missed", 2, prio=0.5), nudge("habit_conflict", 2, prio=0.95),
             nudge("day_preview", 2)]
    picked = select(cands, recent, now=NOW + 3.5 * 3600)
    assert [n.kind for n in picked] == ["habit_conflict", "day_preview"]   # 4th slot to the top priority
    yesterday = [nudge("cancel_likely", i, created=NOW - D) for i in range(6)]
    assert len(select([nudge("habit_missed", 9)], yesterday)) == 1         # yesterday's don't count


def test_per_kind_cooldown_and_one_per_kind_per_run():
    assert select([nudge("habit_missed", 2)], [nudge("habit_missed", 1, created=NOW + 2 * 3600)]) == []
    assert len(select([nudge("habit_missed", 2)], [nudge("habit_missed", 1, created=NOW - 60)])) == 1
    two = select([nudge("contact_window", 2), nudge("contact_window", 3, prio=0.95)])
    assert [n.id for n in two] == ["contact_window-3"]


@pytest.mark.parametrize("kind", ["habit_conflict", "cancel_likely"])
def test_event_keyed_kinds_skip_cooldown_but_keep_budget_and_quiet_hours(kind):
    """Regression: a second conflict on another event waited 3h behind the first."""
    recent = [nudge(kind, 1, created=NOW + 2.9 * 3600)]
    assert [n.id for n in select([nudge(kind, 2)], recent)] == [f"{kind}-2"]
    both = select([nudge(kind, 2), nudge(kind, 3, prio=0.95)])
    assert {n.id for n in both} == {f"{kind}-2", f"{kind}-3"}
    full = [nudge("contact_window", i, created=NOW + 60 * i) for i in range(4)]
    assert select([nudge(kind, 9)], full) == []                                   # daily budget
    assert select([nudge(kind, 9)], now=datetime(2026, 10, 13, 22, 30, tzinfo=TZ).timestamp()) == []


def test_expired_duplicate_and_priority():
    assert select([nudge("cancel_likely", 1, expires=NOW)]) == []
    assert select([nudge("cancel_likely", 1)], exists=lambda i: i == "cancel_likely-1") == []
    (n,) = select([nudge("cancel_likely", 1, prio=0.5)])
    assert n.priority == round(0.5 * N.IMPORTANCE["cancel_likely"], 3)


def test_feedback_mutes_after_two_negatives_within_a_week():
    rows = [{"kind": "cancel_likely", "pattern_key": "p1", "verdict": "wrong", "ts": NOW},
            {"kind": "cancel_likely", "pattern_key": "p1", "verdict": "not_now", "ts": NOW + 2 * D}]
    fb = N.feedback_state(rows, NOW + 3 * D)
    assert fb.muted(("cancel_likely", "p1"), NOW + 3 * D)
    assert not fb.muted(("cancel_likely", "p2"), NOW + 3 * D)
    assert select([nudge("cancel_likely", 1)], now=NOW + 3 * D, fb=fb) == []
    assert len(select([nudge("cancel_likely", 1, pattern="p2")], now=NOW + 3 * D, fb=fb)) == 1
    assert not fb.muted(("cancel_likely", "p1"), NOW + 9 * D + 60)     # 7 days after the 2nd
    spread = [dict(rows[0]), dict(rows[1], ts=NOW + 8 * D)]
    assert not N.feedback_state(spread, NOW + 8 * D).muted(("cancel_likely", "p1"), NOW + 8 * D)


def test_undo_turns_auto_actions_into_offers_and_useful_boosts():
    acts = (Action("add_hold", {"title": "x"}, auto=True),)
    fb = N.feedback_state([{"kind": "cancel_likely", "pattern_key": "p1", "verdict": "undo", "ts": NOW}], NOW)
    (n,) = select([nudge("cancel_likely", 1, actions=acts)], fb=fb)
    assert n.actions[0].auto is False
    fb = N.feedback_state([{"kind": "cancel_likely", "pattern_key": "p1", "verdict": "useful", "ts": NOW}], NOW)
    (boosted,) = select([nudge("cancel_likely", 1, prio=0.5)], fb=fb)
    assert boosted.priority > 0.5 * N.IMPORTANCE["cancel_likely"]


def test_engine_feedback_mutes_habit_conflicts(eng, persona):
    (hc,) = [n for n in eng.generate_nudges(NOW) if n.kind == "habit_conflict"]
    eng.feedback(Feedback(hc.id, "wrong", NOW + 60))
    eng.feedback(Feedback(hc.id, "not_now", NOW + 120))
    # Another meeting lands on Friday's workout; Thursday 8am is within 24h of it.
    fri = Observation(id="fri-clash", kind="event", ts=at(16, 7), end_ts=at(16, 7, 30),
                      subject="Board prep with Owen", source="calendar", attrs={"organizer": "Owen Reed"})
    eng.observe([fri])
    assert not [n for n in eng.generate_nudges(at(15, 8)) if n.kind == "habit_conflict"]
    later = Observation(id="later-clash", kind="event", ts=at(14, 7) + 14 * D, end_ts=at(14, 7, 30) + 14 * D,
                        subject="Foundry check-in", source="calendar")
    eng.observe([later])
    made = eng.generate_nudges(at(27, 8))                               # mute expired after 7 days
    assert [n.evidence[0] for n in made if n.kind == "habit_conflict"] == ["later-clash"]


def test_nudge_ids_are_deterministic():
    assert N.nudge_id("cancel_likely", "evt1") == N.nudge_id("cancel_likely", "evt1")
    assert N.nudge_id("cancel_likely", "evt1") != N.nudge_id("habit_conflict", "evt1")
    assert N.nudge_id("day_preview", "2026-10-13").startswith("n_")


def test_free_slot_skips_busy_time():
    day = datetime(2026, 10, 14, tzinfo=TZ).date()
    busy = [Observation(id="b", kind="event", ts=at(14, 11), end_ts=at(14, 13))]
    assert N._free_slot(busy, day, 10 * 60 + 20, 60, TZ) == (at(14, 13), at(14, 14))
    assert N._free_slot([], day, 20 * 60 + 30, 60, TZ) is None          # nothing before 9pm
    assert timedelta(seconds=N.COOLDOWN_S) == timedelta(hours=3)


# ------------------------------------------ Sunday-afternoon regressions (2026-10-11 13:00 PT)
# A late-week run with two planted plans: a "Foundry export check-in" 07:00-07:45
# on the next gym day (Monday), and four 30-minute meetings tomorrow 14:00-16:00.

SUN = datetime(2026, 10, 11, 13, 0, tzinfo=TZ).timestamp()


def ts(day, hour, minute=0, month=10):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp()


def demo_engine(tmp_path, now, name="demo.db"):
    p = generate_persona(now)
    e = BehaviorEngine(tmp_path / name, tz="America/Los_Angeles")
    e.observe(p.observations + p.upcoming)
    e.mine(now - 3600)
    return e, p


def foundry(day=12, month=10):
    s = ts(day, 7, 0, month)
    return Observation(id=f"cal:drop-gym:{int(s)}", kind="event", ts=s, end_ts=s + 45 * 60,
                       subject="Foundry export check-in", source="calendar", source_id="drop-gym")


def backtoback(day=12, month=10):
    out = []
    for i, title in enumerate(("Pipeline review", "Security packet sync", "Hiring debrief", "Board deck prep")):
        s = ts(day, 14, 0, month) + i * 1800
        out.append(Observation(id=f"cal:b2b{i}:{int(s)}", kind="event", ts=s, end_ts=s + 1800,
                               subject=title, source="calendar", source_id=f"b2b{i}"))
    return out


def test_nudge_horizon_runs_through_end_of_tomorrow(tmp_path):
    e, _ = demo_engine(tmp_path, SUN)
    end_monday = datetime(2026, 10, 13, 0, 0, tzinfo=TZ).timestamp()
    assert e.nudge_horizon(SUN) == end_monday - SUN > 24 * 3600
    late = ts(11, 23, 30)
    assert e.nudge_horizon(late) == end_monday - late
    early = ts(11, 0, 5)
    assert e.nudge_horizon(early) == pytest.approx(48 * 3600 - 300)
    e.close()


def test_demo_sunday_1pm_both_presets_fire(tmp_path):
    """Blockers 1+2: warm-up shows the persona's Harborline clash; then the
    gym-conflict drop (same Monday gym slot, 5 minutes later) AND the
    backtoback drop (Monday 14:00-16:00, 25-27h out) each produce a nudge."""
    e, p = demo_engine(tmp_path, SUN)
    warm = e.generate_nudges(ts(11, 12, 25))
    assert [n.kind for n in warm] == ["habit_conflict"] and "Harborline" in warm[0].title
    e.observe([foundry()])
    gym = e.generate_nudges(ts(11, 12, 30))
    assert [(n.kind, n.title) for n in gym] == [
        ("habit_conflict", "Foundry export check-in overlaps your workout")]
    e.observe(backtoback())
    b2b = e.generate_nudges(SUN)
    assert [(n.kind, n.title) for n in b2b] == [("cancel_likely", "You might skip Board deck prep")]
    assert b2b[0].expires_at == ts(12, 15, 30)
    assert any(a.type == "add_hold" and a.auto for a in b2b[0].actions)
    e.close()


def test_demo_presets_fire_without_warmup_in_one_pass(tmp_path):
    e, _ = demo_engine(tmp_path, SUN)
    e.observe([foundry()] + backtoback())
    made = sorted((n.kind, n.title) for n in e.generate_nudges(SUN))
    assert made == [("cancel_likely", "You might skip Board deck prep"),
                    ("habit_conflict", "Foundry export check-in overlaps your workout"),
                    ("habit_conflict", "Harborline SSO check-in overlaps your workout")]
    e.close()


@pytest.mark.parametrize("now", [ts(11, 13, 0), ts(12, 15, 0), ts(11, 21, 45), ts(13, 8, 0),
                                 ts(14, 21, 30), ts(16, 21, 0)])
def test_one_cancel_risk_per_run_at_any_now(tmp_path, now):
    """Finding 3: whatever the now/horizon edge, a back-to-back run yields at
    most one cancel_risk, and it is never a middle meeting called "ended"."""
    e, _ = demo_engine(tmp_path, now)
    day = datetime.fromtimestamp(now, TZ).date()
    nxt = day + timedelta(days=1)
    e.observe(backtoback(nxt.day, nxt.month))
    # Ground truth: runs over the whole local days (independent of any horizon).
    from service.behavior import miners
    evs = [o for o in e._events_for(now, 72 * 3600) if not miners.is_all_day(o)]
    slots = {s.loc.obs.id: s for s in miners.assign_features(miners.build_slots(evs, [], [], TZ))}
    seen_runs = 0
    for horizon in (None, 3600.0, 24 * 3600, 26 * 3600, 26.5 * 3600, 30 * 3600, 48 * 3600):
        preds = [x for x in e.predict(now, horizon) if x.kind == "cancel_risk"]
        runs = [slots[x.refs[0]].run for x in preds]
        assert len(runs) == len(set(runs)), preds                # one per run
        for x in preds:
            if "ended a 3+" in x.because:
                seen_runs += 1
                assert slots[x.refs[0]].position == "last", (horizon, x)
    if now == SUN:
        assert seen_runs      # (on other days the drop can collide with the persona's own run)
    e.close()


def test_run_cut_by_horizon_edge_is_not_flagged_as_last(tmp_path):
    """Finding 3: horizon ending at Monday 15:00 used to cut the 4-meeting run
    to three and flag "Hiring debrief" (a middle meeting) as the last one."""
    e, _ = demo_engine(tmp_path, SUN)
    e.observe(backtoback())
    edge = [x for x in e.predict(SUN, ts(12, 15, 0) - SUN) if x.kind == "cancel_risk"]
    assert not [x for x in edge if x.subject == "Hiring debrief" and "ended a 3+" in x.because]
    full = [x.subject for x in e.predict(SUN) if x.kind == "cancel_risk"]
    assert full == ["Board deck prep"]
    e.close()


def test_monday_3pm_and_late_night_runs(tmp_path):
    """Monday 15:00 (inside the dropped run): only the run's last meeting is
    still a candidate; late Sunday (quiet hours) nothing is sent."""
    e, _ = demo_engine(tmp_path, SUN)
    e.observe([foundry()] + backtoback())
    assert e.generate_nudges(ts(11, 22, 30)) == []                    # quiet hours
    mon = [n for n in e.generate_nudges(ts(12, 15, 0)) if n.kind == "cancel_likely"
           and datetime.fromtimestamp(n.expires_at, TZ).date().day == 12]
    assert [n.title for n in mon] == ["You might skip Board deck prep"]
    e.close()


def test_weak_contact_rhythm_never_nudges():
    """Finding 4: contact_window nudges need confidence >= 0.4."""
    from service.behavior.types import Pattern, Window
    mk = lambda conf: Pattern(id=f"c{conf}", kind="contact_rhythm", subject="Sam", statement="s",
                              window=Window((), 0, 24 * 60), support=2, opportunities=9,
                              confidence=conf, stats={"verb": "talk to"})
    inp = lambda pats: N.Inputs(now=NOW, tz=TZ, patterns=pats, predictions=[], events={},
                                today_events=[])
    assert N._contact_windows(inp([mk(0.39)])) == []
    assert len(N._contact_windows(inp([mk(0.41)]))) == 1
