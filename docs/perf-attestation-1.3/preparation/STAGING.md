# Staging, dependencies and future evidence

This is a proposed work sequence for later admission. It releases no product paths or execution. Every matrix row remains **NOT_RUN**. The original F1–F5 BLOCK history is retained. The revised distinct review now records PASS_WITH_NOTES at specification/default-off preparation level only; enabling remains blocked and implementation proof is outstanding.

## Design obligations

| Finding | Proposed revision and matrix | Proof still required before enabling |
| --- | --- | --- |
| F1: writes can outlive freshness | Immutable start-based sleep-inclusive lease; strict expiry on each emission; pre/post-await and dispatch checks. L01–L10, B01. | Concrete bundled lower-write dispatch contract, resumed emission and revocation serialization; distinguish wrapper order from OS submission and physical delivery. Old streams retain their own expiry. Choose D1 only with evidence and explicit residual acceptance. |
| F2: endpoint oracle misses native authorization fields | Complete individually qualified socket/process/private-exec profiles and global latch. N01–N07. | Exact deployed OS/build/architecture/translation/interface profiles; every consumed width/offset/constant/semantic; both-end sharing/identity/ABA fixtures and endpoint-preserving mutants. Same size or clean endpoints do not qualify a profile. |
| F3: pair-only native evidence cannot prove global parser parity | Full production global legacy verdict remains authoritative. P01–P05. | Full global refusal corpus, including unrelated IPv6/malformed/inaccessible/oversized inventories. Retain global cost unless full parity is proved or a separate predicate change is independently reviewed and accepted by the owning human. |
| F4: stale/hung jobs and invalidation semantics conflict | Exact publication-token CAS, independent replacement/revocation generations, short metadata locks, bounded actual jobs. C01–C16, L04–L06. | Stale success/failure no-op; qualified finite build deadline; real-job accounting survives logical timeout/close; no slow work under locks; post-await cross-client refusal, fork reset and synchronous cleanup. The proposed two-job budget is unvalidated. |
| F5: exec version and argv are different questions | Separately qualified server/current-parent exec identity; preserve binding on unsupported profiles. E01–E05. | Deployed private interface layout/permissions/availability, same-binary and A–B–A exec, coherent multi-call brackets and version reuse limits. Exec detection cannot substitute for mutable argv checks. |

W is a proposed permission-audit reuse age at admitted dispatch. It does not bound effects of code planted during a transient writable interval, nor establish a strict physical-delivery deadline. No numeric W or healthy-build timeout is chosen by this packet.

## Stage dependencies

The original P0–P4 shorthand is retained for history. The revised author’s S0–S6 sequence adds prerequisites and prevents an automatic P4 default flip.

| Stage | Bounded future outcome | Required predecessor and stop gate |
| --- | --- | --- |
| Current docs preparation | Frozen proposal, NOT_RUN cases, provenance and gates only. | Narrow docs release and registered frozen-base branch; no code/tests/execution. |
| Product admission | Register one writer, exact permitted paths and current compatible base. | Claude baton/disposition ACK, revised independent design result, parent release; reconcile final 1.2/current main only under later authority. No inferred transfer from this packet. |
| S0 / original P0 | Meaningful synthetic legacy behavior pins and red tests on the newly admitted base. | Preserve current lifetime/blind-spot tests; categorize new behavior failures versus existing pins. No blanket red-test dependency on obsolete 4994caa. |
| S1 default-off scaffolding | Pure lease/token/decoder models with unchanged legacy dispatch. | Design constraints and exact-path release. No production native calls or shadow traffic; fakes are not native qualification. |
| S2 / part of P2 | Lease, revocation, publication and dispatch implementation, still disabled. | F1/F4 proof and lower-writer ownership. If the concrete dispatch seam requires files outside admitted paths, parent must obtain a separate reservation before edits. |
| S3 / part of P1 and P3 | Separate exec, process and socket profile qualification; legacy binding/lsof remain authoritative. | Explicit disposable/native execution scope and independent profile evidence. Unsupported mechanisms retain legacy behavior; no implicit R2 acceptance. |
| S4 / original P3 shadow | Full production-parser differential evidence on specified disposable synthetic fixtures. | Explicit spare-port/process scope, profile qualification and full refusal controls. Native findings remain metadata; legacy decides. No normal installed-app shadow traffic is admitted. |
| S5 / future P1, P2, P4 authority changes | Separate candidates for reuse, replacement fence or native authority. | Exact D choices plus frozen-head mechanical evidence, required CI, independent Auditor and triggered distinct specialist QA. F3 blocks claiming a lsof-free parity path. A 500-clean-check count is insufficient by itself. |
| S6 performance qualification | Full release protocol against exact candidate and approved exact baseline. | Qualified candidate, comparable cohort, explicit quiet window/resource scope and resolved performance criteria. All 1.3 merge/activation/release holds still apply. |

Revised static review is not an exact-head product audit. A satisfactory design disposition cannot substitute for mechanical tests, deployed qualification, human residual decisions or measured performance.

## Future validation inventory

Read at frozen base only; none of these commands/checks ran during preparation.

