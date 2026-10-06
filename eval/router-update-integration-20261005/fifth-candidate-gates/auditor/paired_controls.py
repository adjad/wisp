"""Independent paired action-literal controls; no corpus or native calls."""
import asyncio
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-6df5200-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-6df5200-paired-')).resolve()
e.install_guard(state,OUT)
from service.router import router
from service.router.intent import planner,validate_intent,InvalidIntent
from service.router.intent.validation import _instruction_text
from service.workflows import compiler as workflow
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
def source(query):return {'domain':'notes','operation':'records','query':query}
def intent(query):return {'version':1,'kind':'read','sources':[source(query)],'excluded_sources':[],'unsupported_constraints':[]}
cases=[
    ('later-share-with-channel','Find notes about cedar maps and share it by email to Mom','cedar maps','guarded'),
    ('later-forward','Find notes about cedar maps and forward it by email to Mom','cedar maps','guarded'),
    ('quoted-share-data','Find notes about "cedar maps and share it with Mom"','cedar maps and share it with Mom','read'),
    ('literal-share-then-send','Find notes about share it with Mom and send it by email to Mom','share it with Mom','guarded'),
    ('later-share-without-channel','Find notes about cedar maps and share it with Mom','cedar maps and share it with Mom','guarded'),
]
records=[]
for identity,prompt,query,expected in cases:
    c={'id':identity,'prompt':prompt,'intent_response':intent(query),'clock':'2026-10-05T12:00:00-07:00','fixture_answer':'Paired independent fixture '+identity}
    planner.role_target=lambda role:TARGET
    client=FakeClient(intent(query),intent(query))
    plan=asyncio.run(planner.plan_read(prompt,client=client,config=CONFIG,now=datetime(2026,10,5,12)))
    request=router._classify_web_request(prompt,None)
    wf=workflow.compile_new(prompt)
    run=asyncio.run(e.run_case(c,state,candidate=True))
    calls=[(x['name'],x['args']) for x in run['executed_calls']]
    invoked=any(x['kind']=='scripted_intent' for x in run['raw_model_io'])
    # Previous helper's masking stages, reproduced as pure expressions from its
    # reviewed diff: no old script execution, no old application receipt reuse.
    legacy=workflow._EMAIL_NOUN_USE.sub(' ',prompt)
    if workflow._LEADING_EMAIL_NOUN.match(legacy) and not workflow._ADDRESSEE_CUE.search(legacy):legacy=workflow._LEADING_EMAIL_NOUN.sub(' ',legacy,count=1)
    legacy=workflow._MESSAGE_NOUN_USE.sub(' ',legacy)
    if workflow._LEADING_MESSAGE_NOUN.match(legacy) and not workflow._ADDRESSEE_CUE.search(legacy):legacy=workflow._LEADING_MESSAGE_NOUN.sub(' ',legacy,count=1)
    legacy_outbound=bool(workflow._OUTBOUND.search(legacy))
    met=(not calls and not invoked) if expected=='guarded' else calls==[('search_notes',{'query':query})]
    records.append({'case':c,'expected_mode':expected,'expectation_met':met,
        'instruction_text':_instruction_text(prompt,now=datetime(2026,10,5,12)),
        'current_outbound_verb':workflow.outbound_verb(prompt),'prior_without_literal_mask_outbound':legacy_outbound,
        'comparison_limit':'Pure previous-stage expressions for these inputs only; not an old application run or transferred receipt.',
        'workflow':asdict(wf) if wf else None,'classification':{'delivery':asdict(request.delivery) if request.delivery else None,'effects':sorted(request.authorized_effects)},
        'planner':None if plan is None else {'disposition':plan.disposition,'calls':plan.calls},'run':run})
    print(json.dumps({'id':identity,'met':met,'calls':calls,'intent_invoked':invoked,'outbound':workflow.outbound_verb(prompt),'previous_stage_outbound':legacy_outbound}))
data={'exact_sha':'6df5200d0945ee3b050bcde4115a338e392017b1','records':records,'total':len(records),
    'expectations_met':sum(x['expectation_met'] for x in records),'failed_cases':[x['case']['id'] for x in records if not x['expectation_met']],
    'heldout_payload_opened':False,'actual_model_calls':False,'native_tool_bodies_or_outbound_execution':False}
(OUT/'paired-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
print(json.dumps({k:data[k] for k in ('total','expectations_met','failed_cases')}))
