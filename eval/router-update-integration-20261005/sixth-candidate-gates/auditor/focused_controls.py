import sys,json,asyncio,tempfile,copy,traceback
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-98916cf-gates-20261005/auditor');sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-98916cf-focused-')).resolve();e.install_guard(state,OUT)
from service.router.intent import validate_intent,compile_intent,InvalidIntent
from service.router.intent.validation import source_requirements,_positive_effect_instruction,_source_time_requirements,_instruction_text
from service.router.web_request import _clauses,_EFFECT_BOUNDARY,_action_clause_head
from scripts import run_simulation_qa as qa
import pytest,time
NOW=datetime(2026,10,5,12);rows=[]
def s(domain,operation='records',**kw):return dict(domain=domain,operation=operation,**kw)
def v(*sources,excluded=()):return dict(version=1,kind='read',sources=list(sources),excluded_sources=list(excluded),unsupported_constraints=[])
cases=[]
def add(prompt,data,calls,guard=False):cases.append(dict(id='focus-'+str(len(cases)),prompt=prompt,intent_response=data,calls=calls,guard=guard))
for p in ['Find notes about amber route and do not delete it','Find notes about amber route and do not delete it and update my reminders','Find notes about amber route and do not delete it; update my reminders','Find notes about amber route and do not delete it and mark my reminders complete','Find notes about amber route and do not delete it; mark my reminders complete','Find notes about amber route and do not delete it and clear my reminders','Find notes about amber route and do not delete it; clear my reminders']:
 add(p,v(s('notes',query='amber route')),[dict(name='search_notes',args={'query':'amber route'})] if p.endswith('do not delete it') else [],guard=not p.endswith('do not delete it'))
for join in ['and','plus','and then']:
 add('Find notes about amber route without email '+join+' read messages from yesterday',v(s('notes',query='amber route',time={'named':'yesterday'}),s('messages',time={'named':'yesterday'}),excluded=['email']),[dict(name='search_notes',args={'query':'amber route','period':'yesterday'}),dict(name='view_messages',args={'period':'yesterday'})])
 add('Find notes about amber route for today without email '+join+' read messages from yesterday',v(s('notes',query='amber route',time={'named':'today'}),s('messages',time={'named':'yesterday'}),excluded=['email']),[dict(name='search_notes',args={'query':'amber route','period':'today'}),dict(name='view_messages',args={'period':'yesterday'})])
async def main():
 for c in cases:
  row={'case':c}
  try:
   row['public_clauses']=[asdict(x) for x in _clauses(c['prompt'])];row['effect_boundaries']=[{'boundary':m.group(),'tail':c['prompt'][m.end():],'head':bool(_action_clause_head(c['prompt'][m.end():]))} for m in _EFFECT_BOUNDARY.finditer(c['prompt'])]
   row['positive_effect_instruction']=_positive_effect_instruction(c['prompt'],now=NOW)
   req=source_requirements(c['prompt'])[0]; row['expected_time_scopes']=str(_source_time_requirements(c['prompt'],req,now=NOW))
   try:
    val=validate_intent(c['intent_response'],c['prompt'],now=NOW);row['validator']={'accepted':True,'calls':compile_intent(val,now=NOW)[0]}
   except InvalidIntent as exc:row['validator']={'accepted':False,'reason':str(exc)}
   c['expected']={'calls':c['calls'],'sources':[]};r=await e.run_case(c,state,candidate=True);row['run']=r
   row['actual']=[{'name':x['name'],'args':x['args']} for x in r['executed_calls']]
   row['ok']=row['actual']==c['calls']
   if c['guard']:row['ok']=row['ok'] and not row['validator']['accepted']
  except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
  rows.append(row);print(json.dumps({'id':c['id'],'ok':row['ok'],'prompt':c['prompt'],'actual':row.get('actual'),'validator':row.get('validator')}),flush=True)
asyncio.run(main())
for mode in ['normal','unknown','missing']:
 row={'id':'manifest-'+mode}
 try:
  if mode=='normal':
   selected=qa._selected_tests(['full']);assert len(selected)==179
   assert {'tests/test_router_intent_core.py','tests/test_router_intent_main.py','tests/test_router_intent_workflow.py','tests/test_router_overview_grounding.py','tests/test_router_overview_message_scope.py','tests/test_router_update_eval.py'}<=set(selected);row['count']=len(selected)
  else:
   fixture=state/mode
   for rel in qa.SAFE_FULL_TESTS:
    p=fixture/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('')
   if mode=='unknown':(fixture/'tests/test_auditor_unknown.py').write_text('')
   else:(fixture/next(iter(qa.SAFE_FULL_TESTS))).unlink()
   with patch.object(qa,'ROOT',fixture):
    try:qa._selected_tests(['full'])
    except RuntimeError as exc:row['diagnosis']=str(exc)
    else:raise AssertionError('manifest drift accepted')
  row['ok']=True
 except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
 rows.append(row)
with pytest.MonkeyPatch.context() as mp:
 mp.delattr(time,'tzset');row={'id':'missing-tzset'}
 try:
  with e.evaluation_timezone():pass
 except RuntimeError as exc:row.update(ok=True,diagnosis=str(exc))
 else:row['ok']=False
 rows.append(row)
(OUT/'focused-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
