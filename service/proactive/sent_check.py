"""Did the user already do what a "contact this person" suggestion asks?

handled.py's reply check sees an answer in the same thread. This one is for the
other case: a card says "Reach out to your section TA about attendance", and the
user already wrote a new email to that TA, which no thread check can see. Before a
suggestion like that surfaces, and while it stands, its target and topic are looked
for in the user's own recent sent mail, sent after the source message arrived.

Code only: no model, no network. Sent rows are headers (recipients and subject) from
sources.all_sent_rows. They are untrusted text that is only ever compared here, never
put in a prompt, a card or a log, and never read as instructions.

Only a positive match hides anything, so Sent data that is missing, partial or out of
date keeps the suggestion. How a person is matched, strongest first:

- An address or a name ("Prof. Shah", "Priya Shah"): that exact address, or every part
  of the name in a recipient's display name or address, and a subject that shares a
  word with the topic or says nothing ("Quick question"). A subject plainly about
  something else keeps the card.
- A role ("your section TA"): headers can't say who holds a role, so the recipient
  needs outside evidence: the role word in their name or address, their address
  appearing in the source message, or the source sender's organization. Then the
  subject has to be about the topic. Replying to the sender doesn't count, the card
  told the user to contact someone else, and neither does sharing the source itself
  with someone only the organization ties to the role.
- No such evidence: the subject alone has to say it clearly, three topic words at
  least, one of them specific. A named person who didn't get the mail isn't rescued
  by this; emailing Dana about the lease doesn't mean Sam was told.

What this can't catch: a body-only mention (only headers are read), a vague subject
sent to a role, a role holder outside the sender's organization under a differently
worded subject, a text or a call, a Bcc, or mail from an account Wisp can't read.
"""
from __future__ import annotations

import re
from email.utils import getaddresses
from typing import NamedTuple

from service.proactive import sources

# Verb first, like every proactive title (judge._good_title).
_SOFT = r"ask|tell|notify|inform"           # needs someone named: "Ask for an extension" isn't contact
_VERB = re.compile(
    rf"(?i)^\s*(?:please\s+)?(?P<verb>reach\s+out(?:\s+to)?|get\s+in\s+touch(?:\s+with)?|contact|"
    r"e-?mail(?:\s+to)?|message|write(?:\s+an?\s+e-?mail)?\s+to|"
    r"send\s+(?:an?\s+)?(?:e-?mail|message|note)(?:\s+to)?|follow\s+up\s+with|check\s+in\s+with|"
    rf"{_SOFT})\b\s*")
_WRAPPED = re.compile(r"(?i)^\s*(?:please\s+)?(?:let\s+(?P<a>.+?)\s+know|"
                      r"(?:send|drop)\s+(?P<b>.+?)\s+an?\s+(?:e-?mail|message|note|line))\b\s*")
_NOT_A_PERSON = re.compile(r"(?i)^(?:from|thread|draft|address|account|inbox|list|chain|signature)\b")
_TOPIC_START = re.compile(r"(?i)^(?:about|regarding|concerning|re|on|for|that|if)\b")
# Where the person ends and the rest of the sentence begins.
_TARGET_END = re.compile(r"(?i)\s+(?:about|regarding|concerning|re|on|for|to|by|before|after|that|if|when|so|and|"
                         r"because|asking|today|tonight|tomorrow|this|next)\b|\s*[,:;!?(]|\s+[-–—]\s")
_HONORIFIC = re.compile(r"(?i)\b(dr|prof|mr|mrs|ms|mx)\.")
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_COURSE_CODE = re.compile(r"\b[A-Za-z]{2,6}[ -]?\d{1,3}[A-Za-z]?\b")
_MORE = re.compile(r"\s*\(\+\d+ more\)\s*$")
_NAME_AND_ADDRESS = re.compile(r"^(.*?)<([^<>]+)>\s*$")

_DETERMINERS = frozenset("your the my our a an their his her this that".split())
_PRONOUNS = frozenset("them him her it someone everyone anyone people".split())
_HONORIFICS = frozenset("dr prof professor mr mrs ms mx".split())
_ROLES = frozenset("""ta tas gsi gsis tutor professor prof instructor lecturer teacher advisor adviser counselor
    counsellor registrar dean bursar manager boss supervisor recruiter coordinator landlord hr office department
    dept team staff support admin administrator assistant rep representative mentor""".split())
_CONSUMER_DOMAINS = frozenset("""gmail.com googlemail.com outlook.com hotmail.com live.com msn.com yahoo.com
    icloud.com me.com mac.com aol.com proton.me protonmail.com fastmail.com hey.com gmx.com""".split())
