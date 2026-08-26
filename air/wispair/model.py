"""Inference client for the Air node.

Plain OpenAI-compatible chat completions. Nothing here is specific to
TurboFieldfare, which is deliberate — see config.DEFAULTS — but the design is
shaped by two of its hard constraints, both of which cost real time when
violated:

**1. Requests must be strictly sequential.** `ServerPromptCache` holds exactly
one entry. Any interleaved request evicts it: a measured 42-token interloper
forced a full 2,000-token re-prefill that took 18.5s to emit 27 tokens. The
module-level `_LOCK` below enforces this process-wide, and the scheduler runs
sources one at a time rather than gathering them.

**2. Prefer one call carrying everything over several small ones.** Every extra
call is another full prefill, and prefill here is SSD-bandwidth-bound rather
than compute-bound. That's why a run asks for the summary *and* the to-dos in a
single response instead of making a second extraction pass.

Also enforced: system messages precede all conversation messages, text-only
content, no `tool_choice: "required"` (rejected outright), and no reliance on
`reasoning_content` (Gemma has no reasoning channel).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("wispair.model")

# Process-wide serialization of every inference call. See §1 above.
_LOCK = asyncio.Lock()


class ModelError(RuntimeError):
    pass


async def complete(cfg: dict[str, Any], system: str, user: str) -> str:
    """One chat completion. Serialized against every other call in this process."""
    url = cfg["model_base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("model_api_key"):
        headers["Authorization"] = f"Bearer {cfg['model_api_key']}"

    body = {
        "model": cfg["model_name"],
        # System first, then conversation — required ordering.
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": cfg["model_max_tokens"],
        # Low but not zero. Greedy decoding on small models degenerates into
        # repetition loops (this bit the Pro badly with Phi-4), while anything
        # high makes the delimited output format unreliable to parse.
        "temperature": 0.3,
        "stream": False,
    }

    async with _LOCK:
        async with httpx.AsyncClient(timeout=cfg["model_timeout_s"]) as client:
            try:
                r = await client.post(url, json=body, headers=headers)
            except httpx.RequestError as e:
                raise ModelError(f"cannot reach model server at {url}: {e}") from e
        if r.status_code != 200:
            raise ModelError(f"model server returned {r.status_code}: {r.text[:400]}")
        try:
            data = r.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (ValueError, KeyError, IndexError) as e:
            raise ModelError(f"unparseable response: {r.text[:400]}") from e


async def health(cfg: dict[str, Any]) -> dict[str, Any]:
    """Is the model server up and answering? Used by /health and preflight.

    Deliberately a real (tiny) completion rather than a model-list check: on a
    weight-streaming runtime, "the process is listening" and "the model can
    actually produce a token" are genuinely different states, and only the
    second one means a scheduled run will succeed.
    """
    try:
        out = await complete(
            cfg,
            "You are a health check. Reply with exactly one word.",
            "Reply with the single word: ready",
        )
        return {"ok": True, "reply": out[:80]}
    except ModelError as e:
        return {"ok": False, "error": str(e)[:300]}
