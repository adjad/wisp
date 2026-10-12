"""Wisp-private fictional fixtures. Never publish to native caches or databases."""
from __future__ import annotations
from datetime import datetime, timedelta, time as clock
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

_FLAG = os.environ.get("WISP_FOUNDER_DEMO")
_REMOTE_FLAG = os.environ.get("WISP_REMOTE_ONLY")
PROVENANCE = "FICTIONAL FOUNDER DEMO — Wisp-only sample data, not your Mac's Notes, Reminders or Contacts."
ZONE = ZoneInfo("America/Los_Angeles")
TOOL_DESCRIPTIONS = {
    "search_notes": "Search fictional founder-demo notes by title, body or topic. Read-only Wisp sample data, never native Notes. Pass query, period/day or count as needed.",
    "lookup_contact": "Look up one fictional founder-demo contact by name. Returns example.com/55501xx sample details, never a real Mac contact. Read-only; sending is unavailable.",
    "list_contacts": "List fictional founder-demo contacts with roles and organizations. Optional query filters names, roles or organizations. Never reads or changes native Contacts.",
    "search_reminders": "Search fictional founder-demo reminders by title/details with scope today, tomorrow, past_due, upcoming or all. Read-only Wisp sample data, never Apple Reminders.",
    "get_upcoming": "Read the Intel Mac's real Calendar plus clearly marked fictional founder-demo reminders. calendar_only excludes sample reminders. Native Calendar remains the sole source of real events.",
}

@lru_cache(maxsize=1)
def enabled() -> bool:
    if _FLAG is None:
        return False
    if _FLAG != "1" or _REMOTE_FLAG != "1":
        raise ValueError("Founder fixtures require the forced remote-only preview")
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is None:
        raise ValueError("Founder fixtures require the fixed remote-only profile")
    _wire()
    return True

@lru_cache(maxsize=1)
def _wire() -> str:
    raw = Path(__file__).with_name("founder-demo.json").read_bytes()
    if len(raw) > 256000:
        raise ValueError("Founder fixture resource exceeds its bound")
    data = json.loads(raw)
    if data.get("schema_version") != 1 or data.get("synthetic") is not True:
        raise ValueError("Founder fixtures must explicitly be fictional")
    groups = ("notes", "reminders", "contacts")
    for key in groups:
        rows = data.get(key)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 40:
            raise ValueError("Invalid founder fixture inventory")
        ids = [r.get("id") for r in rows if isinstance(r, dict)]
        if len(ids) != len(rows) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Invalid founder fixture IDs")
    ids = {k: {r["id"] for r in data[k]} for k in groups}
    for c in data["contacts"]:
        if not isinstance(c.get("email"), str) or not c["email"].lower().endswith("@example.com"):
            raise ValueError("Founder contacts must use example.com")
        if not re.fullmatch(r".*55501\d\d", re.sub(r"\D", "", str(c.get("phone", "")))):
            raise ValueError("Founder contacts must use fictional55501xx numbers")
    for note in data["notes"]:
        if type(note.get("day_offset")) is not int or not -366 <= note["day_offset"] <= 366:
            raise ValueError("Invalid relative note date")
        if not set(note.get("contact_ids", [])) <= ids["contacts"] or not set(note.get("reminder_ids", [])) <= ids["reminders"]:
            raise ValueError("Invalid note references")
    for row in data["reminders"]:
        if (type(row.get("due_day_offset")) is not int or not -366 <= row["due_day_offset"] <= 366
                or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", row.get("due_time", ""))
                or row.get("status") not in {"active", "done"}
                or row.get("priority") not in {"high", "medium", "low"}
                or not set(row.get("note_ids", [])) <= ids["notes"]
                or row.get("owner_contact_id") not in ids["contacts"]):
            raise ValueError("Invalid founder reminder")
    return json.dumps(data, ensure_ascii=False)

def data() -> dict:
    if not enabled():
        raise ValueError("Founder fixtures are disabled")
    return json.loads(_wire())  # caller-owned copies; no mutable shared native view

def _anchor(now: float | None = None):
    return datetime.fromtimestamp(now, ZONE) if now is not None else datetime.now(ZONE)

def note_rows(now: float | None = None) -> list[dict]:
    day = _anchor(now).date()
    return [{**n, "ts": datetime.combine(day + timedelta(days=n["day_offset"]), clock(10), ZONE).timestamp(),
             "folder": "Fictional Founder Demo", "native_id": None} for n in data()["notes"]]

