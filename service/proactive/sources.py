"""What the proactive engine reads: items from every source, already cached.

Mail threads (the native Mail snapshot plus Gmail-linked accounts through the
shared mail caches), text conversation slices, Notes TODO lines, and the
calendar and reminders from the assistant store. Reads caches only: never a
native request, never a model call.

An item is one thread or conversation slice:
``{id, source, account, who, title, text, lines, received_ts, hash, replied,
ref}``. ``text`` is the bounded material the triage call reads; ``lines`` are
the verbatim incoming lines a quote may be copied from, by code.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from service.proactive.guard import clean

MAIL_WINDOW_S = 7 * 86400
TEXT_WINDOW_S = 4 * 86400
PAST_CALENDAR_DAYS = 7
MAX_MAIL_THREADS = 40
MAX_CONVERSATIONS = 20
MAX_TEXT_LINES = 8
MAX_EXCERPT = 500
MAX_ITEM_CHARS = 1600
OWN = "me"


def _digest(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:20]


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


# --- accounts -------------------------------------------------------------------


def gmail_accounts() -> set[str]:
    """Accounts linked only through Wisp's Google sign-in (read-only)."""
    try:
        from service.gmail_source import connected_accounts
        return {a.casefold() for a in connected_accounts()}
    except Exception:  # noqa: BLE001 — no Google config: no linked accounts
        return set()


def own_account() -> str:
    """The owner's bucket: always "me", never an address. Texts, Notes,
    Reminders, EventKit calendars, Wisp-local items and Mail.app mail all
    belong here. An address would collide with a Google account linked under
    the same address (a UCSC Workspace login) and leak the owner's texts
    into that account's view."""
    return OWN


def owner_label() -> str:
    """How the owner's bucket is shown: their name, else their first address."""
    try:
        from service.memory.identity import user_emails, user_name
        name = (user_name() or "").strip()
        if name:
            return name
        emails = list(user_emails())
        if emails:
            return emails[0]
    except Exception:  # noqa: BLE001
        pass
    return "Me"


def fetched_by_gmail(row: dict, linked: set[str]) -> bool:
    """A mail row Wisp's Google sign-in fetched for a linked account: its
    account is that address and so is its account id. A Mail.app copy under
    the same address has Mail's own account UUID and a "mail:" native id."""
    account = str(row.get("account") or "").strip().casefold()
    if not account or account not in linked:
        return False
    if str(row.get("native_id") or "").startswith("mail:"):
        return False
    return str(row.get("account_id") or "").strip().casefold() == account


def mail_account(row: dict, *, linked: set[str], own: str) -> str:
    return str(row.get("account")).strip().casefold() if fetched_by_gmail(row, linked) else own


def calendar_account(row: dict, *, linked: set[str], own: str) -> str:
    """A Google Calendar row (source_id "gcal:<email>:<event>") belongs to that
    linked account; every other calendar, reminder or local row is the owner's."""
    source_id = str(row.get("source_id") or "")
    if source_id.startswith("gcal:"):
        email = source_id[len("gcal:"):].split(":", 1)[0].strip().casefold()
        if email in linked:
            return email
    return own


def account_of(raw: str | None, *, linked: set[str] | None = None, own: str | None = None) -> str:
    """Only for a label already known to come from a linked account's own
    source; anything else is the owner's."""
    linked = gmail_accounts() if linked is None else linked
    value = str(raw or "").strip()
    if value.casefold() in linked:
        return value.casefold()
    return own if own is not None else own_account()


# --- mail -----------------------------------------------------------------------

_PREFIX = re.compile(r"^\s*(?:re|fwd?|aw|sv)\s*:\s*", re.I)


def bare_subject(subject: str) -> str:
    subject = str(subject or "")
    while (match := _PREFIX.match(subject)):
        subject = subject[match.end():]
    return subject.strip()


