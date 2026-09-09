"""Outbound actions — the things Wisp DOES, as opposed to reads and reports.

The tools that SPEAK IN THE USER'S NAME (send_email, reply_to_email,
send_message) sit in the always-confirm categories (see policy.
_ALWAYS_CONFIRM_OUTBOUND): they ask every single time, they can't be
pre-approved with a standing grant, and full-access mode doesn't skip the
prompt. That asymmetry is deliberate — full-access is about not being nagged
while operating your own computer, and speaking in your name to other people
isn't that.

The DRAFT tools (draft_email, draft_message) deliberately do NOT carry that
weight. A draft opens a compose window with the text filled in and sends
nothing; the user's own click in Mail/Messages is the confirmation, which is a
stronger gate than a card in Wisp. They exist because the natural flow — "write
this, let me look, then send" — otherwise had to be modelled as a send, which
is the one thing that can't be taken back.

The TRIAGE tools (mark_email_read, archive_email) act only on the user's own
mailbox and nothing leaves the machine. Archive is reversible (the message
moves to Archive, it is not deleted), so neither is treated as outbound.

Everything except http_request goes through the app bridge (service/assistant/
outbox.py) because Mail and Messages Automation is granted to Wisp.app, not to
this Python process. http_request runs here since outbound HTTP needs no OS
grant.
"""
from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

import httpx

from service.assistant.outbox import request as app_request
from service.tools.registry import register
from service.tools.timeranges import WHEN_ARG

_MAX_RESPONSE_CHARS = 3000
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


_OUTBOUND_PREVIEW_TOOLS = {
    "send_message", "draft_message", "send_email", "draft_email", "reply_to_email",
}


# Frozen retrieval documents from before the prompt-only latency change.
# The older wording is search data, not instructions: preserving every byte
# keeps semantic vectors, lexical rankings and BM25 corpus statistics stable.
# Model schemas and UI documentation use the updated descriptions below.
_OUTBOUND_RETRIEVAL_DESCRIPTIONS = {
    'send_email': (
        "Send an email from the user's Mail account. Use when the user asks to "
        "email/reply to someone. ALWAYS show the user the recipient, subject, and "
        "full body in your message BEFORE calling this — they approve the send on a "
        "confirmation card that shows only a summary, so the draft itself has to "
        "have been visible in the conversation first. `to` accepts either an email "
        "address or a saved contact's NAME ('Mom', 'Dan') — a name is resolved from "
        "Contacts automatically, and `lookup_contact` will show you the address "
        "first if you want to confirm it. Never invent an address; if it can't be "
        "resolved you'll be told, and you should ask the user rather than guess. "
        "Write the body as the user would send it — no placeholders like "
        "[Your Name], no meta-commentary. If `to` is the USER'S OWN address, this "
        "is refused once by default (it usually means an attribution address got "
        "used as a destination by mistake) — if the user's own words in this "
        "conversation just confirmed they genuinely want to email themselves, "
        "call this again with confirmed_self_send=true."
    ),
    'reply_to_email': (
        "Reply to a specific email IN ITS ORIGINAL THREAD, keeping the subject and "
        "conversation intact. Use this — not send_email — whenever the user says "
        "'reply', 'respond', or 'answer' about mail they received. You need the "
        "message's Message-ID, which view_emails prints for each email; call "
        "view_emails first if you don't have it. Only `body` is yours to write: the "
        "recipient and subject come from the original message, so don't restate "
        "them. ALWAYS show the user the full reply text in your message BEFORE "
        "calling this. Write it as the user would send it — no placeholders like "
        "[Your Name], no meta-commentary. Set reply_all only if the user asked to "
        "include everyone on the thread."
    ),
    'send_message': (
        "Send an iMessage/SMS from the user's Messages app. Use when the user asks "
        "to text/message someone. ALWAYS show the exact text you're about to send "
        "in your message BEFORE calling this. `to` accepts a phone number, an "
        "iMessage email address, or a saved contact's NAME ('Mom', 'Dan') — a name "
        "is resolved from Contacts automatically. Do NOT use view_messages to hunt "
        "for someone's number: it replaces numbers with contact names before you "
        "see them, so it can never show you one. Use `lookup_contact` if you want "
        "to confirm the number first. Keep it in the user's own voice: short, no "
        "sign-off, no 'sent from my assistant'. If `to` is one of the USER'S OWN "
        "email addresses (iMessage can use those as a handle), this is refused "
        "once by default — if the user's own words just confirmed they genuinely "
        "want to message themselves, call this again with confirmed_self_send=true."
    ),
}


def confirm_preview(tool: str, args: dict) -> str | None:
    """A human-readable rendering of what an outbound tool is about to send —
    or None if `tool` isn't one of these.

    MEASURED GAP (2026-08-18): the confirmation card's `reason` for every one
    of these tools is a fixed generic string — "sends something on your
    behalf — always confirmed" — with no recipient and no content. The
    backend already includes the real `args` (to/text/subject/body) in the
    confirm event, but the Swift client's `Pending` struct has no `args`
    field and never reads it, so a user clicking "Allow once" has never
    actually seen what they're approving.

    Rather than changing the client, this reuses the `preview` field
    `create_tool`'s confirmation already populates (service/agent/loop.py) —
    a distinct, already-wired, already-rendered (scrollable, monospaced)
    block in the card, just never populated for these tools. Same mechanism,
    a second use site.
    """
    if tool not in _OUTBOUND_PREVIEW_TOOLS:
        return None
    if tool in ("send_message", "draft_message"):
        to = str(args.get("to", "")).strip() or "(no recipient given)"
        text = str(args.get("text", "")).strip() or "(empty)"
        return f"To: {to}\n\n{text}"
    if tool in ("send_email", "draft_email"):
        to = str(args.get("to", "")).strip() or "(no recipient given)"
        cc = str(args.get("cc", "")).strip()
        subject = str(args.get("subject", "")).strip() or "(no subject)"
        body = str(args.get("body", "")).strip() or "(empty)"
        header = f"To: {to}" + (f"\nCc: {cc}" if cc else "") + f"\nSubject: {subject}"
        return f"{header}\n\n{body}"
    if tool == "reply_to_email":
        from service.tasks.reply_contract import preview, validate_envelope
        if envelope := validate_envelope(args.get("expected_reply")):
            return preview(envelope)
        body = str(args.get("body", "")).strip() or "(empty)"
        note = " (reply-all)" if args.get("reply_all") else ""
        return f"Reply{note} to message {args.get('message_id', '?')}\n\n{body}"
    return None


