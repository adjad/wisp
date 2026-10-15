"""Nudges: turn predictions into a small number of timely, actionable messages.

Two steps, both deterministic:

1. ``build_candidates`` proposes every nudge that would be useful right now,
   each with a deterministic id (kind + key fact: event id, contact + date,
   pattern + date) so regenerating the same situation dedupes.
2. ``select`` applies the user's attention budget: quiet hours 22:00-07:00, at
   most 4 per local day (the morning ``day_preview`` is free), one per kind per
   3 hours, nothing expired, nothing the user muted through feedback.
   Event-keyed kinds (``habit_conflict``, ``cancel_likely``) skip the per-kind
   cooldown: each is about one specific calendar event, dedupes on that event's
   id and expires when it starts, so a second conflict must not wait 3h behind
   the first. They still count toward the daily budget and respect quiet hours.

Auto actions are only reversible types (``add_reminder``, ``add_hold`` - a
tentative Wisp-side hold, never an Apple Calendar change). After the user
undoes an auto action for a kind+pattern, that action is only offered.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import timedelta, tzinfo

from service.behavior import miners, predict
from service.behavior.timeutil import (
    DAY_NAMES, at_minute, day_end, day_start, fmt_clock, fmt_range, local_dt, minute_of, when_phrase,
)
from service.behavior.types import Action, Nudge, Observation, Pattern, Prediction

DAILY_BUDGET = 4
COOLDOWN_S = 3 * 3600
# One nudge per calendar event (id = kind + event id, expires at event start):
# exempt from the per-kind cooldown, not from the daily budget / quiet hours.
EVENT_KEYED_KINDS = frozenset({"habit_conflict", "cancel_likely"})
QUIET_START_MIN = 22 * 60
QUIET_END_MIN = 7 * 60
PREVIEW_END_MIN = 10 * 60
PREVIEW_MAX_USAGE = 2
MUTE_WINDOW_S = 7 * 86400
MUTE_FOR_S = 7 * 86400
FEEDBACK_MEMORY_S = 30 * 86400
CANCEL_NUDGE_MIN_PROB = 0.4
# "This is when you usually talk to X" is only worth an interruption when the
# rhythm is solid; weaker rhythms still show up in predictions/describe().
CONTACT_NUDGE_MIN_CONF = 0.4
UNANSWERED_AFTER_S = 2 * 3600
UNANSWERED_STALE_S = 3 * 86400
MISSED_MIN_CONF = 0.5
LATEST_SUGGESTION_MIN = 21 * 60
NEGATIVE = frozenset({"wrong", "not_now", "undo"})
IMPORTANCE = {"habit_conflict": 1.0, "cancel_likely": 0.85, "habit_missed": 0.7,
              "contact_window": 0.6, "day_preview": 0.5}


def nudge_id(kind: str, key: str) -> str:
    return "n_" + hashlib.sha256(f"{kind}|{key}".encode()).hexdigest()[:16]


def pattern_key(n: Nudge) -> str:
    """Feedback is remembered per (kind, primary pattern)."""
    return n.pattern_ids[0] if n.pattern_ids else ""


@dataclass
class Inputs:
    now: float
    tz: tzinfo
    patterns: list[Pattern]                       # non-rejected
    predictions: list[Prediction]                 # next ~24h
    events: dict[str, Observation]                # calendar events by id (today .. horizon)
    today_events: list[Observation]               # events on today's local date
    last_messages: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)


@dataclass
class FeedbackState:
    muted_until: dict[tuple[str, str], float] = field(default_factory=dict)
    useful: dict[tuple[str, str], int] = field(default_factory=dict)
    auto_off: set[tuple[str, str]] = field(default_factory=set)

    def muted(self, key: tuple[str, str], now: float) -> bool:
        return now < self.muted_until.get(key, 0.0)


def feedback_state(rows: list[dict], now: float) -> FeedbackState:
    """Rows: {kind, pattern_key, verdict, ts}. Two negative verdicts ('wrong',
    'not_now', or 'undo') for one kind+pattern within 7 days mute it for 7 days
    from the second; each 'useful' raises priority; an 'undo' turns that
    kind+pattern's auto actions into offers."""
    st = FeedbackState()
    last_neg: dict[tuple[str, str], float] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        key = (r.get("kind") or "", r.get("pattern_key") or "")
        if now - r["ts"] > FEEDBACK_MEMORY_S:
            continue
        verdict = r.get("verdict")
        if verdict == "useful":
            st.useful[key] = st.useful.get(key, 0) + 1
        if verdict in NEGATIVE:
            prev = last_neg.get(key)
            if prev is not None and r["ts"] - prev <= MUTE_WINDOW_S:
                st.muted_until[key] = max(st.muted_until.get(key, 0.0), r["ts"] + MUTE_FOR_S)
            last_neg[key] = r["ts"]
        if verdict == "undo":
            st.auto_off.add(key)
    return st


