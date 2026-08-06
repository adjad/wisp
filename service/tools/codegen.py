"""Coding-specialist delegation tool for the agent loop.

The agent loop runs on gpt-oss (the only trusted tool-caller). When it needs to
write or fix code, it delegates here to the coding specialist (Qwen2.5-Coder-14B)
for higher quality. Because the coder and gpt-oss can't co-reside under the
memory cap, this swaps the coder in; the agent loop swaps gpt-oss back before its
next step (see service/agent/loop.py). Registered on import.
"""
from __future__ import annotations

from service.config import get_super_model_name, is_super_model_active, role_to_model
from service.inference.omlx_client import OMLXClient
from service.tools.registry import register

# A private client so the tool can drive the coder without threading main's
# client through the registry. Same oMLX server, so swaps are globally visible.
_client: OMLXClient | None = None


def _c() -> OMLXClient:
    global _client
    if _client is None:
        _client = OMLXClient()
    return _client


_CODER_SYS = (
    "You are an expert software engineer. Write correct, idiomatic, "
    "production-quality code. Return only the code with brief inline comments — "
    "no prose explanation unless explicitly asked."
)


@register(
    "write_code",
    "Delegate a coding sub-task (write, fix, refactor, or review code) to the "
    "specialist coding model for higher quality than the agent model. Give a "
    "precise task plus any relevant context (existing code, error text). Returns "
    "the code; use other tools (e.g. write_file) to apply it.",
    {"type": "object",
     "properties": {
         "task": {"type": "string", "description": "what to write, fix, or refactor"},
         "context": {"type": "string",
                     "description": "optional existing code, file contents, or error text"},
     },
     "required": ["task"]},
    category="codegen",
)
async def write_code(task: str, context: str = "") -> str:
    # Normally the agent (gpt-oss) delegates to the "coding" model — which is
    # ALSO gpt-oss today, so that swap is a no-op. Under Super Model the agent
    # is the user's chosen model (e.g. Qwen-27B), which ISN'T the coding model,
    # so delegating here would swap gpt-oss in and out on EVERY write_code call
    # — the model-thrash that caused Super Model coding requests to time out.
    # Keep code generation on the super model itself (already resident → the
    # ensure_only below is a no-op), which is also what the user wants: their
    # hand-picked high-quality model doing the actual coding.
    coder = get_super_model_name() if is_super_model_active() else role_to_model("coding")
    c = _c()
    await c.ensure_only(coder)  # no-op when `coder` is already the resident model
    user = task if not context else f"{task}\n\nContext:\n{context}"
    try:
        resp = await c.chat(
            coder,
            [{"role": "system", "content": _CODER_SYS},
             {"role": "user", "content": user}],
            temperature=0.2, max_tokens=20000,
        )
        return (resp["choices"][0]["message"].get("content")
                or "(coder returned no code)").strip()
    except Exception as e:  # noqa: BLE001
        return f"(coding specialist error: {e})"
