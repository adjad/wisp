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
_PHONE = re.compile(r"(?<![\d/:-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d/:-])")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Links to map pins and meeting rooms are ordinary in real plans ("meet at the park <pin>").
_SAFE_LINK = re.compile(
    r"https?://(?:maps\.apple\.com|maps\.app\.goo\.gl|(?:www\.)?google\.com/maps|(?:[\w-]+\.)?zoom\.us|"
    r"meet\.google\.com|teams\.microsoft\.com|facetime\.apple\.com)\S*", re.I)


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
    cleaned = _SAFE_LINK.sub(" ", text)
    if _LINK.search(cleaned) or _SHORTENER.search(cleaned) or _PHONE.search(cleaned) or _EMAIL.search(cleaned):
        return "contact_vector"
    if item.source == "messages" and _SHORTCODE.match(re.sub(r"[\s()-]", "", item.sender or "")):
        return "automated"
    if item.source == "mail" and _AUTOMATED_ADDRESS.search(item.conversation or ""):
        return "automated"
    return None
