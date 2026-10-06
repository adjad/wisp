import sys, os, json, copy, asyncio, tempfile, traceback
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-44babb7-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision()
assert HEAD=='44babb70ee2eea99897b9437b2f6d49b2d59adff'
STATE=Path(tempfile.mkdtemp(prefix='wisp-44babb7-independent-')).resolve()
e.install_guard(STATE,OUT)
from service import main
from service.router.intent import validate_intent,compile_intent,InvalidIntent
from service.router.intent.validation import source_requirements,applicable_read
from service.tasks.compiler import compile_task
from service.assistant.store import AssistantStore
from service.router.web_request import classify
from service.workflows.compiler import compile_new,outbound_verb
NOW=datetime(2026,10,5,12)
rows=[]
def source(d,op='records',**kw): return dict(domain=d,operation=op,**kw)
def intent(*ss,excluded=()): return dict(version=1,kind='read',sources=list(ss),excluded_sources=list(excluded),unsupported_constraints=[])
def cc(name,**args): return dict(name=name,args=args)
def save():
 (OUT/'application-results.json').write_text(json.dumps(dict(head=HEAD,state=str(STATE),rows=rows,passed=sum(x['pass'] for x in rows),total=len(rows)),indent=2,ensure_ascii=False,default=str))
def check(label,f):
 row=dict(id=label)
 try: row['evidence']=f();row['pass']=True
 except Exception as ex: row.update(pass_=False,error=repr(ex),trace=traceback.format_exc());row['pass']=False
 rows.append(row)
 if not row['pass']: print('FAIL',label,row.get('error'),flush=True)
 if len(rows)%40==0: save();print('progress',len(rows),flush=True)
async def application(prompt,data,expected,mode='read',context=(),assistant=None,exact_answer=None):
 case=dict(id='case-'+str(len(rows)),prompt=prompt,intent_response=data,fixture_answer='Independent synthetic result.',expected=dict(calls=expected,sources=[]),context=list(context))
 if mode=='task': case['approve']=True
 captured={};original=main.agent
 async def observe_agent(body):
  response=await original(body);stream=response.body_iterator
  async def stream_and_state():
   async for item in stream: yield item
   captured['workflow']=copy.deepcopy(main.store.latest_workflow(body['session_id']))
   captured['active_task']=copy.deepcopy(main.store.active_task(body['session_id']))
   captured['latest_task']=copy.deepcopy(main.store.latest_task(body['session_id']))
  response.body_iterator=stream_and_state();return response
 with patch.object(main,'agent',observe_agent):
  if assistant is None: r=await e.run_case(case,STATE,candidate=True)
  else:
   with patch.object(main,'assistant_store',assistant): r=await e.run_case(case,STATE,candidate=True)
 actual=[dict(name=c['name'],args=c['args']) for c in r['executed_calls']]
 r['persisted_state']=captured
 assert actual==expected,dict(expected=expected,actual=actual,answer=r['answer'])
 assert not [x for x in r['events'] if x['type']=='error'],r['events']
 assert captured.get('workflow') is None,captured
 if mode!='task': assert captured.get('active_task') is None,captured
 if mode=='read':
  assert any(x.get('intent_disposition')=='compiled' for x in r['events']),r['events']
  target=exact_answer or ('Independent synthetic result.' if len(expected)==1 else None)
  if target is not None: assert r['answer']==target,r['answer']
  else: assert r['answer'].count('Independent synthetic result.')==len(expected),r['answer']
  assert not any('workflow' in x['type'] or x['type']=='task_plan' for x in r['events']),r['events']
 elif mode=='clarify':
  assert any(x.get('intent_disposition')=='clarify' for x in r['events']),r['events']
 elif mode=='guard':
  assert not any(x.get('intent_disposition')=='compiled' for x in r['events']),r['events']
  assert not any(x.get('kind')=='scripted_intent' for x in r['raw_model_io']),r['raw_model_io']
 elif mode=='task':
  assert any(x['type']=='task_plan' for x in r['events']),r['events']
  assert not any(x.get('kind')=='scripted_intent' for x in r['raw_model_io'])
  assert captured['latest_task'] is not None,captured
 return dict(prompt=prompt,intent=data,expected_calls=expected,result=r)
