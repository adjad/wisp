"""Serialized frozen follow-on measurements; never runs native tools."""
import fcntl,json,os,subprocess,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
PYTHON='/Users/adijain/Desktop/MOE_Project/.venv/bin/python'
JOBS=[['run_followon.py','--split','test','--reps','3','--out','followon_test.jsonl'],['run_laya_english.py','--mode','hybrid','--split','test','--reps','3','--out','english_heldout.jsonl'],['run_laya_english.py','--mode','stability','--split','all','--reps','3','--out','english_stability.jsonl']]
def main():
 lock=open('/private/tmp/wisp-routing-eval-orchestrator.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=HERE,text=True).strip()
 def state(**kw):(HERE/'extension_status.json').write_text(json.dumps({'pid':os.getpid(),'revision':revision,'updated':time.time(),**kw},indent=2)+'\n')
 for job in JOBS:
  with (HERE/(job[-1]+'.log')).open('a') as log:
   child=subprocess.Popen([PYTHON,'-u',str(HERE/job[0]),*job[1:]],cwd=HERE.parents[1],stdout=log,stderr=log,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
   state(state='running',job=job,child_pid=child.pid);code=child.wait()
   if code:state(state='failed',job=job,returncode=code);raise SystemExit(code)
 state(state='measurements_complete',next='Analyze fresh follow-on and English checkpoint results; do not tune on test outputs; draft 9AM report')
if __name__=='__main__':main()
