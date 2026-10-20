"""A narration step that never answers must not put raw evidence JSON on screen.

2026-10-14, "what should I do tmrow": collect_task_evidence returned epoch
seconds only, LFM2.5 (think block cannot be disabled) spent its whole
3,000-token budget converting them, produced no content, the empty-step retry
repeated the same cut-off (104s total), and the fallback showed the tool's JSON
- Messages verification codes included - as the reply.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from service.agent import loop
from service.tools import task_evidence as T
from service.tools.registry import REGISTRY, Tool

MODEL = "Agents-A1-4B-oQe6"
PDT_START = 1792047600.0  # Thu Oct 15 2026 00:00 PDT


@pytest.fixture
def pacific(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def payload() -> str:
    return json.dumps({
        "task_window": {"period": "tomorrow", "start": PDT_START, "end_exclusive": PDT_START + 86400},
        "sources": {
            "calendar": {"records": [
                {"title": "Vercel Ship SF", "when_ts": 1792080000.0, "end_ts": 1792116000.0, "all_day": 0},
                {"title": "UCSC Startup Club Meeting", "when_ts": 1792116000.0, "end_ts": 1792119600.0, "all_day": 0},
                {"title": "Week 3 Reading: Branching", "when_ts": PDT_START, "end_ts": 1792133999.0, "all_day": 1},
            ]},
            "reminders": {"records": [
                {"title": "Get doordash order", "when_ts": 1790990100.0, "temporal_state": "overdue"}]},
            "messages": {"records": [
                {"sender": "36726", "text": "Fidelity Investments: Code is: 420615"}]},
        }}, indent=2)


def test_local_time_is_the_users_wall_clock(pacific):
    assert T._local(1792116000.0) == "Thu Oct 15, 7:00 PM PDT"
    assert T._local(PDT_START, all_day=True) == "Thu Oct 15 (all day)"
    assert T._local(None) is None and T._local(float("nan")) is None
    rows = [{"when_ts": 1792080000.0, "end_ts": 1792116000.0, "all_day": 0}]
    T._add_local_times(rows)
    assert rows[0]["when_local"] == "Thu Oct 15, 9:00 AM PDT"
    assert rows[0]["end_local"] == "Thu Oct 15, 7:00 PM PDT"


def test_rendering_is_readable_and_never_quotes_bodies(pacific):
    text = T.render_evidence_text(payload())
    assert "task_window" not in text and "{" not in text
    assert "Thu Oct 15, 9:00 AM PDT to 7:00 PM PDT: Vercel Ship SF" in text
    assert "Thu Oct 15 (all day): Week 3 Reading: Branching" in text
    assert "Overdue" in text and "Get doordash order" in text
    assert "420615" not in text and "Fidelity" not in text
    assert "messages" in text  # said to be unsummarised, not silently dropped


def test_rendering_ignores_other_tools_results():
    assert T.render_evidence_text("Good afternoon - Wednesday") is None
    assert T.render_evidence_text('{"unrelated": 1}') is None
    assert T.render_evidence_text("") is None


def test_empty_and_withheld_sources_do_not_establish_absence():
    data = {'task_window': {'period':'tomorrow'}, 'excluded_sources':['email'], 'sources':{
        'calendar':{'state':'unknown native current state; native rows withheld',
                    'withheld_unverified':4, 'upstream_uninspected':3, 'records':[]},
        'messages':{'state':'available', 'omitted':2, 'records':[]},
        'notes':{'latest_available':False, 'records':[]}}}
    text = T.render_evidence_text(json.dumps(data))
    assert 'Nothing found' not in text
    assert '4 withheld as unverified' in text and '3 not inspected upstream' in text
    assert '2 omitted' in text and 'freshness unknown' in text
    assert 'Mail: excluded; not read.' in text and 'Notes: unavailable or unverified' in text


@pytest.mark.parametrize('source,expected', [
    ({'state':'syncing', 'coverage':{'status':'unavailable','reason':'current_launch_sync_required'}},
     ('unavailable or unverified', 'current launch sync required')),
    ({'state':'ready', 'coverage':{'status':'partial','skipped':5,'truncated':7,'reached_row_limit':True}},
     ('partial cached evidence', '5 skipped', '7 truncated', 'source row limit reached')),
])
def test_messages_carrier_limitations_are_not_flattened(source, expected):
    text = T.render_coverage_text(json.dumps({'task_window':{},'sources':{'messages':source}}))
    assert all(phrase in text for phrase in expected)


@pytest.mark.parametrize('origin', ['none','restored','expired'])
def test_notes_without_current_receipt_remain_unverified(origin):
    text = T.render_coverage_text(json.dumps({'task_window':{},'sources':{
        'notes':{'origin':origin,'latest_available':None,'records':[]}}}))
    assert 'unavailable or unverified' in text and 'freshness unknown' in text


@pytest.mark.parametrize('claim', ['There are no other tasks or commitments for that day.',
                                 'No other actionable deadlines were found.',
                                 'This is the only personal request in your email.',
                                 "That's the only thing in the window.",
                                 'No messages or other sources show requests.',
                                 'Both email and Messages have no unread personal items today. Nothing needs attention.'])
def test_exhaustive_absence_over_bounded_sources_is_rejected(claim):
    answer = T.verify_evidence_narration(claim, payload())
    assert claim not in answer
    assert 'Source coverage (bounded; absence not verified):' in answer
    assert '420615' not in answer


def test_grounded_narration_is_not_rewritten():
    text = 'Prioritize the review based on the returned email. Messages freshness is unknown.'
    assert T.verify_evidence_narration(text, payload()) == text


@pytest.mark.parametrize('claim', ['No reply from you yet in the cache.',
    'You have not replied to this message.', "You haven't responded yet.",
    'You did not reply.', 'This request remains unanswered.',
    'You still have not replied.', 'You have yet to respond.',
    'The request is still awaiting your response.', 'No reply yet.',
    'No one has replied to this request.',
    "I can't verify whether you have replied, but you have not replied.",
    'Whether you have replied is unclear; no reply from you was found.',
    "I can't determine whether there is a reply from you, and there is no reply from you.",
    'We may not know if you responded; this request remains unanswered.',
    'No one responded to this request.', 'We have not seen you reply to the message.',
    'The question is waiting on your response.',
    "I can't verify whether you replied yet no response from you was recorded.",
    "I can't confirm you replied because the request is still awaiting your response.",
    "I can't tell whether you replied; however, the question is waiting on your response."])
def test_partial_communications_do_not_establish_reply_absence(claim):
    data = json.loads(payload())
    data['sources']['messages']['coverage'] = {'status':'partial','skipped':576,'truncated':9870}
    data['sources']['messages']['omitted'] = 1996
    answer = T.verify_evidence_narration(claim, json.dumps(data))
    assert claim not in answer and 'Source coverage' in answer


@pytest.mark.parametrize('text', [
    "I can't tell whether you have not replied from these partial excerpts.",
    "If you haven't replied, consider responding.",
    'The message says "You have not replied." Reply status cannot be verified.',
    'An outgoing message from you appears after the request.',
    'This message asks a question; check whether it still needs a reply.',
])
def test_reply_uncertainty_quotes_and_positive_records_are_preserved(text):
    assert T.verify_evidence_narration(text, payload()) == text


@pytest.mark.parametrize("claim", ["That's the only thing in the window.", "No reply from you yet in the cache."])
def test_overclaim_gets_one_correction_without_repeating_reads(evidence_tool, claim):
    class Correction(Client):
        async def stream_events(self, model, messages, *, tools=None, **kwargs):
            if self.calls == 0:
                async for event in super().stream_events(model, messages, tools=tools, **kwargs):
                    yield event
            else:
                self.calls += 1
                if self.calls > 2:
                    assert not tools and kwargs.get('tool_choice') == 'none'
                yield {'kind':'final','message':{'role':'assistant','content':(
                    claim if self.calls == 2 else
                    'The returned Calendar records show the meeting. Coverage is incomplete.')}}
    client = Correction(False)
    out = run(client)
    assert client.calls == 3
    assert 'returned Calendar records' in out and 'only thing' not in out
    assert 'Source coverage (bounded; absence not verified):' in out


def test_correction_tool_call_is_rejected_before_any_argument_preparation(evidence_tool, monkeypatch):
    from unittest.mock import Mock
    from service.tools import assistant_tools
    prepare = Mock(side_effect=AssertionError('correction must not prepare or read Calendar'))
    monkeypatch.setattr(assistant_tools, 'prepare_event_move', prepare)
    class MaliciousCorrection(Client):
        async def stream_events(self, model, messages, *, tools=None, **kwargs):
            if self.calls == 0:
                async for event in super().stream_events(model, messages, tools=tools, **kwargs):
                    yield event
            else:
                self.calls += 1
                message = {'role':'assistant', 'content':"That's the only thing in the window."}
                if self.calls > 2:
                    assert tools == [] and kwargs['tool_choice'] == 'none'
                    message['tool_calls'] = [{'id':'evil', 'function':{
                        'name':'update_event', 'arguments':'{"id":"fictional-event","when_iso":"2026-10-20T15:00"}'}}]
                yield {'kind':'final','message':message}
    client = MaliciousCorrection(False)
    out = run(client)
    assert client.calls == 3
    prepare.assert_not_called()
    assert 'Source coverage (bounded; absence not verified):' in out
    assert '420615' not in out


def test_successful_narration_keeps_source_coverage(evidence_tool, monkeypatch):
    class Narrator(Client):
        async def stream_events(self, model, messages, *, tools=None, **kwargs):
            if self.calls == 0:
                async for event in super().stream_events(model, messages, tools=tools, **kwargs):
                    yield event
            else:
                yield {'kind':'final', 'message':{'role':'assistant', 'content':'Prepare for the meeting.'}}
    out = run(Narrator(False))
    assert 'Prepare for the meeting.' in out
    assert 'Source coverage (bounded; absence not verified):' in out
    assert 'Messages:' in out and 'freshness unknown' in out
    assert '420615' not in out


class Approver:
    async def confirm(self, action):
        return True


async def emit(_event):
    return None


class Client:
    """Step 1 calls the tool; every later step comes back empty."""

    def __init__(self, truncated: bool):
        self.truncated = truncated
        self.calls = 0

    async def ensure_only(self, *args, **kwargs):
        return None

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "final", "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "function": {"name": "collect_task_evidence", "arguments": "{}"}}]}}
            return
        message = {"role": "assistant", "content": "", "reasoning_content": "converting epochs...",
                   "tool_calls": None}
        if self.truncated:
            message["_truncated"] = True
        yield {"kind": "final", "message": message}


@pytest.fixture
def evidence_tool(pacific):
    saved = REGISTRY.get("collect_task_evidence")
    REGISTRY["collect_task_evidence"] = Tool(
        "collect_task_evidence", "evidence", {"type": "object", "properties": {}},
        "system_read", lambda: payload())
    yield
    REGISTRY["collect_task_evidence"] = saved


def run(client: Client) -> str:
    return asyncio.run(loop.run_agent(
        client, MODEL, [{"role": "user", "content": "what should I do tmrow"}],
        emit, Approver(), tools=["collect_task_evidence"], max_steps=4))


def test_truncated_narration_is_not_retried_and_shows_readable_text(evidence_tool):
    client = Client(truncated=True)
    out = run(client)
    assert client.calls == 2, "the cut-off step must not be retried with the same budget"
    assert "task_window" not in out and "420615" not in out
    assert "Vercel Ship SF" in out


def test_other_empty_steps_still_retry_and_still_render_readably(evidence_tool):
    client = Client(truncated=False)
    out = run(client)
    assert client.calls == 3, "an unparseable empty reply keeps its re-sample"
    assert "task_window" not in out and "Vercel Ship SF" in out



@pytest.mark.parametrize("end", ["2026-10-16T01:00:00-07:00", "2026-10-18T09:00:00-07:00"])
def test_calendar_fallback_keeps_different_end_date(pacific, end):
    from datetime import datetime
    start = datetime.fromisoformat("2026-10-15T23:00:00-07:00").timestamp()
    finish = datetime.fromisoformat(end).timestamp()
    row = {"title":"Synthetic overnight work", "when_ts":start, "end_ts":finish}
    T._add_local_times([row])
    text = T.render_evidence_text(json.dumps({"task_window":{"period":"tomorrow"},
                                             "sources":{"calendar":{"records":[row]}}}))
    assert f"{row['when_local']} to {row['end_local']}" in text
