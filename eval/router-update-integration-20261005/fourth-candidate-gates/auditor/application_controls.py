"""Fresh exact-273d274 synthetic controls at actual main/intent boundaries."""
import asyncio
import copy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-273d274-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-273d274-application-')).resolve()
e.install_guard(state,OUT)
from service.router.intent import planner,validate_intent,InvalidIntent
from service.router import router
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
NOW=datetime(2026,10,5,12)
def s(domain,operation='records',**kw):return {'domain':domain,'operation':operation,**kw}
def intent(sources,excluded=()):return {'version':1,'kind':'read','sources':sources,'excluded_sources':list(excluded),'unsupported_constraints':[]}
def emails(items):
    calls=[]
    for item in items:
        args={k:item[k] for k in ('query','account','count','unread') if k in item}
        if 'query' in item:args['strict_match']=True
        if 'time' in item:args['period']=next(iter(item['time'].values()))
        calls.append(('view_emails',args))
    return calls
cases=[]
def add(identity,prompt,sources,calls,*,context=(),excluded=(),route='compiled',effects=(),tool_results=None):
    c={'id':identity,'prompt':prompt,'intent_response':intent(sources,excluded),
       'clock':'2026-10-05T12:00:00-07:00','context':list(context),
       'fixture_answer':'Independent synthetic result: '+identity,
       'expected_calls':calls,'expected_disposition':route,'expected_effects':list(effects)}
    if tool_results:c['tool_results']=tool_results
    cases.append(c)

mixed=[s('calendar',time={'named':'tomorrow'}),s('notes',query='harbor sketches',time={'named':'yesterday'})]
mp='Show my calendar tomorrow; find notes about harbor sketches from yesterday'
mc=[('get_upcoming',{'period':'tomorrow','calendar_only':True}),('search_notes',{'query':'harbor sketches','period':'yesterday'})]
add('R3-mixed-positive',mp,mixed,mc)
add('R3-mixed-reversed-positive','Find notes about harbor sketches from yesterday; show my calendar tomorrow',list(reversed(mixed)),list(reversed(mc)))
add('R3-overview-plus-lookup','Recap email; find notes named harbor sketches',[s('email','overview'),s('notes',query='harbor sketches')],[('summarize_emails',{}),('search_notes',{'query':'harbor sketches'})])
for mutation in ('missing','truncated','invented','date-swap'):
    bad=copy.deepcopy(mixed)
    if mutation=='missing':bad[1].pop('query')
    elif mutation=='date-swap':bad[0]['time'],bad[1]['time']=bad[1]['time'],bad[0]['time']
    else:bad[1]['query']='harbor' if mutation=='truncated' else 'invented drawings'
    add('R3-'+mutation,mp,bad,[],route='clarify')
rp='Show my notes tomorrow; find notes about harbor sketches from yesterday'
repeat=[s('notes',time={'named':'tomorrow'}),s('notes',query='harbor sketches',time={'named':'yesterday'})]
add('R3-repeated-positive',rp,repeat,[('search_notes',{'period':'tomorrow'}),('search_notes',{'query':'harbor sketches','period':'yesterday'})])
bad=copy.deepcopy(repeat);bad[0]['query']=bad[1].pop('query')
add('R3-repeated-swapped-query',rp,bad,[],route='clarify')
add('R3-unbounded-second-lookup','Show my notes; find notes',[s('notes')],[],route='clarify')
add('literal-source-unquoted-positive','Find notes about my calendar',[s('notes',query='my calendar')],[('search_notes',{'query':'my calendar'})])
add('literal-source-quoted-positive','Find notes about "my calendar"',[s('notes',query='my calendar')],[('search_notes',{'query':'my calendar'})])
add('literal-source-extra-rejected','Find notes about my calendar',[s('notes',query='my calendar'),s('calendar')],[],route='clarify')
add('literal-action-quoted-positive','Find notes named "send email"',[s('notes',query='send email')],[('search_notes',{'query':'send email'})])

