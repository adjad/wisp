"""model_only: sends every /agent turn to the model (see model_only_enabled),
plus the answer-quality guards a model-only Ask turn runs with.

The guards are pure helpers: they never import tools, read user stores or call
inference. service/agent/loop.py applies them only when its caller passes
`ask_quality=True`, which service/main.py does for model-led turns in
model-only mode. Measured on 1,368 model-only corpus rows (2026-10-23):

* ~40% of turns ended empty because oMLX's prefill memory guard rejected the
  prompt. One turn grew to 38k tokens across repeated reads; after it the guard
  rejected every request, even 4.5k-token ones, until the model was reloaded.
  Hence a hard per-step prompt budget, smaller tool results, a cap on repeated
  calls to one tool, and one trimmed retry when the guard answers 400.
* 28 answers were raw tool dumps (mostly turns that looped on one tool until
  the step limit). Hence a short summarization retry, then compact_results().
* With thinking on, ~27% of final answers were written twice: once inside the
  reasoning, once as the answer. Hence dedupe_reasoning() and the narration
  thinking flag (narration_thinking_enabled).
"""
from __future__ import annotations

import math
import os
import re
from contextvars import ContextVar
from datetime import date, timedelta


# Set by service/main.py for one /agent turn that cannot be model-led (an agent
# role bound to a cloud or local-provider endpoint): that turn takes the rules
# path, and the agent loop must follow it rather than the global setting.
_TURN_OFF: ContextVar[bool] = ContextVar("wisp_model_only_turn_off", default=False)


def disable_for_turn() -> None:
    """Turn model_only off for the rest of the current task's context only."""
    _TURN_OFF.set(True)


def model_only_enabled() -> bool:
    """Every /agent turn goes to the model, nothing answers first. ON by
    default since 0.5.0-rc3 (service/config/models.yaml `model_only`).

    On, it forces model-led discovery for every turn and skips the
    deterministic layers that otherwise answer before the model: the batch
    correction guard, typed tasks, delivery workflows, router reads and
    router-direct calls, and the verbatim short-circuit of presynthesized tool
    results. In the agent loop it also states thinking ON for every
    tool-selection step and never pins temperature, so each model runs on its
    own oMLX profile. Safety is unchanged: the policy engine, confirmation
    cards, routing guard contracts and the audit log still apply to every call.

    WISP_MODEL_ONLY (true/false) wins; otherwise the `model_only` config key
    (~/.moe/config.yaml overrides the packaged default). Invalid values fail
    closed to OFF rather than raising, so a typo never breaks a turn.
    """
    if _TURN_OFF.get():
        return False
    value = os.environ.get("WISP_MODEL_ONLY")
    if value is None:
        from service.config import models_config
        try:
            value = models_config().get("model_only", False)
        except Exception:  # noqa: BLE001 - an unreadable config keeps the default path
            return False
    return _truthy(value)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def narration_thinking_enabled() -> bool:
    """Whether a model-only NARRATION step (every tool called so far answered
    cleanly, nothing is forced) keeps thinking on.

    Default OFF. Tool-selection steps always think in model-only mode; only the
    step that writes prose over results already in context drops the think
    block. Measured on the 2026-10-23 corpus, that step's reasoning was the
    final answer written out in full on 230 of 839 answered turns, and decode
    dominates latency (an output token costs ~137x a prompt token), so thinking
    there doubled the slowest part of the turn to produce a copy. V3 was trained
    thinking-off, and tool_choice stays "auto", so it can still call a tool.

    WISP_MODEL_ONLY_NARRATION_THINKING wins, then the
    `model_only_narration_thinking` config key. Invalid values mean OFF.
    """
    value = os.environ.get("WISP_MODEL_ONLY_NARRATION_THINKING")
    if value is None:
        try:
            from service.config import models_config
            value = models_config().get("model_only_narration_thinking", False)
        except Exception:  # noqa: BLE001
            return False
    return _truthy(value)


# --- budgets -----------------------------------------------------------------

# Estimated prompt tokens (the loop's chars/4 estimate) one model-only step may
# send. Real Ling tokens run ~1.1-1.5x the estimate on email/JSON text, so this
# keeps a step near 9-12k real tokens: under the size where oMLX's prefill guard
# first throttled (a 32k-token step) and far under the 38k step after which it
# rejected everything. A first step is ~5k estimated tokens.
ASK_PROMPT_BUDGET = 8000
# Extra estimated tokens a step may spend on earlier conversation, on top of
# ASK_PROMPT_BUDGET. Measured without it (scripts/measure_ask_history.py, Gemma
# 4 E4B, 16,384 window): the system prompt and schemas take ~6,250 of the 8,000,
# so a direct step kept 8 of 20 earlier exchanges and the step after a
# 6,000-character tool result kept none. Sized to hold the condensed digest
# (memory/context.DIGEST_MAX_CHARS, ~1,200) almost whole; the step stays
# around 9k estimated tokens, far under the window and the guard's 32k.
ASK_HISTORY_RESERVE = 1200
# Per-result cap in the model's context (the general loop allows 12,000).
ASK_TOOL_RESULT_CHARS = 6000
# Calls to ONE tool per turn. The corpus raw dumps were turns that called
# get_upcoming / view_emails eight times with shuffled arguments.
ASK_MAX_SAME_TOOL_CALLS = 3
# Real schemas a model-led turn starts with, and name-only stubs beyond them.
ASK_PRELOAD = 6
ASK_STUBS = 24
# Name-only stubs always offered, whatever the lexical rank: the everyday
# personal sources and lookups a short prompt most often needs.
ASK_CORE_STUBS = frozenset({
    "get_upcoming", "summarize_emails", "view_emails", "summarize_messages",
    "view_messages", "search_notes", "search_reminders", "add_reminder",
    "web_search", "get_weather", "recall", "remember", "daily_brief",
    "collect_task_evidence", "lookup_contact", "set_timer", "system_status",
    "wisp_capabilities", "draft_email", "draft_message", "calculate", "world_time",
    "get_my_day_plan",
    # Toolbench 2026-10-31: the immediate sends, forget, notes and free time were
    # missing, so "tell him yes", "forget that I..." and "email dad" reached only
    # draft_* or an invented memory.forget.
    "send_email", "send_message", "schedule_send", "forget", "create_note", "find_free_time",
    "wisp_status",
})

DAY_PLAN_TOOL = "get_my_day_plan"
_WHEN = (r"(?:today|tonight|tomorrow|(?:this|next)\s+(?:week(?:end)?|month)|the\s+week(?:end)?"
         r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day)\b")
# "and saturday?", "what about friday", "I mean this week", and corrections:
# "actually saturday", "no, friday", "i meant tomorrow", "no wait, saturday"
_DAY_FOLLOWUP = re.compile(r"(?:(?:no|nah|sorry|oops)[\s,]+)?(?:wait[\s,]+)?"
                           r"(?:(?:and|what\s+about|how\s+about|i\s+mean[t]?|same\s+for|actually)[\s,]+)?"
                           r"(?:for\s+|on\s+)?" + _WHEN + r"\s*[?.!]?$", re.I)
_DAY_PLAN = re.compile(
    r"\bwhat\s+(?:should|do|must|can)\s+i\s+(?:do|focus on|work on|tackle|get done|prioriti[sz]e)\b"
    r"|\bwhat(?:['’]?s| is)\s+on\s+my\s+(?:plate|list|agenda|to-?do)"
    r"|\bwhat\s+do\s+i\s+(?:need|have)\s+to\s+(?:do|get done)\b"
    r"|\bplan\s+(?:out\s+)?my\s+day\b|\bwhat\s+am\s+i\s+(?:forgetting|missing)\b"
    r"|\banything\s+i\s+should\s+(?:know|do|handle)\b|\bwhat(?:['’]?s| does| is)\s+my\s+day\b"
    r"|\bmy\s+(?:to-?do|todo)s?(?:\s+list)?\b|\bwhat(?:['’]?s| is)\s+next\s+for\s+me\b"
    r"|\bhow\s+(?:does|is)\s+my\s+day\b|\bwhere\s+should\s+i\s+start\b"
    # Time-scoped "what's going on" questions: the Brief holds what mail,
    # texts and notes add to the calendar (2026-11-02: "what is my plan for
    # saturday" missed a family lunch from a text). "what's up" alone is a
    # greeting, so it counts only with a time.
    r"|\bwhat(?:['’]?s| is| are)\s+coming\s+up\b|\banything\s+(?:coming\s+up|due|planned)\b"
    r"|\banything\s+i\s+(?:need|have)\s+to\s+(?:do|get done|handle)\b"
    r"|\bwhat\s+do\s+i\s+have\s+(?:due|going on|coming up|planned|on|for|this|next|today|tomorrow|tonight)\b"
    r"|\bmy\s+plans?\s+(?:for|on|this|next|today|tomorrow|tonight)\b"
    r"|\bwhat(?:['’]?s| is)\s+(?:up|on|going\s+on|happening|planned)\s+(?:for\s+|on\s+)?" + _WHEN,
    re.I)


def day_plan_question(prompt: str, previous: str = "") -> bool:
    """"What should I do today?", "what's on friday?" and their paraphrases:
    one get_my_day_plan read answers it better than four separate source
    reads. A bare follow-up ("and saturday?") counts when the previous user
    prompt was one."""
    if _DAY_PLAN.search(normalize_day_words(prompt)):
        return True
    return bool(previous and _DAY_FOLLOWUP.match(normalize_day_words(prompt).strip())
                and _DAY_PLAN.search(normalize_day_words(previous)))


# Misspellings of "tomorrow" seen in real prompts ("what is up for tommorow"
# ranked get_upcoming out of the Ask head start: the lexical ranker never
# matched the day word, so the calendar read was offered by name only).
_TOMORROW_TYPO = re.compile(
    r"\b(?:tommorow|tommorrow|tomorow|tommrow|tomorrrow|tomoorow|tmrw|tmrow|tmr|2morrow|2moro)\b", re.I)


