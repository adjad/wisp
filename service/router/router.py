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
from dataclasses import dataclass, field

from service.config import (
    models_config,
    role_to_model,
    is_tool_capable,
)
from service.inference.omlx_client import OMLXClient

# Tool/action detection — sets needs_tools, the ONLY thing that triggers the
# agent loop (the sole path where tools actually run). With the roster collapsed
# to a single tool-capable model (the agent model), this is the router's highest-stakes
# call, so it fails SAFE: an unmatched prompt falls through to the agent model (which can
# drive tools OR just answer) and NEVER to the tool-less fast model. Two tiers
# avoid both whack-a-mole and absurd false positives:
#   STRONG_ACTION_RE  — verbs that almost always mean "operate the machine".
#   ACTION_VERB_RE + SYSTEM_NOUN_RE — softer verbs (check/read/play/summarize)
#       count as tool intent only when aimed at a concrete on-device target.
# NOTE: the screen/vision phrasings ("what's on my screen", "look at my
# screen", "describe my screen", "what am I looking at") used to live here so
# they'd reach see_screen. Wisp has no vision model any more — images are never
# a prompt source and see_screen/describe_image are gone — so matching them
# bought nothing except forcing those prompts onto the undifferentiated
# full-toolset route, the one that measures worst at tool selection. They now
# fall through to the ordinary rule chain like any other sentence.
# "screenshot" stays in the verb list below: TAKING one is still a real machine
# action (run_shell/screencapture), it just isn't something Wisp can then look at.
STRONG_ACTION_RE = re.compile(
    r"\b(run(?:ning|s)?|execute|open|launch|quit|kill|restart|reboot|"
    r"install|uninstall|reinstall|screenshot|clone|commit|push|remind me|"
    r"empty the trash|shut ?down)\b", re.I)

ACTION_VERB_RE = re.compile(
    r"\b(check|read|show|find|search|list|play|pause|resume|skip|mute|unmute|"
    r"summari[sz]e|create|make|delete|remove|move|rename|copy|download|save|"
    r"organi[sz]e|clean|clear|set|configure|enable|disable|turn|switch|"
    r"remind|schedule|snooze|add|send|email|message|text|call|start|stop|"
    r"take|grab|fetch|get|put|write|edit|update|build|generate|look|view|"
    r"glance)\b", re.I)

# `logs` and the plural `folders`/`directories` are here because a filesystem
# chore phrased with them ("move my debug logs into folders by date") matched no
# system noun at all and fell out of the tool routes entirely. Plural `logs`
# only — bare "log" is a verb far more often than a file ("log in", "log my
# workout"), and this set is what decides whether a request gets tools.
SYSTEM_NOUN_RE = re.compile(
    r"\blogs\b|\blog\s?files?\b|"
    r"\b(file|files|folder|folders|directory|directories|dir|desktop|download|downloads|document|"
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
    # Calendar-specific predicates are safe bare.
    r"what'?s\s+(?:due|coming up|happening|scheduled)\b|"
    # "what's on/next" is NOT, and must be anchored to a calendar noun — the
    # same rule _CALENDAR_READ_RE already applies, and for the same reason:
    # unanchored, it eats "what's on my screen", "what's on my clipboard",
    # "what's on TV". This branch was previously unanchored and got away with
    # it only because STRONG_ACTION_RE matched the screen phrasings first and
    # made _domain_subset bail out before any calendar rule ran. Removing the
    # vision path removed that shield and exposed the real bug: "what's on my
    # screen" started routing to a get_upcoming-only calendar lookup.
    r"what'?s\s+(?:on|next)\b[^?]{0,24}\b(?:cal[ae]ndar|schedule|agenda|today|"
    r"tomorrow|this week|my (?:day|plate))\b|"
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
# light read-only routes, because _domain_subset's write handling doesn't catch
# every send phrasing: "email dan@example.com saying I'll be late" contains no
# verb from that guard's list, so it was classified as an inbox LOOKUP and
# handed to the summarizer's read-only subset — the model then tried to call send_email,
# which wasn't offered on that route, and the user was told the action "isn't
# available right now" instead of the mail being sent.
SEND_EMAIL_RE = re.compile(
    # Verb keywords are scoped case-insensitive with (?i:...), not a blanket
    # re.I — sentence-initial "Email myself a summary" was silently falling
    # through to the read-only route because capital "Email" never matched a
    # case-sensitive "e-?mail". [A-Z][a-z]+ stays case-sensitive on purpose
    # (proper-noun heuristic); "myself"/"yourself" were also just missing from
    # the target list, so even lowercase "email myself" never matched either.
    # "me" alongside "myself"/"yourself" — "email me a summary" is a genuine
    # send-to-self request (same shape as "email myself"), not a read. Missing
    # this meant "check my calendar and email me a summary" never got
    # send_email offered at all: EMAIL_RE matched (read tools only), the
    # compound _COMPOSE_RE branches below all explicitly exclude "me" as
    # recipient (correctly, for "tell me"/"give me an update" — those ARE
    # reads), and this regex's own target list had "myself" but not bare "me".
    # Needs its own trailing \b (unlike "him"/"her"/etc, which are inherently
    # boundary-safe): without it, "email median" or "email mentions" would
    # false-positive on "me" as a 2-char prefix match.
    r"\b(?i:e-?mail|mail)\s+(?:\S+@\S+|him|her|them|my\s+\w+|myself|yourself|me\b|[A-Z][a-z]+)|"
    # Bare "message" belongs to SEND_MESSAGE_RE. Treating it as email made an
    # explicit "send a message to Mom" route expose both channels and several
    # mailbox-management tools.
    r"\b(?i:send)\s+(?:an?\s+)?(?i:e-?mail)\s+to\b|"
    r"\b(?i:send)\s+(?:an?\s+)?(?i:message)\s+to\s+\S+@\S+|"
    r"\b(?i:reply|respond)\s+to\b[^.?!]{0,40}\b(?i:e-?mail|mail)\b|"
    r"\b(?i:draft|compose|write)\s+(?:an?\s+)?(?i:e-?mail|reply)\b|"
    # "text/email IT/THAT to me" — an intervening object between the verb and
    # the recipient ("text me" alone doesn't match this shape). Found live:
    # "check my notes for the wifi password and text it to me" never matched
    # the plain verb+recipient pattern above because "it" sits between "text"
    # and "me". Deliberately narrow to a short object list rather than `\w+`
    # to avoid matching arbitrary "email X to me" where X is unrelated noun
    # noise this router shouldn't be guessing about.
    r"\b(?i:e-?mail)\s+(?:it|that|this|them|the\s+\w+)\s+to\s+me\b")

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
    # "what did I tell you about the car" / "did I ever tell you about X" /
    # "what have I told you about Y" — the plainest way anyone asks Wisp to
    # look something up in what they have already told it, and until this
    # branch existed NONE of those phrasings could reach `recall` at all.
    # Verified 2026-08-19: "what did I tell you about the car" matched
    # _COMPOSE_RE's "tell <recipient> about" branch — the recipient being
    # "you", i.e. Wisp itself, now excluded there — and was handed the
    # messages+email read+write subset, with `recall` not in it.
    #
    # Anchored to the assistant as the LISTENER (tell/told/say YOU). That is
    # what makes it a question about the fact store rather than about a
    # conversation with a person: "what did I tell Dan about the car" is a
    # messages question and must not match. "tell you TO <verb>" is excluded
    # for the same reason MEMORY_SAVE_RE excludes "remember to" — a past
    # instruction is not a stored fact.
    r"\bwhat did i (?:tell|say to) you\b(?!\s+to\s+\w)|"
    r"\bwhat did i say about\b|"
    r"\bwhat have i (?:told|said to) you\b|"
    r"\b(?:did|have) i (?:ever\s+)?(?:tell|told) you\s+about\b|"
    r"\b(?:show|list) (?:me )?(?:your |my |everything in )?memor(?:y|ies)\b", re.I)

MEMORY_FORGET_RE = re.compile(
    r"\bforget (?:that|what i|about)\b|\bstop remembering\b", re.I)

_MEMORY_TOOLS = ["remember", "recall", "forget"]

# "What do you know about me/my X", "tell me about myself" -> the same idea
# as MEMORY_QUERY_RE ("what do you remember about X") but broader phrasing,
# so it gets its own narrow subset instead of forcing the ambiguous default.
# Checked AFTER _domain_subset (see route()), so a prompt that also names a
# concrete domain — "what do you know about my calendar" — already matched a
# CALENDAR_NOUN_RE/etc. light-read subset upstream and never reaches this
# rule at all.
#
# Exists because, before this rule, a request like this fell all the way
# through to the generic "ambiguous -> the agent model decides" default with the
# ENTIRE toolset offered — dozens of schemas at once. Verified live on
# LFM2.5-2.6B (now the pinned agent/general model): "What do you know about
# my finances?" got a tool called with {'query': 'investment', 'count': 10}
# (search_notes/recall's shape, borrowed from a neighboring tool under a big
# menu of similarly-themed ones) and summarize_emails called with 'days'
# instead of 'day'. Narrowing to just `recall` removes that ambiguity instead
# of only telling the model harder not to make it.
SELF_QUERY_RE = re.compile(
    r"\bwhat do you know about (?:me\b|myself\b|my\b)|"
    r"\btell me (?:(?:what|everything) you know )?about (?:me|myself)\b", re.I)

# "Who are the people in my life" and its variants — a READ about the user's
# relationships, not a request to save one. Scoped separately from
# SELF_QUERY_RE (which is general self-facts) because this one is specifically
# about PEOPLE, where list_contacts has real data recall alone doesn't.
#
# VERIFIED FAILURE 2026-08-18: this phrasing matched no rule, fell to the
# unscoped 44-tool route, and the model called `recall` twice then — on a pure
# read question — `remember`, WRITING a fact into permanent memory: "Adi's
# family includes his mom (AdiJain888@gmail.com)". That address is the user's
# OWN email (see action_tools._own_address_guard), so the saved fact was not
# just an unwanted write on a read turn, it was wrong. `remember` is
# deliberately absent from this route's toolset — nothing here should be
# writable.
_PEOPLE_QUERY_RE = re.compile(
    r"\bwho\s+(?:are\s+)?the\s+people\s+in\s+my\s+life\b|"
    r"\bwho\s+is\s+in\s+my\s+life\b|"
    r"\bpeople\s+(?:close\s+to|important\s+to)\s+me\b|"
    r"\bwho\s+(?:do\s+i\s+know|are\s+my\s+(?:friends|contacts))\b", re.I)

# Requests with ONE exact, checkable answer that the model must not produce
# from its head. The system prompt already forbids this at length; that turned
# out not to be enough. Verified live: asked for the ISO week number of
# 2026-11-17 (correct: 47), the agent model reasoned "Use tool? No tool for date
# arithmetic" and answered 46, then 52 on a retry after the prompt was
# strengthened — confident, different each time, wrong both times.
#
# The failure is specific and worth naming: the model looks for a DEDICATED
# tool, doesn't find one, and treats that as license to compute manually —
# never considering run_shell or create_tool. So this doesn't restrict WHICH
# tool (unlike the memory rules); it only guarantees the first step is a tool
# call, which forecloses answering from memory. Same lesson as the the summarizer
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
    #
    # Verb keywords are scoped case-insensitive with (?i:...) rather than a
    # blanket re.I on the whole pattern: a sentence-initial "Text mom a
    # summary…" was silently falling through to the read-only route because
    # "Text" (capital T) never matched a case-sensitive "text" — same failure
    # as SEND_EMAIL_RE's "Email myself" below. Blanket re.I was rejected
    # because it would also neuter [A-Z][a-z]+, the proper-noun heuristic that
    # keeps "text the report file" from matching — that must stay case-
    # sensitive so only actually-capitalized names count as a target.
    r"\b(?i:text|imessage|message|dm)\b[^.?!]{0,40}?"
    r"\b(?:to\s+\w+|him|her|them|mom|dad|everyone|"
    r"[A-Z][a-z]+)\b[^.?!]{0,60}?\b(?i:that|saying|telling|and\s+say)\b|"
    # "text mom"/"dm everyone" with no trailing clause — target list mirrors
    # the branch above (was missing "everyone", and the verb list here was
    # missing "message", so "Message Dan about tomorrow" fell through too).
    # "me" added alongside mom/dad/everyone/etc — same self-send gap as
    # SEND_EMAIL_RE above ("text me a summary" never matched anything, so
    # send_message was never offered for a compound "check X and text me").
    # Safe without its own \b: this alternation already has a trailing \b
    # right after the closing paren, so "median"/"meeting" etc. still can't
    # match "me" as a bare prefix.
    r"\b(?i:text|imessage|message|dm)\s+(?:my\s+\w+|him|her|them|mom|dad|everyone|me|[A-Z][a-z]+)\b|"
    r"\b(?i:reply|respond|answer)\s+to\s+(?:\w+'?s?\s+)?"
    r"(?i:text|message|imessage|him|her|them)\b|"
    r"\b(?i:send)\s+(?:a\s+|an\s+)?(?i:text|imessage|message)\b|"
    # "WRITE a message TO my dad", "compose a text to Sarah". Bare "write" is
    # excluded above on purpose ("write to the file that says hello"), but
    # anchoring it to a message NOUN plus "to" removes that ambiguity
    # entirely — there is no non-messaging reading of "write a message to X".
    #
    # VERIFIED FAILURE 2026-08-23 (user's debug export): "write a message to
    # my dad with my upcoming calendar events for the next month" matched
    # NOTHING here — branch 1 needs a trailing that/saying/telling, branch 2
    # needs the target immediately after the verb ("message my dad", not
    # "message TO my dad"). So has_write_intent was False, the route came
    # back "messages+calendar LOOKUP", and send_message/lookup_contact were
    # never offered. The model drafted the whole message in chat and could
    # not send it; the user reasonably read that as "it sent something".
    r"\b(?i:write|compose|draft|shoot|send)\s+(?:a\s+|an\s+)?"
    r"(?i:text|imessage|message|dm|note)\s+to\b|"
    # "write my dad a message" — indirect object before the noun, the other
    # natural word order for the same sentence.
    r"\b(?i:write|compose|draft|shoot|send)\s+"
    r"(?:my\s+\w+|him|her|them|mom|dad|[A-Z][a-z]+)\s+"
    r"(?:a\s+|an\s+)?(?i:text|imessage|message|dm|note)\b|"
    # "text/message IT/THAT to me" — same intervening-object gap as
    # SEND_EMAIL_RE's "email it to me" fix, same live failure ("...and text
    # it to me for my records" never matched anything above).
    r"\b(?i:text|imessage|message|dm)\s+(?:it|that|this|them|the\s+\w+)\s+to\s+me\b")

# ---------------------------------------------------------------------------
# Light read-only "just look and tell me" intents that run on the always-warm
# the summarizer summarizer instead of waking the agent model (see LightReadRoute below). These
# are the messages / calendar / notes lookups the assistant does constantly;
# keeping them off the 12.7GB model is the whole point of the always-resident
# small summarizer. WRITES are excluded (they need the agent model's reliability with
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
# "text" is overloaded exactly the way "message" is above, and was missing the
# same guard: "the text 'wisp'", "plain text", "a text file", "the text of the
# email" carry no SMS meaning at all. Verified live — "use the shell to compute
# the SHA-256 of the text 'wisp'" matched `texts?` and routed to the messages
# summarizer, which is then restricted to view_messages/summarize_messages and
# has no way to do the job; the model can only report that it can't.
#
# Applied by SUBTRACTION rather than as a veto (see _domain_subset): these phrases
# are blanked out of a scratch copy and _MESSAGES_INTENT_RE is re-tested, so a
# genuinely compound "check my messages and read me the text file" still routes
# to messages on the strength of the word that really did mean SMS.
_TEXT_NONSMS_RE = re.compile(
    r"\b(?:the|this|that|a|an|plain|raw|following|above|below|input|output|"
    r"sample|some)\s+text\b|"
    r"\btext\s+(?:file|files|string|strings|field|box|editor|content|body|of)\b", re.I)
# Notes and messages have NO write/send tool in the roster, so every notes/
# messages intent is inherently a read — no write-guard needed for them.
_NOTES_INTENT_RE = re.compile(r"\bnotes?\b", re.I)
# Calendar DOES have write tools, so only match read-style questions here;
# add/cancel/remind fall through to SCHEDULE_RE -> the agent model.
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
    # read-style schedule questions that previously fell through to the agent model:
    # "any meetings today", "any events this week"
    r"\bany\s+(?:meetings?|events?|appointments?|classes)\b|"
    r"\bwhat\s+(?:meetings?|events?|appointments?|classes)\s+(?:are|is)\s+"
    r"(?:scheduled|planned)\b|"
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
# "organize my week"). A/B-tested the summarizer vs the agent model: the summarizer's plans are fast
# (~2s vs ~9s), accurate, and directly usable, while the agent model was only nicer-
# formatted yet 3-4x slower, routinely truncated at the token cap, and once
# leaked raw chain-of-thought — not a good enough win to justify the swap. So
# planning stays on the always-warm the summarizer, routed through the light-read path
# WITH get_upcoming + messages so the plan is grounded in the real schedule
# (a cold "plan my day" on a no-tools route would otherwise plan around
# nothing). Deliberately scoped to day/time planning — "plan a trip", "make a
# plan for the startup", "plan a wedding" do NOT match and fall through to the
# generalist. Write verbs (schedule/book/add) are still caught by the light-
# read write-guard and bounce to the agent model.
_PLANNING_RE = re.compile(
    r"\b(?:plan|organi[sz]e|map out|lay out|structure|sort out)\b[^.?!]{0,24}"
    r"\b(?:day|morning|afternoon|evening|week|schedule|time|tasks?|todos?)\b|"
    r"\bwhat\s+should\s+i\s+(?:do|focus on|prioriti[sz]e|work on|tackle|get done)\b"
    r"[^.?!]{0,20}\b(?:today|now|this (?:morning|afternoon|evening|week))\b|"
    r"\bhelp me (?:plan|organi[sz]e)\b", re.I)

# "what's on my to-do list", "what do I need to do", "my tasks for today".
#
# This is an AGGREGATE question with no home domain, which is exactly what the
# router had no way to express. Reported live: asking for a to-do list searched
# only Notes. Two separate causes, both visible in the routes:
#   - "what's on my to do list" matched the calendar noun and got a subset of
#     exactly ONE tool (get_upcoming), so nothing else could be consulted.
#   - "to do list", "what do I need to do", "my tasks for today" matched
#     nothing at all and fell through to the undifferentiated 44-tool route —
#     the case measured worst at selection (2/3, and it reaches for whatever
#     looks vaguely relevant). Landing on search_notes is that failure exactly.
#
# The user's commitments genuinely live in five places at once: the calendar
# and Reminders (both via get_upcoming), Notes, mail, and texts. So this route
# offers all of them and the prompt tells the model to call them together —
# see loop.SYSTEM's aggregate-question rule.
# Reading or summarizing a FILE on disk — "summarize a PDF", "read this doc",
# "what's in that spreadsheet". Every phrasing of this landed on the unscoped
# route, whose 57 tool schemas are ~9,900 tokens — 62% of a 16,000-token
# window before a single message. Reported live: "Summarize a PDF" spent four
# list_dir calls hunting for the file, and the fourth result pushed the prompt
# past the window; oMLX rejected it with a bare 400 and the turn died.
#
# So this is a context-budget fix as much as a routing one. The job genuinely
# needs about five tools, and five tools is ~900 tokens instead of ~9,900.
#
# PLURALS matter here and were missing: `\bfolder\b` does not match "folders",
# so "organize my files"/"move my logs into folders" fell straight past this
# route to the undifferentiated full toolset — the configuration that measures
# worst at tool selection — even though they are as plainly file jobs as their
# singular forms. Found while adding the reorganize route (2026-08-16).
_DOCUMENT_RE = re.compile(
    r"\b(?:pdfs?|docx?|documents?|spreadsheets?|csvs?|xlsx?|pptx?|slide\s?decks?|"
    r"files?|folders?|director(?:y|ies)|invoices?|receipts?|screenshots?|backups?|"
    # "log" as a VERB ("log me out", "log in") is not a document — only the
    # noun ("logs", "log file") is. Split out from the bare `logs?` the other
    # words share, because "log me out" matched that on "log" alone and routed
    # to the file-tools subset — which has no way to reach power_control.
    r"logs\b|log\s+files?\b|log\b(?!\s+(?:me\s+|you\s+|us\s+)?out\b|\s+in\b))", re.I)

# read_file covers text and PDFs WITH a text layer; list_dir is how an unnamed
# file gets found. run_shell stays for the formats the others can't open.
#
# describe_image used to be here, to cover scans and screenshots. Wisp has no
# vision model any more, so a scanned PDF or an image file genuinely cannot be
# read — read_file says so plainly (see builtin.py) rather than pointing at a
# tool that no longer exists. If that capability is wanted back,
# `unlimited-ocr-mxfp8-mlx` is already installed and is an OCR model rather
# than a chat VLM — much cheaper than re-rostering vision.
#
# THE SAFETY PROPERTY THIS ROUTE EXISTS TO GUARANTEE (2026-08-16): "reorganize
# the wisp debug logs in my downloads folder" once reached a toolset with no
# way to move anything, so the model fell back to run_shell and `rm -rf`'d
# three weeks of logs without ever running a move. See builtin.move_path. The
# fix was giving this domain a move primitive; the invariant that actually
# matters going forward is that `move_path` stays reliably OFFERED whenever
# this route fires, so the model never again has a reason to reach for rm.
#
# RETRIEVAL SINCE 2026-08-21, same reasoning as the apps/media and device
# routes (see _mk_scoped's `subset=None` doc) — this domain had grown to 22
# hand-written tools, well past the reliable menu size, with the SAME
# reachability trap _APPS_MEDIA_TOOLS had: a new tool here needed remembering
# to add to this list by hand, or it stayed registered-but-unreachable (this is
# exactly how find_files went dark for a full diagnosis cycle — see the
# 2026-08-18 note this used to carry).
#
# Verified the safety property survives the switch, not just assumed it does:
# `move_path` scores in retrieval's top-7 for both "reorganize my downloads
# folder" and "move my logs into folders by date" (measured 2026-08-21), and
# `run_shell` is separately PINNED in every retrieval result regardless of
# score (see router/semantic.py's `_PINNED`) — so the escape hatch this
# comment worried about losing is structurally guaranteed present, not just
# likely present.
# (the two _DOCUMENT_RE-matched routes below both pass subset=None to
# _mk_scoped — retrieval fills the tools per the reasoning above)

# Tidying/filing/reorganizing, as opposed to reading a document. Same toolset,
# but the request is about MOVING files rather than opening one — which changes
# what "done" means, so it gets its own route reason and drops the read-oriented
# multi_round/after pair that only makes sense for find-then-read.
_REORGANIZE_RE = re.compile(
    r"\b(?:reorgani[sz]e|organi[sz]e|tidy|clean\s+up|sort|group|rearrange|"
    r"consolidate|file\s+away|move)\b", re.I)

# Does the request name something CONCRETE to act on — a real folder, a real
# path, or a recognisable class of file? Deliberately NARROWER than LOCATION_RE,
# which matches the bare words "folder", "directory" and "my files" and so calls
# "reorganize my files" located when it names nothing at all.
#
# WHY THIS EXISTS (measured 2026-08-18). "reorganize my files" names no target,
# so the model has to invent one. Over 10 runs Ling-3.0-tiny listed the whole
# home directory, then `/Users/`, and answered with a raw directory dump every
# single time — never once a sentence of its own. Agents-A1 sometimes asked
# which folder, which is the correct behaviour, but only sometimes.
#
# An unanchored reorganize is also the most DANGEROUS shape this route has: the
# implied scope is the user's entire home directory, and the job is moving
# files. Asking one short question costs a turn; guessing costs the user their
# layout. Compare _CLARIFY_CHANNEL_HINT, which makes the same trade for a send
# whose channel wasn't named.
_CONCRETE_TARGET_RE = re.compile(
    # a real folder by name
    r"\b(?:downloads?|desktop|documents?|pictures?|photos|movies?|music|"
    r"applications|library|projects?|trash|icloud|dropbox)\b|"
    # an explicit path — `~/…`, `/abs/path`, or a Windows-ish drive letter
    r"(?:^|\s)~[/\\]|(?:^|\s)/(?:[\w.-]+/)*[\w.-]+|"
    # a recognisable class of file, or any bare extension
    r"\b(?:logs?|pdfs?|docx?|xlsx?|pptx?|csvs?|screenshots?|images?|videos?|"
    r"invoices?|receipts?|backups?|spreadsheets?|presentations?|debug)\b|"
    r"\.\w{2,4}\b", re.I)

# ---------------------------------------------------------------------------
# Routes added because they were the biggest populations still landing on the
# undifferentiated full-toolset route.
#
# Measured 2026-08-08: 24 of 55 registered tools were reachable ONLY by falling
# through to the unscoped route — 44% of the registry with no home of its own.
# They cluster cleanly, and each cluster below is one of those clusters. This
# is the same pattern _domain_subset already uses for mail/messages/calendar/
# notes; the clusters simply never got written.
#
# A data noun anywhere in the request disqualifies all three: "open my calendar"
# genuinely could mean open_app OR get_upcoming, and the full toolset letting
# the model choose is better than a device-control subset that has forced the
# answer. Keeps those on exactly the route they take today.
_DATA_NOUN_RE = re.compile(
    r"\b(cal[ae]ndar|agenda|e-?mails?|inbox|mail|messages?|texts?|imessages?|"
    r"notes?|reminders?|events?|meetings?|appointments?)\b", re.I)

# Device state and settings.
_SYSTEM_CONTROL_RE = re.compile(
    r"\b(?:volume|mute|unmute|louder|quieter)\b|"
    r"\bwi-?fi\b|\bbluetooth\b|"
    r"\bbatter(?:y|ies)\b|\bcharging\b|\bcharge\s+(?:level|percent)|"
    r"\block\s+(?:my|the)\s+(?:screen|mac|computer)\b|"
    r"\bclipboard\b|\bwhat\s+did\s+i\s+copy\b|"
    r"\bkeyboard\s+(?:backlight|light|lighting)\b|"
    r"\b(?:internet|network|wi-?fi)\s+speed\b|\bspeed\s?test\b", re.I)


# ---------------------------------------------------------------------------
# ROUTER-DIRECT DISPATCH
#
# When a rule identifies not just WHICH tool but its ARGUMENTS too, the call is
# already fully determined and the model's selection step is re-deriving a
# conclusion this regex has already reached. That step is ~3-3.5s of a ~5s
# one-tool turn. `RouteDecision.direct_calls` carries the resolved calls;
# run_agent executes them through the ordinary decide()/approver/run_tool/audit
# path before its first model call, so the loop opens on what is already a
# narration step.
#
# The quality argument is as strong as the speed one: a tool the router has
# already CALLED cannot be skipped in favour of an answer from the model's own
# memory, which is a verified failure mode on exactly these zero-argument reads.
#
# A route that sets direct_calls must NOT also set expect_tool_first or
# force_first_tool — both make step 0 a forced SELECTION step again, which is
# the step this mechanism exists to skip. _mk_direct enforces that rather than
# leaving it to each call site.

# Each entry is (tool, the phrasings that unambiguously mean it). Zero-argument
# tools only: there is nothing in the request that could change the call.
_DIRECT_DEVICE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("get_battery_status", re.compile(
        r"(?:\bbatter(?:y|ies)\b|\bcharging\b|\bcharge\s+(?:level|percent))"
        # Excludes two real false positives, both WRONG-ACTION bugs rather
        # than just missed offers: "put my mac in battery SAVER" was
        # direct-dispatched to a READ (battery charge %) instead of the WRITE
        # it actually named (toggle_setting low_power_mode); "which apps are
        # EATING my battery" asks about per-app usage, which
        # get_battery_status cannot report at all — list_running_apps is the
        # tool that can. Checked 2026-08-19 via test_alias_reachability.py.
        r"(?!\s*(?:saver|save)\b)(?<!eating\smy\sbattery)", re.I)),
    # Volume and clipboard are INTERROGATIVE-ONLY, unlike _SYSTEM_CONTROL_RE's
    # bare `\bvolume\b`/`\bclipboard\b`. That bare alternation also fires on
    # "turn the volume up" and "copy this to my clipboard" — WRITES, whose
    # arguments the router has not resolved and must never pre-dispatch a read
    # in place of.
    ("get_volume", re.compile(
        r"\b(?:what|what'?s|how)\b[^?]{0,24}\bvolume\b|"
        r"\bvolume\b[^?]{0,16}\b(?:at|level|set\s+to)\b|"
        r"\b(?:is|am\s+i|are\s+we)\b[^?]{0,16}\bmuted\b", re.I)),
    ("clipboard_read", re.compile(
        r"\bwhat(?:'?s| is| did\s+i)\b[^?]{0,24}\b(?:clipboard|copied|copy)\b|"
        r"\bclipboard\s+(?:contents?|say)\b|"
        r"\bread\b[^?]{0,16}\bclipboard\b", re.I)),
    ("lock_screen", re.compile(
        r"\block\s+(?:my|the)\s+(?:screen|mac|computer)\b"
        # "lock my screen AFTER 5 minutes" / "…AUTOMATICALLY" name a TIMEOUT
        # POLICY (set_screen_lock_timeout), not an immediate lock — without
        # this exclusion both were direct-dispatched to lock_screen, which
        # locks the screen RIGHT NOW. That's not a missed offer, it's the
        # wrong action actually running. Checked 2026-08-19.
        r"(?!\s+(?:after|in|automatically|whenever))", re.I)),
    ("set_keyboard_backlight", re.compile(
        r"\bkeyboard\s+(?:backlight|light|lighting)\b", re.I)),
    ("run_speed_test", re.compile(
        r"\b(?:internet|network|wi-?fi)\s+speed\b|\bspeed\s?test\b", re.I)),
]

# Belt-and-braces alongside the interrogative-only patterns above: any phrasing
# that is plainly SETTING one of these disqualifies the whole request from
# direct dispatch, whatever else matched.
_DEVICE_WRITE_RE = re.compile(
    r"\b(?:set|change|adjust|raise|lower|increase|decrease|turn|crank|bump)\b"
    r"[^.?!]{0,24}\b(?:volume|sound|audio|backlight|brightness|louder|quieter)\b|"
    r"\b(?:mute|unmute)\b|"
    r"\b(?:copy|write|put|save)\b[^.?!]{0,24}\bclipboard\b", re.I)

_CONDITIONAL_RE = re.compile(
    r"\b(?:if|unless|otherwise|only\s+if|after\s+confirming)\b", re.I)


def _direct_device_call(t: str) -> list[tuple[str, dict]]:
    """The one zero-argument device tool this request unambiguously names.

    Empty unless EXACTLY ONE pattern matched. "what's my battery and lock my
    screen" names two, and once a direct call has run step 0 becomes a
    thinking-off narration step — so pre-dispatching only the first would
    silently drop the second. Ambiguity falls back to the ordinary scoped
    route, which still answers correctly, just one model call slower.
    """
    if _DEVICE_WRITE_RE.search(t) or _CONDITIONAL_RE.search(t):
        return []
    hits = _matched_device_tools(t)
    return [(hits[0], {})] if len(hits) == 1 else []


def _matched_device_tools(t: str) -> list[str]:
    if _DEVICE_WRITE_RE.search(t) or _CONDITIONAL_RE.search(t):
        return []
    return [name for name, rx in _DIRECT_DEVICE_PATTERNS if rx.search(t)]


# Forward-looking window for a calendar-only read, resolved in PYTHON.
# tools/timeranges.py states the rule this follows: Wisp resolves dates, the
# model does not — it is "reliably wrong" at date arithmetic, and a wrong
# boundary is invisible in the output.
_CALENDAR_WINDOW_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening))\b", re.I), 1),
    (re.compile(r"\btomorrow\b", re.I), 2),
    (re.compile(r"\bthis\s+weekend\b", re.I), 7),
    (re.compile(r"\bthis\s+week\b", re.I), 7),
    (re.compile(r"\b(?:next|this\s+coming)\s+week\b", re.I), 14),
    (re.compile(r"\b(?:this|next)\s+month\b", re.I), 30),
]


