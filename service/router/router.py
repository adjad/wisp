"""The MoE router — decides which expert handles a request.

Layered for speed: cheap keyword rules catch the obvious cases instantly; an LLM
fallback (the fast model) resolves the ambiguous ones. Output is a role + flags.

Because only some models can drive tools, the router also decides `needs_tools`:
when true the request goes through the agent loop on the tool-capable `agent`
model; otherwise it's a plain completion from the chosen specialist.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from service.config import (
    air_compute_config,
    is_tool_capable,
    role_to_model,
    set_air_compute_status,
)
from service.inference.omlx_client import OMLXClient

# Tool/action detection — sets needs_tools, the ONLY thing that triggers the
# agent loop (the sole path where tools actually run). With the roster collapsed
# to a single tool-capable model (gpt-oss), this is the router's highest-stakes
# call, so it fails SAFE: an unmatched prompt falls through to gpt-oss (which can
# drive tools OR just answer) and NEVER to the tool-less fast model. Two tiers
# avoid both whack-a-mole and absurd false positives:
#   STRONG_ACTION_RE  — verbs that almost always mean "operate the machine".
#   ACTION_VERB_RE + SYSTEM_NOUN_RE — softer verbs (check/read/play/summarize)
#       count as tool intent only when aimed at a concrete on-device target.
STRONG_ACTION_RE = re.compile(
    r"\b(run(?:ning|s)?|execute|open|launch|quit|kill|restart|reboot|"
    r"install|uninstall|reinstall|screenshot|clone|commit|push|remind me|"
    r"empty the trash|shut ?down)\b|"
    r"what'?s on (?:my|the) screen|"
    # unambiguous screen/vision phrasing — must reach the agent (so it can call
    # see_screen) or it silently answers from training data instead of looking.
    r"\b(?:look|glance)\s+at\s+(?:my|the)\s+screen\b|"
    r"\bwhat\s+am\s+i\s+(?:looking|staring)\s+at\b|"
    r"\bdescribe\s+(?:my|this|the)\s+screen\b", re.I)

ACTION_VERB_RE = re.compile(
    r"\b(check|read|show|find|search|list|play|pause|resume|skip|mute|unmute|"
    r"summari[sz]e|create|make|delete|remove|move|rename|copy|download|save|"
    r"organi[sz]e|clean|clear|set|configure|enable|disable|turn|switch|"
    r"remind|schedule|snooze|add|send|email|message|text|call|start|stop|"
    r"take|grab|fetch|get|put|write|edit|update|build|generate|look|view|"
    r"glance)\b", re.I)

SYSTEM_NOUN_RE = re.compile(
    r"\b(file|files|folder|directory|dir|desktop|download|downloads|document|"
    r"documents|screen|screenshots?|apps?|applications?|windows?|tabs?|browser|safari|"
    r"chrome|finder|terminal|spotlight|cal[ae]ndars?|schedules?|events?|meetings?|"
    r"email|e-?mail|inbox|mail|messages?|imessages?|texts?|reminders?|note|"
    r"notes|music|songs?|tracks?|playlists?|spotify|volume|brightness|wi-?fi|"
    r"bluetooth|trash|pdf|photos?|images?|disk|battery|readme|config|"
    r"timer|alarm|"
    r"canvas|assignment|assignments|homework|deadline|class|quiz|exam|"
    r"the (?:script|program|code|repo|repository|project)|my (?:mac|computer|files?))\b|"
    r"(?:^|\s)~?[/\\][\w.\-/\\]+|\.\w{1,5}\b", re.I)

# Calendar/schedule intent — always routes to the agent so the assistant tools
# (get_upcoming / add_reminder / add_calendar_event / cancel_event) run, instead
# of relying on the LLM classifier (which was inconsistent: "what's on my
# calendar" worked but "cancel my lunch" fell through to a tool-less reply).
SCHEDULE_RE = re.compile(
    r"\bremind me\b|\b(?:my|the)\s+(?:cal[ae]ndar|schedule|agenda)\b|"
    # "remember to call the dentist" / "don't forget to pay rent" — phrased as
    # memory, but it's a future action, so it wants add_reminder, not the fact
    # store. MEMORY_RE deliberately declines these (see its comment).
    r"\b(?:remember|don'?t forget)\s+to\s+\w+|"
    r"\b(?:cancel|reschedule|postpone)\b|"
    r"\b(?:appointment|deadline|due date)\b|"
    r"what'?s\s+(?:on|due|coming up|next|happening|scheduled)\b|"
    r"what do i have\b|"
    r"\b(?:add|create|schedule|set up|put)\b.{0,24}\b"
    r"(?:meeting|event|appointment|reminder|cal[ae]ndar)\b|"
    # "when is/are my next meeting", "next event/class/appointment/reminder",
    # "do I have any meetings", "am I free" — schedule questions that don't
    # necessarily say "calendar" at all.
    r"\bwhen\s+(?:is|are|do)\b.{0,30}\b(?:meeting|event|class|appointment|reminder)\b|"
    r"\bnext\s+(?:meeting|event|class|appointment|reminder)\b|"
    r"\b(?:any|do i have (?:any|anything)?)\b.{0,20}"
    r"\b(?:meetings?|events?|appointments?|due|scheduled|planned|going on)\b|"
    r"\bam i free\b", re.I)

# Email/inbox intent.
EMAIL_RE = re.compile(r"\b(?:e-?mails?|inbox|mail)\b", re.I)

# Composing/sending an email, as opposed to reading one. Checked BEFORE the
# light read-only routes, because _light_read's write-guard doesn't catch
# every send phrasing: "email dan@example.com saying I'll be late" contains no
# verb from that guard's list, so it was classified as an inbox LOOKUP and
# handed to gemma's read-only subset — the model then tried to call send_email,
# which wasn't offered on that route, and the user was told the action "isn't
# available right now" instead of the mail being sent.
SEND_EMAIL_RE = re.compile(
    r"\b(?:e-?mail|mail)\s+(?:\S+@\S+|him|her|them|my\s+\w+|[A-Z][a-z]+)|"
    r"\bsend\s+(?:an?\s+)?(?:e-?mail|message)\s+to\b|"
    r"\b(?:reply|respond)\s+to\b[^.?!]{0,40}\b(?:e-?mail|mail)\b|"
    r"\b(?:draft|compose|write)\s+(?:an?\s+)?(?:e-?mail|reply)\b")

# Explicit long-term memory intent -> the remember/recall/forget tools
# (service/tools/memory_tools.py). Narrow on purpose: only phrasings that are
# unambiguously ABOUT the memory itself. Bare "remember" is excluded because
# "remember when we..." and "do you remember the file we edited" are ordinary
# conversation about the current session, not a request to persist anything —
# matching those would write junk facts on every nostalgic phrasing.
MEMORY_SAVE_RE = re.compile(
    # "…that/this/I/my", but NOT "remember TO <verb>" — "remember to buy milk"
    # and "don't forget to call Mom" are reminders, not durable facts, and
    # belong on the calendar route (SCHEDULE_RE picks them up instead).
    r"\b(?:remember|keep in mind|make a note|note) (?:that|this|i|my)\b|"
    r"\bdon'?t forget (?:that|this|i|my)\b|"
    r"\bfrom now on\b", re.I)

MEMORY_QUERY_RE = re.compile(
    r"\b(?:what|anything) do you remember\b|"
    r"\bwhat have you remembered\b|"
    r"\b(?:show|list) (?:me )?(?:your |my |everything in )?memor(?:y|ies)\b", re.I)

MEMORY_FORGET_RE = re.compile(
    r"\bforget (?:that|what i|about)\b|\bstop remembering\b", re.I)

_MEMORY_TOOLS = ["remember", "recall", "forget"]

# Requests with ONE exact, checkable answer that the model must not produce
# from its head. The system prompt already forbids this at length; that turned
# out not to be enough. Verified live: asked for the ISO week number of
# 2026-11-17 (correct: 47), gpt-oss reasoned "Use tool? No tool for date
# arithmetic" and answered 46, then 52 on a retry after the prompt was
# strengthened — confident, different each time, wrong both times.
#
# The failure is specific and worth naming: the model looks for a DEDICATED
# tool, doesn't find one, and treats that as license to compute manually —
# never considering run_shell or create_tool. So this doesn't restrict WHICH
# tool (unlike the memory rules); it only guarantees the first step is a tool
# call, which forecloses answering from memory. Same lesson as the gemma
# memory fix: prompt instructions lose to a model's own judgment, schema-level
# forcing doesn't.
# The user explicitly asking for a tool to be built. Forced rather than left to
# the model's judgment: verified live, "create a tool to do so" twice produced a
# prose refusal ("I can't create a tool that talks to Spotify's API…") with
# create_tool sitting right there in the offered schema. Guessing whether a tool
# is buildable is exactly what the model is bad at — create_tool's own checks
# (native-capability redirect, CANNOT_IMPLEMENT escape hatch, fabrication scan)
# give an accurate, specific reason, and they only run if it's actually called.
# So: always call it, and let the real answer come from the attempt.
CREATE_TOOL_RE = re.compile(
    r"\b(?:create|make|build|write|generate)\s+(?:me\s+)?(?:a|an|your own|the)?\s*"
    r"(?:new\s+)?tool\b|"
    r"\btool\s+(?:for|to)\s+(?:do|that|this)\b|"
    r"\bcreate a tool to do so\b", re.I)

COMPUTE_RE = re.compile(
    # calendar / date arithmetic
    r"\b(?:iso\s*)?week\s*(?:number|of the year)\b|"
    r"\bwhat day of the week\b|\bday of the year\b|"
    r"\b(?:how many|number of)\s+(?:business\s+|work(?:ing)?\s+|calendar\s+)?"
    r"(?:days?|weeks?|months?|years?|hours?|minutes?)\s+(?:until|till|between|from|since|ago)\b|"
    r"\bdays? (?:until|till|between|since)\b|"
    # a leap-year question about a SPECIFIC year is a computation; "what is a
    # leap year" is a definition and must stay ordinary chat.
    r"\bleap year\b(?=.*\b\d{4}\b)|\b\d{4}\b(?=.{0,20}\bleap year\b)|"
    # hashes / encodings
    r"\b(?:sha-?\d*|md5|crc32|checksum|base64|base32|hex(?:adecimal)?|url)\s*"
    r"(?:encode|decode|hash|sum)?\b(?=.*\b(?:of|for|this|string|encode|decode)\b)|"
    r"\b(?:encode|decode|hash)\b.*\b(?:base64|hex|sha|md5|url)\b|"
    # randomness / secrets
    r"\b(?:random|generate a|make a|create a)\s+(?:secure\s+)?"
    r"(?:password|passphrase|token|api[_ ]?key|uuid|guid|secret)\b|"
    r"\broll\s+(?:a\s+)?(?:\d*d\d+|dice|die)\b|\bshuffle\b|"
    r"\bpick (?:a |one )?random\b",
    re.I)

# Sending a text/iMessage. Requires a send verb AND a recipient cue ("to X",
# "him", "Mom") so that plain "what did the message say" can't match, and
# excludes "message" used as a noun about something else ("commit message").
SEND_MESSAGE_RE = re.compile(
    # Deliberately excludes bare "write" and bare "send": "write to the file
    # that says hello" and "send the report to Bob about the numbers" are not
    # texting anyone, and both matched when those verbs were in this list.
    r"\b(?:text|imessage|message|dm)\b[^.?!]{0,40}?"
    r"\b(?:to\s+\w+|him|her|them|mom|dad|everyone|"
    r"[A-Z][a-z]+)\b[^.?!]{0,60}?\b(?:that|saying|telling|and\s+say)\b|"
    r"\b(?:text|imessage|dm)\s+(?:my\s+\w+|him|her|them|[A-Z][a-z]+)\b|"
    r"\b(?:reply|respond|answer)\s+to\s+(?:\w+'?s?\s+)?"
    r"(?:text|message|imessage|him|her|them)\b|"
    r"\bsend\s+(?:a\s+|an\s+)?(?:text|imessage|message)\b")

# ---------------------------------------------------------------------------
# Light read-only "just look and tell me" intents that run on the always-warm
# gemma summarizer instead of waking gpt-oss (see LightReadRoute below). These
# are the messages / calendar / notes lookups the assistant does constantly;
# keeping them off the 12.7GB model is the whole point of the always-resident
# small summarizer. WRITES are excluded (they need gpt-oss's reliability with
# the write tools) via _WRITE_INTENT_RE.
_MESSAGES_INTENT_RE = re.compile(
    r"\b(messages?|imessages?|texts?|texted|dm(?:s|ed)?)\b", re.I)
# "message" is overloaded in plain English — "error message", "commit message",
# "log message", "status message" are all common dev-speak with no SMS/iMessage
# meaning at all (found via "explain this error message" wrongly routing to the
# messages summarizer). Checked wherever _MESSAGES_INTENT_RE is, to bail back
# to the normal rule chain (CODE_RE etc.) instead of forcing a messages read.
_MESSAGE_NONSMS_RE = re.compile(
    r"\b(?:error|commit|log|system|debug|console|status|warning|build|"
    r"compile|exception)\s+messages?\b", re.I)
# Notes and messages have NO write/send tool in the roster, so every notes/
# messages intent is inherently a read — no write-guard needed for them.
_NOTES_INTENT_RE = re.compile(r"\bnotes?\b", re.I)
# Calendar DOES have write tools, so only match read-style questions here;
# add/cancel/remind fall through to SCHEDULE_RE -> gpt-oss.
_CALENDAR_READ_RE = re.compile(
    # calendar-specific predicates are safe bare (due/coming up/happening/…)
    r"what'?s\s+(?:due|coming up|happening|scheduled|planned)\b|"
    # but "what's on/next" must be anchored to a calendar noun, else it eats
    # "what's on my screen", "what's on my clipboard", "what's on TV".
    r"what'?s\s+(?:on|next)\b[^?]{0,24}\b(?:cal[ae]ndar|schedule|agenda|today|"
    r"tomorrow|this week|my (?:day|plate))\b|"
    r"what do i have\b|"
    r"when('?s| is| are)\s+my\s+next\b|"
    r"\bam i free\b|"
    r"\b(?:my|the)\s+(?:cal[ae]ndar|schedule|agenda)\b|"
    r"\bdo i have\b.{0,20}\b(?:meetings?|events?|appointments?|scheduled|planned|due)\b|"
    # "do I have anything/plans today/tomorrow/this week" — same read intent,
    # phrased without naming a specific meeting/event noun.
    r"\bdo i have\b[^?]{0,10}\b(?:anything|plans)\b[^?]{0,20}\b(?:today|tomorrow|"
    r"this week|this weekend|going on|scheduled|planned)\b|"
    # read-style schedule questions that previously fell through to gpt-oss:
    # "any meetings today", "any events this week"
    r"\bany\s+(?:meetings?|events?|appointments?|classes)\b|"
    # "when is my gym session/class/game/practice/meeting" (no 'next' required)
    r"\bwhen('?s| is| are)\s+my\b[^?]{0,24}\b(?:meeting|event|class|appointment|"
    r"session|game|practice|gym|dentist|doctor|call|interview|commitment)\b|"
    # "what time is my meeting/appointment/call" — same idea, "what time" not "when"
    r"\bwhat\s+time\s+is\s+my\b[^?]{0,24}\b(?:meeting|event|class|appointment|"
    r"session|game|practice|call|interview)\b|"
    # "what's my next commitment/obligation" — bare 'next' anchored to a
    # generic-but-unambiguous calendar noun, not just meeting/event/etc.
    r"\bmy\s+next\s+(?:commitment|obligation|thing)\b|"
    # academic due-date phrasing without the "what's" contraction:
    # "what assignments are due", "what homework is due this week"
    r"\bwhat\s+(?:assignments?|homework|readings?|projects?)\s+(?:is|are)\s+due\b",
    re.I)
# Day/time PLANNING intent ("plan my day", "what should I focus on today",
# "organize my week"). A/B-tested gemma vs gpt-oss: gemma's plans are fast
# (~2s vs ~9s), accurate, and directly usable, while gpt-oss was only nicer-
# formatted yet 3-4x slower, routinely truncated at the token cap, and once
# leaked raw chain-of-thought — not a good enough win to justify the swap. So
# planning stays on the always-warm gemma, routed through the light-read path
# WITH get_upcoming + messages so the plan is grounded in the real schedule
# (a cold "plan my day" on a no-tools route would otherwise plan around
# nothing). Deliberately scoped to day/time planning — "plan a trip", "make a
# plan for the startup", "plan a wedding" do NOT match and fall through to the
# generalist. Write verbs (schedule/book/add) are still caught by the light-
# read write-guard and bounce to gpt-oss.
_PLANNING_RE = re.compile(
    r"\b(?:plan|organi[sz]e|map out|lay out|structure|sort out)\b[^.?!]{0,24}"
    r"\b(?:day|morning|afternoon|evening|week|schedule|time|tasks?|todos?)\b|"
    r"\bwhat\s+should\s+i\s+(?:do|focus on|prioriti[sz]e|work on|tackle|get done)\b"
    r"[^.?!]{0,20}\b(?:today|now|this (?:morning|afternoon|evening|week))\b|"
    r"\bhelp me (?:plan|organi[sz]e)\b", re.I)
# Inbound "did I get/hear from <someone>" queries that name no channel ("email"/
# "text") explicitly — "did I get anything from UCSC", "did the professor email
# me", "hear back from the recruiter". They're read intents, but without a
# keyword they fell through to gpt-oss, which then called the gemma-backed
# summary tools and thrashed model swaps (a measured 30-65s stall + tool loop).
# Route them to gemma's email+messages read tools so an ambiguous "from X"
# checks both channels directly, fast, with no swap. Scoped tightly (requires
# "from" or "<x> … me") so it doesn't eat "did I get anything done".
_INBOUND_RE = re.compile(
    r"\bdid i (?:get|receive|hear)\b[^?]{0,30}\bfrom\b|"
    r"\bhear(?:d)? back from\b|"
    r"\bdid\b[^?]{0,25}\b(?:e-?mail|message|text|write to|contact)\s+me\b|"
    r"\bany(?:thing)?\s+(?:new\s+)?(?:e-?mails?|messages?|texts?)\s+from\b", re.I)
# A bare calendar noun, for COMPOUND read requests where "calendar" isn't
# preceded by "my/the" — e.g. "what's on my messages and calendar". Only used
# inside _light_read, AFTER the write-guard has excluded add/cancel/etc., so a
# bare mention here is safely a read. "schedule" is deliberately omitted (it's a
# write verb in _WRITE_INTENT_RE and would muddy the guard); "calendar"/"agenda"
# are unambiguous read nouns and appear in no write phrasing. "reminders?" is
# included too — get_upcoming already surfaces reminders alongside events, but
# no domain regex previously recognized the bare word at all, so "check my
# reminders" fell through to the ambiguous default (gpt-oss) instead of the
# always-warm gemma. Safe to include unconditionally: any WRITE-flavored
# "set/add/create a reminder" is already excluded by the write-guard above
# before this ever gets checked (see _WRITE_INTENT_RE).
_CALENDAR_NOUN_RE = re.compile(r"\b(cal[ae]ndar|agenda|reminders?)\b", re.I)
# Any write/modify verb disqualifies the light read-routes (keeps add/cancel/
# reschedule/remind on gpt-oss where the write tools are reliable). "send" is
# here for email specifically: there's no send tool anywhere in the roster,
# but that's still not a job for gemma's narrow read-only subset — bounce it
# to gpt-oss like every other write-shaped request.
# NOTE: "schedule" is deliberately NOT here. As a bare word it's far more often
# the NOUN ("what's on my schedule", "check my schedule") — a READ — than the
# verb, and including it wrongly bounced those reads off gemma to gpt-oss. Real
# "schedule a meeting" WRITES are still caught downstream by SCHEDULE_RE (which
# requires "schedule … meeting/event/…") and routed to the agent. "reschedule"
# stays because it's unambiguously a write.
_WRITE_INTENT_RE = re.compile(
    r"\b(add|create|set\s?up|put|remind|book|make|cancel|"
    r"reschedule|postpone|delete|remove|move|clear|send)\b|"
    # "set/add/create/make (a) reminder(s)" — bare "remind\b" above only
    # catches "remind me…"; it does NOT catch "reminder" (found via a real
    # incident: "set a reminder to buy milk, and tell me what's on my
    # calendar" matched no write cue at all, so the whole compound request —
    # including the write half — landed on gemma's restricted light-read
    # subset, which cannot create reminders). Anchored to a write VERB
    # immediately before "reminder(s)", not bare "remind\w*", so a genuine
    # read like "check my reminders" / "what are my reminders" (no write verb
    # in front) still correctly stays a light-read.
    r"\b(?:set|add|create|make)\s+(?:a\s+|an\s+|\d+\s+)?reminders?\b", re.I)

# Compose/outbound intent — reply/send/forward/draft an email or message, or
# "email <person>". There's no send tool in the roster, but this is still not a
# job for gemma's read-only subset: bail the light-read so it goes to gpt-oss
# (which handles it / explains the limitation) instead of gemma summarizing the
# inbox for a "reply to…" request.
_COMPOSE_RE = re.compile(
    r"\b(?:send|reply|respond|forward|compose|draft)\b|"
    r"\bemail\s+(?:my\s+|the\s+|to\s+)?(?:professor|prof|teacher|boss|mom|dad|"
    r"friend|colleague|him|her|them|everyone|someone|back)\b|"
    # "text/email/message <anyone> back" — "back" is the reliable tell (an
    # arbitrary-name object, e.g. "email the recruiter back", can't be
    # enumerated the way the fixed-noun list above is), covering both email
    # AND messages compose (messages has no compose tool either, but "text
    # mom back" is still not something gemma's read-only subset can do).
    r"\b(?:text|message|email|write|call)\b[^.?!]{0,24}\bback\b", re.I)

# Interrogative info-seeking phrasings that imply "show me the contents of X" or
# "check for new Y" but contain no recognized action VERB at all — "what's in
# my downloads folder", "any new messages" — so they were silently falling
# through _wants_tools (which requires ACTION_VERB_RE + SYSTEM_NOUN_RE) to the
# ambiguous LLM classifier, which then guessed needs_tools=False for genuinely
# tool-needing requests. Found via a live 20-prompt reliability test: every
# failure had this exact shape (interrogative, no verb, LLM classifier said
# general/no-tools).
INFO_QUERY_RE = re.compile(
    r"\bwhat(?:'s| is)\s+in\b|\bany\s+new\b|\bis\s+there\s+anything\b", re.I)

# A DELIBERATELY NARROWER noun-gate than SYSTEM_NOUN_RE for INFO_QUERY_RE.
# SYSTEM_NOUN_RE's abstract catch-alls ("project", "code", "class") are fine
# paired with an explicit verb (ACTION_VERB_RE), but paired with just a bare
# interrogative they false-positive on ordinary brainstorming — "any new ideas
# for the project" was getting FORCED into tool_choice="required" (via
# expect_tool_first), found nothing sensible to call, and refused outright
# instead of just answering. Concrete data locations only here.
_INFO_QUERY_NOUN_RE = re.compile(
    r"\b(files?|folders?|downloads?|desktop|documents?|inbox|mail|"
    r"messages?|imessages?|texts?|emails?|cal[ae]ndar|schedule|screen|"
    r"reminders?|events?|meetings?|appointments?)\b", re.I)

# Confirming an action the ASSISTANT just offered ("Let me know if you'd like
# me to run it" -> user: "yes please" / "go ahead") is a distinct pattern from
# idle chit-chat, even though the words overlap with TRIVIAL_RE (yes/sure/ok).
# Neither rule_route nor the classifier has conversation memory on its own, so
# without this check "go ahead" looks like nothing actionable and gets routed
# to the tool-less fast model — exactly the "have to be very literal" bug.
# Checked with the last ASSISTANT reply, not the current prompt alone.
CONFIRMATION_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|please|go ahead|do it|do that|"
    r"sounds good|please do|please do (?:it|that|so)|go for it|"
    r"(?:yes,?\s*)?(?:please\s*)?(?:go ahead|do it|proceed))\b", re.I)

OFFER_RE = re.compile(
    r"\b(?:would you like|do you want|should i|shall i|let me know if|"
    r"want me to|let me know|i can (?:go ahead and|also)?\s*(?:run|execute|"
    r"apply|save|create|write|delete|open|install)|"
    r"say the word|just say|ready when you are)\b", re.I)

# A fenced code block in the assistant's last reply is a STRONGER and more
# reliable signal than OFFER_RE: whether or not the model happened to add a
# "want me to run it?" line is non-deterministic (LLM phrasing varies run to
# run — verified empirically: the exact same request sometimes offers, some-
# times doesn't), but if code was just shown, "go ahead"/"do it" overwhelmingly
# means "now write/run that", regardless of exact wording.
CODE_BLOCK_RE = re.compile(r"```")


def confirms_offered_action(text: str, last_assistant: str | None) -> bool:
    """True if `text` looks like a short yes/go-ahead AND the assistant's last
    reply plausibly offered (or produced something implying) an action the
    user could now be confirming."""
    if not last_assistant or len(text.split()) > 6:
        return False
    if not CONFIRMATION_RE.match(text.strip()):
        return False
    return bool(OFFER_RE.search(last_assistant)) or bool(CODE_BLOCK_RE.search(last_assistant))


# Positively trivial — greetings, thanks, acknowledgements. fast/gemma is now
# OPT-IN (this list), not a catch-all length rule; a prompt that fails to match
# falls through to gpt-oss, never silently to the tool-less fast model.
# One optional trailing word is allowed so "hello there" / "thanks bud" still
# count, but a real command ("delete my files" = verb+object) exceeds it and is
# already siphoned to the tool path above regardless.
TRIVIAL_RE = re.compile(
    r"^(hi|hey|hello|yo|sup|howdy|hiya|thanks|thank you|thx|ty|appreciate it|"
    r"ok|okay|k|kk|cool|nice|sweet|perfect|great|awesome|got it|gotcha|noted|"
    r"good (?:morning|afternoon|evening|night)|"
    r"how(?:'?s it going| are you| goes it)|what'?s up|nvm|never ?mind|"
    r"lol|haha|lmao|yes|no|yep|nope|yup|sure|alright|fine|"
    r"sounds good|fair enough)\b(?:\s+\w+){0,2}[\s!.?]*$", re.I)

CODE_RE = re.compile(
    r"\b(bug|refactor|function|traceback|exception|stack ?trace|regex|async|"
    r"compile|syntax|debug|unit ?test|npm|pip|webpack|docker|api endpoint|"
    r"def |class |import |println|console\.log|nullpointer|segfault|"
    r"html|css|website|webpage|web ?site|landing page|script|program|"
    r"python|rust|javascript|typescript|kotlin)\b|"
    r"\.(py|js|ts|tsx|jsx|swift|rs|go|java|cpp|c|rb|php|sh|html|css)\b|"
    r"\bcode\b|\bwrite a (?:script|program|function)", re.I)

# Verbs meaning the model must actually AUTHOR or MODIFY code, as opposed to
# TOOL_RE's broader run/open/delete/kill/etc (operating on something that
# already exists, not writing new code) — used to force delegation to the
# coding specialist for agentic requests that need real code quality, whether
# that's fresh code or a fix/refactor of existing code (see main.py).
CODE_AUTHOR_RE = re.compile(
    r"\b(build|create|make|write|generate|develop|"
    r"fix|refactor|rewrite|debug|edit|update|modify|change)\b", re.I)

# Names a filesystem destination — the signal that separates "write me some
# code" from "write me some code AND put it somewhere". Deliberately
# verb-agnostic: enumerating every save-synonym (save/leave/put/place/drop/
# store/stick/keep...) is a losing game, since there's always another one.
# Naming a location is the much smaller, more reliable tell.
LOCATION_RE = re.compile(
    r"\b(downloads?|desktop|documents?|folder|directory|home directory|"
    r"my (?:mac|computer|files?))\b|"
    r"(?:^|\s)~[/\\]|(?:^|\s)/(?:[\w.-]+/)*[\w.-]+", re.I)

REASON_RE = re.compile(
    r"\b(why|prove|calculate|solve|derive|plan|strateg|compare|analy[sz]e|"
    r"reason|step by step|figure out|trade-?offs?|pros and cons|explain how|"
    r"what would happen if|equation|theorem|probability)\b", re.I)

# Genuinely hard reasoning that's worth paying reasoning_effort=medium for.
# Everything else in REASON_RE goes to the fast generalist instead.
HARD_REASON_RE = re.compile(
    r"\b(prove|proof|derive|derivation|theorem|lemma|rigorous|formal(?:ly)?|"
    r"optimi[sz]e|time complexity|space complexity|big-?o|asymptotic|"
    r"integral|derivative|differential|matrix|eigen|calculus|"
    r"step by step|show that|think (?:hard|carefully|deeply|through)|"
    r"olympiad|aime|imo|combinatoric|number theory|multi-?step|"
    r"prove that|optimal strategy|edge cases)\b", re.I)


@dataclass
class RouteDecision:
    role: str
    model: str
    needs_tools: bool
    needs_vision: bool
    source: str          # "rules" or "llm"
    reason: str
    needs_code_delegation: bool = False  # agentic request that's also creating code
    route_source: str = ""
    route_host: str | None = None
    # Forces tool_choice="required" + retry-and-nudge in the agent loop (see
    # run_agent's `expect_tool_first`) — ONLY when a rule confidently detected
    # tool intent. False for the ambiguous fallback (tools are still AVAILABLE,
    # tool_choice stays "auto") — forcing a tool call on a genuinely uncertain
    # prompt is what caused "any new ideas for the project" to get refused
    # outright instead of answered, when nothing sensible was callable.
    expect_tool_first: bool = False
    # When set, the agent loop is restricted to exactly these tools. Used for
    # the light read-only summary routes (messages/calendar/notes) that run on
    # the always-warm gemma instead of gpt-oss: gemma is reliable on this narrow
    # read-only set (verified) even though it's not trusted with the full
    # machine-operating toolset. Its presence also tells _finalize NOT to force
    # the request onto gpt-oss (see is_tool_capable), and main.py NOT to pin it
    # to a resident gpt-oss — the whole point is to keep gpt-oss asleep.
    tool_subset: list[str] | None = None
    # Names the ONE tool the agent loop must offer on its first step (see
    # run_agent's force_first_tool). Set when a rule identified not just that a
    # tool is needed but exactly which one — restricting the schema to a single
    # function is the only real guarantee a small model actually calls it, since
    # oMLX's tool_choice is a soft nudge (see run_agent's docstring).
    force_first_tool: str | None = None

    def as_dict(self) -> dict:
        return {"role": self.role, "model": self.model, "needs_tools": self.needs_tools,
                "needs_vision": self.needs_vision, "source": self.source, "reason": self.reason,
                "needs_code_delegation": self.needs_code_delegation,
                "route_source": self.route_source or self.source,
                "route_host": self.route_host}


def _route_source(source: str) -> str:
    return "local_router" if source == "llm" else source


def _mk(role: str, *, tools=False, vision=False, source="rules", reason="",
        route_source: str | None = None, route_host: str | None = None,
        expect_tool_first: bool | None = None) -> RouteDecision:
    # Every existing rule-based `tools=True` call site is a confident match —
    # default expect_tool_first to follow `tools` unless a caller overrides it
    # (the ambiguous fallback explicitly passes False).
    force = tools if expect_tool_first is None else expect_tool_first
    return RouteDecision(role, role_to_model(role), tools, vision, source, reason,
                         route_source=route_source or _route_source(source),
                         route_host=route_host, expect_tool_first=force)


def _wants_tools(t: str) -> bool:
    """Fail-safe tool-intent detection (see the regex block above)."""
    if STRONG_ACTION_RE.search(t):
        return True
    if ACTION_VERB_RE.search(t) and SYSTEM_NOUN_RE.search(t):
        return True
    # Interrogative phrasings ("what's in my downloads", "any new messages")
    # imply the same tool intent as a verb-based request but contain no verb
    # for ACTION_VERB_RE to match — gated on the NARROWER concrete-noun set,
    # not full SYSTEM_NOUN_RE (see _INFO_QUERY_NOUN_RE's comment for why).
    return bool(INFO_QUERY_RE.search(t) and _INFO_QUERY_NOUN_RE.search(t))


def _mk_light(subset: list[str], reason: str,
              force: str | None = None) -> RouteDecision:
    """A light read-only route on the always-warm gemma summarizer, restricted
    to `subset`. expect_tool_first=True so the loop guarantees the tool runs.

    `force` narrows the FIRST step to a single tool. Needed where gemma is
    unreliable at picking among even a small set — verified live: asked to
    "remember that I prefer short answers" with remember/recall/forget all
    offered, gemma replied "I have remembered that…" and called nothing, then
    on the retry insisted it had already called the tool. Nothing was saved and
    the user was told it had been, which is the worst possible outcome for a
    memory feature.
    """
    d = _mk("fast", tools=True, reason=reason, expect_tool_first=True,
            route_source="rules")
    d.tool_subset = subset
    d.force_first_tool = force
    return d


# A bare follow-up fragment that only narrows the PREVIOUS query's SCOPE (a
# date/time window) but names no domain of its own: "from yesterday", "what
# about today", "and this morning". On its own it classifies as nothing and
# falls through to the gpt-oss default — the reported bug where, right after
# "what's on my email", typing "from yesterday" jumped to "Direct to OSS"
# instead of continuing the email read on gemma. Handled only when the prior
# turn actually used a light-read tool (see _fragment_continuation).
_FRAGMENT_RE = re.compile(
    r"^(?:and\s+|what\s+about\s+|how\s+about\s+|(?:what|any(?:thing)?)\s+from\s+|"
    r"from\s+|just\s+|only\s+)?"
    r"(?:yesterday|today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|week)|"
    r"last\s+(?:week|night)|earlier(?:\s+today)?|so\s+far(?:\s+today)?|"
    r"this\s+past\s+week|the\s+past\s+(?:few\s+)?days?)\b[\s.?!]*$", re.I)

# Which light-read subset to reuse, keyed on a tool name that appears in the
# previous turn's tool_digest. Mirrors the subsets _light_read builds.
_TOOL_TO_LIGHT_SUBSET = {
    "summarize_emails": ["view_emails", "summarize_emails"],
    "view_emails": ["view_emails", "summarize_emails"],
    "summarize_messages": ["view_messages", "summarize_messages"],
    "view_messages": ["view_messages", "summarize_messages"],
    "get_upcoming": ["get_upcoming"],
    "search_notes": ["search_notes"],
}


def _fragment_continuation(text: str, last_tools: str | None) -> RouteDecision | None:
    """A bare scope fragment ("from yesterday") after a light-read turn ->
    continue the SAME light-read domain on gemma, so the follow-up doesn't get
    dumped onto gpt-oss. `last_tools` is the previous assistant turn's
    tool_digest (comma-joined tool names)."""
    if not last_tools or not _FRAGMENT_RE.match(text.strip()):
        return None
    subset: list[str] = []
    domains: list[str] = []
    for tool, sub in _TOOL_TO_LIGHT_SUBSET.items():
        if tool in last_tools:
            subset += sub
            domains.append(tool.replace("summarize_", "").replace("view_", "")
                           .replace("get_upcoming", "calendar").replace("search_notes", "notes"))
    if not subset:
        return None
    subset = list(dict.fromkeys(subset))
    return _mk_light(subset, f"scope follow-up -> continue previous {'+'.join(dict.fromkeys(domains))} read")


def _light_read(t: str) -> RouteDecision | None:
    """Read-only messages/email/calendar/notes lookups -> always-warm gemma,
    keeping gpt-oss asleep. Writes (add/cancel/remind/…) fall through to
    gpt-oss — email has no write tool at all, but a write-phrased email
    request ("send an email to...") still falls through via _WRITE_INTENT_RE
    so gemma doesn't get asked to do something it has no tool for."""
    if _WRITE_INTENT_RE.search(t):
        return None
    # An email/message COMPOSE ("reply to the email", "email my professor") is a
    # write, not a read — don't let gemma summarize the inbox instead.
    if _COMPOSE_RE.search(t):
        return None
    # An unambiguous machine ACTION ("open/launch/quit Notes", "what's on my
    # screen") must reach the agent, even though it may mention a data noun like
    # "notes" or "screen" that would otherwise trip a read domain below.
    if STRONG_ACTION_RE.search(t):
        return None
    # A code-authoring request ("write a regex for email validation") is coding,
    # not a data read — the incidental "email"/"note" noun must not route it to
    # gemma's inbox/notes summary.
    if CODE_AUTHOR_RE.search(t) and CODE_RE.search(t):
        return None
    # Union ALL named read domains, not just the first match — a COMPOUND
    # request ("what's on my messages and calendar") needs BOTH domains' tools
    # in the advertised subset. Previously this returned on the first hit
    # (messages), so get_upcoming was never offered and the calendar half was
    # answered only if gemma happened to recall the tool from the system prompt
    # — i.e. unreliably ("gemma ignores the calendar"). Handling both on gemma
    # also keeps the request off gpt-oss (no swap), which is the whole point.
    subset: list[str] = []
    domains: list[str] = []
    if _MESSAGES_INTENT_RE.search(t) and not _MESSAGE_NONSMS_RE.search(t):
        subset += ["view_messages", "summarize_messages"]; domains.append("messages")
    if EMAIL_RE.search(t):
        subset += ["view_emails", "summarize_emails"]; domains.append("email")
    if _NOTES_INTENT_RE.search(t):
        subset += ["search_notes"]; domains.append("notes")
    if _CALENDAR_READ_RE.search(t) or _CALENDAR_NOUN_RE.search(t):
        subset += ["get_upcoming"]; domains.append("calendar")
    # Inbound "from <someone>" with no channel named -> check both email + texts
    # on gemma (see _INBOUND_RE). Only adds channels not already matched above.
    if _INBOUND_RE.search(t):
        for tool in ("view_emails", "summarize_emails", "view_messages", "summarize_messages"):
            if tool not in subset:
                subset.append(tool)
        if "inbound" not in domains:
            domains.append("inbound")
    # Day planning -> gemma, grounded in the real schedule + message commitments
    # (see _PLANNING_RE). Contributes the calendar + messages read tools so the
    # plan isn't improvised from nothing.
    if _PLANNING_RE.search(t):
        subset += ["get_upcoming", "summarize_messages"]; domains.append("planning")
    if not subset:
        return None
    subset = list(dict.fromkeys(subset))  # dedupe, preserve order
    return _mk_light(subset, f"{'+'.join(domains)} lookup -> always-warm gemma")
    return None