def _address(row: dict) -> str:
    address = str(row.get("sender_address") or "").strip()
    if not address:
        found = re.search(r"<([^>]+@[^>]+)>", str(row.get("sender") or ""))
        address = found.group(1) if found else (str(row.get("sender") or "") if "@" in str(row.get("sender") or "") else "")
    return address.strip().casefold()


def reply_ref_for_row(row: dict) -> dict | None:
    """W3's reply_ref for a raw mail-cache row ({message_id, account,
    account_id}); None when the row can't be replied to (no Message-ID)."""
    try:
        from service.assistant.email_reply_api import reply_ref_for_row as build
        return build(row)
    except ImportError:
        message_id, account = str(row.get("message_id") or "").strip(), str(row.get("account") or "").strip()
        if not message_id or not account:
            return None
        return {"message_id": message_id, "account": account, "account_id": str(row.get("account_id") or "").strip()}


def _message_key(row: dict) -> str:
    return str(row.get("message_id") or "").strip().strip("<>").casefold()


def dedupe_copies(rows: list[dict], linked: set[str]) -> list[dict]:
    """One row per message. The same email often arrives twice — through a
    Google-linked account (read-only) and through Mail.app — so a Mail.app copy
    (the one Wisp can reply from) wins, then a copy with a body."""
    def rank(row):
        native = not fetched_by_gmail(row, linked)
        return (native, bool(row.get("body")), bool(row.get("account_id")))
    best: dict[str, dict] = {}
    loose: dict[tuple, dict] = {}
    for row in rows:
        key = _message_key(row)
        if key:
            if key not in best or rank(row) > rank(best[key]):
                best[key] = row
            continue
        ident = (_address(row), bare_subject(clean(row.get("subject"), 200)).casefold(), round(float(row["ts"]) / 300))
        if ident not in loose or rank(row) > rank(loose[ident]):
            loose[ident] = row
    kept = list(best.values())
    seen = {(_address(r), bare_subject(clean(r.get("subject"), 200)).casefold(), round(float(r["ts"]) / 300))
            for r in kept}
    kept += [r for k, r in loose.items() if k not in seen]
    return kept


def mail_rows(now: float, linked: set[str] | None = None) -> list[dict]:
    """The last week's mail rows, one per message: the body snapshot, the raw
    body cache (Mail.app and Google-linked accounts, the rows W3's reply path
    reads), and cached headers for anything without a body."""
    from service.tools import email_tools as mail
    linked = gmail_accounts() if linked is None else linked
    rows: list[dict] = []
    try:
        snapshot = mail.raw_email_snapshot()
    except Exception:  # noqa: BLE001
        snapshot = None
    if snapshot is not None:
        rows += [dict(r) for r in snapshot.rows]
    try:
        rows += [dict(r) for r in mail._parse_raw()]
    except Exception:  # noqa: BLE001
        pass
    try:
        rows += [dict(r) for r in mail.header_rows(since_ts=now - MAIL_WINDOW_S)]
    except Exception:  # noqa: BLE001
        pass
    rows = [r for r in rows if _finite(r.get("ts")) and now - MAIL_WINDOW_S <= r["ts"] <= now + 300]
    return dedupe_copies(rows, linked)


def _sent_to_list(row: dict) -> bool:
    """Addressed to a list or undisclosed recipients, not to the user."""
    to = str(row.get("to") or "").casefold()
    if not to:
        return False
    try:
        from service.memory.identity import user_emails
        mine = [e.casefold() for e in user_emails()]
    except Exception:  # noqa: BLE001
        return False
    return bool(mine) and not any(m in to for m in mine)