def _calendar_window_days(t: str) -> int:
    """Forward window in days for a calendar-only read. An unqualified read
    uses the tool's widest supported horizon instead of silently meaning seven
    days.

    Takes the WIDEST matching window when a request names more than one
    ("today and tomorrow"). A wider window is a superset, and get_upcoming tags
    every row relative to today, so the narration can still separate them. The
    opposite error — too narrow — silently drops events the user asked for.
    """
    hits = [d for rx, d in _CALENDAR_WINDOW_PATTERNS if rx.search(t)]
    for match in re.finditer(r"\bnext\s+(\d+)\s+(days?|weeks?)\b", t, re.I):
        hits.append(min(60, max(1, int(match[1]) * (7 if match[2].lower().startswith('week') else 1))))
    return max(hits) if hits else 60


# "summarize my inbox" and nothing else. Anchored start-to-end on purpose:
# summarize_emails/summarize_messages take period/day/count/unread/account, so
# ANY qualifier ("summarize my unread email", "summarize my texts from mom
# yesterday") is an argument the router has NOT resolved — it must fail this
# match and fall through to the ordinary route, where the model extracts it.
_DIRECT_SUMMARY_RE = re.compile(
    r"^(?:can\s+you\s+|could\s+you\s+|please\s+)?"
    r"(?:give\s+me\s+a\s+|do\s+a\s+)?"
    r"(?:summari[sz]e|summary\s+of|recap\s+of|recap)\s+"
    r"(?:my\s+|the\s+)?"
    r"(?P<domain>inbox|e-?mails?|mail|messages?|texts?|imessages?)"
    r"[\s.!?]*$", re.I)

# “Send me my email summaries” uses conversational “send me” to mean “show
# me”, with Wisp itself as the destination. It names no recipient or delivery
# channel and must stay a local read. The anchored scope prevents this from
# swallowing real sends such as “send my email summary to Mom”.
_INLINE_EMAIL_SUMMARY_RE = re.compile(
    r"^(?:can\s+you\s+|could\s+you\s+|please\s+)?(?:"
    r"(?:send|show|give|tell)\s+me\s+(?:my\s+|the\s+)?"
    r"(?:e-?mail|inbox)\s+(?:summary|summaries|digest|recap)|"
    r"what\s+are\s+(?:my\s+|the\s+)?(?:e-?mail|inbox)\s+"
    r"(?:summary|summaries|digest|recap))"
    r"(?:\s+(?:for|from)\s+(?P<scope>today|yesterday|"
    r"(?:this|last|past)\s+(?:day|week|month|year)|"
    r"(?:last|past)\s+\d+\s+(?:days?|weeks?|months?)|"
    r"\d{4}-\d{2}(?:-\d{2})?))?[\s.!?]*$", re.I)


def _inline_email_summary_args(text: str) -> dict | None:
    match = _INLINE_EMAIL_SUMMARY_RE.match(text.strip())
    if not match:
        return None
    scope = (match.group("scope") or "").lower()
    if scope in {"today", "yesterday"}:
        return {"day": scope}
    if scope:
        return {"period": scope}
    return {}

# A specific inbox lookup is a search, not an inbox digest. This exact shape
# was misrouted to summarize_emails, which received no PlayStation query and
# confidently summarized unrelated UCSC mail instead.
_DIRECT_EMAIL_SEARCH_RE = re.compile(
    r"^(?:can\s+you\s+|could\s+you\s+|please\s+)?"
    r"(?:check|search|look\s+(?:in|through)|find)\s+"
    r"(?:my\s+|the\s+)?(?:e-?mails?|mail|inbox)\s+"
    r"(?:for|about)\s+(?P<query>.+?)[\s.!?]*$", re.I)


def _email_search_query(text: str) -> str | None:
    match = _DIRECT_EMAIL_SEARCH_RE.match(text.strip())
    if not match:
        return None
    query = match.group("query").strip(" .!?")
    vendor = re.fullmatch(
        r"(?:purchases?|orders?|receipts?|transactions?|charges?)\s+from\s+(.+)",
        query, re.I)
    return (vendor.group(1) if vendor else query).strip()

# "How far back can you check my email" — a question about WISP'S OWN REACH,
# not the user's data. Direct-dispatched to search_coverage.py (see its
# module docstring for why this is a tool call rather than a prompt
# instruction): asked to just answer directly from what it already knows, the
# model called summarize_emails and dumped an inbox digest — TWICE, on two
# separate live replays after the prompt was tightened in between. A range
# question needs a range answer regardless of what the model does with it, so
# this makes the answer deterministic instead of re-tuning the prose a third
# time.
#
# Domain is OPTIONAL and unanchored at the end (unlike _DIRECT_SUMMARY_RE) —
# "how far back can you search" with no domain named is exactly the shape that
# should report on everything, which search_coverage(source="") already does.
_SEARCH_COVERAGE_RE = re.compile(
    r"\bhow\s+far\s+back\s+(?:can|do|does)\s+(?:you|your|wisp)\b|"
    r"\bhow\s+far\s+back\s+(?:wisp|you)\s+can\s+"
    r"(?:check|search|see|read|reach|find|look|go\s+back)\b|"
    r"\bhow\s+(?:many|much)\b.*\b(?:can|do|does)\s+(?:you|wisp)\s+"
    r"(?:check|search|see|read|reach|find|look|go\s+back)\b|"
    r"\b(?:what'?s|what\s+is)\s+(?:the\s+)?(?:oldest|search\s+)?range\b", re.I)
# (source key for search_coverage.py, pattern) — first match wins, so email is
# ordered before the generic \bhistory\b in case a phrasing names both.
_SEARCH_COVERAGE_DOMAINS = [
    ("email", re.compile(r"\be-?mails?\b|\binbox\b", re.I)),
    ("messages", re.compile(r"\b(?:messages?|texts?|imessage|sms)\b", re.I)),
    ("calendar", re.compile(r"\bcalendar|reminders?|schedule\b", re.I)),
    ("notes", re.compile(r"\bnotes?\b", re.I)),
    ("browser", re.compile(r"\bbrows(?:er|ing)|history\b", re.I)),
]

_LOW_POWER_CONDITIONAL_RE = re.compile(
    r"\bif\b[^.?!]{0,80}\bbattery\b[^.?!]{0,40}\bbelow\s+(\d{1,3})\s*%"
    r"[^.?!]{0,100}\b(?:low\s+power\s+mode|battery\s+saver)\b", re.I)

# Launching/quitting apps and controlling playback.
_APPS_MEDIA_RE = re.compile(
    # "open Safari", "quit spotify", "launch the app". Deliberately accepts a
    # LOWERCASE target as well as the capitalized proper-noun form used
    # elsewhere in this file — people type "open safari" at least as often as
    # "open Safari", and requiring the capital sent exactly those to the
    # undifferentiated route. Enumerating app names instead was rejected for
    # the reason LOCATION_RE already documents: there is always another one.
    # Requests naming a FILE are excluded by the _DOCUMENT_RE guard at the call
    # site, so "open the pdf" still reaches the document tools.
    r"\b(?:open|launch|start|quit|close|kill)\s+(?:the\s+|my\s+)?[\w.]+|"
    r"\bspotify\b|\bapple\s+music\b|"
    r"\b(?:play|pause|resume|skip)\b[^.?!]{0,20}\b(?:music|song|track|playlist|album)\b|"
    r"\b(?:play|pause|resume)\s+(?:some\s+)?music\b|"
    r"\bnext\s+(?:song|track)\b|\bprevious\s+(?:song|track)\b", re.I)
# window_control / switch_app / list_running_apps joined 2026-08-19: this
# route claims "close this window" and "switch to Safari", so those requests
# never reach the semantic fallback, and a hand-written subset frozen at
# open/quit/music/spotify meant the window tools were registered and
# unreachable — no error, just the old behavior. That kept happening as the
# domain grew (force_quit_app, manage_spaces, play_podcast/audiobook/ambient/
# radio/streaming, set_timer, stopwatch, place_call all landed here piecemeal
# the same way) until the list reached 18 tools — past the reliable menu size
# (see _mk_scoped's docstring) and still one edit away from staling out again
# on the next addition.
#
# RETRIEVAL SINCE 2026-08-21 (measured: 5/5 -> 5/5 and 5/5 -> 5/5 unchanged,
# 2/5 -> 4/5 and 3/5 -> 5/5 improved, on the two prompts where the 18-tool menu
# was actually causing wrong picks — see _mk_scoped's `subset=None` doc). The
# regex below still claims the request; only the static list is gone, so a
# tool registered in this domain tomorrow is reachable the moment it exists
# rather than needing a fourth hand-edit here.

# Live external facts. The system prompt is emphatic that these must come from
# web_fetch rather than from memory, so giving them a route is also what makes
# that instruction enforceable rather than advisory.
_LING_WEB_MODEL = "Ling-3.0-tiny-oQ4e"

# Current public events are web lookups even when the user never says "news"
# or "search". The missing forms here were observed falling through to
# semantic retrieval, which offered run_shell but not web_search; the selected
# model then invented a NewsAPI key and retried the failing shell request.
# Keep these shapes narrow and question-like so historical/explanatory prompts
# continue through the ordinary informational routes.
_CURRENT_PUBLIC_EVENT_RE = re.compile(
    r"\bwhat\s+(?:happened|has\s+happened|is\s+happening)\s+(?:in|with|to)\b"
    r"[^?]{0,100}\b(?:today|yesterday|recently|this\s+week|right\s+now)\b|"
    r"\bwhat(?:'s|\s+is)\s+(?:the\s+)?(?:latest|current)\s+situation\s+"
    r"(?:in|with|regarding)\b|"
    r"\bwhat(?:'s|\s+is)\s+(?:the\s+)?situation\s+(?:in|with|regarding)\b"
    r"[^?]{0,100}\b(?:right\s+now|currently|today|now)\b|"
    r"\bwhat(?:'s|\s+is)\s+(?:the\s+)?(?:latest|current)\s+status\s+of\s+"
    r"(?:the\s+)?(?:conflict|war|crisis|ceasefire|negotiations?|election|protests?)\b|"
    r"\bwhat(?:'s|\s+is)\s+(?:the\s+)?status\s+of\s+(?:the\s+)?"
    r"(?:conflict|war|crisis|ceasefire|negotiations?|election|protests?)\b"
    r"[^?]{0,100}\b(?:right\s+now|currently|today|now)\b|"
    r"\bwhat(?:'s|\s+is)\s+(?:currently\s+)?happening\s+(?:in|with)\b|"
    r"\bwhat(?:'s|\s+is)\s+going\s+on\s+(?:in|with)\b|"
    r"\btell\s+me\s+(?:about\s+)?(?:the\s+)?(?:latest|current)\s+situation\s+"
    r"(?:in|with|regarding)\b|"
    r"\b(?:give|show)\s+me\s+(?:(?:a|an)\s+)?(?:brief|briefing|update)\s+on\s+"
    r"(?:the\s+)?(?:current\s+)?situation\s+(?:in|with|regarding)\b|"
    r"\bbrief\s+me\s+on\s+(?:the\s+)?(?:current\s+)?situation\s+"
    r"(?:in|with|regarding)\b|"
    r"\bupdate\s+me\s+on\s+(?:the\s+)?(?:current\s+)?situation\s+"
    r"(?:in|with|regarding)\b|"
    r"\bwhat(?:'s|\s+is)\s+(?:the\s+)?latest\s+on\b|"
    r"\bhow\s+are\s+things\s+developing\s+(?:in|with|regarding)\b"
    r"[^?]{0,100}\b(?:right\s+now|currently|today|now)\b|"
    r"\bwhat\s+changed\s+(?:today|recently|this\s+week)\s+(?:in|with|regarding)\b|"
    r"\bare\s+there\s+(?:any\s+)?(?:new|latest|recent)\s+developments?\s+"
    r"(?:in|with|on|regarding)\b|"
    r"\b(?:new|latest|recent|current)\s+developments?\s+"
    r"(?:in|with|on|regarding)\b",
    re.I)

_HISTORICAL_EVENT_RE = re.compile(
    r"\b(?:in|during|throughout)\s+(?:the\s+)?(?:\d{4}s?|"
    r"\d{1,2}(?:st|nd|rd|th)\s+century)\b|"
    r"\b(?:historically|history\s+of|at\s+the\s+time|back\s+then)\b",
    re.I)
_CURRENT_TIME_CUE_RE = re.compile(
    r"\b(?:latest|current|currently|today|right\s+now|now|recent(?:ly)?)\b",
    re.I)
_NARRATIVE_CONTEXT_RE = re.compile(
    r"\b(?:plot|story|storyline|scene|chapter)\s+of\b|"
    r"\b(?:in|within)\s+(?:the\s+|this\s+|that\s+)?(?:plot|story|storyline|"
    r"novel|book|movie|film|episode|chapter|scene)\b"
    r"(?!\s+(?:industry|business|market|sector|bans?|publishing|sales))|"
    r"\bcharacter\s+(?:in|from)\b|"
    r"\bfiction(?:al)?\b",
    re.I)
_LOCAL_CURRENT_CONTEXT_RE = re.compile(
    r"\b(?:my|our|your|their|his|her|team|shared|private|internal|personal|"
    r"this|that|these|those|[\w-]+'s|[\w-]+s')\s+"
    r"(?:[\w.-]+\s+)*?(?:file|files|"
    r"folder|folders|downloads?|desktop|document|documents|pdf|spreadsheet|"
    r"presentations?|code(?!\s+interpreter\b)|functions?|class|scripts?|repos?|repositories|repository|projects?|apps?|"
    r"applications?|screen|computer|mac|calendars?|agenda|e-?mail|inboxes|inbox|mail|"
    r"messages?|texts?|imessages?|notes?|reminders?|events?|meetings?|"
    r"appointments?|volume|wi-?fi|bluetooth|battery|clipboard)\b|"
    r"(?:^|\s)~?[/\\][\w.\-/\\]+",
    re.I)

# An explicit opt-out must win over every positive recency/search cue. This is
# also consumed by _apply_execution_contract, so a later route cannot restore
# a web tool that the user prohibited.
_NO_WEB_SEARCH_RE = re.compile(
    r"\b(?:do\s+not|don'?t|never)\s+(?:"
    r"(?:use|access|check|consult)\s+(?:the\s+)?(?:web|internet|online\s+sources?|"
    r"external\s+sources?)|"
    r"(?:do|perform|run|try|substitute)\s+(?:a\s+|the\s+)?web\s+search|"
    r"search\s+(?:the\s+)?(?:web|internet|online)|"
    r"browse(?:\s+(?:the\s+)?(?:web|internet|online))?|"
    r"go\s+online|"
    r"look\s+(?:it|this|that)?\s*up(?:\s+online)?"
    r")\b|"
    r"\bwithout\s+(?:using|accessing|checking|consulting|searching|browsing|"
    r"looking\s+(?:it|this|that)?\s*up|going\s+online)?\s*(?:(?:a|any|the)\s+)?"
    r"(?:web(?:\s+search)?|internet|online(?:\s+sources?)?|external\s+sources?)\b|"
    r"\bwithout\s+(?:external\s+sources?|browsing|searching|going\s+online)\b|"
    r"\b(?:avoid|refrain\s+from)\s+(?:"
    r"browsing(?:\s+(?:the\s+)?(?:web|internet|online))?|"
    r"going\s+online|"
    r"searching\s+(?:the\s+)?(?:web|internet|online)|"
    r"(?:using|accessing|checking|consulting)\s+(?:the\s+)?"
    r"(?:web|internet|online(?:\s+sources?)?|external\s+sources?)|"
    r"(?:the\s+)?(?:web|internet|online(?:\s+sources?)?|external\s+sources?)"
    r")\b|"
    r"(?:^|[.!?;,]\s*|\bbut\s+)\s*(?:please\s+)?no\s+"
    r"(?:browsing|web(?:\s+search|\s+browsing)?|internet|online(?:\s+sources?)?|"
    r"external\s+sources?|live\s+search)\b|"
    r"\bwith\s+no\s+(?:browsing|web\s+search|online\s+lookup)\b|"
    r"\b(?:cancel|skip|stop|abort)\s+(?:(?:the|that|this|any)\s+)?"
    r"(?:web\s+search|online\s+(?:search|lookup)|browsing|searching\s+the\s+web)\b|"
    r"(?:^|[.!?;,]\s*)\s*(?:please\s+)?(?:cancel|skip|stop|abort)\s+"
    r"(?:the|that|this)\s+(?:search|lookup)\b|"
    r"(?:^|[.!?;,]\s*)\s*(?:please\s+)?offline(?:\s+only)?\b|"
    r"\b(?:answer|respond|stay|remain)\s+offline\b|"
    r"\b(?:answer|respond)\s+(?:from|using)\s+(?:memory|existing\s+knowledge|"
    r"your\s+knowledge)\s+only\b|"
    r"\b(?:use|rely\s+on)\s+(?:only\s+)?(?:your\s+|my\s+)?"
    r"(?:existing|current|prior)\s+(?:knowledge|memory)(?:\s+only)?\b|"
    r"\b(?:your\s+|my\s+)?(?:existing|current|prior)\s+"
    r"(?:knowledge|memory)\s+only\b|"
    r"\buse\s+only\s+(?:what\s+)?(?:you\s+)?(?:already\s+)?know\b|"
    r"\bonly\s+(?:use\s+)?what\s+(?:you|i)\s+(?:already\s+)?know\b",
    re.I)

_EXPLICIT_WEB_SEARCH_RE = re.compile(
    r"\b(?:search|research|browse)\s+(?:the\s+)?(?:web|internet|online)\b|"
    r"\b(?:search|research|look\s+up|find)\b[^.?!]{0,200}"
    r"\b(?:on|using|across)\s+(?:the\s+)?(?:web|internet|online)\b|"
    r"\b(?:search|research|look\s+up|find|check)\b[^.!?\n]{0,200}\bonline\b|"
    r"\b(?:google|bing)\s+(?:for\s+)?\S",
    re.I)

_WEB_RE = re.compile(
    r"\bweather\b|\bforecast\b|\btemperature\b[^.?!]{0,20}\b(?:in|at|outside|today)\b|"
    r"\b(?:search|research|look\s+up|find)\b[^.?!]{0,35}\b(?:web|online|internet|sources?)\b|"
    # `price` not `prices?` was missing the plural form — "check the stock
    # prices for my stocks" (2026-08-18) fell through to the ambiguous 16-tool
    # route instead of the 2-3 tool web route, entirely because of the missing
    # 's'. \b after a bare "price" never matches "prices" (no boundary between
    # two word characters), so the singular-only pattern silently excluded the
    # most natural way to ask about more than one stock.
    r"\b(?:stock|share)s?\s+prices?\b|\bprices?\s+of\b|\bcrypto\b|\bbitcoin\b|\bethereum\b|"
    r"\b(?:latest|today'?s|breaking)\s+news\b|\bheadlines\b|"
    r"\bwho\s+won\b|\bfinal\s+score\b|"
    r"https?://|\bwww\.\w", re.I)
_WEB_TOOLS = ["get_stock_price", "get_weather", "web_search", "web_fetch", "http_request",
             "weather_alerts", "air_quality", "rain_radar"]

# What a request that matched NO rule at all can still reach.
#
# The ambiguous fallback used to offer the entire registry. Real traffic says it
# is almost never a novel machine action — from the debug exports, every prompt
# that landed here was a conversational follow-up ("send it", "what?", "anything
# from earlier today?", "go find it"). What those need is the user's own data,
# their remembered facts, and an escape hatch; not 55 schemas.
#
# `run_shell` IS the escape hatch and is deliberately included: it is the one
# tool that can reach anything the rest of this list can't, and it is
# confirm-gated, so the user sees it before it runs. `remember` is here because
# the system prompt requires saving facts the user reveals in passing, which can
# happen in any conversation. Genuinely destructive or specialised tools
# (delete_path, send_email, create_tool, …) are NOT here — every one of them has
# its own rule, and a prompt that matched no rule has not asked for them.
_CORE_TOOLS = ["get_upcoming", "search_notes", "summarize_emails", "summarize_messages",
               "recall", "remember",
               "lookup_contact", "list_contacts", "search_browser_history",
               "get_stock_price", "get_weather", "web_search", "web_fetch", "use_skill", "run_shell"]

# "What's Mom's number?", "how many contacts do I have", "look up Dan's contact".
#
# Two bugs, one rule. Scoping the ambiguous fallback stranded `lookup_contact`:
# a plain contact question matches no domain, so it landed on the core set,
# which didn't have it — measured, "What's Mom's phone number?" then spent 41s
# flailing through list_contacts -> search_notes -> search_notes -> show_profile
# instead of one direct call. And PRE-EXISTING: "what is dan's email address"
# matches EMAIL_RE on the word "email" and was handed the inbox READ tools,
# which cannot answer it either.
#
# Checked before the domain union so the email/messages nouns inside these
# phrasings don't claim them, but skipped when the request is actually a SEND
# ("text mom's number to dan"), where the domain's write tools are the point.
_CONTACT_LOOKUP_RE = re.compile(
    r"\b\w+'s\s+(?:phone\s*)?(?:number|cell|mobile)\b|"
    r"\b\w+'s\s+e-?mail\s*address\b|"
    r"\b(?:phone\s*number|e-?mail\s*address)\s+(?:for|of)\s+\w+|"
    r"\bcontact\s+(?:info|information|details)\b|"
    r"\bhow\s+many\s+contacts\b|"
    r"\b(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?contacts\b|"
    r"\blook\s*up\s+(?:\w+'s\s+)?contact\b", re.I)
_CONTACT_TOOLS = ["lookup_contact", "list_contacts"]


def _core_tools() -> list[str]:
    """_CORE_TOOLS plus every tool contributed by an installed skill.

    Skill tools have to be added at ROUTE TIME, not baked into the constant:
    the user installs and removes skills at runtime, and — importantly — a
    skill's tool is directly callable. `use_skill` only returns the skill's
    written instructions; a skill that declares a tool (see
    skills/tools.register_skill_tools, category="skill_tool") is invoked by
    calling that tool by name.

    So a static core silently broke skill invocation: "count the vowels in
    banana" matches no rule, lands here, and `count_vowels` would not be on the
    table — the loop's allowed_names check would reject the model's perfectly
    correct call and tell the user the action isn't available. Derived from the
    registry by category rather than from a private list in the skills module,
    so it stays right without a second place to update.
    """
    from service.tools.registry import REGISTRY

    return [*_CORE_TOOLS,
            *(n for n, t in REGISTRY.items() if t.category == "skill_tool")]


def has_write_intent(text: str) -> bool:
    """Whether `text` asks to CHANGE something rather than just read it.

    Pulled out of `_domain_subset` so the semantic fallback and the eval harness
    test write intent the same way the domain routes do, instead of each growing
    its own copy that drifts. The composition is load-bearing and its parts were
    each added for a measured failure — see `_WRITE_INTENT_RE` (add/cancel/remind
    verbs), `_COMPOSE_RE` ("let Dad know" with no send verb in it at all), and
    the two send regexes.

    Deliberately NOT `SCHEDULE_RE`: that one spans reads and writes, so folding
    it in here marks "what's on my calendar" as a write and arms `cancel_event`
    on a pure lookup. `_CALENDAR_WRITE_RE` is the narrow, object-anchored slice
    of it that IS unambiguously a write, and is included for that reason.
    """
    return bool(_WRITE_INTENT_RE.search(text) or _COMPOSE_RE.search(text)
                or _CALENDAR_WRITE_RE.search(text)
                or SEND_EMAIL_RE.search(text) or SEND_MESSAGE_RE.search(text))


async def _semantic_core(text: str) -> list[str]:
    """The tool subset for a request that matched no rule — retrieved from the
    whole registry by the configured lexical/embedding/reranker provider, with
    `_core_tools()` as the exception/empty-result safety net. The packaged
    default is lexical; an inactive provider's failure does not trigger it.

    Write intent comes from `has_write_intent` — the same test the domain routes
    use — rather than a second copy living in the retrieval layer. That matters
    for the reason [[moe-tool-subset-routing]] records: a subset built from a
    read-only reading of a request that was actually a send silently loses the
    send tool, and the model then reasons correctly toward an action it has no
    way to perform.
    """
    writing = has_write_intent(text)
    try:
        provider = str((models_config().get("tool_retrieval") or {}).get(
            "provider", "embedding")).lower()
        if provider == "reranker":
            from service.router import reranker
            names = await reranker.candidates(text, writing=writing)
        elif provider == "lexical":
            from service.router import reranker
            names = reranker.lexical_candidates(text, writing=writing)
        else:
            from service.router import semantic
            names = await semantic.candidates(text, writing=writing)
    except Exception:  # noqa: BLE001 — retrieval must never break the turn
        return _core_tools()
    if not names:
        return _core_tools()
    # Skill tools are NOT force-appended here, unlike in _core_tools().
    #
    # That append exists because a STATIC list structurally cannot reach a tool
    # registered at runtime. Retrieval has no such problem — skill tools are in
    # REGISTRY, so they are in the index, and `is_stale()` picks up ones
    # installed mid-session. Carrying the append over would have re-created the
    # precise failure the 2026-08-18 diagnosis named: `ascii_art_generator`,
    # `business_days_between`, `count_vowels` and `human_shape` sitting in every
    # ambiguous route, "noise competing for the model's attention against tools
    # that actually matter" — except now in EVERY retrieved menu rather than one.
    #
    # A skill that embeds poorly is reachable anyway: `use_skill` and `run_shell`
    # both survive, and a skill can carry its own aliases like any other tool.
    return names


async def _compound_route(text: str) -> RouteDecision | None:
    """Route each explicit action independently, then merge a compact menu.

    Whole-request retrieval lets the most verbose clause dominate and silently
    drops the other actions. Merging every domain route has the opposite
    failure: five clauses can expose 50-80 tools, including unrelated writes.
    This keeps each clause's structural contract and only its strongest few
    candidates. Router-direct calls intentionally become obligations instead
    of being pre-executed: later clauses may depend on earlier results, and the
    agent loop is where ordering and ordinary permission checks are enforced.
    """
    from service.router.reranker import _action_clauses, lexical_rank
    from service.tools.registry import REGISTRY

    mutating_categories = frozenset({
        "assistant_write", "calendar_write", "email_draft", "email_send",
        "email_triage", "fs_delete", "fs_write", "messages_draft",
        "messages_send", "messages_write", "network_active", "network_write",
        "notes_write", "scheduled_send", "shell", "system_write",
        "timer_write", "tool_authoring",
    })
    broad_action = re.compile(
        r"\b(?:add|append|archive|cancel|clear|complete|copy|create|delete|"
        r"draft|edit|encrypt|forget|forward|install|log|move|pause|play|"
        r"remove|rename|run|save|schedule|send|set|start|stop|store|toggle|"
        r"uninstall|update|write)\b", re.I)

    clauses = _action_clauses(text)
    if len(clauses) < 2:
        return None

    clause_decisions: list[tuple[str, RouteDecision, bool]] = []
    confident_actions = 0
    for clause in clauses:
        decision = rule_route(clause)
        matched_rule = decision is not None
        if decision is not None and decision.needs_tools:
            confident_actions += 1
        elif decision is not None:
            # A conversational or coding-only sentence is not an action merely
            # because it appeared beside one in a multi-sentence prompt.
            continue
        else:
            decision = _mk(
                "agent", tools=True, expect_tool_first=True, source="compound",
                reason="explicit task-list clause -> retrieved tools")
        if decision.tool_subset is None:
            decision.tool_subset = await _semantic_core(clause)
        clause_decisions.append((clause, _finalize(decision, clause), matched_rule))

    # Avoid turning ordinary multi-sentence prose into a forced tool workflow.
    # Real task lists have several independently recognizable actions even when
    # one or two clauses use novel skill vocabulary.
    explicit_list = bool(
        re.search(r"\b(?:please\s+do\s+these\s+in\s+this\s+order|"
                  r"for\s+these\s+tasks|i\s+have\s+a\s+few\s+things)\b|"
                  r";\s*(?:then|after\s+that|next)\b", text, re.I))
    minimum_confident = 0 if explicit_list or len(clauses) >= 4 else 2
    if confident_actions < minimum_confident or len(clause_decisions) < 2:
        return None

    merged_tools: list[str] = []
    groups: list[frozenset[str]] = []
    conditionals: list[tuple[str, str, str, object]] = []
    clarify_channel = False
    clarify_target = False

    def add_tool(name: str) -> None:
        if name in REGISTRY and name not in merged_tools:
            merged_tools.append(name)

    for clause, decision, matched_rule in clause_decisions:
        forbidden = set(decision.forbidden_tools)
        reply_analysis = bool(re.search(r"\bwho\s+(?:may|might|could)\s+need\s+a\s+reply\b",
                                        clause, re.I))
        clause_writes = ((has_write_intent(clause) and not reply_analysis)
                         or bool(broad_action.search(clause)))

        def permitted(name: str) -> bool:
            tool = REGISTRY.get(name)
            return bool(tool and (clause_writes or tool.category not in mutating_categories))

        ranked = [name for name in lexical_rank(clause)
                  if name in REGISTRY and name not in forbidden and permitted(name)]

        # Powerful escape hatches belong in a clause menu only when that clause
        # names them; lexical neighborhood alone must never arm them.
        explicit_shell = bool(re.search(r"\b(?:shell|terminal|command|script)\b", clause, re.I))
        explicit_applescript = bool(re.search(r"\bapple\s*script\b", clause, re.I))
        explicit_create = bool(CREATE_TOOL_RE.search(clause))
        ranked = [name for name in ranked
                  if (name != "run_shell" or explicit_shell)
                  and (name != "run_applescript" or explicit_applescript)
                  and (name != "create_tool" or explicit_create)]

        exact_groups = [frozenset(n for n in group if n not in forbidden)
                        for group in decision.required_tool_groups]
        exact_groups = [group for group in exact_groups if group]
        exact_names = [name for name, _ in decision.direct_calls
                       if name not in forbidden]
        if decision.force_first_tool and decision.force_first_tool not in forbidden:
            exact_names.append(decision.force_first_tool)

        pool = [name for name in (decision.tool_subset or [])
                if name in REGISTRY and name not in forbidden and permitted(name)]
        if exact_groups or exact_names:
            candidates = list(dict.fromkeys(
                exact_names + [name for group in exact_groups for name in group]))
        else:
            # Preserve a naturally tiny route. For a broad domain route, keep
            # its two best lexical members and the global top three.
            route_ranked = [name for name in ranked if name in pool]
            candidates = (pool if len(pool) <= 5 else route_ranked[:5])
            # An unresolved clause needs global lexical recovery. A rule-scoped
            # clause does not: adding tools outside its domain is how a pure
            # Messages read acquired send_message in the first implementation.
            if not matched_rule:
                candidates = list(dict.fromkeys(candidates + ranked[:3]))

        if not candidates:
            continue
        for name in candidates:
            add_tool(name)

        if exact_groups:
            groups.extend(exact_groups)
        for name in dict.fromkeys(exact_names):
            if not any(name in group for group in exact_groups):
                groups.append(frozenset({name}))
        if not exact_groups and not exact_names:
            groups.append(frozenset(candidates))

        conditionals.extend(decision.conditional_tools)
        clarify_channel = clarify_channel or decision.clarify_channel
        clarify_target = clarify_target or decision.clarify_target

    if len(groups) < 2 or not merged_tools:
        return None

    merged = _mk_scoped(
        merged_tools,
        f"explicit compound request -> {len(clause_decisions)} clause routes, "
        f"{len(merged_tools)} tools",
        expect=True, light=False, multi=True,
        clarify_channel=clarify_channel, clarify_target=clarify_target,
    )
    merged.required_tool_groups = tuple(groups)
    merged.conditional_tools = tuple(dict.fromkeys(conditionals))
    return _finalize(merged, text)


