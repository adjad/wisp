# Wisp PR #160: ninth exact-head audit — BLOCK

Candidate `67e4cefa53687f265287b9d068fc549c1f7955b6`, base `4994caa15533c0cf84c07208c9097e4197f2815b`, branch `codex/router-update-1-3`, targeting Wisp 1.3.0. PR: https://github.com/adjad/wisp/pull/160. The user forbids merging; the PR remains draft.

## P1 R11: private runtime authority escapes streaming scope

At `scripts/eval_router_update.py:577–582`, `_ScopedResidentClient.stream_events` yields while `_operation` retains the private peer ContextVar. The socket audit at line 886 treats that ambient phase as authority. Async-generator consumption exposes the phase to the caller; `asyncio.create_task` copies it. The copied child still has admitted socket-policy events and synthetic original-home resolution after the stream closes. Parent restoration and owned-stream cleanup succeed, so they do not eliminate the inherited authority.

The fresh supported-factory → ResidentInferenceAdapter reproduction uses synthetic settings/auth/locks, genuine RecoveryGate and normal auth delegation, HTTP MockTransport and `sys.audit` socket-policy events. Before the stream: no phase, socket policy denied, isolated home. Between frames: peer phase, policy admitted, fake original home. Child released after close: peer phase, policy admitted, fake original home. Four of 17 stream assertions fail. A separate inert wrapper probe independently corroborates the leak. This proves an isolation-policy bypass; it does not claim credential theft, real socket dispatch or native execution.

Repair should restore the consumer context before yielding and prevent copied contexts from authorizing I/O after operation closure. Add consumer and inherited-task denial tests; preserve auth/quarantine, pinned lock/lease, expiry/revocation, cancellation and truthful raw cleanup receipts. The Orchestrator must record one repair owner before edits. Any repair creates a new candidate requiring fresh gates.

Bounded finding: `R11_FINDING.json` SHA256 `4db38668bcbd587e7e490955feb88a2679b1462d80559a3b220045ea8ff8a4d7`. Its unchanged ten-entry seal: `1e6e357a268161e75dc8d60c48767d135fa77f331656b60237f51caafaf89a3a`.

## R10 and application verification

The supported R10 repair closes the earlier quoted-title negation finding: 16 straight-double-quoted and 14 recognized single-quoted positive controls pass, preserving literal arguments and singular expected_id. All 24 true outside-negation, five unmatched/possessive and five read/exclusion controls pass; all seven individual typed compiler controls pass. Fake readback remains honest and task/workflow snapshots are preserved.

Raw application results are **568/586**, including 481 actual-main controls. The 18 failed exploratory assertions remain visible: 16 curly-double-quote delimiter expectations and two nested ASCII apostrophe-in-single-quote mask expectations. Paired plain controls and unchanged source establish existing grammar limitations; these are not new R10 regressions or credited passes. `Check off reminder called` remains unsupported. R9 455/455, R7 6/6 and R8 20/20 retained controls pass.

| Fresh evidence group | Result |
| --- | --- |
| Application controls | 568/586 (18 disclosed exploratory grammar failures) |
| Retained routing/overview/deadline/manifest/DST contracts | 91/91 |
| Seven typed compiler quote-scope controls | 7/7 |
| Capability default denial and bounds | 20/20 |
| Supported runtime factory modes | 129/133 (four R11 failures) |
| Exact-head provenance/integrity | 1,473/1,473 |

Runtime policy, cancellation, recovery-generation, contention, marker/managed manifest, wrong identity, missing/symlink/raced lock modes pass. Default caller denial, genuine synthetic auth delegation, parent restoration, early close and caller cancellation pass. Stream repeat/direct probe are corroboration only. The output-root fixture setup errors, correction scripts, exit-one logs and preparatory syntax-error disclosure are retained in `fixture-corrections.json`; no candidate or authority contract changed.

## Mechanical gates and integrity

The parent ran the one authorized local gate: **179/179 PASS**, exit zero, unchanged clean candidate. No duplicate gate was launched. Python CI run **37407801476**, job **112089162704**, and artifact CI run **37407801472**, job **112089162874**, both completed **SUCCESS** at this exact SHA. Both release publishing jobs were skipped. CI metadata and complete raw log bytes are preserved and hashed; these passes do not override R11.

The review covers the intended 449-path diff and 96-path delta. The five source/test delta files and full new runtime/compiler context were reviewed; retained unchanged source/contracts were confirmed by delta and frozen Git bytes. Final clean HEAD and all 449 working fingerprints match. Provenance verifies 26 archive manifests/611 entries and 13 replay bundles using opaque stored/original byte hashes and metadata only, plus assignment/freeze/worker pins. No earlier gate pass transfers; no heldout, author or archived payload was semantically inspected or executed.

## Limits and release holds

Structured planner and evaluation runtime bridge remain default disabled. Synthetic MockTransport/policy proof does not establish real transport authority, runtime readiness/residency, loaded-weight provenance, quality or performance. Actual runtime admission needs separately attributed current identity/readiness/provenance, a valid exclusive resource window and same-SHA auth audit/QA. No loaded-weight hash was invented.

Simulation QA remains held until a nonblocking same-SHA audit and explicit source-parent release; this BLOCK does not qualify. Provider-cancellation diagnosis and Claude owner acknowledgement remain unresolved. No live models, real credentials/user data, native effects, outbound communications, training, activation, installed-app replacement, deployment, publishing or merges were performed. Wisp 1.3.0 and the user's no-merge instruction remain binding.

`REPORT.json` records full structured evidence, `HANDOFF.json` records the disposition, and `CHECKSUMS.json` recursively seals output bytes (including synthetic fixtures and failures). Symlink targets are hashed without dereferencing. Recorded UTC: 2026-10-06T03:44:42.674156+00:00.
