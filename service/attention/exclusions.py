"""Promotional, automated and scam messages never alert (user decision P3).

A false alert from one of these is the failure the user named, so this screen runs
BEFORE any time is read, and errs toward excluding. The cost of the opposite
error is bounded: a real invitation caught by a pattern here is still on the
user's screen as a normal message; it just does not interrupt them.

Rules are plain data so they can be audited and extended from the labelled
`promo`/`scam` examples. They are heuristics, not a classifier, and they make no
claim about truth; they only decide "must not interrupt".
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from service.attention.corpus import Item

_LINK = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(?:com|net|org|co|io|xyz|top|info|club|online|site|link|ly)\b", re.I)
_SHORTENER = re.compile(r"\b(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly|is\.gd|cutt\.ly|rb\.gy)/", re.I)

_PROMO = re.compile(
    r"\b(?:reply\s+stop|text\s+stop|stop\s+to\s+(?:opt|unsubscribe|end)|msg\s*&\s*data\s+rates|"
    r"opt[\s-]*out|unsubscribe|\d{1,3}\s*%\s*off|limited[\s-]time|promo\s*code|coupon|"
    r"free\s+shipping|shop\s+now|flash\s+sale|sale\s+ends|exclusive\s+offer|"
    r"use\s+code|bogo|new\s+arrivals|order\s+now|deal\s+of\s+the\s+day|"
    r"digest|newsletter|view\s+in\s+browser)\b", re.I)

_SCAM_PHRASES = re.compile(
    r"\b(?:gift\s*cards?|bitcoin|crypto(?:currency)?|wire\s+transfer|western\s+union|"
    r"act\s+now|final\s+notice|your\s+account\s+(?:has\s+been|is)\s+(?:suspended|locked|compromised|on\s+hold)|"
    r"unusual\s+(?:activity|sign[\s-]?in)|verify\s+your\s+(?:account|identity|address|payment|information)|"
    r"confirm\s+your\s+(?:account|identity|payment|details)|unpaid\s+(?:toll|balance|invoice)|"
    r"toll\s+(?:fee|violation|balance)|package\s+(?:is\s+)?(?:on\s+hold|held|undeliverable|returned)|"
    r"delivery\s+(?:attempt|fee|failed)|you(?:'ve|\s+have)\s+won|claim\s+your\s+(?:prize|reward|refund)|"
    r"irs|social\s+security\s+number|arrest\s+warrant)\b", re.I)

_AUTOMATED_ADDRESS = re.compile(
    r"(?:^|[._-])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|news|promo|promotions?|"
    r"deals?|offers?|marketing|notifications?|alerts?|updates?|mailer|digest|billing|receipts?)(?:[._-]|@)", re.I)
_SHORTCODE = re.compile(r"^\+?\d{4,6}$")
# A phone number or an email address in the text. Plans between friends do not usually carry
# one, and a lure ("call 800-... at 3pm") does. Together with links this closes the cheapest
# way for a stranger in a group chat to put their words in the user's Reminders.
# Ten or more digits, however they are spaced or bracketed, with no "/" or ":" inside (so dates
# and clock times are not numbers): (800)555-0100, 800-5550100, 8005550100, +44 20 7946 0958.
_DIGIT_RUN = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Links to map pins and meeting rooms are ordinary in real plans ("meet at the park <pin>").
# They are judged by their parsed HOST, never by a regex over the whole URL: a lookalike such as
# maps.apple.com.evil.com or maps.apple.com@evil.com must not pass because it starts right.
_SCHEMED = re.compile(r"https?://[^\s<>\"']+", re.I)
_BARE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|edu|gov|us|co|io|ai|app|dev|me|xyz|top|info|"
                   r"club|online|site|link|ly)\b(?:/\S*)?", re.I)
_SAFE_HOSTS = frozenset({"maps.apple.com", "maps.app.goo.gl", "meet.google.com",
                         "teams.microsoft.com", "facetime.apple.com"})
_SAFE_SUFFIXES = ("zoom.us",)                      # the domain itself and any subdomain


def _safe_url(url: str) -> bool:
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
        host = (parts.hostname or "").lower()
        if "@" in parts.netloc or parts.username or parts.password or not host:
            return False
    except ValueError:
        return False
    if host in _SAFE_HOSTS or any(host == s or host.endswith("." + s) for s in _SAFE_SUFFIXES):
        return True
    return host in ("www.google.com", "google.com") and parts.path.lower().startswith("/maps")


def contact_vector(text: str) -> bool:
    """True when the text carries a phone number, an email address or a link that is not a
    plain map pin or meeting room: a way for a stranger to point the user somewhere."""
    if _EMAIL.search(text) or _SHORTENER.search(text):
        return True
    if any(not _safe_url(m.group()) for m in _SCHEMED.finditer(text)):
        return True
    rest = _SCHEMED.sub(" ", text)
    if any(not _safe_url(m.group()) for m in _BARE.finditer(rest)):
        return True
    return any(len(re.sub(r"\D", "", m.group())) >= 10 for m in _DIGIT_RUN.finditer(text))


def screen(item: Item) -> str | None:
    """Why this must not interrupt the user ("promo" | "scam" | "automated"), or None."""
    text = item.text or ""
    if _SCAM_PHRASES.search(text) and (_LINK.search(text) or _SHORTENER.search(text)):
        return "scam"
    if _SHORTENER.search(text) and re.search(r"\b(?:verify|confirm|claim|urgent|suspend|locked|payment)\b", text, re.I):
        return "scam"
    if _SCAM_PHRASES.search(text) and re.search(r"\b(?:urgent|immediately|within\s+\d+\s*(?:hours?|minutes?)|act\s+now)\b", text, re.I):
        return "scam"
    if _PROMO.search(text):
        return "promo"
    if contact_vector(text):
        return "contact_vector"
    if item.source == "messages" and _SHORTCODE.match(re.sub(r"[\s()-]", "", item.sender or "")):
        return "automated"
    if item.source == "mail" and _AUTOMATED_ADDRESS.search(item.conversation or ""):
        return "automated"
    return None
