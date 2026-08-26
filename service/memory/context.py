"""Turn a stored session into the token-budgeted message list the model sees.

The working set is bounded two ways: keep at most the last KEEP_MESSAGES turns
verbatim, and never exceed the history budget derived from the model's own
context window (see history_budget). Anything older is folded into a
rolling `summary` by `maybe_summarize`, which reuses the already-resident model
so it never triggers a costly model swap. Tool outputs never re-enter context in
full — only a short digest rides along on the assistant turn.
"""
from __future__ import annotations

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

    Layout: [summary block?] + last turns within the token budget.
    `max_tokens` overrides the default budget — see main.py's Super Model
    override, which passes a larger one so a session with a lot of back-and-
    forth doesn't get trimmed as eagerly for the model chosen for the hardest
    requests.
    """
    sess = store.get_session(sid)
    if not sess:
        return []

    if max_tokens is None:
        max_tokens = default_history_budget()

    summary = (sess["summary"] or "").strip()
    summarized_idx = sess["summarized_idx"]
    window = store.turns_from(sid, summarized_idx)
    # Only the last KEEP_MESSAGES are eligible to be shown verbatim.
    window = window[-KEEP_MESSAGES:]

    budget = max_tokens
    if summary:
        budget -= estimate_tokens(summary)

    # Walk newest -> oldest, keeping turns until the budget runs out.
    kept: list[dict] = []
    for turn in reversed(window):
        msg = _render(turn)
        cost = estimate_tokens(msg["content"])
        if cost > budget and kept:
            break
        budget -= cost
        kept.append(msg)
    kept.reverse()

    messages: list[dict] = []
    if summary:
        messages.append({
            "role": "system",
            "content": f"Summary of the earlier conversation:\n{summary}",
        })
    messages.extend(kept)
    return messages


async def maybe_summarize(client: OMLXClient, sid: str, model: str) -> None:
    """Fold turns that fell out of the live window into the rolling summary.

    Reuses `model` (already resident from the just-finished turn) so it costs one
    short generation, not a model swap. Best-effort: never raises.
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
