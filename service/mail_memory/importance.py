"""Which mail suggestions are important enough for the Brief.

The user's rule (2026-10-26): mail memory runs in the background and shows a
suggestion on the Brief "only if it is important". Everything else is still
stored, with the reason it was held back, so the bar can be tuned from counts.

IMPORTANT rules:
- A suggestion is shown only when it is due within SHOW_DAYS (the API's
  window) AND it has one of these reasons:
  - "yours":   the user's own Sent mail promised it.
  - "person":  a real person wrote to the user (Inbox, not bulk, Mail's
               Primary category or none recorded).
  - "money":   a payment, bill, invoice, rent, fee, tuition, refund or an amount.
  - "account": an account, document or contract that expires, renews or has a
               security problem (passport, licence, visa, insurance, lease,
               password, sign-in).
  - "deadline": a dated deadline, appointment, RSVP, interview or exam, from a
               person or from Mail's Transactions category.
- Never shown: suspicious items, sales and offers, and automated mail
  (newsletters, notifications: bulk or Mail's Updates category) whose only
  reason is a deadline. Automated mail is shown only for money or an account.
- Code decides, from the item's verbatim quote and the email's index metadata.
  The model is never asked whether something matters.
"""
from __future__ import annotations

import re

from service.mail_memory.extract import _PROMOTIONAL, PRIMARY, TRANSACTIONS, UPDATES

SHOW_DAYS = 30

_MONEY = re.compile(
    r"(?:[$€£¥₹]\s?\d|\d\s?(?:usd|eur|gbp|dollars?)\b|\b(?:pay(?:ment|able)?|paid|bill(?:ed|ing)?|invoice|"
    r"rent|tuition|fees?|refund|balance|charge[sd]?|deposit|installment|instalment|premium|"
    r"tax(?:es)?|amount due|autopay)\b)", re.I)
_ACCOUNT = re.compile(
    r"\b(?:expir\w*|renew\w*|passport|licen[cs]e|visa|insurance|polic(?:y|ies)|lease|"
    r"contract|warranty|subscription|membership|password|sign[- ]in|log[- ]?in|"
    r"two[- ]factor|2fa|security (?:alert|issue|code)|account (?:locked|suspended|closed|closing))\b", re.I)
_DEADLINE = re.compile(
    r"\b(?:deadline|due|last day|no later than|closes?|closing|cut-?off|rsvp|appointment|"
    r"interview|exam|reservation|booking|check-?in|flight|submit|register|enrol\w*)\b", re.I)


def judge(item: dict, message: dict) -> tuple[bool, str]:
    """(important, why). `why` names the reason shown, or "held:<reason>"."""
    if item.get("suspicious"):
        return False, "held:suspicious"
    text = f"{item.get('what', '')} {item.get('evidence', '')} {item.get('date_phrase', '')}"
    headers = f"{message.get('subject', '')} {message.get('sender', '')}"
    if message.get("box") == "sent":
        return True, "yours"
    category = message.get("category", -1)
    automated = bool(message.get("bulk")) or category in (TRANSACTIONS, UPDATES)
    if _PROMOTIONAL.search(headers) and not _MONEY.search(text):
        return False, "held:promotional"
    if not automated and category in (PRIMARY, -1):
        return True, "person"
    if _MONEY.search(text):
        return True, "money"
    if _ACCOUNT.search(text):
        return True, "account"
    if _DEADLINE.search(text) and item.get("precision") == "day":
        if category == TRANSACTIONS:
            return True, "deadline"
        return False, "held:newsletter_deadline"
    return False, "held:no_signal"
