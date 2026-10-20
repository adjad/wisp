"""Independent reminder review controls; isolated stores and synthetic tools."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import service.tools  # noqa: F401
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_task
from service.tasks.engine import prepare_task_turn
from service.tasks.models import TaskPlan
from service.tasks.planner import plan_task


NOW = datetime(2026, 10, 19, 16, 9)
REQUEST = (
    "set a reminder on october 20th for vicor earnings report "
    "and another on the 28th for microsoft and google earnings report"
)


@pytest.fixture
def stores(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    assistant = AssistantStore(tmp_path / "assistant.db")
    try:
        yield sessions, assistant, sessions.create_session()
    finally:
        sessions._db.close()
        assistant._db.close()


def test_explicit_clock_answer_replaces_pending_all_day_mode(stores):
    sessions, assistant, sid = stores
    initial = prepare_task_turn(
        sessions, sid, "create an all-day reminder to review report",
        assistant_store=assistant, now=NOW,
    )
    assert initial is not None and not initial.executable
    assert initial.plan.parameters["all_day"].value is True

    answered = prepare_task_turn(
        sessions, sid, "tomorrow at 3pm", assistant_store=assistant, now=NOW,
    )
    assert answered is not None and answered.executable
    assert len(answered.plan.steps) == 1
    args = answered.plan.steps[0].args
    assert args["title"] == "review report"
    assert args["when_iso"] == "2026-10-20T15:00"
    assert args.get("all_day", False) is False
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("success_statuses", [["failed"], ["succeeded", "failed"], []])
async def test_restored_batch_cannot_override_verified_success_contract(
    monkeypatch, success_statuses,
):
    from service.tasks import executor

    plan = compile_task(REQUEST, now=NOW)
    assert plan is not None
    plan_task(plan)
    plan.status = "running"
    restored = TaskPlan.from_dict(plan.to_dict())
    restored.steps[0].success_statuses = success_statuses
    execute = AsyncMock(side_effect=AssertionError("invalid batch executed a tool"))
    claim = Mock(side_effect=AssertionError("invalid batch claimed an effect"))
    monkeypatch.setattr(executor, "run_tool", execute)

    result = await executor.execute_task(
        restored, AsyncMock(), SimpleNamespace(confirm=AsyncMock()),
        assistant_store=SimpleNamespace(active_between=lambda *args, **kwargs: []),
        on_claim=claim, now=NOW,
    )
    assert result.status == "failed"
    assert not result.tool_calls and not result.tool_results
    assert not restored.claimed_calls
    execute.assert_not_awaited()
    claim.assert_not_called()


@pytest.mark.parametrize("prompt", [
    "create an all-day reminder tomorrow at 3pm to review report",
    "remind me tomorrow at 3pm to review report all day",
    "set an all-day reminder on october 20th at 3pm for review report",
])
def test_initial_all_day_and_explicit_clock_conflict_never_executes(stores, prompt):
    sessions, assistant, sid = stores
    turn = prepare_task_turn(
        sessions, sid, prompt, assistant_store=assistant, now=NOW,
    )
    assert turn is None or not turn.executable
    assert turn is None or not turn.plan.steps
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


@pytest.mark.parametrize("companion", [
    "mute my volume", "disable Wi-Fi", "frobnicate the gizmo",
])
def test_unknown_unquoted_companion_cannot_partially_create_reminder(stores, companion):
    sessions, assistant, sid = stores
    prompt = "set a reminder on october 20th for review and " + companion
    plan = compile_task(prompt, now=NOW)
    assert plan is None or plan.status != "ready"
    turn = prepare_task_turn(
        sessions, sid, prompt, assistant_store=assistant, now=NOW,
    )
    assert turn is None or not turn.executable
    assert turn is None or not turn.plan.steps
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


@pytest.mark.parametrize("subject", [
    "review, mute my volume",
    "review then mute my volume",
    "review but disable Wi-Fi",
    "review. Mute my volume",
])
def test_unquoted_companion_separators_cannot_create_partial_reminder(stores, subject):
    sessions, assistant, sid = stores
    prompt = "set a reminder on october 20th for " + subject
    plan = compile_task(prompt, now=NOW)
    assert plan is None or plan.status != "ready"
    turn = prepare_task_turn(
        sessions, sid, prompt, assistant_store=assistant, now=NOW,
    )
    assert turn is None or not turn.executable
    assert turn is None or not turn.plan.steps
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


def test_lowercase_company_conjunction_remains_one_exact_subject():
    plan = compile_task(REQUEST, now=NOW)
    assert plan is not None
    steps = plan_task(plan)
    assert [(step.args["title"], step.args["when_iso"]) for step in steps] == [
        ("vicor earnings report", "2026-10-20T09:00"),
        ("microsoft and google earnings report", "2026-10-28T09:00"),
    ]


@pytest.mark.parametrize("title", [
    "review and disable Wi-Fi",
    "email Sam and another on the 28th for a report",
    "review, mute my volume",
])
def test_fully_quoted_conjunction_is_literal_reminder_content(title):
    plan = compile_task(
        'set a reminder on october 20th for "' + title + '"', now=NOW,
    )
    assert plan is not None
    steps = plan_task(plan)
    assert len(steps) == 1
    assert steps[0].tool == "add_reminder"
    assert steps[0].args["title"] == title


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', [
    'Reminder updated: “Synthetic alpha” — Fri Oct 23 at 4:00 PM.',
    'Reminder updated: “Synthetic alpha” — Fri Oct 23 (all day).',
    'Nothing active matches that reminder.',
    '(error: synthetic native failure)',
    'Unrecognized completion text',
])
@pytest.mark.parametrize('dispatch', ['model', 'direct'])
async def test_update_answer_uses_actual_receipt_not_model_invention(monkeypatch, receipt, dispatch):
    from service.agent import loop
    from service.tools.registry import REGISTRY, Tool
    calls, events = [], []

    def upcoming():
        calls.append('read')
        return 'Synthetic alpha on Fri Oct 23; Synthetic beta on Sat Oct 24.'

    def update(**kwargs):
        calls.append('update')
        return receipt

    monkeypatch.setitem(REGISTRY, 'get_upcoming', Tool('get_upcoming', 'synthetic read',
        {'type':'object','properties':{}}, 'system_read', upcoming))
    monkeypatch.setitem(REGISTRY, 'update_reminder', Tool('update_reminder', 'synthetic update',
        {'type':'object','properties':{}}, 'system_write', update))

    class Client:
        calls = 0
        async def ensure_only(self, *args, **kwargs):
            pass
        async def stream_events(self, *args, **kwargs):
            self.calls += 1
            if dispatch == 'model' and self.calls <= 2:
                name = 'get_upcoming' if self.calls == 1 else 'update_reminder'
                message = {'role':'assistant', 'content':'', 'tool_calls':[{
                    'id':str(self.calls), 'function':{'name':name,'arguments':'{}'}}]}
            else:
                message = {'role':'assistant','content':'Updated Synthetic beta to Sat Oct 24 at 4 PM.'}
            yield {'kind':'final','message':message}

    async def emit(event):
        events.append(event)
    client = Client()
    result = await loop.run_agent(client, 'Agents-A1-4B-oQe6',
        [{'role':'user','content':'Change the reminder time.'}], emit,
        SimpleNamespace(confirm=AsyncMock(return_value=True)), tools=['get_upcoming','update_reminder'],
        direct_calls=[('get_upcoming',{}),('update_reminder',{})] if dispatch == 'direct' else None,
        multi_round=True, max_steps=5)
    assert calls == ['read','update']
    assert 'Updated Synthetic beta' not in result and 'Sat Oct 24 at 4 PM' not in result
    if receipt.startswith('Reminder updated:'):
        assert result == receipt
    elif receipt == 'Unrecognized completion text':
        assert 'couldn\'t verify' in result and receipt not in result
    else:
        assert 'not completed' in result or 'not return a verified success' in result
    assert events[-1]['type'] == 'done'


@pytest.mark.asyncio
@pytest.mark.parametrize('dry_run,missing_read,second_failure', [
    (True,False,False), (False,True,False), (False,False,True), (False,False,False),
])
async def test_update_receipts_preserve_partial_work_reads_and_dry_runs(
    monkeypatch, dry_run, missing_read, second_failure,
):
    from service.agent import loop
    from service.tools.registry import REGISTRY, Tool
    calls = []
    good = 'Reminder updated: “Synthetic alpha” — Fri Oct 23 (all day).'
    def update(**kwargs):
        calls.append('update')
        return '(error: synthetic failure)' if second_failure and len(calls) > 1 else good
    monkeypatch.setitem(REGISTRY, 'update_reminder', Tool('update_reminder', 'synthetic update',
        {'type':'object','properties':{'title':{'type':'string'}}}, 'system_write', update))
    monkeypatch.setitem(REGISTRY, 'get_battery_status', Tool('get_battery_status', 'synthetic read',
        {'type':'object','properties':{}}, 'system_read', lambda:'charge: 80%'))
    class Client:
        async def ensure_only(self, *args, **kwargs):
            pass
        async def stream_events(self, *args, **kwargs):
            yield {'kind':'final','message':{'role':'assistant','content':'All done, beta changed.'}}
    async def emit(event):
        pass
    direct = [('update_reminder',{'title':'alpha'})]
    if second_failure:
        direct.append(('update_reminder',{'title':'beta'}))
    elif not missing_read:
        direct.append(('get_battery_status',{}))
    result = await loop.run_agent(Client(), 'Agents-A1-4B-oQe6',
        [{'role':'user','content':'Change the reminder and check battery.'}], emit,
        SimpleNamespace(confirm=AsyncMock(return_value=True)),
        tools=['update_reminder','get_battery_status'], direct_calls=direct,
        required_tool_groups=(frozenset({'update_reminder'}),frozenset({'get_battery_status'})),
        multi_round=True, max_steps=2, test_mode=dry_run)
    assert 'beta changed' not in result
    if dry_run:
        assert not calls and 'Dry run only' in result and 'was changed' in result
    else:
        assert good in result and len(calls) == (2 if second_failure else 1)
        if missing_read or second_failure:
            assert "couldn't complete every requested step" in result
        else:
            assert 'charge: 80%' in result


@pytest.mark.asyncio
@pytest.mark.parametrize('read_result', [
    '(error: synthetic read unavailable)', 'Nothing active matches that battery read.',
])
async def test_required_read_failure_preserves_completed_reminder_change(monkeypatch, read_result):
    from service.agent import loop
    from service.tools.registry import REGISTRY, Tool
    calls = []
    receipt = 'Reminder updated: “Synthetic alpha” — Fri Oct 23 at 4 PM.'
    def update():
        calls.append('update')
        return receipt
    def read():
        calls.append('read')
        return read_result
    monkeypatch.setitem(REGISTRY, 'update_reminder', Tool('update_reminder', 'synthetic update',
        {'type':'object','properties':{}}, 'system_write', update))
    monkeypatch.setitem(REGISTRY, 'get_battery_status', Tool('get_battery_status', 'synthetic read',
        {'type':'object','properties':{}}, 'system_read', read))
    class NoModel:
        async def ensure_only(self, *args, **kwargs):
            raise AssertionError('terminal source failure must not invoke model')
    async def emit(event):
        pass
    result = await loop.run_agent(NoModel(), 'Agents-A1-4B-oQe6',
        [{'role':'user','content':'Change the reminder and check battery.'}], emit,
        SimpleNamespace(confirm=AsyncMock(return_value=True)),
        tools=['update_reminder','get_battery_status'],
        direct_calls=[('update_reminder',{}),('get_battery_status',{})],
        required_tool_groups=(frozenset({'update_reminder'}),frozenset({'get_battery_status'})),
        multi_round=True)
    assert calls == ['update','read']
    assert receipt in result and 'Required source result (get_battery_status)' in result
    assert "couldn't complete every requested step" in result
    assert 'didn’t prepare or send' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('dispatch', ['direct','model'])
@pytest.mark.parametrize('receipt', ['Reminder updated: “Synthetic alpha” — Fri Oct 23 (all day).',
                                    'Unrecognized completion text'])
async def test_single_call_update_shortcut_uses_same_receipt_verifier(monkeypatch, dispatch, receipt):
    from service.agent import loop
    from service.tools.registry import REGISTRY, Tool
    calls = []
    def update():
        calls.append('update')
        return receipt
    monkeypatch.setitem(REGISTRY,'update_reminder',Tool('update_reminder','synthetic update',
        {'type':'object','properties':{}},'system_read',update))
    class Client:
        calls = 0
        async def ensure_only(self,*args,**kwargs):
            pass
        async def stream_events(self,*args,**kwargs):
            self.calls += 1
            assert dispatch == 'model' and self.calls == 1
            yield {'kind':'final','message':{'role':'assistant','content':'','tool_calls':[
                {'id':'u','function':{'name':'update_reminder','arguments':'{}'}}]}}
    async def emit(event):
        pass
    client = Client()
    result = await loop.run_agent(client,'Agents-A1-4B-oQe6',
        [{'role':'user','content':'Change reminder.'}],emit,
        SimpleNamespace(confirm=AsyncMock(return_value=True)),tools=['update_reminder'],
        direct_calls=[('update_reminder',{})] if dispatch == 'direct' else None,
        short_circuit_tools={'update_reminder'})
    assert calls == ['update'] and client.calls == (1 if dispatch == 'model' else 0)
    assert result == receipt if receipt.startswith('Reminder updated:') else "couldn't verify" in result
