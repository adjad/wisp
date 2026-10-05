"""Fresh third-candidate synthetic inputs; no corpus or archived script reuse."""
import asyncio
import copy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-27490ae-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-27490ae-independent-')).resolve()
e.install_guard(state,OUT)
from service.router.intent import planner
from service.router.intent import validate_intent, InvalidIntent
from service.router import router
from tests.test_router_intent_core import FakeClient,CONFIG,TARGET
NOW=datetime(2026,10,5,12)
def s(domain,operation='records',**fields):
    return {'domain':domain,'operation':operation,**fields}
def value(sources,excluded=()):
    return {'version':1,'kind':'read','sources':sources,'excluded_sources':list(excluded),'unsupported_constraints':[]}
cases=[]
def add(identity,prompt,sources,expected,context=(),excluded=(),allow_clarify=False):
    cases.append({'id':identity,'prompt':prompt,'intent_response':value(sources,excluded),
        'clock':'2026-10-05T12:00:00-07:00','context':list(context),
        'fixture_answer':'Independent synthetic result for '+identity+'.',
        'expected_calls':expected,'allow_safe_clarify':allow_clarify})
def calls_for(sources):
    result=[]
    for item in sources:
        fields={key:item[key] for key in ('query','account','count','unread') if key in item}
        if 'query' in item:
            fields['strict_match']=True
        time=item.get('time',{})
        if time:
            fields['period']=time.get('date',time.get('named',time.get('month')))
        result.append(('view_emails',fields))
    return result
prompt=('Read four unread emails from Selene in account "Research" for 2026-10-02; '
        'read six emails from Dorian in account "Household" for 2026-10-03')
good=[s('email',query='Selene',account='Research',count=4,unread=True,time={'date':'2026-10-02'}),
      s('email',query='Dorian',account='Household',count=6,time={'date':'2026-10-03'})]
add('full-tuples-positive',prompt,good,calls_for(good))
add('full-tuples-reversed-positive',prompt,list(reversed(good)),calls_for(list(reversed(good))))
for field in ('time','query','account','count','unread','operation'):
    bad=copy.deepcopy(good)
    if field=='unread':
        bad[1]['unread']=bad[0].pop('unread')
    elif field=='operation':
        bad[0]['operation']='overview'
    else:
        bad[0][field],bad[1][field]=bad[1][field],bad[0][field]
    add('tuple-swap-'+field,prompt,bad,[])
add('tuple-coverage-missing',prompt,good[:1],[])
bad=copy.deepcopy(good)+[s('email',query='Selene',account='Research',count=4,unread=True,time={'date':'2026-10-03'})]
add('tuple-extra-cross-product',prompt,bad,[])
add('duplicate-identical-positive',prompt,good+[copy.deepcopy(good[0])],calls_for(good))

shared=[s('email',query='Selene',time={'named':'yesterday'}),s('email',query='Dorian',time={'named':'yesterday'})]
add('coordinated-shared-date-positive','Read email from Selene and email from Dorian for yesterday',shared,calls_for(shared))
independent=[s('email',query='Selene'),s('email',query='Dorian',time={'named':'yesterday'})]
add('new-verb-independent-date-positive','Read email from Selene and read email from Dorian for yesterday',independent,calls_for(independent))
add('new-verb-date-broadened','Read email from Selene and read email from Dorian for yesterday',shared,[])

context=[{'role':'user','content':'Find four notes about amber notebooks from yesterday'},
    {'role':'assistant','content':'Synthetic notes results','tool_digest':'search_notes'}]
add('fragment-new-query-positive','Actually about violet charts',
    [s('notes',query='violet charts',count=4,time={'named':'yesterday'})],
    [('search_notes',{'query':'violet charts','count':4,'period':'yesterday'})],context)
add('fragment-stale-query-rejected','Actually about violet charts',
    [s('notes',query='amber notebooks',count=4,time={'named':'yesterday'})],[],context)
add('fragment-quoted-positive','Instead named "violet charts"',
    [s('notes',query='violet charts',count=4,time={'named':'yesterday'})],
    [('search_notes',{'query':'violet charts','count':4,'period':'yesterday'})],context)