_TODO_RE = re.compile(
    r"\bto-?\s?do\s*list\b|\bto-?dos?\b|"
    r"\bwhat\s+(?:do|have)\s+i\s+(?:need\s+to|have\s+to|gotta|got\s+to)\b|"
    r"\bwhat\s+(?:do\s+i|i)\s+need\s+to\s+(?:do|get\s+done|take\s+care\s+of)\b|"
    r"\b(?:my|any)\s+tasks?\b|"
    r"\bwhat'?s\s+(?:left|pending|outstanding|on\s+my\s+plate)\b|"
    r"\banything\s+i\s+(?:need\s+to|have\s+to|should)\b|"
    r"\bwhat\s+should\s+i\s+(?:do|work\s+on|tackle|prioriti[sz]e)\b", re.I)

# "what's new / anything I missed / catch me up" — a RECENCY question, answered
# by get_recent_activity's cross-app feed (see tools/recent_tools).
#
# Deliberately distinct from _TODO_RE above, and checked AFTER it, because the
# two look similar and mean opposite things: a to-do question asks what is still
# OUTSTANDING (forward-looking, needs all four sources merged and reasoned over),
# while this asks what has CHANGED (backward-looking, answered by one cheap
# no-model read). Anchored on new/latest/missed/updates rather than on bare
# "anything", which _TODO_RE legitimately claims ("anything I need to do").
_RECENT_RE = re.compile(
    r"\bwhat'?s\s+new\b|"
    r"\banything\s+new\b|"
    r"\bwhat'?s\s+(?:the\s+)?latest\b|"
    r"\bcatch\s+me\s+up\b|"
    r"\bfill\s+me\s+in\b|"
    r"\banything\s+i\s+missed\b|\bdid\s+i\s+miss\b|"
    r"\bany\s+updates?\b|"
    r"\banything\s+happen(?:ed)?\b|"
    r"\bwhat\s+happened\s+(?:while|since)\b|"
    # VERIFIED FAILURE 2026-08-18: "what have I been up to lately" is the same
    # cross-app recency question in different words and matched none of the
    # patterns above, so it fell to the unscoped 44-tool route and called
    # nothing at all. "what I've been doing" is the same shape without "up to".
    r"\bwhat\s+(?:have\s+)?i(?:'ve|\s+have)?\s+been\s+(?:up\s+to|doing)\b", re.I)

# Every read source a "what do I need to do" answer should draw on.
# get_upcoming covers BOTH calendar events and Reminders.app (see its
# description) — that's one tool, two sources.
#
# Deliberately WITHOUT get_past_events: a to-do list is what's still pending,
# and past events are by definition done. Including it would add a tool that
# can only contribute noise, and every extra tool on the list costs selection
# accuracy (see _mk_scoped).
_ALL_SOURCES = ["get_upcoming", "summarize_messages",
                "summarize_emails", "search_notes"]
# search_notes is LAST deliberately: the user keeps very little in Notes, so it
# is the weakest of the four sources. The order is a weak prior on which tool
# the model reaches for first; the aggregate route still requires ALL of them
# (see RouteDecision.narration_after).
# Inbound "did I get/hear from <someone>" queries that name no channel ("email"/
# "text") explicitly — "did I get anything from UCSC", "did the professor email
# me", "hear back from the recruiter". They're read intents, but without a
# keyword they fell through to the full toolset route, which then called the
# summary tools and thrashed model swaps (a measured 30-65s stall + tool loop).
# Route them to the summarizer's email+messages read tools so an ambiguous "from X"
# checks both channels directly, fast, with no swap. Scoped tightly (requires
# "from" or "<x> … me") so it doesn't eat "did I get anything done".
_INBOUND_RE = re.compile(
    r"\bdid i (?:get|receive|hear)\b[^?]{0,30}\bfrom\b|"
    r"\bhear(?:d)? back from\b|"
    r"\bdid\b[^?]{0,25}\b(?:e-?mail|message|text|write to|contact)\s+me\b|"
    r"\bany(?:thing)?\s+(?:new\s+)?(?:e-?mails?|messages?|texts?)\s+from\b", re.I)
# A bare calendar noun, for COMPOUND read requests where "calendar" isn't
# preceded by "my/the" — e.g. "what's on my messages and calendar". Only used
# inside _domain_subset, which now offers write tools alongside reads, so a
# bare mention here is safely a read. "schedule" is deliberately omitted (it's a
# write verb in _WRITE_INTENT_RE and would muddy the guard); "calendar"/"agenda"
# are unambiguous read nouns and appear in no write phrasing. "reminders?" is
# included too — get_upcoming already surfaces reminders alongside events, but
# no domain regex previously recognized the bare word at all, so "check my
# reminders" fell through to the ambiguous default (the agent model) instead of the
# always-warm the summarizer. Safe to include unconditionally: any WRITE-flavored
# "set/add/create a reminder" is already excluded by the write-guard above
# before this ever gets checked (see _WRITE_INTENT_RE).
# "overdue" / "past due" are calendar nouns in their own right: "clear out my
# overdue stuff" names no calendar word at all, so it missed this domain
# entirely and fell to the 14-tool ambiguous route with no clear_past_reminders
# in it (2026-08-18 22:36 log).
_CALENDAR_NOUN_RE = re.compile(
    r"\b(cal[ae]ndar|agenda|reminders?|overdue|past[\s-]?due)\b", re.I)
# PAST-tense schedule questions. get_upcoming only ever returns FUTURE items,
# so before this the light-read calendar subset could not answer them at all:
# "what did I have on my calendar last week" matched _CALENDAR_NOUN_RE, got the
# get_upcoming-only subset, and the one tool it was allowed to call is empty by
# construction for that question. Verified on the agent model (so this was never a
# small-model weakness): it answered "I'm sorry, but I can't retrieve past
# events right now" — while get_past_events, which covers roughly the last
# year, existed the whole time and appeared NOWHERE in this router.
#
# Two gaps found live 2026-08-08 with "was there are calender events from
# ealier today": (1) the "was/were/did/had I" branch only matched the subject
# ordering "was I", not "was there" / "were there", which is at least as
# common a way to ask the same question; (2) the "earlier" branch only
# accepted "earlier this week/month", not "earlier today" — so a same-day
# past-tense question (the single most common case: something already
# happened a few hours ago) fell through every branch, got the get_upcoming-
# only subset, and the model called get_past_events anyway (it correctly
# reasoned that was the right tool from its own system prompt) only to be
# told the action wasn't available. Both are fixed below; "today" joins
# "week"/"month" and a standalone "was/were there" clause covers the subject-
# first phrasing.
_CALENDAR_PAST_RE = re.compile(
    r"\bwhat\s+did\s+i\b|\b(?:did|was|were|had)\s+i\b|\b(?:was|were)\s+there\b|"
    r"\blast\s+(?:week|month|night|year|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)\b|\byesterday\b|"
    r"\bwhen\s+did\s+i\b|\bearlier\s+(?:this\s+)?(?:week|month|today)\b|"
    r"\bthis\s+past\s+week\b|\bpast\s+(?:few\s+)?(?:days?|weeks?|months?)\b|"
    # "past due" / "overdue" / "old reminders" — these describe items whose
    # time has ALREADY GONE BY, which is exactly what get_upcoming cannot
    # return. Without them, "like my past due reminders" was router-dispatched
    # straight to get_upcoming and answered with the user's future schedule
    # (2026-08-18 22:36 log).
    r"\bpast[\s-]?due\b|\boverdue\b|\bold\s+reminders?\b", re.I)
# Any write/modify verb disqualifies the light read-routes (keeps add/cancel/
# reschedule/remind on the agent model where the write tools are reliable). "send" is
# here for email specifically: there's no send tool anywhere in the roster,
# but that's still not a job for the summarizer's narrow read-only subset — bounce it
# to the agent model like every other write-shaped request.
# NOTE: "schedule" is deliberately NOT here. As a bare word it's far more often
# the NOUN ("what's on my schedule", "check my schedule") — a READ — than the
# verb, and including it wrongly bounced those reads off the summarizer to the agent model. Real
# "schedule a meeting" WRITES are still caught downstream by SCHEDULE_RE (which
# requires "schedule … meeting/event/…") and routed to the agent. "reschedule"
# stays because it's unambiguously a write.
_WRITE_INTENT_RE = re.compile(
    r"\b(add|create|set\s?up|put|remind|book|make|change|edit|cancel|"
    r"reschedule|postpone|delete|remove|move|clear|send)\b|"
    # "set/add/create/make (a) reminder(s)" — bare "remind\b" above only
    # catches "remind me…"; it does NOT catch "reminder" (found via a real
    # incident: "set a reminder to buy milk, and tell me what's on my
    # calendar" matched no write cue at all, so the whole compound request —
    # including the write half — landed on the summarizer's restricted light-read
    # subset, which cannot create reminders). Anchored to a write VERB
    # immediately before "reminder(s)", not bare "remind\w*", so a genuine
    # read like "check my reminders" / "what are my reminders" (no write verb
    # in front) still correctly stays a light-read.
    r"\b(?:set|add|create|make)\s+(?:a\s+|an\s+|\d+\s+)?reminders?\b|"
    # Mailbox triage verbs (mark_email_read / archive_email). Without these,
    # "archive the Walgreens email" read as a pure LOOKUP and was handed only
    # the two read tools — the same shape of bug as the compound send case:
    # the model finds the message, then has nothing to act on it with.
    r"\b(?:archive|file\s+away|mark)\b", re.I)

# The "schedule" half of that NOTE, made good. The note above says bare
# "schedule" is excluded because it is usually the NOUN, and that real
# "schedule a meeting" WRITES are "still caught downstream by SCHEDULE_RE and
# routed to the agent". They were not. SCHEDULE_RE spans reads AND writes, so
# matching it contributes only the calendar DOMAIN — and a calendar domain with
# no write intent is precisely _domain_subset's router-direct get_upcoming
# lookup, which returns before any write tool is added.
#
# VERIFIED 2026-08-19: "schedule a meeting with the team next Tuesday" was
# answered by a read-only get_upcoming; add_calendar_event was never offered.
#
# So the verb is disambiguated the way that NOTE asks for — by its OBJECT,
# never by widening the bare word. "schedule" counts as a write only when a
# calendar object follows it, either directly ("schedule lunch") or behind a
# determiner ("schedule a quick sync"). The determiner is what keeps the reads
# out: "what's on my schedule", "my schedule for this week" and "my schedule
# before my meeting" all continue past "schedule" with a preposition, which is
# neither a determiner nor an object, so none of them match.
_CALENDAR_WRITE_RE = re.compile(
    r"\b(?:schedule|pencil\s+in)\s+"
    r"(?:(?:a|an|the|another|my|our)\s+(?:\w+\s+){0,2}?)?"
    r"(?:meetings?|events?|appointments?|calls?|lunch|dinner|coffee|"
    r"interviews?|sessions?|syncs?|check-?ins?|classes|class|reminders?|"
    r"one[\s-]on[\s-]one)\b", re.I)

# VERIFIED FAILURE 2026-08-24 (debug export 18:22): "delete my reminders
# for today" became clear_past_reminders(query="today")—a time scope treated
# as title text—and "clear all of my reminders" used the past-due-only tool.
_BULK_REMINDER_CLEAR_RE = re.compile(
    r"\b(?:delete|clear|remove)\s+(?:"
    r"all\s+(?:of\s+)?(?:my\s+)?reminders?|my\s+reminders)\b|"
    r"\b(?:delete|clear|remove)\s+(?:(?:all|my)\s+)?reminders?\s+"
    r"(?:for\s+|due\s+)?(?:today|tomorrow)\b", re.I)

# Compose/outbound intent — reply/send/forward/draft an email or message, or
# "email <person>". There's no send tool in the roster, but this is still not a
# job for the summarizer's read-only subset: bail the light-read so it goes to the agent model
# (which handles it / explains the limitation) instead of the summarizer summarizing the
# inbox for a "reply to…" request.
# "shoot my professor a note", "drop Dan a line", "send my boss a quick
# message" — the ditransitive send: verb, RECIPIENT, then the thing being sent.
# Two failures meet in this one shape, both verified 2026-08-19 on "shoot my
# professor a note about the extension":
#
#   1. No branch of _COMPOSE_RE covered it. "shoot"/"drop" are in no verb list,
#      and the recipient sits between the verb and the noun, so the request
#      carried no write intent at all.
#   2. Its object is the word "note", which _NOTES_INTENT_RE reads as Notes.app
#      — so a request to SEND something routed to `search_notes` alone, the one
#      tool that cannot deliver it.
#
# Which is why this is a named constant and not just another _COMPOSE_RE
# branch: the same pattern is also subtracted from the text before the notes
# domain is tested, exactly as _TEXT_NONSMS_RE is subtracted before messages.
# Subtracting (rather than suppressing) keeps the compound case right — "check
# my notes and shoot my professor a note about it" still names both.
#
# The recipient slot must exclude articles: without it the `\w+` recipient
# happily matches the "a" in "write a note about the meeting", which is a
# genuine notes request and not a send. me/myself/you are excluded for the same
# reason as in the branches below (a send-to-self still matches SEND_EMAIL_RE).
_OUTBOUND_NOTE = (
    r"\b(?i:shoot|send|drop|fire\s+off|write|leave)\s+"
    r"(?!a\b|an\b|the\b|me\b|myself\b|you\b|it\b|that\b|this\b|out\b)"
    r"(?:(?i:my)\s+\w+|\w+)\s+"
    r"(?:(?i:an?)\s+)?(?i:quick\s+|short\s+|brief\s+)?"
    r"(?i:note|line|message|e-?mail|text)\b")
_OUTBOUND_NOTE_RE = re.compile(_OUTBOUND_NOTE, re.I)

_COMPOSE_RE = re.compile(
    _OUTBOUND_NOTE + r"|"
    # “send me a reminder” requests a local reminder notification. The
    # recipient-like word after send is the user, and the object is a reminder;
    # treating this as outbound compose is what changed “ask Trishy” into a
    # message recipient and changed “my move-in date” into hers.
    r"\bsend\b(?!\s+me\s+(?:an?\s+)?reminder\b)|"
    r"\b(?:reply|respond|forward|compose|draft)\b|"
    # "give my mom an update", "let Dad know", "tell Mom", "update my mom on".
    #
    # Verified failure 2026-08-09: "give an update to my mom VIA MESSAGES about
    # what I did this month" was classified as a pure LOOKUP — none of the verbs
    # above appear in it — so the route was read-only and `send_message` was
    # never offered. The model was asked to send and structurally could not,
    # which is the same shape as the 212s "composed an email it could not send"
    # bug _domain_subset was reworked to eliminate.
    #
    # The recipient is required and must not be the USER: "tell me what's on my
    # calendar" and "give me an update" are reads, not sends. `me`/`myself` are
    # excluded explicitly for that reason — note "email MYSELF a summary" still
    # matches SEND_EMAIL_RE, which handles the genuine send-to-self case.
    r"\b(?:give|send)\s+(?:an?\s+)?update\s+to\s+(?!me\b|myself\b)\w+|"
    # `you` joins me/myself in every recipient lookahead below. The exclusion
    # list was about not reading a request aimed at the USER as a send; the
    # assistant is the other participant in this conversation and is just as
    # impossible a recipient. Verified 2026-08-19: "what did I tell you about
    # the car" matched the `tell` branch with "you" as the recipient and was
    # routed to the messages+email SEND tools — a question about the fact
    # store, answered with the outbound toolset. "I'll let you know later" and
    # "I'll give you an update" are the same false positive on the other two.
    r"\bgive\s+(?!me\b|myself\b|you\b)(?:my\s+\w+|him|her|them|mom|dad|\w+)\s+"
    r"(?:an?\s+)?(?:update|heads[- ]?up|message)\b|"
    r"\blet\s+(?!me\b|you\b)(?:my\s+\w+|him|her|them|mom|dad|everyone|[A-Z]?\w+)\s+know\b|"
    r"\btell\s+(?!me\b|myself\b|you\b)(?:my\s+\w+|him|her|them|mom|dad|everyone|[A-Z]?\w+)\s+"
    r"(?:that|about|what|when|i|i'?m|i'?ll)\b|"
    r"\bupdate\s+(?!me\b|myself\b)(?:my\s+\w+|him|her|them|mom|dad)\s+(?:on|about)\b|"
    # IMPERATIVE "email/text/message <anyone>" at the start of the request.
    # The fixed-noun list below can only cover recipients somebody thought to
    # enumerate, and it didn't have "recruiter" — so "email the recruiter
    # Monday morning saying I accept" registered as no write intent at all and
    # got a read-only inbox subset, the same failure this whole module was
    # just reworked to eliminate. Position does the work a name list can't:
    # leading "email …" is the verb, whereas the noun sense ("email from X",
    # "emails yesterday") doesn't start the sentence. \b keeps plural "emails"
    # out, which is what makes that distinction hold.
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:e-?mail|text|message|dm)\s+\w|"
    r"\bemail\s+(?:my\s+|the\s+|to\s+)?(?:professor|prof|teacher|boss|mom|dad|"
    r"friend|colleague|him|her|them|everyone|someone|back)\b|"
    # "text/email/message <anyone> back" — "back" is the reliable tell (an
    # arbitrary-name object, e.g. "email the recruiter back", can't be
    # enumerated the way the fixed-noun list above is), covering both email
    # AND messages compose (messages has no compose tool either, but "text
    # mom back" is still not something the summarizer's read-only subset can do).
    r"\b(?:text|message|email|write|call)\b[^.?!]{0,24}\bback\b", re.I)

# A compose sentence whose TOPIC is named rather than dictated — "about my
# move-in date", "with my college move in date", "regarding the meeting
# time" — as opposed to a message whose exact words are already in the
# sentence ("tell her I'll be late"). The first shape needs a FACT the model
# doesn't have yet; the second doesn't need anything looked up at all. This
# is a deliberately loose net (any "about/with/regarding/on my|the <noun>"
# after a compose verb) rather than an attempt to name every fact-shaped
# topic — a false positive costs one extra local, no-network tool call
# (cheap); a false negative is the bug this exists to close.
#
# VERIFIED FAILURE 2026-08-24 (two of the user's debug exports, same day):
# "send mom a message reminder her about my move in date" got get_upcoming
# in its subset ONLY because "reminder" (a typo for "remind") happens to
# match _CALENDAR_NOUN_RE below — an accident, not genuine domain detection.
# The retry, phrased naturally as "send a message to mom with my college
# move in date", matched no calendar/notes noun at all and got a
# messages+email-only subset (get_upcoming wasn't even offered). In BOTH
# turns the model's answer was "I don't have that date" instead of
# searching — including on the retry, run AFTER a system-prompt rule was
# added telling it to search before asking. The prompt rule alone changed
# nothing (tool_digest still showed only lookup_contact): a rule can't make
# the model call a tool that was never in its offered set, and even where
# the tool WAS offered (the first turn), plain availability didn't get it
# called either. See the block below that uses this regex for the
# structural fix — widen the subset AND force the first step, the same
# "withhold/require the tool, don't just ask nicely" approach already
# proven by channel_ambiguous (_CHANNEL_OUTBOUND_TOOLS) elsewhere in this
# file.
_TOPIC_LOOKUP_RE = re.compile(
    r"\b(?:about|with|regarding|on)\s+(?:my|the)\s+\w+", re.I)

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


# Positively trivial — greetings, thanks, acknowledgements. fast/the summarizer is now
# OPT-IN (this list), not a catch-all length rule; a prompt that fails to match
# falls through to the agent model, never silently to the tool-less fast model.
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

