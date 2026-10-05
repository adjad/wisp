"""Fresh bounded identity/timezone/scorer/privacy and runtime controls."""
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
OUT=Path('/private/tmp/wisp-router-273d274-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-273d274-boundary-')).resolve()
e.install_guard(state,OUT)
import pytest
from scripts import run_simulation_qa as qa
from service.router.intent import planner
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
from tests import test_message_digest as t
from service.tools import assistant_tools as A,email_tools as E,message_digest as D
from service.workflows import present
from service.agent.loop import _merge_results
rows=[]
def record(identity,passed,**evidence):
    rows.append({'id':identity,'passed':bool(passed),**evidence})

value={'version':1,'kind':'read','sources':[{'domain':'email','operation':'records'}],'excluded_sources':[],'unsupported_constraints':[]}
planner.role_target=lambda role:TARGET
for name,configuration,mutate in [
    ('default-off',None,lambda c:None),('disabled',{'enabled':False},lambda c:None),
    ('wrong-origin',CONFIG,lambda c:setattr(c,'base_url','https://other.example.invalid')),
    ('wrong-provider',CONFIG,lambda c:setattr(c,'provider',SimpleNamespace(name='other'))),
    ('missing-transport',CONFIG,lambda c:setattr(c,'_credential_transport',None)),
    ('wrong-target-role',CONFIG,lambda c:setattr(c,'target',replace(TARGET,role='agent'))),
    ('nonresident',CONFIG,lambda c:setattr(c,'loaded',False)),
]:
    client=FakeClient(value,value);mutate(client)
    plan=asyncio.run(planner.plan_read('Read email',client=client,config=configuration))
    permitted_calls=['status'] if name=='nonresident' else []
    record(name,client.calls==permitted_calls and (plan is None or plan.disposition=='clarify'),client_calls=client.calls)
with patch.dict(os.environ,{'WISP_INTENT_ROUTER_KILL':'1'}):
    client=FakeClient(value)
    plan=asyncio.run(planner.plan_read('Read email',client=client,config=CONFIG))
    record('kill-switch',plan is None and not client.calls)
class Slow(FakeClient):
    async def chat(self,*args,**kwargs):
        self.calls.append('chat')
        try:await asyncio.sleep(3)
        finally:self.calls.append('cancelled')
client=Slow(value)
plan=asyncio.run(planner.plan_read('Read email',client=client,config={**CONFIG,'deadline_seconds':.05}))
record('one-overall-deadline',plan.disposition=='clarify' and client.calls==['status','chat','cancelled'],client_calls=client.calls)
class Block(FakeClient):
    async def status(self):await asyncio.sleep(3)
async def cancel():
    task=asyncio.create_task(planner.plan_read('Read email',client=Block(value),config=CONFIG))
    await asyncio.sleep(0);task.cancel()
    try:await task
    except asyncio.CancelledError:return True
    return False
record('outer-cancellation-propagates',asyncio.run(cancel()))
client=FakeClient('[]',value)
history=[{'role':'assistant','content':'Earlier [Tools: web_search]'},
         {'role':'tool','content':'UNTRUSTED_FIXTURE_BODY'},{'role':'system','content':'UNTRUSTED_SYSTEM'}]
plan=asyncio.run(planner.plan_read('Read email',client=client,config=CONFIG,context=history))
sent=json.dumps(client.context)
record('bounded-repair-clean-context',plan.disposition=='compiled' and plan.attempts==2 and client.calls==['status','chat','status','chat']
    and all(x not in sent for x in ('[Tools:','UNTRUSTED_FIXTURE_BODY','UNTRUSTED_SYSTEM')),client_calls=client.calls)

required={'tests/test_router_intent_core.py','tests/test_router_intent_main.py','tests/test_router_intent_workflow.py',
          'tests/test_router_overview_grounding.py','tests/test_router_overview_message_scope.py','tests/test_router_update_eval.py'}
record('six-modules-admitted',required<=set(qa._selected_tests(['full'])))
old_root,old_safe=qa.ROOT,qa.SAFE_FULL_TESTS
try:
    fake=state/'manifest';(fake/'tests').mkdir(parents=True);(fake/'tests/test_present.py').write_text('')
    qa.ROOT=fake
    for name,safe,needle in [('unknown-failclosed',set(),'unclassified tests'),
        ('missing-failclosed',{'tests/test_present.py','tests/test_absent.py'},'missing reviewed tests')]:
        qa.SAFE_FULL_TESTS=safe
        try:qa._selected_tests(['full']);record(name,False)
        except RuntimeError as exc:record(name,needle in str(exc))
finally:qa.ROOT,qa.SAFE_FULL_TESTS=old_root,old_safe
for host in ('UTC','America/Los_Angeles'):
    os.environ['TZ']=host;time.tzset()
    for clock,day,start,end,hours in [
        ('2026-03-08T22:40:00-07:00','2026-03-08','2026-03-08T00:00:00-08:00','2026-03-09T00:00:00-07:00',23),
        ('2026-11-01T22:40:00-08:00','2026-11-01','2026-11-01T00:00:00-07:00','2026-11-02T00:00:00-08:00',25)]:
        expected=[datetime.fromisoformat(start).timestamp(),datetime.fromisoformat(end).timestamp()]
        before=(os.environ.get('TZ'),time.localtime(expected[0]))
        actual=e.canonical_args('view_emails',{'period':'today'},{},clock=clock)
        exact=e.canonical_args('view_emails',{'day':day},{},clock=clock)
        wrong=e.canonical_args('view_emails',{'period':'tomorrow'},{},clock=clock)
        record('DST-'+host+'-'+day,actual==exact=={'_resolved_span':expected} and actual!=wrong
            and expected[1]-expected[0]==hours*3600 and before==(os.environ.get('TZ'),time.localtime(expected[0])),actual=actual)
