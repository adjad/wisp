"""Fresh fifth-candidate independent controls, no corpus or archived execution."""
import asyncio
import copy
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
state=Path(tempfile.mkdtemp(prefix='wisp-6df5200-app-')).resolve()
e.install_guard(state,OUT)
from service.router import router
from service.router.intent import planner,validate_intent,InvalidIntent
from service.router.intent.validation import source_requirements,applicable_read,_instruction_text
from service.workflows.compiler import compile_new,outbound_verb
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
NOW=datetime(2026,10,5,12)
def s(domain,operation='records',**kw):return {'domain':domain,'operation':operation,**kw}
def v(sources,excluded=()):return {'version':1,'kind':'read','sources':sources,'excluded_sources':list(excluded),'unsupported_constraints':[]}
def mail(items):
    result=[]
    for x in items:
        args={k:x[k] for k in ('query','count','account','unread') if k in x}
        if 'query' in x:args['strict_match']=True
        if 'time' in x:args['period']=next(iter(x['time'].values()))
        result.append(('view_emails',args))
    return result
cases=[]
def add(identity,prompt,sources,calls,*,context=(),excluded=(),kind='compiled',effects=(),config=None):
    cases.append({'id':identity,'prompt':prompt,'intent_response':v(sources,excluded),
        'clock':'2026-10-05T12:00:00-07:00','context':list(context),'fixture_answer':'Independent result '+identity,
        'expected_calls':calls,'expected_kind':kind,'expected_effects':None if effects is None else list(effects),'standalone_config':config})

# Original failures, followed by lexical class variants.
pair=[s('email',query='Lyra'),s('email',query='road to recovery')]
add('R4-original','Read email from Lyra and email about road to recovery',pair,mail(pair))
for cue,query in [('named','guide to weaving'),('containing','response to update'),('about','path to progress')]:
    items=[s('email',query='Cassia'),s('email',query=query)]
    add('R4-class-'+cue,'Review email from Cassia and email '+cue+' '+query,items,mail(items))
title=[s('email',query='Cassia'),s('email',query='Guide to Gardening')]
add('R4-title-literal','Read email from Cassia and email about Guide to Gardening',title,mail(title))
add('R4-title-quoted','Read email from Cassia and email about "Guide to Gardening"',title,mail(title))
add('R5-original','Recap email',[s('email','overview')],[('summarize_emails',{})])
add('R5-original-mixed','Recap email; find notes named harbor sketches',[s('email','overview'),s('notes',query='harbor sketches')],
    [('summarize_emails',{}),('search_notes',{'query':'harbor sketches'})])
for head in ('Review','Inspect','Scan','Browse'):
    add('R5-head-'+head,head+' email',[s('email','overview')],[('summarize_emails',{})])
for noun in ('text','message'):
    add('R5-noun-'+noun,'Recap '+noun,[s('messages','overview')],[('summarize_messages',{})])
for identity,query in [('original','my calendar'),('messages','our messages'),('named','email reminders'),('action-literal','send email')]:
    add('R6-'+identity,'Find notes about '+query,[s('notes',query=query)],[('search_notes',{'query':query})])
for identity,sources in [('extra',[s('notes',query='my calendar'),s('calendar')]),
    ('missing',[s('notes')]),('truncated',[s('notes',query='my')])]:
    add('R6-negative-'+identity,'Find notes about my calendar',sources,[],kind='clarify')
add('R6-quoted-source','Find notes about "our messages"',[s('notes',query='our messages')],[('search_notes',{'query':'our messages'})])
add('R6-exclusions','Find notes about our calendar without email or messages',[s('notes',query='our calendar')],
    [('search_notes',{'query':'our calendar'})],excluded=['email','messages'])
add('R6-exclusion-missing','Find notes about our calendar without email or messages',[s('notes',query='our calendar')],[],kind='clarify')
add('R6-quoted-negative-data','Find notes about "our calendar without email"',[s('notes',query='our calendar without email')],
    [('search_notes',{'query':'our calendar without email'})])
add('R6-notes-domain-only','Find notes about my calendar without email',[s('notes',query='my calendar')],
    [('search_notes',{'query':'my calendar'})],excluded=['email'],config={**CONFIG,'domains':['notes']})
add('R6-independent-source','Find notes about our calendar and read email from Cassia',[s('notes',query='our calendar'),s('email',query='Cassia')],
    [('search_notes',{'query':'our calendar'}),('view_emails',{'query':'Cassia','strict_match':True})])
