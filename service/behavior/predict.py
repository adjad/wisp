"""Predictions for the next horizon, from mined patterns + upcoming calendar events.

Five kinds (``types.PREDICTION_KINDS``):

* ``activity``: a personal habit expected to happen (gym Wed 6:45).
* ``contact``: the next window in which the user usually talks to someone.
* ``cancel_risk``: an upcoming event that falls in a bucket the user tends to
  cancel (e.g. the last of a 3+ back-to-back run); probability = that bucket's
  historical cancel rate.
* ``habit_conflict``: an upcoming event booked over an expected habit occurrence.
* ``usage``: the next window in which the user usually asks Wisp about a topic
  (email around 1:35pm). Only named topics; the catch-all "general" is skipped.

Every prediction carries a ``because`` composed here from counts ("20 of the
last 24 Mon/Wed/Fri"), never by a model, so the evidence line cannot drift from
the statistics.
"""
from __future__ import annotations

from datetime import timedelta, tzinfo

from service.behavior import miners
from service.behavior.timeutil import DAY_NAMES, at_minute, fmt_clock, fmt_range, local_dt
from service.behavior.types import Observation, Pattern, Prediction, Window

ACTIVITY_MIN_CONF = 0.3
CONTACT_MIN_CONF = 0.3
CONFLICT_MIN_CONF = 0.4
CONFLICT_MIN_OVERLAP_S = 10 * 60
# Lower than the habit/contact bars: a usage window is a light hint about when
# the user turns to Wisp, never an action, and Wilson bounds on a few weeks of
# asks sit around 0.2-0.4.
USAGE_MIN_CONF = 0.2
USAGE_SKIP_TOPICS = frozenset({"", "general"})
_USAGE_PHRASES = {"brief": "your brief", "calendar": "checking your calendar",
                  "reminders": "your reminders", "email": "checking email",
                  "messages": "your messages", "weather": "checking the weather", "notes": "your notes",
                  "news": "the news", "music": "music", "web": "a web lookup"}


def usage_phrase(topic: str) -> str:
    """Topic label -> a phrase that reads before "with Wisp": "email" -> "checking email"."""
    return _USAGE_PHRASES.get(topic, f"asking about {topic}")


def occurrences(window: Window, start: float, end: float, tz: tzinfo, *,
                include_ongoing: bool = False) -> list[tuple[float, float]]:
    """Local window instances whose start lies in (start, end] (or ongoing at ``start``)."""
    out = []
    day = local_dt(start, tz).date()
    last = local_dt(end, tz).date()
    while day <= last:
        if not window.weekdays or day.weekday() in window.weekdays:
            s, e = at_minute(day, window.start_minute, tz), at_minute(day, window.end_minute, tz)
            if start < s <= end or (include_ongoing and s <= start < e):
                out.append((s, e))
        day += timedelta(days=1)
    return out


def _clock(ts: float, tz: tzinfo) -> str:
    dt = local_dt(ts, tz)
    return fmt_clock(dt.hour * 60 + dt.minute)


def _end(o: Observation) -> float:
    return o.end_ts if o.end_ts and o.end_ts > o.ts else o.ts + 30 * 60


def habit_evidence(p: Pattern) -> str:
    """"20 of the last 24 Mon/Wed/Fri (6:45–7:45am)"."""
    days = p.stats.get("days") or "days"
    return (f"{p.support} of the last {p.opportunities} {days} "
            f"({fmt_range(p.window.start_minute, p.window.end_minute)})")


def activity_probability(p: Pattern, now: float) -> float:
    """Confidence, pulled toward the last four weeks' adherence and decayed when
    the habit has not been seen for three weeks (people change routines)."""
    rate = p.support / p.opportunities if p.opportunities else 0.0
    r_opp = p.stats.get("recent_opportunities") or 0
    recent = (p.stats.get("recent_support") or 0) / r_opp if r_opp else rate
    prob = p.confidence * (0.5 + 0.5 * (recent / rate)) if rate else 0.0
    if p.last_seen and now - p.last_seen > 21 * 86400:
        prob *= 0.5
    return round(max(0.0, min(1.0, prob)), 2)


def personal_habits(patterns: list[Pattern], min_conf: float) -> list[Pattern]:
    return [p for p in patterns if p.kind == "habit" and p.status != "rejected"
            and p.stats.get("key_type") == "category" and p.confidence >= min_conf]


def predict(patterns: list[Pattern], events: list[Observation], now: float, horizon_s: float,
            tz: tzinfo) -> list[Prediction]:
    """``events``: calendar ``event`` observations covering today and the horizon
    (earlier ones today are needed to place upcoming events in their runs)."""
    live = [p for p in patterns if p.status != "rejected"]
    end = now + horizon_s
    upcoming = [e for e in events if e.kind == "event" and now < e.ts <= end and not miners.is_all_day(e)]
    out: list[Prediction] = []
    out += _activities(live, upcoming, now, end, tz)
    out += _contacts(live, now, end, tz)
    out += _cancel_risks(live, events, now, end, tz)
    out += _conflicts(live, upcoming, now, end, tz)
    out += _usage(live, now, end, tz)
    out.sort(key=lambda p: (p.ts, p.kind, p.subject))
    return out


