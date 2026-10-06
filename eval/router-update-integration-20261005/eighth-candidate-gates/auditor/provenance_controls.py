from pathlib import Path
import json, hashlib, gzip, subprocess, sys
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-44babb7-gates-20261005/auditor')
SHA='44babb70ee2eea99897b9437b2f6d49b2d59adff';BASE='4994caa15533c0cf84c07208c9097e4197f2815b';PREV='8dfd5c9c19d8e31063a1f0d65c87d3d0d1938736'
def h(b): return hashlib.sha256(b).hexdigest()
def git(*a): return subprocess.check_output(['/usr/bin/git',*a],cwd=ROOT)
rows=[]
def record(label,actual,expected):
 rows.append(dict(id=label,actual=actual,expected=expected,pass_=actual==expected))
 if actual!=expected: print('FAIL',label,str(actual)[:180],str(expected)[:180])
head=git('rev-parse','HEAD').decode().strip(); record('HEAD',head,SHA)
record('clean',git('status','--porcelain').decode(),'')
record('base-ancestor',subprocess.run(['/usr/bin/git','merge-base','--is-ancestor',BASE,SHA],cwd=ROOT).returncode,0)
full=git('diff','--name-only',BASE,SHA).decode().splitlines();delta=git('diff','--name-only',PREV,SHA).decode().splitlines()
record('full-path-count',len(full),363);record('delta-path-count',len(delta),72)
sourcepaths=[p for p in delta if p.startswith(('service/','tests/','scripts/','native/'))]
allowed=['service/router/intent/validation.py','service/tasks/compiler.py','tests/test_router_intent_core.py','tests/test_router_intent_main.py']
record('four-repair-paths',sourcepaths,allowed)
files={p:h((ROOT/p).read_bytes()) for p in full}
registry=Path('/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_ROUTER_1_3_IMPLEMENTATION_OWNERSHIP_2026-10-05.json')
x=json.JSONDecoder().raw_decode(registry.read_text())[0]['exactCandidateGateAssignments20261005']
assignment=Path(x['assignmentDocument']); record('assignment-pin',h(assignment.read_bytes()),x['assignmentDocumentSHA256']);record('assignment-head',x['exactSHA'],SHA)
(OUT/'assignment-snapshot.md').write_bytes(assignment.read_bytes())
(OUT/'assignment-registry-snapshot.json').write_text(json.dumps(x,indent=2))
for name,pin in x['handoffFingerprints'].items():record('handoff-'+name,h((ROOT/'eval/router-update-integration-20261005'/name).read_bytes()),pin)
worker=x['coreCompletionReference']
for p,pin in worker['fileFingerprints'].items():
 record('worker-file-'+p,files[p],pin)
 record('worker-git-object-'+p,git('rev-parse',SHA+':'+p).decode().strip(),git('rev-parse',worker['workerSHA']+':'+p).decode().strip())
for pathkey,pinkey in [('handoff','handoffSHA256'),('bundle','bundleSHA256')]:record('worker-'+pathkey,h(Path(worker[pathkey]).read_bytes()),worker[pinkey])
freeze=x['candidateVerification'];fp=Path(freeze['freezeReceipt']);record('freeze-pin',h(fp.read_bytes()),freeze['freezeReceiptSHA256']); f=json.loads(fp.read_text());record('freeze-sha',f['candidate_sha'],SHA)
for name in ['dev.jsonl','test.jsonl','author_corpus.py','seal.json']:
 p='eval/router-update-20261005/'+name;record('opaque-corpus-unchanged-'+name,h((ROOT/p).read_bytes()),h(git('show',PREV+':'+p)))
prior=x['priorEvidencePreservation'];record('prior-complete-manifest-pin',h(Path(prior['completeManifest']).read_bytes()),prior['completeManifestSHA256'])
# Only hash maps are decoded; raw logs, archives, reports, corpus, author data stay opaque.
archive_manifest_count=0;archive_entry_count=0
integration=ROOT/'eval/router-update-integration-20261005'
for mf in sorted(integration.rglob('*sha256.json')):
 if '-candidate-gates' not in str(mf):continue
 d=json.loads(mf.read_text());archive_manifest_count+=1
 if isinstance(d,dict) and 'entries' in d:d=d['entries']
 if isinstance(d,list):
  for entry in d:
   p=Path(entry['path']);pin=entry['sha256'];record('external-original-pin:'+str(mf.relative_to(ROOT))+':'+p.name,h(p.read_bytes()) if p.exists() else 'MISSING',pin);archive_entry_count+=1
  continue
 if 'files' in d:d=d['files']
 for name,pin in d.items():
  if not isinstance(pin,(str,dict)): continue
  if isinstance(pin,str) and len(pin)!=64:continue
  p=mf.parent/name
  if p.exists():
   b=p.read_bytes();actual=h(b);stored=pin if isinstance(pin,str) else pin['stored_sha256'];record('archive-stored:'+str(p.relative_to(ROOT)),actual,stored)
   if isinstance(pin,dict) and pin.get('original_sha256'): record('archive-original:'+str(p.relative_to(ROOT)),h(gzip.decompress(b)) if p.suffix=='.gz' else actual,pin['original_sha256'])
  elif isinstance(pin,str) and Path(str(p)+'.gz').exists(): record('archive-original-restored:'+str(p.relative_to(ROOT)),h(gzip.decompress(Path(str(p)+'.gz').read_bytes())),pin)
  else: record('archive-missing:'+str(p.relative_to(ROOT)),'MISSING',pin)
  archive_entry_count+=1
