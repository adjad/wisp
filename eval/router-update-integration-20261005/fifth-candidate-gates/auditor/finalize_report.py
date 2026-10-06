"""Independently seal exact6df5200 audit; no candidate edits."""
from datetime import datetime,timezone
import hashlib,json,subprocess
from pathlib import Path
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-6df5200-gates-20261005/auditor')
HEAD='6df5200d0945ee3b050bcde4115a338e392017b1'
BASE='4994caa15533c0cf84c07208c9097e4197f2815b'
OLD='273d2748d01ed978b4e4fb56c8ee503b18143bc4'
def sha(data):return hashlib.sha256(data).hexdigest()
def load(n):return json.loads((OUT/n).read_text())
def save(n,v):(OUT/n).write_text(json.dumps(v,indent=2)+'\n')
def git(*args):return subprocess.check_output(['/usr/bin/git',*args],cwd=ROOT)
assert git('rev-parse','HEAD').decode().strip()==HEAD and not git('status','--porcelain')
p=load('provenance.json');a=load('application-results.json');initial=load('application-results-initial.json')
b=load('boundary-results.json');paired=load('paired-results.json')
assert all(sha((ROOT/n).read_bytes())==v for n,v in p['changed_file_sha256'].items())
assert (a['total'],a['expectations_met'])==(67,64) and (initial['total'],initial['expectations_met'])==(67,63)
assert (b['total'],b['passed'])==(36,36) and (paired['total'],paired['expectations_met'])==(5,4)
assert set(a['failed_cases'])=={'R4-title-literal','action-share-full','action-share-prefix'}
def case(name):return next(x for x in a['records'] if x['case']['id']==name)
title=case('R4-title-literal');share=case('action-share-full')
def observed(row):
    return {'request':row['case']['prompt'],'intent_sources':row['case']['intent_response']['sources'],
        'local_validation':row['validation'],'standalone_planner':row['planner'],
        'classification':row['classification'],'instruction_text':row['instruction_text'],
        'outbound_verb':row['outbound_verb'],'observed_calls':row['run']['executed_calls'],
        'actual_route':next((x for x in row['run']['events'] if x.get('type')=='routed'),None)}
findings=[
 {'id':'R4','priority':'P2','blocking':True,'title':'Capitalized search title still becomes a delivery recipient',
  'locations':[{'file':'service/router/web_request.py','start':521,'end':527},
    {'file':'service/router/web_request.py','start':558,'end':562},
    {'file':'service/router/router.py','start':6967,'end':6974}],
  **observed(title),'expected_calls':title['case']['expected_calls'],'offered_tools':title['run']['offered_tools'],
  'description':'The new coordinated read guard invokes _terminal_destination on raw query text. Its capitalized-complement heuristic considers Guide a complete topic and Gardening an addressee, grants send_email metadata, and bypasses structured planning. The shared instruction mask and workflow helper correctly recognize a read; standalone planning compiles both strict queries.',
  'required_behavior':'Bind destinations outside the complete read literal. Capitalization alone must not establish effect authority; unresolved attachment may clarify without authorizing delivery. Retain explicit personal/address recipients and genuine later actions.',
  'controls':['R4-original','R4-class-named','R4-class-containing','R4-class-about','R4-title-quoted','action-outer-destination','action-literal-address','action-later-send'],
  'limits':'Six legacy email tools offered, not send_email; no calls or sends occur. Metadata/planner bypass proven; no executable outbound expansion or real-model failure rate claimed.',
  'evidence':{'file':'application-results.json','case':'R4-title-literal','paired':'R4-title-quoted'}},
 {'id':'R7','priority':'P2','blocking':True,'title':'Shared literal mask consumes genuine later share instruction',
  'locations':[{'file':'service/router/intent/validation.py','start':249,'end':270},
    {'file':'service/workflows/compiler.py','start':71,'end':80}],**observed(share),
  'description':'Later-action bounds omit share, already present in workflow outbound vocabulary. The new shared mask swallows cedar maps and share it with Mom; outbound_verb becomes false, applicable_read true, and actual main executes a pure notes lookup with the action tail inside the query. Supplying only cedar maps instead fails completeness and clarifies via read interpretation.',
  'required_behavior':'Preserve complete established action vocabulary at clause boundaries or decline structured admission conservatively. Do not validate genuine action tails as lookup text. Keep quoted share text as data.',
  'controls':['action-lookup-send','action-lookup-forward','later-share-with-channel','literal-share-then-send','quoted-share-data'],
  'limits':'Only a registered fake read executes. No real share, communication, native body or permission bypass observed; wrong query semantics and loss of action guard are proven.',
  'regression_evidence':'Pure prior masking-stage expressions detect outbound share on this input; current helper does not. No old application/script executed or receipt transferred.',
  'evidence':{'file':'application-results.json','cases':['action-share-full','action-share-prefix'],'paired_file':'paired-results.json','paired_case':'later-share-without-channel'}}]