# Typo tolerance for the words the rule regexes actually key on. Patching
# individual misspellings by hand doesn't scale — e.g. a earlier fix covered
# "calandar" but missed "calender", which is the MORE common real-world typo
# of "calendar" and reproduced the exact same bug (falls through every regex,
# lands on the ambiguous default -> gpt-oss instead of gemma). This generalizes
# the fix: fuzzy-match every word ≥5 chars against the keyword list and correct
# it before any rule regex runs, so new misspellings of these specific words
# don't each need their own patch.
#
# cutoff=0.87 was tuned empirically: catches calender/calandar/shedule/
# remidner/apointment/mesage/meting/assignmemt while producing ZERO false
# positives on tested real words that share letters with these keywords
# (melting, greeting, greetings, settings, meaning, reading, leader, header,
# committee, recommend, texture, textbook, cancel, canvas). A looser cutoff
# (0.8) DID false-positive on "melting"/"greeting" -> "meeting"; a tighter one
# (0.9) missed "calender" itself, the reported case. This only affects the
# router's own matching copy of the text — never what's sent to the model.
#
# The list below includes not just each keyword's base form but the common
# CORRECTLY-SPELLED inflections too (scheduled, calendars, messaged, …) —
# omitting those caused a second-order bug: "scheduled"/"calendars"/etc. are
# real words close enough to "schedule"/"calendar" to get silently REWRITTEN
# by the fuzzy match, which then broke a different rule regex that specifically
# expected the original inflected form (e.g. "what's scheduled for tomorrow"
# needs the literal word "scheduled", not "schedule"). Listing every
# already-correct inflection here makes it an exact match, which short-
# circuits `fix()` before fuzzy-matching ever runs on it.
_TYPO_KEYWORDS = [
    "calendar", "calendars", "schedule", "scheduled", "scheduling", "schedules",
    "agenda", "agendas", "email", "emails", "inbox",
    "messages", "message", "messaged", "texts", "reminder", "reminders", "reminded",
    "meeting", "meetings", "appointment", "appointments",
    "assignment", "assignments", "notes",
]
_TYPO_CUTOFF = 0.87
_WORD_RE = re.compile(r"[A-Za-z]+")

