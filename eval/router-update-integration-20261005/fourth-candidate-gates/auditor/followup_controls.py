"""Fresh paired controls for newly observed application/source-noun gaps."""
import asyncio
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-273d274-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-273d274-followup-')).resolve()
e.install_guard(state,OUT)
from service.router import router
from service.router.intent import planner,validate_intent,InvalidIntent
from service.workflows.compiler import compile_new,outbound_verb
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
def s(domain,op='records',**kw):return {'domain':domain,'operation':op,**kw}
def v(sources):return {'version':1,'kind':'read','sources':sources,'excluded_sources':[],'unsupported_constraints':[]}
cases=[
 ('bare-email-recap','Recap email',[s('email','overview')],[('summarize_emails',{})]),
 ('possessive-email-recap','Recap my email',[s('email','overview')],[('summarize_emails',{})]),
 ('summarize-email-mixed','Summarize email; find notes named harbor sketches',[s('email','overview'),s('notes',query='harbor sketches')],
     [('summarize_emails',{}),('search_notes',{'query':'harbor sketches'})]),
 ('possessive-recap-mixed','Recap my email; find notes named harbor sketches',[s('email','overview'),s('notes',query='harbor sketches')],
     [('summarize_emails',{}),('search_notes',{'query':'harbor sketches'})]),
 ('unquoted-source-data','Find notes about our messages',[s('notes',query='our messages')],[('search_notes',{'query':'our messages'})]),
 ('quoted-source-data','Find notes about "our messages"',[s('notes',query='our messages')],[('search_notes',{'query':'our messages'})]),
 ('unquoted-preposition-data','Read email from Lyra and email about road to recovery',
     [s('email',query='Lyra'),s('email',query='road to recovery')],
     [('view_emails',{'query':'Lyra','strict_match':True}),('view_emails',{'query':'road to recovery','strict_match':True})]),
 ('quoted-preposition-data','Read email from Lyra and email about "road to recovery"',
     [s('email',query='Lyra'),s('email',query='road to recovery')],
     [('view_emails',{'query':'Lyra','strict_match':True}),('view_emails',{'query':'road to recovery','strict_match':True})]),
]
records=[]
for identity,prompt,sources,calls in cases:
    c={'id':identity,'prompt':prompt,'clock':'2026-10-05T12:00:00-07:00','intent_response':v(sources),
        'fixture_answer':'Independent paired result '+identity,'expected_calls':calls}
    planner.role_target=lambda role:TARGET
    client=FakeClient(v(sources),v(sources))
    plan=asyncio.run(planner.plan_read(prompt,client=client,config=CONFIG,now=datetime(2026,10,5,12)))
    request=router._classify_web_request(prompt,None)
    workflow=compile_new(prompt)
    try:validate_intent(v(sources),prompt,now=datetime(2026,10,5,12));validation={'accepted':True}
    except InvalidIntent as exc:validation={'accepted':False,'diagnosis':str(exc)}
    run=asyncio.run(e.run_case(c,state,candidate=True))
    actual=[(x['name'],x['args']) for x in run['executed_calls']]
    records.append({'case':c,'validation':validation,'planner':{'disposition':plan.disposition,'calls':plan.calls},
        'outbound_verb':outbound_verb(prompt),'workflow':asdict(workflow) if workflow else None,
        'classification':{'delivery':asdict(request.delivery) if request.delivery else None,'effects':sorted(request.authorized_effects),'source':request.source},
        'run':run,'expectation_met':actual==calls})
    print(json.dumps({'id':identity,'met':actual==calls,'validator':validation,'planner':plan.disposition,
        'workflow_status':workflow.status if workflow else None,'delivery':asdict(request.delivery) if request.delivery else None,
        'effects':sorted(request.authorized_effects),'actual':actual}))
data={'exact_sha':'273d2748d01ed978b4e4fb56c8ee503b18143bc4','records':records,'total':len(records),
    'expectations_met':sum(r['expectation_met'] for r in records),
    'failed_cases':[r['case']['id'] for r in records if not r['expectation_met']],
    'heldout_payload_opened':False,'actual_model_calls':False,'native_tool_bodies_or_outbound_execution':False}
(OUT/'followup-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
print(json.dumps({k:data[k] for k in ('total','expectations_met','failed_cases')}))
