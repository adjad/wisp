"""Test switch that sends every /agent turn to the model (see model_only_enabled),
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
from datetime import date, timedelta


def model_only_enabled() -> bool:
    """TEST SWITCH: every /agent turn goes to the model, nothing answers first.

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
})

DAY_PLAN_TOOL = "get_my_day_plan"
_DAY_PLAN = re.compile(
    r"\bwhat\s+(?:should|do|must|can)\s+i\s+(?:do|focus on|work on|tackle|get done|prioriti[sz]e)\b"
    r"|\bwhat(?:['’]?s| is)\s+on\s+my\s+(?:plate|list|agenda|to-?do)"
    r"|\bwhat\s+do\s+i\s+(?:need|have)\s+to\s+(?:do|get done)\b"
    r"|\bplan\s+(?:out\s+)?my\s+day\b|\bwhat\s+am\s+i\s+(?:forgetting|missing)\b"
    r"|\banything\s+i\s+should\s+(?:know|do|handle)\b|\bwhat(?:['’]?s| does| is)\s+my\s+day\b"
    r"|\bmy\s+(?:to-?do|todo)s?(?:\s+list)?\b|\bwhat(?:['’]?s| is)\s+next\s+for\s+me\b"
    r"|\bhow\s+(?:does|is)\s+my\s+day\b|\bwhere\s+should\s+i\s+start\b",
    re.I)


def day_plan_question(prompt: str) -> bool:
    """"What should I do today?" and its paraphrases: one get_my_day_plan read
    answers it better than four separate source reads."""
    return bool(_DAY_PLAN.search(prompt or ""))


ASK_DAY_PLAN_HINT = (
    "DAY PLAN: call get_my_day_plan once and answer from it; do not read the "
    "calendar, mail, messages and notes one by one unless it says the plan is "
    "unavailable. Answer as a short prioritized list: one lead line, then 3-6 "
    "bullets — time-bound items first (with times), then people waiting on a "
    "reply, then tasks. No digests, no data-freshness caveats, no source labels."
)

# Appended to a model-led turn's runtime prompt (the light-read style hint the
# legacy router sets is never set on a model-led turn).
ASK_STYLE_HINT = (
    "ANSWER STYLE for this reply:\n"
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
    "say so in one short sentence; never estimate or make up a number."
)