multi=[{'role':'user','content':'Read email from Selene; find notes named amber notebooks'},
    {'role':'assistant','content':'Synthetic two-source receipt','tool_digest':'view_emails, search_notes'}]
add('fragment-multisource-ambiguous','Actually about violet charts',[s('notes',query='violet charts')],[],multi)
repeated_context=[{'role':'user','content':'Find notes named "amber notebooks"; find notes named "green charts"'},
    {'role':'assistant','content':'Synthetic repeated notes receipt','tool_digest':'search_notes'}]
add('fragment-repeated-source-ambiguous','Actually about violet charts',[s('notes',query='violet charts')],[],repeated_context)
email_context=[{'role':'user','content':'Read four unread emails from Selene in account "Research" for yesterday'},
    {'role':'assistant','content':'Synthetic email receipt','tool_digest':'view_emails'}]
new=[s('email',query='Dorian',account='Research',count=4,unread=True,time={'named':'yesterday'})]
add('fragment-inherited-other-filters-positive','Actually from Dorian',new,calls_for(new),email_context)
new_bad=copy.deepcopy(new);new_bad[0]['query']='Selene'
add('fragment-inherited-stale-rejected','Actually from Dorian',new_bad,[],email_context)

prior=[{'role':'user','content':'Read email from Selene for 2026-10-02; read email from Dorian for 2026-10-03'},
    {'role':'assistant','content':'Synthetic two-email receipt','tool_digest':'view_emails'}]
newdates=[s('email',query='Selene',time={'named':'tomorrow'}),s('email',query='Dorian',time={'named':'tomorrow'})]
add('repeated-shared-correction-positive','Same for tomorrow',newdates,calls_for(newdates),prior)
olddates=[s('email',query='Selene',time={'date':'2026-10-02'}),s('email',query='Dorian',time={'date':'2026-10-03'})]
add('repeated-shared-correction-stale-rejected','Same for tomorrow',olddates,[],prior)

add('calendar-alias-positive','Read appointments that are on our calendar tomorrow',
    [s('calendar',time={'named':'tomorrow'})],[('get_upcoming',{'period':'tomorrow','calendar_only':True})])
add('email-alias-positive','Read emails that are in the inbox from yesterday',
    [s('email',time={'named':'yesterday'})],[('view_emails',{'period':'yesterday'})])
add('messages-alias-positive','Recap texts in our messages',[s('messages','overview')],[('summarize_messages',{})])
add('unclear-alias-scopes-rejected','Read appointments tomorrow calendar next week',
    [s('calendar',time={'named':'tomorrow'}),s('calendar',time={'named':'next week'})],[])
add('alias-repeated-scope-positive','Read appointments on my calendar tomorrow; read appointments in the calendar next week',
    [s('calendar',time={'named':'tomorrow'}),s('calendar',time={'named':'next week'})],
    [('get_upcoming',{'period':'tomorrow','calendar_only':True}),('get_upcoming',{'period':'next week','calendar_only':True})])
add('alias-repeated-missing-rejected','Read appointments on my calendar tomorrow; read appointments in the calendar next week',
    [s('calendar',time={'named':'tomorrow'})],[])

add('mixed-source-filter-positive','Show my calendar tomorrow; find notes about amber notebooks from yesterday',
    [s('calendar',time={'named':'tomorrow'}),s('notes',query='amber notebooks',time={'named':'yesterday'})],
    [('get_upcoming',{'period':'tomorrow','calendar_only':True}),('search_notes',{'query':'amber notebooks','period':'yesterday'})])
add('mixed-source-no-filter-positive','Read email today plus messages yesterday',
    [s('email',time={'named':'today'}),s('messages',time={'named':'yesterday'})],
    [('view_emails',{'period':'today'}),('view_messages',{'period':'yesterday'})])
add('reminder-repeated-positive','Read overdue reminders named "dawn task"; read upcoming reminders named "dusk task"',
    [s('reminders',query='dawn task',scope='overdue'),s('reminders',query='dusk task',scope='upcoming')],
    [('search_reminders',{'query':'dawn task','scope':'past_due'}),('search_reminders',{'query':'dusk task','scope':'upcoming'})])
