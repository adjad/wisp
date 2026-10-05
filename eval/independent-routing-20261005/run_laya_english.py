"""Predeclared English Laya replication; fixed .8 threshold, no tuning.
Uses already-present weights and an isolated runtime; no real tool execution.
"""
import argparse,fcntl,hashlib,json,os,random,resource,subprocess,time
from pathlib import Path
from run_eval import Client,read_child,grade,parse_calls,memory,deadline_check,pct
from common import MODEL,NOW,ALL_NAMES,DOMAINS,LABELS,LAYA_QUESTION,messages
HERE=Path(__file__).resolve().parent
PYTHON='/private/tmp/wisp-routing-eval-english-venv/bin/python'
MODEL_PATH='/Users/adijain/Desktop/OMLX_Model_Files/aac6fef/laya-mlx'
def checkpoint_fingerprints():
 root=Path(MODEL_PATH).resolve();manifest=json.loads((root/'manifest.json').read_text());result={}
 for name,expected in manifest['files'].items():
  path=(root/name).resolve()
  if not path.is_relative_to(root):raise RuntimeError('Checkpoint manifest path escapes root')
  h=hashlib.sha256()
  with path.open('rb') as stream:
   for chunk in iter(lambda:stream.read(4*1024*1024),b''):h.update(chunk)
  actual={'bytes':path.stat().st_size,'sha256':h.hexdigest()}
  if actual!=expected:raise RuntimeError('Checkpoint manifest mismatch: '+name)
  result[name]=actual
 if result['model.safetensors']['sha256']!='b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de':raise RuntimeError('Unexpected English checkpoint weights')
 result['manifest.json']={'bytes':(root/'manifest.json').stat().st_size,'sha256':hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()}
 return result

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--mode',choices=['hybrid','stability'],required=True);ap.add_argument('--split',choices=['dev','test','all'],default='test');ap.add_argument('--reps',type=int,default=3);ap.add_argument('--out',required=True);args=ap.parse_args()
 lock=open('/private/tmp/wisp-routing-eval.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 data=json.loads((HERE/'corpus.json').read_text());cases=[c for c in data if args.split=='all' or c['split']==args.split];out=HERE/args.out;mp=HERE/(args.out+'.manifest.json')
 fingerprints=checkpoint_fingerprints()
 contract={'checkpoint_files':fingerprints,'arm':'laya_english_hybrid' if args.mode=='hybrid' else 'laya_english_source','mode':args.mode,'split':args.split,'reps':args.reps,'model':MODEL,'laya_path':MODEL_PATH,'laya_weight_sha256':'b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de','laya_hf_revision':'047678560251f28113ee8f5df4be82102c7bf336','threshold':.8,'source_sha256':{n:hashlib.sha256((HERE/n).read_bytes()).hexdigest() for n in ['run_laya_english.py','laya_english_worker.py','laya-english-runtime-lock.txt','run_eval.py','common.py','corpus.json']},'scope':'English Laya separate checkpoint/runtime replication after primary results; no threshold/prompt fitting to test, no native execution'}
 if out.exists():
  if json.loads(mp.read_text())['contract']!=contract:raise RuntimeError('Changed inputs; choose new filename')
 else:mp.write_text(json.dumps({'contract':contract,'started':time.time(),'pid':os.getpid()},indent=2)+'\n')
 done={(r['id'],r['rep']) for r in (json.loads(l) for l in out.read_text().splitlines())} if out.exists() else set()
 jobs=[(c,r) for c in cases for r in range(args.reps)];random.Random(51028).shuffle(jobs)
 deadline_check();free=memory()
 if free is not None and free<18:raise RuntimeError('Insufficient free memory before worker load')
 child=subprocess.Popen([PYTHON,'-u',str(HERE/'laya_english_worker.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=open(HERE/'laya_english.stderr.log','a'),text=True,bufsize=1)
 try:
  ready=read_child(child,120)
  if not ready.get('ready'):raise RuntimeError('Worker not ready: '+str(ready))
  manifest=json.loads(mp.read_text());manifest.setdefault('worker_starts',[]).append({'timestamp':time.time(),'ready':ready});mp.write_text(json.dumps(manifest,indent=2)+'\n')
  cli=Client() if args.mode=='hybrid' else None
  for c,rep in jobs:
   if (c['id'],rep) in done:continue
   deadline_check();free=memory()
   if free is not None and free<12:raise RuntimeError('Memory pressure')
   question=LAYA_QUESTION
   if args.mode=='stability':
    keys=list(LABELS)
    if rep==1:keys.reverse()
    elif rep==2:random.Random(20261005).shuffle(keys)
    question={'route':{**LAYA_QUESTION['route'],'criteria':{k:LABELS[k] for k in keys}}}
   row={'id':c['id'],'family':c['family'],'split':c['split'],'arm':contract['arm'],'rep':rep,'prompt':c['prompt'],'timestamp':time.time(),'memory_free_percent':free,'question':question};start=time.perf_counter()
   try:
    child.stdin.write(json.dumps({'case':c,'question':question})+'\n');child.stdin.flush();r=read_child(child,90);row['planner']=r
    if 'error' in r:raise RuntimeError(r['error'])
    if r.get('id')!=c['id']:raise RuntimeError('Worker case ID mismatch')
    ans=r['result']['answers']['route'];domain=ans['choice'];usage=r['result']['usage'];row['domain']=domain
    predicted={'email','messages'} if domain=='mail_and_messages' else {domain};row['source_correct']=predicted==set(c['gold']['domains'])
    row['fallback']=bool(domain=='unclear' or ans['answer_confidence']<.8 or usage.get('truncated') or usage.get('options'))
    if args.mode=='hybrid':
     names=ALL_NAMES if row['fallback'] else DOMAINS.get(domain,ALL_NAMES);row['offered_tools']=names;r=cli.infer(messages(c),names);row['generation']=r;calls=parse_calls(r);row['calls']=calls;row['grade']=grade(c,calls)
     if any(call['name'] not in names for call in calls):row['grade']['unoffered_calls']=[call['name'] for call in calls if call['name'] not in names];row['grade']['strict']=False
   except Exception as err:row['error']=type(err).__name__+': '+str(err)
   row['seconds']=time.perf_counter()-start;row['harness_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
   with out.open('a') as f:f.write(json.dumps(row)+'\n');f.flush();os.fsync(f.fileno())
   done.add((c['id'],rep));(HERE/'english_progress.json').write_text(json.dumps({'pid':os.getpid(),'output':str(out),'finished_rows':len(done),'planned_rows':len(jobs),'complete':len(done)==len(jobs),'updated':time.time()},indent=2)+'\n')
   print(json.dumps({'id':c['id'],'rep':rep,'strict':row.get('grade',{}).get('strict'),'source':row.get('source_correct'),'error':row.get('error')}),flush=True)
   if row.get('error'):raise RuntimeError('Stopped on preserved English runtime error')
  if checkpoint_fingerprints()!=fingerprints:raise RuntimeError('Checkpoint changed during measurement; results invalid')
  rs=[json.loads(l) for l in out.read_text().splitlines()];summary={'n':len(rs),'strict_success':sum(r.get('grade',{}).get('strict',False) for r in rs),'source_correct':sum(r.get('source_correct',False) for r in rs),'fallbacks':sum(r.get('fallback',False) for r in rs),'p50_seconds':pct([r['seconds'] for r in rs],.5),'p95_seconds':pct([r['seconds'] for r in rs],.95),'worker_start':ready}
  out.with_suffix('.summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))
 finally:
  child.terminate();child.wait(timeout=10)
if __name__=='__main__':main()
