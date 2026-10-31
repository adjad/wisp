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
  "did you send it?").
Calls to tools that were not offered are handled before this by the loop
(they load a schema and never run that step, or are refused as unoffered).
"""
from __future__ import annotations

import re

_SEND_VERBS = re.compile(
    r"\b(?:send|sent|te+x+t|txt|e-?mail|mail|message|msg|imessage|dm|tell|reply|respond|"
    r"forward|fwd|share|notify|ping|invite|write\s+back)\b|\blet\s+\w+\s+know\b|\bask\s+\w+\s+(?:if|whether|to|for|about)\b",
    re.I)
_DELETE_VERBS = re.compile(
    r"\b(?:delete|remove|cancel|clear|erase|trash|forget|drop|wipe|purge|kill|stop|end|scrap|"
    r"empty|unsubscribe|call\s+off|get\s+rid\s+of|clean\s+(?:up|out)|turn\s+off)\b", re.I)
_MOVE_VERBS = re.compile(
    r"\b(?:move|moved|rename|reschedule|push|shift|bump|postpone|delay|bring\s+forward|organi[sz]e|"
    r"reorgani[sz]e|sort|tidy|clean\s+up|file|put|dump|relocate|separate|group|archive|change|switch|"
    r"make\s+(?:it|that)|missed)\b", re.I)
_WRITE_VERBS = re.compile(
    r"\b(?:add|create|make|set|remind|nudge|book|pencil|block|put|save|remember|note|jot|write|draft|"
    r"update|change|fix|correct|mark|check\s+(?:it|that|this)?\s*off|complete|done|finish|open|launch|start|"
    r"quit|close|play|pause|resume|skip|turn|toggle|mute|unmute|lock|copy|paste|log|schedule|scedule|"
    r"shedule|order|enable|disable)\b", re.I)
_QUESTION_HEAD = re.compile(
    r"^\s*(?:(?:hey|um+|uh+|so|ok(?:ay)?|wisp)[,\s]+)*(?:what|when|where|who|whom|whose|which|why|how|is|"
    r"are|was|were|do|does|did|have|has|had|any|anything|am)\b", re.I)
_STATUS_QUESTION = re.compile(
    r"^\s*(?:(?:hey|um+|uh+|so|ok(?:ay)?)[,\s]+)*(?:did|have|has|was|were)\s+(?:you|i|it|that|this|they|she|he|"
    r"my|the)\b", re.I)
_IMMEDIATE = re.compile(r"\b(?:now|right\s+now|right\s+away|immediately|asap|straight\s+away)\b", re.I)
_SEND_TIME = re.compile(
    r"\b(?:later|tonight|this\s+(?:evening|afternoon))\b"
    r"|\bat\s+\d|\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)\b|\bnoon\b|\bmidnight\b"
    r"|\b(?:tomorrow|tmrw|tmrow|tomorow|mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\s+"
    r"(?:morning|afternoon|evening|night|at)\b"
    r"|\bin\s+(?:an?|\d+|a\s+few)\s+(?:min(?:ute)?s?|hours?)\b|\b(?:on|next|this)\s+(?:mon|tue|wed|thu|fri|sat|sun)",
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
                           "delete_knowledge", "cancel_scheduled_send", "clear_memory", "forget"})
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

    if read_only_question(prompt):
        return stop("the user asked a question and did not ask for any action",
                    "Answer the question from reads only; offer the action if it would help.")
    kind = _kind(tool, args)
    if name == "schedule_send":
        if _IMMEDIATE.search(prompt) and not _SCHEDULE_ASK.search(prompt):
            return stop("the user asked to send it now, not later",
                        "Use send_message or send_email for an immediate send, or ask when it should go.")
        if not (_SEND_TIME.search(said) or _SCHEDULE_ASK.search(said)):
            return stop("the user named no later send time",
                        "Use send_message or send_email to send it now, or ask the user when it should go.")
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
    if kind == "delete" and not _DELETE_VERBS.search(said):
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
    if kind == "move" and not _MOVE_VERBS.search(said):
        return stop("the user did not ask for anything to be moved or rescheduled",
                    "Ask before moving anything.")
    return None
