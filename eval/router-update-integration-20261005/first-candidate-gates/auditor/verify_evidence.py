"""Opaque hash verification only: never deserialize corpus or raw replay rows."""
import gzip
import hashlib
import json
from pathlib import Path
ROOT = Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT = Path('/private/tmp/wisp-router-3f86187-gates-20261005/auditor')
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
seal = json.loads((ROOT/'eval/router-update-20261005/seal.json').read_text())
corpus = []
for split in ('dev','test'):
    path = ROOT/f'eval/router-update-20261005/{split}.jsonl'
    actual = digest(path)
    corpus.append({'split':split, 'sha256':actual, 'matches_seal':actual == seal['splits'][split]['sha256'],
        'handling':'opaque bytes hashed; no prompts or annotations deserialized'})
replays = []
for folder in ('eval/router-update-20261005/baseline-scripted',
    'eval/router-update-integration-20261005/development-before-compiler-repair',
    'eval/router-update-integration-20261005/development-after-compiler-repair'):
    path = ROOT/folder
    declared = json.loads((path/'checksums.json').read_text())
    checks = {'gzip_sha256': digest(path/'raw.jsonl.gz'), 'manifest_sha256':digest(path/'manifest.json'),
        'raw_sha256':hashlib.sha256(gzip.decompress((path/'raw.jsonl.gz').read_bytes())).hexdigest()}
    declared_matches = (checks['raw_sha256'] == declared['raw_sha256']
        and checks['gzip_sha256'] == declared.get('gzip_sha256', declared.get('raw_gzip_sha256'))
        and ('manifest_sha256' not in declared or checks['manifest_sha256'] == declared['manifest_sha256'])
        and ('raw_bytes' not in declared or len(gzip.decompress((path/'raw.jsonl.gz').read_bytes())) == declared['raw_bytes'])
        and ('raw_gzip_bytes' not in declared or (path/'raw.jsonl.gz').stat().st_size == declared['raw_gzip_bytes']))
    replays.append({'path':folder, 'sha256':checks, 'declared':declared,
        'matches_declared':declared_matches,
        'handling':'raw bytes decompressed and hashed only; no replay rows deserialized'})
files = ['eval/router-update-integration-20261005/HANDOFF.md',
    'eval/router-update-integration-20261005/progress.json',
    'eval/router-update-integration-20261005/DEVELOPMENT_RESULTS.md',
    'eval/router-update-20261005/author_corpus.py', 'scripts/eval_router_update.py',
    'service/router/intent/validation.py', 'service/router/intent/compiler.py',
    'service/router/router.py', 'service/workflows/reads.py']
data = {'exact_sha':'3f86187ccfca1a20423506c883acecc6fec59ae1', 'corpus':corpus, 'replays':replays,
    'files':{name:digest(ROOT/name) for name in files},
    'regression_log_sha256':digest(Path('/private/tmp/wisp-router-update-3f86187-regression.log'))}
(OUT/'provenance.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps(data,indent=2))
assert all(item['matches_seal'] for item in corpus)
assert all(item['matches_declared'] for item in replays)
