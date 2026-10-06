import sys,json,asyncio,tempfile,traceback,copy,re
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-44babb7-gates-20261005/auditor')
sys.path.insert(0,str(ROOT));from scripts import eval_router_update as e
HEAD=e.git_revision();assert HEAD=='44babb70ee2eea99897b9437b2f6d49b2d59adff'
STATE=Path(tempfile.mkdtemp(prefix='wisp-44babb7-boundary-')).resolve();e.install_guard(STATE,OUT)
from service.router import router
from service.router.web_request import classify
from service import main
from service.assistant.store import AssistantStore
from service.tasks import compiler as T
rows=[];LAST={}
def check(label,f):
 LAST.clear();r=dict(id=label)
 try:r['evidence']=f();r['pass']=True
 except Exception as ex:r.update(error=repr(ex),trace=traceback.format_exc(),evidence=copy.deepcopy(LAST));r['pass']=False;print('FAIL',label,repr(ex),flush=True)
 rows.append(r)
def route(p,prior=None):
 with patch.object(router,'role_to_model',lambda role:'Synthetic-other-model'):
  return asyncio.run(router.route(p,last_user=prior,last_tools='web_search' if prior else None,last_assistant='Independent synthetic public answer.' if prior else None))
def snapshot(d):return dict(role=d.role,model=d.model,tool_subset=d.tool_subset,direct_calls=d.direct_calls,bindings=d.tool_argument_bindings,forbidden=sorted(d.forbidden_tools))
public=['update me on the situation in Taiwan','give me an update on the situation in Iran','what changed today in Taiwan','latest developments in Peru']
for p in public:
 def direct(p=p):
  d=route(p);LAST.update(snapshot(d));assert d.tool_subset==['web_search'] and d.direct_calls==[('web_search',dict(query=p))],LAST
  assert {'run_shell','http_request'}<=d.forbidden_tools
  return LAST.copy()
 check('public-direct-'+p,direct)
source='search the web for the Miravian accord'
for sep in [' and ','; ','. ',' & ','; afterwards ',', and afterwards ']:
 for verb in ['email','send','forward']:
  p=source+sep+verb+' it to amber@example.invalid'
  def delivery(p=p):
   c=classify(p);d=route(p);LAST.update(classified=dict(source=c.source,query=c.query,delivery=str(c.delivery)),route=snapshot(d))
   assert d.tool_subset==['web_search','send_email'] and not d.direct_calls,LAST
   assert d.tool_argument_bindings['web_search']==dict(query=source)
   assert d.tool_argument_bindings['send_email']==dict(to='amber@example.invalid')
   assert 'lookup_contact' in d.forbidden_tools and 'amber@example.invalid' not in c.query
   return LAST.copy()
  check('public-delivery-'+sep+'-'+verb,delivery)
for p in ['email it to Mom','forward it to Mom by email','email the result to Mom afterwards']:
 def inherited(p=p):
  d=route(p,source);LAST.update(snapshot(d));assert d.tool_subset==['web_search','lookup_contact','send_email'],LAST
  assert d.tool_argument_bindings['web_search']==dict(query=source)
  return LAST.copy()
 check('public-inherited-'+p,inherited)
for p,q,effect in [('email Mom today’s headlines','today’s headlines','send_email'),('text Mom an update on Iran today','an update on Iran today','send_message'),('send Mom what happened in Iran today by text','what happened in Iran today','send_message')]:
 def leading(p=p,q=q,effect=effect):
  d=route(p);LAST.update(snapshot(d));assert d.tool_subset==['web_search','lookup_contact',effect],LAST
  assert d.tool_argument_bindings['web_search']==dict(query=q)
  return LAST.copy()
 check('public-leading-'+p,leading)
for tail in ['cancel the web search','cancel that search','stop browsing','don’t browse']:
 p='look up the latest Python release online; '+tail
 def revoked(p=p):
  c=classify(p);d=route(p);LAST.update(classified=str(c),route=snapshot(d));assert not c.allowed and not d.direct_calls,LAST
  assert 'web_search' not in (d.tool_subset or []),LAST
  return LAST.copy()
 check('public-revoked-'+tail,revoked)