def bulk_mail(row: dict) -> bool:
    """Newsletters, alerts, promos and organizations' mass mail: never a card."""
    from service.assistant.daily_workspace import display_name, not_a_need
    from service.assistant.mail_gists import is_mass_mail
    from service.tools.email_tools import is_machine_sender
    from service.proactive.guard import bulk_sender
    name = display_name(row.get("sender"), _address(row))
    body = str(row.get("body") or "")
    return (not_a_need(row) or is_mass_mail(row) or is_machine_sender(f"{row.get('sender', '')} {_address(row)}")
            or _sent_to_list(row) or bool(re.match(r"\s*(?:(?:re|fwd?):\s*)*\[[^\]]{2,40}\]", str(row.get("subject") or ""), re.I))
            or bulk_sender(name, _address(row))
            or bool(re.search(r"\bunsubscribe\b|manage (?:your )?(?:email )?preferences|"
                              r"view (?:this email )?in (?:your )?browser|you(?:'|’)re receiving this", body, re.I)))


def _deadline(row: dict) -> bool:
    from service.proactive.triage import _DEADLINE
    from service.proactive.guard import normalize_slang
    return bool(_DEADLINE.search(normalize_slang(f"{row.get('subject') or ''} {row.get('body') or ''}")))


def school_mail(row: dict) -> bool:
    try:
        from service.tools.email_tools import digest_category
        return digest_category(row) == "school"
    except Exception:  # noqa: BLE001
        return False


def _excerpt(row: dict) -> str:
    from service.assistant.daily_workspace import _QUOTED_HISTORY
    from service.tools.imessage_tools import redact_summary_codes
    body = _QUOTED_HISTORY.split(str(row.get("body") or ""))[0]
    text = clean(redact_summary_codes(body), 4000)
    return text[:MAX_EXCERPT].rsplit(" ", 1)[0] + "…" if len(text) > MAX_EXCERPT else text


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [clean(p, 300) for p in parts if len(clean(p)) >= 3]


def mail_items(rows: list[dict], *, mine: set[str], now: float, tz: str,
               linked: set[str] | None = None, own: str | None = None,
               counts: dict | None = None) -> list[dict]:
    """One item per thread (account + bare subject), newest first. Threads made
    only of mass mail, or only of the user's own mail, are left out."""
    from service.assistant.daily_workspace import display_name, mass_mail_filter
    from service.tools.imessage_tools import redact_summary_codes
    linked = gmail_accounts() if linked is None else linked
    own = own_account() if own is None else own
    rows = sorted(rows, key=lambda r: r["ts"])
    skip = mass_mail_filter(rows)
    threads: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        subject = clean(row.get("subject"), 200)
        if not subject:
            continue
        # The bucket is part of the key: a Mail.app copy and a Google-fetched
        # copy under one address never merge into one thread.
        threads.setdefault((mail_account(row, linked=linked, own=own), str(row.get("account") or ""),
                            bare_subject(subject).casefold()), []).append(row)
    zone = ZoneInfo(tz)
    items = []
    for (bucket, account, _), messages in threads.items():
        incoming = [m for m in messages if _address(m) not in mine]
        if not incoming:
            continue
        # A person wrote, or a school sent something with a deadline: anything
        # else (newsletters, job alerts, launches, campaigns) is not read at all.
        people = [m for m in incoming if not skip(m) and not bulk_mail(m)]
        school = [m for m in incoming if school_mail(m) and _deadline(m)]
        if counts is not None:
            used = {id(m) for m in people + school}
            tally(counts, bucket, mail=len(incoming),
                  filtered=sum(1 for m in incoming if id(m) not in used))
        if not people and not school:
            continue
        last_in = (people or school)[-1]
        last = messages[-1]
        replied = _address(last) in mine and last["ts"] >= last_in["ts"]
        subject = redact_summary_codes(clean(last_in.get("subject"), 200))
        who = display_name(last_in.get("sender"), _address(last_in)) or "Unknown sender"
        blocks, quotes = [], [subject]
        for message in messages[-3:]:
            from_user = _address(message) in mine
            speaker = "You (the user)" if from_user else (display_name(message.get("sender"), _address(message)) or "Unknown sender")
            stamp = datetime.fromtimestamp(message["ts"], zone).strftime("%a %b %-d %-I:%M %p")
            excerpt = _excerpt(message) if message.get("body") else ""
            blocks.append(f"From {speaker} · {stamp}:\n{excerpt or '(subject only)'}")
            if not from_user and excerpt:
                quotes.extend(_sentences(excerpt))
        text = f"Subject: {subject}\n" + "\n".join(blocks)
        text = text[:MAX_ITEM_CHARS]
        item_id = "mail:" + _digest(account, bare_subject(subject).casefold())
        ref = {"kind": "email", "message_id": str(last_in.get("message_id") or ""), "account": account,
               "account_id": str(last_in.get("account_id") or ""), "to": _address(last_in),
               "subject": subject, "has_body": bool(last_in.get("body"))}
        reply_ref = reply_ref_for_row(last_in)
        items.append(dict(id=item_id, source="mail", account=bucket,
                          raw_account=account, who=who, title=subject, text=text, lines=quotes,
                          received_ts=float(last_in["ts"]), replied=replied, ref=ref, reply_ref=reply_ref,
                          person=bool(people), user_wrote=any(_address(m) in mine for m in messages),
                          hash=_digest(text, replied)))
    items.sort(key=lambda i: i["received_ts"], reverse=True)
    # The cap is per account: a busy linked inbox never pushes out the owner's mail.
    kept, per = [], {}
    for item in items:
        per[item["account"]] = per.get(item["account"], 0) + 1
        if per[item["account"]] <= MAX_MAIL_THREADS:
            kept.append(item)
    return kept