# `debug` carries a NOUN sense that has nothing to do with code — "debug logs",
# "debug mode", "debug output" are things a user wants moved, read, or listed.
# Left bare it made "organize my wisp debug logs by date" read as a code
# request: it matched here AND in CODE_AUTHOR_RE (same single word), so
# _domain_subset's code-authoring bailout (below) sent it to the ambiguous
# fallback instead of the files route it actually needed. The verb sense
# ("debug the parser") still matches.
CODE_RE = re.compile(
    r"\b(bug|refactor|function|traceback|exception|stack ?trace|regex|async|"
    r"compile|syntax|debug(?!\s+(?:logs?|files?|folders?|modes?|outputs?|"
    r"info|data|menus?|messages?|symbols?))|unit ?test|npm|pip|webpack|docker|api endpoint|"
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
    source: str          # "rules" or "llm"
    reason: str
    route_source: str = ""
    # Forces tool_choice="required" + retry-and-nudge in the agent loop (see
    # run_agent's `expect_tool_first`) — ONLY when a rule confidently detected
    # tool intent. False for the ambiguous fallback (tools are still AVAILABLE,
    # tool_choice stays "auto") — forcing a tool call on a genuinely uncertain
    # prompt is what caused "any new ideas for the project" to get refused
    # outright instead of answered, when nothing sensible was callable.
    expect_tool_first: bool = False
    # When set, the agent loop is restricted to exactly these tools. Used for
    # the light read-only summary routes (messages/calendar/notes) that run on
    # the always-warm the summarizer instead of the agent model: the summarizer is reliable on this narrow
    # read-only set (verified) even though it's not trusted with the full
    # machine-operating toolset. Its presence also tells _finalize NOT to force
    # the request onto the agent model (see is_tool_capable), and main.py NOT to pin it
    # to a resident the agent model — the whole point is to keep the agent model asleep.
    tool_subset: list[str] | None = None
    # Names the ONE tool the agent loop must offer on its first step (see
    # run_agent's force_first_tool). Set when a rule identified not just that a
    # tool is needed but exactly which one — restricting the schema to a single
    # function is the only real guarantee a small model actually calls it, since
    # oMLX's tool_choice is a soft nudge (see run_agent's docstring).
    force_first_tool: str | None = None
    # Whether this route is a warm READ-OUT of the user's own data (calendar,
    # notes, messages, mail) — main.py turns this into _LIGHT_READ_STYLE.
    #
    # Deliberately its own field rather than `bool(tool_subset)`, which is what
    # main.py used to test. That conflation was fine while every scoped route
    # was a personal-data read, and became wrong the moment scoping was
    # extended to device control and to the ambiguous fallback: the style hint
    # says "you are giving the user a warm, caring read-out of their own
    # calendar / notes / messages", so inheriting it would have answered
    # "what's my battery level" — and "tell me a joke" — in a bulleted,
    # emoji'd, calendar-readout voice.
    light_read: bool = False
    # Whether this route's tools are SEQUENTIAL/COMPLEMENTARY rather than
    # alternatives — several of them may genuinely be needed in one turn, not
    # just one. Forces agent/loop.run_agent's narration-thinking gate to stay
    # off even in "broad" mode (see config.narration_mode), because that mode's
    # whole premise — "every tool the model has called so far is clean, so it's
    # done" — is false here: calling the FIRST of four required sources and
    # then narrating without thinking is exactly how a route like the aggregate
    # to-do list (get_upcoming + search_notes + summarize_emails +
    # summarize_messages) would silently answer from one source and present it
    # as complete. loop.SYSTEM's own words for that: "not a partial answer, it
    # is a WRONG one." Defaults False because most scoped routes genuinely are
    # alternatives (view_emails vs summarize_emails, one call answers either
    # way) — True is the exception, set explicitly on the handful of routes
    # that need every tool called, not just one.
    multi_round: bool = False
    # The tools that must ALL have answered before a `multi_round` route is
    # allowed to narrate without thinking. Only consulted when multi_round is
    # True; empty means "never narrate on this route", which is what multi_round
    # meant on its own.
    #
    # Why this exists: multi_round was a veto for the whole TURN, and that is
    # stronger than the risk it guards against. The risk is the model stopping
    # after the FIRST of several required tools; it disappears completely once
    # every required tool has come back. Measured 2026-08-09 on the aggregate
    # to-do route, which calls all four sources in step 0 and then narrates in
    # step 1: that narration step still generated 2,415 characters of reasoning
    # over 14.85s, and the ambiguous/core route's 5,097 characters over 25.0s —
    # both on a step where the model had every source in front of it and nothing
    # left to decide. Naming the required set turns the veto into a condition.
    #
    # Deliberately NOT set on the ambiguous core fallback: that route's tools
    # are heterogeneous and there is no statically-knowable required set, so it
    # keeps the original never-narrate behavior.
    narration_after: frozenset[str] = frozenset()
    # What `multi_round` should become for a SCOPED domain rule (_mk_scoped)
    # that left `tool_subset` None to be filled by retrieval instead of a
    # hand-written list — see route()'s post-rule_route block. None (the
    # default) means "this decision has no opinion", which is every _mk()
    # route and covers the genuinely-ambiguous fallback: route() then applies
    # its own historical default (True — heterogeneous by construction, see
    # its comment). A domain rule that already KNOWS its retrieved tools are
    # alternatives, not a required sequence (apps/media, device control — "open
    # safari" is done after one open_app call), sets this to False via
    # _mk_scoped's `multi` param so route() respects it instead of overwriting
    # it. Ignored entirely when tool_subset is not None (nothing to fill).
    multi_round_on_retrieval: bool | None = None
    # True when the route added BOTH messages and email capability because a
    # send/compose intent named a recipient but no channel ("tell mom about
    # my schedule", "send this to mom tonight") — the model was left to guess
    # which app to use. main.py turns this into an explicit "ask which one"
    # directive rather than letting the model silently pick.
    clarify_channel: bool = False
    # True when a REORGANIZE names nothing concrete to act on ("reorganize my
    # files"). main.py turns this into a directive to ask WHICH folder before
    # touching anything — see _CONCRETE_TARGET_RE for the measurement behind it.
    # The implied scope is the whole home directory and the job is moving files,
    # so a guess here is the expensive kind.
    clarify_target: bool = False
    # Tool calls the ROUTER resolved in full — name AND arguments — for
    # run_agent to execute before its first model call. See the direct-dispatch
    # block above _DIRECT_DEVICE_PATTERNS for when a rule may set this, and
    # _mk_direct for the invariants it has to satisfy.
    direct_calls: list[tuple[str, dict]] = field(default_factory=list)
    # Machine-checkable definition of what this turn must accomplish. Each
    # group is an any-of set; every group must be satisfied before the loop may
    # accept final prose. This is intentionally separate from tool_subset:
    # availability is not completion.
    required_tool_groups: tuple[frozenset[str], ...] = ()
    # Registered tools that explicit negative language made unavailable for
    # this turn ("without opening my inbox", "do not send", "not the calendar
    # event"). The loop enforces this too, so a hallucinated registered call
    # cannot bypass schema withholding.
    forbidden_tools: frozenset[str] = frozenset()
    # Conditional action requirements: (source tool, action tool, predicate,
    # value). The loop waives the action only when a successful source result
    # proves its condition false. In test mode the result is synthetic, so the
    # action remains part of the planned call sequence.
    conditional_tools: tuple[tuple[str, str, str, object], ...] = ()
    # Arguments fixed by a typed workflow. The model still writes grounded
    # message content, but it cannot silently change the recipient, channel or
    # scheduled time selected by the user. run_agent overlays these values on
    # every matching model call before safety review and execution.
    tool_argument_bindings: dict[str, dict] = field(default_factory=dict)
    # A self-contained reading of an elliptical follow-up, retained in debug
    # output and passed to generation so routing and answering share intent.
    resolved_request: str = ""
    # A clarification is legitimate; a calendar read is not reminder creation.
    reminder_action: str = ""  # create | clarify_time
    # NOTE on unscoped tool routes: a rule may leave `tool_subset` None when it
    # knows a tool is wanted but not which domain. `route()` then fills it by
    # semantic retrieval — see the block after its `rule_route` call. That is a
    # UNIVERSAL rule, not opt-in: an unscoped route offers the ENTIRE registry,
    # measured at 62 tools as ~12,800 tokens (80% of the 16k window before the
    # user's message) and the configuration this project measured as worst at
    # tool selection (5-7 offered -> 3/3 correct, all 44 -> 2/3). It also does
    # not survive the Capability Atlas work: the same route is ~42,000 tokens at
    # 198 tools. See [[moe-context-window-budget]].
    #
    # An earlier version made this opt-in via a `retrieve_tools` flag, which
    # covered only the one route it was added for — "what do I have open" hit a
    # different unscoped route and still got all 78 schemas. Blanket is correct:
    # no route benefits from the full registry.

    def as_dict(self) -> dict:
        # needs_vision is deliberately still emitted, hardcoded False: this dict
        # is the wire shape for the `routed` SSE event, which the Swift client
        # parses. Dropping the key would break an older peer that still reads
        # it, for no gain — Wisp has no vision route any more, so the honest
        # value is a constant.
        return {"role": self.role, "model": self.model, "needs_tools": self.needs_tools,
                "needs_vision": False, "source": self.source, "reason": self.reason,
                "route_source": self.route_source or self.source,
                # JSON-shaped (not the internal tuples) because this dict is the
                # wire format for the `routed` SSE event. Worth emitting: it is
                # the only record of WHY a turn had no tool-selection step,
                # which the debug export otherwise can't distinguish from the
                # model declining to call anything.
                "direct_calls": [{"tool": n, "args": a} for n, a in self.direct_calls],
                "required_tool_groups": [sorted(g) for g in self.required_tool_groups],
                "forbidden_tools": sorted(self.forbidden_tools),
                "conditional_tools": [list(item) for item in self.conditional_tools],
                "tool_argument_bindings": self.tool_argument_bindings,
                "resolved_request": self.resolved_request,
                "reminder_action": self.reminder_action}


def _mk(role: str, *, tools=False, source="rules", reason="",
        route_source: str | None = None,
        expect_tool_first: bool | None = None) -> RouteDecision:
    # Every existing rule-based `tools=True` call site is a confident match —
    # default expect_tool_first to follow `tools` unless a caller overrides it
    # (the ambiguous fallback explicitly passes False).
    force = tools if expect_tool_first is None else expect_tool_first
    return RouteDecision(role, role_to_model(role), tools, source, reason,
                         route_source=route_source or source,
                         expect_tool_first=force)


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


def _mk_scoped(subset: list[str] | None, reason: str,
               force: str | None = None,
               expect: bool = True,
               light: bool = True,
               multi: bool = False,
               after: frozenset[str] | None = None,
               clarify_channel: bool = False,
               clarify_target: bool = False) -> RouteDecision:
    """A tool route restricted to `subset`. expect_tool_first=True so the loop
    guarantees the tool actually runs.

    `subset=None` leaves the tools for route() to fill by semantic retrieval
    instead of a hand-written list — the domain rule still confidently claims
    the request (so it never falls through to the ambiguous ~everything-else
    route), it just no longer needs its OWN static tool list, which is
    strictly better for reachability: a new tool registered in this domain is
    discoverable by retrieval the moment it exists, where a static list needs
    remembering to add it by hand (find_files went dark for a full diagnosis
    cycle this way in the files domain — see that route's comment). Only worth
    it once a
    domain's real tool count has grown past the reliable menu size (see the
    numbers below) — a domain that's naturally small should stay a literal
    list, since retrieval is strictly more moving parts for no benefit there.

    Narrowing the advertised toolset is a RELIABILITY measure, not a model-swap
    optimization (which is what it used to be, back when this picked the small
    always-warm model to keep a big one asleep). Measured on the current single
    resident model, same prompt, tool_choice="required", greedy:

        offered  5-7 domain tools  -> 3/3 correct calls
        offered  all 44 tools      -> 2/3, and it reached for show_profile

    A long tool list measurably degrades selection, so every rule route that
    knows its domain says so. `force` narrows the FIRST step to a single tool,
    for cases where even a small set is unreliable — verified live: asked to
    "remember that I prefer short answers" with remember/recall/forget all
    offered, the model replied "I have remembered that…" and called nothing,
    then on retry insisted it already had. Nothing was saved and the user was
    told it was, which is the worst possible outcome for a memory feature.

    `expect` (default True) is what pins tool_choice="required" on the first
    step. Set it False when the request may legitimately be INCOMPLETE — a tool
    whose required arguments the user simply hasn't supplied yet. Forcing there
    is actively harmful: measured on "remind me to call mom", which names no
    time while add_reminder requires `when_iso`, the model correctly answers
    "what time would you like to be reminded?" — and a forced tool_choice turns
    that right answer into three rejected attempts and ~7s before it gets said
    anyway.

    `light` (default True) marks this as a warm read-out of the user's own
    data, which main.py turns into _LIGHT_READ_STYLE. It defaults True because
    every route that existed when this was written WAS such a read; pass False
    for device control, web lookups, and the ambiguous fallback, where that
    voice would be plainly wrong (see RouteDecision.light_read).

    `multi` (default False) marks this route's tools as sequential/
    complementary rather than alternatives — see RouteDecision.multi_round for
    why that has to disable the agent loop's broad narration-thinking mode.
    Pass True only for routes that genuinely need several of their offered
    tools called in the same turn, not just one.

    `after` names the tools whose results make a `multi` route provably done, so
    it can narrate without thinking from that point on — see
    RouteDecision.narration_after. Ignored unless `multi` is True. Omit it for a
    route with no statically-knowable required set; that keeps multi_round's
    original never-narrate behavior.
    """
    d = _mk("agent", tools=True, reason=reason, expect_tool_first=expect,
            route_source="rules")
    d.tool_subset = subset
    d.force_first_tool = force
    d.light_read = light
    d.multi_round = multi
    d.clarify_channel = clarify_channel
    d.clarify_target = clarify_target
    if after:
        d.narration_after = frozenset(after)
    # Only meaningful when subset is None (see the field's docstring) — set
    # unconditionally here since it's simply unused otherwise, rather than
    # adding a second condition callers have to get right.
    d.multi_round_on_retrieval = multi
    return d


def _mk_direct(calls: list[tuple[str, dict]], reason: str,
               *, light: bool = True) -> RouteDecision:
    """A scoped route whose tool calls the router has ALREADY resolved in full.

    The tools offered to the model are exactly the ones being pre-called, and
    `expect_tool_first`/`force_first_tool` are deliberately left off: both turn
    step 0 back into a forced tool-SELECTION step, which is the step this whole
    mechanism exists to skip. The model's first (and usually only) job here is
    to narrate results that are already in its context.

    Never emit a direct call the request could have modified — see
    _direct_device_call's exactly-one rule and _DIRECT_SUMMARY_RE's anchoring.
    A wrong pre-resolved argument is worse than a slow turn, because the model
    never gets the chance to notice it.
    """
    d = _mk_scoped([name for name, _ in calls], reason,
                   expect=False, light=light)
    d.direct_calls = calls
    return d


# A bare follow-up fragment that only narrows the PREVIOUS query's SCOPE (a
# date/time window) but names no domain of its own: "from yesterday", "what
# about today", "and this morning". On its own it classifies as nothing and
# falls through to the the agent model default — the reported bug where, right after
# "what's on my email", typing "from yesterday" jumped to "Direct to OSS"
# instead of continuing the email read on the summarizer. Handled only when the prior
# turn actually used a light-read tool (see _fragment_continuation).
_FRAGMENT_RE = re.compile(
    r"^(?:and\s+|what\s+about\s+|how\s+about\s+|(?:what|any(?:thing)?)\s+from\s+|"
    r"from\s+|just\s+|only\s+)?"
    r"(?:yesterday|today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|week)|"
    r"last\s+(?:week|night)|earlier(?:\s+today)?|so\s+far(?:\s+today)?|"
    r"this\s+past\s+week|the\s+past\s+(?:few\s+)?days?)\b[\s.?!]*$", re.I)

# Which light-read subset to reuse, keyed on a tool name that appears in the
# previous turn's tool_digest. Mirrors the subsets _domain_subset builds.
_TOOL_TO_LIGHT_SUBSET = {
    "summarize_emails": ["view_emails", "summarize_emails"],
    "view_emails": ["view_emails", "summarize_emails"],
    "summarize_messages": ["view_messages", "summarize_messages"],
    "view_messages": ["view_messages", "summarize_messages"],
    "search_conversations": ["search_conversations", "view_messages", "summarize_messages"],
    "get_upcoming": ["get_upcoming"],
    "search_notes": ["search_notes"],
}


# Domain for a WRITE tool, the counterpart to _TOOL_TO_LIGHT_SUBSET (which only
# covers reads). Needed because a turn that only wrote something — "remind me
# to X" calling add_reminder, with no get_upcoming alongside it — left
# _confirmation_subset/_write_continuation_subset with nothing to key off, even
# though the write tool itself says exactly which domain the turn was in.
_WRITE_TOOL_DOMAIN = {
    "add_reminder": "calendar", "update_reminder": "calendar",
    "add_calendar_event": "calendar", "cancel_event": "calendar",
    "clear_past_reminders": "calendar", "clear_reminders": "calendar",
    "send_message": "messages", "draft_message": "messages",
    "send_email": "email", "reply_to_email": "email", "draft_email": "email",
    "archive_email": "email", "mark_email_read": "email",
    "create_note": "notes", "append_note": "notes",
}


def _domains_in(last_tools: str) -> list[str]:
    """Every domain touched by the previous turn, read OR write, in the order
    first seen. Shared by _confirmation_subset and _write_continuation_subset so
    the two can't drift on what counts as "the previous turn's domain"."""
    domains: list[str] = []
    for tool, sub in _TOOL_TO_LIGHT_SUBSET.items():
        if tool in last_tools:
            dom = ("messages" if ("messages" in tool or tool == "search_conversations") else
                   "email" if "email" in tool else
                   "calendar" if tool == "get_upcoming" else "notes")
            if dom not in domains:
                domains.append(dom)
    for tool, dom in _WRITE_TOOL_DOMAIN.items():
        if tool in last_tools and dom not in domains:
            domains.append(dom)
    return domains


# A request that continues a WRITE the previous turn just made, without
# repeating enough for any rule_route pattern to fire on its own — "set some
# more the day before it", "add another one", "do the same for Friday", "one
# more like that". Verified live 2026-08-10: exactly this phrasing, right after
# a turn that called add_reminder twice, fell through every rule to the
# ambiguous core-tools fallback — which does not include add_reminder/
# add_calendar_event at all — and the model told the user "I don't have the
# ability to create new calendar events or reminders with my available tools."
# That is false; the tool exists, it just wasn't offered. A capability the
# model has and denies having is worse than it asking a clarifying question,
# because the user has no way to tell the two apart.
_WRITE_CONTINUATION_RE = re.compile(
    r"\b(?:set|add|make|create)\s+(?:some\s+|a\s+few\s+)?more\b|"
    r"\b(?:set|add|make|create)\s+(?:one\s+more|another)\b|"
    r"\bone\s+more\b|"
    r"\banother\s+(?:reminder|event)\b|"
    r"\bthe\s+(?:day|night)\s+(?:before|after)\s+(?:it|that|those|the\s+other\s+one)\b|"
    r"\bsame\s+(?:thing\s+)?for\b|"
    r"\bdo\s+(?:that|it)\s+again\b|"
    r"\balso\s+(?:set|add|make|create)\b", re.I)


# An immediate date correction after creating a reminder is fully specified by
# the previous write plus one new day: "I mean today", "actually tomorrow",
# "make it today instead". Sending that fragment through semantic retrieval
# caused the reported failure: update_reminder did not exist, get_upcoming and
# remember were selected instead, and the model claimed an update no tool had
# performed. This path skips tool selection entirely and preserves the existing
# time of day in update_reminder itself.
_REMINDER_CORRECTION_RE = re.compile(
    r"^\s*(?:(?:i\s+mean|actually)\s*[,—-]?\s*|"
    r"(?:make|move)\s+(?:it|that|the\s+reminder)\s+(?:to\s+)?|"
    r"change\s+(?:it|that|the\s+reminder)\s+to\s+)?"
    r"(?P<day>today|tomorrow)"
    r"(?:\s+(?:instead|sorry))?\s*[.!]?\s*$", re.I)


def _reminder_correction_subset(text: str,
                                last_tools: str | None) -> RouteDecision | None:
    if not last_tools:
        return None
    prior = {name.strip() for name in last_tools.split(",")}
    if not ({"add_reminder", "update_reminder"} & prior):
        return None
    match = _REMINDER_CORRECTION_RE.match(text or "")
    if not match:
        return None
    day = match.group("day").lower()
    decision = _mk_direct(
        [("update_reminder", {"day": day})],
        f"corrects the reminder just created -> update_reminder ({day}, router-direct)",
        light=False)
    return decision


def _reminder_repair_subset(text: str, last_assistant: str | None,
                            last_tools: str | None) -> RouteDecision | None:
    """Repair a reminder after verification showed the wrong date."""
    if not last_assistant or "reminder" not in last_assistant.lower():
        return None
    prior = {name.strip() for name in (last_tools or "").split(",")}
    if not prior.intersection({"get_upcoming", "add_reminder", "update_reminder"}):
        return None
    complaint = re.search(
        r"\bstill\s+(?:set\s+)?for\s+(?:today|tomorrow|tommorow|tmrw?|tmrow)\b",
        text, re.I)
    fix = re.fullmatch(r"\s*(?:(?:ok(?:ay)?\s+)?so\s+)?fix\s+(?:it|that)?\s*[?!.]*\s*",
                       text, re.I)
    if not (complaint or fix):
        return None
    decision = _mk_scoped(
        ["get_upcoming", "update_reminder"],
        "repair the reminder date from conversation context",
        force="update_reminder", light=False, multi=True,
    )
    decision.required_tool_groups = (frozenset({"update_reminder"}),)
    decision.forbidden_tools = frozenset({"remember", "run_shell", "add_calendar_event"})
    return decision


def _notify_correction_subset(text: str,
                              last_tools: str | None) -> RouteDecision | None:
    """Interpret "I mean tell Mom" as an outbound correction, not a reminder."""
    if not last_tools or "add_reminder" not in last_tools:
        return None
    if not re.match(r"^\s*(?:i\s+mean|no[, ]*i\s+mean)\b", text, re.I):
        return None
    if not re.search(r"\b(?:tell|notify|let)\b.*\b(?:mom|dad|him|her|them|my\s+\w+)\b",
                     text, re.I):
        return None
    decision = _mk_scoped(
        ["view_messages", "summarize_messages", "view_emails", "summarize_emails",
         "lookup_contact"],
        "corrects reminder into an outbound notification; ask for channel",
        expect=False, light=False, clarify_channel=True,
    )
    decision.forbidden_tools = frozenset({"add_reminder", "forward_email"})
    return decision


def _offered_text_confirmation(text: str,
                               last_assistant: str | None) -> RouteDecision | None:
    """A short yes to an assistant offer that names text as the available path."""
    if not last_assistant or not CONFIRMATION_RE.match(text.strip()):
        return None
    if not re.search(r"\bi can\s+(?:send|text|message)\b[^.?!]{0,80}\b(?:text|message)\b",
                     last_assistant, re.I):
        return None
    return _mk_scoped(
        ["view_messages", "summarize_messages", "lookup_contact", "send_message"],
        "confirms the offered text-message action", force="send_message", light=False)


_OUTBOUND_FOLLOWUP_RE = re.compile(
    r"^\s*(?:(?:yes|yeah|yep|yup|sure|ok|okay|please|go ahead|do it|do that|"
    r"please do|proceed)(?:\s+(?:please|now))?|(?:ok(?:ay)?\s+)?send\s+(?:it|that|them)|"
    r"(?:it'?s|its|they(?:'re| are))\s+(?:in|on)\s+(?:my\s+)?contacts?)\s*[.!]*\s*$",
    re.I)
_EMAIL_ADDRESS_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_NUMBER_RE = re.compile(
    r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")


def _outbound_channel(text: str) -> str | None:
    """Return an explicitly selected delivery channel.

    ``email summaries`` describes payload, not transport, so email is only a
    channel when it is used as an action, follows ``via/by/through``, names an
    email message, or an address is present.
    """
    if _EMAIL_ADDRESS_RE.search(text):
        return "email"
    if re.fullmatch(r"\s*(?:via\s+)?(?:e-?mail|mail)\s*[.!]?\s*", text, re.I):
        return "email"
    if re.search(r"\b(?:via|by|through|over)\s+(?:e-?mail|mail)\b|"
                 r"\b(?:to|via|through)\s+my\s+(?:e-?mail|inbox)\b|"
                 r"\b(?:send|write|compose|draft)\s+(?:an?\s+)?e-?mail\b|"
                 r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?e-?mail\b",
                 text, re.I):
        return "email"
    if re.fullmatch(r"\s*(?:via\s+)?(?:messages?|imessage|sms|text)\s*[.!]?\s*",
                    text, re.I):
        return "messages"
    if re.search(r"\b(?:via|by|through|over)\s+(?:messages?|imessage|sms|text)\b|"
                 r"\b(?:send|write|compose|draft)\s+(?:a\s+)?(?:text|message|imessage)\b|"
                 r"\b(?:send|write|compose|draft)\b[^.?!]{1,40}"
                 r"\b(?:a\s+)?(?:text|message|imessage)\b|"
                 r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?"
                 r"(?:text|message|imessage|dm)\b",
                 text, re.I):
        return "messages"
    if SEND_MESSAGE_RE.search(text):
        return "messages"
    return None


def _outbound_sources(text: str, last_tools: str | None = None) -> list[str]:
    """Ordered source tools needed to construct an outbound report."""
    sources: list[str] = []
    if re.search(r"\b(?:calendar|calender|schedule|agenda|appointments?|"
                 r"upcoming\s+(?:events?|meetings?))\b", text, re.I):
        sources.append("get_upcoming")
    if re.search(r"\b(?:e-?mail|inbox)\s+(?:summary|summaries|digest|report)\b|"
                 r"\b(?:summary|summaries|digest|report)\s+(?:of|from)\s+"
                 r"(?:my\s+)?(?:e-?mails?|inbox)\b", text, re.I):
        sources.append("summarize_emails")
    if (_STOCK_PAYLOAD_RE.search(text)
            and re.search(r"\b(?:report|summary|price|prices|movement|movements|"
                          r"performance)\b", text, re.I)):
        sources.append("get_stock_price")
    if (_looks_like_live_web_lookup(text)
            and not _has_no_web_constraint(text)):
        sources.append("web_search")

    # Follow-ups often replace the payload noun with "it"/"these". Tool
    # history is useful as a fallback for the source, but never for the action
    # or channel; those come from the user's request.
    if not sources and last_tools:
        prior = {n.strip() for n in last_tools.split(",")}
        for name in ("get_upcoming", "summarize_emails", "summarize_messages",
                     "get_stock_price", "web_search"):
            if name in prior:
                sources.append(name)
    return list(dict.fromkeys(sources))


def _source_outbound_subset(text: str, *, last_user: str | None = None,
                            recent_users: list[str] | None = None,
                            last_assistant: str | None = None,
                            last_tools: str | None = None) -> RouteDecision | None:
    """Build a strict source -> recipient -> delivery workflow.

    This handles both a complete one-turn request and a short continuation.
    It deliberately re-runs the source on a confirmation turn: outbound fact
    grounding only trusts successful reads from the current turn, and a fresh
    read also prevents sending stale calendar, inbox, or market data.
    """
    current = text.strip()
    prior_users = list(recent_users or ([] if last_user is None else [last_user]))
    if last_user and (not prior_users or prior_users[-1] != last_user):
        prior_users.append(last_user)
    prior_context = "\n".join(prior_users)
    channel_followup = bool(
        last_assistant and _ASKED_CHANNEL_RE.search(last_assistant)
        and _CHANNEL_ANSWER_RE.match(current))
    followup = bool(_OUTBOUND_FOLLOWUP_RE.match(current) or channel_followup)
    address_followup = bool(_EMAIL_ADDRESS_RE.search(current)
                            and re.search(r"\b(?:send|to|email)\b", current, re.I))
    if followup or address_followup:
        if not prior_context:
            return None
        # Anchor on the most recent user turn that actually named both a send
        # and a report source. This recovers a task across clarification turns
        # without merging an unrelated older calendar/email request into it.
        anchor = next((item for item in reversed(prior_users)
                       if (_COMPOSE_RE.search(item)
                           or SEND_MESSAGE_RE.search(item)
                           or SEND_EMAIL_RE.search(item))
                       and _outbound_sources(item)), prior_context)
        intent = f"{anchor}\n{current}"
        # A short assent only continues a send when the conversation really
        # offered one. This keeps ordinary "yes" after calendar/reminder
        # questions on their existing paths.
        if followup and not (_COMPOSE_RE.search(last_user or "")
                             or SEND_MESSAGE_RE.search(last_user or "")
                             or SEND_EMAIL_RE.search(last_user or "")
                             or re.search(r"\b(?:send|text|message|email)\b",
                                          last_assistant or "", re.I)):
            return None
    else:
        intent = current
        # Complete requests must name both a delivery action and a report-like
        # payload. Ordinary "text Mom hi" stays on the existing message path.
        if not (_COMPOSE_RE.search(current)
                or SEND_MESSAGE_RE.search(current)
                or SEND_EMAIL_RE.search(current)):
            return None

    sources = _outbound_sources(
        intent, last_tools if followup or address_followup else None)
    if not sources:
        return None
    channel = (_outbound_channel(current)
               or next((found for item in reversed(prior_users)
                        if (found := _outbound_channel(item)) is not None), None))
    has_address = bool(_EMAIL_ADDRESS_RE.search(intent))
    has_phone = bool(_PHONE_NUMBER_RE.search(intent))

    if channel is None:
        # Source and contact tools remain available, but there is no effect
        # tool to let the model silently choose a channel. Asking the user is
        # the only valid completion.
        decision = _mk_scoped(
            list(dict.fromkeys(sources + ["lookup_contact"])),
            "outbound report has no delivery channel -> ask text or email",
            expect=False, light=False, multi=True, clarify_channel=True)
        decision.forbidden_tools = frozenset(
            _CHANNEL_OUTBOUND_TOOLS | {"forward_email"})
        return decision

    normalized_intent = _normalize_typos(intent)
    scheduled = bool(
        _SCHEDULE_ACTION_RE.search(normalized_intent)
        or re.search(r"\b(?:send|email|text|message)\b[^.?!]{0,120}"
                     r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
                     normalized_intent, re.I))
    self_delivery = bool(
        _SELF_SEND_RE.search(normalized_intent)
        or re.search(r"\b(?:to|via|through)\s+my\s+(?:e-?mail|inbox)\b",
                     normalized_intent, re.I))
    draft_only = bool(_DRAFT_ONLY_RE.search(normalized_intent) or self_delivery)
    if scheduled:
        effect = "schedule_send"
    elif draft_only:
        effect = "draft_message" if channel == "messages" else "draft_email"
    else:
        effect = "send_message" if channel == "messages" else "send_email"
    needs_contact = not self_delivery and not (
        has_phone if channel == "messages" else has_address)
    subset = list(sources)
    groups: list[frozenset[str]] = [frozenset({name}) for name in sources]
    if needs_contact:
        subset.append("lookup_contact")
        groups.append(frozenset({"lookup_contact"}))
    subset.append(effect)
    groups.append(frozenset({effect}))
    path = sources + (["lookup_contact"] if needs_contact else []) + [effect]
    decision = _mk_scoped(
        list(dict.fromkeys(subset)),
        f"grounded outbound report -> {' -> '.join(path)}",
        light=False, multi=True)
    decision.required_tool_groups = tuple(groups)
    decision.forbidden_tools = frozenset(
        (_CHANNEL_OUTBOUND_TOOLS | {"forward_email"}) - {effect})
    return decision


def _write_continuation_subset(text: str, last_tools: str | None) -> RouteDecision | None:
    """Tools for a request that extends the previous turn's WRITE, inherited the
    same way _confirmation_subset inherits for a bare "yes" — see its docstring
    and _WRITE_CONTINUATION_RE above for the failure this closes.

    Deliberately NOT forced (`expect=False`): the target is often genuinely
    ambiguous ("the day before IT" could mean either of two reminders just
    set), and forcing a tool call there just turns a fair clarifying question
    into three rejected attempts. The fix is making the tool AVAILABLE, not
    making it mandatory — the model can still ask which one, truthfully this
    time, instead of denying it can do this at all.
    """
    if not last_tools or not _WRITE_CONTINUATION_RE.search(text):
        return None
    domains = [d for d in _domains_in(last_tools) if d in _DOMAIN_WRITE_TOOLS]
    if not domains:
        return None
    subset: list[str] = []
    for dom in domains:
        subset += _TOOL_TO_LIGHT_SUBSET.get(
            {"calendar": "get_upcoming", "email": "summarize_emails",
             "messages": "summarize_messages"}.get(dom, ""), [])
        subset += _DOMAIN_WRITE_TOOLS.get(dom, [])
    subset = list(dict.fromkeys(subset))
    return _mk_scoped(
        subset,
        f"continues previous {'+'.join(domains)} write -> inherited tools ({len(subset)})",
        expect=False, light=False)


# The assistant's own "text or email?" question, and the user's bare answer to
# it. Both halves are needed: the answer alone ("email") is indistinguishable
# from a fresh read request ("email" -> inbox lookup), which is exactly what it
# routed to before this existed — a read-only 2-tool subset with no way to send,
# so answering the question Wisp had just asked left it unable to act on the
# answer. Measured on all seven natural phrasings.
_ASKED_CHANNEL_RE = re.compile(
    r"(?=.*\b(?:text|imessage|messages?)\b)(?=.*\b(?:e-?mail)\b).*\?", re.I | re.S)
_CHANNEL_ANSWER_RE = re.compile(
    r"^\s*(?:(?:please\s+|just\s+|lets?\s+|let's\s+|use\s+|do\s+|send\s+(?:it\s+)?"
    r"(?:as\s+|via\s+|by\s+|through\s+)?|via\s+|by\s+|as\s+|through\s+|through\s+"
    r"the\s+)\s*)*(?:a\s+|an\s+|the\s+)?"
    r"(?P<ch>text|texts|texting|imessage|i-message|sms|messages?|msg|"
    r"e-?mail|mail)\b(?:\s+(?:her|him|them|it|message|please))?\s*[.!]?\s*$", re.I)
def _channel_answer_subset(text: str, last_assistant: str | None,
                           last_tools: str | None) -> RouteDecision | None:
    """The user naming a channel in reply to Wisp's own "text or email?".

    Only fires when the PREVIOUS assistant turn actually asked (both channels
    named, with a question mark) — otherwise a bare "email" is just an inbox
    read and must stay one.
    """
    if not last_assistant or not _ASKED_CHANNEL_RE.search(last_assistant):
        return None
    m = _CHANNEL_ANSWER_RE.match(text or "")
    if not m:
        return None
    word = m.group("ch").lower()
    domain = "email" if word.replace("-", "") in ("email", "mail") else "messages"
    reads = (["view_messages", "summarize_messages"] if domain == "messages"
             else ["view_emails", "summarize_emails"])
    # The reads come along so the answer turn can still see what it is
    # summarizing; get_upcoming too, since the pending request is very often
    # "send them my schedule" and its data is a turn behind now.
    subset = reads + ["get_upcoming"] + _DOMAIN_WRITE_TOOLS[domain]
    return _mk_scoped(list(dict.fromkeys(subset)),
                      f"answered the channel question -> {domain} read+write "
                      f"({len(set(subset))})",
                      expect=False, light=False)


def _contextual_reply_subset(text: str, last_tools: str | None) -> RouteDecision | None:
    """A bare reply inherits the channel that supplied the prior message."""
    if not last_tools or not re.search(r"^\s*(?:reply|respond)\b", text, re.I):
        return None
    domains = _domains_in(last_tools)
    if domains == ["messages"]:
        return _mk_scoped(
            ["search_conversations", "view_messages", "summarize_messages",
             "lookup_contact", "send_message", "draft_message"],
            "reply continues previous Messages thread", expect=False, light=False)
    if domains == ["email"]:
        return _mk_scoped(
            ["view_emails", "summarize_emails", "lookup_contact",
             "reply_to_email", "draft_email"],
            "reply continues previous email thread", expect=False, light=False)
    return None


def _confirmation_subset(last_tools: str | None) -> RouteDecision | None:
    """Tools for a bare "yes"/"go ahead", inherited from the previous turn.

    A confirmation names no domain of its own, so the only evidence available is
    what the turn being confirmed actually used. Maps those back to their domain
    (the same table _fragment_continuation uses) and offers that domain's READ
    tools plus its WRITE tools — a confirmation is approving an ACTION, so the
    write half is the point; offering reads alone reproduces the "found it but
    can't act on it" failure that _domain_subset was reworked to eliminate.

    Returns None when the previous turn used no tools, or used only tools with
    no domain mapping (run_shell, create_tool, …) — there is nothing to inherit
    and the full toolset remains the honest answer.
    """
    if not last_tools:
        return None
    subset: list[str] = []
    domains = _domains_in(last_tools)
    if not domains:
        return None
    for dom in domains:
        subset += _TOOL_TO_LIGHT_SUBSET.get(
            {"calendar": "get_upcoming", "email": "summarize_emails",
             "messages": "summarize_messages", "notes": "search_notes"}.get(dom, ""), [])
        subset += _DOMAIN_WRITE_TOOLS.get(dom, [])
    subset = list(dict.fromkeys(subset))
    return _mk_scoped(
        subset,
        f"confirms an action -> previous turn's {'+'.join(domains)} tools "
        f"({len(subset)})",
        # Never FORCE a tool: "yes" can also be confirming something that needs
        # no tool at all, and forcing there turns a fine answer into three
        # rejected attempts (see _mk_scoped's `expect`).
        expect=False, light=False)


def _fragment_continuation(text: str, last_tools: str | None) -> RouteDecision | None:
    """A bare scope fragment ("from yesterday") after a light-read turn ->
    continue the SAME light-read domain on the summarizer, so the follow-up doesn't get
    dumped onto the agent model. `last_tools` is the previous assistant turn's
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
    return _mk_scoped(subset, f"scope follow-up -> continue previous {'+'.join(dict.fromkeys(domains))} read")


# The WRITE tools each domain contributes, added on top of that domain's read
# tools whenever the request also carries a write/compose intent. Kept beside
# the read subsets rather than folded into them so a pure lookup still gets a
# read-only toolset — offering send_email to "what's in my inbox" is both extra
# selection noise and a way to end up sending something nobody asked for.
# Explicit reminder CREATION. Narrower than SCHEDULE_RE on purpose: this is
# only the phrasings where the user is unambiguously asking for a reminder to
# be made, so it is safe to settle the route before the machine-action bailout
# (see _domain_subset) using only the leading verb, ignoring whatever the
# reminder is ABOUT.
from service.reminder_intent import (
    CAPABILITY_INVENTORY_RE as _CAPABILITY_INVENTORY_RE,
    REMINDER_CREATE_RE as _REMINDER_CREATE_RE, has_alert_time,
    asks_alert_time, has_unsupported_alert_clock, is_time_answer,
    is_unsupported_time_answer, resolve_alert_datetime)

# A channel verb inside the reminder's infinitive is the reminder TITLE, not a
# second action to perform now: "create a reminder tomorrow to send my vaccine
# report" asks for one reminder, not a reminder plus an immediate email/text.
# An explicit second clause containing "and" is deliberately excluded; the
# _AND_NOTIFY_* patterns below own cases such as "...and text Mom about it".
_REMINDER_TASK_CHANNEL_ONLY_RE = re.compile(
    r"^(?!.*\band\b)(?=.*(?:\bremind me\b|"
    r"\b(?:send|give)\s+me\s+(?:an?\s+)?reminder\b|"
    r"\b(?:add|create|set|make)\s+(?:an?\s+)?(?:reminder|alarm)\b))"
    r".*\bto\s+(?:send|email|e-mail|text|message|reply|forward)\b", re.I)

# A person named inside the reminder task is not the recipient of an outbound
# message. This shape also needs a schedule lookup before Wisp can ask a useful
# timing question: “before my move-in date” refers to the user's event, while
# “ask Trishy” is simply what the reminder should say.
_EVENT_RELATIVE_REMINDER_RE = re.compile(
    r"\b(?:before|ahead\s+of|prior\s+to)\s+(?:my|the)\s+"
    r"(?:[\w'-]+\s+){0,4}(?:date|day|event|appointment|meeting)\b", re.I)

# A time the user actually NAMED, for deciding whether a reminder/event request
# is complete enough to force the write tool. Complements _LATER_RE (which is
# tuned for scheduled SENDS) with the calendar-date forms people use when
# creating reminders: "before August 20th", "on the 20th", "2026-08-20",
# "tomorrow", "tonight", "this evening".
#
# Deliberately about the PRESENCE of a time, not its value — Wisp does not try
# to resolve it here. The model still parses the time; this only decides whether
# a clarifying question is still legitimate. See the _REMINDER_CREATE_RE route.
_WHEN_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|tommorow|tmrw?|tmrow|yesterday)\b|"
    r"\b(?:this|next|before|by|after|on)\s+(?:the\s+)?"
    r"(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?\b|"
    r"\b(?:this|next|later\s+)?(?:morning|afternoon|evening|night)\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|"
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|"
    r"\bin\s+(?:an?\s+|\d+\s+)?(?:minute|min|hour|day|week|month)s?\b", re.I)

# A trailing "...and tell/let/message/text/email <someone> ..." clause tacked
# onto an otherwise-unrelated primary request. Distinct from SEND_MESSAGE_RE/
# SEND_EMAIL_RE, which are tuned for a STANDALONE send and deliberately
# exclude bare "tell" (too broad out of context — "tell me a joke" isn't a
# send). Gating on a leading "and" narrows it back down: this only fires for
# the second half of a compound request, which is the one place "tell mom" /
# "let mom know" reliably DOES mean "message her", not "explain something to
# me" or a past-tense retelling.
#
# MEASURED FAILURE (2026-08-23, user's debug export): "create a reminder for
# me to finish a canvas assignment by tonight and tell mom about it too" hit
# _REMINDER_CREATE_RE, which — by its own documented design — decides the
# route from the leading verb and ignores what the reminder is ABOUT. That
# scoped the toolset to add_reminder/add_calendar_event/get_upcoming only,
# with tool_choice FORCED (a time was named). send_message was never offered.
# The model wasn't confused about intent; it had no way to act on "tell mom"
# at all, so it did the only thing available and created a SECOND reminder
# titled "Tell mom about canvas assignment" — which never reaches Mom, it
# just reminds the user to do it manually later. The user had to notice and
# correct it two turns later. This regex lets that route widen instead of
# silently dropping the clause it wasn't written to see.
_NOTIFY_TARGET = r"(?:my\s+\w+|him|her|them|mom|dad|[A-Z][a-z]+)"
# Channel-specific variants, checked FIRST so an explicit "and email dad"
# skips the ask-which-channel step below — SEND_EMAIL_RE/SEND_MESSAGE_RE
# don't cover this because their own target lists don't include bare
# "mom"/"dad", only proper nouns/pronouns; _NOTIFY_TARGET above does.
_AND_NOTIFY_TEXT_RE = re.compile(
    rf"\band\s+(?:also\s+)?(?:text|message)\s+{_NOTIFY_TARGET}\b", re.I)
_AND_NOTIFY_EMAIL_RE = re.compile(
    rf"\band\s+(?:also\s+)?email\s+{_NOTIFY_TARGET}\b", re.I)
# The channel-agnostic catch-all — "tell"/"notify"/"let...know" name no
# channel at all, so a match here (that isn't ALSO one of the two above)
# means both tools get offered and the model asks which one.
_AND_NOTIFY_RE = re.compile(
    rf"\band\s+(?:also\s+)?(?:tell|message|text|email|notify)\s+{_NOTIFY_TARGET}\b|"
    rf"\band\s+(?:also\s+)?let\s+{_NOTIFY_TARGET}\s+know\b", re.I)

_DOMAIN_WRITE_TOOLS = {
    "messages": ["lookup_contact", "send_message", "draft_message"],
    "email": ["send_email", "reply_to_email", "draft_email",
              "mark_email_read", "archive_email", "flag_email",
              "forward_email", "trash_file", "unsubscribe"],
    # clear_past_reminders belongs to the WRITE set, not the read one: it is
    # the only way to act on past-due items in bulk, and without it on the
    # calendar write route "delete all my old reminders" had nothing to call
    # but cancel_event — which then fired six times against the user's
    # UPCOMING events (2026-08-18 22:36 log). See its own docstring.
    # complete_reminder is here, alongside cancel_event, because the two mean
    # different things and only one is recoverable: cancelling DELETES the
    # record, completing keeps it and marks it done. Without it on this route
    # "mark the dentist reminder as done" reached a subset whose only matching
    # tool was cancel_event, i.e. the destructive reading of a request that
    # wasn't destructive.
    "calendar": ["add_calendar_event", "add_reminder", "update_reminder",
                 "cancel_event",
                 "complete_reminder", "clear_past_reminders", "clear_reminders",
                 "get_past_events",
                 "update_event",
                 # manage_timers rides along too: "cancel the pasta timer" hits
                 # this same write-intent match ("cancel" + a noun), and a
                 # timer is exactly the kind of thing this route's broad
                 # cancel-detection was built to catch generically.
                 "manage_timers"],
    # Notes had a read tool and no write one until 2026-08-19, so this domain
    # simply had no entry here. "take a note", "add milk to the shopping list"
    # reached a subset of exactly ["search_notes"] and the model either wrote a
    # text file to disk with write_file — which is not where the user keeps
    # notes and does not sync — or said it couldn't.
    "notes": ["create_note", "append_note", "search_notes", "scan_to_note"],
}

# "When am I free" — an availability question, not a schedule read-out. The
# answer is the GAPS between commitments, which find_free_time computes; listing
# the commitments and leaving the model to subtract them is the freehand
# arithmetic this codebase avoids everywhere else. Anchored on free/available/
# gap/open + "fit in", NOT on bare "when", which most calendar reads contain.
# "join my next meeting" is a calendar lookup by every rule above, so it was
# direct-dispatched to get_upcoming — a schedule LISTING — instead of
# join_video_call, which is what "join" actually asked for. Same shape as
# _AVAILABILITY_RE below: the request names the calendar domain but wants a
# different action on it than "read it back to me".
# "tick off the laundry reminder" — completing a NAMED reminder, so it matches
# the calendar-noun regex and lands on the calendar-lookup direct-dispatch
# path, where it needs write-intent to reach complete_reminder the same way
# "mark X as done" already does. "Tick/check/cross off" aren't in
# has_write_intent's verb set. Narrow and local for the same reason
# _NOTE_WRITE_RE/_EMAIL_ACTION_RE are: these verbs have no business arming
# send/delete tools in an unrelated domain.
_COMPLETE_RE = re.compile(
    r"\b(?:tick|check|cross)\s+(?:this|that|it|the\s+\w+(?:\s+\w+){0,2})?\s*off\b|"
    r"\bfinished\s+(?:the|this|that)\b|\bdone\s+with\s+(?:the|this|that)\b|"
    r"\bcomplete\s+(?:the\s+)?[^.?!]{0,48}\breminder\b|"
    r"\bmark\s+(?:the\s+)?[^.?!]{0,48}\breminder\b\s+(?:as\s+)?(?:done|complete)\b",
    re.I)

_JOIN_CALL_RE = re.compile(
    r"\bjoin\s+(?:my|the|this)\b.{0,20}\b(?:meeting|call|standup|zoom)\b|"
    r"\b(?:get\s+me\s+into|let\s+me\s+into)\b.{0,20}\bcall\b", re.I)

_AVAILABILITY_RE = re.compile(
    r"\b(?:am|are)\s+(?:i|we|you)\s+(?:free|available|around|busy)\b|"
    r"\bwhen\s+(?:am|are)\s+(?:i|we|you)\s+(?:free|available|around)\b|"
    r"\b(?:free|available|open)\s+(?:time|slot|slots|window|windows)\b|"
    r"\b(?:any|a)\s+(?:gap|gaps|opening|openings)\b|"
    r"\bfit\s+(?:in|into)\b|\bsquee?ze\s+in\b|"
    r"\bdo\s+i\s+have\s+time\b|\bspare\s+(?:hour|time|slot)\b", re.I)

# A "code" that is a PIN, not a program — door/gate/safe codes, and the
# "the code is 1234" shape. Used to stop CODE_RE claiming these for the coding
# model, which answers them with no tools at all. See its use site.
_PHYSICAL_CODE_RE = re.compile(
    r"\b(?:door|gate|safe|lock|alarm|garage|building|entry|access|gym|locker|"
    r"vault|keypad|pin)\s+code\b|"
    r"\bcode\s+(?:to|for)\s+the\s+(?:door|gate|safe|lock|garage|building|locker)\b|"
    r"\bcode\s+is\s+\d+", re.I)

# Writing a note, in the words people actually use. See the use site in
# _domain_subset for why this is separate from _WRITE_INTENT_RE.
_NOTE_WRITE_RE = re.compile(
    r"\b(?:take|make|start|jot|scribble|log)\s+(?:me\s+)?(?:a|an|this|that|some)?\s*note\b|"
    r"\bjot\s+(?:this|that|it|down)\b|"
    r"\bwrite\s+(?:this|that|it|down)\b|"
    r"\bnote\s+(?:this|that|it)\s+down\b|"
    r"\badd\s+.{1,40}\bto\s+(?:my\s+)?\w*\s*(?:list|note)\b|"
    r"\btack\s+.{1,40}\bonto\s+(?:my\s+)?\w*\s*(?:list|note)\b|"
    r"\bscan\b.{0,20}\binto\s+(?:my\s+)?notes?\b", re.I)

# Flag/forward an email, in the words people actually use. Same reasoning as
# _NOTE_WRITE_RE: "flag"/"fwd" are not verbs _WRITE_INTENT_RE recognizes, so
# without this cue "flag the email... for follow up" reads as a read-only
# lookup — the email domain's WRITE tools never enter the offered subset, and
# the model can literally not call flag_email. What made this a genuine,
# doubly serious bug (not just a missed offer) is what the model did next:
# with no matching tool available, it called view_emails, found the message,
# and then TOLD THE USER "I've flagged that email for follow-up" — a
# fabricated success claim with zero tool call behind it. Verified live
# 2026-08-19. See the SYSTEM prompt's "not every tool result is the answer"
# rule for the adjacent but distinct lesson (that one is about reasoning over
# a result; this is about not claiming an action that never ran).
_EMAIL_ACTION_RE = re.compile(
    r"\b(?:un)?flag\s+(?:this|that|it|the)\b|"
    r"\bmark\s+(?:this|that|it)\s+(?:as\s+)?important\b|"
    r"\bstar\s+(?:this|that|it)\s+(?:email|message)?\b|"
    r"\b(?:fwd|forward)\s+(?:this|that|it)\b", re.I)

# The user asked for a DRAFT, explicitly — "draft a message to mom", "write me
# an email to X", "compose a reply". A draft is reviewed before it goes; a send
# is irreversible. Those are different requests and the difference is the whole
# point of having both tools.
#
# VERIFIED FAILURE 2026-08-18: "can you draft a message to mom with my stock
# movements for this past week" was routed with BOTH draft_message and
# send_message offered, and the model called send_message. The message left the
# machine. The user asked for a draft and got a send — to the wrong person, as
# it happens (see action_tools._own_address_guard).
#
# Same lesson as _CHANNEL_OUTBOUND_TOOLS records directly above: a prompt is
# not a constraint on a small model, the offered toolset is. If the user said
# draft, the send tools do not go on the table at all.
#
# "draft ... and send it" is deliberately NOT a draft: the sentence names the
# send as the goal, so the negative lookahead lets it through to the send tools.
# VERIFIED FAILURE 2026-08-19: "actually send that email you drafted, do NOT
# draft another one" — a negation of drafting, asking for the opposite — still
# matched bare \bdraft\b and stripped send_email from the offered subset. The
# model then correctly picked the best tool it could actually SEE (draft_email)
# and created a second draft, which looked like the model ignoring an explicit
# instruction. It wasn't: send_email was never offered, so there was nothing
# else it could have called. A negative lookbehind for "not"/"don't"/"no need
# to" immediately before the trigger word closes this — narrow on purpose,
# the same way _PHYSICAL_CODE_RE/_NOTE_WRITE_RE are narrow, so an unrelated
# "don't" elsewhere in a long prompt can't also suppress this.
_DRAFT_ONLY_RE = re.compile(
    r"(?:(?<!not\s)(?<!n't\s)(?<!no\s)(?<!no\sneed\sto\s)"
    r"\b(?:draft|compose|write\s+(?:me\s+)?(?:up\s+)?(?:a|an|the))\b"
    r"(?!.*\b(?:and|then)\s+send\b)|"
    r"\bunsent\s+(?:text|message|e-?mail|reply)\b|"
    r"\b(?:without\s+sending|leave\s+(?:it|this|the\s+message)\s+unsent|"
    r"for\s+review(?:\s+only)?|not\s+sent|never\s+(?:a\s+)?send)\b)", re.I)

# send_* removed when _DRAFT_ONLY_RE fires; the draft_* counterpart stays.
_SEND_TOOLS = {"send_message", "send_email", "reply_to_email", "forward_email",
               "schedule_send"}
# The compose-without-sending counterparts. Grouped with _SEND_TOOLS wherever
# the question is "is the model's output the BODY of a message to someone
# else?" — a draft is read by that person too, so it needs the same voice
# treatment as a send even though nothing leaves the machine yet.
_DRAFT_TOOLS = {"draft_message", "draft_email"}

# The user named THEMSELVES as the recipient — "send a message to myself",
# "email me the summary", "text me". Handled the same way as _DRAFT_ONLY_RE,
# and for a related reason:
#
# VERIFIED FAILURE 2026-08-18: "send a message to myself on email with the
# movement of my two stocks" was offered send_email, which correctly refused
# via action_tools._own_address_guard (their own address is for ATTRIBUTION,
# never a destination) — but the guard's recovery text ("call lookup_contact
# with the person's NAME") is actively wrong advice when the person IS the
# user, and the model went on to call lookup_contact(name="Adi"), which
# matched unrelated contacts (Adithya, Jash) instead. The guard should not be
# loosened — it is correctly blocking the ACCIDENTAL case (the model grabbing
# the identity block's email to fill an unrelated slot). This is the
# DELIBERATE case, and it has a tool that is already safe for it BY
# CONSTRUCTION: draft_email opens a real compose window addressed to the
# user's own inbox and never sends — "nothing has been sent, review it and
# hit send" — so self-drafting needs no bypass of anything. Route it there
# the same way _DRAFT_ONLY_RE routes an explicit "draft" request: take
# send_email/send_message off the table so draft_email is what's left.
_SELF_SEND_RE = re.compile(
    r"\bto\s+myself\b|\bmyself\s+(?:on|via|by|through)\b|"
    r"\b(?:email|text|message)\s+(?:it\s+|this\s+|that\s+)?(?:to\s+)?me\b|"
    r"\bsend\s+(?:it|this|that)?\s*to\s+me\b",
    re.I)

# A send that names a FUTURE time ("text mom at 6", "email them Monday
# morning"). Added on top of the domain's ordinary write tools rather than
# replacing them, because the phrasing is genuinely ambiguous — "send this
# tonight" could equally mean "send it now, it's for tonight" — and the model
# should be able to pick. Offering only schedule_send would force the delayed
# reading onto every such sentence.
_SCHEDULED_SEND_TOOLS = ["schedule_send", "list_scheduled_sends",
                         "cancel_scheduled_send"]

# The CONTENT a message is supposed to carry, when that content has to be
# fetched before anything can be sent. Domain scoping builds the subset from
# the domains it recognises — messages, email, calendar, notes — and external
# data is not one of them, so a compound "send X the Y" arrived with the whole
# comms half and no way to obtain Y.
#
# VERIFIED FAILURE 2026-08-18: "can you send a message to my[mom] with the
# movements of my stocks from today" routed to `messages+email read+write ->
# scoped tools (12)`. All twelve were comms tools; get_stock_price was not
# among them. The model's own reasoning was correct — "I need to first get the
# stock prices using the tool" — and then, with no such tool on offer, it
# answered "I can't send messages directly", got nudged, and returned empty
# content twice. The user got a blank reply.
#
# This is the same shape as the 2026-08-10 compose bug (a send with no send
# tool), one layer in: a send whose PAYLOAD has no source. Fixed the same way —
# by giving the route the tool the job actually needs, since the model can only
# call what it is offered.
#
# Deliberately narrow. Each entry is a read-only fetch that costs one schema,
# and it is added only when the turn's own words name that kind of data, so an
# ordinary "text mom I'm running late" is untouched.
_PAYLOAD_TOOLS = [
    (re.compile(r"\b(?:stock|stocks|share|shares|ticker|equit(?:y|ies)|"
                r"portfolio|market)\b|\bstock\s*price", re.I),
     ["get_stock_price"]),
    (re.compile(r"\b(?:weather|forecast|temperature)\b", re.I),
     ["get_weather"]),
    (re.compile(r"\b(?:news|headlines?)\b", re.I),
     ["web_search", "web_fetch"]),
]

_STOCK_PAYLOAD_RE = re.compile(
    r"\b(?:stock|stocks|ticker|tickers|equit(?:y|ies)|portfolio|market)\b|"
    r"\bshares?\s+(?:price|prices|movement|movements|performance)\b|"
    r"\bstock\s*price", re.I)
_WEATHER_PAYLOAD_RE = re.compile(r"\b(?:weather|forecast|temperature|rain|showers?|storm)\b",
                                 re.I)
_CALENDAR_PAYLOAD_RE = re.compile(
    r"\b(?:schedule|calendar|meeting|appointment|deadline|due\s+date|"
    r"move[ -]?in\s+date|availability)\b", re.I)
_PLAIN_TEXT_FILE_RE = re.compile(r"\bplain[ -]?text\s+file\b|\b\.txt\s+file\b", re.I)

# The outbound tools that COMMIT to one channel. Withheld while the user hasn't
# said which channel they meant (see channel_ambiguous in _domain_subset).
#
# VERIFIED FAILURE 2026-08-10: told to "send an update to my mom", with the
# channel unnamed, the route set clarify_channel=True and the system prompt
# duly told the model to ask first — and the model called draft_message anyway,
# without asking. A prompt is not a constraint on a 4B model; the offered
# toolset is. This is the same lesson _mk_scoped's own docstring records about
# tool_choice being "a soft nudge, not a decode-time constraint": if the model
# must not do a thing, do not hand it the thing.
_CHANNEL_OUTBOUND_TOOLS = {"send_message", "draft_message",
                           "send_email", "reply_to_email", "draft_email",
                           "schedule_send"}

# "schedule an email/text/message" — pulled out to its own name so it can
# ALSO be used as a write-intent cue (see the _domain_subset use site), not
# just as one alternative inside _LATER_RE. Reused, not duplicated.
_SCHEDULE_ACTION_RE = re.compile(
    r"\bschedule\s+(?:a\s+|an\s+|the\s+)?(?:email|text|message|send)\b", re.I)

_LATER_RE = re.compile(
    r"\b(?:at|by|on|before|after)\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|"
    r"noon|midnight)|"
    # Weekday, with or without a preposition — "on Monday" AND bare "Monday
    # morning". Requiring the preposition missed the second form, which is the
    # more natural way people actually say it.
    r"\b(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?\b|"
    r"\b(?:this|tomorrow|tonight|later)\s+(?:morning|afternoon|evening|night)\b|"
    r"\b(?:later|tonight|tomorrow|next\s+week|in\s+(?:an?\s+)?"
    r"(?:\d+\s+)?(?:minute|min|hour|day|week)s?)\b|"
    r"\bremind\s+them\b", re.I)
# _LATER_RE's own use site (below) checks _SCHEDULE_ACTION_RE.search(t) too,
# via the `or` folded into its caller — see _domain_subset.

# Asking about / calling off something already queued — a pure queue operation
# with no domain read behind it.
# This rule runs FIRST in _domain_subset, so anything it claims never reaches
# the calendar route — which is why the "what…scheduled" branch is anchored to
# an outbound cue. Bare "what's scheduled" / "what's scheduled today" means
# "what's on my calendar" to almost everyone, and SCHEDULE_RE lists "scheduled"
# as a bare calendar predicate; unanchored, this rule won that disagreement on
# ordering alone and answered a calendar question with the send queue.
_SCHEDULED_QUERY_RE = re.compile(
    # "queued" is safe bare: nothing on a calendar is described as queued.
    r"\b(?:what|anything|which)\b.{0,30}\bqueued\b|"
    # "scheduled" needs the cue on one side or the other — "what emails are
    # scheduled", "what's scheduled to send" — never bare.
    r"\b(?:what|anything|which)\b.{0,30}\b(?:sends?|emails?|texts?|messages?)\b"
    r".{0,20}\bscheduled\b|"
    r"\b(?:what|anything|which)\b.{0,30}\bscheduled\b\s*to\s+(?:send|go\s+out)\b|"
    r"\bscheduled\s+(?:sends?|emails?|texts?|messages?)\b|"
    r"\b(?:cancel|call off|unschedule|don'?t send)\b.{0,30}"
    r"\b(?:scheduled|queued|queue|later|that email|that text|that message)\b|"
    r"\b(?:delete|remove)\b.{0,30}\b(?:scheduled|queued|queue|later)\b|"
    r"\b(?:delete|remove)\b.{0,30}\bfrom\s+(?:the\s+)?queue\b", re.I)


# ---------------------------------------------------------------------------
# COMPOUND-REQUEST CLAIMS
#
# THE STRUCTURAL PROBLEM THIS SOLVES. Every pre-check in _domain_subset and
# rule_route used to be an exclusive early `return`: the FIRST pattern to
# match decided the whole route, and every other clause in the sentence was
# silently dropped. Those patterns all use .search() (unanchored), so a rule
# written for a LEADING clause fires just as happily on a trailing one — and
# then answers for the entire request.
#
# MEASURED (2026-08-23 audit, each verified live through route(), not
# theorized). Every row lost its first clause entirely:
#
#   "remember that I drive a BMW, and check my calendar"     -> memory save lost
#   "lock my screen, and remind me to take my medicine"      -> lock lost
#   "what's queued to send, and also remind me to call X"    -> reminder lost
#   "what's mom's number, and add a reminder to call her"    -> reminder lost
#   "what's new across my apps, and remind me to call X"     -> sweep lost
#   "who do I know named Sarah, and remind me to call her"   -> people query lost
#   "summarize my inbox, and remind me to reply to Dan"      -> summary lost
#   "what do you know about me, and check my email"          -> self query lost
#   "remind me to call X tonight, and check my email"        -> email read lost
#
# WHY NOT MORE PAIRWISE PATCHES. The earlier reminder+notify fix (see
# _AND_NOTIFY_RE) is a hand-written co-occurrence check: it teaches ONE rule
# to look for ONE other domain's tail. That is O(n^2) in domains and only
# ever covers pairs somebody already thought to write down — i.e. it
# guarantees coming back to fix the next combination, and the one after that.
#
# HOW THIS WORKS INSTEAD. Each domain becomes a self-describing CLAIM: a
# predicate plus the tools/flags it would contribute. Every claim is
# evaluated (none short-circuits the others), then:
#
#   0 claims  -> unchanged; the caller falls through exactly as before.
#   1 claim   -> that claim's EXACT former behavior, reproduced field for
#                field (same subset, force, expect, reason, light, direct
#                calls). This is the overwhelmingly common path and must not
#                move — the single-claim tests pin it.
#   2+ claims -> union the tool lists (dedup, order-preserving) and merge the
#                flags per the rules in _merge_claims.
#
# The merge step never names a specific domain, so a claim added later is
# automatically compound-safe against every existing one, and 3- and 4-way
# compounds cost nothing extra. That is the whole point: the table grows,
# the merge logic does not.
@dataclass
class _Claim:
    """One domain's answer to "do I apply, and what would I contribute?"."""
    domain: str
    tools: list[str]
    reason: str
    # Restrict step 0 to this single tool. DROPPED when merged with another
    # claim — forcing one specific tool is actively wrong once two different
    # actions are both required (it makes the second unreachable on step 0).
    force: str | None = None
    # tool_choice="required" on step 0. A merge keeps this only if EVERY
    # claim wanted it: one incomplete half ("remind me to call mom" with no
    # time) still legitimately needs to ask a question rather than be forced.
    expect: bool = True
    light: bool = True
    multi: bool = False
    after: frozenset[str] = frozenset()
    clarify_channel: bool = False
    clarify_target: bool = False
    # Router-resolved calls (see _mk_direct). DROPPED on merge: a
    # pre-dispatched call consumes step 0, which is exactly the selection
    # step a compound request needs in order to reach its other half.
    direct_calls: list[tuple[str, dict]] = field(default_factory=list)
    # Data domains this claim is a MORE SPECIFIC form of, which must therefore
    # not also fire. Load-bearing for menu size: "remind me to pick up milk
    # tonight" trips the calendar domain too (reminders live on the calendar
    # rules), and unioning both turns a deliberate 3-tool reminder menu into
    # 11 tools — straight back through the 5-7 band this file measures as the
    # reliable one (5-7 -> 3/3 correct, all 44 -> 2/3). The old early `return`
    # suppressed the overlap implicitly by never reaching the union; naming it
    # here keeps that property now that the union is always reached.
    suppresses: frozenset[str] = frozenset()


# The email domain's write tools split by WHAT KIND of write they are. A
# compose/notify intent ("...and tell mom about it") justifies the outbound
# three and nothing else — archiving, flagging and trashing mail are a
# different job that a request to message someone never asked for, and
# `trash_file` in particular is destructive. Only consulted on the compound
# path; a single-domain email request still gets the whole list exactly as
# before (see the `len(domains) == 1 and not claims` short-circuit).
# forward_email belongs here, not with the mailbox verbs: forwarding puts a
# mail in someone ELSE'S inbox, so "send this to my accountant" must reach it.
# Grouping it as mailbox management made that alias unreachable — caught by
# tests/test_alias_reachability.py.
_EMAIL_OUTBOUND_WRITE = ["send_email", "reply_to_email", "draft_email",
                         "forward_email"]


def _domain_writes(domain: str, t: str) -> bool:
    """Does the write intent in `t` actually belong to `domain`?

    Only consulted when a request names SEVERAL domains — see the call site
    for why a single-domain request keeps using the global `has_write_intent`
    verbatim. Each cue is an existing, already-measured pattern rather than a
    new regex, so this cannot drift away from the matcher that put the domain
    in the subset in the first place:

      messages/email — their own send regexes, plus `_COMPOSE_RE` (a send
        naming a recipient with no channel noun) and the notify-tail patterns
        from the reminder+notify fix.
      email          — also `_EMAIL_ACTION_RE`, which covers the mailbox
        verbs that are writes without being sends (archive/flag/mark read).
      calendar       — `_CALENDAR_WRITE_RE` (the narrow object-anchored slice
        of SCHEDULE_RE that is unambiguously a write), `_COMPLETE_RE`, and
        `_REMINDER_CREATE_RE`.
      notes          — `_NOTE_WRITE_RE` ("take a note", "jot this down").

    A domain with no entry (inbound / todo / planning / past / scheduled) has
    no write tools of its own in `_DOMAIN_WRITE_TOOLS`, so it falls through to
    True and changes nothing.
    """
    cues = {
        "messages": (SEND_MESSAGE_RE, _COMPOSE_RE,
                     _AND_NOTIFY_TEXT_RE, _AND_NOTIFY_RE),
        "email": (SEND_EMAIL_RE, _EMAIL_ACTION_RE, _COMPOSE_RE,
                  _AND_NOTIFY_EMAIL_RE, _AND_NOTIFY_RE),
        "calendar": (_CALENDAR_WRITE_RE, _COMPLETE_RE, _REMINDER_CREATE_RE),
        "notes": (_NOTE_WRITE_RE, _WRITE_INTENT_RE),
    }.get(domain)
    if cues is None:
        return True
    return any(p.search(t) for p in cues)


def _merge_claims(claims: list[_Claim]) -> RouteDecision | None:
    """Fold every matched claim into one decision. See the block comment above.

    Single-claim routes reproduce their old behavior exactly; only a genuine
    2+-claim compound takes the merging path.
    """
    if not claims:
        return None
    if len(claims) == 1:
        c = claims[0]
        if c.direct_calls:
            return _mk_direct(c.direct_calls, c.reason, light=c.light)
        return _mk_scoped(c.tools, c.reason, force=c.force, expect=c.expect,
                          light=c.light, multi=c.multi, after=c.after or None,
                          clarify_channel=c.clarify_channel,
                          clarify_target=c.clarify_target)
    tools: list[str] = []
    for c in claims:
        # A direct-call claim still contributes its TOOLS on the merge path —
        # the call itself is dropped (it would consume step 0) but the model
        # must still be able to reach that tool by choosing it.
        tools += c.tools or [n for n, _ in c.direct_calls]
    tools = list(dict.fromkeys(tools))
    domains = "+".join(dict.fromkeys(c.domain for c in claims))
    after: set[str] = set()
    for c in claims:
        after |= set(c.after)
    return _mk_scoped(
        tools,
        f"{domains} (compound) -> scoped tools ({len(tools)})",
        # force/direct_calls deliberately dropped — see _Claim's field notes.
        force=None,
        # Every half must have wanted forcing, or an incomplete half loses its
        # ability to ask for what it's missing.
        expect=all(c.expect for c in claims),
        # A compound request is a multi-step job by construction: its halves
        # are complementary, never alternatives. This is exactly what
        # multi_round means (see RouteDecision.multi_round), and it also
        # stops the broad narration gate firing after only the first half.
        multi=True,
        after=frozenset(after) or None,
        # A clarify flag is a genuine open question about one half; it stays
        # open regardless of what the other half is.
        clarify_channel=any(c.clarify_channel for c in claims),
        clarify_target=any(c.clarify_target for c in claims),
        # The warm read-out voice only fits when EVERY half is such a read.
        light=all(c.light for c in claims),
    )


def _notes_reminder_read_sources(text: str) -> list[str]:
    """Read sources, not source-looking nouns inside a search query.

    Keep this discriminator local to the Notes/Reminders overlap. A shared
    source head and independently requested read clauses still require both.
    """
    unquoted = re.sub(r'''"[^"\n]*"|“[^”\n]*”|(?<!\w)'[^'\n]*'(?!\w)|‘[^’\n]*’''', " ", text)
    clauses = re.split(
        r"(?:[,;]\s*|\band\s+|\balso\s+)(?=(?:please\s+)?"
        r"(?:search|find|check|show|list|look)\b)", unquoted, flags=re.I)
    sources: list[str] = []
    for clause in clauses:
        # 'find reminder ideas in my notes' explicitly locates the search;
        # 'reminder' before that location is content, not another obligation.
        location = re.search(
            r"\b(?:in|from)\s+(?:(?:my|the|our|apple)\s+)*"
            r"(?P<sources>(?:notes?|reminders?)(?:\s+and\s+"
            r"(?:(?:my|the|our|apple)\s+)*(?:notes?|reminders?))?)\b", clause, re.I)
        head = re.split(
            r"\b(?:for|about|containing|matching|named|titled)\b", clause,
            maxsplit=1, flags=re.I)[0]
        # A source directly following the read verb is authoritative; later
        # 'from my reminders' can be part of the Notes query itself. Otherwise
        # a trailing location disambiguates 'find reminder ideas in my notes'.
        explicit_head = re.search(
            r"\b(?:search|find|check|show|list|look\s+(?:in|through))\s+"
            r"(?:(?:my|the|our|apple)\s+)*(?:notes?|reminders?)"
            r"(?:\s+and\s+(?:(?:my|the|our|apple)\s+)*(?:notes?|reminders?))*\s*$", head, re.I)
        if location and not (explicit_head and len(head) < len(clause)):
            head = location["sources"]
        for match in re.finditer(r"\b(notes?|reminders?)\b", head, re.I):
            name = "search_notes" if match[1].lower().startswith("note") else "search_reminders"
            if name not in sources:
                sources.append(name)
    return sources


def _domain_subset(t: str, pre_claims: list[_Claim] | None = None) -> RouteDecision | None:
    """Messages/email/calendar/notes requests -> a toolset scoped to the
    domains the request actually names.

    `pre_claims` carries claims collected by rule_route BEFORE this runs (the
    memory / self-query / people / recency / device routes, which otherwise
    sit AFTER the `_domain_subset` call and so were unreachable once a data
    domain matched — audit rows 1, 2, 5, 6, 8). They participate in exactly
    the same merge as the claims built here; passing them in rather than
    merging afterwards means a single shared decision, so `suppresses` and
    the single-vs-compound distinction still work.

    This used to be `_light_read`: a READ-ONLY route whose real job was picking
    the small always-warm model so a big one could stay asleep. Both halves of
    that are now wrong. Every chat role resolves to the same resident model, so
    there is no swap to avoid — and the read-only half was actively breaking
    compound requests.

    The bug it caused, from a real session: "write an email with my email and
    text summaries and send it to <addr>" matched the email+messages READ
    domains, was handed exactly [view_emails, summarize_emails, view_messages,
    summarize_messages], and spent 212s composing an email it then could not
    send — `send_email` was never on the list. The model was blamed for
    ignoring the instruction; it had simply never been shown the tool.

    So the subset now unions EVERY domain the request names AND adds that
    domain's write tools when a write intent is present, instead of bailing out
    to the full 44-tool set (measured worse: 2/3, reached for show_profile) or
    to a read-only slice (0/3 able to send). Compound intent gets compound
    tools.
    """
    # "remind me to <anything>" is reminder CREATION no matter what the
    # <anything> is, so it is settled before the machine-action bailout below.
    # Without this, the reminder's own CONTENT decides the route: "remind me to
    # call mom" trips STRONG_ACTION_RE on "call", bails to the undifferentiated
    # 44-tool set, and has to rediscover add_reminder among everything Wisp can
    # do. Same for "remind me to open the report", "remind me to email John".
    # The verb belongs to the future task, not to this request.
    # "what's queued to send?" / "cancel that text" — the queue itself is the
    # subject, with no inbox or thread to read first.
    # These three pre-checks are CLAIMS, not exclusive returns — see the
    # _Claim block comment above for the nine measured cases where an early
    # return silently swallowed the rest of the sentence. Each still produces
    # byte-identical behavior when it is the only claim; they only combine
    # when the request genuinely spans several domains.
    claims: list[_Claim] = list(pre_claims or [])
    # External/file payloads are ordinary mergeable claims. Keeping them in
    # the same collection as reminders and personal-data domains prevents an
    # email/reminder early route from swallowing the source half of a compound
    # request (PDF -> email, weather -> reminder, stock -> message).
    if _STOCK_PAYLOAD_RE.search(t):
        claims.append(_Claim("stock_payload", ["get_stock_price"],
                             "stock payload -> get_stock_price", light=False))
    if (_WEATHER_PAYLOAD_RE.search(t)
            and (_REMINDER_CREATE_RE.search(t) or _COMPOSE_RE.search(t)
                 or _CALENDAR_NOUN_RE.search(t))):
        claims.append(_Claim("weather_payload", ["get_weather"],
                             "weather payload -> get_weather", light=False))
    if _PLAIN_TEXT_FILE_RE.search(t) and _NOTES_INTENT_RE.search(t):
        claims.append(_Claim("plain_text_file", ["write_file"],
                             "plain-text output -> write_file", light=False))
    elif (_DOCUMENT_RE.search(t)
          and (SEND_EMAIL_RE.search(t) or _DRAFT_ONLY_RE.search(t)
               or _COMPOSE_RE.search(t))):
        claims.append(_Claim("document_payload", ["find_files", "read_file"],
                             "document payload -> find/read file", light=False,
                             multi=True, after=frozenset({"read_file"})))
    if _SCHEDULED_QUERY_RE.search(t):
        claims.append(_Claim(
            "scheduled_queue", list(_SCHEDULED_SEND_TOOLS),
            "scheduled-send queue -> scoped tools (3)",
            # The QUEUE itself is the whole subject — "cancel that text" and
            # "what's queued to send" name no inbox or thread to read. Their
            # own words ("cancel", "send") nonetheless trip the calendar-write
            # and compose domains, which the old early `return` suppressed by
            # never reaching them; verified by before/after diff, without this
            # both grew from 3 tools to 14 and one gained a spurious
            # ask-which-channel prompt.
            suppresses=frozenset({"calendar", "messages", "email"})))
    # A contact lookup, before any email/messages noun inside it can claim the
    # request (see _CONTACT_LOOKUP_RE). Not when it's really a send.
    if (_CONTACT_LOOKUP_RE.search(t)
            and not (SEND_MESSAGE_RE.search(t) or SEND_EMAIL_RE.search(t)
                     or _COMPOSE_RE.search(t))):
        claims.append(_Claim(
            "contact_lookup", list(_CONTACT_TOOLS),
            f"contact lookup -> scoped tools ({len(_CONTACT_TOOLS)})",
            light=False))
    if _REMINDER_CREATE_RE.search(t):
        # WHEN THE REQUEST NAMES A TIME, FORCE THE TOOL.
        #
        # `expect=False` is right for "remind me to call mom" — add_reminder
        # requires `when_iso`, no time was given, and the model should be free
        # to ask (see _mk_scoped's `expect`). It is WRONG once the user has
        # supplied the time, and the failure is the worst kind:
        #
        # Verified 2026-08-18 on "set a reminder for me to come up with
        # questions for when I meet the governer of california on thursday
        # evening" — tool_choice stayed "auto", the model called NOTHING, and
        # then answered "Here's your reminder set up: Thursday, Aug 20, 6:00 PM".
        # It reasoned its way to a time, described the reminder it would create,
        # and created nothing. The user is told it exists. Same turn shape as
        # "create reminders before August 20th for dorm move in date selection",
        # which called only get_upcoming and likewise created nothing.
        #
        # A claimed write that never happened is worse than a clarifying
        # question AND worse than a wrong time — the user has no way to tell it
        # apart from success, and finds out when the reminder never fires.
        timed = bool(_LATER_RE.search(t) or _WHEN_RE.search(t))
        reminder_tools = ["add_reminder", "add_calendar_event", "get_upcoming"]

        # A second clause naming someone to notify ("...and tell mom about it
        # too") must not be silently dropped just because the LEADING clause
        # was a reminder — see _AND_NOTIFY_RE's docstring for the incident
        # this closes. Widen rather than re-route entirely: the reminder
        # still needs creating in the SAME turn, so this stays `multi=True`
        # rather than picking one domain over the other.
        channel_is_task = bool(_REMINDER_TASK_CHANNEL_ONLY_RE.search(t))
        explicit_channel = _outbound_channel(t)
        wants_text = bool(not channel_is_task
                          and (explicit_channel == "messages"
                               or SEND_MESSAGE_RE.search(t)
                               or _AND_NOTIFY_TEXT_RE.search(t)))
        wants_email = bool(not channel_is_task
                           and (explicit_channel == "email"
                                or SEND_EMAIL_RE.search(t)
                                or _AND_NOTIFY_EMAIL_RE.search(t)))
        if wants_text or wants_email or _AND_NOTIFY_RE.search(t):
            # Channel named explicitly -> only that channel's tools, no
            # clarify hint (the user already told us). Neither named (the
            # common "tell mom" / "let mom know" case, which is channel-
            # agnostic) -> offer both and let the model ask, same contract
            # _CLARIFY_CHANNEL_HINT already uses elsewhere.
            if wants_text and not wants_email:
                notify_tools = ["lookup_contact", "send_message", "draft_message"]
            elif wants_email and not wants_text:
                notify_tools = ["lookup_contact", "send_email", "draft_email"]
            else:
                notify_tools = ["lookup_contact", "send_message", "draft_message",
                                "send_email", "draft_email"]
            subset = reminder_tools + notify_tools
            claims.append(_Claim(
                "reminder_notify", subset,
                "reminder creation + notify -> scoped tools "
                f"({len(subset)})"
                + (" [time named -> forced]" if timed else ""),
                expect=timed, multi=True, light=False,
                clarify_channel=not (wants_text or wants_email),
                suppresses=frozenset({"calendar", "messages", "email"})))
        else:
            suppressed = ({"calendar", "messages", "email"}
                          if channel_is_task else {"calendar"})
            if (re.search(r"\bto\s+(?:append|add)\b[^.?!]{0,100}\bnotes?\b", t, re.I)
                    and not re.search(r"\band\b", t, re.I)):
                # The note edit is the future reminder's content, not a
                # request to edit Notes now. Separate clauses remain intact.
                suppressed.add("notes")
            claims.append(_Claim(
                "reminder", reminder_tools,
                "reminder creation -> scoped tools (3)"
                + (" [time named -> forced]" if timed else ""),
                expect=timed, light=False, multi=True,
                suppresses=frozenset(suppressed)))
    # An unambiguous machine ACTION ("open/launch/quit Notes", "what's on my
    # screen") must reach the full agent toolset, even though it may mention a
    # data noun like "notes" or "screen" that would otherwise trip a domain.
    #
    # `and not claims` is load-bearing in BOTH directions.
    #
    # Without it (the old `return None`), this bailout is unreachable for a
    # reminder — _REMINDER_CREATE_RE returned before it — which is precisely
    # what the ordering was protecting: "remind me to CALL mom" trips
    # STRONG_ACTION_RE on the reminder's own CONTENT verb, and bailing to the
    # unscoped route there is the documented bug this file already fixed once
    # ("The verb belongs to the future task, not to this request").
    #
    # But now that the reminder is a claim rather than a return, control
    # reaches here, and an unconditional bailout would resurrect that exact
    # bug. Gating on `not claims` keeps both properties: with no claim the
    # behavior is byte-identical to before, and with one the request carries
    # on to the data-domain union below so a genuinely separate second clause
    # ("...and check my email" — audit rows 7 and 9) can be claimed too
    # instead of being swallowed by a content verb.
    if STRONG_ACTION_RE.search(t) and not claims:
        return None
    # A code-authoring request ("write a regex for email validation") is coding,
    # not a data read — the incidental "email"/"note" noun must not route it to
    # the inbox/notes summary tools.
    if CODE_AUTHOR_RE.search(t) and CODE_RE.search(t) and not claims:
        return None
    # Does this request WRITE, not just read? Checked once here and applied
    # per-domain below. These two used to be early `return None` bailouts, which
    # is precisely what made a compound "summarize X and send it" fall through
    # to the undifferentiated 44-tool route instead of getting both halves of
    # the tools it needed.
    # NOT SCHEDULE_RE: that one deliberately spans reads AND writes ("what's on
    # my calendar" matches it just as "cancel my lunch" does), so including it
    # here marked every calendar LOOKUP as a write and offered cancel_event to
    # "what's on my calendar today". _WRITE_INTENT_RE draws the line correctly —
    # add/cancel/remind true, pure reads false — so calendar writes are covered
    # without arming a destructive tool on a read.
    # `has_write_intent` IS this test — it was pulled out of here so the
    # semantic fallback and the eval harness ask the question the same way, but
    # the copy left behind here then had to be kept in step by hand. Call it.
    writing = has_write_intent(t)
    # "tick off the laundry reminder" — completing a NAMED reminder is a write
    # (complete_reminder), but has_write_intent's verb set doesn't include
    # tick/check/cross-off. Set HERE, before the calendar direct-dispatch
    # decision below, because that decision reads `writing` directly — unlike
    # _NOTE_WRITE_RE/_EMAIL_ACTION_RE below, which only need to affect the
    # later fallthrough subset and can afford to run after it.
    if _COMPLETE_RE.search(t):
        writing = True
    # Union ALL named domains, not just the first match — a COMPOUND request
    # ("what's on my messages and calendar") needs BOTH domains' tools in the
    # advertised subset. Previously this returned on the first hit (messages),
    # so get_upcoming was never offered and the calendar half was answered only
    # if the model happened to recall the tool from the system prompt — i.e.
    # unreliably ("it ignores the calendar").
    subset: list[str] = []
    domains: list[str] = []
    # Set True by any branch below whose tools are SEQUENTIAL/COMPLEMENTARY
    # rather than alternatives — see RouteDecision.multi_round. Threaded through
    # to the single _mk_scoped call at the end of this function, since every
    # branch here contributes to one shared subset/route rather than returning
    # early.
    multi = False
    # The tools that make a `multi` route provably done — see
    # RouteDecision.narration_after. Accumulated alongside `multi` by the same
    # branches, and unioned when more than one fires (a prompt that is both a
    # past-calendar question and a to-do question genuinely needs all of them).
    after: set[str] = set()
    # Domains a pre-check claim is a more specific form of, and which must
    # therefore not fire alongside it — see _Claim.suppresses.
    _suppressed = {d for c in claims for d in c.suppresses}
    if (("messages" not in _suppressed)
            and ((_MESSAGES_INTENT_RE.search(_TEXT_NONSMS_RE.sub(" ", t))
                  and not _MESSAGE_NONSMS_RE.search(t))
                 or SEND_MESSAGE_RE.search(t))):
        subset += ["view_messages", "summarize_messages"]; domains.append("messages")
    if ("email" not in _suppressed) and (EMAIL_RE.search(t) or SEND_EMAIL_RE.search(t)):
        # scan_subscriptions/summarize_thread/triage_inbox/unsubscribe joined
        # 2026-08-19 — same reachability trap as everywhere else in this file:
        # this rule claims any email-shaped phrasing ("what marketing emails am
        # I getting", "sort my inbox by priority"), so those four tools were
        # registered, aliased, and still unreachable without this.
        subset += ["view_emails", "summarize_emails", "scan_subscriptions",
                  "summarize_thread", "triage_inbox", "unsubscribe"]
        domains.append("email")
    # Same subtraction idiom as the messages branch above: strip the phrases
    # where "note" is the thing being SENT (see _OUTBOUND_NOTE) before asking
    # whether the request is about Notes.app, so "shoot my professor a note"
    # contributes no notes domain while "check my notes and shoot her a note"
    # still contributes one.
    if ("notes" not in _suppressed
            and _NOTES_INTENT_RE.search(_OUTBOUND_NOTE_RE.sub(" ", t))):
        subset += ["search_notes"]; domains.append("notes")
    if (("calendar" not in _suppressed)
            and (_CALENDAR_READ_RE.search(t) or _CALENDAR_NOUN_RE.search(t)
                 or SCHEDULE_RE.search(t))):
        # find_free_time rides along with every calendar read. "When am I free
        # this week" is a calendar LOOKUP by every rule here, so it never
        # reached the semantic fallback, and a subset of exactly ["get_upcoming"]
        # left the model to eyeball gaps out of a list of appointments — which
        # is arithmetic it should not be doing (see conversions.calculate).
        # One extra schema on calendar reads is a cheap price for that.
        subset += ["get_upcoming", "find_free_time", "join_video_call"]; domains.append("calendar")
        # Offer the past-events tool ALONGSIDE get_upcoming rather than instead
        # of it: "what did I have yesterday and what's next" is one question
        # spanning both directions, and the agent prompt already tells the
        # model which of the two covers which direction. multi_round=True: a
        # question spanning both directions may genuinely need BOTH calls, so
        # the broad narration mode (which stops thinking once ANY offered tool
        # has answered) must not fire after only one of the two.
        if _CALENDAR_PAST_RE.search(t):
            subset += ["get_past_events"]; domains.append("past"); multi = True
            # Both directions answered = the question is fully covered, so the
            # model may narrate from there without thinking.
            after |= {"get_upcoming", "get_past_events"}
    # Inbound "from <someone>" with NO channel named -> check both email + texts
    # (see _INBOUND_RE). Only adds channels not already matched above.
    #
    # "with no channel named" is the whole point, and the code used to ignore it:
    # _INBOUND_RE's last alternative matches "any new MESSAGES from …", so
    # "any messages from mom" — which names the channel out loud — was widened to
    # the email tools as well. "Messages" means the Messages app (iMessage/SMS),
    # full stop; offering summarize_emails there invites answering a text
    # question out of the inbox. Same in reverse for "any emails from mom".
    named_messages = "messages" in domains
    named_email = "email" in domains
    if _INBOUND_RE.search(t) and not (named_messages ^ named_email):
        for tool in ("view_emails", "summarize_emails", "view_messages", "summarize_messages"):
            if tool not in subset:
                subset.append(tool)
        if "inbound" not in domains:
            domains.append("inbound")
        if writing and "email" not in domains:
            domains.append("email")
        if writing and "messages" not in domains:
            domains.append("messages")
    # Day planning, grounded in the real schedule + commitments (see
    # _PLANNING_RE), and to-do/task questions (see _TODO_RE). Both are
    # AGGREGATE: the answer isn't in any one place, so they contribute every
    # read source rather than a favoured pair. Planning used to contribute only
    # calendar + messages, which silently excluded notes and mail from "help me
    # plan my day" for the same reason the to-do bug happened.
    if _TODO_RE.search(t) or _PLANNING_RE.search(t):
        # daily_brief rides along as a single-call alternative to calling all
        # four _ALL_SOURCES separately — "what do I need to know this morning"
        # is exactly the composite question it exists to answer in one shot
        # instead of four, which also means four fewer decode-dominated steps
        # (see [[moe-decode-dominates-latency]]) when the model reaches for it.
        subset += [*_ALL_SOURCES, "daily_brief"]
        domains.append("todo" if _TODO_RE.search(t) else "planning")
        multi = True   # see RouteDecision.multi_round — all four sources are required
        # …and once all four HAVE answered, the requirement is met and the
        # remaining work is pure synthesis. See RouteDecision.narration_after:
        # measured 2,415 chars of reasoning / 14.85s on exactly this step.
        after |= set(_ALL_SOURCES)
    # A send that names no CHANNEL: "send this to mom tonight", "forward it to
    # my boss". There's a clear write intent and a recipient but no "email"/
    # "text" noun for the domain matchers above to catch, so the subset came
    # back empty and the request fell through to the full 44-tool route — the
    # exact shape that measures worst at tool selection. Offer both channels
    # and let the model choose; "to mom" and "to my boss" genuinely don't say
    # which, and the read tools come along so it can check how they usually
    # talk to that person.
    # VERIFIED FAILURE 2026-08-10: "tell my mom about my schedule for this
    # week" matches _COMPOSE_RE (a send naming a recipient) AND "schedule"
    # trips the calendar domain above — so `subset` was already non-empty by
    # the time this ran, and the old `if not subset and ...` gate below never
    # fired. The user got read+write CALENDAR tools only (get_upcoming,
    # add_calendar_event, add_reminder, cancel_event) with no way to actually
    # deliver anything. The model's own reasoning said "I don't have a
    # messaging tool" — correct given what it was offered; it wasn't
    # forgetting a capability, the router just never granted it. Any domain
    # noun incidental to a compose sentence (schedule/notes/etc.) can trigger
    # the same shape of bug, so this now fires independent of `subset`,
    # gated only on: writing, COMPOSE_RE matched, and no channel already
    # claimed (so a genuine "email X about Y" isn't touched/duplicated).
    # Neither channel was NAMED, so the model would otherwise have to guess
    # which app to use — surfaced below as an explicit "ask, don't guess"
    # directive (see RouteDecision.clarify_channel) instead of leaving it to
    # pick silently, since the wrong guess sends the message on the wrong app.
    channel_ambiguous = False
    if (writing and _COMPOSE_RE.search(t)
            and not (named_messages or named_email)
            and not (_suppressed & {"messages", "email"})):
        for tool in ("view_messages", "summarize_messages",
                    "view_emails", "summarize_emails"):
            if tool not in subset:
                subset.append(tool)
        if "messages" not in domains:
            domains.append("messages")
        if "email" not in domains:
            domains.append("email")
        channel_ambiguous = True
    # The CONTENT counterpart to channel_ambiguous above: a compose sentence
    # that names its topic ("about my move-in date") rather than dictating
    # the message, where nothing so far added a fact-lookup tool because
    # "date" isn't a domain noun any rule above recognizes. See
    # _TOPIC_LOOKUP_RE's comment for the two live failures this closes.
    # get_upcoming is biased calendar-first deliberately — every observed
    # case so far was date/schedule-shaped, and this user's own prompt rules
    # already say to weight Notes lightest — so it's the one tool worth
    # FORCING (below) rather than just offering; search_notes only rides
    # along as an available fallback if get_upcoming comes back empty.
    needs_calendar_topic_lookup = (
        writing and _COMPOSE_RE.search(t) and _CALENDAR_PAYLOAD_RE.search(t)
        and not (_suppressed & {"calendar", "notes"}))
    explicit_payload = any(c.domain in {
        "stock_payload", "weather_payload", "document_payload", "plain_text_file"
    } for c in claims)
    needs_generic_topic_lookup = (
        writing and _COMPOSE_RE.search(t) and _TOPIC_LOOKUP_RE.search(t)
        and not needs_calendar_topic_lookup and not explicit_payload
        and not (_suppressed & {"notes", "messages"}))
    topic_force: str | None = None
    if needs_calendar_topic_lookup:
        fallback_tools = ["get_upcoming", "search_notes"]
        if re.search(r"\bmove[ -]?in\b", t, re.I):
            fallback_tools += ["view_messages", "search_conversations"]
        for tool in fallback_tools:
            if tool not in subset:
                subset.append(tool)
        if "calendar" not in domains:
            domains.append("calendar")
        if "notes" not in domains:
            domains.append("notes")
        multi = True
        topic_force = "get_upcoming"
    elif needs_generic_topic_lookup:
        # “Email my boss about the project I have been working on” needs
        # project evidence, but it is not a calendar query.  Search durable
        # notes/conversations instead of guessing the message or forcing an
        # unrelated get_upcoming call.  Structured payload claims above
        # (stock/weather/files) keep their own purpose-built source tools.
        for tool in ("search_notes", "search_conversations"):
            if tool not in subset:
                subset.append(tool)
        if "notes" not in domains:
            domains.append("notes")
        if "messages" not in domains:
            domains.append("messages")
        multi = True
        topic_force = "search_notes"
        after |= {"get_upcoming", "search_notes"}
    # No data-domain matched. Any claim collected by the pre-checks above is
    # still the answer (previously `return None` threw those away — audit rows
    # 3, 4, 6: "what's queued to send, and also remind me to call X").
    if not subset:
        return _merge_claims(claims)
    # A pure calendar LOOKUP and nothing else -> call get_upcoming here, with the
    # window resolved in Python (see _calendar_window_days). Every condition
    # below is load-bearing:
    #   - `domains == ["calendar"]` — a compound read ("my messages and
    #     calendar") needs the model to call the other sources too, and step 0
    #     stops being a selection step once a direct call has run;
    #   - `not writing` — add/cancel/reschedule need the write tools chosen by
    #     the model, not a read pre-dispatched in their place;
    #   - `not multi` — set by the past-events branch above, which means
    #     get_past_events may also be required, and get_upcoming only ever
    #     returns FUTURE items.
    #   - `not _AVAILABILITY_RE` — "when am I free this week" is a calendar
    #     lookup by every rule above, so it was direct-dispatched to
    #     get_upcoming and the model was left to eyeball GAPS out of a list of
    #     appointments. That is arithmetic over times, which is exactly what
    #     this codebase does not let the model do freehand (see
    #     conversions.calculate). find_free_time computes them; it only gets the
    #     chance if this dispatch declines.
    #   - `not claims` — same reason as `domains == ["calendar"]` on the line
    #     above, for the pre-check domains rather than the data ones: a
    #     direct call consumes step 0, so pre-dispatching here would strand
    #     the other half of a compound request with no selection step left to
    #     reach its tools. This is audit row 1 ("remember that I drive a BMW,
    #     and check my calendar" lost the memory save exactly this way).
    if ("calendar" in domains and set(domains) <= {"calendar", "past", "notes"}
            and not claims and not writing and not _NOTE_WRITE_RE.search(t)
            and re.search(r"\breminders?\b", t, re.I)
            and not re.search(r"\b(?:calendar|events?|meetings?)\b", t, re.I)
            and not _AVAILABILITY_RE.search(t) and not _JOIN_CALL_RE.search(t)):
        # Includes overdue active reminders; even the generic past-calendar
        # branch cannot substitute for this reminder-only source.
        sources = _notes_reminder_read_sources(t) or ["search_reminders"]
        d = _mk_scoped(sources, "Notes/Reminders source lookup -> " + ", ".join(sources),
                       force=sources[0], multi=len(sources) > 1)
        if len(sources) > 1:
            d.required_tool_groups = tuple(frozenset({source}) for source in sources)
        return d
    if (domains == ["calendar"] and not claims and not writing and not multi
            and not _AVAILABILITY_RE.search(t) and not _JOIN_CALL_RE.search(t)):
        days = _calendar_window_days(t)
        return _mk_direct([("get_upcoming", {"days": days} if days else {})],
                          "calendar lookup -> get_upcoming (router-direct"
                          + (f", {days}d)" if days else ")"))
    # Add each matched domain's write tools when the request actually writes.
    # This is the whole fix: a compound "summarize my email and send it to X"
    # now carries send_email alongside the summarizers, instead of being handed
    # a read-only subset and left to narrate an email it cannot deliver.
    # "take a note", "jot this down", "write that down" are unmistakably writes
    # and contain none of _WRITE_INTENT_RE's verbs, so the notes domain arrived
    # here read-only and offered `search_notes` to a request to CREATE a note.
    #
    # Handled as a local cue rather than by widening _WRITE_INTENT_RE: that
    # pattern gates every domain, its comments record several incidents from it
    # matching too eagerly on reads, and "take"/"jot"/"write down" have no
    # business arming send or delete tools elsewhere.
    if "notes" in domains and _NOTE_WRITE_RE.search(t):
        writing = True
    if "email" in domains and _EMAIL_ACTION_RE.search(t):
        writing = True
    # "schedule an email/text/message to go out later" — VERIFIED FAILURE
    # 2026-08-19 (live testing): this is _LATER_RE's OWN pattern (it already
    # matches "schedule an email"), but _LATER_RE only ever ADDS
    # _SCHEDULED_SEND_TOOLS below — it can't set `writing` itself, and bare
    # "schedule" is not in _WRITE_INTENT_RE's verb list ("reschedule" and
    # "postpone" are, "schedule" the base form isn't). So the whole request
    # landed on a read-only email-lookup subset with schedule_send nowhere in
    # it — the tool existed, was reachable in every other way, and was still
    # never offered for the one phrasing it's actually FOR. Reusing
    # _SCHEDULE_ACTION_RE (the same narrow "schedule + email/text/message/
    # send" shape already proven inside _LATER_RE) rather than widening the
    # shared _WRITE_INTENT_RE, which would also flag "what's the schedule
    # for today" — a bare noun, not a request to schedule anything.
    if _SCHEDULE_ACTION_RE.search(t):
        writing = True
    if writing:
        for dom in list(domains):
            # PER-DOMAIN write intent. `writing` asks the question of the
            # WHOLE sentence, which is right while the sentence names one
            # domain and wrong the moment it names two: "remind me to call
            # the dentist tonight, and check my email" is a write (the
            # reminder) AND a read (the email), but one global flag armed all
            # nine email write tools — send_email, reply_to_email,
            # archive_email, trash_file — on a request that only ever asked
            # to LOOK at the inbox. Measured before this: 20 tools, well past
            # the 5-7 band this project measures as reliable (5-7 -> 3/3
            # correct, all 44 -> 2/3), and several of them destructive.
            #
            # `len(domains) == 1` short-circuits to the old behavior on
            # purpose: with a single domain the global intent IS that
            # domain's intent, there is nowhere else it could belong, so
            # every existing single-domain route keeps its exact toolset
            # (verified byte-identical by tests/test_compound_claims.py and
            # the before/after route diff).
            # `not claims` matters as much as the domain count: a pre-check
            # claim (a reminder, a memory save) is another domain in the same
            # decision even though it never appears in `domains`, so its write
            # intent must not short-circuit this test either.
            if len(domains) == 1 and not claims:
                subset += _DOMAIN_WRITE_TOOLS.get(dom, [])
            elif _domain_writes(dom, t):
                # Compound path: contribute only the KIND of write the
                # sentence actually asked for. See _EMAIL_OUTBOUND_WRITE.
                if dom == "email" and not _EMAIL_ACTION_RE.search(t):
                    subset += _EMAIL_OUTBOUND_WRITE
                else:
                    subset += _DOMAIN_WRITE_TOOLS.get(dom, [])
        # A send with a future time attached also gets the scheduling tools.
        if ((_LATER_RE.search(t) or _SCHEDULE_ACTION_RE.search(t))
                and ("email" in domains or "messages" in domains)):
            subset += _SCHEDULED_SEND_TOOLS
            domains.append("scheduled")
    # Whatever the message is supposed to CONTAIN, when it has to be fetched
    # first — see _PAYLOAD_TOOLS for the turn that died with no way to get it.
    for pattern, payload in _PAYLOAD_TOOLS:
        if pattern.search(t):
            subset += payload
    if writing and _COMPOSE_RE.search(t):
        subset.append("lookup_contact")
    subset = list(dict.fromkeys(subset))  # dedupe, preserve order
    # The user said DRAFT. Take the send tools off the table entirely — see
    # _DRAFT_ONLY_RE for the turn where both were offered and the model sent.
    # Only meaningful when a draft_* counterpart survives, so a domain with no
    # draft tool (calendar) is untouched.
    if ((_DRAFT_ONLY_RE.search(t) or _SELF_SEND_RE.search(t))
            and any(x.startswith("draft_") for x in subset)):
        subset = [x for x in subset if x not in _SEND_TOOLS]
    if _PLAIN_TEXT_FILE_RE.search(t):
        subset = [x for x in subset if x not in {"create_note", "append_note", "scan_to_note"}]
    # Channel still unknown -> withhold every tool that would COMMIT to one, so
    # asking is the only move left. The reads stay, so the model can still
    # gather what it needs and ask an informed question in the same turn; only
    # the irreversible/channel-picking half is removed. See
    # _CHANNEL_OUTBOUND_TOOLS for why this is structural rather than a prompt.
    if channel_ambiguous:
        subset = [x for x in subset if x not in _CHANNEL_OUTBOUND_TOOLS]
    # A delayed send and an immediate send are mutually exclusive actions.
    # Keeping both callable let the model satisfy "schedule" by sending now.
    if _SCHEDULE_ACTION_RE.search(t):
        subset = [x for x in subset if x not in {"send_message", "send_email"}]
    kind = "read+write" if writing else "lookup"
    # COMPOSING A MESSAGE TO SOMEONE ELSE IS NOT A WARM READ-OUT OF YOUR OWN
    # DATA. light_read injects "write like a thoughtful friend, open with a
    # short warm line, a few tasteful emojis are welcome" — correct when
    # reading the user their own calendar, actively wrong when the model's
    # output IS the body of a message to another person.
    #
    # VERIFIED FAILURE 2026-08-23 (user's debug export): "write a message to
    # my dad with my upcoming calendar events" produced a message opening
    # "Hey babe — here's what's coming up next month". The model was doing
    # exactly what it was told: open with a warm line. Nothing addressed it
    # to a partner; the style hint asked for warmth and the model supplied a
    # term of endearment. Sending that to the user's father is the kind of
    # mistake there is no undoing, so the voice has to be switched off
    # structurally rather than counter-instructed in the prompt.
    composing = bool(set(subset) & (_SEND_TOOLS | _DRAFT_TOOLS))
    # The data-domain union is itself just one more CLAIM, so it merges with
    # whatever the pre-checks above collected instead of overwriting them
    # (audit rows 5, 7, 9 — e.g. "summarize my inbox, and remind me to reply
    # to Dan" kept only the reminder). Alone, _merge_claims reproduces this
    # call exactly as it was written before.
    claims.append(_Claim(
        "+".join(domains), subset,
        f"{'+'.join(domains)} {kind} -> scoped tools ({len(subset)})"
        + (f" · forced {topic_force} first (unresolved topic)"
           if topic_force else ""),
        # force is dropped by _merge_claims the moment a SECOND claim is in
        # play (see _Claim.force's own comment) — deliberately: this only
        # ever wins in the common case where nothing else already claimed
        # the turn. That's a graceful downgrade to "offered, not forced",
        # not a silent failure — get_upcoming/search_notes are still in
        # `subset` either way.
        force=topic_force,
        multi=multi, after=frozenset(after),
        clarify_channel=channel_ambiguous,
        # NOT a warm read-out when we still have to ask which
        # channel. light_read injects "give the user a warm,
        # caring read-out of their own calendar … close with a
        # light caring offer to help", which measurably drowned
        # out the clarify instruction: verified live, the model
        # warmly read the schedule back and offered to help
        # instead of asking text-or-email. This request is not a
        # read-out of the user's own data, it is a decision that
        # has to be made before anything can be sent.
        light=not channel_ambiguous and not composing))
    return _merge_claims(claims)


# Typo tolerance for the words the rule regexes actually key on. Patching
# individual misspellings by hand doesn't scale — e.g. a earlier fix covered
# "calandar" but missed "calender", which is the MORE common real-world typo
# of "calendar" and reproduced the exact same bug (falls through every regex,
# lands on the ambiguous default -> the agent model instead of the summarizer). This generalizes
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
    "reschedule", "rescheduled", "rescheduling", "reschedules",
    "agenda", "agendas", "email", "emails", "inbox",
    "messages", "message", "messaged", "texts", "reminder", "reminders", "reminded",
    "meeting", "meetings", "appointment", "appointments",
    "assignment", "assignments", "notes", "earlier",
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


def rule_route(text: str) -> RouteDecision | None:
    t = _normalize_typos(text.strip())
    if (CODE_RE.search(t)
            and re.search(r"\bwhat\s+message\s+is\s+(?:this|the)\s+code\b", t, re.I)):
        return _mk("coding", reason="code explanation, not Messages data")
    # These action nouns are exact tool names in ordinary language. Letting
    # the broad email/notes and document-payload routes claim them first caused
    # `forward_email` to become `search_notes` (because the forward included a
    # note) and a requested .docx write to become another file read.
    if re.search(r"\b(?:forward|fwd)\b[^.?!]{0,100}\b(?:e-?mail|message)\b", t, re.I):
        return _mk_scoped(
            ["view_emails", "forward_email", "lookup_contact"],
            "explicit email forward -> forward_email", force="forward_email",
            light=False)
    if re.search(r"\b(?:write|create|make|save)\b[^.?!]{0,100}"
                 r"\b(?:word\s+(?:document|summary|file)|docx)\b|\.docx\b", t, re.I):
        return _mk_scoped(
            ["write_document"], "explicit Word document -> write_document",
            force="write_document", light=False)
    if re.search(r"\b(?:list|show)\b[^.?!]{0,60}\binstalled\s+skills?\b", t, re.I):
        return _mk_scoped(
            ["wisp_skills"], "installed skill inventory -> wisp_skills",
            force="wisp_skills", light=False)
    if re.search(r"\b(?:flip\s+(?:a\s+)?coin|pick\s+(?:one\s+)?(?:at\s+)?random|"
                 r"choose\s+randomly|random\s+(?:number|choice|pick))\b", t, re.I):
        return _mk_scoped(
            ["random_pick"], "explicit randomness -> random_pick",
            force="random_pick", light=False)
    if re.search(r"\b(?:convert|change)\s+-?\d+(?:\.\d+)?\s*"
                 r"(?:degrees?\s+)?(?:fahrenheit|celsius|kelvin|°?[fck])\b", t, re.I):
        return _mk_scoped(
            ["convert_units"], "explicit unit conversion -> convert_units",
            force="convert_units", light=False)
    if re.search(r"\b(?:current\s+)?time\s+difference\s+between\b", t, re.I):
        return _mk_scoped(
            ["world_time"], "current time comparison -> world_time",
            force="world_time", light=False)
    if re.search(r"\b(?:calculate|compute)\s+(?:the\s+)?(?:total|sum|cost|price)\b", t, re.I):
        return _mk_scoped(
            ["calculate"], "explicit arithmetic -> calculate",
            force="calculate", light=False)
    if re.search(r"\b(?:calculate|compute)\b[^.?!]{0,80}\bpercent\s+of\b", t, re.I):
        return _mk_scoped(
            ["calculate"], "explicit percentage arithmetic -> calculate",
            force="calculate", light=False)
    if re.search(r"\b(?:list|show|find)\b[^.?!]{0,50}\bcontact\s+names?\b", t, re.I):
        return _mk_scoped(
            ["list_contacts"], "contact-name lookup -> list_contacts",
            force="list_contacts", light=False)
    if re.search(r"\b(?:birthdays?|anniversaries?)\b[^.?!]{0,80}\bcontacts?\b|"
                 r"\bcontacts?\b[^.?!]{0,80}\b(?:birthdays?|anniversaries?)\b", t, re.I):
        return _mk_scoped(
            ["contact_dates"], "contact date lookup -> contact_dates",
            force="contact_dates", light=False)
    if re.search(r"\b(?:mail|messages?|notes?)\b[^.?!]{0,80}\blast\s+sync", t, re.I):
        return _mk_scoped(
            ["wisp_sync"], "source sync status -> wisp_sync",
            force="wisp_sync", light=False)
    if (re.search(r"\bcodex\b", t, re.I)
            and re.search(r"\b(?:chats?|tasks?|threads?|agents?|running|active|"
                          r"finished|completed|failed|stalled|status|updates?|"
                          r"doing|catch\s+me\s+up|need(?:s)?\s+me)\b", t, re.I)):
        if re.search(r"\b(?:need(?:s)?\s+me|attention|stalled|failed|broken)\b", t, re.I):
            view = "attention"
        elif re.search(r"\b(?:running|active|still\s+going|in\s+progress)\b", t, re.I):
            view = "active"
        else:
            view = "recent"
        return _mk_direct(
            [("get_codex_updates", {"view": view})],
            "Codex task overview -> get_codex_updates (router-direct)")
    if re.search(r"\bwhich\s+model\s+wisp\s+is\s+using\b|"
                 r"\bwhich\s+models?\s+(?:are\s+)?loaded\b", t, re.I):
        return _mk_scoped(
            ["wisp_status"], "Wisp model status -> wisp_status",
            force="wisp_status", light=False)
    if re.search(r"\b(?:list|show)\s+connected\s+mcp\s+servers?\b", t, re.I):
        return _mk_scoped(
            ["wisp_mcp"], "MCP status -> wisp_mcp",
            force="wisp_mcp", light=False)
    if re.search(r"\b(?:free\s+disk|memory\s+pressure|cpu\s+load|uptime)\b", t, re.I):
        return _mk_scoped(
            ["system_status"], "system resource status -> system_status",
            force="system_status", light=False)
    if re.search(r"\b(?:output\s+)?volume\b[^.?!]{0,50}\bmute\s+state\b", t, re.I):
        return _mk_scoped(
            ["get_volume"], "audio output status -> get_volume",
            force="get_volume", light=False)
    if re.search(r"\b(?:local|public)\s+ip\b|\bwi-?fi\s+network\s+name\b", t, re.I):
        return _mk_scoped(
            ["network_info"], "network status -> network_info",
            force="network_info", light=False)
    if re.search(r"\bbattery\b[^.?!]{0,80}\b(?:charge|health|cycle\s+count)\b", t, re.I):
        return _mk_scoped(
            ["get_battery_status"], "battery status -> get_battery_status",
            force="get_battery_status", light=False)
    if re.search(r"\bbring\b[^.?!]{0,100}\bback\s+to\s+my\s+attention\b", t, re.I):
        return _mk_scoped(
            ["schedule_task"], "deferred notification -> schedule_task",
            force="schedule_task", light=False)
    if re.search(r"\b(?:today'?s|daily)\s+(?:briefing|brief)\b", t, re.I):
        return _mk_scoped(
            ["daily_brief"], "daily briefing -> daily_brief",
            force="daily_brief", light=False)
    if (not _REMINDER_CREATE_RE.search(t) and re.search(
            r"\bappend\b[^.?!]{0,120}\b(?:note|notes)\b|"
            r"\b(?:note|notes)\b[^.?!]{0,120}\bappend\b|"
            r"\badd\s+(?:(?:an?|this|that|these|those|some)\s+)?"
            r"(?:lines?|bullets?|sentences?|text|content|(?:agenda\s+)?items?)\b"
            r"[^.?!]{0,80}\bto\b[^.?!]{0,60}\bnotes?\b", t, re.I)):
        d = _mk_scoped(
            ["search_notes", "append_note"], "append to existing note",
            force="append_note", light=False, multi=True)
        d.required_tool_groups = (frozenset({"search_notes"}),
                                  frozenset({"append_note"}))
        return d
    if re.search(r"\b(?:look\s*up|find|show)\b[^.?!]{0,80}"
                 r"\b(?:saved\s+)?(?:phone\s+number|e-?mail\s+address)\b", t, re.I):
        return _mk_scoped(
            ["lookup_contact"], "saved contact detail -> lookup_contact",
            force="lookup_contact", light=False)
    if re.search(r"\b(?:zip|archive|compress)\b[^.?!]{0,160}\b(?:folder|files?)\b", t, re.I):
        return _mk_scoped(
            ["archive_files"], "file archive request -> archive_files",
            force="archive_files", light=False)
    if re.search(r"\b(?:show|list|check)\b[^.?!]{0,80}"
                 r"\b(?:outbound\s+queue|queued\s+(?:emails?|texts?|messages?)|"
                 r"emails?\s+and\s+texts?\s+currently\s+queued)\b", t, re.I):
        return _mk_scoped(
            ["list_scheduled_sends", "cancel_scheduled_send"],
            "scheduled-send queue lookup", force="list_scheduled_sends",
            light=False)
    if re.search(r"\b(?:put|add)\b[^.?!]{0,100}\bexisting\b[^.?!]{0,60}"
                 r"\b(?:note|packing\s+list)\b", t, re.I):
        d = _mk_scoped(
            ["search_notes", "append_note"], "append to existing note",
            force="append_note", light=False, multi=True)
        d.required_tool_groups = (frozenset({"search_notes"}),
                                  frozenset({"append_note"}))
        return d
    if re.search(r"\b(?:encrypt|decrypt)\b[^.?!]{0,180}(?:/|\bfile\b)", t, re.I):
        return _mk_scoped(
            ["encrypt_file"], "file encryption operation -> encrypt_file",
            force="encrypt_file", light=False)
    if re.search(r"\b(?:empty|clear)\b[^.?!]{0,40}\bclipboard\b", t, re.I):
        return _mk_scoped(
            ["clear_clipboard"], "clear clipboard -> clear_clipboard",
            force="clear_clipboard", light=False)
    if re.search(r"\b(?:build|create|make)\b[^.?!]{0,60}\breusable\s+tool\b", t, re.I):
        return _mk_scoped(
            ["create_tool"], "reusable tool authoring -> create_tool",
            force="create_tool", light=False)
    if re.search(r"\bnotes?\b[^.?!]{0,50}\bdocument\s+scanner\b|"
                 r"\bscan\b[^.?!]{0,60}\b(?:receipt|document)\b[^.?!]{0,30}\bnotes?\b", t, re.I):
        return _mk_scoped(
            ["scan_to_note"], "Notes document scanner -> scan_to_note",
            force="scan_to_note", light=False)
    if re.search(r"\bunsubscribe\b", t, re.I):
        return _mk_scoped(
            ["unsubscribe"], "mail unsubscribe request -> unsubscribe",
            force="unsubscribe", light=False)
    if re.search(r"\bstopwatch\b", t, re.I):
        return _mk_scoped(
            ["stopwatch"], "stopwatch operation -> stopwatch",
            force="stopwatch", light=False)
    if re.search(r"\b(?:active\s+)?timers?\s+and\s+alarms?\b|"
                 r"\btime\s+remains?\b[^.?!]{0,40}\b(?:timers?|alarms?)\b", t, re.I):
        return _mk_scoped(
            ["manage_timers"], "timer inventory -> manage_timers",
            force="manage_timers", light=False)
    if re.search(r"\bturn\s+wi-?fi\s+(?:on|off)\b", t, re.I):
        return _mk_scoped(
            ["set_wifi"], "Wi-Fi power control -> set_wifi",
            force="set_wifi", light=False)
    if re.search(r"\b(?:join|connect\s+to)\s+(?:the\s+)?(?:wi-?fi\s+)?network\b", t, re.I):
        return _mk_scoped(
            ["connect_wifi"], "Wi-Fi network connection -> connect_wifi",
            force="connect_wifi", light=False)
    if re.search(r"\bvoiceover\b", t, re.I):
        return _mk_scoped(
            ["accessibility_toggle"], "accessibility control -> accessibility_toggle",
            force="accessibility_toggle", light=False)
    if re.search(r"\b(?:macos|software)\s+updates?\b", t, re.I):
        return _mk_scoped(
            ["software_update"], "software update operation -> software_update",
            force="software_update", light=False)
    if re.search(r"\bturn\b[^.?!]{0,40}\b(?:bluetooth|airdrop|"
                 r"do\s+not\s+disturb|low\s+power\s+mode)\b[^.?!]{0,20}\b(?:on|off)\b", t, re.I):
        return _mk_scoped(
            ["toggle_setting"], "system setting control -> toggle_setting",
            force="toggle_setting", light=False)
    if re.search(r"\b(?:screenshot|capture\s+(?:a\s+)?(?:window|screen|area))\b", t, re.I):
        return _mk_scoped(
            ["screen_capture"], "screen capture request -> screen_capture",
            force="screen_capture", light=False)
    if re.search(r"\b(?:remove|clear|delete)\b[^.?!]{0,60}"
                 r"\b(?:overdue|past[- ]due|past)\b[^.?!]{0,50}\breminders?\b", t, re.I):
        return _mk_scoped(
            ["clear_past_reminders"], "past reminder cleanup -> clear_past_reminders",
            force="clear_past_reminders", light=False)
    if re.search(r"\bclear\b[^.?!]{0,60}\breminders?\b[^.?!]{0,60}"
                 r"\bexcept\b", t, re.I):
        d = _mk_scoped(
            ["get_upcoming"], "ambiguous reminder exception -> inspect and clarify",
            force="get_upcoming", light=False)
        d.forbidden_tools = frozenset({"clear_reminders", "clear_past_reminders",
                                       "cancel_event"})
        return d
    if re.search(r"\bpermanently\s+delete\b[^.?!]{0,180}(?:/|\bfile\b)", t, re.I):
        return _mk_scoped(
            ["delete_path"], "permanent path deletion -> delete_path",
            force="delete_path", light=False)
    if reschedule := re.match(r"^(?:(?:please|can\s+you|could\s+you|would\s+you|"
                              r"i\s+need\s+you\s+to)\s+)*reschedule\b", t, re.I):
        parts = re.split(r"\b(?:to|for)\b", t[reschedule.end():],
                         maxsplit=1, flags=re.I)
        target_text = parts[0]
        reminder = bool(re.search(r"\breminders?\b", target_text, re.I))
        event = bool(re.search(r"\b(?:events?|meetings?|appointments?)\b", target_text, re.I))
        updating = ["update_reminder"] if reminder else []
        if event and (not reminder or re.search(r"\band\b", target_text, re.I)):
            updating.append("update_event")
        if updating:
            d = _mk_scoped(["get_upcoming", *updating],
                           "reschedule existing item -> " + ", ".join(updating),
                           expect=False, light=False, multi=True)
            # A missing/ambiguous target or change may need a question. An
            # unconditional required group would force an ungrounded update.
            d.forbidden_tools = frozenset({"add_calendar_event", "add_reminder"})
            if "update_event" in updating:
                # Existing Calendar metadata cannot support a preservation-
                # safe update. The loop reports the registered unavailability
                # before reading/approving/dispatching anything; do not bind
                # a title query as identity or invent location/duration values.
                d.force_first_tool = "update_event"
                d.expect_tool_first = True
                d.required_tool_groups = (frozenset({"update_event"}),)
                d.forbidden_tools |= frozenset({"cancel_event"})
                if updating == ["update_event"]:
                    d.tool_subset = ["update_event"]
            return d
    if re.search(r"\b(?:add|create|schedule)\b[^.?!]{0,100}"
                 r"\b(?:calendar\s+)?(?:event|meeting)\b", t, re.I):
        return _mk_scoped(
            ["add_calendar_event"], "calendar event creation -> add_calendar_event",
            force="add_calendar_event", light=False)
    if re.search(r"\bbring\b[^.?!]{0,60}\bforward\b[^.?!]{0,60}\bapps?\b|"
                 r"\bbring\s+(?:safari|chrome|finder|mail|messages|notes)\s+forward\b", t, re.I):
        return _mk_scoped(
            ["switch_app"], "foreground app switch -> switch_app",
            force="switch_app", light=False)
    if re.search(r"^(?:patch|post|put|delete|get)\s+https?://", t, re.I):
        return _mk_scoped(
            ["http_request"], "explicit HTTP request -> http_request",
            force="http_request", light=False)
    if re.search(r"\b(?:rename|change|move(?![ -]?in\b)|correct)\b[^.?!]{0,100}\breminder\b|"
                 r"\breminder\b[^.?!]{0,100}\b(?:rename|change|move(?![ -]?in\b)|correct)\b", t, re.I):
        return _mk_scoped(
            ["update_reminder"], "reminder update -> update_reminder",
            force="update_reminder", light=False)
    # "How far back can you check my email" — checked BEFORE _domain_subset,
    # which would otherwise claim it as an ordinary email-domain request (as it
    # did live: "email lookup -> scoped tools (2)", handed to a model that then
    # called summarize_emails and dumped a digest instead of answering the
    # range question actually asked). See _SEARCH_COVERAGE_RE's own comment for
    # why this bypasses the model rather than just instructing it harder.
    if _SEARCH_COVERAGE_RE.search(t):
        source = next((key for key, pat in _SEARCH_COVERAGE_DOMAINS if pat.search(t)), "")
        return _mk_direct([("search_coverage", {"source": source} if source else {})],
                          "capability question -> search_coverage (router-direct)")
    if _CAPABILITY_INVENTORY_RE.search(t):
        return _mk_direct([("wisp_capabilities", {"query": t})],
                          "capability inventory -> wisp_capabilities (router-direct)",
                          light=False)
    if (m := _LOW_POWER_CONDITIONAL_RE.search(t)):
        d = _mk_scoped(["get_battery_status", "toggle_setting"],
                       "conditional battery action -> read then optional setting",
                       force="get_battery_status", light=False, multi=True)
        d.required_tool_groups = (frozenset({"get_battery_status"}),
                                  frozenset({"toggle_setting"}))
        d.conditional_tools = (("get_battery_status", "toggle_setting",
                                "percent_below", int(m.group(1))),)
        return d
    if _BULK_REMINDER_CLEAR_RE.search(t):
        scope = ("today" if re.search(r"\btoday\b", t, re.I)
                 else "tomorrow" if re.search(r"\btomorrow\b", t, re.I)
                 else "all")
        d = _mk_direct([("clear_reminders", {"scope": scope})],
                       f"bulk reminder deletion ({scope}) -> clear_reminders "
                       "(router-direct)", light=False)
        d.required_tool_groups = (frozenset({"clear_reminders"}),)
        d.forbidden_tools = frozenset({"clear_past_reminders", "cancel_event"})
        return d
    if _COMPLETE_RE.search(t) and re.search(r"\breminder\b", t, re.I):
        named = re.search(r"\bcomplete\s+(?:the\s+)?(.{1,48}?)\s+reminder\b", t, re.I)
        title = named.group(1).strip() if named else ""
        if title and title.lower() not in {"this", "that", "it"}:
            d = _mk_direct([("complete_reminder", {"title": title})],
                           "named reminder completion -> complete_reminder (router-direct)",
                           light=False)
        else:
            d = _mk_scoped(["complete_reminder"],
                           "explicit reminder completion -> complete_reminder",
                           force="complete_reminder", light=False)
        d.required_tool_groups = (frozenset({"complete_reminder"}),)
        d.forbidden_tools = frozenset({"cancel_event", "update_event"})
        return d
    if query := _email_search_query(t):
        return _mk_direct(
            [("view_emails", {"query": query, "count": 10,
                              "strict_match": True})],
            "specific email search -> view_emails (router-direct)")
    if (args := _inline_email_summary_args(t)) is not None:
        return _mk_direct(
            [("summarize_emails", args)],
            "email summary requested in Wisp -> summarize_emails (router-direct)")
    # A bare "summarize my inbox" / "recap my messages" — the whole request, with
    # no qualifier the summarizer would need an argument for (see
    # _DIRECT_SUMMARY_RE's anchoring). Checked before _domain_subset, which would
    # otherwise claim it and hand the model a selection step to reach the same
    # call. Combined with run_agent's existing short-circuit for these
    # pre-synthesized tools, the turn ends with ONE model call total: the
    # summarizer's own internal synthesis.
    if (m := _DIRECT_SUMMARY_RE.match(t)):
        domain = m.group("domain").lower()
        tool = ("summarize_messages"
                if domain.startswith(("message", "text", "imessage"))
                else "summarize_emails")
        return _mk_direct([(tool, {})], f"explicit summary request -> {tool} (router-direct)")
    # Messages/email/calendar/notes requests -> a toolset scoped to the domains
    # actually named, reads and writes both (see _domain_subset).
    #
    # Send intent used to be checked HERE, ahead of the domain builder, and
    # returned the undifferentiated full toolset. That ordering is what broke
    # compound requests from both directions at once: "text Dan" jumped the
    # queue correctly, but "summarize my emails and send them to X" also jumped
    # it and got all 44 tools, which measures WORSE at picking (2/3, and it
    # reached for show_profile) than the 7 tools the request actually needs.
    # _domain_subset now handles send intent itself — it matches the same
    # SEND_*_RE patterns, so "text Dan" still contributes the messages domain,
    # and `writing` adds send_message/lookup_contact on top of the read tools
    # rather than instead of them.
    # Claims for the routes that sit BELOW the _domain_subset call but whose
    # patterns can match a clause anywhere in the sentence. Collected here and
    # handed down so they merge with any data domain instead of being
    # unreachable once one matched — audit rows 1, 2, 5, 6, 8, e.g. "remember
    # that I drive a BMW, and also check my calendar" silently lost the
    # memory save because the calendar domain returned first.
    #
    # Each mirrors its own route below EXACTLY (same tools, same force, same
    # light), so a request that matches only this one still routes as it
    # always did — _merge_claims reproduces a single claim verbatim.
    _pre: list[_Claim] = []
    if MEMORY_SAVE_RE.search(t):
        _pre.append(_Claim("memory_save", list(_MEMORY_TOOLS),
                           "memory save intent -> remember", force="remember"))
    elif MEMORY_FORGET_RE.search(t):
        _pre.append(_Claim("memory_forget", list(_MEMORY_TOOLS),
                           "memory forget intent -> forget", force="forget"))
    elif MEMORY_QUERY_RE.search(t):
        _pre.append(_Claim("memory_query", list(_MEMORY_TOOLS),
                           "memory query intent -> recall", force="recall"))
    elif SELF_QUERY_RE.search(t):
        _pre.append(_Claim("self_query", ["recall"],
                           "self query -> recall", force="recall"))
    elif _PEOPLE_QUERY_RE.search(t):
        _pre.append(_Claim("people_query", ["recall", *_CONTACT_TOOLS],
                           "people-in-my-life query -> read-only contacts",
                           light=False))
    if _RECENT_RE.search(t) and not _TODO_RE.search(t) and not _PLANNING_RE.search(t):
        _pre.append(_Claim("recency", ["get_recent_activity", *_ALL_SOURCES],
                           "recency sweep -> get_recent_activity",
                           force="get_recent_activity"))
    # Device control contributes only when it is NOT the whole request: on its
    # own it keeps its existing unscoped/direct-dispatch route below (which
    # has its own `_direct_device_call` fast path this claim cannot express).
    # `_DATA_NOUN_RE` mirrors that route's own gate.
    if (_SYSTEM_CONTROL_RE.search(t) and not _DATA_NOUN_RE.search(t)
            and (direct := _direct_device_call(t))):
        _pre.append(_Claim("device", [n for n, _ in direct],
                           f"device control -> {direct[0][0]} (router-direct)",
                           light=False, direct_calls=direct))
    if (scoped := _domain_subset(t, _pre)) is not None:
        return scoped
    # Explicit memory intent -> restricted to the three memory tools. Saving a
    # fact is a one-line write with no reasoning in it, so the narrower the
    # advertised toolset the better.
    # Each memory verb forces its OWN tool as the only one on the first step,
    # rather than offering all three and hoping the model picks right (see
    # _mk_scoped's `force`).
    # "create a tool for X" -> actually call create_tool (see CREATE_TOOL_RE).
    # force_first_tool restricts step 1 to that single function, which is the
    # only thing that reliably stops the model talking itself out of it.
    if CREATE_TOOL_RE.search(t):
        d = _mk("agent", tools=True, expect_tool_first=True,
                reason="explicit request to build a tool -> create_tool")
        d.force_first_tool = "create_tool"
        return d

    # Exact-answer computation -> the agent model, but it MUST call a tool first (see
    # COMPUTE_RE). It picks which: run_shell for a one-off, create_tool when
    # the user signals they'll want it again.
    if COMPUTE_RE.search(t):
        return _mk("agent", tools=True, expect_tool_first=True,
                   reason="exact-answer computation -> must run code, not answer from memory")

    # Recency sweep across every app — one cheap no-model read (see
    # tools/recent_tools). force_first_tool guarantees it actually runs rather
    # than the model reaching for a single source and calling that "what's new";
    # the other read tools ride along so it can drill down afterwards.
    if _RECENT_RE.search(t) and not _TODO_RE.search(t) and not _PLANNING_RE.search(t):
        return _mk_scoped(["get_recent_activity", *_ALL_SOURCES],
                          "recency sweep -> get_recent_activity",
                          force="get_recent_activity")

    if MEMORY_SAVE_RE.search(t):
        return _mk_scoped(_MEMORY_TOOLS, "memory save intent -> remember", force="remember")
    if MEMORY_FORGET_RE.search(t):
        return _mk_scoped(_MEMORY_TOOLS, "memory forget intent -> forget", force="forget")
    if MEMORY_QUERY_RE.search(t):
        return _mk_scoped(_MEMORY_TOOLS, "memory query intent -> recall", force="recall")
    if SELF_QUERY_RE.search(t):
        return _mk_scoped(["recall"], "self query -> recall", force="recall")
    if _PEOPLE_QUERY_RE.search(t):
        return _mk_scoped(["recall", *_CONTACT_TOOLS],
                          "people-in-my-life query -> read-only contacts", light=False)
    # Calendar/schedule and email intent that _domain_subset did NOT scope —
    # i.e. it bailed out because the request also reads as a machine action
    # ("open my calendar") or as code authoring. Those need the full toolset,
    # so they get the unscoped agent route on purpose. Ordinary calendar and
    # email requests never reach here: _domain_subset matches SCHEDULE_RE and
    # EMAIL_RE/SEND_EMAIL_RE itself and returns a scoped subset above.
    if SCHEDULE_RE.search(t):
        return _mk("agent", tools=True, reason="calendar/schedule intent -> assistant tools")
    if EMAIL_RE.search(t):
        return _mk("agent", tools=True, reason="email intent -> agent (full toolset)")
    # Machine actions -> agent loop on the tool-capable model. Checked before the
    # code/reason rules so "delete the file", "quit safari", "play my music"
    # reliably get TOOLS instead of leaking to a tool-less completion.
    # A file/document job gets the five tools that job needs, rather than the
    # whole registry (see _DOCUMENT_RE). Checked just before the catch-all so
    # more specific routes above still win — "email me the PDF" is a send, and
    # _domain_subset has already claimed it.
    # NOT when the request also MUTATES: "delete that file", "move that file to
    # Documents" mention a file but need delete_path, which this subset
    # deliberately excludes — a route that fires on the bare noun "file" must
    # not be the one arming deletion. Those fall through to the full toolset.
    # (_WRITE_INTENT_RE, not STRONG_ACTION_RE: the latter is about operating
    # APPS and matches none of these, verified.)
    # Tidying/filing/reorganizing files. Checked BEFORE the document-read branch
    # below because that one bails on _WRITE_INTENT_RE, whose verb list contains
    # "move" — so "move my logs into folders" fell through to the undifferentiated
    # 55-tool route, the configuration that measures worst at tool selection, for
    # a job whose whole point is calling one specific tool repeatedly.
    #
    # No `after`: a reorganize is done when every file has been moved, and there
    # is no statically-knowable tool whose result proves that. Letting the
    # narration gate fire early here is how a half-finished move gets reported as
    # complete (multi_round's original never-narrate meaning is correct).
    if _REORGANIZE_RE.search(t) and _DOCUMENT_RE.search(t):
        vague = not _CONCRETE_TARGET_RE.search(t)
        return _mk_scoped(["find_files", "list_dir", "move_path", "organize_files"],
                          "reorganize files"
                          + (" [no target named -> ask first]" if vague else ""),
                          expect=False, multi=True, clarify_target=vague)
    if (_DOCUMENT_RE.search(t) and not _WRITE_INTENT_RE.search(t)
            and not STRONG_ACTION_RE.search(t)):
        # multi=True: this route is inherently two-step for an unnamed file —
        # list_dir finds it, THEN read_file reads it. See RouteDecision.multi_round.
        #
        # after={"read_file"}: once the file's CONTENTS are in context there is
        # nothing left to look up, so narration can drop the think block. A bare
        # listing ("what's in my Downloads") never calls read_file and so never
        # satisfies this — which is the conservative outcome, identical to the
        # behavior before narration_after existed.
        return _mk_scoped(None, "file/document task",
                          expect=False, multi=True, after=frozenset({"read_file"}))
    # Device control / apps & media / live web — three clusters that were the
    # bulk of what still fell through to the full toolset (see their comments).
    # Checked BEFORE _wants_tools, which would otherwise claim them all and
    # return the unscoped route. A request naming a data noun is left alone, so
    # "open my calendar" keeps the whole toolset and the model still gets to
    # choose between open_app and get_upcoming.
    #
    # light=False on all three: these are not read-outs of the user's own
    # calendar/notes/messages, and the warm bulleted voice would be wrong.
    if not _DATA_NOUN_RE.search(t):
        if _SYSTEM_CONTROL_RE.search(t):
            # One unambiguously-named zero-argument tool -> call it here rather
            # than spending a model step deciding what this regex already knows.
            # See _direct_device_call for why anything ambiguous declines.
            if (direct := _direct_device_call(t)):
                return _mk_direct(direct,
                                  f"device control -> {direct[0][0]} (router-direct)",
                                  light=False)
            if len(hits := _matched_device_tools(t)) > 1:
                return _mk_scoped(hits, "compound device control -> resolved tools",
                                  expect=False, light=False, multi=True)
            return _mk_scoped(None, "device control",
                              expect=False, light=False)
        # `not _DOCUMENT_RE` so "open the pdf in my downloads" stays a document
        # task — open/launch is also how people talk about FILES, and open_app
        # cannot do anything with one.
        if _APPS_MEDIA_RE.search(t) and not _DOCUMENT_RE.search(t):
            return _mk_scoped(None, "apps/media",
                              expect=False, light=False)
        if _WEB_RE.search(t) and not _has_no_web_constraint(t):
            return _mk_scoped(_WEB_TOOLS,
                              f"live external fact -> scoped tools ({len(_WEB_TOOLS)})",
                              light=False)
    if _wants_tools(t):
        # Tool intent is certain, the DOMAIN is not — "set a timer for 10
        # minutes" and "throw that in the trash" both land here. Left unscoped
        # on purpose: `route()` fills the subset by retrieval (see the note on
        # RouteDecision about why that is universal rather than opt-in).
        return _mk("agent", tools=True, reason="action verb implies operating the machine")
    # Code authoring/fixing with no machine action -> coding model (now the agent model).
    # If it also names a save location, it needs file access -> agent loop.
    if CODE_RE.search(t):
        if LOCATION_RE.search(t):
            return _mk("agent", tools=True,
                       reason="code request also names a save location -> needs file access")
        # "code" is not always programming. CODE_RE matches the bare word, so
        # "jot down that the door code is 1234" was routed to the coding model
        # with NO tools — the user asked to save a PIN and got a chat reply.
        # Checked here rather than by editing CODE_RE, which several other call
        # sites depend on.
        if not _PHYSICAL_CODE_RE.search(t):
            return _mk("coding", reason="code-related request")
    # "why is my mac slow/being slow" and "why are YOU slow" both read as an
    # open-ended reasoning question to REASON_RE below — needs_tools=False, no
    # tools offered at all — when both have a deterministic, checkable answer.
    # Checked here, ahead of REASON_RE, for the same reason _JOIN_CALL_RE and
    # _AVAILABILITY_RE are checked ahead of their broader neighbors: a
    # narrow, specific shape should win over a broad catch-all that would
    # otherwise claim it first.
    if re.search(r"\bwhy\s+(?:is|does)\s+(?:my\s+)?(?:mac|computer)\b.{0,20}\bslow\b|"
                r"\bwhy\s+(?:are|is)\s+you\b.{0,20}\bslow\b", t, re.I):
        asks_mac = bool(re.search(r"\b(?:mac|computer)\b", t, re.I))
        asks_wisp = bool(re.search(r"\b(?:you|wisp)\b", t, re.I))
        calls = ([('system_status', {})] if asks_mac else [])
        if asks_wisp:
            calls.append(("wisp_status", {}))
        return _mk_direct(calls, "performance question -> status tools (router-direct)",
                          light=False)
    if HARD_REASON_RE.search(t):
        return _mk("reasoning", reason="hard reasoning -> deliberate effort")
    if REASON_RE.search(t):
        return _mk("general", reason="light reasoning -> fast generalist")
    # Positively trivial ONLY -> fast/the summarizer. Everything else is
    # ambiguous and falls through to route()'s own default handling, which
    # sends it straight to the agent model with a semantically-retrieved tool
    # subset — see route()'s big comment on why there is no LLM classify step
    # any more (it silently mis-classified ~15% of genuinely tool-needing
    # requests).
    if TRIVIAL_RE.match(t) and len(t.split()) <= 8:
        return _mk("fast", reason="trivial chit-chat")
    return None  # ambiguous -> route()'s default handling decides


_ALL_MUTATING_TOOLS = frozenset(
    tool for tools in _DOMAIN_WRITE_TOOLS.values() for tool in tools
) | frozenset({"write_file", "move_path", "delete_path", "organize_files",
               "toggle_setting", "schedule_send", "cancel_scheduled_send"})
_INBOX_READ_TOOLS = frozenset({"view_emails", "summarize_emails", "scan_subscriptions",
                               "summarize_thread", "triage_inbox"})


def _stock_exact_args(t: str) -> dict | None:
    """Resolve exact short stock spans without asking the model to bucket them."""
    span = re.search(r"\b(?:from|over|for)\s+(?:today\s+and\s+from\s+)?"
                     r"(?:exactly\s+)?(\d+|one|two|three|four|five|six)\s+weeks?\s+ago\b",
                     t, re.I)
    if not span:
        return None
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    raw = span.group(1).lower()
    count = int(raw) if raw.isdigit() else words[raw]
    names = re.search(r"\b(?:share\s+price\s+of|price\s+of|stocks?\s+(?:of|for))\s+"
                      r"(.+?)\s+(?:from|over|for)\s+", t, re.I)
    if not names:
        return None
    segment = re.sub(r"\b(?:my|the|shares?|stocks?)\b", " ", names.group(1), flags=re.I)
    symbols = [p.strip(" ,.") for p in re.split(r"\s*(?:,|\band\b)\s*", segment,
                                                flags=re.I) if p.strip(" ,.")]
    if not symbols:
        return None
    return {"symbols": symbols[:4], "period": f"{count} weeks"}


def _apply_execution_contract(decision: RouteDecision, text: str) -> None:
    """Attach obligations/negative constraints and make their tools reachable.

    These are structural invariants for high-risk boundary shapes, not prompt
    advice. The agent loop consumes the groups before accepting final prose.
    """
    t = _normalize_typos(text.strip())
    groups = list(decision.required_tool_groups)
    forbidden = set(decision.forbidden_tools)
    conditionals = list(decision.conditional_tools)

    def require(*tools: str) -> None:
        group = frozenset(tools)
        if group and group not in groups:
            groups.append(group)

    if (_REMINDER_CREATE_RE.search(t) and not decision.reminder_action
            and not _CAPABILITY_INVENTORY_RE.search(t)):
        decision.reminder_action = "create" if has_alert_time(t) else "clarify_time"
    if decision.reminder_action and has_unsupported_alert_clock(t):
        # A recognizable but unresolved clock is an explicit clarification,
        # including after a time question. Do not retain a retrieved alarm,
        # a forced write, or a guessed date-only binding as an escape hatch.
        decision.reminder_action = "clarify_time"
        decision.tool_subset = ["get_upcoming"]
        decision.direct_calls = []
        decision.force_first_tool = None
        decision.expect_tool_first = False
        decision.required_tool_groups = ()
        decision.tool_argument_bindings = {}
        decision.conditional_tools = ()
        decision.forbidden_tools = frozenset(forbidden | {
            "add_reminder", "add_calendar_event", "set_alarm", "remember"})
        decision.needs_tools = True
        decision.light_read = False
        decision.multi_round = True
        return
    if decision.reminder_action:
        forbidden.add("remember")
        decision.tool_subset = list(dict.fromkeys(
            (decision.tool_subset or []) + ["get_upcoming", "add_reminder"]))
        decision.needs_tools = True
        decision.light_read = False
        decision.multi_round = True
        if decision.reminder_action == "create":
            if resolved := resolve_alert_datetime(t):
                decision.tool_argument_bindings.setdefault("add_reminder", {})[
                    "when_iso"] = resolved.isoformat(timespec="minutes")
                # The time is complete and resolved. Force the actual reminder
                # write first so get_upcoming cannot replace it and trigger an
                # unnecessary clarification.
                decision.force_first_tool = "add_reminder"
            if re.search(r"\b(?:before|ahead of|early|earlier|same time|starts|begins)\b", t, re.I):
                require("get_upcoming")
            require("add_reminder")
        else:
            # Missing alert time: allow a lookup/question, never guess the
            # appointment's start time or fabricate a midnight default.
            forbidden.add("add_reminder")
            groups = [g for g in groups if "add_reminder" not in g]
            if _EVENT_RELATIVE_REMINDER_RE.search(t):
                require("get_upcoming")
                decision.force_first_tool = "get_upcoming"
        # An explicitly requested reminder is not another calendar event.
        if not re.search(r"\band\b.*\b(?:add|create|schedule)\b.*\b(?:event|meeting)\b", t, re.I):
            forbidden.add("add_calendar_event")
        # A second "and tell Mom" clause is a second required action. When
        # its channel is explicit, completing only the reminder is incomplete.
        if _AND_NOTIFY_RE.search(t):
            channel = _outbound_channel(t)
            if channel == "messages":
                require("lookup_contact"); require("send_message")
                forbidden |= {"send_email", "draft_message", "draft_email"}
            elif channel == "email":
                require("lookup_contact"); require("send_email")
                forbidden |= {"send_message", "draft_message", "draft_email"}

    # A literal recipient address is already fully resolved. The model was
    # observed calling lookup_contact("johnstandark") and stopping on no-match
    # instead of using the address the user supplied. Bind and require the
    # actual send for imperative email requests; drafts/scheduled sends retain
    # their own effect tools and confirmation behavior.
    if (SEND_EMAIL_RE.search(t) and _outbound_channel(t) == "email"
            and not re.search(r"\b(?:draft|compose|schedule)\b", t, re.I)
            and (address := _EMAIL_ADDRESS_RE.search(t))):
        require("send_email")
        decision.tool_argument_bindings.setdefault("send_email", {})[
            "to"] = address.group(0).rstrip(".,;:!?")
        forbidden.add("lookup_contact")

    # Explicit prohibitions are subtracted after every positive obligation.
    if re.search(r"\bwithout\s+(?:opening|checking|reading)\s+(?:my\s+|the\s+)?inbox\b", t, re.I):
        forbidden |= set(_INBOX_READ_TOOLS)
    if re.search(r"\b(?:do\s+not|don'?t|never)\s+send\b", t, re.I):
        forbidden |= set(_SEND_TOOLS)
    if re.search(r"\b(?:do\s+not|don'?t|never)\s+(?:add|set|create|change|modify)\b|\bread\s+only\b", t, re.I):
        forbidden |= set(_ALL_MUTATING_TOOLS)
    if _has_no_web_constraint(t) or _has_private_web_context(t):
        forbidden |= {"web_search", "web_fetch", "http_request"}
        if _has_no_web_constraint(t) or _has_public_lookup_cue(t):
            forbidden.add("run_shell")
    if re.search(r"\bdo\s+not\b[^.?!]{0,80}\bopen\s+(?:a\s+)?different\s+app\b", t, re.I):
        forbidden |= {"open_app", "switch_app"}
    if re.search(r"\bnot\s+(?:the\s+)?calendar\s+event\b", t, re.I):
        forbidden |= {"cancel_event", "update_event"}

    if re.search(r"\bwi-?fi\s+password\b", t, re.I) and _SELF_SEND_RE.search(t):
        require("search_notes"); require("draft_email")
        forbidden |= {"send_email"}
    if _DOCUMENT_RE.search(t) and re.search(r"\b(?:draft|email)\b", t, re.I):
        require("find_files"); require("read_file"); require("lookup_contact"); require("draft_email")
    if (_DOCUMENT_RE.search(t)
            and re.search(r"\b(?:read|summari[sz]e|inspect|contents?|first\s+(?:few\s+)?lines?)\b",
                          t, re.I)
            and not _REORGANIZE_RE.search(t)):
        require("find_files", "list_dir"); require("read_file")
    if (_WEATHER_PAYLOAD_RE.search(t) and re.search(r"\b(?:rain|outdoor)\b", t, re.I)
            and re.search(r"\bremind", t, re.I)):
        require("get_upcoming"); require("get_weather"); require("add_reminder")
        conditionals.append(("get_weather", "add_reminder", "rain_chance_above", 0))
    if _PLAIN_TEXT_FILE_RE.search(t) and _NOTES_INTENT_RE.search(t):
        require("search_notes"); require("write_file")
        forbidden |= {"create_note", "append_note", "scan_to_note"}
    if re.search(r"\bremind\s+me\b", t, re.I) and _SELF_SEND_RE.search(t):
        require("add_reminder"); require("draft_email")
        forbidden |= {"send_email"}
    if re.search(r"\bwhat\s+did\s+i\s+tell\s+(?!you\b|wisp\b)\w+\b", t, re.I):
        require("search_conversations", "summarize_messages")
        forbidden |= {"recall", "get_upcoming"}
        decision.clarify_channel = False
    if re.search(r"\bwhat\s+do\s+you\s+know\s+about\s+my\s+calendar\b", t, re.I):
        require("get_upcoming")
        forbidden.add("recall")
    if re.search(r"\bdon'?t\s+forget\s+to\s+email\b", t, re.I):
        require("add_reminder")
        forbidden |= {"remember", "send_email"}
    if _COMPLETE_RE.search(t) and re.search(r"\breminder\b", t, re.I):
        require("complete_reminder")
        forbidden |= {"cancel_event", "update_event"}
    if re.search(r"\bwhy\b.*\b(?:mac|computer)\b.*\bslow\b.*\bwisp\b", t, re.I):
        require("system_status"); require("wisp_status")
    if _CAPABILITY_INVENTORY_RE.search(t):
        require("wisp_capabilities")
        forbidden |= {"send_message", "send_email", "add_reminder"}
    # File reorganization must always expose a non-destructive move primitive.
    # Semantic retrieval is useful for discovery, but it is not a safety
    # boundary: an embedding-score change once dropped both move_path and
    # organize_files while leaving run_shell available.  Keep the alternatives
    # as a structural obligation so the loop can never be forced to improvise
    # a move with rm/mv shell commands.
    if _REORGANIZE_RE.search(t) and _DOCUMENT_RE.search(t):
        if not decision.clarify_target:
            # Bulk organization needs a set operation. One move_path receipt
            # cannot prove that every matching file was moved.
            bulk = bool(re.search(r"\b(?:reorgani[sz]e|organi[sz]e|tidy|sort|group|"
                                  r"rearrange|consolidate|clean\s+up|file\s+away)\b", t, re.I))
            if bulk:
                require("find_files")
                require("organize_files")
                decision.tool_subset = ["find_files", "organize_files"]
                # Discovery is by filename across file types. A generated
                # folder/pdf filter can silently drop part of a mixed set.
                decision.tool_argument_bindings["find_files"] = {"kind": "", "content": False}
            else:
                require("find_files", "list_dir")
                require("move_path", "organize_files")
    if re.search(r"\bmove[ -]?in\s+date\b", t, re.I) and _COMPOSE_RE.search(t):
        require("get_upcoming"); require("search_notes")
        require("search_conversations", "view_messages")
        if SEND_MESSAGE_RE.search(t) or re.search(r"\b(?:send|message)\s+mom\b", t, re.I):
            require("send_message")
        if not any(n == "get_upcoming" for n, _ in decision.direct_calls):
            decision.direct_calls.append(("get_upcoming", {"days": 60}))
    if _STOCK_PAYLOAD_RE.search(t) and _COMPOSE_RE.search(t):
        require("get_stock_price")
        if SEND_MESSAGE_RE.search(t) or re.search(r"\b(?:send|message)\s+mom\b", t, re.I):
            require("send_message")
        # The generic unresolved-topic rule must never force calendar here.
        decision.direct_calls = [(n, a) for n, a in decision.direct_calls
                                 if n != "get_upcoming"]
        if (args := _stock_exact_args(t)) is not None:
            decision.direct_calls.append(("get_stock_price", args))
    if (_SCHEDULE_ACTION_RE.search(t) and re.search(r"\b(?:tomorrow|tmrow)\b", t, re.I)):
        require("get_upcoming"); require("schedule_send")
        if not any(n == "get_upcoming" for n, _ in decision.direct_calls):
            decision.direct_calls.append(("get_upcoming", {"days": 2}))

    # Every obligation must be callable. Alternatives are all offered; the
    # contract still requires only one successful member of each group.
    if groups:
        decision.needs_tools = True
        decision.role = "agent"
        decision.model = role_to_model("agent")
        subset = list(decision.tool_subset or [])
        subset += [name for group in groups for name in group]
        subset += [name for name, _ in decision.direct_calls]
        decision.tool_subset = list(dict.fromkeys(subset))
        decision.multi_round = (decision.multi_round or len(groups) > 1
                                or bool(conditionals))

    decision.forbidden_tools = frozenset(forbidden)
    decision.required_tool_groups = tuple(g for g in groups if not g <= forbidden)
    decision.conditional_tools = tuple(dict.fromkeys(conditionals))
    if decision.tool_subset is not None:
        decision.tool_subset = [n for n in decision.tool_subset if n not in forbidden]
    decision.direct_calls = [(n, a) for n, a in decision.direct_calls if n not in forbidden]


def _finalize(decision: RouteDecision, text: str) -> RouteDecision:
    """Apply model/tool invariants after any classifier source picks a role."""
    # Agentic work must run on the tool-capable agent model (the agent model). The
    # classifier can flag needs_tools on a non-agent role (e.g. fast=the summarizer);
    # the summarizer can't reliably drive the FULL toolset, so force the agent model here.
    # EXCEPTION: a light read route (tool_subset set) deliberately runs the summarizer on
    # a narrow, verified-reliable read-only toolset — don't force it to the agent model,
    # that would defeat keeping the big model asleep.
    _apply_execution_contract(decision, text)
    if decision.needs_tools and not decision.tool_subset and not is_tool_capable(decision.model):
        decision.role = "agent"
        decision.model = role_to_model("agent")
        decision.reason += " · forced agent model (the agent model) for tool use"
    return decision


_APOSTROPHE_TRANSLATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u02bc": "'",
    "\uff07": "'",
})


