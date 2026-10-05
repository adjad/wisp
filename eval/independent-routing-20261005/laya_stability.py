"""Raw source classification across three fixed option orders; no tool execution."""
import json,subprocess,time,fcntl,hashlib,random
from common import HERE,LABELS,ROUTE_INSTRUCTION
from run_eval import read_child,deadline_check,memory

def main():
 lock=open('/private/tmp/wisp-routing-eval.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 cases=json.loads((HERE/'corpus.json').read_text());out=HERE/'laya_stability.jsonl'
 done={(r['id'],r['order']) for r in [json.loads(x) for x in out.read_text().splitlines()]} if out.exists() else set()
 labels=list(LABELS);orders=[labels,list(reversed(labels))];shuffled=labels.copy();random.Random(724).shuffle(shuffled);orders.append(shuffled)
 manifest={'corpus':hashlib.sha256((HERE/'corpus.json').read_bytes()).hexdigest(),'orders':orders,'instructions':ROUTE_INSTRUCTION,'harness':hashlib.sha256(__import__('pathlib').Path(__file__).read_bytes()).hexdigest()}
 mp=HERE/'laya_stability.manifest.json'
 if mp.exists() and json.loads(mp.read_text())!=manifest:raise RuntimeError('Frozen Laya stability mismatch')
 mp.write_text(json.dumps(manifest,indent=2)+'\n')
 child=subprocess.Popen(['/private/tmp/wisp-routing-eval-laya-venv/bin/python','-u',str(HERE/'laya_worker.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=open(HERE/'laya.stderr.log','a'),text=True,bufsize=1)
 try:
  print(read_child(child,120),flush=True)
  for order,ls in enumerate(orders):
   for c in cases:
    if (c['id'],order) in done:continue
    deadline_check()
    if (memory() or 100)<12:raise RuntimeError('Memory pressure')
    q={'route':{'type':'choice','instructions':ROUTE_INSTRUCTION,'criteria':{k:LABELS[k] for k in ls}}}
    child.stdin.write(json.dumps({'case':c,'question':q})+'\n');child.stdin.flush();r=read_child(child,30)
    if r.get('id')!=c['id'] or 'error'in r:raise RuntimeError('Laya protocol/inference failure '+str(r))
    expected=c['gold']['domains'];expected=['mail_and_messages'] if set(expected)=={'email','messages'} else expected
    r.update(order=order,split=c['split'],family=c['family'],expected=expected,source_correct=r['result']['answers']['route']['choice'] in expected)
    with out.open('a') as f:f.write(json.dumps(r)+'\n');f.flush()
   print('finished order',order,flush=True)
 finally:child.terminate();child.wait(timeout=10)
if __name__=='__main__':main()