# --- texts ----------------------------------------------------------------------


def text_records() -> list[tuple[float, str, str, str]]:
    """(ts, conversation, "Sender: text", chat guid) for every cached message,
    oldest first. The chat guid comes from the native record with the same
    timestamp (the V3 feed), "" when there is none."""
    from service.tools import imessage_tools as im
    if im.messages_sync_state() != "ready":
        return []
    guids: dict[float, set[str]] = {}
    try:
        for record in im.structured_messages_snapshot().get("records") or []:
            stamp = record.get("timestamp")
            if isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            if _finite(stamp) and isinstance(record.get("conversation"), str):
                guids.setdefault(round(float(stamp), 3), set()).add(record["conversation"])
    except Exception:  # noqa: BLE001 — no native ids: no text replies, everything else stands
        guids = {}
    out = []
    for ts, _identity, context, text, _unread in im._parse_records():
        found = guids.get(round(float(ts), 3)) or set()
        out.append((float(ts), context, text, next(iter(found)) if len(found) == 1 else ""))
    return sorted(out, key=lambda r: r[0])


def chat_handle(guids: set[str]) -> str:
    """The other participant's handle when every message is from one one-to-one
    chat ("iMessage;-;+15551234567"); "" for a group ("…;+;chat123…"), a mix of
    chats, or an unknown chat."""
    if len(guids) != 1:
        return ""
    guid = next(iter(guids))
    service, sep, handle = guid.partition(";-;")
    if not sep or not handle or handle.casefold().startswith("chat"):
        return ""
    return handle.strip()


_LITERAL_LABEL = re.compile(r"^[+\d(][\d() .+-]{2,}$|^\S+@\S+$")


def _render(rows: list[tuple[float, str, str]]) -> list[str]:
    try:
        from service.tools.imessage_tools import render_for_summary
        return render_for_summary(rows)
    except Exception:  # noqa: BLE001 — the plain cached shape still names the sender first
        return [f"[{text.partition(':')[0].strip()}] {text.partition(':')[2].strip()}" for _, _, text in rows]


def tally(counts: dict, account: str, **add) -> None:
    """Add to what Wisp looked at for one account: {mail, texts, notes, filtered}."""
    row = counts.setdefault(account, {"mail": 0, "texts": 0, "notes": 0, "filtered": 0})
    for key, value in add.items():
        row[key] += value