# Explicit alias map for misspellings the fuzzy matcher CAN'T catch at a safe
# cutoff. "calendar" is the pathological case: "calander" (a real reported
# typo) is only 0.75 similar to "calendar" because it has TWO errors at once
# (an 'a' where the 'e' goes AND an 'e' where the 'a' goes), unlike the
# single-error "calender"/"calandar" (0.875) the fuzzy pass does catch. There's
# no fuzzy cutoff that catches a two-error word like "calander" without also
# firing on genuinely different real words (e.g. "melting"->"meeting" is only
# ONE error away), so the tail of a heavily-misspelled word is handled by an
# explicit list instead of by loosening the threshold. Keys are lowercase;
# checked before the fuzzy pass.
_TYPO_ALIASES = {
    "calander": "calendar", "calandar": "calendar", "calender": "calendar",
    "calenndar": "calendar", "calnder": "calendar", "calndar": "calendar",
    "calendr": "calendar", "calenderr": "calendar", "callendar": "calendar",
    "calanders": "calendars", "calenders": "calendars", "calandars": "calendars",
    "shedule": "schedule", "scedule": "schedule", "schedual": "schedule",
    "agena": "agenda", "agendaa": "agenda",
}


def _normalize_typos(text: str) -> str:
    def fix(m: re.Match) -> str:
        w = m.group(0)
        lw = w.lower()
        if lw in _TYPO_KEYWORDS:
            return w
        if lw in _TYPO_ALIASES:
            return _TYPO_ALIASES[lw]
        if len(w) < 5:
            return w
        match = difflib.get_close_matches(lw, _TYPO_KEYWORDS, n=1, cutoff=_TYPO_CUTOFF)
        return match[0] if match else w
    return _WORD_RE.sub(fix, text)


