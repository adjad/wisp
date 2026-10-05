"""Fresh exact-head runtime, privacy, scorer, timezone and rendering controls."""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-6df5200-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-6df5200-boundary-')).resolve()
e.install_guard(state,OUT)
import pytest
from scripts import run_simulation_qa as qa
from service.router.intent import planner,validate_intent,InvalidIntent
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
from tests import test_message_digest as t
from service.tools import assistant_tools as A,email_tools as E,message_digest as D
from service.workflows import present
from service.agent.loop import _merge_results
rows=[]
def record(identity,ok,**kw):rows.append({'id':identity,'passed':bool(ok),**kw})
value={'version':1,'kind':'read','sources':[{'domain':'email','operation':'records'}],'excluded_sources':[],'unsupported_constraints':[]}
planner.role_target=lambda role:TARGET
for name,config,mutate,expected in [
    ('default-disabled',None,lambda c:None,[]),
    ('explicit-disabled',{'enabled':False},lambda c:None,[]),
    ('wrong-origin',CONFIG,lambda c:setattr(c,'base_url','https://fixture.example.invalid'),[]),
    ('wrong-provider',CONFIG,lambda c:setattr(c,'provider',SimpleNamespace(name='fixture-other')),[]),
    ('missing-transport',CONFIG,lambda c:setattr(c,'_credential_transport',None),[]),
    ('wrong-role',CONFIG,lambda c:setattr(c,'target',replace(TARGET,role='agent')),[]),
    ('nonresident',CONFIG,lambda c:setattr(c,'loaded',False),['status'])]:
    client=FakeClient(value,value);mutate(client)
    result=asyncio.run(planner.plan_read('Read email',client=client,config=config))
    record(name,client.calls==expected and (result is None or result.disposition=='clarify'),calls=client.calls)
with patch.dict(os.environ,{'WISP_INTENT_ROUTER_KILL':'1'}):
    client=FakeClient(value);result=asyncio.run(planner.plan_read('Read email',client=client,config=CONFIG))
    record('kill-switch',result is None and not client.calls)
class Slow(FakeClient):
    async def chat(self,*a,**kw):
        self.calls.append('chat')
        try:await asyncio.sleep(2)
        finally:self.calls.append('cancelled')
client=Slow(value)
result=asyncio.run(planner.plan_read('Read email',client=client,config={**CONFIG,'deadline_seconds':.05}))
record('overall-deadline',result.disposition=='clarify' and client.calls==['status','chat','cancelled'],calls=client.calls)
class Block(FakeClient):
    async def status(self):await asyncio.sleep(2)
async def cancel():
    task=asyncio.create_task(planner.plan_read('Read email',client=Block(value),config=CONFIG))
    await asyncio.sleep(0);task.cancel()
    try:await task
    except asyncio.CancelledError:return True
    return False
record('outer-cancellation',asyncio.run(cancel()))
client=FakeClient('[]',value)
result=asyncio.run(planner.plan_read('Read email',client=client,config=CONFIG,context=[
    {'role':'assistant','content':'Earlier [Tools: web_search]'},
    {'role':'tool','content':'FAKE_TOOL_SECRET'},{'role':'system','content':'FAKE_SYSTEM_INJECTION'}]))
record('bounded-repair-context',result.disposition=='compiled' and result.attempts==2
    and client.calls==['status','chat','status','chat'] and all(x not in json.dumps(client.context)
    for x in ('[Tools:','FAKE_TOOL_SECRET','FAKE_SYSTEM_INJECTION')),calls=client.calls)
for field,invalid in [('version',True),('count',True),('count',1.0)]:
    bad={**value,'sources':[dict(value['sources'][0])]}
    if field=='version':bad[field]=invalid
    else:bad['sources'][0][field]=invalid
    try:validate_intent(bad,'Read one email',now=datetime(2026,10,5,12));record('typed-'+field+'-'+str(type(invalid)),False)
    except InvalidIntent:record('typed-'+field+'-'+str(type(invalid)),True)
required={'tests/test_router_intent_core.py','tests/test_router_intent_main.py','tests/test_router_intent_workflow.py',
    'tests/test_router_overview_grounding.py','tests/test_router_overview_message_scope.py','tests/test_router_update_eval.py'}
record('six-manifest-modules',required<=set(qa._selected_tests(['full'])))
old_root,old_safe=qa.ROOT,qa.SAFE_FULL_TESTS
try:
    fake=state/'manifest';(fake/'tests').mkdir(parents=True);(fake/'tests/test_admitted.py').write_text('')
    qa.ROOT=fake
    for name,safe,needle in [('unknown-failclosed',set(),'unclassified tests'),
        ('missing-failclosed',{'tests/test_admitted.py','tests/test_absent.py'},'missing reviewed tests')]:
        qa.SAFE_FULL_TESTS=safe
        try:qa._selected_tests(['full']);record(name,False)
        except RuntimeError as exc:record(name,needle in str(exc))
finally:qa.ROOT,qa.SAFE_FULL_TESTS=old_root,old_safe
for host in ('UTC','America/Los_Angeles'):
    os.environ['TZ']=host;time.tzset()
    for clock,day,start,end,hours in [
        ('2026-03-08T20:10:00-07:00','2026-03-08','2026-03-08T00:00:00-08:00','2026-03-09T00:00:00-07:00',23),
        ('2026-11-01T20:10:00-08:00','2026-11-01','2026-11-01T00:00:00-07:00','2026-11-02T00:00:00-08:00',25)]:
        expected=[datetime.fromisoformat(start).timestamp(),datetime.fromisoformat(end).timestamp()]
        before=(os.environ.get('TZ'),time.localtime(expected[0]))
        actual=e.canonical_args('view_messages',{'period':'today'},{},clock=clock)
        exact=e.canonical_args('view_messages',{'day':day},{},clock=clock)
        wrong=e.canonical_args('view_messages',{'period':'tomorrow'},{},clock=clock)
        record('DST-'+host+'-'+day,actual==exact=={'_resolved_span':expected} and actual!=wrong
            and expected[1]-expected[0]==hours*3600 and before==(os.environ.get('TZ'),time.localtime(expected[0])),span=actual)
