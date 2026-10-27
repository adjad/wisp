"""A general-knowledge question must not be refused because web_search was not offered.

Reported live 2026-10-26 ("what makes the sky blue"): the route retrieved 16
tools, web_search was not among them, the model called it anyway, and the loop
answered "web_search is not available for this request — tell the user this
action isn't available". The model then applied the live-data rule ("say you
couldn't retrieve it, don't answer from memory") to a question about optics and
declined to answer.

Two fixes are pinned here: the unoffered-web-lookup result now tells the model to
answer stable knowledge from its own knowledge (every other unoffered tool keeps
the old wording), and the system prompt's live-data rule says it is for LIVE data
only.

    .venv/bin/python -m pytest tests/test_general_knowledge_unoffered_web.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402

PROMPT = "what makes the sky blue"
ANSWER = ("Sunlight is scattered by air molecules, and shorter blue wavelengths "
          "scatter far more than red ones (Rayleigh scattering).")


class ScriptedClient:
    """Answers with a queue of canned messages, recording what the model saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    async def ensure_only(self, model, *, exclusive=False, emit=None, **kw):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        self.seen.append(list(messages))
        msg = self.replies.pop(0) if self.replies else {
            "role": "assistant", "content": ANSWER, "tool_calls": None}
        yield {"kind": "final", "message": msg}


class Approver:
    async def confirm(self, action):
        return True


def _call(name, args="{}", cid="c1"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": args}}]}


def _run(replies, prompt=PROMPT, tools=("read_file", "list_dir")):
    results: list[str] = []

    async def emit(ev):
        if ev.get("type") == "tool_result":
            results.append(ev["result"])

    c = ScriptedClient(replies)
    text = asyncio.run(loop.run_agent(
        c, "Agents-A1-4B-oQe6", [{"role": "user", "content": prompt}],
        emit, Approver(), max_steps=3, tools=list(tools)))
    return c, results, text


def test_unoffered_web_search_tells_the_model_to_answer_from_knowledge():
    c, results, text = _run([
        _call("web_search", '{"query": "why is the sky blue"}'),
        {"role": "assistant", "content": ANSWER, "tool_calls": None},
    ])
    assert results, "the unoffered call should produce a tool result"
    r = results[0]
    assert "web_search is not available for this request" in r
    assert "answer it now from your own knowledge" in r
    assert "Do NOT say you searched" in r
    # The old message told the model to report the action as unavailable.
    assert "Tell the user this specific action isn't available" not in r
    # The same text is what went back into the conversation.
    tool_msgs = [m for m in c.seen[-1] if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[-1]["content"] == r


def test_the_turn_still_ends_with_the_knowledge_answer():
    # The only hard failure is the unoffered web tool; the loop must hand the
    # model another step and return its answer, with no refusal injected.
    c, results, text = _run([
        _call("web_search", '{"query": "why is the sky blue"}'),
        {"role": "assistant", "content": ANSWER, "tool_calls": None},
    ])
    assert len(c.seen) == 2
    assert "Rayleigh" in text
    assert "couldn't retrieve" not in text.lower()
    assert "not available" not in text.lower()


def test_every_web_lookup_tool_gets_the_knowledge_message():
    for name in ("web_search", "web_fetch", "http_request", "wikipedia_summary"):
        _, results, _ = _run([
            _call(name, '{"url": "https://example.com", "topic": "sky"}'),
            {"role": "assistant", "content": ANSWER, "tool_calls": None},
        ])
        assert results and "from your own knowledge" in results[0], name


def test_a_non_web_unoffered_tool_keeps_the_old_message():
    _, results, _ = _run([
        _call("delete_path", '{"path": "~/Downloads"}'),
        {"role": "assistant", "content": "ok", "tool_calls": None},
    ])
    assert results
    assert "delete_path is not available for this request" in results[0]
    assert "Tell the user this specific action isn't available right now" in results[0]
    assert "own knowledge" not in results[0]


def test_system_prompt_scopes_the_live_data_rule_to_live_data():
    s = loop.SYSTEM
    assert "That rule is for LIVE data only" in s
    assert "what makes the sky blue" in s
    assert "even when web_search is unavailable or fails" in s
    # The refuse-instead-of-guess rule is still there for live figures.
    assert "do NOT fall back to a number from memory" in s


def test_default_voice_is_gentle_and_last():
    s = loop.SYSTEM
    voice = s.index("- YOUR VOICE: be gentle, calm and kind")
    assert "energetic" not in s[voice:] and "upbeat" not in s[voice:].lower()
    assert "emoji sparingly" in s[voice:].lower()
    assert "Gentleness never replaces accuracy" in s[voice:]
    # Answers stay organized: the direct answer first, no wall of text.
    assert "organized and easy to scan" in s[voice:] and "wall of text" in s[voice:]
    # Nothing but the voice rules follows the voice header.
    assert s[voice:].count("\n- ") == 4
