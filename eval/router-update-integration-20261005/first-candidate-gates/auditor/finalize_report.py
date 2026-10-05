"""Seal auditor-owned outputs after read-only candidate identity verification."""
import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone
OUT = Path('/private/tmp/wisp-router-3f86187-gates-20261005/auditor')
ROOT = Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
head = subprocess.check_output(['/usr/bin/git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
status = subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=ROOT,text=True)
assert head == '3f86187ccfca1a20423506c883acecc6fec59ae1' and not status
report = json.loads((OUT/'REPORT.json').read_text())
native_path = Path('/private/tmp/wisp-router-update-3f86187-native-peer-unsandboxed-launch.json')
native = json.loads(native_path.read_text())
assert native['candidate_sha'] == head == native['ending_sha']
assert native['status'] == 'PASS' and native['clean_start'] and native['clean_end']
assert native['cases']['collected'] == 9 and native['cases']['exitstatus'] == 0
assert len([row for row in native['cases']['rows'] if row['phase']=='call' and row['outcome']=='passed']) == 9
report['parent_mechanical_gate']['failures'][1]['assessment'] = (
    'Original nested native gate failed before collection because sandbox-exec sandbox_apply was denied by the outer Codex sandbox. '
    'Separate same-SHA compatible-launch receipt now reports PASS for all nine synthetic cases, with clean start/end. '
    'This resolves that diagnosis; the original 175/179 full-gate result remains unchanged.')
report['parent_mechanical_gate']['native_peer_followup'] = {
    'status':'PASS','cases':9,'exact_sha':head,'receipt':str(native_path),
    'sha256':hashlib.sha256(native_path.read_bytes()).hexdigest()}
report['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
report['final_checkout_verification'] = {'head':head, 'porcelain':status, 'clean':True}
(OUT/'REPORT.json').write_text(json.dumps(report,indent=2)+'\n')

lines = ['# Independent audit: BLOCK','',
    'PR [#160](https://github.com/adjad/wisp/pull/160), branch `codex/router-update-1-3`.',
    '',f'Candidate: `{head}`; base: `{report["base_sha"]}`.','',
    '**This candidate is not approved.** Four actionable blocking findings follow. Candidate source remained clean and unchanged.',
    '',report['scope'],'','## Blocking findings','']
for f in report['findings']:
    lines += [f'### {f["priority"]} {f["id"]}: {f["title"]}','',f['description'],'',
        '**Reproduction:** '+f['observed'],'',
        '**Locations:** '+ '; '.join(loc['file']+':'+', '.join(map(str,loc['lines'])) for loc in f['locations'])+'.','',
        '**Required correction:** '+f['remediation'],'','**Evidence:** '+ '; '.join(f['evidence'])+'.','']
lines += ['## Checks and evidence','']
for check in report['validation']:
    lines += ['- `'+check['command']+'` (exit '+str(check['exit_code'])+'): '+check['result']]
lines += ['', 'The parent full gate completed **175/179 modules passing**. Its four recorded failures are:', '']
for failure in report['parent_mechanical_gate']['failures']:
    lines += ['- `'+failure['module']+'`: '+failure['assessment']]
lines += ['', 'The separate native-peer PASS resolves its environment diagnosis, not the failed full-suite result. '
    'The parent plans a complete compatible-environment gate after repairs. Required CI is unverified in this audit; no pass or unavailability is inferred from the queued assignment snapshot.',
    '', 'Both opaque corpus byte hashes match the existing seal. All three stored replay bundles match their declared raw/gzip hashes and, where declared, manifest hashes and byte counts. '
    'No corpus or compressed raw replay rows were deserialized. Handoff/progress/development-result pins match; see `provenance.json`. '
    'The auditor verifier initially assumed one checksum convention and was corrected for the baseline raw_gzip_sha256/byte-count schema; no checked-in evidence was altered.',
    '', '## Nonblocking scorer note','']
for note in report['notes']:
    lines += ['**'+note['priority']+': '+note['title']+'.** '+note['description'], '',
        'Locations: '+ '; '.join(loc['file']+':'+', '.join(map(str,loc['lines'])) for loc in note['locations'])+'. Evidence: '+note['evidence']+'.','']
lines += ['## Positive observations and limits','']
for observation in report['positive_observations']:
    lines += ['- '+observation]
lines += ['']
for limitation in report['limitations']:
    lines += ['- '+limitation]
lines += ['', 'The bad-intent injection proves a local validation/orchestration defect; it does not measure how often the resident model makes that mistake. '
    'The existing sender-replacement control was rejected without a source read. No additional actionable endpoint/credential, outbound or diagnostic-privacy defect was established.',
    '', report['handoff'], '', 'Simulation QA remains held while this verdict is BLOCK.']
(OUT/'REPORT.md').write_text('\n'.join(lines)+'\n')
handoff = {'exact_sha':head,'verdict':'BLOCK','role':report['role'],'candidate_changed':False,
    'findings':[{'id':f['id'],'priority':f['priority'],'title':f['title'],'blocking':f['blocking']} for f in report['findings']],
    'report_md':str(OUT/'REPORT.md'),'report_json':str(OUT/'REPORT.json'),
    'required_next_action':report['handoff'],'heldout_payload_opened':False,'actual_model_calls':False,
    'native_or_outbound_execution_by_auditor':False,'ci_pass_claimed':False,
    'full_gate_pass_claimed':False,'parent_native_peer_followup':report['parent_mechanical_gate']['native_peer_followup']}
(OUT/'HANDOFF.json').write_text(json.dumps(handoff,indent=2)+'\n')
checksums = {path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(OUT.iterdir())
    if path.is_file() and path.name!='checksums.json'}
(OUT/'checksums.json').write_text(json.dumps(checksums,indent=2)+'\n')
print(json.dumps({'verdict':report['verdict'],'head':head,'clean':True,'findings':len(report['findings']),
    'report_sha256':checksums['REPORT.json'],'native_followup':report['parent_mechanical_gate']['native_peer_followup']}))
