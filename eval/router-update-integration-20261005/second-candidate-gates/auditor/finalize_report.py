"""Render and seal auditor-owned re-audit outputs after unchanged-head checks."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
OUT=Path('/private/tmp/wisp-router-40ee88a-gates-20261005/auditor')
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
report=json.loads((OUT/'REPORT.json').read_text())
head=subprocess.check_output(['/usr/bin/git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
status=subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=ROOT,text=True)
assert head==report['exact_sha'] and not status
scope=json.loads((OUT/'fresh-scope-results.json').read_text())
boundary=json.loads((OUT/'fresh-boundary-results.json').read_text())
assert scope['total']==25 and scope['expectations_met']==23
assert {r['case']['id'] for r in scope['rows'] if not r['expectation_met']}=={'repeated-domain-date-swapped','fragment-query-stale'}
assert boundary['total']==boundary['passed']==25
report['completed_at_utc']=datetime.now(timezone.utc).isoformat()
report['final_checkout_verification']={'head':head,'clean':True,'porcelain':status}
log=Path(report['external_gates']['full_regression']['log'])
raw=log.read_bytes()
if b'Regression gate:' in raw:
    summary=next(line for line in reversed(raw.decode().splitlines()) if line.startswith('Regression gate:'))
    report['external_gates']['full_regression'].update(status='FINAL_SUMMARY_OBSERVED',summary=summary,
        description='Parent log final summary observed; no independent duplicate full gate. Exact receipt/CI remain parent responsibilities.')
report['external_gates']['full_regression']['observed_log_sha256']=hashlib.sha256(raw).hexdigest()
(OUT/'REPORT.json').write_text(json.dumps(report,indent=2)+'\n')
lines=['# Independent re-audit: BLOCK','',
    'PR [#160](https://github.com/adjad/wisp/pull/160), branch `codex/router-update-1-3`.',
    '',f'Candidate: `{head}`; base: `{report["base_sha"]}`.','',
    '**This exact commit is not approved.** The original individual omission, shortcut, manifest and timezone repairs work in fresh controls, but two source-scope defects remain. Candidate source stayed clean and unchanged.',
    '',report['scope']['method'],'','## Blocking findings','']
for f in report['findings']:
    lines += [f'### {f["priority"]} {f["id"]}: {f["title"]}','',f['description'],'',
        '**Latest request:** '+f['request'],'']
    if 'context' in f:
        lines += ['**Prior context:** `Find notes about cedar manuscripts` followed by a synthetic `search_notes` receipt.','']
    lines += ['**Actual compiled and executed calls:** `'+json.dumps(f['observed_calls'])+'`.','',
        '**Positive/control behavior:** '+f['positive_control'],'',
        '**Locations:** '+ '; '.join(loc['file']+':'+', '.join(map(str,loc['lines'])) for loc in f['locations'])+'.','',
        '**Required behavior:** '+f['required_behavior'],'',
        '**Evidence:** `'+f['evidence']['file']+'`, case `'+f['evidence']['negative_case']+'`; full exact supplied intent, fake model output, SSE, scopes and fixture execution receipts are retained.','']
lines += ['## Prior findings and UTC repair','']
for old in report['prior_finding_reassessment']:
    lines += ['- **'+old['id']+' — '+old['status']+':** '+old['details']]
lines += ['','## Independent verification','']
for check in report['validation']:
    lines += ['- `'+check['command']+'` (exit '+str(check['exit_code'])+'): '+check['summary']]
lines += ['']
for correction in report['auditor_fixture_corrections']:
    lines += [correction['description'],'']
lines += ['**Skips:** none in the fresh checks. All source-call receipts are fake; no real tool or model body executes. '
    'A reproduction program exiting zero does not mean its negative cases passed; the two wrong-call cases are explicitly false in the sealed results.','',
    'The opaque heldout, development, seal and author bytes are unchanged from the prior candidate. '
    'All four saved raw/gzip replay bundles and17 archived first-candidate evidence files match declared hashes. '
    'Handoff/progress/development-result pins and the exact registry assignment match. See `provenance.json` and `checksums.json`.','',
    '## External gates and limits','',json.dumps(report['external_gates'],indent=2),'']
for observation in report['positive_observations']:
    lines += ['- '+observation]
lines += ['']
for limit in report['limitations']:
    lines += ['- '+limit]
lines += ['',report['handoff'],'','Simulation QA remains held while this verdict is BLOCK.']
(OUT/'REPORT.md').write_text('\n'.join(lines)+'\n')
handoff={'exact_sha':head,'base_sha':report['base_sha'],'verdict':'BLOCK','candidate_changed':False,
    'role':report['role'],'findings':[{'id':f['id'],'priority':f['priority'],'title':f['title'],
        'blocking':f['blocking'],'locations':f['locations'],'evidence':f['evidence']} for f in report['findings']],
    'prior_finding_reassessment':report['prior_finding_reassessment'],
    'report_md':str(OUT/'REPORT.md'),'report_json':str(OUT/'REPORT.json'),
    'required_next_action':report['handoff'],'simulation_qa_release':False,
    'heldout_payload_opened':False,'actual_model_calls':False,'native_or_outbound_execution_by_auditor':False,
    'full_gate_or_ci_pass_claimed':False}
(OUT/'HANDOFF.json').write_text(json.dumps(handoff,indent=2)+'\n')
checksums={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.iterdir())
    if p.is_file() and p.name!='checksums.json'}
(OUT/'checksums.json').write_text(json.dumps(checksums,indent=2)+'\n')
print(json.dumps({'verdict':'BLOCK','head':head,'clean':True,'findings':len(report['findings']),
    'scope_expectations_met':'23/25','boundary_controls_passed':'25/25',
    'report_json_sha256':checksums['REPORT.json'],'external_gates':report['external_gates']}))
