"""Hard stops for model-led calls the user's own words do not support.

The model picks every tool on a model-led turn. These checks run in code,
before a call's policy decision and card, whatever the model or its prompt
says, and only ever REFUSE: the call does not run, the model is told plainly
why and what to do instead (call the right tool, or ask the user), and its
arguments are never rewritten. Confirmation cards, full access and the policy
tiers are unchanged; forget still runs unconfirmed under full access when the
user named what to forget.

Stops (toolbench 2026-10-31 unsafe_call cases, Gemma and Bolt):
- schedule_send when the user said now, or named no send time
  (compose_weather_text, compose_self_email_now); an immediate send when the
  user asked to schedule it.
- forget with a broad scope ("everything from my mail"), or a scope the
  user's words do not name (mem_forget_everything_mail).
- remember for a passing remark the user did not ask to keep
  (mem_not_fact_recruiter, mem_not_fact_sister_moving).
- a send, delete or move the user did not ask for: the request (or the
  user's previous message) has to say it, or answer the assistant's own
  offer (files_sort_downloads_by_type -> move_path, dev_timer_cancel_all ->
  clear_reminders).
- any write or send on a read-only question ("what's on my calendar?",
  "did you send it?"). A skill tool or an MCP tool is not stopped for this:
  a question is how people use them ("what issues are assigned to me?"),
  Wisp cannot tell their lookups from their writes, and each one always
  shows its own card first.
Calls to tools that were not offered are handled before this by the loop
(they load a schema and never run that step, or are refused as unoffered).
"""
from __future__ import annotations

import re

_SEND_VERBS = re.compile(
    r"\b(?:send|sent|te+x+t|txt|e-?mail|mail|message|msg|imessage|dm|tell|reply|respond|"
    r"forward|fwd|share|notify|ping|invite|write\s+back)\b"
    # "let my mom know", "ask the whole team if": one to three words between.
    r"|\blet\s+(?:[\w'’]+\s+){1,3}know\b|\bask\s+(?:[\w'’]+\s+){1,3}(?:if|whether|to|for|about)\b",
    re.I)
# "take the dentist off my calendar" counts; "take friday off" does not.
_DELETE_VERBS = re.compile(
    r"\b(?:delete|remove|cancel|clear|erase|trash|forget|drop|wipe|purge|kill|stop|end|scrap|"
    r"empty|unsubscribe|call\s+off|get\s+rid\s+of|clean\s+(?:up|out)|turn\s+off|"
    r"take\s+(?:\S+\s+){1,5}off\s+(?:of\s+)?(?:my|the|our))\b", re.I)
# Ways to call off a send that is waiting: "don't send that text", "hold off
# on that email". Accepted for cancel_scheduled_send only (the user sees and
# approves the exact item every time): these words do not ask for an event,
# reminder, file or memory to be removed. "draft it but don't send it" is
# about the draft, so "it" does not count.
_UNSEND_WORDS = re.compile(
    r"\b(?:(?:(?:don['’]?t|do\s+not)\s+send|hold\s+off\s+on)\s+(?:that|the|those|my)|unsend)\b", re.I)
# "transfer report.pdf to Documents" is a move; the site transfer.sh is not.
_MOVE_VERBS = re.compile(
    r"\b(?:move|moved|rename|reschedule|push|shift|bump|postpone|delay|bring\s+forward|organi[sz]e|"
    r"reorgani[sz]e|sort|tidy|clean\s+up|file|put|dump|relocate|separate|group|archive|change|switch|"
    r"make\s+(?:it|that)|missed|transfer(?![.-]\w))\b", re.I)
# More ways to ask for a calendar event to change. Accepted for update_event
# only, which always shows its card: "update" and "set the…" are too loose to
# let a file move run on (move_path can run with no card). "update" is the
# verb, not the noun in "my portfolio update for today".
_EVENT_CHANGE = re.compile(
    r"\b(?:update(?!\s+(?:for|on|from|about|of)\b)(?=\s+\S)|edit|extend|shorten|lengthen|adjust|modify|"
    r"(?:set|make)\s+(?:the|my)|needs?\s+to\s+be|got\s+pushed)\b", re.I)
_WRITE_VERBS = re.compile(
    r"\b(?:add|create|make|set|remind|nudge|book|pencil|block|put|save|remember|note|jot|write|draft|"
    r"update|change|fix|correct|mark|check\s+(?:it|that|this)?\s*off|complete|done|finish|open|launch|start|"
    r"quit|close|play|pause|resume|skip|turn|toggle|mute|unmute|lock|copy|paste|log|schedule|scedule|"
    r"shedule|order|enable|disable)\b", re.I)