def normalize_day_words(prompt: str) -> str:
    """The prompt with common misspellings of "tomorrow" spelled out, for
    lexical matching only (the user's text is never rewritten)."""
    return _TOMORROW_TYPO.sub("tomorrow", prompt or "")


ASK_DAY_PLAN_HINT = (
    "DAY PLAN: call get_my_day_plan once for the day or range asked about (day='tomorrow', "
    "a weekday like 'saturday', a date, or 'this_week') and answer from it, including the "
    "items it found in email and texts; for a follow-up like 'and friday?' call it again "
    "for that day; do not read the calendar, mail, messages and "
    "notes one by one unless it says the plan is unavailable. predict_my_day is "
    "learned habits only, never the plan. Answer as a short prioritized list: one "
    "lead line, then 3-6 bullets — time-bound items first (with times), then "
    "overdue items, then people waiting on a reply, then suggestions and tasks, "
    "unless the user asked for another length or format. "
    "No digests, no data-freshness caveats, no source labels."
)

# A reply that only promises to look ("I'm checking your schedule now. Give me
# just a moment!") with no tool run is not an answer: the turn ends there and
# the user never gets the result (2026-10-29 dev7 export).
_STALL = re.compile(
    r"^\s*(?:(?:sure|okay|ok|of course|absolutely|got it)[.,!]?\s*)?"
    r"(?:i(?:'|’)m|i am|i(?:'|’)ll|i will|let me)\s+(?:now\s+)?(?:go(?:ing)? (?:to )?)?"
    r"(?:check|look|pull|fetch|grab|search|review)\w*\b[^\n]{0,160}$",
    re.I)
# "Hang on to the bananas! Overripe ones are perfect for banana bread: mash
# three, ..." opens with a stall phrase but is a real answer. A filler phrase
# is a stall only when the whole reply is a line or two, and "hang on to",
# "stand by your decision", "one moment in history" are not fillers at all.
_STALL_FILLER = re.compile(
    r"\b(?:give me (?:just )?(?:a|one) (?:moment|sec(?:ond)?|minute)|(?:one|just a) moment(?!\s+(?:in|of|when|that)\b)|"
    r"hang (?:on(?!\s+to\b)|tight)|bear with me|stand by(?=\s*(?:[.!,;]|$)))", re.I)
_STALL_FILLER_MAX = 160
_STALL_ACK = re.compile(r"^\s*(?:sure|okay|ok|of course|absolutely|got it|alright|yes|yep|on it|"
                        r"(?:checking|looking|searching)(?: (?:now|on that|into it|that))?)\s*[.,!]*\s*$", re.I)


# Gemma's tool-call markup written as plain text: what it does on a turn that
# offers no tools (a knowledge question) or with a name it made up
# ("public_web:web_search", "device.system_status"). 13 of the first 252 answers
# in the 2026-10-29 Ask run were nothing but this markup.
_TEXT_TOOL_CALL = re.compile(r"<\|tool_call>.*?(?:<tool_call\|>|$)", re.S)


def text_tool_call(text: str) -> bool:
    """The reply holds tool-call markup instead of (or inside) an answer."""
    return "<|tool_call>" in (text or "")


def strip_text_tool_calls(text: str) -> str:
    """The reply without any tool-call markup (and its stray quote tokens)."""
    return _TEXT_TOOL_CALL.sub("", text or "").replace('<|"|>', '"').strip()


def stall_reply(text: str) -> bool:
    """A short "I'm checking now / one moment" promise instead of an answer."""
    text = (text or "").strip()
    if not text or len(text) > 300:
        return False
    if _STALL.search(text):
        return True
    if len(text) > _STALL_FILLER_MAX:
        return False
    # A line or two that are each only a filler or an acknowledgement ("Sure!\nOne
    # moment.") is a stall; a line with anything else in it is an answer.
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and any(_STALL_FILLER.search(line) for line in lines) and all(
        _STALL_FILLER.search(line) or _STALL_ACK.match(line) or _STALL.search(line) for line in lines)



# The Ask LoRA r3 reward's stall phrases (training/ask-lora-r3/grpo/reward.py
# `_STALL`, act_dont_promise), plus "I am …" and curly apostrophes. Unanchored,
# unlike _STALL above: the toolbench misses were multi-sentence ("It looks like
# you're curious about RAM! I'll look up the best details on that for you right
# now.", "I'm looking up the schedule for you now. … I'll check the public web").
_PROMISE = re.compile(
    r"\b(let me (?:look|check|find|pull|see|search|get|grab|take a look|dig)|"
    r"i(?:'| wi)ll (?:look|check|find|pull|search|get|grab|take a look|dig|see)|"
    r"i(?:'m| am) (?:looking|checking|pulling|searching|grabbing)|one moment|give me a (?:moment|sec(?:ond)?)|hang on|"
    r"looking (?:that|it|this) up)\b", re.I)


def promise_only_reply(text: str) -> bool:
    """A reply that promises to look something up instead of answering. Never a
    question back to the user ("Which city? Once I have it, I'll check.") and
    never a long answer that only mentions a follow-up."""
    text = (text or "").strip().replace("\u2019", "'")
    return (bool(text) and len(text) <= 400 and "?" not in text
            and bool(_PROMISE.search(text)))

# Appended to a model-led turn's runtime prompt (the light-read style hint the
# legacy router sets is never set on a model-led turn).
ASK_STYLE_HINT = (
    "ANSWER STYLE for this reply:\n"
    # Defaults only: "one sentence", "no lists" or "just the time" from the
    # user, in this message or earlier in the chat, wins over every line below.
    "- If the user said how long or in what format to answer, in this message "
    "or earlier in this chat, do exactly that; it overrides this list.\n"
    "- Lead with the answer itself. Do not restate or rephrase the question.\n"
    "- Warm, natural and concise: at most 4 sentences unless the user asks for "
    "more detail or a list.\n"
    "- Use a short bullet list only when there are several items (events, "
    "emails, steps); otherwise plain sentences.\n"
    "- Never paste raw tool output, JSON, IDs or field labels; say it in your "
    "own words, grounded only in what the tools returned.\n"
    "- If a tool rejected an argument, fix the call yourself and try again; do "
    "not ask the user to rephrase.\n"
    "- Match the time range the user asked about. If a tool returned a wider range "
    "(a week when they asked about today or tomorrow), mention only the asked day, "
    "and never describe a multi-day count as \"today\".\n"
    "- If no tool you have can know the answer (a personal fact nothing records), "
    "say so in one short sentence; never estimate or make up a number.\n"
    "- When a contact lookup answers \"who is X?\", say \"X is a contact: <their "
    "number or email>\" — a phone number is what a contact has, never what they are."
)

# Appended for a capability question ("Can you send texts?"): the corpus and
# sweep had the model deny real capabilities or ask "which email?".
ASK_CAPABILITY_HINT = (
    "This is a question about Wisp itself. Never name internal tools to the user. "
    "In short: you can read and summarize the user's email, texts, calendar, reminders "
    "and notes; draft and send emails and texts (every send is shown to the user to "
    "confirm first); add reminders and calendar events; search the web, weather and "
    "stocks; and control Mac settings and apps. To open Wisp from any app press "
    "⌥Space; to ask out loud press ⌥⇧Space or click the mic in Ask (Wisp has its "
    "own voice input; never send the user to Siri).\n"
    "Connecting Google (Gmail and Google Calendar), when asked: answer with ALL of "
    "these, in 3 short sentences: (1) open the notch, then Quick Settings (the gear) > "
    "Google > Connect Google, and sign in in the browser; (2) Wisp reads Gmail and "
    "Google Calendar read-only and builds your to-dos in about a minute; (3) "
    "\"Disconnect & erase\" there removes the account and everything Wisp derived from "
    "it. Never say it cannot be done."
)

# Wisp's own reports, in the order a self-question should reach for them: each
# is deterministic text written from live state (service/wisp_about.py,
# misc_t1.wisp_status, wisp_mcp, wisp_skills), so the answer cannot drift.
WISP_SELF_TOOLS = (
    ("wisp_capabilities", "what you can do (no topic; pass the question as `query` for a "
     "specific 'can you ...'), topic='identity' who you are, 'privacy' whether data stays on "
     "this Mac, 'howto' how to use Wisp, voice, Google, settings"),
    ("wisp_status", "topic='models' which model is answering and whether a cloud model is on, "
     "'sync' when mail/messages/notes last synced, 'accounts' linked accounts, 'whats_new'"),
    ("wisp_mcp", "connected MCP servers and whether they work"),
    ("wisp_skills", "installed skills"),
)
_WISP_SELF = re.compile(
    r"\b(?:who|what)\s+(?:exactly\s+)?are\s+(?:you|u)\b|\bwhat\s+is\s+wisp\b|\babout\s+yourself\b"
    r"|\b(?:what|which)\s+(?:ai\s+|llm\s+|language\s+)?models?\s+(?:are\s+(?:you|u)|is\s+(?:wisp|answering|running|this))\b|\bare\s+you\s+(?:using\s+)?(?:the\s+)?cloud\b"
    r"|\b(?:running|run)\s+(?:locally|on\s+(?:my|this)\s+mac)\b|\b(?:is|are)\s+my\s+(?:data|info\w*)\s+"
    r"(?:private|safe|secure|local)\b|\bprivacy\b|\bon-device\b|\bmcp\b|\bskills?\s+do\s+i\s+have\b"
    r"|\b(?:last|latest)\s+(?:sync|synced)\b|\bwhen\s+did\s+(?:my\s+)?\w+\s+(?:last\s+)?sync\b"
    r"|\bhow\s+far\s+back\s+(?:can|do|does|will)\s+(?:you|u|wisp)\b|\bconnect\s+(?:my\s+)?(?:gmail|google)\b"
    r"|\btalk\s+to\s+(?:you|u|wisp)\b|\bhow\s+do\s+i\s+use\s+(?:you|u|wisp|this)\b"
    r"|\bwhat(?:'s|\s+is)\s+new\s+in\s+wisp\b|\b(?:about|use|using)\s+wisp\b|\bwisp(?:'s)?\s+settings\b"
    r"|\bwhat\s+(?:else\s+)?(?:can|could)\s+(?:you|u)\s+do\b|\bwhat\s+(?:tools|capabilities)\b", re.I)