# Appended for a capability question ("Can you send texts?"): the corpus and
# sweep had the model deny real capabilities or ask "which email?".
ASK_CAPABILITY_HINT = (
    "This is a question about what you can do. Answer it directly from the "
    "Capabilities list above, without calling a tool: you can read and summarize "
    "the user's email, texts, calendar, reminders and notes; draft and send emails "
    "and texts (every send is shown to the user to confirm first); add reminders "
    "and calendar events; search the web, weather and stocks; and control Mac "
    "settings and apps. A Google account is linked in Wisp's Settings, under Google."
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


_COMPOSE = re.compile(r"^\s*(?:please\s+)?(?:can you\s+|could you\s+)?(?:write|compose|draft|come up with)\b", re.I)
_DESTINATION = re.compile(
    r"\b(?:e-?mail|text|message|imessage|send|reply|respond|(?:in|to|into)\s+(?:my\s+)?notes?\b|"
    r"save|file|document|doc|calendar|reminder)\b", re.I)


def compose_only(prompt: str) -> bool:
    """A writing request with no destination ("Write a short thank-you note to
    my professor"): the answer IS the text, so no write/draft/send tool runs."""
    text = prompt or ""
    return bool(_COMPOSE.match(text)) and not _DESTINATION.search(text)


COMPOSE_ONLY_FORBIDDEN_CATEGORIES = frozenset({
    "notes_write", "fs_write", "email_draft", "messages_draft", "email_send",
    "messages_send", "scheduled_send", "calendar_write", "assistant_write",
})


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


def capability_question(prompt: str) -> bool:
    """A question about what Wisp can do or how to do something, not a request
    for the user's data: "What can you do with my calendar?", "How do I
    connect Google?", "Can you read my email?"."""
    text = (prompt or "").strip().lower()
    if re.match(r"^(?:so\s+)?(?:what|which)\s+(?:\w+\s+){0,2}(?:can|could|do)\s+you\s+(?:do|help|handle|access|see|read|check)\b", text):
        return True
    if re.match(r"^(?:what|which)\s+(?:can|could)\s+you\b", text):
        return True
    if re.match(r"^(?:how\s+(?:do|can|should|would)\s+i|how\s+to)\b", text):
        return True
    if re.search(r"\bwhat\s+are\s+you\s+(?:able|good|capable)\b|\bwhat\s+do\s+you\s+(?:do|know how)\b"
                 r"|\b(?:do|does)\s+(?:you|wisp)\s+have\s+access\b|\bare\s+you\s+able\s+to\b", text):
        return True
    words = re.findall(r"[a-z']+", text)
    return bool(re.match(r"^(?:can|could|will|do|are)\s+you\b", text) and len(words) <= 6
                and not _TIME_WORDS.search(text))


_PERSONAL_NOUNS = (
    r"calendar|schedule|agenda|events?|meetings?|appointments?|plans?|plate|day|week|weekend|"
    r"emails?|e-mails?|inbox|mail|texts?|messages?|imessages?|reminders?|to-?dos?|tasks?|notes?|"
    r"contacts?|brief|backpack|bag|keys?|wallet|password|wifi|order|package|deadlines?|"
    r"classes|class|exams?|homework|assignments?|flight|trip|birthday|name|address|phone|number"
)
_PERSONAL = re.compile(
    rf"\b(?:my|mine)\s+(?:\w+\s+)?(?:{_PERSONAL_NOUNS})\b"
    r"|\b(?:do|did|am|have|was|were|should)\s+i\b"
    r"|\b(?:anyone|anybody|someone)\s+(?:text|email|message|call|write)\w*\s+me\b"
    r"|\b(?:any|new|unread)\s+(?:texts?|emails?|messages?|mail)\b"
    r"|\bwhat(?:'s| is| are)?\s+(?:on\s+)?my\b"
    r"|\b(?:mom|dad|mother|father)\s+(?:text|said|say|send|email)"
    r"|\b(?:reply|replied|respond|responded|text|texted|email|emailed|message|messaged|call|called|"
    r"write|wrote)\s+(?:back\s+)?(?:to\s+)?me\b"
    # Implicit personal deadlines, events and items: "When is the W-9 due?",
    # "what's on tomorrow", "is there a meeting today", "has the package shipped".
    r"|\b(?:due|deadline|expires?|expiring|overdue|rsvp|shipped|delivered|arriv(?:e|ing))\b"
    r"|\bwhat(?:'s| is)\s+on\b"
    r"|\b(?:is|are)\s+there\s+(?:a|an|any)\s+(?:meeting|event|email|message|text|reminder|appointment|class)"
    r"|\b(?:when|where|what time)\s+(?:is|was|are)\s+(?:the|my|our)\s+\w+.*\b(?:today|tomorrow|tonight|"
    r"this|next|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I)
_QUESTION = re.compile(r"\?\s*$|^\s*(?:what|when|where|who|which|how|is|are|am|do|does|did|have|has|"
                       r"was|were|will|should|any)\b", re.I)
_COMPOSE_OR_GENERAL = re.compile(
    r"^\s*(?:please\s+)?(?:write|draft|compose|create|make|explain|translate|rewrite|tell me (?:a|about)|"
    r"give me (?:tips|ideas|a )|how (?:do|can|should|would) i|how to|why)\b", re.I)


def asks_personal_data(prompt: str) -> bool:
    """The request asks for the user's own current data, which only a tool can
    supply ("what's on my calendar", "did Jordan reply", "how heavy is my
    backpack"). Capability, how-to and compose requests are not."""
    text = prompt or ""
    if capability_question(text) or _COMPOSE_OR_GENERAL.match(text):
        return False
    return bool(_PERSONAL.search(text)
                or (_QUESTION.search(text) and re.search(r"\bmy\s+[a-z]", text, re.I)))


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
