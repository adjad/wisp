"""Follow-ups: someone the user writes to asked them something by email a day
ago and nothing answers it, so Wisp adds a reminder to reply and reports it on a
"Wisp did this" card with Undo.

This acts without asking, so it is deliberately narrow: a wrong reminder is
worse than a missed one. `plan_for` is pure (no store, no I/O); `step` is the
background cycle's one call: it plans, re-checks the gate, writes through the
verified reminder path, and records the action with its Undo before any card.

IMPORTANT rules:
- Mail only for now. Texts stay off (TEXTS_ENABLED) until the user's own
  reactions reach the cache: a "Loved" ask reads as unanswered today.
- An email qualifies only when EVERY check in `plan_for` holds:
  - from one person (never a thread with several other senders, a promo, bulk
    or automated mail, an account alert or a school roll-up), addressed to the
    user in To with at most one other recipient, and not from the user;
  - someone the user writes to: Sent mail to that exact address before the ask
    on two different local days in 30, or once in that very thread. A shared
    subject alone never counts;
  - named by the user's own Contacts entry for the address, never by the name
    the sender chose, and not a sender whose display name holds an address;
  - a direct ask in the message's own words (a question put to the user, or a
    plain request such as "let me know"), not small talk, rhetorical, negated,
    reported or put to a crowd, and not withdrawn later ("nvm");
  - nothing sensitive or withheld, and no link, address or phone number in it;
  - unanswered for one to four days by the thread AND by Sent mail that is
    known to be current for that account: a Google account signed in and
    synced in the last 10 minutes, or Apple Mail's index after Mail has been
    running and the Mac awake 30 minutes with a Mail read since it woke;
  - arrived after the user's yes to "Let Wisp act for me", judged by the ask's
    own arrival (a subject ask by the thread's first message);
  - not already in Reminders or the to-dos under the person's name, not on a
    card the user dismissed or acted on during this wait, and not a message
    auto-add already acted on;
  - never twice: by fingerprint, by person for 30 days after an Undo or a
    dismissed card, and one follow-up a week per person whatever came of it.
- Wisp can't see phone calls, and a reply after the reminder is written leaves
  it standing; the card says both. Cc isn't in any cache, so "the user in To
  plus a big Cc" is a known limit.
- Message text is untrusted: it is compared and quoted, never obeyed. The title
  ("Reply to Priya Shah") and the time come from code, never from the message.
- At most 2 a rolling day and 1 a cycle, inside the shared cap. Never during
  quiet hours, and the reminder's time is never inside them. A starting turn
  stops the Sent read and the write; the audit line carries no message text.
- Nothing outbound, ever: Wisp reminds the user to reply; it never replies.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from service.proactive import guard

DAY_S = 86400
FOLLOW_UP_AFTER_S = DAY_S           # unanswered for a day
MAX_AGE_S = 4 * DAY_S               # the texts window: older asks aren't seen whole
CONTACT_WINDOW_S = 30 * DAY_S
MIN_TEXTS_OUT = 3                   # "talk to often": the user's own texts in the window...
MIN_DAYS_OUT = 2                    # ...on at least this many different days
MAX_PER_DAY = 2
MAX_PER_CYCLE = 1
PERSON_BLOCK_S = 30 * DAY_S         # after an Undo or dismiss, that person is left alone this long
PERSON_REST_S = 7 * DAY_S           # after any follow-up, the next one for that person waits this long
DUE_LEAD_S = 30 * 60
MAX_LINES = 4                       # the newest unanswered lines read for the ask
NAME_MAX = 40
MAX_TO = 2                          # the user plus at most one other in To
SENT_EVERY_S = 300.0                # Sent mail is read at most this often
SENT_TIMEOUT_S = 10.0
SENT_FRESH_S = 600.0                # a linked Gmail account's Sent synced within this, or it's stale
AWAKE_GRACE_S = 30 * 60             # Apple Mail's Sent counts only after this long awake with Mail running
SLEEP_JUMP_S = 120.0                # wall clock running ahead of the monotonic clock by this much = a sleep
NATIVE_SENT_DAYS = 31               # the app's limit: covers the 30-day "writes to often" window
# Texts stay off until the user's own tapbacks reach the cache: a "Loved" ask
# reads as unanswered today (MessagesReader drops every reaction).
TEXTS_ENABLED = False


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


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
        if not math.isfinite(ts):
            continue
        entry = out.setdefault(context, TextHistory())
        (entry.outgoing if text.partition(":")[0].strip() == "Me" else entry.incoming).append(ts)
    for entry in out.values():
        entry.outgoing.sort()
        entry.incoming.sort()
    return out


def _local_days(times, tz: str) -> set:
    """The calendar dates, in the user's own time zone, the times fall on."""
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — no readable zone: UTC dates
        zone = ZoneInfo("UTC")
    return {datetime.fromtimestamp(t, zone).date() for t in times}