def text_items(records: list[tuple], *, now: float, own: str | None = None,
               counts: dict | None = None) -> list[dict]:
    """One item per conversation active in the last few days: its latest lines,
    rendered sender-first so the model can't credit the user's words to the
    other person."""
    from service.tools.imessage_tools import redact_summary_codes
    own = own_account() if own is None else own
    conversations: dict[str, list[tuple[float, str, str]]] = {}
    chats: dict[str, set[str]] = {}
    ever_sent: set[str] = set()
    for record in records:
        ts, context, text = record[:3]
        guid = record[3] if len(record) > 3 else ""
        if text.partition(":")[0].strip() == "Me":
            ever_sent.add(context)
        if now - TEXT_WINDOW_S <= ts <= now + 300:
            conversations.setdefault(context, []).append((ts, context, redact_summary_codes(text)))
            if guid:
                chats.setdefault(context, set()).add(guid)
    items = []
    for context, rows in conversations.items():
        # A sender who is only a number or a short code that the user never
        # texted is a campaign or a service ("Vote YES on Measure I"), not a person.
        bare = context[len("Group "):].strip().strip('"') if context.startswith("Group ") else context
        bare = re.sub(r"\s*\(conversation \d+\)\s*$", "", bare).strip()
        if counts is not None:
            tally(counts, own, texts=1)
        if (_LITERAL_LABEL.match(bare) or re.fullmatch(r"\d{3,8}", bare)) and context not in ever_sent:
            if counts is not None:
                tally(counts, own, filtered=1)
            continue
        rows = rows[-MAX_TEXT_LINES:]
        incoming = [r for r in rows if r[2].partition(":")[0].strip() != "Me"]
        if not incoming:
            continue
        group = context.startswith("Group ")
        label = clean(context[len("Group "):].strip().strip('"') if group else context, 60)
        replied = rows[-1][2].partition(":")[0].strip() == "Me"
        rendered = _render(rows)
        text = ("Conversation: " + label + (" (group)" if group else "") + "\n" + "\n".join(rendered))[:MAX_ITEM_CHARS]
        quotes = []
        for _, _, line in incoming:
            sender, _, body = line.partition(":")
            if body.strip():
                quotes.append(clean(body, 300))
        last_in = incoming[-1]
        speaker = last_in[2].partition(":")[0].strip()
        who = label if not group else (f"{speaker} in {label}" if speaker else label)
        items.append(dict(id="messages:" + _digest(context), source="messages", account=own, raw_account="",
                          who=who, title=label, text=text, lines=quotes, received_ts=last_in[0],
                          replied=replied, group=group, person=True,
                          user_wrote=any(r[2].partition(":")[0].strip() == "Me" for r in rows),
                          handle="" if group else chat_handle(chats.get(context, set())),
                          ref={"kind": "text", "conversation": context, "label": label},
                          reply_ref="messages:" + context, hash=_digest(text, replied)))
    items.sort(key=lambda i: i["received_ts"], reverse=True)
    return items[:MAX_CONVERSATIONS]


# --- notes ------------------------------------------------------------------------


_TODO_LINE = re.compile(r"^\s*(?:[-*]\s+\[ \]\s+|TODO:?\s+|Action(?: item)?:\s+)(.+)", re.I)


def note_items(*, own: str | None = None) -> list[dict]:
    """Explicit unchecked tasks in Notes ("- [ ] …", "TODO: …"): to-dos as written."""
    from service.tools.imessage_tools import redact_summary_codes
    from service.tools.notes_tools import raw_notes_snapshot
    own = own_account() if own is None else own
    snapshot = raw_notes_snapshot()
    if getattr(snapshot, "origin", "") != "live":
        return []
    items = []
    for row in snapshot.records:
        for index, line in enumerate(str(row.get("body") or "").splitlines()):
            match = _TODO_LINE.match(line)
            if not match:
                continue
            quote = clean(redact_summary_codes(match.group(1)), 200)
            if not quote or re.search(r"details omitted", quote, re.I):
                continue
            from service.proactive.guard import source_sensitive
            if source_sensitive(quote):
                continue
            ident = f"{row.get('reference')}:{index}"
            items.append(dict(id="notes:" + _digest(ident), source="notes", account=own, raw_account="",
                              who=clean(row.get("title"), 80), title=quote, text=quote, lines=[quote],
                              received_ts=None, replied=False, ref={"kind": "note", "reference": row.get("reference")},
                              reply_ref=None, hash=_digest(quote)))
            if len(items) >= 60:
                return items
    return items


