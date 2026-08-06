"""iMessage/SMS summary via gemma.

Message history is READ by the Swift app (MessagesReader.swift, direct SQLite
read of ~/Library/Messages/chat.db under Wisp.app's own Full Disk Access grant)
and pushed to /assistant/sync/messages — same split as Mail and Calendar,
because chat.db is one of the classic FDA-protected paths and the Python
backend is a separate process that can't share the app's TCC grant. Here we
just cache the pushed lines and summarize with the FAST model (gemma).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from service.config import role_to_model
from service.inference.omlx_client import OMLXClient
from service.tools import cache_store
from service.tools.registry import register

_SYS = (
    "You are Wisp, the user's warm, caring personal assistant catching them up on "
    "their recent texts — the way a close friend who scrolled through your messages "
    "would fill you in, not a terse machine report.\n"
    "\n"
    "FORMAT it to be pleasant and easy to scan — NOT a wall of text:\n"
    "- Open with a short, warm one-line lead-in (a fitting emoji is welcome, e.g. "
    "💬).\n"
    "- Go conversation by conversation, each with a bold header that STARTS "
    "with a relevant emoji — a person emoji for a one-to-one chat, 👥 for a "
    "group — and the conversation's name, e.g. '**👩 Mom**', '**👥 Grad GC**'.\n"
    "- COVER EVERY CONVERSATION that has messages, one-to-one ones included. "
    "Each line's middle field is its conversation label: labels beginning "
    "'Group' are group chats (treat each as its OWN separate section — never "
    "merge two different groups), and anything else is a one-to-one chat with "
    "that person. A label that is still a bare phone number or email just "
    "means that contact isn't saved — summarize it anyway, referring to them "
    "by that handle. Do NOT skip a conversation because its label looks "
    "technical.\n"
    "- Under each, a couple of natural sentences on what's going on and the vibe. "
    "Clearly flag anything that's a question waiting on YOUR reply or is time-"
    "sensitive (a ⏰ or a bolded 'needs a reply' is great).\n"
    "- Close with a brief, caring line — offer to help reply if something's "
    "pending, or just a warm note if it's all quiet.\n"
    "\n"
    "VOICE: warm, human, and caring — like a friend catching you up — with varied "
    "sentence rhythm and a few tasteful emojis where they fit (not on every line). "
    "Lead with whatever needs a response soonest. Do NOT restate messages one-by-"
    "one or copy lines verbatim — synthesize (e.g. 'Mom wants to firm up Saturday's "
    "party — she's asking what time works and whether you're bringing a swimsuit', "
    "not just 'Mom: party details'). If there's genuinely only one short message, a "
    "sentence or two is plenty. Stay grounded in what's actually there — never "
    "invent people, plans, times, or details that aren't in the messages."
)

# Latest message lines pushed by the Swift app — "epochSecs | context | Who: text"
# per line (MessagesReader.swift), newest-scanned-first.
_lines: str = ""
_available = False
_unavailable_reason = ""

_client: OMLXClient | None = None

# Restore the last run's synced messages (see cache_store). `_available` stays
# False until a real sync confirms chat.db is still readable — restored content
# proves the last run could read it, not that this one can.
_lines = cache_store.load("messages")

# handle (phone/email, digits-normalized) -> contact display name, pushed by
# ContactsReader.swift. chat.db only stores handles, so without this map the
# model sees "+19255576442" and has no way to know who that is — it guessed,
# which is how the profile ended up mismatching relationships.
_contacts: dict[str, str] = {}


def _norm_handle(h: str) -> str:
    """Normalize a phone/email handle for matching. Phone numbers appear in
    several shapes across chat.db and Contacts (+1 925…, (925) …, 925-…), so
    phones reduce to their last 10 digits; emails just lowercase."""
    h = (h or "").strip()
    if "@" in h:
        return h.lower()
    digits = re.sub(r"\D", "", h)
    return digits[-10:] if len(digits) >= 10 else digits


# name (lowercased) -> the contact's handles in their ORIGINAL form.
#
# _contacts above is keyed by NORMALIZED handle (last 10 digits) because its
# job is recognizing an incoming handle. That normalization is lossy — it drops
# the country code — so it can't be used to SEND to someone: reversing it would
# hand Messages a bare 10-digit number and break every non-US contact. This map
# keeps what Contacts actually reported, which is what `send_message` needs.
_name_handles: dict[str, list[str]] = {}


def cache_contacts(mapping: dict) -> None:
    global _contacts, _name_handles
    _contacts = {_norm_handle(k): v for k, v in (mapping or {}).items()
                 if k and v and _norm_handle(k)}
    by_name: dict[str, list[str]] = {}
    for handle, name in (mapping or {}).items():
        if not (handle and name):
            continue
        by_name.setdefault(str(name).strip().lower(), []).append(str(handle).strip())
    _name_handles = by_name
    cache_store.save("contacts", json.dumps(_contacts))
    cache_store.save("contact_handles", json.dumps(_name_handles))


try:
    _contacts = json.loads(cache_store.load("contacts") or "{}")
except Exception:  # noqa: BLE001
    _contacts = {}

try:
    _name_handles = json.loads(cache_store.load("contact_handles") or "{}")
except Exception:  # noqa: BLE001
    _name_handles = {}

if not _name_handles and _contacts:
    # Cold-start fallback: rebuild the reverse map from the normalized handles
    # we already have. Those are digits-only and country-code-stripped, so this
    # is strictly worse than what ContactsReader pushes — but it's the
    # difference between "can't find Mom" and a number that works for domestic
    # contacts, and the next contacts sync (on app launch, then every 6h)
    # overwrites it with the real handles. Also covers the upgrade case, where
    # contacts.txt exists from a previous version but contact_handles.txt
    # doesn't yet.
    _rebuilt: dict[str, list[str]] = {}
    for _handle, _name in _contacts.items():
        if _handle and _name:
            _rebuilt.setdefault(str(_name).strip().lower(), []).append(str(_handle))
    _name_handles = _rebuilt


_HANDLE_RE = re.compile(r"\+?\d[\d\-().\s]{8,}\d|\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def resolve_contact(text: str, *, prefix_only: bool = False) -> str:
    """Replace any phone/email handle in `text` with the contact's name when
    known. `prefix_only` limits substitution to the leading "Who:" speaker
    label on a message line, so a phone number quoted inside the message BODY
    is left alone (rewriting body text would corrupt what was actually said).
    """
    if not _contacts:
        return text
    if prefix_only:
        head, sep, rest = text.partition(":")
        if not sep or len(head) > 60:
            return text
        return f"{resolve_contact(head)}{sep}{rest}"

    def sub(m: re.Match) -> str:
        return _contacts.get(_norm_handle(m.group(0)), m.group(0))
    return _HANDLE_RE.sub(sub, text)


def find_contacts(name: str) -> list[dict]:
    """Reverse lookup: a person's NAME -> their saved handles.

    The map only ever ran handle -> name (for labeling incoming messages), so
    there was no way to answer "what's Mom's number" at all. Verified failure:
    asked to text Mom, the model had 99 contacts cached including an exact
    "Mom" entry, and still reported it couldn't find a number — `send_message`
    told it to find the handle via `view_messages`, which is precisely the tool
    that REPLACES handles with names before the model ever sees them.

    Matching runs exact -> whole-word -> substring, and stops at the first tier
    that hits. Without the tiering, "Mom" also matched "Mommy's Gym" and any
    contact whose surname contains those letters, and a send tool that resolves
    a recipient ambiguously is worse than one that resolves none.
    """
    query = (name or "").strip().lower()
    if not query or not _name_handles:
        return []

    exact = [n for n in _name_handles if n == query]
    word = [n for n in _name_handles
            if n != query and re.search(rf"\b{re.escape(query)}\b", n)]
    partial = [n for n in _name_handles
               if n not in exact and n not in word and query in n]
    matches = exact or word or partial

    out = []
    for n in matches:
        handles = _name_handles.get(n, [])
        out.append({"name": n.title() if n.islower() else n,
                    "handles": handles,
                    "preferred": _preferred_handle(handles)})
    return out


# Digits-only projection of the message cache, for "has this number actually
# been texted?" checks. Memoized on the cache's identity — stripping every
# non-digit out of the full history is far too expensive to redo per handle
# per lookup, and the cache only changes on a sync.
_digits_blob: tuple[int, str] = (-1, "")


def _messages_digits() -> str:
    global _digits_blob
    if _digits_blob[0] != len(_lines):
        _digits_blob = (len(_lines), re.sub(r"\D", "", _lines))
    return _digits_blob[1]


def _preferred_handle(handles: list[str]) -> str:
    """Which handle to actually send to when a contact has several.

    A handle the person has DEMONSTRABLY messaged from wins over any ordering
    heuristic — for a contact with a mobile, a landline, and an email, message
    history is direct evidence of which one reaches them. Phone beats email
    only as the fallback when there's no history to go on.
    """
    if not handles:
        return ""
    if _lines:
        blob = _messages_digits()
        for h in handles:
            key = _norm_handle(h)
            if key and "@" not in h and len(key) >= 10 and key in blob:
                return h
        for h in handles:
            if "@" in h and h.lower() in _lines.lower():
                return h
    phones = [h for h in handles if "@" not in h]
    return (phones or handles)[0]


def contact_names() -> list[str]:
    """Distinct saved contact names, alphabetical — identity ground truth for
    the profile builder (see profile.contact_roster)."""
    return sorted({v for v in _contacts.values() if v})


def contact_message_stats() -> dict[str, dict]:
    """Per-SAVED-CONTACT message activity from the cache: {name: {count,
    first_ts, last_ts}} — a deterministic join against Contacts, no model
    involved. Used by memory/entities.py so "People in their life" has a
    ground-truth floor instead of depending entirely on an LLM noticing a name
    in a batch of raw text (a real build left the section empty despite 100+
    saved contacts and hundreds of resolved messages sitting in this same
    cache — see memory/entities.py's docstring).

    A name counts once per line, either as the 1:1 conversation partner
    (`ctx`, for non-group chats) or as the speaker (for group chats) — both
    already resolved from handle to contact name by `_parse_lines`.
    """
    known = {v for v in _contacts.values() if v}
    stats: dict[str, dict] = {}
    for ts, ctx, txt in _parse_lines():
        names: set[str] = set()
        ctx = ctx.strip()
        if not ctx.startswith("Group") and ctx in known:
            names.add(ctx)
        speaker, sep, _body = txt.partition(":")
        speaker = speaker.strip()
        if sep and speaker in known:
            names.add(speaker)
        for name in names:
            s = stats.setdefault(name, {"count": 0, "first_ts": ts, "last_ts": ts})
            s["count"] += 1
            s["first_ts"] = min(s["first_ts"], ts)
            s["last_ts"] = max(s["last_ts"], ts)
    return stats


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _c() -> OMLXClient:
    global _client
    if _client is None:
        _client = OMLXClient()
    return _client


def cache_messages(lines: str, available: bool, reason: str = "") -> None:
    global _lines, _available, _unavailable_reason
    _lines = lines or ""
    _available = available
    _unavailable_reason = reason
    # Only persist a SUCCESSFUL read. A failed sync (no Full Disk Access, DB
    # locked) posts empty lines with available=False — saving that would wipe
    # a perfectly good restored cache on the first failed sync after launch.
    if available and _lines:
        cache_store.save("messages", _lines)


def have_messages() -> bool:
    return bool(_lines.strip())


def _parse_lines() -> list[tuple[float, str, str]]:
    """Each cached line -> (epoch_seconds, context, text), with phone/email
    handles already swapped for saved contact names.

    Resolved HERE rather than per-caller so every consumer benefits — summaries
    and verbatim lookups as well as the profile builder. Previously only
    all_message_text() resolved, so `summarize_messages` still described
    conversations as "+16507961110" instead of "Mom", which is exactly the sort
    of line the summarizer then skipped as noise.
    """
    out: list[tuple[float, str, str]] = []
    for line in _lines.strip().splitlines():
        parts = line.split(" | ", 2)
        if len(parts) != 3:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        out.append((ts, resolve_contact(parts[1]),
                    resolve_contact(parts[2], prefix_only=True)))
    return out


# --- Addressee detection -----------------------------------------------------
#
# A prompt rule alone was not enough to stop the misattribution this exists for.
# The summarizer, told who the user is and that "your" in someone else's message
# belongs to whoever they addressed, still reported "@Trishe - How is the AI
# conference going? Any traction for your app?" — Mom, to a four-person family
# group — as the user's conference and the user's app. The fast model (gemma
# E4B) reads a line at a time and simply does not connect a mid-sentence
# @mention to the pronoun three words later.
#
# So the addressee is resolved HERE, deterministically, and stated on the line.
# Cheap string work over names Wisp already knows, and the model no longer has
# to infer anything: it is told, in words, whose message this is about.

# `Group of N (A, B, C, +K more)` — the members MessagesReader.label lists for
# an unnamed group, after handle resolution.
_GROUP_MEMBERS_RE = re.compile(r"^Group of \d+ \((.*?)(?:, \+\d+ more)?\)$")


def _label_members(context: str) -> list[str]:
    """Names a conversation label lists, or [] if it lists none.

    Named groups (`Group "Grad GC"`) carry no member list, so they fall back to
    @mention matching against the full contact roster below — an explicit `@`
    is unambiguous enough on its own.
    """
    m = _GROUP_MEMBERS_RE.match(context.strip())
    if not m:
        return []
    return [p.strip() for p in m.group(1).split(",") if p.strip()]


def _addressee(context: str, sender: str, body: str) -> str:
    """Who a group message is addressed to, or "" if it doesn't say.

    Two patterns, both requiring a name Wisp actually knows — never a bare word
    out of the message text:

    1. `@Name` anywhere. An explicit mention, so it is matched against every
       saved contact and works in named groups too.
    2. `Name -` / `Name,` / `Name?` leading the message, matched only against
       the members the label lists. Restricting pattern 2 to listed members is
       what keeps "tell Sriram he should have checked in with you first" from
       reading as a message TO Sriram — he is talked about, not addressed.

    Returns "" for one-to-one chats: there is only one possible addressee there
    and tagging every line would be noise.
    """
    if not context.startswith("Group"):
        return ""
    body = body.strip()
    members = _label_members(context)
    sender_key = sender.strip().casefold()

    # 1. Explicit @mention, against the whole roster.
    for m in re.finditer(r"@\s*([A-Za-z][\w'’.-]*(?:\s+[A-Za-z][\w'’.-]*)?)", body):
        cand = m.group(1).strip()
        for known in list(_contacts.values()) + members:
            if known.casefold() == cand.casefold() and known.casefold() != sender_key:
                return known
            # "@Sam Rivera" style: the mention carries a longer form of a saved
            # name (saved as just "Sam"). Match on the first token so it still
            # resolves.
            if cand.casefold().startswith(known.casefold() + " ") and known.casefold() != sender_key:
                return known

    # 2. Vocative at the head of the message, listed members only.
    for known in members:
        if known.casefold() == sender_key:
            continue
        if re.match(rf"^{re.escape(known)}\s*[-–—,:?]", body, re.IGNORECASE):
            return known
    return ""


# How long an explicit @mention keeps applying to the SAME sender's other
# messages in the SAME conversation. Sized for the case that produced the bug:
# Mom sent "Your post has 439 likes", then 20 seconds later the edited
# "* @Trishe - Your post has 439 likes". Only the second names Trishe, so
# without carry-over the first still read as the user's post — the very claim
# the user reported. Same sender and same conversation only; a REPLY from
# someone else may well have changed who is being talked to, and guessing that
# would trade one misattribution for another.
_ADDRESSEE_CARRY_SECS = 300.0


def render_for_summary(rows: list[tuple[float, str, str]]) -> list[str]:
    """`conversation | Sender: text` lines for a SUMMARIZER's prompt, with the
    addressee spelled out where one is detectable.

    Deliberately not used by view_messages/all_message_text: those return the
    verbatim record, and an annotation inside it would read as something a
    person actually typed.
    """
    parsed: list[tuple[float, str, str, str, str]] = []  # ts, ctx, txt, sender, addressee
    for ts, ctx, txt in rows:
        sender, sep, body = txt.partition(":")
        sender = sender.strip()
        parsed.append((ts, ctx, txt, sender,
                       _addressee(ctx, sender, body) if sep else ""))

    # Carry an explicit mention across the sender's own nearby messages. Order-
    # independent, because callers hand rows over both newest-first (the brief)
    # and oldest-first (the per-day summary).
    anchors = [(ts, ctx, sender, who) for ts, ctx, _t, sender, who in parsed if who]

    out: list[str] = []
    for ts, ctx, txt, sender, who in parsed:
        if not who and sender:
            for a_ts, a_ctx, a_sender, a_who in anchors:
                if (a_ctx == ctx and a_sender == sender
                        and abs(a_ts - ts) <= _ADDRESSEE_CARRY_SECS):
                    who = a_who
                    break
        if who:
            out.append(f"{ctx} | {txt}   [addressed to {who} — 'you'/'your' "
                       f"in this message means {who}, NOT the user]")
        else:
            out.append(f"{ctx} | {txt}")
    return out


def _unavailable_message() -> str:
    if _unavailable_reason:
        return (f"(Can't read Messages: {_unavailable_reason}. Grant Wisp Full "
                "Disk Access — System Settings > Privacy & Security > Full Disk "
                "Access > Wisp — then try again.)")
    return ("(No message data yet. Grant Wisp Full Disk Access — System "
            "Settings > Privacy & Security > Full Disk Access > Wisp — then "
            "try again.)")


def _day_bounds(day: str) -> tuple[float, float, str]:
    today = datetime.now().date()
    if day == "today":
        d = today
    elif day == "yesterday":
        d = today - timedelta(days=1)
    else:
        d = datetime.fromisoformat(day).date()
    start = datetime(d.year, d.month, d.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp(), d.strftime("%A, %B %-d")


async def _summarize(raw_lines: list[str], header_label: str) -> str:
    if not raw_lines:
        return f"No messages found for {header_label}."
    c = _c()
    model = role_to_model("fast")  # gemma — cheap
    await c.ensure_only(model)
    today_str = datetime.now().strftime("%A, %B %-d, %Y")
    # See email_tools._summarize — same reasoning: this output is returned
    # verbatim as the final answer, so the profile has to be injected here or it
    # never reaches a message summary at all.
    #
    # identity_prompt_block goes FIRST and is not optional: this summary is
    # written in the second person about a multi-person conversation, and
    # without a statement of who the user is the model read "@Trishe - Your
    # post has 439 likes" (Mom, to a family group) as the user's own post.
    from service.memory.identity import identity_prompt_block
    from service.memory.profile import profile_context_block
    identity = identity_prompt_block().strip()
    resp = await c.chat(
        model,
        [{"role": "system", "content": f"{identity}\n\n{_SYS}\nToday is {today_str}; resolve any "
                                        "relative dates ('tonight', 'tomorrow') against it."
                                        + profile_context_block(1600)},
         {"role": "user", "content": f"Recent messages for {header_label} "
                                     f"(conversation | sender: text):\n" + "\n".join(raw_lines)}],
        # See email_tools.py's _summarize — same rationale for the higher token
        # budget and the temperature 0.6 (warmer, more expressive phrasing while
        # staying grounded in the actual messages).
        temperature=0.6, max_tokens=800)
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    return text or "\n".join(raw_lines)


async def summarize_messages_for_day(day: str) -> str:
    if not _lines.strip():
        return _unavailable_message()
    try:
        start, end, label = _day_bounds(day)
    except ValueError:
        return f"(couldn't understand the date {day!r} — use 'today', 'yesterday', or YYYY-MM-DD)"
    rows = [(ts, ctx, txt) for ts, ctx, txt in _parse_lines() if start <= ts < end]
    rows.sort(key=lambda r: r[0])
    return await _summarize(render_for_summary(rows), label)


async def summarize_messages_recent(count: int = 30) -> str:
    if not _lines.strip():
        return _unavailable_message()
    return await _summarize(render_for_summary(_parse_lines()[:count]),
                            "your recent messages")


def _matches(query: str, context: str, text: str) -> bool:
    q = query.lower()
    return q in context.lower() or q in text.lower()


async def view_messages_impl(query: str | None = None, day: str | None = None,
                             count: int = 20) -> str:
    if not _lines.strip():
        return _unavailable_message()
    rows = _parse_lines()
    label = "your recent messages"
    if day:
        try:
            start, end, label = _day_bounds(day)
        except ValueError:
            return f"(couldn't understand the date {day!r} — use 'today', 'yesterday', or YYYY-MM-DD)"
        rows = [r for r in rows if start <= r[0] < end]
    rows.sort(key=lambda r: r[0])
    scoped = rows
    note = ""
    if query:
        matched = [r for r in scoped if _matches(query, r[1], r[2])]
        if matched:
            rows = matched
        else:
            # No exact keyword hit — hand back the whole scoped set so gpt-oss
            # can read through it itself rather than dead-ending on a substring
            # miss (e.g. it searches "pickup" but the text says "grab it").
            rows = scoped
            count = max(count, 15)
            note = (f"(no exact match for {query!r} — showing the raw set below so you "
                    "can look through it yourself)\n\n")
    if not rows:
        return f"No messages found for {label}."
    rows = rows[-count:]  # most recent `count` after filtering
    lines = []
    for ts, ctx, text in rows:
        when = datetime.fromtimestamp(ts).strftime("%a %b %-d, %-I:%M %p")
        lines.append(f"[{when}] {ctx} — {text}")
    return note + "\n".join(lines)


@register(
    "view_messages",
    "Get the RAW, verbatim text of the user's messages — exact wording, not a "
    "summary. Use this instead of summarize_messages when the user needs a "
    "specific detail FROM a message: an order number, an address, a pickup "
    "time, party details, a code someone sent, exact wording to quote back, "
    "etc. Pass `query` to search for a keyword across sender/conversation and "
    "text (e.g. 'birthday', 'pizza', a person's name); pass `day` to scope to "
    "a specific day; omit both for the most recent messages.",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "keyword to search for in the conversation or message text"},
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — scope to that day"},
         "count": {"type": "integer",
                   "description": "max messages to return (default 20)"},
     }},
    category="messages_read",
)
async def view_messages(query: str | None = None, day: str | None = None, count: int = 20) -> str:
    return await view_messages_impl(query, day, count)


def all_message_text() -> str:
    """Every cached message line, verbatim, DATE-STAMPED and oldest-first —
    for the profile builder (service/memory/profile.py).

    The date is essential, not decorative: without it the profile builder had
    no way to tell a year-old message from today's and wrote things like
    "England v France match at 2 PM tomorrow" as a current fact when the
    message was eleven months old. Oldest-first so the model reads the user's
    history in the order it happened and the newest (most likely still true)
    facts land last.

    Handles are resolved to contact names where known (see cache_contacts) —
    a bare "+19255576442 | ..." gave the model no way to tell who anyone was,
    which is how relationships ended up mismatched.
    """
    rows = sorted(_parse_lines(), key=lambda r: r[0])
    out = []
    for ts, ctx, txt in rows:
        # Handles are already resolved by _parse_lines.
        speaker, sep, body = txt.partition(":")
        if sep:
            # Body QUOTED so message content can never be read as identity.
            # Unquoted, a message from Mom whose text was literally "Arati Wani"
            # rendered as `Mom: Arati Wani` and the builder concluded that was
            # Mom's name. Quoting makes "what was said" unmistakably distinct
            # from "who said it".
            out.append(f'{_date(ts)} | {ctx} | {speaker} said: "{body.strip()}"')
        else:
            out.append(f"{_date(ts)} | {ctx} | {txt}")
    return "\n".join(out)


@register(
    "summarize_messages",
    "Read the user's recent iMessage/SMS conversations and summarize them "
    "(grouped by conversation, flags anything needing a reply). Use whenever "
    "the user asks about their messages/texts/iMessage. Pass `day` ('today', "
    "'yesterday', or 'YYYY-MM-DD') to summarize ALL of that day's messages; "
    "omit it for just the most recent ones. Summarized by the fast local model.",
    {"type": "object",
     "properties": {
         "day": {"type": "string",
                 "description": "'today', 'yesterday', or 'YYYY-MM-DD' — summarize that whole day's messages"},
         "count": {"type": "integer",
                   "description": "when `day` is omitted, how many recent messages to scan (default 30)"},
     }},
    category="messages_read",
)
async def summarize_messages(day: str | None = None, count: int = 30) -> str:
    if day:
        return await summarize_messages_for_day(day)
    return await summarize_messages_recent(count)


@register(
    "lookup_contact",
    "Look up a saved contact's phone number or email by their NAME — 'Mom', "
    "'Dan', 'Dr. Patel'. Use this before send_message or send_email whenever "
    "the user names a person instead of giving an address. Do NOT try to find "
    "someone's number with view_messages: that tool replaces phone numbers "
    "with contact names before you see them, so it can never show you a "
    "number. If this returns nothing, say you couldn't find them in Contacts "
    "and ask the user for the number — never guess one.",
    {"type": "object",
     "properties": {
         "name": {"type": "string", "description": "the contact's name, e.g. 'Mom'"},
     },
     "required": ["name"]},
    category="messages_read",
)
async def lookup_contact(name: str) -> str:
    matches = find_contacts(name)
    if not matches:
        if not _name_handles:
            return ("(No contacts are synced yet — Wisp needs Contacts access, "
                    "or the first sync hasn't finished. Ask the user for the "
                    "number directly.)")
        return (f"(No saved contact matches {name!r}. Ask the user for the "
                "number or email — do not guess one.)")
    if len(matches) > 1:
        listed = "\n".join(f"- {m['name']}: {m['preferred']}" for m in matches[:8])
        return (f"Several contacts match {name!r} — ask the user which one they "
                f"mean before sending anything:\n{listed}")
    m = matches[0]
    others = [h for h in m["handles"] if h != m["preferred"]]
    out = f"{m['name']}: {m['preferred']}"
    if others:
        out += f" (also on {', '.join(others)})"
    return out


@register(
    "list_contacts",
    "List the user's saved contacts by name — from the same Contacts sync "
    "lookup_contact uses, not a generated script (a self-authored tool can "
    "never do this: it has no Contacts access of its own). Use this for "
    "'list/show all my contacts', not lookup_contact, which only resolves ONE "
    "name at a time. Pass `query` to filter by a substring of the name; omit "
    "it to list everyone. Returns names only, not numbers — call "
    "lookup_contact on a specific name for their number or email.",
    {"type": "object",
     "properties": {
         "query": {"type": "string",
                   "description": "optional substring to filter names by"},
     }},
    category="messages_read",
)
async def list_contacts(query: str = "") -> str:
    names = contact_names()
    if not names:
        return ("(No contacts are synced yet — Wisp needs Contacts access, or "
                "the first sync hasn't finished.)")
    q = (query or "").strip().lower()
    if q:
        names = [n for n in names if q in n.lower()]
        if not names:
            return f"(No saved contact name contains {query!r}.)"
    return f"{len(names)} contact(s):\n" + "\n".join(f"- {n}" for n in names)