_MEMORY_SELF = re.compile(r"\b(?:know|remember|learned)\s+about\s+me\b|\bwhat\s+do\s+(?:you|u)\s+know\b", re.I)


def wisp_self_question(prompt: str) -> bool:
    """A question about Wisp itself (what it can do, who/what it is, its model,
    privacy, sync, MCP, skills, how to use it), not about the user's data or a
    memory question ("what do you know about me" is recall/describe_me)."""
    text = prompt or ""
    if _MEMORY_SELF.search(text):
        return False
    return capability_question(text) or bool(_WISP_SELF.search(text))


def wisp_self_hint() -> str:
    """The capability sheet for a question about Wisp itself, built per turn:
    the live identity line from wisp_about (local vs cloud read from settings),
    Wisp's own report tools as registered and usable now, and the how-to facts
    (ASK_CAPABILITY_HINT). tests/test_wisp_self_reference.py checks each fact
    against the registry and wisp_about, so the sheet cannot drift."""
    lines = ["ABOUT WISP (this question is about you, Wisp; the facts below come from this build):"]
    try:
        from service import wisp_about
        lines.append("- " + wisp_about.identity().split("\n\n", 1)[0].replace("**", ""))
    except Exception:  # noqa: BLE001 - say less rather than guess
        pass
    try:
        from service.tools.registry import routable_tool_names
        usable = routable_tool_names()
    except Exception:  # noqa: BLE001
        usable = set()
    reports = [f"{name}: {what}" for name, what in WISP_SELF_TOOLS if name in usable]
    if reports:
        lines.append("- Answer from Wisp's own report, not from memory: call " + "; ".join(reports)
                     + ". Its text is written for the user; pass it on in your words.")
    lines.append("- Never call yourself GPT, Claude, Siri or another assistant, and never "
                 "invent a setting, menu or shortcut.")
    return "\n".join(lines) + "\n" + ASK_CAPABILITY_HINT


ASK_KNOWLEDGE_HINT = (
    "GENERAL QUESTION: no personal data is involved, so answer from your own "
    "knowledge, directly and briefly. No tool is needed and none is available. "
    "Start with the answer itself (begin with Yes or No only for a yes/no "
    "question). Never mention internal tool names or Wisp features, and never say "
    "where the answer comes from or mention these instructions. For a Mac how-to "
    "give the real keyboard shortcuts and menus, listing the main options."
)

# Tools that cannot ground a personal fact: a calculation or Mac status read
# after a forced re-prompt is not evidence of "my backpack weighs 1.02 kg".
UNGROUNDING_CATEGORIES = frozenset({"compute", "system_read", "wisp_admin"})
ASK_UNKNOWN_MESSAGE = (
    "I don't have that. Nothing I can check (your calendar, email, messages, "
    "reminders, notes or what you've told me) records it."
)

_DEGRADED_WEB = re.compile(
    r"General-web results were unavailable|\(no public web results|couldn't reach|could not reach|"
    r"timed out|search (?:is|was) unavailable|no results found", re.I)


def web_degradation_hint(tool: str, result: str) -> str:
    """A model-facing hedge when a web lookup came back degraded, or ""."""
    if tool not in {"web_search", "web_fetch", "wikipedia_summary"}:
        return ""
    if not _DEGRADED_WEB.search(str(result or "")[:600]):
        return ""
    return ("\n(Search coverage was degraded: these results do not verify current facts. "
            "Say the search could not confirm it, hedge anything you state, and do not add "
            "specifics such as scores, dates or prices that are not in the results above.)")


# Replying is a destination and draws on the message replied to, in any form:
# "draft a message responding to trishe" read nothing and drafted nothing
# (2026-11-02 r3). The verbs need only "to"; the nouns ("a response to", "an
# answer to") need a person or a message after it, so "the public response to
# the policy" stays plain writing.
_REPLY_TO = (r"(?:\b(?:repl(?:y|ies|ied|ying)|respon(?:d|ds|ded|ding))\s+to\b"
             r"|\b(?:response|answer|reply)\s+to\s+(?:[\w.-]+['’]s\b|(?:my|his|her|their|the|this|that|"
             r"your)\s+(?:e-?mail|message|text|dm|thread|note|invite|post)\b|(?:my\s+)?(?:mom|mum|dad|"
             r"mother|father|sister|brother|boss|manager|professor|prof|teacher|landlord|roommate|"
             r"friend|wife|husband|partner|girlfriend|boyfriend)\b|(?-i:[A-Z][\w.'-]*)\b|(?:him|her|them)\b)"
             r"|\banswer(?:ing)?\s+(?:him|her|them)\b)")
_COMPOSE = re.compile(r"^\s*(?:please\s+)?(?:can you\s+|could you\s+)?(?:write|compose|draft|come up with)\b", re.I)
# A destination is somewhere the text goes: a named recipient ("an email to
# Priya"), a send/reply verb, or a place it is saved. The noun alone ("a
# birthday message for my sister") is just the kind of text to write.
_DESTINATION = re.compile(
    r"\b(?i:e-?mail|text|message|imessage|dm|note|letter|card)\s+(?i:to|for)\s+[A-Z][\w.'-]*"
    # A channel noun addressed TO someone is sent, whatever the case: "an email
    # to mom", "a message to the landlord" (toolbench 2026-10-31: these banned
    # draft_email). A "note to my professor" is still only text to write.
    r"|(?i:\b(?:e-?mail|text|message|imessage|dm)\s+to\s+(?:my\s+|the\s+|our\s+)?[a-z][\w.'-]*)"
    r"|(?i:\b(?:send|sent|reply|respond|forward|post\s+(?:it|this)|share)\b)"
    r"|(?i:" + _REPLY_TO + r")"
    r"|(?i:\b(?:in|to|into)\s+(?:my\s+)?(?:notes?|notes app|a file|calendar|reminders?)\b)"
    r"|(?i:\b(?:save|file|document|doc|calendar|reminder|clipboard)\b)"
    r"|\b(?i:text|e-?mail|message)\s+[A-Z][\w.'-]*")


def compose_only(prompt: str) -> bool:
    """A writing request with no destination ("Write a short thank-you note to
    my professor"): the answer IS the text, so no write/draft/send tool runs."""
    text = prompt or ""
    return bool(_COMPOSE.match(text)) and not _DESTINATION.search(text)


ASK_COMPOSE_HINT = (
    "COMPOSE: the answer IS the requested text, and writing it is always within what "
    "you can do. Write it now, ready to copy: no preamble, no lookups, no clarifying "
    "question, no refusal. For a personal detail the request does not give (a name, "
    "a date, a course, a company), use a short placeholder such as [Name] or "
    "[Company] instead of searching or asking."
)

# A writing request that draws on the user's stored content keeps its reads:
# "summary of my notes", "reply to Ed's email", "based on my calendar".
_STORED_CONTENT = re.compile(
    r"\b(?:my|our)\s+(?:upcoming\s+|next\s+)?(?:notes?|e-?mails?|inbox|calendar|schedule|agenda|"
    r"messages?|texts?|chats?|reminders?|to-?dos?|docs?|documents?|files?|folder|drafts?|"
    r"meetings?|events?|plans?|conversations?|browsing|history)\b"
    r"|\b(?:based on|using|from|according to|summari[sz]e|summary of|recap of)\b"
    r"|" + _REPLY_TO +
    r"|\b[\w.-]+['’]s\s+(?:e-?mail|message|text|note|doc|document|post|invite)\b"
    r"|\bthe\s+(?:doc|document|file|attachment|e-?mail|thread|message|text|note|"
    r"meeting|call|invite|spreadsheet|pdf|deck)\b"
    r"|\b(?:today|tomorrow|yesterday|this week|last week)['’]?s?\s+(?:meeting|call|event|"
    r"class|appointment)\b"
    r"|\battached\b|\bwe (?:discussed|talked)\b",
    re.I)
_WRITING = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?(?:help me\s+)?"
    r"(?:write|compose|draft|come up with|pen)\b", re.I)


def reply_reads(prompt: str) -> list[str]:
    """The conversation a reply draft answers is read first: "draft a message
    responding to trishe" needs her thread before draft_message. An email
    reply reads mail, a text or message reply reads Messages, otherwise both."""
    text = prompt or ""
    if not (_WRITING.match(text) or _COMPOSE.match(text)) or not re.search(_REPLY_TO, text, re.I):
        return []
    if re.search(r"\be-?mails?\b", text, re.I):
        return ["view_emails"]
    if re.search(r"\b(?:text|message|imessage|dm|chat|group)s?\b", text, re.I):
        return ["view_messages"]
    return ["view_messages", "view_emails"]


def pure_writing(prompt: str) -> bool:
    """"Write a short thank-you note to my professor", "Draft a LinkedIn post
    about my new job": the request supplies everything, so no read tool helps;
    a placeholder covers any personal detail. False when the text must draw on
    the user's stored content (their notes, an email, the calendar)."""
    text = prompt or ""
    return bool(_WRITING.match(text)) and not _STORED_CONTENT.search(text)


# Reads withheld from a pure writing request. Contact lookups stay available so
# "Draft an email to Priya" can still address the draft.
WRITING_FORBIDDEN_READ_CATEGORIES = frozenset({
    "assistant_read", "email_read", "email_triage", "notes_read", "browser_history_read",
    "fs_read", "web_read", "system_read",
})
WRITING_ALLOWED_READS = frozenset({"lookup_contact", "list_contacts"})
WRITING_FORBIDDEN_READS = frozenset({"view_messages", "summarize_messages"})


COMPOSE_ONLY_FORBIDDEN_CATEGORIES = frozenset({
    "notes_write", "fs_write", "email_draft", "messages_draft", "email_send",
    "messages_send", "scheduled_send", "calendar_write", "assistant_write",
})


