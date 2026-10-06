import sys,json,asyncio,tempfile,copy,traceback
from pathlib import Path
from datetime import datetime
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-8dfd5c9-gates-20261005/auditor');sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-8dfd5c9-tuples-')).resolve();e.install_guard(state,OUT)
from service.router.intent import validate_intent,compile_intent,InvalidIntent
NOW=datetime(2026,10,5,12)
def s(**kw):return dict(domain='email',operation='records',**kw)
def v(*sources):return dict(version=1,kind='read',sources=list(sources),excluded_sources=[],unsupported_constraints=[])
cases=[]
def add(id,prompt,data,calls,**kw):cases.append(dict(id=id,prompt=prompt,intent_response=data,calls=calls,**kw))
p='Read 2 unread email from Selene in account "Work" for yesterday and read 3 email from Petra in account "Personal" for today'
sources=[s(query='Selene',account='Work',unread=True,count=2,time={'named':'yesterday'}),s(query='Petra',account='Personal',count=3,time={'named':'today'})]
cs=[{'name':'view_emails','args':{'query':'Selene','account':'Work','unread':True,'count':2,'period':'yesterday','strict_match':True}},{'name':'view_emails','args':{'query':'Petra','account':'Personal','count':3,'period':'today','strict_match':True}}]
add('all-fields-good',p,v(*sources),cs);add('all-fields-reverse',p,v(*reversed(sources)),list(reversed(cs)));add('duplicate-exact',p,v(*sources,sources[0]),cs)
for field in ['account','query','count','time','unread','operation']:
 bad=copy.deepcopy(sources)
 if field=='unread':bad[1]['unread']=bad[0].pop('unread')
 elif field=='operation':bad[0]['operation']='overview'
 else:bad[0][field],bad[1][field]=bad[1][field],bad[0][field]
 add('swapped-'+field,p,v(*bad),[])
 if field!='operation':
  bad=copy.deepcopy(sources);bad[0].pop(field);add('omitted-'+field,p,v(*bad),[])
context=[{'role':'user','content':'Read 2 unread email from Selene in account "Work" for yesterday'},{'role':'assistant','content':'Synthetic mail result.','tool_digest':'view_emails'}]
for query in ['Petra','Selene']:
 data=v(s(query=query,account='Work',unread=True,count=2,time={'named':'yesterday'}))
 add('replace-'+query,'Actually from Petra',data,[{'name':'view_emails','args':{'query':'Petra','account':'Work','unread':True,'count':2,'period':'yesterday','strict_match':True}}] if query=='Petra' else [],context=context)
for field in ['account','unread','count','time']:
 data=v(s(query='Petra',account='Work',unread=True,count=2,time={'named':'yesterday'}));data['sources'][0].pop(field);add('replace-omitted-'+field,'Actually from Petra',data,[],context=context)
rows=[]
async def run():
 for c in cases:
  row={'case':c}
  try:
   try:
    val=validate_intent(c['intent_response'],c['prompt'],context=c.get('context',[]),prior_tools=['view_emails'] if c.get('context') else [],now=NOW);row['validator']={'accepted':True,'calls':compile_intent(val,now=NOW)[0]}
   except InvalidIntent as exc:row['validator']={'accepted':False,'reason':str(exc)}
   c['expected']={'calls':c['calls'],'sources':[]};c['fixture_answer']='Synthetic exact tuple result.';r=await e.run_case(c,state,candidate=True);row['run']=r
   row['actual']=[{'name':x['name'],'args':x['args']} for x in r['executed_calls']];row['ok']=row['actual']==c['calls'] and not any(x['type']=='error' for x in r['events'])
   if not c['calls']:row['ok']=row['ok'] and not row['validator']['accepted']
  except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
  rows.append(row);print(json.dumps({'id':c['id'],'ok':row['ok'],'actual':row.get('actual'),'validator':row.get('validator'),'error':row.get('error')}),flush=True)
 (OUT/'tuple-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
asyncio.run(run())