add('R6-independent-missing','Find notes about our calendar and read email from Cassia',[s('notes',query='our calendar')],[],kind='clarify')
ctx=[{'role':'user','content':'Find notes about our calendar'},{'role':'assistant','content':'Synthetic prior notes','tool_digest':'search_notes'}]
add('R6-context-positive','Same for yesterday',[s('notes',query='our calendar',time={'named':'yesterday'})],
    [('search_notes',{'query':'our calendar','period':'yesterday'})],context=ctx)
add('R6-context-wrong-domain','Same for yesterday',[s('calendar',time={'named':'yesterday'})],[],context=ctx,kind='clarify')

mixed=[s('calendar',time={'named':'tomorrow'}),s('notes',query='cedar maps',time={'named':'yesterday'})]
mp='Show my calendar tomorrow; find notes about cedar maps from yesterday'
add('R3-mixed',mp,mixed,[('get_upcoming',{'period':'tomorrow','calendar_only':True}),('search_notes',{'query':'cedar maps','period':'yesterday'})])
bad=copy.deepcopy(mixed);bad[1].pop('query')
add('R3-missing',mp,bad,[],kind='clarify')
repeated=[s('notes',time={'named':'tomorrow'}),s('notes',query='cedar maps',time={'named':'yesterday'})]
rp='Show notes tomorrow; find notes about cedar maps from yesterday'
add('R3-repeated',rp,repeated,[('search_notes',{'period':'tomorrow'}),('search_notes',{'query':'cedar maps','period':'yesterday'})])
bad=copy.deepcopy(repeated);bad[0]['query']=bad[1].pop('query')
add('R3-repeated-misbound',rp,bad,[],kind='clarify')

tp='Read five unread emails from Cassia in account "FieldLab" for 2026-10-02; read two emails from Soren in account "HomeLab" for 2026-10-04'
tuples=[s('email',query='Cassia',count=5,unread=True,account='FieldLab',time={'date':'2026-10-02'}),
    s('email',query='Soren',count=2,account='HomeLab',time={'date':'2026-10-04'})]
add('R1-tuples',tp,tuples,mail(tuples))
add('R1-reversed',tp,list(reversed(tuples)),mail(list(reversed(tuples))))
for field in ('query','time','count','account','unread','operation'):
    bad=copy.deepcopy(tuples)
    if field=='unread':bad[1]['unread']=bad[0].pop('unread')
    elif field=='operation':bad[0]['operation']='overview'
    else:bad[0][field],bad[1][field]=bad[1][field],bad[0][field]
    add('R1-swap-'+field,tp,bad,[],kind='clarify')
add('R1-missing',tp,tuples[:1],[],kind='clarify')
add('R1-duplicate',tp,tuples+[copy.deepcopy(tuples[0])],mail(tuples))
context=[{'role':'user','content':'Find five notes about cedar maps from yesterday'},
    {'role':'assistant','content':'Synthetic notes','tool_digest':'search_notes'}]
add('R2-correction','Actually about river charts',[s('notes',query='river charts',count=5,time={'named':'yesterday'})],
    [('search_notes',{'query':'river charts','count':5,'period':'yesterday'})],context=context)
add('R2-stale','Actually about river charts',[s('notes',query='cedar maps',count=5,time={'named':'yesterday'})],[],context=context,kind='clarify')
add('alias-calendar','Read appointments that are on our calendar tomorrow',[s('calendar',time={'named':'tomorrow'})],[('get_upcoming',{'period':'tomorrow','calendar_only':True})])
add('alias-inbox','Read email that is in the inbox yesterday',[s('email',time={'named':'yesterday'})],[('view_emails',{'period':'yesterday'})])
add('F1-date','Read email for 2026-10-02',[s('email')],[],kind='clarify')
add('F1-reminder','Read overdue reminders',[s('reminders')],[],kind='clarify')
add('F1-duration','Find a 60 minute free slot on my calendar tomorrow',[s('calendar','free_time',time={'named':'tomorrow'})],[],kind='clarify')
add('F1-duration-valid','Find a 60 minute free slot on my calendar tomorrow',[s('calendar','free_time',time={'named':'tomorrow'},minutes=60)],[('find_free_time',{'period':'tomorrow','minutes':60})])
add('F3-shortcut','what is up tomorrow?',[],[('get_upcoming',{'period':'tomorrow','calendar_only':True})],kind='shortcut')
add('unsupported-daypart','Read email tomorrow before noon',[s('email',time={'named':'tomorrow'})],[],kind='clarify')
add('excluded-violation','Read email without notes',[s('email'),s('notes')],[],excluded=['notes'],kind='clarify')