def rule_route(text: str, has_image: bool) -> RouteDecision | None:
    t = _normalize_typos(text.strip())
    if has_image:
        return _mk("vision", vision=True, reason="image attached")
    # Light read-only messages/email/calendar/notes lookups -> the always-
    # resident gemma summarizer (NOT gpt-oss). Checked first so these common,
    # cheap queries never wake the 12.7GB model. Write-phrased requests are
    # excluded (see _light_read) and fall through to the gpt-oss routes below.
    # Sending a text beats reading them, so this is checked BEFORE _light_read:
    # "text Dan" and "dm my brother" both matched the messages READ route
    # otherwise, which would have had gemma summarize Dan's messages back at
    # the user instead of sending anything. When the phrasing really is
    # ambiguous, the agent route is the safe side of the coin — it has the read
    # tools too, whereas the light route can only read.
    if SEND_MESSAGE_RE.search(t):
        return _mk("agent", tools=True, reason="message send intent -> agent (send_message)")
    if SEND_EMAIL_RE.search(t):
        return _mk("agent", tools=True, reason="email send intent -> agent (send_email)")
    if (light := _light_read(t)) is not None:
        return light
    # Explicit memory intent -> the same always-warm gemma, restricted to the
    # three memory tools. Saving a fact is a one-line write with no reasoning
    # in it; waking the 12.7GB model to do it would make "remember that…" one
    # of the slowest things Wisp does, when it should be instant.
    # Each memory verb forces its OWN tool as the only one on the first step,
    # rather than offering all three and hoping gemma picks right (see
    # _mk_light's `force`).
    # "create a tool for X" -> actually call create_tool (see CREATE_TOOL_RE).
    # force_first_tool restricts step 1 to that single function, which is the
    # only thing that reliably stops the model talking itself out of it.
    if CREATE_TOOL_RE.search(t):
        d = _mk("agent", tools=True, expect_tool_first=True,
                reason="explicit request to build a tool -> create_tool")
        d.force_first_tool = "create_tool"
        return d

    # Exact-answer computation -> gpt-oss, but it MUST call a tool first (see
    # COMPUTE_RE). It picks which: run_shell for a one-off, create_tool when
    # the user signals they'll want it again.
    if COMPUTE_RE.search(t):
        return _mk("agent", tools=True, expect_tool_first=True,
                   reason="exact-answer computation -> must run code, not answer from memory")

    if MEMORY_SAVE_RE.search(t):
        return _mk_light(_MEMORY_TOOLS, "memory save intent -> remember", force="remember")
    if MEMORY_FORGET_RE.search(t):
        return _mk_light(_MEMORY_TOOLS, "memory forget intent -> forget", force="forget")
    if MEMORY_QUERY_RE.search(t):
        return _mk_light(_MEMORY_TOOLS, "memory query intent -> recall", force="recall")
    # Calendar/schedule WRITE intent -> agent (assistant tools). Before the
    # generic tool check so "cancel my lunch", "remind me …", "add a meeting"
    # all reliably reach the write tools. (Read-only calendar queries were
    # already siphoned to gemma above.)
    if SCHEDULE_RE.search(t):
        return _mk("agent", tools=True, reason="calendar/schedule intent -> assistant tools")
    # Write-phrased email intent ("send an email to...") that _light_read's
    # write-guard bounced -> agent, which now has a real send_email tool (see
    # service/tools/action_tools.py). Stays on gpt-oss rather than gemma:
    # composing a message the user will actually send, and getting the
    # recipient right, is exactly where the small model shouldn't be.
    # (Read-only email queries were already siphoned to gemma above.)
    if EMAIL_RE.search(t):
        return _mk("agent", tools=True, reason="email write intent -> agent (send_email)")
    # Machine actions -> agent loop on the tool-capable model. Checked before the
    # code/reason rules so "delete the file", "quit safari", "play my music"
    # reliably get TOOLS instead of leaking to a tool-less completion.
    if _wants_tools(t):
        return _mk("agent", tools=True, reason="action verb implies operating the machine")
    # Code authoring/fixing with no machine action -> coding model (now gpt-oss).
    # If it also names a save location, it needs file access -> agent loop.
    if CODE_RE.search(t):
        if LOCATION_RE.search(t):
            return _mk("agent", tools=True,
                       reason="code request also names a save location -> needs file access")
        return _mk("coding", reason="code-related request")
    if HARD_REASON_RE.search(t):
        return _mk("reasoning", reason="hard reasoning -> deliberate effort")
    if REASON_RE.search(t):
        return _mk("general", reason="light reasoning -> fast generalist")
    # Positively trivial ONLY -> fast/gemma. Everything else is ambiguous and
    # falls through to the LLM classifier, which defaults to gpt-oss (general) —
    # never silently to gemma.
    if TRIVIAL_RE.match(t) and len(t.split()) <= 8:
        return _mk("fast", reason="trivial chit-chat")
    return None  # ambiguous -> let the LLM decide (defaults to general/gpt-oss)