for tail in ['without sending it','; don’t send it','; cancel the email','; do not deliver it']:
 p=source+' and email it to Mom '+tail
 def revoked_delivery(p=p):
  c=classify(p);d=route(p);LAST.update(classified=str(c),route=snapshot(d));assert c.delivery_cancelled and c.delivery is None,LAST
  assert not any(n in (d.tool_subset or []) for n in ['send_email','send_message','lookup_contact'])
  assert d.tool_argument_bindings.get('web_search')==dict(query=source)
  return LAST.copy()
 check('public-delivery-revoked-'+tail,revoked_delivery)
async def application(case,assistant=None):
 captured={};original=main.agent
 async def observer(body):
  response=await original(body);stream=response.body_iterator
  async def items():
   async for x in stream:yield x
   captured['workflow']=copy.deepcopy(main.store.latest_workflow(body['session_id']))
   captured['active_task']=copy.deepcopy(main.store.active_task(body['session_id']))
   captured['latest_task']=copy.deepcopy(main.store.latest_task(body['session_id']))
  response.body_iterator=items();return response
 with patch.object(main,'agent',observer):
  if assistant is None:r=await e.run_case(case,STATE,candidate=True)
  else:
   with patch.object(main,'assistant_store',assistant):r=await e.run_case(case,STATE,candidate=True)
 r['persisted_state']=captured;return r
for p in public:
 def public_main(p=p):
  case=dict(id='pub-'+str(len(rows)),prompt=p,expected=dict(calls=[dict(name='web_search',args=dict(query=p))],sources=['public']),fixture_answer='Independent public fixture result.')
  r=asyncio.run(application(case));LAST.update(result=r)
  assert [dict(name=x['name'],args=x['args']) for x in r['executed_calls']]==case['expected']['calls'],r
  assert r['answer']=='Independent public fixture result.' and not r['persisted_state']['workflow'] and not r['persisted_state']['active_task'],r
  return LAST.copy()
 check('public-main-'+p,public_main)
# Regression isolation for negation text inside an otherwise supported quoted target.
old_guard=re.compile(r"\b(?:do\s+not|don't|dont|never)\s+(?:set|add|create|make|schedule|send|give|remind|delete|remove|clear|complete|finish|mark|check|cross|tick|cancel|update|change|rename|reschedule|move)\b",re.I)
for ap in ['’','‘','ʼ','＇']:
 for action in ['check','delete']:
  title='Archive: don'+ap+'t '+action+' this item'
  p='Complete reminder called "'+title+'"'
  def quoted_task(p=p,title=title):
   astore=AssistantStore(STATE/('literal-'+str(len(rows))+'.db'))
   try:
    now=datetime(2026,10,5,12);wanted=astore.add_manual(title,(now+timedelta(days=1)).timestamp())
    actual=T.compile_task(p,now=now)
    with patch.object(T,'_NEGATED',old_guard): old=T.compile_task(p,now=now)
    case=dict(id='quoted-'+str(len(rows)),prompt=p,expected=dict(calls=[],sources=[]),fixture_answer='Independent synthetic result.',approve=True,intent_response=dict(version=1,kind='none',sources=[],excluded_sources=[],unsupported_constraints=[]))
    r=asyncio.run(application(case,astore))
    LAST.update(prompt=p,title=title,current_plan=None if actual is None else actual.to_dict(),previous_guard_plan=None if old is None else old.to_dict(),expected_calls=[dict(name='complete_reminder',args=dict(title=title,expected_id=wanted['id']))],result=r)
    assert old is not None and old.target.value==title,LAST
    assert actual is not None and actual.target.value==title,'Supported quoted target rejected by new raw-text negation guard'
    assert [dict(name=x['name'],args=x['args']) for x in r['executed_calls']]==LAST['expected_calls'],r
    return LAST.copy()
   finally:astore._db.close()
  check('quoted-negative-task-'+repr(ap)+'-'+action,quoted_task)
(OUT/'boundary-results.json').write_text(json.dumps(dict(head=HEAD,state=str(STATE),passed=sum(r['pass'] for r in rows),total=len(rows),rows=rows),indent=2,ensure_ascii=False,default=str))
print(json.dumps(dict(passed=sum(r['pass'] for r in rows),total=len(rows),failures=[dict(id=r['id'],error=r.get('error')) for r in rows if not r['pass']]),ensure_ascii=False))
