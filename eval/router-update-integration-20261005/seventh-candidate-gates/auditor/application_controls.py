"""New auditor-owned synthetic cases; never imports corpus or prior scripts."""
import sys,json,asyncio,tempfile,copy,traceback
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
from dataclasses import asdict
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-8dfd5c9-gates-20261005/auditor');sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-8dfd5c9-main-')).resolve();e.install_guard(state,OUT)
from service import main
from service.router.intent import validate_intent,compile_intent,InvalidIntent
from service.router.intent.validation import applicable_read,source_requirements,_instruction_text
from service.router.web_request import classify,_effect_clauses
from service.workflows.compiler import outbound_verb
NOW=datetime(2026,10,5,12)
def src(domain,operation='records',**kw):return dict(domain=domain,operation=operation,**kw)
def intent(*sources,excluded=()):return dict(version=1,kind='read',sources=list(sources),excluded_sources=list(excluded),unsupported_constraints=[])
def call(name,**args):return dict(name=name,args=args)
cases=[]
def add(family,prompt,data,calls,mode='read',**kw):cases.append(dict(id=family+'-'+str(len(cases)),family=family,prompt=prompt,intent_response=data,expected_calls=calls,mode=mode,**kw))
notes=src('notes',query='copper atlas');nc=call('search_notes',query='copper atlas')
negative_tails=['do not share it with Mom','please do not share it with Mom','never send it to Mom','do not forward it by email to Mom','without sharing it with Mom','would you mind not sharing it with Mom','I would prefer not to send it to Mom','I prefer not to forward it by email to Mom','I’d prefer not to share it with Mom',"don't share it with Mom",'don’t share it with Mom','donʼt share it with Mom','do not delete it']
for tail in negative_tails:
 add('negative','Find notes about copper atlas and '+tail,intent(notes),[nc])
for join in ['and','plus','and then','and afterwards','afterwards','&',';']:
 for neg in ['do not delete it','I’d prefer not to share it with Mom']:
  for positive in ['update my reminders','mark my reminders complete','clear my reminders','send it by email to Rowan']:
   add('later-effect','Find notes about copper atlas and '+neg+' '+join+' '+positive,intent(notes),[],mode='guard')
 for good in [True,False]:
  data=intent(notes,src('reminders')) if good else intent(notes)
  add('later-read','Find notes about copper atlas and do not share it with Mom '+join+' read my reminders',data,[nc,call('search_reminders',query='',scope='all')] if good else [],mode='read' if good else 'clarify')
for tail in ['share it with Mom','please send it by email to Rowan','would you mind sharing it with Mom','save it in Notes']:
 add('positive-share','Find notes about copper atlas plus '+tail,intent(notes),[],mode='guard')
for q in ['Guide to Gardening','Road to Recovery','Send Instructions','share it with Mom','calendar email reminders texts']:
 p='Read email from Selene and email about '+q
 add('governed-title',p,intent(src('email',query='Selene'),src('email',query=q)),[call('view_emails',query='Selene',strict_match=True),call('view_emails',query=q,strict_match=True)])
 add('truncated-title',p,intent(src('email',query='Selene'),src('email',query=q.split()[0])),[],mode='clarify')
for q,quoted in [('Guide to Gardening to Rowan',False),('Guide to Gardening to Rowan',True)]:
 p='Read email from Selene and email about '+('"'+q+'"' if quoted else q)
 add('multi-to',p,intent(src('email',query='Selene'),src('email',query=q)),[call('view_emails',query='Selene',strict_match=True),call('view_emails',query=q,strict_match=True)] if quoted else [],mode='read' if quoted else 'clarify')
for q in ['copper atlas and do not share it with Mom and afterwards read my reminders','I’d prefer not to send it to Mom','copper atlas and do not delete it and update my reminders']:
 add('quoted-effects','Find notes about "'+q+'"',intent(src('notes',query=q)),[call('search_notes',query=q)])
for apostrophe in ["'",'’','ʼ','＇']:
 tail='don'+apostrophe+'t read my email'
 p='Find notes about copper atlas and '+tail
 add('excluded-good',p,intent(notes,excluded=['email']),[nc])
 add('excluded-extra',p,intent(notes,src('email','overview')),[],mode='clarify')
for join in ['and','plus','and afterwards','&']:
 p='Find notes about copper atlas without email '+join+' read messages'
 add('excluded-later',p,intent(notes,src('messages'),excluded=['email']),[nc,call('view_messages')])
 add('excluded-omitted',p,intent(notes,excluded=['email']),[],mode='clarify')
