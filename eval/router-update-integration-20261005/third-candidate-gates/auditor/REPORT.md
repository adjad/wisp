# Wisp PR #160 — independent audit

**Verdict: BLOCK** on `27490ae5055502423f187e84ca52d0064b794396`, base `4994caa15533c0cf84c07208c9097e4197f2815b`, branch `codex/router-update-1-3`. Recorded 2026-10-05T21:17:15.869861+00:00.

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

Parent command: `PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python scripts/test_replay_failure_fixes.py`. Receipt: `parent-gate-receipt.json`; external log `/private/tmp/wisp-router-update-27490ae-regression.log`, verified SHA256 `2d5448d21bdfd1d83f3d0dbd06bf0724a28bc3d53b900299607c7f59b324b39c`. Exit1, **178/179 modules**, only `tests/test_message_digest.py` failed: `test_distinct_redacted_requests_keep_both_source_occurrences` at line2961 (1427 tests passed in that module, one failed).

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