def _own_address_guard(to: str, tool: str, confirmed_self_send: bool = False) -> str | None:
    """Corrective message when `to` is one of the USER'S OWN addresses — else None.

    MEASURED FAILURE (2026-08-18). Asked to "draft a message to mom with my
    stock movements", the model sent an iMessage to `AdiJain888@gmail.com` —
    the user's own address, which the identity block puts in every agent system
    prompt (agent/loop.py's identity_hint). Mom never got it; the user texted
    themselves.

    This is the SAME failure shape as builtin._wrong_account_path, where the
    same email became a fake `/Users/AdiJain888` home directory: an identifier
    that exists in the prompt for ATTRIBUTION gets grabbed to fill an unrelated
    slot. Because a literal address matches _EMAIL_RE, it skips
    _resolve_recipient entirely — the very code that would have caught a name
    it could not resolve. So the guard has to sit before that branch.

    Corrective, not silent: name the tool that finds the real recipient, the
    way tools/timeranges.BadPeriod teaches its vocabulary. Deliberately NOT a
    hard block — texting yourself is legitimate ("send me a reminder"), so the
    message tells the model how to proceed if the user REALLY meant themselves.

    VERIFIED FAILURE 2026-08-19 (live testing, this session): the docstring's
    "not a hard block" claim was FALSE as originally written — the guard fired
    unconditionally on every call with no way to pass, no matter how explicitly
    the user confirmed. Live transcript: guard blocks -> user says "yes I
    genuinely mean to send it to myself, this is intentional" -> model calls
    send_email again with the identical arguments -> guard blocks AGAIN,
    verbatim, because nothing about the retry was different from the model's
    point of view. It concluded (reasonably, given what it could see) "they
    can't send to their own email address using this tool" — an absolute
    limitation that isn't real, presented as if it were. `confirmed_self_send`
    is the actual escape hatch the docstring always claimed existed: the model
    sets it to true on a RETRY, once the user's own words in this conversation
    have confirmed the self-send is intentional — not before, and not as a
    default, so an unconfirmed self-send is still caught the first time.
    """
    addr = (to or "").strip().lower()
    if not addr or confirmed_self_send:
        return None
    try:
        from service.memory.identity import user_emails
        own = {e.strip().lower() for e in (user_emails() or []) if e}
    except Exception:  # noqa: BLE001 — no identity yet is not a failure
        return None
    if addr not in own:
        return None
    return (f"(error: NOT sent — `{to}` is the USER'S OWN address, not a "
            f"recipient. Their email address is in your prompt for ATTRIBUTION "
            f"(telling their messages from other people's), never as a "
            f"destination. If this is a MISTAKE, call `lookup_contact` with the "
            f"person's NAME to get the real handle, then call {tool} again. If "
            f"the user's own words just confirmed they genuinely mean to send "
            f"to THEMSELVES, call {tool} again with confirmed_self_send=true — "
            f"that is the only thing that changes on the retry; the same "
            f"arguments without it will be blocked again.)")

# Unfilled template placeholders — "[Your Name]", "{recipient}", "<company>".
# Observed live: the agent model signed a real outgoing email "Best,\n[Your Name]"
# despite the tool description telling it not to. Worth catching in code rather
# than only in a prompt, because the cost is asymmetric — the user's actual
# correspondent receives the mistake, and nothing downstream can undo a sent
# message.
_PLACEHOLDER_RE = re.compile(
    r"\[[^\]\n]{2,40}\]|\{\{?[a-z_ ]{2,40}\}?\}|<[a-z][a-z ._-]{1,30}>", re.I)
# Bracketed text that's ordinary prose rather than a template slot.
_PLACEHOLDER_OK_RE = re.compile(
    r"^\[?\s*(?:sic|\d+|https?://|see |note:|via |source:)", re.I)
# A SQUARE-bracketed span is ambiguous in a way the other two forms are not.
# "{recipient}" and "<company>" essentially only occur in templates, but "[…]"
# is ordinary punctuation, and REAL content routinely arrives wearing it.
#
# MEASURED FALSE POSITIVE (2026-09-08, live): a two-week calendar digest the
# user had already approved for their mom was refused at the last step because
# two event titles, copied verbatim out of get_upcoming, carry brackets of
# their own — "Move-in [Adi Jain]" and "Community Meetings with your RA
# [MANDATORY for On-Campus Frosh]". Neither is an unfilled slot; both are the
# real value, already filled in, straight from the calendar.
#
# So a bracketed span now has to READ like a slot before it counts as one: it
# must NAME the kind of thing that belongs there ("[Your Name]", "[date]",
# "[insert address]") rather than BE a thing. The other two forms keep the
# old, looser rule — nothing writes "{recipient}" by accident.
_SLOT_WORD_RE = re.compile(
    r"\b(?:your|name|recipient|sender|insert|placeholder|tbd|todo|date|time|"
    r"address|phone|email|company|subject|topic|link|url|amount|x{2,})\b", re.I)


def _unfilled_placeholders(text: str) -> list[str]:
    holes: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(text or ""):
        span = m.group(0)
        inner = span.strip("[]{}<>").strip()
        if _PLACEHOLDER_OK_RE.match(inner):
            continue
        if span.startswith("[") and not _SLOT_WORD_RE.search(inner):
            continue
        holes.append(span)
    return holes