def ask_forbidden_tools(prompt: str, registry: dict) -> frozenset[str]:
    """Tools a model-only Ask turn may not offer for this request: every read
    (except contact lookups) for a pure writing request, and every write, draft
    and send for a writing request with no destination."""
    forbidden: set[str] = set()
    if pure_writing(prompt):
        forbidden |= {n for n, t in registry.items() if n not in WRITING_ALLOWED_READS
                      and (t.category in WRITING_FORBIDDEN_READ_CATEGORIES
                           or n in WRITING_FORBIDDEN_READS)}
    if compose_only(prompt):
        forbidden |= {n for n, t in registry.items() if t.category in COMPOSE_ONLY_FORBIDDEN_CATEGORIES}
    return frozenset(forbidden)


# Read tools whose result is the user's own current data.
PERSONAL_SOURCE_CATEGORIES = frozenset({
    "assistant_read", "email_read", "messages_read", "notes_read", "browser_history_read",
})
PERSONAL_SOURCE_TOOLS = frozenset({
    "recall", "search_memory", "search_conversations", "read_knowledge", "describe_me",
})


def guard_rejection(exc: BaseException) -> bool:
    """A local-engine refusal of the prompt's SIZE: oMLX answers its prefill
    memory guard and context overflows with HTTP 400. The streamed body is not
    readable here, so any 400 from the managed engine counts."""
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    try:
        return exc.response.status_code == 400
    except Exception:  # noqa: BLE001
        return False


# --- text clean-up -----------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_think(text: str) -> str:
    """Remove think markup from answer text: closed blocks, a stray closing
    tag (keep what follows), and an unclosed opening block (drop it)."""
    text = _THINK_BLOCK.sub("", text or "")
    if re.search(r"</think>", text, re.I):
        text = re.split(r"</think>", text, flags=re.I)[-1]
    if re.search(r"<think>", text, re.I):
        text = re.split(r"<think>", text, flags=re.I)[0]
    return text.strip()


_PLANNING = re.compile(
    r"^\s*(?:the user\b|user (?:wants|asks|is)|we (?:need|should|can|have)|i (?:need|should|will|'ll|must|have to|can|"
    r"think|want|am going)|i'll\b|let me\b|let's\b|so,?\s|now,?\s|first,?\s|next,?\s|then,?\s|okay|ok\b|hmm|wait\b|"
    r"actually\b|maybe\b|but\b|looking at\b|based on (?:the )?(?:tool|function)|need to\b|step \d|to answer\b|"
    r"the (?:tool|function|result|results|request|question)\b|this (?:is|means|requires)\b|they (?:want|asked|are)\b)",
    re.I)
_TOOLISH = re.compile(r"\b[a-z]+_[a-z_]+\b|\btool\b|\bfunction call\b|\bschema\b", re.I)


def salvage_answer(reasoning: str, *, limit: int = 1500) -> str:
    """The final-answer portion of a reasoning trace, or "".

    Used only when a step returned no content and no tool call. Walks the
    trace's paragraphs from the end and keeps the trailing ones that read as an
    answer to the user; the first planning paragraph ("The user wants...",
    "I should call...") stops it, so the monologue itself is never returned."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", reasoning or "") if p.strip()]
    kept: list[str] = []
    for paragraph in reversed(paragraphs):
        if _PLANNING.match(paragraph) or _TOOLISH.search(paragraph):
            break
        kept.insert(0, paragraph)
    answer = "\n\n".join(kept).strip()
    if len(answer) < 2 or len(answer) > limit:
        return ""
    return answer


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def dedupe_reasoning(steps: list[str], answer: str) -> list[str]:
    """Drop the final answer from the reasoning shown beside it.

    A step whose reasoning contains the whole answer (40+ chars) has that copy
    removed; a step left with nothing of substance is dropped. Shorter answers
    are left alone: a coincidental phrase is not a duplicate."""
    target = _norm(answer)
    if len(target) < 40:
        return list(steps)
    out: list[str] = []
    for step in steps:
        if target in _norm(step):
            flat = re.sub(r"\s+", " ", step)
            index = flat.lower().find(target)
            remainder = (flat[:index] + flat[index + len(target):]).strip() if index >= 0 else ""
            if len(remainder) >= 20:
                out.append(remainder)
            continue
        out.append(step)
    return out


# Data-freshness and coverage boilerplate the read tools write for the MODEL.
# It never belongs in an answer the user reads.
_CAVEAT = re.compile(
    r"local cache|cache is stale|history cache|freshness|newer (?:messages|emails) may be missing|"
    r"has not completed|refresh(?:ed|ing)?\b.*(?:mail|messages|notes|inbox)|unverified|not verified|"
    r"\bcoverage\b|scan hit its|messages? cap|untrusted|publisher metadata|try again in a moment|"
    r"wisp is still syncing|is NOT all of it|were cut to fit", re.I)


def _plain_line(line: str) -> str:
    """One line with markdown emphasis/headers removed; "" for a bare header."""
    raw = line.strip()
    if re.fullmatch(r"#{1,6}\s.*|\*\*[^*]{1,60}\*\*:?|__[^_]{1,60}__:?", raw):
        return ""
    text = re.sub(r"\*\*|__|`", "", raw)
    text = re.sub(r"^#{1,6}\s*", "", text).strip()
    if re.fullmatch(r"[^:]{1,40}:", text):
        return ""
    return text


def compact_results(results: list[tuple[str, str]], labels: dict[str, str], *,
                    lines_per_source: int = 6, line_chars: int = 160,
                    total_chars: int = 1200) -> str:
    """A short, readable fallback over tool results: the first lines of each
    usable source under a friendly label, never a full dump. Error notices,
    policy blocks and timeouts are skipped. "" when nothing usable remains."""
    blocks: list[str] = []
    seen: set[str] = set()
    for name, result in results:
        text = str(result or "").strip()
        if (name in seen or not text or text.startswith(("(", "BLOCKED", "The user denied"))
                or "timed out" in text[:200].lower()):
            continue
        seen.add(name)
        lines = [_plain_line(ln) for ln in text.splitlines()]
        lines = [ln for ln in lines if ln and not _CAVEAT.search(ln)
                 and not re.match(r"(?:Account(?:-ID)?|Result coverage|Message-ID|To|Coverage|URL):", ln)]
        if not lines:
            continue
        kept = [ln if len(ln) <= line_chars else ln[:line_chars - 1].rstrip() + "…"
                for ln in lines[:lines_per_source]]
        body = "\n".join("- " + re.sub(r"^[-•*]\s*", "", ln) for ln in kept)
        if len(lines) > len(kept):
            body += f"\n- …and {len(lines) - len(kept)} more"
        label = labels.get(name) or name.replace("_", " ").capitalize()
        blocks.append(f"{label}:\n{body}")
    if not blocks:
        return ""
    out = "Here's what I found:\n\n" + "\n\n".join(blocks)
    if len(out) > total_chars:
        out = out[:total_chars].rsplit("\n", 1)[0] + "\n- …"
    return out


# --- request shape -----------------------------------------------------------

_TIME_WORDS = re.compile(
    r"\b(?:today|tonight|tomorrow|tmrw|tmr|yesterday|this|next|last|week|weekend|month|morning|"
    r"afternoon|evening|now|recent|lately|from|about|with)\b", re.I)


_ABOUT_WISP = re.compile(
    r"\b(?:you|your|yourself|wisp|connect|link|sign\s+in|log\s+in|account|google|gmail|"
    r"permissions?|grant|integrat\w*|set\s+(?:you|wisp)\s+up)\b", re.I)
_KNOWLEDGE = re.compile(
    r"^(?:so\s+)?(?:how\s+(?:do|can|should|would)\s+(?:i|you|one|we)|how\s+to|how\s+(?:many|much|long|far|big|old|does|do|is|are)"
    r"|why\b|what(?:'s|\s+is|\s+are|\s+was|\s+were)\s+(?:a|an|the)\b|what(?:'s|\s+is)\s+the\s+difference"
    r"|who\s+(?:wrote|invented|discovered|painted|founded|was)"
    r"|explain|define|describe|give\s+me\s+(?:some\s+)?(?:tips|advice|ideas)|tips\s+for|tell\s+me\s+(?:a\s+joke|about\s+(?:the|a|an)\b))",
    re.I)
_LIVE = re.compile(
    r"\b(?:latest|recent|recently|current|currently|news|today|tonight|tomorrow|tmrw|yesterday|"
    r"this\s+(?:week|weekend|month|year)|right\s+now|now|weather|forecast|temperature|rain|"
    r"stocks?|price|prices|cost\s+now|score|won|winning|playing|happening|open\s+now|near\s+me|"
    r"traffic|time\s+is\s+it|time\s+in|exchange\s+rate|next|upcoming|coming\s+up|"
    r"schedule|calendar|inbox|here|nearby|nearest|directions|route|get\s+to|drive\s+to|commute|"
    r"my|mine|i|i'm|i've|we|our|us)\b"
    r"|\b(?:e-?mail|text|messag|call|tell|told|sen[dt]|wr[io]te|ping|invit)\w*\s+me\b"
    r"|\d[\d,.]*\s*(?:%|percent|[x*/+^-])|[%*/+^=]\s*\d", re.I)


_MAILISH = re.compile(
    r"\b(?:due|deadline|submit|form|w-?\d|1099|bill|invoice|statement|receipt|order|package|"
    r"shipp?(?:ed|ing)|tracking|delivery|confirmation|refund|renewal|subscription)\b", re.I)


def mail_first(prompt: str) -> bool:
    """A personal question whose answer usually sits in the user's email
    ("When is the W-9 due?", "Did my order ship?"): view_emails leads the
    head start, so the web is not the first place looked."""
    return bool(_MAILISH.search(prompt or "")) and asks_personal_data(prompt)


# "When is my midterm?" was offered no calendar tool and ended in "I don't
# have that"; "what are my school tasks" lost Reminders to Google Tasks and
# asked which list (2026-10-29 Ask run). Each leads with its usual source.
_CALENDARISH = re.compile(
    r"\b(?:when(?:'s|\s+is|\s+are)\s+my|midterms?|finals?|exams?|quiz(?:zes)?|class(?:es)?|"
    r"lectures?|appointments?|interviews?|meetings?|dentist|doctor)\b", re.I)
_TASKISH = re.compile(r"\b(?:tasks?|to-?dos?|todo\s+list|pending|homework|assignments?)\b", re.I)


def calendar_first(prompt: str) -> bool:
    """A personal question about a dated event ("When is my midterm?"):
    get_upcoming leads the head start."""
    return bool(_CALENDARISH.search(prompt or "")) and asks_personal_data(prompt)


def reminders_first(prompt: str) -> bool:
    """A personal question about to-dos ("what are my open tasks"):
    search_reminders leads the head start."""
    return bool(_TASKISH.search(prompt or "")) and asks_personal_data(prompt)


# The Mac's own state is read with a tool, never answered from knowledge:
# "how much free disk space?" was offered no tools (2026-10-29 Ask run).
# A device word counts only with state phrasing ("how much RAM is free", "my
# disk space"): "What is RAM?" and "How do I screenshot on macOS?" are
# knowledge (0.5.0 dev8 audit).
_DEVICE_WORD = re.compile(
    r"\b(?:disk|storage|space|battery|charg\w*|ram|memory|cpu|wi-?fi|bluetooth|"
    r"ip\s+address|uptime|volume|brightness|clipboard|macos|os\s+version|downloads?|desktop)\b", re.I)
_DEVICE_STATE_PHRASE = re.compile(
    r"\b(?:my|this\s+mac|the\s+mac|my\s+mac|free|left|used|using|available|remaining|full|"
    r"level|percent|connected|currently|right\s+now|do\s+i\s+have|have\s+i\s+got|am\s+i\s+on|"
    r"what\s+version|which\s+version|is\s+(?:it|the\s+\w+)\s+(?:on|off))\b", re.I)


class _DeviceState:
    """search() is truthy for a device word with state phrasing."""
    @staticmethod
    def search(text: str):
        return bool(_DEVICE_WORD.search(text or "") and _DEVICE_STATE_PHRASE.search(text or ""))


_DEVICE_STATE = _DeviceState()
# General advice about oneself, with nothing of the user's to read: "how many
# hours of sleep should I get?" was re-prompted for a tool and ended in "I
# don't have that".
_ADVICE = re.compile(r"\b(?:should\s+(?:i|we)\s+(?:get|drink|eat|sleep|do|take|use|study|exercise|"
                     r"run|walk|save|spend|wear|bring|pack|tip)|do\s+(?:i|we)\s+need)\b", re.I)
_PRONOUNS = re.compile(r"\b(?:i|i'm|i've|we|us|our)\b", re.I)


def advice_question(prompt: str) -> bool:
    """General advice ("how much water should I drink?"), not the user's data."""
    text = (prompt or "").strip()
    return bool(_KNOWLEDGE.match(text) and _ADVICE.search(text)
                and not re.search(r"\b(?:my|mine|me)\b", text, re.I)
                and not _LIVE.search(_PRONOUNS.sub("", text))
                and not _SOURCE_RECORD.search(text) and not _DEVICE_STATE.search(text))