def _clock(ts: float, tz: tzinfo) -> str:
    return fmt_clock(minute_of(local_dt(ts, tz)))


def _when(ts: float, now: float, tz: tzinfo) -> str:
    d, today = local_dt(ts, tz).date(), local_dt(now, tz).date()
    if d == today:
        return f"today at {_clock(ts, tz)}"
    if d == today + timedelta(days=1):
        return f"tomorrow at {_clock(ts, tz)}"
    return f"{DAY_NAMES[d.weekday()]} at {_clock(ts, tz)}"


def _end(o: Observation) -> float:
    return o.end_ts if o.end_ts and o.end_ts > o.ts else o.ts + 30 * 60


def _others(ev: Observation | None) -> str:
    """Who to write to about an event: its organizer unless that's the user, else attendees."""
    if ev is None:
        return "the attendees"
    org = ev.attrs.get("organizer")
    user = ev.attrs.get("user_name") or ""
    if org and not ev.attrs.get("organizer_is_user", False) and org != user:
        return str(org)
    people = [a for a in ev.attrs.get("attendees") or [] if a and a not in (org, user)]
    if people:
        return ", ".join(people[:3])
    return "the attendees"


def build_candidates(inp: Inputs) -> list[Nudge]:
    by_id = {p.id: p for p in inp.patterns}
    out: list[Nudge] = []
    out += _habit_conflicts(inp, by_id)
    out += _cancel_likely(inp, by_id)
    out += _contact_windows(inp)
    out += _day_preview(inp)
    out += _habit_missed(inp)
    return out


def _habit_conflicts(inp: Inputs, by_id: dict[str, Pattern]) -> list[Nudge]:
    protection = {p.stats.get("habit_pattern_id"): p for p in inp.patterns if p.kind == "habit_protection"}
    out = []
    for pr in inp.predictions:
        habit = by_id.get(pr.pattern_id)
        if pr.kind != "habit_conflict" or habit is None or not pr.refs:
            continue
        ev = inp.events.get(pr.refs[0])
        noun = miners.habit_noun(habit)
        prefix = "Gym" if habit.stats.get("key") == "fitness" else (habit.stats.get("label") or habit.subject)
        end_min = habit.window.end_minute
        title = f"{pr.subject} overlaps your {noun}"
        body = (f"{pr.subject} ({_when(pr.ts, inp.now, inp.tz)}) lands on time usually scheduled for your {noun} "
                f"({fmt_range(habit.window.start_minute, end_min)}). Whether the activity happens is unknown.")
        remind = Action("add_reminder", {
            "title": f"Review calendar overlap: {pr.subject} / {prefix}",
            "when_ts": max(inp.now, pr.ts - 12 * 3600), "notes": pr.because}, auto=True,
            label="Remind me")
        draft = Action("open_prompt", {
            "prompt": f"Draft a note to {_others(ev)} asking to move {pr.subject} to after {fmt_clock(end_min)}"},
            label="Draft a move request")
        pids = (habit.id,) + ((protection[habit.id].id,) if habit.id in protection else ())
        out.append(Nudge(id=nudge_id("habit_conflict", pr.refs[0]), kind="habit_conflict", title=title,
                         body=body, because=pr.because, created_at=inp.now, expires_at=pr.ts,
                         priority=pr.probability, pattern_ids=pids,
                         evidence=tuple(pr.refs) + tuple(habit.evidence[-2:]), actions=(remind, draft)))
    return out