# These content heuristics — placeholders above, truncation below — exist to
# catch the MODEL's mistakes BEFORE a human sees the draft. They are guesses
# about text, and a guess must never outrank a person who has read the actual
# words and said send.
#
# MEASURED FAILURE (2026-09-08, live): the user was shown the full outgoing
# text on the confirmation card, clicked "Allow once", and the send was then
# refused by the placeholder check running INSIDE the tool — i.e. after the
# approval it was supposed to inform. The ordering was backwards: these checks
# belong in front of the card (see outbound_content_problem, called from
# agent/loop.py before the card is raised), never behind it.
#
# Set only where the exact outgoing content was actually rendered for the user
# — a confirmation card carrying a `preview`, or Wisp's editable draft card —
# never for a bare "sends something on your behalf" card that shows no text.
_CONTENT_REVIEWED: ContextVar[bool] = ContextVar(
    "wisp_content_reviewed", default=False)


@contextmanager
def human_reviewed_content(reviewed: bool = True) -> Iterator[None]:
    """Mark the enclosed send as one whose exact text a human just read."""
    token = _CONTENT_REVIEWED.set(bool(reviewed))
    try:
        yield
    finally:
        _CONTENT_REVIEWED.reset(token)


def _unreviewed() -> bool:
    """True when no human has read the exact text about to go out."""
    return not _CONTENT_REVIEWED.get()


# The small on-device agent model (LFM2.5) sometimes over-escapes when it
# writes tool-call JSON by hand: it emits a literal backslash-n instead of an
# actual newline, and backslash-quote instead of a bare quote (JSON doesn't
# need either escaped that way — it's imitating Python string literals).
# json.loads happily decodes that into a Python string that still CONTAINS
# the two-character sequence "\n" / "\'" rather than a real newline/quote, and
# nothing downstream un-mangles it — it round-trips verbatim through
# AppleScript and lands in the delivered mail/message exactly as typed.
# Observed live: an SMS delivered with literal "\n\n" between lines instead of
# line breaks. Fixed here rather than in the AppleScript layer (OutboundSender
# .escape) because by then a real newline and this artifact are
# indistinguishable — the fix has to happen before that distinction is lost.
_LITERAL_ESCAPE_RE = re.compile(r"\\[nt]|\\(['\"])")


def _degarble(text: str) -> str:
    if not text or "\\" not in text:
        return text
    return _LITERAL_ESCAPE_RE.sub(
        lambda m: {"\\n": "\n", "\\t": "\t"}.get(m.group(0), m.group(1)), text)


def _looks_truncated(text: str) -> bool:
    """A value that opens with a quote character it never closes is the
    signature of another observed artifact: the model started wrapping the
    field in a quote (or an apostrophe'd word) and generation stopped before
    finishing it. Verified live: a scheduling email sent with body "'Here is
    your schedule for tomorrow (Friday" — cut off mid-sentence, no closing
    punctuation, sent as-is with nothing else in it. Deliberately narrow (vs.
    e.g. flagging any text lacking terminal punctuation) so it doesn't reject
    ordinary short, punctuation-free texts like "omw" or "yep"."""
    t = (text or "").strip()
    return bool(t) and t[0] in "'\"" and t[0] not in t[1:]


# Fields whose content is the thing a human would be approving, per tool.
_OUTBOUND_CONTENT_FIELDS = {
    "send_message": ("text",),
    "send_email": ("subject", "body"),
    "reply_to_email": ("body",),
    "schedule_send": ("subject", "body", "text"),
}


def outbound_content_problem(tool: str, args: dict) -> str | None:
    """The content complaint an outbound tool would raise, computed BEFORE the
    confirmation card — or None if the draft is fine.

    Called from agent/loop.py so a draft with an unfilled slot or a cut-off
    body is handed straight back to the model to rewrite, and never becomes a
    card at all. That keeps the checks doing the job they were written for
    (catching the model) while leaving the human's answer final: once the card
    has been shown and approved, the same checks are skipped inside the tool
    (see human_reviewed_content).

    Only reads args, exactly as the tool will receive them — no resolution, no
    sending — so calling it costs nothing and can't have side effects.
    """
    fields = _OUTBOUND_CONTENT_FIELDS.get(tool)
    if not fields:
        return None
    verb = "scheduled" if tool == "schedule_send" else "sent"
    parts = [_degarble(str(args.get(f) or "")) for f in fields]
    if any(_looks_truncated(p) for p in parts):
        correction = (
            f"Rewrite the complete text in {tool}'s arguments for the approval preview."
            if tool in {"send_email", "send_message", "reply_to_email"} else
            f"Write the FULL text out in your reply first, then pass that same complete text to {tool}.")
        return (f"(NOT {verb} — the draft looks cut off mid-sentence rather "
                f"than a complete message. {correction})")
    if (holes := _unfilled_placeholders("\n".join(parts))):
        return (f"(NOT {verb} — the draft still has unfilled placeholders: "
                f"{', '.join(holes[:4])}. Rewrite it with the real values and "
                f"call {tool} again. For the user's own name, use what you "
                f"know about them from your context, or just end the message "
                f"without a signature — a message signed '[Your Name]' is "
                f"worse than one with no sign-off.)")
    return None


def _split_recipients(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;]", value or "") if p.strip()]


def _resolve_recipient(name: str, *, want_email: bool = False) -> tuple[str, str]:
    """A contact NAME -> (handle, ""), or ("", explanation) if it can't be
    resolved unambiguously. The explanation is written for the model to relay.
    """
    from service.tools.imessage_tools import find_contacts

    matches = find_contacts(name)
    if not matches:
        return "", (f"(no saved contact matches {name!r}, and it isn't a valid "
                    "address. Ask the user for it — don't guess.)")
    if len(matches) > 1:
        listed = ", ".join(m["name"] for m in matches[:6])
        return "", (f"({name!r} matches several contacts ({listed}). Ask the "
                    "user which one they mean — do NOT pick one yourself.)")

    handles = matches[0]["handles"]
    if want_email:
        emails = [h for h in handles if "@" in h]
        if not emails:
            return "", (f"({matches[0]['name']} has no email address saved in "
                        "Contacts — only a phone number. Ask the user for it.)")
        if len(set(emails)) > 1:
            return "", (f"({matches[0]['name']} has multiple email addresses saved: "
                        f"{', '.join(emails)}. Ask the user which address to use.)")
        return emails[0], ""

    handle = matches[0]["preferred"]
    if not handle:
        return "", f"({matches[0]['name']} has no usable handle saved in Contacts.)"
    return handle, ""


