"""Who the user is, in their own words — the profile behind "Getting to know you".

Wisp reads the user's calendar, mail and messages, but until this module it knew
nothing the user had not typed into a chat: the brief greeted "you" and every
reply had one tone. A chief of staff starts by learning who they work for. The
notch's Getting to know you flow (KnowYouView.swift) asks, one question at a
time, and this module keeps the answers and puts them to work in three places:

  * `profile_block()`  — the system-prompt block every LOCAL text path carries
    after the identity block (service/agent/loop.py, main.py's plain chat
    branches): name, goals, struggles, people, tone, boundaries, plus the rules
    for using them kindly.
  * `keeping_in_mind()` — the brief's deterministic "Keeping in mind" section
    (service/assistant/brief.py). No model writes it; it fires only on signals
    the day actually shows.
  * `prioritize()` — raises a "Needs you" item that names one of the user's
    people in the Wisp window's day planner (service/assistant/daily_workspace.py,
    via daily_workspace_api.current_workspace). The notch's Day and Brief do not
    pass the profile yet, so nothing the user is TOLD promises that ordering:
    `preview()` and About you promise only the brief's people line.

Everything is optional, bounded and local. The file is plain JSON under
~/.moe (owner-only, 0600) so it is easy to read, edit and erase. The profile
does not travel to an external local provider. When the user runs ALL of Wisp
on a cloud model (Settings > Cloud, "Everything", or WISP_REMOTE_ONLY), it rides
in that model's system prompt like the rest of Wisp's context; `status()` then
reports local_only=False so the app stops saying "Stays on this Mac".

What this deliberately is NOT: a VIP list for notifications. Proactive attention
decides what may interrupt from recent two-way contact (user decision P4,
docs/PROACTIVE_ATTENTION_PLAN.md). `people` changes the ORDER of Today and the
wording of the brief, never who Wisp alerts about.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import threading
import time

from service.paths import MOE_DIR

PROFILE_PATH = MOE_DIR / "profile.json"
# 2: the default length became "thorough" (Wisp's own full-answer voice). Every
# version-1 file holds "short" whether or not the person chose it, because the
# old flow saved its preselected default; see _read.
VERSION = 2

TONES = ("gentle", "warm", "direct")
DETAILS = ("short", "thorough")
GOAL_STATUSES = ("on_track", "behind", "done")

# Bounds. Generous enough for a real answer, small enough that the prompt block
# stays a block and a pasted essay cannot crowd out the user's actual question.
LIMITS = {"name": 60, "about": 600, "goal": 200, "struggle": 160,
          "person_name": 60, "relation": 60, "person_note": 160,
          "boundaries": 600, "notes": 800}
MAX_GOALS, MAX_STRUGGLES, MAX_PEOPLE = 5, 6, 8

# Offer the flow again this long after a "Later", once. A second "Later" means
# the user has decided; the menu bar and /about remain the way in.
REOFFER_AFTER_S = 3 * 86400
MAX_AUTO_OFFERS = 2
# After this long unchanged, About you says the answers may be worth a look.
STALE_AFTER_S = 45 * 86400

_lock = threading.Lock()


def empty() -> dict:
    return {"version": VERSION, "name": "", "about": "", "goals": [], "struggles": [],
            "people": [], "tone": "warm", "detail": "thorough",
            "boundaries": "", "notes": "", "completed_at": None, "updated_at": None,
            "dismissed_at": None, "dismiss_count": 0,
            # Weekly goal pulse: its clock starts at a goal change or an answer.
            "goal_status": {}, "goal_pulse_at": None}


# ---------------------------------------------------------------------------
# Validation


def _text(value, limit: int, field: str, *, single_line: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if single_line:
        # A name, goal or struggle is one line of a prompt: a pasted newline
        # must not become a second, unlabelled instruction.
        cleaned = re.sub(r"\s+", " ", value).strip()
    else:
        cleaned = re.sub(r"[ \t]+", " ", value.replace("\r", "")).strip()
    if len(cleaned) > limit:
        raise ValueError(f"{field} is longer than {limit} characters")
    return cleaned


def _texts(value, limit: int, count: int, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    out: list[str] = []
    for entry in value:
        cleaned = _text(entry, limit, field, single_line=True)
        if cleaned and cleaned.casefold() not in {o.casefold() for o in out}:
            out.append(cleaned)
    if len(out) > count:
        raise ValueError(f"{field} holds more than {count} entries")
    return out


def _people(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("people must be a list")
    out: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("each person must be an object")
        name = _text(entry.get("name"), LIMITS["person_name"], "person name")
        if not name:
            continue
        person = {"name": name,
                  "relation": _text(entry.get("relation"), LIMITS["relation"], "relation"),
                  "note": _text(entry.get("note"), LIMITS["person_note"], "person note")}
        if person["name"].casefold() not in {p["name"].casefold() for p in out}:
            out.append(person)
    if len(out) > MAX_PEOPLE:
        raise ValueError(f"people holds more than {MAX_PEOPLE} entries")
    return out


# Wisp's own voice (the base prompts: energetic, full and vivid). A default
# choice adds nothing to the prompt; only a different one does.
_DEFAULT_CHOICE = {"tone": "warm", "detail": "thorough"}


def _choice(value, options: tuple[str, ...], field: str) -> str:
    if value is None or value == "":
        return _DEFAULT_CHOICE[field]
    if value not in options:
        raise ValueError(f"{field} must be one of {', '.join(options)}")
    return value


EDITABLE = ("name", "about", "goals", "struggles", "people", "tone", "detail",
            "boundaries", "notes")


def validate(patch: dict) -> dict:
    """The editable fields of `patch`, cleaned. Raises ValueError on anything
    out of bounds; unknown keys are an error too, so a typo cannot silently
    drop an answer."""
    if not isinstance(patch, dict):
        raise ValueError("profile must be an object")
    unknown = set(patch) - set(EDITABLE)
    if unknown:
        raise ValueError("unknown profile field(s): " + ", ".join(sorted(unknown)))
    out: dict = {}
    for field, value in patch.items():
        if field == "name":
            out[field] = _text(value, LIMITS["name"], field, single_line=True)
        elif field == "about":
            out[field] = _text(value, LIMITS["about"], field)
        elif field == "goals":
            out[field] = _texts(value, LIMITS["goal"], MAX_GOALS, field)
        elif field == "struggles":
            out[field] = _texts(value, LIMITS["struggle"], MAX_STRUGGLES, field)
        elif field == "people":
            out[field] = _people(value)
        elif field == "tone":
            out[field] = _choice(value, TONES, field)
        elif field == "detail":
            out[field] = _choice(value, DETAILS, field)
        elif field == "boundaries":
            out[field] = _text(value, LIMITS["boundaries"], field)
        elif field == "notes":
            out[field] = _text(value, LIMITS["notes"], field)
    return out


# ---------------------------------------------------------------------------
# Storage


def _quarantine() -> None:
    """Move an unreadable profile.json aside as profile.json.bad-<timestamp>."""
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = PROFILE_PATH.with_name(f"{PROFILE_PATH.name}.bad-{stamp}")
        n = 1
        while target.exists():
            n += 1
            target = PROFILE_PATH.with_name(f"{PROFILE_PATH.name}.bad-{stamp}-{n}")
        os.replace(PROFILE_PATH, target)
        _owner_only(target)
    except OSError:
        pass


def _owner_only(path) -> None:
    """A version-1 file, or one restored by hand, may be world-readable.
    Tighten it to 0600 the first time Wisp reads it."""
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


def _read() -> dict:
    try:
        raw = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty()
    except OSError:
        return empty()
    except ValueError:
        # Unparseable: keep the bytes for the user (a hand edit gone wrong, or a
        # torn write) instead of letting the next save silently replace them.
        _quarantine()
        return empty()
    _owner_only(PROFILE_PATH)
    if not isinstance(raw, dict):
        _quarantine()
        return empty()
    profile = empty()
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        version = 1  # missing or hand-edited to something odd: read it as the oldest format
    if version < 2 and raw.get("detail") == "short":
        # Version 1 saved the preselected "short" with every answer, so it is
        # not evidence of a choice. Read it as Wisp's default.
        raw = {**raw, "detail": _DEFAULT_CHOICE["detail"]}
    try:
        profile.update(validate({k: raw.get(k) for k in EDITABLE if k in raw}))
    except ValueError:
        # Hand-edited past a bound: keep what validates field by field.
        for field in EDITABLE:
            if field in raw:
                try:
                    profile.update(validate({field: raw[field]}))
                except ValueError:
                    pass
    for stamp in ("completed_at", "updated_at", "dismissed_at", "goal_pulse_at"):
        value = raw.get(stamp)
        profile[stamp] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    statuses = raw.get("goal_status")
    if isinstance(statuses, dict):
        profile["goal_status"] = {
            str(goal): {"status": entry["status"], "at": float(entry.get("at") or 0)}
            for goal, entry in statuses.items()
            if isinstance(entry, dict) and entry.get("status") in GOAL_STATUSES
            and str(goal) in profile["goals"]}
    count = raw.get("dismiss_count")
    profile["dismiss_count"] = int(count) if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
    return profile


def _write(profile: dict) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_PATH.with_suffix(".json.tmp")
    # Owner-only from the first byte: struggles, people and boundaries are the
    # most personal text Wisp keeps.
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({**profile, "version": VERSION}, handle, indent=2, ensure_ascii=False, allow_nan=False)
    os.replace(tmp, PROFILE_PATH)


def load() -> dict:
    with _lock:
        return _read()


def update(patch: dict, *, now: float | None = None) -> dict:
    """Merge validated fields into the stored profile and return the result."""
    cleaned = validate(patch)
    with _lock:
        profile = _read()
        stamp = time.time() if now is None else now
        goals = cleaned.get("goals", profile["goals"])
        if goals != profile["goals"]:
            profile["goal_pulse_at"] = stamp if goals else None
        elif goals and cleaned and profile.get("goal_pulse_at") is None:
            # Freeze a legacy profile's goal clock before updated_at advances.
            # Without any recorded date its pulse was already due; an epoch
            # anchor preserves that instead of postponing it on an unrelated edit.
            previous = profile.get("updated_at")
            profile["goal_pulse_at"] = previous if previous is not None else 0.0
        profile.update(cleaned)
        if "goals" in cleaned:
            profile["goal_status"] = {g: v for g, v in profile.get("goal_status", {}).items()
                                      if g in cleaned["goals"]}
        if cleaned:
            profile["updated_at"] = stamp
        _write(profile)
        return profile


def set_goal_status(goal: str, status: str, *, now: float | None = None) -> dict:
    """The weekly pulse's one tap. `done` retires the goal from the brief's
    rotation; `behind` makes it the goal the brief leads with."""
    if status not in GOAL_STATUSES:
        raise ValueError("status must be on_track, behind or done")
    now = time.time() if now is None else now
    with _lock:
        profile = _read()
        if goal not in profile.get("goals", []):
            raise ValueError("that goal is not in the profile")
        profile.setdefault("goal_status", {})[goal] = {"status": status, "at": now}
        profile["goal_pulse_at"] = now
        _write(profile)
        return profile


def mark_complete(*, now: float | None = None) -> dict:
    with _lock:
        profile = _read()
        profile["completed_at"] = time.time() if now is None else now
        profile["dismissed_at"] = None
        _write(profile)
        return profile


def dismiss(*, now: float | None = None) -> dict:
    """"Later" on the welcome screen."""
    with _lock:
        profile = _read()
        profile["dismissed_at"] = time.time() if now is None else now
        profile["dismiss_count"] = profile.get("dismiss_count", 0) + 1
        _write(profile)
        return profile


def clear(*, now: float | None = None) -> dict:
    """Erase every answer. What remains is only the fact that the person chose
    to erase, so the questions are not offered again on their own (the menu
    bar and /about still open them).

    Erase means every copy: the answers in profile.json and any unreadable
    copies set aside as profile.json.bad-*, which hold the same personal text."""
    with _lock:
        for leftover in PROFILE_PATH.parent.glob(f"{PROFILE_PATH.name}.bad-*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        profile = empty()
        profile["dismissed_at"] = time.time() if now is None else now
        profile["dismiss_count"] = MAX_AUTO_OFFERS
        _write(profile)
        return profile


# ---------------------------------------------------------------------------
# Status


def filled_fields(profile: dict) -> list[str]:
    out = []
    for field in EDITABLE:
        value = profile.get(field)
        if field in _DEFAULT_CHOICE and value == _DEFAULT_CHOICE[field]:
            continue  # untouched defaults are not evidence the user answered
        if value:
            out.append(field)
    return out


def has_content(profile: dict) -> bool:
    return bool(filled_fields(profile))


def status(profile: dict, *, now: float | None = None) -> dict:
    """What the app needs to decide whether to offer the flow and how to label
    About you. `offer` is true only on a Mac where the user has neither finished
    nor said Later recently (and never more than MAX_AUTO_OFFERS times)."""
    now = time.time() if now is None else now
    completed = profile.get("completed_at") is not None
    dismissed_at = profile.get("dismissed_at")
    count = profile.get("dismiss_count", 0)
    # Someone who already filled in About you has told Wisp who they are; the
    # questionnaire would only ask it again. A name alone (step one, then the
    # person left) still leaves the rest worth offering.
    beyond_name = set(filled_fields(profile)) - {"name"}
    offer = (not completed and not beyond_name and count < MAX_AUTO_OFFERS
             and (dismissed_at is None or now - dismissed_at >= REOFFER_AFTER_S))
    updated = profile.get("updated_at")
    days = int((now - updated) // 86400) if updated is not None else None
    return {"completed": completed, "has_content": has_content(profile),
            "filled": filled_fields(profile), "offer": offer,
            "stale": has_content(profile) and updated is not None and now - updated >= STALE_AFTER_S,
            "days_since_update": days}


# ---------------------------------------------------------------------------
# Use 1: the system prompt


_TONE_RULES = {
    "gentle": ("Speak gently and reassuringly. Lead with what is going well or is "
               "handled before what is not; never scold."),
    "warm": ("Be warm and energetic: friendly, upbeat and vivid, with real "
             "enthusiasm for what they are doing."),
    "direct": ("Be straight to the point. Skip pleasantries and preamble; give the "
               "answer or the ask first."),
}
_DETAIL_RULES = {
    "short": "Keep replies short (a line or two) unless they ask for more.",
    "thorough": "They prefer thorough answers: include the detail and the reasoning.",
}


def profile_block(profile: dict | None = None) -> str:
    """The prompt block. Empty string when the user has told Wisp nothing yet
    (so a cold install pays no prompt tokens for it)."""
    profile = load() if profile is None else profile
    if not has_content(profile):
        return ""
    lines = ["ABOUT THE USER — they told Wisp this directly, in the Getting to know "
             "you questions. Trust it over guesses from their data, and use it to "
             "make help personal, not to lecture."]
    if profile.get("name"):
        lines.append(f"- Call them {profile['name']}. Use the name naturally, not in every sentence.")
    if profile.get("about"):
        lines.append(f"- Who they are, in their words: {profile['about']}")
    if profile.get("goals"):
        lines.append("- What they are working toward: " + "; ".join(profile["goals"])
                     + ". Connect help to these when it genuinely fits; do not turn "
                       "them into a to-do list or bring them up unasked.")
    if profile.get("struggles"):
        lines.append("- What tends to get in their way, by their own account: "
                     + "; ".join(profile["struggles"])
                     + ". Mention one only when it helps with what they are doing right "
                       "now, as an ally would — never as a reproach, a diagnosis or a "
                       "reminder of past failure.")
    if profile.get("people"):
        people = []
        for person in profile["people"]:
            label = person["name"]
            if person.get("relation"):
                label += f" ({person['relation']})"
            if person.get("note"):
                label += f" — {person['note']}"
            people.append(label)
        lines.append("- People who matter to them: " + "; ".join(people)
                     + ". Recognise these names in their mail, texts and calendar. This "
                       "list says who matters to the user; it does not make Wisp alert "
                       "about them, and it is never a reason to send anything.")
    # Only a choice that differs from Wisp's own voice is stated: the default
    # adds a second, competing voice rule to every prompt for nothing.
    manner = [rule for field, rules in (("tone", _TONE_RULES), ("detail", _DETAIL_RULES))
              if (choice := profile.get(field)) and choice != _DEFAULT_CHOICE[field]
              and (rule := rules.get(choice))]
    if manner:
        lines.append("- How to talk to them: " + " ".join(manner))
    if profile.get("boundaries"):
        lines.append(f"- Be careful about: {profile['boundaries']}. Respect this even when "
                     "it would be convenient not to.")
    if profile.get("notes"):
        lines.append(f"- They also wanted Wisp to know: {profile['notes']}")
    return "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Use 2: the brief's "Keeping in mind" section


def _word_pattern(name: str) -> re.Pattern | None:
    first = name.strip().split()
    if not first:
        return None
    # Match the first name as a whole word; "Sam" must not match "Samantha"
    # or "same". A full "Sam Lee" also matches on its first name alone, since
    # mail and texts rarely carry the surname.
    return re.compile(r"(?<!\w)" + re.escape(first[0]) + r"(?!\w)", re.IGNORECASE)


def people_mentioned(profile: dict, texts: list[str]) -> list[str]:
    """Profile people whose first name appears as a word in any of `texts`, in
    profile order, each once."""
    found: list[str] = []
    joined = "\n".join(t for t in texts if isinstance(t, str) and t)
    if not joined:
        return found
    for person in profile.get("people") or []:
        pattern = _word_pattern(person["name"])
        if pattern and pattern.search(joined):
            found.append(person["name"])
    return found


def goal_in_focus(profile: dict, now: float) -> str:
    """One goal a day, rotating, so every goal gets its mornings. A goal the
    user marked behind in the weekly pulse leads until they say otherwise; a
    goal marked done leaves the rotation."""
    statuses = profile.get("goal_status") or {}
    goals = [g for g in profile.get("goals") or [] if (statuses.get(g) or {}).get("status") != "done"]
    if not goals:
        return ""
    behind = [g for g in goals if (statuses.get(g) or {}).get("status") == "behind"]
    if behind:
        return behind[0]
    day = datetime.fromtimestamp(now).timetuple().tm_yday
    return goals[day % len(goals)]


# Each struggle theme fires on ONE kind of evidence the day actually shows. A
# theme with no matching signal says nothing: an observation the day does not
# support would read as nagging, which is the one thing this section must
# never do.
_STRUGGLE_THEMES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"overcommit|too much|saying yes|say yes|over-?schedul|spread thin|overbook", re.I), "full_day"),
    (re.compile(r"procrastinat|put(ting)? (things|it) off|start(ing)?|avoid", re.I), "slipped"),
    (re.compile(r"e-?mail|inbox|repl(y|ies)|follow[- ]?up|forget", re.I), "mail_waiting"),
    (re.compile(r"focus|distract|scatter|deep work|concentrat", re.I), "clear_day"),
    (re.compile(r"sleep|late night|night owl|morning|wak(e|ing) up|tired|rest", re.I), "early_start"),
)


def _oxford(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def keeping_in_mind(profile: dict, now: float, *, events_today: int = 0,
                    first_event_hour: int | None = None, overdue_reminders: int = 0,
                    mail_from_people: int = 0, people_in_events: list[str] = (),
                    people_in_mail: list[str] = (), people_in_messages: list[str] = (),
                    carried_over: list[dict] = ()) -> str:
    """The brief's personal section, or "" when there is nothing honest to say.

    Deterministic by design: it states the user's own words back to them next
    to a fact about today. At most one goal line, one people line and two
    struggle lines, so it stays a glance, not a sermon.
    """
    if not has_content(profile) and not carried_over:
        return ""
    lines: list[str] = []
    goal = goal_in_focus(profile, now)
    if goal:
        behind = ((profile.get("goal_status") or {}).get(goal) or {}).get("status") == "behind"
        lines.append(f"- You're working toward: {goal}" + (" (you said you're behind; this week leads with it)" if behind else ""))
    if carried_over:
        titles = [str(c.get("title") or "") for c in carried_over if c.get("title")]
        shown = "; ".join(titles[:3]) + (f"; and {len(titles) - 3} more" if len(titles) > 3 else "")
        plural = "s" if len(carried_over) != 1 else ""
        lines.append(f"- {len(carried_over)} task{plural} carried over from before: {shown}. Already on today's plan.")
    people_lines = []
    if people_in_events:
        people_lines.append(f"time with {_oxford(list(people_in_events))} on your calendar")
    if people_in_mail:
        people_lines.append(f"mail from {_oxford(list(people_in_mail))}")
    if people_in_messages:
        people_lines.append(f"texts from {_oxford(list(people_in_messages))}")
    if people_lines:
        lines.append("- Today has " + _oxford(people_lines) + ".")

    fired: set[str] = set()
    for struggle in profile.get("struggles") or []:
        if len(fired) >= 2:
            break
        for pattern, theme in _STRUGGLE_THEMES:
            if theme in fired or not pattern.search(struggle):
                continue
            quoted = struggle.rstrip(".")
            line = ""
            if theme == "full_day" and events_today >= 5:
                line = (f"- {events_today} events today. You said \"{quoted}\" gets in your way; "
                        "this may be a day to protect, not add to.")
            elif theme == "slipped" and overdue_reminders > 0:
                plural = "s" if overdue_reminders != 1 else ""
                line = (f"- {overdue_reminders} reminder{plural} slipped past their time. You "
                        f"mentioned \"{quoted}\"; one small start counts.")
            elif theme == "mail_waiting" and mail_from_people > 0:
                plural = "s" if mail_from_people != 1 else ""
                line = (f"- {mail_from_people} email{plural} from people are waiting. You said "
                        f"\"{quoted}\"; a ten-minute pass would clear the important ones.")
            elif theme == "clear_day" and events_today == 0:
                target = f" for {goal}" if goal else ""
                line = (f"- Your calendar is clear. You said \"{quoted}\" is hard; this is a "
                        f"good day to block time{target}.")
            elif theme == "early_start" and first_event_hour is not None and first_event_hour < 9:
                line = (f"- Your first event is before 9. You mentioned \"{quoted}\"; "
                        "be kind to yourself about the start.")
            if line:
                lines.append(line)
                fired.add(theme)
            break
    if not lines:
        return ""
    return "**🧭 Keeping in mind**\n" + "\n".join(lines)


def greeting_name(profile: dict | None = None) -> str:
    profile = load() if profile is None else profile
    return profile.get("name") or ""


# ---------------------------------------------------------------------------
# Use 3: Today's "Needs you" order


def prioritize(items: list[dict], profile: dict | None = None) -> list[dict]:
    """Raise items that name one of the user's people to the top priority and
    say why. Items are copied, never mutated; order is left to the caller's sort."""
    profile = load() if profile is None else profile
    people = profile.get("people") or []
    if not people:
        return items
    patterns = [(p["name"], _word_pattern(p["name"])) for p in people]
    out = []
    for item in items:
        text = " ".join(str(item.get(k) or "") for k in ("title", "quote"))
        names = [name for name, pattern in patterns if pattern and pattern.search(text)]
        if names and item.get("source") not in ("calendar", "wisp"):
            item = {**item, "priority": 1,
                    "reason": f"From {_oxford(names)}, who you said matters to you. " + str(item.get("reason") or ""),
                    "profile_people": names}
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# What the user sees at the end of the flow