def _talks_often(times, before: float, tz: str = "UTC") -> bool:
    recent = [t for t in times if before - CONTACT_WINDOW_S <= t < before]
    return len(recent) >= MIN_TEXTS_OUT and len(_local_days(recent, tz)) >= MIN_DAYS_OUT


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


def _visible(text) -> str:
    """Text without control, format (bidi overrides, zero-width) or private-use
    characters, which can make a name or a quote read as something it isn't."""
    return "".join(ch for ch in str(text or "")
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[^\W\d_]+", str(name or "").casefold()) if len(t) >= 2}


# --- the ask ---------------------------------------------------------------------------

_SECOND_PERSON = re.compile(r"\b(?:you|u|ya|your|ur|yours|yourself|y'?all|you'?re|you'?d|you'?ll|you'?ve)\b", re.I)
_PROPOSAL = re.compile(r"\b(?:should|shall|can|could|do|are|will|would)\s+(?:we|i)\b|\bwant\s+to\b|\bwanna\b", re.I)
# A request in so many words, at the start of a sentence (after a greeting or a
# "please" at most): "Let me know if Friday works", never "John will send me the
# keys" or "Mom says you can email me".
_LEAD = r"^\W*(?:(?:hey|hi|hello|also|and|so|ok|okay|oh|but|btw|plus)\W+)*(?:(?:please|pls|plz)\W+)?"
_REQUEST = re.compile(
    _LEAD + r"(?:let\s+me\s+know|lmk|get\s+back\s+to\s+me|text\s+me\s+back|call\s+me\s+back|"
    r"email\s+me|send\s+me|reply|respond|confirm|rsvp|answer)\b", re.I)
_PLEASE = re.compile(_LEAD.replace(r"(?:(?:please|pls|plz)\W+)?", r"(?:please|pls|plz)\W+") +
                     r"(?:let|send|reply|respond|confirm|answer|check|look|review|sign|fill|text|call|email|tell)\b",
                     re.I)
_NOT_AN_ASK = re.compile(
    r"\b(?:don'?t|do\s+not|never|no\s+need\s+to|says|said|told|tells|asked\s+if)\b|"
    r"^\W*tell\s+me\s+about\s+it\b", re.I)
_RHETORICAL = re.compile(
    r"\b(?:can|could|would)\s+(?:you|u)\s+(?:believe|imagine)\b|\bare\s+(?:you|u)\s+(?:kidding|serious|for\s+real)\b|"
    r"\b(?:you|u)\s+know\s+what\s+i\s+mean\b|\bguess\s+what\b|\bwho\s+knew\b|\bwhy\s+not\b", re.I)
# An ask put to a crowd: a team mail, a class list, a group someone forwarded.
_CROWD = re.compile(r"\b(?:you\s+all|y'?all|all\s+of\s+you|everyone|everybody|each\s+of\s+you|"
                    r"team|guys|folks|you\s+both)\b", re.I)
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
    if _NOT_AN_ASK.search(sentence) or _RHETORICAL.search(sentence) or _CROWD.search(sentence):
        return False
    if sentence.rstrip().endswith("?"):
        return bool(_SECOND_PERSON.search(sentence) or _PROPOSAL.search(sentence))
    return bool(_REQUEST.match(sentence) or _PLEASE.match(sentence))


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
    """The newest incoming message: what waits on the user. The subject counts only
    when it is theirs alone: in a thread the user wrote in, "Re: Can we meet
    Friday?" is the user's own question coming back."""
    blocks = _mail_blocks(item)
    incoming = [t for s, t in blocks if not s.startswith("You (the user)")]
    body = incoming[-1].strip() if incoming else ""
    body = "" if body == "(subject only)" else body
    if body:
        return [body]
    if any(s.startswith("You (the user)") for s, _ in blocks) or item.get("user_wrote"):
        return []
    subject = str((item.get("ref") or {}).get("subject") or item.get("title") or "")
    return [re.sub(r"(?i)^\s*(?:(?:re|fwd?|aw|sv)\s*:\s*)+", "", subject)]


def _spoofed_sender(item: dict) -> bool:
    """A From whose display name itself holds an address ("Maya <maya@x>" <evil@y>):
    the name and the address can then say different people. Checked on the raw
    header when the item carries it."""
    raw = str(item.get("sender_raw") or "")
    if "<" in raw:
        display = raw[:raw.rfind("<")]
        if "@" in display or "<" in display:
            return True
    who = str(item.get("who") or "")
    return "@" in who or "<" in who


def _mail_address(item: dict) -> str:
    return str((item.get("ref") or {}).get("to") or "").strip().casefold()


def _automated_address(address: str) -> bool:
    from service.attention.exclusions import _AUTOMATED_ADDRESS
    return bool(_AUTOMATED_ADDRESS.search(address or ""))


def _writes_to(sent, address: str, item: dict, ask_ts: float, mine, tz: str) -> bool:
    from service.proactive.sent_check import recipients
    subject = re.sub(r"(?i)^\s*(?:(?:re|fwd?|aw|sv)\s*:\s*)+", "",
                     str((item.get("ref") or {}).get("subject") or item.get("title") or "")).strip().casefold()
    days, in_thread = [], False
    for row in sent or ():
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not (ask_ts - CONTACT_WINDOW_S <= ts < ask_ts):
            continue
        if address not in {a for _, a in recipients(row.get("to")) if a not in mine}:
            continue
        days.append(ts)
        bare = re.sub(r"(?i)^\s*(?:(?:re|fwd?|aw|sv)\s*:\s*)+", "", str(row.get("subject") or "")).strip().casefold()
        in_thread = in_thread or (bool(subject) and bare == subject)
    return in_thread or len(_local_days(days, tz)) >= MIN_DAYS_OUT


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


def contacts_name(address: str) -> str:
    """The user's own Contacts name for an address, or "" (no Contacts, no entry)."""
    try:
        from service.tools.imessage_tools import contact_name_for_handle
        return str(contact_name_for_handle(address) or "")
    except Exception:  # noqa: BLE001
        return ""


def _display_name(item: dict, lookup=contacts_name) -> str:
    """The person's name for a title: the contact label of a text, the user's own
    Contacts name for a mail sender (never the name the sender chose for
    themself). Never a number, an address or anything that reads like one."""
    raw = item.get("title") if item.get("source") == "messages" else lookup(_mail_address(item))
    name = guard.clean(re.sub(r"[\"“”‘’<>\[\](){}]", " ", _visible(raw)), 80)
    if not name or len(name) > NAME_MAX or not re.search(r"[^\W\d_]", name):
        return ""
    if guard.has_contact_or_link(name) or guard.output_has_pii(name) or _LABEL_NUMBER.match(name):
        return ""
    return name


def due_time(now: float, tz: str, quiet=None) -> float | None:
    """When the reminder comes up: the next half hour at least 30 minutes away,
    moved past quiet hours. From code, never from the message."""
    try:
        zone = ZoneInfo(tz)
        at = datetime.fromtimestamp(now + DUE_LEAD_S, zone)
    except Exception:  # noqa: BLE001 — no readable time zone or time: no time
        return None
    at = at.replace(second=0, microsecond=0)
    if at.minute not in (0, 30):
        at = at.replace(minute=30) if at.minute < 30 else at.replace(minute=0) + timedelta(hours=1)
    for _ in range(96):
        try:
            if quiet is None or not quiet(at):
                return at.timestamp()
        except Exception:  # noqa: BLE001 — hours that can't be read: no time
            return None
        at += timedelta(minutes=30)
    return None


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
             since_ts: float | None = None, blocked=frozenset(), quiet=default_quiet,
             defer_sent: bool = False, self_names=(), waved=None,
             stale_accounts=frozenset(), lookup=contacts_name, gmail_current=frozenset(),
             apple_current: bool = False) -> tuple[dict | None, str]:
    """(plan, "") for an unanswered ask Wisp should remind the user about, else
    (None, why not). Pure: no store, no I/O. `history` is `text_history(...)`,
    `sent` the user's sent mail rows, `sent_fresh` whether that list was read
    this cycle, `mine` the user's own addresses, `reminders` the open reminders
    and to-dos (titles or rows), `result` the judge's verdict on the item,
    `since_ts` when the user said yes to acting for them, `blocked` the
    fingerprints and person keys never to plan again. `defer_sent` (the
    first pass in `plans`) leaves every check that needs Sent mail for later.
    `self_names` are the user's own names (a note to self is never a follow-up),
    `waved` {item id: newest ask time whose card the user dismissed or acted on},
    `stale_accounts` the linked Google accounts whose Sent mail isn't known to be
    current, `gmail_current` those whose Sent mail is, and `apple_current`
    whether Apple Mail's own Sent index can be trusted to hold a recent reply
    (`apple_mail_current`)."""
    from service.proactive import triage
    source = item.get("source")
    if source not in ("mail", "messages"):
        return None, "not mail or texts"
    if source == "messages" and not TEXTS_ENABLED:
        return None, "texts are off: reactions aren't visible yet"
    if not _finite(since_ts):
        return None, "acting for the user is off"
    blocked = blocked or frozenset()
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
    if sent is None or (source == "messages" and not sent_fresh):
        return None, "sent mail not checked"
    mine = {str(a).casefold() for a in mine or ()}
    name = _display_name(item, lookup)
    if not name:
        return None, "not in your Contacts" if source == "mail" else "no safe name"
    if person_key(item) in blocked:
        return None, "undone or dismissed before"
    me = set()
    for own in self_names or ():
        me |= _name_tokens(own)
    if me and _name_tokens(name) and _name_tokens(name) <= me:
        return None, "the user themself"

    # Who, when the waiting began, and what was asked.
    consent_ts = None
    if source == "messages":
        context = str((item.get("ref") or {}).get("conversation") or "")
        if not context or context.startswith("Group ") or not _saved_contact(item.get("title") or ""):
            return None, "not a saved contact"
        # One-to-one for certain: the native chat is a single person's, not a group
        # the cache labelled by one member's name.
        if not str(item.get("handle") or "").strip():
            return None, "not a known one-to-one chat"
        entry = (history or {}).get(context)
        if entry is None:
            return None, "no text history"
        ask_ts = _streak_start(entry, now)
        if ask_ts is None:
            return None, "answered in the thread"
        if not _talks_often(entry.outgoing, ask_ts, tz):
            return None, "not someone the user texts often"
        newest = max(t for t in entry.incoming if t <= now)
        lines = list(item.get("pending") or []) or list(item.get("lines") or [])
        lines = lines[-MAX_LINES:]
    else:
        address = _mail_address(item)
        if not address or address in mine or _automated_address(address):
            return None, "not a person"
        if _spoofed_sender(item):
            return None, "the sender's name hides an address"
        if len(_other_senders(item)) > 1:
            return None, "several people in the thread"
        from service.proactive.sent_check import recipients
        to = {a for _, a in recipients(item.get("to_header"))}
        if not to:
            return None, "not addressed to the user"
        if len(to) > MAX_TO:
            return None, "sent to several people"
        # Who "the user" is comes partly from Sent mail: checked once it is read.
        if not defer_sent and not to & mine:
            return None, "not addressed to the user"
        # A reply sent from another device must be visible in Sent before "unanswered"
        # means anything: a current Google Sent sync for this account, or Apple Mail's
        # index after Mail has been running and awake long enough to have fetched it.
        stale_accounts = set(stale_accounts or ())
        accounts = {str(item.get(k) or "").casefold() for k in ("account", "raw_account")} - {""}
        if not defer_sent:
            if "*" in stale_accounts or accounts & stale_accounts:
                return None, "sent mail not checked"
            if not accounts & set(gmail_current or ()) and not (sent_fresh and apple_current):
                return None, "sent mail not checked"
        ask_ts = newest = item.get("received_ts")
        if not _finite(ask_ts):
            return None, "no arrival time"
        # Someone the user writes to: Sent mail to this exact address before the ask,
        # on two different days, or once in this very thread. A shared subject alone
        # never counts: anyone can send "Quick question".
        if not defer_sent and not _writes_to(sent, address, item, ask_ts, mine, tz):
            return None, "not someone the user writes to often"
        lines = _mail_ask_lines(item)
        if lines and not any(t.strip() and t.strip() != "(subject only)" for s_, t in _mail_blocks(item)
                             if not s_.startswith("You (the user)")):
            # The ask is the subject itself: it is as old as the thread.
            first = item.get("subject_ts")
            consent_ts = min(ask_ts, float(first)) if _finite(first) else ask_ts

    consent_ts = ask_ts if consent_ts is None else consent_ts
    if min(ask_ts, consent_ts) < since_ts:
        return None, "arrived before the yes"
    if (waved or {}).get(str(item.get("id")), float("-inf")) >= int(ask_ts):
        return None, "its card was dismissed or acted on"
    waited = now - ask_ts
    # Every line still waiting is a day old, so the ask is too, wherever it sits.
    if now - newest < FOLLOW_UP_AFTER_S:
        return None, "not a day yet"
    if waited > MAX_AGE_S:
        return None, "too old"

    lines = [_visible(line) for line in lines]
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
    try:
        due = due_time(now, tz, quiet)
    except Exception:  # noqa: BLE001 — hours that can't be read: no time to pick
        due = None
    if due is None:
        return None, "no time outside quiet hours"
    quote = guard.clean(ask, 160)
    return {
        "kind": "reminder", "action": "followup", "title": title, "due_ts": float(due),
        "quote": quote,
        "why": f"{name} asked {_age_phrase(waited)} by {channel}, and there's no reply in "
               + ("Mail. " if source == "mail" else "Messages or Mail. ") + "Wisp can't see calls.",
        "fingerprint": fp, "person_key": person_key(item), "item_id": item.get("id"), "source": source,
        "channel": channel, "who": name, "account": item.get("account", ""), "received_ts": float(ask_ts),
        "consent_ts": float(min(ask_ts, consent_ts)),
        "has_clock": True, "timezone": tz, "item_fingerprint": f"{item.get('id')}:{item.get('hash')}",
    }, ""


