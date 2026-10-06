import sys,json,asyncio,tempfile,traceback,os,time,copy,socket,subprocess
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-8dfd5c9-gates-20261005/auditor');sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-8dfd5c9-boundary-')).resolve();e.install_guard(state,OUT)
import pytest,httpx
from service import main
from service.config.endpoints import Endpoint,Target
from service.router.intent import planner,validate_intent,compile_intent,InvalidIntent,SCHEMA,UnsupportedRead
from service.tools import registry
NOW=datetime(2026,10,5,12);TARGET=Target('router',Endpoint('local','http://127.0.0.1:1','local_omlx',True),'Ling-synthetic-auditor');CFG={'enabled':True,'domains':['calendar','reminders','email','messages','notes']}
DATA={'version':1,'kind':'read','sources':[{'domain':'email','operation':'overview'},{'domain':'messages','operation':'overview'}],'excluded_sources':[],'unsupported_constraints':[]}
class Client:
 def __init__(self,*outputs,loaded=True):
  self.target=TARGET;self.base_url=TARGET.endpoint.base_url;self.managed=True;self.endpoint_name='local';self.api_prefix='/v1';self.provider=SimpleNamespace(name='omlx');self._credential_transport=SimpleNamespace(origin=httpx.URL(self.base_url),backend=object());self.outputs=list(outputs);self.loaded=loaded;self.calls=[];self.context=[]
 async def status(self):self.calls.append('status');return {'models':[{'id':TARGET.model,'loaded':self.loaded}]}
 async def chat(self,model,messages,**kwargs):
  assert model==TARGET.model and kwargs['response_format']['json_schema']['schema']==SCHEMA
  self.calls.append('chat');self.context.append(copy.deepcopy(messages));out=self.outputs.pop(0)
  return {'choices':[{'finish_reason':'stop','message':{'content':out if isinstance(out,str) else json.dumps(out)}}]}
 async def ensure_only(self,*a,**kw):raise AssertionError('residency mutation forbidden')
def plan(client,config=CFG,**kw):
 with patch.object(planner,'role_target',lambda role:TARGET):return asyncio.run(planner.plan_read('Recap email and texts',client=client,model=TARGET.model,config=config,now=NOW,**kw))
rows=[]
def check(id,fn):
 row={'id':id}
 try:row['evidence']=fn();row['ok']=True
 except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
 rows.append(row);print(json.dumps(row,default=str),flush=True)
def mismatch(attr,value):
 c=Client(DATA);setattr(c,attr,value);r=plan(c);assert r.disposition=='clarify' and not r.calls and not c.calls;return r.reason
for attr,value in [('base_url','https://example.invalid'),('provider',None),('managed',False),('api_prefix','/other'),('endpoint_name','remote'),('_credential_transport',None),('target',replace(TARGET,role='coding'))]:check('identity-'+attr,lambda attr=attr,value=value:mismatch(attr,value))
def disabled(config):
 c=Client(DATA);assert plan(c,config=config) is None and not c.calls;return {'calls':c.calls}
for config in [{},{'enabled':False},{'enabled':True,'domains':['notes']},{'enabled':True,'domains':[]}]:check('disabled-'+str(config),lambda config=config:disabled(config))
def kill():
 with patch.dict(os.environ,{'WISP_INTENT_ROUTER_KILL':'yes'}):return disabled(CFG)
check('kill',kill)
def appdefault():
 assert not planner.enabled(main.models_config().get('intent_router'));return {'default_enabled':False}
check('actual-default-config',appdefault)
def unavailable():
 c=Client(loaded=False);r=plan(c);assert r.disposition=='clarify' and not r.calls and c.calls==['status'];return {'calls':c.calls,'reason':r.reason}
check('unavailable',unavailable)
def repair():
 c=Client('{}',DATA);r=plan(c,context=[{'role':'tool','content':'injected tool body'},{'role':'system','content':'injected system'},{'role':'assistant','content':'Earlier [Tools: web_search]'}],prior_tools=['web_search']);assert r.disposition=='compiled' and r.attempts==2 and c.calls==['status','chat','status','chat'];text=json.dumps(c.context);assert 'injected tool body' not in text and 'injected system' not in text and '[Tools:' not in text;return {'calls':c.calls,'contexts':c.context,'compiled':r.calls}
