"""Behavior-learning tools: what Wisp has learned about the user, and what it
expects next.

All four read (or, for ``correct_pattern``, edit) Wisp-internal learned state
only — never a native source — so they need no confirmation. Every line
carries its evidence counts so the model can say how sure Wisp is.
"""
from __future__ import annotations

import re
from datetime import datetime

from service.tools.registry import register

BEHAVIOR_TOOL_NAMES = frozenset({"describe_me", "predict_my_day", "my_patterns", "correct_pattern"})
_OFF = ("Behavior learning is off, so I'm not learning your routines. "
        "(Turn on \"Learn my routines\" in Settings.)")


def _unavailable() -> str:
    """Non-routable (hidden from retrieval and dispatch) while behavior
    learning is off, so the tool menus are unchanged when it is."""
    from service.behavior_switch import enabled
    return "" if enabled() else _OFF


_EMPTY = ("I haven't learned any reliable routines yet — I need a few more weeks of your calendar "
          "and activity before I can describe patterns with confidence.")

# Everyday words -> what the engine's patterns say (subjects/categories/statements).
_SYNONYMS = {
    "work out": ("fitness", "gym", "workout", "exercise", "run", "yoga"),
    "workout": ("fitness", "gym", "workout", "exercise"),
    "exercise": ("fitness", "gym", "workout", "exercise"),
    "gym": ("fitness", "gym", "workout"),
    "run": ("fitness", "run"),
    "talk": ("contact", "message", "talk", "text", "call"),
    "text": ("contact", "message", "text"),
    "message": ("contact", "message"),
    "call": ("contact", "call"),
    "cancel": ("cancel", "move", "back-to-back", "back_to_back", "skip"),
    "skip": ("cancel", "skip", "missed"),
    "reschedule": ("cancel", "move", "moved"),
    "meeting": ("meeting", "one_on_one", "back-to-back", "back_to_back"),
    "lunch": ("meal", "lunch"),
    "eat": ("meal", "lunch", "dinner", "breakfast"),
    "focus": ("focus", "deep work"),
    "wake": ("day_shape", "first"),
    "start my day": ("day_shape", "first"),
    "afternoon": ("afternoon",),
    "morning": ("morning",),
    "evening": ("evening",),
    "wisp": ("wisp_usage", "ask"),
}
_STOP = {"i", "do", "my", "me", "the", "a", "an", "usually", "normally", "typically", "when", "what",
         "who", "which", "how", "often", "time", "to", "in", "on", "at", "with", "does", "is", "are",
         "you", "know", "about", "habits", "habit", "pattern", "patterns", "routine", "routines", "tend"}


def _runtime():
    from service.assistant import behavior_runtime
    return behavior_runtime.get_runtime()


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%-I:%M %p")


def _pattern_line(p: dict) -> str:
    support, opp = int(p.get("support") or 0), int(p.get("opportunities") or 0)
    conf = float(p.get("confidence") or 0.0)
    status = p.get("status") or "active"
    bits = [f"seen {support} of {opp} times" if opp else f"seen {support} times",
            f"confidence {round(conf * 100)}%"]
    if status == "confirmed":
        bits.append("you confirmed this")
    if p.get("next_expected"):
        try:
            bits.append("next expected " + datetime.fromtimestamp(float(p["next_expected"])).strftime("%a %-I:%M %p"))
        except (TypeError, ValueError, OSError):
            pass
    return f"- {p.get('statement') or p.get('subject')} ({'; '.join(bits)}) [pattern {p.get('id')}]"


def _visible(patterns: list[dict]) -> list[dict]:
    """Non-rejected patterns in the engine's own interest ranking, confirmed first."""
    rows = [p for p in patterns if (p.get("status") or "active") != "rejected"]
    return sorted(rows, key=lambda p: p.get("status") != "confirmed")


# Question intent -> pattern kinds/categories that answer it best.
_INTENT = (
    (r"\b(?:who|talk|text|chat|speak|message|call|in touch)\b", {"contact_rhythm"}),
    (r"\b(?:cancel|skip|move|reschedul\w*|bail|drop|decline)\b", {"cancel_propensity", "habit_protection"}),
    (r"\b(?:work\s*out|workouts?|gym|exercise|lift|run|running|fitness|yoga)\b", {"fitness"}),
    (r"\b(?:ask|use)\s+(?:you|wisp)\b", {"wisp_usage"}),
    (r"\b(?:start|end|first|last|lunch)\b.*\bday\b|\bday\s+shape\b", {"day_shape"}),
)


