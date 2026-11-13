"""Auto-add: put a time-sensitive event or deadline the user missed in a text or
email onto Calendar or Reminders, then report it on a "Wisp did this" card.

Today's cards wait for a tap. This acts first and reports after, so it is
deliberately narrow: a wrong add is worse than a missed one.

IMPORTANT rules:
- An item qualifies only when EVERY check in `plan_for` holds: a real person, a
  school, or transactional mail (never promos, bulk mail or a critical alert);
  not withheld or sensitive; not a group chat; an explicit date in the
  source's own words, resolved against when the message ARRIVED, never today;
  at least 30 minutes ahead and at most 60 days out; missed (unanswered and
  unread or read over a day ago, or answered with a plain yes); not already on
  Calendar or in Reminders (fuzzy title within a time window); and never
  auto-added, undone, dismissed or tapped before (fingerprinted in the store).
- An event with a clock goes to Calendar (the default calendar, the stated
  length or an hour); a deadline goes to Reminders (its stated time, else 9 AM
  that day). Never both for one item.
- At most 3 writes a cycle and 10 a rolling day. Background cycle only: the
  cycle's abort (a user's turn) is checked before every write, and again right
  before dispatch.
- Every write goes through the verified native paths (attention_calendar's
  create_calendar; the outbox's verified create_reminder), is audit-logged,
  and counts only when the native readback verified it. Only then is a done
  card made. An unconfirmed write is fingerprinted and never retried, so it
  can never land twice.
- Undo deletes exactly the item Wisp created, by the identifier stored here
  (never one from a request or a card), through the verified cancel/delete
  paths, and blocks the item from ever being added again.
- Off by default: writing needs the person's yes to "Let Wisp act for me"
  (service/proactive/acting.py, their standing approval), and only a message
  that ARRIVED after that yes is ever added. Off, or for an older message, the
  same items become ordinary tap-to-add cards (the executor's
  calendar_event/reminder).
- One path per message: an item Caught for you already turned into a reminder
  or event (service/attention) is never added here as well.
- Mail does not act in this release (MAIL_ACTS is False): a dated email always
  stays a tap card. The proof it will need is built and tested behind that
  switch (default_mail_proof): a mail sender's identity comes from an
  unauthenticated From header, so a qualifying mail item would be added without
  a tap only when it was fetched by Wisp's own Google sign-in,
  its sender is read strictly, that exact address is a saved contact's or one
  the user's own Sent mail went to before it arrived, the dated sentence is in
  that very message, and Gmail itself reports the mail as authenticated for the
  From domain. Anything missing, stale or unreadable means a tap card. Mail seen
  only through Apple Mail is never added without a tap. Texts do not need this
  (a text's sender is a handle matched to a saved contact).
- "Arrived after the yes" is judged per sentence: a mail subject by the first
  in-view message that carried it without a Re:/Fwd: prefix (sources._subject_ts),
  a mail body sentence by its own message, or by an EARLIER message in view that
  already said it (quoted history the cut missed), a text line by its own line.
  A body whose history can't be told apart has no known time: never added
  without a tap. Every date resolves against its own sentence's time; a
  relative date ("Friday") whose time is unknown is never used.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from service.proactive import guard

MAX_PER_CYCLE = 3
MAX_PER_DAY = 10
DAY_S = 86400
MIN_LEAD_S = 30 * 60
MAX_AHEAD_S = 60 * DAY_S
DEFAULT_EVENT_MIN = 60
DONE_SHOWN_S = 3 * DAY_S
MAX_ATTEMPTS = 2
# Mail acts only with PROOF. A From line is not proof of who sent a mail (anyone can write
# "From: Prof. Lee"), so a qualifying MAIL item is added without a tap only when
# `mail_proof` holds every one of its checks; otherwise it stays a tap card. This constant
# is the fail-closed switch over all of it: False, and no mail is ever added without a tap,
# whatever the proof says. Texts do not pass through here: a text's sender is a handle
# matched to a saved contact (_saved_contact).
#
# OFF in this release. An independent review found the proof not yet strong enough to act
# on: a yes given before email was named would silently cover it; Gmail writes no result
# for mail that did not arrive by SMTP (an IMAP append, an import), so a forged one could
# stand alone; a pass could rest on SPF alone; and the evidence is keyed by Message-ID,
# not Gmail's own id. The proof code and its tests stay, exercised with this flipped on.
# Do not set it True without those fixed and re-reviewed, and without changing acting.DOES
# and SCOPE in the same commit: every screen reads them.
MAIL_ACTS = False
# A body sentence counts as already said in an earlier message when at least this share
# of its word 3-grams appear in that message (sources.quote_grams).
QUOTED_SHARE = 0.5
# Records never block forever, but they outlive every source window (7 days of
# mail and texts) many times over.
KEEP_S = 90 * DAY_S
_RECORD_STATES_BLOCKING = ("added", "undone", "unknown", "undo_unknown", "dispatching")


def _digest(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def enabled() -> bool:
    try:
        from service.proactive import acting
        return acting.enabled("add")             # on, and the yes named adding (acting.ACTIONS)
    except Exception:  # noqa: BLE001 — a setting that can't be read writes nothing
        return False


def _audit(event: str, **fields) -> None:
    try:
        from service.safety.audit import audit
        audit(event, **fields)
    except Exception:  # noqa: BLE001 — auditing never blocks a result
        pass


# --- reading the source -------------------------------------------------------------

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_ABSOLUTE = re.compile(rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}"
                       r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b", re.I)
_WEEKDAY = (r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|"
            r"sat(?:urday)?|sun(?:day)?)")
_RELATIVE = re.compile(rf"\b(?:tomorrow|tmrw|{_WEEKDAY})\b", re.I)
_RECURRING = re.compile(r"\b(?:every|each|weekly|daily|biweekly|monthly|recurring)\b", re.I)
# A proposal still being worked out is not a plan. A plain invitation ("can you
# come to my recital Sat Oct 31 at 6pm?") is: it names the event and the time.
_HEDGE = re.compile(r"\b(?:maybe|might|possibly|perhaps|tentative(?:ly)?|how about|what about|should we|"
                    r"could we|can we|shall we|wanna|thinking (?:of|about)|not sure|tbd|if you(?:'re| are)? free|"
                    r"lmk if|let me know if)\b", re.I)
_DEADLINE = re.compile(r"\b(due|deadline|submit(?:ted)?|submission|turn in|hand in|pay(?:ment)?|rsvp|"
                       r"register|registration|apply|application|expires?|last day)\b", re.I)
_EVENT = re.compile(r"\b(meeting|exam|midterm|final exam|quiz|test|appointment|appt|call|interview|class|lecture|"
                    r"lab|section|seminar|session|recital|concert|performance|game|party|dinner|lunch|breakfast|"
                    r"brunch|reservation|ceremony|office hours|orientation|rehearsal|practice|tour|checkup|"
                    r"check-up|dentist|doctor|consultation|visit|meetup|meet-up|workshop|presentation)\b", re.I)
_PROMO = re.compile(r"\bunsubscribe\b|\d+\s?% off\b|\b(?:sale|deals?|promo(?:tion)?|coupon|discount|"
                    r"limited time|shop now|order now|free shipping|webinar|newsletter)\b", re.I)
_TRANSACTIONAL = re.compile(r"\b(?:confirm(?:ed|ation)|booked|booking|reservation|appointment|scheduled)\b", re.I)
_DURATION = re.compile(r"\bfor\s+(?:an?\s+)?(\d{1,3}(?:\.\d)?)?\s*(hours?|hrs?|minutes?|mins?)\b|"
                       r"\b(\d{1,3})[- ](hour|minute|min)\b", re.I)
_CODE = re.compile(r"\b([A-Z]{2,5})\s?-?(\d{1,3}[A-Z]?)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_BLOCK = re.compile(r"(?:^|\n)From (.+?) · [^\n]*?:\n")
_STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "with", "at", "on", "in", "my", "your", "our", "is",
         "are", "be", "due", "by", "from", "reminder", "event", "please", "you", "can", "come", "will", "this",
         "that", "it", "i", "we", "am", "pm", "re", "fwd"}
_CONFIRM = re.compile(r"\b(?:yes|yeah|yep|yup|sure|sounds good|see you|i'?ll be there|count me in|confirmed?|"
                      r"will do|definitely|of course|absolutely|works for me|i'?m in|perfect)\b", re.I)
_DECLINE = re.compile(r"\b(?:no|nope|can'?t|cannot|won'?t|not|sorry|unfortunately|busy|maybe|might|"
                      r"later|unsure|reschedule|conflict)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if len(s.strip()) >= 3]


def _latest_mail_block(item: dict) -> tuple[str, str]:
    """(the subject, the newest incoming message's text) from a mail item."""
    text = item.get("text") or ""
    subject = str(item.get("title") or "")
    parts = _BLOCK.split(text)
    incoming = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)
                if not parts[i].startswith("You (the user)")]
    body = incoming[-1][1] if incoming else ""
    return subject, "" if body.strip() == "(subject only)" else body


