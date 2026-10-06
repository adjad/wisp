# Wisp PR #160 — fifth independent audit

**Verdict: BLOCK** at `6df5200d0945ee3b050bcde4115a338e392017b1`, base `4994caa15533c0cf84c07208c9097e4197f2815b`, branch `codex/router-update-1-3`.

Previous R4/R5/R6 reproducers pass. Two boundaries remain blocking in fresh controls. Parent full regression passes179/179; that mechanical pass does not override these findings.

## R4 — P2 — capitalized search title still becomes a recipient

**Read email from Cassia and email about Guide to Gardening** correctly validates and compiles standalone strict reads for `Cassia` and `Guide to Gardening`. The shared instruction mask and workflow helper recognize a read. However, the coordinated noun guard invokes `_terminal_destination` on raw query text (`web_request.py:558–562`). Its capitalized-complement heuristic at521–527 considers `Guide` a complete topic and `Gardening` an addressee, grants `send_email` metadata, and truncates source text to the first clause.

Actual main bypasses structured planning and takes the legacy `rules / email lookup -> scoped tools (6)` route without verified-results-only. Quoting the title executes both exact reads; lower-case named/containing/about variants work. Bind destination attachment outside the complete read literal. Capitalization alone must not authorize an effect; unresolved attachment may clarify while retaining explicit recipients and later delivery actions.

No send occurs. The observed fallback offers six legacy email tools, **not** `send_email`. Incorrect metadata and planner bypass are proven; no executable outbound expansion or real-model failure rate is alleged. Evidence: `application-results.json`, `R4-title-literal` / `R4-title-quoted`.

## R7 — P2 — later share instruction is consumed as search text

**Find notes about cedar maps and share it with Mom** loses its later action. The new conjunction boundary list in `_unquoted_queries` (`validation.py:249–261`) omits `share`, although it is already an outbound workflow verb. The shared instruction mask hides the whole `cedar maps and share it with Mom` tail; workflow admission reports no outbound verb and the compound action enters structured reads.

Supplying the swallowed tail as a query validates, and actual main executes a registered fake `search_notes(query="cedar maps and share it with Mom")`. Supplying the real `cedar maps` query instead rejects it as incomplete after bounded read interpretation. Both lose the action boundary.

Paired later forward/send, share with an explicit email channel, and a subsequent send after literal share text retain guards. Genuinely quoted share text remains data. Pure previous masking-stage expressions detect outbound share on the failing input while the current helper does not; no old script or application receipt was executed/transferred.

Keep the full established action vocabulary visible at clause boundaries or conservatively decline structured admission. Do not validate action tails as exact lookup text. Only a fake read executes; no actual share, communication, native body or permission bypass occurs. Evidence: `application-results.json`, `action-share-full` / `action-share-prefix`; `paired-results.json`, `later-share-without-channel` and paired controls.

## Repair closure and coverage

- R5: bare/mixed recap, Review/Inspect/Scan/Browse and recap text/message reach reads; real recipients/drafts/later sends keep guards.
- R6: unquoted source/action literals grant only notes authority. Missing/truncated/extra sources reject; raw exclusions remain instructions and quoted exclusions data. Prior literal words do not grant contextual domains; independent sources and standalone notes-only admission work.
- R3/R1/R2: mixed and one-filter repeated reads work. Correct/reversed tuples and dedupe work; query/date/count/account/unread/operation swaps and omission reject. Current source-free query replaces stale text with count/date inheritance. Previously completed account/unread/ambiguity and messages alias coverage retained through unchanged code/context review.
- Natural calendar/inbox aliases, omission/unsupported rejection,60minute availability and calendar-only tomorrow work. Shortcut's unused empty scripted intent is not separately validator-valid.
- Runtime controls pass: pre-I/O origin/provider/role/transport/residency, defaultoff/kill switch, bounded repair/context stripping, deadline/cancellation and typed schema.
- Actual G1 repaired test passes three clocks and injected-leak oracle rejects private text. Counts, duplicate provenance, actor direction, attribution, partial coverage and merged results remain grounded.
- Six test modules admitted; unknown/missing failclosed. Typed supplemental scorer preserves disclosed legacy numeric equality. UTC/Pacific23h/25h bounds, restoration and missing-tzset controls pass.

Fresh application expectations **64/67**, paired controls **4/5**, runtime/privacy/rendering helpers **36/36**. Failures represent the two findings; correlated synthetic probes are not model accuracy samples. Inputs, scripted replies, schemas, SSE routes and registered fake calls are preserved.

Initial raw application result (**63/67**) is retained. One extra failure was an auditor expectation error: generic “send it to Mom” does not request email, so channel-null delivery cannot require email-specific metadata. Only that auditor expectation was corrected before a bounded rerun; no candidate/corpus/gold/scorer/raw overwrite. The first large report-write call stalled without writing and was terminated; smaller writes completed. See `auditor-fixture-correction.json`.

## Scope, gates and limits

Complete **181-path** intended base-to-head scope assessed through prior full source/schema/harness/overview reviews plus the complete **49-path** delta and current context. New production changes are validation, web-request classification and workflow compiler; two existing test modules changed. Final clean HEAD and every changed-file hash verified. Provenance covers registered assignment/handoff pins, nine replay bundles,102 archived entry checks, unchanged opaque corpus/author bytes and seals.

Parent full gate **PASS179/179**, exit0, exact SHA, completed2026-10-05T22:25:55.452075+00:00. Receipt `parent-gate-receipt.json`; complete log `/private/tmp/wisp-router-update-6df5200-regression.log`, verified SHA256 `d7683188e2e2a2cb4a72e178031aed79a50bfc0db9ac1ded3f61fbbe4d7d7102`. Auditor did not duplicate the full gate or DEV harness. RequiredCI remains running in parent evidence; no independently inspected terminal PASS receipt. Prior outcomes stay bound to their own heads.

Heldout/dev corpus/author/raw archive/replay payloads stayed opaque: bytes/hashes and permitted metadata only; no archived script executed. No real model/endpoint/native/user data/settings/credentials/outbound/network effects, training/residency/activation, installed-app change, deploy or merge. Main fixture uses standard all-domain configuration; notes-only admission was tested standalone. Planner stays default-disabled and external resident-window ACK pending. Model95%/strict-JSON/latency/training/generalization/activation unmeasured. BuilderDEV80/80source/exact and72/80E2E remain scripted metadata with annotation limitations; zero-applicable measures establish neither property.

## Handoff

Separate Simulation QA remains held by this BLOCK. Parent reads files; route R4/R7 to the Orchestrator for one recorded repair owner per finding before edits. Require new freeze, ACK and independent exact-SHA re-review. No activation, QA release, merge or shipping approval follows.
