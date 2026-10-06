"""Fresh auditor cases, authored without corpus or archived prompt access."""
import sys, json, asyncio, tempfile, copy, traceback
from pathlib import Path
from datetime import datetime
ROOT = Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT = Path('/private/tmp/wisp-router-98916cf-gates-20261005/auditor')
sys.path.insert(0, str(ROOT))
from scripts import eval_router_update as e
HEAD = e.git_revision()
state = Path(tempfile.mkdtemp(prefix='wisp-98916cf-main-')).resolve()
e.install_guard(state, OUT)
from service.router.intent import validate_intent, compile_intent, InvalidIntent
from service.router.intent.validation import applicable_read, source_requirements, _instruction_text
from service.router.web_request import classify
NOW = datetime(2026,10,5,12)
def s(domain, operation='overview', **kw):
    return dict(domain=domain,operation=operation,**kw)
def v(*sources, excluded=()):
    return dict(version=1,kind='read',sources=list(sources),excluded_sources=list(excluded),unsupported_constraints=[])
def call(name, **args): return dict(name=name,args=args)
cases=[]
def add(id,prompt,intent,expected=None,guard=False,**extra):
    cases.append(dict(id=id,prompt=prompt,intent_response=intent,expected_calls=expected,guard=guard,**extra))
for title in ['Guide to Gardening','road to recovery','Atlas to Orchard','Send Instructions','schedule calendar email messages reminders','share it with Mom']:
    prompt='Read email from Cassia and email about '+title
    data=v(s('email','records',query='Cassia'),s('email','records',query=title))
    add('title-'+str(len(cases)),prompt,data,[call('view_emails',query='Cassia',strict_match=True),call('view_emails',query=title,strict_match=True)])
    bad=copy.deepcopy(data); bad['sources'][1]['query']=title.split()[0]
    if bad!=data: add('title-truncated-'+str(len(cases)),prompt,bad,[])
for title in ['Guide to Gardening to Rowan','Road to Recovery to Morgan']:
    for q in [title,title.rsplit(' to ',1)[0]]:
        add('ambiguous-'+str(len(cases)),'Read email from Cassia and email about '+title,v(s('email','records',query='Cassia'),s('email','records',query=q)),[])
    add('quoted-multi-to-'+str(len(cases)),'Read email from Cassia and email about "'+title+'"',v(s('email','records',query='Cassia'),s('email','records',query=title)),[call('view_emails',query='Cassia',strict_match=True),call('view_emails',query=title,strict_match=True)])
tails=['and share it with Mom','plus share it with Mom','plus would you mind sharing it with Mom',
       'and then could you share it with Mom','& please share it with Mom','but share it with Mom',
       ', share it with Mom','; share it with Mom','. Share it with Mom','then forward it to Mom by email',
       'plus please email it to cassia@example.invalid','and afterwards send it to Mom',
       'plus save it in Notes','and move it to Archive','and update my reminder','and cancel my appointment',
       'and compose an email to Mom','plus mark my reminders complete']
for tail in tails:
    for q in ['amber route','amber route '+tail]:
        add('action-'+str(len(cases)),'Find notes about amber route '+tail,v(s('notes','records',query=q)),[],guard=True)
for tail in ['and do not share it with Mom','plus never share it with Mom','without sharing it with Mom',
             'and please do not email it to Mom','and don\'t send it to Mom']:
    add('negative-'+str(len(cases)),'Find notes about amber route '+tail,v(s('notes','records',query='amber route')),[call('search_notes',query='amber route')])
    add('negative-swallow-'+str(len(cases)),'Find notes about amber route '+tail,v(s('notes','records',query='amber route '+tail)),[])
for literal in ['amber route and share it with Mom','amber route plus would you mind sharing it with Mom',
                'amber route without email or messages','amber route to Recovery to Rowan']:
    add('quoted-notes-'+str(len(cases)),'Find notes about "'+literal+'"',v(s('notes','records',query=literal)),[call('search_notes',query=literal)])
for join in ['and','plus','and then']:
    p='Find notes about amber route without email '+join+' read messages from yesterday'
    good=v(s('notes','records',query='amber route'),s('messages','records',time={'named':'yesterday'}),excluded=['email'])
    add('exclusion-read-'+str(len(cases)),p,good,[call('search_notes',query='amber route'),call('view_messages',period='yesterday')])
    bad=copy.deepcopy(good); bad['sources'].pop();bad['excluded_sources'].append('messages')
    add('exclusion-dropped-'+str(len(cases)),p,bad,[])
add('source-list','Find notes about amber route without email or messages',v(s('notes','records',query='amber route'),excluded=['email','messages']),[call('search_notes',query='amber route')])
for dest in ['Mom','cassia@example.invalid','+15550100404']:
    add('outer-destination-'+str(len(cases)),'Read email from Cassia and email about Guide to Gardening to '+dest,v(s('email','records',query='Cassia'),s('email','records',query='Guide to Gardening')),[],guard=True)