@register(
    "send_email",
    "Send an email from the user's Mail account. Use when the user asks to "
    "email someone; use reply_to_email for replies in an existing thread. "
    "Put the complete recipient, subject and body in the tool arguments once; "
    "the confirmation card displays them for approval. `to` accepts either an email "
    "address or a saved contact's NAME ('Mom', 'Dan') — a name is resolved from "
    "Contacts automatically, and `lookup_contact` will show you the address "
    "first if you want to confirm it. Never invent an address; if it can't be "
    "resolved you'll be told, and you should ask the user rather than guess. "
    "Write the body as the user would send it — no placeholders like "
    "[Your Name], no meta-commentary. If `to` is the USER'S OWN address, this "
    "is refused once by default (it usually means an attribution address got "
    "used as a destination by mistake) — if the user's own words in this "
    "conversation just confirmed they genuinely want to email themselves, "
    "call this again with confirmed_self_send=true.",
    {"type": "object",
     "properties": {
         "to": {"type": "string",
                "description": "recipient email address; comma-separate several"},
         "subject": {"type": "string", "description": "subject line"},
         "body": {"type": "string", "description": "the full message body"},
         "cc": {"type": "string", "description": "optional cc addresses"},
         "confirmed_self_send": {"type": "boolean",
                                 "description": "set true ONLY on a retry, after the user has explicitly confirmed they mean to email themselves"},
     },
     "required": ["to", "subject", "body"]},
    category="email_send",
    retrieval_description=_OUTBOUND_RETRIEVAL_DESCRIPTIONS["send_email"],
)
async def send_email(to: str, subject: str, body: str, cc: str = "",
                     confirmed_self_send: bool = False) -> str:
    if (msg := _own_address_guard(to, "send_email", confirmed_self_send)):
        return msg
    subject, body = _degarble(subject), _degarble(body)
    if _unreviewed() and (_looks_truncated(subject) or _looks_truncated(body)):
        return ("(NOT sent — the draft looks cut off mid-sentence rather than "
                "a complete subject/body. Rewrite the complete subject and "
                "body in send_email's arguments for the approval preview.)")
    recipients = _split_recipients(to)
    if not recipients:
        return "(no recipient — ask the user who this should go to)"
    # A name where an address belongs gets resolved from Contacts, same as
    # send_message. Anything still unaddressable after that is reported rather
    # than guessed at.
    resolved: list[str] = []
    for r in recipients:
        if _EMAIL_RE.match(r):
            resolved.append(r)
            continue
        handle, problem = _resolve_recipient(r, want_email=True)
        if problem:
            return problem
        resolved.append(handle)
    recipients = resolved
    bad = [r for r in recipients + _split_recipients(cc) if not _EMAIL_RE.match(r)]
    if bad:
        return (f"(not valid email addresses: {', '.join(bad)}. Don't guess an "
                "address — look it up in the user's mail or ask them for it.)")
    if _unreviewed() and (holes := _unfilled_placeholders(f"{subject}\n{body}")):
        return (f"(NOT sent — the draft still has unfilled placeholders: "
                f"{', '.join(holes[:4])}. Rewrite it with the real values. For "
                "the user's own name, use what you know about them from your "
                "context, or just end the message without a signature — an "
                "email signed '[Your Name]' is worse than one with no sign-off.)")

    res = await app_request("send_email", {
        "to": recipients, "cc": _split_recipients(cc),
        "subject": subject or "", "body": body or "",
    })
    if not res.get("ok"):
        return f"(the email was NOT sent: {res.get('error') or 'unknown error'})"
    return f"Email sent to {', '.join(recipients)}."


