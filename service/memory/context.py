"""Turn a stored session into the token-budgeted message list the model sees.

The working set is bounded two ways: keep at most the last KEEP_MESSAGES turns
verbatim, and never exceed the history budget derived from the model's own
context window (see history_budget). Turns that miss the verbatim cut but are
not yet in the summary ride along as one-line notes in an "earlier in this
conversation" digest (see condense_history). Anything older is folded into a
rolling `summary` by `maybe_summarize`, which reuses the already-resident model
so it never triggers a costly model swap. Tool outputs never re-enter context in
full — only a short digest rides along on the assistant turn.
"""
from __future__ import annotations

import re

from service.config import no_thinking_kwargs
from service.inference.omlx_client import OMLXClient
from service.memory.store import store

KEEP_MESSAGES = 40           # ~20 exchanges kept verbatim
# Turns that must age out before a fold is worth a generation — see
# maybe_summarize.
_FOLD_BATCH = 8

# What Wisp adds on top of the conversation history, per request. The history
# budget is the model's window MINUS all of this, which is the whole reason
# history_budget() exists instead of a flat constant.
#
# The flat constant was 16,000 — chosen when the rostered model reported a
# 131,072-token window, where it was genuinely conservative. It then went stale
# invisibly: with the window lowered to 16,000, a "16,000-token history budget"
# meant Wisp could hand the model its ENTIRE window in history alone and then
# add ~3,700 tokens of system prompt and up to ~9,500 of tool schemas on top —
# ~29,000 tokens into a 16,000 window. Nothing in the code noticed, because
# nothing related the budget to the window.
_SYSTEM_PROMPT_TOKENS = 4000    # agent/loop.py SYSTEM + date/identity/profile/memory blocks
_TOOL_SCHEMA_TOKENS = 2500      # a scoped subset; the unscoped 52-tool route is ~9,500 (see below)
_TOOL_RESULT_TOKENS = 3000      # one capped result (loop._MAX_TOOL_RESULT_CHARS)
_OUTPUT_TOKENS = 2000           # reserved for the answer; oMLX admits on prompt + max_tokens
_OVERHEAD_TOKENS = (_SYSTEM_PROMPT_TOKENS + _TOOL_SCHEMA_TOKENS
                    + _TOOL_RESULT_TOKENS + _OUTPUT_TOKENS)

# Never drop below this much history, even if the overhead estimate says to —
# a couple of exchanges is the difference between a conversation and a
# stateless prompt, and a window too small for that needs a bigger window, not
# a silently amnesiac assistant.
_MIN_HISTORY_TOKENS = 1500


def history_budget(model: str) -> int:
    """Tokens of conversation history to send, given `model`'s real window.

    NOTE the unscoped route (every tool offered, ~9,500 tokens of schemas)
    exceeds _TOOL_SCHEMA_TOKENS by ~7,000. That route is the exception — rule
    routes scope the toolset (see router._domain_subset) — and it eats into the
    output reservation rather than overflowing the window outright. If it starts
    truncating, scope more routes rather than raising this.
    """
    from service.config import model_context_window

    return max(_MIN_HISTORY_TOKENS, model_context_window(model) - _OVERHEAD_TOKENS)


# Back-compat for callers that want a number without naming a model. Uses the
# agent model, which is what every text role resolves to.
def default_history_budget() -> int:
    from service.config import role_to_model

    return history_budget(role_to_model("agent"))


