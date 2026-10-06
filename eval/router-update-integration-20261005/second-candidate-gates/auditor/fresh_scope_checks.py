"""Independently authored synthetic cases: no corpus, real models or tools."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from datetime import datetime

ROOT = Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT = Path('/private/tmp/wisp-router-40ee88a-gates-20261005/auditor')
sys.path.insert(0, str(ROOT))
from scripts import eval_router_update as e
state = Path(tempfile.mkdtemp(prefix='wisp-40ee88a-independent-scope-')).resolve()
e.install_guard(state, OUT)
from service.router.intent import planner
from tests.test_router_intent_core import FakeClient, CONFIG, TARGET

NOW = datetime(2026,10,5,12)
def source(domain, operation='records', **fields):
    return {'domain':domain, 'operation':operation, **fields}
def intent(*sources):
    return {'version':1, 'kind':'read', 'sources':list(sources),
        'excluded_sources':[], 'unsupported_constraints':[]}
def case(identity, prompt, sources, expected_calls, context=None):
    return {'id':identity,'prompt':prompt,'intent_response':intent(*sources),
        'context':context or [],'clock':'2026-10-05T12:00:00-07:00',
        'fixture_answer':'Independent synthetic fixture result.',
        'expected_calls':expected_calls}

cases = [
    case('date-omission','Read email for 2026-10-03',[source('email')],[]),
    case('date-positive','Read email for 2026-10-03',[source('email',time={'date':'2026-10-03'})],
        [('view_emails',{'period':'2026-10-03'})]),
    case('month-omission','Read email for November 2026',[source('email')],[]),
    case('month-positive','Read email for November 2026',[source('email',time={'month':'2026-11'})],
        [('view_emails',{'period':'2026-11'})]),
    case('overdue-omission','Read overdue reminders',[source('reminders')],[]),
    case('overdue-all-contradiction','Read overdue reminders',[source('reminders',scope='all')],[]),
    case('overdue-positive','Read overdue reminders',[source('reminders',scope='overdue')],
        [('search_reminders',{'query':'','scope':'past_due'})]),
    case('duration-omission','Find a 75 minute free slot on my calendar tomorrow',
        [source('calendar','free_time',time={'named':'tomorrow'})],[]),
    case('duration-operation-wrong','Find a 75 minute free slot on my calendar tomorrow',
        [source('calendar',time={'named':'tomorrow'},query='free slot')],[]),
    case('duration-positive','Find a 75 minute free slot on my calendar tomorrow',
        [source('calendar','free_time',time={'named':'tomorrow'},minutes=75)],
        [('find_free_time',{'period':'tomorrow','minutes':75})]),
    case('query-truncated','Find notes about cedar manuscripts',[source('notes',query='cedar')],[]),
    case('query-positive','Find notes about cedar manuscripts',[source('notes',query='cedar manuscripts')],
        [('search_notes',{'query':'cedar manuscripts'})]),
    case('query-suffix-positive','Find notes about cedar manuscripts from yesterday limit to four',
        [source('notes',query='cedar manuscripts',time={'named':'yesterday'},count=4)],
        [('search_notes',{'query':'cedar manuscripts','count':4,'period':'yesterday'})]),
    case('independent-domains-date-swapped','Read email for 2026-10-03 and notes for 2026-10-04',
        [source('email',time={'date':'2026-10-04'}),source('notes',time={'date':'2026-10-03'})],[]),
    case('independent-domains-positive','Read email for 2026-10-03 and notes for 2026-10-04',
        [source('email',time={'date':'2026-10-03'}),source('notes',time={'date':'2026-10-04'})],
        [('view_emails',{'period':'2026-10-03'}),('search_notes',{'period':'2026-10-04'})]),
    case('repeated-domain-date-swapped','Read email from Elara for 2026-10-03; read email from Tobias for 2026-10-04',
        [source('email',query='Elara',time={'date':'2026-10-04'}),source('email',query='Tobias',time={'date':'2026-10-03'})],[]),
    case('repeated-domain-positive','Read email from Elara for 2026-10-03; read email from Tobias for 2026-10-04',
        [source('email',query='Elara',time={'date':'2026-10-03'}),source('email',query='Tobias',time={'date':'2026-10-04'})],
        [('view_emails',{'query':'Elara','strict_match':True,'period':'2026-10-03'}),
         ('view_emails',{'query':'Tobias','strict_match':True,'period':'2026-10-04'})]),
    case('fragment-query-stale','Actually about birch diagrams',[source('notes',query='cedar manuscripts')],[],
        [{'role':'user','content':'Find notes about cedar manuscripts'},
         {'role':'assistant','content':'Synthetic prior results','tool_digest':'search_notes'}]),
    case('fragment-query-positive','Actually about birch diagrams',[source('notes',query='birch diagrams')],
        [('search_notes',{'query':'birch diagrams'})],
        [{'role':'user','content':'Find notes about cedar manuscripts'},
         {'role':'assistant','content':'Synthetic prior results','tool_digest':'search_notes'}]),
    case('source-named-query-correction','Actually find notes about birch diagrams',[source('notes',query='birch diagrams')],
        [('search_notes',{'query':'birch diagrams'})],
        [{'role':'user','content':'Find notes about cedar manuscripts'},
         {'role':'assistant','content':'Synthetic prior results','tool_digest':'search_notes'}]),
    case('tomorrow-calendar-shortcut','what is up tomorrow?',[],[('get_upcoming',{'period':'tomorrow','calendar_only':True})]),
    case('explicit-calendar-shortcut','show my calendar this week',[],[('get_upcoming',{'period':'this week','calendar_only':True})]),
    case('combined-agenda-positive','show my agenda tomorrow',[],[('get_upcoming',{'period':'tomorrow'})]),
    case('unsupported-daypart','Read email tomorrow before noon',[source('email',time={'named':'tomorrow'})],[]),
    case('same-source-date-correction','Same for tomorrow',[source('email',time={'named':'tomorrow'})],
        [('view_emails',{'period':'tomorrow'})],
        [{'role':'user','content':'Read email yesterday'},
         {'role':'assistant','content':'Synthetic prior results','tool_digest':'view_emails'}]),
]
rows=[]
for c in cases:
    # Safe clarification is acceptable when bounds of a contextual query are
    # unsupported; executing the stale query is not. Keep intended calls visible.
    c['allow_safe_clarify'] = c['id'] == 'fragment-query-positive'
    prior_tools=[name for turn in c['context'] for name in turn.get('tool_digest','').split(',') if name]
    planner.role_target=lambda role: TARGET
    fake=FakeClient(c['intent_response'],c['intent_response'])
    planned=asyncio.run(planner.plan_read(c['prompt'],client=fake,config=CONFIG,
        context=c['context'],prior_tools=prior_tools,now=NOW))
    run=asyncio.run(e.run_case(c,state,candidate=True))
    actual=[(item['name'],item['args']) for item in run['executed_calls']]
    row={'case':c,'planner':{'disposition':planned.disposition,'calls':planned.calls,
        'response':planned.response,'synthetic_client_calls':fake.calls},'run':run,
        'expectation_met':actual == c['expected_calls'] or
            (c['allow_safe_clarify'] and not actual and planned.disposition == 'clarify')}
    rows.append(row)
    print(json.dumps({'id':c['id'],'planner':planned.disposition,'actual_calls':actual,
        'expected_calls':c['expected_calls'],'expectation_met':row['expectation_met'],
        'errors':[item for item in run['events'] if item.get('type')=='error']}))
data={'exact_sha':'40ee88a409eaec943ae4474130bd87263ea794e1', 'mode':'fresh synthetic; actual main.agent; all tool callables fake',
    'heldout_payload_read':False,'actual_model_calls':False,'native_or_outbound_execution':False,
    'rows':rows,'expectations_met':sum(row['expectation_met'] for row in rows),'total':len(rows)}
(OUT/'fresh-scope-results.json').write_text(json.dumps(data,indent=2,default=str)+'\n')
