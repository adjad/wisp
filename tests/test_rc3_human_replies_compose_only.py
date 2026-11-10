"""Settings › Human-sounding replies shapes only messages Wisp writes for the
user to send (owner decision 2026-11-10, review finding a04-settings#1).

Compose paths: the agent loop on a turn that offers a send/draft tool (the
model writes the body argument), the email reply draft call and the proactive
reply-card draft call. Agent paths that must stay untouched: an Ask turn with
no send/draft tool and the tool-free plain chat reply.

All model replies are synthetic. No model is loaded. WISP_HOME is isolated.
"""
from __future__ import annotations

import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-human-compose-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from service import skills  # noqa: E402
from service.agent import loop  # noqa: E402
from service.memory import identity  # noqa: E402
from service.safety import policy  # noqa: E402

STYLE = "--- humanizer: style for messages"


class ScriptedClient:
    def __init__(self):
        self.requests = []

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        yield {"kind": "final", "message": {"role": "assistant", "content": "Done."}}


class Approver:
    async def confirm(self, action):
        return True


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original = skills.all_skills()
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "user-skills")
    skills.load()
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr(loop.prompt_blocks, "memory_block", lambda **kwargs: "")
    monkeypatch.setattr(loop, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    try:
        yield
    finally:
        skills._skills = original


def _switch(on: bool) -> None:
    assert skills.set_enabled("humanizer", on) is True
    assert skills.get("humanizer").enabled is on


def _agent_system(tools: list[str], prompt: str) -> str:
    client = ScriptedClient()

    async def emit(event):
        pass
    asyncio.run(loop.run_agent(
        client, "Ling-3.0-tiny-oQ4e", [{"role": "user", "content": prompt}],
        emit, Approver(), debug=False, max_steps=1, tools=tools))
    return "\n".join(m["content"] for m in client.requests[0]["messages"]
                     if m.get("role") == "system")


def _email_draft_system() -> str:
    from service.assistant.email_reply_api import draft_prompt
    system, _ = draft_prompt({"sender": "Sam <sam@example.com>", "to": "me@example.com",
                              "subject": "Lunch", "body": "Free Thursday?", "ts": 0})
    return system


def _card_draft_system() -> str:
    from service.proactive import prepare
    seen: list[str] = []

    async def reply_fn(system, material, abort, tokens):
        seen.append(system)
        return None   # stand down: only the prompt is under test
    item = {"item": {"text": "Free Thursday?", "source": "mail"}, "who": "Sam", "title": "Lunch"}
    assert asyncio.run(prepare.draft(item, "reply", reply_fn, None, allowed=set())) is None
    return seen[0]


def _plain_chat_block() -> str:
    from service.main import _plain_chat_skill_blocks
    return _plain_chat_skill_blocks("hi there", "", role="general", literal_envelope=False)


def test_on_compose_paths_get_the_style() -> None:
    _switch(True)
    assert STYLE in _agent_system(["send_email"], "Email Sam asking about lunch")
    assert STYLE in _agent_system(["send_message", "get_upcoming"], "Text Sam I'm late")
    assert STYLE in _email_draft_system()
    assert STYLE in _card_draft_system()
    # The body is the shipped humanizer body, framed as compose-only.
    assert skills.get("humanizer").body in skills.compose_style_block()
    assert "not apply to your own answer" in skills.compose_style_block()


def test_on_agent_answers_are_untouched() -> None:
    _switch(True)
    assert STYLE not in _agent_system(["get_upcoming"], "What is on today?")
    assert "humanizer" not in _agent_system(["get_upcoming"], "What is on today?")
    assert "humanizer" not in _plain_chat_block()
    assert "humanizer" not in skills.always_skills_block()


def test_off_nothing_gets_the_style() -> None:
    _switch(False)
    assert skills.compose_style_block() == ""
    assert STYLE not in _agent_system(["send_email"], "Email Sam asking about lunch")
    assert STYLE not in _agent_system(["get_upcoming"], "What is on today?")
    assert STYLE not in _email_draft_system()
    assert STYLE not in _card_draft_system()
    assert "humanizer" not in _plain_chat_block()


def test_compose_scope_follows_the_name_not_the_frontmatter(tmp_path: Path) -> None:
    """An edited or older copy of the humanizer still counts as compose-only."""
    copy_skill = skills.Skill("humanizer", "old description", "OLD BODY",
                              tmp_path / "humanizer", always=True)
    skills._skills["humanizer"] = copy_skill
    assert copy_skill.compose is True
    assert "OLD BODY" in skills.compose_style_block()
    assert "OLD BODY" not in skills.always_skills_block()
