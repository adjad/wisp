"""Checks every model output passes before it is shown or acted on.

Model output is untrusted: it is parsed fail-closed, checked for personal
identifiers, and held to what its source actually says. Nothing here calls a
model or reads a source.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# --- personal identifiers -----------------------------------------------------

_SSN = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")
_DIGIT_RUN = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
# A code or secret stated with its value: "code is 482913", "password: hunter2",
# "OTP 4829". The value is what matters; the word alone is not an identifier.
_SECRET_WITH_VALUE = re.compile(
    r"\b(?:password|passcode|passwd|pwd|pin|otp|one[- ]time (?:code|password|passcode)|"
    r"verification code|security code|confirmation code|2fa code|auth(?:entication)? code|login code|"
    r"sign[- ]in code|access code|code)\b\s*(?:is|:|=|-)?\s*[\"'“]?[A-Za-z0-9!#$%^&*_-]{4,}", re.I)
# In model OUTPUT even the bare words are enough to drop a summary: a summary
# never needs to talk about a credential.
_SECRET_WORD = re.compile(r"\b(?:password|passcode|passwd|otp|one[- ]time code|verification code|security code|"
                          r"2fa|social security|ssn|routing number|account number|card number|cvv)\b", re.I)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def has_card_number(text: str) -> bool:
    for match in _DIGIT_RUN.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def source_sensitive(text: str) -> bool:
    """The source itself carries an identifier: never sent to the model."""
    text = text or ""
    return bool(_SSN.search(text) or has_card_number(text) or _SECRET_WITH_VALUE.search(_without_benign_code(text)))


def _without_benign_code(text: str) -> str:
    # "dress code", "zip code", "promo code" and "code review" are not secrets.
    return re.sub(r"\b(?:dress|zip|postal|area|promo|discount|coupon|country|source|bar|qr|color|morse)\s+code\b|"
                  r"\bcode\s+(?:review|freeze|of conduct|base|camp)\b", " ", text, flags=re.I)


def output_has_pii(text: str) -> bool:
    text = text or ""
    return bool(_SSN.search(text) or has_card_number(text) or _SECRET_WITH_VALUE.search(_without_benign_code(text))
                or _SECRET_WORD.search(text))


# --- addresses and links ------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|co|edu|gov|ly|me|ai|app|dev|xyz)\b", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){7,15}(?!\d)")


def has_contact_or_link(text: str) -> bool:
    """An address, link or phone number: model text may never carry one, so an
    instruction hidden in an email can't smuggle a destination into a card."""
    return bool(_EMAIL.search(text or "") or _URL.search(text or "") or _PHONE.search(text or ""))


# --- grounding ------------------------------------------------------------------

_ALWAYS_OK = {"i", "i'm", "i'll", "i've", "i'd", "ok", "okay", "re", "fwd", "tbd", "asap", "rsvp", "fyi"}


def _words(text: str) -> set[str]:
    return {re.sub(r"['’]s$", "", w).casefold() for w in re.findall(r"[\w'’]+", text or "")}


def grounded(text: str, source: str, *, allowed: set[str] | None = None) -> bool:
    """Held to its source: no number the source doesn't carry, no address,
    link or phone, and every capitalized word past a sentence's start (a name,
    a place, a day) appears in the source or the allowed words (the user's
    own name)."""
    text = (text or "").strip()
    if not text:
        return False
    if has_contact_or_link(text):
        return False
    source_words = _words(source) | _ALWAYS_OK | {w.casefold() for w in (allowed or set())}
    for number in re.findall(r"\d+", text):
        if not re.search(rf"(?<!\d){number}(?!\d)", source or ""):
            return False
    for sentence in re.split(r"(?<=[.!?:;])\s+|\n+", text):
        words = re.findall(r"[\w'’]+", sentence)
        for word in words[1:]:
            bare = re.sub(r"['’]s$", "", word)
            if bare[:1].isupper() and bare.casefold() not in source_words:
                return False
    return True


# --- parsing --------------------------------------------------------------------


def strip_think(text: str) -> str | None:
    """The reply without a closed think block; None when a think tag is left
    (an unclosed block puts the monologue in the content)."""
    text = re.sub(r"<\s*think\s*>.*?<\s*/\s*think\s*>", "", text or "", flags=re.I | re.S)
    if re.search(r"<\s*/?\s*think", text, re.I):
        return None
    return text.strip()


