# Smart Search reliability

## Selection and implementation plan

Base: `c3b5afe26a1744dd7927586262be0533322443af` (`origin/main` at dispatch).
Branch: `codex/smart-search-reliability`.

Ranked audit shortlist:

1. **Smart Search request lifecycle and fallback.** Rapid typing can cancel
   indexing needed by the next request, stale responses can replace a newer
   same-text search, and canceled answers keep generating. Invalid embedding
   responses can abort otherwise useful lexical search. Frequent interaction,
   reproducible offline, independent of current workstreams: selected.
2. **Independent reminder scheduling.** Serial background jobs can delay alerts
   beyond their grace period. Valuable, but a separate scheduler outcome.
3. **Durable reminder delivery.** A reminder can be marked notified without a
   connected recipient. A complete delivery protocol needs shared app plumbing.

Outcome: keep Smart Search responsive and correct when queries change, the panel
closes, or the optional embedding service returns unusable data. Preserve tier
ordering, answer grounding, and existing search controls.

Owned production paths: `service/search/embedder.py`, `service/search/engine.py`,
`app/Sources/WispApp/SearchModel.swift`. Supporting scope: new search regression
tests, a standalone Swift checker script, and this document. No changes to the
Local checkout or files owned by memory, email hardening, or UI mockups.

Implementation sequence:

1. Give each app request a unique identity; invalidate it immediately on edits,
   reset, capture, and explicit re-runs. Scope events and completion to that ID,
   cancel pending debounce on explicit searches, and clear obsolete progress.
2. Give shared indexing ownership of its cleanup and shield it from individual
   waiter cancellation. Cancel and await request-owned retrieval/synthesis work.
3. Validate embedding response structure and vectors, degrading through the
   existing semantic-unavailable event while retaining lexical results.
4. Add deterministic cancellation, fallback, healthy-answer, and native state
   regressions. Run targeted/combined tests and compile the full Swift app.

Validation uses synthetic documents, mocked HTTP/model clients, and temporary
`WISP_HOME` directories. No live app, connector, credential, or delivery access.
Integration has no dependency on other workstreams and changes no API schema.

## Handoff

Delivered behavior:

- Editing a query immediately cancels its request, clears obsolete progress,
  and rejects late results even when the user returns to identical query text.
- Whole-document search and explicit answer requests cancel pending ordinary
  searches. Old completion cannot turn off a newer request's progress indicator.
- Reset, replacement capture, blank queries, and stream completion return the
  search model to a consistent state. Unreadable-page explanations survive typing.
- Request cancellation/iterator closure cancels and awaits answer generation.
  Failed retrieval cancels the other request-owned retrieval work; failed HTTP
  batches cancel their siblings before the client closes.
- Shared document indexing survives canceled individual waiters and remains
  discoverable by the next query. Its task owns cache/flight cleanup; abandoned
  failures are consumed and the next request can retry.
- Invalid JSON, record counts, indices, numeric values, and dimensions produce
  semantic-unavailable events. Existing lexical results and grounded answers
  from lexical evidence remain usable. Successful citation generation and tier
  ordering are unchanged.

Files changed:

| Path | Purpose |
| --- | --- |
| `app/Sources/WispApp/SearchModel.swift` | Request identity, immediate cancellation, progress/error lifecycle |
| `service/search/embedder.py` | Shared task ownership, response validation, batch cleanup |
| `service/search/engine.py` | Request-owned retrieval and synthesis cleanup |
| `tests/test_search_reliability.py` | Offline HTTP, lifecycle, fallback, and real synthesis-grounding regressions |
| `tests/SearchModelChecks.swift` | Actual Swift search model exercised through an inert transport |
| `scripts/test_search_contract.sh` | Isolated native compilation/check runner |
| `docs/SMART_SEARCH_RELIABILITY.md` | Ranked audit, implementation plan, and handoff |

Validation:

- Targeted Python suite: **19 passed, 28 subtests passed**.
- Combined search/research/inference/prompt suite: **149 passed, 28 subtests
  passed**. Files: `test_search_reliability.py`, `test_research_mode.py`,
  `test_lazy_inference_readiness.py`, `test_latency_prompt_contract.py`.
- Existing `scripts/test_replay_failure_fixes.py` gate: **448 passed, 1 skipped**;
  legacy check-counter suites also passed (**28 + 56 + 87 + 4 checks**). The skip
  is the opt-in local Ling integration in `test_reminder_creation.py`.
- Native `bash scripts/test_search_contract.sh`: **50 regression checks passed**.
- Full Swift build and final rebuild after the two review fixes passed; existing
  PageReader Sendable/deprecation warnings remain.
- `git diff --check` and shell syntax validation passed.
- Failure reproduction against `c3b5afe` loaded in isolation: all three selected
  backend lifecycle regressions failed, and 20 malformed-response subtests failed.
  The same Swift harness failed at immediate cancellation during debounce.
  These are expected failures demonstrating the original defects, not failures
  remaining on this branch. Working-tree source was never replaced for these checks.
- Initial branch creation was blocked by the filesystem sandbox because Git
  metadata lives in the shared repository; the permitted retry succeeded.

To demonstrate locally (use an existing Python environment with runtime
dependencies and pytest):

```bash
search_state=$(mktemp -d /tmp/wisp-search-demo.XXXXXX)
WISP_HOME="$search_state" PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  -q -p no:cacheprovider tests/test_search_reliability.py
bash scripts/test_search_contract.sh
```

No live app/engine run, installation, deployment, source-app mutation, or outbound
delivery was performed. Live oMLX performance and manual panel behavior remain
unmeasured. Cancellation is verified through Wisp's HTTP/model coroutine boundary;
the external engine's internal scheduling after HTTP disconnect is outside this
change. Document indexing intentionally finishes its remaining timeout-limited
batches as prewarm after all current search waiters leave. Embedding cache model
identity is an existing separate limitation; incompatible dimensions now fall
back safely, while switching between equal-dimensional models is not addressed.

No implementation work remains. Integration requires only this branch; no data
migration, dependency update, or changes from the memory/email/UI workstreams are
required. Any later edits to the three production paths need normal conflict
review. Branch: `codex/smart-search-reliability`; exact commit and PR are included
in the task's final handoff. Nothing was merged or archived.
