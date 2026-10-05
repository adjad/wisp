"""Actual model-inference routing benchmark; proposed calls are NEVER executed.

Baseline is frozen Wisp rule/structured-read routing + a controlled Ling tool
selection stage, not a claim of full production end-to-end equivalence.
All candidates receive identical synthetic context and schema descriptions.
"""
import os,sys,json,time,asyncio,hashlib,subprocess,random,statistics,fcntl,re,resource,argparse,selectors
from zoneinfo import ZoneInfo
from datetime import datetime
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]))
os.environ['WISP_HOME']='/private/tmp/wisp-routing-eval-state'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
os.environ['TZ']='America/Los_Angeles'
time.tzset()
import httpx
from common import *
from intent import INTENT_SYSTEM,intent_messages,compile_intent
from service.tools.assistant_tools import calendar_time_problem
from service.tools import REGISTRY,tool_schemas
from service.tools.registry import _validate_args
from service.tools.timeranges import resolve_span
from service.router import router
from service.workflows.reads import compile_read
ANCHOR=datetime(2026,10,5,0,55)
DEADLINE=datetime(2026,10,5,8,30,tzinfo=ZoneInfo('America/Los_Angeles')).timestamp()
def deadline_check():
 if time.time()>=DEADLINE:raise RuntimeError('Absolute evaluation deadline reached')
def read_child(child,timeout):
 sel=selectors.DefaultSelector();sel.register(child.stdout,selectors.EVENT_READ)
 stop=min(DEADLINE,time.time()+timeout);buffer=getattr(child,'_eval_buffer',b'')
 try:
  while b'\n' not in buffer:
   left=stop-time.time()
   if left<=0 or not sel.select(left):raise TimeoutError('Laya subprocess response timed out')
   chunk=os.read(child.stdout.fileno(),65536)
   if not chunk:raise RuntimeError('Laya subprocess exited without response')
   buffer+=chunk
  line,buffer=buffer.split(b'\n',1);child._eval_buffer=buffer
  return json.loads(line)
 finally:sel.close()

def runtime_validation(n,a):
 err=_validate_args(REGISTRY[n],a)
 if err:return err
 if n=='add_calendar_event' and calendar_time_problem(a.get('when_iso','')):return 'invalid calendar local start'
 if n in ('view_emails','summarize_emails','view_messages','summarize_messages','search_notes') and a.get('day'):
  d=a['day']
  if d not in ('today','yesterday'):
   if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',d):return 'unsupported day'
   try:datetime.fromisoformat(d)
   except ValueError:return 'invalid day'
 if n=='find_free_time' and a.get('period') and not re.fullmatch(r'today|tomorrow|\d{4}-\d{2}-\d{2}',a['period']):return 'unsupported availability period'
 if n in ('get_upcoming','find_free_time','view_emails','summarize_emails','view_messages','summarize_messages','search_notes') and a.get('period'):
  try:resolve_span(a['period'],now=ANCHOR)
  except Exception:return 'unsupported period'
 return None

ARMS=['current_rules','ling_direct','ling_plan','ling_intent','laya_hybrid']

def memory():
 try:
  s=subprocess.check_output(['memory_pressure','-Q'],text=True,timeout=4)
  return int(re.search(r'free percentage:\s*(\d+)',s).group(1))
 except Exception:return None

def pct(xs,p):
 return sorted(xs)[round((len(xs)-1)*p)] if xs else None

def period_equal(a,b):
 try:return resolve_span(str(a),now=ANCHOR)[:2]==resolve_span(str(b),now=ANCHOR)[:2]
 except Exception:return str(a).strip().lower()==str(b).strip().lower()

def matches(v,expected,key):
 if isinstance(expected,dict):
  if 'contains' in expected:return expected['contains'].casefold() in str(v).casefold()
  if 'endswith' in expected:return str(v).endswith(expected['endswith'])
  if 'iso' in expected:
   try:
    actual=datetime.fromisoformat(str(v));expected_dt=datetime.fromisoformat(expected['iso']).replace(tzinfo=ZoneInfo('America/Los_Angeles'))
    actual=actual if actual.tzinfo else actual.replace(tzinfo=ZoneInfo('America/Los_Angeles'))
    return actual.timestamp()==expected_dt.timestamp()
   except:return False
  if 'duration_seconds' in expected:
   s=str(v).lower().strip();m=re.fullmatch(r'(\d+)\s*(?:minutes?|mins?|m)?',s)
   if m:return int(m[1])*60==expected['duration_seconds']
   m=re.fullmatch(r'(\d+)\s*(?:seconds?|secs?|s)',s)
   return bool(m and int(m[1])==expected['duration_seconds'])
 if key in ['period','day']:return period_equal(v,expected)
 if isinstance(expected,bool):return v is expected
 if isinstance(expected,str):return str(v).casefold()==expected.casefold()
 return v==expected