def choose(plans, *, done_today: int, now: float) -> list[dict]:
    """The plans to act on this cycle: the longest-waiting first, at most one a
    cycle and never past MAX_PER_DAY in a rolling day (`done_today`, counted by
    the caller from its records), one per person."""
    try:
        room = max(0, min(MAX_PER_CYCLE, MAX_PER_DAY - int(done_today)))
    except (TypeError, ValueError):
        return []
    picked, people = [], set()
    valid = [p for p in plans or () if isinstance(p, dict) and p.get("person_key") and _finite(p.get("received_ts"))]
    for plan in sorted(valid, key=lambda p: p["received_ts"]):
        if len(picked) >= room:
            break
        if plan["person_key"] in people:
            continue
        people.add(plan["person_key"])
        picked.append(plan)
    return picked


def audit_fields(plan: dict) -> dict:
    """What the audit line records: no message text, no name in the clear."""
    return {"fingerprint": plan.get("fingerprint"), "person": plan.get("person_key"), "source": plan.get("source"),
            "title_sha256": hashlib.sha256(str(plan.get("title") or "").encode()).hexdigest()[:16],
            "due_ts": plan.get("due_ts"), "received_ts": plan.get("received_ts")}


# --- one cycle ------------------------------------------------------------------------


_sent_cache: dict = {"at": None, "rows": [], "fresh": False}


