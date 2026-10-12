"""Production prepare_turn must not mistake file text for a message recipient."""
import socket
import subprocess
import asyncio
import copy
import datetime as dt
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.memory.store import SessionStore
from service.workflows.compiler import _file_directed_write, compile_new
from service.workflows.engine import prepare_turn
from service.workflows import executor  # Import before per-test registry guards.


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Compiler regressions may not call native/network/model tools")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("service.tools.registry.run_tool", forbidden)


@pytest.mark.parametrize("prompt", [
    "Write the text Synthetic body to /private/tmp/wisp-pilot-only-synthetic.txt",
    "Please write Synthetic body to ./scratch.txt",
    "Could you write the text Synthetic body into ../scratch.md",
    "Write Synthetic body to ~/Documents/scratch.txt",
    "Write Synthetic body to scratch.txt",
    'Write Synthetic body to "/private/tmp/fictional notes.txt"',
    'Write "Synthetic body" to "fictional notes.md"',
    "Write to /private/tmp/fictional.txt the text Synthetic body",
    "Write a text file with Synthetic body",
    "Draft a markdown file containing Synthetic body",
    "Write Synthetic body into a file",
    "Don't write Synthetic body to /private/tmp/fictional.txt",
    "Write Synthetic body to /private/tmp/fictional.txt and email Mom my calendar summary",
    "Write Synthetic body to /private/tmp/fictional.txt; text Mom when finished",
])
def test_file_authorship_does_not_allocate_delivery_workflow(tmp_path, prompt):
    assert _file_directed_write(prompt)
    assert compile_new(prompt) is None
    store = SessionStore(tmp_path / "synthetic.db")
    sid = store.create_session()
    assert prepare_turn(store, sid, prompt) is None
    assert store.latest_workflow(sid) is None


@pytest.mark.parametrize("prompt", [
    "Text Mom a summary of my calendar tomorrow",
    "Send Mom a summary of my calendar tomorrow via Messages",
    "Draft an email to Mom with a summary of my calendar tomorrow",
    'Text Mom "write Synthetic body to /private/tmp/fictional.txt"',
    'Write Mom a message saying "save this to /private/tmp/fictional.txt"',
    "Write an email to Mom about the file /private/tmp/fictional.txt",
    "Draft a text to Mom with my email summary",
    "Write a text to Dr.Smith containing my calendar summary",
    "Write an email to jane@example.com about how to /private/tmp/fictional.txt",
    "Write Mom a message about how to /private/tmp/fictional.txt",
    "Do not text Mom the file /private/tmp/fictional.txt",
    "Text Mom a summary and write it to /private/tmp/fictional.txt",
])
def test_explicit_communications_not_consumed_by_leading_file_grammar(prompt):
    assert not _file_directed_write(prompt)


def test_grounded_message_workflow_preserves_destination_sources_and_period(tmp_path):
    store = SessionStore(tmp_path / "synthetic.db")
    sid = store.create_session()
    turn = prepare_turn(store, sid, "Send Mom a summary of my calendar tomorrow via Messages")
    assert turn and turn.plan.recipient == "Mom" and turn.plan.channel == "messages"
    assert turn.plan.sources == ["calendar"]
    assert turn.plan.source_args == {"calendar": {"period": "tomorrow"}}
    assert turn.plan.delivery == "send" and turn.plan.status == "running"
    assert turn.decision and "send_message" in turn.decision.tool_subset


