"""Follow-ups: someone the user talks to often asked them something a day ago
and nothing answers it, so Wisp adds a reminder to reply and reports it on a
"Wisp did this" card with Undo.

This acts without asking, so it is deliberately narrow: a wrong reminder is
worse than a missed one. Everything here is pure (no store, no I/O): the caller
passes in what the sources already read, and gets back a plan or the reason
there is none. The write, the card, the Undo and the gate belong to the shared
"Let Wisp act for me" path.

IMPORTANT rules:
- An item qualifies only when EVERY check in `plan_for` holds:
  - a text or an email from one person (never a group chat, a thread with
    several other senders, a promo, bulk or automated mail, an account alert
    or a school roll-up);
  - someone the user talks to often: a saved contact the user texted at least
    3 times on 2 different days in the 30 days before the ask, or, for mail,
    a thread the user already wrote in or an address the user mailed on 2
    different days in that window;
  - a direct ask in the message's own words (a question put to the user, or a
    plain request such as "let me know"), not small talk, not a rhetorical tag,
    and not withdrawn later ("nvm", "figured it out");
  - nothing sensitive or withheld, and no link, address or phone number in it;
  - unanswered for at least a day and at most 4 by the thread itself AND by
    Sent mail, which must have been read freshly (no Sent data, no follow-up);
    for a text, no reply in another conversation with the same person either;
  - arrived after the user said yes to "Let Wisp act for me" (`since_ts`);
  - not already in Reminders or the to-dos under the person's name;
  - never planned twice: the fingerprint and, after an Undo or dismiss, the
    person are blocked by the caller (`blocked`).
- Wisp can't see phone calls, so "unanswered" means texts and mail only, and the
  card says so.
- Message text is untrusted: it is compared and quoted, never obeyed. The title
  ("Reply to Maya") and the time come from code, never from the message.
- At most 2 a rolling day and 1 a cycle (`choose`). Never during quiet hours,
  and the reminder's time is never inside them. The caller aborts when a user
  turn starts and audits every action (`audit_fields` carries no message text).
- Nothing outbound, ever: Wisp reminds the user to reply; it never replies.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from service.proactive import guard

DAY_S = 86400
FOLLOW_UP_AFTER_S = DAY_S           # unanswered for a day
MAX_AGE_S = 4 * DAY_S               # the texts window: older asks aren't seen whole
SETTLE_S = 3600                     # the newest line is at least an hour old: not mid-conversation
CONTACT_WINDOW_S = 30 * DAY_S
MIN_TEXTS_OUT = 3                   # "talk to often": the user's own texts in the window...
MIN_DAYS_OUT = 2                    # ...on at least this many different days
MAX_PER_DAY = 2
MAX_PER_CYCLE = 1
PERSON_BLOCK_S = 30 * DAY_S         # after an Undo or dismiss, that person is left alone this long
DUE_LEAD_S = 30 * 60
MAX_LINES = 4                       # the newest unanswered lines read for the ask
NAME_MAX = 40


def _digest(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --- who the user talks to ---------------------------------------------------------


@dataclass
class TextHistory:
    """One conversation's timestamps, oldest first: who wrote when, nothing else."""
    outgoing: list[float] = field(default_factory=list)
    incoming: list[float] = field(default_factory=list)


def text_history(records) -> dict[str, TextHistory]:
    """{conversation: TextHistory} from `sources.text_records()` rows
    (ts, conversation, "Sender: text", ...). Only who and when are kept."""
    out: dict[str, TextHistory] = {}
    for record in records or ():
        try:
            ts, context, text = float(record[0]), str(record[1]), str(record[2])
        except (TypeError, ValueError, IndexError):
            continue
        entry = out.setdefault(context, TextHistory())
        (entry.outgoing if text.partition(":")[0].strip() == "Me" else entry.incoming).append(ts)
    for entry in out.values():
        entry.outgoing.sort()
        entry.incoming.sort()
    return out


