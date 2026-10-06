import sys,json,asyncio,tempfile,traceback,os,time,copy,socket,subprocess
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-98916cf-gates-20261005/auditor');sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-98916cf-boundary-')).resolve();e.install_guard(state,OUT)
import pytest
from service.router.intent import planner,validate_intent,compile_intent,InvalidIntent
from tests.test_router_intent_core import FakeClient,TARGET,MODEL,CONFIG,value,source
NOW=datetime(2026,10,5,12);rows=[]
def check(id,fn):
 row={'id':id}
 try: row['evidence']=fn();row['ok']=True
 except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
 rows.append(row);print(json.dumps(row,default=str),flush=True)
def plan(client,config=None,**kw):
 with patch.object(planner,'role_target',lambda role:TARGET):
  return asyncio.run(planner.plan_read('Recap email and texts',client=client,model=MODEL,config=CONFIG if config is None else config,now=NOW,**kw))
data=value(source('email'),source('messages'))
def identity(attr,bad):
 c=FakeClient(data);setattr(c,attr,bad);r=plan(c);assert r.disposition=='clarify' and not c.calls and not r.calls;return {'disposition':r.disposition,'client_calls':c.calls,'reason':r.reason}
for attr,bad in [('base_url','https://example.invalid'),('managed',False),('provider',None),('_credential_transport',None),('api_prefix','/other'),('endpoint_name','remote'),('target',None)]:
 if attr=='target':continue
 check('identity-'+attr,lambda attr=attr,bad=bad:identity(attr,bad))
def off(cfg):
 c=FakeClient(data);r=plan(c,config=cfg);assert r is None and not c.calls;return {'client_calls':c.calls}
for cfg in [{},{'enabled':False},{'enabled':True,'domains':['notes']}]:check('disabled-'+str(cfg),lambda cfg=cfg:off(cfg))
def kill():
 with patch.dict(os.environ,{'WISP_INTENT_ROUTER_KILL':'true'}):return off(CONFIG)
check('kill',kill)
def residency():
 c=FakeClient(loaded=False);r=plan(c);assert r.disposition=='clarify' and c.calls==['status'] and not r.calls;return {'reason':r.reason,'calls':c.calls}
check('residency',residency)
def repair():
 c=FakeClient('{}',data);r=plan(c,context=[{'role':'tool','content':'secret-tool-body'},{'role':'assistant','content':'Earlier. [Tools: web_search]'}],prior_tools=['web_search'])
 assert r.disposition=='compiled' and r.attempts==2 and c.calls==['status','chat','status','chat'];blob=json.dumps(c.context);assert 'secret-tool-body' not in blob and '[Tools:' not in blob;return {'calls':c.calls,'contexts':c.context,'compiled':r.calls}
check('repair-context',repair)
def bounded_invalid():
 c=FakeClient('{}','{}');r=plan(c);assert r.disposition=='clarify' and r.attempts==2 and not r.calls;return {'calls':c.calls,'attempts':r.attempts}
check('bounded-invalid',bounded_invalid)
class Slow(FakeClient):
 async def chat(self,*a,**kw):
  self.calls.append('slow-chat');await asyncio.sleep(2)
def deadline():
 c=Slow();r=plan(c,config={**CONFIG,'deadline_seconds':0.05});assert r.disposition=='clarify' and not r.calls and c.calls==['status','slow-chat'];return {'reason':r.reason,'calls':c.calls}
check('planner-only-deadline',deadline)
def outer_cancel():
 async def go():
  c=Slow()
  with patch.object(planner,'role_target',lambda role:TARGET):
   t=asyncio.create_task(planner.plan_read('Recap email and texts',client=c,model=MODEL,config=CONFIG,now=NOW));await asyncio.sleep(.01);t.cancel()
   try:await t
   except asyncio.CancelledError:return {'propagated':True,'calls':c.calls}
   raise AssertionError('cancel became answer')
 return asyncio.run(go())
check('planner-only-outer-cancel',outer_cancel)
for field,bad in [('count',True),('count','2'),('time',{'date':'2026-02-30'}),('query','amber')]:
 def strict(field=field,bad=bad):
  d=value(source('notes','records',query='amber route'));d['sources'][0][field]=bad
  try:validate_intent(d,'Find notes about amber route',now=NOW)
  except InvalidIntent as exc:return str(exc)
  raise AssertionError('accepted invented/typed filter')
 check('strict-'+field+str(bad),strict)
def deny(fn):
 try:fn()
 except PermissionError as exc:return str(exc)
 raise AssertionError('guard allowed external effect')
