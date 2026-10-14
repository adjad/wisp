"""The stable system prompt describes only the tools the model will see.

Two measured prompt reductions (docs/plans/2026-10-13-system-prompt-optimizations.md):

* rules are rebuilt from the tools that survive the window fit, so a tool
  _fit_window drops from the request does not leave its rule block behind;
* the installed-skills catalog rides along only when `use_skill` is offered —
  a matched skill body still does, on every route.

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
_scratch = tempfile.TemporaryDirectory(prefix="wisp-prompt-scope-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from service import config, skills  # noqa: E402
from service.agent import loop  # noqa: E402
from service.memory import identity  # noqa: E402
from service.safety import policy  # noqa: E402

FIVE = ["get_upcoming", "add_reminder", "summarize_emails", "view_emails", "search_notes"]


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


def run(client, tools, **kwargs):
    async def emit(event):
        pass
    return asyncio.run(loop.run_agent(
        client, "Ling-3.0-tiny-oQ4e", [{"role": "user", "content": "What is on today?"}],
        emit, Approver(), debug=False, max_steps=1, tools=tools, **kwargs))


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "\nIDENTITY")
    monkeypatch.setattr(loop.prompt_blocks, "memory_block", lambda **kwargs: "\nMEMORY")
    monkeypatch.setattr(loop, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)


def _stable_system(client) -> str:
    return client.requests[0]["messages"][0]["content"]


def test_rules_follow_the_tools_that_survive_the_window_fit(monkeypatch):
    monkeypatch.setattr(skills, "skills_context_block", lambda *args: "")
    full = loop.build_system(set(FIVE))
    first_four = loop.build_system(set(FIVE[:4]))
    assert full != first_four  # search_notes carries a rule block of its own

    # Wide window: nothing is dropped, so the rules cover all five tools.
    monkeypatch.setattr(config, "model_context_window", lambda model: 50_000)
    wide = ScriptedClient()
    run(wide, list(FIVE))
    assert len(wide.requests[0]["tools"]) == 5
    assert _stable_system(wide).startswith(full)

    # Tight window: _fit_window keeps the first _MIN_TOOLS schemas, and the
    # rules are rebuilt for exactly those — the dropped tool's block is gone.
    monkeypatch.setattr(config, "model_context_window", lambda model: 5_500)
    tight = ScriptedClient()
    run(tight, list(FIVE))
    sent = [t["function"]["name"] for t in tight.requests[0]["tools"]]
    assert sent == FIVE[:loop._MIN_TOOLS]
    assert _stable_system(tight).startswith(first_four)
    assert not _stable_system(tight).startswith(full)


def test_rules_always_cover_every_offered_tool_across_windows(monkeypatch):
    """The rebuilt rules must stay a superset of what the step actually offers.

    The first cut fitted with the full rules and rebuilt from the survivors; the
    smaller rebuilt text then let the real fit keep MORE tools than the pre-fit
    did, so at some windows a tool was offered without its rule block (audit
    finding). Sweep the window across every tool-drop boundary and check the
    invariant directly against the rule blocks.
    """
    monkeypatch.setattr(skills, "skills_context_block", lambda *args: "")
    loop.build_system(set())  # populate the block cache
    blocks = [(named, text) for named, text in loop._BLOCKS_CACHE if named]
    full = loop.build_system(set(FIVE))
    saved_somewhere = False
    for window in range(3_000, 9_600, 75):
        monkeypatch.setattr(config, "model_context_window", lambda model, w=window: w)
        client = ScriptedClient()
        run(client, list(FIVE))
        offered = {t["function"]["name"] for t in client.requests[0]["tools"]}
        stable = _stable_system(client)
        for named, text in blocks:
            if named & offered:
                assert text in stable, (window, sorted(named & offered))
        if len(offered) < len(FIVE) and not stable.startswith(full):
            saved_somewhere = True
    assert saved_somewhere


def _blocks():
    loop.build_system(set())
    return [(named, text) for named, text in loop._BLOCKS_CACHE if named]


def _assert_rules_cover_every_request(client):
    for request in client.requests:
        offered = {t["function"]["name"] for t in request["tools"]}
        stable = request["messages"][0]["content"]
        for named, text in _blocks():
            if named & offered:
                assert text in stable, sorted(named & offered)


class CallingClient(ScriptedClient):
    """Calls `first` on step 0, then answers."""

    def __init__(self, first):
        super().__init__()
        self.first = first

    async def stream_events(self, model, messages, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        if len(self.requests) == 1:
            yield {"kind": "final", "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "function": {"name": self.first, "arguments": "{}"}}]}}
        else:
            yield {"kind": "final", "message": {"role": "assistant", "content": "Done."}}


@pytest.fixture
def fake_read():
    from service.tools.registry import REGISTRY, Tool
    saved = REGISTRY.get("fake_read")
    REGISTRY["fake_read"] = Tool("fake_read", "fake read", {"type": "object", "properties": {}},
                                 "system_read", lambda: "tiny")
    try:
        yield "fake_read"
    finally:
        if saved is None:
            REGISTRY.pop("fake_read", None)
        else:
            REGISTRY["fake_read"] = saved


def test_rules_cover_later_steps_after_protected_history_is_freed(monkeypatch, fake_read):
    """Step 0 protects a large assistant turn as one of the newest messages;
    step 1 (after a tool call and result) can evict it and keep MORE tools.
    The rules were sized against the minimal message list, so they still
    cover whatever step 1 offers (second audit finding)."""
    monkeypatch.setattr(skills, "skills_context_block", lambda *args: "")
    history = [{"role": "user", "content": "earlier"},
               {"role": "assistant", "content": "long reply " * 1500},
               {"role": "user", "content": "What is on today?"}]
    tools = [fake_read] + list(FIVE)

    async def emit(event):
        pass

    seen_growth = False
    for window in range(6_000, 10_000, 100):
        monkeypatch.setattr(config, "model_context_window", lambda model, w=window: w)
        client = CallingClient(fake_read)
        asyncio.run(loop.run_agent(client, "Ling-3.0-tiny-oQ4e", list(history), emit, Approver(),
                                   debug=False, max_steps=2, tools=tools))
        assert len(client.requests) == 2
        _assert_rules_cover_every_request(client)
        if len(client.requests[1]["tools"]) > len(client.requests[0]["tools"]):
            seen_growth = True
    assert seen_growth  # the scenario the finding described actually occurred


def test_rules_cover_a_required_group_tool_the_fit_would_drop(monkeypatch):
    monkeypatch.setattr(skills, "skills_context_block", lambda *args: "")
    monkeypatch.setattr(config, "model_context_window", lambda model: 5_500)
    client = ScriptedClient()
    run(client, list(FIVE), required_tool_groups=(frozenset({"search_notes"}),))
    sent = [t["function"]["name"] for t in client.requests[0]["tools"]]
    assert sent == ["search_notes"]  # the forced step offers the group only
    _assert_rules_cover_every_request(client)


def test_model_led_discovery_keeps_the_skills_catalog(monkeypatch):
    monkeypatch.setattr(config, "model_context_window", lambda model: 50_000)
    calls = []

    def recorder(*args):
        calls.append(args)
        return "\nSKILLS"

    monkeypatch.setattr(skills, "skills_context_block", recorder)
    client = ScriptedClient()
    run(client, None, model_led_discovery=True)
    offered = {t["function"]["name"] for t in client.requests[0]["tools"]}
    assert "use_skill" not in offered  # only the discovery tool is on the table
    assert calls and calls[-1][2] is True
    assert _stable_system(client).endswith("\nIDENTITY\nMEMORY\nSKILLS")


def test_skills_catalog_only_when_use_skill_is_offered(monkeypatch):
    monkeypatch.setattr(config, "model_context_window", lambda model: 50_000)
    calls = []

    def recorder(*args):
        calls.append(args)
        return "\nSKILLS"

    monkeypatch.setattr(skills, "skills_context_block", recorder)
    client = ScriptedClient()
    run(client, list(FIVE[:2]))
    assert calls and calls[-1][2] is False
    assert _stable_system(client).endswith("\nIDENTITY\nMEMORY\nSKILLS")

    calls.clear()
    with_skill = ScriptedClient()
    run(with_skill, None)  # the unscoped route offers every registered tool
    offered = {t["function"]["name"] for t in with_skill.requests[0]["tools"]}
    assert calls and calls[-1][2] is ("use_skill" in offered)


def test_context_block_without_catalog_keeps_matched_bodies(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_skills", {})
    skills._skills["one-shot"] = skills.Skill(
        name="one-shot", description="A trigger-matched instruction skill",
        body="Do one thing.", path=tmp_path / "one-shot", triggers=["one shot"])
    skills._skills["other"] = skills.Skill(
        name="other", description="Unrelated", body="Other body.",
        path=tmp_path / "other", triggers=["zzz"])

    listed = skills.skills_context_block("Run the one shot procedure")
    assert "Installed skills" in listed and "- other:" in listed and "Do one thing." in listed

    bodies_only = skills.skills_context_block("Run the one shot procedure", "", False)
    assert "Installed skills" not in bodies_only and "- other:" not in bodies_only
    assert "Do one thing." in bodies_only

    assert skills.skills_context_block("nothing matches", "", False) == ""
    assert "Other body." in skills.skills_context_block("nothing matches", "other", False)
