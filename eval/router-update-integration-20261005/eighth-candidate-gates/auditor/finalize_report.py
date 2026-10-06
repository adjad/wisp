from pathlib import Path
import json,hashlib,subprocess
from datetime import datetime,timezone
OUT=Path('/private/tmp/wisp-router-44babb7-gates-20261005/auditor');ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
SHA='44babb70ee2eea99897b9437b2f6d49b2d59adff';BASE='4994caa15533c0cf84c07208c9097e4197f2815b';PREVIOUS='8dfd5c9c19d8e31063a1f0d65c87d3d0d1938736'
def h(b):return hashlib.sha256(b).hexdigest()
initial=json.loads((OUT/'provenance.json').read_text());final=json.loads((OUT/'provenance-final.json').read_text())
assert initial['files']==final['files'];assert final['head']==SHA and final['passed']==final['total']==774
head=subprocess.check_output(['/usr/bin/git','rev-parse','HEAD'],cwd=ROOT).decode().strip();status=subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=ROOT).decode();assert head==SHA and status==''
a=json.loads((OUT/'application-results-initial.json').read_text());c=json.loads((OUT/'application-correction-results.json').read_text());replacement={r['id']:r for r in c['rows']};effective=[replacement.get(r['id'],r) for r in a['rows']]
assert len(effective)==484 and all(r['pass'] for r in effective)
runtime=json.loads((OUT/'runtime-results.json').read_text());boundary=json.loads((OUT/'boundary-results.json').read_text());supplemental=json.loads((OUT/'supplemental-results.json').read_text())
failures=[r for r in boundary['rows'] if not r['pass']];assert len(failures)==8 and all(r['id'].startswith('quoted-negative-task-') for r in failures)
assert runtime['passed']==runtime['total']==66 and supplemental['passed']==supplemental['total']==32
for receipt in final['ci']:assert receipt['metadata']['headSha']==SHA
finding=dict(id='R10',priority='P2',title='Unicode negation inside a quoted reminder title suppresses a supported positive task',file='service/tasks/compiler.py',lines=[20,409],introduced_by='The sole task-compiler delta broadens _NEGATED to four Unicode apostrophes; compile_reminder_complete still applies it to raw text including quoted target data.',trigger='Complete reminder called "Archive: don’t check this item"',expected='Compile reminder.complete with target exactly Archive: don’t check this item; resolve the isolated matching reminder and issue one fake complete_reminder call with literal title and singular expected_id.',actual='compile_task returns None. Actual main.agent executes zero calls, stores no task or workflow, and responds: I couldn\'t complete every requested step. Still missing one of: complete_reminder.',variants='U+2019, U+2018, U+02BC, U+FF07, each with check and delete inside the quoted title: eight independent reproductions.',isolation='On this same exact head, injecting only the previous ASCII _NEGATED regex in memory restores the exact literal target and expected fake completion call for all four Unicode forms. Four current-head ordinary Unicode names also complete the same fake call path. This is a one-line guard counterfactual, not execution or a pass claim at the old candidate.',impact='Functional regression for the already-supported Complete reminder called grammar. No unauthorized effect or private read occurred in these controls.',recommendation='Apply negation detection to quote-masked instruction text while retaining original quoted target bytes for parsing. Add positive quoted-negation target tests alongside real negative instruction and existing source-exclusion tests.',evidence=['boundary-results.json','supplemental-results.json'],ascii_limit='The analogous ASCII quoted-negation limitation predates this repair; the four newly covered Unicode forms are the demonstrated regression. The separately disclosed Check off reminder called observation is unchanged and not relied on.')
(OUT/'R10-proof.json').write_text(json.dumps(dict(candidate_sha=SHA,finding=finding,reproductions=failures,paired=[r for r in supplemental['rows'] if r['id'].startswith('R10-paired-')]),indent=2,ensure_ascii=False))
now=datetime.now(timezone.utc).isoformat()
limits=[
 'All inference/status/registered tool interfaces were synthetic. These results establish orchestration contracts, not resident Ling accuracy, hardware residency, performance, or installed-app behavior.',
 'No heldout/corpus/author/archived payload was decoded; archived evidence, including decompressed original bytes, was only hashed. No archived script executed.',
 'No real native source, user data, outbound communication, credentials, network or endpoint probe. No candidate file edits, full179 gate duplication, DEV replay, provider cancellation diagnosis or provider tests.',
 'Positive task fixtures deliberately do not update reminder readback; exact fake tool arguments and the truthful unverified-change response/stored failed task are checked. No success assertion is fabricated.',
 'Required Python and macOS CI receipts observed at this head remain in_progress, not terminal success. Saved receipts are parent-fetched metadata, not an auditor network check.',
 'The prior6df provider-cancellation failure cause and retained owner ACK/relay remain unresolved. Later passing CI does not diagnose it.',
 'Resident-Ling resource/training/activation, applicable Live QA, final-release performance, installed replacement and shipping remain separate holds. Structured planner remains default-disabled.',
 'Distinct Simulation QA remains held: this audit is BLOCK and cannot satisfy a nonblocking same-SHA audit prerequisite.'
]
coverage={
 'application_core':dict(passed=484,total=484,actual_main=373,initial=dict(passed=473,total=484),corrected_auditor_fixtures=11,corrections='fixture-corrections.json'),
 'R9':dict(passed=395,total=395,description='Five apostrophes × five source domains × read/include/check: correct intent plus omitted, added and added-without-exclusion replies; source lists/later reads, exact quoted data, negative and positive supported task controls.'),
 'R7':dict(passed=12,total=12,description='Negative delivery tails preserve exact independent Notes read and create neither workflow nor active task.'),
 'R8':dict(passed=60,total=60,description='Independent later reads/omissions and positive later effect guards; explicit later send workflow checked as authorized, not prohibited.'),
 'R1_R2_R4':dict(passed=17,total=17,description='Fresh tuple swaps/omission, inherited current-versus-stale query, governed title/action boundaries; exact fake read calls.'),
 'public':dict(passed=40,total=40,actual_main=4,description='Public update phrases, six separator × three delivery verb literal-address matrix, source freezing/contact exclusion, inherited and leading payloads, lookup/delivery revocations.'),
 'runtime_privacy_overview':dict(passed=66,total=66,description='Identity-before-I/O, residency, default-off/kill, bounded repair/context, planner-only timeout/cancellation; schema, strict Messages/legacy miss behavior, overview coverage/counts/provenance/actors/privacy; G1 three clocks plus injected leak; manifest179/drift and DST23/25h/typed scorer; process/network/write guard.'),
 'supplemental':dict(passed=32,total=32,description='Actual main calendar-only/aliases and source authority, mixed source completeness, exact dates/unread/count/account/query/duration, unsupported requests, quoted normalization rejection/context exclusion, truthful source errors and eight R10 paired controls.'),
 'R10':dict(passed=0,total=8,description='New functional regression in supported positive tasks with negation data inside Unicode quoted titles.'),
 'unique_contract_controls':dict(passed=622,total=630),
 'provenance':dict(passed=774,total=774,archive_manifests=21,archive_entries=450,replay_bundles=12,full_paths=363,delta_paths=72,all_changed_file_bytes_unchanged=True)
}
review=dict(method='Retained full intended source/schema/harness/overview review from seven earlier candidate audits, verified unchanged production paths through Git delta and byte fingerprints; fully reviewed current four-file source/test delta and task guard/parser/planner/executor plus current intent source/exclusion/literal context. Fresh exact-head independent controls re-exercise contracts; no earlier gate pass transfers.',full_production_paths=[p for p in final['full_paths'] if p.startswith(('service/','scripts/'))],delta_source_test_paths=[p for p in final['delta_paths'] if p.startswith(('service/','tests/','scripts/','native/'))],historical_findings='R1–R8/F1–F4 contracts retained; R9 source leak reproduced at previous head is closed by current independent exclusion controls. P3 original int/float equality versus typed request metric caveat retained. New R10 remains open.',prior_failure_evidence='All seven original candidate bundles and CI/worker additions were hash-verified without parsing payloads. Original reports, failed logs, corpus/gold/seals and original replay metrics are preserved.')
report=dict(verdict='BLOCK',candidate_sha=SHA,base_sha=BASE,previous_candidate_sha=PREVIOUS,pr_url='https://github.com/adjad/wisp/pull/160',branch='codex/router-update-1-3',recorded_at=now,findings=[finding],review=review,coverage=coverage,parent_full_gate=dict(status='PASS_PARENT_RECEIPT_VERIFIED',modules=179,receipt='parent-gate-receipt.json',metadata=final['parent_gate'],not_rerun_by_auditor=True),ci=final['ci'],limits=limits,candidate_clean=True,source_writers='FROZEN',simulation_qa='HELD',output=str(OUT))
(OUT/'REPORT.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
md=f'''# Wisp PR #160 independent release audit — BLOCK

Candidate: `{SHA}`. Base: `{BASE}`. Branch: `codex/router-update-1-3`.
[PR #160](https://github.com/adjad/wisp/pull/160). Recorded {now}.

## Finding R10 — P2: quoted reminder data is treated as negation

`Complete reminder called "Archive: don’t check this item"` is a supported positive completion request for an exact reminder title. The new Unicode `_NEGATED` regex at `service/tasks/compiler.py:20` is searched against raw request text at line409, including the quoted title. `compile_task` returns None. Actual `main.agent` runs zero fake tools, creates no task or workflow, and answers:

> I couldn't complete every requested step. Still missing one of: complete_reminder.

All four supported Unicode apostrophes reproduce this with `check` and `delete` inside the quoted title (8/8 failures). A same-head, in-memory counterfactual replacing only `_NEGATED` with the previous ASCII regex restores exact target parsing and one expected fake completion call for each form. Four current ordinary Unicode-name positive controls also pass. The ASCII quoted-negation limitation is pre-existing; these Unicode failures are newly introduced. The known unsupported `Check off reminder called` observation remains disclosed and was not used as a positive fixture.

Apply negation checks to quote-masked instruction text and preserve original literal target bytes. Add positive quoted-target controls while retaining real negative-instruction and source-exclusion coverage. No candidate files were edited.

Evidence: [R10-proof.json](R10-proof.json), [boundary-results.json](boundary-results.json), [supplemental-results.json](supplemental-results.json).

## Current repair and retained contracts

R9 source exclusions now pass all395 independent core/application controls: ASCII plus four Unicode apostrophes, five domains, read/include/check, correct completion and malicious omitted/extra replies, coordinated exclusions and independently requested later reads, literal query/name/target bytes and supported positive task controls. Pure reads retain exact fake calls/answers and neither a delivery workflow nor an active task.

Fresh logical controls: **622/630 pass**. The eight failures are one R10 family. Application/core484/484 includes373 actual-main runs; public40/40; runtime/privacy/overview66/66; supplemental32/32. Eleven initial auditor fixture errors were corrected in a bounded rerun: five guessed completion argument shapes and six overbroad no-workflow expectations for an explicit later send. Initial scripts/results/logs, exact explanations and11/11 corrected evidence are preserved. No candidate assertion, gold or production code changed.

R7 negative deliveries12/12 and R8 later-clause controls60/60 pass. R1 tuple mutations, R2 inherited query replacement and R4 governed titles pass. Supplemental actual-main controls cover calendar-only/aliases, mixed source completeness, dates/unread/count/accounts/query/duration, unsupported filters, context exclusions, Unicode literal rejection and honest source errors. Public checks retain frozen queries, literal-address/contact exclusion, inherited/leading payloads, cancellation and revoked delivery. G1 passes at all three clocks, and its injected text leak fails the oracle. Identity checks occur before fake I/O; deadline/outer cancellation tests cover only the structured planner. Overview/strict-message coverage, manifest drift179, DST23/25-hour spans, default-disabled config/kill switch and typed-metric caveat pass.

The full intended production/schema/harness/overview review from earlier audits is retained only for unchanged source, verified against the full363-path diff and current72-path delta. The four current source/test files and relevant parser/executor contexts were reviewed. Earlier pass claims do not transfer. Current provenance774/774 checks verify all changed file bytes, worker/assignment/HANDOFF/progress pins, opaque corpus/gold continuity,21 archive manifests with450 entries and12 replay bundles. Archived payloads stayed opaque; no archived script ran.

## Mechanical gates and release boundaries

Parent local full gate: **179/179 PASS**, exit0, head unchanged/clean; completed2026-10-06T01:37:36.695077Z in579.96s. Receipt and opaque raw-log SHA256 verified: `5b09a3939d61d26350e86c71c87bad63309ab3122ab762051e54f3c8560b9305`. The auditor did not duplicate it.

The latest saved parent-fetched CI metadata at this head remains `in_progress` for `python-regressions` run37399273554/job112062543794 and `Verified macOS artifact` run37399273475/job112062543801. No terminal CI success or old-head result is credited. Passing mechanical gates would not override R10.

All model/status/tool interfaces and state were synthetic. Positive task fakes do not mutate reminder readback, so exact calls and truthful unverified-effect response/stored failed task are expected; success is not invented. These checks do not establish resident Ling accuracy, real source/native behavior, resource residency, installed-app behavior or release performance. No real models/native/user data/outbound/network/endpoints, provider-cancellation diagnosis, full gate duplication, corpus/author/heldout decoding or source edits occurred.

Retained6df provider-cancellation cause/owner ACK/relay remains unresolved. Resource/training/activation/Live QA/performance/shipping and installed replacement are separate holds. Planner stays default-disabled. **Simulation QA remains HELD** because this audit is BLOCK; any repair needs a new freeze and fresh exact-head gates.
'''
(OUT/'REPORT.md').write_text(md)
handoff=dict(verdict='BLOCK',candidate_sha=SHA,pr_url=report['pr_url'],finding_ids=['R10'],finding_priority='P2',report=str(OUT/'REPORT.md'),report_sha256=h((OUT/'REPORT.md').read_bytes()),report_json_sha256=h((OUT/'REPORT.json').read_bytes()),independent_contract_controls=dict(passed=622,total=630),parent_full_gate_modules=179,parent_full_gate='PASS_VERIFIED_RECEIPT_NOT_RERUN',ci='PENDING_IN_PROGRESS_SAVED_EXACT_HEAD_METADATA',simulation_qa='HELD_NO_RELEASE',candidate_clean=True,source_writers='FROZEN',no_source_edits=True,provider_diagnosis='UNRESOLVED_EXCLUDED',resource_activation_shipping='HELD',next_action='Parent may triage R10; no repair authorization or gate relaxation is added. New edits require new frozen SHA and fresh assigned gates.',created_at=now)
(OUT/'HANDOFF.json').write_text(json.dumps(handoff,indent=2))
files={p.name:h(p.read_bytes()) for p in sorted(OUT.iterdir()) if p.is_file() and p.name not in ['checksums.json','seal-verification.json','finalize-command.log']}
(OUT/'checksums.json').write_text(json.dumps(dict(candidate_sha=SHA,sealed_at=now,files=files),indent=2))
verified={name:h((OUT/name).read_bytes())==pin for name,pin in files.items()};assert all(verified.values())
print(json.dumps(dict(verdict='BLOCK',sha=SHA,checks=len(files),report_sha256=handoff['report_sha256'],handoff_sha256=h((OUT/'HANDOFF.json').read_bytes()),clean=status=='')))