def _incoming_blocks(item: dict) -> int:
    parts = _BLOCK.split(item.get("text") or "")
    return sum(1 for i in range(1, len(parts) - 1, 2) if not parts[i].startswith("You (the user)"))


def _said_at(sentence: str, body_ts: float | None, earlier) -> float | None:
    """When a sentence of the newest mail block was first said: the earliest
    earlier in-view message that already carries it (an unmarked quote), else
    the block's own time. None when that is unknown, or when the sentence is too
    short to compare and anything earlier is in view."""
    if body_ts is None:
        return None
    from service.proactive.sources import GRAM_N, quote_grams
    grams = quote_grams(sentence.rstrip("…"))
    found = []
    for entry in earlier or []:
        try:
            ts, seen = float(entry[0]), set(entry[1])
        except (TypeError, ValueError, IndexError):
            return None                      # an unreadable record: the time is not known
        if len(re.findall(r"[a-z0-9]+", sentence.casefold())) < GRAM_N:
            return None
        if grams and sum(g in seen for g in grams) >= QUOTED_SHARE * len(grams):
            found.append(ts)
    return min(found) if found else body_ts


def _user_reply(item: dict) -> str:
    """The user's own newest message in the thread, or ""."""
    text = item.get("text") or ""
    if item.get("source") == "mail":
        parts = _BLOCK.split(text)
        mine = [parts[i + 1] for i in range(1, len(parts) - 1, 2) if parts[i].startswith("You (the user)")]
        return mine[-1] if mine else ""
    # Rendered sender-first: "[Adi (you) -> Maya] yes!". Only the sender counts:
    # "[Maya -> Adi (you)]" is Maya's line addressed to the user.
    lines = [line for line in text.splitlines() if line.startswith("[") and "]" in line]
    mine = [line.split("]", 1)[1] for line in lines
            if re.search(r"\(you\)|you \(the user\)", line[1:].split("]", 1)[0].split("->", 1)[0], re.I)]
    return mine[-1].strip() if mine else ""


def _confirmed(item: dict) -> bool:
    """The user answered with a plain yes, nothing that hedges or declines."""
    reply = _user_reply(item)
    return bool(reply) and len(reply) <= 200 and bool(_CONFIRM.search(reply)) and not _DECLINE.search(reply)


def _candidates(item: dict) -> list[tuple[str, bool, float | None]]:
    """(sentence, may use a relative date, when THAT sentence arrived) in reading
    order. Relative dates only in the newest message: "Friday" means the Friday
    after that message arrived. The third value is the arrival time of the line
    or message the sentence is in, or None when it is not known: the person's
    yes covers only what arrived after it (acting.allows), and an item's
    received_ts is only its newest line, so a "👍" today must never carry
    yesterday's "recital Sunday at 2pm" past that rule."""
    def stamp(value):
        return float(value) if type(value) in (int, float) else None
    if item["source"] == "mail":
        subject, body = _latest_mail_block(item)
        # The subject is as old as the first message that carried it unprefixed
        # (sources._subject_ts). The newest incoming block's own words arrived at
        # body_ts, which sources leaves None when the block's history can't be told
        # apart; and the block read here must be the one body_ts is about.
        body_ts = stamp(item.get("body_ts"))
        if _incoming_blocks(item) != item.get("mail_blocks_in"):
            body_ts = None
        return [(subject, True, stamp(item.get("subject_ts")))] + \
               [(s, True, _said_at(s, body_ts, item.get("mail_earlier"))) for s in _sentences(body)]
    every, stamps = list(item.get("lines") or []), list(item.get("line_ts") or [])
    known = len(stamps) == len(every)
    from service.proactive.sources import ORIGIN_LINES as n        # the same lines text_origin vouches for
    lines, times = every[-n:], (stamps[-n:] if known else [None] * len(every[-n:]))
    out = []
    for index, line in enumerate(lines):
        newest = index == len(lines) - 1
        out += [(s, newest, stamp(times[index])) for s in _sentences(line)]
    return out


def _resolve(sentence: str, arrival: float, tz: str):
    from service.attention.resolve import resolve
    try:
        return resolve(guard.normalize_slang(sentence), datetime.fromtimestamp(arrival, ZoneInfo(tz)), tz)
    except Exception:  # noqa: BLE001 — an unreadable phrase dates nothing
        return None


# Country calling codes by locale region, for a saved number that has none. Only used to
# TIGHTEN a match; an unknown region never loosens one.
_REGION_CODES = {"US": "1", "CA": "1", "GB": "44", "IE": "353", "AU": "61", "NZ": "64", "IN": "91", "DE": "49",
                 "FR": "33", "ES": "34", "IT": "39", "NL": "31", "SE": "46", "NO": "47", "DK": "45", "CH": "41",
                 "AT": "43", "BE": "32", "PT": "351", "BR": "55", "MX": "52", "JP": "81", "KR": "82", "SG": "65",
                 "HK": "852", "CN": "86", "ZA": "27", "AE": "971", "IL": "972", "PL": "48", "FI": "358", "IS": "354",
                 "LU": "352", "GR": "30", "CZ": "420", "HU": "36", "RO": "40", "TR": "90", "UA": "380", "AR": "54",
                 "CL": "56", "CO": "57", "PH": "63", "ID": "62", "MY": "60", "TH": "66", "VN": "84", "TW": "886",
                 "SA": "966", "EG": "20", "NG": "234", "PK": "92", "SM": "378", "VA": "39"}
# Where a national number's leading 0 is part of the number, not a trunk prefix to drop:
# Italy and Vatican City (+39 06…), San Marino.
_KEEPS_LEADING_ZERO = ("39", "378")


def _region_code() -> str:
    """The user's country calling code, or "" if unknown. From WISP_REGION, the region of the
    Mac's own locale that the app passes to the service when it starts it (an app opened from
    Finder has no LANG), else from the process locale ("en_US" -> "1"). When the app said a
    region, that is the answer even if it is one this table does not know (then ""): the
    locale of whatever shell started the service is never a better guess than the Mac's own."""
    import os
    region = (os.environ.get("WISP_REGION") or "").strip().upper()
    if region:
        return _REGION_CODES.get(region, "")
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(name) or ""
        region = value.split(".")[0].rpartition("_")[2].upper()
        if region in _REGION_CODES:
            return _REGION_CODES[region]
    return ""


