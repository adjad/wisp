"""Pattern miners: pure functions from an observation timeline to ``Pattern``s.

No I/O and no model. Each miner turns counts into a claim only when the counts
can carry it (minimum support, minimum spread across weeks, minimum lift), and
reports its confidence as the Wilson lower bound of support/opportunities (one-
sided z=1.28, i.e. ~90%), so "4 of 4" is not treated as more certain than
"40 of 44". Every pattern keeps <= 5 evidence observation ids and the raw counts
in ``stats`` so a statement can always be traced back to what the user did.

All bucketing is in local wall-clock time (see ``timeutil``).

Pattern ids are ``sha256(kind|subject|window_key)[:16]``. The window key is
deliberately coarse (weekday set + daypart for habits/contacts, empty for the
rest) so that one more week of data nudging a window by 15 minutes does not
mint a new id and silently drop the user's confirmed/rejected verdict.
"""
from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Iterable

from service.behavior.timeutil import (
    DAY_ABBR, DAY_NAMES, WEEKDAYS, WEEKEND, count_weekdays, daypart, days_word, fmt_clock,
    fmt_range, local_dt, minute_of, at_minute, on_days, percentile, round_to, when_phrase,
)
from service.behavior.types import Observation, Pattern, Window

Z = 1.28                      # one-sided ~90% Wilson bound
BIN_MIN = 30                  # habit clustering bin
HABIT_MIN_SUPPORT = 3        # 3 is enough over >= 3 separate weeks: a weekly class shows up after three
HABIT_MIN_WEEKS = 3
USAGE_MIN_SUPPORT = 4        # Wisp asks are cheap and bursty, so usage keeps the stricter bar
WEEKDAY_MIN_RATE = 0.4        # a weekday joins a habit window when it happens >= 40% of those days
MAX_WINDOW_MIN = 240          # wider than this is "sometime during the day", not a habit
CONTACT_MIN_INTERACTIONS = 8
CONTACT_MIN_WEEKS = 2
CONTACT_DAYPART_SHARE = 0.5
B2B_GAP_S = 10 * 60           # adjacent = next starts <= 10 min after the previous ends
B2B_OVERLAP_S = 5 * 60        # ...and no more than 5 min before it ends
CANCEL_MIN_OPPS = 4
CANCEL_MIN_RATE = 0.4
CANCEL_MIN_LIFT = 1.5
PROTECTION_MIN_COLLISIONS = 3
RECENT_DAYS = 28
LONG_SLOT_S = 4 * 3600        # all-day-ish blocks (offsites, travel days) are not "meetings"

# Categories that describe the user's own routine. Habits keyed on these are
# "who you are" (gym, focus, lunch); everything else is clustered by title,
# because "meeting" at 2pm on Tuesday says nothing until we know which one.
PERSONAL_CATEGORIES = frozenset({"fitness", "focus", "meal", "class", "health", "social",
                                 "personal", "errand", "travel"})
CONVERSATION_CATEGORIES = frozenset({"call", "one_on_one"})


# --------------------------------------------------------------------- basics

def wilson_lower(k: int, n: int, z: float = Z) -> float:
    if n <= 0:
        return 0.0
    p = min(1.0, k / n)
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(max(0.0, (centre - margin) / denom), 4)


def pattern_id(kind: str, subject: str, window_key: str = "") -> str:
    return hashlib.sha256(f"{kind}|{subject}|{window_key}".encode()).hexdigest()[:16]


def window_key(weekdays, start_minute: int) -> str:
    return ",".join(str(w) for w in sorted(weekdays)) + "|" + daypart(start_minute)


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATEISH = re.compile(
    rf"\b{_MONTHS}\s*\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?\b|\b\d{{1,2}}\s+{_MONTHS}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\bweek\s+of\b|\bw\d{1,2}\b", re.I)


def clean_title(title: str) -> str:
    """Drop dates so "Weekly priorities — week of Oct 5" groups with next week's."""
    text = _DATEISH.sub(" ", title or "")
    return re.sub(r"[\s\-–—:,(]+$", "", re.sub(r"\s+", " ", text)).strip() or (title or "").strip()


def normalize_subject(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_title(title).lower()).strip()


def is_all_day(o: Observation) -> bool:
    if o.attrs.get("all_day"):
        return True
    return o.end_ts is not None and o.end_ts - o.ts >= 20 * 3600


@dataclass(frozen=True)
class Local:
    """An observation positioned on the local clock."""
    obs: Observation
    start: float
    end: float
    day: date
    wd: int
    minute: int
    end_minute: int          # minutes after the same local midnight (clamped to 1440)

    @property
    def id(self) -> str:
        return self.obs.id