def _activities(live, upcoming, now, end, tz) -> list[Prediction]:
    out = []
    for p in live:
        if p.kind != "habit" or p.stats.get("key_type") == "subject" or p.confidence < ACTIVITY_MIN_CONF:
            continue
        label = p.stats.get("label") or p.subject
        for s, e in occurrences(p.window, now, end, tz):
            on_cal = [ev.id for ev in upcoming if miners.matches_habit(ev, p)
                      and ev.ts < e and _end(ev) > s]
            because = f"{label} on {habit_evidence(p)}" + ("; it's on your calendar" if on_cal else "")
            out.append(Prediction(kind="activity", subject=label, ts=s, end_ts=e,
                                  probability=activity_probability(p, now), because=because,
                                  pattern_id=p.id, refs=tuple(on_cal)))
    return out


def _contacts(live, now, end, tz) -> list[Prediction]:
    out = []
    for p in live:
        if p.kind != "contact_rhythm" or p.confidence < CONTACT_MIN_CONF:
            continue
        occ = occurrences(p.window, now, end, tz, include_ongoing=True)
        if not occ:
            continue
        s, e = occ[0]
        part = p.stats.get("daypart", "")
        because = (f"You were in touch with {p.subject} on {p.support} of the last {p.opportunities} "
                   f"{p.stats.get('days', 'days')} ({part}s, "
                   f"{fmt_range(p.window.start_minute, p.window.end_minute)})")
        out.append(Prediction(kind="contact", subject=p.subject, ts=s, end_ts=e,
                              probability=round(p.confidence, 2), because=because, pattern_id=p.id))
    return out


def _cancel_risks(live, events, now, end, tz) -> list[Prediction]:
    buckets = {p.stats.get("bucket"): p for p in live
               if p.kind == "cancel_propensity" and p.stats.get("action") == "cancel"}
    if not buckets:
        return []
    # Runs are built from EVERY event passed in (the engine bounds them to the
    # end of the horizon's last local day), not only those before ``end``:
    # cutting at ``end`` would split a run at the horizon edge and flag a middle
    # meeting as its "last". Only events in (now, end] can be flagged.
    timed = [e for e in events if e.kind == "event" and not miners.is_all_day(e)]
    slots = miners.assign_features(miners.build_slots(timed, [], [], tz))
    best: dict = {}   # one cancel_risk per back-to-back run (key: slot.run)
    for s in slots:
        o = s.loc.obs
        if not (now < o.ts <= end):
            continue
        matches = [buckets[b] for b in s.buckets if b in buckets]
        if not matches:
            continue
        p = max(matches, key=lambda m: (m.stats.get("rate", 0), m.support))
        because = (f"You cancelled {p.support} of the last {p.opportunities} "
                   f"{miners.bucket_phrase(p.stats['bucket'])}")
        pred = Prediction(kind="cancel_risk", subject=o.subject, ts=o.ts, end_ts=_end(o),
                          probability=round(float(p.stats.get("rate", 0)), 2), because=because,
                          pattern_id=p.id, refs=(o.id,))
        key = s.run if s.run is not None else ("event", o.id)
        rank = (pred.probability, s.position == "last", o.ts)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, pred)
    return [pred for _, pred in best.values()]


def _conflicts(live, upcoming, now, end, tz) -> list[Prediction]:
    protection = {p.stats.get("habit_pattern_id"): p for p in live if p.kind == "habit_protection"}
    out = []
    for h in personal_habits(live, CONFLICT_MIN_CONF):
        label = h.stats.get("label") or h.subject
        noun = miners.habit_noun(h)
        for s, e in occurrences(h.window, now - 3600 * 24, end, tz):
            for ev in upcoming:
                if miners.matches_habit(ev, h) or ev.category == h.stats.get("key"):
                    continue
                overlap = min(_end(ev), e) - max(ev.ts, s)
                if overlap < min(CONFLICT_MIN_OVERLAP_S, _end(ev) - ev.ts):
                    continue
                prot = protection.get(h.id)
                day = DAY_NAMES[local_dt(s, tz).weekday()]
                because = (f"{ev.subject} overlaps your usual {noun} on {day} "
                           f"({label} on {habit_evidence(h)})")
                if prot is not None and prot.stats.get("collisions"):
                    because += (f"; you moved or cancelled {prot.support} of the last "
                                f"{prot.opportunities} things booked over it")
                    prob = prot.stats.get("rate", h.confidence)
                else:
                    prob = h.confidence
                out.append(Prediction(kind="habit_conflict", subject=ev.subject, ts=ev.ts,
                                      end_ts=_end(ev), probability=round(float(prob), 2),
                                      because=because, pattern_id=h.id, refs=(ev.id,)))
    return out


def _usage(live, now, end, tz) -> list[Prediction]:
    out = []
    for p in live:
        topic = p.stats.get("key") or p.subject
        if p.kind != "wisp_usage" or p.confidence < USAGE_MIN_CONF or topic in USAGE_SKIP_TOPICS:
            continue
        occ = occurrences(p.window, now, end, tz)
        if not occ:
            continue
        around = p.stats.get("around_minute")
        s, e = occ[0]
        if around is not None and s <= at_minute(local_dt(s, tz).date(), int(around), tz) <= e:
            ts = at_minute(local_dt(s, tz).date(), int(around), tz)
        else:
            ts = s
        because = (f"You asked Wisp about {topic} on {p.support} of the last {p.opportunities} "
                   f"{p.stats.get('days', 'days')} ({fmt_range(p.window.start_minute, p.window.end_minute)})")
        out.append(Prediction(kind="usage", subject=topic, ts=ts, end_ts=e,
                              probability=round(p.confidence, 2), because=because, pattern_id=p.id))
    return out