- Targeted pytest suites: `tests/test_runtime_attestation_t0.py`, `tests/test_runtime_proc_path.py`, `tests/test_runtime_peer.py`, `tests/test_web_response_followup.py`, and future `tests/test_runtime_attestation_t1t3.py` only after admitted creation. Later use the qualified interpreter with `-m pytest -q` for these modules, under isolated synthetic state.
- Full repository regression: `python scripts/test_replay_failure_fixes.py`. Current runner discovers repository modules, uses pytest for modern modules and direct execution for named legacy modules, and rejects empty test execution. The original design's claim that bare pytest always runs zero tests is not a reliable description of this base.
- Configured regression workflow: `.github/workflows/regression-gate.yml`, Python 3.13, hashed test lock, exact candidate checkout. Python 3.14, UTC/Kiritimati and constrained sandbox profiles are requested design coverage, not checks already completed or assumed available.
- Preserve existing T0 pins: every new authority walks each tree twice; each load/connection obtains current legacy authority; repeated binding walks no tree on the same instance; no bytes before verification and every nonempty write rechecked; header/body epoch/failure fences; two sequential owner snapshots bracketed by bindings; benchmark seam existence/wrapper counts; canceled close remains nonwaiting; fork resets pool; no new transient reason.
- Keep new optimized-policy tests additional to legacy pins. Do not rename current lifetime tests into evidence for an unimplemented lease. Any intended legacy verdict/reason change needs independent explanation and admission.
- Cover the 45 cases with actual emission observations, controlled schedules, independent full-field fixtures and full global parser oracle. Split composite cases into executable parameters when later authorized; the 45 specifications are not a promised executable test count.
- Mutation requirements include each server/parent comparison field and boundary fingerprint, clock source and expiry equality, token/generation/lifetime/key dimensions, stale-error eviction, real-worker budget, post-await/dispatch checks, every consumed native field and fallback, global parser filtering and argv/exec distinctions. Record every survivor as an unresolved gap or repair it under a recorded owner and re-review.
- Later soak and cold/sleep/restart/refusal/close/fork coverage require explicitly admitted synthetic runtime scope. Disposable processes/spare ports are not admitted by these documents. No installed app or live engine port traffic is part of synthetic QA.
- Before a final product gate cycle: fetch/reconcile then-current main without force, verify nonempty intended diff/no unrelated files, freeze and push one exact candidate SHA. Mechanical results, required remote-head CI, independent Auditor and triggered specialist QA must bind that same SHA. Zero configured checks means unavailable/non-passing. Any repair/reconciliation creates a new candidate and invalidates prior evidence.

Shared `scripts/run_simulation_qa.py` remains held; later specialists need their own recorded scope rather than modifying that runner here. Builders do not approve their own candidate repairs.

## Performance criterion discrepancy

| Source | Stated criterion | Scope/evidence limit |
| --- | --- | --- |
| Earlier T0 plan, stage 5 | Less than 25% reduction in re-baselined per-call overhead: do not merge stages 2–4. | Historical walker/spawn/path optimization stop criterion; not a release-performance PASS. |
| Claude attestation handoff | Refers to the T0 under-25% criterion. | Proposal, not human acceptance or threshold selection for this revised design. |
| Original design section 8 | P1+P2 must reduce measured per-request attestation by at least 50% before P3. | Different optimization criterion; design also uses distinct component targets. |
| Frozen-base release protocol, release_policy_v2 | At least 20 measured samples and two separate warmups per required scenario/side; regression requires both more than 20% slower and more than 250 ms at p50 or p95. Correctness/refusal/effect/integrity gates also apply. | Release policy with its own hash. Attribution counts/CPU are diagnostics; synthetic fixture timings are not performance evidence. |

Parent must record which optimization stop threshold applies, its denominator, baseline, scenario/statistic, and how it relates to the versioned release policy before candidate freeze or measurement. This packet chooses neither 25% nor 50% and changes no policy. Passing a component target cannot waive end-to-end gates.

The pinned release protocol requires exact clean candidate/baseline worktrees, Desktop lane, approved baseline and digest, qualified resident model, suitable quiet resources and a fresh output directory. Its live run is outside this scope; do not execute its info/run/client-status paths here. Source statements about historical qualification are retained context, not verified present runtime status.

Full later costs must include complete server/parent signature/identifier/team/exec facts, synchronization, lower-write checks, background contention, cold/sleep/restart/refusal behavior and idle CPU. The historical 40–41 ms arithmetic cannot be carried forward while claiming full global legacy parity and eliminating its inventory cost.

## Mandatory supplemental N1 control

The revised reviewer requires a deterministic LP-R versus final nonblocking submission control, beyond L04/L08/L09/L10. See N1_CONTROL.json: pause a writer after its sample but before submission, complete another client's revocation, then resume and observe no old-token emission. Reverse the order to document unretractable earlier submission and forbid later partial/resumed emissions. Mutants must remove the shared gate or release it between comparison and submission. Also prove that the short gate contains no await, blocking send or slow work and creates no deadlock. This supplemental specification remains NOT_RUN; it neither amends the frozen author matrix nor grants runtime authority.