replays=[]
for top in [ROOT/'eval/router-update-20261005',integration]:
 for mf in top.glob('*/manifest.json'):
  if not (mf.parent/'raw.jsonl.gz').exists():continue
  # Replay manifest is metric/provenance metadata, never raw cases.
  meta=json.loads(mf.read_text());raw=(mf.parent/'raw.jsonl.gz').read_bytes();original=gzip.decompress(raw)
  for k in ['raw_sha256','raw_jsonl_sha256']:
   if k in meta:record('replay-'+k+':'+str(mf.parent.relative_to(ROOT)),h(original),meta[k])
  checksum=mf.parent/'checksums.json';d=json.loads(checksum.read_text())
  for name,pin in d.items():
   if not isinstance(pin,(str,dict)):continue
   if name in ['raw_sha256','raw_jsonl_sha256']:b=original
   elif name in ['raw_gzip_sha256','raw_jsonl_gzip_sha256','gzip_sha256']:b=raw
   elif name=='manifest_sha256':b=mf.read_bytes()
   else:
    p=mf.parent/name
    if not p.exists():record('replay-missing:'+str(p),'MISSING',pin);continue
    b=p.read_bytes()
   record('replay-stored:'+str(mf.parent.relative_to(ROOT))+':'+name,h(b),pin if isinstance(pin,str) else pin['stored_sha256'])
   if isinstance(pin,dict) and 'original_sha256' in pin:record('replay-original:'+str(mf.parent.relative_to(ROOT))+':'+name,h(gzip.decompress(b)) if name.endswith('.gz') else h(b),pin['original_sha256'])
  replays.append(dict(path=str(mf.parent.relative_to(ROOT)),manifest_sha256=h(mf.read_bytes()),raw_gzip_sha256=h(raw),raw_original_sha256=h(original),metadata=meta))
receipt=Path('/private/tmp/wisp-router-update-44babb7-gate-receipt.json')
gate=None
if receipt.exists():
 gate=json.loads(receipt.read_text());(OUT/'parent-gate-receipt.json').write_bytes(receipt.read_bytes())
 record('parent-gate-sha',gate.get('candidate_sha',gate.get('head')),SHA)
 record('parent-gate-exit',gate['exit_code'],0);record('parent-gate-head-unchanged',gate['head_unchanged'],True);record('parent-gate-clean',gate['status_porcelain'],'')
 log=Path(gate.get('log','/private/tmp/wisp-router-update-44babb7-regression.log'));record('parent-full-log-pin',h(log.read_bytes()),gate['log_sha256'])
ci=[]
for p in Path('/private/tmp').glob('wisp-router-update-44babb7*ci*.json'):
 d=json.loads(p.read_text());ci.append(dict(path=str(p),sha256=h(p.read_bytes()),metadata=d));(OUT/p.name).write_bytes(p.read_bytes())
report=dict(candidate_sha=SHA,head=head,full_paths=full,delta_paths=delta,files=files,rows=rows,passed=sum(r['pass_'] for r in rows),total=len(rows),archive_manifests=archive_manifest_count,archive_entries=archive_entry_count,replays=replays,parent_gate=gate,ci=ci,restrictions='Archived/corpus/raw payloads only hashed, including restored gzip bytes. No archived scripts executed, heldout decoded, native/network/provider probes, or full gate duplicated.')
(OUT/('provenance-final.json' if '--final' in sys.argv else 'provenance.json')).write_text(json.dumps(report,indent=2))
print(json.dumps(dict(passed=report['passed'],total=len(rows),archive_manifests=archive_manifest_count,archive_entries=archive_entry_count,replays=len(replays),parent_gate=gate is not None,ci_receipts=len(ci))))