@register(
    "schedule_send",
    "Send an email or a text LATER, at a specific time. Use whenever the user "
    "says when something should go out — 'text mom at 6', 'email the recruiter "
    "Monday morning', 'send this tonight'. The user approves it once, now, on a "
    "confirmation card showing the recipient, the message, and the delivery "
    "time; it then sends by itself at that time with no further prompting, so "
    "write the message exactly as it should arrive. ALWAYS show the full text "
    "and repeat the time back to the user IN YOUR REPLY (the words they used, "
    "or this tool's returned confirmation time — never a time you computed "
    "yourself) BEFORE calling this. `channel` is 'email' or 'message'. `to` "
    "accepts an address, a phone number, or a saved contact's NAME. Wisp must "
    "be running at that time to send — say so if the time is far off or "
    "overnight. If this tool's result says the time is in the past or "
    "couldn't be understood, NOTHING was scheduled — say so plainly and ask "
    "what time they meant; do not tell the user it was queued.",
    {"type": "object",
     "properties": {
         "channel": {"type": "string", "enum": ["email", "message"],
                     "description": "'email' for mail, 'message' for iMessage/SMS"},
         "to": {"type": "string",
                "description": "email address, phone number, or contact name"},
         "body": {"type": "string", "description": "the full message to send"},
         "text": {"type": "string",
                   "description": "alias for `body` — pass either one, not both. "
                                  "Accepted because send_message/draft_message use "
                                  "`text` for this same concept, so a channel='message' "
                                  "call reaching for that name still works first try."},
         "when": WHEN_ARG,
         "subject": {"type": "string", "description": "subject line (email only)"},
     },
     "required": ["channel", "to", "when"]},
    category="scheduled_send",
)
async def schedule_send(channel: str, to: str, when: str, body: str = "",
                        text: str = "", subject: str = "") -> str:
    from service.assistant.outbound_queue import outbound_queue
    from service.tools.timeranges import BadWhen, resolve_when

    channel = (channel or "").strip().lower()
    if channel not in ("email", "message"):
        return "(channel must be 'email' or 'message')"
    body = _degarble(body or text)
    subject = _degarble(subject)
    if not body.strip():
        return "(nothing to send — pass `body` with the full message)"
    if _unreviewed() and _looks_truncated(body):
        return ("(NOT scheduled — the draft looks cut off mid-sentence. Write "
                "the FULL text out in your reply first, then pass that same "
                "complete text to schedule_send.)")
    # Placeholders are rejected here exactly as they are for an immediate send:
    # nobody is going to be looking at this when it goes out, so a "[Your Name]"
    # that survives to delivery is strictly worse than one caught now.
    if _unreviewed() and (holes := _unfilled_placeholders(f"{subject}\n{body}")):
        return (f"(NOT scheduled — the draft still has unfilled placeholders: "
                f"{', '.join(holes[:4])}. Rewrite it with the real values.)")
    try:
        when_dt, _when_label = resolve_when(when)
    except BadWhen as e:
        return str(e)
    when_ts = when_dt.timestamp()
    if when_ts <= time.time():
        return (f"(NOT scheduled — {when!r} resolves to "
                f"{when_dt:%a %b %-d at %-I:%M %p}, which is in the past. Ask "
                "the user what time they actually meant, or send it now with "
                f"{'send_email' if channel == 'email' else 'send_message'}.)")

    display = to
    if channel == "email":
        recipients = _split_recipients(to)
        if not recipients:
            return "(no recipient — ask the user who this should go to)"
        r = recipients[0]
        if not _EMAIL_RE.match(r):
            r, problem = _resolve_recipient(r, want_email=True)
            if problem:
                return problem
            display = f"{to} ({r})"
        recipient = r
    else:
        recipient = to.strip()
        if not recipient:
            return "(no recipient — ask the user who to text)"
        if not (_EMAIL_RE.match(recipient)
                or re.fullmatch(r"[+()\-.\s\d]{7,}", recipient)):
            handle, problem = _resolve_recipient(recipient)
            if problem:
                return problem
            display = f"{to} ({handle})"
            recipient = handle

    sid = outbound_queue.add(channel=channel, recipient=recipient, body=body,
                             when_ts=when_ts, subject=subject, display=display)
    pretty = when_dt.strftime("%a %b %-d at %-I:%M %p")
    return (f"Scheduled: {'email' if channel == 'email' else 'text'} to {display} "
            f"on {pretty} (id {sid}). It sends automatically — Wisp needs to be "
            "running at that time. Use cancel_scheduled_send to call it off.")


@register(
    "list_scheduled_sends",
    "Show emails and texts the user has scheduled to go out later, with their "
    "ids. Use when they ask what's queued, or before cancelling something.",
    {"type": "object", "properties": {}},
    category="assistant_read",
)
async def list_scheduled_sends() -> str:
    from datetime import datetime

    from service.assistant.outbound_queue import outbound_queue

    rows = outbound_queue.pending()
    if not rows:
        return "Nothing is scheduled to send."
    out = []
    for r in rows:
        when = datetime.fromtimestamp(r["when_ts"]).strftime("%a %b %-d at %-I:%M %p")
        kind = "Email" if r["channel"] == "email" else "Text"
        subj = f" — {r['subject']}" if r["subject"] else ""
        out.append(f"[{r['id']}] {kind} to {r['display']} on {when}{subj}\n"
                   f"    {r['body'][:160]}")
    return "\n".join(out)


@register(
    "cancel_scheduled_send",
    "Cancel a scheduled email or text so it never goes out. Needs the id from "
    "list_scheduled_sends — call that first if you don't have it.",
    {"type": "object",
     "properties": {
         "id": {"type": "string", "description": "id from list_scheduled_sends"},
     },
     "required": ["id"]},
    category="assistant_write",
)
async def cancel_scheduled_send(id: str) -> str:
    from service.assistant.outbound_queue import outbound_queue

    row = outbound_queue.cancel((id or "").strip())
    if not row:
        return ("(no pending scheduled send with that id — call "
                "list_scheduled_sends to see what's actually queued)")
    return f"Cancelled the {row['channel']} to {row['display']}."


