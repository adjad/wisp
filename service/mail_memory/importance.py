"""Which mail suggestions are important enough for the Brief.

The user's rule (2026-10-26): mail memory runs in the background and shows a
suggestion on the Brief "only if it is important". Everything else is still
stored, with the reason it was held back, so the bar can be tuned from counts.

IMPORTANT rules:
- A suggestion is shown only when it is due within SHOW_DAYS (the API's
  window), is not suspicious, and has one of these reasons:
  - "yours":       the user's own Sent mail promised it.
  - "person":      a real person wrote to the user (Inbox, not bulk, not a
                   no-reply sender, Mail's Primary category or none recorded).
  - "money":       a bill, invoice, payment due, rent, tuition, fee, refund, tax
                   return, or an amount of money.
  - "account":     a named thing that expires, renews or lapses (passport,
                   licence, visa, insurance, lease, card, registration, trial...),
                   or a password or account that is locked, reset or expiring.
  - "appointment": a dated appointment, flight, check-in, reservation, interview,
                   exam, court or jury date, permission slip or overdue item.
  - "deadline":    a plain deadline or due date, from a person or Mail's
                   Transactions category only.
- Never shown: suspicious items, and automated mail (bulk, a no-reply sender, or
  Mail's Transactions or Updates category) that reads as a sale or offer. A plain
  deadline in a newsletter is held back.
- Code decides, from the item's verbatim quote and the email's index metadata.
  The model is never asked whether something matters.
"""
from __future__ import annotations

import re

from service.mail_memory.extract import _PROMOTIONAL, PRIMARY, TRANSACTIONS, UPDATES

SHOW_DAYS = 30

_AMOUNT = r"(?:[$€£¥₹]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars?)\b)"
_MONEY = re.compile(
    _AMOUNT + r"|\b(?:bill(?:ed|ing)?|invoice|payment|autopay|amount due|rent|tuition|fees?|refund|"
    r"tax(?:es)? (?:due|return|bill|payment)|file your (?:tax )?return|pay (?:by|before|your|the)\b)", re.I)
_THINGS = (r"passport|licen[cs]e|visa|insurance|lease|registration|subscription|membership|certificate|"
           r"domain|permit|warranty|trial|card|policy|plan|contract")
_LAPSES = r"expir\w*|renew\w*|lapse\w*|ends?|ending"
_ACCOUNT = re.compile(
    rf"\b(?:{_THINGS})\b[^.\n]{{0,40}}?\b(?:{_LAPSES})\b|\b(?:expir\w*|renew\w*)\b[^.\n]{{0,40}}?\b(?:{_THINGS})\b|"
    r"\b(?:password|sign[- ]in|account)\b[^.\n]{0,30}?\b(?:expir\w*|reset|locked|suspended|compromised|closing)\b",
    re.I)
_APPOINTMENT = re.compile(
    r"\b(?:appointment|flight|check-?in|reservation|booking|interview|exam|court|hearing|jury|summons|"
    r"permission slip|overdue)\b", re.I)
_DEADLINE = re.compile(r"\b(?:deadline|due|last day|no later than|rsvp|cut-?off)\b", re.I)
_NO_REPLY = re.compile(r"\b(?:no-?reply|do-?not-?reply|notifications?|alerts?|mailer|news(?:letter)?)@", re.I)


def judge(item: dict, message: dict) -> tuple[bool, str]:
    """(important, why). `why` names the reason shown, or "held:<reason>"."""
    if item.get("suspicious"):
        return False, "held:suspicious"
    if message.get("box") == "sent":
        return True, "yours"
    text = f"{item.get('what', '')} {item.get('evidence', '')} {item.get('date_phrase', '')}"
    headers = f"{message.get('subject', '')} {message.get('sender', '')}"
    category = message.get("category", -1)
    automated = (bool(message.get("bulk")) or category in (TRANSACTIONS, UPDATES)
                 or _NO_REPLY.search(str(message.get("sender") or "")) is not None)
    if not automated and category in (PRIMARY, -1):
        return True, "person"
    if _PROMOTIONAL.search(headers) or _PROMOTIONAL.search(text):
        return False, "held:promotional"
    if _MONEY.search(text):
        return True, "money"
    if _ACCOUNT.search(text):
        return True, "account"
    if _APPOINTMENT.search(text):
        return True, "appointment"
    if _DEADLINE.search(text):
        return (True, "deadline") if category == TRANSACTIONS else (False, "held:newsletter_deadline")
    return False, "held:no_signal"
