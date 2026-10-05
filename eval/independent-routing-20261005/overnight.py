"""Serialized, bounded orchestration. Safe to restart after checking its PID."""
import json,os,subprocess,sys,time,fcntl
from pathlib import Path
HERE=Path(__file__).resolve().parent
STEPS=[
 ['run_eval.py','--split','test','--reps','3','--out','heldout.jsonl'],
 ['run_eval.py','--split','challenge','--reps','2','--out','challenges.jsonl'],
 ['answer_eval.py'],
 ['laya_stability.py'],
]
def main():
 lock=open('/private/tmp/wisp-routing-eval-orchestrator.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 (HERE/'overnight.pid').write_text(str(os.getpid())+'\n')
 for step in STEPS:
  (HERE/'overnight_status.json').write_text(json.dumps({'pid':os.getpid(),'stage':step,'updated':time.time(),'state':'running'},indent=2)+'\n')
  with (HERE/(step[0]+'.log')).open('a') as log:
   r=subprocess.run([sys.executable,'-u',str(HERE/step[0]),*step[1:]],cwd=HERE.parents[1],stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
  if r.returncode:
   (HERE/'overnight_status.json').write_text(json.dumps({'pid':os.getpid(),'stage':step,'updated':time.time(),'state':'stopped','exit_code':r.returncode},indent=2)+'\n');return r.returncode
 (HERE/'overnight_status.json').write_text(json.dumps({'pid':os.getpid(),'updated':time.time(),'state':'measurements_complete','next':'Independent result audit, aggregate report, commit/push report and deliver at9AM'},indent=2)+'\n')
 return 0
if __name__=='__main__':sys.exit(main())