# --- calendar and reminders ------------------------------------------------------


def _midnight(now: float, tz: str) -> float:
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now, zone)
    return datetime.combine(local.date(), datetime.min.time(), zone).timestamp()


def schedule(store, now: float, tz: str, *, own: str | None = None, linked: set[str] | None = None) -> dict:
    """{"calendar": today's and tomorrow's events, "past_calendar": linked Google accounts' last week, "reminders": open reminders
    (overdue, due by tomorrow, and undated)}, each row {id, title, when_ts,
    all_day, account, source}."""
    own = own_account() if own is None else own
    linked = gmail_accounts() if linked is None else linked
    start = _midnight(now, tz)
    end = start + 2 * 86400
    try:
        rows = store.upcoming(now - 14 * 86400, days=16)
    except Exception:  # noqa: BLE001
        rows = []
    try:
        hidden = store.hidden_commitment_ids(now)
    except Exception:  # noqa: BLE001
        hidden = set()
    calendar, reminders, past = [], [], []
    linked_set = {str(a).casefold() for a in linked}
    for row in rows:
        if row.get("id") in hidden or not _finite(row.get("when_ts")):
            continue
        entry = dict(id=str(row["id"]), title=clean(row.get("title"), 200), when_ts=float(row["when_ts"]),
                     all_day=bool(row.get("all_day")), source=row.get("source"),
                     account=calendar_account(row, linked=linked, own=own) if row.get("source") == "calendar"
                     else own)
        if not entry["title"]:
            continue
        if row.get("source") == "calendar" or row.get("kind") in ("event", "meeting"):
            if start <= entry["when_ts"] < end:
                calendar.append(entry)
            elif (start - PAST_CALENDAR_DAYS * 86400 <= entry["when_ts"] < start
                  and str(entry["account"]).casefold() in linked_set):
                # A linked Google account's last week: a meeting that just
                # happened can need a follow-up.
                past.append(entry)
        elif row.get("source") in ("reminders", "manual") and entry["when_ts"] < end:
            reminders.append(entry)
    try:
        undated = store.undated_reminders()
    except Exception:  # noqa: BLE001
        undated = []
    for row in undated[:20]:
        title = clean(row.get("title"), 200)
        if title:
            reminders.append(dict(id="undated:" + str(row.get("source_id")), title=title, when_ts=None,
                                  all_day=False, source="reminders", account=own))
    calendar.sort(key=lambda r: r["when_ts"])
    past.sort(key=lambda r: r["when_ts"], reverse=True)
    return {"calendar": calendar, "reminders": reminders, "past_calendar": past}


# --- everything -------------------------------------------------------------------


def collect(now: float | None = None, tz: str = "UTC", counts: dict | None = None) -> list[dict]:
    """Every mail, text and note item, each source on its own: one failing
    never takes the others with it. `counts` gets, per account, everything
    looked at, including the bulk mail and campaign texts left out."""
    now = time.time() if now is None else now
    linked, own = gmail_accounts(), own_account()
    items: list[dict] = []
    try:
        from service.memory.identity import user_emails
        mine = {e.casefold() for e in user_emails()}
    except Exception:  # noqa: BLE001
        mine = set()
    try:
        items += mail_items(mail_rows(now, linked), mine=mine, now=now, tz=tz, linked=linked, own=own,
                            counts=counts)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    try:
        items += text_items(text_records(), now=now, own=own, counts=counts)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    try:
        notes = note_items(own=own)
        items += notes
        if counts is not None:
            for note in notes:
                tally(counts, note.get("account") or own, notes=1)
    except Exception:  # noqa: BLE001
        pass
    return items