def _normalize_routing_text(text: str) -> str:
    """Normalize punctuation variants that must not change user constraints."""
    return text.translate(_APOSTROPHE_TRANSLATION)


def _has_no_web_constraint(text: str) -> bool:
    normalized = _normalize_routing_text(text)
    for match in _NO_WEB_SEARCH_RE.finditer(normalized):
        # In an explicit search, subordinate topic wording describes the
        # subject, not an instruction to the assistant. A separate sentence
        # or clause (e.g. '; avoid browsing') still prohibits the lookup.
        prefix = re.split(r"[.!?;\n,]", normalized[:match.start()])[-1]
        if _EXPLICIT_WEB_SEARCH_RE.search(prefix):
            if re.search(r"\bhow\s+to\s*$", prefix, re.I):
                continue
            if (re.search(r"\b(?:for|about)\s+\S.+", prefix, re.I)
                    and re.fullmatch(r"without\s+(?:the\s+)?internet", match.group(), re.I)):
                continue
        return True
    return False


def _has_private_web_context(text: str) -> bool:
    normalized = _normalize_routing_text(text)
    if _COMPOSE_RE.search(normalized) or SEND_EMAIL_RE.search(normalized):
        # Delivery to the user's address is not a request to read their inbox.
        normalized = re.sub(r"\bto\s+my\s+e-?mail\b", "to recipient",
                            normalized, flags=re.I)
    return bool(_LOCAL_CURRENT_CONTEXT_RE.search(normalized))


