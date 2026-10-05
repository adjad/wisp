"""Post-freeze analysis only; no inference or candidate modifications."""
import argparse,hashlib,json,random,statistics
from pathlib import Path
from collections import Counter,defaultdict
from analyze_results import read_rows,stats,source_of,q
HERE=Path(__file__).resolve().parent

def verified(name,expected):
 rows=read_rows(HERE/name);keys=[(r['id'],r['arm'],r['rep']) for r in rows]
 if set(keys)!=expected or len(set(keys))!=len(keys):raise ValueError(name+' incomplete or duplicate')
 m=json.loads((HERE/(name+'.manifest.json')).read_text());c=m.get('contract',m)
 hashes=c.get('source_sha256',{})
 for path,h in hashes.items():
  if hashlib.sha256((HERE/path).read_bytes()).hexdigest()!=h:raise ValueError(name+' source mismatch '+path)
 if 'corpus_sha256' in c:
  corpus='followon_corpus.json' if name.startswith('followon') else 'corpus.json'
  if hashlib.sha256((HERE/corpus).read_bytes()).hexdigest()!=c['corpus_sha256']:raise ValueError('Corpus mismatch')
 return rows,m

def candidate_metrics(rs,gold):
 bycase=defaultdict(list);byfamily=defaultdict(list)
 for r in rs:bycase[r['id']].append(r);byfamily[r['family']].append(r)
 ok=lambda r:bool(r.get('grade',{}).get('strict')) and not r.get('error')
 source=lambda r:not r.get('error') and {source_of(c['name']) for c in r.get('calls',[])}==set(gold[r['id']]['gold']['domains'])-{'none'}
 return {'trials':len(rs),'unique_cases':len(bycase),'strict_success':sum(ok(r) for r in rs),'strict_rate':statistics.mean(ok(r) for r in rs),'cases_all_reps_pass':sum(all(ok(r) for r in xs) for xs in bycase.values()),'cases_with_varying_success':sum(len({ok(r) for r in xs})>1 for xs in bycase.values()),'tool_exact':sum(r.get('grade',{}).get('tool_exact',False) for r in rs),'source_exact':sum(source(r) for r in rs),'forbidden_call_trials':sum(bool(r.get('grade',{}).get('forbidden_calls')) for r in rs),'unrequested_effect_trials':sum(bool(r.get('grade',{}).get('unrequested_effects')) for r in rs),'invalid_intent_trials':sum(bool(r.get('invalid_intent')) for r in rs),'repaired_trials':sum(len(r.get('planner_trace',[]))>1 for r in rs),'errors':dict(Counter(r['error'].split(':')[0] for r in rs if 'error'in r)),'latency_seconds':stats([r['seconds'] for r in rs]),'memory_free_percent':stats([r['memory_free_percent'] for r in rs if r.get('memory_free_percent') is not None]),'family_accuracy':{f:statistics.mean(ok(r) for r in xs) for f,xs in sorted(byfamily.items())},'strict_failure_cases':[cid for cid,xs in bycase.items() if not all(ok(r) for r in xs)]}

