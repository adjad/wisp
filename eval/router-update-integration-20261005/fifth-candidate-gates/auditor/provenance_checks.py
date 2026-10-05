"""Opaque-byte provenance verification for exact6df5200, no payload parsing."""
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-6df5200-gates-20261005/auditor')
HEAD='6df5200d0945ee3b050bcde4115a338e392017b1'
OLD='273d2748d01ed978b4e4fb56c8ee503b18143bc4'
BASE='4994caa15533c0cf84c07208c9097e4197f2815b'
def git(*args):return subprocess.check_output(['/usr/bin/git',*args],cwd=ROOT)
def sha(data):return hashlib.sha256(data).hexdigest()
assert git('rev-parse','HEAD').decode().strip()==HEAD and not git('status','--porcelain')
changed=git('diff','--name-only',BASE,HEAD).decode().splitlines();delta=git('diff','--name-only',OLD,HEAD).decode().splitlines()
hashes={name:sha((ROOT/name).read_bytes()) for name in changed}
opaque=[]
for name in ('dev.jsonl','test.jsonl','seal.json','author_corpus.py'):
    path='eval/router-update-20261005/'+name;data=(ROOT/path).read_bytes()
    assert data==git('show',OLD+':'+path)
    opaque.append({'file':path,'sha256':sha(data),'unchanged_since_previous_candidate':True,'handling':'opaque bytes only'})
seal=json.loads((ROOT/'eval/router-update-20261005/seal.json').read_text())
for split in ('dev','test'):assert hashes['eval/router-update-20261005/'+split+'.jsonl']==seal['splits'][split]['sha256']
replays=[]
folders=['eval/router-update-20261005/baseline-scripted']+[
    str(p.relative_to(ROOT)) for p in sorted((ROOT/'eval/router-update-integration-20261005').glob('development-*')) if p.is_dir()]
for folder in folders:
    p=ROOT/folder;checks=json.loads((p/'checksums.json').read_text())
    compressed=(p/'raw.jsonl.gz').read_bytes();raw=gzip.decompress(compressed)
    actual={'raw_sha256':sha(raw),'gzip_sha256':sha(compressed),'manifest_sha256':sha((p/'manifest.json').read_bytes())}
    if 'raw.jsonl.gz' in checks:
        assert actual['raw_sha256']==checks['raw.jsonl.gz']['original_sha256'] and actual['gzip_sha256']==checks['raw.jsonl.gz']['stored_sha256']
        assert actual['manifest_sha256']==checks['manifest.json']['stored_sha256']==checks['manifest.json']['original_sha256']
    else:
        assert actual['raw_sha256']==checks.get('raw_sha256',checks.get('raw_jsonl_sha256'))
        assert actual['gzip_sha256']==checks.get('gzip_sha256',checks.get('raw_gzip_sha256',checks.get('raw_jsonl_gzip_sha256')))
        assert 'manifest_sha256' not in checks or actual['manifest_sha256']==checks['manifest_sha256']
    assert 'raw_bytes' not in checks or len(raw)==checks['raw_bytes']
    assert 'raw_gzip_bytes' not in checks or len(compressed)==checks['raw_gzip_bytes']
    manifest=json.loads((p/'manifest.json').read_text())
    replays.append({'folder':folder,'computed':actual,'declared_match':True,
        'metadata':{key:manifest.get(key) for key in ('mode','not_model_measurement','candidate','split','cases','head','metrics')},'raw_rows_parsed':False})
archives=[]
for folder in ('first-candidate-gates','second-candidate-gates','third-candidate-gates','fourth-candidate-gates'):
    p=ROOT/'eval/router-update-integration-20261005'/folder;bundle=json.loads((p/'bundle-sha256.json').read_text())
    for name,record in bundle.items():
        data=(p/name).read_bytes();wanted=record if isinstance(record,str) else record['stored_sha256']
        assert sha(data)==wanted
        if isinstance(record,dict) and 'original_sha256' in record:
            restored=gzip.decompress(data) if name.endswith('.gz') else data
            assert sha(restored)==record['original_sha256']
    archives.append({'folder':folder,'entries':len(bundle),'stored_and_original_hashes_match':True,'archived_scripts_executed':False,'raw_payloads_parsed':False})
rp=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_1_3_IMPLEMENTATION_OWNERSHIP_2026-10-05.json')
text=rp.read_text();registry,end=json.JSONDecoder().raw_decode(text);assignment=registry['exactCandidateGateAssignments20261005']
assert assignment['exactSHA']==HEAD and assignment['baseSHA']==BASE and set(assignment['changedFiles'])==set(changed)
ap=Path(assignment['assignmentDocument']);assert sha(ap.read_bytes())==assignment['assignmentDocumentSHA256']
for name,wanted in assignment['handoffFingerprints'].items():assert sha((ROOT/'eval/router-update-integration-20261005'/name).read_bytes())==wanted
progress=json.loads((ROOT/'eval/router-update-integration-20261005/progress.json').read_text())
log=Path('/private/tmp/wisp-router-update-6df5200-regression.log');snapshot=log.read_bytes()
data={'exact_sha':HEAD,'base_sha':BASE,'previous_audit_sha':OLD,'head_verified':True,'tree_clean':True,
    'observed_at_utc':datetime.now(timezone.utc).isoformat(),'complete_changed_files':changed,'changed_since_previous_candidate':delta,
    'changed_file_sha256':hashes,'opaque_corpus_checks':opaque,'replays':replays,'archives':archives,'exact_assignment':assignment,
    'registry_sha256':sha(rp.read_bytes()),'registry_trailing_whitespace_only':not text[end:].strip(),
    'assignment_sha256':sha(ap.read_bytes()),'handoff_pins_verified':assignment['handoffFingerprints'],'progress_metadata':progress,
    'regression_snapshot':{'bytes':len(snapshot),'sha256':sha(snapshot),'final_summary_present':b'Regression gate:' in snapshot},
    'heldout_payload_opened_or_parsed':False}
(OUT/'provenance.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps({'head':HEAD,'clean':True,'changed':len(changed),'delta':len(delta),'replay_bundles':len(replays),
    'archive_entries':sum(x['entries'] for x in archives),'regression_final_summary':data['regression_snapshot']['final_summary_present']}))
