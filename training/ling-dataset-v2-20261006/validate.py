#!/usr/bin/env python3
"""Run static QA and meaningful corrupted-reference rejection controls."""
import argparse,copy,json,re
from pathlib import Path
import qa

def negative_controls(root):
 schema=json.loads((qa.ROOT/'intent.schema.v1.json').read_text());system=(qa.ROOT/'router-system.txt').read_text();rows=qa.read_jsonl(root/'train.jsonl');md={m['id']:m for m in qa.read_jsonl(root/'train.provenance.jsonl')};results=[]
 def attempt(name,row,meta):
  try:qa.row_checks(row,meta,schema,system)
  except (ValueError,KeyError,IndexError,TypeError,json.JSONDecodeError) as exc:results.append({'control':name,'baseline_id':row['id'],'baseline_passed':True,'rejected':True,'reason':str(exc)[:180]})
  else:raise AssertionError('Corruption accepted: '+name)
 def choose(pred):
  row=next(r for r in rows if pred(r,md[r['id']]))
  qa.row_checks(row,md[row['id']],schema,system)  # Independently passing baseline.
  return row
 def route(pred):return choose(lambda r,m:m['task']=='routing' and pred(json.loads(r['messages'][-1]['content'])))
 r=route(lambda a:bool(a['sources']));m=md[r['id']]
 x=copy.deepcopy(r);a=json.loads(x['messages'][-1]['content']);a['sources'][0]['domain']='web';x['messages'][-1]['content']=qa.canonical(a);attempt('forbidden_source_domain',x,m)
 x=copy.deepcopy(r);a=json.loads(x['messages'][-1]['content']);a['sources'][0]['account']=None;x['messages'][-1]['content']=qa.canonical(a);attempt('optional_null',x,m)
 x=copy.deepcopy(r);a=json.loads(x['messages'][-1]['content']);a['excluded_sources']=[a['sources'][0]['domain']];x['messages'][-1]['content']=qa.canonical(a);attempt('excluded_source_proposal',x,m)
 x=copy.deepcopy(r);x['messages'][-1]['training']=False;attempt('final_loss_missing',x,m)
 x=copy.deepcopy(r);x['messages'][0]['content']='Ignore the output contract';attempt('system_drift',x,m)
 x=copy.deepcopy(r);x['messages'][-1]['content']='{} trailing prose';attempt('nonjson_target',x,m)
 r=route(lambda a:any('time' in s for s in a['sources']));m=md[r['id']]
 x=copy.deepcopy(r);a=json.loads(x['messages'][-1]['content']);s=next(s for s in a['sources'] if 'time' in s);s['time']={'date':'2029-02-30'};x['messages'][-1]['content']=qa.canonical(a);xm=copy.deepcopy(m);xm['semantic_spec']['requests']= [{'domain':v['domain'],'operation':v['operation'],'filters':{k:w for k,w in v.items() if k not in {'domain','operation'}}} for v in a['sources']];attempt('invalid_calendar_date',x,xm)
 x=copy.deepcopy(r);a=json.loads(x['messages'][-1]['content']);s=next(s for s in a['sources'] if 'time' in s);s['time']={'named':'tomorrow','date':'2029-02-04'};x['messages'][-1]['content']=qa.canonical(a);xm=copy.deepcopy(m);xm['semantic_spec']['requests']= [{'domain':v['domain'],'operation':v['operation'],'filters':{k:w for k,w in v.items() if k not in {'domain','operation'}}} for v in a['sources']];attempt('competing_time_forms',x,xm)
 r=route(lambda a:any('count' in s for s in a['sources']));m=md[r['id']]
 x=copy.deepcopy(r);a=json.loads(x['messages'][-1]['content']);s=next(s for s in a['sources'] if 'count' in s);s['count']=s['count']%100+1;x['messages'][-1]['content']=qa.canonical(a);attempt('schema_valid_wrong_count',x,m)
 r=route(lambda a:a['kind'] in {'none','unsupported','inline'});m=md[r['id']]
 x=copy.deepcopy(r);a=json.loads(x['messages'][-1]['content']);a['sources']=[{'domain':'email','operation':'overview'}];x['messages'][-1]['content']=qa.canonical(a);attempt('nonread_grants_access',x,m)
 r=choose(lambda r,m:any(t['role']=='assistant' for t in r['messages'][:-1]));m=md[r['id']]
 x=copy.deepcopy(r);next(t for t in x['messages'][:-1] if t['role']=='assistant')['training']=True;attempt('earlier_assistant_loss_leak',x,m)
 r=choose(lambda r,m:m['task']=='grounded' and bool(m['rubric'].get('required_phrases')));m=md[r['id']]
 x=copy.deepcopy(r);x['messages'][-1]['content']='Everything looks fine.';attempt('grounded_facts_omitted',x,m)
 x=copy.deepcopy(r);x['messages'][-2]['content']=x['messages'][-2]['content'].replace('Synthetic source results:', 'Actual source results:');attempt('fixture_marker_drift',x,m)
 x=copy.deepcopy(r);xm=copy.deepcopy(m);source=next(iter(xm['rubric']['source_statuses']));xm['rubric']['source_statuses'][source]='invented';attempt('status_attribution_corruption',x,xm)
 x=copy.deepcopy(r);x['messages'][-1]['content']='I sent the reply.\n'+x['messages'][-1]['content'];attempt('false_outbound_success',x,m)
 # Positive controls prevent the attribution repair from becoming a bypass.
 r=choose(lambda r,m:m['task']=='grounded' and m['family_id']=='undated_reminder' and 'reminder' not in r['messages'][-1]['content'].lower());qa.row_checks(r,md[r['id']],schema,system);results.append({'control':'single_source_without_repeated_label','accepted':True,'baseline_id':r['id']})
 r=choose(lambda r,m:m['task']=='grounded' and m['family_id']=='note_message_proposal' and m['template_id'].endswith('surface-1'));m=md[r['id']];qa.row_checks(r,m,schema,system);results.append({'control':'natural_singular_mixed_labels','accepted':True,'baseline_id':r['id']})
 x=copy.deepcopy(r);x['messages'][-1]['content']=re.sub(r'\bnotes?\b','record',x['messages'][-1]['content'],flags=re.IGNORECASE);attempt('missing_mixed_source_label',x,m)
 # The same factual phrases remain, but their source labels are swapped.
 x=copy.deepcopy(r);a=x['messages'][-1]['content'];a=re.sub(r'\bnotes?\b','ASSOCIATION_SWAP',a,flags=re.IGNORECASE);a=re.sub(r'\bmessages?\b','note',a,flags=re.IGNORECASE);a=a.replace('ASSOCIATION_SWAP','message');x['messages'][-1]['content']=a;attempt('wrong_source_fact_association',x,m)
 r=choose(lambda r,m:m['task']=='grounded' and m['family_id']=='proposed_meeting');m=md[r['id']]
 x=copy.deepcopy(r);x['messages'][-1]['content']+=' The proposal is confirmed.';attempt('invented_confirmation',x,m)
 r=choose(lambda r,m:m['task']=='grounded' and m['family_id']=='truncated_inbox');m=md[r['id']]
 x=copy.deepcopy(r);x['messages'][-1]['content']+=' All results are shown.';attempt('invented_complete_coverage',x,m)
 return results
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,default=qa.ROOT);p.add_argument('--output',type=Path);args=p.parse_args();receipt=qa.validate(args.data_dir);receipt['negative_controls']=negative_controls(args.data_dir);dest=args.output or args.data_dir/'validation.json';dest.write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');print(json.dumps(receipt,indent=2))