_NOT_TOPIC = frozenset("""the and for with about from this that your you are was were have has had can could would will
    please just get got let lets regarding concerning into out reach contact email e-mail mail message send sent write
    follow check touch ask tell notify inform them him her his their our not but what when how why who
    fwd""".split())
# On topic only with company: nearly any school or work email could say these.
_GENERIC = frozenset("""section discussion class course lecture question help request meeting update info information
    note quick hello thanks thank today tomorrow yesterday week time issue problem concern follow followup
    monday tuesday wednesday thursday friday saturday sunday morning afternoon tonight day""".split())
_MAX_MENTIONED = 6


class Contact(NamedTuple):
    kind: str                # "address", "name", "role" or "none"
    words: frozenset         # the address, the name's parts, or the role's words
    topic: str               # the title's words after the person


def contact_of(title) -> Contact | None:
    """The person and topic of a "contact X" suggestion, or None for anything else."""
    text = _HONORIFIC.sub(r"\1", re.sub(r"\s+", " ", str(title or "")).strip())
    wrapped = _WRAPPED.match(text)
    if wrapped:
        target, rest = wrapped.group("a") or wrapped.group("b"), text[wrapped.end():]
        soft = True
    else:
        verb = _VERB.match(text)
        if not verb:
            return None
        rest = text[verb.end():]
        if _NOT_A_PERSON.match(rest):
            return None
        soft = re.fullmatch(_SOFT, verb.group("verb"), re.I) is not None
        if _TOPIC_START.match(rest):
            target = ""
        else:
            end = _TARGET_END.search(rest)
            target, rest = (rest[:end.start()], rest[end.start():]) if end else (rest, "")
    contact = _classify(target.strip(" .'’\""), rest.strip(" .,:;!?-–—("))
    if soft and contact.kind == "none":
        return None
    return contact


def _classify(target: str, topic: str) -> Contact:
    found = _ADDRESS.search(target)
    if found:
        return Contact("address", frozenset({found.group(0).casefold()}), topic)
    words = [re.sub(r"['’]s$", "", w).strip(".'’\"") for w in target.split()]
    words = [w for w in words if w and w.casefold() not in _DETERMINERS]
    if not words or (len(words) == 1 and words[0].casefold() in _PRONOUNS):
        return Contact("none", frozenset(), f"{target} {topic}".strip())
    if words[-1].casefold() not in _ROLES:
        # Capitalized, and not an acronym: "TA Priya" is Priya, "Prof Shah" is Shah.
        names = [w.casefold() for w in words if re.fullmatch(r"[A-Z][\w'’-]*[a-z][\w'’-]*", w)
                 and w.casefold() not in _HONORIFICS and w.casefold() not in _ROLES]
        if names and words[-1].casefold() in names:
            return Contact("name", frozenset(names), topic)
    return Contact("role", frozenset(_singular(w.casefold()) for w in words), topic)


def _singular(word: str) -> str:
    return word[:-1] if len(word) >= 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text) -> set[str]:
    """The words that can say what a subject is about."""
    bare = _COURSE_CODE.sub(" ", str(text or ""))
    # Any script's letters: a subject in another language says something, it isn't vague.
    words = re.findall(r"[^\W\d_][\w'’-]*", bare.casefold())
    found = {_singular(w) for w in words if len(w) >= 3 or not w.isascii()}
    return found - _NOT_TOPIC


def recipients(to) -> list[tuple[str, str]]:
    """(display name, address) pairs. Gmail gives the raw To header; Apple Mail a list
    of "Name <address>" or bare addresses, whose names aren't quoted."""
    if isinstance(to, (list, tuple)):
        out = []
        for entry in to:
            found = _NAME_AND_ADDRESS.match(str(entry))
            out.append((found.group(1).strip(' "'), found.group(2).strip()) if found else ("", str(entry).strip()))
    else:
        out = getaddresses([_MORE.sub("", str(to or ""))])
    return [(name, address.casefold()) for name, address in out if "@" in address]


def _organization(address: str) -> str:
    """A mail domain's organization, or "" for a mailbox provider, where sharing a
    domain says nothing. Two labels, three for a country's "ac.uk" or "edu.au"."""
    domain = address.rpartition("@")[2].casefold()
    if not domain or domain in _CONSUMER_DOMAINS:
        return ""
    parts = domain.split(".")
    keep = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("ac", "co", "edu", "gov", "org") else 2
    return ".".join(parts[-keep:])


