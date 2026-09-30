"""Does a send/email/text request WRITE prose, or deliver data Wisp must read?

"email Sam asking to move our meeting" is authored prose that merely mentions a
topic. Source words in it (meeting, calendar, schedule, reminders, email)
describe what the message is about, not data to fetch and deliver. Before this
guard every such request was treated as a report: the workflow compiler turned
it into a summary delivery and mailed the user's own calendar, reminders or
inbox to the recipient under the subject "Wisp report", and the router required
the source tools before any send.

Dependency-free on purpose: the workflow compiler and the router both need the
answer and neither may import the other.
"""
from __future__ import annotations

import re

_COMPOSE_INTENT = re.compile(
    r"\b(?:asking|requesting|telling|thanking|inviting|reminding|wondering|informing|"
    r"notifying|confirming|congratulating|apologi[sz]ing|updating\s+(?:him|her|them)|"
    r"letting\s+\w+\s+know|let\s+\w+\s+know|tell\s+(?:him|her|them)|"
    r"ask\s+(?:him|her|them|if|whether|about|for|to)|"
    r"to\s+(?:ask|tell|say|let|thank|remind|invite|confirm|apologi[sz]e|inform|notify))\b",
    re.I)
_OUTBOUND_LEAD = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+)?(?:send|email|e-?mail|text|message|dm|write|drop|shoot)\b\s*",
    re.I)
_MESSAGE_NOUN = re.compile(r"\b(?:an?|the)\s+(?:e-?mail|message|text|note|dm)\b", re.I)
_PAYLOAD_WORD = re.compile(
    r"\b(?:summar\w*|digest|rundown|overview|report|brief|agenda|schedule|calendar|"
    r"meetings?|events?|reminders?|to-?dos?|tasks?|inbox|e-?mails?|messages?|texts?|"
    r"news|notes?|unread|stocks?|weather|forecast|plans?|itinerary|this|that|it|these|those)\b",
    re.I)


def authored_message_intent(text: str) -> bool:
    """True when the request writes new prose rather than delivering read data.

    A compose-intent phrase ("asking", "telling X", "letting her know", "to
    ask ...") must follow the recipient, and nothing BEFORE it may name a
    payload ("my calendar", "a summary", "it"). "send Sam my calendar asking if
    he is free" therefore stays a delivery; "email Sam asking to move our
    meeting" does not.
    """
    match = _COMPOSE_INTENT.search(text or "")
    if not match or len(re.findall(r"\w+", text[match.end():])) < 2:
        return False
    lead = _MESSAGE_NOUN.sub(" ", _OUTBOUND_LEAD.sub("", text[:match.start()], count=1))
    return not _PAYLOAD_WORD.search(lead)
