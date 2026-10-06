import json,hashlib,gzip,subprocess,sys
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-98916cf-gates-20261005/auditor')
HEAD='98916cf5d17fee49c94045e5a0e3a713d47dce39';BASE='4994caa15533c0cf84c07208c9097e4197f2815b';PREVIOUS='6df5200d0945ee3b050bcde4115a338e392017b1'
def sha(b):return hashlib.sha256(b).hexdigest()
def git(*a):return subprocess.check_output(['git',*a],cwd=ROOT)
checks=[]
def check(name,ok,detail=None):checks.append(dict(name=name,ok=bool(ok),detail=detail))
check('exact-head',git('rev-parse','HEAD').decode().strip()==HEAD)
check('clean',git('status','--porcelain')==b'')
check('base-ancestor',subprocess.run(['git','merge-base','--is-ancestor',BASE,HEAD],cwd=ROOT).returncode==0)
changed=git('diff','--name-only',BASE,HEAD).decode().splitlines();delta=git('diff','--name-only',PREVIOUS,HEAD).decode().splitlines()
fingerprints={p:sha((ROOT/p).read_bytes()) for p in changed if (ROOT/p).is_file()}
check('scope-count',len(changed)==231,len(changed))
source_delta=[p for p in delta if p.startswith(('service/','tests/','scripts/','.github/'))]
check('four-authorized-delta-paths',set(source_delta)=={'service/router/intent/validation.py','service/router/web_request.py','tests/test_router_intent_core.py','tests/test_router_intent_main.py'},source_delta)
p=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_1_3_IMPLEMENTATION_OWNERSHIP_2026-10-05.json');registry,_=json.JSONDecoder().raw_decode(p.read_text());a=registry['exactCandidateGateAssignments20261005']
check('registry-exact-head',a['exactSHA']==HEAD)
scope=Path(a['assignmentDocument']);check('assignment-pin',sha(scope.read_bytes())==a['assignmentDocumentSHA256'])
for name,digest in a['handoffFingerprints'].items():check('handoff-'+name,sha((ROOT/name).read_bytes())==digest)
for name in ['dev.jsonl','test.jsonl','author_corpus.py','seal.json']:
 p=ROOT/'eval/router-update-20261005'/name;current=sha(p.read_bytes());prior=sha(git('show',PREVIOUS+':'+str(p.relative_to(ROOT))));check('opaque-unchanged-'+name,current==prior,{'sha256':current,'parsed_payload':False})
seal=json.loads((ROOT/'eval/router-update-20261005/seal.json').read_text())
for split in ['dev','test']:check('seal-'+split,sha((ROOT/'eval/router-update-20261005'/f'{split}.jsonl').read_bytes())==seal['splits'][split]['sha256'],{'metadata':seal['splits'][split],'payload_parsed':False})
archivechecks=[]
for directory in (ROOT/'eval/router-update-integration-20261005').iterdir():
 if not directory.is_dir():continue
 for manifest in directory.rglob('*sha256.json'):
  entries=json.loads(manifest.read_text())
  if not isinstance(entries,dict):continue
  for rel,expected in entries.items():
   if not isinstance(expected,(str,dict)):continue
   p=manifest.parent/rel
   if not p.is_file():continue
   raw=p.read_bytes();digest=sha(raw)
   if isinstance(expected,str):stored=expected;original=None
   else:stored=expected.get('stored_sha256');original=expected.get('original_sha256')
   if not stored:continue
   ok=digest==stored;restored=None
   if original:
    restored=sha(gzip.decompress(raw)) if p.suffix=='.gz' else digest;ok=ok and restored==original
   archivechecks.append(dict(manifest=str(manifest.relative_to(ROOT)),path=rel,ok=ok,stored_sha256=digest,restored_sha256=restored,parsed_payload=False))
check('opaque-archive-manifests',bool(archivechecks) and all(x['ok'] for x in archivechecks),{'entry_checks':len(archivechecks),'manifests':len({x['manifest'] for x in archivechecks})})
replays=[]
for directory in [ROOT/'eval/router-update-20261005',ROOT/'eval/router-update-integration-20261005']:
 for m in directory.rglob('manifest.json'):
  meta=json.loads(m.read_text());checksum=m.parent/'checksums.json'
  if not checksum.exists():continue
  pins=json.loads(checksum.read_text());rows=[]
  for rel,expected in pins.items():
   if isinstance(expected,str) and (m.parent/rel).is_file():rows.append(dict(path=rel,ok=sha((m.parent/rel).read_bytes())==expected,sha256=sha((m.parent/rel).read_bytes())))
  replays.append(dict(path=str(m.relative_to(ROOT)),mode=meta.get('mode'),cases=meta.get('cases'),not_model_measurement=meta.get('not_model_measurement'),metrics=meta.get('metrics'),git_head=meta.get('provenance',{}).get('git_head'),checks=rows,opaque_raw=True))
check('replay-checksums',bool(replays) and all(c['ok'] for r in replays for c in r['checks']),len(replays))
parent=Path('/private/tmp/wisp-router-update-98916cf-gate-receipt.json');receipt=json.loads(parent.read_text());parentlog=Path('/private/tmp/wisp-router-update-98916cf-regression.log')
check('parent-receipt-pin',sha(parent.read_bytes())=='29a2187461e29787490c27127aef6ee11b944a514bd224b225cfc1a4067480cc')
check('parent-log-pin',sha(parentlog.read_bytes())=='966187e0d6a9059b8f4a30bb13deadd48cde419ec04aeae6b15edf784ac8f201')
(OUT/'parent-gate-receipt.json').write_bytes(parent.read_bytes())
(OUT/'assignment-snapshot.json').write_text(json.dumps(a,indent=2))
result=dict(head=HEAD,base=BASE,previous=PREVIOUS,recorded_at=datetime.now(timezone.utc).isoformat(),checks=checks,changed_files=changed,delta=delta,changed_file_sha256=fingerprints,archive_entry_checks=archivechecks,replay_metadata=replays,parent_gate=receipt,assignment_sha256=sha(scope.read_bytes()),registry_snapshot_sha256=sha(p.read_bytes()) if False else sha(Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_1_3_IMPLEMENTATION_OWNERSHIP_2026-10-05.json').read_bytes()),limits=['No raw corpus/archive/replay parsing','No provider cancellation diagnosis or execution','No network/remote verification by Auditor'])
(OUT/'provenance.json').write_text(json.dumps(result,indent=2));print(json.dumps({'checks':len(checks),'passed':sum(x['ok'] for x in checks),'archives':len(archivechecks),'replays':len(replays),'failures':[x for x in checks if not x['ok']]}))