def grade(c,calls):
 g=c['gold'];names=[x['name'] for x in calls];required=set(g['tools']);got=set(names)
 bad_schema=[];arg_errors=[]
 for x in calls:
  if x['name'] not in REGISTRY:bad_schema.append(x['name']+': unknown');continue
  try:
   err=runtime_validation(x['name'],x['arguments'])
   if err:bad_schema.append(x['name']+': '+str(err))
   if x['name']=='add_calendar_event' and calendar_time_problem(x['arguments'].get('when_iso','')):bad_schema.append('calendar: invalid local start')
   if x['name'] in ('get_upcoming','find_free_time','view_emails','summarize_emails','view_messages','summarize_messages','search_notes') and x['arguments'].get('period'):
    try:resolve_span(x['arguments']['period'],now=ANCHOR)
    except Exception:bad_schema.append(x['name']+': unsupported period')
  except Exception as e:bad_schema.append(type(e).__name__)
 for n,expect in g['args'].items():
  opts=[x['arguments'] for x in calls if x['name']==n]
  if not opts:arg_errors.append(n+': missing');continue
  good=False
  for a in opts:
   ok=True
   for k,v in expect.items():
    if k=='$range':ok=ok and (a.get('days')==7 and not a.get('period'))
    else:ok=ok and matches(a.get(k,a.get('day') if k=='period' else None),v,k)
   if ok:good=True
   else:good=False;break
  if not good:arg_errors.append(n+': '+json.dumps(expect))
 for n,keys in g['absent'].items():
  if any(any(a['arguments'].get(k) not in (None,'') for k in keys) for a in calls if a['name']==n):arg_errors.append(n+': unwanted date scope')
 forbidden=sorted(got&set(g['forbidden']))
 # Additional effect must never be rewarded on a read/no-tool case.
 writes={'send_email','send_message','draft_email','draft_message','add_calendar_event','add_reminder','cancel_event','reply_to_email','create_note'}
 unauthorized=sorted((got-required)&writes)
 duplicates=sorted(n for n in got if names.count(n)!=1)
 exact=got==required and not duplicates
 return {'duplicate_tools':duplicates,'tool_exact':exact,'required_tools_present':required<=got,'argument_errors':arg_errors,'schema_errors':bad_schema,'forbidden_calls':forbidden,'unrequested_effects':unauthorized,'strict':exact and not (arg_errors or bad_schema or forbidden or unauthorized)}

class Client:
 def __init__(self):
  s=json.loads((Path.home()/'.omlx/settings.json').read_text())
  self.http=httpx.Client(base_url='http://127.0.0.1:8000',headers={'Authorization':'Bearer '+s['auth']['api_key']},timeout=90,trust_env=False,follow_redirects=False)
 def status(self):
  deadline_check();r=self.http.get('/v1/models/status',timeout=max(.1,min(5,DEADLINE-time.time())));r.raise_for_status();return r.json()
 def infer(self,msgs,names=None,plan=False):
  deadline_check()
  loaded=[x['id'] for x in self.status().get('models',[]) if x.get('loaded')]
  if MODEL not in loaded:raise RuntimeError('Resident Ling absent; refusing implicit model load')
  b={'model':MODEL,'messages':msgs,'temperature':0,'max_tokens':700,'stream':False,'chat_template_kwargs':{'enable_thinking':False}}
  if names:b.update(tools=tool_schemas(names),tool_choice='auto')
  if plan:b['response_format']={'type':'json_object'}
  deadline_check();t=time.perf_counter();r=self.http.post('/v1/chat/completions',json=b,timeout=max(.1,min(90,DEADLINE-time.time())));r.raise_for_status();d=r.json()
  return {'seconds':time.perf_counter()-t,'response':d,'request':b}

def parse_calls(r):
 m=r['response']['choices'][0]['message'];calls=[]
 for t in m.get('tool_calls') or []:
  f=t['function'];a=f.get('arguments',{})
  try:a=json.loads(a) if isinstance(a,str) else a
  except:a={'__invalid_json__':str(a)}
  calls.append({'name':f['name'],'arguments':a})
 return calls