def parse_object(text: str) -> dict | None:
    """The first complete JSON object in a reply, or None. Fails closed."""
    text = strip_think(text)
    if text is None:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start = text.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for index in range(start, len(text)):
            ch = text[index]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                    except ValueError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def clean(value, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# --- due phrases ----------------------------------------------------------------

_SLANG = [(re.compile(p, re.I), r) for p, r in (
    (r"\b(?:tmrw|tmrww|tmr|tmw|tmrow|tomoz|tomo|2moro|2morrow|tomorow|tommorow|tommorrow)\b", "tomorrow"),
    (r"\b(?:tonite|2nite|tnite)\b", "tonight"),
    (r"\b(?:tdy)\b", "today"),
    (r"\b(?:wknd)\b", "weekend"),
    (r"\b(?:nxt)\b", "next"),
)]


def normalize_slang(text: str) -> str:
    for pattern, replacement in _SLANG:
        text = pattern.sub(replacement, text or "")
    return text


_THIS_WEEK = re.compile(r"\b(?:this|later this|by the end of (?:the|this)|end of (?:the|this)|by end of)\s+week\b|"
                        r"\bthis\s+weekend\b|\bsometime\s+this\s+week\b", re.I)
_TONIGHT = re.compile(r"\btonight\b|\bthis evening\b", re.I)
_TODAY = re.compile(r"\btoday\b|\bthis (?:morning|afternoon)\b", re.I)


def resolve_due(phrase: str, arrival_ts: float, tz: str) -> tuple[float | None, str, bool]:
    """(due_ts, label, has_clock) for a due phrase copied from a source, read
    as of when the source arrived. Unresolvable phrases give (None, "", False).

    "tmrw" and friends are spelled out first; "this week" gets a label and the
    end of the week, since the resolver deliberately won't date it."""
    text = normalize_slang(clean(phrase, 200))
    if not text:
        return None, "", False
    zone = ZoneInfo(tz)
    arrival = datetime.fromtimestamp(arrival_ts, zone)
    try:
        from service.attention.resolve import resolve
        got = resolve(text, arrival, tz)
    except Exception:  # noqa: BLE001 — an unreadable phrase dates nothing
        got = None
    if got is not None and got.blocked is None:
        if got.has_clock and got.start is not None:
            return got.start.timestamp(), "", True
        if got.day is not None:
            end = datetime.combine(got.day, datetime.min.time(), zone) + timedelta(hours=23, minutes=59)
            return end.timestamp(), "", False
    if _TONIGHT.search(text) or _TODAY.search(text):
        end = arrival.replace(hour=23, minute=59, second=0, microsecond=0)
        return end.timestamp(), "", False
    if _THIS_WEEK.search(text):
        days = 6 - arrival.weekday()
        end = (arrival + timedelta(days=days)).replace(hour=23, minute=59, second=0, microsecond=0)
        return end.timestamp(), "This week", False
    return None, "", False


def due_label(due_ts: float | None, now: float, tz: str, *, has_clock: bool = False, fixed: str = "") -> str:
    """"Today 3:00 PM", "Tomorrow", "Overdue · 3 days", "This week", "Mon Oct 26"."""
    if due_ts is None:
        return fixed
    zone = ZoneInfo(tz)
    due = datetime.fromtimestamp(due_ts, zone)
    today = datetime.fromtimestamp(now, zone).date()
    clock = due.strftime("%-I:%M %p") if has_clock else ""
    if due_ts < now and due.date() < today:
        days = (today - due.date()).days
        return f"Overdue · {days} day{'s' if days != 1 else ''}"
    if fixed:
        return fixed
    if due.date() == today:
        return f"Today {clock}".strip() if has_clock or due_ts >= now else "Today"
    if due.date() == today + timedelta(days=1):
        return f"Tomorrow {clock}".strip()
    if (due.date() - today).days < 7:
        return f"{due.strftime('%a')} {clock}".strip()
    return f"{due.strftime('%a %b %-d')} {clock}".strip()


# --- second person ------------------------------------------------------------------

_VERB_S = ("says", "asks", "wants", "needs", "plans", "thinks", "tells", "agrees", "replies", "tries", "has",
           "is", "was", "does", "goes", "gets", "keeps", "sends", "shares", "posts", "hopes", "feels",
           "likes", "loves", "knows", "owes", "promises", "offers", "suggests", "mentions", "confirms",
           "explains", "writes", "receives", "finds", "launches", "signs", "joins", "attends", "decides",
           "invites", "thanks", "texts", "emails", "calls", "meets", "works", "applies", "registers",
           "reminds", "needs", "wonders", "reports", "announces", "shares", "plans", "remembers",
           "notes", "adds", "lists", "updates", "requests", "reminds", "submits", "studies", "starts",
           "finishes", "visits", "talks", "says", "wants", "agrees", "replies")
_IRREGULAR = {"is": "are", "was": "were", "has": "have", "does": "do", "goes": "go", "tries": "try",
              "replies": "reply", "applies": "apply"}


def _you_verb(match: re.Match) -> str:
    you, verb = match.group(1), match.group(2)
    low = verb.casefold()
    if low not in _VERB_S or low == "posted":
        return match.group(0)
    if low in _IRREGULAR:
        fixed = _IRREGULAR[low]
    elif low.endswith(("sses", "shes", "ches", "xes")):
        fixed = low[:-2]
    else:
        fixed = low[:-1]
    return f"{you} {fixed}"


def _conjugate(word: str) -> str:
    low = word.casefold()
    if low in _IRREGULAR:
        return _IRREGULAR[low]
    if low in _VERB_S and low.endswith("s"):
        if low.endswith("ies") and len(low) > 4:
            return low[:-3] + "y"
        if low.endswith(("sses", "shes", "ches", "xes", "zes")):
            return low[:-2]
        return low[:-1]
    return word


def second_person(text: str, names: list[str] | tuple[str, ...] = ()) -> str:
    """Model prose about the user, addressed to them: "The user posted…" and
    "Adi told Trishe…" become "You posted…" and "You told Trishe…"; "Adi's"
    becomes "your", and a verb right after a replaced subject agrees ("The
    user reminds" → "You remind")."""
    if not text:
        return text
    subjects = [r"the user"] + [re.escape(n) for n in sorted({n for n in names if n and len(n) >= 2},
                                                             key=len, reverse=True)]
    pattern = re.compile(r"\b(?:" + "|".join(subjects) + r")\b(['’]s)?(\s+(\w+))?", re.I)

    def swap(match: re.Match) -> str:
        before = text[:match.start()].rstrip()
        start = not before or before[-1] in ".!?:;\n"
        if match.group(1):
            word = "Your" if start else "your"
            return word + (match.group(2) or "")
        word = "You" if start else "you"
        if match.group(3):
            return f"{word}{match.group(2)[:-len(match.group(3))]}{_conjugate(match.group(3))}"
        return word
    return pattern.sub(swap, text)


def names_user(text: str, names) -> bool:
    """The text names the user (their name, or "the user") as a person."""
    subjects = [r"the user"] + [re.escape(n) for n in names if n and len(n) >= 2]
    return bool(re.search(r"\b(?:" + "|".join(subjects) + r")\b", text or "", re.I))


# --- bulk senders ---------------------------------------------------------------------

_BULK_LOCAL = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply|notifications?|notify|alerts?|news|newsletters?|"
    r"info|hello|hi|team|marketing|updates?|digest|mailer|mail|bounce|support|contact|community|events?|"
    r"announce(?:ments?)?|promo(?:tions?)?|offers?|deals?|store|shop|billing|accounts?|admin|outreach|"
    r"campaigns?|members?|membership|subscriptions?|welcome|feedback|jobs?|careers?|recruiting|"
    r"invitations?|reminders?|calendar-notification|postmaster|messages?|edu|learn|courses?)(?:[-_.+].*)?$", re.I)
