import json,hashlib,collections,difflib,re
from pathlib import Path
OLD=Path('/private/tmp/ling-dataset-v2-review-input-v1');NEW=Path('/private/tmp/ling-dataset-v2-review-input-v2');OUT=Path('/private/tmp/ling-dataset-v2-review/v2')
def rows(root,name):return [json.loads(x) for x in (root/name).read_text().splitlines()]
def verify(root,want):
 m=json.loads((root/'snapshot-manifest.json').read_text());assert hashlib.sha256((root/'snapshot-manifest.json').read_bytes()).hexdigest()==want
 for name,v in m['files'].items():assert hashlib.sha256((root/name).read_bytes()).hexdigest()==v['sha256'] and (root/name).stat().st_size==v['bytes']
 return m
verify(OLD,'3f7c3f7b84cf8c23fcaeb38322e72f41d460f76924ddb665258f43d1ec115ef0');verify(NEW,'4fe27d9ec60788a234f857437af244c3c6ed2c935a5080049a126a7e39c2e391')
expected_groups=json.loads((OUT.parent/'mixed-source-findings.json').read_text());expected={rid for g in expected_groups for rid in g['ids']}
assert len(expected)==98
changed=[];surface_reps={};pilot_changed=[]
for split in ['train','dev']:
 old=rows(OLD,split+'.jsonl');new=rows(NEW,split+'.jsonl');assert len(old)==len(new)
 assert (OLD/(split+'.provenance.jsonl')).read_bytes()==(NEW/(split+'.provenance.jsonl')).read_bytes()
 md={m['id']:m for m in rows(NEW,split+'.provenance.jsonl')}
 for a,b in zip(old,new):
  assert a['id']==b['id'];assert a['messages'][:-1]==b['messages'][:-1]
  assert {k:v for k,v in a['messages'][-1].items() if k!='content'}=={k:v for k,v in b['messages'][-1].items() if k!='content'}
  if a!=b:
   m=md[b['id']];assert m['task']=='grounded' and b['id'] in expected
   changed.append(b['id']);key=(m['family_id'],m['template_id'])
   rep=dict(id=b['id'],family=key[0],template=key[1],fixture=m['fixture'],before=a['messages'][-1]['content'],after=b['messages'][-1]['content'])
   if key not in surface_reps or b['id']<surface_reps[key]['id']:surface_reps[key]=rep
 assert all(a==b for a,b in zip(rows(OLD,'shards/arguments/'+split+'.jsonl'),rows(NEW,'shards/arguments/'+split+'.jsonl')))
 assert all(a==b for a,b in zip(rows(OLD,'shards/context/'+split+'.jsonl'),rows(NEW,'shards/context/'+split+'.jsonl')))
 for shard in ['arguments','context','grounded']:
  assert rows(NEW,'shards/'+shard+'/'+split+'.jsonl')==sorted([x for x in new if x['id'].startswith('v2-'+shard)],key=lambda x:x['id'])
assert set(changed)==expected and len(changed)==98 and len(surface_reps)==17
train={x['id']:x for x in rows(NEW,'train.jsonl')};oldpilot=rows(OLD,'pilot-train.jsonl');newpilot=rows(NEW,'pilot-train.jsonl');assert len(oldpilot)==len(newpilot)==2000
for a,b in zip(oldpilot,newpilot):
 assert a['id']==b['id'] and b==train[b['id']]
 if a!=b:assert b['id'] in expected;pilot_changed.append(b['id'])
samples=rows(NEW,'review-samples.jsonl');assert len(samples)==497
receipt=dict(status='PASS_EXACT_INDEPENDENT_REPAIR_COMPARISON',changed_final_answers=len(changed),train_changed=sum('-train-' in i for i in changed),dev_changed=sum('-dev-' in i for i in changed),surfaces=len(surface_reps),changed_ids=sorted(changed),pilot_changed_ids=sorted(pilot_changed),samples=497,prompts_system_prior_messages_fixtures_provenance_routing_unaffected=True)
(OUT/'repair-diff.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
(OUT/'repaired-surfaces.jsonl').write_text(''.join(json.dumps(v,ensure_ascii=False,sort_keys=True)+'\n' for k,v in sorted(surface_reps.items())))
diff=''.join(difflib.unified_diff((OLD/'shards/grounded/generate.py').read_text().splitlines(keepends=True),(NEW/'shards/grounded/generate.py').read_text().splitlines(keepends=True),fromfile='snapshot1-grounded-generator',tofile='snapshot2-grounded-generator'))
(OUT/'grounded-generator.diff').write_text(diff)
print(json.dumps({k:v for k,v in receipt.items() if not k.endswith('_ids')},sort_keys=True))
for k,v in sorted(surface_reps.items()):print(v['id'],k,'\nBEFORE:',v['before'],'\nAFTER:',v['after'])
