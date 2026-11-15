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
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from typing import NamedTuple


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
# Cloud-everything (remote_policy.cloud_everything_active): no prefill guard and a
# 1M window, but each step's input still costs time to first token, so the step
# grows moderately and Ask stays quick.
CLOUD_ASK_PROMPT_BUDGET = 24000
CLOUD_ASK_HISTORY_RESERVE = 6000


def ask_prompt_budget() -> int:
    """ASK_PROMPT_BUDGET, or the cloud value under cloud-everything."""
    from service.inference.remote_policy import cloud_everything_active
    return CLOUD_ASK_PROMPT_BUDGET if cloud_everything_active() else ASK_PROMPT_BUDGET


def ask_history_reserve() -> int:
    """ASK_HISTORY_RESERVE, or the cloud value under cloud-everything."""
    from service.inference.remote_policy import cloud_everything_active
    return CLOUD_ASK_HISTORY_RESERVE if cloud_everything_active() else ASK_HISTORY_RESERVE


# Per-result cap in the model's context (the general loop allows 12,000).
ASK_TOOL_RESULT_CHARS = 6000
# Under cloud-everything, reading an email thread or a Messages conversation
# comes back whole; every other Ask result keeps the cap above, for speed.
WHOLE_READ_TOOLS = frozenset({"view_emails", "view_messages"})
# Still bounded: at most about 100k tokens, and at most a third of the active
# profile's window (4 characters a token), so two whole reads in one turn still
# leave room for the prompt, the schemas and the reply.
CLOUD_WHOLE_READ_CHARS = 400_000
# Under this a "whole" read cannot hold one long thread; tools keep their digest.
WHOLE_READ_MIN_CHARS = 50_000


def cloud_whole_read_chars() -> int:
    """Characters one whole read may carry under cloud-everything: CLOUD_WHOLE_READ_CHARS,
    or less on a smaller window or byte budget (the same profile limits
    documents.cloud_text_chars reads). 0 with the gate off."""
    from service.inference.remote_policy import cloud_everything_active, remote_only_profile
    if not cloud_everything_active():
        return 0
    try:
        profile = remote_only_profile()
        by_window = int(profile.context_window) * 4 // 3
        by_bytes = (int(profile.aggregate_bytes) - 16_384) // 4 // 3   # worst-case UTF-8
    except Exception:  # noqa: BLE001 — no readable profile: no whole reads
        return 0
    return max(0, min(CLOUD_WHOLE_READ_CHARS, by_window, by_bytes))


def ask_result_limit(name: str) -> int:
    """The Ask cap for one result of tool `name`."""
    if name in WHOLE_READ_TOOLS:
        return max(ASK_TOOL_RESULT_CHARS, cloud_whole_read_chars())
    return ASK_TOOL_RESULT_CHARS


# The cap the agent loop applies to the result of the tool now running, so a tool
# that can return a whole read sizes it to what its caller keeps. None outside the
# loop: such callers get the tools' small, digested results.
_RESULT_BUDGET: ContextVar[int | None] = ContextVar("wisp_tool_result_budget", default=None)


@contextmanager
def tool_result_budget(chars: int | None):
    token = _RESULT_BUDGET.set(chars)
    try:
        yield
    finally:
        _RESULT_BUDGET.reset(token)


def whole_read_budget() -> int:
    """Characters a whole read may return for the running call: the caller's cap,
    within cloud_whole_read_chars(). 0 when whole reads do not apply: gate off, no
    caller budget, or an effective cap under WHOLE_READ_MIN_CHARS (summarize_emails
    in Ask, non-Ask turns), where the tool keeps its old digested path."""
    whole = cloud_whole_read_chars()
    caller = _RESULT_BUDGET.get()
    if not whole or caller is None:
        return 0
    budget = min(whole, int(caller))
    return budget if budget >= WHOLE_READ_MIN_CHARS else 0


# Calls to ONE tool per turn. The corpus raw dumps were turns that called
# get_upcoming / view_emails eight times with shuffled arguments.
ASK_MAX_SAME_TOOL_CALLS = 3
# Real schemas a model-led turn starts with, and name-only stubs beyond them.
ASK_PRELOAD = 6
ASK_STUBS = 24
# The same under cloud-everything: the window holds a wider menu.
CLOUD_ASK_PRELOAD = 16
CLOUD_ASK_STUBS = 40


def ask_preload() -> int:
    from service.inference.remote_policy import cloud_everything_active
    return CLOUD_ASK_PRELOAD if cloud_everything_active() else ASK_PRELOAD


def ask_stubs() -> int:
    from service.inference.remote_policy import cloud_everything_active
    return CLOUD_ASK_STUBS if cloud_everything_active() else ASK_STUBS