add('literal-domains','Find notes about send email calendar reminders texts',intent(src('notes',query='send email calendar reminders texts')),[call('search_notes',query='send email calendar reminders texts')])
add('literal-extra','Find notes about send email calendar reminders texts',intent(src('notes',query='send email calendar reminders texts'),src('email','overview')),[],mode='clarify')
for p,data,calls in [('Recap email',intent(src('email','overview')),[call('summarize_emails')]),('Recap email and texts',intent(src('email','overview'),src('messages','overview')),[call('summarize_emails'),call('summarize_messages')]),('Recap my inbox',intent(src('email','overview')),[call('summarize_emails')]),('Recap my calender',intent(src('calendar','overview')),[call('get_upcoming',calendar_only=True)]),('Recap unread messages',intent(src('messages','overview',unread=True)),[])]:add('retained-alias',p,data,calls,mode='read' if calls else 'clarify')
p='Read 2 email from Selene for yesterday and 3 email from Petra for today'
sources=[src('email',query='Selene',time={'named':'yesterday'},count=2),src('email',query='Petra',time={'named':'today'},count=3)]
cs=[call('view_emails',query='Selene',strict_match=True,count=2,period='yesterday'),call('view_emails',query='Petra',strict_match=True,count=3,period='today')]
add('tuple-good',p,intent(*sources),cs);add('tuple-reverse',p,intent(*reversed(sources)),list(reversed(cs)))
for field in ['query','time','count']:
 bad=copy.deepcopy(sources);bad[0][field],bad[1][field]=bad[1][field],bad[0][field];add('tuple-swap-'+field,p,intent(*bad),[],mode='clarify')
 bad=copy.deepcopy(sources);bad[0].pop(field);add('tuple-omit-'+field,p,intent(*bad),[],mode='clarify')
context=[dict(role='user',content='Find 2 notes about blue lanterns from yesterday'),dict(role='assistant',content='Synthetic notes result.',tool_digest='search_notes')]
for q in ['copper atlas','blue lanterns','copper']:
 add('context-replace','Actually about copper atlas',intent(src('notes',query=q,count=2,time={'named':'yesterday'})),[call('search_notes',query='copper atlas',count=2,period='yesterday')] if q=='copper atlas' else [],mode='read' if q=='copper atlas' else 'clarify',context=context)
for p,data,calls in [('Recap my calendar this week',intent(src('calendar','overview',time={'named':'this week'})),[call('get_upcoming',period='this week',calendar_only=True)]),('Read overdue reminders',intent(src('reminders','overview',scope='overdue')),[call('search_reminders',query='',scope='past_due')]),('Read email for October 2026',intent(src('email',time={'month':'2026-10'})),[call('view_emails',period='2026-10')]),('Find a 75 minute free slot on my calendar tomorrow',intent(src('calendar','free_time',time={'named':'tomorrow'},minutes=75)),[call('find_free_time',period='tomorrow',minutes=75)])]:add('retained-constraint',p,data,calls)
rows=[]
async def execute(c):
 c['expected']={'calls':c['expected_calls'],'sources':[]};c['fixture_answer']='Verified synthetic '+c['id']
 captured={};original=main.agent
 async def agent(body):
  response=await original(body);iterator=response.body_iterator
  async def observe():
   async for item in iterator:yield item
   captured['latest_workflow']=copy.deepcopy(main.store.latest_workflow(body['session_id']))
  response.body_iterator=observe();return response
 with patch.object(main,'agent',agent):r=await e.run_case(c,state,candidate=True)
 return r,captured
async def run():
 for c in cases:
  row={'case':c}
  try:
   row.update(applicable=applicable_read(c['prompt'],c.get('context',[]),['search_notes'] if c.get('context') else []),source_requirements=[sorted(x) for x in source_requirements(c['prompt'])],outbound=outbound_verb(c['prompt']),instruction=_instruction_text(c['prompt'],now=NOW),clauses=[asdict(x) for x in _effect_clauses(c['prompt'])])
   try:
    val=validate_intent(c['intent_response'],c['prompt'],context=c.get('context',[]),prior_tools=['search_notes'] if c.get('context') else [],now=NOW);row['validator']={'accepted':True,'calls':compile_intent(val,now=NOW)[0]}
   except InvalidIntent as exc:row['validator']={'accepted':False,'reason':str(exc)}
   r,captured=await execute(c);row['run']=r;row['store']=captured
   actual=[{'name':x['name'],'args':x['args']} for x in r['executed_calls']];row['actual']=actual
   ds=[x.get('intent_disposition') for x in r['events'] if x['type']=='routed'];row['dispositions']=ds
   row['ok']=actual==c['expected_calls'] and not any(x['type']=='error' for x in r['events'])
   if c['mode']=='read':row['ok']=row['ok'] and captured.get('latest_workflow') is None and not any('workflow' in x['type'] for x in r['events']) and c['fixture_answer'] in r['answer']
   elif c['mode']=='clarify':row['ok']=row['ok'] and 'clarify' in ds and captured.get('latest_workflow') is None
   else:row['ok']=row['ok'] and 'compiled' not in ds and not row['validator']['accepted'] and not row['applicable'] and not any(x.get('kind')=='scripted_intent' for x in r['raw_model_io'])
  except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
  rows.append(row);print(json.dumps({'id':c['id'],'ok':row['ok'],'calls':row.get('actual'),'dispositions':row.get('dispositions'),'workflow':row.get('store',{}).get('latest_workflow',{}),'error':row.get('error')}),flush=True)
 (OUT/'application-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
asyncio.run(run())