def preview(profile: dict | None = None) -> list[str]:
    """Plain sentences for the last screen: exactly what Wisp will now do with
    the answers. Each one corresponds to a real code path above."""
    profile = load() if profile is None else profile
    lines: list[str] = []
    if profile.get("name"):
        lines.append(f"I'll call you {profile['name']}, and your brief will greet you by name.")
    if profile.get("goals"):
        lines.append("Each morning's brief keeps one of your goals in view, taking turns.")
    if profile.get("people"):
        names = _oxford([p["name"] for p in profile["people"]][:3])
        more = len(profile["people"]) - 3
        tail = f" and {more} more" if more > 0 else ""
        # Exactly what keeping_in_mind() does; nothing about ranking, which the
        # notch's Needs you does not do with the profile.
        lines.append(f"When {names}{tail} show up on your calendar or in your mail and texts, "
                     "your morning brief points it out. This never changes what I alert you about.")
    if profile.get("struggles"):
        lines.append("When a day shows the pattern you described, your brief says so once, gently, "
                     "with a small next step. Never a reproach.")
    tone = profile.get("tone") or _DEFAULT_CHOICE["tone"]
    detail = profile.get("detail") or _DEFAULT_CHOICE["detail"]
    manner = {"gentle": "gently", "warm": "",
              "direct": "straight to the point"}[tone]
    length = {"short": "keep replies short", "thorough": ""}[detail]
    said = [part for part in (f"talk to you {manner}" if manner else "", length) if part]
    if said:
        lines.append("I'll " + " and ".join(said) + ".")
    if profile.get("boundaries"):
        lines.append("I'll respect what you asked me to be careful about.")
    if profile.get("about") or profile.get("notes"):
        lines.append("What you told me about yourself shapes every answer.")
    if not has_content(profile):
        lines.append("You haven't told me anything yet. Whenever you like, I'm here.")
    return lines
