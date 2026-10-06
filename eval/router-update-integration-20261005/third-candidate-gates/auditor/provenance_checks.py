"""Opaque corpus/archive hashing and candidate/assignment metadata verification."""
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-27490ae-gates-20261005/auditor')
HEAD='27490ae5055502423f187e84ca52d0064b794396'
OLD='40ee88a409eaec943ae4474130bd87263ea794e1'
BASE='4994caa15533c0cf84c07208c9097e4197f2815b'
def git(*args):return subprocess.check_output(['/usr/bin/git',*args],cwd=ROOT)
def digest(data):return hashlib.sha256(data).hexdigest()
assert git('rev-parse','HEAD').decode().strip()==HEAD and not git('status','--porcelain')
files=git('diff','--name-only',BASE,HEAD).decode().splitlines()
delta=git('diff','--name-only',OLD,HEAD).decode().splitlines()
hashes={name:digest((ROOT/name).read_bytes()) for name in files}
opaque=[]
for name in ('test.jsonl','dev.jsonl','seal.json','author_corpus.py'):
    path='eval/router-update-20261005/'+name
    current=(ROOT/path).read_bytes()
    assert current==git('show',OLD+':'+path)
    opaque.append({'file':path,'sha256':digest(current),'unchanged_since_previous_candidate':True,
        'handling':'byte hashes/comparison only; payload not parsed'})
seal=json.loads((ROOT/'eval/router-update-20261005/seal.json').read_text())
for split in ('dev','test'):
    assert hashes[f'eval/router-update-20261005/{split}.jsonl']==seal['splits'][split]['sha256']
replays=[]
for folder in ('eval/router-update-20261005/baseline-scripted',
    *(f'eval/router-update-integration-20261005/{name}' for name in (
        'development-before-compiler-repair','development-after-compiler-repair','development-after-audit-repairs',
        'development-clause-repair-regression','development-after-alias-repair'))):
    path=ROOT/folder
    declared=json.loads((path/'checksums.json').read_text())
    compressed=(path/'raw.jsonl.gz').read_bytes();raw=gzip.decompress(compressed)
    computed={'raw_sha256':digest(raw),'gzip_sha256':digest(compressed),
        'manifest_sha256':digest((path/'manifest.json').read_bytes())}
    assert computed['raw_sha256']==declared.get('raw_sha256',declared.get('raw_jsonl_sha256'))
    assert computed['gzip_sha256']==declared.get('gzip_sha256',declared.get('raw_gzip_sha256',declared.get('raw_jsonl_gzip_sha256')))
    assert 'manifest_sha256' not in declared or computed['manifest_sha256']==declared['manifest_sha256']
    assert 'raw_bytes' not in declared or len(raw)==declared['raw_bytes']
    assert 'raw_gzip_bytes' not in declared or len(compressed)==declared['raw_gzip_bytes']
    manifest=json.loads((path/'manifest.json').read_text())
    replays.append({'folder':folder,'computed':computed,'declared_match':True,
        'manifest_metadata':{key:manifest.get(key) for key in ('mode','not_model_measurement','split','cases','head','metrics')},
        'handling':'raw payload hashed only; metadata inspected'})
archives=[]
for folder in ('first-candidate-gates','second-candidate-gates'):
    path=ROOT/'eval/router-update-integration-20261005'/folder
    bundle=json.loads((path/'bundle-sha256.json').read_text())
    for name,record in bundle.items():
        data=(path/name).read_bytes()
        wanted=record if isinstance(record,str) else record['stored_sha256']
        assert digest(data)==wanted
        if isinstance(record,dict) and 'original_sha256' in record:
            original=gzip.decompress(data) if name.endswith('.gz') else data
            assert digest(original)==record['original_sha256']
    archives.append({'folder':folder,'entries':len(bundle),'stored_and_declared_original_hashes_match':True,
        'archived_scripts_executed':False,'payloads_deserialized':False})
pins={'HANDOFF.md':'48ec85b646477ceaa2cae5376808535fc3ee572160f4301d50943422898404cb',
    'progress.json':'1a5d98f19420496694f7393dc5e16b8af76c7fafd0a2f2faf3ddae0d41929be1',
    'DEVELOPMENT_RESULTS.md':'3f1a96f5335c91988ad7269738e4f9e0b89177c63f4fa8da334d85eb78ccec95'}
assert all(digest((ROOT/'eval/router-update-integration-20261005'/name).read_bytes())==wanted for name,wanted in pins.items())
registry_path=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_1_3_IMPLEMENTATION_OWNERSHIP_2026-10-05.json')
text=registry_path.read_text();registry,end=json.JSONDecoder().raw_decode(text)
assignment=registry['exactCandidateGateAssignments20261005']
assert assignment['exactSHA']==HEAD and assignment['baseSHA']==BASE
gatefile=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_27490AE_GATE_ASSIGNMENTS_2026-10-05.md')
assert digest(gatefile.read_bytes())==assignment['assignmentDocumentSHA256']
progress=json.loads((ROOT/'eval/router-update-integration-20261005/progress.json').read_text())
log=Path('/private/tmp/wisp-router-update-27490ae-regression.log');snapshot=log.read_bytes()
data={'exact_sha':HEAD,'base_sha':BASE,'previous_audit_sha':OLD,'head_verified':True,'tree_clean':True,
    'observed_at_utc':datetime.now(timezone.utc).isoformat(),'complete_changed_files':files,
    'changed_since_previous_candidate':delta,'changed_file_sha256':hashes,'opaque_corpus_checks':opaque,
    'replays':replays,'archives':archives,'handoff_pins_verified':pins,
    'registry_sha256':digest(registry_path.read_bytes()),'registry_trailing_whitespace_only':not text[end:].strip(),
    'exact_assignment':assignment,'assignment_sha256':digest(gatefile.read_bytes()),
    'progress_metadata':progress,
    'regression_snapshot':{'bytes':len(snapshot),'sha256':digest(snapshot),'final_summary_present':b'Regression gate:' in snapshot},
    'heldout_payload_opened_or_deserialized':False}
(OUT/'provenance.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps({'head':HEAD,'clean':True,'changed_files':len(files),'delta_files':len(delta),
    'replay_bundles_verified':len(replays),'archive_entries_verified':sum(a['entries'] for a in archives),
    'regression_final_summary_present':data['regression_snapshot']['final_summary_present']}))