def _is_current_public_event(text: str) -> bool:
    """Recognize current-world questions without stealing local-data reads."""
    normalized = _normalize_routing_text(text)
    if (_LOCAL_CURRENT_CONTEXT_RE.search(normalized)
            or _NARRATIVE_CONTEXT_RE.search(normalized)):
        return False
    if (_HISTORICAL_EVENT_RE.search(normalized)
            and not _CURRENT_TIME_CUE_RE.search(normalized)):
        return False
    return bool(_CURRENT_PUBLIC_EVENT_RE.search(normalized))


def _has_public_lookup_cue(text: str) -> bool:
    normalized = _normalize_routing_text(text)
    return bool(
        re.search(r"\b(?:news|headlines?)\b", normalized, re.I)
        or _CURRENT_PUBLIC_EVENT_RE.search(normalized)
        or _EXPLICIT_WEB_SEARCH_RE.search(normalized)
    )


def _looks_like_live_web_lookup(text: str) -> bool:
    normalized = _normalize_routing_text(text)
    if _has_private_web_context(normalized):
        return False
    # Explicit public research may concern history or fiction. Implicit
    # current-event detection must not reinterpret those as live events.
    return bool(
        re.search(r"\b(?:news|headlines?)\b", normalized, re.I)
        or _is_current_public_event(normalized)
        or _EXPLICIT_WEB_SEARCH_RE.search(normalized)
    )