async def current(c):
 history=c.get('context',[]);users=[x['content'] for x in history if x['role']=='user'];assist=[x['content'] for x in history if x['role']=='assistant'];last=assist[-1] if assist else ''
 m=re.search(r'\[Tools: ([^]]+)\]',last);lasttools=m[1] if m else ''
 cr=compile_read(c['prompt'],last_user=users[-1] if users else '',last_tools=lasttools)
 if cr is not None:
  return {'subset':[n for n,a in cr[0]],'reason':'production structured-read shortcut','route_source':'structured_read','needs_tools':bool(cr[0]),'direct':cr[0],'forbidden':[],'force_first_tool':None,'bindings':{},'resolved_request':None,'direct_text':cr[1]}
 d=await router.route(c['prompt'],last_user=users[-1] if users else None,recent_users=users,last_assistant=last,last_tools=lasttools)
 return {'subset':d.tool_subset,'reason':d.reason,'route_source':d.source,'needs_tools':d.needs_tools,'direct':d.direct_calls or None,'forbidden':list(d.forbidden_tools),'force_first_tool':d.force_first_tool,'bindings':d.tool_argument_bindings,'resolved_request':d.resolved_request}

def summarize(path):
 rows=[json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
 out={}
 for arm in ARMS:
  for split in ['dev','test','challenge']:
   rs=[r for r in rows if r['arm']==arm and r['split']==split];valid=[r for r in rs if 'grade'in r]
   if not rs:continue
   out[arm+'/'+split]={'n':len(rs),'strict_pass':sum(r.get('grade',{}).get('strict',False) for r in rs),'tool_exact':sum(r.get('grade',{}).get('tool_exact',False) for r in rs),'errors':len(rs)-len(valid),'forbidden_call_cases':sum(bool(r['grade']['forbidden_calls']) for r in valid),'unrequested_effect_cases':sum(bool(r['grade']['unrequested_effects']) for r in valid),'p50_seconds':pct([r['seconds'] for r in rs],.5),'p95_seconds':pct([r['seconds'] for r in rs],.95)}
 path.with_suffix('.summary.json').write_text(json.dumps(out,indent=2)+'\n')
 return out

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--split',choices=['dev','test','challenge','all'],default='dev');ap.add_argument('--arms',default=','.join(ARMS));ap.add_argument('--reps',type=int,default=1);ap.add_argument('--out',default='primary.jsonl');ap.add_argument('--limit',type=int,default=0);opts=ap.parse_args()
 lock=open('/private/tmp/wisp-routing-eval.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 out=HERE/opts.out
 data=json.loads((HERE/'corpus.json').read_text());cases=[x for x in data if opts.split=='all' or x['split']==opts.split]
 if opts.limit:cases=cases[:opts.limit]
 arms=opts.arms.split(',');cli=Client();child=None
 done={(r['id'],r['arm'],r['rep']) for r in [json.loads(x) for x in out.read_text().splitlines()]} if out.exists() else set()
 manifest={'base':'fa66cb3e3b0b0c207fe0dce7cc42de4c912f93b0','pid':os.getpid(),'started':time.time(),'model':MODEL,'anchor':NOW,'corpus_sha256':hashlib.sha256((HERE/'corpus.json').read_bytes()).hexdigest(),'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [HERE/n for n in ('common.py','intent.py','run_eval.py','laya_worker.py')]},'arms':arms,'system':SYSTEM,'route_instruction':ROUTE_INSTRUCTION,'intent_system':INTENT_SYSTEM,'catalog':ALL_NAMES,'laya_threshold':0.8,'status':cli.status(),'memory_free_percent':memory(),'measurement':'actual inference; first proposed calls only; no real tool execution; ambient desktop contention possible'}
 manifest_path=HERE/(opts.out+'.manifest.json')
 if out.exists():
  if not manifest_path.exists():raise RuntimeError('Refusing resume without original manifest')
  original=json.loads(manifest_path.read_text())
  for key in ['base','model','anchor','corpus_sha256','source_sha256','arms','system','route_instruction','intent_system','catalog','laya_threshold']:
   if original.get(key)!=manifest.get(key):raise RuntimeError('Frozen experiment mismatch: '+key+'; use a new output filename')
  manifest=original
 else:manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
 try:
  if 'laya_hybrid' in arms:
   child=subprocess.Popen(['/private/tmp/wisp-routing-eval-laya-venv/bin/python','-u',str(HERE/'laya_worker.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=open(HERE/'laya.stderr.log','a'),text=True,bufsize=1)
   ready=read_child(child,120);manifest['laya_start']=ready
   (HERE/(opts.out+'.manifest.json')).write_text(json.dumps(manifest,indent=2)+'\n')
  jobs=[(c,a,rep) for rep in range(opts.reps) for c in cases for a in arms];random.Random(20261005).shuffle(jobs)
  for c,arm,rep in jobs:
   if (c['id'],arm,rep) in done:continue
   # Bound overnight execution to 08:30 Pacific; continuation handles final review.
   deadline_check()
   free=memory()
   if free is not None and free<12:raise RuntimeError('Memory pressure: less than 12 percent free; stopped before inference')
   row={'id':c['id'],'family':c['family'],'split':c['split'],'arm':arm,'rep':rep,'prompt':c['prompt'],'timestamp':time.time(),'memory_free_percent':free}
   start=time.perf_counter();names=ALL_NAMES;calls=None
   try:
    if arm=='current_rules':
     t=time.perf_counter();r=asyncio.run(current(c));row['route']=r;row['route_seconds']=time.perf_counter()-t
     if r['direct'] is not None:calls=[{'name':n,'arguments':a} for n,a in r['direct']]
     names=[n for n in (r['subset'] if r['subset'] is not None else ALL_NAMES) if n not in r['forbidden']]
     if not r['needs_tools']:names=[]
     if r['force_first_tool']:names=[r['force_first_tool']]
    elif arm=='ling_plan':
     pm=[{'role':'system','content':ROUTE_INSTRUCTION+' Return JSON only: {"domain": "one_label"}. Labels: '+json.dumps(LABELS)}]+c.get('context',[])+[{'role':'user','content':c['prompt']}]
     r=cli.infer(pm,plan=True);row['planner']=r;domain=json.loads(r['response']['choices'][0]['message']['content'])['domain'];row['domain']=domain
     names=ALL_NAMES if domain=='unclear' or domain not in DOMAINS else DOMAINS[domain]
    elif arm=='ling_intent':
     r=cli.infer(intent_messages(c),plan=True);row['planner']=r
     x=json.loads(r['response']['choices'][0]['message']['content']);row['intent']=x;row['domain']=x.get('domain')
     calls=compile_intent(x,runtime_validation,lambda p:resolve_span(p,now=ANCHOR))
     row['compiled']=calls is not None
     names=DOMAINS.get(x.get('domain'),ALL_NAMES) if x.get('domain')!='unclear' else ALL_NAMES
     if calls is None and not names:names=ALL_NAMES
    elif arm=='laya_hybrid':
     child.stdin.write(json.dumps({'case':c})+'\n');child.stdin.flush();r=read_child(child,90);row['planner']=r
     if 'error'in r:raise RuntimeError(r['error'])
     if r.get('id')!=c['id']:raise RuntimeError('Laya response case ID mismatch; stopping to preserve attribution')
     ans=r['result']['answers']['route'];domain=ans['choice'];usage=r['result']['usage'];row['domain']=domain
     fallback=(domain=='unclear' or ans['answer_confidence']<.8 or usage.get('truncated') or bool(usage.get('options')))
     row['fallback']=bool(fallback);names=ALL_NAMES if fallback else DOMAINS.get(domain,ALL_NAMES)
    row['offered_tools']=names
    if calls is None:
     effective=dict(c)
     if arm=='current_rules' and row['route'].get('resolved_request'):effective['prompt']=row['route']['resolved_request']
     r=cli.infer(messages(effective),names);row['generation']=r;calls=parse_calls(r)
     # Frozen router bindings represent enforced arguments in production.
     if arm=='current_rules':
      for call in calls:call['arguments'].update(row['route']['bindings'].get(call['name'],{}))
    row['calls']=calls;row['grade']=grade(c,calls)
    if calls is not None and any(x['name'] not in names for x in calls) and not (arm=='current_rules' and row['route'].get('direct') is not None) and not (arm=='ling_intent' and row.get('compiled')):
     row['grade']['unoffered_calls']=[x['name'] for x in calls if x['name'] not in names];row['grade']['strict']=False
   except Exception as e:row['error']=type(e).__name__+': '+str(e)
   row['seconds']=time.perf_counter()-start;row['harness_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
   with out.open('a') as f:f.write(json.dumps(row)+'\n');f.flush();os.fsync(f.fileno())
   done.add((c['id'],arm,rep));summarize(out)
   if arm=='laya_hybrid' and row.get('error'):raise RuntimeError('Stopped on Laya protocol/inference failure; see preserved row')
   (HERE/'progress.json').write_text(json.dumps({'pid':os.getpid(),'output':str(out),'finished_rows':len(done),'planned_rows':len(jobs),'last_case':c['id'],'last_arm':arm,'updated':time.time(),'complete':False},indent=2)+'\n')
   print(json.dumps({'id':c['id'],'arm':arm,'strict':row.get('grade',{}).get('strict'),'error':row.get('error'),'seconds':round(row['seconds'],3)}),flush=True)
  print(json.dumps(summarize(out),indent=2),flush=True)
  (HERE/'progress.json').write_text(json.dumps({'pid':os.getpid(),'output':str(out),'finished_rows':len(done),'planned_rows':len(jobs),'updated':time.time(),'complete':len(done)>=len(jobs)},indent=2)+'\n')
 finally:
  if child:child.terminate();child.wait(timeout=10)
if __name__=='__main__':main()
