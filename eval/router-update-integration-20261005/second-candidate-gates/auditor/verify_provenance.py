"""Read-only byte/metadata checks; heldout and archived raw rows stay opaque."""
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-40ee88a-gates-20261005/auditor')
HEAD='40ee88a409eaec943ae4474130bd87263ea794e1'
BASE='4994caa15533c0cf84c07208c9097e4197f2815b'
OLD='3f86187ccfca1a20423506c883acecc6fec59ae1'
def git(*args):
    return subprocess.check_output(['/usr/bin/git',*args],cwd=ROOT)
def digest(data):
    return hashlib.sha256(data).hexdigest()
assert git('rev-parse','HEAD').decode().strip()==HEAD
assert git('status','--porcelain')==b''
files=git('diff','--name-only',BASE,HEAD).decode().splitlines()
changed_since_prior=git('diff','--name-only',OLD,HEAD).decode().splitlines()
hashes={name:digest((ROOT/name).read_bytes()) for name in files}
seal=json.loads((ROOT/'eval/router-update-20261005/seal.json').read_text())
opaque=[]
for name in ('test.jsonl','dev.jsonl','seal.json','author_corpus.py'):
    relative='eval/router-update-20261005/'+name
    current=(ROOT/relative).read_bytes()
    same_as_prior=current==git('show',OLD+':'+relative)
    assert same_as_prior
    opaque.append({'file':relative,'sha256':digest(current),'unchanged_since_prior':same_as_prior,
        'handling':'opaque byte comparison/hash; no payload parse'})
for split in ('dev','test'):
    assert hashes[f'eval/router-update-20261005/{split}.jsonl']==seal['splits'][split]['sha256']
replays=[]
for folder in ('eval/router-update-20261005/baseline-scripted',
    'eval/router-update-integration-20261005/development-before-compiler-repair',
    'eval/router-update-integration-20261005/development-after-compiler-repair',
    'eval/router-update-integration-20261005/development-after-audit-repairs'):
    path=ROOT/folder
    declared=json.loads((path/'checksums.json').read_text())
    compressed=(path/'raw.jsonl.gz').read_bytes()
    raw=gzip.decompress(compressed)
    computed={'raw_sha256':digest(raw),'gzip_sha256':digest(compressed),'manifest_sha256':digest((path/'manifest.json').read_bytes())}
    matches=(computed['raw_sha256']==declared['raw_sha256']
        and computed['gzip_sha256']==declared.get('gzip_sha256',declared.get('raw_gzip_sha256'))
        and ('manifest_sha256' not in declared or computed['manifest_sha256']==declared['manifest_sha256'])
        and ('raw_bytes' not in declared or len(raw)==declared['raw_bytes'])
        and ('raw_gzip_bytes' not in declared or len(compressed)==declared['raw_gzip_bytes']))
    assert matches
    metadata=json.loads((path/'manifest.json').read_text())
    replays.append({'folder':folder,'computed':computed,'declared_match':matches,
        'manifest_metadata':{key:metadata.get(key) for key in ('mode','not_model_measurement','split','cases','head','metrics')},
        'handling':'raw payload bytes hashed only; manifest metadata read'})
archive=ROOT/'eval/router-update-integration-20261005/first-candidate-gates'
bundle=json.loads((archive/'bundle-sha256.json').read_text())
assert all(digest((archive/name).read_bytes())==wanted for name,wanted in bundle.items())
pins={'HANDOFF.md':'971e07f9f2dd7f7c21bf10ca7eb0a807588e023a02cbb5bfad1a086f96643746',
    'progress.json':'4f56d27a32723144a18d21b9706febfb9ee9ce9c91d2f082c3bb0b2617153395',
    'DEVELOPMENT_RESULTS.md':'359a9aa0f9da3247d1e50a945930163a99bb0287cf101ec3d8350b999f96092c'}
assert all(digest((ROOT/'eval/router-update-integration-20261005'/name).read_bytes())==wanted for name,wanted in pins.items())
registry_path=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_1_3_IMPLEMENTATION_OWNERSHIP_2026-10-05.json')
registry_text=registry_path.read_text()
registry,end=json.JSONDecoder().raw_decode(registry_text)
assignment=registry['exactCandidateGateAssignments20261005']
assert HEAD in json.dumps(assignment)
gatefile=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_40EE88A_GATE_ASSIGNMENTS_2026-10-05.md')
log=Path('/private/tmp/wisp-router-update-40ee88a-regression.log')
regression=log.read_bytes()
data={'exact_sha':HEAD,'base_sha':BASE,'previous_audit_sha':OLD,'checked_at_utc':datetime.now(timezone.utc).isoformat(),
    'head_verified':True,'tree_clean':True,'complete_changed_files':files,'changed_since_previous_audit':changed_since_prior,
    'changed_file_sha256':hashes,'opaque_corpus_checks':opaque,'replays':replays,
    'archive_bundle_verified_files':len(bundle),'handoff_pins_verified':pins,
    'registry_sha256':digest(registry_path.read_bytes()),'registry_trailing_whitespace_only':not registry_text[end:].strip(),
    'exact_assignment':assignment,'gate_scope_sha256':digest(gatefile.read_bytes()),
    'regression_snapshot':{'sha256':digest(regression),'bytes':len(regression),
        'final_summary_present':b'Regression gate:' in regression},
    'heldout_payload_opened_or_deserialized':False,'old_archived_scripts_executed':False}
(OUT/'provenance.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps({'head':HEAD,'clean':True,'changed_files':len(files),'delta_files':len(changed_since_prior),
    'opaque_corpus_unchanged':len(opaque),'replay_bundles_verified':len(replays),
    'archived_evidence_files_verified':len(bundle),'regression_summary_present':data['regression_snapshot']['final_summary_present']}))