def knowledge_question(prompt: str) -> bool:
    """General knowledge or a general how-to ("How many ounces are in a cup?",
    "How do I take a screenshot on a Mac?", "Give me tips for studying"): no
    personal data, nothing live, nothing about Wisp. Answered from knowledge in
    one tool-free step, since a tool path only added failures and refusals."""
    text = (prompt or "").strip()
    head = _KNOWLEDGE.match(text)
    if not head or _DEVICE_STATE.search(text):
        return False
    if advice_question(text):
        return not _ABOUT_WISP.search(text) and not _SECOND_CLAUSE.search(text)
    rest = text[head.end():]  # "How do I" itself is not first-person data
    return (not _LIVE.search(rest) and not _ABOUT_WISP.search(text)
            and not _SOURCE_RECORD.search(rest) and not _SECOND_CLAUSE.search(rest)
            and not asks_personal_data(text))


# A knowledge question that names one of the user's records ("Explain the
# email from Alex", "What is the reminder about rent about?") needs that
# record read, and one with a further clause ("Define X; delete the Laundry
# reminder") carries a request the tool-free step would drop. Both keep tools.
_SOURCE_RECORD = re.compile(
    r"\b(?:e-?mails?|mail|inbox|messages?|texts?|imessages?|reminders?|notes?|events?|"
    r"meetings?|appointments?|contacts?|files?|documents?|attachments?|threads?|chats?)\b", re.I)
_SECOND_CLAUSE = re.compile(r"[;]|\b(?:and|then|also)\s+(?:please\s+)?(?:delete|remove|clear|send|"
                            r"text|e-?mail|add|create|set|remind|cancel|move|mark|reply|forward|save|"
                            r"schedule|open|show|read|summari[sz]e|check)\b", re.I)


def capability_question(prompt: str) -> bool:
    """A question about what Wisp can do or how to do something, not a request
    for the user's data: "What can you do with my calendar?", "How do I
    connect Google?", "Can you read my email?"."""
    text = (prompt or "").strip().lower()
    # "do you know my birthday?" asks what Wisp remembers, not what it can do.
    if re.match(r"^(?:do|did)\s+you\s+(?:know|remember|recall)\s+(?:my|me|who|what|when|where)\b", text):
        return False
    if re.match(r"^(?:so\s+)?(?:what|which)\s+(?:\w+\s+){0,2}(?:can|could|do)\s+you\s+(?:do|help|handle|access|see|read|check)\b", text):
        return True
    if re.match(r"^(?:what|which)\s+(?:can|could)\s+you\b", text):
        return True
    if re.match(r"^(?:how\s+(?:do|can|should|would)\s+i|how\s+to)\b", text):
        # "How do I connect Google to you?" is about Wisp; "How do I take a
        # screenshot?" is a general how-to (knowledge_question).
        return bool(_ABOUT_WISP.search(text))
    if re.search(r"\bwhat\s+are\s+you\s+(?:able|good|capable)\b|\bwhat\s+do\s+you\s+(?:do|know how)\b"
                 r"|\b(?:do|does)\s+(?:you|wisp)\s+have\s+access\b|\bare\s+you\s+able\s+to\b", text):
        return True
    words = re.findall(r"[a-z']+", text)
    return bool(re.match(r"^(?:can|could|will|do|are)\s+you\b", text) and len(words) <= 6
                and not _TIME_WORDS.search(text))


# Nouns that are the user's stored data wherever they follow "my": "my calendar",
# "my tasks", "my midterm". Ambiguous nouns ("my phone keeps dying", "my class is
# so boring", "my day was rough", "my bag for a beach day", "my order at a
# restaurant") are chat unless asked in a lookup shape, below.
_PERSONAL_NOUNS = (
    r"calendar|schedule|agenda|events?|meetings?|appointments?|"
    r"emails?|e-mails?|inbox|mail|texts?|messages?|imessages?|reminders?|to-?dos?|tasks?|notes?|"
    r"contacts?|brief|deadlines?|homework|assignments?|exams?|midterms?|finals?|"
    r"flights?|birthday|password|wifi"
)
_AMBIGUOUS_NOUNS = (
    r"plans?|plate|day|week|weekend|backpack|bag|keys?|wallet|order|orders|package|packages|"
    r"class|classes|trip|name|address|phone|number"
)
_RECORD_NOUNS = _PERSONAL_NOUNS + r"|calls?|shifts?|interviews?|lectures?|quizzes|quiz|tests?"
_MY_WORD = r"my\s+(?:[\w'’-]+\s+)?"
# A lookup lead: "what's / when is / where are / how heavy is / how does ... my X".
_LOOKUP_LEAD = (r"(?:what(?:['’]s|\s+is|\s+are)|when(?:['’]s|\s+is|\s+are|\s+was|\s+do|\s+does|\s+will)|where(?:['’]s|\s+is|\s+are)|"
                r"what\s+time(?:['’]s|\s+is|\s+are)?|how\s+(?:heavy|long|big|much|many|far|late|early)\s+(?:is|are)|"
                r"how(?:['’]s|\s+is|\s+are|\s+does|\s+do))")
# Day anchors; "my coffee with Alex tomorrow" is the user's plan, "my bag for a beach day" is not.
_DATE_ANCHOR = (r"(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|weekend|week)|next\s+(?:week|month|\w+day)|"
                r"on\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
                r"(?:mon|tues|wednes|thurs|fri|satur|sun)day)")