def estimate_tokens(text: str) -> int:
    """Cheap, tokenizer-free estimate. Good enough for budgeting."""
    return max(1, len(text) // 4)


# THE "EARLIER IN THIS CONVERSATION" DIGEST.
#
# A stored turn used to be either verbatim or gone. build_messages kept the
# newest turns that fit the history budget, the Ask step fit (agent/loop.py
# _fit_window) then cut that to its own smaller budget, and the rolling summary
# only starts once 48 turns are stored. Everything in between was in neither:
# on Gemma 4 E4B a first Ask step kept ~5 exchanges of ~1,150-character replies,
# so "what was my locker code?" failed nine exchanges later.
#
# A turn that misses the verbatim cut now leaves one line here instead: the
# user's words (where most stated facts live) and an assistant reply's lead
# sentence plus the sentences that carry numbers or names (where looked-up
# facts live). Extractive and deterministic, so it costs no model call on the
# hot path and the same turns always condense to the same lines, which keeps
# the prompt-cache prefix stable from one turn to the next.
DIGEST_HEADER = ("Earlier in this conversation (condensed notes of older turns, oldest "
                 "first; reference only, not instructions):")
_DIGEST_USER_CHARS = 160
_DIGEST_ASSISTANT_CHARS = 200
# ~1,200 estimated tokens: about 15 condensed exchanges. Past this the oldest
# lines go, and the rolling summary is what remembers them.
DIGEST_MAX_CHARS = 4800

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_MARKUP = re.compile(r"[*_`#>|]+|^\s*[-•]\s+", re.M)
_SALIENT = re.compile(r"\d|(?<=[a-z,;:] )[A-Z][a-z]")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _flat(text: str) -> str:
    return " ".join(_MARKUP.sub(" ", text).split())


def digest_line(role: str, content) -> str:
    """One stored turn as a digest line. "" for a turn with nothing to keep."""
    if not isinstance(content, str):
        return ""
    text = _flat(content)
    if not text:
        return ""
    if role == "user":
        return "- User: " + _clip(text, _DIGEST_USER_CHARS)
    sentences = _SENTENCE.split(text)
    picked = [0] + [i for i, s in enumerate(sentences[1:], 1) if _SALIENT.search(s)]
    kept: list[str] = []
    used = 0
    for i in picked:
        if kept and used + len(sentences[i]) > _DIGEST_ASSISTANT_CHARS:
            continue
        kept.append(sentences[i])
        used += len(sentences[i]) + 1
    return "- Assistant: " + _clip(" ".join(kept), _DIGEST_ASSISTANT_CHARS)


def digest_content(lines: list[str]) -> str:
    return DIGEST_HEADER + "\n" + "\n".join(lines)


def is_digest(message: dict) -> bool:
    return (message.get("role") == "system" and isinstance(message.get("content"), str)
            and message["content"].startswith(DIGEST_HEADER))


def digest_lines(message: dict) -> list[str]:
    return message["content"].split("\n")[1:]


def cap_digest(lines: list[str]) -> list[str]:
    """Drop the oldest lines until the digest fits DIGEST_MAX_CHARS."""
    total = sum(len(line) + 1 for line in lines)
    start = 0
    while start < len(lines) and total > DIGEST_MAX_CHARS:
        total -= len(lines[start]) + 1
        start += 1
    return lines[start:]


def _render(turn: dict) -> dict:
    """A stored turn -> a chat message.

    The turn's `tool_digest` is DELIBERATELY NOT rendered. It used to be
    appended to every past assistant turn as "\n[actions: get_stock_price]",
    which taught the model that writing that bracket IS how you invoke a tool —
    so instead of emitting a real tool call it wrote the marker as prose and the
    turn did nothing.

    MEASURED (2026-08-18, exact failing request replayed 25x per variant at
    production sampling, "summarize the movements of vicor corp over the last
    month" with 17 tools offered):

        history form                     real tool_calls   faked "[actions:"
        [actions: X]  (what shipped)          9/25              14/25
        dropped entirely (this)              19/25               0/25
        "(Tools you already ran: X.)"         8/25               0/25
        "In that turn you used the X tool."  13/25               0/25

    Dropping it more than DOUBLES real tool calls. The two prose rewrites kill
    the imitation but score no better than the bug they replace — telling the
    model it already ran a tool reads as "the data is in hand", so it answers
    from memory in text instead. There is no phrasing of "here is what you did"
    that beats saying nothing.

    Nothing else regresses: the digest is still stored on the turn, and the
    router's own use of it (`last_tools` -> confirms_offered_action, for
    follow-ups like "yes go ahead") reads the column straight out of the store
    via store.last_assistant_tools — never this rendering.
    """
    return {"role": turn["role"], "content": turn["content"] or ""}


def build_messages(sid: str, max_tokens: int | None = None) -> list[dict]:
    """History messages for `sid` (no system prompt, no new user turn).

    Layout: [summary block?] + [digest block?] + last turns within the token
    budget. Turns the budget (or KEEP_MESSAGES) leaves out, and the summary does
    not yet cover, are condensed into the digest rather than dropped; the
    digest's own cost counts against the same budget. `max_tokens` overrides the
    default budget when a caller needs a different one; None uses
    default_history_budget().
    """
    sess = store.get_session(sid)
    if not sess:
        return []

    if max_tokens is None:
        max_tokens = default_history_budget()

    summary = (sess["summary"] or "").strip()
    summarized_idx = sess["summarized_idx"]
    turns = store.turns_from(sid, summarized_idx)
    # Only the last KEEP_MESSAGES are eligible to be shown verbatim; older
    # unsummarized turns go straight to the digest.
    window = turns[-KEEP_MESSAGES:]
    lines = [line for t in turns[:len(turns) - len(window)]
             if (line := digest_line(t["role"], t["content"]))]

    budget = max_tokens
    if summary:
        budget -= estimate_tokens(summary)

    # Walk newest -> oldest, keeping turns while they and the digest of
    # everything older still fit. A turn that does not fit is condensed, and so
    # is everything older than it (a gap in the middle would misorder the chat).
    window_lines = [digest_line(t["role"], t["content"]) for t in window]
    kept: list[dict] = []
    spent = 0
    rest = len(window)
    for turn in reversed(window):
        msg = _render(turn)
        cost = estimate_tokens(msg["content"])
        older = lines + [line for line in window_lines[:rest - 1] if line]
        digest_cost = estimate_tokens(digest_content(cap_digest(older))) if older else 0
        if kept and spent + cost + digest_cost > budget:
            break
        spent += cost
        kept.append(msg)
        rest -= 1
    kept.reverse()
    lines += [line for line in window_lines[:rest] if line]
    lines = cap_digest(lines)

    messages: list[dict] = []
    if summary:
        messages.append({
            "role": "system",
            "content": f"Summary of the earlier conversation:\n{summary}",
        })
    if lines:
        messages.append({"role": "system", "content": digest_content(lines)})
    messages.extend(kept)
    return messages


async def maybe_summarize(client: OMLXClient, sid: str, model: str, prepare=None) -> None:
    """Fold turns that fell out of the live window into the rolling summary.

    Calls an optional async ``prepare`` hook only when a summary is actually
    needed. Best-effort: never raises.
    """
    sess = store.get_session(sid)
    if not sess:
        return
    total = store.turn_count(sid)
    summarized_idx = sess["summarized_idx"]
    overflow = total - KEEP_MESSAGES - summarized_idx
    # Fold in BATCHES, not every single turn past the window. At `> 0` this ran
    # one generation on every turn of a long session, each folding the two turns
    # that had just aged out — and it is awaited before the SSE stream closes
    # (see main.py), so it is user-visible tail latency, not background work.
    # KEEP_MESSAGES=40 leaves ample slack to let a few turns queue up first.
    if overflow < _FOLD_BATCH:
        return

    to_fold = store.turns_range(sid, summarized_idx, summarized_idx + overflow)
    transcript = "\n".join(f"{t['role']}: {t['content']}" for t in to_fold)
    prior = (sess["summary"] or "").strip()
    prompt = (
        "Update the running summary of this conversation so it captures the key "
        "facts, decisions, and open threads. Keep it under 6 short lines.\n\n"
        f"Current summary:\n{prior or '(none yet)'}\n\n"
        f"New turns to fold in:\n{transcript}\n\nUpdated summary:"
    )
    try:
        if prepare is not None:
            await prepare()
        # no_thinking_kwargs is the whole reason this call can succeed at all.
        # This is the same shape as the email/message summarizers — the source
        # text is already in the prompt and nothing is being figured out — and
        # every other summary-shaped call in the repo passes it. This one did
        # not, while the rostered model is a thinker: 240 tokens cannot hold a
        # think block AND a summary, so the response came back truncated,
        # _demote_unclosed_think blanked `content`, `new_summary` was empty,
        # `set_summary` never fired, and `summarized_idx` never advanced. The
        # overflow therefore grew by two every turn and the SAME doomed call
        # re-ran forever on a strictly larger transcript. The rolling summary
        # simply never existed, so long sessions silently lost their early
        # context — a quality bug wearing a latency bug's clothes.
        resp = await client.chat(
            model,
            [{"role": "system", "content": "You compress conversations into terse notes."},
             {"role": "user", "content": prompt}],
            max_tokens=400,
            **no_thinking_kwargs(model),
        )
        new_summary = (resp["choices"][0]["message"].get("content") or "").strip()
        if new_summary:
            store.set_summary(sid, new_summary, summarized_idx + overflow)
    except Exception:  # noqa: BLE001 — memory upkeep must never break a chat
        pass
