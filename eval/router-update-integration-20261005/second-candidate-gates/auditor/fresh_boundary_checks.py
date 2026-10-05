"""Independent timezone, manifest, schema and pre-I/O checks in isolation."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT = Path('/private/tmp/wisp-router-40ee88a-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-40ee88a-boundary-')).resolve()
e.install_guard(state,OUT)
from scripts import run_simulation_qa as qa
from service.router.intent import planner, validate_intent, InvalidIntent
from service.router.intent.validation import applicable_read
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET

records=[]
def check(identity, condition, **evidence):
    records.append({'id':identity,'passed':bool(condition),**evidence})
    assert condition, (identity,evidence)

for host in ('UTC','America/Los_Angeles'):
    os.environ['TZ']=host
    time.tzset()
    for day,clock,start,end,hours in [
        ('2026-03-08','2026-03-08T23:15:00-07:00','2026-03-08T00:00:00-08:00','2026-03-09T00:00:00-07:00',23),
        ('2026-11-01','2026-11-01T23:15:00-08:00','2026-11-01T00:00:00-07:00','2026-11-02T00:00:00-08:00',25),
    ]:
        expected=[datetime.fromisoformat(start).timestamp(),datetime.fromisoformat(end).timestamp()]
        before=(os.environ.get('TZ'),time.localtime(expected[0]))
        a=e.canonical_args('view_emails',{'period':'today'},{},clock=clock)
        b=e.canonical_args('view_emails',{'day':day},{},clock=clock)
        naive=e.canonical_args('view_emails',{'period':'today'},{},clock=clock[:19])
        wrong=e.canonical_args('view_emails',{'period':'tomorrow'},{},clock=clock)
        issues=e.runtime_constraint_issues({'clock':clock,'expected':{'calls':[]}},
            [{'name':'get_upcoming','args':{'period':'today'}}])
        check('dst-'+host+'-'+day,a==b==naive=={'_resolved_span':expected} and a!=wrong
            and expected[1]-expected[0]==hours*3600 and not issues
            and before==(os.environ.get('TZ'),time.localtime(expected[0])),
            expected_span=expected,actual=a,hours=hours,host_timezone_restored=True)
for host in ('UTC','America/Los_Angeles',None):
    if host is None:
        os.environ.pop('TZ',None)
    else:
        os.environ['TZ']=host
    time.tzset()
    before=(os.environ.get('TZ'),time.localtime(1772956800))
    try:
        with e.evaluation_timezone():
            raise LookupError('synthetic failure')
    except LookupError:
        pass
    check('exception-restores-'+str(host),before==(os.environ.get('TZ'),time.localtime(1772956800)))
tzset=time.tzset
del time.tzset
try:
    previous=os.environ.get('TZ')
    try:
        with e.evaluation_timezone():
            raise AssertionError('missing tzset accepted')
    except RuntimeError as error:
        check('missing-tzset-fails-closed', 'requires time.tzset' in str(error) and os.environ.get('TZ')==previous)
finally:
    time.tzset=tzset
os.environ['TZ']='America/Los_Angeles'
time.tzset()

admitted={'tests/test_router_intent_core.py','tests/test_router_intent_main.py',
    'tests/test_router_intent_workflow.py','tests/test_router_overview_grounding.py',
    'tests/test_router_overview_message_scope.py','tests/test_router_update_eval.py'}
selected=qa._selected_tests(['full'])
check('six-modules-admitted',admitted<=set(selected),selected_count=len(selected))
saved_root,saved_safe=qa.ROOT,qa.SAFE_FULL_TESTS
try:
    fake=state/'manifest'
    (fake/'tests').mkdir(parents=True)
    (fake/'tests/test_present.py').write_text('')
    qa.ROOT=fake
    for identity,reviewed,substring in [
        ('unknown-module-fails-closed',set(),'unclassified tests'),
        ('missing-module-fails-closed',{'tests/test_present.py','tests/test_missing.py'},'missing reviewed tests'),
    ]:
        qa.SAFE_FULL_TESTS=reviewed
        try:
            qa._selected_tests(['full'])
            raise AssertionError('unsafe drift accepted')
        except RuntimeError as error:
            check(identity,substring in str(error))
finally:
    qa.ROOT,qa.SAFE_FULL_TESTS=saved_root,saved_safe

value={'version':1,'kind':'read','sources':[{'domain':'email','operation':'records'}],
       'excluded_sources':[],'unsupported_constraints':[]}
planner.role_target=lambda role:TARGET
for identity,configuration,mutator in [
    ('default-disabled',None,lambda c:None),
    ('wrong-origin',CONFIG,lambda c:setattr(c,'base_url','https://wrong.example.invalid')),
    ('missing-transport',CONFIG,lambda c:setattr(c,'_credential_transport',None)),
    ('wrong-provider',CONFIG,lambda c:setattr(c,'provider',SimpleNamespace(name='other'))),
]:
    client=FakeClient(value,value)
    mutator(client)
    result=asyncio.run(planner.plan_read('Read email',client=client,config=configuration,
        now=datetime(2026,10,5,12)))
    check(identity,not client.calls and (result is None or result.disposition=='clarify'),client_calls=client.calls)
for prompt in ('Send an email to Elara','Create a reminder tomorrow','Forward my email to Tobias',
    'Read email and delete it','Draft an email to Elara'):
    check('action-not-read-'+prompt,not applicable_read(prompt))

for identity,case in [
    ('version-bool-rejected',{**value,'version':True}),
    ('numeric-count-bool-rejected',{**value,'sources':[{'domain':'email','operation':'records','count':True}]}),
    ('excluded-source-rejected',{**value,'sources':[{'domain':'email','operation':'records'},
        {'domain':'notes','operation':'records'}],'excluded_sources':['notes']}),
]:
    try:
        validate_intent(case,'Read email without notes',now=datetime(2026,10,5,12))
        raise AssertionError('invalid/excluded intent accepted')
    except InvalidIntent:
        check(identity,True)
scorer=[]
for count in (True,1.0):
    case={'expected':{'calls':[{'name':'view_emails','args':{'count':1}}],'sources':['email']}}
    run={'executed_calls':[{'name':'view_emails','args':{'count':count}}],
        'events':[{'type':'done'}],'answer':'synthetic'}
    metrics=e.score(case,run)['metrics']
    check('typed-scorer-'+type(count).__name__,metrics['exact_arguments'] and not metrics['request_argument']
        and not metrics['guarded_end_to_end'],metrics=metrics)
data={'exact_sha':'40ee88a409eaec943ae4474130bd87263ea794e1','records':records,
    'total':len(records),'passed':sum(row['passed'] for row in records),
    'heldout_payload_read':False,'actual_model_calls':False,'native_or_outbound_execution':False}
(OUT/'fresh-boundary-results.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps({'total':data['total'],'passed':data['passed']}))