check('guard-network',lambda:deny(lambda:socket.getaddrinfo('example.invalid',443)))
check('guard-process',lambda:deny(lambda:subprocess.Popen(['true'])))
check('guard-write',lambda:deny(lambda:Path('/private/tmp/wisp-98916cf-outside-denied').write_text('x')))
from tests import test_message_digest as t
for now,collision in [(1791222000.0,None),(1791234567.0,'1234'),(1791256782.0,'5678')]:
 def privacy(now=now,collision=collision):
  with pytest.MonkeyPatch.context() as mp:
   t.isolated.__wrapped__(mp);t.test_distinct_redacted_requests_keep_both_source_occurrences(mp,now,collision)
  return {'clock':now,'metadata_collision':collision,'actual_test_passed':True}
 check('G1-'+str(now),privacy)
def leak():
 with pytest.MonkeyPatch.context() as mp:
  t.isolated.__wrapped__(mp);original=t.M.filter_summary_message_rows
  def leaking(*a,**kw):
   result=original(*a,**kw)
   return [t.D.SummaryRow((r[0],r[1],r[2]+' 1234'),r.source_before,r.source_after) for r in result]
  mp.setattr(t.M,'filter_summary_message_rows',leaking)
  try:t.test_distinct_redacted_requests_keep_both_source_occurrences(mp,1791234567.0,'1234')
  except AssertionError:return {'injected_text_leak_rejected':True}
  raise AssertionError('privacy oracle missed injection')
check('G1-injected-leak-oracle',leak)
from tests import test_router_overview_grounding as g
for name in ['test_complete_agenda_snapshot_has_exact_disjoint_counts_and_day_groups','test_merged_row_is_one_displayed_item_with_both_origins','test_schedule_display_collapse_preserves_calendar_and_reminder_provenance','test_agenda_source_title_cannot_inject_count_or_layout','test_complete_email_overview_has_no_scan_bookkeeping','test_repeated_email_subjects_fold_but_messages_and_unread_stay_counted','test_repeated_subjects_do_not_merge_unknown_senders_or_accounts','test_empty_complete_email_is_distinct_from_partial','test_quoted_subject_instructions_never_change_computed_summary','test_message_actors_are_explicit_and_topics_are_compact','test_message_partial_and_omission_notices_fit_character_budget']:
 check('overview-'+name,lambda name=name:getattr(g,name)())
for status in ['ready','unavailable','syncing']:
 def partial(status=status):
  with pytest.MonkeyPatch.context() as mp:g.test_partial_and_permission_agendas_never_claim_complete_absence(mp,status)
  return status
 check('overview-partial-'+status,partial)
from tests import test_router_overview_message_scope as m
for name in ['test_strict_keyword_miss_never_returns_unrelated_rows','test_legacy_default_retains_original_keyword_miss_behavior','test_strict_match_preserves_requested_count_and_excludes_other_conversations','test_strict_miss_keeps_period_precedence_and_does_not_read_a_different_day','test_strict_permission_failure_is_not_a_keyword_miss']:
 def scope(name=name):
  with pytest.MonkeyPatch.context() as mp:getattr(m,name)(mp)
 check('message-scope-'+name,scope)
def typed_scorer():
 gold={'expected':{'calls':[{'name':'view_emails','args':{'count':2}}],'sources':['email']}}
 bad={'executed_calls':[{'name':'view_emails','args':{'count':2.0}}],'events':[{'type':'done'}],'answer':''}
 r=e.score(gold,bad)['metrics'];assert r['exact_arguments'] is True and r['request_argument'] is False;return r
check('typed-supplement-retains-legacy-metric',typed_scorer)
for tz in ['UTC','America/Los_Angeles']:
 for clock,day,hours in [('2026-03-08T12:00:00-07:00','2026-03-08',23),('2026-11-01T12:00:00-08:00','2026-11-01',25)]:
  def dst(tz=tz,clock=clock,day=day,hours=hours):
   before=os.environ.get('TZ');os.environ['TZ']=tz;time.tzset()
   try:
    for name in ['view_emails','summarize_emails','view_messages','summarize_messages','search_notes']:
     a=e.canonical_args(name,{'day':day},{},clock=clock);b=e.canonical_args(name,{'period':'today'},{},clock=clock)
     assert a==b and b['_resolved_span'][1]-b['_resolved_span'][0]==hours*3600
     assert b!=e.canonical_args(name,{'period':'tomorrow'},{},clock=clock) and os.environ['TZ']==tz
    return {'host_timezone':tz,'hours':hours,'day':day}
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
 assert os.environ.get('TZ')==before;return before
check('timezone-restored-after-error',restore)
(OUT/'boundary-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