@pytest.mark.parametrize('prompt,args', [
    ('Text Mom my free windows tomorrow', {'period':'tomorrow','minutes':5}),
    ('Draft Maya an email with my availability this week', {'period':'this week','minutes':5}),
    ('Text Mom my free time tomorrow morning', {'period':'tomorrow','minutes':5,'day_start':9,'day_end':12}),
    ('Text Mom my free time tomorrow morning and afternoon', {'period':'tomorrow','minutes':5,'day_start':9,'day_end':18}),
    ('Text Mom my free time tomorrow after 2PM', {'period':'tomorrow','minutes':5,'day_start':14,'day_end':24}),
    ('Send my availability tomorrow after 8 am to morgan@example.com', {'period':'tomorrow','minutes':5,'day_start':8,'day_end':24}),
    ('Send my availability tomorrow before 9 pm to morgan@example.com', {'period':'tomorrow','minutes':5,'day_start':0,'day_end':21}),
])
def test_production_availability_compilation_preserves_source_constraints(tmp_path,prompt,args):
    from service.workflows.compiler import source_call
    store = SessionStore(tmp_path/'synthetic.db'); sid=store.create_session()
    turn = prepare_turn(store,sid,prompt)
    assert turn and turn.plan.status == 'running'
    assert turn.plan.sources == ['calendar']
    assert source_call(turn.plan,'calendar') == ('find_free_time',args)
    assert turn.decision.direct_calls[0] == ('find_free_time',args)
    assert turn.decision.tool_argument_bindings['find_free_time'] == args
    assert '_workflow_availability' not in args


