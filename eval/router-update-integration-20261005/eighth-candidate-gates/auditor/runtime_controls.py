import sys,os,json,copy,asyncio,tempfile,traceback,inspect,time,socket,subprocess
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-44babb7-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();assert HEAD=='44babb70ee2eea99897b9437b2f6d49b2d59adff'
STATE=Path(tempfile.mkdtemp(prefix='wisp-44babb7-runtime-')).resolve();e.install_guard(STATE,OUT)
import pytest
from tests import test_router_intent_core as C,test_router_overview_grounding as O,test_router_overview_message_scope as S,test_message_digest as P
from service.router.intent import planner
from service import main
from scripts import run_simulation_qa as gate
from service.tools import imessage_tools as M,message_digest as D
from service.router import router
from service.router.web_request import classify
rows=[]
def check(label,func):
 r=dict(id=label)
 try:r['evidence']=func();r['pass']=True
 except Exception as ex:r.update(error=repr(ex),trace=traceback.format_exc());r['pass']=False;print('FAIL',label,repr(ex),flush=True)
 rows.append(r)
def fixture(f,*a):
 with pytest.MonkeyPatch.context() as mp:
  P.isolated.__wrapped__(mp)
  args=[mp,*a] if next(iter(inspect.signature(f).parameters),'')=='monkeypatch' else list(a)
  f(*args)
 return dict(function=f.__module__+'.'+f.__name__,parameters=a)
with patch.object(planner,'role_target',lambda role:C.TARGET if role=='router' else None):
 for name in ['test_one_repair_and_clean_role_context_metadata','test_invalid_intent_never_falls_back_to_broad_tools','test_unavailable_model_never_loads_or_generates','test_remote_or_wrong_model_is_not_resource_eligible','test_deadline_cancels_generation_without_retry','test_outer_cancellation_is_not_reclassified_as_clarification','test_schema_asset_matches_callable_contract','test_loop_merge_keeps_scope_and_failure_evidence','test_registered_strict_message_capability_is_used_exactly','test_missing_binding_or_resolution_failure_fails_closed','test_configured_target_is_authoritative_over_model_argument_and_config','test_scope_labels_escape_literal_markdown_and_preserve_failed_receipt']:
  check(name,lambda n=name:fixture(getattr(C,n)))
 for config in [None,{},dict(enabled=False),dict(enabled=True),dict(enabled=True,domains=[]),dict(enabled=True,domains=['notes'])]:
  check('defaultoff-'+repr(config),lambda c=config:fixture(C.test_defaultoff_and_domain_allowlist_do_not_generate,c))
 check('kill-switch',lambda:fixture(C.test_kill_switch_never_generates))
 for mutation in [lambda c:setattr(c,'base_url','https://remote.example.invalid'),lambda c:setattr(c,'provider',None),lambda c:setattr(c,'_credential_transport',None)]:
  def identity(mutation=mutation):
   client=C.FakeClient();mutation(client)
   result=asyncio.run(planner.plan_read('Recap my email',client=client,model=C.MODEL,config=C.CONFIG))
   assert result.disposition=='clarify' and client.calls==[],(result,client.calls)
   return dict(disposition=result.disposition,calls=client.calls)
  check('identity-before-IO-'+str(len(rows)),identity)
for now,collision in [(1791222000.0,None),(1791234567.0,'1234'),(1791256782.0,'5678')]:
 check('G1-three-clock-'+str(now),lambda n=now,c=collision:fixture(P.test_distinct_redacted_requests_keep_both_source_occurrences,n,c))
def leak_oracle():
 with pytest.MonkeyPatch.context() as mp:
  P.isolated.__wrapped__(mp);original=M.filter_summary_message_rows
  def leaking(*a,**kw):return [D.SummaryRow((r[0],r[1],r[2]+' 1234'),r.source_before,r.source_after) for r in original(*a,**kw)]
  mp.setattr(M,'filter_summary_message_rows',leaking)
  try:P.test_distinct_redacted_requests_keep_both_source_occurrences(mp,1791234567.0,'1234')
  except AssertionError:return dict(injected_text='1234',oracle_rejected=True)
  raise AssertionError('text leak was not caught')