shared=[s('email',query='Lyra',time={'named':'yesterday'}),s('email',query='Orion',time={'named':'yesterday'})]
ep='Read email from Lyra and email from Orion for yesterday'
add('R4-coordinated-positive',ep,shared,emails(shared))
add('R4-quoted-positive','Read email from "Lyra" and email from "Orion" for yesterday',shared,emails(shared))
separate=[s('email',query='Lyra'),s('email',query='Orion',time={'named':'yesterday'})]
add('R4-new-verb-positive','Read email from Lyra and read email from Orion for yesterday',separate,emails(separate))
add('R4-new-verb-wrong-shared-date','Read email from Lyra and read email from Orion for yesterday',shared,[],route='clarify')
add('R4-mixed-source-noun-positive','Read my notes and email about harbor sketches',[s('notes'),s('email',query='harbor sketches')],[('search_notes',{}),('view_emails',{'query':'harbor sketches','strict_match':True})])
for identity,tail,effect in [('email-addressee','email Mom','send_email'),
    ('explicit-destination','email from Orion to Mom','send_email'),
    ('pronoun-destination','email it to Mom','send_email'),
    ('text-addressee','text Mom','send_message'),
    ('draft-destination','draft an email to Mom','draft_email'),
    ('later-delivery','email from Orion and send it by email to Mom','send_email')]:
    add('action-'+identity,'Read email from Lyra and '+tail,[s('email',query='Lyra')],[],route='guarded-action',effects=[effect])
add('action-quoted-topic','Read email about "email Mom" and email from Orion',[s('email',query='email Mom'),s('email',query='Orion')],
    [('view_emails',{'query':'email Mom','strict_match':True}),('view_emails',{'query':'Orion','strict_match':True})])
add('action-negated','Read email from Lyra and do not email Mom',[s('email',query='Lyra')],[],route='no-effects')

tp='Read three unread emails from Lyra in account "ArchiveLab" for 2026-10-01; read seven emails from Orion in account "Studio" for 2026-10-04'
tuples=[s('email',query='Lyra',count=3,unread=True,account='ArchiveLab',time={'date':'2026-10-01'}),s('email',query='Orion',count=7,account='Studio',time={'date':'2026-10-04'})]
add('R1-whole-tuples-positive',tp,tuples,emails(tuples))
add('R1-reverse-order-positive',tp,list(reversed(tuples)),emails(list(reversed(tuples))))
for field in ('query','time','account','count','unread','operation'):
    bad=copy.deepcopy(tuples)
    if field=='unread':bad[1]['unread']=bad[0].pop('unread')
    elif field=='operation':bad[0]['operation']='overview'
    else:bad[0][field],bad[1][field]=bad[1][field],bad[0][field]
    add('R1-swap-'+field,tp,bad,[],route='clarify')
add('R1-missing-occurrence',tp,tuples[:1],[],route='clarify')
add('R1-duplicate-positive',tp,tuples+[copy.deepcopy(tuples[0])],emails(tuples))

ctx=[{'role':'user','content':'Find three notes about harbor sketches from yesterday'},{'role':'assistant','content':'Synthetic prior result','tool_digest':'search_notes'}]
add('R2-new-query-positive','Actually about copper maps',[s('notes',query='copper maps',count=3,time={'named':'yesterday'})],
    [('search_notes',{'query':'copper maps','count':3,'period':'yesterday'})],context=ctx)