@pytest.fixture
def delivery_sources(monkeypatch):
    """Actual checked availability tool, fictional coherent Calendar generation."""
    from service.tools import schedule_extras as S
    from service.workflows import executor as E
    from service.safety.policy import Tier
    zone=ZoneInfo('America/Los_Angeles')
    now=dt.datetime(2026,10,10,9,tzinfo=zone)
    state=SimpleNamespace(calls=[], approvals=[], rows=[], stale=False, allow=True)
    class Clock(dt.datetime):
        @classmethod
        def now(cls,tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    state.clock=Clock
    monkeypatch.setattr(S,'dt',SimpleNamespace(datetime=Clock,time=dt.time,timedelta=dt.timedelta))
    monkeypatch.setattr(S,'_availability_timezone',lambda:zone.key)
    receipt={'available':True,'syncing':False,'last_sync':now.timestamp(),
        'snapshot_started_at':now.timestamp(),'diagnostics':{
        'coverage_start':dt.datetime(2026,10,1,tzinfo=zone).timestamp(),
        'coverage_end':dt.datetime(2026,12,1,tzinfo=zone).timestamp()}}
    def snapshot(day,timezone):
        info=copy.deepcopy(receipt)
        if state.stale: info['last_sync']=0
        return {'sources':{'calendar':info},'commitments':copy.deepcopy(state.rows),'tasks':[]}
    monkeypatch.setattr(S,'_store',SimpleNamespace(today_snapshot=snapshot))
    monkeypatch.setattr('service.assistant.sync_status.ensure_sources',AsyncMock(return_value={
        'sources':[{'id':'calendar','state':'ready'},{'id':'reminders','state':'ready'}],
        'reminders_fresh':True}))
    async def run(tool,args):
        state.calls.append((tool.name,dict(args)))
        if tool.name == 'find_free_time':
            result=tool.func(**args)
            return await result if inspect.isawaitable(result) else result
        if tool.name == 'get_upcoming': return 'Fictional Calendar: Synthetic workshop tomorrow 10 AM.'
        if tool.name == 'summarize_emails': return 'Fictional Mail: Synthetic invoice received today.'
        if tool.name == 'send_message': return 'Message sent to the approved recipient.'
        if tool.name == 'send_email': return 'Email sent to the approved recipient.'
        if tool.name == 'draft_email': return 'Draft opened in Mail.'
        if tool.name == 'schedule_send': return 'Scheduled: synthetic approved delivery.'
        raise AssertionError(f'Unexpected tool {tool.name}')
    monkeypatch.setattr(E,'run_tool',run)
    monkeypatch.setattr(E,'resolve_destination',lambda *a:('synthetic@example.com',''))
    monkeypatch.setattr(E,'decide',lambda category,args,tool:SimpleNamespace(tier=Tier.ALLOW,reason='isolated fictional test'))
    monkeypatch.setattr('service.workflows.present.sender_name',lambda:'Synthetic Sender')
    async def confirm(card):
        state.approvals.append(copy.deepcopy(card)); return state.allow
    state.approver=SimpleNamespace(confirm=confirm)
    return state


def execute_synthetic_delivery(tmp_path,prompt,state,*,dry_run=False):
    from service.workflows.executor import execute_workflow
    store=SessionStore(tmp_path/'synthetic.db'); sid=store.create_session()
    turn=prepare_turn(store,sid,prompt)
    assert turn and turn.plan.status=='running'
    async def emit(item): pass
    result=asyncio.run(execute_workflow(turn.plan,emit,state.approver,
        test_mode=dry_run,store=store,session_id=sid))
    return turn,result


@pytest.mark.parametrize('dry_run',[False,True])
def test_both_executor_paths_use_checked_availability_without_internal_metadata(tmp_path,delivery_sources,dry_run):
    state=delivery_sources
    turn,result=execute_synthetic_delivery(tmp_path,'Text Mom my free time tomorrow after 2PM',state,dry_run=dry_run)
    assert result.status == ('planned' if dry_run else 'completed')
    assert result.tool_calls[0]['name']=='find_free_time'
    assert result.tool_calls[0]['args']=={'period':'tomorrow','minutes':5,'day_start':14,'day_end':24}
    assert all('_workflow_availability' not in call['args'] for call in result.tool_calls)
    if dry_run:
        assert not state.calls and not state.approvals
    else:
        assert [name for name,args in state.calls]==['find_free_time','send_message']
        assert len(state.approvals)==1
        assert '2026-10-11' in state.approvals[0]['args']['text']
        assert '14:00' in state.approvals[0]['args']['text']
        assert turn.plan.sources==['calendar']


@pytest.mark.parametrize('prompt', [
    'Text Mom my free time tomorrow after 2:30PM',
    'Text Mom my free time today and tomorrow',
    'Send Alice\'s availability tomorrow to morgan@example.com',
    'Text Mom my free time tomorrow morning after 10AM',
    'Text Mom my free time tomorrow for an hour',
    'Schedule an email to Maya tomorrow at 6PM with my availability',
    'Send an email containing my availability tomorrow to Mom via Messages',
])
def test_unsupported_availability_never_delivers(tmp_path,delivery_sources,prompt):
    state=delivery_sources
    turn=prepare_turn(SessionStore(tmp_path/'preview.db'),'nonexistent',prompt,persist=False)
    assert turn
    if turn.plan.status!='running':
        assert turn.plan.content_error
        return
    _,result=execute_synthetic_delivery(tmp_path,prompt,state)
    assert result.status=='failed'
    assert not state.approvals
    assert all(name not in {'send_message','send_email','draft_email'} for name,args in state.calls)


@pytest.mark.parametrize('problem',['stale','missing_end','denied'])
def test_unverified_availability_or_denied_preview_has_no_effect(tmp_path,delivery_sources,problem):
    state=delivery_sources
    state.stale=problem=='stale'; state.allow=problem!='denied'
    if problem=='missing_end':
        state.rows=[{'id':'synthetic.cal','source':'calendar','title':'Unknown end',
            'when_ts':dt.datetime(2026,10,11,10,tzinfo=ZoneInfo('America/Los_Angeles')).timestamp(),
            'end_ts':None,'all_day':False,'status':'active'}]
    _,result=execute_synthetic_delivery(tmp_path,'Text Mom my free time tomorrow',state)
    assert result.status==('denied' if problem=='denied' else 'failed')
    assert [name for name,args in state.calls]==['find_free_time']
    assert len(state.approvals)==(1 if problem=='denied' else 0)


@pytest.mark.parametrize('prompt,names', [
    ('Text Mom a summary of my calendar tomorrow',['get_upcoming','send_message']),
    ('Text Mom a summary of my emails today',['summarize_emails','send_message']),
])
@pytest.mark.parametrize('dry_run',[False,True])
def test_source_dispatch_keeps_other_reads_unchanged(tmp_path,delivery_sources,prompt,names,dry_run):
    _,result=execute_synthetic_delivery(tmp_path,prompt,delivery_sources,dry_run=dry_run)
    assert result.status==('planned' if dry_run else 'completed')
    assert [call['name'] for call in result.tool_calls]==(names[:1] if dry_run else names)
    assert all('_workflow_availability' not in call['args'] for call in result.tool_calls)


@pytest.mark.parametrize('prompt',[
    'Give a briefing of the next two days plus today messages and email.',
    'What did Mom text today?',
    'What did my Dad email me yesterday?',
    'Did Maya message me today?',
    'Show me my email and calendar today',
])
def test_read_nouns_and_retrospective_communications_do_not_allocate_outbound(tmp_path,prompt):
    from service.workflows.compiler import outbound_verb
    assert not outbound_verb(prompt)
    store=SessionStore(tmp_path/'synthetic.db'); sid=store.create_session()
    assert compile_new(prompt) is None
    assert prepare_turn(store,sid,prompt) is None
    assert store.latest_workflow(sid) is None


@pytest.mark.parametrize('prompt',[
    'Email Mom a summary of my calendar tomorrow',
    'Draft Maya an email with my calendar summary tomorrow',
    'Show my calendar tomorrow and email Mom a summary',
    'What did Mom text today, and email Maya a summary of my calendar tomorrow',
    'Show my email and calendar. Email Mom a summary of my calendar tomorrow.',
    'Show my email and calendar, email Mom a summary of my calendar tomorrow.',
    'Show my email and calendar. Then email Mom a summary of my calendar tomorrow.',
    'Show my email and calendar\nEmail Mom a summary of my calendar tomorrow.',
    'Show my email and calendar while email Mom a summary of my calendar tomorrow.',
])
def test_actual_delivery_clauses_are_not_suppressed_as_read_questions(prompt):
    from service.workflows.compiler import outbound_verb
    assert outbound_verb(prompt)
    assert compile_new(prompt) is not None


def test_prohibited_communication_does_not_allocate_a_delivery():
    assert compile_new('Do not email Mom my calendar summary tomorrow') is None


@pytest.mark.parametrize('daypart',['morning','afternoon'])
def test_availability_source_period_and_dispatch_are_independent(tmp_path,daypart):
    from service.workflows.compiler import source_call
    prompt=f'Send my availability tomorrow {daypart} to morgan@example.com to go out today at 8 pm'
    store=SessionStore(tmp_path/'synthetic.db'); sid=store.create_session()
    turn=prepare_turn(store,sid,prompt)
    assert turn and turn.plan.status=='running'
    assert turn.plan.delivery=='scheduled' and turn.plan.when=='today at 8 pm'
    name,args=source_call(turn.plan,'calendar')
    assert name=='find_free_time' and args['period']=='tomorrow'
    assert (args['day_start'],args['day_end'])==((9,12) if daypart=='morning' else (12,18))
    assert not any(key.startswith('_workflow') for key in args)


def test_scheduled_availability_executes_explicit_dispatch_span(tmp_path,delivery_sources,monkeypatch):
    state=delivery_sources
    monkeypatch.setattr('service.tools.timeranges.datetime',state.clock)
    monkeypatch.setattr(executor,'datetime',state.clock)
    _,result=execute_synthetic_delivery(tmp_path,
        'Send my availability tomorrow afternoon to morgan@example.com to go out today at 8 pm',state)
    assert result.status=='completed'
    assert [name for name,args in state.calls]==['find_free_time','schedule_send']
    args=state.calls[-1][1]
    assert dt.datetime.fromisoformat(args['when']).replace(tzinfo=None)==dt.datetime(2026,10,10,20)
    approved=state.approvals[0]['args']
    assert {key:value for key,value in args.items() if key!='when'} == {
        key:value for key,value in approved.items() if key!='when'}
    assert dt.datetime.fromisoformat(args['when']).timestamp()==dt.datetime.fromisoformat(approved['when']).timestamp()
    assert '2026-10-11' in args['body']


def test_empty_availability_stays_fail_closed_without_structured_completion_receipt(tmp_path,delivery_sources):
    state=delivery_sources; zone=ZoneInfo('America/Los_Angeles')
    state.rows=[{'id':'synthetic.busy','source':'calendar','title':'Busy day',
        'when_ts':dt.datetime(2026,10,11,9,tzinfo=zone).timestamp(),
        'end_ts':dt.datetime(2026,10,11,18,tzinfo=zone).timestamp(),
        'all_day':False,'status':'active'}]
    _,result=execute_synthetic_delivery(tmp_path,'Text Mom my free time tomorrow',state)
    assert result.status=='failed'
    assert 'No gaps' in result.response
    assert len(state.calls)==1 and not state.approvals