async def read_sent_fresh() -> tuple[list[dict], bool]:
    """The user's sent mail and whether Apple Mail's Sent index was read just now:
    the app reads Mail's own index on disk (never the network) for the last 31
    days, at most every SENT_EVERY_S and never longer than SENT_TIMEOUT_S. The
    linked Gmail accounts' Sent rows are taken as they are now, never from a
    cache older than their own freshness verdict. A failed read is not fresh."""
    import asyncio
    from service.proactive import sources
    try:
        gmail = list(sources.sent_rows())
    except Exception:  # noqa: BLE001
        gmail = []
    now = time.time()
    at = _sent_cache["at"]
    if at is None or not 0 <= now - at < SENT_EVERY_S:
        _sent_cache.update(at=now, rows=[], fresh=False)
        try:
            from service.mail_memory import bridge
            rows, _capped = await asyncio.wait_for(
                bridge.read_sent(NATIVE_SENT_DAYS, timeout=SENT_TIMEOUT_S - 1), SENT_TIMEOUT_S)
            _sent_cache.update(rows=list(rows), fresh=True)
        except Exception:  # noqa: BLE001 — the app is away, Mail can't be read, or it took too long: not fresh
            pass
    return gmail + list(_sent_cache["rows"]), bool(_sent_cache["fresh"])