# Genuine deliveries and later action clauses must not enter structured reads.
for name,prompt,effect in [
    ('recap-email','Recap email and email Mom the summary','send_email'),
    ('review-text','Review email and text Mom saying hello','send_message'),
    ('draft','Recap email and draft an email to Mom','draft_email'),
    ('outer-destination','Read email from Cassia and email about path to progress to Mom','send_email'),
    ('literal-address','Read email from Cassia and email about roadmap to fixture@example.invalid','send_email'),
    ('later-send','Read email from Cassia and email about path to progress and send it by email to Mom','send_email'),
    ('lookup-send','Find notes about cedar maps and send it to Mom',None),
    ('lookup-forward','Find notes about cedar maps and forward it by email to Mom','send_email')]:
    add('action-'+name,prompt,[s('notes',query='cedar maps')],[],kind='guarded-action',effects=None if effect is None else [effect])
for name,query,prompt in [
    ('share-full','cedar maps and share it with Mom','Find notes about cedar maps and share it with Mom'),
    ('share-prefix','cedar maps','Find notes about cedar maps and share it with Mom')]:
    add('action-'+name,prompt,[s('notes',query=query)],[],kind='guarded-action',effects=None)
add('quoted-action','Find notes about "send email to Mom"',[s('notes',query='send email to Mom')],[('search_notes',{'query':'send email to Mom'})])
add('negated-action','Read email from Cassia and do not email Mom',[s('email',query='Cassia')],[],kind='no-effects')

records=[]
for c in cases:
    planner.role_target=lambda role:TARGET
    tools=[name.strip() for turn in c['context'] for name in turn.get('tool_digest','').split(',') if name.strip()]
    client=FakeClient(c['intent_response'],c['intent_response'])
    plan=asyncio.run(planner.plan_read(c['prompt'],client=client,config=c['standalone_config'] or CONFIG,context=c['context'],prior_tools=tools,now=NOW))
    request=router._classify_web_request(c['prompt'],None)
    workflow=compile_new(c['prompt'])
    try:validate_intent(c['intent_response'],c['prompt'],context=c['context'],prior_tools=tools,now=NOW);validation={'accepted':True}
    except InvalidIntent as exc:validation={'accepted':False,'diagnosis':str(exc)}
    run=asyncio.run(e.run_case(c,state,candidate=True))
    actual=[(x['name'],x['args']) for x in run['executed_calls']]
    route=next((ev for ev in run['events'] if ev.get('type')=='routed'),{})
    invoked=any(io['kind']=='scripted_intent' for io in run['raw_model_io'])
    effects=sorted(request.authorized_effects)
    met=actual==c['expected_calls'] and (c['expected_effects'] is None or effects==sorted(c['expected_effects']))
    if c['expected_kind']=='compiled':met=met and route.get('intent_disposition')=='compiled' and plan is not None and plan.disposition=='compiled'
    if c['expected_kind']=='clarify':met=met and route.get('intent_disposition')=='clarify'
    if c['expected_kind']=='guarded-action':met=met and not invoked and route.get('intent_disposition')!='compiled'
    records.append({'case':c,'validation':validation,'planner':None if plan is None else {'disposition':plan.disposition,'calls':plan.calls,'reason':plan.reason,'attempts':plan.attempts},
        'planner_client_calls':client.calls,'classification':{'effects':effects,'delivery':asdict(request.delivery) if request.delivery else None,'source':request.source},
        'source_requirements':[sorted(z) for z in source_requirements(c['prompt'])],
        'applicable_read':applicable_read(c['prompt'],c['context'],tools),'outbound_verb':outbound_verb(c['prompt']),
        'instruction_text':_instruction_text(c['prompt'],now=NOW),'workflow':asdict(workflow) if workflow else None,
        'run':run,'expectation_met':met})
    print(json.dumps({'id':c['id'],'met':met,'calls':actual,'route':route.get('intent_disposition'),'diagnosis':validation.get('diagnosis')}))
data={'exact_sha':'6df5200d0945ee3b050bcde4115a338e392017b1','records':records,'total':len(records),
    'expectations_met':sum(r['expectation_met'] for r in records),'failed_cases':[r['case']['id'] for r in records if not r['expectation_met']],
    'mode':'fresh actual-main scripted fixtures plus standalone planner/classifier','heldout_payload_opened':False,
    'actual_model_calls':False,'native_tool_bodies_or_outbound_execution':False}
(OUT/'application-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
print(json.dumps({k:data[k] for k in ('total','expectations_met','failed_cases')}))
