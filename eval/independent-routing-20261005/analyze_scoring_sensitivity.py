"""Separately labeled sensitivity; never changes raw/gold/primary scores."""
import copy,hashlib,inspect,json
from pathlib import Path
from collections import defaultdict
from run_eval import REGISTRY,grade
HERE=Path(__file__).resolve().parent
EQUIVALENCES={'get_upcoming':{'days':7,'calendar_only':False},'view_emails':{'count':5},'send_message':{'confirmed_self_send':False},'send_email':{'confirmed_self_send':False}}
def normalize(c,calls):
 c=copy.deepcopy(c);calls=copy.deepcopy(calls)
 if c['id']=='local_no_web-1':c['gold']['args'].get('get_upcoming',{}).pop('calendar_only',None)
 for call in calls:
  n=call['name'];a=call['arguments'];expected=c['gold']['args'].get(n,{})
  if n=='get_upcoming' and 'days' not in a and ('$range' in expected or expected.get('days')==7):a['days']=7
  if n=='view_emails' and 'count' not in a and expected.get('count')==5:a['count']=5
  for k in c['gold']['absent'].get(n,[]):
   d=EQUIVALENCES.get(n,{})
   if k in a and k in d and type(a[k]) is type(d[k]) and a[k]==d[k]:del a[k]
 return c,calls

def main():
 for n,args in EQUIVALENCES.items():
  signature=inspect.signature(REGISTRY[n].func)
  for k,v in args.items():assert signature.parameters[k].default==v,(n,k)
 out={'scope':'Only proven omitted days=7 / count=5 equivalence, harmless explicit registered defaults in absent checks, and removal of over-specified calendar_only on local_no_web-1. Original scores preserved; day/period alias is never shadowed. No relaxation of nondefault filters, malformed output, unrequested effects, query semantics or recipient identity.','equivalences':EQUIVALENCES,'results':{},'input_sha256':{}}
 for dataset,filename,corpus in [('original','heldout.jsonl','corpus.json'),('followon','followon_test.jsonl','followon_corpus.json'),('english_original','english_heldout.jsonl','corpus.json')]:
  gold={c['id']:c for c in json.loads((HERE/corpus).read_text())};rs=[json.loads(l) for l in (HERE/filename).read_text().splitlines()];keys=[(r['id'],r['arm'],r['rep']) for r in rs];assert len(keys)==len(set(keys));arms=defaultdict(list)
  for r in rs:
   before=bool(r.get('grade',{}).get('strict')) and not r.get('error');c,calls=normalize(gold[r['id']],r.get('calls',[]));after=bool(grade(c,calls)['strict']) and not r.get('error') and not r.get('invalid_intent')
   # Retain primary unoffered-tool policy even though grade() is shared independently.
   if r.get('grade',{}).get('unoffered_calls'):after=False
   arms[r['arm']].append((r,before,after))
  out['results'][dataset]={}
  for arm,xs in sorted(arms.items()):
   cases=defaultdict(list)
   for r,b,a in xs:cases[r['id']].append((b,a))
   out['results'][dataset][arm]={'trials':len(xs),'cases':len(cases),'before_trial_passes':sum(b for r,b,a in xs),'after_trial_passes':sum(a for r,b,a in xs),'before_all_reps_case_passes':sum(all(b for b,a in v) for v in cases.values()),'after_all_reps_case_passes':sum(all(a for b,a in v) for v in cases.values()),'changed_trials':[[r['id'],r['rep']] for r,b,a in xs if b!=a],'changed_cases':sorted({r['id'] for r,b,a in xs if b!=a})}
  for name in (filename,corpus):out['input_sha256'][name]=hashlib.sha256((HERE/name).read_bytes()).hexdigest()
 (HERE/'scoring_sensitivity.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out['results'],indent=2))
if __name__=='__main__':main()