for host in (None,'UTC'):
    if host is None:os.environ.pop('TZ',None)
    else:os.environ['TZ']=host
    time.tzset();before=(os.environ.get('TZ'),time.localtime(1772956800))
    try:
        with e.evaluation_timezone():raise LookupError('synthetic')
    except LookupError:pass
    record('TZ-restores-'+str(host),before==(os.environ.get('TZ'),time.localtime(1772956800)))
tzset=time.tzset;del time.tzset
try:
    try:
        with e.evaluation_timezone():pass
        record('missing-tzset-failclosed',False)
    except RuntimeError as exc:record('missing-tzset-failclosed','requires time.tzset' in str(exc))
finally:time.tzset=tzset
os.environ['TZ']='America/Los_Angeles';time.tzset()
for count in (True,1.0):
    case={'expected':{'calls':[{'name':'view_emails','args':{'count':1}}],'sources':['email']}}
    run={'executed_calls':[{'name':'view_emails','args':{'count':count}}],'events':[{'type':'done'}],'answer':'fixture'}
    metrics=e.score(case,run)['metrics']
    record('typed-scorer-'+type(count).__name__,metrics['exact_arguments'] and not metrics['request_argument'] and not metrics['guarded_end_to_end'],metrics=metrics)

for now,collision in [(1791222000.0,None),(1791234567.0,'1234'),(1791256782.0,'5678')]:
    with pytest.MonkeyPatch.context() as mp:
        t.isolated.__wrapped__(mp)
        t.test_distinct_redacted_requests_keep_both_source_occurrences(mp,now,collision)
        record('G1-repaired-test-'+str(collision),True,clock=now,
            coverage='Actual repaired test: two occurrences/positions/timestamps, cache selection, body/context/output/model/debug privacy, four summaries')
with pytest.MonkeyPatch.context() as mp:
    t.isolated.__wrapped__(mp)
    mp.setattr(t.M,'redact_summary_codes',lambda _:'Alex: Please review report. Verification code is 1234.')
    try:
        t.test_distinct_redacted_requests_keep_both_source_occurrences(mp,1791234567.0,'1234')
        record('G1-oracle-detects-text-leak',False)
    except AssertionError as exc:
        frames=traceback.extract_tb(exc.__traceback__)
        frame=frames[-1]
        record('G1-oracle-detects-text-leak','all(secret not in value' in (frame.line or ''),failing_line=frame.lineno,assertion=frame.line)

now=datetime(2026,10,5,12)
def event(title,offset,source='calendar',**kw):return {'title':title,'when_ts':now.timestamp()+offset,'source':source,'location':'',**kw}
agenda=A._format_forward_agenda([event('Fixture planning',3600),event('Fixture task',7200,'reminders'),
    event('Fixture joint item',10800,duplicate_sources=['reminders'])],now=now.timestamp(),window_label='Oct 5–11, 2026')
record('agenda-source-counts',agenda.count('Fixture joint item')==1 and '2 calendar events' in agenda.splitlines()[0]
    and '1 reminder' in agenda.splitlines()[0] and '[also Apple Reminder]' in agenda,output=agenda)
with patch.object(present,'sender_name',lambda:'Fixture Person'):
    output,attributed=present.render('calendar','Work calendar unavailable; this schedule may be incomplete.\n'+agenda)
    record('workflow-attribution-partial',attributed and 'Fixture Person' in output and '2 calendar events' in output
        and 'may be incomplete' in output and 'Apple Reminders deletion status' in output,output=output)
mail=[{'ts':now.timestamp(),'sender':'Mara','sender_address':'mara@example.invalid','account':'Lab','account_id':'Lab',
       'message_id':str(i),'subject':'Fixture update','unread':True} for i in range(3)]
digest=E.sender_digest(mail,'today')
record('mail-duplicate-counts',digest.count('“Fixture update”')==1 and '3 emails · 3 unread' in digest,output=digest)
partial=E.sender_digest([], 'today',scan_incomplete_accounts=['Lab'])
record('mail-partial-not-empty', 'total coverage is unknown' in partial and 'No emails found' not in partial,output=partial)
message=D.render(D.analyze([(now.timestamp(),'Mara',"Me: I'll bring tea tomorrow."),
    (now.timestamp()+1,'Mara','Mara: Please review the report.')],['Mara','You']),'today')
record('message-actor-grounding','You to Mara: commitment' in message and 'Mara to You: request' in message
    and 'Mara to You: commitment' not in message,output=message)
merged=_merge_results([('search_notes','Fixture note A'),('search_notes','Fixture note B'),('get_upcoming','Work calendar unavailable')])
record('merge-retains-independent-results',all(x in merged for x in ('Fixture note A','Fixture note B','Work calendar unavailable')),output=merged)
data={'exact_sha':'273d2748d01ed978b4e4fb56c8ee503b18143bc4','rows':rows,'total':len(rows),
    'passed':sum(x['passed'] for x in rows),'failed':[x['id'] for x in rows if not x['passed']],
    'heldout_payload_opened':False,'actual_model_calls':False,'native_tool_bodies_or_outbound_execution':False}
(OUT/'boundary-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
print(json.dumps({k:data[k] for k in ('total','passed','failed')}))
