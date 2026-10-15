"""The always-on "who you are" card for the system prompt, and describe().

The card is small by contract (default 700 UTF-8 bytes) because it rides along
with every request. It is code-composed: pattern statements and prediction
lines only, so nothing in it is a model's paraphrase of the user. Ranking
favours what the user confirmed, then leads with what makes the user feel
known (personal habits, the people, what they cancel or protect) before the
day's shape, Wisp usage and recurring meetings, and takes the best pattern of
each kind first so one busy calendar of recurring meetings cannot crowd out the
gym, the people and the cancel habits.
"""
from __future__ import annotations

from datetime import tzinfo

from service.behavior import miners
from service.behavior.categorize import categorize
from service.behavior.timeutil import fmt_clock, fmt_range, local_dt, minute_of
from service.behavior.types import CATEGORIES, Pattern, Prediction

HEADER = "About you (learned from your behavior):"
TODAY = "Today:"
MIN_CONFIDENCE = 0.3
# Cancel/protection confidences are Wilson bounds of ~60% rates, structurally
# lower than adherence-style habits, so they get more weight to be seen at all.
_KIND_WEIGHT = {"habit": 1.0, "contact_rhythm": 1.0, "cancel_propensity": 1.3,
                "habit_protection": 1.2, "wisp_usage": 0.8, "day_shape": 0.5}


def _group(p: Pattern) -> str:
    """Diversity groups: each personal habit category is its own group (gym and
    focus both make the first pass); notes/reminders and calendar-derived habits
    are one group each."""
    if p.kind == "habit":
        kt = p.stats.get("key_type")
        if kt == "category":
            return "habit:" + str(p.stats.get("key"))
        return "habit:calendar" if kt == "subject" else "habit:notes"
    return p.kind


# Personal-life habit categories: what the user does for themselves, not what
# their calendar of meetings repeats.
_PERSONAL_CATEGORIES = ("fitness", "health", "personal")
# Lower tier first: personal habits, contact rhythms, cancel/move propensity,
# habit protection, day shape, Wisp usage, then recurring meetings/other habits.
_TIER = {"contact_rhythm": 1, "cancel_propensity": 2, "habit_protection": 3, "day_shape": 4,
         "wisp_usage": 5}


def _tier(p: Pattern) -> int:
    if p.kind == "habit":
        return 0 if (p.stats.get("key_type") == "category"
                     and p.stats.get("key") in _PERSONAL_CATEGORIES) else 6
    return _TIER.get(p.kind, 7)


def _score(p: Pattern) -> float:
    w = _KIND_WEIGHT.get(p.kind, 0.5)
    if _group(p) in ("habit:calendar", "habit:notes"):
        w = 0.5      # a recurring meeting is the calendar talking, not the person
    return p.confidence * w


def rank_patterns(patterns: list[Pattern]) -> list[Pattern]:
    """Confirmed first; then the best of each kind; then everything else, each
    pass in tier order (_tier) and by score within a tier. Rejected and
    low-confidence (unconfirmed) patterns are left out."""
    usable = [p for p in patterns if p.status != "rejected"
              and (p.status == "confirmed" or p.confidence >= MIN_CONFIDENCE)]
    ordered = sorted(usable, key=lambda p: (p.status != "confirmed", _tier(p), -_score(p), p.id))
    first, rest, groups = [], [], set()
    for p in ordered:
        if p.status == "confirmed" or _group(p) not in groups:
            first.append(p)
            groups.add(_group(p))
        else:
            rest.append(p)
    return first + rest


def prediction_line(pr: Prediction, tz: tzinfo, patterns: dict[str, Pattern]) -> str:
    t = fmt_clock(minute_of(local_dt(pr.ts, tz)))
    pct = int(round(pr.probability * 100))
    if pr.kind == "activity":
        source = patterns.get(pr.pattern_id)
        scheduled = source is None or source.stats.get("basis", "scheduled") == "scheduled"
        return (f"Expected schedule: {pr.subject} around {t} (pattern score {pct}%; completion unknown)" if scheduled
                else f"Expected routine: {pr.subject} around {t} (pattern score {pct}%)")
    if pr.kind == "cancel_risk":
        return f"{pr.subject} at {t} — schedule may change (historical rate {pct}%)"
    if pr.kind == "habit_conflict":
        h = patterns.get(pr.pattern_id)
        noun = miners.habit_noun(h) if h else "a routine"
        return f"{pr.subject} at {t} overlaps your usual {noun}"
    if pr.kind == "contact":
        end = minute_of(local_dt(pr.end_ts, tz)) if pr.end_ts else None
        span = fmt_range(minute_of(local_dt(pr.ts, tz)), end) if end else t
        return f"Usually in touch with {pr.subject} {span}"
    if pr.kind == "usage":
        return f"Usually asks Wisp about {pr.subject} around {t}"
    return f"{pr.subject} at {t}"


