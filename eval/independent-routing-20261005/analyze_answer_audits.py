"""Join completed blinded reviews after their scores have been frozen."""
import json,hashlib,statistics,re
from pathlib import Path
from collections import Counter
HERE=Path(__file__).resolve().parent
DIMENSIONS=['grounding','scope','coverage','attribution','usefulness']
def main():
 reviews=[];inputs={}
 for name,key in [('answer_audit_one.json','rows'),('answer_audit_two.json','scores'),('answer_audit_three.json','answers')]:
  path=HERE/name;inputs[name]=hashlib.sha256(path.read_bytes()).hexdigest();reviews.extend(json.loads(path.read_text())[key])
 ids=[r['id'] for r in reviews]
 assert len(ids)==165 and len(set(ids))==165 and set(ids)=={f'A{i:03}' for i in range(1,166)}
 assert all(len(r['scores'])==5 and all(type(s)==int and 0<=s<=2 for s in r['scores']) for r in reviews)
 mapping={r['audit_id']:r for r in json.loads((HERE/'answer_review_key.json').read_text())}
 blind={r['audit_id']:r for r in json.loads((HERE/'blinded_answer_review.json').read_text())['answers']}
 primary_rows=[r for r in (json.loads(l) for l in (HERE/'heldout.jsonl').read_text().splitlines()) if r['rep']==0]
 primary={(r['id'],r['arm']):r for r in primary_rows}
 assert len(primary)==len(primary_rows), 'Duplicate primary rep0 rows'
 joined=[]
 for r in reviews:
  m=mapping[r['id']];b=blind[r['id']]
  joined.append({**r,**m,'first_call_strict':bool(primary[(m['case'],m['arm'])].get('grade',{}).get('strict')),'marker_only':bool(re.fullmatch(r'\s*\[Tools?:[^]]+\]\s*',b['answer'])),'empty':not b['answer'].strip(),'unfinished':b['unfinished']})
 def summarize(rs):
  return {'n':len(rs),'all_five_dimensions_2':sum(all(s==2 for s in r['scores']) for r in rs),'grounding_and_scope_2':sum(r['scores'][0]==2 and r['scores'][1]==2 for r in rs),'any_critical':sum(bool(r['critical']) for r in rs),'critical_ids':[r['id'] for r in rs if r['critical']],'marker_only':sum(r['marker_only'] for r in rs),'empty':sum(r['empty'] for r in rs),'unfinished':sum(r['unfinished'] for r in rs),'mean_dimension_scores':{d:statistics.mean(r['scores'][i] for r in rs) if rs else None for i,d in enumerate(DIMENSIONS)}}
 out={'scope':'165 controlled synthetic continuations, 11 selected original test cases x 5 arms x 3 source states. One blind reviewer per answer; scores are exploratory judgments, not a validated preference study or rendered-UI evaluation. Each reviewer received the same rubric and a shuffled mixture of arms.','audit_input_sha256':inputs,'clarification':'Email subject-only summaries need not disclose headers-only unless making unsupported body claims. Optional action offers are noncritical; proposed or claimed unrequested actions would be critical.','overall':summarize(joined),'arms':{},'by_source_state':{},'joined_reviews':joined}
 for arm in sorted({r['arm'] for r in joined}):
  rs=[r for r in joined if r['arm']==arm];out['arms'][arm]={'all':summarize(rs),'conditional_correct_first_call':summarize([r for r in rs if r['first_call_strict']])}
 for state in ('complete','partial','failed'):out['by_source_state'][state]=summarize([r for r in joined if r['state']==state])
 (HERE/'answer_audit_analysis.json').write_text(json.dumps(out,indent=2)+'\n')
 print(json.dumps({k:v for k,v in out.items() if k not in ['joined_reviews','audit_input_sha256']},indent=2))
if __name__=='__main__':main()
