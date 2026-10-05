"""Fresh bounded helper checks; no archived executions, native tools or model I/O."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-27490ae-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-27490ae-boundary-')).resolve()
e.install_guard(state,OUT)
from scripts import run_simulation_qa as qa
from service.router.intent import planner
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
from service.workflows.compiler import outbound_verb,compile_new
rows=[]
def check(identity,ok,**extra):
    rows.append({'id':identity,'passed':bool(ok),**extra})
    assert ok, (identity,extra)
required={'tests/test_router_intent_core.py','tests/test_router_intent_main.py',
    'tests/test_router_intent_workflow.py','tests/test_router_overview_grounding.py',
    'tests/test_router_overview_message_scope.py','tests/test_router_update_eval.py'}
check('manifest-six-admitted',required<=set(qa._selected_tests(['full'])))
saved_root,saved_safe=qa.ROOT,qa.SAFE_FULL_TESTS
try:
    fake=state/'manifest'
    (fake/'tests').mkdir(parents=True)
    (fake/'tests/test_present.py').write_text('')
    qa.ROOT=fake
    for name,safe,diagnosis in [('unknown-fails-closed',set(),'unclassified tests'),
        ('missing-fails-closed',{'tests/test_present.py','tests/test_missing.py'},'missing reviewed tests')]:
        qa.SAFE_FULL_TESTS=safe
        try:
            qa._selected_tests(['full'])
            raise AssertionError('manifest drift accepted')
        except RuntimeError as error:
            check(name,diagnosis in str(error))
finally:
    qa.ROOT,qa.SAFE_FULL_TESTS=saved_root,saved_safe
for host in ('UTC','America/Los_Angeles'):
    os.environ['TZ']=host;time.tzset()
    for clock,day,start,end,hours in [
        ('2026-03-08T21:20:00-07:00','2026-03-08','2026-03-08T00:00:00-08:00','2026-03-09T00:00:00-07:00',23),
        ('2026-11-01T21:20:00-08:00','2026-11-01','2026-11-01T00:00:00-07:00','2026-11-02T00:00:00-08:00',25)]:
        expected=[datetime.fromisoformat(start).timestamp(),datetime.fromisoformat(end).timestamp()]
        previous=(os.environ.get('TZ'),time.localtime(expected[0]))
        actual=e.canonical_args('view_messages',{'period':'today'},{},clock=clock)
        exact=e.canonical_args('view_messages',{'day':day},{},clock=clock)
        different=e.canonical_args('view_messages',{'period':'tomorrow'},{},clock=clock)
        check('dst-'+host+'-'+day,actual==exact=={'_resolved_span':expected} and actual!=different
            and expected[1]-expected[0]==hours*3600 and previous==(os.environ.get('TZ'),time.localtime(expected[0])),
            exact_span=expected,hours=hours)
for host in ('UTC',None):
    if host is None:os.environ.pop('TZ',None)
    else:os.environ['TZ']=host
    time.tzset()
    previous=(os.environ.get('TZ'),time.localtime(1772956800))
    try:
        with e.evaluation_timezone():raise LookupError('synthetic error')
    except LookupError:pass
    check('tz-restored-on-error-'+str(host),previous==(os.environ.get('TZ'),time.localtime(1772956800)))
tzset=time.tzset;del time.tzset
try:
    previous=os.environ.get('TZ')
    try:
        with e.evaluation_timezone():raise AssertionError('tzset required')
    except RuntimeError as error:check('tzset-missing-fails-closed','requires time.tzset' in str(error) and os.environ.get('TZ')==previous)
finally:time.tzset=tzset
os.environ['TZ']='America/Los_Angeles';time.tzset()
for number in (True,1.0):
    case={'expected':{'calls':[{'name':'view_emails','args':{'count':1}}],'sources':['email']}}
    run={'executed_calls':[{'name':'view_emails','args':{'count':number}}],'events':[{'type':'done'}],'answer':'synthetic'}
    metrics=e.score(case,run)['metrics']
    check('typed-scorer-'+type(number).__name__,metrics['exact_arguments'] and not metrics['request_argument'] and not metrics['guarded_end_to_end'],metrics=metrics)
value={'version':1,'kind':'read','sources':[{'domain':'email','operation':'records'}],
    'excluded_sources':[],'unsupported_constraints':[]}
planner.role_target=lambda role:TARGET
for name,config,mutate in [('absent-config',None,lambda c:None),
    ('wrong-origin',CONFIG,lambda c:setattr(c,'base_url','https://wrong.example.invalid')),
    ('wrong-provider',CONFIG,lambda c:setattr(c,'provider',SimpleNamespace(name='wrong'))),
    ('missing-transport',CONFIG,lambda c:setattr(c,'_credential_transport',None))]:
    client=FakeClient(value,value);mutate(client)
    result=asyncio.run(planner.plan_read('Read email',client=client,config=config,now=datetime(2026,10,5,12)))
    check(name,not client.calls and (result is None or result.disposition=='clarify'),client_calls=client.calls)
previous=os.environ.get('WISP_INTENT_ROUTER_KILL')
try:
    os.environ['WISP_INTENT_ROUTER_KILL']='1'
    client=FakeClient(value)
    result=asyncio.run(planner.plan_read('Read email',client=client,config=CONFIG))
    check('kill-switch',result is None and not client.calls)
finally:
    if previous is None:os.environ.pop('WISP_INTENT_ROUTER_KILL',None)
    else:os.environ['WISP_INTENT_ROUTER_KILL']=previous
for prompt in ('Summarize recent text messages','Show text messages from Selene','What did people text me?'):
    check('message-read-'+prompt,not outbound_verb(prompt) and compile_new(prompt) is None)
for prompt in ('Text Mom my calendar summary','Send my email summary to Mom via Messages'):
    check('delivery-retained-'+prompt,outbound_verb(prompt) and compile_new(prompt) is not None)
for prompt in ('Do not text Mom my calendar summary','Explain the phrase "text Mom my calendar summary"'):
    check('quoted-negated-'+prompt,compile_new(prompt) is None)
data={'exact_sha':'27490ae5055502423f187e84ca52d0064b794396','rows':rows,
    'total':len(rows),'passed':sum(r['passed'] for r in rows),'heldout_payload_opened':False,
    'actual_model_calls':False,'native_or_outbound_execution':False}
(OUT/'boundary-results.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps({'total':data['total'],'passed':data['passed']}))