@register(
    "reply_to_email",
    "Reply to a specific email IN ITS ORIGINAL THREAD, keeping the subject and "
    "conversation intact. Use this — not send_email — whenever the user says "
    "'reply', 'respond', or 'answer' about mail they received. You need the "
    "message's Message-ID, which view_emails prints for each email; call "
    "view_emails first if you don't have it. Only `body` is yours to write: the "
    "recipient and subject come from the original message, so don't restate "
    "them. Put the complete reply in `body` once; the confirmation card "
    "displays it for approval. Write it as the user would send it — no placeholders like "
    "[Your Name], no meta-commentary. Set reply_all only if the user asked to "
    "include everyone on the thread.",
    {"type": "object",
     "properties": {
         "message_id": {"type": "string",
                        "description": "Message-ID of the email being replied to, from view_emails"},
         "body": {"type": "string", "description": "the full reply body"},
         "reply_all": {"type": "boolean",
                       "description": "reply to everyone on the thread instead of just the sender"},
         "expected_reply": {"type": "object",
                            "description": "Native reply envelope populated by Wisp before approval; never invent this."},
         "account": {"type": "string",
                     "description": ("optional: the Mail account whose copy of this "
                                     "message to reply from. A message you were cc'd "
                                     "on exists in several accounts under one "
                                     "Message-ID; without this the reply goes from "
                                     "whichever account Mail finds first.")},
     },
     "required": ["message_id", "body"]},
    category="email_send",
    retrieval_description=_OUTBOUND_RETRIEVAL_DESCRIPTIONS["reply_to_email"],
)
async def reply_to_email(message_id: str, body: str, reply_all: bool = False,
                         account: str = "", expected_reply: dict | None = None) -> str:
    message_id = (message_id or "").strip()
    if not message_id:
        return ("(no message_id — call view_emails first and use the Message-ID "
                "it prints for the email you're replying to)")
    # Preparation already rendered these exact words. Never transform an
    # approved escape sequence or line break after the card was accepted.
    if not body.strip():
        return "(nothing to send — the reply body is empty)"
    if _unreviewed() and _looks_truncated(body):
        return ("(NOT sent — the reply looks cut off mid-sentence rather than a "
                "complete message. Rewrite the complete body in "
                "reply_to_email's arguments for the approval preview.)")
    if _unreviewed() and (holes := _unfilled_placeholders(body)):
        return (f"(NOT sent — the reply still has unfilled placeholders: "
                f"{', '.join(holes[:4])}. Rewrite it with the real values.)")
    from service.tasks.reply_contract import envelope_matches, make_receipt, validate_envelope
    expected = validate_envelope(expected_reply)
    if (expected is None or expected["message_id"] != message_id
            or expected["account"] != account or not expected["content"].startswith(body)):
        return "(reply NOT sent — resolve and preview the outgoing reply before approval)"
    res = await app_request("reply_to_email", {
        "message_id": message_id, "body": body, "reply_all": bool(reply_all),
        "account": account, "expected_reply": expected,
    })
    if res.get("ok") is not True:
        return (f"(the reply was not confirmed as sent: {res.get('error') or 'unknown error'}. "
                "Check Mail before trying again; do not retry automatically.)")
    if res.get("accepted") is not True or not envelope_matches(res.get("reply"), expected):
        return "(reply outcome unknown — the bridge did not confirm the approved reply. Check Mail before trying again; do not retry automatically.)"
    return make_receipt(expected)


async def prepare_reply_args(args: dict) -> tuple[dict | None, str]:
    """Inspect a disposable native reply, then freeze the envelope for approval.

    Never trusts an expected_reply supplied by a model. The bridge prepares
    and discards a temporary compose window; this operation cannot send.
    """
    from service.tasks.reply_contract import validate_envelope
    message_id = str(args.get("message_id") or "").strip()
    account = str(args.get("account") or "")
    account_id = str(args.get("account_id") or "")
    body = str(args.get("body") or "")
    if not message_id or not body.strip():
        return None, "I need a specific email and reply text before preparing a reply."
    result = await app_request("prepare_email_reply", {
        "message_id": message_id, "account": account, "account_id": account_id, "body": body,
        "reply_all": bool(args.get("reply_all")),
    })
    envelope = validate_envelope(result.get("reply"))
    if (result.get("ok") is not True or envelope is None
            or envelope["message_id"] != message_id
            or (account and envelope["account"] != account)
            or (account_id and envelope["account_id"] != account_id)
            or not envelope["content"].startswith(body)):
        return None, "I couldn’t verify the outgoing reply in Mail. Nothing was sent. Refresh the email or choose its account and try again."
    return {"message_id": message_id, "account": envelope["account"],
            "body": body, "reply_all": bool(args.get("reply_all")),
            "expected_reply": envelope}, ""


@register(
    "draft_email",
    "Open a NEW email in Mail with the recipient, subject, and body already "
    "filled in — WITHOUT sending it. The user reviews it in Mail and clicks "
    "send themselves. Use this whenever they want to look the message over "
    "first ('draft an email to…', 'write it but don't send', 'let me check it "
    "before it goes'). Prefer this over send_email when there's any doubt about "
    "whether they want it sent immediately. `to` accepts an address or a saved "
    "contact's NAME, same as send_email.",
    {"type": "object",
     "properties": {
         "to": {"type": "string",
                "description": "recipient email address or contact name; comma-separate several"},
         "subject": {"type": "string", "description": "subject line"},
         "body": {"type": "string", "description": "the full message body"},
         "cc": {"type": "string", "description": "optional cc addresses"},
     },
     "required": ["to", "subject", "body"]},
    category="email_draft",
)
async def draft_email(to: str, subject: str, body: str, cc: str = "") -> str:
    # No _own_address_guard here, unlike send_email — deliberately. This is
    # now the tool _SELF_SEND_RE in router.py routes an explicit "send/email
    # ME" request to (send_email is removed from the offered set for that
    # turn, so this is what's left). Blocking the user's own address here
    # would defeat that routing: drafting to yourself is legitimate and safe
    # BY CONSTRUCTION — this opens a real compose window and never sends, so
    # even a wrongly-guessed address is a draft the user reviews before
    # anything goes anywhere, not a silent misfire like send_email's.
    subject, body = _degarble(subject), _degarble(body)
    recipients = _split_recipients(to)
    if not recipients:
        return "(no recipient — ask the user who this should go to)"
    resolved: list[str] = []
    for r in recipients:
        if _EMAIL_RE.match(r):
            resolved.append(r)
            continue
        handle, problem = _resolve_recipient(r, want_email=True)
        if problem:
            return problem
        resolved.append(handle)
    # Placeholders are NOT rejected here, unlike send_email. A draft is exactly
    # where a half-finished line is legitimate — the user is about to read it
    # and can fix anything before it goes. Blocking here would make the safe
    # tool stricter than the irreversible one.
    res = await app_request("draft_email", {
        "to": resolved, "cc": _split_recipients(cc),
        "subject": subject or "", "body": body or "",
    })
    if not res.get("ok"):
        return f"(the draft was NOT created: {res.get('error') or 'unknown error'})"
    return (f"Draft opened in Mail to {', '.join(resolved)} — nothing has been "
            "sent. Tell the user to review it and hit send.")


