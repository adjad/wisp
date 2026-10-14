"""Shared data contract for the behavior engine, Mac runtime and Swift app.
Change only with every consumer in mind: these shapes are persisted as JSON
(``to_dict``/``from_dict``) and travel into the app's hub events
(``Nudge.to_event``).

Times are epoch seconds (float). Weekdays are 0=Monday..6=Sunday. Minutes are
minutes after local midnight. "Local" means the engine's configured timezone
(``BehaviorEngine(tz=...)``, which must be the user's timezone). Snapshots and observations carry only epoch timestamps,
no ``tz``: the engine does not adopt a timezone from its callers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

# --------------------------------------------------------------------------
# Observations: one normalized timeline of what the user did / has planned.
# --------------------------------------------------------------------------

OBSERVATION_KINDS = (
    "event",              # calendar event occurrence (past or upcoming); end_ts set when known
    "event_cancelled",    # an upcoming event vanished from the calendar before it started
    "event_moved",        # same source_id re-appeared at another time; attrs: from_ts, to_ts
    "event_declined",     # user's participant status is declined
    "reminder",           # a reminder with a due time (attrs: list, status)
    "reminder_done",      # completed (ts = completion time when known, else due time)
    "reminder_dismissed",
    "note",               # a note (subject = title); ts = modified time
    "message_in",         # incoming message; subject = contact display name
    "message_out",        # outgoing message; subject = contact display name
    "wisp_ask",           # the user asked Wisp something; subject = route/tool/topic
    "approval",           # user approved a Wisp action; subject = tool
    "denial",             # user denied a Wisp action; subject = tool
    "nudge_feedback",     # attrs: nudge_id, verdict
)

ObservationKind = Literal[
    "event", "event_cancelled", "event_moved", "event_declined", "reminder",
    "reminder_done", "reminder_dismissed", "note", "message_in", "message_out",
    "wisp_ask", "approval", "denial", "nudge_feedback",
]

# Categories the categorizer may assign (fixed list; an LLM may only pick from it).
CATEGORIES = (
    "fitness", "meeting", "one_on_one", "class", "meal", "call", "focus",
    "travel", "social", "personal", "errand", "health", "other",
)


@dataclass(frozen=True)
class Observation:
    id: str                     # stable + idempotent, e.g. "cal:<source_id>:<int(when_ts)>"
    kind: str                   # one of OBSERVATION_KINDS
    ts: float                   # when it happened / starts
    subject: str = ""           # event title, contact name, tool, note title
    source: str = ""            # calendar | reminders | notes | messages | wisp | demo
    end_ts: float | None = None
    category: str = ""          # filled by categorize() when empty
    source_id: str = ""         # upstream id (EventKit identifier etc.), for move detection
    attrs: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Observation":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------
# Patterns: what the engine learned, always with evidence.
# --------------------------------------------------------------------------

PATTERN_KINDS = (
    "habit",              # category/subject recurring in a weekday x time window ("gym weekday mornings")
    "contact_rhythm",     # when the user talks to a person ("Sam, afternoons")
    "cancel_propensity",  # what the user tends to cancel/move ("last of 3+ back-to-back")
    "habit_protection",   # what happens to events that collide with a habit
    "day_shape",          # first/last commitment, lunch window
    "wisp_usage",         # what the user asks Wisp, and when
)

PatternStatus = Literal["active", "confirmed", "rejected"]


@dataclass(frozen=True)
class Window:
    weekdays: tuple[int, ...] = ()   # empty = any day
    start_minute: int = 0            # inclusive
    end_minute: int = 24 * 60        # exclusive

    def to_dict(self) -> dict[str, Any]:
        return {"weekdays": list(self.weekdays), "start_minute": self.start_minute,
                "end_minute": self.end_minute}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Window":
        d = d or {}
        return cls(tuple(int(x) for x in d.get("weekdays", ())), int(d.get("start_minute", 0)),
                   int(d.get("end_minute", 24 * 60)))


@dataclass(frozen=True)
class Pattern:
    id: str                      # deterministic: hash of (kind, subject, window key)
    kind: str                    # one of PATTERN_KINDS
    subject: str                 # "fitness", "Sam", "back_to_back_last", ...
    statement: str               # human sentence, e.g. "You go to the gym on weekday mornings (6:30-8:00)"
    window: Window = Window()
    support: int = 0             # times it happened
    opportunities: int = 0       # times it could have happened
    confidence: float = 0.0      # Wilson lower bound of support/opportunities, 0..1
    last_seen: float | None = None
    next_expected: float | None = None
    evidence: tuple[str, ...] = ()   # observation ids (<= 5)
    stats: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    status: str = "active"       # PatternStatus
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["window"] = self.window.to_dict()
        d["evidence"] = list(self.evidence)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Pattern":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["window"] = Window.from_dict(d.get("window"))
        data["evidence"] = tuple(d.get("evidence") or ())
        return cls(**data)


# --------------------------------------------------------------------------
# Predictions for the next horizon.
# --------------------------------------------------------------------------

PREDICTION_KINDS = ("activity", "cancel_risk", "contact", "habit_conflict", "usage")


@dataclass(frozen=True)
class Prediction:
    kind: str                    # one of PREDICTION_KINDS
    subject: str                 # "gym", "Team sync", "Sam"
    ts: float                    # expected start
    end_ts: float | None = None
    probability: float = 0.0
    because: str = ""            # one line, code-composed
    pattern_id: str = ""
    refs: tuple[str, ...] = ()   # observation ids involved (e.g. the at-risk event)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["refs"] = list(self.refs)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Prediction":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["refs"] = tuple(d.get("refs") or ())
        return cls(**data)


# --------------------------------------------------------------------------
# Actions and nudges.
# --------------------------------------------------------------------------

ACTION_TYPES = (
    "add_reminder",      # reversible; params: title, when_ts, notes
    "add_hold",          # reversible; Wisp-schedule tentative hold (not Apple Calendar); params: title, when_ts, end_ts
    "open_prompt",       # opens the overlay with a prefilled request; params: prompt
)

NUDGE_KINDS = ("habit_conflict", "cancel_likely", "contact_window", "day_preview", "habit_missed")


@dataclass(frozen=True)
class Action:
    type: str                        # one of ACTION_TYPES
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    auto: bool = False               # True = Wisp performs it without asking (only reversible types)
    label: str = ""                  # button text, e.g. "Draft reschedule note"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Action | None":
        if not d:
            return None
        return cls(type=str(d.get("type", "")), params=dict(d.get("params") or {}),
                   auto=bool(d.get("auto", False)), label=str(d.get("label", "")))


@dataclass(frozen=True)
class Nudge:
    id: str                          # deterministic from (kind, key facts) so re-generation dedupes
    kind: str                        # one of NUDGE_KINDS
    title: str                       # short headline
    body: str                        # one or two sentences (may be LLM-phrased)
    because: str                     # code-composed evidence line
    created_at: float
    expires_at: float | None = None
    priority: float = 0.0            # 0..1 used by the budget
    pattern_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()   # observation ids
    actions: tuple[Action, ...] = () # offered actions (auto ones are performed by the Mac runtime)
    seq: int = 0                     # monotonically increasing cursor assigned by the store

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "body": self.body,
            "because": self.because, "created_at": self.created_at, "expires_at": self.expires_at,
            "priority": self.priority, "pattern_ids": list(self.pattern_ids),
            "evidence": list(self.evidence), "actions": [a.to_dict() for a in self.actions],
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Nudge":
        return cls(
            id=str(d["id"]), kind=str(d["kind"]), title=str(d.get("title", "")),
            body=str(d.get("body", "")), because=str(d.get("because", "")),
            created_at=float(d.get("created_at", 0.0)), expires_at=d.get("expires_at"),
            priority=float(d.get("priority", 0.0)), pattern_ids=tuple(d.get("pattern_ids") or ()),
            evidence=tuple(d.get("evidence") or ()),
            actions=tuple(a for a in (Action.from_dict(x) for x in d.get("actions") or ()) if a),
            seq=int(d.get("seq", 0)),
        )

    def to_event(self) -> dict[str, Any]:
        """Hub/SSE payload for the Swift app (``case "nudge"`` in OverlayModel).
        Required keys for the app: type, nudge_id, title, body, because."""
        return {"type": "nudge", "nudge_id": self.id, "kind": self.kind, "title": self.title,
                "body": self.body, "because": self.because, "priority": self.priority,
                "actions": [a.to_dict() for a in self.actions], "expires_at": self.expires_at}


FEEDBACK_VERDICTS = ("useful", "not_now", "wrong", "undo")


@dataclass(frozen=True)
class Feedback:
    nudge_id: str
    verdict: str          # one of FEEDBACK_VERDICTS
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
