"""Author and seal fourth-candidate independent audit; source stays read-only."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-273d274-gates-20261005/auditor')
HEAD='273d2748d01ed978b4e4fb56c8ee503b18143bc4'
BASE='4994caa15533c0cf84c07208c9097e4197f2815b'
OLD='27490ae5055502423f187e84ca52d0064b794396'
def sha(data):return hashlib.sha256(data).hexdigest()
def git(*args):return subprocess.check_output(['/usr/bin/git',*args],cwd=ROOT)
def load(name):return json.loads((OUT/name).read_text())
def save(name,value):(OUT/name).write_text(json.dumps(value,indent=2)+'\n')
assert git('rev-parse','HEAD').decode().strip()==HEAD and not git('status','--porcelain')
p=load('provenance.json')
assert git('diff','--name-only',BASE,HEAD).decode().splitlines()==p['complete_changed_files']
assert all(sha((ROOT/n).read_bytes())==h for n,h in p['changed_file_sha256'].items())
a=load('application-results.json');b=load('boundary-results.json');f=load('followup-results.json')
assert (a['total'],a['expectations_met'])==(53,51)
assert (b['total'],b['passed'])==(33,33)
assert (f['total'],f['expectations_met'])==(8,5)
assert set(a['failed_cases'])=={'R3-overview-plus-lookup','literal-source-unquoted-positive'}
assert set(f['failed_cases'])=={'bare-email-recap','unquoted-source-data','unquoted-preposition-data'}
def case(dataset,name):return next(r for r in dataset['records'] if r['case']['id']==name)
recap=case(f,'bare-email-recap');literal=case(a,'literal-source-unquoted-positive');relation=case(f,'unquoted-preposition-data')
receipt_path=Path('/private/tmp/wisp-router-update-273d274-gate-receipt.json')
receipt_bytes=receipt_path.read_bytes();receipt=json.loads(receipt_bytes)
assert receipt['exact_sha']==HEAD and receipt['status']=='PASS' and receipt['passed_modules']==receipt['total_modules']==179
log_path=Path(receipt['log']);log_bytes=log_path.read_bytes()
assert sha(log_bytes)==receipt['log_sha256']=='b7c4b99573d6285388493808469f29214ec5f460f089cf81405df9a40f0a4c4c'
(OUT/'parent-gate-receipt.json').write_bytes(receipt_bytes)
now=datetime.now(timezone.utc).isoformat()
p['final_source_verification']={'exact_sha':HEAD,'tree_clean':True,'all_changed_file_hashes_match':True,'observed_at_utc':now}
p['parent_gate_receipt']={'source_path':str(receipt_path),'sha256':sha(receipt_bytes),
    'status':'PASS','passed_modules':179,'total_modules':179,'external_log_sha256_verified':sha(log_bytes)}
save('provenance.json',p)
fixture_repairs=[
    {'command':'boundary_controls.py initial run','exit_code':1,
     'diagnosis':'Pytest prohibits calling a decorated fixture directly; fresh auditor setup called t.isolated(mp).',
     'repair':'Only auditor-owned script changed to call the fixture underlying __wrapped__ setup with MonkeyPatch context; targeted controls then ran33/33. No candidate/test edits.'},
    {'command':'provenance_checks.py initial run','exit_code':1,
     'diagnosis':'Fresh verifier expected flat replay checksum keys; two newest bundles use per-file stored_sha256/original_sha256 records.',
     'repair':'Only auditor-owned verifier added declared per-file schema support; all eight replay and66 archive entry hashes then verified. No raw payload repair or source edit.'}
]
save('auditor-fixture-repairs.json',fixture_repairs)
findings=[
 {'id':'R4','priority':'P2','blocking':True,'status':'ORIGINAL_REPRODUCER_CLOSED_REPAIR_INCOMPLETE',
  'title':'Do not reinterpret a to preposition inside an unquoted email search as a delivery recipient',
  'locations':[{'file':'service/router/web_request.py','start':553,'end':557},
    {'file':'service/router/web_request.py','start':471,'end':474},
    {'file':'service/router/router.py','start':6967,'end':6974}],
  'request':relation['case']['prompt'],'intent_sources':relation['case']['intent_response']['sources'],
  'expected_calls':relation['case']['expected_calls'],'local_validation':relation['validation'],
  'standalone_planner':relation['planner'],'classification':relation['classification'],
  'actual_main_route':next(ev for ev in relation['run']['events'] if ev.get('type')=='routed'),
  'offered_tools':relation['run']['offered_tools'],'observed_calls':relation['run']['executed_calls'],
  'description':'The new coordinated-read helper rejects any to token in the entire unquoted complement. In email about road to recovery, to belongs to the literal search, yet _delivery treats recovery as a recipient and classifies send_email authority. The valid standalone intent compiles two strict reads, but actual main.agent suppresses structured planning and returns to the broad legacy email lookup menu.',
  'required_behavior':'Bind delivery destinations and payload relations structurally, after excluding the bounded read query. Preserve genuine email from Orion to Mom and later send clauses; do not classify arbitrary query prepositions as an addressee.',
  'positive_controls':['R4-coordinated-positive','R4-quoted-positive','quoted-preposition-data'],
  'genuine_action_controls':['action-email-addressee','action-explicit-destination','action-pronoun-destination','action-text-addressee','action-draft-destination','action-later-delivery'],
  'impact_limits':'Incorrect effect metadata and structured-planner bypass are proven. The actual fallback exposed six legacy email tools, not send_email, and executed no tool. No unauthorized send or executable outbound capability expansion is claimed. The canned fallback answer is not actual-model failure-rate evidence.',
  'evidence':{'file':'followup-results.json','case':'unquoted-preposition-data','paired_control':'quoted-preposition-data'}},
 {'id':'R5','priority':'P2','blocking':True,
  'title':'Keep ordinary email recap requests out of the delivery workflow preflight',
  'locations':[{'file':'service/workflows/compiler.py','start':43,'end':48},
    {'file':'service/workflows/compiler.py','start':2483,'end':2484},
    {'file':'service/workflows/engine.py','start':155,'end':158},
    {'file':'service/main.py','start':1492,'end':1510}],
  'request':recap['case']['prompt'],'expected_calls':recap['case']['expected_calls'],
  'local_validation':recap['validation'],'standalone_planner':recap['planner'],
  'classification':recap['classification'],'outbound_verb':recap['outbound_verb'],'workflow':recap['workflow'],
  'observed_calls':recap['run']['executed_calls'],'actual_answer':recap['run']['answer'],
  'description':'The workflow email-noun mask supports summarize/read but omits recap. Recap email is accepted as a supported overview by the intent validator/planner and has no web-request effects, yet outbound_verb returns true and compile_new creates a deliver_summary waiting_for_content plan. Main preflight emits a delivery question and returns before routing or model interpretation. Recap email; find notes named harbor sketches loses both supported reads in the same way.',
  'required_behavior':'Recognize the recap head as a source read in workflow admission, consistently with the structured grammar and web classifier; keep real recipients/delivery verbs and later action clauses visible.',
  'positive_controls':['possessive-email-recap','summarize-email-mixed','possessive-recap-mixed'],
  'impact_limits':'No source read or send occurs. This is deterministic workflow preemption and an inappropriate delivery prompt, not a real outbound effect. The defect is in full intended execution scope even though its noun mask is unchanged in the latest repair delta.',
  'evidence':{'file':'followup-results.json','case':'bare-email-recap','additional_file':'application-results.json','additional_case':'R3-overview-plus-lookup'}},
 {'id':'R6','priority':'P2','blocking':True,
  'title':'Compute source authority from instruction text before requiring domains found inside a search literal',
  'locations':[{'file':'service/router/intent/validation.py','start':593,'end':593},
    {'file':'service/router/intent/validation.py','start':683,'end':693}],
  'request':literal['case']['prompt'],'intent_sources':literal['case']['intent_response']['sources'],
  'expected_calls':literal['case']['expected_calls'],'local_validation':literal['validation'],
  'standalone_planner':literal['planner'],'observed_calls':literal['run']['executed_calls'],
  'description':'source_requirements(prompt) masks quoted text but treats calendar inside the recognized unquoted notes query my calendar as a requested domain. A correct notes-only intent fails Missing requested source before the newly added instruction-text authority check. Adding calendar satisfies the earlier domain requirement but the new check correctly rejects that literal-derived extra domain, leaving no valid representation of the supported query. Find notes about our messages behaves identically.',
  'required_behavior':'Use the same bounded instruction/literal distinction for required-source and extra-source authority. Permit the complete notes-only query; continue rejecting model-invented additional reads and preserving genuine separately requested calendar/messages clauses.',
  'positive_controls':['literal-source-quoted-positive','quoted-source-data','R3-mixed-positive'],
  'negative_controls':['literal-source-extra-rejected','R3-missing','R3-truncated','R1-swap-query'],
  'impact_limits':'The new authority check prevents literal-derived extra reads. This finding is safe but deterministic over-abstention for unquoted queries; no privacy expansion or native read was observed.',
  'evidence':{'file':'application-results.json','case':'literal-source-unquoted-positive','additional_file':'followup-results.json','additional_case':'unquoted-source-data'}}
]
reassessment=[
 {'id':'R3','status':'RESOLVED_FOR_ORIGINAL_AND_FRESH_SCOPE','details':'Mixed calendar/notes and reversed ordering compile correctly. Repeated notes with one unfiltered and one filtered occurrence compile; missing/truncated/invented filters, date swaps and misbound repeated queries reject. R5 is a distinct application preflight gap for overview wording.'},
 {'id':'R4','status':'ORIGINAL_REPRODUCER_RESOLVED_BUT_PARTIAL','details':'Original two-sender shared-date, quoted variants and mixed notes/email nouns work. True delivery and later effect clauses keep the read-planner guard; quoted/negated action controls grant no effects. A bounded unquoted query containing to still misclassifies delivery: current blocking R4.'},
 {'id':'G1','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Production redaction code unchanged. Actual repaired target test passes clean/1234/5678 fixed clocks; exact timestamps, positions, two occurrences, selected cache rows and four summaries preserve text/model/debug privacy assertions. Injecting a common private body into redact_summary_codes causes the new textual assertion to fail, proving the narrowed oracle still detects leaks.'},
 {'id':'R1','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Correct/reversed whole tuples execute; sender/date/account/count/unread/operation swaps and missing occurrences reject; exact duplicate dedupe works.'},
 {'id':'R2','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'New source-free query executes with inherited date/count/account/unread where requested; stale response and multi-source ambiguous context reject.'},
 {'id':'ALIASES','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'Natural calendar/appointments, inbox/email, texts/messages aliases work; unclear independent date bounds clarify.'},
 {'id':'F1_F2_F3','status':'ORIGINAL_REPRODUCERS_RESOLVED','details':'Date/reminder scope/duration omissions, truncated query and unsupported daypart reject. Supported90-minute free slot executes; actual tomorrow shortcut is calendar-only (its synthetic unused empty intent is not separately validator-valid).'},
 {'id':'F4','status':'RESOLVED','details':'All six added test modules explicitly admitted, missing/unknown entries fail closed.'},
 {'id':'P3','status':'MITIGATED_LEGACY_LIMIT_RETAINED','details':'Original Python numeric equality remains disclosed. Supplemental typed request_argument and guarded_end_to_end reject bool/float count mismatch.'},
 {'id':'UTC','status':'RESOLVED_FOR_AUDITED_SCOPE','details':'UTC/Pacific hosts match exact23h/25h DST bounds, distinguish wrong days, restore absent/exceptional TZ and fail closed without tzset.'}
]
limits=[
 'Real model inference/status/endpoints, accuracy/95% targets, strict JSON support, latency and actual model generalization remain unmeasured. Scripted oracle replies do not establish activation readiness.',
 'No real native tool bodies, personal data/settings/credentials, communications, external networking, training/model residency/configuration changes, installed-app relaunch/replacement, deployment or merge. Guard installed before service imports; application calls use registered fake interfaces.',
 'Heldout test.jsonl, dev.jsonl and corpus author script stayed opaque bytes; raw replay/archive payloads only hashed/decompressed as bytes, never parsed or fed to repair authors. No archived script executed.',
 'No duplicate full regression or DEV harness rerun. Parent once-only179/179PASS verified by exact receipt and complete log hash; prior27490ae178/179FAIL and auditBLOCK preserved and do not transfer.',
 'Required CI observed still running in parent receipt; no current independent passing CI receipt inspected. A passing local gate does not approve audit defects.',
 'Separate Simulation QA remains held by BLOCK. Separate Live QA/performance/protected integration/ship gates remain where required.',
 'Enabled builder DEV80/80source/exact,48/48first-call,72/80original/guarded E2E and16/16failure honesty remain scripted metadata. Default-off control is distinct and preserved (78/80source/exact,70/80E2E); neither is model quality. Zero applicable answer-fact/query-guard denominators prove neither property.',
 'Planner remains default-disabled with external resident-window dependency pending. This report grants no activation, model loading, merge or shipping authority.',
 'No blanket action/helper PASS inferred from no-call scripts. Genuine actions were classification/planner-guard controls; native/outbound execution and installed UI were not tested. Known fallback no-call output is fixture behavior, not real-model failure incidence.'
]
report={'schema_version':1,'role':'independent read-only Wisp Release Auditor',
    'independent_of':['builder/integrator','repair authors','Simulation QA actor'],
    'verdict':'BLOCK','exact_sha':HEAD,'base_sha':BASE,'previous_audit_sha':OLD,
    'branch':'codex/router-update-1-3','pr':'https://github.com/adjad/wisp/pull/160',
    'checkout':str(ROOT),'output_directory':str(OUT),'recorded_at_utc':now,
    'scope':{'complete_changed_files':141,'delta_since_previous_audit':49,
      'method':'Complete current intended base-to-head scope assessed using prior completed full source/context reviews for unchanged files plus full current production/test/docs/progress delta and relevant preflight/validator/router context. All141 changed files rehashed; opaque corpus/author/replay/archive handling is an explicit scope restriction.',
      'production_delta':['service/router/intent/validation.py','service/router/web_request.py'],
      'test_delta':['tests/test_message_digest.py','tests/test_router_intent_core.py','tests/test_router_intent_main.py'],
      'areas':['source/query/time/count/account occurrence authority','context correction and aliases','read/outbound workflow preflight and classifier','schema/compiler/runtime identity and exclusions','deadline/cancellation/defaultoff','multi-source grounding/coverage/rendering','G1 privacy test oracle','scorer/timezone/manifest','opaque provenance and activation limits'],
      'changed_files_and_hashes':'provenance.json'},
    'findings':findings,'prior_finding_reassessment':reassessment,
    'independent_execution':{'application':{'total':53,'expectations_met':51,'failed_cases':a['failed_cases']},
      'paired_followup':{'total':8,'expectations_met':5,'failed_cases':f['failed_cases']},
      'boundary':{'total':33,'passed':33,'failed':[]},
      'actual_model_calls':False,'native_or_outbound_execution':False,'heldout_payload_opened':False,
      'auditor_fixture_repairs':fixture_repairs,
      'commands':['PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python '+str(OUT/n)
          for n in ('application_controls.py','boundary_controls.py','followup_controls.py')]+[
            'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 '+str(OUT/'provenance_checks.py')],
      'raw_evidence':['application-results.json','boundary-results.json','followup-results.json','provenance.json']},
    'parent_mechanical_gate':{'status':'PASS','modules':179,'total_modules':179,'exit_code':0,
      'exact_sha':HEAD,'receipt':'parent-gate-receipt.json','external_log':str(log_path),'verified_log_sha256':sha(log_bytes),
      'completed_at_utc':receipt['completed_at_utc'],'full_gate_run_by_auditor':False},
    'provenance_summary':{'head_clean_at_completion':True,'changed_files':141,'replay_bundles_verified':8,
      'archive_entries_verified':66,'opaque_seals_and_corpus_bytes_unchanged':True,
      'exact_registered_scope_and_handoff_pins_verified':True,'candidate_source_edits':False},
    'failures_skips_limits':limits,
    'handoff':'Parent reads formal files; route current R4/R5/R6 to Orchestrator for one recorded repair owner each before editing. Read-only diagnosis reservation is not edit authority. New repairs require new freeze/ACK and independent exact-SHA review. No QA release, activation or merge approval from this BLOCK.'}
save('REPORT.json',report)
text=f'''# Wisp PR #160 — fourth independent audit

**Verdict: BLOCK** at `{HEAD}`, base `{BASE}`, branch `codex/router-update-1-3`. Recorded {now}.

The original mixed-source lookup defect and two-sender email reproducer are repaired. The privacy-test timestamp collision repair preserves its content/model/debug assertions and passes the three fixed clocks. The parent full gate passes179/179. Fresh independent positives still reveal three blocking execution gaps.

## Blocking findings

### R4 — P2 — unquoted query preposition becomes a delivery recipient

Request: **Read email from Lyra and email about road to recovery**.

The correct intent and standalone planner compile strict email reads for `Lyra` and `road to recovery`. The new helper at `service/router/web_request.py:553–557` rejects a coordinated read noun whenever its whole unquoted complement contains `to`. `_delivery` then treats `recovery` as a recipient and grants `send_email` metadata. `service/router/router.py:6967–6974` suppresses the structured planner, and actual `main.agent` falls back to `rules / email lookup -> scoped tools (6)`.

Quoting `"road to recovery"` makes the same two reads execute correctly. Original shared-date sender requests, quoted sender variants and genuine addressee/later-action controls pass. Repair the boundary between a bounded search literal and a destination relation; retain genuine `email from Orion to Mom` and later `send` actions.

No send occurred. The observed fallback exposed six legacy email tools, **not** `send_email`; no executable outbound expansion is claimed. The defect is incorrect action metadata and bypass of structured read validation. Evidence: `followup-results.json`, `unquoted-preposition-data` and paired `quoted-preposition-data`.

### R5 — P2 — email recap is intercepted by a delivery workflow

**Recap email** is a supported overview: the validator accepts it, standalone planning compiles `summarize_emails`, and the web classifier grants no effects. However, the workflow noun mask at `service/workflows/compiler.py:43–48` omits `recap`; `outbound_verb` returns true. The typed preflight creates a `deliver_summary / waiting_for_content` workflow with no recipient/channel, emits “I couldn't tell exactly what to send”, and returns before routing (`service/main.py:1492–1510`). No intent generation or source read runs.

**Recap email; find notes named harbor sketches** loses both supported reads. Paired **Recap my email**, **Recap my email; find notes …**, and **Summarize email; find notes …** execute correctly. Recognize the read head consistently in workflow admission without hiding real delivery clauses. No actual outbound effect is alleged. This is a full execution-scope defect, although the mask is unchanged in the newest repair delta.

Evidence: `application-results.json`, `R3-overview-plus-lookup`; `followup-results.json`, `bare-email-recap` and its paired controls.

### R6 — P2 — unquoted search data remains required source authority

**Find notes about my calendar**, supplied with the complete notes query `my calendar`, fails “Missing requested source”. `source_requirements(prompt)` at `validation.py:593` treats the query word `calendar` as a second required domain; lines683–685 reject the correct notes-only intent before the new instruction-text check. Adding calendar satisfies that earlier condition but the new check at691–693 correctly denies the literal-derived extra read. No valid representation remains for this supported query.

**Find notes about our messages** has the same failure. Quoting either literal executes a single exact notes lookup. Derive required and extra-source authority from the same bounded instruction/literal distinction. Keep literal-derived extra-read rejection and genuine mixed-source authority checks.

This is deterministic over-abstention, with no extra source read or privacy expansion observed. Evidence: `application-results.json`, `literal-source-unquoted-positive`, `literal-source-quoted-positive`, `literal-source-extra-rejected`; `followup-results.json`, `unquoted-source-data` and `quoted-source-data`.

## Repair assessment and independent evidence

- R3 original and fresh scope: calendar/notes in either order and repeated notes with only one filtered occurrence execute. Missing, truncated, invented and misbound filters/date swaps reject.
- R4 original reproducer: natural and quoted two-sender shared-date reads execute; explicit second read retains its own date. Genuine recipient, draft, message and later delivery clauses keep the planner guard; quoted/negated action controls grant no effects. The new unquoted-preposition variant above remains blocking.
- G1: actual repaired target test passes clean,1234-collision and5678-collision clocks. It preserves exact timestamps/source positions/two occurrences, cache selection and four output summaries with original model/debug/text privacy checks. A synthetic injected private body fails the new textual assertion. Production redaction code is unchanged.
- R1/R2 and aliases: whole tuple swaps/omissions reject; valid order/duplicate handling works. Current source-free query replaces stale text while retaining valid inherited constraints; ambiguous context clarifies. Natural calendar/inbox/messages aliases work.
- Original omissions/shortcut: date, reminder scope, duration, truncated query and unsupported daypart reject. A valid90-minute slot runs. Actual tomorrow shortcut remains calendar-only; its unused empty synthetic intent is not separately validator-valid.
- Fresh identity/runtime: config/defaultoff/kill switch/wrong origin/provider/role/transport and nonresident guards; one overall deadline, outer cancellation propagation and bounded clean-context repair pass.
- Manifest/scorer/timezone: six modules admitted, unknown/missing fail closed; typed supplemental numeric mismatch rejection retains disclosed legacy equality; UTC/Pacific23h/25h DST and restoration/fail-closed controls pass.
- Fresh overview helpers preserve source counts, duplicate attribution, actor direction, partial coverage and independent merged results.

Actual-main plus standalone-planner batch: **51/53** expectations met. Followup paired controls: **5/8**; these repeat affected families to establish causes, not independent accuracy samples. Boundary/privacy/overview helpers: **33/33**. The five failed positive cases correspond to the three findings. Raw inputs, supplied fake replies, SSE routes, schemas and registered fake calls are preserved. No genuine source bodies or real model ran.

## Mechanical gate and provenance

The parent once-only full regression passes **179/179 modules**, exit0, on this exact SHA. Receipt `parent-gate-receipt.json`; complete external log `{log_path}`, independently verified SHA256 `{sha(log_bytes)}`. Completed at {receipt['completed_at_utc']}. The auditor did not duplicate this gate or rerun DEV. Required CI was still running in the parent receipt; no independent current passing receipt inspected.

Complete intended **141-path** base-to-head scope assessed through prior completed full reviews plus the full **49-path** delta/context. New production changes are `validation.py` and `web_request.py`; the privacy repair changes one test file. Final clean HEAD and every changed-file hash verified. `provenance.json` retains exact registered assignment/handoff pins, full paths/hashes, eight replay bundles and66 preserved archive entry checks. Opaque corpus/author bytes are unchanged.

Two auditor setup errors are explicitly recorded in `auditor-fixture-repairs.json`: direct invocation of a decorated pytest fixture, corrected to its underlying setup; and a verifier expecting flat checksum keys, extended to the declared nested checksum schema. Both repaired scripts completed successfully. Only auditor-owned files changed; candidate and archived evidence stayed immutable.

## Limits and handoff

Heldout/corpus author/raw replay/archive payloads stayed opaque; hashes/metadata only, no archived script execution. No real models/endpoints/native/user data/settings/credentials/outbound/network effects, training/residency/activation, installed-app changes, deployment or merge. Scripted DEV80/80source/exact and72/80E2E are stage evidence with unchanged annotation limits, not model quality; zero-applicable measures do not pass a property. The default-off control is separately preserved. Planner default-disabled/resource limits persist.

The local pass does not override these findings. Distinct Simulation QA remains held. Parent reads the formal files; route R4/R5/R6 to the Orchestrator for one recorded repair owner each before edits. A read-only diagnosis reservation is not edit authority. Repairs require a new frozen candidate, ACK and independent re-review. No release, activation or merge approval follows from this BLOCK.
'''
(OUT/'REPORT.md').write_text(text)
save('HANDOFF.json',{'schema_version':1,'verdict':'BLOCK','exact_sha':HEAD,'base_sha':BASE,
    'branch':report['branch'],'pr':report['pr'],'recorded_at_utc':now,
    'blocking_findings':['R4','R5','R6'],'original_R3_closed':True,'original_R4_reproducer_closed':True,'G1_closed':True,
    'parent_full_gate':'PASS179/179','required_ci':'No current passing receipt independently inspected',
    'report':'REPORT.md','structured_report':'REPORT.json','provenance':'provenance.json','checksums':'checksums.json',
    'candidate_source_edits':False,'tree_clean':True,'simulation_qa_release':False,'activation_or_shipping_approval':False,
    'next_step':'Parent routes findings to Orchestrator for sole-owner edit ACKs, new freeze and independent re-review; preserve current failed positive controls. Parent reads final, no cross-chat response required.'})
checks={file.name:sha(file.read_bytes()) for file in sorted(OUT.iterdir()) if file.is_file() and file.name!='checksums.json'}
save('checksums.json',{'schema_version':1,'exact_sha':HEAD,'recorded_at_utc':now,'algorithm':'SHA256','files':checks,
    'excludes_self':True,'external_regression_log':{'path':str(log_path),'sha256':sha(log_bytes)}})
assert all(sha((OUT/name).read_bytes())==wanted for name,wanted in checks.items())
print(json.dumps({'verdict':'BLOCK','head':HEAD,'findings':['R4','R5','R6'],'sealed_files':len(checks),
    'report_sha256':checks['REPORT.md'],'handoff_sha256':checks['HANDOFF.json'],'clean':True}))
