# Wisp 1.3 attestation documentation preparation

Status: **documentation only; current PR #170 qualification released; production integration and enablement separately gated**.

This packet preserves the frozen author proposal and 45 future case specifications. It organizes dependencies and later evidence without closing implementation findings, accepting D1–D4, enabling an optimization, or qualifying performance. The original independent **BLOCK** remains preserved in `original-review/`. No local product tests, probes, production imports, native inspections, socket/model/cloud operations or benchmarks were run for this packet. Configured remote CI may execute production checks independently; its results are bound to the actual remote head and are not supplied by these local document checks.

## Identity, scope and provenance

Sole preparation writer: chat `01a11333-ac03-7301-a6fe-6b21f0145c60`, Wisp 1.3 attestation performance. Source parent and sole dispatcher: `01a112be-3e4e-7833-99e8-eda89e84c53b`, 1.3.0 work. Designated protected merge executor: `01a10aef-17bd-7f80-b5c8-f5cc08fe2506`. Preparation branch: `codex/attestation-perf-1-3`. Frozen source base: `ebe82735d699a88c05c94fa41fd0fee0e35fdd68`.

The parent released branch setup and then documentation preparation contingent on conforming registration. Before these writes the branch was registered in chat at that exact HEAD with a clean checkout. The original preparation wrote Markdown/JSON under `docs/perf-attestation-1.3/preparation/**`; this preflight updates only README.md, HANDOFF.md, STAGING.md, PROVENANCE.json and CHECKSUMS.json. The other 13 retained author/reviewer/N1 files remain byte-identical. Original branch setup needed an approved retry because shared Git worktree metadata was outside the sandbox; those setup retries had no automatic approval rejection.

The initial preparation checkpoint is `cfa69196ac43e215f0374f16b76e63693f88edad`. Under the separately recorded documentation-preflight release, this branch fetched and non-force merged integration base `6ee9e8b76990fb22dbbc46b0be583fa9481da713` at merge commit `b8fbe73a827e847be629eca0863d9fed7086674f`, without conflicts. The historical proposal/source base remains `ebe82735d699a88c05c94fa41fd0fee0e35fdd68`; no retained proposal or review was rebound to current main. This is documentation integration, not proof of final 1.2 product compatibility.

On 2026-10-09 the same writer reconciled current main `cbf6bd7a5c0c879e473956c2f365c99043f19ac0` by ordinary non-force merge at `e7612c19792603a201a63ee17651e2817f6de89d`, preserving all 18 payloads. Root then released minimal current-status corrections in the five wrappers listed above; all 13 immutable inputs remain unchanged. Earlier checkpoint review/CI remain historical context; fresh corrected-candidate evidence is recorded externally.

- [PROVENANCE.json](PROVENANCE.json) binds source paths, hashes, receipt identities and evidence limits.
- [STAGING.md](STAGING.md) maps planned stages, future cases, preserved tests and enabling gates.
- [HANDOFF.md](HANDOFF.md) states delivery, checks, holds and next ownership steps.
- [CHECKSUMS.json](CHECKSUMS.json) lists every other prepared file's exact digest and byte size; it excludes itself to avoid self-reference.

## Retained immutable inputs

`author/DESIGN_PROPOSAL.md` is an unchanged copy of SHA-256 `746dd9cae6b5a01346093f6f0114f407d9915d61d736ddd94e024e93c9424886`. `author/TEST_MATRIX.json` is an unchanged copy of SHA-256 `128143beca2c9e13bfaadd580b7b4f60f4894e7562de8cddc50650cd4e51db25`.

The matrix contains L01–L10, C01–C16, N01–N07, P01–P05, E01–E05 and B01–B02: 45 unique specifications. Its global status stays **NOT_RUN**; neither row count nor document checks prove any assertion or kill any mutant.

`author/FINDING_RESPONSE.json`, `INPUT_HASHES.json`, `HANDOFF.md`, `CHECKSUMS.json` and `SEAL.json` are also copied unchanged. The author's seal/manifest refer to its original temporary directory and additional artifacts not copied here. They are retained historical provenance, not a manifest of this preparation packet; use this packet's root checksums for its files.