def main():
 old=json.loads((HERE/'corpus.json').read_text());fresh=json.loads((HERE/'followon_corpus.json').read_text());oldgold={c['id']:c for c in old};newgold={c['id']:c for c in fresh};oldtest=[c for c in old if c['split']=='test'];newtest=[c for c in fresh if c['split']=='test'];arms=['current_rules','ling_direct','ling_intent','ling_intent_v2']
 fr,fm=verified('followon_test.jsonl',{(c['id'],a,r) for c in newtest for a in arms for r in range(3)})
 er,em=verified('english_heldout.jsonl',{(c['id'],'laya_english_hybrid',r) for c in oldtest for r in range(3)})
 es,esm=verified('english_stability.jsonl',{(c['id'],'laya_english_source',r) for c in old for r in range(3)})
 ch,cm=verified('challenges.jsonl',{(c['id'],a,r) for c in old if c['split']=='challenge' for a in ['current_rules','ling_direct','ling_plan','ling_intent','laya_hybrid'] for r in range(2)})
 out={'all_stages_complete':True,'scope':'Post-primary follow-on fresh corpus with pre-test candidate freeze, plus fixed English checkpoint replication on original corpus. Do not pool accuracy across different corpora or interpret sequential latency as randomized cross-checkpoint performance.','followon':{a:candidate_metrics([r for r in fr if r['arm']==a],newgold) for a in arms},'english_hybrid':candidate_metrics(er,oldgold),'english_stability':{},'challenges':{'trials':len(ch),'cases':len({r['id'] for r in ch}),'strict_aggregate_reported':False},'input_sha256':{name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in ['followon_test.jsonl','followon_test.jsonl.manifest.json','english_heldout.jsonl','english_heldout.jsonl.manifest.json','english_stability.jsonl','english_stability.jsonl.manifest.json','challenges.jsonl']}}
 out['english_hybrid']['fallback_trials']=sum(r['fallback'] for r in er)
 out['english_hybrid']['worker_starts']=em.get('worker_starts')
 out['english_hybrid']['checkpoint_files']=em['contract']['checkpoint_files']
 out['english_hybrid']['raw_laya_seconds']=stats([r['planner']['seconds'] for r in er])
 out['english_hybrid']['worker_peak_rss_bytes_max']=max(r['planner']['peak_rss_bytes'] for r in er)
 for order in range(3):
  rs=[r for r in es if r['rep']==order and r['split']=='test'];high=[r for r in rs if r['planner']['result']['answers']['route']['answer_confidence']>=.8]
  out['english_stability'][str(order)]={'n':len(rs),'source_correct':sum(r['source_correct'] for r in rs),'high_probability_cases':len(high),'high_probability_errors':sum(not r['source_correct'] for r in high),'latency_seconds':stats([r['planner']['seconds'] for r in rs]),'truncated_cases':sum(r['planner']['result']['usage'].get('truncated',False) for r in rs),'collapsed_option_cases':sum(bool(r['planner']['result']['usage'].get('options')) for r in rs),'worker_peak_rss_bytes_max':max(r['planner']['peak_rss_bytes'] for r in rs)}
 out['english_stability']['option_order_disagreements']=sum(len({r['domain'] for r in es if r['id']==c['id']})>1 for c in oldtest)
 out['english_stability']['worker_starts']=esm.get('worker_starts')
 out['followon_family_differences']={};rng=random.Random(50311)
 for a in arms[1:]:
  x=out['followon'][a]['family_accuracy'];b=out['followon']['current_rules']['family_accuracy'];diffs=[x[k]-b[k] for k in sorted(b)];boots=[statistics.mean(rng.choices(diffs,k=len(diffs))) for _ in range(4000)]
  out['followon_family_differences'][a]={'family_balanced_mean_vs_rules':statistics.mean(diffs),'descriptive_bootstrap_95':[q(boots,.025),q(boots,.975)],'families':len(diffs)}
 # Original context-format stratification is descriptive, not a rerun or a cause estimate.
 original=read_rows(HERE/'heldout.jsonl');out['original_context_strata']={}
 for context in (False,True):
  out['original_context_strata'][str(context)]={a:candidate_metrics([r for r in original if r['arm']==a and bool(oldgold[r['id']]['context'])==context],oldgold) for a in sorted({r['arm'] for r in original})}
 out['challenges']['first_rep_proposals']=[{'id':r['id'],'arm':r['arm'],'prompt':r['prompt'],'calls':r.get('calls'),'error':r.get('error'),'answer_text':r.get('generation',{}).get('response',{}).get('choices',[{}])[0].get('message',{}).get('content')} for r in ch if r['rep']==0]
 (HERE/'extension_analysis.json').write_text(json.dumps(out,indent=2)+'\n')
 print(json.dumps({'followon':{a:{k:v for k,v in x.items() if k in ['trials','strict_success','cases_all_reps_pass','source_exact','tool_exact','forbidden_call_trials','unrequested_effect_trials','latency_seconds','errors']} for a,x in out['followon'].items()},'english_hybrid':{k:v for k,v in out['english_hybrid'].items() if k in ['trials','strict_success','source_exact','tool_exact','fallback_trials','forbidden_call_trials','unrequested_effect_trials','latency_seconds','raw_laya_seconds','worker_peak_rss_bytes_max']},'english_stability':out['english_stability']},indent=2))
if __name__=='__main__':main()
