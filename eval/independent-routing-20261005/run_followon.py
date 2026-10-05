"""Fresh-corpus comparison; refuses resume after candidate or corpus changes."""
import argparse,asyncio,fcntl,hashlib,json,os,random,resource,time
from pathlib import Path
from run_eval import Client,current,router,compile_read,grade,runtime_validation,resolve_span,ANCHOR,parse_calls,memory,deadline_check
from common import ALL_NAMES,DOMAINS,messages,MODEL
from intent import intent_messages,compile_intent
import intent_v2
HERE=Path(__file__).resolve().parent
ARMS=['current_rules','ling_direct','ling_intent','ling_intent_v2']
async def current_with_metadata(c):
 history=c.get('context',[]);users=[m['content'] for m in history if m['role']=='user'];assist=[m['content'] for m in history if m['role']=='assistant'];lasttools=', '.join(c.get('context_tools',[]))
 cr=compile_read(c['prompt'],last_user=users[-1] if users else '',last_tools=lasttools)
 if cr is not None:return {'subset':[n for n,a in cr[0]],'reason':'production structured-read shortcut','route_source':'structured_read','needs_tools':bool(cr[0]),'direct':cr[0],'forbidden':[],'force_first_tool':None,'bindings':{},'resolved_request':None,'direct_text':cr[1]}
 d=await router.route(c['prompt'],last_user=users[-1] if users else None,recent_users=users,last_assistant=assist[-1] if assist else '',last_tools=lasttools)
 return {'subset':d.tool_subset,'reason':d.reason,'route_source':d.source,'needs_tools':d.needs_tools,'direct':d.direct_calls or None,'forbidden':list(d.forbidden_tools),'force_first_tool':d.force_first_tool,'bindings':d.tool_argument_bindings,'resolved_request':d.resolved_request}
def with_metadata(msgs,c):
 if c.get('context_tools'):msgs[0]={**msgs[0],'content':msgs[0]['content']+' Previous completed tool names (source context only, not new authorization): '+json.dumps(c['context_tools'])}
 return msgs
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--split',choices=['dev','test'],required=True);ap.add_argument('--reps',type=int,default=1);ap.add_argument('--out',required=True);args=ap.parse_args()
 lock=open('/private/tmp/wisp-routing-eval.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 corpus=HERE/'followon_corpus.json';data=json.loads(corpus.read_text());cases=[c for c in data if c['split']==args.split]
 out=HERE/args.out;mp=HERE/(args.out+'.manifest.json')
 contract={'model':MODEL,'split':args.split,'reps':args.reps,'arms':ARMS,'corpus_sha256':hashlib.sha256(corpus.read_bytes()).hexdigest(),'source_sha256':{n:hashlib.sha256((HERE/n).read_bytes()).hexdigest() for n in ['run_followon.py','intent_v2.py','run_eval.py','common.py','intent.py']},'scope':'New independently authored corpus after primary test inspection; no claim of original held-out replication or production execution.'}
 if out.exists():
  if json.loads(mp.read_text())['contract']!=contract:raise RuntimeError('Changed inputs; new filename required')
 else:mp.write_text(json.dumps({'contract':contract,'started':time.time(),'pid':os.getpid()},indent=2)+'\n')
 done={(r['id'],r['arm'],r['rep']) for r in (json.loads(l) for l in out.read_text().splitlines())} if out.exists() else set()
 cli=Client();jobs=[(c,a,r) for r in range(args.reps) for c in cases for a in ARMS];random.Random(51027).shuffle(jobs)
 for c,arm,rep in jobs:
  if (c['id'],arm,rep) in done:continue
  deadline_check();free=memory()
  if free is not None and free<12:raise RuntimeError('Memory pressure')
  row={'id':c['id'],'family':c['family'],'split':c['split'],'arm':arm,'rep':rep,'prompt':c['prompt'],'timestamp':time.time(),'memory_free_percent':free};t=time.perf_counter();calls=None;names=ALL_NAMES
  try:
   if arm=='current_rules':
    r=asyncio.run(current_with_metadata(c));row['route']=r
    if r['direct'] is not None:calls=[{'name':n,'arguments':a} for n,a in r['direct']]
    names=[n for n in (r['subset'] if r['subset'] is not None else ALL_NAMES) if n not in r['forbidden']]
    if not r['needs_tools']:names=[]
    if r['force_first_tool']:names=[r['force_first_tool']]
   elif arm=='ling_intent':
    r=cli.infer(with_metadata(intent_messages(c),c),plan=True);row['planner']=r;x=json.loads(r['response']['choices'][0]['message']['content']);row['intent']=x;row['domain']=x.get('domain');calls=compile_intent(x,runtime_validation,lambda p:resolve_span(p,now=ANCHOR));row['compiled']=calls is not None
    names=DOMAINS.get(x.get('domain'),ALL_NAMES) if x.get('domain')!='unclear' else ALL_NAMES
    if calls is None and not names:names=ALL_NAMES
   elif arm=='ling_intent_v2':
    r=intent_v2.route(c,cli);calls=r.pop('calls');names=r.pop('names');row.update(r)
   row['offered_tools']=names
   if calls is None:
    effective=dict(c)
    if arm=='current_rules' and row['route'].get('resolved_request'):effective['prompt']=row['route']['resolved_request']
    r=cli.infer(with_metadata(messages(effective),c),names);row['generation']=r;calls=parse_calls(r)
    if arm=='current_rules':
     for call in calls:call['arguments'].update(row['route']['bindings'].get(call['name'],{}))
   row['calls']=calls;row['grade']=grade(c,calls)
   if row.get('invalid_intent'):row['grade']['strict']=False
   if any(x['name'] not in names for x in calls) and not (arm=='current_rules' and row['route'].get('direct') is not None) and not row.get('compiled'):
    row['grade']['unoffered_calls']=[x['name'] for x in calls if x['name'] not in names];row['grade']['strict']=False
  except Exception as err:row['error']=type(err).__name__+': '+str(err)
  row['seconds']=time.perf_counter()-t;row['harness_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
  with out.open('a') as f:f.write(json.dumps(row)+'\n');f.flush();os.fsync(f.fileno())
  done.add((c['id'],arm,rep));(HERE/'followon_progress.json').write_text(json.dumps({'pid':os.getpid(),'output':str(out),'finished_rows':len(done),'planned_rows':len(jobs),'complete':len(done)==len(jobs),'updated':time.time()},indent=2)+'\n')
  print(json.dumps({'id':c['id'],'arm':arm,'strict':row.get('grade',{}).get('strict'),'error':row.get('error'),'seconds':round(row['seconds'],3)}),flush=True)
if __name__=='__main__':main()
