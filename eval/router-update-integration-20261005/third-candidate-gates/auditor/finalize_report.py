"""Seal independently authored third-candidate audit; never modifies source."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-27490ae-gates-20261005/auditor')
HEAD='27490ae5055502423f187e84ca52d0064b794396'
BASE='4994caa15533c0cf84c07208c9097e4197f2815b'
OLD='40ee88a409eaec943ae4474130bd87263ea794e1'
def sha(data):return hashlib.sha256(data).hexdigest()
def git(*args):return subprocess.check_output(['/usr/bin/git',*args],cwd=ROOT)
def load(name):return json.loads((OUT/name).read_text())
def save(name,value):(OUT/name).write_text(json.dumps(value,indent=2)+'\n')
assert git('rev-parse','HEAD').decode().strip()==HEAD
assert not git('status','--porcelain')
provenance=load('provenance.json')
assert git('diff','--name-only',BASE,HEAD).decode().splitlines()==provenance['complete_changed_files']
assert all(sha((ROOT/name).read_bytes())==expected for name,expected in provenance['changed_file_sha256'].items())
results=load('independent-results.json')
boundary=load('boundary-results.json')
supplement=load('supplemental-results.json')
assert results['total']==44 and results['expectations_met']==42
assert boundary['total']==boundary['passed']==24
failed=[r['case']['id'] for r in results['rows'] if not r['expectation_met']]
assert set(failed)=={'mixed-source-filter-positive','coordinated-shared-date-positive'}
receipt_path=Path('/private/tmp/wisp-router-update-27490ae-gate-receipt.json')
receipt_bytes=receipt_path.read_bytes();receipt=json.loads(receipt_bytes)
assert receipt['candidate_sha']==HEAD and receipt['status']=='FAIL'
log_path=Path(receipt['log']);log_bytes=log_path.read_bytes()
assert sha(log_bytes)==receipt['log_sha256']
(OUT/'parent-gate-receipt.json').write_bytes(receipt_bytes)
time=datetime.now(timezone.utc).isoformat()
provenance['final_source_verification']={'exact_sha':HEAD,'base_sha':BASE,'tree_clean':True,
    'all_changed_file_hashes_match':True,'observed_at_utc':time}
provenance['parent_gate_receipt']={'source_path':str(receipt_path),'sha256':sha(receipt_bytes),
    'log_sha256_verified':sha(log_bytes),'status':'FAIL','passed_modules':178,'total_modules':179}
save('provenance.json',provenance)

def case(identity):return next(r for r in results['rows'] if r['case']['id']==identity)
mixed=case('mixed-source-filter-positive');email=case('coordinated-shared-date-positive')
findings=[
 {'id':'R3','priority':'P2','blocking':True,
  'title':'Bind required lookup filters to the source clause that requested them',
  'locations':[{'file':'service/router/intent/validation.py','start':671,'end':677}],
  'request':mixed['case']['prompt'],'injected_intent_sources':mixed['case']['intent_response']['sources'],
  'expected_calls':mixed['case']['expected_calls'],'observed_calls':mixed['run']['executed_calls'],
  'local_validation':mixed['local_validation'],'planner_disposition':mixed['planner']['disposition'],
  'description':'A find/about cue anywhere in the prompt makes the validator require a query or conversation on every non-free-time source. The correctly scoped calendar read has no requested query, so a valid calendar-plus-filtered-notes request is rejected after both bounded interpretation attempts. Actual main.agent clarifies and reads neither source. This is deterministic over-abstention for an unambiguous supported request, independent of model quality.',
  'required_behavior':'Require and validate a lookup filter only on the source occurrence whose clause requests that filter. Preserve omission, invented-query and tuple-binding rejection; do not disable completeness validation globally.',
  'controls':['mixed-source-no-filter-positive','full-tuples-positive','original-truncated-query-rejected','tuple-swap-query'],
  'evidence':{'file':'independent-results.json','case_id':'mixed-source-filter-positive'}},
 {'id':'R4','priority':'P2','blocking':True,
  'title':'Keep a coordinated email source noun from authorizing delivery and bypassing the read planner',
  'locations':[{'file':'service/router/web_request.py','start':455,'end':480},
    {'file':'service/router/web_request.py','start':581,'end':583},
    {'file':'service/router/router.py','start':6967,'end':6974}],
  'request':email['case']['prompt'],'injected_intent_sources':email['case']['intent_response']['sources'],
  'expected_calls':email['case']['expected_calls'],'local_validation':email['local_validation'],
  'standalone_planner_calls':email['planner']['calls'],'classification':email['classification'],
  'actual_main_route':next(ev for ev in email['run']['events'] if ev.get('type')=='routed'),
  'offered_tools':email['run']['offered_tools'],'observed_calls':email['run']['executed_calls'],
  'description':'The second clause email from Dorian for yesterday is a source noun coordinated with Read email from Selene. _delivery treats it as the email verb and accepts from Dorian for yesterday as a recipient. The web request reports send_email authority, which suppresses plan_read. The standalone validator accepts the correct intent and the standalone planner compiles both strict sender/yesterday reads, but actual main.agent falls back to the rules email lookup route, with no scripted_intent generation and without verified-results-only or strict read limits.',
  'impact_limits':'The executed fallback fixture exposed six legacy email tools and did not expose send_email. No real send, native call or expanded executable outbound capability was observed. The scripted fallback emitted a fixture answer with no calls; that does not establish that every actual fallback model would fail to read. The proven defect is incorrect action metadata and deterministic bypass of structured read validation.',
  'required_behavior':'Recognize coordinated source-noun email from/about phrases as reads before delivery classification; retain genuine addressee delivery and quoted/negated boundaries. Ensure supported coordinated reads reach the structured compiler in actual main.agent.',
  'controls':['new-verb-independent-date-positive','coordinated-source-noun','explicit-second-read','genuine-compound-delivery','genuine-standalone-delivery','negated-compound-delivery','quoted-delivery-description'],
  'evidence':{'file':'independent-results.json','case_id':'coordinated-shared-date-positive','supplement':'supplemental-results.json'}}
]
prior=[
 {'id':'R1','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Full repeated-source tuples and coverage are checked per occurrence. Correct and reversed tuples execute scoped reads; query/date/account/count/unread/operation swaps, missing occurrences and extra cross-products reject without reads. Identical duplicate requests dedupe correctly.'},
 {'id':'R2','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Source-free new-query corrections bind to uniquely established context and replace stale queries; other inherited account/count/unread/date filters remain intact. Conflicting multi-source or repeated-source context clarifies without calls.'},
 {'id':'F1','status':'ORIGINAL_REPRODUCERS_RESOLVED','details':'Original date/scope/duration omissions and unsupported daypart reject. R1 full-tuple repair independently closes the later association defect. R3 is a new completeness overrestriction.'},
 {'id':'F2','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Truncated unquoted query rejects; new and quoted correction queries execute, stale queries reject.'},
 {'id':'F3','status':'RESOLVED','details':'Tomorrow agenda shortcut emits get_upcoming(period=tomorrow, calendar_only=True).'},
 {'id':'F4','status':'RESOLVED','details':'All six new test modules are explicitly admitted; unknown and missing manifest entries fail closed.'},
 {'id':'P3','status':'MITIGATED_WITH_LEGACY_LIMIT','details':'Original Python equality scores remain unchanged for bool/float versus int; typed supplemental request_argument and guarded_end_to_end reject both mismatches. Legacy exact_arguments alone is not typed fidelity.'},
 {'id':'UTC','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Fresh UTC and America/Los_Angeles controls match exact 23h/25h DST epochs, distinguish wrong days, restore TZ on exceptions/absence, and fail closed without tzset.'},
 {'id':'ALIASES','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Appointments that are on our calendar, emails in the inbox and texts in our messages work. Separately scoped repeated aliases preserve both reads; ambiguous scopes and dropped occurrences reject.'}
]
limits=[
 'No actual model inference, accuracy measurement, real endpoint/status probes, training, residency changes or activation. Scripted synthetic results do not establish a 95% model-validity target.',
 'No native data-source/tool bodies, personal data/settings/credentials, real communications, network effects, install/relaunch, deployment or merge. Guard installed before all service imports; registered callables are fake fixtures.',
 'Heldout test.jsonl, corpus authoring script, raw replay/archive payloads remained opaque. Only bytes/hashes and permitted manifest/seal metadata were inspected. No archived script executed.',
 'No duplicate full regression was run. Parent once-only failure is preserved; no waiver, repaired source or replacement passing receipt exists in this audit.',
 'Required CI recorded pending in the parent receipt at 2026-10-05T21:10:36.836107+00:00; no fresh independent CI pass receipt inspected. Earlier candidate CI/local success does not transfer.',
 'Distinct Simulation QA remains held by this BLOCK. Protected integration, any required Live QA and release-performance gates remain separate.',
 'Planner default-disabled/resource limitations persist. This audit does not approve activation, installed-app replacement or shipping.',
 'Original scripted DEV80/80 exact/source and original/guarded E2E72/80, with eight annotation limitations, remain historical builder fixture evidence. Zero-applicable denominators are not a property pass.',
 'The raw outbound_verb helper is lexical, not sole authorization. Supplemental negated compound produces a waiting_for_content workflow with a content error, while web classification grants no effects. No workflow execution or denial bypass is claimed from that helper return.'
]
gate={
 'id':'G1','status':'FAIL','blocking':True,'source':'parent once-only full isolated regression',
 'passed_modules':178,'total_modules':179,'failed_modules':['tests/test_message_digest.py'],
 'failing_test':'test_distinct_redacted_requests_keep_both_source_occurrences',
 'location':{'file':'tests/test_message_digest.py','start':2952,'end':2962},
 'diagnosis':'The privacy assertion searches str(rows), including retained source timestamps, for OTP substrings. At fixed epoch1791234560 both message bodies and contexts are secret-free, two distinct source occurrences remain and second-pass cardinality is two, but whole-row substring check fails because timestamps contain1234. The same body fixture at epoch1790000000 passes the whole-row check. This independently establishes the deterministic timestamp-collision mechanism; the parent traceback did not print rows, so the exact failed process timestamp is parent-reported rather than independently extracted.',
 'required_behavior':'Repair the assertion to inspect sensitive text/diagnostic surfaces while testing timestamps separately; use deterministic clocks for the remaining time-sensitive checks. Preserve occurrence/cardinality/privacy coverage. A repair creates a new candidate and requires new exact-SHA gates.',
 'receipt':'parent-gate-receipt.json','external_log':str(log_path),'log_sha256':sha(log_bytes),
 'independent_evidence':'supplemental-results.json','repair_owner':'Dedicated repair-owner reservation requested by parent; no approval inferred; auditor made no source changes.'}
report={'schema_version':1,'role':'independent read-only Wisp Release Auditor',
 'independent_of':['builder/integrator','source repair authors','Simulation QA actor'],
 'verdict':'BLOCK','exact_sha':HEAD,'base_sha':BASE,'previous_audit_sha':OLD,
 'branch':'codex/router-update-1-3','pr':'https://github.com/adjad/wisp/pull/160',
 'checkout':str(ROOT),'output_directory':str(OUT),'recorded_at_utc':time,
 'scope':{'complete_changed_files':len(provenance['complete_changed_files']),
    'changed_since_previous_audit':len(provenance['changed_since_previous_candidate']),
    'method':'Completed full base-to-head assessment by carrying forward completed prior source/context reviews for unchanged files and reading the entire current delta and relevant execution context. All current changed files hashed; production delta is validation.py. Corpus/author/raw archives are explicitly opaque exceptions, not unreviewed production code.',
    'changed_files_manifest':'provenance.json',
    'areas':['schema/compiler/planner','source/query/date/count/account tuple authority','corrections and source aliases','pre-I/O endpoint/client/role identity','read/outbound and quoted/negated boundaries','exclusions/privacy','actual ASGI/agent orchestration with fixtures','grounding/partial/error rendering','scorer/timezone/manifest safety','evidence provenance and activation limits']},
 'findings':findings,'prior_finding_reassessment':prior,'mechanical_failure':gate,
 'independent_execution':{'cases':44,'expectations_met':42,'failed_supported_cases':failed,
    'boundary_controls':24,'boundary_controls_passed':24,
    'supplemental_controls':'Six classification probes and two fixed-timestamp redaction probes; observations retained without recasting helper returns as application effects.',
    'all_commands_exit_zero_after_auditor_fixture_repair':True,
    'auditor_fixture_repair':'Initial provenance verifier failed with KeyError raw_sha256 because newest checksum manifest uses raw_jsonl_sha256/raw_jsonl_gzip_sha256. Only auditor-owned verifier was repaired to accept the declared schema; rerun verified hashes. No candidate/evidence payload was altered.',
    'commands':['PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python '+str(OUT/'independent_cases.py'),
      'PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python '+str(OUT/'boundary_controls.py'),
      'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 '+str(OUT/'provenance_checks.py'),
      'PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python '+str(OUT/'supplemental_controls.py')],
    'evidence':['independent-results.json','boundary-results.json','supplemental-results.json','provenance.json']},
 'provenance_summary':{'head_and_tree_verified_at_completion':True,'replay_bundles_verified':6,
    'archive_entries_verified':sum(x['entries'] for x in provenance['archives']),
    'opaque_seals_and_corpus_bytes_unchanged':True,'handoff_fingerprints_verified':True,
    'exact_assignment_verified':True,'candidate_source_edits':False},
 'failures_skips_limits':limits,
 'handoff':'Parent reads this report; no cross-chat response is required. Route R3/R4/G1 to Orchestrator for one recorded owner per finding before source edits. Require new freeze/ACK and independent re-review after repairs. No specialist QA release or merge approval from this BLOCK.'}
save('REPORT.json',report)

markdown=f'''# Wisp PR #160 — independent audit

**Verdict: BLOCK** on `{HEAD}`, base `{BASE}`, branch `codex/router-update-1-3`. Recorded {time}.

The previous repeated-source tuple and stale-query defects are closed in fresh controls. Two supported requests still expose deterministic routing defects. The parent's once-only regression is also failed (178/179 modules); its timestamp-dependent privacy assertion needs repair, not a waiver.

## Blocking findings

### R3 — P2 — required query leaks across independent source clauses

`service/router/intent/validation.py:671–677` treats any lookup cue in the whole prompt as a requirement for a query on every non-free-time source.

Request: **Show my calendar tomorrow; find notes about amber notebooks from yesterday**.

The supplied intent correctly requests calendar/tomorrow and notes/amber notebooks/yesterday. Expected calls are `get_upcoming(period="tomorrow", calendar_only=True)` and `search_notes(query="amber notebooks", period="yesterday")`. Instead, validation reports “A requested lookup filter was dropped”; both bounded interpretation attempts fail, and actual `main.agent` clarifies with no source calls. The calendar clause never asked for a query. This is an unsupported restriction on a clear, supported mixed-source request.

Bind filter completeness to the relevant source occurrence; retain missing/invented/truncated query and tuple rejection. Evidence: `independent-results.json`, case `mixed-source-filter-positive`. Mixed-source unfiltered reads and strict query negative controls pass.

### R4 — P2 — coordinated email noun becomes a delivery authorization

`service/router/web_request.py:455–480,581–583` parses **Read email from Selene and email from Dorian for yesterday** as containing a delivery, recipient `from Dorian for yesterday`, and `authorized_effects=["send_email"]`. `service/router/router.py:6967–6974` therefore bypasses the structured read planner.

The standalone validator accepts the correct intent; standalone planning compiles two strict `view_emails` sender reads for yesterday. Actual `main.agent` instead takes `rules / email lookup -> scoped tools (6)`, performs no intent generation, and lacks verified-results-only and strict read limits. The fixture fallback makes no reads.

The proven defect is incorrect action metadata and planner bypass. The fallback menu contained six legacy email tools; it did **not** expose `send_email`. No real or fixture send occurred. No-call fallback output is a scripted observation, not a claim that every real fallback model fails. Recognize coordinated `email from/about` source nouns while retaining genuine addressee delivery.

Evidence: `independent-results.json`, case `coordinated-shared-date-positive`; `supplemental-results.json`. An explicit second `read` reaches the planner. Genuine “email Mom” requests still classify as delivery. Quoted and negated controls grant no web-request effects. The workflow compiler already recognizes the coordinated read noun as non-outbound, confirming an inconsistent boundary between components.

## Mechanical gate failure G1

Parent command: `PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python scripts/test_replay_failure_fixes.py`. Receipt: `parent-gate-receipt.json`; external log `{log_path}`, verified SHA256 `{sha(log_bytes)}`. Exit1, **178/179 modules**, only `tests/test_message_digest.py` failed: `test_distinct_redacted_requests_keep_both_source_occurrences` at line2961 (1427 tests passed in that module, one failed).

The assertion searches `str(rows)` for synthetic OTPs as well as private words, including numeric timestamps. Fresh guarded helper probes at epoch1791234560 preserve two source occurrences, correctly redact bodies/contexts, and preserve two on a second pass; the whole-row assertion still fails because retained timestamps contain `1234`. At epoch1790000000 the identical text fixture passes. This proves the timestamp-collision mechanism. The parent traceback does not print rows; its exact live timestamp is parent-reported. It does not establish a body-redaction leak.

The gate remains **FAIL**. Repair the text-surface assertion and deterministic clock coverage without weakening privacy/cardinality checks. A dedicated repair-owner reservation is requested, not assumed approved. The auditor did not edit source or rerun the full gate.

## Reassessed repairs and evidence

- R1: correct and reversed full tuples execute; query/date/account/count/unread/operation swaps, missing occurrence and cross-product extras reject; duplicate identical reads dedupe.
- R2: source-free corrections use the new full query and retain valid inherited count/date/account/unread. Stale outputs and ambiguous multi-source/repeated context reject without reads.
- Aliases: natural appointments/calendar, email/inbox and texts/messages variants work. Repeated independently dated aliases preserve both scopes; ambiguous or omitted scopes reject.
- Original F1–F3 reproducers: date/scope/duration omissions, truncated query and unsupported daypart reject; tomorrow shortcut remains calendar-only.
- F4: six added test modules admitted; unknown/missing manifest entries fail closed.
- P3: legacy bool/float/int equality remains disclosed; supplemental typed argument and guarded end-to-end metrics reject those mismatches.
- UTC repair: UTC/Los_Angeles hosts match exact 23h/25h DST spans, distinguish wrong days, restore environment after errors/absent TZ, and fail closed without tzset.
- Identity/exclusion boundaries: absent config, wrong origin/provider, missing transport and kill switch make no fake model calls; exclusion positive/negative controls preserve requested scope. Genuine message delivery and quoted/negated controls remain differentiated in their tested components.

Fresh application/planner fixtures: **42/44 expectations met**, with the two failed supported positives above preserved. Fresh boundary helpers: **24/24**. Supplemental evidence contains six classification probes and two timestamp probes. These are not model accuracy scores. All service imports followed guard installation; actual orchestration used scripted clients and registered fake callables.

## Scope, provenance and limits

Reviewed the complete **102-path** current base-to-head change through prior complete source/context reviews plus the full **31-path** delta. The only new production delta is `validation.py`; current tests/docs/context were assessed. Final HEAD, clean tree and every changed-file hash were reverified. `provenance.json` includes the complete paths, six replay bundle hash checks, 34 archived entry hash checks, sealed opaque corpus byte equality, handoff pins and exact registered scope.

Initial auditor provenance verification had a `KeyError` from two checksum field-name schemas. Only the fresh auditor-owned verifier was corrected; declared raw/gzip hashes then matched. No payload or candidate repair was made. No archived script was executed.

Heldout `test.jsonl`, corpus-author script and raw replay/archive payloads stayed opaque; permitted bytes/hashes and metadata only. No real model/native tools, personal data/settings/credentials, outbound/network actions, training/residency/activation, install/relaunch, deployment or merge occurred. No full-gate duplication. Builder DEV80/80 and E2E72/80 are scripted stage evidence with annotation limits, not actual-model/activation proof; empty denominators are not passes.

CI was recorded pending in the parent receipt at21:10:36UTC; no new independent passing CI receipt was inspected. Prior candidate passes do not transfer. Distinct Simulation QA remains held by this BLOCK. Planner default-disabled/resource limits and separate integration/Live QA/performance/ship gates remain.

## Handoff

Return R3/R4/G1 to the Orchestrator for one recorded repair owner per finding before edits. Repairs require a new exact candidate and independent re-review. The parent reads these files; no cross-chat reply is required. This audit authorizes no source repair, QA release, activation or merge.
'''
(OUT/'REPORT.md').write_text(markdown)
save('HANDOFF.json',{'schema_version':1,'verdict':'BLOCK','exact_sha':HEAD,'base_sha':BASE,
    'branch':report['branch'],'pr':report['pr'],'recorded_at_utc':time,
    'blocking_findings':['R3','R4'],'blocking_mechanical_failure':'G1',
    'previous_findings_closed':['R1','R2'],'report':'REPORT.md','structured_report':'REPORT.json',
    'provenance':'provenance.json','checksums':'checksums.json',
    'candidate_source_edits':False,'tree_clean':True,
    'simulation_qa_release':False,'activation_or_shipping_approval':False,
    'next_step':'Orchestrator records sole repair ownership; new freeze/ACK and independent review after repairs. Preserve failed receipts. Parent reads final; no cross-chat message required.'})
checks={p.name:sha(p.read_bytes()) for p in sorted(OUT.iterdir()) if p.is_file() and p.name!='checksums.json'}
save('checksums.json',{'schema_version':1,'exact_sha':HEAD,'recorded_at_utc':time,'algorithm':'SHA256',
    'files':checks,'excludes_self':True,'external_regression_log':{'path':str(log_path),'sha256':sha(log_bytes)}})
assert all(sha((OUT/name).read_bytes())==value for name,value in checks.items())
print(json.dumps({'verdict':'BLOCK','head':HEAD,'audit_findings':['R3','R4'],
    'mechanical_failure':'G1','sealed_files':len(checks),'report_sha256':checks['REPORT.md'],
    'handoff_sha256':checks['HANDOFF.json'],'tree_clean':True}))