_WEB_TIME_SCOPE_RE = re.compile(
    r"\b(?:right\s+now|today|yesterday|tomorrow|tonight|this\s+(?:week|month|year)|"
    r"(?:last|next)\s+(?:week|month|year)|in\s+\d{4}|on\s+\d{4}-\d{2}-\d{2})\b", re.I)


def _web_followup_query(text: str, last_user: str | None) -> str | None:
    """Resolve only a short public topic/time continuation of a web lookup."""
    if (not last_user or not _looks_like_live_web_lookup(last_user)
            or _has_no_web_constraint(last_user)
            or _has_private_web_context(text)
            or _NARRATIVE_CONTEXT_RE.search(_normalize_routing_text(text))
            or len(text.split()) > 20):
        return None
    match = re.match(r"\s*(?:and(?:\s+in)?|what\s+about|how\s+about)\s+(.+)",
                     text, re.I)
    if not match:
        return None
    topic = match.group(1).strip(' ?.!')
    if re.match(r"(?:please\s+)?(?:explain|write|debug|fix|translate|calculate|"
                r"solve|refactor|implement|open|close|summarize)\b", topic, re.I):
        return None
    # A new complete request owns its own scope and needs no inherited query.
    if _looks_like_live_web_lookup(text):
        return text
    current_scope = _WEB_TIME_SCOPE_RE.search(topic)
    prior_scope = _WEB_TIME_SCOPE_RE.search(last_user)
    if current_scope:
        remainder = _WEB_TIME_SCOPE_RE.sub('', topic).strip(' ,?!.')
        if not remainder:
            # A time-only continuation keeps the prior topic, replacing all
            # prior temporal qualifiers instead of appending contradictory ones.
            previous = _WEB_TIME_SCOPE_RE.sub('', last_user).strip(' ?.!')
            previous = re.sub(r"\b(?:latest|current|currently|recent(?:ly)?)\b",
                              '', previous, flags=re.I)
            return re.sub(r"\s+", ' ', f"{previous} {current_scope.group(0)}").strip()
        return f"{topic} news"
    return f"{topic} news {prior_scope.group(0) if prior_scope else 'latest'}"