def localize(o: Observation, tz: tzinfo, *, start: float | None = None, end: float | None = None,
             default_s: float = 30 * 60) -> Local:
    start = float(o.ts if start is None else start)
    if end is None:
        end = o.end_ts if o.end_ts is not None and o.end_ts > start else start + default_s
    dt = local_dt(start, tz)
    m = minute_of(dt)
    return Local(o, start, float(end), dt.date(), dt.weekday(), m,
                 min(24 * 60, m + max(1, int(round((end - start) / 60)))))


def habit_key(o: Observation) -> tuple[str, str]:
    """(key_type, key) under which an observation counts toward a habit."""
    if o.kind == "note":
        return ("note", normalize_subject(o.subject))
    if o.kind.startswith("reminder"):
        return ("reminder", normalize_subject(o.subject))
    if o.category in PERSONAL_CATEGORIES:
        return ("category", o.category)
    return ("subject", normalize_subject(o.subject))


def matches_habit(o: Observation, p: Pattern) -> bool:
    return habit_key(o) == (p.stats.get("key_type"), p.stats.get("key"))


def next_occurrence(window: Window, now: float, tz: tzinfo, *, days: int = 8,
                    include_ongoing: bool = False) -> tuple[float, float] | None:
    today = local_dt(now, tz).date()
    for i in range(days):
        d = today + timedelta(days=i)
        if window.weekdays and d.weekday() not in window.weekdays:
            continue
        s, e = at_minute(d, window.start_minute, tz), at_minute(d, window.end_minute, tz)
        if s > now or (include_ongoing and s <= now < e):
            return s, e
    return None


def _evidence(items: Iterable[Local]) -> tuple[str, ...]:
    return tuple(i.id for i in sorted(items, key=lambda x: x.start)[-5:])


# ----------------------------------------------------------- window clustering

@dataclass
class Cluster:
    weekdays: tuple[int, ...]
    start_minute: int
    end_minute: int
    members: list[Local]
    support: int
    opportunities: int
    weekday_counts: list[int]
    weekday_opps: list[int]
    bins: dict[str, int]
    weeks: int
    median_minute: int
    recent_support: int
    recent_opportunities: int


def _opps(first: date, now_dt: datetime, end_minute: int, member_days: set[date],
          *, since: date | None = None) -> list[int]:
    """Opportunities per weekday: every day in [first, yesterday], plus today
    when the window is over (or already happened today)."""
    today = now_dt.date()
    lo = max(first, since) if since else first
    counts = count_weekdays(lo, today - timedelta(days=1))
    if lo <= today and (minute_of(now_dt) >= end_minute or today in member_days):
        counts[today.weekday()] += 1
    return counts