_PERSONAL = re.compile(
    rf"\b(?:my|mine)\s+(?:[\w'’-]+\s+)?(?:{_PERSONAL_NOUNS})\b"
    # "when is my midterm", "where are my keys", "how heavy is my backpack",
    # "how does my day look", "what's my wifi password"; "my phone keeps dying" is not.
    rf"|\b{_LOOKUP_LEAD}\s+(?:all\s+)?{_MY_WORD}(?:{_AMBIGUOUS_NOUNS})\b"
    # "did my order ship", "has my package arrived", "is my flight on time"
    rf"|\b(?:did|has|have|is|are|was|were|will)\s+(?:all\s+)?{_MY_WORD}(?:orders?|packages?|parcels?|"
    r"keys|wallet|plans|trip|name|address)\b"
    # First person counts only in a record or lookup form: "do I have class
    # tomorrow", "did I pay the electric bill", "am I free at 3". Bare "do I" /
    # "should I" is advice ("what do I do with overripe bananas", "should I learn guitar").
    rf"|\bdo\s+i\s+have\s+(?:(?:anything|something|nothing|plans?|class|classes|work|school|practice)\b|"
    rf"(?:\w+\s+){{0,3}}?(?:{_RECORD_NOUNS})\b)"
    rf"|\bdo\s+i\s+(?:have|got)\s+(?:[\w'’-]+\s+){{0,3}}?{_DATE_ANCHOR}\b"  # "what do I have tomorrow"
    r"|\bdid\s+i\s+(?:already\s+)?(?:pay|reply|respond|send|book|rsvp|miss|sign|submit|schedule|renew|text|email|call)\b"
    r"|\bam\s+i\s+(?:free|busy|available|double[- ]?booked)\b"
    r"|\bhave\s+i\s+(?:got|paid|replied|responded|sent|booked|missed)\b"
    r"|\b(?:anyone|anybody|someone)\s+(?:text|email|message|call|write)\w*\s+me\b"
    r"|\b(?:any|new|unread)\s+(?:texts?|emails?|messages?|mail)\b"
    r"|\bwhat(?:['’]s| is| are)?\s+(?:on\s+)?my\b(?!\s+(?:best|thoughts?|options?|chances?|odds|purpose|meaning|problem)\b)"
    # "did mom text me?" — but not "my dad said cracking knuckles causes arthritis".
    r"|\b(?:mom|dad|mother|father)\s+(?:text|send|sent|email|call|messag)"
    r"|\b(?:reply|replied|respond|responded|text|texted|email|emailed|message|messaged|call|called|"
    r"write|wrote)\s+(?:back\s+)?(?:to\s+)?me\b"
    # Implicit personal deadlines, events and items: "When is the W-9 due?",
    # "what's on tomorrow", "is there a meeting today". Not "due to climate
    # change", and ship/deliver/arrive only for a parcel ("when was the first
    # iphone shipped", "when do swallows arrive in spring").
    r"|\b(?:due(?!\s+to\b)|deadline|expires?|expiring|overdue|rsvp)\b"
    r"|\b(?:package|parcel|order|delivery|shipment)s?\b[^?.!]{0,30}\b(?:ship(?:ped|s)?|arriv\w*|deliver(?:ed|s)?|here)\b"
    r"|\b(?:has|did|have)\s+(?:it|they)\s+(?:ship|shipped|arrived|been\s+delivered)\b"
    # "what's on tv tonight" is not the calendar.
    rf"|\bwhat(?:'s| is)\s+on\s+(?:today|tonight|tomorrow|this\b|next\b|for\b|the\s+(?:calendar|agenda|schedule)|{_DATE_ANCHOR})"
    # "what is up for tomorrow", "what's happening today", "anything planned this weekend"
    r"|\b(?:what(?:['’]?s| is| are)|anything)\s+(?:up|happening|going on|planned|scheduled|coming up)\s+"
    r"(?:for\s+|on\s+|this\s+|next\s+)?(?:today|tonight|tomorrow|weekend|week|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)\b"
    r"|\b(?:is|are)\s+there\s+(?:a|an|any)\s+(?:meeting|event|email|message|text|reminder|appointment|class)"
    r"|\b(?:when|where|what time)\s+(?:is|was|are)\s+(?:the|my|our)\s+\w+.*\b(?:today|tomorrow|tonight|"
    r"this|next|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I)
# First-person and "my <time>" lookups that the narrowed _PERSONAL above does not
# reach on its own: "who am I meeting tomorrow", "how busy am I this week", "did
# I get a text from mom", "do I owe Sam money", "track my order", "summarize my
# day", "am I going to be late for my 3pm", "what time is my lunch with Sam",
# "how long is my commute". Each is led by a lookup word or names a record, so
# "who am I to judge", "am I overthinking this" and "did I do the right thing"
# stay chat.
_CLOCK = r"(?:\d{1,2}(?::\d\d)?\s*(?:am|pm|a\.m\.|p\.m\.)|noon|midnight)"
_PARCEL_NOUNS = (r"orders?|packages?|parcels?|deliver(?:y|ies)|shipments?|refunds?|claims?|payments?|reservations?|"
                 r"bookings?|tickets?|luggage|flights?|rides?|returns?|applications?")
_ARRIVING_NOUNS = (r"invoices?|bills?|statements?|receipts?|refunds?|payments?|che(?:que|ck)s?|packages?|parcels?|"
                   r"orders?|shipments?|deliver(?:y|ies)|stuff|things|luggage|bags?|rides?|food|groceries|mail|"
                   r"letters?|tickets?|passport|card")
_ARRIVING_VERBS = (r"arriv\w*|come|came|coming|get\s+here|be\s+here|here|ship\w*|go\s+through|gone\s+through|"
                   r"clear\w*|post(?:ed)?|show\s+up|land\w*|deliver\w*|been\s+(?:sent|received|paid|processed)")
_DAY_WORDS = r"(?:day|week|weekend|morning|afternoon|evening|month|schedule|calendar)"
_PERSONAL_LOOKUPS = re.compile(
    # "my 3pm", "my 2pm", "my noon": a clock time after "my" is a calendar entry.
    rf"\bmy\s+{_CLOCK}"
    r"|\bwho\s+(?:am\s+i|do\s+i)\s+(?:meeting|seeing|having|owe|need\s+to\s+(?:call|text|email|reply|meet|see)|"
    r"have\s+(?:a|an|any|to\s+(?:call|text|email|reply)))\b"
    rf"|\bhow\s+busy\s+(?:am\s+i|is\s+(?:my\b|today|tonight|tomorrow|this\s+{_DAY_WORDS}|next\s+(?:week|month)|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day))"
    r"|\bam\s+i\s+(?:going\s+to\s+(?:be\s+late|make\s+it|miss|be\s+on\s+time)|late\s+for|running\s+late|on\s+time|"
    r"meeting|seeing|supposed\s+to\s+(?:meet|see|be\s+at))\b"
    r"|\bdid\s+i\s+(?:get|receive|miss)\s+(?:an?|any|the|my|that)\s+(?:[\w'’-]+\s+)?"
    r"(?:texts?|e-?mails?|messages?|mail|calls?|voicemails?|invites?|invitations?|packages?|parcels?|repl(?:y|ies)|"
    r"responses?|notifications?|alerts?|letters?)\b"
    r"|\bdo\s+i\s+owe\s+(?:[\w'’]+\s+){0,2}?(?:money|(?:an?\s+)?(?:repl(?:y|ies)|text|call|e-?mail|response|payment|"
    r"rent|taxes|tax|bill))\b"
    r"|\bhow\s+much\s+(?:do\s+i|i)\s+owe\b"
    r"|\bhow\s+many\s+(?:(?:unread|new|open|overdue|pending)\s+)?(?:e-?mails?|texts?|messages?|tasks?|reminders?|"
    r"meetings?|events?|appointments?|notifications?|to-?dos?|invites?|calls?|voicemails?)\b"
    r"[^?.!]{0,40}\b(?:do\s+i|have\s+i|did\s+i|i\s+(?:have|got)|are\s+(?:on|in|there|unread|waiting|left|due|open)|"
    r"unread|overdue|today|tomorrow|this\s+(?:week|morning|afternoon)|my\b)"
    r"|\bhow\s+many\s+(?:e-?mails?|texts?|messages?|tasks?|reminders?)\s+(?:are\s+)?(?:unread|overdue|due|open)\b"
    # parcels and payments
    rf"|\b(?:status\s+of|track(?:ing)?|where(?:['’]s|\s+is|\s+are))\s+(?:all\s+)?my\s+(?:[\w'’-]+\s+)?(?:{_PARCEL_NOUNS})\b"
    r"|\bstatus\s+of\s+(?:the|that|this)\s+(?:orders?|packages?|parcels?|deliver(?:y|ies)|shipments?|refund|claim|payment|invoice)\b"
    rf"|\b(?:did|has|have|will|when\s+will|when\s+does|when\s+do)\s+(?:all\s+)?(?:the|my|that|this|our|these|those)\s+"
    rf"(?:[\w'’-]+\s+)?(?:{_ARRIVING_NOUNS})\s+(?:\w+\s+){{0,2}}?(?:{_ARRIVING_VERBS})\b"
    r"|\b(?:when\s+(?:will|does|is)\s+(?:it|they)\s+(?:arrive|get\s+here|be\s+here|ship|come)|is\s+(?:it|they)\s+here\s+yet)\b"
    # recaps of the day
    rf"|\b(?:summari[sz]e|describe|recap|walk\s+me\s+through|run\s+through)\s+(?:for\s+me\s+)?my\s+(?:(?:whole|entire|upcoming)\s+)?{_DAY_WORDS}\b"
    rf"|\b(?:rundown|summary|overview|recap|run-?through)\s+of\s+my\s+(?:(?:whole|entire|upcoming)\s+)?{_DAY_WORDS}\b"
    # "what time is my lunch with Sam": a lookup lead, so any noun counts
    r"|\bwhat\s+time\s+(?:is|are|does|do|was)\s+my\s+(?!body\b|favou?rite\b)\w+"
    r"|\bwhat\s+time\s+(?:is|does)\s+(?:the|our)\s+(?:[\w'’-]+\s+)?(?:meeting|call|standup|stand-up|sync|interview|"
    r"appointment|class|lecture|flight|dinner|lunch|event)\b"
    r"|\bwhen(?:['’]s|\s+is|\s+are|\s+was|\s+does|\s+do)\s+my\s+(?!body\b|favou?rite\b)\w+"
    rf"|\bwhat(?:['’]s|\s+is)\s+on\s+at\s+(?:\d|{_CLOCK})"
    rf"|\bwhat\s+do\s+i\s+(?:have|got)\s+(?:at|by|before|after|around)\s+(?:\d|{_CLOCK})"
    rf"|\bwhat\s+do\s+i\s+need\s+to\s+(?:do|get\s+done|finish)\s+(?:\w+\s+){{0,2}}?{_DATE_ANCHOR}\b"
    r"|\bwhen\s+do\s+i\s+(?:need\s+to\s+|have\s+to\s+)?(?:leave|head\s+out|land|get\s+there|check\s+in)\b"
    r"|\bhow\s+long\s+(?:is|will|does)\s+my\s+(?:[\w'’-]+\s+)?(?:commute|drive|ride|meeting|flight)\b"
    r"|\bhow\s+long\s+is\s+(?:the\s+)?commute\b"
    rf"|\bhow(?:['’]s|\s+is|\s+does|\s+do)\s+(?:my\s+{_DAY_WORDS}|{_DATE_ANCHOR}|{_DAY_WORDS})\s+(?:look|looking|shaping)"
    # "is my dentist tomorrow", "is my interview still on for Friday"
    rf"|\b(?:is|are|was|will)\s+my\s+(?:[\w'’-]+\s+){{0,4}}?{_DATE_ANCHOR}\b"
    # the state of one of the user's things: "Are my bills paid?", "Is my train delayed?"
    r"|\b(?:is|are|was|were|does|did|has|have)\s+my\s+(?:[\w'’-]+\s+){0,2}?(?:been\s+)?(?:paid|pending|confirmed|"
    r"approved|declined|charged|sent|delivered|delayed|cancell?ed|active|valid|expired|expiring|renewed|done|filed|"
    r"booked|covered|processed|received|overdue|due|late|on\s+time|still\s+on|on\s+the\s+calendar)\b"
    # replies and updates from a person
    r"|\b(?:has|did|have)\s+(?!you\b|i\b|we\b|it\b)\w+\s+(?:replied|reply|responded|respond|texted|text|emailed|email|"
    r"called|call|messaged|message|gotten\s+back|got\s+back|answered|confirmed)\b"
    r"|\bany\s+(?:word|news|updates?|replies|messages?|texts?|e-?mails?|mail)\s+from\b"
    r"|\b(?:anything|is\s+there\s+anything)\s+(?:new\s+)?from\s+(?:my\s+)?\w+\b(?!\s+(?:the|a|an|that)\b)"
    # "what did Mom say?", "what did I say to Maya yesterday?", "did my landlord write back?"
    # Not "What did Einstein say about imagination?": family, "my X", or a recent channel.
    r"|\bwhat\s+did\s+(?:mom|mum|dad|my\s+[\w'’-]+|i|he|she|they)\s+(?:say|send|write|tell)\b"
    r"|\bwhat\s+did\s+(?!you\b)\w+\s+(?:say|send|write)\s+(?:in|on|last|yesterday|today|tonight|this|earlier|just)\b"
    r"|\bdo\s+i\s+owe\s+(?:my\s+)?\w+\s+(?:for|money|anything|back)\b|\bam\s+i\s+booked\b"
    r"|\bhow(?:['’]s|\s+is|\s+does)\s+my\s+\w+\s+look(?:ing)?\b"
    r"|\b(?:did|has|have)\s+(?:my\s+)?(?!you\b|i\b|we\b|it\b)\w+\s+(?:write|written|wrote|get|gotten|got)\s+back\b"
    rf"|\bwhat\s+am\s+i\s+(?:doing|up\s+to)\s+(?:\w+\s+){{0,2}}?{_DATE_ANCHOR}\b"
    r"|\bwhat\s+time\s+do\s+i\s+(?:need\s+to\s+|have\s+to\s+)?\w+"
    r"|\bdo\s+i\s+have\s+any\s+(?:new\s+|unread\s+|missed\s+)?(?:voicemails?|missed\s+calls?|calls?|invites?|"
    r"packages?|deliveries|bills?|payments?|replies)\b",
    re.I)
# A lookup lead with "my <thing>" close behind: "Who is my dentist?", "How old
# is my dog?", "What day is my interview?", "How much is my electric bill?". The
# catch-all main had ("any question with my X") also took "my phone keeps dying,
# any tips?"; this one needs the lead and leaves opinion and advice alone ("what's
# the best way to prepare for my exam", "how much water should my dog drink").
# A first-person question is about the user's own things ("Do I have a doctor's
# appointment?", "When do I fly out?", "How much PTO do I have?", "Where do I
# park?") unless it asks for an opinion, advice or a hypothetical: "should I
# learn guitar or piano", "what do I do with overripe bananas", "am I
# overthinking this", "do I need to peel ginger", "what do I do if my car
# won't start". "should I" never counts.
_FIRST_PERSON_ASK = re.compile(r"\b(?:do|did|am|have|was|were)\s+i\b", re.I)
_FIRST_PERSON_CASUAL = re.compile(
    r"\b(?:should|would|could|ought|better|best|worth|normal|weird|okay|wrong|crazy|mistake|"
    r"tips?|advice|ideas?|recommend\w*|suggest\w*|realistically|generally|"
    r"in\s+general|if|look\s+(?:like|tired|good|old|young|fat|thin)|too\s+(?:old|young|late|early)|"
    r"overthinking|over-?reacting|being\s+(?:silly|dramatic|unreasonable|paranoid)|right\s+thing|"
    r"apolog\w*|sorry|vote|judge|"
    r"going\s+to\s+be\s+(?:ok|okay|alright|all\s+right|fine)|how\s+(?:do|can|should|would)\s+i)\b"
    r"|\bwhat\s+do\s+i\s+(?:do|say|cook|make|eat|wear|watch|read|play)\s+(?:with|about|to|for|when|if)\b"
    r"|\bdo\s+i\s+(?:need|have)\s+to\s+(?!(?:reply|respond|answer|pay|call|text|email|send|submit|be|go|"
    r"bring|pick|leave|renew|sign|book|finish|return|check|show|attend|meet|see)\b)\w+"
    r"|\bdo\s+i\s+need\s+(?:a|an)\s+(?:visa|passport|vaccine|license|licence|permit)\b"
    r"|\bam\s+i\s+(?:a|an|the|too|really|being|right|smart|good|bad|ready|old|allowed)\b"
    # "Do I need to tip in Japan?", not "When do I fly to Japan?"
    r"|\b(?:need|have\s+to|tip|allowed)\b[^?.!]{0,30}\b(?:in|to)\s+(?:japan|france|europe|the\s+us|america|"
    r"canada|mexico|china|india|general)\b", re.I)


# "Is my prescription ready?", "Are my test results back?", "Is my gym membership
# still active?": the state of one of the user's things, unless the question
# judges it ("Is my sleep schedule bad?", "Is my cat normal?").
_MY_STATE = re.compile(r"^\s*(?:so\s+|and\s+|hey\s+|ok\s+)?(?:is|are|was|were|has|have|did|does)\s+my\s+[a-z]", re.I)
_MY_STATE_JUDGED = re.compile(
    r"\b(?:normal|bad|good|healthy|unhealthy|weird|strange|okay|ok|fine|enough|too|worth|safe|unsafe|smart|"
    r"right|wrong|better|worse|best|worst|ugly|pretty|cute|fat|thin|overweight|underweight|dangerous|common|"
    r"rare|possible|supposed\s+to|a\s+sign|considered|polite|rude|realistic|reasonable|toxic|healthy|harmful|"
    r"bad\s+for|good\s+for|should|would|could|likely|strong|weak|even|dying|sick|ill|readable|lying|"
    r"cheating|love|loves|like|likes|hate|hates|mad|angry|upset|attractive|gotten|getting|normal)\b", re.I)


_MY_LOOKUP = re.compile(
    r"^\s*(?:so\s+|and\s+|hey\s+|ok\s+)?(?:what|when|where|which|who|whose|how\s+(?:much|many|old|long|far|big|heavy)"
    r"|did|has|have)\b[^?.!]{0,40}?\bmy\s+[a-z]", re.I)
_MY_LOOKUP_OPINION = re.compile(
    r"\b(?:best|better|worst|good|great|nice|ideal|favou?rite|should|would|could|ought|recommend\w*|suggest\w*|"
    r"tips?|ideas?|advice|way\s+to|ways\s+to|worth|purpose|meaning|point\s+of|why|wrong\s+with|"
    r"opinion|think|thoughts?|feel|chances?|odds|options?|gift|present|worse)\b", re.I)
# "Is it going to rain during my coffee with Alex tomorrow?": a question that
# puts one of the user's plans on a day. "my" alone is not enough ("my phone
# keeps dying fast, any tips?"), and neither is a plan-less advice lead ("what
# should I wear to my sister's wedding tomorrow?").
_PLAN_ON_A_DAY = re.compile(
    rf"\b(?:with|during|before|after|around|until|for|at)\s+my\s+[\w'’ -]{{1,40}}?\b{_DATE_ANCHOR}\b"
    rf"|\bmy\s+[\w'’ -]{{1,40}}?\s+(?:with|at)\s+[\w'’ -]{{1,30}}?\b{_DATE_ANCHOR}\b", re.I)
# Asking for tips or comfort about one's own stuff is chat: "my meetings are
# exhausting, any tips?", "my day was rough, can you cheer me up?".
_CHAT_TAIL = re.compile(r"\bany\s+(?:tips|advice|ideas|suggestions)\b|\bcheer\s+me\s+up\b|"
                        r"\bis\s+(?:that|it)\s+(?:true|rude|normal|weird)\b", re.I)
_ADVICE_LEAD = re.compile(r"^\s*(?:what\s+should|should\s+(?:i|we)|what\s+to|any\s+(?:tips|advice|ideas)|"
                          r"how\s+(?:do|can|should|would)\s+(?:i|we))\b", re.I)
_QUESTION = re.compile(r"\?\s*$|^\s*(?:what|when|where|who|which|how|is|are|am|do|does|did|have|has|"
                       r"was|were|will|should|any)\b", re.I)
_COMPOSE_OR_GENERAL = re.compile(
    r"^\s*(?:please\s+)?(?:write|draft|compose|create|make|explain|translate|rewrite|tell me (?:a|about)|"
    r"give me (?:tips|ideas|a )|how (?:do|can|should|would) i|how to|why)\b", re.I)


def asks_personal_data(prompt: str) -> bool:
    """The request asks for the user's own current data, which only a tool can
    supply ("what's on my calendar", "did Jordan reply", "how heavy is my
    backpack"). Capability, how-to and compose requests are not."""
    text = normalize_day_words(prompt)
    if capability_question(text) or _COMPOSE_OR_GENERAL.match(text) or advice_question(text):
        return False
    if _CHAT_TAIL.search(text) and not re.search(_DATE_ANCHOR, text, re.I):
        return False
    return bool(_PERSONAL.search(text) or _PERSONAL_LOOKUPS.search(text)
                or (_MY_LOOKUP.match(text) and not _MY_LOOKUP_OPINION.search(text))
                or (_MY_STATE.match(text) and not _MY_STATE_JUDGED.search(text)
                    and not _MY_LOOKUP_OPINION.search(text))
                or (_QUESTION.search(text) and _FIRST_PERSON_ASK.search(text)
                    and not _FIRST_PERSON_CASUAL.search(text))
                # "Do I need to work Saturday?", "Do I have to fill out the form by Friday?"
                or (re.search(r"\bdo\s+i\s+(?:need|have)\s+to\b", text, re.I)
                    and re.search(_DATE_ANCHOR, text, re.I) and not re.search(r"\bshould\b", text, re.I))
                or (_QUESTION.search(text) and not _ADVICE_LEAD.match(text) and _PLAN_ON_A_DAY.search(text)))


# --- public facts ----------------------------------------------------------------
# A public, live fact asked plainly: a market price, an exchange rate, a game
# result. "What's the price of bitcoin?" was refused with "Private data is in
# context" on almost every turn (2026-10-29 Ask run): remembered facts ride
# along on nearly every Ask turn, and only "search the web for…" phrasing
# counted as an explicit public lookup. The whole request must be one such
# question (anchored, one clause, a few words), with nothing of the user's in it:
# "what's the exchange rate. Also the gate code from Dana is 4471" and "who won
# the game Dana went to" are not (0.5.0 dev8 egress audit).
_CURRENCY = (r"(?:usd|eur|gbp|jpy|inr|cad|aud|cny|chf|mxn|dollars?|euros?|pounds?|yen|"
             r"rupees?|pesos?|francs?|yuan)")
_ASSET = (r"(?:bitcoin|btc|ethereum|eth|dogecoin|solana|gold|silver|oil|gas|"
          r"(?-i:[A-Z]{2,5})(?:\s+stock)?|[a-z]+\s+stock)")
_EVENT = (r"(?:game|match|series|world\s+series|super\s+bowl|cup|world\s+cup|final|finals|"
          r"championship|race)")
_FACT_LEAD = (r"^(?:(?:what(?:'s|s|\s+is|\s+was|\s+are)|tell\s+me|check|get|show\s+me|look\s+up)\s+)?"
              r"(?:the\s+)?(?:current\s+|latest\s+|today'?s\s+|live\s+)?")
_FACT_TAIL = r"(?:\s+(?:today|now|right\s+now|currently|tonight|yesterday))?\s*[?.!]?$"
_PUBLIC_FACT = re.compile(
    _FACT_LEAD + r"(?:"
    r"(?:price|value|cost)\s+of\s+(?:a\s+|an\s+|one\s+)?" + _ASSET
    + r"|" + _ASSET + r"\s+(?:stock\s+)?(?:price|quote)"
    + r"|how\s+much\s+is\s+(?:a\s+|one\s+)?(?:bitcoin|btc|ethereum|eth|gold)"
    + r"|convert\s+[\d.,]+\s*" + _CURRENCY + r"\s+(?:to|into|in)\s+" + _CURRENCY
    + r"|[\d.,]+\s*" + _CURRENCY + r"\s+(?:to|in)\s+" + _CURRENCY
    + r"|exchange\s+rate(?:\s+(?:for|of|from)\s+(?:the\s+)?" + _CURRENCY
    + r"(?:\s+(?:to|and|vs)\s+(?:the\s+)?" + _CURRENCY + r")?)?"
    + r"|who\s+won\s+(?:the\s+)?(?:last\s+|latest\s+|this\s+year'?s\s+|\d{4}\s+)?(?:[a-z]+\s+){0,2}?" + _EVENT
    + r"|(?:score|result)s?\s+(?:of|for|from)\s+(?:the\s+)?(?:last\s+|latest\s+|today'?s\s+|tonight'?s\s+)?"
    r"(?:[a-z]+\s+){0,2}?(?:game|match|race)"
    + r")" + _FACT_TAIL, re.I)
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|i'd|i'll|me|my|mine|myself|we|we're|our|ours|ourselves|"
                           r"us|let'?s|lemme|gimme)\b", re.I)