def app(prompt,data,calls,**kw): return asyncio.run(application(prompt,data,calls,**kw))
AP=["'",'’','‘','ʼ','＇'];DOMAINS=['calendar','reminders','email','messages','notes']
for ap in AP:
 for domain in DOMAINS:
  for verb in ['read','include','check']:
   prefix='Read email from D’Angelo' if domain=='notes' else 'Find notes about violet bridges'
   good=source('email',query='D’Angelo') if domain=='notes' else source('notes',query='violet bridges')
   expected=[cc('view_emails',query='D’Angelo',strict_match=True)] if domain=='notes' else [cc('search_notes',query='violet bridges')]
   prompt=prefix+' and don'+ap+'t '+verb+' my '+domain
   data=intent(good,excluded=[domain])
   label=repr(ap)+'-'+domain+'-'+verb
   def pure(p=prompt,d=data,ex=expected,dom=domain,allowed=good['domain']):
    assert source_requirements(p)==({allowed},{dom}),source_requirements(p)
    assert [cc(n,**a) for n,a in compile_intent(validate_intent(d,p,now=NOW),now=NOW)[0]]==ex
    return dict(prompt=p,intent=d,calls=ex)
   check('R9-core-'+label,pure)
   check('R9-main-good-'+label,lambda p=prompt,d=data,x=expected:app(p,d,x))
   for mutation in ['omitted','extra','extra-no-exclusion']:
    bad=copy.deepcopy(data)
    if mutation=='omitted': bad['sources']=[]
    else:
     bad['sources'].append(source(domain,'overview'))
     if mutation=='extra-no-exclusion': bad['excluded_sources']=[]
    check('R9-main-'+mutation+'-'+label,lambda p=prompt,d=bad:app(p,d,[],mode='clarify'))
for ap in AP:
 prompt='Find notes about violet bridges don'+ap+'t read my email or messages and afterwards read my reminders'
 data=intent(source('notes',query='violet bridges'),source('reminders'),excluded=['email','messages'])
 expected=[cc('search_notes',query='violet bridges'),cc('search_reminders',query='',scope='all')]
 check('R9-source-list-'+repr(ap),lambda p=prompt,d=data,x=expected:app(p,d,x))
 literal='D’Angelo: don'+ap+'t check my reminders or email'
 prompt='Find notes about "'+literal+'"'
 check('R9-literal-'+repr(ap),lambda p=prompt,q=literal:app(p,intent(source('notes',query=q)),[cc('search_notes',query=q)]))
 def targets(ap=ap):
  neg='don'+ap+'t check my reminders'
  assert compile_task(neg,now=NOW) is None
  name='D'+ap+'Angelo'
  pos=compile_task('Complete reminder called "'+name+'"',now=NOW)
  assert pos.intent=='reminder.complete' and pos.target.value==name,pos
  return dict(negative=neg,positive=pos.to_dict())
 check('R9-negative-and-positive-task-'+repr(ap),targets)
 # A matched, isolated manual reminder permits a supported positive operation.
 def positive_task(ap=ap):
  astore=AssistantStore(STATE/('assistant-'+str(len(rows))+'.db'))
  try:
   name='D'+ap+'Angelo';item=astore.add_manual(name,(NOW+timedelta(days=1)).timestamp())
   return app('Complete reminder called "'+name+'"',intent(source('reminders')),[cc('complete_reminder',id=item['id'],expected_ids=[item['id']])],mode='task',assistant=astore)
  finally: astore._db.close()
 check('R9-positive-task-main-'+repr(ap),positive_task)
negative_tails=['do not share it with Avery','never share it with Avery','please do not share it with Avery','do not forward it by email to Avery','never send it to Avery','do not email it to Avery','without sharing it with Avery','would you mind not sharing it with Avery','do not delete it','I would prefer not to send it to Avery','I prefer not to forward it by email to Avery','I’d prefer not to share it with Avery']
for tail in negative_tails:
 p='Find notes about violet bridges and '+tail
 check('R7-negative-'+tail,lambda p=p:app(p,intent(source('notes',query='violet bridges')),[cc('search_notes',query='violet bridges')]))