def find_windows(items: list[Local], first: date, now_dt: datetime, *, step: int = 15,
                 min_support: int = HABIT_MIN_SUPPORT, min_weeks: int = HABIT_MIN_WEEKS) -> list[Cluster]:
    """Weekday x time windows in which ``items`` recur.

    Start times are binned into 30-minute bins; each contiguous run of
    non-empty bins is a candidate time cluster. Within it, a weekday joins the
    window if it happened on >= 40% of that weekday's days (and at least
    twice), so an occasional Tuesday does not dilute a Mon/Wed/Fri habit. The
    reported time range is the central 80% (p10 start .. p90 end)."""
    hist = Counter(i.minute // BIN_MIN for i in items)
    runs: list[list[int]] = []
    for b in sorted(hist):
        if runs and b == runs[-1][-1] + 1:
            runs[-1].append(b)
        else:
            runs.append([b])
    today = now_dt.date()
    out: list[Cluster] = []
    for run in runs:
        bins = set(run)
        by_day: dict[date, Local] = {}
        for it in sorted((i for i in items if i.minute // BIN_MIN in bins), key=lambda x: x.start):
            by_day.setdefault(it.day, it)
        days = list(by_day.values())
        if len(days) < min_support:
            continue
        counts = [0] * 7
        for d in days:
            counts[d.wd] += 1
        opps = _opps(first, now_dt, max(d.end_minute for d in days), set(by_day))
        chosen = tuple(wd for wd in range(7)
                       if counts[wd] >= 2 and opps[wd] and counts[wd] / opps[wd] >= WEEKDAY_MIN_RATE)
        if not chosen:
            continue
        members = [d for d in days if d.wd in chosen]
        weeks = len({d.day.isocalendar()[:2] for d in members})
        if len(members) < min_support or weeks < min_weeks:
            continue
        starts = sorted(d.minute for d in members)
        ends = sorted(d.end_minute for d in members)
        start = round_to(percentile(starts, 0.1), step)
        end = round_to(percentile(ends, 0.9), step)
        if end <= start:
            end = start + step
        if end - start > MAX_WINDOW_MIN:
            continue
        opps = _opps(first, now_dt, end, set(by_day))
        recent_first = today - timedelta(days=RECENT_DAYS - 1)
        recent_opps = _opps(first, now_dt, end, set(by_day), since=recent_first)
        support = len(members)
        opportunities = max(support, sum(opps[w] for w in chosen))
        out.append(Cluster(
            weekdays=chosen, start_minute=start, end_minute=min(end, 24 * 60), members=members,
            support=support, opportunities=opportunities, weekday_counts=counts,
            weekday_opps=opps, bins={fmt_clock(b * BIN_MIN): hist[b] for b in run}, weeks=weeks,
            median_minute=int(statistics.median(starts)),
            recent_support=sum(1 for d in members if d.day >= recent_first),
            recent_opportunities=sum(recent_opps[w] for w in chosen)))
    return out


# --------------------------------------------------------------------- habits

def _label(members: list[Local]) -> str:
    titles = Counter(clean_title(m.obs.subject) for m in members if m.obs.subject)
    return titles.most_common(1)[0][0] if titles else ""


def habit_verb(key_type: str, key: str, label: str) -> str:
    if key_type == "category":
        return {"fitness": "work out", "focus": "block time for focus", "travel": "travel",
                "errand": f"do {label.lower() or 'errands'}"}.get(key, f"have {label or key}")
    if key_type == "note":
        return f"write “{label}”"
    if key_type == "reminder":
        return f"tick off “{label}”"
    low = label.lower()
    if low.startswith("call "):
        return "call " + label[5:]
    return f"have {label}"


def habit_noun(p: Pattern) -> str:
    """"workout", "focus time", "Morning lift" - what a conflict nudge calls the habit."""
    key = p.stats.get("key")
    if p.stats.get("key_type") == "category":
        return {"fitness": "workout", "focus": "focus time"}.get(key, p.stats.get("label") or key)
    return p.stats.get("label") or p.subject


def mine_habits(items: list[Local], spans: dict[str, date], now_dt: datetime) -> list[Pattern]:
    groups: dict[tuple[str, str], list[Local]] = defaultdict(list)
    for it in items:
        kt, key = habit_key(it.obs)
        if key:
            groups[(kt, key)].append(it)
    found: dict[str, Pattern] = {}
    now = now_dt.timestamp()
    tz = now_dt.tzinfo
    for (kt, key), members in groups.items():
        if len(members) < HABIT_MIN_SUPPORT:
            continue
        # A category spans the whole history. A titled event, note or reminder
        # is judged from its first appearance: a class or office hours that began
        # this term must not count the weeks before it existed as misses.
        own_first = min(m.day for m in members)
        first = own_first if kt != "category" else (spans.get(kt) or own_first)
        for c in find_windows(members, first, now_dt):
            label = _label(c.members)
            subject = key if kt == "category" else label
            window = Window(c.weekdays, c.start_minute, c.end_minute)
            part = daypart(c.start_minute)
            statement = (f"You usually {habit_verb(kt, key, label)} {when_phrase(c.weekdays, part)} "
                         f"({fmt_range(c.start_minute, c.end_minute)})")
            durations = sorted(m.end_minute - m.minute for m in c.members)
            nxt = next_occurrence(window, now, tz)
            pid = pattern_id("habit", f"{kt}:{key}", window_key(c.weekdays, c.start_minute))
            p = Pattern(
                id=pid, kind="habit", subject=subject, statement=statement, window=window,
                support=c.support, opportunities=c.opportunities,
                confidence=wilson_lower(c.support, c.opportunities),
                last_seen=max(m.start for m in c.members), next_expected=nxt[0] if nxt else None,
                evidence=_evidence(c.members),
                stats={"key_type": kt, "key": key, "label": label, "daypart": part,
                       "category": Counter(m.obs.category for m in c.members).most_common(1)[0][0],
                       "weekday_counts": dict(zip(DAY_ABBR, c.weekday_counts)),
                       "weekday_opportunities": dict(zip(DAY_ABBR, c.weekday_opps)),
                       "start_bins": c.bins, "weeks": c.weeks, "median_start": c.median_minute,
                       "median_duration_min": int(statistics.median(durations)),
                       "recent_support": c.recent_support,
                       "recent_opportunities": c.recent_opportunities,
                       "days": days_word(c.weekdays)})
            if pid not in found or found[pid].support < p.support:
                found[pid] = p
    return list(found.values())


# -------------------------------------------------------------- contact rhythm

def _contact_index(names: Iterable[str]) -> list[tuple[str, re.Pattern]]:
    """Regexes that find a contact in an event title: full name, or a first
    name that belongs to only one contact ("Dev / Robin 1:1" -> Dev Malik)."""
    names = sorted({n for n in names if n})
    firsts = Counter(n.split()[0].lower() for n in names)
    index = []
    for n in names:
        alts = [re.escape(n)]
        first = n.split()[0]
        if len(first) >= 3 and firsts[first.lower()] == 1 and first.lower() != n.lower():
            alts.append(re.escape(first))
        index.append((n, re.compile(r"(?<![\w])(?:" + "|".join(alts) + r")(?![\w])", re.I)))
    return index


def contact_mentions(title: str, index: list[tuple[str, re.Pattern]]) -> list[str]:
    return [name for name, rx in index if rx.search(title or "")]


def mine_contacts(messages: list[Local], conversations: list[Local], first: date | None,
                  now_dt: datetime) -> list[Pattern]:
    if not messages and not conversations:
        return []
    index = _contact_index(m.obs.subject for m in messages)
    per: dict[str, dict[str, list[Local]]] = defaultdict(lambda: {"in": [], "out": [], "call": []})
    for m in messages:
        if m.obs.subject:
            per[m.obs.subject]["in" if m.obs.kind == "message_in" else "out"].append(m)
    for ev in conversations:
        for name in contact_mentions(ev.obs.subject, index):
            per[name]["call"].append(ev)
    first = first or min(x.day for x in messages + conversations)
    now = now_dt.timestamp()
    out: list[Pattern] = []
    for name, d in per.items():
        inter = sorted(d["in"] + d["out"] + d["call"], key=lambda x: x.start)
        weeks = {x.day.isocalendar()[:2] for x in inter}
        if len(inter) < CONTACT_MIN_INTERACTIONS or len(weeks) < CONTACT_MIN_WEEKS:
            continue
        parts = Counter(daypart(x.minute) for x in inter)
        part, n_part = parts.most_common(1)[0]
        share = n_part / len(inter)
        if share < CONTACT_DAYPART_SHARE or part == "night":
            continue
        in_part = [x for x in inter if daypart(x.minute) == part]
        day_counts = Counter(x.day for x in in_part)
        wd_days = Counter(dd.weekday() for dd in day_counts)
        top_wd, top_n = wd_days.most_common(1)[0]
        if top_n / sum(wd_days.values()) >= 0.5:
            weekdays: tuple[int, ...] = (top_wd,)
        else:
            weekdays = tuple(w for w in range(7) if wd_days[w] >= max(2, 0.25 * top_n))
            if set(weekdays) <= set(WEEKDAYS) and len(weekdays) >= 4:
                weekdays = WEEKDAYS
        in_window = [x for x in in_part if x.wd in weekdays]
        if not weekdays or not in_window:
            # Spread too thinly over the week for any weekday to qualify: no
            # rhythm. (An empty weekday set would read as "every day" downstream
            # and the empty minute list as a bogus 12–1am window.)
            continue
        minutes = sorted(x.minute for x in in_window)
        start = int(percentile(minutes, 0.1)) // 60 * 60
        ends = sorted(x.end_minute if x.obs.kind == "event" else x.minute + 1 for x in in_window)
        end = min(24 * 60, -(-int(percentile(ends, 0.9)) // 60) * 60)
        if end <= start:
            end = start + 60
        window = Window(weekdays, start, end)
        active = {x.day for x in in_window}
        opps = _opps(first, now_dt, end, active)
        opportunities = max(len(active), sum(opps[w] for w in weekdays))
        n_calls = len(d["call"])
        verb = "call" if n_calls >= 3 and n_calls >= 0.2 * len(inter) else "talk to"
        if len(weekdays) == 1:
            when = when_phrase(weekdays, part)
        else:
            when = when_phrase(weekdays, part, most=True)
        statement = f"You {verb} {name} {when} ({fmt_range(start, end)})"
        all_days = sorted({x.day for x in inter})
        gaps = [(b - a).days for a, b in zip(all_days, all_days[1:])]
        latencies = []
        outs = sorted(x.start for x in d["out"])
        for m in d["in"]:
            nxt = next((t for t in outs if t > m.start), None)
            if nxt is not None and nxt - m.start <= 86400:
                latencies.append(nxt - m.start)
        nxt = next_occurrence(window, now, now_dt.tzinfo)
        hours = [0] * 24
        wds = [0] * 7
        for x in inter:
            hours[x.minute // 60] += 1
            wds[x.wd] += 1
        out.append(Pattern(
            id=pattern_id("contact_rhythm", name, window_key(weekdays, start)),
            kind="contact_rhythm", subject=name, statement=statement, window=window,
            support=len(active), opportunities=opportunities,
            confidence=wilson_lower(len(active), opportunities), last_seen=inter[-1].start,
            next_expected=nxt[0] if nxt else None, evidence=_evidence(in_window),
            stats={"contact": name, "daypart": part, "daypart_share": round(share, 3),
                   "hour_hist": hours, "weekday_hist": dict(zip(DAY_ABBR, wds)),
                   "messages": len(d["in"]) + len(d["out"]), "messages_in": len(d["in"]),
                   "messages_out": len(d["out"]), "calls": n_calls, "verb": verb,
                   "cadence_days": statistics.median(gaps) if gaps else None,
                   "reply_latency_s": round(statistics.median(latencies)) if latencies else None,
                   "last_in_ts": max((x.start for x in d["in"]), default=None),
                   "last_out_ts": max((x.start for x in d["out"]), default=None),
                   "days": days_word(weekdays)}))
    return out


# ------------------------------------------------------------ cancel / moved

@dataclass
class Slot:
    """A calendar slot as it was originally planned, with what became of it."""
    loc: Local
    outcome: str                      # kept | cancelled | moved
    category: str
    organizer_self: bool | None = None
    buckets: tuple[str, ...] = ()
    run: tuple[date, int] | None = None
    run_len: int = 1
    position: str = "single"


def _organizer_self(o: Observation) -> bool | None:
    for key in ("organizer_is_user", "organizer_self", "is_organizer"):
        if key in o.attrs:
            return bool(o.attrs[key])
    return None


def build_slots(events: Iterable[Observation], cancelled: Iterable[Observation],
                moved: Iterable[Observation], tz: tzinfo) -> list[Slot]:
    """Kept events at their time; cancelled/moved ones at their ORIGINAL time
    (attrs start_ts/from_ts), because that is the run they would have been in."""
    slots: list[Slot] = []
    for o in events:
        if not is_all_day(o) and (o.end_ts is None or o.end_ts - o.ts < LONG_SLOT_S):
            slots.append(Slot(localize(o, tz), "kept", o.category, _organizer_self(o)))
    for o in cancelled:
        s = float(o.attrs.get("start_ts") or o.ts)
        e = o.attrs.get("end_ts") or o.end_ts
        cat = o.category or o.attrs.get("category") or ""
        slots.append(Slot(localize(o, tz, start=s, end=e), "cancelled", cat, _organizer_self(o)))
    for o in moved:
        s = float(o.attrs.get("from_ts") or o.ts)
        e = o.attrs.get("from_end_ts")
        if e is None and o.attrs.get("to_end_ts") and o.attrs.get("to_ts"):
            e = s + float(o.attrs["to_end_ts"]) - float(o.attrs["to_ts"])
        cat = o.category or o.attrs.get("category") or ""
        slots.append(Slot(localize(o, tz, start=s, end=e), "moved", cat, _organizer_self(o)))
    return slots


def assign_features(slots: list[Slot]) -> list[Slot]:
    """Group each day's slots into back-to-back runs and tag feature buckets."""
    by_day: dict[date, list[Slot]] = defaultdict(list)
    for s in slots:
        by_day[s.loc.day].append(s)
    for day, items in by_day.items():
        items.sort(key=lambda s: (s.loc.start, s.loc.end))
        runs: list[list[Slot]] = []
        run_end = None
        for s in items:
            # Adjacent, not double-booked: a slot that starts well before the run
            # ends is a conflict (see habit_protection), not "back to back".
            if runs and -B2B_OVERLAP_S <= s.loc.start - run_end <= B2B_GAP_S:
                runs[-1].append(s)
                run_end = max(run_end, s.loc.end)
            else:
                runs.append([s])
                run_end = s.loc.end
        for ri, run in enumerate(runs):
            n = len(run)
            for i, s in enumerate(run):
                s.run, s.run_len = (day, ri), n
                s.position = "single" if n == 1 else "first" if i == 0 else "last" if i == n - 1 else "middle"
                b = [f"daypart_{daypart(s.loc.minute)}", f"weekday_{DAY_ABBR[s.loc.wd]}"]
                if s.category:
                    b.append(f"category_{s.category}")
                if s.organizer_self is not None:
                    b.append("organizer_self" if s.organizer_self else "organizer_other")
                if n >= 3:
                    b += ["back_to_back_3plus", f"back_to_back_{s.position}"]
                elif n == 2:
                    b.append("back_to_back_pair")
                s.buckets = tuple(b)
    return slots


_BUCKET_PHRASES = {
    "back_to_back_last": "meetings that ended a 3+ back-to-back run",
    "back_to_back_first": "meetings that started a 3+ back-to-back run",
    "back_to_back_middle": "meetings in the middle of a 3+ back-to-back run",
    "back_to_back_3plus": "meetings in 3+ back-to-back runs",
    "back_to_back_pair": "back-to-back pairs",
    "organizer_self": "meetings you organized",
    "organizer_other": "meetings other people organized",
}


def bucket_phrase(bucket: str) -> str:
    if bucket in _BUCKET_PHRASES:
        return _BUCKET_PHRASES[bucket]
    kind, _, value = bucket.partition("_")
    if kind == "daypart":
        return f"{value} events"
    if kind == "weekday":
        return f"{DAY_NAMES[DAY_ABBR.index(value)]} events" if value in DAY_ABBR else f"{value} events"
    if kind == "category":
        return f"{value.replace('_', '-')} events"
    return bucket.replace("_", " ")


def _pct(rate: float) -> int:
    return int(round(rate * 20)) * 5


def mine_cancel_propensity(slots: list[Slot], now: float) -> list[Pattern]:
    past = [s for s in slots if s.loc.start < now]
    if not past:
        return []
    out: list[Pattern] = []
    for action, outcome, verb in (("cancel", "cancelled", "cancel"), ("move", "moved", "move")):
        hits_total = sum(1 for s in past if s.outcome == outcome)
        if not hits_total:
            continue
        baseline = hits_total / len(past)
        buckets: dict[str, list[Slot]] = defaultdict(list)
        for s in past:
            for b in s.buckets:
                buckets[b].append(s)
        for bucket, members in buckets.items():
            hits = [s for s in members if s.outcome == outcome]
            rate = len(hits) / len(members)
            if len(members) < CANCEL_MIN_OPPS or rate < CANCEL_MIN_RATE or rate < CANCEL_MIN_LIFT * baseline:
                continue
            stats = {"action": action, "bucket": bucket, "rate": round(rate, 3),
                     "baseline": round(baseline, 3), "lift": round(rate / baseline, 2),
                     "hits": len(hits), "opportunities": len(members)}
            statement = f"You {verb} about {_pct(rate)}% of {bucket_phrase(bucket)}"
            if bucket.startswith("back_to_back_") and bucket != "back_to_back_pair":
                runs: dict = defaultdict(list)
                for s in past:
                    if s.run_len >= 3:
                        runs[s.run].append(s)
                with_hit = [r for r in runs.values() if any(s.outcome == outcome for s in r)]
                run_hits = [s for r in runs.values() for s in r if s.outcome == outcome]
                last_share = (sum(1 for s in run_hits if s.position == "last") / len(run_hits)
                              if run_hits else 0.0)
                run_rate = len(with_hit) / len(runs) if runs else 0.0
                stats.update(runs=len(runs), runs_with_hit=len(with_hit),
                             run_rate=round(run_rate, 3), last_share=round(last_share, 3))
                if bucket == "back_to_back_last" and last_share >= 0.6:
                    statement = (f"When you have 3+ back-to-back meetings you {verb} one about "
                                 f"{_pct(run_rate)}% of the time — usually the last one")
            hit_wds = Counter(s.loc.wd for s in hits)
            wds = tuple(sorted(w for w, n in hit_wds.items() if n >= 0.2 * len(hits)))
            mins = [s.loc.minute for s in hits]
            window = Window(wds, round_to(min(mins), 15) if mins else 0,
                            round_to(max(s.loc.end_minute for s in hits), 15) if hits else 24 * 60)
            stats["weekday_hits"] = dict(zip(DAY_ABBR, (hit_wds[w] for w in range(7))))
            subject = bucket if action == "cancel" else f"{bucket}:moved"
            out.append(Pattern(
                id=pattern_id("cancel_propensity", subject, ""), kind="cancel_propensity",
                subject=subject, statement=statement, window=window, support=len(hits),
                opportunities=len(members), confidence=wilson_lower(len(hits), len(members)),
                last_seen=max(s.loc.start for s in hits),
                evidence=_evidence(s.loc for s in hits), stats=stats))
    return out


def mine_habit_protection(habits: list[Pattern], slots: list[Slot], now: float) -> list[Pattern]:
    """For each personal habit: what happens to other things booked over it."""
    out: list[Pattern] = []
    for h in habits:
        if h.stats.get("key_type") != "category" or h.confidence < 0.4:
            continue
        w = h.window
        hits = []
        for s in slots:
            if s.loc.start >= now or s.loc.wd not in w.weekdays or s.category == h.stats.get("key"):
                continue
            overlap = min(s.loc.end_minute, w.end_minute) - max(s.loc.minute, w.start_minute)
            if overlap >= min(10, s.loc.end_minute - s.loc.minute):
                hits.append(s)
        if len(hits) < PROTECTION_MIN_COLLISIONS:
            continue
        n = len(hits)
        moved = sum(1 for s in hits if s.outcome == "moved")
        cancelled = sum(1 for s in hits if s.outcome == "cancelled")
        kept = n - moved - cancelled
        protected = moved + cancelled
        noun = habit_noun(h)
        if protected / n >= 0.5:
            verb = "move" if not cancelled else "cancel" if not moved else "move or cancel"
            statement = f"When something lands on your {noun} you {verb} it ({protected} of {n} times)"
        else:
            statement = f"When something lands on your {noun} you usually keep it ({kept} of {n} times)"
        out.append(Pattern(
            id=pattern_id("habit_protection", h.subject, window_key(w.weekdays, w.start_minute)),
            kind="habit_protection", subject=h.subject, statement=statement, window=w,
            support=protected, opportunities=n, confidence=wilson_lower(protected, n),
            last_seen=max(s.loc.start for s in hits), evidence=_evidence(s.loc for s in hits),
            stats={"habit_pattern_id": h.id, "label": h.stats.get("label"), "noun": noun,
                   "collisions": n, "moved": moved, "cancelled": cancelled, "kept": kept,
                   "rate": round(protected / n, 3)}))
    return out


# ------------------------------------------------------------------ day shape

def mine_day_shape(slots: list[Slot], first: date | None, now_dt: datetime) -> list[Pattern]:
    kept = [s for s in slots if s.outcome == "kept" and s.loc.start < now_dt.timestamp()]
    if not kept or first is None:
        return []
    by_day: dict[date, list[Slot]] = defaultdict(list)
    for s in kept:
        by_day[s.loc.day].append(s)
    out = []
    for name, group, min_days in (("weekdays", WEEKDAYS, 5), ("weekends", WEEKEND, 4)):
        days = {d: v for d, v in by_day.items() if d.weekday() in group}
        if len(days) < min_days:
            continue
        firsts = sorted(min(s.loc.minute for s in v) for v in days.values())
        lasts = sorted(max(s.loc.end_minute for s in v) for v in days.values())
        lunch_s, lunch_e = [], []
        for v in days.values():
            busy = sorted((s.loc.minute, s.loc.end_minute) for s in v)
            gap = _largest_gap(busy, 11 * 60, 14 * 60 + 30)
            if gap and gap[1] - gap[0] >= 30:
                lunch_s.append(gap[0])
                lunch_e.append(gap[1])
        start = round_to(statistics.median(firsts), 15)
        end = round_to(statistics.median(lasts), 15)
        opps = _opps(first, now_dt, 24 * 60, set(days))
        opportunities = max(len(days), sum(opps[w] for w in group))
        statement = (f"On {name} your first commitment is usually around {fmt_clock(start)} "
                     f"and your last ends around {fmt_clock(end)}")
        stats = {"days": len(days), "first_start_median": start, "last_end_median": end}
        if len(lunch_s) >= max(3, len(days) // 2):
            ls = round_to(statistics.median(lunch_s), 15)
            le = round_to(statistics.median(lunch_e), 15)
            if le - ls >= 30:
                statement += f"; lunch is usually free {fmt_range(ls, le)}"
                stats.update(lunch_start=ls, lunch_end=le)
        out.append(Pattern(
            id=pattern_id("day_shape", name, ""), kind="day_shape", subject=name,
            statement=statement, window=Window(group, start, max(end, start + 15)),
            support=len(days), opportunities=opportunities,
            confidence=wilson_lower(len(days), opportunities),
            last_seen=max(s.loc.start for v in days.values() for s in v),
            evidence=_evidence(v[0].loc for _, v in sorted(days.items())), stats=stats))
    return out


def _largest_gap(busy: list[tuple[int, int]], lo: int, hi: int) -> tuple[int, int] | None:
    cursor, best = lo, None
    for s, e in busy:
        if e <= cursor:
            continue
        if s >= hi:
            break
        if s > cursor and (best is None or s - cursor > best[1] - best[0]):
            best = (cursor, min(s, hi))
        cursor = max(cursor, e)
    if cursor < hi and (best is None or hi - cursor > best[1] - best[0]):
        best = (cursor, hi)
    return best


# ----------------------------------------------------------------- wisp usage

def mine_wisp_usage(asks: list[Local], first: date | None, now_dt: datetime) -> list[Pattern]:
    groups: dict[str, list[Local]] = defaultdict(list)
    for a in asks:
        key = normalize_subject(a.obs.subject)
        if key:
            groups[key].append(a)
    out = []
    for key, items in groups.items():
        if len(items) < USAGE_MIN_SUPPORT:
            continue
        for c in find_windows(items, first or min(i.day for i in items), now_dt, step=5,
                              min_support=USAGE_MIN_SUPPORT):
            around = round_to(c.median_minute, 5)
            label = _label(c.members)
            if "brief" in key:
                statement = f"You check your brief with Wisp around {fmt_clock(around)} {on_days(c.weekdays)}"
            else:
                statement = f"You ask Wisp about {label} around {fmt_clock(around)} {on_days(c.weekdays)}"
            window = Window(c.weekdays, c.start_minute, c.end_minute)
            nxt = next_occurrence(window, now_dt.timestamp(), now_dt.tzinfo)
            out.append(Pattern(
                id=pattern_id("wisp_usage", key, window_key(c.weekdays, c.start_minute)),
                kind="wisp_usage", subject=label or key, statement=statement, window=window,
                support=c.support, opportunities=c.opportunities,
                confidence=wilson_lower(c.support, c.opportunities),
                last_seen=max(m.start for m in c.members), next_expected=nxt[0] if nxt else None,
                evidence=_evidence(c.members),
                stats={"key": key, "around_minute": around, "weeks": c.weeks,
                       "weekday_counts": dict(zip(DAY_ABBR, c.weekday_counts)),
                       "days": days_word(c.weekdays)}))
    return out


# ------------------------------------------------------------------- driver

def mine_all(observations: Iterable[Observation], now: float, tz: tzinfo) -> list[Pattern]:
    """Run every miner over observations that happened before ``now``.

    Callers pass observations with categories already filled (see
    ``categorize``); upcoming events are ignored here (they feed ``predict``)."""
    now_dt = local_dt(now, tz)
    by_kind: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        if o.ts < now:
            by_kind[o.kind].append(o)

    events = [o for o in by_kind["event"] if not is_all_day(o)]
    slots = assign_features(build_slots(events, by_kind["event_cancelled"], by_kind["event_moved"], tz))
    ev_first = min((s.loc.day for s in slots), default=None)

    habit_items = [localize(o, tz) for o in events]
    habit_items += [localize(o, tz, default_s=15 * 60) for o in by_kind["note"]]
    habit_items += [localize(o, tz, default_s=15 * 60) for o in by_kind["reminder_done"]]
    spans = {"category": ev_first, "subject": ev_first,
             "note": _first_day(by_kind["note"], tz), "reminder": _first_day(by_kind["reminder_done"], tz)}
    habits = mine_habits(habit_items, spans, now_dt)

    messages = [localize(o, tz, default_s=60) for o in by_kind["message_in"] + by_kind["message_out"]]
    conversations = [localize(o, tz) for o in events if o.category in CONVERSATION_CATEGORIES]
    msg_first = _first_day(by_kind["message_in"] + by_kind["message_out"], tz)
    contacts = mine_contacts(messages, conversations, msg_first or ev_first, now_dt)

    asks = [localize(o, tz, default_s=60) for o in by_kind["wisp_ask"]]
    patterns = (habits + contacts + mine_cancel_propensity(slots, now)
                + mine_habit_protection(habits, slots, now)
                + mine_day_shape(slots, ev_first, now_dt)
                + mine_wisp_usage(asks, _first_day(by_kind["wisp_ask"], tz), now_dt))
    patterns.sort(key=lambda p: (p.kind, -p.confidence, p.id))
    return patterns


def _first_day(obs: list[Observation], tz: tzinfo) -> date | None:
    return local_dt(min(o.ts for o in obs), tz).date() if obs else None

