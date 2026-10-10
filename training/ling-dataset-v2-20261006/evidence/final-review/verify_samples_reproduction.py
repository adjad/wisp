import json,hashlib,shutil,subprocess,sys,os
from pathlib import Path
R=Path('/private/tmp/ling-dataset-v2-review-input-v2');O=Path('/private/tmp/ling-dataset-v2-review/v2')
def jl(name):return [json.loads(x) for x in (R/name).read_text().splitlines()]
rd={x['id']:x for split in ['train','dev'] for x in jl(split+'.jsonl')};md={x['id']:x for split in ['train','dev'] for x in jl(split+'.provenance.jsonl')}
samples=jl('review-samples.jsonl');assert len(samples)==497
for s in samples:assert s['row']==rd[s['row']['id']] and s['provenance']==md[s['row']['id']]
assert {s['provenance']['family_id'] for s in samples}=={m['family_id'] for m in md.values()}
repaired=json.loads((O/'repair-diff.json').read_text());affected={(md[rid]['family_id'],md[rid]['template_id']) for rid in repaired['changed_ids']};sampled={(s['provenance']['family_id'],s['provenance']['template_id']) for s in samples}
sandbox=O/'generator-sandbox';sandbox.mkdir(exist_ok=True)
for name in ['generate.py','router-system.txt','intent.schema.v1.json','SPEC.md']:
 shutil.copyfile(R/name,sandbox/name)
for shard in ['arguments','context','grounded']:
 d=sandbox/'shards'/shard;d.mkdir(parents=True,exist_ok=True);shutil.copyfile(R/'shards'/shard/'generate.py',d/'generate.py')
temp=O/'reproduction-tmp';temp.mkdir(exist_ok=True);dest=O/'reproduced';env=dict(os.environ,TMPDIR=str(temp),PYTHONDONTWRITEBYTECODE='1')
run=subprocess.run([sys.executable,'-B',str(sandbox/'generate.py'),'--output-dir',str(dest)],env=env,capture_output=True,text=True);assert run.returncode==0,run.stderr
names=['train.jsonl','dev.jsonl','train.provenance.jsonl','dev.provenance.jsonl','pilot-train.jsonl','manifest.json']
assert all((dest/name).read_bytes()==(R/name).read_bytes() for name in names)
receipt=dict(status='PASS_FRESH_SAMPLES_AND_INDEPENDENT_REPRODUCTION',samples=497,families=186,repaired_surfaces_in_supplied_samples=len(affected&sampled),repaired_surfaces_reviewed_from_fullrow_diff=17,extra_surface_selection_needed=sorted(affected-sampled),byte_identical_files={n:hashlib.sha256((dest/n).read_bytes()).hexdigest() for n in names},all_writes_under_advisory_output=True)
(O/'reproduction-receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');print(json.dumps(receipt,sort_keys=True))
