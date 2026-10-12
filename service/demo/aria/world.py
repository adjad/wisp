"""The Aria demo's synthetic, writable world: Mail, Messages, Notes, Reminders,
Contacts and Calendar for the fictional founder Aria Solano (Cedarloop).

One SQLite file under the demo state directory (``<WISP_HOME>/aria-demo/
aria-world.db``, see service.paths.state_dir), seeded from:

* ``service/demo/founder-demo.json`` — notes, reminders, contacts;
* ``service/behavior/persona.py`` — calendar events and message history
  (the same persona the demo's behavior engine learns from);
* ``service/demo/aria/*.json`` — mail threads, extra message threads, extra
  contacts and scripted reply templates (see data.py for the schema).

Every write the demo performs lands here and nowhere else: a "sent" email or
message is appended to its synthetic thread, and a deterministic scripted
reply from a known contact becomes visible a few seconds later. ``reset()``
re-seeds everything relative to the current day.

Nothing in this module touches the network, AppleScript, EventKit or any
native store; it is pure stdlib + SQLite.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, time as clock, timedelta
from pathlib import Path
from typing import Any, Iterable

from service.demo.aria import data as aria_data

DB_NAME = "aria-world.db"
SCHEMA_VERSION = 2
REPLY_DELAY_S = 6.0
USER_NAME = "Aria Solano"
USER_EMAIL = "aria.solano@example.com"
USER_PHONE = "+1-202-555-0100"
CALENDAR_NAME = "Wisp Demo"


def _delay(template: dict) -> float:
    """Scripted replies arrive after the template's delay (25-84 s in the
    dataset), capped so a live demo never waits long; ``REPLY_DELAY_S`` is
    the floor."""
    try:
        return max(REPLY_DELAY_S, min(float(template.get("delay_seconds") or 0), 90.0))
    except (TypeError, ValueError):
        return REPLY_DELAY_S


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")[-10:]


class World:
    def __init__(self, path: Path, *, clock_fn=time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock_fn
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT,
                organization TEXT, email TEXT, phone TEXT, relationship TEXT, context TEXT);
            CREATE TABLE IF NOT EXISTS mail(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, folder TEXT NOT NULL,
                from_name TEXT, from_addr TEXT, to_addrs TEXT, cc_addrs TEXT, subject TEXT, body TEXT,
                ts REAL NOT NULL, unread INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0, demo_action TEXT);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, contact TEXT NOT NULL,
                handle TEXT, direction TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL,
                unread INTEGER NOT NULL DEFAULT 0, demo_action TEXT, sender TEXT);
            CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                folder TEXT, ts REAL NOT NULL, tags TEXT);
            CREATE TABLE IF NOT EXISTS reminders(id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT,
                due_ts REAL, priority TEXT, status TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'reminder');
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, title TEXT NOT NULL, start_ts REAL NOT NULL,
                end_ts REAL, location TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS scheduled(id TEXT PRIMARY KEY, channel TEXT NOT NULL, recipient TEXT NOT NULL,
                subject TEXT, body TEXT NOT NULL, due_ts REAL NOT NULL, status TEXT NOT NULL, created_ts REAL NOT NULL);
        """)
        self._db.commit()
        if self.meta("schema_version") != str(SCHEMA_VERSION):
            # An older layout: start over (demo data only, re-seeded).
            self._db.executescript("DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS mail;")
            self._db.executescript("""
                CREATE TABLE messages(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, contact TEXT NOT NULL,
                    handle TEXT, direction TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL,
                    unread INTEGER NOT NULL DEFAULT 0, demo_action TEXT, sender TEXT);
                CREATE TABLE mail(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, folder TEXT NOT NULL,
                    from_name TEXT, from_addr TEXT, to_addrs TEXT, cc_addrs TEXT, subject TEXT, body TEXT,
                    ts REAL NOT NULL, unread INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0, demo_action TEXT);
            """)
            self.reset()

    # ---- infrastructure ---------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._db.close()

    def meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def _q(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(args))]

    def _new_id(self, prefix: str, *parts: Any) -> str:
        raw = json.dumps([prefix, self.clock(), *map(str, parts)])
        return f"{prefix}:{hashlib.sha256(raw.encode()).hexdigest()[:12]}"

    # ---- seeding -----------------------------------------------------------
    def reset(self, now: float | None = None) -> dict:
        """Re-seed every synthetic source relative to ``now``'s local day."""
        now = self.clock() if now is None else now
        bundle = aria_data.load()
        tz = _local_tz()
        day = datetime.fromtimestamp(now, tz).date()

        def at(offset: int, hhmm: str = "10:00") -> float:
            h, m = map(int, hhmm.split(":"))
            return datetime.combine(day + timedelta(days=int(offset)), clock(h, m), tz).timestamp()

        from service.behavior.persona import generate_persona
        from service.behavior.timeutil import resolve_tz, tz_name
        persona = generate_persona(now, tz=tz_name(resolve_tz(None)))
        with self._lock, self._db:
            for table in ("contacts", "mail", "messages", "notes", "reminders", "events", "scheduled"):
                self._db.execute(f"DELETE FROM {table}")
            founder = bundle["founder"]
            for c in founder["contacts"] + bundle["contacts_extra"]:
                name = c.get("name") or f"{c.get('given_name', '')} {c.get('family_name', '')}".strip()
                self._db.execute("INSERT OR REPLACE INTO contacts VALUES(?,?,?,?,?,?,?,?)",
                                 (c["id"], name, c.get("role"), c.get("organization"), c.get("email"),
                                  c.get("phone"), c.get("relationship"), c.get("context")))
            for n in founder["notes"]:
                self._db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?)",
                                 (n["id"], n["title"], n["body"], "Cedarloop", at(n["day_offset"]),
                                  json.dumps(n.get("tags", []))))
            for r in founder["reminders"]:
                self._db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,?)",
                                 (r["id"], r["title"], r.get("detail"), at(r["due_day_offset"], r["due_time"]),
                                  r.get("priority"), "done" if r.get("status") == "done" else "active", "reminder"))
            for e in persona.calendar_events:
                self._db.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?)",
                                 (e["demo_id"], e["title"], e["start_ts"], e.get("end_ts"),
                                  e.get("location") or "", e.get("notes") or "", "active"))
            # Mail and Messages come from the dataset (consistent with the
            # persona's rhythm); dates move by whole weeks to stay fresh.
            shift = aria_data.week_shift(bundle["as_of"], now)
            for thread in bundle["mail_threads"]:
                for m in thread["messages"]:
                    folder = "sent" if m["mailbox"] == "sent" else "inbox"
                    self._db.execute(
                        "INSERT INTO mail VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (m["id"], thread["id"], folder, m["from"]["name"], m["from"]["email"],
                         json.dumps([t["email"] for t in m["to"]]), json.dumps([t["email"] for t in m["cc"]]),
                         m["subject"] or thread["subject"], m["body"], m["ts"] + shift, int(m["unread"]),
                         int(m["flagged"]), int(m["mailbox"] == "archive"), None))
            by_id = {c["id"]: c for c in self._q("SELECT * FROM contacts")}
            for chat in bundle["chats"]:
                others = [p for p in chat["participants"] if p != "me"]
                handle = None
                if not chat["is_group"] and others and others[0] in by_id:
                    handle = by_id[others[0]].get("phone") or by_id[others[0]].get("email")
                for m in chat["messages"]:
                    sender = "me" if m["sender"] == "me" else (by_id.get(m["sender"], {}).get("name") or m["sender"])
                    self._db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                                     (m["id"], chat["id"], chat["name"], handle, m["direction"], m["text"],
                                      m["ts"] + shift, int(m["unread"]), None, sender))
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            self._set_meta("seeded_at", str(now))
            self._set_meta("calendar_day", day.isoformat())
        return self.counts()

    def _chat_for(self, contact: dict | None, name: str) -> tuple[str, str]:
        """(thread id, chat name) for a one-to-one chat with ``contact``."""
        if contact:
            for row in self._q("SELECT DISTINCT thread_id, contact FROM messages WHERE handle IN (?,?)",
                               (contact.get("phone") or "", contact.get("email") or "")):
                return row["thread_id"], row["contact"]
        return "chat:" + re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-"), name

    def refresh_calendar(self, now: float | None = None) -> int:
        """Once per local day, add the persona's calendar for the current
        window (the behavior engine re-plants its next gym conflict and
        back-to-back run daily). Existing rows, including events the demo
        cancelled or added, are kept as they are."""
        now = self.clock() if now is None else now
        today = datetime.fromtimestamp(now, _local_tz()).date().isoformat()
        if self.meta("calendar_day") == today:
            return 0
        from service.behavior.persona import generate_persona
        from service.behavior.timeutil import resolve_tz, tz_name
        persona = generate_persona(now, tz=tz_name(resolve_tz(None)))
        added = 0
        with self._lock, self._db:
            for e in persona.calendar_events:
                added += self._db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                                          (e["demo_id"], e["title"], e["start_ts"], e.get("end_ts"),
                                           e.get("location") or "", e.get("notes") or "", "active")).rowcount
            self._set_meta("calendar_day", today)
        return added

    def _insert_message(self, mid, contact, handle, direction, text, ts, *, unread, demo_action=None,
                        thread=None, sender=None):
        thread = thread or "chat:" + re.sub(r"[^a-z0-9]+", "-", contact.casefold()).strip("-")
        self._db.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (mid, thread, contact, handle, direction, text, ts, int(unread), demo_action,
                          sender or ("me" if direction == "out" else contact)))

    def counts(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        one = lambda sql, *a: self._q(sql, a)[0]["n"]  # noqa: E731
        return {
            "mail_threads": one("SELECT COUNT(DISTINCT thread_id) n FROM mail WHERE ts<=?", now),
            "mail_messages": one("SELECT COUNT(*) n FROM mail WHERE ts<=?", now),
            "message_threads": one("SELECT COUNT(DISTINCT thread_id) n FROM messages WHERE ts<=?", now),
            "messages": one("SELECT COUNT(*) n FROM messages WHERE ts<=?", now),
            "notes": one("SELECT COUNT(*) n FROM notes"),
            "reminders": one("SELECT COUNT(*) n FROM reminders"),
            "active_reminders": one("SELECT COUNT(*) n FROM reminders WHERE status='active'"),
            "contacts": one("SELECT COUNT(*) n FROM contacts"),
            "events": one("SELECT COUNT(*) n FROM events WHERE status='active'"),
            "scheduled": one("SELECT COUNT(*) n FROM scheduled WHERE status='pending'"),
        }

    # ---- contacts ----------------------------------------------------------
    def contacts(self, query: str = "") -> list[dict]:
        rows = self._q("SELECT * FROM contacts ORDER BY name")
        q = (query or "").casefold().strip()
        if not q:
            return rows
        exact = [r for r in rows if r["name"].casefold() == q or r["name"].split()[0].casefold() == q
                 or (r.get("role") or "").casefold() == q]
        return exact or [r for r in rows if q in " ".join(str(r.get(k) or "") for k in
                                                          ("name", "role", "organization", "email", "relationship")).casefold()]

    def resolve(self, to: str) -> dict | None:
        """A contact by name, email or phone; None when unknown."""
        to = (to or "").strip()
        if not to:
            return None
        for r in self._q("SELECT * FROM contacts"):
            if "@" in to and (r.get("email") or "").casefold() == to.casefold():
                return r
            if "@" not in to and _norm_phone(to) and _norm_phone(to) == _norm_phone(r.get("phone") or ""):
                return r
        matches = self.contacts(to)
        return matches[0] if len(matches) == 1 else None

    def add_contact(self, name: str, *, email: str = "", phone: str = "") -> dict:
        with self._lock, self._db:
            existing = self._q("SELECT * FROM contacts WHERE lower(name)=lower(?)", (name,))
            if existing:
                c = existing[0]
                self._db.execute("UPDATE contacts SET email=COALESCE(NULLIF(?,''),email), "
                                 "phone=COALESCE(NULLIF(?,''),phone) WHERE id=?", (email, phone, c["id"]))
            else:
                cid = self._new_id("contact", name)
                self._db.execute("INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?)",
                                 (cid, name, "", "", email or None, phone or None, "new", "Added in the demo"))
        return self.contacts(name)[0]

    # ---- mail --------------------------------------------------------------
    def mail(self, *, query: str = "", since: float | None = None, until: float | None = None,
             unread: bool | None = None, folder: str | None = None, count: int = 20,
             include_archived: bool = False, now: float | None = None) -> list[dict]:
        now = self.clock() if now is None else now
        sql, args = "SELECT * FROM mail WHERE ts<=?", [now]
        if not include_archived:
            sql += " AND archived=0"
        if folder:
            sql += " AND folder=?"
            args.append(folder)
        if since is not None:
            sql += " AND ts>=?"
            args.append(since)
        if until is not None:
            sql += " AND ts<?"
            args.append(until)
        if unread is not None:
            sql += " AND unread=?"
            args.append(int(unread))
        rows = self._q(sql + " ORDER BY ts DESC", args)
        terms = [t for t in re.findall(r"\w+", (query or "").casefold()) if len(t) > 2]
        if terms:
            rows = [r for r in rows if all(t in " ".join(str(r.get(k) or "") for k in
                    ("subject", "body", "from_name", "from_addr", "to_addrs")).casefold() for t in terms)]
        return rows[:max(1, min(int(count or 20), 100))]

    def mail_message(self, message_id: str) -> dict | None:
        rows = self._q("SELECT * FROM mail WHERE id=? AND ts<=?", (message_id, self.clock()))
        if rows:
            return rows[0]
        # The model may pass a subject instead of an id.
        rows = self.mail(query=message_id, count=1)
        return rows[0] if rows else None

    def thread(self, thread_id: str) -> list[dict]:
        return self._q("SELECT * FROM mail WHERE thread_id=? AND ts<=? ORDER BY ts", (thread_id, self.clock()))

    def set_mail_flag(self, message_id: str, field: str, value: bool) -> dict | None:
        assert field in {"unread", "flagged", "archived"}
        row = self.mail_message(message_id)
        if row is None:
            return None
        with self._lock, self._db:
            self._db.execute(f"UPDATE mail SET {field}=? WHERE id=?", (int(value), row["id"]))
        return {**row, field: int(value)}

    def send_mail(self, to: str, subject: str, body: str, *, cc: str = "", thread_id: str | None = None,
                  action: str = "send") -> dict:
        now = self.clock()
        recipients = [a.strip() for a in re.split(r"[,;]", to or "") if a.strip()]
        resolved = [self.resolve(a) for a in recipients]
        addrs = [(c.get("email") if c and c.get("email") else a) for a, c in zip(recipients, resolved)]
        thread_id = thread_id or self._new_id("thread", subject, to)
        mid = self._new_id("mail", to, subject)
        with self._lock, self._db:
            self._db.execute("INSERT INTO mail VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (mid, thread_id, "sent", USER_NAME, USER_EMAIL, json.dumps(addrs),
                              json.dumps([c for c in re.split(r"[,;]", cc or "") if c.strip()]),
                              subject, body, now, 0, 0, 0, action))
            contact = next((c for c in resolved if c), None)
            if action == "send" and contact and contact.get("email"):
                template = aria_data.reply_template("mail", contact["id"], body)
                reply = template["text"]
                self._db.execute("INSERT INTO mail VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (mid + ":reply", thread_id, "inbox", contact["name"], contact["email"],
                                  json.dumps([USER_EMAIL]), "[]",
                                  subject if subject.lower().startswith("re:") else f"Re: {subject}",
                                  reply, now + _delay(template), 1, 0, 0, "scripted_reply"))
        return {"id": mid, "thread_id": thread_id, "to": addrs, "subject": subject, "ts": now}

    # ---- messages ----------------------------------------------------------
    def messages(self, *, query: str = "", conversation: str = "", since: float | None = None,
                 until: float | None = None, unread: bool | None = None, count: int = 30,
                 now: float | None = None) -> list[dict]:
        now = self.clock() if now is None else now
        sql, args = "SELECT * FROM messages WHERE ts<=?", [now]
        if since is not None:
            sql += " AND ts>=?"
            args.append(since)
        if until is not None:
            sql += " AND ts<?"
            args.append(until)
        if unread is not None:
            sql += " AND unread=?"
            args.append(int(unread))
        rows = self._q(sql + " ORDER BY ts DESC", args)
        conv = (conversation or "").casefold().strip()
        if conv:
            rows = [r for r in rows if conv in r["contact"].casefold()]
        terms = [t for t in re.findall(r"\w+", (query or "").casefold()) if len(t) > 2]
        if terms:
            rows = [r for r in rows if any(t in (r["text"] + " " + r["contact"]).casefold() for t in terms)]
        return rows[:max(1, min(int(count or 30), 200))]

    def send_message(self, to: str, text: str, *, action: str = "send") -> dict:
        now = self.clock()
        contact = self.resolve(to)
        handle = (contact.get("phone") or contact.get("email")) if contact else to.strip()
        thread, chat_name = self._chat_for(contact, contact["name"] if contact else to.strip())
        mid = self._new_id("msg", to, text)
        with self._lock, self._db:
            self._insert_message(mid, chat_name, handle, "out", text, now, unread=False, demo_action=action,
                                 thread=thread, sender="me")
            if action == "send" and contact:
                reply = aria_data.reply_template("message", contact["id"], text)
                self._insert_message(mid + ":reply", chat_name, handle, "in", reply["text"],
                                     now + _delay(reply), unread=True, demo_action="scripted_reply",
                                     thread=thread, sender=contact["name"])
        return {"id": mid, "to": contact["name"] if contact else chat_name, "handle": handle, "ts": now}

    # ---- notes -------------------------------------------------------------
    def notes(self, query: str = "", *, since: float | None = None, until: float | None = None,
              count: int = 6) -> list[dict]:
        rows = self._q("SELECT * FROM notes ORDER BY ts DESC")
        if since is not None:
            rows = [r for r in rows if r["ts"] >= since and (until is None or r["ts"] < until)]
        stop = {"my", "the", "notes", "note", "about", "show", "find", "recent", "all", "for", "and"}
        terms = [t for t in re.findall(r"\w+", (query or "").casefold()) if t not in stop and len(t) > 1]
        if terms:
            scored = []
            for r in rows:
                hay = (r["title"] + " " + r["body"] + " " + (r.get("tags") or "")).casefold()
                hits = sum(t in hay for t in terms)
                if hits:
                    scored.append((hits, r))
            rows = [r for _, r in sorted(scored, key=lambda x: -x[0])]
        return rows[:max(1, min(int(count or 6), 20))]

    def create_note(self, title: str, body: str = "", folder: str = "") -> dict:
        nid = self._new_id("note", title)
        with self._lock, self._db:
            self._db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?)",
                             (nid, title, body or "", folder or "Cedarloop", self.clock(), "[]"))
        return {"id": nid, "title": title}

    def append_note(self, title: str, text: str) -> dict | None:
        rows = self._q("SELECT * FROM notes WHERE lower(title)=lower(?)", (title,)) or self.notes(title, count=1)
        if not rows:
            return None
        with self._lock, self._db:
            self._db.execute("UPDATE notes SET body=body || ? , ts=? WHERE id=?",
                             ("\n" + text, self.clock(), rows[0]["id"]))
        return rows[0]

    # ---- reminders ---------------------------------------------------------
    def reminders(self, query: str = "", scope: str = "all", *, include_done: bool = False) -> list[dict]:
        now = self.clock()
        tz = _local_tz()
        start = datetime.combine(datetime.fromtimestamp(now, tz).date(), clock(), tz).timestamp()
        end, end2 = start + 86400, start + 2 * 86400
        rows = self._q("SELECT * FROM reminders ORDER BY due_ts")
        if not include_done:
            rows = [r for r in rows if r["status"] == "active"]
        windows = {"today": lambda t: start <= t < end, "tomorrow": lambda t: end <= t < end2,
                   "past_due": lambda t: t < now, "upcoming": lambda t: t >= now, "all": lambda t: True}
        keep = windows.get(scope or "all", windows["all"])
        rows = [r for r in rows if r["due_ts"] is None and scope in ("all", None, "") or
                (r["due_ts"] is not None and keep(r["due_ts"]))]
        terms = [t for t in re.findall(r"\w+", (query or "").casefold())
                 if t not in {"my", "reminder", "reminders", "the", "all", "any", "show"}]
        if terms:
            rows = [r for r in rows if any(t in (r["title"] + " " + (r["detail"] or "")).casefold() for t in terms)]
        return rows

    def find_reminder(self, title: str, expected_id: str = "") -> list[dict]:
        if expected_id:
            rows = self._q("SELECT * FROM reminders WHERE id=?", (expected_id,))
            if rows:
                return rows
        rows = self._q("SELECT * FROM reminders WHERE status='active' AND lower(title)=lower(?)", (title,))
        return rows or self.reminders(title)

    def add_reminder(self, title: str, due_ts: float | None, kind: str = "reminder") -> dict:
        rid = self._new_id("rem", title)
        with self._lock, self._db:
            self._db.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?,?)",
                             (rid, title, "", due_ts, "medium", "active", kind or "reminder"))
        return {"id": rid, "title": title, "due_ts": due_ts}

    def update_reminder(self, rid: str, *, title: str | None = None, due_ts: float | None = None,
                        status: str | None = None) -> None:
        with self._lock, self._db:
            if title:
                self._db.execute("UPDATE reminders SET title=? WHERE id=?", (title, rid))
            if due_ts is not None:
                self._db.execute("UPDATE reminders SET due_ts=? WHERE id=?", (due_ts, rid))
            if status:
                self._db.execute("UPDATE reminders SET status=? WHERE id=?", (status, rid))

    # ---- calendar ----------------------------------------------------------
    def events(self, start: float, end: float, query: str = "") -> list[dict]:
        rows = self._q("SELECT * FROM events WHERE status='active' AND start_ts<? AND COALESCE(end_ts,start_ts)>=? "
                       "ORDER BY start_ts", (end, start))
        terms = [t for t in re.findall(r"\w+", (query or "").casefold()) if len(t) > 2
                 and t not in {"meeting", "meetings", "calendar", "event", "events"}]
        if terms:
            rows = [r for r in rows if any(t in (r["title"] + " " + (r["location"] or "")).casefold() for t in terms)]
        return rows

    def find_event(self, title: str) -> list[dict]:
        now = self.clock()
        rows = self._q("SELECT * FROM events WHERE status='active' AND lower(title)=lower(?) AND start_ts>=? "
                       "ORDER BY start_ts", (title, now - 3600))
        if rows:
            return rows
        return [r for r in self.events(now - 3600, now + 60 * 86400) if title.casefold() in r["title"].casefold()]

    def add_event(self, title: str, start_ts: float, duration_min: int = 60, location: str = "") -> dict:
        eid = self._new_id("evt", title, start_ts)
        with self._lock, self._db:
            self._db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                             (eid, title, start_ts, start_ts + 60 * int(duration_min or 60), location or "",
                              "Added in the Aria demo", "active"))
        return {"id": eid, "title": title, "start_ts": start_ts}

    def cancel_event(self, eid: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE events SET status='cancelled' WHERE id=?", (eid,))

    # ---- scheduled sends ---------------------------------------------------
    def schedule(self, channel: str, recipient: str, body: str, due_ts: float, subject: str = "") -> dict:
        sid = self._new_id("sched", channel, recipient, due_ts)
        with self._lock, self._db:
            self._db.execute("INSERT INTO scheduled VALUES(?,?,?,?,?,?,?,?)",
                             (sid, channel, recipient, subject, body, due_ts, "pending", self.clock()))
        return {"id": sid, "channel": channel, "to": recipient, "due_ts": due_ts}

    def scheduled(self, status: str = "pending") -> list[dict]:
        if status in ("all", "history"):
            return self._q("SELECT * FROM scheduled ORDER BY due_ts")
        return self._q("SELECT * FROM scheduled WHERE status=? ORDER BY due_ts", (status,))

    def cancel_scheduled(self, sid: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("UPDATE scheduled SET status='cancelled' WHERE id=? AND status='pending'",
                                    (sid,)).rowcount == 1

    def deliver_due(self, now: float | None = None) -> list[dict]:
        """Deliver pending scheduled sends into their synthetic threads."""
        now = self.clock() if now is None else now
        out = []
        for row in self._q("SELECT * FROM scheduled WHERE status='pending' AND due_ts<=?", (now,)):
            with self._lock, self._db:
                if self._db.execute("UPDATE scheduled SET status='sent' WHERE id=? AND status='pending'",
                                    (row["id"],)).rowcount != 1:
                    continue
            if row["channel"] == "email":
                self.send_mail(row["recipient"], row["subject"] or "(no subject)", row["body"])
            else:
                self.send_message(row["recipient"], row["body"])
            out.append(row)
        return out


_world: World | None = None
_world_lock = threading.Lock()


def get_world() -> World:
    global _world
    with _world_lock:
        if _world is None:
            from service.paths import state_dir
            _world = World(state_dir() / DB_NAME)
        return _world


def set_world(world: World | None) -> None:
    """Install (tests) or drop the process world."""
    global _world
    with _world_lock:
        _world = world