def reminder_rows(now: float | None = None, *, include_done=False) -> list[dict]:
    day = _anchor(now).date()
    out = []
    for r in data()["reminders"]:
        if r["status"] != "active" and not include_done:
            continue
        hour, minute = map(int, r["due_time"].split(":"))
        when = datetime.combine(day + timedelta(days=r["due_day_offset"]), clock(hour, minute), ZONE).timestamp()
        out.append({"id": "founder-demo:" + r["id"], "source_id": r["id"], "source": "founder_demo",
                    "kind": "reminder", "title": "[DEMO] " + r["title"], "context": PROVENANCE + " " + r["detail"],
                    "when_ts": when, "end_ts": None, "all_day": False, "status": r["status"],
                    "priority": r["priority"], "account": "Fictional Founder Demo", "confidence": 1.0,
                    "native_id": None, "demo": True})
    return sorted(out, key=lambda r: r["when_ts"])

def matching_reminders(query="", scope="all", now: float | None = None) -> list[dict]:
    when = _anchor(now);start = datetime.combine(when.date(), clock(), ZONE).timestamp();end = datetime.combine(when.date() + timedelta(days=1), clock(), ZONE).timestamp()
    if scope not in {"all", "today", "tomorrow", "past_due", "upcoming"}:
        raise ValueError("Unknown reminder scope")
    q = (query or "").casefold().strip()
    return [r for r in reminder_rows(now) if (not q or q in (r["title"] + " " + r["context"]).casefold()) and
            (scope == "all" or scope == "today" and start <= r["when_ts"] < end or
             scope == "tomorrow" and end <= r["when_ts"] < datetime.combine(when.date() + timedelta(days=2), clock(), ZONE).timestamp() or
             scope == "past_due" and r["when_ts"] < when.timestamp() or
             scope == "upcoming" and r["when_ts"] >= when.timestamp())]

def reminder_text(query="", scope="all") -> str:
    rows = matching_reminders(query, scope)
    return PROVENANCE + "\n" + ("\n".join(f"- {r['title']} — {_anchor(r['when_ts']):%a %b %d at %I:%M %p} ({r['priority']})\n  {r['context'][len(PROVENANCE):].strip()}" for r in rows) or "No fictional reminder matches this query.")

def notes_text(query=None, count=None, period=None, day=None) -> str:
    rows = note_rows()
    if period or day:
        from service.tools.timeranges import resolve_span
        start, end, _ = resolve_span(period or day)
        rows = [r for r in rows if start <= r["ts"] < end]
    terms = re.findall(r"[\w]+", (query or "").casefold())
    stop = {"my", "the", "notes", "note", "about", "show", "find", "recent", "all"}
    terms = [t for t in terms if t not in stop]
    rows = [r for r in rows if all(t in (r["title"] + " " + r["body"] + " " + " ".join(r["tags"])).casefold() for t in terms)]
    limit = max(1, min(12, int(count or 6)));rows.sort(key=lambda r:r["ts"], reverse=True)
    return PROVENANCE + "\n" + ("\n\n".join(f"## {r['title']}\nFolder: Fictional Founder Demo\n{r['body']}" for r in rows[:limit]) or "No fictional note matches this query.")

def contacts(query="", *, lookup=False) -> str:
    q = (query or "").casefold().strip();rows = data()["contacts"]
    def name(c):return (c["given_name"] + " " + c["family_name"]).strip()
    if q:
        exact = [c for c in rows if name(c).casefold() == q]
        rows = exact or [c for c in rows if q in (name(c) + " " + c["role"] + " " + c["organization"]).casefold()]
    rows.sort(key=name)
    if not rows:return PROVENANCE + "\nNo fictional contact matches; real Mac contacts were not consulted."
    if lookup:
        return PROVENANCE + "\n" + "\n\n".join(f"{name(c)} — {c['role']}, {c['organization']}\nSample email: {c['email']} | Sample phone: {c['phone']}\n{c['relationship']}: {c['context']}" for c in rows[:8])
    return PROVENANCE + f"\n{len(rows)} fictional contacts:\n" + "\n".join(f"- {name(c)} — {c['role']}, {c['organization']}" for c in rows)