add('recap-bare','Recap email',v(s('email')),[call('summarize_emails')])
add('recap-mixed','Recap email and texts',v(s('email'),s('messages')),[call('summarize_emails'),call('summarize_messages')])
add('mixed-records','Find notes about amber route and recap my messages',v(s('notes','records',query='amber route'),s('messages')),[call('search_notes',query='amber route'),call('summarize_messages')])
add('literal-domains','Find notes about calendar send email reminders texts',v(s('notes','records',query='calendar send email reminders texts')),[call('search_notes',query='calendar send email reminders texts')])
add('invented-domain','Find notes about calendar send email reminders texts',v(s('notes','records',query='calendar send email reminders texts'),s('calendar')),[])
paired=[s('email','records',query='Cassia',time={'named':'yesterday'},count=2),s('email','records',query='Nerys',time={'named':'today'},count=3)]
p='Read 2 email from Cassia for yesterday and 3 email from Nerys for today'
goodcalls=[call('view_emails',query='Cassia',strict_match=True,period='yesterday',count=2),call('view_emails',query='Nerys',strict_match=True,period='today',count=3)]
add('tuple-good',p,v(*paired),goodcalls)
add('tuple-reordered',p,v(*reversed(paired)),list(reversed(goodcalls)))
for field in ['query','time','count']:
    bad=copy.deepcopy(paired);bad[0][field],bad[1][field]=bad[1][field],bad[0][field]
    add('tuple-swapped-'+field,p,v(*bad),[])
for field in ['query','time','count']:
    bad=copy.deepcopy(paired);bad[0].pop(field)
    add('tuple-omitted-'+field,p,v(*bad),[])
context=[dict(role='user',content='Find 2 notes about violet sketches from yesterday'),dict(role='assistant',content='Synthetic violet results',tool_digest='search_notes')]
for query in ['amber route','violet sketches','amber']:
    add('context-'+query.replace(' ','-'),'Actually about amber route',v(s('notes','records',query=query,count=2,time={'named':'yesterday'})),[call('search_notes',query='amber route',count=2,period='yesterday')] if query=='amber route' else [],context=context)
for p,data,expected in [
    ('Read email for October 2026',v(s('email','records',time={'month':'2026-10'})),[call('view_emails',period='2026-10')]),
    ('Read email for October 2026',v(s('email','records')),[]),
    ('Read overdue reminders',v(s('reminders',scope='overdue')),[call('search_reminders',query='',scope='past_due')]),
    ('Find a 60 minute free slot on my calendar tomorrow',v(s('calendar','free_time',minutes=60,time={'named':'tomorrow'})),[call('find_free_time',minutes=60,period='tomorrow')]),
    ('Recap my calendar this week',v(s('calendar',time={'named':'this week'})),[call('get_upcoming',period='this week',calendar_only=True)]),
    ('Recap my inbox',v(s('email')),[call('summarize_emails')]),
    ('Recap my calender',v(s('calendar')),[call('get_upcoming',calendar_only=True)]),
    ('Recap unread messages',v(s('messages',unread=True)),[]),
]: add('retained-'+str(len(cases)),p,data,expected)
rows=[]
async def main():
    for c in cases:
        c['expected']={'calls':c['expected_calls'],'sources':[]}
        row={'case':c,'head':HEAD,'synthetic_only':True}
        try:
            try:
                intent=validate_intent(c['intent_response'],c['prompt'],context=c.get('context',[]),prior_tools=['search_notes'] if c.get('context') else [],now=NOW)
                row['validator']={'accepted':True,'calls':compile_intent(intent,now=NOW)[0]}
            except InvalidIntent as exc: row['validator']={'accepted':False,'reason':str(exc)}
            row['instruction']=_instruction_text(c['prompt'],now=NOW)
            row['source_requirements']=[sorted(x) for x in source_requirements(c['prompt'])]
            row['applicable']=applicable_read(c['prompt'],c.get('context',[]),['search_notes'] if c.get('context') else [])
            r=await e.run_case(c,state,candidate=True)
            row['run']=r
            actual=[{'name':x['name'],'args':x['args']} for x in r['executed_calls']]
            dispositions=[x.get('intent_disposition') for x in r['events'] if x['type']=='routed']
            errors=[x for x in r['events'] if x['type']=='error']
            row['actual_calls']=actual;row['dispositions']=dispositions
            row['ok']=actual==c['expected_calls'] and not errors
            if c['guard']:
                row['ok']=row['ok'] and 'compiled' not in dispositions and not any(x.get('kind')=='scripted_intent' for x in r['raw_model_io'])
        except Exception as exc:
            row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
        rows.append(row)
        print(json.dumps({'id':c['id'],'ok':row['ok'],'actual':row.get('actual_calls'),'dispositions':row.get('dispositions'),'error':row.get('error')}),flush=True)
    (OUT/'application-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(r['ok'] for r in rows),'rows':rows},indent=2))
asyncio.run(main())
