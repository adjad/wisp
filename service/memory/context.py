"""Turn a stored session into the token-budgeted message list the model sees.

The working set is bounded two ways: keep at most the last KEEP_MESSAGES turns
verbatim, and never exceed MAX_CONTEXT_TOKENS. Anything older is folded into a
rolling `summary` by `maybe_summarize`, which reuses the already-resident model
so it never triggers a costly model swap. Tool outputs never re-enter context in
full — only a short digest rides along on the assistant turn.
"""
from __future__ import annotations

from service.inference.omlx_client import OMLXClient
from service.memory.store import store

# Sized for gpt-oss-20b (now the default for everything — agent/coding/
# reasoning/general all consolidated to it), not the old 14B specialist these
# were originally tuned for. gpt-oss reports a 131K native context window
# (oMLX's max_context_window); these are conservative relative to that, not
# the model's actual ceiling — just enough more conversational memory to stop
# summarizing away recent turns so eagerly.
KEEP_MESSAGES = 40           # ~20 exchanges kept verbatim
MAX_CONTEXT_TOKENS = 16000   # hard cap on the live window


def estimate_tokens(text: str) -> int:
    """Cheap, tokenizer-free estimate. Good enough for budgeting."""
    return max(1, len(text) // 4)


def _render(turn: dict) -> dict:
    """A stored turn -> a chat message, with tool actions noted inline."""
    content = turn["content"] or ""
    if turn["role"] == "assistant" and turn.get("tool_digest"):
        content = f"{content}\n[actions: {turn['tool_digest']}]".strip()
    return {"role": turn["role"], "content": content}


def build_messages(sid: str, max_tokens: int = MAX_CONTEXT_TOKENS) -> list[dict]:
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
    if overflow <= 0:
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
        resp = await client.chat(
            model,
            [{"role": "system", "content": "You compress conversations into terse notes."},
             {"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=240,
        )
        new_summary = (resp["choices"][0]["message"].get("content") or "").strip()
        if new_summary:
            store.set_summary(sid, new_summary, summarized_idx + overflow)
    except Exception:  # noqa: BLE001 — memory upkeep must never break a chat
        pass