_BULK_DOMAIN = re.compile(
    r"(?:^|\.)(?:indeed\.com|kaggle\.com|princetonreview\.com|review\.com|devpost\.com|mlh\.io|substack\.com|"
    r"linkedin\.com|medium\.com|mailchimp\.com|mcsv\.net|sendgrid\.net|hubspot(?:email)?\.(?:com|net)|"
    r"vote\.org|actblue\.com|ngpvan\.com|eab\.com|e2ma\.net|constantcontact\.com|campaign-archive\.com|"
    r"beehiiv\.com|convertkit\.com|klaviyo(?:mail)?\.com|ccsend\.com|mktomail\.com|salesforce\.com|"
    r"exacttarget\.com|amazonses\.com|quora\.com|glassdoor\.com|handshake\.com|joinhandshake\.com|"
    r"ziprecruiter\.com|youtube\.com|facebookmail\.com|instagram\.com|twitter\.com|"
    r"discord\.com|notion\.so|canva\.com|coursera\.org|udemy\.com|duolingo\.com|"
    r"uber\.com|doordash\.com|amazon\.com|apple\.com|paypal\.com|venmo\.com|chase\.com|spotify\.com|"
    r"netflix\.com|hacktoberfest\.com|digitalocean\.com|outreach\.senate\.ca\.gov|senate\.ca\.gov|house\.gov)$",
    re.I)
# Campus and civic mass-mail platforms send under a staff member's name
# ("Vanessa via Navigate", a councilmember's newsletter).
_BULK_PLATFORM = re.compile(r"(?:^|\.)navigate[\w-]*\.|(?:^|\.)(?:lists?|listserv|mailman|bulk|news|e|em|email|"
                            r"mail\d*|send|go|info|marketing|comms?|communications)\.[\w-]+\.|\.gov$", re.I)
_BULK_NAME = re.compile(r"\b(?:team|newsletter|briefing|digest|weekly|daily|news|alerts?|updates?|notifications?|"
                        r"department|office|services|housing|police|university|college|program|center|"
                        r"foundation|campaign|committee|club|society|association|hq|official|advising|advisors?|"
                        r"admissions|registrar|financial aid|career center|student life|residential|"
                        r"\w*news\w*|\w*letter)\b", re.I)


def bulk_sender(name: str, address: str) -> bool:
    """An organization or an automated sender, not a person writing to the user."""
    address = (address or "").strip().casefold()
    local, _, domain = address.partition("@")
    if local and _BULK_LOCAL.match(local):
        return True
    if domain and (_BULK_DOMAIN.search(domain) or _BULK_PLATFORM.search(domain)):
        return True
    if name and _BULK_NAME.search(name):
        return True
    return False