def _terms(query: str) -> set[str]:
    q = " ".join((query or "").casefold().replace("’", "'").split())
    out: set[str] = set()
    for phrase, expansion in _SYNONYMS.items():
        if re.search(r"\b" + re.escape(phrase) + r"\b", q):
            out.update(expansion)
    out.update(w for w in re.findall(r"[a-z][a-z'_-]+", q) if w not in _STOP and len(w) > 2)
    return out


def _hay(p: dict) -> str:
    hay = " ".join(str(p.get(k) or "") for k in ("statement", "subject", "kind")).casefold()
    return hay + " " + " ".join(f"{k} {v}" for k, v in (p.get("stats") or {}).items()
                                if isinstance(v, str)).casefold()


def _score(p: dict, query: str, terms: set[str]) -> int:
    hay = _hay(p)
    score = sum(1 for t in terms if re.search(r"\b" + re.escape(t), hay))
    q = (query or "").casefold()
    for pattern, wanted in _INTENT:
        if re.search(pattern, q) and (p.get("kind") in wanted
                                      or (p.get("stats") or {}).get("key") in wanted
                                      or (p.get("stats") or {}).get("category") in wanted):
            score += 3
    return score


def _best(patterns: list[dict], query: str, limit: int = 6) -> list[dict]:
    terms = _terms(query)
    scored = [(s, i, p) for i, p in enumerate(patterns) if (s := _score(p, query, terms)) > 0]
    if not scored:
        return []
    top = max(s for s, _, _ in scored)
    return [p for s, i, p in sorted(scored, key=lambda x: (-x[0], x[1])) if s * 2 >= top][:limit]


def _describe(subject: str | None = None) -> dict:
    runtime = _runtime()
    if runtime is None:
        return {}
    data = runtime.call("describe", subject or None)
    return data if isinstance(data, dict) else {}


@register(
    "describe_me",
    "Describe what Wisp has LEARNED about the user's routines from their calendar, reminders and "
    "activity: habits (e.g. gym on weekday mornings), who they talk to and when, which meetings "
    "they tend to cancel, the shape of their day. Each pattern has evidence counts and a pattern "
    "id. Use for 'what do you know about me', 'describe me', 'what are my habits'. Read-only.",
    {"type": "object", "properties": {
        "subject": {"type": "string", "description": "optional filter, e.g. 'gym' or 'Sam'"},
    }},
    category="assistant_read",
    unavailable_reason=_unavailable(),
    aliases=["what do you know about me", "describe me", "what are my habits",
             "what routines have you noticed", "what have you learned about me"],
)
def describe_me(subject: str | None = None) -> str:
    if _runtime() is None:
        return _OFF
    data = _describe(None)
    patterns = _visible(list(data.get("patterns") or []))
    if subject:
        patterns = _best(patterns, subject, limit=12)
    if not patterns:
        return _EMPTY if not subject else f"I haven't learned a pattern about {subject!r} yet."
    lines = ["What I've learned from your calendar and activity (patterns, not certainties — "
             "say 'that's wrong' about any of them and I'll stop using it):"]
    lines += [_pattern_line(p) for p in patterns[:12]]
    if data.get("summary"):
        lines.append("Summary: " + str(data["summary"]))
    return "\n".join(lines)


@register(
    "predict_my_day",
    "Predict the user's day from their learned routines plus what is already scheduled: likely "
    "activities (gym, lunch), contacts they usually talk to, meetings likely to be cancelled, and "
    "conflicts with habits. Each prediction has a probability and a 'because'. Use for 'predict my "
    "day', 'what will tomorrow look like'. Read-only; it does not change the calendar.",
    {"type": "object", "properties": {
        "day": {"type": "string", "enum": ["today", "tomorrow"], "description": "today or tomorrow (default)"},
    }},
    category="assistant_read",
    unavailable_reason=_unavailable(),
    aliases=["predict my day", "what will tomorrow look like", "how will my day go"],
)
def predict_my_day(day: str = "tomorrow") -> str:
    from service.assistant import behavior_runtime
    runtime = _runtime()
    if runtime is None:
        return _OFF
    if day not in {"today", "tomorrow"}:
        day = "tomorrow"
    preds = behavior_runtime.predictions_for(runtime, day)
    start, _ = behavior_runtime.day_bounds(day, runtime.clock())
    label = ("Today" if day == "today" else "Tomorrow") + datetime.fromtimestamp(start).strftime(" (%A, %B %-d)")
    if not preds:
        return f"{label}: I don't have enough learned routine to predict anything specific yet."
    names = {"activity": "likely", "cancel_risk": "may be cancelled", "contact": "likely to talk",
             "habit_conflict": "conflicts with a habit"}
    lines = [f"{label}, based on your learned routines (probabilities, not commitments):"]
    for p in preds[:12]:
        span = _clock(p.ts) + (f"–{_clock(p.end_ts)}" if p.end_ts else "")
        line = f"- {span} {p.subject} — {names.get(p.kind, p.kind)} ({round(float(p.probability) * 100)}%)"
        if p.because:
            line += f": {p.because}"
        lines.append(line)
    return "\n".join(lines)


