import json,hashlib,subprocess
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-98916cf-gates-20261005/auditor');HEAD='98916cf5d17fee49c94045e5a0e3a713d47dce39'
def sha(b):return hashlib.sha256(b).hexdigest()
p=Path('/private/tmp/wisp-router-update-98916cf-python-ci.json');raw=p.read_bytes();meta=json.loads(raw)
assert sha(raw)=='732a0aff1412f72977d158b1c112654b7ed51b587f3b2295941fb10ea63f05f1'
assert meta['headSha']==HEAD and meta['conclusion']=='success' and meta['status']=='completed'
assert meta['jobs'][0]['conclusion']=='success' and meta['jobs'][0]['databaseId']==112025875624
log=Path('/private/tmp/wisp-router-update-98916cf-python-ci.log');digest=sha(log.read_bytes());assert digest=='c72f98a6cdf9fa9012dd7f7027dae13a306f361dadb4882e148452521bbb71c4'
(OUT/'python-ci-receipt.json').write_bytes(raw)
for name in ['REPORT.md','REPORT.json','HANDOFF.json','checksums.json']:(OUT/('pre-python-ci-'+name)).write_bytes((OUT/name).read_bytes())
r=json.loads((OUT/'REPORT.json').read_text());r['gates']['required_ci']='Python CI terminal same-head SUCCESS verified from parent-fetched metadata and opaque complete-log hash; parent reports179/179 modules. macOS artifact remains pending; no terminal artifact pass inspected.'
r['gates']['python_ci']={'run':37387940899,'job':112025875624,'conclusion':'success','sha':HEAD,'completed_at':meta['updatedAt'],'metadata_sha256':sha(raw),'log_sha256':digest,'parent_reported_modules':179,'metadata_independently_checked':True,'auditor_network_request':False,'provider_diagnosis_performed':False,'prior_failure_waived':False}
r['terminal_ci_update_at']=datetime.now(timezone.utc).isoformat();(OUT/'REPORT.json').write_text(json.dumps(r,indent=2))
p=OUT/'REPORT.md';text=p.read_text();text=text.replace('Both required current CI jobs were last parent-reported running; no terminal PASS independently verified.', 'Python CI run37387940899/job112025875624 is now terminal SUCCESS on this exact head, completed2026-10-05T23:38:44Z; parent reports179/179 modules. Auditor verified the parent-fetched metadata SHA256 `732a0aff1412f72977d158b1c112654b7ed51b587f3b2295941fb10ea63f05f1` and complete opaque log SHA256 `c72f98a6cdf9fa9012dd7f7027dae13a306f361dadb4882e148452521bbb71c4`. macOS artifact CI remains parent-reported running, with no terminal pass inspected.');p.write_text(text)
h=json.loads((OUT/'HANDOFF.json').read_text());h['incomplete_work']=r['gates'];h['terminal_ci_update_at']=r['terminal_ci_update_at'];h['reports']={name:{'path':str(OUT/name),'sha256':sha((OUT/name).read_bytes())} for name in ['REPORT.md','REPORT.json']};(OUT/'HANDOFF.json').write_text(json.dumps(h,indent=2))
initial=json.loads((OUT/'final-verification.json').read_text());assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()==HEAD;assert subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)==b''
assert {p:sha((ROOT/p).read_bytes()) for p in initial['changed_file_sha256']}==initial['changed_file_sha256']
initial['post_ci_update_clean_head_verified_at']=r['terminal_ci_update_at'];(OUT/'final-verification.json').write_text(json.dumps(initial,indent=2))
files={p.name:sha(p.read_bytes()) for p in sorted(OUT.iterdir()) if p.is_file() and p.name!='checksums.json'};(OUT/'checksums.json').write_text(json.dumps({'sha':HEAD,'sealed_at':r['terminal_ci_update_at'],'files':files},indent=2))
for name,digest in files.items():assert sha((OUT/name).read_bytes())==digest
print(json.dumps({'verdict':'BLOCK','sha':HEAD,'sealed_files':len(files),'report_sha256':files['REPORT.md'],'handoff_sha256':files['HANDOFF.json'],'python_ci':'SUCCESS','artifact_ci':'PENDING','prior_provider_diagnosis':'UNRESOLVED'}))
