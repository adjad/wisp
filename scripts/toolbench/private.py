"""The PRIVATE real-use eval: toolbench cases and a frozen world built from a user's own exports.

This module holds NO personal data and must never gain any. It reads everything from a directory named by
the environment variable PRIVATE_EVAL_DIR (the user keeps it under ~/.moe/evals/private-real-use/) and
fails closed: an unset variable, a missing directory or file, a directory inside a git checkout, or a case
that tries to leave the private split is an error, never a silent fallback to the shipped world or cases.

The directory holds (all written by hand or by a helper OUTSIDE the repo, never committed, never synced,
never uploaded, never copied to the A6000 or into a training build):

    world.json   the frozen world: what the user's sources held when a failure was recorded
    cases.yaml   toolbench cases (scripts/toolbench/cases.py format), loaded as split "private"
    ideal.yaml   optional: per case a scripted ideal trajectory ("ideal") and the recorded failure ("fail"),
                 for `python -m scripts.toolbench.private_selfcheck` (no model, no network)
    runs/        results of `run_toolbench.py --private` (they quote the user's data, so they stay here)
    <person>/    optional per-person subfolders for another tester's exports (README.md in the directory)

The private split is sealed: `select()` leaves it out of every unfiltered selection, `HELD_OUT_SPLITS` does not
include it, so `--export-shingles` never writes a hash of a private prompt (the shingle file travels to the
training-data builder; nothing derived from a private prompt may). The bar it feeds is "candidate >= base"
(training/ask-lora-r4/EVAL-session.md).

world.json format (every key optional except `today`; dates are ISO strings, clocks "2:00 PM" or "14:00"):

    {"today": "2026-11-02", "now": "17:57",
     "summary": "One event today ...", "cards": [{"title": "...", "why": "..."}],
     "events":    [{"title", "day", "time", "all_day", "location", "calendar", "account", "invitees"}],
     "reminders": [{"title", "due" (date or null), "time", "created"}],
     "emails":    [{"id", "from", "account", "subject", "day", "time", "unread", "body", "cat"}],
     "messages":  [{"from" (the conversation), "sender", "text", "day", "time", "unread", "group"}],
     "contacts":  [{"name", "full", "email", "phone"}],
     "brief":     [{"title", "source" (mail|messages|notes), "who", "due", "clock" (null = a date only),
                    "due_label"}],
     "waiting":   [{"who", "source", "needs_reply", "summary", "title"}]}

The world starts from the canonical synthetic world (so every fake finds the state it expects) and REPLACES the
user-visible sources with the spec's; what the spec omits is empty, not canonical.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, datetime, time as dtime
from pathlib import Path

import yaml

ENV_VAR = "PRIVATE_EVAL_DIR"
SPLIT = "private"
CATEGORY = "private"
RUNS_DIRNAME = "runs"

_REPO_ROOT = Path(__file__).resolve().parents[2]

_WORLD_KEYS = {"today", "now", "summary", "cards", "events", "reminders", "emails", "messages", "contacts",
               "brief", "waiting"}
_MAIL_CATS = {"attention", "people", "accounts", "social", "news"}
_BRIEF_SOURCES = {"mail", "messages", "notes"}


class PrivateEvalError(RuntimeError):
    """The private eval cannot run as asked. Always fatal: there is no fallback to other data."""


# ------------------------------------------------------------------ where the data lives

def _inside_git_checkout(path: Path) -> bool:
    return any((p / ".git").exists() for p in (path, *path.parents))


def private_dir(environ=None) -> Path:
    """The private directory from PRIVATE_EVAL_DIR, or an error. Never defaults, never guesses."""
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_VAR) or "").strip()
    if not raw:
        raise PrivateEvalError(f"{ENV_VAR} is not set: the private eval reads only from the directory it names "
                               "(keep it outside the repo, e.g. ~/.moe/evals/private-real-use)")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise PrivateEvalError(f"{ENV_VAR}={raw!r} is not a directory")
    if _REPO_ROOT in (path, *path.parents) or _inside_git_checkout(path):
        raise PrivateEvalError(f"{ENV_VAR} points inside a git checkout ({path}): personal data must never "
                               "live in a repository")
    return path


def require_outside_checkout(path: Path, what: str) -> Path:
    """`path` (a results directory, say) resolved, refused when it is inside the repo or any git checkout."""
    path = Path(path).expanduser().resolve()
    if _REPO_ROOT in (path, *path.parents) or _inside_git_checkout(path):
        raise PrivateEvalError(f"{what} {path} is inside a git checkout: private-eval output stays outside the repo")
    return path


def runs_dir(directory: Path) -> Path:
    return directory / RUNS_DIRNAME


# ------------------------------------------------------------------ cases

def load_private_cases(directory: Path | None = None) -> list:
    """cases.yaml as Case objects of category and split "private".

    A case may not name another split (a private case must not slide into gate and from there into the shingle
    export), and its id may not collide with a shipped case id.
    """
    from scripts.toolbench.cases import load_cases, parse_case
    directory = directory or private_dir()
    path = directory / "cases.yaml"
    if not path.is_file():
        raise PrivateEvalError(f"{path} is missing")
    raw_cases = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw_cases, list) or not raw_cases:
        raise PrivateEvalError(f"{path} holds no cases")
    shipped = {c.id for c in load_cases()}
    out, seen = [], set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise PrivateEvalError(f"{path}: every case is a mapping")
        if raw.get("split", SPLIT) != SPLIT:
            raise PrivateEvalError(f"{path}: case {raw.get('id')!r} sets split {raw.get('split')!r}; "
                                   f"private cases are split {SPLIT!r} only")
        case = replace(parse_case({**raw, "split": SPLIT}, CATEGORY), split=SPLIT)
        if case.id in seen or case.id in shipped:
            raise PrivateEvalError(f"{path}: duplicate or shipped case id {case.id!r}")
        seen.add(case.id)
        out.append(case)
    return out


def load_plans(directory: Path | None = None) -> dict[str, dict]:
    """ideal.yaml: {case id: {"ideal": [plan entries], "fail": [plan entries] | [[...], ...], "why": str}}."""
    directory = directory or private_dir()
    path = directory / "ideal.yaml"
    if not path.is_file():
        raise PrivateEvalError(f"{path} is missing (the self-check needs a scripted ideal per case)")
    raw = yaml.safe_load(path.read_text()) or []
    return {str(e["id"]): e for e in raw}


# ------------------------------------------------------------------ the world

def load_world_spec(directory: Path | None = None) -> dict:
    directory = directory or private_dir()
    path = directory / "world.json"
    if not path.is_file():
        raise PrivateEvalError(f"{path} is missing")
    spec = json.loads(path.read_text())
    if not isinstance(spec, dict) or "today" not in spec:
        raise PrivateEvalError(f"{path}: a JSON object with at least 'today'")
    if extra := set(spec) - _WORLD_KEYS:
        raise PrivateEvalError(f"{path}: unknown keys {sorted(extra)}")
    return spec


def _date(value, where: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise PrivateEvalError(f"{where}: {value!r} is not an ISO date") from None


def _clock(value, where: str) -> dtime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().upper()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise PrivateEvalError(f"{where}: {value!r} is not a clock time")


def _need(entry: dict, keys: tuple[str, ...], where: str) -> None:
    if missing := [k for k in keys if k not in entry]:
        raise PrivateEvalError(f"{where}: missing {missing}")


def _label(due: date, clock: dtime | None) -> str:
    base = f"{due:%a}"
    return f"{base} {clock.strftime('%-I:%M %p')}" if clock else base


def build_private_world(spec: dict):
    """A toolbench World whose sources are the spec's. The canonical world supplies everything the spec does
    not (the fakes' other state), then the user-visible sources are replaced."""
    from scripts.toolbench import world as W
    today = _date(spec["today"], "today")
    w = W.build_world(today)
    now_clock = _clock(spec.get("now") or "08:30", "now")
    w.now = datetime.combine(today, now_clock)

    w.events, w.past_events = [], []
    for i, e in enumerate(spec.get("events") or ()):
        where = f"events[{i}]"
        _need(e, ("title", "day"), where)
        day = _date(e["day"], where)
        ev = W._event(e["title"], day, e.get("time", "9:00 AM"), e.get("location", ""),
                      account=e.get("account", ""), calendar=e.get("calendar", "Home"),
                      writable=bool(e.get("writable", True)), invitees=int(e.get("invitees", 0)),
                      all_day=bool(e.get("all_day")), duration=int(e.get("duration", 60)))
        ev["account"] = {"work": "Work"}.get(ev["account"], ev["account"]) or "iCloud"
        (w.past_events if day < today else w.events).append(ev)

    w.reminders = []
    for i, r in enumerate(spec.get("reminders") or ()):
        where = f"reminders[{i}]"
        _need(r, ("title",), where)
        due = _date(r["due"], where) if r.get("due") else None
        created = _date(r["created"], where) if r.get("created") else today
        w.reminders.append(W._reminder(r["title"], due, r.get("time", ""), created=created))
    for i, r in enumerate(w.reminders):
        r["seq"] = r["useq"] = i
    w.next_id["rseq"] = len(w.reminders)

    w.emails = []
    for i, m in enumerate(spec.get("emails") or ()):
        where = f"emails[{i}]"
        _need(m, ("id", "from", "subject", "day", "time", "body"), where)
        cat = m.get("cat", "people")
        if cat not in _MAIL_CATS:
            raise PrivateEvalError(f"{where}: cat must be one of {sorted(_MAIL_CATS)}")
        row = W._mail(str(m["id"]), m["from"], m.get("account", "personal"), m["subject"],
                      (_date(m["day"], where) - today).days, m["time"], bool(m.get("unread", True)),
                      m["body"], cat, bulk=bool(m.get("bulk")))
        w.emails.append(row)

    w.messages = []
    for i, m in enumerate(spec.get("messages") or ()):
        where = f"messages[{i}]"
        _need(m, ("from", "sender", "text", "day", "time"), where)
        w.messages.append(W._text(m["from"], m["sender"], m["text"], (_date(m["day"], where) - today).days,
                                  m["time"], bool(m.get("unread", False)), group=bool(m.get("group"))))
    for row in w.emails + w.messages:
        row["day"] = W._day(row["day_offset"], today)

    w.contacts = [{"name": c["name"], "full": c.get("full", c["name"]), "email": c.get("email", ""),
                   "phone": c.get("phone", "")} for c in spec.get("contacts") or ()]
    w.notes = []

    w.brief = []
    for i, b in enumerate(spec.get("brief") or ()):
        where = f"brief[{i}]"
        _need(b, ("title", "source", "due"), where)
        if b["source"] not in _BRIEF_SOURCES:
            raise PrivateEvalError(f"{where}: source must be one of {sorted(_BRIEF_SOURCES)}")
        due, clock = _date(b["due"], where), _clock(b.get("clock"), where)
        w.brief.append({"title": b["title"], "source": b["source"], "who": b.get("who", ""), "due": due,
                        "clock": clock, "due_label": b.get("due_label") or _label(due, clock)})
    w.waiting = []
    for i, t in enumerate(spec.get("waiting") or ()):
        where = f"waiting[{i}]"
        _need(t, ("who", "source", "summary"), where)
        w.waiting.append({"who": t["who"], "source": t["source"], "needs_reply": bool(t.get("needs_reply", True)),
                          "summary": t["summary"], "title": t.get("title", t["summary"])})
    w.cards = [dict(c) for c in spec.get("cards") or ()]
    w.summary = spec.get("summary") or ""

    # The canonical world's remembered facts, profile, routines and sends are not this user's.
    w.memory_facts, w.profile, w.patterns, w.predictions = [], {}, [], []
    w.scheduled_sends, w.timers, w.drive_files, w.conversations = [], [], [], []
    w.snapshot()
    return w


def private_world(directory: Path | None = None):
    directory = directory or private_dir()
    return build_private_world(load_world_spec(directory))