# Name-only stubs always offered, whatever the lexical rank: the everyday
# personal sources and lookups a short prompt most often needs.
ASK_CORE_STUBS = frozenset({
    "get_upcoming", "summarize_emails", "view_emails", "summarize_messages",
    "view_messages", "search_notes", "search_reminders", "add_reminder",
    "web_search", "get_weather", "recall", "remember", "daily_brief",
    "collect_task_evidence", "lookup_contact", "set_timer", "system_status",
    "wisp_capabilities", "draft_email", "draft_message", "calculate", "world_time",
    "get_my_day_plan", "read_email_attachments",
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

# A final that says a change was made: "Set a reminder tomorrow at 10 PM: …",
# "I've added it to your calendar.", "Your reminder is set." The loop checks it
# against the turn's own tool outcomes (2026-11-04 Ask export: "yes go ahead"
# got "Set a reminder tomorrow at 10 PM…" with no tool call, and the user
# looked for a reminder that never existed).
_CLAIM_VERBS = (r"(?:set|created|added(?!\s+up\b)|scheduled|rescheduled|sent|texted|emailed|messaged|moved|deleted|"
                r"removed|cancel+ed|booked|saved|updated|archived|forwarded|replied|"
                r"marked\s+\S+\s+(?:as\s+)?(?:done|complete|completed|read)|"
                r"turned\s+(?:on|off)|switched\s+(?:on|off)|muted|unmuted)")
_CLAIM_NOUNS = r"(?:reminders?|timers?|alarms?|events?|meetings?|notes?|messages?|texts?|e-?mails?|drafts?)"
_CLAIM_I = re.compile(r"^(?:i(?:'ve|\s+have)?|i(?:'ve|\s+have)?\s+(?:just|now|also|already|gone\s+ahead\s+and))\s+"
                      + _CLAIM_VERBS + r"\b(?!\s+(?:aside|out|on|together|apart)\b)", re.I)
# Subjectless or passive, only as the reply's lead: "Set a reminder for 9 PM.",
# "Added to your calendar.", "Reminder set for Friday.", "Your note is saved."
_CLAIM_LEAD = re.compile(
    r"^(?:(?:created|added|scheduled|rescheduled|sent|texted|emailed|moved|deleted|removed|cancel+ed|booked|"
    r"saved|archived|forwarded)\b(?!\s+up\b)"
    r"|set\s+(?:up\s+)?(?:a|an|the|your)?\s*(?:reminder|timer|alarm|event)\b"
    r"|(?:(?:the|your|a)\s+)?" + _CLAIM_NOUNS + r"\s+(?:(?:is|has\s+been|was|are|have\s+been)\s+(?:now\s+)?)?"
    r"(?:set|created|added|scheduled|sent|booked|saved|moved|deleted|cancel+ed)\b"
    r"|you(?:'re|\s+are)\s+all\s+set\b|all\s+set\b|done\s*[.!]*$)", re.I)
# Not a claim: a question, a negation or a condition. A modal ("I can set…",
# "I'll add…") never matches the claim patterns in the first place.
_CLAIM_HEDGE = re.compile(r"\?|\b(?:not|never|nothing|n't)\b|n't\b|^(?:if|once|when|whenever|after)\b",
                          re.I)
_CLAIM_BACKREF = re.compile(r"\b(?:earlier|already|before|previously|last\s+time|yesterday|this\s+morning|"
                            r"a\s+(?:moment|minute|while)\s+ago)\b", re.I)
_CLAIM_PREFIX = re.compile(r"^\s*(?:\*\*)?(?:(?:ok(?:ay)?|sure|great|got\s+it|alright|perfect|done|yes|yep|"
                           r"all\s+set)\s*[—–,:!.-]+\s*)+", re.I)


class EffectClaim(NamedTuple):
    sentence: str
    backref: bool       # points back to an earlier turn ("I set it earlier")
    first_person: bool  # "I've set…"; False for "Set a reminder…"/"Reminder set."


def claimed_effect(text: str) -> EffectClaim | None:
    """The sentence where the reply says it made a change, else None.
    First-person claims count anywhere outside quotes, code and list items;
    subjectless and passive ones ("Set a reminder for 9 PM.", "Reminder set.")
    only as the reply's lead, where a narrated source line cannot be mistaken
    for one. A subjectless lead can also be advice ("Set a reminder so you
    don't forget"), so callers trust it only when the user asked for a change."""
    lead = True
    for line in (text or "").replace("’", "'").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith((">", '"', "`", "“", "-", "*", "•")) and not stripped.startswith("**"):
            lead = False
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", stripped):
            body = _CLAIM_PREFIX.sub("", sentence).strip().strip("*").strip()
            if re.fullmatch(r"(?:done|all\s+set)\s*[.!]*", sentence.strip().strip("*"), re.I):
                body = "done"
            if body and not _CLAIM_HEDGE.search(re.sub(r"\bdo\s+not\s+disturb\b", "", body, flags=re.I)):
                first = bool(_CLAIM_I.search(body))
                if first or (lead and _CLAIM_LEAD.search(body)):
                    return EffectClaim(sentence.strip(), bool(_CLAIM_BACKREF.search(sentence)), first)
            lead = False
    return None


_CLAIM_ITEM = re.compile(r"\b" + _CLAIM_NOUNS + r"\b|\b(?:to|from|off)\s+(?:your|the|my)\s+calendar\b|\bdo\s+not\s+disturb\b|\bfocus\b|"
                         r"\b(?:texted|emailed|messaged)\b", re.I)


def claimed_item(sentence: str) -> bool:
    """The claim names a thing a tool makes or sends (a reminder, a text, the
    calendar), not just a phrase like "I've sent you the list below"."""
    return bool(_CLAIM_ITEM.search(sentence or ""))


# Says plainly that nothing happened, in place of a claim no tool backs.
ASK_CLAIM_NOT_DONE = "I haven't done that yet: nothing was created, sent or changed."

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
    "to remove it later, open Settings → Google in the Wisp window (⌘, or right-click the "
    "menu bar icon, then Settings…) and choose \"Disconnect & erase\", which removes the "
    "account and everything Wisp derived from it. Never say it cannot be done."
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
    return capability_question(text) or bool(_WISP_SELF.search(text)) or bool(own_question(text))


# --- A write the assistant asked about, then never made ---------------------
# 2026-11-13 run: "Add one more bullet to the Wisp test note" -> "What should
# it say?" -> "Demo point 4" -> "four bullets are saved", with only a note
# search run. The answer to Wisp's own question carries no write word, so the
# turn lost the write's tools and the claim check did not see a requested
# change. A turn that answers such a question keeps the earlier request.
_NEW_REQUEST = re.compile(
    r"^\W*(?:(?:please|ok(?:ay)?|hey\s+wisp|wisp)[,\s]+)*(?:what|when|where|who|why|how|which|is|are|do|does|did|can|could|"
    r"will|would|show|list|find|search|read|check|tell)\b", re.I)
_PENDING_DECLINE = re.compile(r"^\W*(?:no|nope|cancel|stop|never\s*mind|don't|do\s+not|forget\s+it|nothing)\b", re.I)


def pending_write_request(prompt: str, previous: str, last_assistant: str | None) -> str:
    """The earlier request this message completes, else "". True only when the
    person's previous message asked for a change, the assistant's last reply
    ENDED with a question, and this message is a plain answer to it: not a
    decline, not a question and not a new request of its own."""
    from service.router.router import has_write_intent
    text = " ".join((prompt or "").replace("’", "'").split())
    earlier = (previous or "").strip()
    asked = (last_assistant or "").strip()
    if not text or not earlier or not asked or len(text) > 300 or "?" in text:
        return ""
    if "?" not in asked or re.search(r"[A-Za-z0-9]", asked[asked.rfind("?") + 1:]):
        return ""
    if _PENDING_DECLINE.search(text) or _NEW_REQUEST.search(text) or has_write_intent(text):
        return ""
    return earlier if has_write_intent(earlier) else ""


def pending_write_hint(earlier: str) -> str:
    return ("STILL OPEN: the user's last message answers the question you asked. Their request is "
            f"still not done: \"{earlier[:300]}\". Their answer is the missing detail. Make the tool call "
            "for that request now, with their answer; do not search again, and do not say it is done "
            "until the tool result says so.")


# A change stated in any form, for a turn where the person asked for one and
# no tool made it: "The note now has four bullets.", "Demo point 4 was added
# to your note.", "Your four bullets are saved." claimed_effect covers the
# first-person and lead forms; this covers the rest.
_WRITE_DONE = (r"(?:set|created|added|appended|scheduled|rescheduled|sent|moved|deleted|removed|cancel+ed|booked|"
               r"saved|updated|edited|changed|renamed|archived|forwarded|written|recorded|logged|completed|"
               r"marked\s+(?:as\s+)?(?:done|complete|completed))")
_WRITE_THING = (r"(?:reminders?|timers?|alarms?|events?|meetings?|appointments?|notes?|messages?|texts?|e-?mails?|"
                r"drafts?|bullets?|bullet\s+points?|lines?|items?|entr(?:y|ies)|lists?|calendar)")
_WRITE_PASSIVE = re.compile(
    rf"\b{_WRITE_THING}\b[^.?!\n]{{0,80}}\b(?:is|are|was|were|has\s+been|have\s+been|got|gets)\s+"
    rf"(?:now\s+|successfully\s+|all\s+)?{_WRITE_DONE}\b"
    rf"|\b(?:is|are|was|were|has\s+been|have\s+been)\s+(?:now\s+|successfully\s+)?{_WRITE_DONE}\b"
    rf"[^.?!\n]{{0,60}}\b{_WRITE_THING}\b"
    rf"|\b{_WRITE_THING}\b[^.?!\n]{{0,40}}\bnow\s+(?:has|have|contains?|includes?|reads?|shows?|lists?|says?)\b"
    rf"|\bnow\s+(?:has|have|contains?|includes?)\b[^.?!\n]{{0,40}}\b{_WRITE_THING}\b"
    rf"|^(?:i(?:'ve|\s+have)?(?:\s+(?:just|now|also|already|gone\s+ahead\s+and))?\s+)?"
    rf"(?:appended|edited|changed|renamed|written|wrote|recorded|logged|put|completed)\b[^.?!\n]{{0,80}}\b{_WRITE_THING}\b",
    re.I)
_WRITE_MODAL = re.compile(
    r"\b(?:i'll|i\s+will|i\s+can|i\s+could|can\s+i|should\s+i|shall\s+i|would\s+you|want\s+me|let\s+me|"
    r"going\s+to|about\s+to|ready\s+to|will\s+be|can\s+be|could\s+be|to\s+be|needs?\s+to|once|before\s+i)\b", re.I)


# A change said to be under way or promised, with nothing behind it (2026-11-13
# real-model run: "Move my 3pm with Alex Chen today to 4" -> "I'm moving your
# Coffee with Alex Chen from 3:00 PM to 4:00 PM today… I'll let you know as
# soon as that change is confirmed." Only a calendar read had run). A turn has
# no later step: what is not done when the answer is written is not done.
_WRITE_DOING = (r"(?:moving|sending|adding|appending|creating|scheduling|rescheduling|deleting|removing|"
                r"cancel+ing|booking|saving|updating|setting|texting|e-?mailing|messaging|forwarding|replying|"
                r"changing|renaming|writing|putting|marking|completing|archiving)")
_WRITE_PROGRESS = re.compile(
    rf"\bi(?:'m|\s+am)\s+(?:now\s+|just\s+|currently\s+|also\s+|going\s+ahead\s+and\s+)*{_WRITE_DOING}\b"
    rf"|^(?:now\s+)?{_WRITE_DOING}\s+(?:it|that|this|your|the)\b[^.?!\n]{{0,80}}\b(?:now|\.\.\.|…)"
    r"|\bi(?:'ll|\s+will)\s+(?:let\s+you\s+know|confirm|update\s+you|tell\s+you|report\s+back)\b[^.?!\n]{0,80}"
    r"\b(?:confirmed|done|complete|completed|finished|through|moved|sent|saved|added|deleted|updated|set|ready)\b",
    re.I)
_PROGRESS_HEDGE = re.compile(r"\?|\b(?:not|never|nothing)\b|n't\b|^(?:if|should|would)\b", re.I)


def claimed_write(text: str) -> str:
    """The sentence where the reply states a change was made, in any form,
    else "". Only for a turn where the person asked for a change: on a read it
    would catch narrated data ("the event was moved by Dana")."""
    for line in (text or "").replace("’", "'").splitlines():
        stripped = line.strip().lstrip("-*• ").strip()
        if not stripped or stripped.startswith((">", '"', "`", "“")):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", stripped):
            body = _CLAIM_PREFIX.sub("", sentence).strip().strip("*").strip()
            if body and _WRITE_PROGRESS.search(body) and not _PROGRESS_HEDGE.search(body):
                return sentence.strip()
            if not body or _CLAIM_HEDGE.search(body) or _WRITE_MODAL.search(body):
                continue
            if _CLAIM_I.search(body) or _CLAIM_LEAD.search(body) or _WRITE_PASSIVE.search(body):
                return sentence.strip()
    return ""


# --- One named reminder is deleted by cancel_event, never a bulk clearer ----
# "Delete the Wisp test reminder" got a card for clear_past_reminders
# ("permanently deletes 0 past-due items"). cancel_event removes ONE reminder
# or event by title, in Reminders.app too; the past-due clearer only sees items
# whose time has gone by.
_ONE_REMINDER = re.compile(
    r"\b(?:delete|remove|cancel|erase|trash|get\s+rid\s+of|drop)\s+(?:the|my|that|this)\s+"
    r"(?:[^.?!\n,]{1,60}?\s+)?reminder\b(?!s)", re.I)
_MANY_REMINDERS = re.compile(
    r"\b(?:all|every|each|overdue|old|older|past[-\s]due|past|expired|stale|completed|finished|done|"
    r"today's|tomorrow's|reminders)\b", re.I)
# clear_reminders with the title as `query` stays: its card lists exactly the
# matching reminder (tests/test_conversation_authority_contract.py).
BULK_REMINDER_CLEARERS = frozenset({"clear_past_reminders"})
ONE_REMINDER_DELETE_TOOL = "cancel_event"


def single_reminder_delete(prompt: str) -> bool:
    """The person asks to delete ONE named reminder ("Delete the dentist
    reminder"), not many ("clear my overdue reminders")."""
    text = " ".join((prompt or "").replace("’", "'").split())
    return bool(_ONE_REMINDER.search(text)) and not _MANY_REMINDERS.search(text)


# --- "Delete that reminder", right after one was made in this chat -----------
# 2026-11-13 real-model run: after "Remind me to call the dentist tomorrow at
# 9am", "Delete that reminder" went to clear_reminders and then to cancel_event
# with no usable title; nothing was removed. The reminder is found in the
# store by what this chat just said, and cancel_event is bound to its title.
_THAT_REMINDER = re.compile(
    r"^\W*(?:(?:please|ok(?:ay)?|actually|wait|no|never\s*mind|hey\s+wisp|wisp)[,.\s]+)*"
    r"(?:(?:can|could|would)\s+you\s+)?(?:delete|remove|cancel|erase|trash|get\s+rid\s+of|drop)\s+"
    r"(?:that|this|the)\s+(?:last\s+|new\s+)?reminder"
    r"(?:\s+(?:i|you|we)\s+just\s+(?:made|set|added|created))?(?:\s+please)?\W*$", re.I)
ALL_REMINDER_CLEARERS = frozenset({"clear_past_reminders", "clear_reminders"})
CHAT_REMINDER_WINDOW_S = 3600


def _recent_reminders() -> list[dict]:
    """Reminders first seen in the last hour, newest first. Read-only."""
    import time
    try:
        from service.assistant.store import assistant_store
        return [r for r in assistant_store.recently_added(time.time() - CHAT_REMINDER_WINDOW_S)
                if r.get("kind") == "reminder"]
    except Exception:  # noqa: BLE001
        return []


def chat_reminder(prompt: str, previous: str, last_assistant: str | None) -> str:
    """The title of the reminder "delete that reminder" points at: the newest
    reminder added in the last hour whose title this chat's previous exchange
    names. "" when the message names a reminder itself or nothing matches."""
    text = " ".join((prompt or "").replace("’", "'").split())
    if not text or not _THAT_REMINDER.search(text):
        return ""
    said = set(re.findall(r"[a-z0-9]+", f"{previous or ''} {last_assistant or ''}".casefold()))
    for row in _recent_reminders():
        title = " ".join(str(row.get("title") or "").split())
        words = [w for w in re.findall(r"[a-z0-9]+", title.casefold()) if len(w) > 2]
        if words and all(w in said for w in words):
            return title
    return ""


def chat_reminder_hint(title: str) -> str:
    return (f"THAT REMINDER: the user means the reminder made earlier in this chat, \"{title[:120]}\". "
            f"Call cancel_event with title=\"{title[:120]}\" now. Do not ask which one and do not use a "
            "tool that clears many reminders.")


# --- "Set a reminder at 9:00 a.m. tomorrow for Mom's birthday" ---------------
# 2026-11-15: the model read "for Mom's birthday" as "on the birthday's date",
# looked the birthday up in Contacts, found none and refused, though the person
# had given both the time and the title. The typed compiler resolves the
# request; when it is complete (one reminder, an explicit time, a title) the
# turn creates exactly that and no contact dates are looked up. "Remind me on
# Mom's birthday" has no time, so it does not qualify and keeps its lookup.
def typed_reminder(prompt: str, *, now=None) -> dict | None:
    """add_reminder's arguments for a complete single-reminder request, else None."""
    from service.tasks.compiler import compile_reminder_create
    try:
        plan = compile_reminder_create(prompt, now=now)
    except Exception:  # noqa: BLE001 — a compiler miss leaves the turn to the model
        return None
    if (plan is None or plan.status != "ready" or not plan.subject.value
            or not plan.temporal.absolute_iso or plan.temporal.reference):
        return None
    # Two clock times ("at 9 … and text Dad at 5") are two things to do, or one
    # unclear alert: the model reads those itself.
    if len(re.findall(r"\b(?:at|by|@)\s*\d{1,2}(?::\d{2})?\b|\b\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\b"
                      r"|\b(?:noon|midnight)\b", plan.original_request, re.I)) > 1:
        return None
    return {"title": plan.subject.value, "when_iso": plan.temporal.absolute_iso, "kind": "reminder"}


def typed_reminder_hint(args: dict) -> str:
    return (f"THIS REMINDER: the user gave its time and title. Call add_reminder now with "
            f"title=\"{args['title'][:120]}\" and when_iso=\"{args['when_iso']}\". The time they "
            "gave is when it goes off; do not look up a birthday, anniversary or any other date, "
            "and do not ask for one.")


# --- "Reply to Dana's last email and say yes" --------------------------------
# Same run: only view_emails ran, searching for the instruction's own words
# ("Reply to Dana"), and the answer was that nothing was found. The sender's
# name is the search; the newest email from them is the one to answer.
_REPLY_VERB = r"(?:reply|respond|write\s+back|answer)"
_REPLY_WHO = (r"((?-i:[A-Z][\w.-]*(?:\s+[A-Z][\w.-]*)?)|(?:my\s+)?(?:mom|mum|dad|mother|father|sister|brother|"
              r"boss|manager|professor|prof|teacher|landlord|roommate|wife|husband|partner|girlfriend|boyfriend))")
_REPLY_NEWEST = r"(?:(?:last|latest|most\s+recent|recent|newest)\s+)?"
_EMAIL_REPLY = re.compile(
    rf"\b{_REPLY_VERB}\s+(?:to\s+)?{_REPLY_WHO}'s\s+{_REPLY_NEWEST}e-?mail\b"
    rf"|\b{_REPLY_VERB}\s+(?:to\s+)?(?:the\s+|that\s+)?{_REPLY_NEWEST}e-?mail\s+from\s+{_REPLY_WHO}", re.I)
_REPLY_WORDS = re.compile(r"\b(?:repl\w+|respon\w+|answer\w*|e-?mails?|last|latest|recent|newest|say\w*)\b", re.I)


def email_reply_sender(prompt: str) -> str:
    """Whose email a "reply to X's (last) email" instruction answers, else "".
    Not a writing request ("Draft a reply to..."): reply_reads covers those."""
    text = " ".join((prompt or "").replace("’", "'").split())
    if not text or len(text) > 400 or _WRITING.match(text) or _COMPOSE.match(text):
        return ""
    found = _EMAIL_REPLY.search(text)
    if not found:
        return ""
    name = (found.group(1) or found.group(2) or "").strip()
    name = re.sub(r"^my\s+", "", name, flags=re.I)
    return "" if name.casefold() in {"the", "that", "this", "my", "an", "a"} else name


# --- "Any emails I need to answer?" -----------------------------------------
# The Brief's reply cards and the day plan's "waiting on a reply" rows come from
# the Brief snapshot's threads (needs_reply). A bare mail search has no such
# state and said there were none. These questions read the day plan instead.
_OWE_REPLY = re.compile(
    r"\b(?:any|which|what|are\s+there\s+any|do\s+i\s+have\s+any|have\s+i\s+got\s+any)\s+"
    r"(?:unanswered\s+|new\s+|important\s+|urgent\s+)?(?:e-?mails?|messages?|mail)\b[^.?!]{0,40}?"
    r"\b(?:need|needs|require|requires|waiting\s+(?:on|for))\b[^.?!]{0,20}?\b(?:answer\w*|repl\w+|respon\w+)\b"
    r"|\b(?:any|which|what)\s+(?:e-?mails?|mail)\b[^.?!]{0,30}\b(?:i\s+(?:need|have|should|must|ought)\s+to|should\s+i)\s+"
    r"(?:answer|repl\w+|respond)\b"
    r"|\bdo\s+i\s+owe\s+(?:anyone|anybody|someone|people)\s+(?:a\s+|an\s+)?(?:repl\w+|answer|response|e-?mail)\b"
    r"|\bwho\s+(?:is|are)\s+(?:waiting\s+(?:on|for)\s+(?:a\s+)?(?:repl\w+|response|answer)|"
    r"(?:still\s+)?waiting\s+to\s+hear)\b"
    r"|\bwho\s+(?:do|should)\s+i\s+(?:need\s+to\s+)?(?:repl\w+\s+to|respond\s+to|answer|write\s+back)\b"
    r"|\b(?:e-?mails?|mail)\s+(?:that\s+)?(?:i\s+)?(?:still\s+)?(?:need|needs|have)\s+to\s+(?:be\s+)?(?:answer\w*|repl\w+|respon\w+)", re.I)


def emails_to_answer_question(prompt: str) -> bool:
    """"Any emails I need to answer?", "which emails need a reply?", "do I owe
    anyone a reply?": a question about replies owed, not a request to reply."""
    text = " ".join((prompt or "").replace("\u2019", "'").split())
    if not text or len(text) > 200 or _WRITING.match(text) or _COMPOSE.match(text):
        return False
    return bool(_OWE_REPLY.search(text))


EMAILS_TO_ANSWER_HINT = (
    "EMAILS TO ANSWER: call get_my_day_plan(day='today') once and answer from its people-waiting-on-a-reply "
    "items: those are the same emails the Brief marks as needing a reply. Name each sender and what they "
    "asked, newest first. Do not answer from a plain mail search, which cannot tell what needs a reply; say "
    "there are none only when the plan lists nobody waiting."
)


def reply_search_args(args: dict, sender: str) -> dict:
    """view_emails arguments for a reply turn: the search is the sender's name
    when the model searched for the instruction's words or for nothing."""
    query = str((args or {}).get("query") or "")
    if query and sender.casefold() in query.casefold() and not _REPLY_WORDS.search(query):
        return args
    return {**(args or {}), "query": sender}


def email_reply_hint(sender: str) -> str:
    return (f"REPLY TO AN EMAIL: first call view_emails with query=\"{sender}\" (the sender's name only, "
            f"never the words of the instruction). The newest email from {sender} is the one to answer: "
            "take its Message-ID and call reply_to_email with that message_id and the reply text in `body`, "
            "written as the user would send it. Never say nothing was found before searching by the name.")


# --- A tool call written as plain text in an answer -------------------------
# "What's on my calendar tomorrow?" ended with `get_my_day_plan(day='tomorrow')`
# printed in the reply. Only a registered tool's name followed by a call's
# brackets is removed, with its backticks.
_CALL_TEXT = re.compile(r"`{0,3}\s*(?:print\s*\(\s*)?\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\s*"
                        r"\((?:[^()\n`]|\([^()\n`]*\)){0,300}\)\s*\)?\s*`{0,3}")
_CALL_FENCE = re.compile(r"```(?:tool_code|tool_call|python|json)?\s*```", re.I)


def strip_call_text(text: str, tool_names) -> str:
    """The reply without tool calls written as text (`tool_name(arg='x')`)."""
    names = set(tool_names or ())
    if not text or "(" not in text or not names:
        return text or ""
    out = _CALL_TEXT.sub(lambda m: "" if m.group(1) in names else m.group(0), text)
    if out == text:
        return text
    out = _CALL_FENCE.sub("", out)
    if out.count("```") % 2:
        # The call took its closing fence with it: drop the opening one too.
        out = re.sub(r"(?m)^[ \t]*```\w*[ \t]*\n?", "", out, count=1)
    lines = [line.rstrip() for line in out.splitlines()]
    # A line left with only list or label debris ("- ", "Calling:") goes too.
    debris = re.compile(r"\s*(?:[-*•]\s*)?(?:(?:calling|tool\s+call|call)\s*:?)?\s*[.:]?\s*", re.I)
    lines = [line for line in lines if not line.strip() or not debris.fullmatch(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --- Wisp's own permissions and switches ------------------------------------
# Ask has no tool for any of these: Full access, view-only, a standing "always
# allow" / "stop asking" rule, "Let Wisp act for me", Demo mode. Only the
# person changes them, in the app. A request to change one is answered with
# OWN_SWITCH_ANSWER and never reaches the model (2026-11-13 run: "Turn on Full
# access and always allow sending email" was answered as done, with no tool
# call), and a reply that claims such a change is replaced by it.
OWN_SWITCH_ANSWER = (
    "**I can't change that myself.** Wisp's own permissions and switches are changed only by "
    "you, in the app. Nothing was changed.\n\n"
    "- **Full access** and **Let Wisp act for me**: Settings › Privacy, or the notch's "
    "settings (the gear).\n"
    "- **Demo mode**: the menu-bar icon's menu.\n"
    "- **Always allow**: a button on an approval card, when the card offers one.\n"
    "- **Sending**: every email and text asks you every time. It can't be set to always allow."
)
_OWN_NAMED = r"(?:full\s+access|view[-\s]only|read[-\s]only\s+mode|demo\s+mode)"
_OWN_TOGGLE = (r"(?:turn(?:ing|ed)?|switch(?:ing|ed)?|enabl\w+|disabl\w+|activat\w+|deactivat\w+|toggl\w+"
               r"|start(?:ing)?|stop(?:ping)?|exit|leave|enter|go(?:ing)?\s+(?:in)?to"
               r"|(?:put|set|give|grant|allow)\s+(?:yourself|you|u|wisp)"
               r"|take\s+(?:yourself|you|wisp)\s+out\s+of)")
# "Give Dana full access to the folder" is someone else's access, not Wisp's.
_OWN_OTHER_ACCESS = re.compile(
    r"\bfull\s+access\s+to\s+(?!you\b|u\b|wisp\b|everything\b|(?:my|this|the)\s+mac\b)", re.I)
_OWN_NAMED_REQUEST = re.compile(
    rf"\b{_OWN_TOGGLE}\b[^.?!\n]{{0,60}}\b{_OWN_NAMED}\b|\b{_OWN_NAMED}\s+(?:back\s+)?(?:on|off)\b"
    rf"|\b{_OWN_NAMED}\b[^.?!\n]{{0,30}}\b(?:turned|switched)\s+(?:on|off)\b", re.I)
_OWN_ACT_REQUEST = re.compile(
    rf"\b{_OWN_TOGGLE}\b[^.?!\n]{{0,40}}\bact(?:ing)?\s+for\s+me\b"
    r"|^\W*(?:(?:yes|ok(?:ay)?|sure|please|hey\s+wisp|wisp)[,\s]+)*(?:let\s+(?:wisp|you|yourself)\s+act\s+for\s+me"
    r"|(?:start\s+)?act(?:ing)?\s+for\s+me|act\s+on\s+your\s+own)\b"
    r"|\b(?:you|u|wisp)\s+(?:can|may|should)\s+(?:now\s+)?act\s+(?:for\s+me|on\s+your\s+own)\b"
    r"|\bi\s*(?:want|would\s+like|'d\s+like)\s+(?:you|wisp)\s+to\s+act\s+(?:for\s+me|on\s+your\s+own)\b", re.I)
_OWN_STANDING_REQUEST = re.compile(
    r"\balways\s+allow\b|\bstop\s+asking(?!\s+(?:me\s+)?(?:about|how|what|if|whether|why|questions?)\b)"
    r"|\b(?:don't|do\s+not|never|no\s+need\s+to|quit)\s+ask(?:ing)?\s+(?:me\s+)?"
    r"(?:again|every\s*time|each\s+time|any\s*more|before\s+(?:you\s+)?(?:send\w*|text\w*|e-?mail\w*|reply\w*)"
    r"|for\s+(?:permission|confirmation|approval|my\s+ok(?:ay)?))\b"
    r"|\b(?:from\s+now\s+on|always|every\s*time)\b[^.?!\n]{0,60}\bwithout\s+(?:asking|confirm\w+|my\s+(?:ok(?:ay)?|approval|permission))\b"
    r"|\bwithout\s+(?:asking|confirm\w+|my\s+(?:ok(?:ay)?|approval|permission))\b[^.?!\n]{0,40}\b(?:from\s+now\s+on|always|every\s*time)\b"
    r"|\bauto[-\s]?(?:approve|confirm|send)\b|\bskip\s+(?:the\s+)?confirm\w+"
    r"|\b(?:turn|switch)\s+off\s+(?:the\s+|all\s+)?(?:confirm\w+|approval\w*)\b", re.I)
# A question about a switch ("Is Full access on?", "What does Demo mode do?")
# is not a request to change it. "How do I…" and "Can you…" are: the fixed
# answer says where it is done.
_OWN_STATE_QUESTION = re.compile(
    r"^\W*(?:(?:so|and|hey\s+wisp|wisp)[,\s]+)*(?:what|why|when|which|who|is|are|was|does|do|did|has|have"
    r"|how\s+(?:much|many|does|is|long))\b", re.I)


def own_switch_request(prompt: str) -> bool:
    """The person asks Wisp to change one of its OWN permissions or switches
    (Full access, view-only, Demo mode, "Let Wisp act for me", or a standing
    always-allow / stop-asking rule). Not a question about one, and not access
    to something else ("give Dana full access to the folder")."""
    text = " ".join((prompt or "").replace("’", "'").split())
    if not text or len(text) > 400 or _OWN_STATE_QUESTION.search(text):
        return False
    named = _OWN_OTHER_ACCESS.sub(" ", text)
    return bool(_OWN_NAMED_REQUEST.search(named) or _OWN_ACT_REQUEST.search(text)
                or _OWN_STANDING_REQUEST.search(text))


_OWN_THING = (r"(?:full\s+access|view[-\s]only|read[-\s]only\s+mode|demo\s+mode|act(?:ing)?\s+for\s+(?:you|me)"
              r"|always[-\s]allow\w*|standing\s+rule|without\s+asking|auto[-\s]?(?:approv\w+|send\w*))")
_OWN_DID = (r"(?:\bi(?:'ve|\s+have)?(?:\s+(?:just|now|also|already))?\s+(?:turned|switched|enabled|disabled|activated"
            r"|allowed|set|granted|updated|changed|added|saved|stopped|started|put|given)\b"
            r"|\b(?:is|are)\s+now\s+(?:on|off|enabled|disabled|active|allowed|set|in\s+place)\b"
            r"|\b(?:has|have)\s+been\s+(?:turned|switched|enabled|disabled|activated|allowed|set|granted|saved)\b"
            r"|\b(?:both|all)\s+(?:are|of\s+(?:those|them)\s+are)\s+(?:now\s+)?(?:on|enabled|set)\b)")
_OWN_PROMISE = re.compile(
    r"\bi\s*(?:won't|will\s+not|(?:'ll|\s+will)\s+no\s+longer|(?:'ll|\s+will)\s+stop)\s+ask(?:ing)?\b"
    r"|\bi(?:'ll|\s+will)\s+(?:now\s+|always\s+)?(?:send|text|e-?mail|reply)\b[^.?!\n]{0,60}\bwithout\s+asking\b"
    r"|\bi(?:'ll|\s+will)\s+always\s+allow\b", re.I)
_OWN_CLAIM = re.compile(rf"{_OWN_DID}[^.?!\n]{{0,80}}\b{_OWN_THING}|\b{_OWN_THING}\b[^.?!\n]{{0,60}}{_OWN_DID}", re.I)
_OWN_CLAIM_HEDGE = re.compile(
    r"\?|\b(?:can't|cannot|can\s+not|couldn't|unable|not|never|nothing|haven't|hasn't|didn't)\b"
    r"|^(?:if|once|when|whenever|after|to)\b", re.I)


def own_switch_claim(text: str) -> bool:
    """The reply says Wisp changed one of its own permissions or switches, or
    promises a standing rule ("I won't ask again"). No tool can do either, so
    the claim is false whatever ran this turn."""
    for line in (text or "").replace("’", "'").splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            body = _CLAIM_PREFIX.sub("", sentence).strip().strip("*").strip()
            if not body:
                continue
            if _OWN_PROMISE.search(body):
                return True
            if _OWN_CLAIM.search(body) and not _OWN_CLAIM_HEDGE.search(body):
                return True
    return False


# --- Questions about what Wisp does on its own ------------------------------
# Answered by Wisp's own report (service/wisp_about.own_answer), written from
# live state, so the reply cannot claim more than is built. The 2026-11-13 run
# had Ask unable to explain "Let Wisp act for me", and "Will you reply to my
# emails for me?" ended in "Which source?".
_Q_LEAD = (r"^\W*(?:(?:so|and|ok(?:ay)?|hey\s+wisp|wisp|hey)[,\s]+)*(?:how\s+much|what|do|does|will|would|can|could"
           r"|is|are|when|which|tell\s+me|explain)\b")
_OWN_ALONE = (r"(?:on\s+(?:your|its)\s+own|by\s+(?:yourself|itself)|without\s+(?:me\s+)?(?:asking|being\s+asked)"
              r"|without\s+asking\s+me|automatically|unprompted|act(?:ing|s)?\s+for\s+me)")
_ACTING_Q = re.compile(rf"{_Q_LEAD}[^\n]*\b{_OWN_ALONE}\b", re.I)
_ABOUT_WISP = re.compile(r"\b(?:you|u|wisp|yourself)\b|\bact(?:ing|s)?\s+for\s+me\b", re.I)
_REPLIES_Q = re.compile(
    r"^\W*(?:(?:so|and|hey\s+wisp|wisp)[,\s]+)*(?:will|do|does|can|could|would)\s+(?:you|u|wisp)\s+"
    r"(?:ever\s+|automatically\s+|just\s+|also\s+)*(?:reply|respond|answer|write\s+back)\s+(?:to\s+)?"
    r"(?:(?:all\s+)?my|all|any|people's|incoming)?\s*(?:e-?mails|texts|messages|mail|people)\b", re.I)
_LEAVES_Q = re.compile(
    r"\b(?:leave|leaves|leaving)\s+(?:this|my|the)\s+(?:mac|macbook|computer|laptop|device|machine)\b"
    r"|\bstay(?:s|ing)?\s+on\s+(?:this|my|the)\s+(?:mac|macbook|computer|laptop|device|machine)\b"
    r"|\b(?:sent|go(?:es)?|uploaded)\s+(?:off|away\s+from)\s+(?:this|my|the)\s+(?:mac|macbook|computer|laptop|device)\b",
    re.I)
_WHATS_NEW_Q = re.compile(
    r"\bwhat(?:'s|\s+is|\s+has)\s+(?:new|changed)\s+(?:in|with|about)\s+(?:wisp|you|this\s+(?:version|release|update|build))\b"
    r"|\bwhat(?:'s|\s+is)\s+new\s+(?:in\s+)?(?:0\.5|this\s+version)\b"
    r"|\bwhat\s+changed\s+in\s+(?:wisp|this\s+(?:version|release|update))\b", re.I)


def own_question(prompt: str) -> str:
    """Which of Wisp's own reports answers this question: "acting" (what it
    does on its own), "replies" (will it answer mail for you), "leaves"
    (does anything leave this Mac), "whats_new", else ""."""
    text = " ".join((prompt or "").replace("’", "'").split())
    if not text or len(text) > 200:
        return ""
    if _WHATS_NEW_Q.search(text):
        return "whats_new"
    if _REPLIES_Q.search(text):
        return "replies"
    if _LEAVES_Q.search(text) and (text.endswith("?") or re.search(_Q_LEAD, text, re.I)):
        return "leaves"
    if _ACTING_Q.search(text) and _ABOUT_WISP.search(text):
        return "acting"
    return ""


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
    try:
        from service.proactive import acting
        lines.append(f"- What you do on your own: nothing unless \"Let Wisp act for me\" is on (it is "
                     f"{'on' if acting.enabled() else 'off'} now; the person switches it in Settings › Privacy). "
                     "With it on, with no limit per day: " + " ".join(acting.DOES)
                     + " " + acting.NEVER + " You draft replies; the person sends them.")
    except Exception:  # noqa: BLE001 - say less rather than guess
        pass
    lines.append("- You cannot change your own permissions or switches (Full access, Let Wisp act for me, "
                 "Demo mode, an always-allow rule). Never say you did; say where the person does it.")
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


# The channel is the person's word: "Email Priya Shah that I'll be ten minutes
# late" was sent as a text 1 run in 3 (2026-11-13 real-model run). An
# instruction that opens with "email <someone>" and never says text or message
# is not offered the text send, and the reverse.
_CHANNEL_LEAD = r"^\W*(?:(?:please|pls|hey\s+wisp|wisp|ok(?:ay)?|can\s+you|could\s+you|quickly|just)[,\s]+)*"
_EMAIL_SOMEONE = re.compile(_CHANNEL_LEAD + r"(?:send\s+(?:an?\s+)?e-?mail\s+to|e-?mail)\s+\w", re.I)
_TEXT_SOMEONE = re.compile(_CHANNEL_LEAD + r"(?:send\s+(?:a\s+)?(?:text|i?message)\s+to|text|i?message)\s+\w", re.I)
_EMAIL_WORD = re.compile(r"\be-?mail\w*|\bmail\b|\binbox\b", re.I)
_TEXT_WORD = re.compile(r"\btext\w*|\bi?messag\w+|\bsms\b", re.I)


NOTE_WRITERS = frozenset({"create_note", "append_note", "delete_note"})
_IN_NOTES = re.compile(r"\b(?:in|to|into|from)\s+(?:my\s+|the\s+)?notes\b|\bnotes\s+app\b|\bmy\s+[\w\s]{0,30}note\b", re.I)


def other_channel_sends(prompt: str) -> frozenset[str]:
    """The send and draft of the channel the person did NOT name: the text
    ones for "email Priya …", the email ones for "text Priya …". Empty when both or
    neither are named."""
    text = " ".join((prompt or "").split())
    # "Email myself a note saying Wisp test 3" made a Notes note (2026-11-13,
    # real Mac): after "email"/"text", "a note saying …" is the message.
    notes = frozenset() if _IN_NOTES.search(text) else NOTE_WRITERS
    if _EMAIL_SOMEONE.search(text) and not _TEXT_WORD.search(text):
        return frozenset({"send_message", "draft_message"}) | notes
    if _TEXT_SOMEONE.search(text) and not _EMAIL_WORD.search(text):
        return frozenset({"send_email", "draft_email"}) | notes
    return frozenset()


SELF_SEND_HINT = ("SEND TO THE USER: the user named themselves as the recipient. Call the send tool now with "
                  "to=\"me\"; Wisp fills in their own address. Do not ask for an address or a number, and do not "
                  "use any words of the message as the recipient. Text after a colon is the message itself.")


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
    forbidden |= other_channel_sends(prompt) & set(registry)
    if single_reminder_delete(prompt):
        # One named reminder: cancel_event deletes it; the past-due clearer cannot see it.
        forbidden |= {n for n in BULK_REMINDER_CLEARERS if n in registry}
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


# An attachment is named, or a kind of file is said to be in or from an email.
_ATTACHMENT_WORD = re.compile(r"\b(?:attachments?|attached|attaching)\b", re.I)
_FILE_KIND = (r"(?:pdf|pdfs|spreadsheet|spreadsheets|excel|xlsx?|docx?|word\s+doc(?:ument)?|"
              r"document|slides?|powerpoint|pptx?|screenshot|screenshots|picture|pictures|"
              r"photo|photos|image|images|scan|csv|invoice\s+pdf|file|files)")
_FILE_IN_EMAIL = re.compile(
    rf"\b{_FILE_KIND}\b[^.?!]{{0,40}}\b(?:(?:in|from|on|inside)\s+(?:(?:that|this|the|an?|my|her|his|their|\w+'s)\s+)*"
    r"e-?mails?|(?:e-?mail(?:ed)?|sent|sends|send)\s+(?:me|us|over)|(?:e-?mail(?:ed)?|sent)\b)", re.I)
_EMAIL_THEN_FILE = re.compile(
    rf"\b(?:e-?mail(?:ed)?|sent)\s+(?:me|us)\s+(?:a\s+|the\s+|an\s+)?{_FILE_KIND}\b", re.I)
# "email Sam the file" / "attach this to my note" are actions on a file, not a read.
_ATTACH_ACTION = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:attach|e-?mail|send|forward|add)\b"
    r"(?!.*\b(?:what|which|read|open|summari[sz]e|tell\s+me|show\s+me)\b)", re.I)


def attachment_first(prompt: str) -> bool:
    """A prompt that asks about a file inside an email ("what's in the
    attachment", "read the PDF John emailed me"): read_email_attachments leads
    the head start. Sending or attaching a file is not a read of one."""
    text = prompt or ""
    if _ATTACHMENT_WORD.search(text):
        return not _ATTACH_ACTION.search(text) or bool(
            re.search(r"\b(?:what|which|read|open|summari[sz]e|tell\s+me|show\s+me|in\s+the\s+attach)", text, re.I))
    if _ATTACH_ACTION.search(text):
        return False
    return bool(_FILE_IN_EMAIL.search(text) or _EMAIL_THEN_FILE.search(text))


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
    return ((bool(_CALENDARISH.search(prompt or "")) and asks_personal_data(prompt))
            or personal_event_question(prompt))


# "When is the next meeting?" / "the party on Saturday": a bare definite event
# of the kind people put on their own calendar, with nothing else in the
# request. It names no owner, but nobody asks the web when "the meeting" is:
# it is the user's own event (0.5.0-rc2 known issue). The whole request must
# be this shape; anything more keeps its existing route.
_OWN_EVENT_KIND = (r"(?:(?:team|staff|work|office|birthday|dinner|holiday|surprise|family)\s+)?"
                   r"(?:meeting|party|appointment|class|call|dinner|lunch|brunch|interview|"
                   r"stand-?up|shift|practice|rehearsal|check-?up|event)")
_OWN_EVENT_DAY = (r"(?:today|tomorrow|tonight|this\s+(?:week|weekend|morning|afternoon|evening)|next\s+week|"
                  r"(?:(?:on|this|next)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))")
_OWN_EVENT = re.compile(
    r"^(?:(?:when|what\s+time)\s+is\s+|when'?s\s+)?the\s+(?:next\s+)?" + _OWN_EVENT_KIND
    + r"(?:\s+" + _OWN_EVENT_DAY + r")?\s*[?.!]?$", re.I)


def personal_event_question(prompt: str) -> bool:
    """The whole request asks after a bare definite event of the user's own
    kind ("When is the next meeting?", "When is the party on Saturday?", "the
    party on Saturday"): it reads the calendar and reminders, never the web."""
    text = _ascii_text(prompt).strip()
    return bool(text.isascii() and _OWN_EVENT.match(text))


# Everyday wordings whose tool the lexical head start left out of its six real
# schemas (2026-11-12 probe of demo-style questions): "what did mom say" led
# with lookup_contact and travel_time, "set a timer for 5 minutes" with
# plan_meeting, "email Dana the notes" had no send_email at all. Each entry is
# (pattern, tools to lead with). They only order tools the turn already has.
# "what did you say", "what did the president say": not someone who texts the user.
_NOT_A_SENDER = r"(?!(?:you|i|we|it|this|that|the|a|an|wisp|they|there)\b)"
_DIRECT_LEADS = (
    # What someone said or whether they answered: their texts first, then mail.
    (re.compile(r"^(?:what\s+did|what\s+has|did|has|have)\s+" + _NOT_A_SENDER + r"[a-z][\w'.-]*(?:\s+[a-z][\w'.-]*){0,2}\s+"
                r"(?:say|said|texted|text|messaged?|repl(?:y|ied)|respond(?:ed)?|answer(?:ed)?|"
                r"wr(?:ite|ote)|g(?:e|o)t\s+back)\b", re.I),
     ("view_messages", "view_emails")),
    (re.compile(r"\b(?:any|new|unread|latest|recent)\s+(?:new\s+|unread\s+)?(?:texts?|messages?|imessages?)\b"
                r"|\b(?:texts?|messages?)\s+from\b|\bwho\s+(?:texted|messaged)\b", re.I),
     ("view_messages", "summarize_messages")),
    (re.compile(r"^(?:did|has|have)\s+" + _NOT_A_SENDER + r"[a-z][\w'.-]*(?:\s+[a-z][\w'.-]*){0,2}\s+e-?mail(?:ed)?\b"
                r"|\b(?:any|new|unread|latest)\s+e-?mails?\s+from\b|\bgrade\s+updates?\b", re.I),
     ("view_emails", "summarize_emails")),
    # "email <someone> ..." as an instruction. Never a question, and never
    # "email from Dana", "email about the W-9", "email Dana sent yesterday" or
    # "email addresses": those ask about mail, and send_email must not lead a read.
    (re.compile(r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?e-?mail\s+"
                r"(?!(?:me|address(?:es)?|from|about|re|regarding|threads?|that|which|the|in|on|with|to|for|"
                r"sent|and|or|is|was|of)\b)"
                r"[a-z][\w'.-]*(?!\s+(?:sent|send|sends|wrote|emailed|forwarded|replied|said|got|received)\b)\b",
                re.I),
     ("send_email",), "instruction"),
    (re.compile(r"\b(?:set|start|put)\s+(?:a|an|the|another)?\s*(?:[\w-]+\s+){0,3}?timer\b"
                r"|\btimer\s+for\b|\b(?:set|wake)\b.*\balarm\b|\balarm\s+(?:for|at)\b", re.I),
     ("set_timer", "manage_timers"), "timer"),
    (re.compile(r"\b(?:do\s+i\s+have|any|show|list|what\s+are)\b.*\breminders?\b|\bwhat(?:'s|\s+is)\s+overdue\b", re.I),
     ("search_reminders",)),
    # An index by name. The price tool only: a web search leads through
    # web_first, which has the privacy checks this pattern does not.
    (re.compile(r"\b(?:s\s*&\s*p(?:\s*500)?|s\s+and\s+p\s+500|dow\s+jones|the\s+dow|nasdaq)\b", re.I),
     ("get_stock_price",)),
)
# A request worded as a question ("did I set an alarm for 7?", "email from
# Dana?"): it reads, so a tool that sends or creates never leads it.
_ASKS = re.compile(r"^(?:did|do|does|is|are|was|were|has|have|had|how|what|when|where|who|which|why|any|"
                   r"can\s+i|could\s+i|should\s+i|will)\b|\?\s*$", re.I)


def direct_leads(prompt: str) -> list[str]:
    """Tools that lead the Ask head start for an everyday wording (see
    _DIRECT_LEADS), most wanted first. Empty for anything else."""
    text = _ascii_text(normalize_day_words(prompt or "")).strip()
    if not text.isascii() or len(text.split()) > 24:
        return []
    asks = bool(_ASKS.search(text))
    out: list[str] = []
    for pattern, tools, *kind in _DIRECT_LEADS:
        if not pattern.search(text):
            continue
        if kind == ["instruction"] and asks:
            continue                            # a question never leads with a send
        if kind == ["timer"] and asks:
            tools = ("manage_timers",)          # a question reads the timers; it sets none
        out.extend(n for n in tools if n not in out)
    return out


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
    Series?", "What are Costco's hours?"), in plain ASCII, with nothing of the
    user's in it."""
    text = _ascii_text(prompt).strip()
    return bool(text.isascii() and len(text.split()) <= _FACT_MAX_WORDS
                and _PUBLIC_FACT.match(text) and not _FIRST_PERSON.search(text)) or public_possessive_question(text)


# "What are Costco's hours?" got no web search on the first message of a chat
# (0.5.0-rc2 known issue): an 's of unknown ownership reads as private, as
# "what's Mom's number" must. A name owning a business's public hours, menu or
# return policy is a public fact. The owner is at most four words and never a
# relation or a pronoun; the head is only one of these public business facts
# (never a number, address, schedule or plan).
_POSSESSIVE_OWNER = r"(?P<owner>[a-z0-9][a-z0-9&-]*(?:\s+[a-z][a-z0-9&-]*){0,3})'s"
_POSSESSIVE_DAY = (r"(?:\s+(?:today|tomorrow|tonight|now|right\s+now|this\s+weekend|"
                   r"on\s+(?:mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|"
                   r"christmas(?:\s+eve)?|thanksgiving|easter|new\s+year'?s(?:\s+(?:day|eve))?)))?\s*[?.!]?$")
# A counter inside the business, between its name and the question.
_POSSESSIVE_DEPT = (r"(?:\s+(?:pharmacy|bakery|deli|cafe|food\s+court|gas\s+station|tire\s+center|"
                    r"garden\s+center|auto\s+center|optical|drive-?thru|store))?")
_PUBLIC_POSSESSIVE = re.compile(
    r"^(?:(?:what|when)\s+(?:are|is)\s+|what'?s\s+)?(?:the\s+)?"
    + _POSSESSIVE_OWNER + r"\s+"
    r"(?:(?:opening|store|holiday|business|weekend|pharmacy|regular|normal|current)\s+)?"
    r"(?:hours|opening\s+times|menu|return\s+policy)"
    + _POSSESSIVE_DAY, re.I)
# The same public fact asked as a yes/no or a time ("Is Trader Joe's open on
# Thanksgiving?", "When does Costco's pharmacy close?", "How late is Lowe's
# open?"): rc2 knew only the "<name>'s hours" wording, so these stayed private
# and got no search on the first message of a chat.
_PUBLIC_POSSESSIVE_OPEN = tuple(re.compile(r"^" + lead + _POSSESSIVE_DAY, re.I) for lead in (
    r"(?:is|are)\s+(?:the\s+)?" + _POSSESSIVE_OWNER + _POSSESSIVE_DEPT + r"\s+(?:still\s+)?(?:open|closed)(?:\s+late)?",
    r"(?:when|what\s+time)\s+(?:does|do|will)\s+(?:the\s+)?" + _POSSESSIVE_OWNER + _POSSESSIVE_DEPT
    + r"\s+(?:open|close)",
    r"how\s+late\s+(?:is|are)\s+(?:the\s+)?" + _POSSESSIVE_OWNER + _POSSESSIVE_DEPT + r"\s+open",
))


def _possessive_match(text: str):
    """The match of either public possessive wording; its `owner` group is the name."""
    return _PUBLIC_POSSESSIVE.match(text) or next(
        (m for pattern in _PUBLIC_POSSESSIVE_OPEN if (m := pattern.match(text))), None)
# Owners that are people in the user's life (or not a name at all).
_RELATION_OWNERS = frozenset("""
mom mum mommy mummy mama ma dad daddy papa pa mother father parents grandma grandpa granny gran nana
grandmother grandfather sister brother sis bro son daughter wife husband partner spouse boyfriend
girlfriend fiance fiancee aunt auntie uncle cousin niece nephew friend bestie boss manager coworker
colleague roommate neighbor neighbour kid kids baby teacher professor doctor dentist therapist nanny
babysitter landlord tutor coach family team honey babe everyone everybody someone somebody
nobody it that there here what who he she they his her their your its this these those
today tomorrow tonight yesterday
monday tuesday wednesday thursday friday saturday sunday
""".split())


def _possessive_owner_is_personal(text: str) -> bool:
    m = _possessive_match(text)
    if not m or _KIN_PUBLIC.fullmatch(m.group("owner").lower() + "'s"):     # "Papa John's" is a shop
        return False
    return any(w.lower() in _RELATION_OWNERS for w in m.group("owner").split())


# An owner that is a phone number or a street address is the user's detail,
# never a business ("<10 digits>'s hours", "123 Main St's hours"). A brand
# with a short number keeps working ("7-Eleven", "24 Hour Fitness").
_STREET_TYPES = frozenset("st street ave avenue rd road blvd dr drive ln lane way ct court "
                          "pl place hwy highway".split())
_PHONE_GROUP = re.compile(r"\d+(?:-\d+)+")


def _possessive_owner_is_contact_detail(owner: str) -> bool:
    words = owner.lower().split()
    if re.search(r"\d{4,}", owner) or any(_PHONE_GROUP.search(w) for w in words):
        return True
    return bool(len(words) >= 2 and words[-1] in _STREET_TYPES
                and any(re.fullmatch(r"\d+[a-z]?", w) for w in words[:-1]))


# Titles before a name ("Dr Patel"), dropped before the known-people check.
_OWNER_TITLES = frozenset("the dr mr mrs ms mx miss prof".split())


def _known_people_tokens() -> set[str] | None:
    """Lower-cased words of the names Wisp already holds locally: the know-you
    profile's people and the contacts already cached in memory. No model, no
    embedder, no Contacts or AppleScript read: the contacts module is used
    only if this process has already loaded it. None when no people data
    exists at all. Raises on a failed read (the caller fails closed)."""
    import sys
    from service.memory import profile as _profile
    names = [str(p.get("name") or "") for p in (_profile.load().get("people") or [])
             if isinstance(p, dict)]
    contacts = sys.modules.get("service.tools.imessage_tools")
    if contacts is not None:
        names.extend(contacts.contact_names())
    tokens = {w for name in names for w in re.findall(r"[a-z0-9&-]{2,}", _ascii_text(name).lower())}
    return tokens or None


def _possessive_owner_is_known_person(owner: str) -> bool:
    """The owner (titles aside) starts with, or is made only of, words of a
    name Wisp knows: "Dana", "Dr Patel", and "Dana Lee" when a Dana Smith is in
    the profile or contacts. "Trader Joe" is not, with a Joe known. A failed
    lookup counts as known; no people data at all counts as unknown, which
    leaves the words-only search rule as the only check."""
    words = [w for w in owner.lower().split() if w not in _OWNER_TITLES]
    if not words:
        return True
    try:
        tokens = _known_people_tokens()
    except Exception:  # noqa: BLE001 — an unreadable profile or cache fails closed
        return True
    return bool(tokens) and (words[0] in tokens or all(w in tokens for w in words))


def public_possessive_question(prompt: str) -> bool:
    """The whole request is a public business fact owned by a name ("What are
    Costco's hours?", "Trader Joe's holiday hours on Christmas"): the same
    ASCII, length and first-person rules as public_fact_question. The owner is
    never a relation, a pronoun, a phone number, a street address or a person
    Wisp knows locally."""
    text = _ascii_text(prompt).strip()
    if not (text.isascii() and len(text.split()) <= _FACT_MAX_WORDS
            and (m := _possessive_match(text)) and not _FIRST_PERSON.search(text)
            and not _possessive_owner_is_personal(text)):
        return False
    owner = m.group("owner")
    return not (_possessive_owner_is_contact_detail(owner) or _possessive_owner_is_known_person(owner))


# A public question whose answer changes, so the model's own memory is stale:
# opening hours ("Is Costco open on Sunday?", "When does Target close?"), who
# holds an office now ("Who is the president?"), the newest version of a
# product, today's news. In the 2026-11-04 debug export "who is the president?"
# was answered from memory with no search. These lead the Ask head start with
# web_search (service/main._ask_tool_rank); they open nothing a turn did not
# already have.
_WEB_FIRST = re.compile(
    r"^(?:"
    r"(?:is|are)\s+(?:the\s+)?[a-z0-9][a-z0-9&' -]{1,40}?\s+(?:still\s+)?(?:open|closed)\b.*"
    r"|(?:when|what\s+time)\s+(?:does|do|will)\s+(?:the\s+)?[a-z0-9][a-z0-9&' -]{1,40}?\s+(?:open|close)\b.*"
    r"|how\s+late\s+(?:is|are)\s+(?:the\s+)?[a-z0-9][a-z0-9&' -]{1,40}?\s+open\b.*"
    r"|[a-z0-9][a-z0-9&' -]{1,40}?\s+(?:store\s+|opening\s+|holiday\s+)?hours\b.*"
    r"|who(?:\s+is|'s|\s+are)\s+(?:the\s+)?(?:current\s+|new\s+|next\s+)?(?:us\s+|u\.s\.\s+)?"
    r"(?:president|vice\s+president|prime\s+minister|governor|mayor|senators?|chancellor|pope|king|queen|"
    r"secretary\s+of\s+[a-z ]+|speaker\s+of\s+the\s+house|ceo|chief\s+executive|chairman|head\s+coach)\b.*"
    # The newest of a product line, or of a named thing's versions: never "the
    # latest on <a topic>", which may be a deal or a project of the user's own.
    r"|(?:what(?:'s|\s+is)|which\s+is)\s+the\s+(?:latest|newest|most\s+recent)\s+(?:"
    r"(?:version|release|update|model|generation)\s+of\s+[a-z0-9][a-z0-9 .+-]{1,30}"
    r"|(?:apple\s+|samsung\s+|google\s+)?(?:iphone|ipad|macbook|imac|mac|macos|ios|ipados|watchos|apple\s+watch|"
    r"airpods|pixel|galaxy|android|windows|playstation|xbox|kindle)\b[a-z0-9 .+-]{0,20})\s*[?.!]?"
    # The day's news, with no topic: "any news on <x>" names a topic that may be private.
    r"|(?:what(?:'s|\s+is)\s+(?:the\s+|in\s+the\s+)?|today'?s\s+|top\s+|latest\s+)?"
    r"(?:news|headlines)(?:\s+(?:today|tonight|now|right\s+now|this\s+morning))?\s*[?.!]?"
    r")$", re.I)


def web_first(prompt: str) -> bool:
    """A short public question whose answer changes (see _WEB_FIRST), with
    nothing of the user's in it: the search leads the Ask head start."""
    text = _ascii_text(prompt).strip()
    if not (text.isascii() and len(text.split()) <= _FACT_MAX_WORDS
            and not _FIRST_PERSON.search(text) and _WEB_FIRST.match(text)
            and not _possessive_owner_is_personal(text)):
        return False
    words = set(re.findall(r"[a-z]+", text.lower()))
    # A relation, a thing of the user's own (their mail, their door), something
    # "from" or "with" someone, or "open to" an idea is not a web question.
    if (words & (_RELATION_OWNERS - _WEB_FIRST_PLAIN) or words & _WEB_FIRST_OWN
            or words & {"from", "with"} or re.search(r"\b(?:open|closed)\s+(?:to|for|about)\b", text, re.I)
            or asks_personal_data(prompt)):
        return False
    try:                                        # a person Wisp knows is not a business
        known = _known_people_tokens()
    except Exception:  # noqa: BLE001 — an unreadable profile or cache: no head start
        return False
    return not (known and words & known)


# Things that are the user's own even with no "my" ("the latest email from
# Sam", "is the garage door open", "office hours with the TA").
_WEB_FIRST_OWN = frozenset("""
email emails mail inbox message messages text texts imessage reminder reminders calendar event events
meeting meetings class classes office ta note notes file files folder document documents doc docs
order package delivery door window garage gate account appointment schedule flight trip homework
assignment exam midterm quiz grade grades chat thread task tasks todo timer alarm brief
deal deals project projects ticket tickets case cases application applications offer offers claim claims
status issue issues bug bugs request requests position role job listing sale contract proposal
budget budgets spreadsheet spreadsheets resume cv draft drafts plan report essay paper thesis
enrollment registration poll survey invoice order orders pr
""".split())
# Relation words that are also ordinary words of a public question.
_WEB_FIRST_PLAIN = frozenset("it that there here what who this these those today tomorrow tonight "
                             "yesterday monday tuesday wednesday thursday friday saturday sunday".split())


def personal_possessive_question(prompt: str) -> bool:
    """The request has the shape of a public possessive question but its owner
    is the user's own: a relation, a contact detail or a person Wisp knows
    ("Is Mom's open on Sunday?", "When does Dana's close tonight?"). It is the
    user's private matter and nothing about it goes to the web. With a time
    word the classifier used to leave these of unknown ownership, where a
    model-led turn could still search them (real-model run, 2026-11-12: a
    search for "Mom's operating hours on Sunday")."""
    text = _ascii_text(prompt).strip()
    return bool(text.isascii() and len(text.split()) <= _FACT_MAX_WORDS
                and _possessive_match(text) and not public_possessive_question(text))


# ---- Whose question is it? The checks below only ever tighten web access, so
# they carry none of the ASCII or length gates of the public shapes above: a
# request they cannot read is not thereby public.

# People in the user's life, as a word of the request ("How is Grandma doing
# today?", "Is Mom home yet?"). Narrower than _RELATION_OWNERS: "team",
# "doctor" and "teacher" are also words of public questions.
_KIN = frozenset("""
mom mum mommy mummy mama mamma dad daddy papa mother father parents grandma grandpa granny gran nana
grandmother grandfather sister brother sis bro son daughter wife husband spouse boyfriend girlfriend
fiance fiancee aunt auntie uncle cousin niece nephew bestie roommate landlord babysitter nanny
""".split())
# The same words inside a public name.
_KIN_PUBLIC = re.compile(
    r"\b(?:mother'?s\s+day|father'?s\s+day|uncle\s+sam|big\s+brother|papa\s+john'?s?|papa\s+murphy'?s?|"
    r"auntie\s+anne'?s?|sister\s+act|sister\s+wives|mamma\s+mia|mama\s+mia|brother\s+printers?|"
    r"son\s+of\s+\w+|son\s+heung-?min|father\s+of\s+the\s+bride|mother\s+of\s+the\s+bride|"
    r"mother\s+teresa|mother\s+nature|father\s+time|dad'?s\s+army|daddy\s+yankee|papa\s+roach|"
    r"big\s+mama|mama\s+june|father\s+christmas|mother\s+goose|brother\s+hl-?\w+|brother\s+mfc-?\w+)\b")
_KIN_PUBLIC_CASED = re.compile(_KIN_PUBLIC.pattern, re.I)
# Owners of an 's that are a person in the user's life (the relation words
# that are not pronouns or dates).
_PERSON_OWNERS = frozenset(_RELATION_OWNERS - frozenset("""
it that there here what who he she they his her their your its this these those everyone everybody
someone somebody nobody today tomorrow tonight yesterday monday tuesday wednesday thursday friday
saturday sunday
""".split()))
# A contraction's first word, never an owner ("what's", "it's", "let's").
_CONTRACTED = frozenset("what who it that there here he she how where when why let".split())
_LOOSE_APOSTROPHES = str.maketrans({c: "'" for c in "\u2018\u2019\u02bc\u02b9\u02bb\u2032\u00b4`\uff07\u055a\u07f4"})
# An apostrophe lookalike before a possessive s: the public shapes do not read
# it, so the request would pass as having no owner at all.
_LOOKALIKE_BACKTICK = re.compile(r"[A-Za-z]`s\b")
_LOOKALIKE_SITE = re.compile(r"[A-Za-z]([^\x00-\x7f])s\b")
_LOOSE_POSSESSIVE = re.compile(r"(?<![a-z0-9&.'-])([a-z0-9][a-z0-9&.-]{0,40})'s?(?=\s|$|[?.!,;:])")


def _loose_cased(text: str) -> str:
    """Accents removed and every apostrophe-like mark a plain one, casing kept."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text or ""))
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return plain.translate(_LOOSE_APOSTROPHES)


def _loose_text(text: str) -> str:
    """_loose_cased, lower-cased: "Is Mam\u00e1\u02b9s open?" reads as "is mama's open?"."""
    return _loose_cased(text).lower()


# The longest text the ownership checks read. A question about someone is
# short; a cap keeps every pattern below linear in what it is given.
_OWNER_TEXT_CAP = 600
_KIN_ALT = "|".join(sorted(_KIN, key=len, reverse=True))
# Family words that are just as often ordinary words ("baby formula", "pop
# music", "kids menu", "partner visa"). They count only where what follows
# shows a person is meant ("Is the baby asleep yet?", "Are the kids home yet?").
_SOFT_KIN = frozenset("kid kids baby twins pop pops partner hubby wifey granddad grandad grandson "
                      "granddaughter grandparents grandkids toddler".split())
_SOFT_ALT = "|".join(sorted(_SOFT_KIN, key=len, reverse=True))
_ANY_KIN_ALT = "|".join(sorted(_KIN | _SOFT_KIN, key=len, reverse=True))
# "my mom", "our landlord", "my little sister", "my kids".
_KIN_MINE = re.compile(rf"\b(?:my|our)\s+(?:[a-z]+\s+)?(?:{_ANY_KIN_ALT})\b")
# A question lead: "is", "when will", "how's", and with words between,
# "how old is", "which hospital is", "what time does".
_KIN_AUX = (r"(?:is|are|was|were|did|does|do|has|have|had|will|would|can|could|should|might|may|"
            r"isn't|aren't|wasn't|didn't|doesn't|hasn't|won't|can't)")
_KIN_LEAD = (rf"(?:(?:how|where|when|why|who|which|what)(?:'s|\s+{_KIN_AUX})?"
             rf"|(?:how|what|which)\s+[a-z]+(?:\s+[a-z]+)?\s+{_KIN_AUX}|{_KIN_AUX})")
# The kin word in subject position, right after a question lead at the start
# of a clause: "How is Grandma doing", "When will Dad be home", "Is Mom mad at
# me", "How old is Mom", "Which hospital is Grandma at". Whatever follows, the
# question is about them, unless what follows makes the kin word the start of
# a name (see personal_reference).
_KIN_SUBJECT = re.compile(rf"^{_KIN_LEAD}\s+(?:the\s+)?(?P<kin>{_KIN_ALT})\b", re.I)
_SOFT_SUBJECT = re.compile(rf"^{_KIN_LEAD}\s+(?:the\s+)?(?:{_SOFT_ALT})\b\s*(?P<next>[a-z']*)", re.I)
# The same with the verb dropped: "Mom home yet?", "Dad ok?", and with any
# -ing or -ed word when the clause is a question or carries a time word
# ("Mom feeling better today?", "Dad landed yet?").
_KIN_STATE = (r"(?:home|ok|okay|alright|back|here|there|awake|asleep|up|coming|free|busy|sick|better|fine|"
              r"around|in|out|still|safe|ready|done|yet)")
_KIN_BARE = re.compile(rf"^(?:the\s+)?(?:{_ANY_KIN_ALT})\s+{_KIN_STATE}\b", re.I)
_KIN_BARE_VERB = re.compile(rf"^(?:the\s+)?(?:{_ANY_KIN_ALT})\s+[a-z]+(?:ing|ed)\b", re.I)
_TIME_WORD = re.compile(r"\b(?:today|tonight|tomorrow|yet|now|lately|recently|this\s+(?:morning|afternoon|"
                        r"evening|week|weekend)|right\s+now|so\s+far)\b", re.I)
# Words that may follow a kin subject with a capital letter and still not make
# it a name ("Is Mom OK", "Is Dad Home"), and that show a soft kin word is a person.
_KIN_FOLLOW = frozenset("""
doing home ok okay alright fine well sick better worse awake asleep up free busy coming going here
there back around out in at on still yet now today tonight tomorrow right call called calling text
texted texting message messaged email emailed reply replied say said saying arrive arrived land
landed leave left leaving get got getting make made want wants wanted need needs needed feel feeling
sleep sleeping eat eating work working visit visiting drive driving fly flying send sent know see
ask asked like have has go come stay staying move moved moving plan planning pick picking mention
mentioned write wrote tell told think thinks and or with up be been mad upset lonely sad happy angry
tired hurt safe ready done late early eaten take taken pay paid fall fell i
""".split())
# The kin word as the one something was done to, in a question ("Did the
# hospital call Mom today?", "Will they let Dad go home today?").
_KIN_OBJECT = re.compile(
    rf"^(?:did|does|do|has|have|had|will|would|can|could|should|is|are|was|were|when|why|how)\b[^.!?\n]{{0,200}}?\b"
    rf"(?:call(?:ed|ing)?|text(?:ed|ing)?|let|see|seen|saw|visit(?:ed|ing)?|help(?:ed|ing)?|pick(?:ed|ing)?\s+up|"
    rf"told|tell|ask(?:ed|ing)?|e-?mail(?:ed|ing)?|message[d]?|messaging|release[d]?|discharg(?:e|ed|ing)|"
    rf"admit(?:ted)?|take|took|taking|bring|brought|drive|drove|driving|pay|paid|charge[d]?|evict(?:ed|ing)?)\s+"
    rf"(?:the\s+)?(?:{_KIN_ALT})\b")
# The same inside a clause: "Tell me if Mom is online right now", "do you know when Dad lands".
_KIN_CLAUSE = re.compile(
    rf"\b(?:if|whether|when|where|why|how|what|that)\s+(?:the\s+)?(?:{_ANY_KIN_ALT})\s+"
    rf"(?:is|are|was|were|has|had|will|would|did|does|can|could|got|gets|said|says|wants|needs|called|"
    rf"texted|arrived|arrives|landed|lands|left|leaves)\b")
# News of them, at the end: "any news from mom", "any word from Dad yet?".
_KIN_FROM = re.compile(
    rf"\b(?:from|with)\s+(?:the\s+)?(?:{_KIN_ALT})\b"
    rf"(?:\s+(?:today|tonight|yet|now|lately|recently|so\s+far|at\s+all|this\s+[a-z]+|right\s+now))*\s*[?.!]*$")
# With them, anywhere in a question: "Is dinner with Dad still on tonight?".
_KIN_WITH = re.compile(rf"\bwith\s+(?:the\s+)?(?:{_KIN_ALT})\b")
# Looking in on them: "check on mom", "check in on grandma today".
_KIN_CHECK = re.compile(rf"\bcheck(?:ing)?\s+(?:in\s+|up\s+)?on\s+(?:the\s+)?(?:{_ANY_KIN_ALT})\b")
# The 's left out, or no 's at all, before something that is theirs: "is moms
# flight on time today", "mom flight today", "grandma hospital today".
_KIN_THEIRS = (r"(?:flight|flights|surgery|appointment|appointments|hospital|doctor|meds|medication|"
               r"prescription|birthday|address|number|package|house|place|apartment|car|test|results?|"
               r"diagnosis|treatment|therapy|recovery|fever|schedule|trip|visit|rent|lease)")
_KIN_NOUN = re.compile(rf"\b(?:{_KIN_ALT})s?\s+{_KIN_THEIRS}\b")
# The subject after an auxiliary anywhere, when a state or action word follows:
# "xyzzy is mom ok today?" has no clause start to match, and is about Mom.
_KIN_MID = re.compile(
    rf"\b(?:is|are|was|were|did|does|has|have|had|will|would|can|could|should|how's|where's)\s+"
    rf"(?:the\s+)?(?:{_ANY_KIN_ALT})\s+(?P<next>[a-z']+)")
# Words at the start of a clause that are not part of the question.
_CLAUSE_FILLER = re.compile(
    r"^(?:(?:hi|hey|hello|yo|ok|okay|so|and|also|but|btw|by\s+the\s+way|wisp|please|um+|uh+|er+|well|"
    r"quick\s+question|one\s+more\s+thing|good\s+(?:morning|afternoon|evening)|morning|"
    r"sorry(?:\s+to\s+bother\s+you)?|thanks|thank\s+you|actually|anyway|oh|hmm+|now|then)\b[\s,!]*"
    r"|(?:[-*>]+|\d{1,2}[.)])\s*)+", re.I)
_CLAUSE_END = re.compile(r"[.!?:;,\n\"()\[\]]+|\s[-–—]+\s")


def _clauses(cased: str):
    """(clause, is_question) for each sentence or clause of `cased`: split at
    . ! ? : ; , quotes, brackets and spaced dashes, with greetings, fillers
    and list markers removed from the front of each."""
    start = 0
    for m in [*_CLAUSE_END.finditer(cased), None]:
        end = m.start() if m else len(cased)
        clause = _CLAUSE_FILLER.sub("", cased[start:end].strip(), count=1).strip()
        if clause:
            yield clause, bool(m and "?" in m.group())
        if m is None:
            break
        start = m.end()


def _readable(prompt: str) -> str:
    """The text the ownership checks read: accents and apostrophes folded,
    zero-width and other format characters and emoji removed, capped."""
    import unicodedata
    cased = _loose_cased((prompt or "")[:_OWNER_TEXT_CAP * 2])
    cased = "".join(" " if unicodedata.category(c) in {"So", "Sk", "Cs", "Co"} else c
                    for c in cased if unicodedata.category(c) != "Cf")
    return cased[:_OWNER_TEXT_CAP]


def personal_reference(prompt: str) -> bool:
    """The request is ABOUT someone in the user's life, named by a kin word:
    with "my" or "our" ("my mom", "our landlord"); in subject position after
    a question lead at the start of any sentence or clause, whatever follows
    ("How is Grandma doing today?", "Hi, is Mom home yet?", "How old is
    Mom", "Which hospital is Grandma at today?"), or with the verb dropped
    ("Mom home yet?", "Dad landed yet?"); as the one something was done to
    in a question ("Did the hospital call Mom today?"); inside a clause
    ("Tell me if Mom is online"); with them in a question ("Is dinner with
    Dad still on tonight?"); looked in on ("check on grandma"); or before
    something that is theirs with the 's left out ("is moms flight on time").

    A kin word that merely appears is not one: "best gifts for dad",
    "husband pillow reviews", "Brother printer". In subject position it is
    still a name when it starts one on the public list (_KIN_PUBLIC) or when,
    in the text as typed, the next word is capitalised and is not an ordinary
    word ("Is Sister Rosetta Tharpe touring?"). An 's is personal_owner's.

    No ASCII gate, and greetings, list markers, quotes, emoji and zero-width
    characters do not hide a question. A bare first name is not recognised:
    reading the know-you profile for every request would break the host's
    rule that routing loads no saved context in test mode or after an opt-out."""
    cased = _KIN_PUBLIC_CASED.sub(lambda m: " " * len(m.group()), _readable(prompt))
    loose = cased.lower()
    if (_KIN_MINE.search(loose) or _KIN_CLAUSE.search(loose) or _KIN_FROM.search(loose.strip())
            or _KIN_CHECK.search(loose) or _KIN_NOUN.search(loose)):
        return True
    if any(m.group("next").strip("'") in _KIN_FOLLOW for m in _KIN_MID.finditer(loose)):
        return True
    whole_is_question = "?" in loose
    for clause, asked in _clauses(cased):
        low = clause.lower()
        if _KIN_OBJECT.match(low) or _KIN_BARE.match(low):
            return True
        if (asked or whole_is_question or _TIME_WORD.search(low)) and _KIN_BARE_VERB.match(low):
            return True
        if (asked or whole_is_question) and _KIN_WITH.search(low) and re.match(_KIN_LEAD + r"\b", low):
            return True
        soft = _SOFT_SUBJECT.match(low)
        if soft and soft.group("next").strip("'") in _KIN_FOLLOW:
            return True
        m = _KIN_SUBJECT.match(clause)
        if m:
            following = re.match(r"\s+([A-Z][\w'-]*)", clause[m.end():])
            # "Is Sister Rosetta ..." is a name; "Is Mom OK" and "Is Dad Home" are not.
            if not (following and following.group(1).lower().strip("'") not in _KIN_FOLLOW):
                return True
    return False


_LEADING_RECIPIENT = re.compile(
    r"^\s*(?:(?:please|hey|ok|okay|wisp|can\s+you|could\s+you|would\s+you)[, ]+)*"
    r"(?:e-?mail|text|message|msg|tell|send|forward|notify|ping|call|remind|let)\s+"
    r"(?:to\s+)?(?:my\s+|our\s+)?(?!(?:if|whether|when|where|what|why|how|that|me|us)\b)[A-Za-z]+\b", re.I)


def without_leading_recipient(text: str) -> str:
    """`text` without the person an instruction at its start is addressed to
    ("text mom the bitcoin price", "send my dad the news"): the recipient of
    a delivery is not whom the request is about. Only at the start, so "Did
    the hospital call Mom today?" keeps its Mom."""
    return _LEADING_RECIPIENT.sub(" ", text or "", count=1)


def personal_owner(prompt: str) -> bool:
    """Some 's in the request belongs to a person in the user's life: a
    relation ("the doctor's office"), a kin word, a known person, or a contact
    detail. No ASCII or length gate; a failed people lookup counts as personal."""
    loose = _KIN_PUBLIC.sub(" ", _readable(prompt).lower())
    for owner in _LOOSE_POSSESSIVE.findall(loose):
        owner = owner.strip(".-&")
        if not owner or owner in _CONTRACTED:
            continue
        if (owner in _PERSON_OWNERS or owner in _KIN or _possessive_owner_is_contact_detail(owner)
                or _possessive_owner_is_known_person(owner)):
            return True
    return False


def lookalike_possessive(prompt: str) -> bool:
    """An 's written with an apostrophe lookalike: a backtick, or any
    non-ASCII character that is not an ordinary letter or a real apostrophe,
    between an ASCII letter and "s" (U+02B9, a prime, the saltillo U+A78C,
    U+02BD, U+02C8, U+2035). "caf\u00e9s" is a word, not a possessive."""
    import unicodedata
    text = (prompt or "")[:_OWNER_TEXT_CAP * 2]
    if _LOOKALIKE_BACKTICK.search(text):
        return True
    for mark in _LOOKALIKE_SITE.findall(text):
        if mark in "\u2019\u2018\uff07":            # real apostrophes: read as one everywhere
            continue
        if mark in "\ua78c\ua78b" or unicodedata.category(mark) in {"Lm", "Sk", "Po", "Pf", "Pi", "Mn", "Mc", "Cf"}:
            return True
    return False


# What an owner has that is the user's own business, whoever the owner is.
_PERSONAL_HEADS = frozenset("""
address addresses number numbers birthday anniversary flight flights package packages
parcel appointment appointments schedule plans place house home apartment room car kids wedding party
location whereabouts salary password diagnosis results result health surgery visit trip itinerary
reservation booking ticket tickets order orders delivery account accounts email emails mail text texts
message messages call calls note notes file files folder document documents fever recovery interview
test tests exam treatment therapy medication pregnancy baby funeral divorce date dates
""".split())
# What an owner has that is public whoever the owner is. An 's of unknown
# ownership reopens a search only with one of these: "Is Ben's fever down
# today?" names none, and stays closed.
_PUBLIC_HEADS = frozenset("""
stock stocks share shares price prices market cap valuation earnings revenue sales profit dividend
ipo game games match matches score scores standings record season playoffs final finals race
album albums song songs single tour concert concerts show shows movie movies film films episode
episodes series trailer premiere menu hours release launch event events keynote lineup roster
ratio ratios ceo headquarters founder showing airing streaming playing
phone phones laptop laptops tablet console version update model models product products
burger burgers flavor flavors flavour flavours sandwich breakfast brunch drive-thru
""".split())
# A holiday written with an 's: its date is public.
_HOLIDAY = re.compile(r"\b(?:mother|father|valentine|st\.?\s+patrick|new\s+year|president|veteran|"
                      r"grandparent|sibling|women|men|children|april\s+fool)s?'?s?\s+(?:day|eve)\b")
_FRESH_WORDS = frozenset("new newest latest current upcoming next".split())
_OWNER_REQUEST_MAX_WORDS = 16


_HOLIDAY_CASED = re.compile(_HOLIDAY.pattern, re.I)


def holiday_question(prompt: str) -> bool:
    """A question about a public holiday written with an 's ("When is Father's
    Day?", "Mother's Day brunch near me today", "Valentine's Day gift ideas"):
    its date and customs are public, with or without a time word.

    The holiday excuses only itself. With its name taken out, the rest must
    hold nobody and nothing of anyone's: no other possessive ('s or s'), no
    family word of either set, no he/she/him/her/his/hers, no "with <Name>",
    no street address (a number before a capitalised word), no my or our, no
    long number that is not a year. "Grandma fell on Mother's Day, is she
    ok?" and "Dana's Mother's Day plans today" are not holiday questions."""
    text = _ascii_text(prompt).strip()
    if not (text.isascii() and len(text.split()) <= _OWNER_REQUEST_MAX_WORDS
            and _HOLIDAY.search(text.lower())) or lookalike_possessive(prompt):
        return False
    rest_cased = _HOLIDAY_CASED.sub(" ", text)
    rest = rest_cased.lower()
    words = set(re.findall(r"[a-z0-9&-]+", rest))
    if "'" in rest or words & (_KIN | _SOFT_KIN | _PERSON_OWNERS):
        return False
    if words & {"my", "our", "mine", "ours", "he", "she", "him", "her", "his", "hers"}:
        return False
    if re.search(r"\bwith\s+[A-Z][a-z]+", rest_cased) or re.search(r"\b\d+[a-z]?\s+[A-Z][a-z]+", rest_cased):
        return False
    if personal_reference(rest_cased) or personal_owner(rest_cased):
        return False
    return not any(re.fullmatch(r"(?:19|20)\d{2}", run) is None for run in re.findall(r"\d{4,}", text))


def public_owner_request(prompt: str) -> bool:
    """A request with an 's of unknown ownership that is still plainly public:
    "Who won Sunday's game?", "What's Apple's stock price today?", "What is
    Tesla's market cap right now?", "What's Google's latest phone this year?".
    Plain ASCII, short, no first person, nobody of the user's own, a public
    noun (_PUBLIC_HEADS) or a "new"/"latest" thing, and nothing that is
    anyone's private business (an address, a flight, a fever). It reopens only
    a search of the user's own words (see web_request._parse_request)."""
    text = _ascii_text(prompt).strip()
    if not (text.isascii() and len(text.split()) <= _OWNER_REQUEST_MAX_WORDS
            and not _FIRST_PERSON.search(text)):
        return False
    if personal_reference(prompt) or personal_owner(prompt) or lookalike_possessive(prompt):
        return False
    words = set(re.findall(r"[a-z0-9&-]+", _KIN_PUBLIC.sub(" ", text.lower())))
    if words & _PERSONAL_HEADS or words & _WEB_FIRST_OWN or words & (_PERSON_OWNERS | _KIN):
        return False
    # A public noun, not merely "new" or "latest": "Is Ben's new puppy okay today?" has none.
    if not (words & _PUBLIC_HEADS or _HOLIDAY.search(text.lower())):
        return False
    # Every long number must itself be a year: one year does not excuse a phone number.
    if any(not re.fullmatch(r"(?:19|20)\d{2}", run) for run in re.findall(r"\d{4,}", text)):
        return False
    if _PHONE_GROUP.search(text) or re.search(r"\d{3}[\s.)]+\d{3}[\s.-]+\d{4}\b", text):
        return False                            # "415-555-2019" ends in a year-like group
    return bool(re.search(r"[a-z0-9]'(?:s\b|(?=\s|$|[?.!,]))", text.lower()))


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
hours open opens opening close closes closing closed time times store holiday menu return policy
monday tuesday wednesday thursday friday saturday sunday
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
