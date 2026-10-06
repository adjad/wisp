# Wisp PR #160 — seventh independent audit

**Verdict: BLOCK** at `8dfd5c9c19d8e31063a1f0d65c87d3d0d1938736`, base `4994caa15533c0cf84c07208c9097e4197f2815b`, branch `codex/router-update-1-3`.

The previous R7-negative/R8 findings are repaired in fresh actual-application controls. One explicit-source exclusion boundary remains blocking. Local179/179 mechanical success does not override it.

## R9 — P1 — Unicode apostrophe reverses source exclusion

**Find notes about quartz birds and don’t read my email** produces required sources `notes,email` and no exclusion. The correctly represented notes-only intent with `excluded_sources=["email"]` clarifies as “Missing requested source.” A deliberately wrong scripted intent that includes email and omits the exclusion is accepted and compiled. Actual main executes registered fake `search_notes(query="quartz birds")` and `summarize_emails()` and returns both synthetic receipts.

`source_requirements` takes raw `mask_quoted(prompt)` at `validation.py:38` and matches the ASCII-only `_NEGATIVE` expression at20/66/71. Instruction/action helpers instead normalize Unicode apostrophes (`web_request.py:177–180`). Consequently the source prohibition is missed, and the noun is treated as positive authority. The ordinary read governor is not a negative effect head that would mask the clause.

Curly U+2019, modifier U+02BC and fullwidth U+FF07 contractions independently reproduce for email, messages and calendar; fake `summarize_messages` or calendar-only `get_upcoming` runs in the corresponding wrong-intent case. ASCII apostrophe and “do not,” “excluding,” and “without” controls execute only the authorized notes read and reject added sources. Model-declared exclusions still reject an extra source. Fully quoted prohibition words remain exact query data.

Normalize semantic exclusion matching consistently while preserving original literal bytes/offsets, or fail closed on unresolved prohibition. A model must not gain access to a user-prohibited source by omitting `excluded_sources`. Require paired actual-main exact-call/response/no-workflow checks for the correct intent and malicious extra-source reply.

Evidence: `application-results.json` excluded-good-104/106/108 and excluded-extra-105/107/109; `exclusion-results.json` exclusion-9/10 (email),12/13 (messages),15/16 (calendar), with further apostrophe variants. `R9-proof.json` preserves compact SSE/call/input/store receipts. Only fake synthetic reads occurred; no real data/native/model access or model failure rate is claimed. The source-negation regex itself was not newly edited in this delta; this is a remaining exact-candidate contract failure.

## Repair closure and retained coverage

R7-negative **13/13** now executes the exact registered notes read and faithful fixture response, with no stored delivery workflow or workflow event. Coverage includes plain/polite/never, inflected sharing, negative preference and apostrophe variants. R8 **56/56** later update/mark/clear/email cases retain an independent action guard across and/plus/and then/and afterwards/afterwards/&/semicolon. None compiles or silently succeeds as notes-only; later genuine email binds Rowan rather than the prior negative Mom clause. The supported read plus seven omitted-source cases **14/14** preserve later read authority and clarify incomplete interpretation.

Earlier title/capitalization/second-destination/quoted-action and positive share controls retain their boundaries. Mixed reads, aliases, source literal words, typed dates, calendar-only and75minute availability are covered. Full repeated query/date/count/account/unread/operation tuple, reordered entries, omission/swap rejection, exact deduplication and source-free replacement/inherited constraints pass **20/20** in a separate fresh batch. Prior full unchanged-code reviews retain the earlier R1–R6/F1–F4/P3 scope; this is not transfer of old passing gates.

Public controls **39/39** pass for update-me, explicit web cancellation, punctuation/address delivery, payload and later-turn context. Runtime/privacy/overview controls **60/60** pass: G1 three clocks and injected-leak rejection; attributed client identity before I/O; actual default configuration disabled, allowlist/kill/residency/repair/context; planner-only deadline and propagated cancellation; schema/missing-tool safety; exact counts/provenance/actors/partial coverage/strict message misses versus legacy fallback; typed scorer preservation; UTC/Pacific23h/25h DST and timezone restoration/missing-tzset;179-module manifest and failclosed unknown/missing drift.

Raw application expectations are **134/140** and exclusion-pair expectations **25/43**; all failures in these batches are the correlated R9 correct-intent rejection or wrong-source acceptance probes. Separate tuple/public/boundary batches all pass. These synthetic counts are not model accuracy, statistical coverage or a release pass rate. All inputs, scripted replies, registered fixture calls, SSE routes and persistent workflow snapshots remain preserved. No failed application expectation was rewritten.

## Full scope, provenance and gates

Full **298-path** candidate reviewed through retained independent full production/schema/harness/overview/context work plus complete **76-path** delta/current context. Exactly five authorized source/test paths changed; remaining additions are owned evidence/docs. Current production patch and all new test contracts were read. Archived payloads remain opaque and were never executed. Assignment/freeze/handoff/worker object pins and unchanged corpus/author/seals pass; **307** stored/restored archive-entry checks and **11** replay bundles verify. All **33** provenance checks and final clean HEAD/changed-file fingerprints pass.

Initial provenance schema/format assumptions are preserved, with corrections in `auditor-fixture-notes.json`. The parent receipt renamed SHA/clean fields. Older manifests use original uncompressed log pins and semantic raw/manifest checksum keys; correctly mapping opaque stored/restored bytes resolves those initial metadata failures. No candidate, corpus, archive or score was modified.

Parent full gate reports **179/179PASS**, with verified exact candidate/unchanged clean head, exit0, completed `2026-10-06T00:56:45.176603+00:00`,600.80seconds. Receipt SHA256 `b05e48f5ff18ebc91474a6eb20f27a4abddd0dced3550f596eb58a0389df65f9`; complete opaque log SHA256 `33f7a42b797d273f910501823ad5e41395745f88e016c643cccf120ec06acf58`. Auditor did not duplicate the full gate or DEV replay. Required Python37395706278/job112051025918 and artifact37395705962/job112051025311 were last parent-reported running; no terminal current-head CI receipt was available at seal.

Prior6df provider cancellation failure remains unresolved and still needs the retained Claude owner diagnosis/ACK even after later passes; no provider-path investigation or tests were performed here. Builder10300tests+4640subtests,748focusedPASS/one skip, and unchanged scripted80/80source/exact,48/48first,72/80originalE2E,16/16failure honesty remain attributed builder evidence, not model measurement. Original annotation limitations and zero-applicable measures remain disclosed.

No real model/status/endpoint/network/native/personal-data/credential/outbound effects; no heldout/dev corpus/author/raw archive/replay parsing or archived helper execution; no source edits, activation/settings/residency/training, merge/deploy/installed replacement/publication. Structured routing stays default-disabled. Actual-model quality/strict JSON/latency/generalization/resource ACK and applicable live/visual/performance/shipping qualification remain separate and unreleased.

## Handoff

Simulation QA remains held by this BLOCK. Parent should route R9 to the Orchestrator for one recorded repair owner before editing. A repair requires a new frozen SHA, fresh mechanical/required CI and independent review; only a nonblocking same-SHA audit plus explicit parent release admits distinct Simulation QA. No shipping or activation approval follows.