@register(
    "draft_message",
    "Prepare an editable message draft inside Wisp — WITHOUT sending it. "
    "The user can edit, discard, or send it from the draft card. Use when they want to see the "
    "text first ('draft a text to Mom', \"write it but don't send\"). `to` "
    "accepts a phone number or a saved contact's NAME.",
    {"type": "object",
     "properties": {
         "to": {"type": "string",
                "description": "phone number, iMessage address, or contact name"},
         "text": {"type": "string", "description": "the message to prefill"},
     },
     "required": ["to", "text"]},
    category="messages_draft",
)
async def draft_message(to: str, text: str) -> str:
    # No _own_address_guard here — same reasoning as draft_email above: this
    # is the safe landing spot for an explicit self-send request, and a draft
    # never sends on its own.
    to = (to or "").strip()
    if not to:
        return "(no recipient — ask the user who to text)"
    text = _degarble(text)
    if not text.strip():
        return "(nothing to draft — the message body is empty)"
    display = to
    if not (_EMAIL_RE.match(to) or re.fullmatch(r"[+()\-.\s\d]{7,}", to)):
        handle, problem = _resolve_recipient(to)
        if problem:
            return problem
        display = f"{to} ({handle})"
        to = handle
    # The structured card is emitted by the agent loop after this succeeds.
    # Do not open Messages or type into its active conversation: the draft now
    # remains inside Wisp until the user presses the card's Send button.
    return (f"Message draft prepared in Wisp for {display} — nothing has been "
            "sent. The user can edit or send it from the draft card.")


@register(
    "mark_email_read",
    "Mark a specific email as read (or unread) in Mail. Needs the message's "
    "Message-ID, which view_emails prints for each email. Use when the user "
    "says they've dealt with something, or asks to flag it to come back to.",
    {"type": "object",
     "properties": {
         "message_id": {"type": "string",
                        "description": "Message-ID of the email, from view_emails"},
         "read": {"type": "boolean",
                  "description": "true to mark read (default), false to mark unread"},
     },
     "required": ["message_id"]},
    category="email_triage",
)
async def mark_email_read(message_id: str, read: bool = True) -> str:
    message_id = (message_id or "").strip()
    if not message_id:
        return ("(no message_id — call view_emails first and use the Message-ID "
                "it prints for the email you mean)")
    res = await app_request("mark_email_read",
                            {"message_id": message_id, "read": bool(read)})
    if not res.get("ok"):
        return f"(couldn't update the message: {res.get('error') or 'unknown error'})"
    return f"Marked as {'read' if read else 'unread'}."


@register(
    "archive_email",
    "Move a specific email out of the inbox into the Archive mailbox. Needs the "
    "message's Message-ID, which view_emails prints for each email. This is NOT "
    "a delete — the message stays in Archive and can be found again. Use when "
    "the user asks to clear something out of their inbox.",
    {"type": "object",
     "properties": {
         "message_id": {"type": "string",
                        "description": "Message-ID of the email to archive, from view_emails"},
     },
     "required": ["message_id"]},
    category="email_triage",
)
async def archive_email(message_id: str) -> str:
    message_id = (message_id or "").strip()
    if not message_id:
        return ("(no message_id — call view_emails first and use the Message-ID "
                "it prints for the email you mean)")
    res = await app_request("archive_email", {"message_id": message_id})
    if not res.get("ok"):
        return f"(couldn't archive the message: {res.get('error') or 'unknown error'})"
    return "Moved to Archive."


@register(
    "forward_email",
    "Forward a specific email to someone else, keeping the original content "
    "intact. Needs the Message-ID from view_emails. `to` accepts addresses or "
    "saved contact NAMES (comma-separate several); optional `note` is a short "
    "message prepended above the forwarded content.",
    {"type": "object",
     "properties": {
         "message_id": {"type": "string", "description": "Message-ID of the email to forward, from view_emails"},
         "to": {"type": "string", "description": "recipient(s) — address or contact name, comma-separated"},
         "note": {"type": "string", "description": "optional short note to add above the forwarded content"},
     },
     "required": ["message_id", "to"]},
    category="email_send",
    aliases=["forward this email to Dan", "send this to my accountant",
             "fwd this to the team", "can you forward that email over to mom"],
)
async def forward_email(message_id: str, to: str, note: str = "") -> str:
    message_id = (message_id or "").strip()
    if not message_id:
        return ("(no message_id — call view_emails first and use the Message-ID "
                "it prints for the email you want to forward)")
    recipients = _split_recipients(to)
    if not recipients:
        return "(no recipient — ask the user who this should go to)"
    resolved: list[str] = []
    for r in recipients:
        if _EMAIL_RE.match(r):
            resolved.append(r)
            continue
        handle, problem = _resolve_recipient(r, want_email=True)
        if problem:
            return problem
        resolved.append(handle)
    note = _degarble(note) if note else ""
    res = await app_request("forward_email", {
        "message_id": message_id, "to": resolved, "body": note,
    })
    if not res.get("ok"):
        return f"(the forward was NOT sent: {res.get('error') or 'unknown error'})"
    return f"Forwarded to {', '.join(resolved)}."


@register(
    "flag_email",
    "Flag or unflag a specific email for follow-up (the same flag Mail.app's "
    "UI shows). Needs the Message-ID from view_emails.",
    {"type": "object",
     "properties": {
         "message_id": {"type": "string", "description": "Message-ID of the email, from view_emails"},
         "flagged": {"type": "boolean", "description": "true to flag, false to unflag. Default true."},
     },
     "required": ["message_id"]},
    category="email_triage",
    aliases=["flag this email for follow-up", "mark this important",
             "remove the flag from that email", "flag it for follow-up"],
)
async def flag_email(message_id: str, flagged: bool = True) -> str:
    message_id = (message_id or "").strip()
    if not message_id:
        return ("(no message_id — call view_emails first and use the Message-ID "
                "it prints for the email you mean)")
    res = await app_request("flag_email", {"message_id": message_id, "flagged": bool(flagged)})
    if not res.get("ok"):
        return f"(couldn't update the flag: {res.get('error') or 'unknown error'})"
    return f"{'Flagged' if flagged else 'Unflagged'}."