# "do it", "do that", "do both", "do the first one" and "do not disturb on" are
# commands, not questions: an object pronoun or "not" cannot follow a question's
# "do". "do that/this/both/the first…" count only when nothing but a filler
# follows, so "do those emails mention it?" and "do the kids have school?" stay
# questions, as does every "do you/I/we/they…".
_DO_COMMAND = (
    r"(?!\s+(?:it|so|them|not)\b)"
    r"(?!\s+(?:that|this|these|those|both|the\s+(?:first|second|third|last|same))"
    r"(?:\s+(?:one|ones|two|thing|option))?(?:\s+(?:please|pls|then|too|again|instead|for\s+me))*"
    r"\s*[.!]*\s*$)")
_QUESTION_HEAD = re.compile(
    r"^\s*(?:(?:hey|um+|uh+|so|ok(?:ay)?|wisp)[,\s]+)*(?:what|when|where|who|whom|whose|which|why|how|is|"
    r"are|was|were|do" + _DO_COMMAND + r"|does|did|have|has|had|any|anything|am)\b", re.I)
# "have it remind me at 5" is a command ("have it/that/this…" is never a
# question: that would be "has it…"), so "have" takes a question's subjects only.
_STATUS_QUESTION = re.compile(
    r"^\s*(?:(?:hey|um+|uh+|so|ok(?:ay)?)[,\s]+)*(?:(?:did|has|was|were)\s+(?:you|i|it|that|this|they|she|he|"
    r"my|the)|have\s+(?:you|i|they|my|the))\b", re.I)
# A short reply that answers the assistant's own open question with a time or
# a choice: "What time should I remind you?" -> "how about 6pm".
_ANSWER_HEAD = re.compile(
    r"^\s*(?:(?:hey|um+|uh+|so|ok(?:ay)?)[,\s]+)*(?:(?:how|what)\s+about\b|any\s?time\b|does\s+\d)", re.I)
_OPEN_QUESTION = re.compile(
    r"^\W*(?:(?:and|so|ok(?:ay)?|sure|got\s+it|great|alright)[,.!\s]+)*(?:(?:at|for|on|to|by|until)\s+)?"
    r"(?:what|when|which|who|whom|where|how\s+(?:long|many|much|often|early|late|soon))\b", re.I)
_IMMEDIATE = re.compile(r"\b(?:now|right\s+now|right\s+away|immediately|asap|straight\s+away)\b", re.I)
# A later time for a send. Spelled-out times ("at five", "in two hours", "in
# half an hour") and "friday", "tomorrow" or "next week" on their own count
# only when the message or the send verb comes right after ("in two hours
# that I landed", "friday saying happy birthday", "next week about the
# offsite"): at the end of the request they are part of the message ("text mom
# I'll be there in ten minutes", "text Sam the weather for tomorrow").
_HOURS = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_SEND_TIME = re.compile(
    r"\b(?:later|tonight|this\s+(?:evening|afternoon))\b"
    r"|\bat\s+\d|\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)\b|\bnoon\b|\bmidnight\b"
    r"|\b(?:tomorrow|tmrw|tmrow|tomorow|mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\s+"
    r"(?:morning|afternoon|evening|night|at|saying|to\s+say)\b"
    r"|\bin\s+(?:an?|\d+|a\s+few)\s+(?:min(?:ute)?s?|hours?)\b|\b(?:on|next|this)\s+(?:mon|tue|wed|thu|fri|sat|sun)"
    r"|\b(?:at\s+(?:" + _HOURS + r")(?:\s+(?:o['’]?clock|thirty|fifteen|forty[\s-]?five))?(?:\s*(?:am|pm))?"
    r"|in\s+(?:a\s+couple(?:\s+of)?|half\s+an|" + _HOURS + r"|fifteen|twenty|thirty|forty[\s-]?five|ninety)"
    r"\s+(?:min(?:ute)?s?|hours?))(?:\s+(?:today|tomorrow))?"
    r"\s+(?:that|saying|to\s+say|te+x+t|e-?mail|message|send|tell)\b"
    r"|(?<!\bfor\s)(?<!\babout\s)(?<!\bof\s)(?<!\band\s)\bnext\s+week\s+(?:about|saying|to\s+say)\b",
    re.I)
_SCHEDULE_ASK = re.compile(
    r"\b(?:schedule|scedule|shedule)\s+(?:a|an|the|this|that|my|it|me)?\s*(?:text|e-?mail|message|send|it|this)\b"
    r"|\bsend\s+(?:it|this|that)\s+(?:later|tonight)\b", re.I)