`original-review/FINDINGS.json` and `HANDOFF.json` preserve the original reviewer’s BLOCK and exact report/review references. The full original report remains hash-bound at its source location; this packet does not rewrite that review.

Historical references to pending builder setup in retained author/receipt text are preserved byte-for-byte. The identity correction and current branch registration above supersede that setup inference only. They do not transfer Claude's product ownership, close review findings, or admit runtime work.

## Current behavior and holds

Current legacy admission remains authoritative: new Desktop authority per load, full existing binding checks, connect/header/body verification, complete production lsof parsing and current reasons/retries. Proposed reuse, replacement fences and native authorization remain disabled. The future legacy kill switch must select this complete behavior; an unset switch cannot authorize activation.

PR161 synchronous nonwaiting inspection-pool cleanup, PR162 readiness reuse, instrumentation seams and Managed behavior are protected. Runtime manifest, gateway, keepalive, `omlx_client.py`, `CHANGELOG.md` and shared simulation runner are outside the reservation.

Claude's correlated seq72 reply reports quiescence and no unpublished work on six transport/test paths and conditionally offers them to source parent `01a112be`. It explicitly requires Root to record the return/accept; receipt delivery alone is not transfer. PROVENANCE.json binds the exact artifact/hash and six paths. This corrects the earlier seq68-absence status without inferring completed transfer or product-stage release.

Root's current documentation-only release removes PR #174 delivery as a prerequisite for PR #170. The earlier global 1.3 no-merge hold is superseded by standing gated merge authorization; only the designated integrator may merge after fresh same-head CI, independent document review and protected expected-head verification. Product integration still needs recorded ownership, its own conforming scope/release and qualification. D1–D4 remain unaccepted. No installed app effects, live ports/credentials/communications/user data, model changes, activation, deployment or publication are authorized by this packet.

The approximately 40–41 ms values in historical material are derived estimates. The revised proposal retains full legacy global decisions; removing lsof cost is not presently qualified. This packet makes no speed claim.

## Completed revised static review

The distinct Wisp Release Auditor returned **PASS_WITH_NOTES** for design consistency/default-off preparation on proposal `746dd9c`. F1–F5 were revised adequately at specification level; none is implementation/runtime closure. The enabling verdict remains **BLOCKED**. Unchanged report, handoff and seal copies are in `revised-review/`; source hashes are bound in PROVENANCE.json.

The reviewer added N1 (P2): deterministically distinguish revocation-before-final-submission from submission-before-revocation and kill removal/early release of the serialization gate. [N1_CONTROL.json](N1_CONTROL.json) documents this additional **NOT_RUN** control without changing the frozen 45-row author matrix. Partial/resumed writes must be observed at the actual emission seam; wrapper exceptions/cancellation alone cannot prove zero bytes.

On 2026-10-07 the human said “ok go ahead” after an update identifying ownership handoff and implementation/testing release as next steps. That historical instruction was forwarded to the source parent and was not D1–D4 acceptance or native/model admission. Later PR #173 separately delivered an inert pure S1 foundation unused by production; it does not mark the original 45 cases or N1 as run. Current documentation authority and conditional seq72 attribution are stated above; no product qualification is claimed.

## PR #170 candidate preflight and separate gates

Earlier 2026-10-07 scope/release receipts remain bound in PROVENANCE.json. Current Root documentation qualification and its five-wrapper amendment are SHA-256 `2aad95c9909a458e3b4eaabb191fa82df265f4bb52238e521ffaa3b761bfbee7`, also bound there. All 18 files remain documentation only, with the 13 immutable inputs preserved.

The exact frozen new candidate SHA, raw command/exit evidence, clean checkout and matching remote branch are reported outside the commit to avoid circular provenance. Required remote checks are **python-regressions** and **Verified macOS artifact** for that same new head. The distinct document-consistency/provenance review must also bind the same head after its own recorded acceptance/release. Until those results are recorded, the packet supplies no CI pass or exact-head document verdict. A changed head invalidates commit-bound evidence.

A documentation CI pass can establish its configured checks only; it cannot establish optimization completion, deployed native behavior, a measured speedup or accepted D choices. Publication remains skipped. The merged release protocol contains an explicitly per-version ad-hoc benchmark waiver for 1.2.0; it does not authorize a 1.3.0 waiver, publication or any action in this task.