for host in (None,'UTC'):
    if host is None:os.environ.pop('TZ',None)
    else:os.environ['TZ']=host
    time.tzset();before=(os.environ.get('TZ'),time.localtime(1772956800))
    try:
        with e.evaluation_timezone():raise LookupError('fixture')
    except LookupError:pass
    record('TZ-restoration-'+str(host),before==(os.environ.get('TZ'),time.localtime(1772956800)))
tzset=time.tzset;del time.tzset
try:
    try:
        with e.evaluation_timezone():pass
        record('tzset-failclosed',False)
    except RuntimeError as exc:record('tzset-failclosed','requires time.tzset' in str(exc))
finally:time.tzset=tzset
os.environ['TZ']='America/Los_Angeles';time.tzset()
for count in (True,1.0):
    case={'expected':{'calls':[{'name':'view_emails','args':{'count':1}}],'sources':['email']}}
    run={'executed_calls':[{'name':'view_emails','args':{'count':count}}],'events':[{'type':'done'}],'answer':'fixture'}
    metrics=e.score(case,run)['metrics']
    record('P3-'+str(type(count)),metrics['exact_arguments'] and not metrics['request_argument'] and not metrics['guarded_end_to_end'],metrics=metrics)
for now,collision in [(1791222000.0,None),(1791234567.0,'1234'),(1791256782.0,'5678')]:
    with pytest.MonkeyPatch.context() as mp:
        t.isolated.__wrapped__(mp)
        t.test_distinct_redacted_requests_keep_both_source_occurrences(mp,now,collision)
        record('G1-'+str(collision),True,clock=now,scope='Actual repaired target test: timestamps/positions/cardinality/cache/text/model/debug/four summaries')
with pytest.MonkeyPatch.context() as mp:
    t.isolated.__wrapped__(mp)
    mp.setattr(t.M,'redact_summary_codes',lambda _:'Alex: Please review report. Verification code is 1234.')
    try:t.test_distinct_redacted_requests_keep_both_source_occurrences(mp,1791256782.0,'5678');record('G1-leak-oracle',False)
    except AssertionError as exc:
        frame=traceback.extract_tb(exc.__traceback__)[-1]
        record('G1-leak-oracle','all(secret not in value' in (frame.line or ''),line=frame.lineno,assertion=frame.line)
now=datetime(2026,10,5,12)
def event(title,delta,source='calendar',**kw):return {'title':title,'when_ts':now.timestamp()+delta,'source':source,'location':'',**kw}
agenda=A._format_forward_agenda([event('Fixture meeting',3600),event('Fixture reminder',7200,'reminders'),
    event('Fixture overlap',10800,duplicate_sources=['reminders'])],now=now.timestamp(),window_label='Oct 5–11, 2026')
record('agenda-count-provenance',agenda.count('Fixture overlap')==1 and '2 calendar events' in agenda.splitlines()[0]
    and '1 reminder' in agenda.splitlines()[0] and '[also Apple Reminder]' in agenda,output=agenda)
with patch.object(present,'sender_name',lambda:'Fixture Owner'):
    output,attributed=present.render('calendar','Work calendar unavailable; schedule may be incomplete.\n'+agenda)
    record('workflow-attribution',attributed and 'Fixture Owner' in output and 'may be incomplete' in output
        and '2 calendar events' in output and 'Apple Reminders deletion status' in output,output=output)
mail=[{'ts':now.timestamp(),'sender':'Nia','sender_address':'nia@example.invalid','account':'Lab','account_id':'Lab',
       'message_id':str(i),'subject':'Fixture subject','unread':True} for i in range(4)]
digest=E.sender_digest(mail,'today')
record('email-fold-counts',digest.count('“Fixture subject”')==1 and '4 emails · 4 unread' in digest,output=digest)
partial=E.sender_digest([], 'today',scan_incomplete_accounts=['Lab'])
record('email-partial-coverage','total coverage is unknown' in partial and 'No emails found' not in partial,output=partial)
messages=D.render(D.analyze([(now.timestamp(),'Nia',"Me: I'll bring soup tomorrow."),
    (now.timestamp()+1,'Nia','Nia: Please review the report.')],['Nia','You']),'today')
record('message-actor-direction','You to Nia: commitment' in messages and 'Nia to You: request' in messages
    and 'Nia to You: commitment' not in messages,output=messages)
merged=_merge_results([('search_notes','Fixture result one'),('search_notes','Fixture result two'),('get_upcoming','Calendar unavailable')])
record('result-merge-preserves-sources',all(x in merged for x in ('Fixture result one','Fixture result two','Calendar unavailable')),output=merged)
data={'exact_sha':'6df5200d0945ee3b050bcde4115a338e392017b1','rows':rows,'total':len(rows),'passed':sum(r['passed'] for r in rows),
    'failed':[r['id'] for r in rows if not r['passed']], 'heldout_payload_opened':False,'actual_model_calls':False,'native_tool_bodies_or_outbound_execution':False}
(OUT/'boundary-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
print(json.dumps({k:data[k] for k in ('total','passed','failed')}))