_FACT_MAX_WORDS = 12


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u201c": '"', "\u201d": '"'})


def _ascii_text(text: str) -> str:
    """NFKC-folded: full-width letters and digits become their ASCII forms, and
    curly quotes become straight ones (iOS and macOS smart punctuation types
    "what\u2019s", which would otherwise never count as a public fact)."""
    import unicodedata
    return unicodedata.normalize("NFKC", text or "").translate(_QUOTES)


def public_fact_question(prompt: str) -> bool:
    """The whole request is one public live fact asked plainly ("What's the
    price of bitcoin?", "convert 100 USD to euros", "who won the last World
    Series?"), in plain ASCII, with nothing of the user's in it."""
    text = _ascii_text(prompt).strip()
    return bool(text.isascii() and len(text.split()) <= _FACT_MAX_WORDS
                and _PUBLIC_FACT.match(text) and not _FIRST_PERSON.search(text))


# Words a search for a public fact may add to the user's own: the query of a
# public-fact search is checked against these plus the user's message, so a
# remembered fact or anything else from context cannot reach the web.
_LOOKUP_WORDS = frozenset("""
a an the of to in on for and vs at is what whats who won win winner wins
price prices current currently today now live latest last recent value quote cost
score scores result results final game games match series standings
rate rates exchange convert conversion usd eur gbp jpy inr cad aud cny chf mxn
dollar dollars euro euros pound pounds yen rupee rupees peso pesos franc francs yuan
stock stocks share shares crypto cryptocurrency market
""".split())
# The only characters a public-fact search may contain, after NFKC folding:
# anything else (Cyrillic, other scripts' digits) is invisible to a word check.
_QUERY_CHARS = re.compile(r"^[A-Za-z0-9 '.,?$%\-]*$")
# How many words a public-fact search may add to the user's own (dev8 egress
# re-review: lookup words alone had no cap).
_QUERY_EXTRA_WORDS = 4


