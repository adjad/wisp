import json,hashlib,gzip,subprocess
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-8dfd5c9-gates-20261005/auditor');HEAD='8dfd5c9c19d8e31063a1f0d65c87d3d0d1938736';BASE='4994caa15533c0cf84c07208c9097e4197f2815b';PREVIOUS='98916cf5d17fee49c94045e5a0e3a713d47dce39'
def sha(b):return hashlib.sha256(b).hexdigest()
def git(*a):return subprocess.check_output(['git',*a],cwd=ROOT)
checks=[]
def check(name,ok,detail=None):checks.append(dict(name=name,ok=bool(ok),detail=detail))
check('exact-head',git('rev-parse','HEAD').decode().strip()==HEAD);check('clean',git('status','--porcelain')==b'');check('base-ancestor',subprocess.run(['git','merge-base','--is-ancestor',BASE,HEAD],cwd=ROOT).returncode==0)
changed=git('diff','--name-only',BASE,HEAD).decode().splitlines();delta=git('diff','--name-only',PREVIOUS,HEAD).decode().splitlines();fingerprints={p:sha((ROOT/p).read_bytes()) for p in changed if (ROOT/p).is_file()}
check('full-scope-count',len(changed)==298,len(changed));check('delta-count',len(delta)==76,len(delta))
source=[p for p in delta if p.startswith(('service/','tests/','scripts/','.github/'))];check('five-authorized-paths',set(source)=={'service/router/intent/validation.py','service/router/web_request.py','service/workflows/compiler.py','tests/test_router_intent_core.py','tests/test_router_intent_main.py'},source)
registry_path=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_1_3_IMPLEMENTATION_OWNERSHIP_2026-10-05.json');registry_bytes=registry_path.read_bytes();registry,_=json.JSONDecoder().raw_decode(registry_bytes.decode());a=registry['exactCandidateGateAssignments20261005'];check('registry-head',a['exactSHA']==HEAD)
scope=Path(a['assignmentDocument']);check('assignment-pin',sha(scope.read_bytes())==a['assignmentDocumentSHA256'])
for name,digest in a['handoffFingerprints'].items():check('handoff-'+name,sha((ROOT/'eval/router-update-integration-20261005'/name).read_bytes())==digest)
for p,digest in a['coreCompletionReference']['fiveFileFingerprintsVerified'].items():check('worker-fingerprint-'+p,sha((ROOT/p).read_bytes())==digest)
for p in source:check('worker-object-'+p,git('rev-parse',HEAD+':'+p)==git('rev-parse',a['coreCompletionReference']['workerSHA']+':'+p))
freeze=Path(a['candidateVerification']['freezeReceipt']);check('freeze-pin',sha(freeze.read_bytes())==a['candidateVerification']['freezeReceiptSHA256']);(OUT/'freeze-receipt.json').write_bytes(freeze.read_bytes())
for name in ['dev.jsonl','test.jsonl','author_corpus.py','seal.json']:
 p=ROOT/'eval/router-update-20261005'/name;digest=sha(p.read_bytes());check('opaque-unchanged-'+name,digest==sha(git('show',PREVIOUS+':'+str(p.relative_to(ROOT)))),dict(sha256=digest,payload_parsed=False))
seal=json.loads((ROOT/'eval/router-update-20261005/seal.json').read_text())
for split in ['dev','test']:check('seal-'+split,sha((ROOT/'eval/router-update-20261005'/f'{split}.jsonl').read_bytes())==seal['splits'][split]['sha256'],seal['splits'][split])
archive=[];missing=[]
for directory in (ROOT/'eval/router-update-integration-20261005').iterdir():
 if not directory.is_dir():continue
 for manifest in directory.rglob('*sha256.json'):
  meta=json.loads(manifest.read_text());entries=meta.get('files',meta) if isinstance(meta,dict) else {}
  for rel,expected in entries.items():
   if isinstance(expected,str) and len(expected)==64:stored=expected;original=None
   elif isinstance(expected,dict) and expected.get('stored_sha256'):stored=expected['stored_sha256'];original=expected.get('original_sha256')
   else:continue
   p=manifest.parent/rel
   if not p.is_file():
    compressed=p.with_name(p.name+'.gz')
    if isinstance(expected,str) and compressed.is_file():
     raw=compressed.read_bytes();restored=sha(gzip.decompress(raw));archive.append(dict(manifest=str(manifest.relative_to(ROOT)),path=rel,stored_path=compressed.name,ok=restored==expected,stored_sha256=sha(raw),restored_sha256=restored,payload_parsed=False,format='legacy-original-hash-for-compressed-storage'));continue
    missing.append(dict(manifest=str(manifest.relative_to(ROOT)),path=rel));continue
   raw=p.read_bytes();digest=sha(raw);restored=sha(gzip.decompress(raw)) if original and p.suffix=='.gz' else digest if original else None
   archive.append(dict(manifest=str(manifest.relative_to(ROOT)),path=rel,ok=digest==stored and (not original or original==restored),stored_sha256=digest,restored_sha256=restored,payload_parsed=False))