def phones_match(handle: str, saved: str, region: str = "") -> bool:
    """A chat handle and a number as it is saved in Contacts are the SAME number.

    Full numbers, never "the last ten digits": +44 20 7946 0958 is not +1 207 946 0958.
    `handle` is the chat's own handle, which Messages gives with its country code.
    - A saved number that carries a country code ("+…" or "00…") must equal it digit for digit.
    - A saved number with none is a national number in the USER'S country. It matches only when
      that country's code is known (`region`) and the handle is exactly that code followed by
      the number (less a trunk "0"). The digits alone are never enough: (361) 234-5678 saved by
      someone in the US is +1 361…, not Hungary's +36 1 234 5678.
    - A saved number written with its country code but no "+" (15551234567) matches only when
      it starts with the user's own country code.
    - With the region unknown, only a saved number that carries its country code can match."""
    if not handle.strip().startswith("+"):
        return False
    want = re.sub(r"\D", "", handle)
    raw = saved.strip()
    digits = re.sub(r"\D", "", raw)
    if not (7 <= len(want) <= 15) or not digits:
        return False
    if raw.startswith("+"):
        return digits == want
    if raw.startswith("00"):
        return digits[2:] == want
    if not region:
        return False
    national = digits[1:] if digits.startswith("0") and region not in ("1",) + _KEEPS_LEADING_ZERO else digits
    if len(national) >= 7 and want == region + national:
        return True
    return digits == want and digits.startswith(region) and len(digits) > len(region) + 6


def strict_contact_name(handle: str, *, region: str | None = None) -> str:
    """The saved contact whose OWN number or address this handle is, or "".

    Stricter than imessage_tools.contact_name_for_handle, which matches phone numbers
    on their last ten digits (right for labelling a conversation, wrong for deciding who
    may put something on the calendar). Here an email must be saved exactly (case aside)
    and a phone number must be the same full number (phones_match). Contacts that were
    not synced in this launch, or are not allowed, hold nobody."""
    from service.tools import imessage_tools as im
    if not getattr(im, "_contacts_privacy_current", False) or getattr(im, "_contacts_not_allowed", False):
        return ""
    h = (handle or "").strip()
    if not h:
        return ""
    region = _region_code() if region is None else region
    for name, handles in (im._name_handles or {}).items():
        for saved in handles or ():
            saved = str(saved or "")
            if "@" in h or "@" in saved:
                if "@" in h and "@" in saved and saved.strip().casefold() == h.casefold():
                    return name.title() if name.islower() else name
            elif phones_match(h, saved, region):
                return name.title() if name.islower() else name
    return ""


def _contact_name(handle: str) -> str:
    """The saved contact this handle belongs to, or "" (Contacts, as the app synced it), by
    the strict match above. Tests replace this."""
    return strict_contact_name(handle)


def _plan_handle(item: dict, count: int = 3) -> str:
    """The handle of the lines a plan reads (the last `count`, as _candidates does): every one
    of them joined to the same known one-to-one chat, else "". Who the sender is does not
    depend on how the message was delivered; that is _text_service's question."""
    from service.proactive import sources
    lines, chats = list(item.get("lines") or []), list(item.get("line_chat") or [])
    if item.get("group") or not lines or len(chats) != len(lines):
        return ""
    used = set(chats[-count:])
    if len(used) != 1 or "" in used:
        return ""
    return sources.chat_handle(used).strip()


def _text_service(item: dict) -> str:
    """How the lines this plan reads were delivered: one known service for all of them, else ""."""
    from service.proactive import sources
    return sources.text_origin(item)[1]


def _own_handle(handle: str) -> bool:
    """The user's own address (a note-to-self chat): never a correspondent."""
    try:
        from service.memory.identity import user_emails
        return "@" in handle and handle.casefold() in {str(e).casefold() for e in user_emails() or []}
    except Exception:  # noqa: BLE001
        return False


def _person_handle(handle: str) -> bool:
    """A phone number or an email address a person texts from. Not a short code, not an
    alphanumeric sender id ("AMAZON"), not a business chat ("urn:biz:...")."""
    if not handle or handle.casefold().startswith("urn:"):
        return False
    if "@" in handle:
        from service import mail_sender
        return mail_sender.real_address(handle) == handle.casefold()
    return bool(re.fullmatch(r"\+?\d{7,15}", re.sub(r"[\s().-]", "", handle)))


def _saved_contact(item: dict) -> bool:
    """A one-to-one text conversation whose HANDLE (the number or address the messages
    really come from) is saved in the person's Contacts.

    Decided from the handle, never from the conversation's label: a label is what is
    shown ("+1 555 123 4567 (conversation 2)", "AMAZON"), and a stranger's label can
    look like a name. No handle (a group, a mix of chats, an unknown chat), a handle
    that is not a phone number or address, or a handle Contacts does not hold exactly,
    is not a saved contact. Having answered someone does not make them one."""
    if item.get("source") != "messages" or item.get("group"):
        return False
    # The handle of the lines this plan reads (sources.text_origin): every one of them from
    # the same known one-to-one chat. item["handle"] is the reply card's, on a looser rule.
    handle = _plan_handle(item)
    if not _person_handle(handle) or _own_handle(handle):
        return False
    try:
        return bool(str(_contact_name(handle) or "").strip())
    except Exception:  # noqa: BLE001 — Contacts can't be asked: nobody is known
        return False


_FREE_MAIL = frozenset({"gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com", "yahoo.com",
                        "outlook.com", "hotmail.com", "live.com", "aol.com", "proton.me", "protonmail.com"})


def _same_org(item: dict) -> bool:
    """Mail from the user's own organisation (a professor at the user's
    university): the sender's domain is one of the user's own, and not a
    free-mail provider anyone can sign up at."""
    if item.get("source") != "mail":
        return False
    sender = str((item.get("ref") or {}).get("to") or "").rpartition("@")[2].casefold()
    if not sender or sender in _FREE_MAIL:
        return False
    try:
        from service.memory.identity import user_emails
        own = {str(e).rpartition("@")[2].casefold() for e in user_emails() or []}
    except Exception:  # noqa: BLE001 — no identity: no organisation to match
        own = set()
    own.add(str(item.get("account") or "").rpartition("@")[2].casefold())
    return sender in own or any(sender.endswith("." + d) for d in own if d)


def _explicit(got, relative_ok: bool) -> bool:
    """A date the sender actually wrote: a month and day, a numeric date, or (in
    the newest message) a weekday or "tomorrow". A bare clock ("at 7"), "today",
    "tonight", "this week", a guessed am/pm, or anything the resolver had to
    infer beyond the year never counts."""
    if got is None or got.blocked is not None or got.guessed or got.day is None:
        return False
    if any(i in ("meridiem", "date:next_occurrence") for i in got.inferred):
        return False
    if _ABSOLUTE.search(got.quote or ""):
        return True
    return relative_ok and bool(_RELATIVE.search(got.quote or ""))


def _duration(sentence: str) -> int:
    match = _DURATION.search(sentence)
    if not match:
        return DEFAULT_EVENT_MIN
    amount, unit = (match.group(1), match.group(2)) if match.group(2) else (match.group(3), match.group(4))
    value = float(amount) if amount else 1.0
    minutes = int(round(value * (60 if unit.lower().startswith("h") else 1)))
    return minutes if 15 <= minutes <= 8 * 60 else DEFAULT_EVENT_MIN


def _first_name(who: str) -> str:
    word = re.split(r"[\s,]+", (who or "").strip())[0] if who else ""
    return word if re.fullmatch(r"[A-Za-z][A-Za-z'-]{0,30}", word or "") else ""