def public_fact_query_problem(query: str, user_text: str, *, year: int | None = None) -> str | None:
    """None when a public-fact search is plain ASCII and every word comes from
    the user's own message or the small lookup vocabulary above; a year the
    user didn't type may be added only once and only this year or last year
    ("who won the World Series" in spring). The search is at most four words
    longer than the user's message, so lookup words alone can't spell out a
    long query. Anything else (a phone number, a gate code, a name) is refused."""
    q = _ascii_text(query).strip()
    if not q:
        return "Search with the user's own words."
    if not _QUERY_CHARS.match(q):
        return ("Private data is in context, so a public lookup may only search the user's own "
                "words in plain letters and digits. Search again with the user's words only.")
    user_tokens = re.findall(r"[a-z0-9]+", _ascii_text(user_text).lower().replace("'", ""))
    query_tokens = re.findall(r"[a-z0-9]+", q.lower().replace("'", ""))
    if len(query_tokens) > len(user_tokens) + _QUERY_EXTRA_WORDS:
        return ("Private data is in context, so a public lookup may only search the user's own "
                "words. Search again with a shorter query.")
    # Both spellings of the user's own words count: "Dana's" allows "danas" and "dana".
    allowed = set(user_tokens) | set(re.findall(r"[a-z0-9]+", _ascii_text(user_text).lower())) | _LOOKUP_WORDS
    this_year = year if year is not None else date.today().year
    extra, added_years = [], 0
    for w in query_tokens:
        if w in allowed:
            continue
        if re.fullmatch(r"\d{4}", w) and int(w) in (this_year, this_year - 1) and added_years == 0:
            added_years += 1
            continue
        extra.append(w)
    if extra:
        return ("Private data is in context, so a public lookup may only search the "
                "user's own words. Search again without: " + ", ".join(dict.fromkeys(extra)) + ".")
    return None


# --- argument repair -----------------------------------------------------------

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Only a tool's own parenthesized notice counts: a successful result (an email
# body saying "as expected") must never get a repair hint appended.
_ARG_PROBLEM = re.compile(r"couldn't understand|unsupported|not understood|invalid|unrecognized|"
                          r"must be one of|is not a valid", re.I)


def _next_weekday(name: str, today: date) -> date:
    target = _WEEKDAYS.index(name)
    return today + timedelta(days=(target - today.weekday()) % 7)


def argument_fix_hint(tool: str, args: dict, result: str, *, today: date | None = None) -> str:
    """A model-facing correction for a rejected argument, or "".

    Appended to the tool result so the model repairs its own call ("next 48
    hours" -> days=2) instead of asking the user to rephrase."""
    text = str(result or "").lstrip()
    if not (text.startswith("(") and _ARG_PROBLEM.search(text[:400])):
        return ""
    today = today or date.today()
    raw = " ".join(str(args.get(k) or "") for k in ("period", "day", "when", "range")).strip().lower()
    suggestion = ""
    if m := re.search(r"\bnext\s+(\d+|a|one|two|three|four|five|six|seven)\s+(hour|day|week)s?\b", raw):
        words = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
        n = int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
        days = math.ceil(n / 24) if m.group(2) == "hour" else n * (7 if m.group(2) == "week" else 1)
        suggestion = f"use days={max(1, min(days, 60))} instead of period"
    elif "weekend" in raw:
        saturday = _next_weekday("saturday", today)
        suggestion = f"use period='{saturday.isoformat()}' with days=2 (Saturday-Sunday)"
    elif day := next((d for d in _WEEKDAYS if d in raw), None):
        suggestion = f"use period='{_next_weekday(day, today).isoformat()}' (the date of {day.capitalize()})"
    elif re.search(r"\b(?:tonight|this evening|this morning|this afternoon)\b", raw):
        suggestion = "use period='today'"
    elif re.search(r"\b(?:last night|overnight)\b", raw):
        suggestion = "use period='yesterday' or 'last 1 days'"
    elif re.search(r"\b(?:tomorrow (?:morning|night|evening|afternoon))\b", raw):
        suggestion = "use period='tomorrow'"
    hint = (f"\n(This is an argument problem, not an answer. Fix the call yourself and call {tool} "
            "again now with a supported value; do NOT ask the user to rephrase.")
    return hint + (f" Suggested: {suggestion}.)" if suggestion else ")")