def _has_live_lookup_write_intent(text: str) -> bool:
    """Distinguish public lookup topics from separately requested actions."""
    normalized = _normalize_routing_text(text)
    if (_EXPLICIT_WEB_SEARCH_RE.search(normalized)
            and re.match(r"\s*(?:(?:please|can\s+you|could\s+you)\s+)?"
                         r"(?:search|research|look\s+up|find|check|google|bing)\b",
                         normalized, re.I)):
        # Verbs inside the requested search topic ("climate change", "how to
        # delete a file") are not instructions to mutate local state. Keep a
        # separately requested action such as "and text Mom a summary".
        clauses = re.split(r"\b(?:and(?:\s+then)?|then)\s+|[;.!?]\s*",
                           normalized, flags=re.I)
        normalized = ' '.join(clauses[1:])
    normalized = re.sub(
        r"\bbook\s+(?:bans?|industry|market|sales|publishing|stores?|authors?|"
        r"awards?|releases?)\b",
        "publishing topic",
        normalized,
        flags=re.I,
    )
    # A cancelled lookup is a constraint, not a calendar cancellation action.
    normalized = re.sub(
        r"\b(?:cancel|skip|stop|abort)\s+(?:(?:the|that|this|any)\s+)?"
        r"(?:web\s+search|online\s+(?:search|lookup)|browsing|searching\s+the\s+web|"
        r"search|lookup)\b",
        '', normalized, flags=re.I)
    return has_write_intent(normalized)


def _pin_ling_web_decision(decision: RouteDecision) -> RouteDecision:
    """Apply after finalization so execution-contract widening cannot undo it."""
    decision.model = _LING_WEB_MODEL
    decision.forbidden_tools = frozenset(
        set(decision.forbidden_tools) | {"run_shell", "http_request"})
    if decision.tool_subset is not None:
        decision.tool_subset = [
            name for name in decision.tool_subset
            if name not in decision.forbidden_tools
        ]
    return decision


def _direct_web_search(query: str, reason: str) -> RouteDecision:
    """Run dedicated search first and keep its narration on low-latency Ling."""
    decision = _mk_direct([("web_search", {"query": query})], reason, light=False)
    decision.tool_argument_bindings = {"web_search": {"query": query}}
    decision.required_tool_groups = (frozenset({"web_search"}),)
    # The one-tool subset already withholds these; keep the prohibition
    # explicit as defense in depth against an invented API-key fallback.
    decision.forbidden_tools = frozenset({"run_shell", "http_request"})
    decision.resolved_request = (
        f"Find and summarize {query}. Report only supported findings from the "
        "search results; say when coverage is insufficient."
    )
    return decision


async def route(text: str, *,
                last_user: str | None = None,
                recent_users: list[str] | None = None,
                last_assistant: str | None = None,
                last_tools: str | None = None) -> RouteDecision:
    # A topic substitution keeps the preceding operation. "And in biotech?"
    # after news asks for news, even if the new topic has its own data tool.
    followup_query = _web_followup_query(text, last_user)
    web_opt_out = _has_no_web_constraint(text)
    live_web_lookup = bool(
        _looks_like_live_web_lookup(text) or followup_query)
    live_lookup_write = _has_live_lookup_write_intent(text)
    if web_opt_out and not live_lookup_write and not _has_private_web_context(text):
        decision = _finalize(_mk(
            "agent",
            reason="live-information wording with explicit no-web request -> answer without tools",
        ), text)
        return _pin_ling_web_decision(decision)
    if live_web_lookup and not live_lookup_write:
        query = followup_query or text
        decision = _direct_web_search(
            query, "current public information -> web_search on Ling (router-direct)")
        return _pin_ling_web_decision(_finalize(decision, text))
    # Resolve this before the outbound workflow: its conversational “send me”
    # means display the summary in Wisp, not deliver it through another app.
    if (args := _inline_email_summary_args(text)) is not None:
        return _finalize(_mk_direct(
            [("summarize_emails", args)],
            "email summary requested in Wisp -> summarize_emails (router-direct)"), text)
    # Time answers continue the authorized reminder request, not a new generic
    # calendar read. A missing-item complaint first checks real reminders;
    # it must not repeat the previous memory-save substitution.
    if last_assistant and asks_alert_time(last_assistant):
        if is_unsupported_time_answer(text):
            d = _mk_scoped(["get_upcoming"], "reminder clock needs clarification",
                           expect=False, light=False)
            d.reminder_action = "clarify_time"
            return _finalize(d, text)
        if is_time_answer(text):
            d = _mk_scoped(["get_upcoming", "add_reminder"],
                           "reminder alert-time answer", light=False, multi=True)
            d.reminder_action = "create"
            return _finalize(d, text)
        # The user may correct ownership or say which reminder app they mean
        # without answering the still-missing lead time. Keep the unfinished
        # reminder intent instead of routing the word “reminders” as a fresh
        # calendar read and leaking the raw schedule when narration is empty.
        if (last_user and _REMINDER_CREATE_RE.search(last_user)
                and re.search(r"\b(?:reminders?|for\s+me|myself|i\s+need\s+it|"
                              r"do\s+it)\b", text, re.I)):
            d = _mk_scoped(["get_upcoming"],
                           "reminder clarification still awaiting alert time",
                           expect=False, light=False)
            d.reminder_action = "clarify_time"
            return _finalize(d, text)
    # Cross-tool reports need an execution plan, not a bag of related schemas.
    # Check this before generic confirmations and compound decomposition so a
    # bare "yes" can recover the prior recipient/channel and so the send step
    # cannot be replaced by a neighboring calendar or mail-management action.
    if (outbound := _source_outbound_subset(
            text, last_user=last_user, recent_users=recent_users,
            last_assistant=last_assistant,
            last_tools=last_tools)) is not None:
        decision = _finalize(outbound, text)
        if (_looks_like_live_web_lookup(text)
                and "web_search" in (decision.tool_subset or ())):
            return _pin_ling_web_decision(decision)
        return decision
    if (offered_text := _offered_text_confirmation(text, last_assistant)) is not None:
        return _finalize(offered_text, text)
    if (last_assistant and re.fullmatch(
            r"\s*(?:i\s+)?(?:don'?t|do not|can'?t|cannot)\s+(?:see|find)\s+(?:it|the reminder)[.!?]*\s*",
            text, re.I) and re.search(r"\breminder\b", last_assistant, re.I)):
        prior = {n.strip() for n in (last_tools or "").split(",")}
        if prior & {"get_upcoming", "remember", "add_reminder", "update_reminder"}:
            d = _mk_scoped(["get_upcoming"], "verify missing reminder, not calendar event",
                           light=False, force="get_upcoming")
            if not (prior & {"add_reminder", "update_reminder"}):
                d.reminder_action = "clarify_time"
            return _finalize(d, text)
    # Checked before EVERYTHING else, including rule_route: a bare "yes"/"go
    # ahead" would otherwise match TRIVIAL_RE and get sent to the tool-less
    # fast model, even though it's confirming an action the assistant just
    # offered. This is the fix for having to be unnaturally literal with follow-ups.
    if confirms_offered_action(text, last_assistant):
        # "yes" / "go ahead" — the tools to offer are whatever the PREVIOUS turn
        # was working with, not the entire registry.
        #
        # Verified failure 2026-08-09: "yes archive them", right after Wisp had
        # listed emails, was handed all 55 tools — the configuration this
        # project measured as WORST at tool selection (5-7 tools -> 3/3 correct
        # calls, all 44 -> 2/3). It called view_emails again and never
        # archive_email. That is the exact moment accuracy matters most: the
        # user has already said do it.
        #
        # `last_tools` is the previous assistant turn's tool_digest, so a
        # confirmation inherits that turn's domain — reusing the same mapping
        # _fragment_continuation uses, plus the domain's WRITE tools, because a
        # confirmation is nearly always approving an action rather than another
        # read. Falls back to the full toolset when the previous turn used no
        # tools, which is the case this can't infer anything about.
        if (inherited := _confirmation_subset(last_tools)) is not None:
            return _finalize(inherited, text)
        # No inheritable domain (the previous turn used no tools). Retrieve on
        # the ASSISTANT's last message rather than on the user's "yes" — "yes"
        # carries no signal at all, while the offer being confirmed describes
        # the action in full. Falling back to the whole registry here is the
        # documented worst case: "yes archive them" was handed all 55 schemas
        # and called view_emails instead of archive_email, at the exact moment
        # the user had already said do it.
        d = _mk("agent", tools=True, reason="confirms an action the assistant just offered")
        d.tool_subset = await _semantic_core(last_assistant or text)
        d.multi_round = True
        return _finalize(d, text)
    # A bare scope fragment ("from yesterday") continuing the previous
    # light-read turn -> stay on that same the summarizer domain instead of falling to
    # the the agent model default. Checked before rule_route since the fragment names
    # no domain of its own and rule_route would find nothing to match.
    # Answering Wisp's own "text or email?" — checked BEFORE rule_route, which
    # would otherwise read a bare "email" as an inbox lookup and hand back a
    # read-only subset, leaving Wisp unable to act on the answer to the question
    # it had just asked. See _channel_answer_subset.
    if (chan := _channel_answer_subset(text, last_assistant, last_tools)) is not None:
        return _finalize(chan, text)
    if (reply := _contextual_reply_subset(text, last_tools)) is not None:
        return _finalize(reply, text)
    if (notify := _notify_correction_subset(text, last_tools)) is not None:
        return _finalize(notify, text)
    if (correction := _reminder_correction_subset(text, last_tools)) is not None:
        return _finalize(correction, text)
    if (repair := _reminder_repair_subset(text, last_assistant, last_tools)) is not None:
        return _finalize(repair, text)
    if (cont := _fragment_continuation(text, last_tools)) is not None:
        return _finalize(cont, text)
    # Quoted or hypothetical text can contain highly actionable words while
    # explicitly asking only for an explanation. These recurring shapes must
    # not be decomposed into machine actions from the quoted content.
    if (re.match(r"\s*(?:explain\s+(?:this\s+error\s+message|why\s+a\s+variable|"
                 r"the\s+musical\s+notes)|if\s+i\s+said\b|"
                 r"summari[sz]e\s+only\s+this\s+supplied\s+email)", text, re.I)
            and re.search(r"\b(?:do\s+not|don'?t)\b", text, re.I)):
        return _finalize(_mk("fast", reason="quoted/hypothetical explanation only"), text)
    if (compound := await _compound_route(text)) is not None:
        return compound
    decision = rule_route(text)
    if decision is not None and decision.needs_tools and decision.tool_subset is None:
        retrieved = await _semantic_core(text)
        decision.tool_subset = retrieved
        decision.reason += f" -> retrieved tools ({len(retrieved)})"
        # Default: heterogeneous by construction — the rule established that a
        # tool is wanted but not which domain, so these are not alternatives
        # the way one domain's read/summarize pair is. Same reasoning as the
        # ambiguous fallback below sets it.
        #
        # EXCEPTION: a domain rule that already knows its own tools ARE
        # alternatives (_mk_scoped's `multi` param, carried through as
        # multi_round_on_retrieval) — e.g. apps/media or device control, where
        # "open safari" is done after one open_app call. Forcing multi_round
        # True there would be a real regression: it disables the
        # narration-thinking-skip for a route that never needed several tools
        # in the first place, undoing a measured win (see narration_after's
        # docstring) for no reason tied to retrieval at all.
        decision.multi_round = (decision.multi_round_on_retrieval
                                if decision.multi_round_on_retrieval is not None
                                else True)
    if decision is None:
        # Before falling back to the generic core set: does this CONTINUE the
        # write the previous turn just made? "set some more the day before it"
        # names no domain and matches no rule, so it landed on the core tools —
        # which contain no add_reminder — and the model answered "I don't have
        # the ability to create new calendar events or reminders with my
        # available tools." It does; it just wasn't handed them. Inheriting the
        # previous turn's domain is the same trick _confirmation_subset uses for
        # a bare "yes". See _WRITE_CONTINUATION_RE.
        if (wcont := _write_continuation_subset(text, last_tools)) is not None:
            return _finalize(wcont, text)
        # No separate LLM-classify step for local requests anymore (previously
        # the summarizer, or the agent model itself when resident, or the Air). It was a real,
        # measured reliability problem: a live 20-prompt test found ~15% of
        # genuinely tool-needing requests got silently mis-classified as
        # needs_tools=False by that extra step — before the agent model ever got a
        # chance to see the request, so no amount of tool-calling hardening in
        # the agent loop could save it. On top of that it cost either an Air
        # round-trip or a small-model swap, for a latency difference the user
        # found negligible. Ambiguous prompts now go straight to the agent model via
        # the agent loop with tools AVAILABLE — tool_choice stays "auto"
        # (expect_tool_first=False), so the agent model decides for itself rather than
        # being forced, which avoids the opposite failure mode (forcing a tool
        # call on a genuinely conversational prompt and getting a refusal
        # instead of an answer — see expect_tool_first's docstring).
        #
        # It no longer offers the WHOLE registry, though. That was 55 schemas
        # (~9,600 tokens) plus the unscoped system prompt (~4,800) — 14,400
        # tokens of fixed overhead before the user's message, on the route that
        # also measures worst at picking a tool (5-7 offered -> 3/3 correct
        # calls, all 44 -> 2/3, and it reached for an unrelated tool). It now
        # gets _CORE_TOOLS: the user's own data, their memory, and run_shell as
        # an escape hatch. See _CORE_TOOLS for why that covers this route's real
        # traffic.
        #
        # Built with _mk rather than _mk_scoped on purpose — _mk_scoped defaults
        # expect_tool_first=True, and forcing a tool call here is exactly the
        # failure this route exists to avoid ("any new ideas for the project"
        # getting refused because nothing sensible was callable).
        # RETRIEVED, not static, since 2026-08-18. `_CORE_TOOLS` is a fixed list
        # of 14 hand-picked names, which was the right answer while the registry
        # held ~54 tools and every prompt landing here was a conversational
        # follow-up. It does not survive the registry growing: a static core
        # cannot contain a timer tool, a file-search tool and a transit tool at
        # once without becoming the 44-tool menu this route was scoped to escape.
        #
        # So the fallback now EMBEDS the request and offers the ~10 best-matching
        # tools out of the whole registry (see router/semantic.py). The menu the
        # model sees stays the same size no matter how large the registry gets —
        # which is the only number that affects either the context budget or
        # selection accuracy.
        #
        # The static list stays as the safety net, deliberately: if the embedder
        # is unreachable (oMLX down, model not installed, request timed out)
        # this route must still work. Offering the previous 14 tools is a
        # degraded fallback; offering nothing is a broken turn.
        core = await _semantic_core(text)
        decision = _mk("agent", tools=True, expect_tool_first=False, source="default",
                       reason=f"ambiguous -> retrieved tools ({len(core)}), model decides")
        decision.tool_subset = core
        # multi_round=True: the core set is heterogeneous (calendar, notes,
        # mail, messages, memory, run_shell, …) — its tools are NOT alternatives
        # the way a single domain's read/summarize pair is, so the broad
        # narration mode's "stop thinking once anything answered" premise does
        # not hold here. See RouteDecision.multi_round.
        decision.multi_round = True
    return _finalize(decision, text)