_REMEMBER_ASK = re.compile(
    r"\b(?:remember|save|note|keep\s+in\s+mind|don['’]?t\s+forget|store|memori[sz]e|keep\s+track|"
    r"update\s+(?:that|it|this|my)|correct\s+(?:that|it|this)|from\s+now\s+on|for\s+(?:the\s+)?future|"
    r"call\s+me)\b", re.I)
_BROAD_FORGET = re.compile(
    r"\b(?:everything|all|anything|every|whole|entire|any)\b|\b(?:from|in|about)\s+(?:my\s+)?"
    r"(?:mail|e-?mails?|inbox|messages?|texts?|notes?|calendar|history)\b", re.I)
_STOP = frozenset("the a an my me i to of for and that this is am are was it be you your about from "
                  "please pls forget remember".split())

_BULK_SORT = re.compile(r"\b(?:sort|organi[sz]e|reorgani[sz]e|tidy|clean\s+up|group|separate)\b", re.I)
_TIMERS = re.compile(r"\b(?:timers?|alarms?|stopwatch)\b", re.I)
_SCHEDULE_ITEMS = re.compile(r"\b(?:events?|meetings?|appointments?|class(?:es)?|calendar|remind\w*|"
                             r"to-?dos?|tasks?)\b", re.I)

_SEND_CATEGORIES = frozenset({"email_send", "messages_send", "scheduled_send"})
_DELETE_TOOLS = frozenset({"cancel_event", "clear_reminders", "clear_past_reminders", "delete_path",
                           "delete_knowledge", "cancel_scheduled_send", "clear_memory", "forget",
                           "delete_note"})
_IMMEDIATE_SENDS = frozenset({"send_message", "send_email"})


def _words(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]+", (text or "").casefold())
            if len(w) > 2 and w not in _STOP}


def _kind(tool, args: dict) -> str:
    """send | delete | move | write for an effectful call."""
    if tool.category in _SEND_CATEGORIES:
        return "send"
    if tool.name in _DELETE_TOOLS or tool.category == "fs_delete":
        return "delete"
    if tool.name == "manage_timers" and str(args.get("action") or "").startswith("cancel"):
        return "delete"
    if tool.name in {"move_path", "update_event"} or (tool.name == "organize_files" and args.get("confirm")):
        return "move"
    return "write"


def _answers_send_offer(prompt: str, last_assistant: str) -> bool:
    """A short reply to the assistant's own question about a send ("Who should
    I text it to?" -> "dad"; "Messages or email?" -> "messages")."""
    from service.router.model_led import continuation_send_exemption
    if continuation_send_exemption(prompt, last_assistant):
        return True
    if not last_assistant or "?" not in last_assistant or len(prompt.split()) > 6:
        return False
    question = last_assistant[last_assistant.rfind(".", 0, last_assistant.rfind("?")) + 1:]
    return bool(_SEND_VERBS.search(question)) and not _DELETE_VERBS.search(prompt)


def read_only_question(prompt: str) -> bool:
    """A question that asks for information and names no action:
    "what's on my calendar?", "did you send it?"."""
    text = prompt or ""
    if _STATUS_QUESTION.match(text):
        return True
    asks = bool(_QUESTION_HEAD.match(text))
    return asks and not (_WRITE_VERBS.search(text) or _SEND_VERBS.search(text)
                         or _DELETE_VERBS.search(text) or _MOVE_VERBS.search(text))


def _asks_at_its_own_card(tool) -> bool:
    """Calls a question may still reach: skill and MCP tools. The user answers
    each one at its card in every access mode, and no standing grant skips it
    (policy._ALWAYS_CONFIRM_EXTENSION). Nothing else: shell commands,
    AppleScript and app controls stay stopped on a question."""
    from service.safety.policy import OPAQUE_ACTION_CATEGORIES
    return tool.category in OPAQUE_ACTION_CATEGORIES


def _answers_own_question(prompt: str, last_assistant: str) -> bool:
    """The user is replying to the assistant, not asking something new: a bare
    assent to its closing offer ("Should I set a reminder?" -> "ok do"), or a
    short "how about 6pm" to its closing open question ("What time?"). The
    assistant's reply has to END with that question, and "did you send it?"
    never counts. Only the read-only-question stop is lifted; every other
    stop still runs."""
    if not last_assistant or _STATUS_QUESTION.match(prompt or ""):
        return False
    from service.router.model_led import answers_offer, continuation_send_exemption
    if answers_offer(prompt, last_assistant) or continuation_send_exemption(prompt, last_assistant):
        return True
    if not _ANSWER_HEAD.match(prompt) or len(prompt.split()) > 6:
        return False
    end = last_assistant.rfind("?")
    if end < 0 or re.search(r"[A-Za-z0-9]", last_assistant[end + 1:]):
        return False
    question = re.split(r"(?<=[.!?\n])\s+", last_assistant[:end + 1].strip())[-1]
    return bool(_OPEN_QUESTION.match(question))