add('R2-stale-query-rejected','Actually about copper maps',[s('notes',query='harbor sketches',count=3,time={'named':'yesterday'})],[],context=ctx,route='clarify')
ectx=[{'role':'user','content':'Read three unread emails from Lyra in account "ArchiveLab" for yesterday'},{'role':'assistant','content':'Synthetic prior email','tool_digest':'view_emails'}]
new=[s('email',query='Orion',account='ArchiveLab',count=3,unread=True,time={'named':'yesterday'})]
add('R2-inherited-constraints-positive','Actually from Orion',new,emails(new),context=ectx)
mctx=[{'role':'user','content':'Read email from Lyra; find notes named harbor sketches'},{'role':'assistant','content':'Synthetic two sources','tool_digest':'view_emails, search_notes'}]
add('R2-ambiguous-context','Actually about copper maps',[s('notes',query='copper maps')],[],context=mctx,route='clarify')
add('alias-calendar-positive','Show appointments that are on our calendar tomorrow',[s('calendar',time={'named':'tomorrow'})],[('get_upcoming',{'period':'tomorrow','calendar_only':True})])
add('alias-inbox-positive','Read emails that are in the inbox from yesterday',[s('email',time={'named':'yesterday'})],[('view_emails',{'period':'yesterday'})])
add('alias-messages-positive','Recap texts in our messages',[s('messages','overview')],[('summarize_messages',{})])
add('alias-ambiguous','Read appointments tomorrow calendar next week',[s('calendar',time={'named':'tomorrow'}),s('calendar',time={'named':'next week'})],[],route='clarify')
add('F1-date-omission','Read email for 2026-10-01',[s('email')],[],route='clarify')
add('F1-scope-omission','Read overdue reminders',[s('reminders')],[],route='clarify')
add('F1-minutes-omission','Find a 90 minute free slot on my calendar tomorrow',[s('calendar','free_time',time={'named':'tomorrow'})],[],route='clarify')
add('F1-duration-positive','Find a 90 minute free slot on my calendar tomorrow',[s('calendar','free_time',time={'named':'tomorrow'},minutes=90)],[('find_free_time',{'period':'tomorrow','minutes':90})])
add('F3-calendar-only','what is up tomorrow?',[],[('get_upcoming',{'period':'tomorrow','calendar_only':True})],route='shortcut')
add('exclusion-positive','Read email without notes',[s('email')],[('view_emails',{})],excluded=['notes'])
add('exclusion-negative','Read email without notes',[s('email'),s('notes')],[],excluded=['notes'],route='clarify')
add('unsupported-daypart','Read email tomorrow before noon',[s('email',time={'named':'tomorrow'})],[],route='clarify')

records=[]
for c in cases:
    planner.role_target=lambda role:TARGET
    prior_tools=[n.strip() for turn in c['context'] for n in turn.get('tool_digest','').split(',') if n.strip()]
    fake=FakeClient(c['intent_response'],c['intent_response'])
    plan=asyncio.run(planner.plan_read(c['prompt'],client=fake,config=CONFIG,context=c['context'],prior_tools=prior_tools,now=NOW))
    request=router._classify_web_request(c['prompt'],None)
    try:
        validate_intent(c['intent_response'],c['prompt'],context=c['context'],prior_tools=prior_tools,now=NOW)
        validation={'accepted':True}
    except InvalidIntent as exc:validation={'accepted':False,'diagnosis':str(exc)}
    run=asyncio.run(e.run_case(c,state,candidate=True))
    actual=[(x['name'],x['args']) for x in run['executed_calls']]
    classified=sorted(request.authorized_effects)
    route=next((ev for ev in run['events'] if ev.get('type')=='routed'),{})
    invoked_intent=any(io['kind']=='scripted_intent' for io in run['raw_model_io'])
    expected=c['expected_disposition']
    met=actual==c['expected_calls'] and classified==sorted(c['expected_effects'])
    if expected=='compiled':met=met and route.get('intent_disposition')=='compiled' and plan is not None and plan.disposition=='compiled'
    if expected=='clarify':met=met and route.get('intent_disposition')=='clarify'
    if expected=='guarded-action':met=met and not invoked_intent and route.get('intent_disposition')!='compiled'
    records.append({'case':c,'validation':validation,'classification':{'source':request.source,'effects':classified,
        'delivery':None if request.delivery is None else {'text':request.delivery.text,'channel':request.delivery.channel,'recipient':request.delivery.recipient}},
        'planner':None if plan is None else {'disposition':plan.disposition,'calls':plan.calls,'reason':plan.reason,'attempts':plan.attempts},
        'planner_client_calls':fake.calls,'run':run,'expectation_met':met})
    print(json.dumps({'id':c['id'],'met':met,'actual_calls':actual,'route':route.get('intent_disposition'),'diagnosis':validation.get('diagnosis')}))
data={'exact_sha':'273d2748d01ed978b4e4fb56c8ee503b18143bc4','records':records,'total':len(records),
    'expectations_met':sum(r['expectation_met'] for r in records),'failed_cases':[r['case']['id'] for r in records if not r['expectation_met']],
    'mode':'fresh synthetic actual-main plus pure classifier and standalone planner',
    'heldout_payload_opened':False,'actual_model_calls':False,'native_tool_bodies_or_outbound_execution':False}
(OUT/'application-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
print(json.dumps({k:data[k] for k in ('total','expectations_met','failed_cases')}))
