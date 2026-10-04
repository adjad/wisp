"""Synthetic regressions for colloquial tomorrow planning, without live sources."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import service.tools  # noqa: F401
from service.router import router as R


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["tomorrow", "tommorow", "tommorrow", "tomorow", "tmrw", "tmrow"])
@pytest.mark.parametrize("phrase", ["any plans for {}?", "what am I doing {}?", "what is up {}?"])
async def test_tomorrow_plans_bind_exact_day(spelling, phrase):
    with patch.object(R, "_semantic_core", new=AsyncMock(return_value=["recall", "run_shell"])):
        d = await R.route(phrase.format(spelling))
    assert d.direct_calls == [("get_upcoming", {"period": "tomorrow", "calendar_only": True})]
    assert d.verified_results_only
    assert {"recall", "run_shell", "web_search"} <= d.forbidden_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", [
    "what do I have to do tmrow", "what is on my do list tmrow",
    "what's on my to-do list tomorrow?", "what is on my todo list tommorow?",
])
async def test_tomorrow_tasks_check_current_sources_before_narration(phrase):
    with patch.object(R, "_semantic_core", new=AsyncMock(return_value=["recall"])):
        d = await R.route(phrase)
    assert d.direct_calls == [
        ("get_upcoming", {"period": "tomorrow"}),
        ("summarize_messages", {}), ("summarize_emails", {}), ("search_notes", {}),
    ]
    assert d.verified_results_only
    assert "recall" in d.forbidden_tools
    assert "tomorrow" in d.resolved_request.lower()
    # One any-of group lets the loop execute the whole batch even when the
    # calendar is empty; singleton groups stop on the first no-match.
    assert d.required_tool_groups == (frozenset(name for name, _ in d.direct_calls),)


@pytest.mark.asyncio
async def test_tomorrow_tasks_preserve_source_exclusions():
    d = await R.route("don't read my email; what do I have to do tmrow?")
    names = {name for name, _ in d.direct_calls}
    assert names == {"get_upcoming", "summarize_messages", "search_notes"}
    assert "summarize_emails" in d.forbidden_tools


@pytest.mark.asyncio
async def test_all_sources_excluded_does_not_read_private_data():
    d = await R.route("don't read my calendar or email or messages or notes; "
                      "what do I have to do tmrow?")
    assert d.direct_calls == []
    assert d.tool_subset == []
    assert d.verified_results_only


@pytest.mark.asyncio
async def test_target_day_is_supplied_by_python_not_model_reasoning():
    start = datetime(2026, 10, 4).timestamp()
    end = datetime(2026, 10, 5).timestamp()
    with patch("service.tools.timeranges.resolve_span", return_value=(start, end, "tomorrow")):
        d = await R.route("what do I have to do tmrow?")
    assert "Sunday" in d.resolved_request
    assert "2026-10-04" in d.resolved_request
    assert "unavailable" in d.resolved_request.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", [
    'Create a note saying "what am I doing tomorrow?"',
    "Any plans for tomorrow? Send them to Mom.",
    "Search the web for public plans for tomorrow",
    "What do I have to do tomorrow and next week?",
    "What am I doing tomorrow in Paris?",
])
async def test_compound_quoted_and_modified_requests_are_not_preexecuted(phrase):
    with patch.object(R, "_semantic_core", new=AsyncMock(return_value=["calculate"])):
        d = await R.route(phrase)
    assert not d.verified_results_only
    assert ("get_upcoming", {"period": "tomorrow"}) not in d.direct_calls


@pytest.mark.asyncio
async def test_planning_turn_drops_stale_conversation_history():
    from service import main

    d = await R.route("what do I have to do tmrow?")
    prompt = {"role": "user", "content": d.resolved_request}
    with patch.object(main, "build_messages", return_value=[
        {"role": "assistant", "content": "Synthetic stale July repair appointment"}
    ]) as history:
        messages = main._tool_turn_messages("synthetic", prompt, max_tokens=1500,
            test_mode=False, verified_results_only=d.verified_results_only)
    assert messages == [prompt]
    history.assert_not_called()


class RecordingNarrator:
    def __init__(self, attempt_recall=False):
        self.messages = []
        self.attempt_recall = attempt_recall
        # An explicit window: without it the default 8000-token window is almost full, so
        # loading the skills catalog elsewhere in the same process evicts the oldest
        # message (the Calendar receipt) and this test starts to depend on collection order.
        self.target = SimpleNamespace(context_window=64000)

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, **kwargs):
        self.messages.append(list(messages))
        if self.attempt_recall and len(self.messages) == 1:
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": "stale-memory", "type": "function", "function": {
                        "name": "recall", "arguments": '{"query":"old plans"}'}}]}}
            return
        yield {"kind": "content", "text": "Verified synthetic commitment."}
        yield {"kind": "final", "message": {
            "role": "assistant", "content": "Verified synthetic commitment."}}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["partial", "empty", "unavailable", "memory_attempt"])
async def test_empty_calendar_does_not_stop_other_sources(case):
    from service.agent import loop

    d = await R.route("what do I have to do tmrow?")
    assert len(d.direct_calls) == 4
    calendar = "Today is Saturday, October 3, 2026. Nothing scheduled in tomorrow."
    unavailable = "Wisp could not check Calendar. This schedule may be incomplete."
    results = {name: "Nothing found." for name, _ in d.direct_calls}
    results["get_upcoming"] = unavailable if case == "unavailable" else calendar
    if case in {"partial", "memory_attempt"}:
        results["summarize_messages"] = "Synthetic commitment due Sunday, October 4, 2026."
    executed = []

    async def fake_read(tool, args, **kwargs):
        executed.append((tool.name, dict(args)))
        return results[tool.name]

    narrator = RecordingNarrator(attempt_recall=case == "memory_attempt")
    approver = AsyncMock()
    with patch.object(loop, "run_tool", side_effect=fake_read), patch.object(loop, "audit"):
        out = await loop.run_agent(narrator, "fixture-model",
            [{"role": "user", "content": d.resolved_request}], AsyncMock(), approver,
            tools=d.tool_subset, direct_calls=d.direct_calls,
            required_tool_groups=d.required_tool_groups, forbidden_tools=d.forbidden_tools,
            tool_argument_bindings=d.tool_argument_bindings,
            strict_read_limits=d.strict_read_limits, multi_round=d.multi_round,
            narration_after=d.narration_after, include_memory_context=False, max_steps=3)
    assert executed == d.direct_calls
    approver.confirm.assert_not_called()
    if case in {"partial", "memory_attempt"}:
        assert len(narrator.messages) == (2 if case == "memory_attempt" else 1)
        receipts = [m for m in narrator.messages[0] if m["role"] == "tool"]
        assert len(receipts) == 4
        assert "Sunday, October 4, 2026" in str(receipts)
        assert calendar in str(receipts)
    else:
        assert narrator.messages == []
        if case == "unavailable":
            assert "couldn’t retrieve" in out.lower()
            assert "get_upcoming" in out