now=datetime.now(timezone.utc).isoformat()
rp=Path('/private/tmp/wisp-router-update-6df5200-gate-receipt.json')
receipt_bytes=rp.read_bytes();receipt=json.loads(receipt_bytes)
log=Path(receipt['log']);log_bytes=log.read_bytes()
assert receipt['exact_sha']==HEAD and receipt['status']=='PASS' and receipt['passed_modules']==receipt['total_modules']==179
assert sha(log_bytes)==receipt['log_sha256']=='d7683188e2e2a2cb4a72e178031aed79a50bfc0db9ac1ded3f61fbbe4d7d7102'
(OUT/'parent-gate-receipt.json').write_bytes(receipt_bytes)
gate={'status':'PASS','exact_sha':HEAD,'passed_modules':179,'total_modules':179,'exit_code':0,
    'receipt':'parent-gate-receipt.json','external_log':str(log),'verified_log_sha256':sha(log_bytes),
    'completed_at_utc':receipt['completed_at_utc'],'full_gate_run_by_auditor':False}
p['final_source_verification']={'exact_sha':HEAD,'tree_clean':True,'all_changed_file_hashes_match':True,'observed_at_utc':now}
p['parent_mechanical_gate']=gate
save('provenance.json',p)
fixture_note={'initial_evidence':'application-results-initial.json','initial_log':'application-command-initial.log',
    'initial_expectations_met':63,'corrected_expectations_met':64,'total':67,
    'correction':'Generic send it to Mom did not request an email channel. Its correct channel-null delivery cannot require send_email metadata. Corrected only auditor expected-effects field to require action guard without guessing a channel, reran bounded batch, and preserved initial raw evidence. No candidate/corpus/gold/scorer/raw overwrite.',
    'report_write_retry':'First large final-report apply_patch call stalled without writing a file and was terminated (cell103); smaller writes completed. No candidate process or full gate was stopped.'}
save('auditor-fixture-correction.json',fixture_note)
reassessment=[
 {'id':'R4','status':'PARTIAL','details':'Original lowercase road to recovery and named/containing/about variants work; quoted capitalized title works. Capitalization variant remains blocking.'},
 {'id':'R5','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Bare/mixed Recap email, Review/Inspect/Scan/Browse and recap text/message execute reads; real delivery/draft/later action guards remain.'},
 {'id':'R6','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Complete unquoted source/action literals grant only notes authority. Missing/truncated/extra sources reject; raw exclusions visible and quoted exclusions data. Contextual domain/independent source authority and notes-only standalone admission work.'},
 {'id':'R3_R1_R2_ALIASES','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Mixed and one-filter repeated reads work. Correct/reversed tuples and dedupe work; query/date/count/account/unread/operation swaps and omission reject. New source-free query replaces stale text with inherited count/date; calendar/inbox aliases work. Prior completed account/unread/ambiguity/messages alias coverage retained through unchanged context/alias code review.'},
 {'id':'F1_F2_F3','status':'ORIGINAL_REPRODUCERS_RESOLVED','details':'Date/reminder scope/duration omissions and unsupported daypart reject; valid60minute slot and calendar-only tomorrow shortcut work. Shortcut unused empty intent is not separately validator-valid.'},
 {'id':'G1','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Actual repaired test passes three fixed clocks with timestamps/positions/cardinality/cache/text/model/debug/four summaries. Injected private body fails new textual assertion; production redaction unchanged.'},
 {'id':'F4_P3_UTC','status':'RESOLVED_OR_MITIGATED','details':'Six modules admitted and unknown/missing failclosed. Typed supplemental scorer rejects bool/float equality caveat; legacy scores unchanged. UTC/Pacific23h/25h DST/restoration/missing-tzset controls pass.'}]