check('G1-injected-text-leak',leak_oracle)
for n,f in vars(O).items():
 if not n.startswith('test_'):continue
 sig=list(inspect.signature(f).parameters)
 if 'state' in sig:
  for status in ['ready','unavailable','syncing']:check(n+'-'+status,lambda f=f,s=status:fixture(f,s))
 elif sig==['rows']:
  for v in [[],[O.mail('Independent source fact')]]:check(n+'-'+str(len(v)),lambda f=f,v=v:fixture(f,v))
 else:check(n,lambda f=f:fixture(f))
for n,f in vars(S).items():
 if n.startswith('test_'):check(n,lambda f=f:fixture(f))
def manifest():
 names=gate._selected_tests(['full']);assert len(names)==179,len(names)
 new=['tests/test_router_intent_core.py','tests/test_router_intent_main.py','tests/test_router_intent_workflow.py','tests/test_router_update_eval.py','tests/test_router_overview_grounding.py','tests/test_router_overview_message_scope.py']
 assert set(new)<=set(names)
 return dict(count=len(names),new=new)
check('full-manifest-179',manifest)
for kind in ['unknown','missing']:
 def drift(kind=kind):
  root=STATE/('manifest-'+kind);(root/'tests').mkdir(parents=True)
  for n in gate.SAFE_FULL_TESTS:
   p=root/n;p.parent.mkdir(parents=True,exist_ok=True);p.touch()
  if kind=='unknown':(root/'tests/test_unknown_fixture.py').touch()
  else:(root/next(iter(sorted(gate.SAFE_FULL_TESTS)))).unlink()
  with patch.object(gate,'ROOT',root):
   try:gate._selected_tests(['full'])
   except RuntimeError as ex:return dict(kind=kind,error=str(ex))
  raise AssertionError('manifest drift accepted')
 check('manifest-'+kind,drift)
for clock,hours in [('2026-03-08T12:00:00-07:00',23),('2026-11-01T12:00:00-08:00',25)]:
 for host in ['UTC','America/Los_Angeles']:
  def dst(clock=clock,hours=hours,host=host):
   old=os.environ.get('TZ')
   try:
    os.environ['TZ']=host;time.tzset();prior=os.environ['TZ']
    day=clock[:10];a=e.canonical_args('view_messages',dict(day=day),{},clock=clock);b=e.canonical_args('view_messages',dict(period='today'),{},clock=clock);c=e.canonical_args('view_messages',dict(period='tomorrow'),{},clock=clock)
    assert a==b and a!=c,(a,b,c)
    span=a['_resolved_span'];assert span[1]-span[0]==hours*3600,span
    assert os.environ['TZ']==prior
    return dict(clock=clock,host=host,args=a,duration_hours=hours)
   finally:
    if old is None:os.environ.pop('TZ',None)
    else:os.environ['TZ']=old
    time.tzset()
  check('DST-'+clock+'-'+host,dst)
def typed():
 run=dict(executed_calls=[dict(name='search_notes',args=dict(query='orchid',count=2.0))],schemas={},events=[dict(type='done')],answer='')
 case=dict(expected=dict(calls=[dict(name='search_notes',args=dict(query='orchid',count=2))],sources=['notes']))
 score=e.score(case,run)['metrics'];assert score['exact_arguments'] is True and score['request_argument'] is False,score
 return score
check('P3-original-versus-typed-metric',typed)
def default_config():
 d=main.models_config();assert d.get('intent_router',{}).get('enabled') is not True,d.get('intent_router')
 return dict(intent_router=d.get('intent_router'))
check('actual-config-default-disabled',default_config)
for label,f in [('DNS',lambda:socket.getaddrinfo('example.invalid',443)),('process',lambda:subprocess.Popen(['/usr/bin/true'])),('outside-write',lambda:(ROOT/'auditor-forbidden').write_text('denied'))]:
 def denied(f=f):
  try:f()
  except PermissionError as ex:return dict(denied=True,error=str(ex))
  raise AssertionError('guard bypass')
 check('isolation-'+label,denied)
(OUT/'runtime-results.json').write_text(json.dumps(dict(head=HEAD,state=str(STATE),passed=sum(r['pass'] for r in rows),total=len(rows),rows=rows),indent=2,default=str))
print(json.dumps(dict(passed=sum(r['pass'] for r in rows),total=len(rows),failures=[r['id'] for r in rows if not r['pass']])))