@register(
    "my_patterns",
    "Search the user's learned routines for one question, e.g. 'when do I usually work out', 'who "
    "do I talk to in the afternoon', 'which meetings do I usually cancel'. Returns matching "
    "patterns with evidence counts and pattern ids. Read-only.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "what to look for, in the user's words"},
    }, "required": ["query"]},
    category="assistant_read",
    unavailable_reason=_unavailable(),
    aliases=["when do I usually work out", "who do I talk to in the afternoon",
             "which meetings do I usually cancel", "do I usually skip"],
)
def my_patterns(query: str) -> str:
    if _runtime() is None:
        return _OFF
    patterns = _visible(list(_describe(None).get("patterns") or []))
    if not patterns:
        return _EMPTY
    hits = _best(patterns, query)
    if not hits:
        lines = [f"Nothing I've learned matches {query!r} yet. Everything I know so far:"]
        return "\n".join(lines + [_pattern_line(p) for p in patterns[:8]])
    return "\n".join([f"Learned patterns matching {query!r}:"] + [_pattern_line(p) for p in hits])


@register(
    "correct_pattern",
    "Confirm or reject one learned pattern when the user says it is right or wrong (e.g. 'that's "
    "wrong, I stopped going to the gym'). Pass the pattern id from describe_me/my_patterns, or a "
    "short description. Rejected patterns stop driving predictions and suggestions. This only "
    "changes Wisp's own learned profile.",
    {"type": "object", "properties": {
        "pattern_id": {"type": "string", "description": "id shown as [pattern …]"},
        "text": {"type": "string", "description": "description, used when no id is given"},
        "status": {"type": "string", "enum": ["confirmed", "rejected", "active"]},
    }, "required": ["status"]},
    category="assistant_write",
    unavailable_reason=_unavailable(),
    aliases=["that pattern is wrong", "forget that habit", "yes that's right about me"],
)
def correct_pattern(status: str, pattern_id: str | None = None, text: str | None = None) -> str:
    runtime = _runtime()
    if runtime is None:
        return _OFF
    if status not in {"confirmed", "rejected", "active"}:
        return "(error calling correct_pattern: status must be confirmed, rejected or active.)"
    patterns = [p.to_dict() for p in runtime.call("patterns")]
    target = None
    if pattern_id:
        pid = str(pattern_id).strip().strip("[]").removeprefix("pattern ").strip()
        target = next((p for p in patterns if p["id"] == pid), None)
    if target is None and text:
        hits = _best(patterns, text, limit=5)
        if len(hits) > 1:
            return "Which one? " + " | ".join(f"{p['statement']} [pattern {p['id']}]" for p in hits[:5])
        target = hits[0] if hits else None
    if target is None:
        return "Nothing active matches that pattern; ask me to describe what I've learned first."
    updated = runtime.call("set_pattern_status", target["id"], status)
    runtime.invalidate_card()
    verb = {"confirmed": "Confirmed", "rejected": "Got it — I'll stop using",
            "active": "Restored"}[status]
    return f"{verb}: {updated.statement} [pattern {updated.id}]"


def sync_availability() -> str:
    """Re-apply the on/off switch to the four registered tools, so changing the
    setting hides or restores them at once without restarting Wisp."""
    from service.tools.registry import REGISTRY
    reason = _unavailable()
    for name in BEHAVIOR_TOOL_NAMES:
        if name in REGISTRY:
            REGISTRY[name].unavailable_reason = reason
    return reason