def _strip_dates(sentence: str, quote: str) -> str:
    text = sentence
    for piece in (quote or "").split(" / "):
        if piece.strip():
            text = text.replace(piece.strip(), " ")
    text = _ABSOLUTE.sub(" ", text)
    text = re.sub(rf"\b(?:tomorrow|tmrw|{_WEEKDAY})\b,?", " ", text, flags=re.I)
    text = re.sub(r"\b(?:at|@)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", " ", text, flags=re.I)
    text = re.sub(r"^(?:hey|hi|hello)\b[^,!]*[,!]\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"^(?:reminder|fyi|note|heads up|update)\s*[:\-–]\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:please|pls|remember to|don'?t forget to)\s+", "", text, flags=re.I)
    text = re.sub(r"^(?:can|could|would|will) you (?:come|make it|join (?:me|us))(?: to| for)?\s+", "", text,
                  flags=re.I)
    text = re.sub(r"[\s,;:.!?()\-–—]+$", "", re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"\s+\b(?:is|are|on|at|by|before|for|this|next|the)$", "", text, flags=re.I).strip()
    text = re.sub(r"\s+\b(?:is|are|on|at|by|before|for)$", "", text, flags=re.I).strip()
    return text


def _title(item: dict, sentence: str, quote: str, kind: str, word: str) -> str:
    """The event's or reminder's name, from the source's own words, by code."""
    subject = re.sub(r"^\s*(?:(?:re|fwd?)\s*:\s*)+", "", str(item.get("title") or ""), flags=re.I).strip()
    mail = item["source"] == "mail"
    code = _CODE.search(subject if mail else "") or _CODE.search(sentence)
    code_text = f"{code.group(1)} {code.group(2)}" if code else ""
    first = _first_name(item.get("who", ""))
    noun = word.lower()
    mine = re.search(rf"\bmy\s+(?:\w+\s+)?{re.escape(noun)}\b", sentence, re.I) if noun else None
    if kind == "calendar" and not mail and mine:
        title = f"{first}'s {noun}" if first else noun.capitalize()
    else:
        title = _strip_dates(sentence, quote)
        if mail and title.casefold() == subject.casefold():
            title = _strip_dates(subject, quote)
        words = title.split()
        if not 1 <= len(words) <= 8 or len(title) < 2 or re.search(r"\b(?:you|your)\b", title, re.I) and not mail:
            title = (subject if mail and subject and not _ABSOLUTE.search(subject) else
                     f"{noun.capitalize()} with {first}" if first and kind == "calendar" else noun.capitalize())
        if code_text and code_text.replace(" ", "").casefold() not in title.replace(" ", "").casefold():
            title = f"{code_text} {title}"
    title = guard.clean_title(title, 80)
    return title[:1].upper() + title[1:] if title else ""


# --- dedupe ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").casefold())
            if t not in _STOP and (len(t) >= 3 or t.isdigit() or re.search(r"\d", t))}


def already_there(plan: dict, rows: list[dict], tz: str) -> dict | None:
    """The existing Calendar event or reminder this plan would duplicate, or None.

    Errs toward "already there": an event at the same start (±5 min) whatever
    its name; anything sharing a meaningful word within 3 hours (an event), the
    same day (an all-day event), or a day (a reminder)."""
    zone = ZoneInfo(tz)
    at = plan["start_ts"] if plan["kind"] == "calendar" else plan["due_ts"]
    words = _tokens(plan["title"]) | _tokens(plan.get("word", ""))
    day = datetime.fromtimestamp(at, zone).date()
    for row in rows or []:
        when = row.get("when_ts")
        if type(when) not in (int, float):
            continue
        shared = bool(words & _tokens(row.get("title", "")))
        is_event = row.get("source") == "calendar" or row.get("kind") in ("event", "meeting")
        if is_event and plan["kind"] == "calendar" and abs(when - at) <= 5 * 60:
            return row
        if not shared:
            continue
        if row.get("all_day") and datetime.fromtimestamp(when, zone).date() == day:
            return row
        if abs(when - at) <= (3 * 3600 if is_event else DAY_S):
            return row
    return None


def default_existing(now: float) -> list[dict]:
    """Every synced Calendar event and reminder from yesterday to 61 days out:
    the cycle's own schedule only covers today and tomorrow."""
    try:
        from service.assistant.store import assistant_store
        return assistant_store.upcoming(now - DAY_S, days=62)
    except Exception:  # noqa: BLE001 — nothing readable: nothing is "already there", so the cap and checks still hold
        return []


# --- the plan for one item --------------------------------------------------------------


