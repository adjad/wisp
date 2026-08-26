"""A private, local journal — distinct from Notes.app on purpose.

`create_note`/`append_note` already give the user a durable, Finder/iCloud-
visible note. This is the opposite: a quick, timestamped, append-only entry
that never leaves this Mac and was never meant to be a document — a workout
log, a mood check-in, a "dear diary" line. SQLite-backed like `memory/facts.py`
for the same reason that module gives (stdlib only, single-user, no server).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from service.paths import MOE_DIR
from service.tools.registry import register

_DB_PATH = MOE_DIR / "log.db"
_db: sqlite3.Connection | None = None


def _conn() -> sqlite3.Connection:
    global _db
    if _db is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _db = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute(
            "CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, "
            "category TEXT, text TEXT, created_at REAL)")
        _db.execute(
            "CREATE TABLE IF NOT EXISTS goals (key TEXT PRIMARY KEY, value TEXT, updated_at REAL)")
        _db.commit()
    return _db


@register(
    "log_entry",
    "Add a quick private journal entry — a mood check-in, a workout note, "
    "anything the user wants a timestamped record of without it being a real "
    "Note. Never synced anywhere; stays on this Mac.",
    {"type": "object",
     "properties": {
         "text": {"type": "string", "description": "The entry."},
         "category": {"type": "string", "description": "Optional tag, e.g. 'mood', 'workout', 'journal'."},
     },
     "required": ["text"]},
    category="assistant_write",
    aliases=["log that I'm feeling good today", "journal entry: had a great run",
             "add to my log that I finished the workout", "quick log entry"],
)
def log_entry(text: str, category: str = "journal") -> str:
    txt = (text or "").strip()
    if not txt:
        return "(error: log_entry needs `text`.)"
    cat = (category or "journal").strip().lower()
    db = _conn()
    db.execute("INSERT INTO entries (category, text, created_at) VALUES (?,?,?)",
              (cat, txt, time.time()))
    db.commit()
    return f"Logged ({cat})."


@register(
    "read_log",
    "Read back recent private journal entries, optionally filtered by "
    "category or how many days back.",
    {"type": "object",
     "properties": {
         "category": {"type": "string", "description": "Only entries tagged with this. Omit for all."},
         "days": {"type": "integer", "description": "How many days back. Default 30."},
     }},
    category="assistant_read",
    aliases=["show me my journal entries", "what did I log this week",
             "read back my workout log", "show my mood entries"],
)
def read_log(category: str = "", days: int = 30) -> str:
    try:
        window = max(1, min(3650, int(days)))
    except (TypeError, ValueError):
        window = 30
    cutoff = time.time() - window * 86400
    db = _conn()
    if category.strip():
        rows = db.execute(
            "SELECT * FROM entries WHERE category=? AND created_at>=? ORDER BY created_at DESC",
            (category.strip().lower(), cutoff)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM entries WHERE created_at>=? ORDER BY created_at DESC",
            (cutoff,)).fetchall()
    if not rows:
        scope = f" tagged {category!r}" if category.strip() else ""
        return f"No log entries{scope} in the last {window} days."
    import datetime

    lines = [f"  [{datetime.datetime.fromtimestamp(r['created_at']):%b %-d %-I:%M%p}] "
            f"({r['category']}) {r['text']}" for r in rows[:40]]
    more = f"\n  (+{len(rows) - 40} more)" if len(rows) > 40 else ""
    return f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}:\n" + "\n".join(lines) + more


@register(
    "set_fitness_goal",
    "Set or update a personal goal to track — a step count target, a weekly "
    "workout count, anything with a target number. Stored locally.",
    {"type": "object",
     "properties": {
         "name": {"type": "string", "description": "What the goal is, e.g. 'daily steps', 'workouts per week'."},
         "target": {"type": "string", "description": "The target value, e.g. '10000', '4'."},
     },
     "required": ["name", "target"]},
    category="assistant_write",
    aliases=["set a goal of 10000 steps a day", "I want to work out 4 times a week",
             "track my goal of running 3 times a week"],
)
def set_fitness_goal(name: str, target: str) -> str:
    n, t = (name or "").strip().lower(), (target or "").strip()
    if not n or not t:
        return "(error: set_fitness_goal needs both `name` and `target`.)"
    db = _conn()
    db.execute(
        "INSERT INTO goals (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (n, t, time.time()))
    db.commit()
    return f"Goal set: {n} = {t}."


# Health logging is NOT a separate store — it's the same journal above with a
# recognized category. "log_entry(text='128 lbs', category='weight')" IS how
# you log a weigh-in; this tool just summarizes those categories together
# instead of the user having to call read_log five times (weight, water,
# meals, meds, mood).
_HEALTH_CATEGORIES = ("weight", "water", "meal", "medication", "mood", "workout")


@register(
    "health_summary",
    "Summarize recent health logging — weight, water, meals, medication, "
    "mood, workouts — across all categories at once, most recent first. Log "
    "entries with `log_entry` using one of these categories; this just reads "
    "them back together.",
    {"type": "object",
     "properties": {"days": {"type": "integer", "description": "How many days back. Default 7."}}},
    category="assistant_read",
    aliases=["give me a health summary", "how have I been doing health-wise this week",
             "recap my health logging", "how's my weight and water intake trending"],
)
def health_summary(days: int = 7) -> str:
    try:
        window = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        window = 7
    cutoff = time.time() - window * 86400
    db = _conn()
    placeholders = ",".join("?" * len(_HEALTH_CATEGORIES))
    rows = db.execute(
        f"SELECT * FROM entries WHERE category IN ({placeholders}) AND created_at>=? "
        f"ORDER BY created_at DESC", (*_HEALTH_CATEGORIES, cutoff)).fetchall()
    if not rows:
        return (f"No health logging in the last {window} days. Log entries with "
                f"log_entry, using category: {', '.join(_HEALTH_CATEGORIES)}.")
    import datetime
    from collections import defaultdict

    by_cat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    lines = []
    for cat in _HEALTH_CATEGORIES:
        entries = by_cat.get(cat)
        if not entries:
            continue
        lines.append(f"{cat.title()} ({len(entries)}):")
        for r in entries[:5]:
            when = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%b %-d")
            lines.append(f"  [{when}] {r['text']}")
    return "\n".join(lines)