check('opaque-archive-entries',bool(archive) and all(x['ok'] for x in archive),dict(entry_checks=len(archive),manifests=len({x['manifest'] for x in archive})))
# External original worker manifests can refer to their original absolute/outside custody paths.
# Missing original references are recorded; complete candidate stored manifests must have no missing entries.
stored_missing=[x for x in missing if 'original-bundle' not in x['manifest']]
check('stored-archive-completeness',not stored_missing,stored_missing)
complete=Path(a['priorEvidencePreservation']['completeManifest']);check('complete-archive-pin',sha(complete.read_bytes())==a['priorEvidencePreservation']['completeManifestSHA256'])
replays=[]
for directory in [ROOT/'eval/router-update-20261005',ROOT/'eval/router-update-integration-20261005']:
 for m in directory.rglob('manifest.json'):
  checksum=m.parent/'checksums.json'
  if not checksum.exists():continue
  meta=json.loads(m.read_text());pins=json.loads(checksum.read_text());rows=[]
  for rel,expected in pins.items():
   if isinstance(expected,str) and len(expected)==64:
    if rel in {'raw_sha256','raw_jsonl_sha256'}:
     p=m.parent/'raw.jsonl.gz';digest=sha(gzip.decompress(p.read_bytes())) if p.is_file() else None
    elif rel in {'raw_gzip_sha256','raw_jsonl_gzip_sha256','gzip_sha256'}:
     p=m.parent/'raw.jsonl.gz';digest=sha(p.read_bytes()) if p.is_file() else None
    elif rel=='manifest_sha256':
     p=m;digest=sha(p.read_bytes())
    else:
     p=m.parent/rel;digest=sha(p.read_bytes()) if p.is_file() else None
    rows.append(dict(path=rel,stored_path=p.name,ok=digest==expected,sha256=digest,payload_parsed=False))
  replays.append(dict(path=str(m.relative_to(ROOT)),mode=meta.get('mode'),cases=meta.get('cases'),not_model_measurement=meta.get('not_model_measurement'),metrics=meta.get('metrics'),git_head=meta.get('provenance',{}).get('git_head'),checks=rows,payload_parsed=False))
check('replay-checksums',bool(replays) and all(x['ok'] for r in replays for x in r['checks']),len(replays))
parent=Path('/private/tmp/wisp-router-update-8dfd5c9-gate-receipt.json');gate=None
if parent.exists():
 gate=json.loads(parent.read_text());check('parent-exact-gate',gate['candidate_sha']==HEAD and gate['head_unchanged'] is True and gate['exit_code']==0 and gate['status_porcelain']=='')
 log=Path(gate['log']);check('parent-log-pin',sha(log.read_bytes())==gate['log_sha256']);(OUT/'parent-gate-receipt.json').write_bytes(parent.read_bytes())
ci=[]
for kind in ['python','artifact']:
 p=Path('/private/tmp')/f'wisp-router-update-8dfd5c9-{kind}-ci.json'
 if not p.exists():continue
 raw=p.read_bytes();meta=json.loads(raw);entry=dict(kind=kind,path=str(p),sha256=sha(raw),metadata=meta)
 log=Path('/private/tmp')/f'wisp-router-update-8dfd5c9-{kind}-ci.log'
 if log.exists():entry['log_sha256']=sha(log.read_bytes())
 ci.append(entry);(OUT/(kind+'-ci-receipt.json')).write_bytes(raw)
(OUT/'assignment-snapshot.json').write_text(json.dumps(a,indent=2))
result=dict(head=HEAD,base=BASE,previous=PREVIOUS,recorded_at=datetime.now(timezone.utc).isoformat(),checks=checks,changed_files=changed,delta=delta,changed_file_sha256=fingerprints,archive_entry_checks=archive,missing_original_references=missing,replay_metadata=replays,parent_gate=gate,parent_gate_sha256=sha(parent.read_bytes()) if parent.exists() else None,ci_metadata=ci,assignment_sha256=sha(scope.read_bytes()),registry_sha256=sha(registry_bytes),limits=['No corpus/author/raw archive/replay parsing','No archived script execution','No provider diagnosis, model/native/outbound effects or network verification'])
(OUT/'provenance.json').write_text(json.dumps(result,indent=2));print(json.dumps({'checks':len(checks),'passed':sum(x['ok'] for x in checks),'archives':len(archive),'replays':len(replays),'parent_gate_present':bool(gate),'ci_receipts':len(ci),'failures':[x for x in checks if not x['ok']]}))