@register(
    "send_message",
    "Send an iMessage/SMS from the user's Messages app. Use when the user asks "
    "to text/message someone. Put the complete outgoing text in `text` once; "
    "the confirmation card displays it and the recipient for approval. "
    "`to` accepts a phone number, an "
    "iMessage email address, or a saved contact's NAME ('Mom', 'Dan') — a name "
    "is resolved from Contacts automatically. Do NOT use view_messages to hunt "
    "for someone's number: it replaces numbers with contact names before you "
    "see them, so it can never show you one. Use `lookup_contact` if you want "
    "to confirm the number first. Keep it in the user's own voice: short, no "
    "sign-off, no 'sent from my assistant'. If `to` is one of the USER'S OWN "
    "email addresses (iMessage can use those as a handle), this is refused "
    "once by default — if the user's own words just confirmed they genuinely "
    "want to message themselves, call this again with confirmed_self_send=true.",
    {"type": "object",
     "properties": {
         "to": {"type": "string",
                "description": "phone number or iMessage email address"},
         "text": {"type": "string", "description": "the message to send"},
         "confirmed_self_send": {"type": "boolean",
                                 "description": "set true ONLY on a retry, after the user has explicitly confirmed they mean to message themselves"},
     },
     "required": ["to", "text"]},
    category="messages_send",
    retrieval_description=_OUTBOUND_RETRIEVAL_DESCRIPTIONS["send_message"],
)
async def send_message(to: str, text: str, confirmed_self_send: bool = False) -> str:
    if (msg := _own_address_guard(to, "send_message", confirmed_self_send)):
        return msg
    to = (to or "").strip()
    if not to:
        return "(no recipient — ask the user who to text)"
    text = _degarble(text)
    if _unreviewed() and _looks_truncated(text):
        return ("(NOT sent — the draft looks cut off mid-sentence rather than "
                "a complete message. Rewrite the complete text in "
                "send_message's arguments for the approval preview.)")

    # Display name for the result line, kept separate from the handle actually
    # dialed — if `to` resolves from a contact name, the transcript should say
    # "Mom (…1234)", not just the raw number, so a resolution mistake leaves a
    # visible trace instead of a plausible-looking but wrong send.
    display = to
    if not (_EMAIL_RE.match(to) or re.fullmatch(r"[+()\-.\s\d]{7,}", to)):
        # `to` is a NAME. Resolve it here rather than only erroring: the model
        # gets the user's phrasing ("text Mom"), and making the send fail
        # unless it first thinks to call lookup_contact is a failure mode that
        # actually happened. Only a single unambiguous match sends — several
        # "Mom"-ish contacts must come back to the user, never a coin flip.
        handle, problem = _resolve_recipient(to)
        if problem:
            return problem
        display = f"{to} ({handle})"
        to = handle

    if not (text or "").strip():
        return "(nothing to send — the message body is empty)"
    if _unreviewed() and (holes := _unfilled_placeholders(text)):
        return (f"(NOT sent — the message still has unfilled placeholders: "
                f"{', '.join(holes[:4])}. Rewrite it with the real values.)")
    res = await app_request("send_message", {"to": to, "text": text})
    if not res.get("ok"):
        return f"(the message was NOT sent: {res.get('error') or 'unknown error'})"
    return f"Message sent to {display}."


@register(
    "http_request",
    "Make an HTTP request that CHANGES something on a remote service — POST, "
    "PUT, PATCH, or DELETE. Use for webhooks (Slack/Discord incoming webhooks, "
    "IFTTT, home automation), and for APIs the user has given you a URL and "
    "token for. For plain reading, use web_fetch instead — it's a GET and "
    "doesn't interrupt the user for approval. Pass `body` as a JSON string for "
    "JSON APIs. Only include auth headers the user actually gave you; never "
    "invent a token.",
    {"type": "object",
     "properties": {
         "url": {"type": "string", "description": "the https URL to call"},
         "method": {"type": "string",
                    "description": "POST, PUT, PATCH, or DELETE (default POST)"},
         "body": {"type": "string",
                  "description": "request body; a JSON string for JSON APIs"},
         "headers": {"type": "string",
                     "description": "optional headers as a JSON object string"},
     },
     "required": ["url"]},
    category="network_write",
)
async def http_request(url: str, method: str = "POST", body: str = "",
                       headers: str = "") -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return f"(refusing to call {url!r} — only http:// and https:// URLs are allowed)"
    method = (method or "POST").strip().upper()
    if method not in ("POST", "PUT", "PATCH", "DELETE"):
        return (f"({method} isn't a state-changing method. Use web_fetch for "
                "GET requests.)")

    hdrs: dict[str, str] = {}
    if headers:
        try:
            parsed = json.loads(headers)
            if not isinstance(parsed, dict):
                raise ValueError("headers must be a JSON object")
            hdrs = {str(k): str(v) for k, v in parsed.items()}
        except Exception as e:  # noqa: BLE001
            return f"(couldn't parse headers as a JSON object: {e})"

    kwargs: dict = {"headers": hdrs}
    if body:
        # Send a valid JSON body as JSON (so the Content-Type is right without
        # the model having to remember to set it), anything else as raw content.
        try:
            kwargs["json"] = json.loads(body)
        except Exception:  # noqa: BLE001
            kwargs["content"] = body

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
            r = await c.request(method, url, **kwargs)
    except httpx.TimeoutException:
        return f"(timed out calling {url} — the request may or may not have gone through)"
    except Exception as e:  # noqa: BLE001
        return f"(error calling {url}: {type(e).__name__}: {e})"

    text = (r.text or "").strip()
    if len(text) > _MAX_RESPONSE_CHARS:
        text = text[:_MAX_RESPONSE_CHARS] + "…[truncated]"
    status = f"HTTP {r.status_code}"
    if r.status_code >= 400:
        return f"(request failed: {method} {url} returned {status}. Response: {text or '(empty)'})"
    return f"{method} {url} -> {status}. Response: {text or '(empty)'}"