_clock: dict = {"offset": None, "awake_since": None}


def awake_since() -> float:
    """When the Mac last woke, or this service started: the monotonic clock stops
    during sleep, so the wall clock running ahead of it marks a sleep."""
    offset = time.time() - time.monotonic()
    previous = _clock["offset"]
    if previous is None or offset - previous > SLEEP_JUMP_S or _clock["awake_since"] is None:
        _clock["awake_since"] = time.time()
    _clock["offset"] = offset
    return float(_clock["awake_since"])


def _mail_app_running() -> bool:
    try:
        import subprocess
        return subprocess.run(["/usr/bin/pgrep", "-x", "Mail"], capture_output=True, timeout=2).returncode == 0
    except Exception:  # noqa: BLE001 — unknown: not running
        return False


def _native_mail_synced_at() -> float:
    """When the app last finished reading Mail through Mail.app itself, or 0."""
    try:
        from service.tools import email_tools
        if getattr(email_tools, "_email_read_source", "") != "mail_app":
            return 0.0
        return float(email_tools._source_receipt(getattr(email_tools, "_native_headers_at", 0.0)) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def apple_mail_current(now: float | None = None, *, running=_mail_app_running, synced=_native_mail_synced_at,
                       woke=awake_since) -> bool:
    """Apple Mail's Sent index can be trusted to hold a reply sent from another
    device: Mail is running, the Mac has been awake AWAKE_GRACE_S, and the app
    has finished a Mail read since it woke. The index on disk holds only what
    Mail last fetched, so a successful read alone proves nothing."""
    now = time.time() if now is None else now
    try:
        since = float(woke())
        if not running() or now - since < AWAKE_GRACE_S:
            return False
        return float(synced()) > since
    except Exception:  # noqa: BLE001
        return False


def gmail_sent_state(now: float | None = None) -> tuple[set[str], set[str]]:
    """(linked Google accounts whose Sent mail is current, those whose isn't):
    current means signed in and a Sent sync in the last SENT_FRESH_S. A stale
    account's cached Sent list may miss a reply, so its mail is never followed up."""
    now = time.time() if now is None else now
    try:
        from service import gmail_source
        accounts = gmail_source.connected_accounts()
        last = getattr(gmail_source, "_last", {}) or {}
    except Exception:  # noqa: BLE001 — Google state unreadable: no Google account is current
        return set(), {"*"}
    current, stale = set(), set()
    for account, config in (accounts or {}).items():
        synced = (last.get(account) or {}).get("sent") if isinstance(last.get(account), dict) else None
        expired = isinstance(config, dict) and config.get("auth_expired")
        fresh = not expired and _finite(synced) and 0 <= now - synced <= SENT_FRESH_S
        (current if fresh else stale).add(str(account).casefold())
    return current, stale


def stale_gmail_accounts(now: float | None = None) -> set[str]:
    """The linked Google accounts whose Sent mail isn't current (gmail_sent_state)."""
    return gmail_sent_state(now)[1]


def _safely(item, **kw) -> tuple[dict | None, str]:
    """plan_for, but an item it can't read is a refusal, never an error in the cycle."""
    try:
        return plan_for(item, **kw)
    except Exception as exc:  # noqa: BLE001
        return None, f"unreadable: {type(exc).__name__}"


async def plans(items, *, now: float, tz: str, records, reminders=(), results=None,
                since_ts: float | None, blocked=frozenset(), done_today: int = 0,
                quiet=default_quiet, read_sent=read_sent_fresh, abort=None, self_names=(),
                waved=None, gmail=gmail_sent_state, apple=apple_mail_current,
                lookup=contacts_name) -> tuple[list[dict], dict]:
    """(the plans to act on this cycle, {item id: why not}) for the cycle's items.

    Sent mail is read only when some item passes every other check, so a cycle
    with nothing waiting costs no read. Writes nothing: the caller checks for a
    user turn, writes through the verified reminder path, records the Undo,
    audits (`audit_fields`) and makes the card."""
    try:
        done_today = int(done_today)
    except (TypeError, ValueError):
        return [], {}
    if not _finite(since_ts) or done_today >= MAX_PER_DAY:
        return [], {}
    history = text_history(records)
    results = results if isinstance(results, dict) else {}
    blocked = blocked if isinstance(blocked, (set, frozenset)) else frozenset()
    try:
        gmail_current, stale_accounts = gmail(now)
        gmail_current, stale_accounts = set(gmail_current), set(stale_accounts)
    except Exception:  # noqa: BLE001
        gmail_current, stale_accounts = set(), {"*"}
    common = dict(now=now, tz=tz, history=history, reminders=reminders, since_ts=since_ts,
                  blocked=blocked, quiet=quiet, self_names=self_names, waved=waved or {},
                  stale_accounts=stale_accounts, lookup=lookup, gmail_current=gmail_current)
    try:
        from service.proactive.sent_check import own_addresses
        own = own_addresses(())
    except Exception:  # noqa: BLE001
        own = set()
    why: dict[str, str] = {}
    hopeful = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        # Everything but Sent mail first: an empty, "fresh" list is only a filter.
        got, reason = _safely(item, sent=[], sent_fresh=True, mine=own, result=results.get(item.get("id")),
                              defer_sent=True, **common)
        if got is None:
            why[str(item.get("id"))] = reason
        else:
            hopeful.append(item)
    if not hopeful:
        return [], why
    if abort is not None and abort():
        return [], why
    try:
        sent, fresh = await read_sent()
        sent = [r for r in sent if isinstance(r, dict)]
        from service.proactive.sent_check import own_addresses
        mine = own_addresses(sent) | own
    except Exception:  # noqa: BLE001 — Sent mail that can't be read is not fresh
        sent, fresh, mine = [], False, set()
    try:
        apple_ok = bool(fresh) and bool(apple(now))
    except Exception:  # noqa: BLE001
        apple_ok = False
    ready = []
    for item in hopeful:
        got, reason = _safely(item, sent=sent, sent_fresh=bool(fresh), mine=mine, apple_current=apple_ok,
                              result=results.get(item.get("id")), **common)
        if got is None:
            why[str(item.get("id"))] = reason
        else:
            ready.append(got)
    return choose(ready, done_today=done_today, now=now), why


# --- acting: the background cycle's step ------------------------------------------------

KIND = "follow_up"
MAX_ATTEMPTS = 2
_REPLY_METHODS = ("text_reply", "email_reply", "read", "reminder", "add_to_day")


def open_reminder_titles(sched: dict, store, now: float) -> list[str]:
    """Every open reminder, dated or not, and the proactive to-dos, by title."""
    rows = list(sched.get("reminders") or [])
    try:
        from service.proactive.autoadd import default_existing
        rows += list(default_existing(now) or [])
    except Exception:  # noqa: BLE001
        pass
    try:
        from service.assistant.store import assistant_store
        rows += list(assistant_store.undated_reminders() or [])
    except Exception:  # noqa: BLE001 — unreadable: the dated rows and to-dos still count
        pass
    try:
        rows += [t for t in (store.state().get("todos") or []) if not t.get("done")]
    except Exception:  # noqa: BLE001
        pass
    return [str(r.get("title") or "") if isinstance(r, dict) else str(r) for r in rows]


def _blocked(store, now: float) -> tuple[set[str], int, dict]:
    """(fingerprints and person keys never to plan again, follow-ups in the last
    day that may have landed, this kind's records by fingerprint)."""
    from service.proactive import acting
    from service.proactive.prepare import card_id
    dismissed = set(store.decisions().get("dismissed") or [])
    blocked, mine = set(), {}
    for record in acting.records(store, kind=KIND):
        fp = record.get("fingerprint")
        if not fp:
            continue
        mine.setdefault(fp, record)
        status = record.get("status")
        if status in acting.LANDED or int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
            blocked.add(fp)
        person = record.get("person")
        if not person:
            continue
        # Undone, or its "Wisp did this" card waved away: leave that person alone.
        waved = card_id(fp, "undo_add") in dismissed or status in ("undone", "undo_unknown")
        if waved and now - float(record.get("undone_at") or record.get("at") or 0) <= PERSON_BLOCK_S:
            blocked.add(person)
        # One follow-up a week per person, whatever came of it: never a nag.
        if status in acting.LANDED and now - float(record.get("at") or 0) <= PERSON_REST_S:
            blocked.add(person)
    return blocked, acting.done_since(store, now - DAY_S, kind=KIND), mine


def _self_names() -> tuple[str, ...]:
    try:
        from service.memory.identity import user_name
        return tuple(n for n in (user_name(),) if n)
    except Exception:  # noqa: BLE001
        return ()


def _waved(decided: dict) -> dict[str, float]:
    """{item id: the newest ask time a card for it was dismissed or acted on}, from
    the stable ask keys ("ask:<item id>@<time>") the store keeps for every card the
    user dealt with: a new message after that still counts as the same wait."""
    out: dict[str, float] = {}
    for bucket in ("suppressed", "held_asks"):
        for key in (decided.get(bucket) or {}):
            if not isinstance(key, str) or not key.startswith("ask:") or "@" not in key:
                continue
            item_id, _, stamp = key[4:].rpartition("@")
            try:
                at = float(stamp)
            except ValueError:
                continue
            out[item_id] = max(out.get(item_id, float("-inf")), at)
    return out


def _when_phrase(due: float, now: float, tz: str) -> str:
    zone = ZoneInfo(tz)
    at, today = datetime.fromtimestamp(due, zone), datetime.fromtimestamp(now, zone).date()
    clock = at.strftime("%-I %p") if at.minute == 0 else at.strftime("%-I:%M %p")
    day = "today" if at.date() == today else "tomorrow" if at.date() == today + timedelta(days=1) \
        else at.strftime("%A")
    return f"{day} at {clock}"


async def undo(record: dict) -> dict:
    """Delete exactly the reminder Wisp added, by the identifier, title and time
    stored in the record: the verified delete, read back as absent."""
    stored = record.get("undo") if isinstance(record.get("undo"), dict) else {}
    if not str(stored.get("source_id") or "").strip():
        return {"ok": False, "status": "blocked", "error": "Wisp has no record of that reminder."}
    from service.proactive.autoadd import delete_reminder
    result = await delete_reminder(stored)
    if isinstance(result, dict) and result.get("ok") is True:
        return {"ok": True, "status": "succeeded", "message": "Reminder removed"}
    return result if isinstance(result, dict) else {"ok": False, "status": "unknown"}


def _register() -> None:
    try:
        from service.proactive import acting
        acting.register_undoer(KIND, undo)
    except Exception:  # noqa: BLE001
        pass



async def step(survivors: list[dict], sched: dict, *, now: float, tz: str, store, abort,
               write=None, read_sent=read_sent_fresh, records=None, quiet=default_quiet,
               reminders=None, debug: list | None = None, gmail=None, apple=None, lookup=None) -> dict:
    """Plan follow-ups for this cycle's items, and add the ones that pass through
    the verified reminder write, each recorded with its Undo before any card.

    `survivors` are the cycle's {"item", "result"} pairs. Returns {"added":
    fingerprints added this pass}. `write`, `read_sent`, `records`, `quiet` and
    `reminders` let tests replace the native write, Sent mail, the texts cache,
    the hours and the open reminders."""
    from service.proactive import acting
    from service.proactive.prepare import card_id
    _register()
    since = acting.enabled_at()
    if since is None:
        return {"added": []}
    blocked, today, recorded = _blocked(store, now)
    if today >= MAX_PER_DAY or acting.room(store, now) <= 0:
        return {"added": []}
    # One message gets at most one thing done on its own: an item auto-add already
    # acted on (an event or deadline added, even if undone since) gets no follow-up.
    acted = {str(r.get("item_id")) for r in acting.records(store, statuses=acting.LANDED)
             if r.get("kind") != KIND and r.get("item_id")}
    decided = store.decisions()
    gone = set(decided.get("dismissed") or []) | set(decided.get("fired") or [])
    suppressed = set(decided.get("suppressed") or [])
    items, results = [], {}
    for survivor in survivors or ():
        item = survivor.get("item") if isinstance(survivor, dict) else None
        if not isinstance(item, dict) or item.get("source") not in ("mail", "messages"):
            continue
        if not acting.allows(item.get("received_ts")):
            continue
        if str(item.get("id")) in acted:
            if debug is not None:
                debug.append({"item": item.get("id"), "follow_up": "auto-add acted on it"})
            continue
        # The user waved this item's card away, or already acted on it.
        item_fp = f"{item.get('id')}:{item.get('hash')}"
        if item_fp in suppressed or any(card_id(item_fp, m) in gone for m in _REPLY_METHODS):
            continue
        items.append(item)
        results[item.get("id")] = survivor.get("result")
    if not items:
        return {"added": []}
    if records is None:
        records = []
        if any(i.get("source") == "messages" for i in items):
            try:
                from service.proactive.sources import text_records
                records = text_records()
            except Exception:  # noqa: BLE001 — no texts readable: texts don't qualify
                records = []
    picked, why = await plans(
        items, now=now, tz=tz, records=records,
        reminders=open_reminder_titles(sched, store, now) if reminders is None else reminders,
        results=results, since_ts=since, blocked=blocked, done_today=today, quiet=quiet, read_sent=read_sent,
        abort=abort, self_names=_self_names(), waved=_waved(decided), **({"gmail": gmail} if gmail else {}),
        **({"apple": apple} if apple else {}), **({"lookup": lookup} if lookup else {}))
    if debug is not None:
        debug.extend({"item": k, "follow_up": v} for k, v in why.items())
    if write is None:
        from service.proactive.autoadd import create_reminder as write
    added = []
    for plan in picked:
        fp = plan["fingerprint"]
        previous = recorded.get(fp) if isinstance(recorded.get(fp), dict) else {}
        if not acting.allows(plan.get("consent_ts", plan["received_ts"])):
            continue
        if acting.still_ok() is not None or acting.room(store, now) <= 0 or abort() \
                or _in_quiet(quiet, now, tz):
            break
        try:
            outcome = await write(plan)
        except Exception as exc:  # noqa: BLE001 — after a possible dispatch the outcome is unknown
            outcome = {"ok": False, "status": "unknown", "error": type(exc).__name__}
        outcome = outcome if isinstance(outcome, dict) else {"ok": False, "status": "unknown"}
        source_id = str(outcome.get("source_id") or "").strip()
        verified = outcome.get("ok") is True and bool(source_id)
        status = "added" if verified else "unknown" if outcome.get("status") == "unknown" else \
            "blocked" if outcome.get("status") == "blocked" else "failed"
        attempts = int(previous.get("attempts") or 0) + (0 if status == "blocked" else 1)
        title = outcome.get("title") if verified and isinstance(outcome.get("title"), str) \
            and outcome["title"].strip() else plan["title"]
        when = _when_phrase(plan["due_ts"], now, tz)
        acting.record(
            store, fp, kind=KIND, status=status, now=now, title=title, quote=plan["quote"], who=plan["who"],
            source=plan["source"], item_id=str(plan.get("item_id") or ""), received_ts=plan["received_ts"],
            timezone=tz, account=plan.get("account", ""), person=plan["person_key"], attempts=attempts,
            due_ts=plan["due_ts"], item_fingerprint=plan["item_fingerprint"],
            card={"meta": "REMINDER", "title": guard.clean_title(f"Reminder to reply to {plan['who']}"),
                  "did": f"No reply yet in Mail, so I'll remind you {when}. If you answer before then, "
                         "check it off. Wisp can't see calls: if you already called, Undo."
                         if plan["source"] == "mail" else
                         f"No reply yet in Messages or Mail, so I'll remind you {when}. If you answer before "
                         "then, check it off. Wisp can't see calls: if you already called, Undo."},
            show_until=plan["due_ts"],
            undo={"source_id": source_id, "title": title, "due_ts": plan["due_ts"]} if verified else None)
        if verified:
            added.append(fp)
    return {"added": added}