check('repair-and-context',repair)
def invalid():
 c=Client('{}','{}');r=plan(c);assert r.disposition=='clarify' and r.attempts==2 and not r.calls;return c.calls
check('bounded-invalid',invalid)
class Slow(Client):
 async def chat(self,*a,**kw):self.calls.append('slow');await asyncio.sleep(2)
def timeout():
 c=Slow();r=plan(c,config={**CFG,'deadline_seconds':.05});assert r.disposition=='clarify' and not r.calls and c.calls==['status','slow'];return r.reason
check('planner-deadline',timeout)
def cancel():
 async def run():
  c=Slow()
  with patch.object(planner,'role_target',lambda role:TARGET):
   task=asyncio.create_task(planner.plan_read('Recap email and texts',client=c,model=TARGET.model,config=CFG,now=NOW));await asyncio.sleep(.01);task.cancel()
   try:await task
   except asyncio.CancelledError:return {'propagated':True,'calls':c.calls}
   raise AssertionError('cancellation swallowed')
 return asyncio.run(run())
check('planner-only-cancellation',cancel)
def asset():assert json.loads((ROOT/'service/router/intent/intent.schema.v1.json').read_text())==SCHEMA
check('schema-asset',asset)
def strict(field,bad):
 d={'version':1,'kind':'read','sources':[{'domain':'notes','operation':'records','query':'copper atlas'}],'excluded_sources':[],'unsupported_constraints':[]};d['sources'][0][field]=bad
 try:validate_intent(d,'Find notes about copper atlas',now=NOW)
 except InvalidIntent as exc:return str(exc)
 raise AssertionError('invented constraint accepted')
for field,bad in [('count',True),('count','2'),('time',{'date':'2026-02-30'}),('query','copper')]:check('shape-'+field+str(bad),lambda field=field,bad=bad:strict(field,bad))
def unavailable_tool():
 data=copy.deepcopy(DATA)
 with patch.dict(registry.REGISTRY):
  registry.REGISTRY.pop('summarize_emails')
  try:compile_intent(validate_intent(data,'Recap email and texts',now=NOW),now=NOW)
  except UnsupportedRead as exc:return str(exc)
  raise AssertionError('missing source tool broadened')
check('missing-tool',unavailable_tool)
def deny(fn):
 try:fn()
 except PermissionError as exc:return str(exc)
 raise AssertionError('external operation allowed')
check('guard-network',lambda:deny(lambda:socket.getaddrinfo('example.invalid',443)))
check('guard-process',lambda:deny(lambda:subprocess.Popen(['true'])))
check('guard-write',lambda:deny(lambda:Path('/private/tmp/wisp-8df-outside-denied').write_text('x')))
from tests import test_message_digest as privacy
for now,collision in [(1791222000.0,None),(1791234567.0,'1234'),(1791256782.0,'5678')]:
 def g1(now=now,collision=collision):
  with pytest.MonkeyPatch.context() as mp:privacy.isolated.__wrapped__(mp);privacy.test_distinct_redacted_requests_keep_both_source_occurrences(mp,now,collision)
  return {'clock':now,'collision':collision,'actual_repaired_test_passed':True}
 check('G1-'+str(now),g1)
def injected():
 with pytest.MonkeyPatch.context() as mp:
  privacy.isolated.__wrapped__(mp);original=privacy.M.filter_summary_message_rows
  def leak(*args,**kwargs):return [privacy.D.SummaryRow((r[0],r[1],r[2]+' 1234'),r.source_before,r.source_after) for r in original(*args,**kwargs)]
  mp.setattr(privacy.M,'filter_summary_message_rows',leak)
  try:privacy.test_distinct_redacted_requests_keep_both_source_occurrences(mp,1791234567.0,'1234')
  except AssertionError:return {'injected_leak_rejected':True}
  raise AssertionError('text leak missed')