limits=[
 'No real model/status/endpoint/inference/native/user data/settings/credentials/outbound/network effects, training/residency/configuration, installed-app change, deploy or merge. Guard installed before service imports; registered callables all synthetic.',
 'Heldout/dev corpus/author/raw archive and replay payloads opaque: bytes/hashes and permitted metadata only. No archived script executed or raw cases supplied to repair authors.',
 'No duplicate full regression or DEV run. Exact parent179/179PASS receipt/log verified; prior pass/fail/BLOCK stages preserved and do not transfer. RequiredCI stillrunning per parent; no independent terminal pass receipt.',
 'Separate Simulation QA held by BLOCK. Separate LiveQA/performance/protected integration/shipping gates remain where required.',
 'Planner default-disabled and resident resource-window ACK pending. Actual model95%/strict-JSON/latency/training/generalization/activation unmeasured.',
 'BuilderDEV80/80exact/source48/48firstcall72/80original/guardedE2E16/16failurehonesty is scripted metadata with annotation limits; zero-applicable answer-fact/query guards prove neither. Defaultoff control retained separately.',
 'Correlated synthetic probes are not accuracy samples. Genuine actions tested for guard/classification/absence of unintended calls, not real delivery approval execution.',
 'Notes-only domain restriction tested at standalone planner; main fixture uses standard all-domain fake config. No production config changed.',
 'One auditor expectation correction and initial raw evidence retained explicitly; generic send without channel cannot imply email-specific effect.'
]
report={'schema_version':1,'role':'independent read-only Wisp Release Auditor','independent_of':['builder/integrator','repair authors','Simulation QA actor'],
 'verdict':'BLOCK','exact_sha':HEAD,'base_sha':BASE,'previous_audit_sha':OLD,'branch':'codex/router-update-1-3',
 'pr':'https://github.com/adjad/wisp/pull/160','checkout':str(ROOT),'output_directory':str(OUT),'recorded_at_utc':now,
 'scope':{'complete_changed_files':181,'delta_since_previous_audit':49,
   'method':'Complete intended current base-to-head scope assessed through prior full source/schema/harness/overview reviews for unchanged code plus full current production/tests/docs/progress delta and relevant execution context. All181 paths rehashed; opaque corpus/author/archive treatment explicitly required.',
   'production_delta':['service/router/intent/validation.py','service/router/web_request.py','service/workflows/compiler.py'],
   'test_delta':['tests/test_router_intent_core.py','tests/test_router_intent_main.py'],
   'areas':['source/literal/context/exclusion authority','query/date/count/account/operation occurrence pairing','workflow/read/outbound classification','schema/compiler/main/runtime','identity/transport/residency/defaultoff/repair/deadline/cancellation','privacy and overview grounding/partial attribution','scorer/manifest/UTC','opaque provenance/activation limits'],
   'paths_and_hashes':'provenance.json'},
 'findings':findings,'prior_finding_reassessment':reassessment,
 'independent_execution':{'application':{'total':67,'expectations_met':64,'failed_cases':a['failed_cases']},
   'paired':{'total':5,'expectations_met':4,'failed_cases':paired['failed_cases']},'boundary':{'total':36,'passed':36},
   'fixture_correction':fixture_note,'actual_model_calls':False,'native_or_outbound_execution':False,'heldout_payload_opened':False,
   'commands':['PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python '+str(OUT/n) for n in ('application_controls.py','paired_controls.py','boundary_controls.py')]+['PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 '+str(OUT/'provenance_checks.py')],
   'evidence':['application-results-initial.json','application-results.json','paired-results.json','boundary-results.json','provenance.json']},
 'parent_mechanical_gate':gate,'ci':'Required jobs running; no independently inspected current terminal PASS receipts',
 'provenance_summary':{'head_clean_at_completion':True,'changed_files':181,'replay_bundles_verified':9,'archive_entries_verified':102,
   'opaque_corpus_bytes_and_seals_unchanged':True,'registered_scope_and_handoff_pins_verified':True,'candidate_source_edits':False},
 'failures_skips_limits':limits,
 'handoff':'Parent reads files. Orchestrator must record one repair owner perR4/R7 before edits; newfreeze/ACK/review required. No QA release/activation/merge/ship approval.'}
save('REPORT.json',report)
assert (OUT/'REPORT.md').exists()
save('HANDOFF.json',{'schema_version':1,'verdict':'BLOCK','exact_sha':HEAD,'base_sha':BASE,
    'branch':report['branch'],'pr':report['pr'],'recorded_at_utc':now,'blocking_findings':['R4','R7'],
    'previous_R4_reproducer_closed':True,'R5_closed':True,'R6_closed':True,'parent_mechanical_gate':'PASS179/179',
    'required_ci':'No terminal PASS independently inspected','report':'REPORT.md','structured_report':'REPORT.json',
    'provenance':'provenance.json','checksums':'checksums.json','candidate_source_edits':False,'tree_clean':True,
    'simulation_qa_release':False,'activation_or_shipping_approval':False,
    'next_step':'Orchestrator records sole repair owners forR4/R7 before edits; newfreeze/ACK/review. Parent reads final; no cross-chat response required.'})
checks={file.name:sha(file.read_bytes()) for file in sorted(OUT.iterdir()) if file.is_file() and file.name!='checksums.json'}
save('checksums.json',{'schema_version':1,'exact_sha':HEAD,'recorded_at_utc':now,'algorithm':'SHA256','files':checks,'excludes_self':True})
assert all(sha((OUT/n).read_bytes())==v for n,v in checks.items())
print(json.dumps({'verdict':'BLOCK','head':HEAD,'findings':['R4','R7'],'sealed_files':len(checks),
    'report_sha256':checks['REPORT.md'],'handoff_sha256':checks['HANDOFF.json'],'clean':True,'mechanical':'PASS179/179'}))
