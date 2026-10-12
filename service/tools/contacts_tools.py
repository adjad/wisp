"""Contacts writes, and birthdays/anniversaries.

Birthdays come from `imessage_tools.birthdays_raw()` — pushed by the SAME
ContactsReader.swift sync that already builds the phone/email -> name map
(see its `read()`), extended to also read `CNContactBirthdayKey`. Not a
second sync path: same cadence, same one-time Contacts permission grant.

`manage_contacts` writes via AppleScript (Contacts.app DOES have a working
scripting dictionary for creating/updating properties, unlike Podcasts/Books —
confirmed live; the earlier finding about Contacts being impractically SLOW
was specifically about ENUMERATING every contact one by one for a bulk read,
not about a single targeted create/update, which is fast).
"""
from __future__ import annotations

import datetime
import re
import subprocess

from service.tools.imessage_tools import birthdays_raw
from service.tools.registry import register

# Contacts.app's AppleScript bridge is measurably slower than Mail/Messages/
# Notes' — even a single "make new person; save" timed out at the 20s this
# module started with, live on this machine. 45s is a real, measured budget,
# not a defensive guess.
_TIMEOUT = 45


def _osa(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=_TIMEOUT)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@register(
    "contact_dates",
    "List upcoming birthdays from Contacts, soonest first. Needs birth dates "
    "saved in Contacts.app (most saved dates have no year, so age isn't "
    "reported — only how many days away).",
    {"type": "object",
     "properties": {
         "days": {"type": "integer", "description": "How many days ahead to look. Default 30."},
     }},
    category="assistant_read",
    aliases=["whose birthday is coming up", "any birthdays this month",
             "who has a birthday soon", "which of my contacts has a birthday this month"],
)
def contact_dates(days: int = 30) -> str:
    try:
        window = max(1, min(366, int(days)))
    except (TypeError, ValueError):
        window = 30
    table = birthdays_raw()
    if not table:
        return ("No birthdays found — either none are saved in Contacts, or "
                "Contacts hasn't synced yet on this Mac.")

    today = datetime.date.today()
    upcoming = []
    for key, names in table.items():
        m = re.fullmatch(r"(\d{2})-(\d{2})", key)
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        try:
            this_year = today.replace(month=month, day=day)
        except ValueError:
            continue  # Feb 29 in a context that can't hold it
        target = this_year if this_year >= today else this_year.replace(year=today.year + 1)
        delta = (target - today).days
        if delta <= window:
            upcoming.append((delta, target, names))
    if not upcoming:
        return f"No birthdays in the next {window} days."
    upcoming.sort(key=lambda t: t[0])
    lines = []
    for delta, when, names in upcoming:
        when_str = ("today" if delta == 0 else "tomorrow" if delta == 1
                   else when.strftime("%A, %B %-d"))
        lines.append(f"  {', '.join(names)} — {when_str}")
    return f"Upcoming birthdays (next {window} days):\n" + "\n".join(lines)


@register(
    "manage_contacts",
    "Create a new contact, or add a phone/email to an existing one. Contacts "
    "writes are simple, single-target operations here — for anything more "
    "elaborate (merging duplicates, editing addresses), open Contacts.app "
    "directly.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "add_phone", "add_email"]},
            "name": {"type": "string", "description": "Contact's full name."},
            "value": {"type": "string",
                      "description": "Phone number or email address, for add_phone/add_email."},
        },
        "required": ["action", "name"],
    },
    category="messages_write",
    aliases=["add a new contact for John", "save this number as a new contact",
             "save this number to Dan's contact card"],
)
def manage_contacts(action: str, name: str, value: str = "") -> str:
    a = (action or "").strip().lower()
    who = (name or "").strip()
    if not who:
        return "(error: manage_contacts needs a `name`.)"

    if a == "create":
        parts = who.split(None, 1)
        given, family = parts[0], (parts[1] if len(parts) > 1 else "")
        script = (f'tell application "Contacts"\n'
                  f'  set p to make new person with properties '
                  f'{{first name:"{_esc(given)}", last name:"{_esc(family)}"}}\n'
                  f'  save\n'
                  f'end tell')
        p = _osa(script)
        if p.returncode != 0:
            return f"(could not create contact: {(p.stderr or '').strip()})"
        return f"Created contact {who}."

    if a in ("add_phone", "add_email"):
        if not value.strip():
            return f"(error: {a} needs `value`.)"
        prop = "phones" if a == "add_phone" else "emails"
        label = "phone" if a == "add_phone" else "email"
        script = (
            f'tell application "Contacts"\n'
            f'  set hits to (every person whose name contains "{_esc(who)}")\n'
            f'  if (count of hits) is 0 then return "NONE"\n'
            f'  if (count of hits) > 1 then\n'
            f'    set out to ""\n'
            f'    repeat with p in hits\n      set out to out & (name of p) & "\\n"\n'
            f'    end repeat\n    return "MANY:" & out\n'
            f'  end if\n'
            f'  set target to item 1 of hits\n'
            f'  tell target to make new {label} at end of {prop} '
            f'with properties {{value:"{_esc(value.strip())}"}}\n'
            f'  save\n  return "OK:" & (name of target)\n'
            f'end tell')
        p = _osa(script)
        if p.returncode != 0:
            return f"(could not update {who}: {(p.stderr or '').strip()})"
        out = p.stdout.strip()
        if out == "NONE":
            return f"No contact matches {who!r}. Use action='create' to add one."
        if out.startswith("MANY:"):
            names = [n for n in out[5:].splitlines() if n.strip()]
            return f"Several contacts match {who!r} — which one?\n" + "\n".join(f"  - {n}" for n in names[:8])
        return f"Added {label} to {out[3:] if out.startswith('OK:') else who}."

    return f"(error: unknown action {a!r}. Use create, add_phone, or add_email.)"
