import sys,json,asyncio,copy,tempfile,re,traceback
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-44babb7-gates-20261005/auditor')
sys.path.insert(0,str(ROOT));from scripts import eval_router_update as e
HEAD=e.git_revision();assert HEAD=='44babb70ee2eea99897b9437b2f6d49b2d59adff'
STATE=Path(tempfile.mkdtemp(prefix='wisp-44babb7-supplemental-')).resolve();e.install_guard(STATE,OUT)
from service import main
from service.tasks import compiler as T
from service.assistant.store import AssistantStore
from service.router.intent import validate_intent,InvalidIntent
NOW=datetime(2026,10,5,12);rows=[];LAST={}
def source(d,op='records',**kw):return dict(domain=d,operation=op,**kw)
def intent(*ss,excluded=()):return dict(version=1,kind='read',sources=list(ss),excluded_sources=list(excluded),unsupported_constraints=[])
def cc(n,**kw):return dict(name=n,args=kw)
def check(label,f):
 LAST.clear();r=dict(id=label)
 try:r['evidence']=f();r['pass']=True
 except Exception as ex:r.update(error=repr(ex),trace=traceback.format_exc(),evidence=copy.deepcopy(LAST));r['pass']=False;print('FAIL',label,repr(ex),flush=True)
 rows.append(r)
async def run(prompt,data,expected,mode='read',context=(),assistant=None,result='Independent synthetic receipt.'):
 case=dict(id='supp-'+str(len(rows)),prompt=prompt,intent_response=data,fixture_answer=result,expected=dict(calls=expected,sources=[]),context=list(context),approve=mode=='task')
 capture={};original=main.agent
 async def observe(body):
  response=await original(body);stream=response.body_iterator
  async def it():
   async for item in stream:yield item
   capture['workflow']=copy.deepcopy(main.store.latest_workflow(body['session_id']));capture['active_task']=copy.deepcopy(main.store.active_task(body['session_id']));capture['latest_task']=copy.deepcopy(main.store.latest_task(body['session_id']))
  response.body_iterator=it();return response
 with patch.object(main,'agent',observe):
  if assistant is None:r=await e.run_case(case,STATE,candidate=True)
  else:
   with patch.object(main,'assistant_store',assistant):r=await e.run_case(case,STATE,candidate=True)
 r['persisted_state']=capture;LAST.update(case=case,result=r)
 assert [cc(x['name'],**x['args']) for x in r['executed_calls']]==expected,LAST
 assert capture['workflow'] is None,LAST
 if mode!='task':assert capture['active_task'] is None,LAST
 if mode=='clarify':assert any(x.get('intent_disposition')=='clarify' for x in r['events']),LAST
 elif mode=='task':
  assert any(x['type']=='task_plan' for x in r['events']),LAST
  assert capture['latest_task']['status']=='failed'
  assert r['answer']=='I couldn’t mark the reminder done. The requested change was not verified.'
 else:
  assert r['answer'].count(result)==len(expected),LAST
  assert not any('workflow' in x['type'] or x['type']=='task_plan' for x in r['events']),LAST
 assert not [x for x in r['events'] if x['type']=='error'],LAST
 return LAST.copy()
