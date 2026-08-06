"""Outbound actions — the things Wisp DOES, as opposed to reads and reports.

Everything here reaches outside the machine, so all three tools sit in the
always-confirm categories (see policy._ALWAYS_CONFIRM_OUTBOUND): they ask every
single time, they can't be pre-approved with a standing grant, and full-access
mode doesn't skip the prompt. That asymmetry is deliberate — full-access is
about not being nagged while operating your own computer, and speaking in your
name to other people isn't that.

send_email/send_message go through the app bridge (service/assistant/outbox.py)
because Mail and Messages Automation is granted to Wisp.app, not to this Python
process. http_request runs here since outbound HTTP needs no OS grant.
"""
from __future__ import annotations

import json
import re

import httpx

from service.assistant.outbox import request as app_request
from service.tools.registry import register

_MAX_RESPONSE_CHARS = 3000
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

# Unfilled template placeholders — "[Your Name]", "{recipient}", "<company>".
# Observed live: gpt-oss signed a real outgoing email "Best,\n[Your Name]"
# despite the tool description telling it not to. Worth catching in code rather
# than only in a prompt, because the cost is asymmetric — the user's actual
# correspondent receives the mistake, and nothing downstream can undo a sent
# message.
_PLACEHOLDER_RE = re.compile(
    r"\[[^\]\n]{2,40}\]|\{\{?[a-z_ ]{2,40}\}?\}|<[a-z][a-z ._-]{1,30}>", re.I)
# Bracketed text that's ordinary prose rather than a template slot.
_PLACEHOLDER_OK_RE = re.compile(
    r"^\[?\s*(?:sic|\d+|https?://|see |note:|via |source:)", re.I)


def _unfilled_placeholders(text: str) -> list[str]:
    return [m.group(0) for m in _PLACEHOLDER_RE.finditer(text or "")
            if not _PLACEHOLDER_OK_RE.match(m.group(0).strip("[]{}<>").strip())]


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
        return emails[0], ""

    handle = matches[0]["preferred"]
    if not handle:
        return "", f"({matches[0]['name']} has no usable handle saved in Contacts.)"
    return handle, ""


@register(
    "send_email",
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
    "[Your Name], no meta-commentary.",
    {"type": "object",
     "properties": {
         "to": {"type": "string",
                "description": "recipient email address; comma-separate several"},
         "subject": {"type": "string", "description": "subject line"},
         "body": {"type": "string", "description": "the full message body"},
         "cc": {"type": "string", "description": "optional cc addresses"},
     },
     "required": ["to", "subject", "body"]},
    category="email_send",
)
async def send_email(to: str, subject: str, body: str, cc: str = "") -> str:
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
    if (holes := _unfilled_placeholders(f"{subject}\n{body}")):
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
    "send_message",
    "Send an iMessage/SMS from the user's Messages app. Use when the user asks "
    "to text/message someone. ALWAYS show the exact text you're about to send "
    "in your message BEFORE calling this. `to` accepts a phone number, an "
    "iMessage email address, or a saved contact's NAME ('Mom', 'Dan') — a name "
    "is resolved from Contacts automatically. Do NOT use view_messages to hunt "
    "for someone's number: it replaces numbers with contact names before you "
    "see them, so it can never show you one. Use `lookup_contact` if you want "
    "to confirm the number first. Keep it in the user's own voice: short, no "
    "sign-off, no 'sent from my assistant'.",
    {"type": "object",
     "properties": {
         "to": {"type": "string",
                "description": "phone number or iMessage email address"},
         "text": {"type": "string", "description": "the message to send"},
     },
     "required": ["to", "text"]},
    category="messages_send",
)
async def send_message(to: str, text: str) -> str:
    to = (to or "").strip()
    if not to:
        return "(no recipient — ask the user who to text)"

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
    if (holes := _unfilled_placeholders(text)):
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