check('G1-leak-oracle',injected)
from tests import test_router_overview_grounding as g
for name in ['test_complete_agenda_snapshot_has_exact_disjoint_counts_and_day_groups','test_merged_row_is_one_displayed_item_with_both_origins','test_schedule_display_collapse_preserves_calendar_and_reminder_provenance','test_agenda_source_title_cannot_inject_count_or_layout','test_complete_email_overview_has_no_scan_bookkeeping','test_repeated_email_subjects_fold_but_messages_and_unread_stay_counted','test_repeated_subjects_do_not_merge_unknown_senders_or_accounts','test_empty_complete_email_is_distinct_from_partial','test_quoted_subject_instructions_never_change_computed_summary','test_message_actors_are_explicit_and_topics_are_compact','test_message_partial_and_omission_notices_fit_character_budget']:check('overview-'+name,lambda name=name:getattr(g,name)())
for status in ['ready','unavailable','syncing']:
 def partial(status=status):
  with pytest.MonkeyPatch.context() as mp:g.test_partial_and_permission_agendas_never_claim_complete_absence(mp,status)
  return status
 check('partial-'+status,partial)
from tests import test_router_overview_message_scope as m
for name in ['test_strict_keyword_miss_never_returns_unrelated_rows','test_legacy_default_retains_original_keyword_miss_behavior','test_strict_match_preserves_requested_count_and_excludes_other_conversations','test_strict_miss_keeps_period_precedence_and_does_not_read_a_different_day','test_strict_permission_failure_is_not_a_keyword_miss']:
 def scope(name=name):
  with pytest.MonkeyPatch.context() as mp:getattr(m,name)(mp)
 check('message-scope-'+name,scope)
def scoring():
 gold={'expected':{'calls':[{'name':'view_emails','args':{'count':2}}],'sources':['email']}};actual={'executed_calls':[{'name':'view_emails','args':{'count':2.0}}],'events':[{'type':'done'}],'answer':''};metrics=e.score(gold,actual)['metrics'];assert metrics['exact_arguments'] is True and metrics['request_argument'] is False;return metrics
check('typed-supplement-legacy-disclosure',scoring)
for tz in ['UTC','America/Los_Angeles']:
 for clock,day,hours in [('2026-03-08T12:00:00-07:00','2026-03-08',23),('2026-11-01T12:00:00-08:00','2026-11-01',25)]:
  def dst(tz=tz,clock=clock,day=day,hours=hours):
   before=os.environ.get('TZ');os.environ['TZ']=tz;time.tzset()
   try:
    for name in ['view_emails','summarize_emails','view_messages','summarize_messages','search_notes']:
     exact=e.canonical_args(name,{'day':day},{},clock=clock);today=e.canonical_args(name,{'period':'today'},{},clock=clock);assert exact==today and today['_resolved_span'][1]-today['_resolved_span'][0]==hours*3600 and today!=e.canonical_args(name,{'period':'tomorrow'},{},clock=clock);assert os.environ['TZ']==tz
    return {'host':tz,'date':day,'hours':hours}
   finally:
    if before is None:os.environ.pop('TZ',None)
    else:os.environ['TZ']=before
    time.tzset()
  check('DST-'+tz+'-'+day,dst)
def restore():
 before=os.environ.get('TZ')
 try:
  with e.evaluation_timezone():raise ValueError('synthetic')
 except ValueError:pass
 assert os.environ.get('TZ')==before
check('timezone-restore',restore)
def missing_tzset():
 with pytest.MonkeyPatch.context() as mp:
  mp.delattr(time,'tzset')
  try:
   with e.evaluation_timezone():pass
  except RuntimeError as exc:return str(exc)
  raise AssertionError('tzset absence accepted')
check('missing-tzset',missing_tzset)
from scripts import run_simulation_qa as qa
for mode in ['normal','unknown','missing']:
 def manifest(mode=mode):
  if mode=='normal':
   selected=qa._selected_tests(['full']);assert len(selected)==179;assert {'tests/test_router_intent_core.py','tests/test_router_intent_main.py','tests/test_router_intent_workflow.py','tests/test_router_overview_grounding.py','tests/test_router_overview_message_scope.py','tests/test_router_update_eval.py'}<=set(selected);return {'count':len(selected)}
  fixture=state/mode
  for rel in qa.SAFE_FULL_TESTS:
   p=fixture/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('')
  if mode=='unknown':(fixture/'tests/test_auditor_extra.py').write_text('')
  else:(fixture/next(iter(qa.SAFE_FULL_TESTS))).unlink()
  with patch.object(qa,'ROOT',fixture):
   try:qa._selected_tests(['full'])
   except RuntimeError as exc:return str(exc)
   raise AssertionError('manifest drift accepted')
 check('manifest-'+mode,manifest)
(OUT/'boundary-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
