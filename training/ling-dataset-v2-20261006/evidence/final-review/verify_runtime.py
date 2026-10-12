import json,importlib.util,sys,hashlib,shutil,subprocess
from pathlib import Path
ROOT=Path('/private/tmp/ling-dataset-v2-review-input-v2');OUT=Path('/private/tmp/ling-dataset-v2-review/v2')
# Advisory runtime copies: redirect only the root/import, preserving QA logic.
qa_source=(ROOT/'qa.py').read_text().replace('ROOT=Path(__file__).resolve().parent',"ROOT=Path('/private/tmp/ling-dataset-v2-review-input-v2')",1)
(OUT/'frozenqa.py').write_text(qa_source)
control_source=(ROOT/'validate.py').read_text().replace('import qa\n','import frozenqa as qa\n',1)
(OUT/'frozencontrols.py').write_text(control_source)
sys.path.insert(0,str(OUT))
import frozenqa, frozencontrols
qa=frozenqa.validate(ROOT);controls=frozencontrols.negative_controls(ROOT)
assert qa['status']=='PASS_STATIC_DATA_QA'
assert len(controls)==21 and sum(c.get('rejected',False) for c in controls)==19 and sum(c.get('accepted',False) for c in controls)==2
assert all(c['baseline_passed'] for c in controls if c.get('rejected'))
stored=json.loads((ROOT/'validation.json').read_text())
assert qa=={k:v for k,v in stored.items() if k!='negative_controls'}
assert controls==stored['negative_controls']
(OUT/'qa-and-controls.json').write_text(json.dumps(dict(qa=qa,controls=controls,logic='Snapshot QA/controls copied to advisory directory with only ROOT/import redirection; no snapshot writes'),sort_keys=True,indent=2)+'\n')
scratch=OUT/'packaging-sandbox';scratch.mkdir(exist_ok=True)
members=['train.jsonl','dev.jsonl','pilot-train.jsonl','train.provenance.jsonl','dev.provenance.jsonl','intent.schema.v1.json','router-system.txt','DATA_CARD.md','README.md','preflight.py','qa.py','validate.py','manifest.json','validation.json']
for name in members+['package.py']:shutil.copyfile(ROOT/name,scratch/name)
def package():return subprocess.run([sys.executable,'-B',str(scratch/'package.py')],capture_output=True,text=True)
baseline=package();assert baseline.returncode==0,baseline.stderr
package_receipt=json.loads(baseline.stdout)
mutations=[]
for name in ['train.jsonl','qa.py']:
 path=scratch/name;original=path.read_bytes();path.write_bytes(original+b'\n');attempt=package();assert attempt.returncode!=0 and 'Stale validation: '+name in attempt.stderr,attempt.stderr
 mutations.append(dict(name=name,rejected=True,reason='Stale validation: '+name))
 path.write_bytes(original)
receipt=dict(status='PASS_QA_CONTROLS_AND_PACKAGING_SMOKE',rows=4400,negative_controls_rejected=19,positive_controls_accepted=2,control_baselines_passed=True,stored_validation_matches=True,packaging_baseline=package_receipt,stale_hash_controls=mutations)
(OUT/'runtime-receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');print(json.dumps(receipt,sort_keys=True))