def _size(lines: list[str]) -> int:
    return len("\n".join(lines).encode("utf-8"))


def profile_card(patterns: list[Pattern], predictions: list[Prediction], now: float, tz: tzinfo,
                 max_bytes: int = 700) -> str:
    ranked = rank_patterns(patterns)
    by_id = {p.id: p for p in patterns}
    p_lines = [f"- {p.statement}" + (" (you confirmed)" if p.status == "confirmed" else "") for p in ranked]
    today = local_dt(now, tz).date()
    # Usage windows are left out: the card is read by Wisp itself, and the
    # matching wisp_usage statements already sit in the patterns section.
    todays = [pr for pr in predictions if local_dt(pr.ts, tz).date() == today and pr.kind != "usage"
              and (by_id.get(pr.pattern_id) is None or by_id[pr.pattern_id].status != "rejected")]
    order = {"habit_conflict": 0, "cancel_risk": 1, "activity": 2, "contact": 3}
    todays = sorted(todays, key=lambda pr: (order.get(pr.kind, 9), -pr.probability, pr.ts))
    t_lines = list(dict.fromkeys(f"- {prediction_line(pr, tz, by_id)}" for pr in todays))

    # Greedy by priority (top patterns, then a few of today's lines, then the
    # rest), rendered in section order; a line is taken only if the whole card
    # still fits, so the cap always holds on whole lines.
    picks_p: list[str] = []
    picks_t: list[str] = []

    def render(pp: list[str], tt: list[str]) -> list[str]:
        out = ([HEADER] + pp) if pp else []
        return out + ([TODAY] + tt if tt else [])

    plan = ([("p", x) for x in p_lines[:4]] + [("t", x) for x in t_lines[:3]]
            + [("p", x) for x in p_lines[4:]] + [("t", x) for x in t_lines[3:]])
    for which, line in plan:
        pp = picks_p + [line] if which == "p" else picks_p
        tt = picks_t + [line] if which == "t" else picks_t
        if _size(render(pp, tt)) <= max_bytes:
            picks_p, picks_t = pp, tt
    lines = render(sorted(picks_p, key=p_lines.index), sorted(picks_t, key=t_lines.index))
    return "\n".join(lines)


# Words people use for a category that the title taxonomy does not (or should
# not: "exercise" would catch "Tabletop exercise") map on its own.
_SUBJECT_SYNONYMS = {"exercise": "fitness", "exercising": "fitness", "working out": "fitness",
                     "workouts": "fitness", "sport": "fitness", "sports": "fitness",
                     "eating": "meal", "meals": "meal", "meetings": "meeting", "1:1s": "one_on_one",
                     "one on ones": "one_on_one", "one-on-ones": "one_on_one"}


def _subject_category(needle: str) -> str | None:
    """"gym" / "workout" / "exercise" -> "fitness"; None when no category fits."""
    cat = (needle.replace(" ", "_") if needle.replace(" ", "_") in CATEGORIES
           else _SUBJECT_SYNONYMS.get(needle) or categorize(needle))
    return cat if cat and cat != "other" else None


def _category_match(p: Pattern, cat: str, habit_ids: set[str]) -> bool:
    if p.kind == "habit":
        return cat in (p.stats.get("key"), p.stats.get("category"))
    if p.kind == "habit_protection":
        return p.stats.get("habit_pattern_id") in habit_ids
    if p.kind == "cancel_propensity":
        return str(p.stats.get("bucket", "")) == f"category_{cat}"
    return False


def describe(patterns: list[Pattern], predictions: list[Prediction], subject: str | None) -> dict:
    live = [p for p in patterns if p.status != "rejected"]
    if subject:
        needle = " ".join(subject.strip().lower().split())
        cat = _subject_category(needle)
        habit_ids = {p.id for p in live if cat and p.kind == "habit" and _category_match(p, cat, set())}
        live = [p for p in live if needle in p.subject.lower() or needle in p.statement.lower()
                or needle in str(p.stats.get("label", "")).lower()
                or (cat is not None and _category_match(p, cat, habit_ids))]
        ids = {p.id for p in live}
        predictions = [pr for pr in predictions if needle in pr.subject.lower()
                       or needle in pr.because.lower() or (cat is not None and pr.pattern_id in ids)]
    ranked = rank_patterns(live)
    shown = {p.id for p in ranked}
    ranked += sorted((p for p in live if p.id not in shown), key=lambda p: -p.confidence)
    if ranked:
        summary = "From your schedule and recorded activity: " + "; ".join(p.statement for p in ranked[:5]) + "."
    elif subject:
        summary = f"I haven't noticed a pattern about {subject} yet."
    else:
        summary = ("I haven't learned your routines yet — that takes a few weeks of calendar, "
                   "messages and Wisp use.")
    return {"patterns": [p.to_dict() for p in ranked],
            "predictions": [pr.to_dict() for pr in predictions], "summary": summary}