def fingerprint(item: dict, kind: str, at: float) -> str:
    """Stable across new messages in the same thread: the thread, the kind and
    the minute. An undone add is never re-added, whatever else arrives."""
    return "autoadd:" + _digest(item.get("id"), kind, int(at // 60))


def _missed(item: dict, now: float) -> bool:
    from service.proactive.cycle import card_due
    if item.get("replied"):
        return _confirmed(item)
    return card_due(item, now)


def plan_for(item: dict, result: dict | None, *, now: float, tz: str) -> tuple[dict | None, str]:
    """(plan, "") for an eligible item, else (None, why not). Pure: no store, no I/O."""
    from service.proactive import triage
    source = item.get("source")
    if source not in ("mail", "messages"):
        return None, "not mail or texts"
    if item.get("critical"):
        return None, "account alert"
    if item.get("group"):
        return None, "group chat"
    if triage.withheld(item) or item.get("secret"):
        return None, "withheld"
    if not isinstance(result, dict) or result.get("sensitive") or result.get("junk"):
        return None, "sensitive or junk"
    text = f"{item.get('title') or ''}\n{item.get('text') or ''}"
    person = bool(item.get("person", False))
    if source == "mail":
        if _PROMO.search(text):
            return None, "promotional"
        if not person and not (_TRANSACTIONAL.search(text) or triage._DEADLINE.search(text)):
            return None, "not a person, school or confirmation"
    elif not person:
        return None, "not a person"
    # A sender whose display name carries an address or a bracket is posing as someone:
    # nothing is added on its strength (sources.odd_sender).
    if item.get("odd_sender"):
        return None, "sender name carries an address"
    # A stranger never writes to the user's calendar. A TEXT must be one-to-one with a
    # handle saved in Contacts; having answered them once is not enough. (Mail has its
    # own, stronger proof and does not act at all while MAIL_ACTS is False.)
    if source == "messages":
        if not _saved_contact(item):
            return None, "not a saved contact: tap to add"
    # MAIL: who the sender is, is not decided here. Having written in the thread, or sharing
    # the user's domain, used to qualify a mail sender; both rest on the From line alone.
    # A mail plan is only ever WRITTEN with default_mail_proof (step); without it the plan
    # is a card the person taps. While mail does not act at all, a sender the user has
    # neither written to in this thread nor shares an organisation with gets no add-card
    # here either, as before: a dated email from a stranger is what phishing looks like.
    elif person and not MAIL_ACTS and not (item.get("user_wrote") or _same_org(item)):
        return None, "unknown sender: tap to add"
    arrival = item.get("received_ts")
    if type(arrival) not in (int, float):
        return None, "no arrival time"
    if not _missed(item, now):
        return None, "already handled"
    reason = "no explicit date"
    for sentence, relative_ok, line_ts in _candidates(item):
        if len(sentence) > 300 or _RECURRING.search(sentence) or guard.source_sensitive(sentence):
            continue
        deadline, event = _DEADLINE.search(sentence), _EVENT.search(sentence)
        if not deadline and not event:
            continue
        if _HEDGE.search(sentence):
            reason = "only a proposal"
            continue
        # Against the sentence's own arrival: a subject's "Friday" is the Friday after the
        # thread began, not after its latest reply. A sentence whose time is unknown may
        # use only an absolute date (its year read against the item's arrival), and its
        # plan keeps line_ts None: a tap card.
        known = type(line_ts) in (int, float)
        got = _resolve(sentence, float(line_ts) if known else float(arrival), tz)
        if not _explicit(got, relative_ok and known):
            continue
        zone = ZoneInfo(tz)
        if deadline:
            kind, word = "reminder", deadline.group(1)
            due = got.start.timestamp() if got.has_clock and got.start is not None else \
                datetime.combine(got.day, datetime.min.time(), zone).replace(hour=9).timestamp()
            at = due
        elif got.has_clock and got.start is not None:
            kind, word = "calendar", event.group(1)
            at = got.start.timestamp()
        else:
            reason = "an event with no start time"
            continue
        if at < now + MIN_LEAD_S:
            reason = "in the past or too soon"
            continue
        if at > now + MAX_AHEAD_S:
            reason = "too far ahead"
            continue
        title = _title(item, sentence, got.quote, kind, word)
        if not title or guard.output_has_pii(title) or guard.has_contact_or_link(title):
            reason = "no safe title"
            continue
        quote = guard.clean(sentence, 160)
        plan = {"kind": kind, "title": title, "word": word.lower(), "quote": quote,
                "fingerprint": fingerprint(item, kind, at), "item_id": item.get("id"), "source": source,
                "who": item.get("who", ""), "account": item.get("account", ""), "received_ts": float(arrival),
                # When the sentence this plan was read from arrived (None: not known).
                "line_ts": line_ts,
                "has_clock": bool(got.has_clock), "timezone": tz,
                "item_fingerprint": f"{item.get('id')}:{item.get('hash')}"}
        if kind == "calendar":
            plan.update(start_ts=float(at), duration_min=_duration(sentence),
                        day=datetime.fromtimestamp(at, zone).date().isoformat())
        else:
            plan.update(due_ts=float(at))
            if got.has_clock:
                plan["alerts"] = deadline_alerts(float(at), now, tz)
        return plan, ""
    return None, reason


# --- the writes (production) -------------------------------------------------------------


DEADLINE_LEAD_S = 3 * 3600


def deadline_alerts(due: float, now: float, tz: str) -> list[float]:
    """When to be reminded of a deadline with a clock time: 9 AM on the day it
    is due and 3 hours before it, whichever are still ahead (one when they
    nearly coincide). The deadline itself when neither is."""
    zone = ZoneInfo(tz)
    day = datetime.fromtimestamp(due, zone).date()
    nine = datetime.combine(day, datetime.min.time(), zone).replace(hour=9).timestamp()
    times = sorted({t for t in (nine, due - DEADLINE_LEAD_S) if now + 5 * 60 <= t < due})
    if len(times) == 2 and times[1] - times[0] < 3600:
        times = times[1:]
    return times or [due]


def _app_connected_now() -> bool:
    """Only while the app is subscribed: a background cycle never waits out a
    reconnect grace with a write pending."""
    try:
        from service.assistant.hub import hub
        return bool(hub.has_subscribers)
    except Exception:  # noqa: BLE001
        return False


def _turn_running() -> bool:
    try:
        from service import idle
        return bool(idle.foreground_busy())
    except Exception:  # noqa: BLE001 — unknown counts as busy
        return True


def _still_ok(action: str = "add") -> str | None:
    """Re-checked at dispatch: the setting is still on for `action` (the caller's own: a
    follow-up reminder is not an add) and no turn has started."""
    try:
        from service.proactive import acting
        on = enabled() if action == "add" else acting.enabled(action)
    except Exception:  # noqa: BLE001 — a setting that can't be read writes nothing
        on = False
    if not on:
        return "Acting for you was turned off"
    if _turn_running():
        return "A turn started; auto-add waits for the background"
    return None


async def create_event(plan: dict) -> dict:
    """The verified EventKit create: attention_calendar.create_calendar, for
    exactly this plan. The person's yes to "Let Wisp act for me" is the standing approval
    (checked again at dispatch); view-only mode and the calendar_write policy
    are enforced inside create_calendar."""
    if not _app_connected_now():
        return {"ok": False, "status": "blocked", "error": "The Wisp app isn't connected"}
    from service.assistant.attention_calendar import approve_capture, create_calendar
    capture = {"capture_key": "capture:" + plan["fingerprint"], "source_id": "proactive:" + plan["fingerprint"],
               "title": plan["title"], "day": plan["day"], "timezone": plan["timezone"],
               "start_ts": plan["start_ts"], "duration_min": plan["duration_min"], "all_day": False,
               "automatic": False}
    result = await create_calendar(capture, approval=approve_capture(capture),
                                   source_current=lambda: _still_ok() is None)
    return result if isinstance(result, dict) else {"ok": False, "status": "unknown", "error": "unreadable"}


def _reminder_problem(action: str = "add") -> str | None:
    from service.proactive.executor import _reminder_problem as policy
    return _still_ok(action) or policy()


async def create_reminder(plan: dict, *, action: str = "add") -> dict:
    """The verified native reminder write the attention capture's live mode
    uses: the outbox's create_reminder with a policy check just before dispatch.
    `action` is the acting.ACTIONS key the caller acts under: the check right before
    dispatch asks whether THAT action is still agreed to (follow-ups pass their own)."""
    if not _app_connected_now():
        return {"ok": False, "status": "blocked", "error": "The Wisp app isn't connected"}
    check = _reminder_problem if action == "add" else (lambda: _reminder_problem(action))
    if problem := check():
        return {"ok": False, "status": "blocked", "error": problem}
    from service.assistant.outbox import request
    made: list[dict] = []
    result: dict = {}
    for when in plan.get("alerts") or [plan["due_ts"]]:
        result = await request("create_reminder", {"title": plan["title"][:200], "due_ts": float(when),
                                                   "commitment_kind": "reminder"},
                               before_dispatch=check)
        result = result if isinstance(result, dict) else {"ok": False, "status": "unknown", "error": "unreadable"}
        if result.get("ok") is not True or not str(result.get("source_id") or "").strip():
            # One that landed before this miss is still Wisp's: undo finds it.
            return {**result, "ok": False, "reminders": made}
        made.append({"source_id": str(result["source_id"]).strip(), "due_ts": float(when),
                     "title": result.get("title") if isinstance(result.get("title"), str) else plan["title"]})
    return {**result, "ok": True, "source_id": made[0]["source_id"], "title": made[0]["title"], "reminders": made}


async def delete_event(record: dict) -> dict:
    """The verified one-occurrence cancel: the app checks the event is still the
    one Wisp made (identifier, title, start, no invitees), removes it, and
    reads back that it is gone."""
    from service.safety import read_only
    if read_only():
        return {"ok": False, "status": "blocked", "error": "View-only mode: Calendar can't be changed."}
    from service.assistant.outbox import request
    return await request("cancel_calendar_event", {
        "source_id": record["source_id"], "expected_title": record["title"],
        "expected_start_ts": float(record["start_ts"]), "invitee_count": 0})


async def delete_reminder(record: dict) -> dict:
    """The verified reminder delete: exact identifier, title and due time, read
    back as absent. A deadline's every alert, each by its own identifier."""
    from service.proactive.executor import _reminder_problem as policy
    if problem := policy():
        return {"ok": False, "status": "blocked", "error": problem}
    from service.assistant.outbox import request
    made = record.get("reminders") or [{"source_id": record["source_id"], "title": record["title"],
                                         "due_ts": record["due_ts"]}]
    result: dict = {"ok": True, "status": "succeeded"}
    for one in made:
        result = await request("delete_reminder", {
            "source_id": one["source_id"], "expected_title": one.get("title") or record["title"],
            "expected_due_ts": float(one["due_ts"])}, before_dispatch=policy)
        if not isinstance(result, dict) or result.get("ok") is not True:
            return result if isinstance(result, dict) else {"ok": False, "status": "unknown"}
    return result


WRITERS = {"calendar": create_event, "reminder": create_reminder}
UNDOERS = {"calendar": delete_event, "reminder": delete_reminder}


# --- the cards ---------------------------------------------------------------------------------


def _clock(dt: datetime) -> str:
    return dt.strftime("%-I") if dt.minute == 0 else dt.strftime("%-I:%M")


def when_words(record: dict, tz: str) -> str:
    """"Wed, Oct 28, 10–11 AM" for an event; "Fri, Oct 30 at 11:59 PM" for a reminder."""
    zone = ZoneInfo(tz)
    if record["kind"] == "calendar":
        start = datetime.fromtimestamp(record["start_ts"], zone)
        end = start + timedelta(minutes=int(record.get("duration_min") or DEFAULT_EVENT_MIN))
        day = start.strftime("%a, %b %-d")
        if end.date() != start.date():
            return f"{day}, {_clock(start)} {start:%p}–{end:%a} {_clock(end)} {end:%p}"
        if start.strftime("%p") == end.strftime("%p"):
            return f"{day}, {_clock(start)}–{_clock(end)} {end:%p}"
        return f"{day}, {_clock(start)} {start:%p}–{_clock(end)} {end:%p}"
    due = datetime.fromtimestamp(record["due_ts"], zone)
    return f"{due:%a, %b %-d} at {_clock(due)} {due:%p}"


def _received_words(ts: float, now: float, tz: str) -> str:
    zone = ZoneInfo(tz)
    day, today = datetime.fromtimestamp(ts, zone).date(), datetime.fromtimestamp(now, zone).date()
    if day == today:
        return "today"
    if (today - day).days == 1:
        return "yesterday"
    if 0 < (today - day).days < 7:
        return day.strftime("%a")
    return day.strftime("%b %-d")


def _alert_words(times: list[float], tz: str) -> str:
    """"Fri, Oct 30 at 9 AM and 8:59 PM": the second time alone on the same day."""
    zone = ZoneInfo(tz)
    out = []
    for i, t in enumerate(times):
        moment = datetime.fromtimestamp(t, zone)
        same = i and datetime.fromtimestamp(times[i - 1], zone).date() == moment.date()
        out.append(f"{_clock(moment)} {moment:%p}" if same else f"{moment:%a, %b %-d} at {_clock(moment)} {moment:%p}")
    return " and ".join(out)


def done_card(record: dict, now: float) -> dict:
    tz = record.get("timezone") or "UTC"
    first = _first_name(record.get("who", ""))
    noun = "email" if record.get("source") == "mail" else "text"
    origin = f"{first}'s {noun}" if first else f"an {noun}" if noun == "email" else "a text"
    quote = record.get("quote") or ""
    when = when_words(record, tz)
    if record["kind"] == "calendar":
        meta, title = "ADDED · CALENDAR", f"Added {record['title']} to your calendar"
        did = f"I added it for {when}."
    else:
        meta, title = "ADDED · REMINDER", f"Added {record['title']} to your reminders"
        alerts = [r.get("due_ts") for r in record.get("reminders") or [] if isinstance(r, dict)]
        if len(alerts) > 1:
            did = f"It's due {when}; I'll remind you {_alert_words(alerts, tz)}."
        else:
            did = f"I set a reminder for {when}."
    body = (f"From {origin} {_received_words(record['received_ts'], now, tz)}: “{quote}” {did}" if quote
            else f"From {origin}. {did}")
    return {
        "id": "card:" + _digest(record["fingerprint"], "undo_add"),
        "meta": meta,
        "title": guard.clean_title(title),
        "body": body,
        "why": "It was time-sensitive and still waiting on you, so Wisp added it. Undo removes exactly what Wisp added.",
        "account": record.get("account", ""),
        "sources": [{"kind": record.get("source", ""), "ref": "autoadd:" + str(record.get("item_id") or ""),
                     "who": record.get("who", ""), "quote": quote}],
        "action": {"method": "undo_add", "button": "Undo?", "draft": "", "recipient": "", "event": None,
                   "envelope": {"kind": "undo_add"}, "to": [], "cc": [], "subject": "", "reply_all": False,
                   "reply_ref": None, "open_url": "", "open_label": "", "record": record["fingerprint"],
                   "added": record["kind"]},
        "todo_id": None,
        "fingerprints": [],
        "due_ts": record.get("start_ts") if record["kind"] == "calendar" else record.get("due_ts"),
        "auto_added": True,
    }


def tap_card(plan: dict, now: float) -> dict:
    """With auto-add off (or the caps reached): the ordinary tap-to-add card,
    fired by the executor's own calendar_event / reminder paths."""
    from service.proactive.prepare import card_id
    when = when_words(plan, plan["timezone"])
    first = _first_name(plan.get("who", ""))
    noun = "email" if plan["source"] == "mail" else "text"
    origin = f"{first}'s {noun}" if first else f"an {noun}" if noun == "email" else "a text"
    if plan["kind"] == "calendar":
        method = "calendar_event"
        event = {"title": plan["title"], "start_ts": plan["start_ts"], "duration_min": plan["duration_min"],
                 "day": plan["day"], "timezone": plan["timezone"], "all_day": False}
        envelope, button = {"kind": "calendar", **event}, "Add to calendar?"
        did = f"I can add it for {when}."
    else:
        method, event = "reminder", None
        envelope, button = {"kind": "reminder", "title": plan["title"], "due_ts": plan["due_ts"]}, "Set a reminder?"
        did = f"I can remind you {when}."
    zone = ZoneInfo(plan["timezone"])
    at = plan.get("start_ts") or plan.get("due_ts")
    return {
        "id": card_id(plan["item_fingerprint"], method),
        "meta": f"{datetime.fromtimestamp(at, zone):%a}".upper() + (" · MAIL" if plan["source"] == "mail" else " · TEXTS"),
        "title": guard.clean_title(plan["title"]),
        "body": f"From {origin} {_received_words(plan['received_ts'], now, plan['timezone'])}: “{plan['quote']}” {did}",
        "why": "Time-sensitive and still waiting on you.",
        "account": plan.get("account", ""),
        "sources": [{"kind": plan["source"], "ref": plan["item_id"], "who": plan.get("who", ""),
                     "quote": plan["quote"]}],
        "action": {"method": method, "button": button, "draft": "", "recipient": "", "event": event,
                   "envelope": envelope, "to": [], "cc": [], "subject": "", "reply_all": False, "reply_ref": None,
                   "open_url": "", "open_label": ""},
        # Its own identity, not the reply card's: tapping or dismissing this must not
        # check off the message's reply to-do or hide its reply card. The plan's
        # fingerprint (thread, kind, minute) also outlives a changed thread hash.
        "todo_id": None,
        "fingerprints": [plan["fingerprint"]],
        "due_ts": at,
    }


def done_cards(store, now: float) -> list[dict]:
    """The "Wisp did this" cards still worth showing: verified adds, not undone
    or dismissed, under three days old and not yet past."""
    decided = store.decisions()
    gone = set(decided["dismissed"]) | set(decided["fired"])
    out = []
    for record in sorted(store.auto_added().values(), key=lambda r: -float(r.get("at") or 0)):
        if not isinstance(record, dict) or record.get("status") != "added" or not record.get("title"):
            continue
        if record.get("kind") not in ("calendar", "reminder"):
            # Another kind of action behind the same gate (acting.py): its card is built from its record.
            from service.proactive import acting
            if acting.shown(record, now):
                card = acting.did_card(record, now)
                if card["id"] not in gone:
                    out.append(card)
            continue
        at = record.get("start_ts") if record.get("kind") == "calendar" else record.get("due_ts")
        if type(at) not in (int, float) or at <= now or now - float(record.get("at") or 0) > DONE_SHOWN_S:
            continue
        card = done_card(record, now)
        if card["id"] not in gone:
            out.append(card)
    return out


_TAP_METHODS = ("calendar_event", "reminder", "add_to_day", "block_time")
_REPLY_METHODS = ("email_reply", "text_reply")


_HOLD_METHODS = ("block_time", "add_to_day")


def held_items(store) -> set[str]:
    """The items Wisp is holding an hour for (autohold): their "Block time?" and "Add to Day?"
    cards step aside, and only those; the date itself may still be offered."""
    return {str(r["item_id"]) for r in store.auto_added().values()
            if isinstance(r, dict) and r.get("kind") == "hold" and r.get("status") == "added" and r.get("item_id")}


def merge_cards(done: list[dict], tap: list[dict], cards: list[dict], added_items: set[str],
                limit: int, held: set[str] | frozenset = frozenset()) -> list[dict]:
    """Done cards first, ahead of alerts, counting toward the cap. A tap card
    for an item Wisp already added goes (the done card says it), and a tap card
    of ours steps aside for a calendar/reminder card prepare already made. A card
    offering to block time for an item Wisp already holds an hour for (`held`) goes too."""
    def ref(card):
        return str(((card.get("sources") or [{}])[0] or {}).get("ref") or "")
    kept = [c for c in cards if not (ref(c) in added_items and c["action"].get("method") in _TAP_METHODS)
            and not (ref(c) in held and c["action"].get("method") in _HOLD_METHODS)]
    taken = {ref(c) for c in kept if c["action"].get("method") in _TAP_METHODS}
    # A reply card that already offers the add ("Reply & add to calendar?") makes ours a second one.
    taken |= {ref(c) for c in kept if c["action"].get("method") in _REPLY_METHODS and c["action"].get("event")}
    ours = [c for c in tap if ref(c) not in taken and c["id"] not in {k["id"] for k in kept}]
    alerts = [c for c in kept if c.get("critical")]
    replies = [c for c in kept if c["action"].get("method") in _REPLY_METHODS and not c.get("critical")]
    rest = [c for c in kept if c not in alerts and c not in replies]
    return (done + alerts + replies + ours + rest)[:limit]


# --- one pass, from the background cycle ------------------------------------------------------


# --- proof for mail ------------------------------------------------------------------------------


async def default_mail_proof(item: dict, plan: dict, evidence: dict) -> tuple[bool, str]:
    """(True, "") only when EVERY check holds for this mail item; else (False, why).

    1. Fetched by Wisp's own Google sign-in. The row's account is the linked Gmail
       address itself, so its sender and Message-ID were parsed by gmail_source from
       Gmail's own data. Mail that reaches Wisp only through Apple Mail is not acted
       on: nothing yet confirms what Mail reports as a sender's address, and there is
       no authentication result for it.
    2. A sender read strictly (service/mail_sender.py): not odd, one real address.
    3. A real person writing to the user, not an organisation or a roll-up.
    4. The dated sentence is from the very message whose proof is checked: its own
       arrival is this message's arrival. A date from an older message of the thread
       (its subject, a quoted line) has no proof here.
    5. A known correspondent, proven from the user's side (mail_known.known): that
       exact address is a saved contact's, or the user's own Sent mail went to it
       before this message arrived. Never inferred from the thread or the domain.
    6. Authenticated by the receiving provider (gmail_source.authenticated_for,
       service/mail_auth.py): exactly one result from Gmail itself, showing DMARC
       pass or an aligned DKIM/SPF pass for the From domain. No result on hand
       (never fetched, a restart since) is no proof.
    `evidence` caches the Sent rows, Contacts addresses and the user's own addresses
    for one cycle."""
    from service import gmail_source
    from service.proactive import mail_known
    account, raw_account = str(item.get("account") or ""), str(item.get("raw_account") or "").strip().casefold()
    # Said by sources from the row itself (the linked address is both its account and its
    # account id, and it is not a Mail.app copy), never inferred here from how names look.
    if item.get("google_fetched") is not True or "@" not in raw_account:
        return False, "not from a Google account you linked"
    ref = item.get("ref") or {}
    address, message_id = str(ref.get("to") or "").strip().casefold(), str(ref.get("message_id") or "").strip()
    if item.get("odd_sender") or not address or not message_id:
        return False, "sender not read cleanly"
    if not item.get("person") or item.get("digest") or item.get("critical"):
        return False, "not a person"
    arrived = item.get("received_ts")
    if type(arrived) not in (int, float) or plan.get("line_ts") != arrived:
        return False, "the date is not from this message"
    if "sent" not in evidence:
        evidence["sent"] = await mail_known.read_sent()
        evidence["contacts"] = mail_known.read_contact_addresses()
        try:
            from service.memory.identity import user_emails
            mine = {str(e).casefold() for e in user_emails() or []}
        except Exception:  # noqa: BLE001
            mine = set()
        evidence["mine"] = mine
    sent_rows, sent_fresh = evidence["sent"]
    known, why = mail_known.known(address, before_ts=float(arrived), sent_rows=sent_rows, sent_fresh=sent_fresh,
                                  contact_addresses=evidence["contacts"],
                                  mine=evidence["mine"] | {account.casefold()})
    if known is not True:
        return False, why
    verdict = gmail_source.authenticated_for(raw_account, message_id, address)
    if not (isinstance(verdict, tuple) and len(verdict) == 2 and verdict[0] is True):
        return False, "not verified by your mail provider" if verdict is None else str(verdict[1])
    return True, ""


async def step(survivors: list[dict], sched: dict, *, now: float, tz: str, store, abort,
               writers: dict | None = None, existing=None, is_enabled=None, allows=None,
               mail_proof=None, debug: list | None = None) -> dict:
    """Plan every eligible item, write up to the caps, record each outcome.

    Returns {"tap": tap-to-add cards (auto-add off, or over the caps),
    "added": fingerprints added this pass, "items": item ids Wisp has added}.
    `writers`, `existing`, `is_enabled` and `allows` let tests replace the native
    writes, the synced schedule, the setting and the arrived-after-the-yes check;
    production uses the verified paths and acting.allows."""
    writers = writers or WRITERS
    on = (is_enabled or enabled)()
    evidence: dict = {}                      # default_mail_proof's per-cycle cache
    if allows is None:
        if is_enabled is None:
            from service.proactive import acting
            allows = lambda arrived: acting.allows(arrived, "add")       # noqa: E731
        else:
            # A caller that replaces the setting must also say which arrivals it covers.
            # Nothing is assumed: without it, everything stays a tap card.
            allows = lambda _received: False                             # noqa: E731
    rows = list(sched.get("calendar") or []) + list(sched.get("reminders") or [])
    rows += list((existing or default_existing)(now) or [])
    decided = store.decisions()
    gone = set(decided["dismissed"]) | set(decided["fired"])
    suppressed = set(decided["suppressed"])
    records = store.auto_added()
    # A write an earlier cycle began and never finished recording (cancelled, timed out, crashed):
    # it may have landed. It becomes "unknown", which counts toward the daily cap and is never
    # written again; the person may find the event or reminder and remove it themselves.
    for stale_fp, stale in list(records.items()):
        if isinstance(stale, dict) and stale.get("status") == "dispatching" and stale.get("kind") in ("calendar", "reminder"):
            try:
                store.auto_add_put(stale_fp, {**stale, "status": "unknown"})
                _audit("proactive_auto_add", source="proactive_auto_add", status="unknown", ok=False,
                       args={"fingerprint": stale_fp, "reason": "a write was begun and never recorded"})
            except Exception:  # noqa: BLE001 — it still blocks as "dispatching" below
                pass
    records = store.auto_added()
    # An undone add retires its whole thread: a later message with a nudged
    # time, or the same plan as a reminder, is never added again.
    retired = {r.get("item_id") for r in records.values()
               if isinstance(r, dict) and r.get("status") in ("undone", "undo_unknown") and r.get("item_id")}
    acted_elsewhere = {r.get("item_id") for r in records.values()
                       if isinstance(r, dict) and r.get("kind") not in ("calendar", "reminder", None)
                       and r.get("status") in ("added", "undone", "unknown", "undo_unknown", "dispatching")
                       and r.get("item_id")}
    today = store.auto_added_since(now - DAY_S)
    from service.proactive.prepare import card_id
    tap, added = [], []
    attempts = 0
    seen: set[str] = set()

    def note(item, reason):
        if debug is not None:
            debug.append({"who": item.get("who", ""), "item": item.get("id"), "auto_add": reason})

    for survivor in sorted(survivors, key=lambda s: -(s["item"].get("received_ts") or 0)):
        item, result = survivor["item"], survivor.get("result")
        plan, reason = plan_for(item, result, now=now, tz=tz)
        if plan is None:
            note(item, reason)
            continue
        if item.get("id") in seen:
            continue
        seen.add(item["id"])
        if item.get("id") in retired:
            note(item, "undone before")
            continue
        fp = plan["fingerprint"]
        record = records.get(fp) or {}
        if record.get("status") in _RECORD_STATES_BLOCKING or int(record.get("attempts") or 0) >= MAX_ATTEMPTS:
            note(item, f"already {record.get('status') or 'tried'}")
            continue
        # The user waved this item away, or already tapped its card.
        if plan["item_fingerprint"] in suppressed or fp in suppressed or any(
                card_id(plan["item_fingerprint"], m) in gone
                for m in ("calendar_event", "reminder", "add_to_day", "text_reply", "email_reply", "read")):
            note(item, "dismissed or handled")
            continue
        if (dup := already_there(plan, rows, tz)) is not None:
            note(item, f"already on {'Calendar' if dup.get('source') == 'calendar' else 'your list'}")
            continue
        if item.get("id") in acted_elsewhere:
            # One message, at most one action of Wisp's own (a follow-up reminder is already on
            # it). Offering the date is not an action: the card the person taps stays. Checked
            # after the lines above, so a card they dismissed or handled, or a date they already
            # put on Calendar themselves, does not come back.
            tap.append(tap_card(plan, now))
            note(item, "Wisp already acted on this message: tap to add")
            continue
        # Only a line that itself arrived after the person's yes is added without a tap:
        # the time of the sentence the date came from, never the conversation's latest line.
        fresh = on and allows(plan.get("line_ts"))
        if on and fresh and plan["source"] == "messages" and _text_service(item) != "iMessage":
            # Only an iMessage's sender handle is one Apple verified. An SMS (or a message whose
            # delivery is not known) can carry any sender number, a saved contact's included.
            tap.append(tap_card(plan, now))
            note(item, "not an iMessage: tap to add")
            continue
        if on and fresh and plan["source"] == "mail":
            # Checked only for a mail plan that would otherwise be written, so the evidence
            # (Sent mail, Contacts, the provider's verdict) is read at most once per cycle.
            if not MAIL_ACTS:
                proven, why = False, "mail does not act"
            else:
                try:
                    proven, why = await (mail_proof or default_mail_proof)(item, plan, evidence)
                except Exception as exc:  # noqa: BLE001 — proof that can't be had is no proof
                    proven, why = False, "proof unavailable: " + type(exc).__name__
            if proven is not True:
                tap.append(tap_card(plan, now))
                note(item, "mail: tap to add (" + str(why) + ")")
                continue
        if not fresh or attempts >= MAX_PER_CYCLE or today + attempts >= MAX_PER_DAY:
            tap.append(tap_card(plan, now))
            note(item, "tap to add" if not on else "arrived before the yes: tap to add" if not fresh
                 else "cap reached: tap to add")
            continue
        if abort():
            note(item, "a turn started")
            break
        attempts += 1
        write = writers["calendar" if plan["kind"] == "calendar" else "reminder"]
        # Recorded BEFORE the write: if this cycle is cancelled or times out while Calendar or
        # Reminders is answering, the next one finds "dispatching", reads it as a write that may
        # have landed (see the top of this function), and never writes it again.
        store.auto_add_put(fp, {**_record(plan, now), "status": "dispatching",
                                "attempts": int(record.get("attempts") or 0) + 1})
        try:
            outcome = await write(plan)
        except Exception as exc:  # noqa: BLE001 — after a possible dispatch the outcome is unknown
            outcome = {"ok": False, "status": "unknown", "error": type(exc).__name__}
        outcome = outcome if isinstance(outcome, dict) else {"ok": False, "status": "unknown"}
        verified = outcome.get("ok") is True and isinstance(outcome.get("source_id"), str) \
            and bool(outcome["source_id"].strip())
        status = "added" if verified else "unknown" if outcome.get("status") == "unknown" else \
            "blocked" if outcome.get("status") == "blocked" else "failed"
        _audit("proactive_auto_add", tool="add_calendar_event" if plan["kind"] == "calendar" else "add_reminder",
               source="proactive_auto_add", status=status, ok=verified,
               args={"fingerprint": fp, "title_sha256": hashlib.sha256(plan["title"].encode()).hexdigest()[:16],
                     "when_ts": plan.get("start_ts") or plan.get("due_ts")})
        if status == "blocked":
            # Nothing was dispatched (the app away, view-only, the policy, a
            # turn): a later cycle may try again; the attempt cap still binds.
            note(item, "blocked: " + str(outcome.get("error") or ""))
            store.auto_add_put(fp, {**_record(plan, now), "status": "blocked",
                                    "attempts": int(record.get("attempts") or 0)})
            continue
        entry = {**_record(plan, now), "status": status, "attempts": int(record.get("attempts") or 0) + 1}
        if isinstance(outcome.get("reminders"), list) and outcome["reminders"]:
            entry["reminders"] = [r for r in outcome["reminders"] if isinstance(r, dict)]
        if verified:
            entry["source_id"] = outcome["source_id"].strip()
            if plan["kind"] == "reminder" and isinstance(outcome.get("title"), str) and outcome["title"].strip():
                entry["title"] = outcome["title"]        # what Reminders read back: undo matches it exactly
            added.append(fp)
            note(item, "added")
        else:
            note(item, f"write {status}")
        store.auto_add_put(fp, entry)
    # Only what auto-add itself put on Calendar or Reminders hides a message's own add-card: a
    # reply reminder or a held hour on the same item does not make the date "already added".
    items = {r.get("item_id") for r in store.auto_added().values()
             if isinstance(r, dict) and r.get("status") == "added" and r.get("item_id")
             and r.get("kind") in ("calendar", "reminder")}
    return {"tap": tap, "added": added, "items": {"autoadd:" + i for i in items} | items}


def _record(plan: dict, now: float) -> dict:
    keep = ("kind", "title", "quote", "item_id", "source", "who", "account", "received_ts", "timezone",
            "start_ts", "duration_min", "day", "due_ts", "fingerprint", "item_fingerprint")
    return {**{k: plan[k] for k in keep if k in plan}, "at": now}


# --- undo (the executor's undo_add) -------------------------------------------------------------


async def undo(card: dict, store, *, undoers: dict | None = None, now: float | None = None) -> dict:
    """Delete exactly what Wisp added for this card, by the identifier in the
    store's record. Verified, or nothing changes; an unconfirmed delete is
    never retried blindly (the record says so)."""
    now = time.time() if now is None else now
    fp = (card.get("action") or {}).get("record")
    record = store.auto_added().get(fp) if isinstance(fp, str) else None
    native = isinstance(record, dict) and record.get("kind") in ("calendar", "reminder")
    if not isinstance(record, dict) or record.get("status") != "added" or (native and not record.get("source_id")):
        return {"ok": False, "status": "blocked", "error": "Wisp has no record of adding this, so nothing was removed."}
    from service.proactive import acting
    call = {**UNDOERS, **(undoers or {})}.get(record.get("kind")) or acting.undoer(record.get("kind"))
    if call is None:
        return {"ok": False, "status": "blocked", "error": "Wisp can't undo that one from here."}
    try:
        result = await call(record)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "status": "unknown", "error": type(exc).__name__}
    result = result if isinstance(result, dict) else {"ok": False, "status": "unknown"}
    ok = result.get("ok") is True
    _audit("proactive_auto_add_undo", tool="cancel_calendar_event" if record.get("kind") == "calendar"
           else "delete_reminder", source="proactive_card", ok=ok,
           status="succeeded" if ok else result.get("status", "failed"), args={"fingerprint": fp})
    if ok:
        store.auto_add_put(fp, {**record, "status": "undone", "undone_at": now})
        if not native:
            return {"ok": True, "status": "succeeded", "message": str(result.get("message") or "Undone")}
        where = "your calendar" if record.get("kind") == "calendar" else "your reminders"
        return {"ok": True, "status": "succeeded", "message": f"Removed it from {where}"}
    if result.get("status") == "unknown":
        store.auto_add_put(fp, {**record, "status": "undo_unknown"})
        return {"ok": False, "status": "unknown",
                "error": "Wisp couldn't confirm it was removed. Check before trying again."}
    return {"ok": False, "status": result.get("status") or "failed",
            "error": str(result.get("error") or "That didn't go through.")}
