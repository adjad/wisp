"""A skill called by name is an internal mis-call, never a failure to report.

Reported live: on a broad goal the model called a skill as if it were a tool, was
told "Tell the user this specific action isn't available right now", and did. These
pin the recoverable wording, that real actions keep the honest one, that nothing
loads past the skill boundary, and that a refused call reaches the debug export.

    .venv/bin/python -m pytest tests/test_ask_skill_misnamed_as_tool.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-skill-misnamed-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service import skills  # noqa: E402
from service.agent import loop  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

PROMPT = "Help me with my goal: learn the cello"
ANSWER = "Let's start with your practice time. How many evenings are free this week?"
HONEST = "Tell the user this specific action isn't available right now"


class ScriptedClient:
    managed = True
    endpoint_name = "managed"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.choices: list = []

    async def ensure_only(self, model, *, exclusive=False, emit=None, **kw):
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        self.seen.append(list(messages))
        self.choices.append(tool_choice)
        msg = self.replies.pop(0) if self.replies else {
            "role": "assistant", "content": ANSWER, "tool_calls": None}
        yield {"kind": "final", "message": msg}


class ExternalClient(ScriptedClient):
    managed = False
    endpoint_name = "local_provider"


class Approver:
    async def confirm(self, action):
        raise AssertionError("a refused call must never reach a confirmation")


def _call(name, args="{}", cid="c1"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": args}}]}


def _say():
    return {"role": "assistant", "content": ANSWER, "tool_calls": None}


def _run(replies, client=ScriptedClient, tools=("read_file", "list_dir"), **kw):
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    c = client(replies)
    text = asyncio.run(loop.run_agent(
        c, "Agents-A1-4B-oQe6", [{"role": "user", "content": PROMPT}],
        emit, Approver(), max_steps=4, tools=list(tools), **kw))
    return c, events, text


def _results(events):
    return [e for e in events if e.get("type") == "tool_result"]


def setup_module():
    skills.load()
    assert skills.get("interview-me") is not None, "the bundled skill this file relies on"


def test_a_skill_called_by_name_gets_a_recoverable_note():
    c, events, text = _run([_call("interview-me"), _say()])
    note = _results(events)[0]
    result = note["result"]
    assert HONEST not in result
    assert "Nothing ran" in result and "do not say it ran" in result
    assert "Do not mention it" in result
    assert "skill" not in result.lower()
    assert "denied" not in result.lower()  # main treats that word as a user denial
    assert note["internal"] is True
    # The model saw exactly this, and its next answer is the turn's answer.
    assert [m for m in c.seen[-1] if m.get("role") == "tool"][-1]["content"] == result
    assert text == ANSWER


def test_the_next_step_can_still_call_read_tools():
    c, _, _ = _run([_call("interview-me"), _say()])
    assert len(c.choices) == 2
    assert c.choices[1] != "none"


def test_an_underscored_spelling_and_use_skill_count_too():
    for name, args in (("interview_me", "{}"), ("use_skill", '{"name": "interview-me"}')):
        _, events, _ = _run([_call(name, args), _say()])
        assert HONEST not in _results(events)[0]["result"], name
        assert not any("--- Skill:" in str(e) for e in events), name


def test_no_skill_body_loads():
    c, events, _ = _run([_call("interview-me"), _say()])
    body = skills.get("interview-me").body
    assert all(body not in str(m.get("content")) for m in c.seen[-1])
    assert all(body not in str(e) for e in events)


def test_a_repeat_forces_an_answer_instead_of_spending_every_step():
    c, _, text = _run([_call("interview-me"), _call("interview-me", cid="c2"), _say()])
    assert c.choices[1] != "none"
    assert c.choices[2] == "none"
    assert text == ANSWER


def test_real_actions_keep_the_honest_rule():
    # Registered and unoffered, and a name that is nothing at all.
    assert "run_shell" in REGISTRY
    for name in ("run_shell", "not_a_real_tool"):
        _, events, _ = _run([_call(name, '{"command": "ls"}'), _say()])
        result = _results(events)[0]
        assert HONEST in result["result"], name
        assert "do NOT say you performed" in result["result"], name
        assert "internal" not in result, name


def test_a_real_tool_sharing_a_skill_name_is_a_real_action(monkeypatch):
    monkeypatch.setitem(skills._skills, "run_shell", skills.get("interview-me"))
    _, events, _ = _run([_call("run_shell"), _say()])
    assert HONEST in _results(events)[0]["result"]


def test_across_the_boundary_the_wording_reveals_nothing():
    # On the external provider an installed skill must read exactly like a made-up
    # name, or the refusal would confirm which skills exist.
    def refusal(name, **kw):
        _, events, _ = _run([_call(name), _say()], **kw)
        return _results(events)[0]["result"].replace(name, "NAME")

    for kw in ({"client": ExternalClient}, {"allow_skill_context": False}):
        assert refusal("interview-me", **kw) == refusal("not_a_real_tool", **kw), kw
        assert HONEST in refusal("interview-me", **kw)


def test_a_refused_call_reaches_the_debug_export_with_name_and_args():
    _, events, _ = _run([_call("interview-me", json.dumps({"topic": "cello"})), _say()])
    index = next(i for i, e in enumerate(events) if e.get("type") == "tool_rejected")
    rejected = events[index]
    assert rejected["name"] == "interview-me"
    assert rejected["args"] == {"topic": "cello"}
    # Right before its result, under the same id, so the record gets the refusal.
    assert events[index + 1]["type"] == "tool_result" and events[index + 1]["id"] == rejected["id"]
    # Not a tool_call: main stores those as calls that were made (tool digest, subject).
    assert not [e for e in events if e.get("type") == "tool_call"]


def test_every_refusal_is_recorded_not_only_the_skill_one():
    _, events, _ = _run([_call("delete_path", '{"path": "~/Downloads"}'), _say()])
    rejected = [e for e in events if e.get("type") == "tool_rejected"]
    assert [(e["name"], e["args"]) for e in rejected] == [("delete_path", {"path": "~/Downloads"})]


def test_a_call_that_ran_is_not_recorded_as_rejected():
    _, events, _ = _run([_call("list_dir", '{"path": "~/no-such-dir-for-this-test"}'), _say()])
    assert [e for e in events if e.get("type") == "tool_call"]
    assert _results(events)
    assert not [e for e in events if e.get("type") == "tool_rejected"]


def test_rejected_arguments_get_the_meeting_redaction():
    secret = "wisp-meeting-" + "a" * 32
    original = loop._meeting_safe
    try:
        loop._meeting_safe = lambda v: json.loads(json.dumps(v).replace(secret, "[removed]"))
        _, events, _ = _run([_call("delete_path", json.dumps({"path": secret})), _say()])
    finally:
        loop._meeting_safe = original
    rejected = next(e for e in events if e.get("type") == "tool_rejected")
    assert rejected["args"] == {"path": "[removed]"}


def test_the_goal_chip_reaches_useful_read_tools():
    # The chip's words alone must lead somewhere: its own lexical head start holds
    # a read of the user's day or tasks, and the rest stay reachable by name.
    from service import main as service_main
    from service.model_only import ASK_CORE_STUBS
    preload, stubs = service_main._ask_tool_rank("Help me with my goal: Doing well in school")
    useful = {"get_my_day_plan", "search_reminders", "get_upcoming", "predict_my_day"}
    assert useful & set(preload)
    assert {"get_upcoming", "search_reminders", "get_my_day_plan"} <= ASK_CORE_STUBS
    assert "use_skill" not in preload and "interview-me" not in preload + stubs