for joiner in ['and','and then','plus','and afterwards','afterwards','&']:
 p='Find notes about violet bridges and do not share it with Avery '+joiner+' read my reminders'
 check('R8-later-read-'+joiner,lambda p=p:app(p,intent(source('notes',query='violet bridges'),source('reminders')),[cc('search_notes',query='violet bridges'),cc('search_reminders',query='',scope='all')]))
 check('R8-later-read-omitted-'+joiner,lambda p=p:app(p,intent(source('notes',query='violet bridges')),[],mode='clarify'))
 for effect in ['update my reminders','mark my reminders complete','clear my reminders','send it by email to Kai']:
  p='Find notes about violet bridges and do not delete it '+joiner+' '+effect
  def coreguard(p=p):
   assert not applicable_read(p)
   try: validate_intent(intent(source('notes',query='violet bridges')),p,now=NOW)
   except InvalidIntent: return dict(prompt=p,guarded=True)
   raise AssertionError('positive effect accepted as read')
  check('R8-core-'+joiner+'-'+effect,coreguard)
  check('R8-main-'+joiner+'-'+effect,lambda p=p:app(p,intent(source('notes',query='violet bridges')),[],mode='guard'))
# Full occurrence tuple binding, independent fresh identities and dates.
p='Read two unread emails from Rowan in account "Office" for 2026-10-01; read three emails from Tessa in account "Home" for 2026-10-02'
good=intent(source('email',query='Rowan',account='Office',count=2,unread=True,time={'date':'2026-10-01'}),source('email',query='Tessa',account='Home',count=3,time={'date':'2026-10-02'}))
for field in ['time','query','account','count','unread','operation','missing']:
 bad=copy.deepcopy(good)
 if field=='missing': bad['sources'].pop()
 elif field=='operation': bad['sources'][0]['operation']='overview'
 elif field=='unread': bad['sources'][0].pop('unread');bad['sources'][1]['unread']=True
 else: bad['sources'][0][field],bad['sources'][1][field]=bad['sources'][1][field],bad['sources'][0][field]
 def tuplebad(d=bad):
  assert validate_intent(good,p,now=NOW)
  try: validate_intent(d,p,now=NOW)
  except InvalidIntent: return dict(prompt=p,good=good,bad=d,rejected=True)
  raise AssertionError('tuple mutation accepted')
 check('R1-tuple-'+field,tuplebad)
for q in ['Guide to Harbor Maps','road to the library','Topic to Rowan','Send Instructions']:
 p='Read email from Rowan and email about '+q
 data=intent(source('email',query='Rowan'),source('email',query=q))
 check('R4-governed-title-'+q,lambda p=p,d=data,q=q:app(p,d,[cc('view_emails',query='Rowan',strict_match=True),cc('view_emails',query=q,strict_match=True)]))
for fragment in ['Actually about lilac schedules','Instead named lilac schedules','Actually about "lilac schedules"']:
 context=[dict(role='user',content='Find five notes about violet bridges from yesterday'),dict(role='assistant',content='Synthetic prior note receipt',tool_digest='search_notes')]
 data=intent(source('notes',query='lilac schedules',count=5,time={'named':'yesterday'}))
 check('R2-current-query-'+fragment,lambda p=fragment,d=data,c=context:app(p,d,[cc('search_notes',query='lilac schedules',count=5,period='yesterday')],context=c))
 stale=intent(source('notes',query='violet bridges',count=5,time={'named':'yesterday'}))
 check('R2-stale-query-'+fragment,lambda p=fragment,d=stale,c=context:app(p,d,[],mode='clarify',context=c))
save(); print(json.dumps(dict(head=HEAD,passed=sum(x['pass'] for x in rows),total=len(rows),failures=[dict(id=x['id'],error=x.get('error')) for x in rows if not x['pass']]),ensure_ascii=False),flush=True)
