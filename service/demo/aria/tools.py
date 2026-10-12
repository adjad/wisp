"""Synthetic implementations of Wisp's personal-data tools for the Aria demo.

``run_tool`` (service/tools/registry.py) calls ``dispatch`` INSTEAD of the
registered implementation for every tool while the Aria demo is on. A tool in
``SYNTHETIC`` runs against the synthetic world (world.py); a tool in
``PASSTHROUGH`` touches no personal data or native app and runs as usual;
everything else is refused. So in the demo the real AppleScript / EventKit /
Mail / Messages / Notes / Contacts implementations are unreachable by
construction, not by convention. Approval happens BEFORE dispatch with the
normal policy (policy.decide), so sends still show a confirmation card every
time and writes still ask once/always/never.

Every reply is plain text for the model, marked as fictional demo data.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from service.demo.aria.world import CALENDAR_NAME, USER_EMAIL, USER_NAME, get_world

MARK = "[Aria demo — fictional data]"

# No personal data, no native app, no writes: safe to run for real in the demo.
PASSTHROUGH = frozenset({
    "air_quality", "astronomy", "calculate", "convert_currency", "convert_units", "country_info",
    "define_word", "find_place", "get_sports_scores", "get_stock_price", "get_weather",
    "lookup_media_title", "rain_radar", "random_pick", "recipe_lookup", "track_flight",
    "track_package", "transit_info", "travel_time", "weather_alerts", "web_fetch", "web_search",
    "wikipedia_summary", "world_time", "wisp_status", "wisp_capabilities",
    # Behavior tools read/edit Wisp's own learned profile, which in the demo
    # lives in the demo state directory and learns from the synthetic world.
    "describe_me", "predict_my_day", "my_patterns", "correct_pattern",
})


def _tz():
    return datetime.now().astimezone().tzinfo


def _fmt(ts: float | None) -> str:
    if ts is None:
        return "no date"
    return datetime.fromtimestamp(ts, _tz()).strftime("%a %b %-d, %-I:%M %p")


def _span(period: str | None = None, day: str | None = None, default_days: int | None = None,
          *, past: bool = False) -> tuple[float | None, float | None, str]:
    phrase = period or day
    if phrase:
        from service.tools.timeranges import resolve_span
        start, end, label = resolve_span(phrase)
        return start, end, label
    if default_days is None:
        return None, None, ""
    now = time.time()
    return (now - default_days * 86400, now, f"last {default_days} days") if past else \
        (now, now + default_days * 86400, f"next {default_days} days")


def _iso(when_iso: str) -> float:
    dt = datetime.fromisoformat(str(when_iso).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt.timestamp()


def _addrs(raw: str | None) -> str:
    try:
        return ", ".join(json.loads(raw or "[]"))
    except ValueError:
        return raw or ""


# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------

def _mail_line(r: dict) -> str:
    who = (f"From: {r['from_name'] or ''} <{r['from_addr']}>" if r["folder"] == "inbox"
           else f"To: {_addrs(r['to_addrs'])} (sent by you)")
    flags = "".join([" [unread]" if r["unread"] else "", " [flagged]" if r["flagged"] else ""])
    return (f"- id={r['id']} | {_fmt(r['ts'])} | {who} | Subject: {r['subject']}{flags}\n"
            f"  {r['body'][:600]}")


async def view_emails(query: str = "", period: str = "", day: str = "", unread: bool | None = None,
                      count: int = 10, account: str = "", strict_match: bool = False) -> str:
    since, until, label = _span(period, day)
    rows = get_world().mail(query=query, since=since, until=until, unread=True if unread else None,
                            count=count or 10)
    head = f"{MARK} Mail for {USER_NAME} <{USER_EMAIL}>" + (f" ({label})" if label else "")
    return head + "\n" + ("\n".join(_mail_line(r) for r in rows) or "No matching email.")


async def summarize_emails(period: str = "", day: str = "", count: int = 25, unread: bool | None = None,
                           account: str = "") -> str:
    since, until, label = _span(period, day)
    if since is None and not unread:
        since, until, label = time.time() - 3 * 86400, None, "last 3 days"
    rows = get_world().mail(since=since, until=until, unread=True if unread else None, folder="inbox",
                            count=count or 25)
    return (f"{MARK} Inbox digest ({label or 'recent'}; summarize these for Aria):\n"
            + ("\n".join(_mail_line(r) for r in rows) or "No email in that window."))


async def summarize_thread(subject: str, account: str = "", count: int = 10) -> str:
    w = get_world()
    hits = w.mail(query=subject, count=1, include_archived=True)
    if not hits:
        return f"{MARK} No email thread matches {subject!r}."
    rows = w.thread(hits[0]["thread_id"])[-(count or 10):]
    return f"{MARK} Thread: {hits[0]['subject']}\n" + "\n".join(_mail_line(r) for r in rows)


async def triage_inbox(count: int = 20) -> str:
    rows = get_world().mail(folder="inbox", unread=True, count=count or 20)
    return f"{MARK} Unread inbox to triage:\n" + ("\n".join(_mail_line(r) for r in rows) or "Inbox zero.")


async def _mail_flag(message_id: str, field: str, value: bool, verb: str) -> str:
    row = get_world().set_mail_flag(message_id, field, value)
    if row is None:
        return f"{MARK} No email with id {message_id!r}; nothing changed."
    return f"{MARK} {verb} the demo email {row['subject']!r}."


async def archive_email(message_id: str) -> str:
    return await _mail_flag(message_id, "archived", True, "Archived")


async def flag_email(message_id: str, flagged: bool = True) -> str:
    return await _mail_flag(message_id, "flagged", bool(flagged), "Flagged" if flagged else "Unflagged")


async def mark_email_read(message_id: str, read: bool = True) -> str:
    return await _mail_flag(message_id, "unread", not read, "Marked read" if read else "Marked unread")


def _check_recipients(to: str) -> str | None:
    from service.demo.aria.data import email_ok, phone_ok
    w = get_world()
    for part in [p.strip() for p in re.split(r"[,;]", to or "") if p.strip()]:
        c = w.resolve(part)
        if c is None and not (email_ok(part) or phone_ok(part)):
            return (f"{MARK} {part!r} is not one of Aria's demo contacts and not a reserved fictional "
                    "address (example.com / 555-01xx), so nothing was sent. Use lookup_contact first.")
    return None


async def send_email(to: str, subject: str, body: str, cc: str = "", confirmed_self_send: bool = False) -> str:
    if (err := _check_recipients(to + ("," + cc if cc else ""))):
        return err
    sent = get_world().send_mail(to, subject, body, cc=cc)
    return (f"{MARK} Sent (simulated — it stays inside the demo) to {', '.join(sent['to'])}: "
            f"{subject!r}. It now shows in Sent; a reply may arrive in a few seconds.")


async def draft_email(to: str, subject: str, body: str, cc: str = "") -> str:
    if (err := _check_recipients(to)):
        return err
    get_world().send_mail(to, subject, body, cc=cc, action="draft")
    return f"{MARK} Saved a demo draft to {to}: {subject!r} (not sent)."


async def reply_to_email(message_id: str, body: str, reply_all: bool = False,
                         expected_reply: dict | None = None, account: str = "") -> str:
    w = get_world()
    original = w.mail_message(message_id)
    if original is None:
        return f"{MARK} No email with id {message_id!r}; nothing was sent."
    to = original["from_addr"] if original["folder"] == "inbox" else _addrs(original["to_addrs"])
    subject = original["subject"] if original["subject"].lower().startswith("re:") else f"Re: {original['subject']}"
    w.send_mail(to, subject, body, thread_id=original["thread_id"])
    return f"{MARK} Replied (simulated) to {to} in the thread {original['subject']!r}."


async def forward_email(message_id: str, to: str, note: str = "") -> str:
    if (err := _check_recipients(to)):
        return err
    w = get_world()
    original = w.mail_message(message_id)
    if original is None:
        return f"{MARK} No email with id {message_id!r}; nothing was forwarded."
    body = (note + "\n\n" if note else "") + f"---------- Forwarded message ----------\n{original['body']}"
    w.send_mail(to, f"Fwd: {original['subject']}", body)
    return f"{MARK} Forwarded (simulated) {original['subject']!r} to {to}."


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def _msg_line(r: dict) -> str:
    chat = r["contact"]
    sender = r.get("sender") or chat
    if r["direction"] == "in":
        arrow = f"{sender} -> you" + (f" (in {chat})" if sender != chat else "")
    else:
        arrow = f"you -> {chat}"
    return f"- {_fmt(r['ts'])} [{arrow}] {r['text']}" + (" [unread]" if r["unread"] else "")


async def view_messages(query: str = "", period: str = "", day: str = "", count: int = 30,
                        unread: bool | None = None, conversation: str = "") -> str:
    since, until, label = _span(period, day)
    rows = get_world().messages(query=query, conversation=conversation, since=since, until=until,
                                unread=True if unread else None, count=count or 30)
    return (f"{MARK} Messages" + (f" ({label})" if label else "") + ":\n"
            + ("\n".join(_msg_line(r) for r in reversed(rows)) or "No matching messages."))


async def summarize_messages(period: str = "", day: str = "", conversation: str = "", count: int = 40,
                             unread: bool | None = None) -> str:
    since, until, label = _span(period, day)
    if since is None:
        since, label = time.time() - 2 * 86400, "last 2 days"
    rows = get_world().messages(conversation=conversation, since=since, until=until,
                                unread=True if unread else None, count=count or 40)
    return (f"{MARK} Messages to summarize for Aria ({label}):\n"
            + ("\n".join(_msg_line(r) for r in reversed(rows)) or "No messages in that window."))


async def send_message(to: str, text: str, confirmed_self_send: bool = False) -> str:
    if (err := _check_recipients(to)):
        return err
    sent = get_world().send_message(to, text)
    return (f"{MARK} Sent (simulated — it stays inside the demo) to {sent['to']}. "
            "It now shows in the thread; a reply may arrive in a few seconds.")


async def draft_message(to: str, text: str) -> str:
    if (err := _check_recipients(to)):
        return err
    return f"{MARK} Draft for {to} (not sent): {text}"


async def schedule_send(channel: str, to: str, when: str, body: str = "", text: str = "",
                        subject: str = "") -> str:
    if (err := _check_recipients(to)):
        return err
    from service.tools.timeranges import BadWhen, resolve_when
    try:
        when_dt, label = resolve_when(when)
    except BadWhen as exc:
        return str(exc)
    if when_dt.timestamp() <= time.time():
        return f"{MARK} NOT scheduled — {when!r} is in the past."
    content = body or text
    if not content:
        return f"{MARK} NOT scheduled — there is nothing to send."
    row = get_world().schedule(channel, to, content, when_dt.timestamp(), subject=subject)
    return f"{MARK} Scheduled (simulated) {channel} to {to} for {label} (id {row['id']})."


async def list_scheduled_sends(status: str = "pending", count: int = 20, id: str = "") -> str:
    rows = get_world().scheduled(status or "pending")
    if id:
        rows = [r for r in rows if r["id"] == id]
    return f"{MARK} Scheduled sends:\n" + ("\n".join(
        f"- id={r['id']} {r['channel']} to {r['recipient']} at {_fmt(r['due_ts'])} [{r['status']}]: {r['body'][:120]}"
        for r in rows[:count or 20]) or "None.")


async def cancel_scheduled_send(id: str) -> str:
    ok = get_world().cancel_scheduled(id)
    return f"{MARK} " + ("Cancelled the scheduled send." if ok else f"No pending scheduled send {id!r}.")


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------

def _contact_line(c: dict, *, full: bool) -> str:
    base = f"{c['name']} — {c.get('role') or ''}{', ' + c['organization'] if c.get('organization') else ''}"
    if not full:
        return "- " + base
    return (base + f"\nEmail: {c.get('email') or '—'} | Phone: {c.get('phone') or '—'}"
            + (f"\n{c.get('relationship') or ''}: {c.get('context') or ''}" if c.get("context") else ""))


async def lookup_contact(name: str) -> str:
    rows = get_world().contacts(name)
    return f"{MARK}\n" + ("\n\n".join(_contact_line(c, full=True) for c in rows[:8])
                          or f"No demo contact matches {name!r}.")


async def list_contacts(query: str = "") -> str:
    rows = get_world().contacts(query)
    return f"{MARK} {len(rows)} contacts:\n" + "\n".join(_contact_line(c, full=False) for c in rows)


async def manage_contacts(action: str, name: str, value: str = "") -> str:
    from service.demo.aria.data import email_ok, phone_ok
    if value and not (email_ok(value) or phone_ok(value)):
        return f"{MARK} Demo contacts may only use example.com addresses or 555-01xx numbers; nothing changed."
    c = get_world().add_contact(name, email=value if "@" in (value or "") else "",
                                phone=value if value and "@" not in value else "")
    return f"{MARK} Saved demo contact {c['name']} ({c.get('email') or ''} {c.get('phone') or ''})."


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------

async def search_notes(query: str = "", period: str = "", day: str = "", count: int = 6) -> str:
    since, until, _ = _span(period, day)
    rows = get_world().notes(query, since=since, until=until, count=count or 6)
    return f"{MARK}\n" + ("\n\n".join(f"## {r['title']}\nFolder: {r['folder']} | Modified {_fmt(r['ts'])}\n{r['body']}"
                                      for r in rows) or "No demo note matches.")


async def create_note(title: str, body: str = "", folder: str = "", checklist: bool = False) -> str:
    if checklist and body:
        body = "\n".join("- [ ] " + line.strip("-• ").strip() for line in body.splitlines() if line.strip())
    get_world().create_note(title, body, folder)
    return f"{MARK} Created the demo note {title!r}."


async def append_note(title: str, text: str) -> str:
    row = get_world().append_note(title, text)
    return f"{MARK} " + (f"Added to {row['title']!r}." if row else f"No demo note titled {title!r}; nothing changed.")


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------

def _rem_line(r: dict) -> str:
    return (f"- id={r['id']} {r['title']} — due {_fmt(r['due_ts'])}"
            f"{' (' + r['priority'] + ')' if r.get('priority') else ''}"
            f"{' [done]' if r['status'] != 'active' else ''}"
            + (f"\n  {r['detail']}" if r.get("detail") else ""))


async def search_reminders(query: str = "", scope: str = "all") -> str:
    rows = get_world().reminders(query, scope or "all")
    return f"{MARK} Reminders ({scope or 'all'}):\n" + ("\n".join(_rem_line(r) for r in rows) or "None match.")


async def add_reminder(title: str, when_iso: str, kind: str = "reminder") -> str:
    try:
        due = _iso(when_iso)
    except ValueError:
        return f"(error: when_iso {when_iso!r} is not an ISO date/time)"
    get_world().add_reminder(title, due, kind)
    _mirror()
    return f"{MARK} Added the reminder {title!r} for {_fmt(due)}."


def _one_reminder(title: str, expected_id: str = "") -> tuple[dict | None, str]:
    rows = get_world().find_reminder(title or "", expected_id or "")
    if not rows:
        return None, f"{MARK} No active demo reminder matches {title!r}; nothing changed."
    if len(rows) > 1 and not expected_id:
        return None, (f"{MARK} Several reminders match {title!r}; call again with expected_id:\n"
                      + "\n".join(_rem_line(r) for r in rows[:6]))
    return rows[0], ""


async def update_reminder(title: str = "", when_iso: str = "", day: str = "", new_title: str = "",
                          expected_id: str = "") -> str:
    row, err = _one_reminder(title, expected_id)
    if row is None:
        return err
    due = None
    if when_iso:
        try:
            due = _iso(when_iso)
        except ValueError:
            return f"(error: when_iso {when_iso!r} is not an ISO date/time)"
    elif day in ("today", "tomorrow") and row["due_ts"]:
        base = datetime.fromtimestamp(row["due_ts"], _tz())
        target = datetime.now(_tz()).date() + timedelta(days=1 if day == "tomorrow" else 0)
        due = base.replace(year=target.year, month=target.month, day=target.day).timestamp()
    get_world().update_reminder(row["id"], title=new_title or None, due_ts=due)
    _mirror()
    return f"{MARK} Updated the reminder {row['title']!r}" + (f" to {_fmt(due)}" if due else "") + "."


async def complete_reminder(title: str, expected_id: str = "") -> str:
    row, err = _one_reminder(title, expected_id)
    if row is None:
        return err
    get_world().update_reminder(row["id"], status="done")
    _mirror()
    return f"{MARK} Marked {row['title']!r} done."


async def clear_reminders(scope: str, query: str = "", expected_ids: list | None = None) -> str:
    rows = get_world().reminders(query, scope)
    if expected_ids:
        rows = [r for r in rows if r["id"] in set(expected_ids)]
    for r in rows:
        get_world().update_reminder(r["id"], status="deleted")
    _mirror()
    return f"{MARK} Deleted {len(rows)} demo reminder(s)."


async def clear_past_reminders(query: str = "", days: int = 30) -> str:
    return await clear_reminders("past_due", query)


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def _event_line(r: dict) -> str:
    end = datetime.fromtimestamp(r["end_ts"], _tz()).strftime("%-I:%M %p") if r.get("end_ts") else ""
    return (f"- {_fmt(r['start_ts'])}{'–' + end if end else ''} {r['title']}"
            + (f" @ {r['location']}" if r.get("location") else ""))


async def get_upcoming(days: int = 7, period: str = "", calendar_only: bool = False, query: str = "",
                       account: str = "", include_holidays: bool = False) -> str:
    start, end, label = _span(period, None, days or 7)
    w = get_world()
    events = w.events(start, end, query)
    out = [f"{MARK} Calendar ({CALENDAR_NAME}, {label}):"] + ([_event_line(r) for r in events] or ["No events."])
    if not calendar_only:
        rems = [r for r in w.reminders(query, "all") if r["due_ts"] is not None and start <= r["due_ts"] < end]
        out += ["Reminders due:"] + ([_rem_line(r) for r in rems] or ["None."])
    return "\n".join(out)


async def get_past_events(days: int = 7, query: str = "", account: str = "", include_holidays: bool = False) -> str:
    now = time.time()
    rows = get_world().events(now - (days or 7) * 86400, now, query)
    return f"{MARK} Past events (last {days or 7} days):\n" + ("\n".join(_event_line(r) for r in rows) or "None.")


async def find_free_time(days: int = 3, period: str = "", minutes: int = 60, day_start: int = 9,
                         day_end: int = 18) -> str:
    start, end, label = _span(period, None, days or 3)
    w = get_world()
    tz = _tz()
    need = 60 * (minutes or 60)
    slots = []
    d = datetime.fromtimestamp(start, tz).date()
    while datetime.combine(d, datetime.min.time(), tz).timestamp() < end and len(slots) < 8:
        lo = max(start, datetime.combine(d, datetime.min.time(), tz).timestamp() + (day_start or 9) * 3600)
        hi = datetime.combine(d, datetime.min.time(), tz).timestamp() + (day_end or 18) * 3600
        cursor = lo
        for ev in w.events(lo, hi):
            if ev["start_ts"] - cursor >= need:
                slots.append((cursor, ev["start_ts"]))
            cursor = max(cursor, ev.get("end_ts") or ev["start_ts"])
        if hi - cursor >= need:
            slots.append((cursor, hi))
        d += timedelta(days=1)
    return (f"{MARK} Free {minutes or 60}-minute windows ({label}):\n"
            + ("\n".join(f"- {_fmt(a)} to {datetime.fromtimestamp(b, tz):%-I:%M %p}" for a, b in slots[:8])
               or "No free window that long."))


async def add_calendar_event(title: str, when_iso: str, duration_min: int = 60, location: str = "") -> str:
    try:
        start = _iso(when_iso)
    except ValueError:
        return f"(error: when_iso {when_iso!r} is not an ISO date/time)"
    get_world().add_event(title, start, duration_min or 60, location)
    _mirror()
    return f"{MARK} Added {title!r} to the demo calendar on {_fmt(start)}."


async def cancel_event(title: str) -> str:
    rows = get_world().find_event(title)
    if not rows:
        return f"{MARK} No upcoming demo event matches {title!r}; nothing changed."
    get_world().cancel_event(rows[0]["id"])
    _mirror()
    return f"{MARK} Cancelled {rows[0]['title']!r} ({_fmt(rows[0]['start_ts'])})."


SYNTHETIC: dict[str, Callable[..., Awaitable[str]]] = {f.__name__: f for f in (
    view_emails, summarize_emails, summarize_thread, triage_inbox, archive_email, flag_email,
    mark_email_read, send_email, draft_email, reply_to_email, forward_email,
    view_messages, summarize_messages, send_message, draft_message, schedule_send,
    list_scheduled_sends, cancel_scheduled_send,
    lookup_contact, list_contacts, manage_contacts,
    search_notes, create_note, append_note,
    search_reminders, add_reminder, update_reminder, complete_reminder, clear_reminders, clear_past_reminders,
    get_upcoming, get_past_events, find_free_time, add_calendar_event, cancel_event,
    # update_event is unavailable in real Wisp too (it cannot yet preserve an
    # event's details), so the demo does not pretend otherwise.
)}

DEMO_TOOLS = frozenset(SYNTHETIC) | PASSTHROUGH


def allowed(tool_name: str) -> bool:
    return tool_name in DEMO_TOOLS


async def dispatch(tool, args: dict) -> str:
    """Run ``tool`` inside the demo. Never calls a native implementation."""
    impl = SYNTHETIC.get(tool.name)
    if impl is None:
        raise LookupError(tool.name)
    return await impl(**args)


# --------------------------------------------------------------------------
# Mirror the synthetic calendar + reminders into the (demo) assistant store,
# the same way the app's Calendar/Reminders sync does on a real Mac, so the
# panel's Today/upcoming views, due-reminder notifications and the demo's
# behavior engine all see the synthetic world.
# --------------------------------------------------------------------------

def mirror_items(now: float | None = None) -> tuple[list[dict], list[dict]]:
    now = time.time() if now is None else now
    w = get_world()
    events = [{"source_id": r["id"], "kind": "event", "title": r["title"], "context": CALENDAR_NAME,
               "organizer": None, "account": CALENDAR_NAME, "when_ts": float(r["start_ts"]),
               "end_ts": r.get("end_ts"), "all_day": False, "location": r.get("location") or None,
               "confidence": 1.0}
              for r in w.events(now - 21 * 86400, now + 21 * 86400)]
    reminders = [{"source_id": r["id"], "kind": "reminder", "title": r["title"], "context": CALENDAR_NAME,
                  "organizer": None, "account": CALENDAR_NAME, "when_ts": float(r["due_ts"]),
                  "end_ts": None, "all_day": False, "location": None, "confidence": 1.0}
                 for r in w.reminders("", "all") if r["due_ts"] is not None]
    return events, reminders


def _mirror() -> None:
    try:
        mirror()
    except Exception:  # noqa: BLE001 — the panel mirror must never fail a demo action
        pass


def mirror() -> dict:
    from service.assistant import scheduler
    from service.assistant.store import assistant_store
    events, reminders = mirror_items()
    diag = {"authorized": True, "available": True, "reason": "Aria demo (fictional)"}
    n_events = assistant_store.sync_source("calendar", events, diagnostics=diag)
    n_rems = assistant_store.sync_source("reminders", reminders, diagnostics=diag, undated=None)
    scheduler.record_sync("calendar", n_events, diagnostics=diag)
    scheduler.record_sync("reminders", n_rems, diagnostics=diag)
    return {"events": n_events, "reminders": n_rems}


def status() -> dict[str, Any]:
    return {"enabled": True, "counts": get_world().counts()}


_last_mirror = 0.0
MIRROR_EVERY_S = 300.0


async def tick(hub, now: float | None = None) -> list[dict]:
    """Scheduler hook: deliver due scheduled sends into their synthetic
    threads, and keep the panel mirror's rolling window current."""
    global _last_mirror
    import asyncio
    now = time.time() if now is None else now
    delivered = await asyncio.to_thread(get_world().deliver_due, now)
    refreshed = await asyncio.to_thread(get_world().refresh_calendar, now)
    if delivered or refreshed or now - _last_mirror >= MIRROR_EVERY_S:
        await asyncio.to_thread(mirror)
        _last_mirror = now
        await hub.publish({"type": "changed"})
    for row in delivered:
        await hub.publish({"type": "scheduled_send_result", "ok": True, "channel": row["channel"],
                           "display": row["recipient"], "error": "", "demo": True})
    return delivered
