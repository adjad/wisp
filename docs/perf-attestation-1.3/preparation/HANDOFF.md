# PR #170 documentation-preflight handoff

Outcome: existing 18-file documentation packet reconciled with current main; only the five released wrapper/manifest files updated. All 13 retained author/original-review/revised-review/N1 files remain byte-identical to preparation checkpoint cfa6919. This supplies documentation-integrity evidence and a candidate for separate CI/document review, not product or performance qualification.

Writer: `01a11333-ac03-7301-a6fe-6b21f0145c60`. Parent: `01a112be-3e4e-7833-99e8-eda89e84c53b`. Existing branch: `codex/attestation-perf-1-3`. PR: `https://github.com/adjad/wisp/pull/170`.

## Bases, release and permitted changes

Historical source/proposal/review base remains `ebe82735d699a88c05c94fa41fd0fee0e35fdd68`. Prior documentation checkpoint is `cfa69196ac43e215f0374f16b76e63693f88edad`. The actual fetched integration base is `6ee9e8b76990fb22dbbc46b0be583fa9481da713`, merged without force or conflicts at `b8fbe73a827e847be629eca0863d9fed7086674f`. Upstream files entered through Git reconciliation; this writer did not independently edit their source, tests or release controls.

Fresh scope receipt: `d923d94435843f1ef6294d706bb2a69188e4f8ba2aefb4c089fe98ed3f919d3d`. Explicit parent release: `fca4ba9d82a0b750a5d16cec63616a7a5d3b33467812f57a9ae2c1b72fe70701`. Paths and identities are recorded in PROVENANCE.json. Post-merge authored changes are limited to README.md, HANDOFF.md, STAGING.md, PROVENANCE.json and CHECKSUMS.json. No extra packet files or executable drafts were added.

The exact frozen new head, metadata command outputs/exit statuses, clean checkout, matching remote branch and checksum-manifest digest are delivered outside the commit to avoid circular hashes. CHECKSUMS.json covers every other packet file and excludes itself. Historical source hashes stay preserved alongside separately named integration-context hashes.

## Local evidence and failures

Local checks cover metadata, JSON syntax, source/retained SHA-256 and byte sizes, all 18 packet paths, manifest coverage excluding self, 45 unique matrix IDs with global NOT_RUN, supplemental N1 NOT_RUN, immutable-input comparison and Git whitespace. No local product tests/scripts/full regression, production imports, actual mutations, probes, native inspections, sockets, model/cloud/client-status calls or benchmarks ran. No children or runtime resources were created. Remote configured CI may run production checks; its execution/results are separate.

Historical failures remain recorded: initial branch creation and index/tracking writes met sandbox Operation not permitted and succeeded on approved retries; one documentation-check wrapper initially misclassified expected diff exit 1 with empty output and was corrected; the GitHub connector returned 403 for PR creation and authenticated gh succeeded. This preflight's initial fetch also failed with exit 255 on FETCH_HEAD sandbox writes; the approved fetch retry and non-force merge succeeded. No unowned conflict or automatic approval rejection occurred. Raw exit/output evidence remains outside the frozen candidate.

## Separate gates and unchanged limits

Required **python-regressions** and **Verified macOS artifact** must bind the new remote head. Distinct exact-head document-consistency/provenance review needs its own registered input hashes, acceptance and parent release. The writer does not self-approve. CI/review on prior head cfa6919 cannot qualify the new head; any later edit or reconciliation invalidates commit-bound evidence. This packet supplies no CI or new document-review verdict; those results are recorded externally after freeze. Publication remains skipped.

The original BLOCK history remains immutable. The distinct revised static review is PASS_WITH_NOTES for default-off specification/preparation only, with enablement BLOCKED. All 45 author cases and supplemental N1 remain NOT_RUN. N1 still requires actual final-emission/revocation serialization evidence; it is not proved by this documentation candidate.

Seq68 remains zero recorded correlated reply/ACK according to the source-parent release; Claude's precise ownership/quiescence/unpublished-work handoff is absent and product scope remains unreleased. D1–D4 are unaccepted. Current legacy admission stays authoritative; reuse/replacement/native authority remain disabled. Preserve PR161 cleanup, PR162 readiness, instrumentation seams and Managed behavior.

Git documentation reconciliation does not prove final 1.2 product compatibility or complete optimization work. The integrated per-version 1.2.0 ad-hoc benchmark waiver does not cover 1.3.0 or qualify performance. The unresolved 25%/50% criterion, numeric budgets, lower-writer proof, ABI/private-exec/global-refusal evidence, independent product gates and full release protocol still require their separate scopes. No installed app effects, merge, activation, deployment or publication is admitted. Explicit 1.3 NO-MERGE persists.

## Handoff state

After document checks, commit and non-force push, freeze one exact new head and return its raw evidence to the parent for same-head CI and independent document review. Do not edit the frozen candidate merely to refresh status. A blocking finding needs one recorded repair owner and a new candidate/review cycle. Writer handoff is **DOCUMENTATION_PREFLIGHT_COMPLETE_GATES_PENDING** at the SHA reported in chat; it is not a product approval or completion claim.