def _cancel_likely(inp: Inputs, by_id: dict[str, Pattern]) -> list[Nudge]:
    out = []
    for pr in inp.predictions:
        if pr.kind != "cancel_risk" or pr.probability < CANCEL_NUDGE_MIN_PROB or not pr.refs:
            continue
        p = by_id.get(pr.pattern_id)
        hold = Action("add_hold", {"title": f"Schedule may change: {pr.subject}", "when_ts": pr.ts,
                                   "end_ts": pr.end_ts or pr.ts + 1800, "tentative": True,
                                   "notes": pr.because}, auto=True, label="Tentative schedule note")
        draft = Action("open_prompt", {"prompt": f"Draft a reschedule note for {pr.subject}"},
                       label="Draft reschedule note")
        why = f" {p.statement}." if p is not None else ""
        out.append(Nudge(
            id=nudge_id("cancel_likely", pr.refs[0]), kind="cancel_likely",
            title=f"The schedule for {pr.subject} may change",
            body=f"{pr.subject} ({_when(pr.ts, inp.now, inp.tz)}) resembles bookings that were removed or moved on your calendar.{why}",
            because=pr.because, created_at=inp.now, expires_at=pr.ts, priority=pr.probability,
            pattern_ids=(pr.pattern_id,) if pr.pattern_id else (), evidence=tuple(pr.refs) + (
                tuple(p.evidence[-2:]) if p else ()), actions=(hold, draft)))
    return out