def _talks_often(times, before: float) -> bool:
    recent = [t for t in times if before - CONTACT_WINDOW_S <= t < before]
    days = {int(t // DAY_S) for t in recent}
    return len(recent) >= MIN_TEXTS_OUT and len(days) >= MIN_DAYS_OUT


def _streak_start(entry: TextHistory, now: float) -> float | None:
    """When the run of their lines since the user's last one began."""
    last_mine = max((t for t in entry.outgoing if t <= now), default=float("-inf"))
    waiting = [t for t in entry.incoming if last_mine < t <= now]
    return waiting[0] if waiting else None


_LABEL_NUMBER = re.compile(r"^[+\d(][\d() .+-]{2,}$|^\S+@\S+$")


def _saved_contact(label: str) -> bool:
    """A conversation labelled by a contact's name, not a bare number or address."""
    label = str(label or "").strip()
    return bool(label) and not _LABEL_NUMBER.match(label) and not label.startswith("Group ")


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[^\W\d_]+", str(name or "").casefold()) if len(t) >= 2}


# --- the ask ---------------------------------------------------------------------------

_SECOND_PERSON = re.compile(r"\b(?:you|u|ya|your|ur|yours|yourself|y'?all|you'?re|you'?d|you'?ll|you'?ve)\b", re.I)
_PROPOSAL = re.compile(r"\b(?:should|shall|can|could|do|are|will|would)\s+(?:we|i)\b|\bwant\s+to\b|\bwanna\b", re.I)
# A request in so many words, wherever it sits in the sentence.
_REQUEST = re.compile(
    r"\b(?:let\s+me\s+know|lmk|get\s+back\s+to\s+me|text\s+me\s+back|call\s+me\s+back|"
    r"email\s+me|send\s+me|tell\s+me|"
    r"(?:please|pls|plz)\s+(?:let|send|reply|respond|confirm|answer|check|look|review|sign|fill|text|call|email))\b", re.I)
# A bare verb counts only at the start, as an instruction: "Confirm by Friday",
# never "Will confirm later".
_IMPERATIVE = re.compile(r"^\W*(?:reply|respond|confirm|rsvp|answer)\b", re.I)
# Small talk and rhetorical tags: a question, but nothing waits on the answer.
_SMALL_TALK = re.compile(
    r"^\W*(?:(?:hey|hi|hello|yo|hiya|sup)\W+)?(?:"
    r"how\s+(?:are|r)\s+(?:you|u|ya)(?:\s+(?:doing|doin|today))?|how'?s\s+it\s+going|how\s+(?:have|ve)\s+you\s+been|"
    r"how'?s\s+(?:your|ur)\s+(?:day|week|weekend|night|morning)(?:\s+going)?|how\s+was\s+(?:your|ur)\s+\w+|"
    r"wh?at'?s\s+up|wassup|sup|wyd|hbu|wbu|and\s+(?:you|u)|you\s+know|u\s+know|right|ok|okay|lol|"
    r"(?:did|have)\s+(?:you|u)\s+(?:see|seen|watch|watched|hear|heard)\b.*|(?:you|u)\s+see\s+(?:that|this)|"
    r"(?:you|u)\s+(?:up|good|ok|okay|there|alive|around)|miss\s+(?:you|u)|isn'?t\s+it|aren'?t\s+you)"
    r"\W*$", re.I)
_WITHDRAWN = re.compile(
    r"\b(?:nvm|never\s*mind|nevermind|figured\s+it\s+out|all\s+good|no\s+worries|don'?t\s+worry|dw|"
    r"forget\s+it|ignore\s+(?:that|this|my\s+last)|got\s+it\s+(?:now|sorted)|found\s+it|sorted\s+it|"
    r"no\s+need|disregard)\b", re.I)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
_QUOTED_REPLY = re.compile(r"^\s*>|^\s*On .{3,80} wrote:\s*$|^-{2,}\s*Original Message", re.I)


def _sentences(text: str) -> list[str]:
    kept = []
    for line in str(text or "").splitlines():
        if _QUOTED_REPLY.search(line):
            break
        kept.append(line)
    return [s.strip() for s in _SENTENCE.findall("\n".join(kept)) if len(s.strip()) >= 3]


def is_ask(sentence: str) -> bool:
    """A sentence that puts something to the user that waits on their answer."""
    sentence = str(sentence or "").strip()
    if not sentence or len(sentence) > 300 or _SMALL_TALK.match(sentence):
        return False
    words = re.findall(r"[\w']+", sentence)
    if len(words) < 3:
        return False
    if sentence.rstrip().endswith("?"):
        return bool(_SECOND_PERSON.search(sentence) or _PROPOSAL.search(sentence) or _REQUEST.search(sentence))
    if re.match(r"(?i)^\W*(?:i|we|i'?ll|we'?ll|i'?m|we'?re)\b", sentence):
        return False
    return bool(_REQUEST.search(sentence) or _IMPERATIVE.match(sentence))


def find_ask(lines) -> tuple[str, str]:
    """(the newest ask, "") or ("", why not), from the unanswered lines oldest
    first. An ask withdrawn by a later line ("nvm") counts as answered."""
    lines = [str(line or "") for line in lines or () if str(line or "").strip()]
    ask_at = None
    sentences = [(i, s) for i, line in enumerate(lines) for s in _sentences(line)]
    for index, (_, sentence) in enumerate(sentences):
        if is_ask(sentence):
            ask_at = index
    if ask_at is None:
        return "", "no direct ask"
    if any(_WITHDRAWN.search(s) for _, s in sentences[ask_at + 1:]) or \
            _WITHDRAWN.search(sentences[ask_at][1]):
        return "", "the ask was withdrawn"
    return sentences[ask_at][1], ""


# --- mail -----------------------------------------------------------------------------

_BLOCK = re.compile(r"(?:^|\n)From (.+?) · [^\n]*?:\n")


def _mail_blocks(item: dict) -> list[tuple[str, str]]:
    """(speaker, text) for each message the item shows, oldest first."""
    parts = _BLOCK.split(item.get("text") or "")
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def _other_senders(item: dict) -> set[str]:
    return {speaker.casefold() for speaker, _ in _mail_blocks(item) if not speaker.startswith("You (the user)")}


def _mail_ask_lines(item: dict) -> list[str]:
    """The subject and the newest incoming message: what waits on the user."""
    blocks = [(s, t) for s, t in _mail_blocks(item) if not s.startswith("You (the user)")]
    body = blocks[-1][1].strip() if blocks else ""
    body = "" if body == "(subject only)" else body
    subject = str((item.get("ref") or {}).get("subject") or item.get("title") or "")
    return [subject, body] if body else [subject]


def _mail_address(item: dict) -> str:
    return str((item.get("ref") or {}).get("to") or "").strip().casefold()


def _automated_address(address: str) -> bool:
    from service.attention.exclusions import _AUTOMATED_ADDRESS
    return bool(_AUTOMATED_ADDRESS.search(address or ""))


def _sent_times_to(sent, address: str, name: str, mine) -> list[float]:
    """When the user's sent mail went to this person: by address, or by a
    recipient whose name holds every word of `name` (one word is enough to
    match, which errs toward "already answered")."""
    from service.proactive.sent_check import _names_match, recipients
    names = frozenset(_name_tokens(name))
    out = []
    for row in sent or ():
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        for display, addr in recipients(row.get("to")):
            if addr in mine:
                continue
            if (address and addr == address) or (names and _names_match(names, display, addr)):
                out.append(ts)
                break
    return sorted(out)


# --- the plan -------------------------------------------------------------------------


def person_key(item: dict) -> str:
    """The person, stable across new messages: the conversation or the sender."""
    if item.get("source") == "mail":
        return "followup-person:" + _digest("mail", _mail_address(item) or item.get("who"))
    return "followup-person:" + _digest("messages", (item.get("ref") or {}).get("conversation") or item.get("title"))


def fingerprint(item: dict, ask_ts: float) -> str:
    """One per unanswered ask: the person and when the waiting began."""
    return "followup:" + _digest(person_key(item), int(float(ask_ts) // 60))


def _display_name(item: dict) -> str:
    """The person's name for a title, from the contact label or the sender's display
    name: never a number, an address or anything that reads like one."""
    raw = item.get("title") if item.get("source") == "messages" else item.get("who")
    name = guard.clean(re.sub(r"[\"“”‘’<>\[\](){}]", " ", str(raw or "")), 80)
    if not name or len(name) > NAME_MAX or not re.search(r"[^\W\d_]", name):
        return ""
    if guard.has_contact_or_link(name) or guard.output_has_pii(name) or _LABEL_NUMBER.match(name):
        return ""
    return name


def due_time(now: float, tz: str, quiet=None) -> float:
    """When the reminder comes up: the next half hour at least 30 minutes away,
    moved past quiet hours. From code, never from the message."""
    zone = ZoneInfo(tz)
    at = datetime.fromtimestamp(now + DUE_LEAD_S, zone)
    at = at.replace(second=0, microsecond=0)
    if at.minute not in (0, 30):
        at = at.replace(minute=30) if at.minute < 30 else at.replace(minute=0) + timedelta(hours=1)
    for _ in range(96):
        if quiet is None or not quiet(at):
            break
        at += timedelta(minutes=30)
    return at.timestamp()


def _in_quiet(quiet, now: float, tz: str) -> bool:
    try:
        return bool(quiet(datetime.fromtimestamp(now, ZoneInfo(tz))))
    except Exception:  # noqa: BLE001 — hours that can't be read: hold, never act
        return True


def default_quiet(moment: datetime) -> bool:
    from service.user_hours import in_quiet_hours
    return in_quiet_hours(moment)


def _already_a_reminder(name: str, reminders) -> bool:
    """A reminder or to-do already names this person. Errs toward "already there":
    any open one with the person's name in its title."""
    tokens = _name_tokens(name)
    if not tokens:
        return True
    for row in reminders or ():
        title = row.get("title") if isinstance(row, dict) else row
        if tokens & _name_tokens(title):
            return True
    return False


def _age_phrase(seconds: float) -> str:
    days = int(seconds // DAY_S)
    return "yesterday" if days <= 1 else f"{days} days ago"


def plan_for(item: dict, *, now: float, tz: str, history: dict | None = None, sent=None,
             sent_fresh: bool = False, mine=(), reminders=(), result: dict | None = None,
             since_ts: float | None = None, blocked=frozenset(), quiet=default_quiet) -> tuple[dict | None, str]:
    """(plan, "") for an unanswered ask Wisp should remind the user about, else
    (None, why not). Pure: no store, no I/O. `history` is `text_history(...)`,
    `sent` the user's sent mail rows, `sent_fresh` whether that list was read
    this cycle, `mine` the user's own addresses, `reminders` the open reminders
    and to-dos (titles or rows), `result` the judge's verdict on the item,
    `since_ts` when the user said yes to acting for them, `blocked` the
    fingerprints and person keys never to plan again."""
    from service.proactive import triage
    source = item.get("source")
    if source not in ("mail", "messages"):
        return None, "not mail or texts"
    if since_ts is None:
        return None, "acting for the user is off"
    if _in_quiet(quiet, now, tz):
        return None, "quiet hours"
    if item.get("critical") or item.get("digest") or item.get("recall"):
        return None, "not a person"
    if not item.get("person"):
        return None, "not a person"
    if item.get("group"):
        return None, "group chat"
    if item.get("replied"):
        return None, "answered in the thread"
    if triage.withheld(item) or item.get("secret"):
        return None, "withheld"
    if isinstance(result, dict) and (result.get("sensitive") or result.get("junk")):
        return None, "sensitive or junk"
    if not sent_fresh or sent is None:
        return None, "sent mail not checked"
    mine = {str(a).casefold() for a in mine or ()}
    name = _display_name(item)
    if not name:
        return None, "no safe name"
    if person_key(item) in blocked:
        return None, "undone or dismissed before"

    # Who, when the waiting began, and what was asked.
    if source == "messages":
        context = str((item.get("ref") or {}).get("conversation") or "")
        if not context or context.startswith("Group ") or not _saved_contact(item.get("title") or ""):
            return None, "not a saved contact"
        entry = (history or {}).get(context)
        if entry is None:
            return None, "no text history"
        ask_ts = _streak_start(entry, now)
        if ask_ts is None:
            return None, "answered in the thread"
        if not _talks_often(entry.outgoing, ask_ts):
            return None, "not someone the user texts often"
        newest = max(t for t in entry.incoming if t <= now)
        lines = list(item.get("pending") or []) or list(item.get("lines") or [])
        lines = lines[-MAX_LINES:]
    else:
        address = _mail_address(item)
        if not address or address in mine or _automated_address(address):
            return None, "not a person"
        if len(_other_senders(item)) > 1:
            return None, "several people in the thread"
        ask_ts = newest = item.get("received_ts")
        if type(ask_ts) not in (int, float):
            return None, "no arrival time"
        wrote_before = [t for t in _sent_times_to(sent, address, "", mine)
                        if ask_ts - CONTACT_WINDOW_S <= t < ask_ts]
        if not item.get("user_wrote") and len({int(t // DAY_S) for t in wrote_before}) < MIN_DAYS_OUT:
            return None, "not someone the user writes to often"
        lines = _mail_ask_lines(item)

    if ask_ts < since_ts:
        return None, "arrived before the yes"
    waited = now - ask_ts
    if waited < FOLLOW_UP_AFTER_S:
        return None, "not a day yet"
    if waited > MAX_AGE_S:
        return None, "too old"
    if now - newest < SETTLE_S:
        return None, "still talking"

    text = "\n".join(lines)
    if guard.source_sensitive(text) or guard.output_has_pii(text):
        return None, "sensitive"
    try:
        from service.attention.exclusions import contact_vector
        vector = contact_vector(text)
    except Exception:  # noqa: BLE001 — unreadable: treat as a pointer somewhere
        vector = True
    if vector or guard.has_contact_or_link(text):
        return None, "a link, address or number"
    try:
        from service.attention.corpus import Item
        from service.attention.exclusions import screen
        screened = screen(Item(id="", source=source, ts=float(ask_ts), direction="incoming",
                               sender=str(item.get("who") or ""),
                               conversation=_mail_address(item) if source == "mail" else str(item.get("title") or ""),
                               text=text))
    except Exception:  # noqa: BLE001 — a screen that can't run excludes
        screened = "unscreened"
    if screened:
        return None, f"screened: {screened}"
    ask, why = find_ask(lines)
    if not ask:
        return None, why

    # Nothing answers it elsewhere: Sent mail to them after the ask, or (for a
    # text) the user writing in another conversation with the same name.
    if [t for t in _sent_times_to(sent, _mail_address(item) if source == "mail" else "", name, mine)
            if ask_ts < t <= now + 300]:
        return None, "answered by sent mail"
    if source == "messages":
        tokens = _name_tokens(name)
        for other, entry in (history or {}).items():
            if other == context or not tokens & _name_tokens(other):
                continue
            if any(ask_ts < t <= now for t in entry.outgoing):
                return None, "answered in another conversation"
    if _already_a_reminder(name, reminders):
        return None, "already in Reminders"
    fp = fingerprint(item, ask_ts)
    if fp in blocked:
        return None, "planned before"

    channel = "email" if source == "mail" else "text"
    title = guard.clean_title(f"Reply to {name}")
    due = due_time(now, tz, quiet)
    quote = guard.clean(ask, 160)
    return {
        "kind": "reminder", "action": "followup", "title": title, "due_ts": float(due),
        "quote": quote, "notes": f"{name} asked: “{quote}”",
        "why": f"{name} asked {_age_phrase(waited)} by {channel}, and there's no reply in Messages or Mail. "
               "Wisp can't see calls.",
        "fingerprint": fp, "person_key": person_key(item), "item_id": item.get("id"), "source": source,
        "channel": channel, "who": name, "account": item.get("account", ""), "received_ts": float(ask_ts),
        "has_clock": True, "timezone": tz, "item_fingerprint": f"{item.get('id')}:{item.get('hash')}",
    }, ""


def choose(plans, *, done_today: int, now: float) -> list[dict]:
    """The plans to act on this cycle: the longest-waiting first, at most one a
    cycle and never past MAX_PER_DAY in a rolling day (`done_today`, counted by
    the caller from its records), one per person."""
    room = max(0, min(MAX_PER_CYCLE, MAX_PER_DAY - int(done_today)))
    picked, people = [], set()
    for plan in sorted(plans or (), key=lambda p: p["received_ts"]):
        if len(picked) >= room:
            break
        if plan["person_key"] in people:
            continue
        people.add(plan["person_key"])
        picked.append(plan)
    return picked


def audit_fields(plan: dict) -> dict:
    """What the audit line records: no message text, no name in the clear."""
    return {"fingerprint": plan["fingerprint"], "person": plan["person_key"], "source": plan["source"],
            "title_sha256": hashlib.sha256(plan["title"].encode()).hexdigest()[:16],
            "due_ts": plan["due_ts"], "received_ts": plan["received_ts"]}


# --- one cycle ------------------------------------------------------------------------


async def read_sent_fresh() -> tuple[list[dict], bool]:
    """The user's sent mail and whether it was read just now: Apple Mail's Sent
    mailboxes through the app (its own index on disk, never the network) plus the
    linked Gmail accounts' Sent label. A failed read is not fresh, so nothing is
    planned on it."""
    from service.proactive import sources
    gmail = sources.sent_rows()
    try:
        from service.mail_memory import bridge
        rows, _capped = await bridge.read_sent(sources.NATIVE_SENT_DAYS)
    except Exception:  # noqa: BLE001 — the app is away or Mail can't be read: not fresh
        return gmail, False
    return gmail + list(rows), True


async def plans(items, *, now: float, tz: str, records, reminders=(), results=None,
                since_ts: float | None, blocked=frozenset(), done_today: int = 0,
                quiet=default_quiet, read_sent=read_sent_fresh) -> tuple[list[dict], dict]:
    """(the plans to act on this cycle, {item id: why not}) for the cycle's items.

    Sent mail is read only when some item passes every other check, so a cycle
    with nothing waiting costs no read. Writes nothing: the caller checks for a
    user turn, writes through the verified reminder path, records the Undo,
    audits (`audit_fields`) and makes the card."""
    if since_ts is None or done_today >= MAX_PER_DAY:
        return [], {}
    history = text_history(records)
    results = results or {}
    common = dict(now=now, tz=tz, history=history, reminders=reminders, since_ts=since_ts,
                  blocked=blocked, quiet=quiet)
    why: dict[str, str] = {}
    hopeful = []
    for item in items or ():
        # Everything but Sent mail first: an empty, "fresh" list is only a filter.
        got, reason = plan_for(item, sent=[], sent_fresh=True, mine=(), result=results.get(item.get("id")),
                               **common)
        if got is None:
            why[str(item.get("id"))] = reason
        else:
            hopeful.append(item)
    if not hopeful:
        return [], why
    sent, fresh = await read_sent()
    from service.proactive.sent_check import own_addresses
    mine = own_addresses(sent)
    ready = []
    for item in hopeful:
        got, reason = plan_for(item, sent=sent, sent_fresh=fresh, mine=mine,
                               result=results.get(item.get("id")), **common)
        if got is None:
            why[str(item.get("id"))] = reason
        else:
            ready.append(got)
    return choose(ready, done_today=done_today, now=now), why