CASES=[
 ('calendar-only','Read appointments on my calendar tomorrow',intent(source('calendar',time={'named':'tomorrow'})),[cc('get_upcoming',period='tomorrow',calendar_only=True)],'read'),
 ('calendar-alias','Show appointments that are in the calendar tomorrow',intent(source('calendar',time={'named':'tomorrow'})),[cc('get_upcoming',period='tomorrow',calendar_only=True)],'read'),
 ('calendar-additional-unauthorized','Read appointments on my calendar tomorrow',intent(source('calendar',time={'named':'tomorrow'}),source('reminders',time={'named':'tomorrow'})),[],'clarify'),
 ('mixed-lookup-overview','Read email from Rowan and recap my texts',intent(source('email',query='Rowan'),source('messages','overview')),[cc('view_emails',query='Rowan',strict_match=True),cc('summarize_messages')],'read'),
 ('mixed-missing-messages','Read email from Rowan and recap my texts',intent(source('email',query='Rowan')),[],'clarify'),
 ('scope-exact-email','Read email for 2026-10-01',intent(source('email',time={'date':'2026-10-01'})),[cc('view_emails',period='2026-10-01')],'read'),
 ('scope-omitted-email','Read email for 2026-10-01',intent(source('email')),[],'clarify'),
 ('unread-count-account','Read two unread emails from Rowan in account "Office" for 2026-10-01',intent(source('email',query='Rowan',unread=True,count=2,account='Office',time={'date':'2026-10-01'})),[cc('view_emails',query='Rowan',unread=True,count=2,account='Office',period='2026-10-01',strict_match=True)],'read'),
 ('messages-strict-query','Read messages about harbor pickup',intent(source('messages',query='harbor pickup')),[cc('view_messages',query='harbor pickup',strict_match=True)],'read'),
 ('notes-date-count','Find five notes about violet bridges from yesterday',intent(source('notes',query='violet bridges',count=5,time={'named':'yesterday'})),[cc('search_notes',query='violet bridges',count=5,period='yesterday')],'read'),
 ('free-time-duration','Find a 75 minute free slot on my calendar tomorrow',intent(source('calendar','free_time',minutes=75,time={'named':'tomorrow'})),[cc('find_free_time',minutes=75,period='tomorrow')],'read'),
 ('free-time-duration-omitted','Find a 75 minute free slot on my calendar tomorrow',intent(source('calendar','free_time',time={'named':'tomorrow'})),[],'clarify'),
 ('unsupported-weekly-reminder','Read reminders this week',intent(source('reminders',time={'named':'this week'})),[],'clarify'),
 ('unsupported-summary-query','Recap email from Rowan',intent(source('email','overview',query='Rowan')),[],'clarify'),
 ('unsupported-summary-date-unread','Recap unread email from yesterday',intent(source('email','overview',unread=True,time={'named':'yesterday'})),[],'clarify'),
]
for label,p,d,x,m in CASES:check(label,lambda p=p,d=d,x=x,m=m:asyncio.run(run(p,d,x,m)))
for ap in ['’','‘','ʼ','＇']:
 literal='Rowan: don'+ap+'t read my email'
 p='Find notes about "'+literal+'"';bad=intent(source('notes',query=literal.replace(ap,"'")))
 check('raw-query-ascii-change-'+repr(ap),lambda p=p,d=bad:asyncio.run(run(p,d,[],mode='clarify')))
 context=[dict(role='user',content='Read email from Rowan'),dict(role='assistant',content='Prior synthetic email receipt',tool_digest='view_emails')]
 p='Find notes about violet bridges and don'+ap+'t check my email';good=intent(source('notes',query='violet bridges'),excluded=['email'])
 check('negative-check-over-context-'+repr(ap),lambda p=p,d=good,c=context:asyncio.run(run(p,d,[cc('search_notes',query='violet bridges')],context=c)))
# Same-head guard-only counterfactual isolates the new one-line task change.
old=re.compile(r"\b(?:do\s+not|don't|dont|never)\s+(?:set|add|create|make|schedule|send|give|remind|delete|remove|clear|complete|finish|mark|check|cross|tick|cancel|update|change|rename|reschedule|move)\b",re.I)
for ap in ['’','‘','ʼ','＇']:
 for kind in ['previous-guard','current-positive-name']:
  def paired(ap=ap,kind=kind):
   title='Archive: don'+ap+'t check this item' if kind=='previous-guard' else 'Archive D'+ap+'Angelo'
   p='Complete reminder called "'+title+'"';astore=AssistantStore(STATE/('task-'+str(len(rows))+'.db'))
   try:
    item=astore.add_manual(title,(NOW+timedelta(days=1)).timestamp());expected=[cc('complete_reminder',title=title,expected_id=item['id'])]
    data=intent(source('reminders'))
    if kind=='previous-guard':
     with patch.object(T,'_NEGATED',old):r=asyncio.run(run(p,data,expected,mode='task',assistant=astore))
    else:r=asyncio.run(run(p,data,expected,mode='task',assistant=astore))
    return dict(kind=kind,comparison='Current exact head; previous ASCII guard injected only in memory for counterfactual',evidence=r)
   finally:astore._db.close()
  check('R10-paired-'+repr(ap)+'-'+kind,paired)
# Failed source receipts stay literal and disclose failure through the application.
check('source-error-faithful',lambda:asyncio.run(run('Find notes about violet bridges',intent(source('notes',query='violet bridges')),[cc('search_notes',query='violet bridges')],result='(error: synthetic Notes permission unavailable)')))
(OUT/'supplemental-results.json').write_text(json.dumps(dict(head=HEAD,state=str(STATE),passed=sum(r['pass'] for r in rows),total=len(rows),rows=rows),indent=2,ensure_ascii=False,default=str))
print(json.dumps(dict(passed=sum(r['pass'] for r in rows),total=len(rows),failures=[dict(id=r['id'],error=r.get('error')) for r in rows if not r['pass']]),ensure_ascii=False))