def _contact_windows(inp: Inputs) -> list[Nudge]:
    now_dt = local_dt(inp.now, inp.tz)
    m, today = minute_of(now_dt), now_dt.date()
    out = []
    for p in inp.patterns:
        if p.kind != "contact_rhythm" or p.confidence < CONTACT_NUDGE_MIN_CONF:
            continue
        w = p.window
        if (w.weekdays and today.weekday() not in w.weekdays) or not (w.start_minute <= m < w.end_minute):
            continue
        name = p.subject
        last_in, last_out = inp.last_messages.get(name, (None, None))
        rng = fmt_range(w.start_minute, w.end_minute)
        mixed = bool(p.stats.get("calls"))
        has_messages = bool(p.stats.get("messages"))
        source = ("Messages and scheduled calls" if mixed and has_messages else
                  "Scheduled calls" if mixed else "Recorded messages" if has_messages else
                  "Contact timing (source details unavailable)")
        timing = f"{source} with {name} recur in this window ({rng}); attendance is unknown" if mixed else (
            f"Recorded messages with {name} recur in this window ({rng})")
        if p.stats.get("messages") or last_in or last_out:
            if (last_in is None or (last_out is not None and last_out >= last_in)
                    or not (UNANSWERED_AFTER_S < inp.now - last_in < UNANSWERED_STALE_S)):
                continue
            hours = int((inp.now - last_in) // 3600)
            body = f"{name} messaged you {hours}h ago; no later reply is recorded. {timing}."
            because = (f"{source} on {p.support} of the last {p.opportunities} "
                       f"{p.stats.get('days', 'days')} ({rng}); no later recorded reply for {hours}h")
            title = f"Reply to {name}?"
        else:
            body = timing + "."
            because = f"{source} on {p.support} of the last {p.opportunities} {p.stats.get('days', 'days')} ({rng})"
            title = f"Review contact window with {name}?"
        out.append(Nudge(
            id=nudge_id("contact_window", f"{name}|{today.isoformat()}"), kind="contact_window",
            title=title, body=body, because=because, created_at=inp.now,
            expires_at=at_minute(today, w.end_minute, inp.tz), priority=p.confidence,
            pattern_ids=(p.id,), evidence=tuple(p.evidence[-3:]),
            actions=(Action("open_prompt", {"prompt": f"Draft a message to {name}"},
                            label=f"Draft a message to {name}"),)))
    return out


def _preview_item(pr: Prediction, tz: tzinfo, patterns: dict[str, Pattern]) -> str:
    t = _clock(pr.ts, tz)
    if pr.kind == "activity":
        p = patterns.get(pr.pattern_id)
        label = "Expected schedule" if p is None or p.stats.get("basis", "scheduled") == "scheduled" else "Possible routine"
        return f"{label}: {pr.subject} around {t}"
    if pr.kind == "cancel_risk":
        return f"the schedule for {pr.subject} at {t} may change"
    if pr.kind == "habit_conflict":
        return f"{pr.subject} at {t} clashes with a routine"
    if pr.kind == "usage":
        return f"{predict.usage_phrase(pr.subject)} with Wisp around {t}"
    p = patterns.get(pr.pattern_id)
    calls, messages = (p.stats.get("calls"), p.stats.get("messages")) if p else (0, 0)
    source = ("Messages and scheduled calls" if calls and messages else "Scheduled calls" if calls
              else "Recorded message window" if messages else "Contact timing (source details unavailable)")
    return f"{source} with {pr.subject} from {t}"


def _day_preview(inp: Inputs) -> list[Nudge]:
    now_dt = local_dt(inp.now, inp.tz)
    if not (QUIET_END_MIN <= minute_of(now_dt) < PREVIEW_END_MIN):
        return []
    today = now_dt.date()
    # Today's upcoming items; a contact window may already be under way.
    todays = [p for p in inp.predictions if local_dt(p.ts, inp.tz).date() == today
              and (p.ts >= inp.now or p.kind == "contact")]
    if not todays:
        return []
    rank = {"habit_conflict": 0, "cancel_risk": 1, "activity": 2, "contact": 3, "usage": 4}
    ordered = sorted(todays, key=lambda p: (rank.get(p.kind, 9), -p.probability, p.ts))
    # Usage fills the leftover slots only: the likeliest window per topic, at
    # most PREVIEW_MAX_USAGE of them, so "web" three times can't crowd the list.
    seen_topics: set[str] = set()
    picked = []
    for p in ordered:
        if p.kind == "usage":
            if p.subject in seen_topics or len(seen_topics) >= PREVIEW_MAX_USAGE:
                continue
            seen_topics.add(p.subject)
        picked.append(p)
    top = picked[:4]
    top.sort(key=lambda p: p.ts)
    patterns = {p.id: p for p in inp.patterns}
    body = "Likely today: " + "; ".join(_preview_item(p, inp.tz, patterns) for p in top) + "."
    pids = tuple(dict.fromkeys(p.pattern_id for p in top if p.pattern_id))
    return [Nudge(id=nudge_id("day_preview", today.isoformat()), kind="day_preview",
                  title="Your day, as Wisp sees it", body=body,
                  because=f"From {len(pids)} routines learned from your calendar, messages and Wisp use",
                  created_at=inp.now, expires_at=at_minute(today, 12 * 60, inp.tz), priority=1.0,
                  pattern_ids=pids, evidence=tuple(r for p in top for r in p.refs)[:5], actions=())]


def _habit_missed(inp: Inputs) -> list[Nudge]:
    now_dt = local_dt(inp.now, inp.tz)
    m, today = minute_of(now_dt), now_dt.date()
    out = []
    for h in inp.patterns:
        if (h.kind != "habit" or h.stats.get("key_type") != "category" or h.confidence < MISSED_MIN_CONF
                or (h.window.weekdays and today.weekday() not in h.window.weekdays)
                or m < h.window.end_minute or m >= LATEST_SUGGESTION_MIN):
            continue
        if any(miners.matches_habit(e, h) for e in inp.today_events):
            continue
        dur = int(h.stats.get("median_duration_min") or 45)
        slot = _free_slot(inp.today_events, today, max(m, h.window.end_minute), dur, inp.tz)
        if slot is None:
            continue
        label = h.stats.get("label") or h.subject
        noun = miners.habit_noun(h)
        s, e = slot
        verb = miners.habit_verb("category", h.stats.get("key", ""), label)
        out.append(Nudge(
            id=nudge_id("habit_missed", f"{h.id}|{today.isoformat()}"), kind="habit_missed",
            title=f"{label} isn't on your calendar today",
            body=f"Your calendar usually includes {label} {when_phrase(h.window.weekdays, h.stats.get('daypart', 'morning'))}. "
                 f"It isn't on your calendar today; completion is unknown. {_clock(s, inp.tz)} is unbooked.",
            because=f"{label} on {h.support} of the last {h.opportunities} {h.stats.get('days', 'days')}",
            created_at=inp.now, expires_at=s, priority=h.confidence, pattern_ids=(h.id,),
            evidence=tuple(h.evidence[-3:]),
            actions=(Action("add_hold", {"title": f"{label} (Wisp suggested)", "when_ts": s, "end_ts": e,
                                         "tentative": True}, auto=True, label="Hold time"),)))
    return out


def _free_slot(events: list[Observation], day, from_minute: int, dur: int, tz: tzinfo):
    busy = sorted((e.ts, _end(e)) for e in events if not miners.is_all_day(e))
    minute = int(math.ceil(from_minute / 15.0)) * 15
    while minute + dur <= LATEST_SUGGESTION_MIN:
        s = at_minute(day, minute, tz)
        e = s + dur * 60
        if all(e <= bs or s >= be for bs, be in busy):
            return s, e
        minute += 15
    return None


def select(candidates: list[Nudge], *, now: float, tz: tzinfo, recent: list[Nudge],
           fb: FeedbackState, exists) -> list[Nudge]:
    """Apply quiet hours, dedupe, expiry, mutes, the daily budget and per-kind cooldown.

    ``recent``: nudges already created since the start of yesterday;
    ``exists(id)``: whether a nudge id was ever created (dedupe across days)."""
    now_dt = local_dt(now, tz)
    m = minute_of(now_dt)
    if m < QUIET_END_MIN or m >= QUIET_START_MIN:
        return []
    start = day_start(now_dt.date(), tz)
    used = sum(1 for n in recent if start <= n.created_at < day_end(now_dt.date(), tz)
               and n.kind != "day_preview")
    last_by_kind: dict[str, float] = {}
    for n in recent:
        last_by_kind[n.kind] = max(last_by_kind.get(n.kind, 0.0), n.created_at)
    chosen: list[Nudge] = []
    seen: set[str] = set()
    scored = []
    for c in candidates:
        key = (c.kind, pattern_key(c))
        boost = 1.0 + 0.25 * fb.useful.get(key, 0)
        prio = round(max(0.0, min(1.0, c.priority * IMPORTANCE.get(c.kind, 0.5) * boost)), 3)
        if key in fb.auto_off:
            c = replace(c, actions=tuple(replace(a, auto=False) for a in c.actions))
        scored.append(replace(c, priority=prio))
    for c in sorted(scored, key=lambda n: (-n.priority, n.expires_at or float("inf"), n.id)):
        if c.id in seen or exists(c.id):
            continue
        if c.expires_at is not None and c.expires_at <= now:
            continue
        if fb.muted((c.kind, pattern_key(c)), now):
            continue
        if c.kind != "day_preview" and used >= DAILY_BUDGET:
            continue
        if c.kind not in EVENT_KEYED_KINDS and now - last_by_kind.get(c.kind, -1e18) < COOLDOWN_S:
            continue
        seen.add(c.id)
        chosen.append(c)
        last_by_kind[c.kind] = now
        if c.kind != "day_preview":
            used += 1
    return chosen