_CLASSIFY_SYS = (
    "Classify the user's request into exactly one role. Reply with ONLY a JSON "
    'object: {"role": "<role>", "needs_tools": <bool>}. Roles: '
    '"coding" (write/fix code, no machine actions), '
    '"reasoning" (analysis, planning, math, explanation), '
    '"agent" (do something on the computer: run/open/create/delete/organize files or apps), '
    '"general" (anything else). needs_tools is true only for the agent role. '
    "When unsure, prefer general."
)


async def llm_route(client: OMLXClient, text: str, *, last_assistant: str | None = None,
                    model: str | None = None) -> RouteDecision:
    classify_model = model or role_to_model("router")
    try:
        # The classifier otherwise sees ONLY the current message — a short
        # follow-up like "let's do that" is unclassifiable in a vacuum. One
        # line of prior context is enough for cases too soft for CONFIRMATION_RE
        # to catch outright (route() checks that first, and skips this call
        # entirely for the clear-cut ones).
        user_content = text
        if last_assistant:
            user_content = (f"[Assistant just said: {last_assistant[:300]!r}]\n"
                            f"User: {text}")
        resp = await client.chat(
            classify_model,
            [{"role": "system", "content": _CLASSIFY_SYS},
             {"role": "user", "content": user_content}],
            temperature=0.0, max_tokens=40,
        )
        content = resp["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0)) if m else {}
        role = data.get("role", "general")
        # never downgrade an ambiguous prompt to fast/gemma — fast is opt-in via
        # the positive TRIVIAL_RE rule only, so the tool-less model can't swallow
        # a request the rules already deemed non-trivial.
        if role not in {"coding", "reasoning", "agent", "general"}:
            role = "general"
        # reserve reasoning_effort=medium for genuinely hard prompts; downgrade the rest
        if role == "reasoning" and not HARD_REASON_RE.search(text):
            role = "general"
        tools = bool(data.get("needs_tools")) or role == "agent"
        return _mk(role, tools=tools, source="llm", reason="llm classification")
    except Exception:  # noqa: BLE001 — never let routing crash a request
        return _mk("general", source="llm", reason="fallback after classifier error")


def _host_label(base_url: str) -> str:
    parsed = urlparse(base_url)
    return parsed.hostname or base_url


def _from_remote(data: dict, route_host: str) -> RouteDecision | None:
    role = data.get("role")
    # The Pro only consults the Air for prompts its own rules found ambiguous
    # (i.e. not positively trivial). So an Air "fast" would downgrade such a
    # prompt onto the tool-less gemma — the same fail-unsafe we removed from the
    # local classifier. Map it to general, keeping the Air path consistent.
    if role == "fast":
        role = "general"
    if role not in {"coding", "reasoning", "agent", "vision", "general"}:
        return None
    needs_tools = bool(data.get("needs_tools")) or role == "agent"
    needs_vision = bool(data.get("needs_vision")) or role == "vision"
    return _mk(
        role,
        tools=needs_tools,
        vision=needs_vision,
        source="remote",
        route_source="air_router",
        route_host=route_host,
        reason=str(data.get("reason") or "classified by MacBook Air router"),
    )


async def air_route(text: str, has_image: bool) -> tuple[RouteDecision | None, str | None, str | None]:
    """Try the optional MacBook Air router. Returns (decision, host, error)."""
    cfg = air_compute_config()
    if not cfg["enabled"] or not cfg["capabilities"].get("routing", False):
        return None, None, None
    # Enabled but never pointed at anything. Fall through to local routing
    # silently rather than spending the timeout on a request to "".
    if not cfg["base_url"].strip():
        return None, None, None

    base_url = cfg["base_url"].rstrip("/")
    route_host = _host_label(base_url)
    timeout = max(0.1, cfg["timeout_ms"] / 1000)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as remote:
            resp = await remote.post(
                f"{base_url}/router/classify",
                json={"prompt": text, "has_image": has_image},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — remote routing must be optional
        set_air_compute_status("offline", message=str(exc), latency_ms=None)
        return None, route_host, str(exc)

    latency_ms = int((time.perf_counter() - started) * 1000)
    decision = _from_remote(data, route_host)
    if decision is None:
        set_air_compute_status("offline", message="invalid router response",
                               latency_ms=latency_ms)
        return None, route_host, "invalid router response"

    set_air_compute_status("available", latency_ms=latency_ms)
    return decision, route_host, None


async def classify_local(client: OMLXClient, text: str, *, has_image: bool = False) -> RouteDecision:
    """Classify for a remote caller without recursively using Air compute."""
    decision = rule_route(text, has_image)
    if decision is None:
        decision = await llm_route(client, text)
    return _finalize(decision, text)


def _finalize(decision: RouteDecision, text: str) -> RouteDecision:
    """Apply model/tool invariants after any classifier source picks a role."""
    # Agentic work must run on the tool-capable agent model (gpt-oss). The
    # classifier can flag needs_tools on a non-agent role (e.g. fast=gemma);
    # gemma can't reliably drive the FULL toolset, so force the agent model here.
    # EXCEPTION: a light read route (tool_subset set) deliberately runs gemma on
    # a narrow, verified-reliable read-only toolset — don't force it to gpt-oss,
    # that would defeat keeping the big model asleep.
    if decision.needs_tools and not decision.tool_subset and not is_tool_capable(decision.model):
        decision.role = "agent"
        decision.model = role_to_model("agent")
        decision.reason += " · forced agent model (gpt-oss) for tool use"
    # Agentic request that's ALSO authoring or modifying code (e.g. "build a
    # website and save it to my downloads", or "fix the bug in my script and
    # leave it in downloads") — flag it so the agent loop forces delegation to
    # the coding specialist instead of leaving it to gpt-oss's own (unreliable)
    # judgment about when to call write_code.
    #
    # Only meaningful when the coding role is a DISTINCT model from the agent
    # model. Since Coder-14B was retired (coding == agent == gpt-oss), delegating
    # would just make gpt-oss author code as a redundant extra round-trip before
    # its own step. Gating on the model split keeps the machinery dormant now and
    # self-reactivates if a real coding specialist is reassigned via Settings.
    if (decision.needs_tools and CODE_AUTHOR_RE.search(text) and CODE_RE.search(text)
            and role_to_model("coding") != role_to_model("agent")):
        decision.needs_code_delegation = True
    return decision


async def route(text: str, *, has_image: bool = False,
                last_assistant: str | None = None,
                last_tools: str | None = None) -> RouteDecision:
    # Checked before EVERYTHING else, including rule_route: a bare "yes"/"go
    # ahead" would otherwise match TRIVIAL_RE and get sent to the tool-less
    # fast model, even though it's confirming an action the assistant just
    # offered. This is the fix for having to be unnaturally literal with follow-ups.
    if not has_image and confirms_offered_action(text, last_assistant):
        return _mk("agent", tools=True, reason="confirms an action the assistant just offered")
    # A bare scope fragment ("from yesterday") continuing the previous
    # light-read turn -> stay on that same gemma domain instead of falling to
    # the gpt-oss default. Checked before rule_route since the fragment names
    # no domain of its own and rule_route would find nothing to match.
    if not has_image and (cont := _fragment_continuation(text, last_tools)) is not None:
        return _finalize(cont, text)
    decision = rule_route(text, has_image)
    if decision is None:
        # No separate LLM-classify step for local requests anymore (previously
        # gemma, or gpt-oss itself when resident, or the Air). It was a real,
        # measured reliability problem: a live 20-prompt test found ~15% of
        # genuinely tool-needing requests got silently mis-classified as
        # needs_tools=False by that extra step — before gpt-oss ever got a
        # chance to see the request, so no amount of tool-calling hardening in
        # the agent loop could save it. On top of that it cost either an Air
        # round-trip or a small-model swap, for a latency difference the user
        # found negligible. Ambiguous prompts now go straight to gpt-oss via
        # the agent loop with tools AVAILABLE — tool_choice stays "auto"
        # (expect_tool_first=False), so gpt-oss decides for itself rather than
        # being forced, which avoids the opposite failure mode (forcing a tool
        # call on a genuinely conversational prompt and getting a refusal
        # instead of an answer — see expect_tool_first's docstring).
        decision = _mk("agent", tools=True, expect_tool_first=False, source="default",
                       reason="ambiguous -> gpt-oss decides directly (no classify step)")
    return _finalize(decision, text)
