import sys,json,asyncio,tempfile,traceback
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-98916cf-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision()
state=Path(tempfile.mkdtemp(prefix='wisp-98916cf-paired-')).resolve()
e.install_guard(state,OUT)
from service.router.intent import validate_intent,compile_intent,InvalidIntent
from service.router.intent.validation import applicable_read,source_requirements,_instruction_text
from service.router.web_request import classify
from service.workflows.compiler import outbound_verb
NOW=datetime(2026,10,5,12)
def s(domain,operation='records',**kw):return dict(domain=domain,operation=operation,**kw)
def v(*sources,excluded=()):return dict(version=1,kind='read',sources=list(sources),excluded_sources=list(excluded),unsupported_constraints=[])
cases=[]
def add(id,prompt,intent,calls,**kw):cases.append(dict(id=id,prompt=prompt,intent_response=intent,calls=calls,**kw))
for tail in ['and do not share it with Mom','and please do not share it with Mom','without sharing it with Mom','and never share it with Mom','and do not forward it by email to Mom','and never send it to Mom','and do not email it to Mom','and do not save it in Notes']:
 add('negative-'+str(len(cases)),'Find notes about amber route '+tail,v(s('notes',query='amber route')),[dict(name='search_notes',args={'query':'amber route'})])
for negative in ['do not save it in Notes','do not delete it','do not open Calendar','do not share it with Mom']:
 for positive in ['update my reminders','cancel my appointment','mark my reminders complete','clear my reminders','save it in Notes','share it with Mom']:
  add('negative-positive-'+str(len(cases)),'Find notes about amber route and '+negative+' and '+positive,v(s('notes',query='amber route')),[],guard=True)
for join in ['and','plus','and then']:
 for tail,timearg in [('read messages',None),("read yesterday's messages",{'named':'yesterday'})]:
  src=s('messages',**({'time':timearg} if timearg else {}));args={'period':'yesterday'} if timearg else {}
  add('exclusion-'+str(len(cases)),'Find notes about amber route without email '+join+' '+tail,v(s('notes',query='amber route'),src,excluded=['email']),[dict(name='search_notes',args={'query':'amber route'}),dict(name='view_messages',args=args)])
rows=[]
async def main():
 for c in cases:
  row={'case':c}
  try:
   row.update(instruction=_instruction_text(c['prompt'],now=NOW),applicable=applicable_read(c['prompt']),outbound=outbound_verb(c['prompt']),requirements=[sorted(x) for x in source_requirements(c['prompt'])],web=asdict(classify(c['prompt'])))
   try:
    val=validate_intent(c['intent_response'],c['prompt'],now=NOW);row['validator']={'accepted':True,'calls':compile_intent(val,now=NOW)[0]}
   except InvalidIntent as exc:row['validator']={'accepted':False,'reason':str(exc)}
   c['expected']={'calls':c['calls'],'sources':[]};r=await e.run_case(c,state,candidate=True);row['run']=r
   actual=[{'name':x['name'],'args':x['args']} for x in r['executed_calls']];row['actual']=actual
   row['ok']=actual==c['calls'] and not any(x['type']=='error' for x in r['events'])
   if c.get('guard'):row['ok']=row['ok'] and not row['validator']['accepted'] and not row['applicable'] and not any(x.get('kind')=='scripted_intent' for x in r['raw_model_io'])
  except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
  rows.append(row)
  print(json.dumps({'id':c['id'],'ok':row['ok'],'prompt':c['prompt'],'actual':row.get('actual'),'validator':row.get('validator'),'answer':row.get('run',{}).get('answer','')[:300]}),flush=True)
 (OUT/'paired-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
asyncio.run(main())
