"""Read-only inference-result analysis. Writes derived summaries; never calls models.

Repeated trials and paraphrases are correlated. Family-level paired bootstrap
intervals measure uncertainty within this authored corpus, not population truth.
"""
from __future__ import annotations
import json,random,statistics,hashlib,math,argparse
from pathlib import Path
from collections import defaultdict,Counter
HERE=Path(__file__).resolve().parent
ARMS=['current_rules','ling_direct','ling_plan','ling_intent','laya_hybrid']

def read_rows(path):
 if not path.exists():return []
 lines=path.read_text().splitlines();rows=[]
 for i,line in enumerate(lines):
  if not line.strip():continue
  try:rows.append(json.loads(line))
  except json.JSONDecodeError:
   if i==len(lines)-1:break # running writer may not have completed its last row
   raise
 return rows

def q(xs,p):
 if not xs:return None
 s=sorted(xs);return s[round((len(s)-1)*p)]

def stats(xs):
 return {'n':len(xs),'p50':q(xs,.5),'p95':q(xs,.95),'min':min(xs) if xs else None,'max':max(xs) if xs else None}

def source_of(name):
 if name in ('get_upcoming','find_free_time','get_past_events','add_calendar_event','add_reminder','search_reminders','cancel_event','update_reminder'):return 'calendar'
 if name in ('view_emails','summarize_emails','send_email','draft_email','reply_to_email','delete_email','forward_email'):return 'email'
 if name in ('view_messages','summarize_messages','send_message','draft_message'):return 'messages'
 if name in ('search_notes','create_note','append_note'):return 'notes'
 if name in ('web_search','web_fetch'):return 'web'
 if name=='get_weather':return 'weather'
 if name in ('get_battery_status','set_timer'):return 'device'
 if name in ('list_dir','find_files'):return 'files'
 return 'other:'+name

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--require-complete',action='store_true');args=parser.parse_args()
 corpus=json.loads((HERE/'corpus.json').read_text());gold={c['id']:c for c in corpus};test=[c for c in corpus if c['split']=='test'];rows=read_rows(HERE/'heldout.jsonl')
 expected={(c['id'],a,r) for c in test for a in ARMS for r in range(3)}
 actual=[(r['id'],r['arm'],r['rep']) for r in rows]
 counts=Counter(actual);duplicates=[list(k) for k,v in counts.items() if v>1]
 complete=set(actual)==expected and not duplicates
 if args.require_complete and not complete:raise SystemExit('Held-out set incomplete or duplicate rows; refusing final summary')
 manifest_path=HERE/'heldout.jsonl.manifest.json'
 if args.require_complete:
  if not manifest_path.exists():raise SystemExit('Missing held-out manifest')
  manifest=json.loads(manifest_path.read_text())
  if manifest['corpus_sha256']!=hashlib.sha256((HERE/'corpus.json').read_bytes()).hexdigest():raise SystemExit('Corpus hash mismatch')
  if any(hashlib.sha256((HERE/n).read_bytes()).hexdigest()!=h for n,h in manifest['source_sha256'].items()):raise SystemExit('Frozen source hash mismatch')
 byarm={a:[r for r in rows if r['arm']==a] for a in ARMS}
 out={'complete':complete,'complete_scope':'primary held-out rows only; check all_stages_complete before final report','expected_trials':len(expected),'completed_trials':len(rows),'unique_test_cases':len(test),'test_families':len({c['family'] for c in test}),'duplicate_keys':duplicates,'scope':'Actual local first-call inference; simulated/no tool execution. Error/malformed output counts as failure.','corpus_sha256':hashlib.sha256((HERE/'corpus.json').read_bytes()).hexdigest(),'input_hashes':{n:hashlib.sha256((HERE/n).read_bytes()).hexdigest() for n in ('heldout.jsonl','heldout.jsonl.manifest.json','answers.jsonl','laya_stability.jsonl','answer_audit_rubric.json') if (HERE/n).exists()},'arms':{},'paired_family_differences':{},'limitations':['Authored English-focused synthetic corpus, not population-distributed or entire Wisp capability catalog','Three repetitions and paraphrases are correlated','Common controlled argument-generation prompt differs from production agent loop','All primary arms ran while Laya was resident; memory is not independently isolated per architecture','No Ling cold model reload; cache-hit/miss timing is not cold model timing','Desktop hardware exclusivity was not confirmed']}
 for arm,rs in byarm.items():
  cases=defaultdict(list);families=defaultdict(list)
  for r in rs:cases[r['id']].append(r);families[r['family']].append(r)
  ok=lambda r:not r.get('error') and bool(r.get('grade',{}).get('strict'))
  srcgood=0;stage_times=defaultdict(list);prefix_classes=defaultdict(list);tokens=Counter();rawplanner_good=0
  for r in rs:
   expected_sources=set(gold[r['id']]['gold']['domains'])-{'none'}
   got={source_of(c['name']) for c in r.get('calls',[])}
   if got==expected_sources and 'error' not in r:srcgood+=1
   d=r.get('domain')
   predicted={'email','messages'} if d=='mail_and_messages' else ({d}-{'none'} if d else set())
   if d is not None and predicted==expected_sources:rawplanner_good+=1
   for stage in ('planner','generation'):
    x=r.get(stage,{})
    if 'seconds' in x:stage_times[stage].append(x['seconds'])
    usage=x.get('response',{}).get('usage',{})
    if usage:
     tokens['prompt']+=usage.get('prompt_tokens',0);tokens['completion']+=usage.get('completion_tokens',0)
     cached=usage.get('prompt_tokens_details',{}).get('cached_tokens',0)
     tokens['cached_prompt']+=cached
     prefix_classes[stage+('/with_cached_prefix' if cached else '/without_cached_prefix')].append(x['seconds'])
  out['arms'][arm]={'trials':len(rs),'strict_success':sum(ok(r) for r in rs),'strict_rate':sum(ok(r) for r in rs)/len(rs) if rs else None,'tool_exact':sum(bool(r.get('grade',{}).get('tool_exact')) for r in rs),'source_exact':srcgood,'raw_source_planner_correct':rawplanner_good if any('domain' in r for r in rs) else None,'errors':dict(Counter(r['error'].split(':',1)[0] for r in rs if 'error'in r)),'argument_failure_trials':sum(bool(r.get('grade',{}).get('argument_errors')) for r in rs),'schema_runtime_failure_trials':sum(bool(r.get('grade',{}).get('schema_errors')) for r in rs),'forbidden_call_trials':sum(bool(r.get('grade',{}).get('forbidden_calls')) for r in rs),'unrequested_effect_trials':sum(bool(r.get('grade',{}).get('unrequested_effects')) for r in rs),'duplicate_call_trials':sum(bool(r.get('grade',{}).get('duplicate_tools')) for r in rs),'unoffered_call_trials':sum(bool(r.get('grade',{}).get('unoffered_calls')) for r in rs),'unique_cases_all_three_pass':sum(len(v)==3 and all(ok(r) for r in v) for v in cases.values()),'unique_cases_majority_pass':sum(len(v)==3 and sum(ok(r) for r in v)>=2 for v in cases.values()),'cases_with_three_trials':sum(len(v)==3 for v in cases.values()),'unique_cases_vary_across_repetitions':sum(len(v)==3 and len({ok(r) for r in v})>1 for v in cases.values()),'latency_seconds':stats([r['seconds'] for r in rs]),'stage_latency_seconds':{k:stats(v) for k,v in stage_times.items()},'cache_conditioned_stage_seconds':{k:stats(v) for k,v in prefix_classes.items()},'model_tokens':dict(tokens),'family_accuracy':{k:sum(ok(r) for r in v)/len(v) for k,v in families.items()},'laya_fallbacks':sum(bool(r.get('fallback')) for r in rs) if arm=='laya_hybrid' else None,'memory_free_percent_observed':stats([r['memory_free_percent'] for r in rs if r.get('memory_free_percent') is not None])}
 # Complete corpus only: family-paired intervals with no pseudo-independent repeats.
 if complete:
  rng=random.Random(50310)
  for a in ARMS:
   if a=='current_rules':continue
   fa=out['arms'][a]['family_accuracy'];fb=out['arms']['current_rules']['family_accuracy'];families=sorted(fa.keys()&fb.keys())
   diffs=[fa[f]-fb[f] for f in families]
   boots=[statistics.mean(rng.choices(diffs,k=len(diffs))) for _ in range(4000)]
   out['paired_family_differences'][a+' minus current_rules']={'family_balanced_mean':statistics.mean(diffs),'bootstrap_95pct_interval':[q(boots,.025),q(boots,.975)],'families':len(families),'interpretation':'Descriptive paired family bootstrap for this corpus; not an independent user-population confidence claim'}
 # Raw Laya source calibration and option-order robustness; no threshold fitting.
 lr=read_rows(HERE/'laya_stability.jsonl');lo={}
 lkeys=[(r['id'],r['order']) for r in lr];lexpected={(c['id'],o) for c in corpus for o in range(3)}
 lcomplete=set(lkeys)==lexpected and len(lkeys)==len(set(lkeys))
 for order in range(3):
  rs=[r for r in lr if r['order']==order and r['split']=='test'];buckets=defaultdict(list)
  for r in rs:buckets[min(9,int(r['result']['answers']['route']['answer_confidence']*10))].append(r)
  high=[r for r in rs if r['result']['answers']['route']['answer_confidence']>=.8]
  lo[str(order)]={'n':len(rs),'correct':sum(r['source_correct'] for r in rs),'high_confidence_coverage':len(high)/len(rs) if rs else None,'high_confidence_errors':sum(not r['source_correct'] for r in high),'confidence_bins':{str(k):{'n':len(v),'mean_max_probability':statistics.mean(r['result']['answers']['route']['answer_confidence'] for r in v),'accuracy':statistics.mean(r['source_correct'] for r in v)} for k,v in sorted(buckets.items())},'latency_seconds':stats([r['seconds'] for r in rs]),'peak_rss_bytes_max':max((r['peak_rss_bytes'] for r in rs),default=None),'truncated':sum(r['result']['usage'].get('truncated',False) for r in rs),'collapsed_options':sum(bool(r['result']['usage'].get('options')) for r in rs)}
 lby=defaultdict(list)
 for r in lr:
  if r['split']=='test':lby[r['id']].append(r)
 out['laya_stability']={'complete':lcomplete,'expected_unique_keys':len(lexpected),'observed_unique_keys':len(set(lkeys)),'duplicate_keys':len(lkeys)-len(set(lkeys)),'orders':lo,'complete_test_case_triplets':sum(len(v)==3 and {r['order'] for r in v}=={0,1,2} for v in lby.values()),'option_order_disagreements':sum(len(v)==3 and {r['order'] for r in v}=={0,1,2} and len({r['result']['answers']['route']['choice'] for r in v})>1 for v in lby.values())}
 ans=read_rows(HERE/'answers.jsonl')
 selected={'reported_week_context-1','week_context-1','month-1','texts_overview-1','texts_context-1','texts_conversation-1','dual_overview-1','exclude_email-1','mail_unread-1','mail_lookup-1','notes_lookup-1'}
 aexpected={(cid,arm,state) for cid in selected for arm in ARMS for state in ('complete','partial','failed')}
 akeys=[(r['id'],r['arm'],r['state']) for r in ans];acomplete=set(akeys)==aexpected and len(akeys)==len(set(akeys))
 out['all_stages_complete']=complete and lcomplete and acomplete
 out['answers_status']={'complete':acomplete,'rows':len(ans),'expected':len(aexpected),'unique_keys':len(set(akeys)),'duplicate_keys':len(akeys)-len(set(akeys)),'errors':dict(Counter(r['error'].split(':',1)[0] for r in ans if 'error'in r)),'review_required':'Inspect blinded_answer_review.json; latency/repetition metrics alone do not establish pleasantness or factual quality.'}
 for arm in ARMS:
  rs=[r for r in ans if r['arm']==arm];out['answers_status'][arm]={'n':len(rs),'empty_answer':sum(not r.get('answer') for r in rs),'unfinished_call_cases':sum(bool(r.get('unfinished_calls')) for r in rs),'repeated_read_cases':sum(r['repeated_reads']>0 for r in rs),'continuation_latency_seconds':stats([r['seconds'] for r in rs])}
 (HERE/'analysis.json').write_text(json.dumps(out,indent=2)+'\n')
 # Full selected fixture audit in stable randomized order, with anonymous candidate IDs.
 shuffle=ans.copy();random.Random(904).shuffle(shuffle);blind=[];mapping=[]
 rubric=json.loads((HERE/'answer_audit_rubric.json').read_text()) if (HERE/'answer_audit_rubric.json').exists() else {}
 manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
 for i,r in enumerate(shuffle,1):
  key=f'A{i:03d}';c=gold[r['id']]
  family=c['family'];oracle='weekly_agenda' if family in ('reported_week_context','week_context') else 'monthly_agenda' if family=='month' else 'messages' if family.startswith('texts_') or family=='exclude_email' else 'notes' if family=='notes_lookup' else 'email'
  oracle_keys=['failed'] if r['state']=='failed' else (['email','messages'] if family=='dual_overview' else [oracle])
  expected_facts={k:rubric.get('fixture_oracles',{}).get(k) for k in oracle_keys}
  blind.append({'audit_id':key,'fixture_clock':manifest.get('anchor','2026-10-05T00:55:00-07:00'),'timezone':'America/Los_Angeles','expected_facts_and_limitations':expected_facts,'prompt':c['prompt'],'context':c['context'],'fixture_state':r['state'],'synthetic_tool_results':[t['simulated_results'] for t in r.get('trace',[]) if 'simulated_results' in t],'proposed_calls':r.get('initial_calls',[])+[{'name':tc.get('function',{}).get('name'),'arguments':tc.get('function',{}).get('arguments')} for t in r.get('trace',[]) for choice in t.get('response',{}).get('choices',[]) for tc in choice.get('message',{}).get('tool_calls',[])],'answer':r.get('answer',''),'repeated_reads':r['repeated_reads'],'unfinished':bool(r.get('unfinished_calls')),'error':r.get('error')})
  mapping.append({'audit_id':key,'case':r['id'],'arm':r['arm'],'state':r['state']})
 (HERE/'blinded_answer_review.json').write_text(json.dumps({'fixture_clock':manifest.get('anchor','2026-10-05T00:55:00-07:00'),'timezone':'America/Los_Angeles','rubric':{k:v for k,v in rubric.items() if k!='fixture_oracles'},'answers':blind},indent=2)+'\n');(HERE/'answer_review_key.json').write_text(json.dumps(mapping,indent=2)+'\n')
 print(json.dumps({'complete':complete,'trials':len(rows),'answers':len(ans),'laya_order_trials':len(lr),'output':str(HERE/'analysis.json')}))
if __name__=='__main__':main()
