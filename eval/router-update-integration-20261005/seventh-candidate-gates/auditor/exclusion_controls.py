import sys,json,asyncio,tempfile,copy,traceback
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-8dfd5c9-gates-20261005/auditor');sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-8dfd5c9-exclusion-')).resolve();e.install_guard(state,OUT)
from service import main
from service.router.intent import validate_intent,compile_intent,InvalidIntent
from service.router.intent.validation import source_requirements,_instruction_text
NOW=datetime(2026,10,5,12)
def s(domain,operation='overview',**kw):return dict(domain=domain,operation=operation,**kw)
def v(*sources,excluded=()):return dict(version=1,kind='read',sources=list(sources),excluded_sources=list(excluded),unsupported_constraints=[])
cases=[]
def add(prompt,data,calls,kind):cases.append(dict(id='exclusion-'+str(len(cases)),prompt=prompt,intent_response=data,calls=calls,kind=kind))
for apostrophe in ["'",'’','ʼ','＇']:
 for domain,noun,tool,args in [('email','my email','summarize_emails',{}),('messages','my messages','summarize_messages',{}),('calendar','my calendar','get_upcoming',{'calendar_only':True})]:
  p='Find notes about quartz birds and don'+apostrophe+'t read '+noun
  for variant in ['correct','extra-no-exclusion','extra-with-exclusion']:
   data=v(s('notes','records',query='quartz birds'),excluded=[domain]) if variant=='correct' else v(s('notes','records',query='quartz birds'),s(domain),excluded=[domain] if variant=='extra-with-exclusion' else [])
   add(p,data,[{'name':'search_notes','args':{'query':'quartz birds'}}] if variant=='correct' else [],variant)
for negative in ["don't read my email",'do not read my email','excluding email','without email']:
 add('Find notes about quartz birds and '+negative,v(s('notes','records',query='quartz birds'),excluded=['email']),[{'name':'search_notes','args':{'query':'quartz birds'}}],'ascii-control')
for q in ['quartz birds and don’t read my email','donʼt read my calendar','quartz birds without email']:
 add('Find notes about "'+q+'"',v(s('notes','records',query=q)),[{'name':'search_notes','args':{'query':q}}],'quoted-control')
rows=[]
async def run():
 for c in cases:
  row={'case':c}
  try:
   row['requirements']=[sorted(x) for x in source_requirements(c['prompt'])];row['instruction']=_instruction_text(c['prompt'],now=NOW)
   try:
    val=validate_intent(c['intent_response'],c['prompt'],now=NOW);row['validator']={'accepted':True,'calls':compile_intent(val,now=NOW)[0]}
   except InvalidIntent as exc:row['validator']={'accepted':False,'reason':str(exc)}
   c['expected']={'calls':c['calls'],'sources':[]};c['fixture_answer']='Distinct synthetic exclusion fixture.'
   captured={};original=main.agent
   async def agent(body):
    response=await original(body);iterator=response.body_iterator
    async def observe():
     async for item in iterator:yield item
     captured['workflow']=copy.deepcopy(main.store.latest_workflow(body['session_id']))
    response.body_iterator=observe();return response
   with patch.object(main,'agent',agent):result=await e.run_case(c,state,candidate=True)
   row['run']=result;row['store']=captured
   row['actual']=[{'name':x['name'],'args':x['args']} for x in result['executed_calls']]
   row['ok']=row['actual']==c['calls'] and captured.get('workflow') is None and not any(x['type']=='error' for x in result['events'])
   if c['kind'].startswith('extra'):row['ok']=row['ok'] and not row['validator']['accepted']
  except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
  rows.append(row);print(json.dumps({'id':c['id'],'ok':row['ok'],'prompt':c['prompt'],'actual':row.get('actual'),'validator':row.get('validator'),'error':row.get('error')}),flush=True)
 (OUT/'exclusion-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
asyncio.run(run())
