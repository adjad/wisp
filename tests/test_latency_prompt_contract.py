"""Prompt-cache layout and complete outbound approval without duplicate prose.

All model replies and bridge operations are synthetic. No model is loaded and
no message is sent. Run with pytest; WISP_HOME must remain isolated.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-latency-prompt-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from service.agent import loop
from service import config
from service.memory import identity
from service.safety import policy
from service.tools import action_tools
from service.tools.registry import REGISTRY, tool_schemas


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        response = self.responses.pop(0) if self.responses else {
            "role": "assistant", "content": "Done."}
        yield {"kind": "final", "message": response}


class Approver:
    def __init__(self, approved=True):
        self.approved = approved
        self.actions = []

    async def confirm(self, action):
        self.actions.append(copy.deepcopy(action))
        return self.approved


def tool_call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}


def run(client, approver=None, messages=None, **kwargs):
    events = []

    async def emit(event):
        events.append(event)

    answer = asyncio.run(loop.run_agent(
        client, "Ling-3.0-tiny-oQ4e",
        messages or [{"role": "user", "content": "Please do that."}],
        emit, approver or Approver(), debug=False, max_steps=2, **kwargs))
    return answer, events


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    # Keep system text reproducible and prevent user memory/identity reads.
    monkeypatch.setattr(identity, "identity_prompt_block", lambda **kwargs: "\nIDENTITY")
    monkeypatch.setattr(loop.prompt_blocks, "memory_block", lambda **kwargs: "\nMEMORY")
    import service.skills
    monkeypatch.setattr(service.skills, "skills_context_block", lambda *args: "\nSKILLS")
    monkeypatch.setattr(loop, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)


def test_clock_style_and_safety_order_survive_context_fitting(monkeypatch):
    clock = "\nThe current date and time is Tuesday, September 8, 2026 at 11:59 PM. Resolve relative dates/times against this."
    seen = []

    def now_line(*, resolve_hint=False):
        seen.append(resolve_hint)
        return clock

    monkeypatch.setattr(loop.prompt_blocks, "now_line", now_line)
    monkeypatch.setattr(config, "model_context_window", lambda model: 6500)
    history = [{"role": "system", "content": "OLD SUMMARY " * 9000},
               {"role": "assistant", "content": "old response"},
               {"role": "user", "content": "Remind me about tomorrow's appointment."}]
    client = ScriptedClient()
    run(client, messages=history, tools=["get_upcoming", "add_reminder"],
        style_hint="STYLE: Preserve the full detail.", test_mode=True,
        reminder_action="clarify_time")

    messages = client.requests[0]["messages"]
    assert seen == [True]
    assert [m["role"] for m in messages[:2]] == ["system", "system"]
    stable, runtime = [m["content"] for m in messages[:2]]
    assert stable.endswith("\nIDENTITY\nMEMORY\nSKILLS")
    assert clock not in stable and runtime.startswith(clock)
    assert runtime.index("STYLE:") < runtime.index(loop._TEST_MODE_SUFFIX)
    assert runtime.index(loop._TEST_MODE_SUFFIX) < runtime.index("REMINDER TASK:")
    assert "Do not invent a time or claim a reminder was set" in runtime
    assert not any("OLD SUMMARY" in str(m.get("content")) for m in messages)
    assert history[-1] in messages
    assert all(set(m) <= {"role", "content", "tool_calls", "tool_call_id"} for m in messages)


def test_fit_protects_only_explicit_runtime_prefix_and_latest_exchange(monkeypatch):
    monkeypatch.setattr(config, "model_context_window", lambda model: 3500)
    stable = {"role": "system", "content": "stable rules"}
    clock = {"role": "system", "content": "exact current time and runtime rules"}
    old_summary = {"role": "system", "content": "old memory " * 1800}
    question = {"role": "user", "content": "What time is it?"}
    result = {"role": "tool", "content": "latest tool result", "tool_call_id": "c"}
    messages = [stable, clock, old_summary, question,
                {"role": "assistant", "content": "tool preamble"}, result]
    fitted, schemas, output = loop._fit_window(
        messages, [], 3000, "test", None, protected_prefix_count=2)
    assert fitted[:2] == [stable, clock]
    assert old_summary not in fitted and question in fitted and result in fitted
    assert output == 3000 and schemas == []
    assert messages[2] is old_summary  # the caller's history is not mutated


def test_direct_tool_result_followup_retains_runtime_clock(monkeypatch):
    from dataclasses import replace
    monkeypatch.setitem(REGISTRY, "get_upcoming", replace(
        REGISTRY["get_upcoming"], func=lambda **kwargs: "Tomorrow: fixture appointment"))
    clock = "\nExact fixture clock 11:59 PM; tomorrow means September 9."
    monkeypatch.setattr(loop.prompt_blocks, "now_line", lambda **kwargs: clock)
    client = ScriptedClient()
    run(client, tools=["get_upcoming"], direct_calls=[("get_upcoming", {})])
    messages = client.requests[0]["messages"]
    assert messages[1] == {"role": "system", "content": clock}
    assert any(m.get("role") == "tool" and "fixture appointment" in m["content"] for m in messages)


def test_required_clock_survives_every_forced_retry(monkeypatch):
    clock = "\nThe current date and time is September 8, 2026 at 11:59 PM."
    monkeypatch.setattr(loop.prompt_blocks, "now_line", lambda **kwargs: clock)
    monkeypatch.setattr(config, "model_context_window", lambda model: 6000)
    client = ScriptedClient()
    run(client, tools=["get_upcoming"], force_first_tool="get_upcoming",
        test_mode=True, messages=[
            {"role": "system", "content": "OBSOLETE SUMMARY " * 9000},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "Check my calendar."}])
    assert len(client.requests) >= 3
    for request in client.requests:
        assert request["messages"][1]["content"].startswith(clock)
        assert not any("OBSOLETE SUMMARY" in str(m.get("content")) for m in request["messages"])


def test_installed_ling_template_places_schemas_before_changing_clock(monkeypatch):
    # Use the actual model template when installed; portable invariant tests
    # above still run on machines without the local Ling model.
    path = Path(os.environ.get("WISP_LING_TEMPLATE", str(
        Path.home() / "Desktop/OMLX_Model_Files/Ling-3.0-tiny-oQ4e/chat_template.jinja")))
    if not path.exists():
        pytest.skip("Ling's installed chat template is unavailable")
    from jinja2 import Environment
    template = Environment().from_string(path.read_text())
    rendered = []
    for minute in (58, 59):
        clock = f"\nThe current date and time is Tuesday, September 8, 2026 at 11:{minute} PM."
        monkeypatch.setattr(loop.prompt_blocks, "now_line", lambda **kwargs: clock)
        client = ScriptedClient()
        run(client, tools=["view_emails", "summarize_emails"])
        request = client.requests[0]
        text = template.render(messages=request["messages"], tools=request["tools"],
                               enable_thinking=True, add_generation_prompt=True)
        assert text.index("</tools>") < text.index(clock)
        assert "<role>SYSTEM</role>" + clock in text
        assert text.count(clock) == 1
        rendered.append(text)
    shared = os.path.commonprefix(rendered)
    assert "</tools>" in shared
    assert "11:" in shared


@pytest.mark.parametrize("name", ["send_email", "send_message", "reply_to_email"])
@pytest.mark.parametrize("outcome", ["approved", "denied", "failed"])
@pytest.mark.parametrize("full_access", [False, True])
def test_full_outbound_approval_without_assistant_draft(monkeypatch, name, outcome, full_access):
    monkeypatch.setattr(policy, "_FULL_ACCESS", full_access)
    body = "\n".join(f"Item {i}: September 9 at 11:59 PM — full source detail." for i in range(160))
    if name == "send_message":
        args = {"to": "+15555550123", "text": body}
    elif name == "send_email":
        args = {"to": "recipient@example.test", "cc": "copy@example.test",
                "subject": "Complete itinerary", "body": body}
    else:
        args = {"message_id": "fixture-message", "account": "Test Account", "body": body}
    envelope = {
        "message_id": "fixture-message", "account": "Test Account", "account_id": "test-account",
        "from": "sender@example.test", "to": ["recipient@example.test"],
        "cc": ["copy@example.test"], "bcc": [], "subject": "Re: Complete itinerary", "content": body,
    }
    requests = []

    async def bridge(operation, payload, **kwargs):
        requests.append((operation, copy.deepcopy(payload)))
        if operation == "prepare_email_reply":
            return {"ok": True, "reply": envelope}
        assert operation == name
        if outcome == "failed":
            return {"ok": False, "error": "synthetic bridge failure"}
        return {"ok": True, "accepted": True, "reply": envelope}

    monkeypatch.setattr(action_tools, "app_request", bridge)
    client = ScriptedClient(tool_call(name, args), {
        "role": "assistant", "content": "Sent." if outcome == "approved" else "Nothing was sent."})
    approver = Approver(approved=outcome != "denied")
    answer, events = run(client, approver, tools=[name])

    assert len(approver.actions) == 1  # still mandatory in full-access mode
    preview = approver.actions[0]["preview"]
    assert body in preview and preview.count(body) == 1
    if name == "send_message":
        assert args["to"] in preview
    else:
        assert "recipient@example.test" in preview and "copy@example.test" in preview
        assert "Complete itinerary" in preview
    effects = [payload for operation, payload in requests if operation == name]
    assert len(effects) == (0 if outcome == "denied" else 1)
    if effects:
        assert effects[0]["text" if name == "send_message" else "body"] == body
    assert not any(body in e.get("text", "") for e in events if e.get("type") == "delta")
    if outcome != "approved":
        assert answer != "Sent."


@pytest.mark.parametrize("name,field", [("send_message", "text"), ("send_email", "body"), ("reply_to_email", "body")])
def test_truncated_payload_retry_targets_arguments_without_duplicate_prose(name, field):
    problem = action_tools.outbound_content_problem(name, {field: "'An unfinished reply"})
    assert problem and "NOT sent" in problem and "arguments" in problem
    assert "reply first" not in problem
    assert "confirmation card" in REGISTRY[name].description
    assert "BEFORE calling" not in REGISTRY[name].description


def test_schedule_send_and_draft_review_are_preserved():
    problem = action_tools.outbound_content_problem("schedule_send", {"body": "'An unfinished draft"})
    assert "reply first" in problem
    assert action_tools.confirm_preview("schedule_send", {}) is None
    assert "ALWAYS show the full text" in REGISTRY["schedule_send"].description
    for name in ("draft_email", "draft_message"):
        assert "send" in REGISTRY[name].description
        assert action_tools.confirm_preview(name, {"to": "recipient@example.test", "body": "Keep the draft.", "text": "Keep the draft."}).endswith("Keep the draft.")


@pytest.mark.parametrize("name,digest", [
    ("send_email", "eb89dc61176d382959164ce855558e23e841e70bee4e8e74f88b491e17d9ee56"),
    ("reply_to_email", "1f5b3090576348c5688bae4f901c54ae9060904f5aceb568967398cb1ec6d65f"),
    ("send_message", "9f9144e3f1a3d139eb6a962bef0a91cef02cc00cfc2db24b7e57f5437a4e7252"),
])
def test_original_retrieval_text_is_preserved_without_returning_to_model(name, digest):
    from service.router.semantic import _docs
    tool = REGISTRY[name]
    # Digests captured from the untouched pre-optimization decorators. Even
    # punctuation changes can affect lexical scores or invalidate embeddings.
    assert hashlib.sha256(tool.effective_retrieval_description.encode()).hexdigest() == digest
    assert _docs(tool)[0] == f"{name.replace('_', ' ')}. {tool.retrieval_description}"
    schema = tool_schemas([name])[0]
    assert schema["function"]["description"] == tool.description
    assert "BEFORE calling" not in json.dumps(schema)
    assert "retrieval_description" not in json.dumps(schema)


def test_model_wording_edits_do_not_change_retrieval_cache_or_rankings(monkeypatch):
    from service.router import reranker, semantic
    from service.search.embedder import doc_key
    from service.router.tool_aliases import apply as apply_aliases
    apply_aliases()
    index = semantic.ToolIndex()
    index._keys = {name: [doc_key(doc) for doc in semantic._docs(tool)]
                   for name, tool in REGISTRY.items()}
    queries = ["text Mom my schedule", "reply to the email from my professor",
               "show my messages and draft a reply", "send that itinerary by email"]
    old_signature = reranker._signature()
    old_ranks = [reranker.lexical_rank(query) for query in queries]
    monkeypatch.setattr(REGISTRY["send_message"], "description", "completely different schema prose")
    assert reranker._signature() == old_signature
    assert not index.is_stale()
    assert [reranker.lexical_rank(query) for query in queries] == old_ranks
    monkeypatch.setattr(REGISTRY["send_message"], "retrieval_description", "new search document")
    assert reranker._signature() != old_signature
    assert index.is_stale()


def test_retrieval_override_is_optional_and_register_preserves_it(monkeypatch):
    from service.tools import registry
    monkeypatch.setattr(registry, "REGISTRY", {})
    registry.register("plain", "normal description", {}, "fs_read")(lambda: "ok")
    registry.register("frozen", "compact model description", {}, "fs_read",
                      retrieval_description="stable search document")(lambda: "ok")
    assert registry.REGISTRY["plain"].effective_retrieval_description == "normal description"
    frozen = registry.REGISTRY["frozen"]
    assert frozen.effective_retrieval_description == "stable search document"
    assert frozen.schema()["function"]["description"] == "compact model description"