add('reminder-repeated-swap-rejected','Read overdue reminders named "dawn task"; read upcoming reminders named "dusk task"',
    [s('reminders',query='dawn task',scope='upcoming'),s('reminders',query='dusk task',scope='overdue')],[])
add('duration-repeated-positive','Find a 75 minute free slot on my calendar tomorrow; find a 45 minute free slot on my calendar today',
    [s('calendar','free_time',time={'named':'tomorrow'},minutes=75),s('calendar','free_time',time={'named':'today'},minutes=45)],
    [('find_free_time',{'period':'tomorrow','minutes':75}),('find_free_time',{'period':'today','minutes':45})])
add('duration-repeated-swap-rejected','Find a 75 minute free slot on my calendar tomorrow; find a 45 minute free slot on my calendar today',
    [s('calendar','free_time',time={'named':'tomorrow'},minutes=45),s('calendar','free_time',time={'named':'today'},minutes=75)],[])
add('quoted-separator-data-positive','Find notes named "amber and violet"; find notes named "green then blue"',
    [s('notes',query='amber and violet'),s('notes',query='green then blue')],
    [('search_notes',{'query':'amber and violet'}),('search_notes',{'query':'green then blue'})])
add('original-date-omission-rejected','Read email for 2026-10-02',[s('email')],[])
add('original-overdue-omission-rejected','Read overdue reminders',[s('reminders')],[])
add('original-duration-omission-rejected','Find a 75 minute free slot on my calendar tomorrow',
    [s('calendar','free_time',time={'named':'tomorrow'})],[])
add('original-truncated-query-rejected','Find notes about amber notebooks',[s('notes',query='amber')],[])
add('unsupported-daypart-rejected','Read email tomorrow before noon',[s('email',time={'named':'tomorrow'})],[])
add('calendar-only-shortcut-positive','what is up tomorrow?',[],[('get_upcoming',{'period':'tomorrow','calendar_only':True})])
add('excluded-notes-positive','Read email without notes',[s('email')],[('view_emails',{})],excluded=['notes'])
add('excluded-notes-rejected','Read email without notes',[s('email'),s('notes')],[],excluded=['notes'])

rows=[]
for c in cases:
    planner.role_target=lambda role:TARGET
    prior_tools=[n.strip() for turn in c['context'] for n in turn.get('tool_digest','').split(',') if n.strip()]
    fake=FakeClient(c['intent_response'],c['intent_response'])
    plan=asyncio.run(planner.plan_read(c['prompt'],client=fake,config=CONFIG,
        context=c['context'],prior_tools=prior_tools,now=NOW))
    run=asyncio.run(e.run_case(c,state,candidate=True))
    request=router._classify_web_request(c['prompt'],None)
    classification={'delivery':None if request.delivery is None else {
        'text':request.delivery.text,'channel':request.delivery.channel,
        'recipient':request.delivery.recipient,'target_missing':request.delivery.target_missing},
        'authorized_effects':sorted(request.authorized_effects)}
    try:
        validate_intent(c['intent_response'],c['prompt'],context=c['context'],prior_tools=prior_tools,now=NOW)
        local_validation={'accepted':True}
    except InvalidIntent as error:
        local_validation={'accepted':False,'diagnosis':str(error)}
    actual=[(item['name'],item['args']) for item in run['executed_calls']]
    met=actual==c['expected_calls'] or c['allow_safe_clarify'] and not actual and plan.disposition=='clarify'
    rows.append({'case':c,'classification':classification,'local_validation':local_validation,
        'planner':{'disposition':plan.disposition,'calls':plan.calls,'response':plan.response,
        'client_calls':fake.calls},'run':run,'expectation_met':met})
    print(json.dumps({'id':c['id'],'disposition':plan.disposition,'actual':actual,'expectation_met':met}))
data={'exact_sha':'27490ae5055502423f187e84ca52d0064b794396','rows':rows,'total':len(rows),
    'expectations_met':sum(r['expectation_met'] for r in rows),'heldout_payload_opened':False,
    'actual_model_calls':False,'native_or_outbound_execution':False,'mode':'fresh synthetic actual-main/planner'}
(OUT/'independent-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
print(json.dumps({'total':data['total'],'expectations_met':data['expectations_met'],
    'failures':[r['case']['id'] for r in rows if not r['expectation_met']]}))