def problem(tool, args: dict, prompt: str, *, previous_user: str = "", last_assistant: str = "") -> str | None:
    """Why this model-led call must not run, as a tool result, or None."""
    from service.router.model_led import effectful_call
    args = args if isinstance(args, dict) else {}
    if tool is None or not effectful_call(tool, args):
        return None
    name = tool.name
    said = f"{prompt} {previous_user}".strip()

    def stop(why: str, instead: str) -> str:
        return (f"({name} was NOT run: {why}. Nothing was changed or sent; do not say it ran. "
                f"{instead})")

    if (read_only_question(prompt) and not _asks_at_its_own_card(tool)
            and not _answers_own_question(prompt, last_assistant)):
        return stop("the user asked a question and did not ask for any action",
                    "Answer the question from reads only; offer the action if it would help.")
    kind = _kind(tool, args)
    if name == "schedule_send":
        # "text Sam at 6pm tonight saying I'm leaving now": a "now" that comes
        # after the send time belongs to the message, not to when it goes.
        now, timed = _IMMEDIATE.search(prompt), _SEND_TIME.search(prompt)
        if now and not _SCHEDULE_ASK.search(prompt) and not (timed and timed.start() < now.start()):
            return stop("the user asked to send it now, not later",
                        "Use send_message or send_email for an immediate send, or ask when it should go.")
        if not (_SEND_TIME.search(said) or _SCHEDULE_ASK.search(said)):
            return stop("the user named no later send time",
                        "If the user gave any time or day for it, ask them when it should go and "
                        "do not send it now. If they gave none, use send_message or send_email.")
    if name in _IMMEDIATE_SENDS and _SCHEDULE_ASK.search(prompt):
        return stop("the user asked to schedule it for later",
                    "Use schedule_send with the time they gave, or ask what time they meant.")
    if name == "forget":
        query = str(args.get("query") or "")
        if _BROAD_FORGET.search(query) or _BROAD_FORGET.search(prompt):
            return stop("forget removes one fact the user names; it cannot forget a whole source, topic "
                        "or everything",
                        "Tell the user that broad scope is not supported here, and offer to forget "
                        "specific facts one by one (they can review memories with /memory).")
        if not _words(query) & _words(said):
            return stop("its query does not match what the user asked to forget",
                        "Call forget with the user's own words for the fact, or ask which fact they mean.")
    if name == "clear_memory" and not re.search(r"\b(?:clear|erase|wipe|delete)\s+(?:all\s+)?(?:of\s+)?"
                                                 r"(?:my\s+|your\s+)?memor", prompt, re.I):
        return stop("the user did not ask to erase all memory", "Ask the user what they want removed.")
    if name == "remember" and not _REMEMBER_ASK.search(said):
        return stop("the user did not ask Wisp to remember this",
                    "Reply to what they said; offer to remember it only if it seems useful.")
    if kind == "send" and not (_SEND_VERBS.search(said) or _answers_send_offer(prompt, last_assistant)):
        return stop("the user did not ask for anything to be sent", "Ask before sending anything.")
    if kind == "delete" and not (_DELETE_VERBS.search(said)
                                 or (name == "cancel_scheduled_send" and _UNSEND_WORDS.search(said))):
        return stop("the user did not ask for anything to be deleted or cancelled",
                    "Ask before deleting anything.")
    if kind == "delete" and name in {"clear_reminders", "clear_past_reminders"} and \
            not re.search(r"\b(?:remind\w*|to-?dos?|tasks?|overdue|old\s+ones)\b", said, re.I):
        return stop("the user did not name reminders", "Delete only what the user named (timers: manage_timers).")
    if kind == "delete" and name in {"cancel_event", "clear_reminders", "clear_past_reminders"} \
            and _TIMERS.search(prompt) and not _SCHEDULE_ITEMS.search(prompt):
        return stop("the user asked about timers, not calendar events or reminders",
                    "Use manage_timers for timers and alarms.")
    if name == "move_path" and _BULK_SORT.search(prompt) and not re.search(r"\bmove\b", prompt, re.I):
        return stop("sorting or organizing a folder is organize_files, which previews first",
                    "Call organize_files for the folder (its preview changes nothing).")
    if kind == "move" and not (_MOVE_VERBS.search(said)
                               or (name == "update_event" and _EVENT_CHANGE.search(said))):
        return stop("the user did not ask for anything to be moved or rescheduled",
                    "Ask before moving anything.")
    return None