def _identity_tokens(name: str, address: str) -> tuple[set[str], str]:
    local = address.partition("@")[0]
    return ({t for t in re.findall(r"[a-z]+", f"{name} {local}".casefold()) if len(t) >= 2}, local)


def _names_match(names: frozenset, name: str, address: str) -> bool:
    tokens, local = _identity_tokens(name, address)
    if names <= tokens:
        return True
    # "pshah" or "shahp" for Priya Shah; for a surname alone, an initial and the name.
    bare = re.sub(r"[^a-z]", "", local.casefold())
    ordered = sorted(names, key=len)
    if len(names) == 2:
        short, long = ordered
        return len(long) >= 3 and bare in (short[0] + long, long + short[0], long[0] + short, short + long[0])
    return len(names) == 1 and len(ordered[0]) >= 4 and len(bare) == len(ordered[0]) + 1 and bare[1:] == ordered[0]


def _holds_role(contact: Contact, name: str, address: str, mentioned) -> bool:
    """Something ties this recipient to the role itself."""
    tokens, _ = _identity_tokens(name, address)
    return address in mentioned or bool({_singular(t) for t in tokens}
                                        & (contact.words & {_singular(r) for r in _ROLES}))


def _about_topic(subject_words: set[str], topic_words: set[str]) -> bool:
    overlap = subject_words & topic_words
    return bool(overlap - _GENERIC) or len(overlap) >= 2


def matches(contact: Contact, row: dict, *, subject: str, sender: str, mentioned, mine) -> bool:
    """This one sent row did what the suggestion asks."""
    people = [(n, a) for n, a in recipients(row.get("to")) if a not in mine]
    if not people:
        return False                     # a note to self, or nobody we can see
    sent_subject = sources.bare_subject(row.get("subject") or "")
    sent_words = _words(sent_subject)
    same_subject = bool(subject) and sent_subject.casefold() == sources.bare_subject(subject).casefold()
    # The title says what it's about; the source's subject stands in only when it doesn't.
    topic_words = _words(contact.topic) or _words(subject)
    topic_words -= {_singular(w) for w in contact.words}
    if contact.kind in ("address", "name"):
        reached = any(a in contact.words if contact.kind == "address" else _names_match(contact.words, n, a)
                      for n, a in people)
        return reached and (same_subject or not topic_words or bool(sent_words & topic_words)
                            or not sent_words - _GENERIC)
    # A role, or nobody at all. The sender isn't who the card sent them to.
    others = [(n, a) for n, a in people if a != sender]
    if not others or not topic_words:
        return False
    if contact.kind == "role" and any(_holds_role(contact, n, a, mentioned) for n, a in others):
        return same_subject or _about_topic(sent_words, topic_words)
    # The source's own subject, forwarded or replied to all, to someone nothing ties to
    # the role: sharing the message with a classmate isn't contacting the TA.
    if same_subject:
        return False
    organization = _organization(sender)
    if (contact.kind == "role" and organization and len(others) == len(people)
            and any(_organization(a) == organization for _, a in others)):
        return _about_topic(sent_words, topic_words)
    overlap = sent_words & topic_words
    return len(overlap) >= 3 and bool(overlap - _GENERIC)


def own_addresses(sent=()) -> set[str]:
    mine = {str(r.get("account") or "").casefold() for r in sent if "@" in str(r.get("account") or "")}
    try:
        from service.memory.identity import user_emails
        mine |= {e.casefold() for e in user_emails()}
    except Exception:  # noqa: BLE001 — unknown identity: only the sending accounts
        pass
    return mine


def already_contacted(title, *, received_ts, sent, subject: str = "", sender: str = "", mentioned=(), mine=None,
                      now: float | None = None) -> bool:
    """The user sent mail after the source arrived that does what `title` suggests.
    With no source time there's no "after", so nothing can count."""
    contact = contact_of(title)
    if contact is None or not sent or type(received_ts) not in (int, float):
        return False
    mine = own_addresses(sent) if mine is None else mine
    sender = str(sender or "").casefold()
    mentioned = {str(a).casefold() for a in mentioned or ()}
    for row in sent:
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= received_ts or (now is not None and ts > now + 300):
            continue
        if matches(contact, row, subject=subject, sender=sender, mentioned=mentioned, mine=mine):
            return True
    return False


def mentioned_addresses(text) -> list[str]:
    """Addresses a source message names, for a role it hands the user: "write to your
    TA at ...". Kept beside the rest of what handled.py remembers about an item."""
    found = dict.fromkeys(a.casefold().rstrip(".") for a in _ADDRESS.findall(str(text or "")))
    return list(found)[:_MAX_MENTIONED]
