# Dataset V2 repaired snapshot v2 targeted advisory review

Verdict: **PASS_WITH_NOTES**. The initial review's bounded attribution, checker, and diversity-reporting findings are resolved in this frozen snapshot. No further repair is required by this review. The original snapshot v1 report remains preserved with its original BLOCK verdict.

This is the same fourth independent reviewer, reopening only after accepted scope and explicit release. This is advisory dataset review, not formal Release Auditor credit, approval to merge/activate/deploy, evidence of model quality, tokenizer qualification, or trainer compatibility.

## Exact binding and integrity

- Source base: `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`.
- Reviewed snapshot: `/private/tmp/ling-dataset-v2-review-input-v2`.
- Snapshot manifest SHA256: `4fe27d9ec60788a234f857437af244c3c6ed2c935a5080049a126a7e39c2e391`.
- Train SHA256: `f9830d01ac92d0e351077ec5fc7f53edb7c6c10d9070714bbded762aa934fa7d`.
- Development SHA256: `633a9c448e443a6f3d3b75f516189bd7e6532690c0d13ee2821976e68112455a`.
- QA SHA256: `41839385a023c010ffe226ee282fd88f484ae2db4b3dafd73c1b6fea5548e3ae`.
- Validator SHA256: `dca23b6c576afbc0b0f8f689a99f5f6910ac04eb50d1efb064b4e978ae8d4da2`.

All 40 snapshot file hashes and byte lengths plus the manifest hash were verified at start and completion. After explicit scope clarification, initial V2 snapshot v1 was read only for direct comparison; its 37 files and manifest also remain unchanged. This did not access original older training/development/final/evaluation data. The machine-readable `receipt.json` binds this report, all input hashes, and the supporting check receipts.

## Verified repair

Independent direct row comparison confirms **exactly 98 final-answer changes: 84 train and 14 development**, matching the initial review's exact ID inventory, across **17 surfaces in 8 families**. System/user/prior-assistant messages, prompts, fixture JSON, provenance files, routing rows, and every other final answer remain unchanged. Pilot membership/order is preserved; its updated answers exactly match the repaired training rows. Combined payloads match their shard rows. See `repair-diff.json` and `repaired-surfaces.jsonl`.

All 17 repaired surfaces were manually inspected against their fixtures and the generator diff. Source labels now occur in the relevant fact clauses rather than appended boilerplate:

- calendar/reminder agenda distinguishes event starts from reminder due times;
- calendar/email disagreement labels both conflicting times and leaves authority unresolved;
- note/message proposal keeps the confirmed recorded choice separate from the proposed later message;
- partial five-source results label the calendar item while retaining partial reminders and denied/timeout/missing source statuses;
- three-source preparation labels calendar timing, the named sender's email request, and the note checklist, including shared-title cases;
- handover preserves both supplied timezone/date timestamps and the reminder's distinct due time;
- travel keeps the calendar entry confirmed, the email a draft, and the packing text a note;
- unlinked calendar/note records retain the explicit absence of an established relationship.

Dates, timezones, names, speaker associations, item counts, draft/plan statuses, limits, and factual content are preserved. No invented urgency, deadline, source access, completed effect, or false confirmation was introduced.

The 497 fresh sample wrappers exactly match current rows/provenance and cover all 186 families. They cover 15/17 repaired surfaces. I selected the missing `calendar_note_unlinked-surface-2` (`v2-grounded-dev-0057`) and `zone_handover_with_task-surface-2` (`v2-grounded-dev-0003`) directly from frozen full rows, so targeted semantic review covers all 17. No change to sample packaging is required by this review.

## Checks and evidence

1. Revised QA/controls were inspected, then rerun from advisory copies with only root/import redirection. All **4,400 rows** pass static QA, including the existing **880 history replays**. The rerun receipt exactly matches frozen `validation.json`.
2. **19 negative controls reject** after independently passing baselines; **2 positive controls accept**. The revised controls cover implicit single-source context, natural singular mixed labels, missing mixed labels, swapped fact/source labels, invented confirmation, and invented complete coverage. The wrong-source control preserves factual phrases, ensuring rejection does not depend on dropping required content.
3. Independent generation from copies in the assigned advisory directory reproduces **six files byte-for-byte**: train, development, both provenance files, pilot, and manifest. No source generator or snapshot file was written.
4. Packaging was inspected and executed only in an advisory sandbox. Baseline allowlisted ZIP/hash/CRC verification passes. Changing `train.jsonl` causes `Stale validation: train.jsonl`; changing `qa.py` causes `Stale validation: qa.py`. Both changes were confined to disposable advisory copies. See `runtime-receipt.json`.
5. Expanded identifier statistics are now named `expanded_template_ids`; authored opening prefixes, full request surfaces, assistant-context phrasings, and contrast branches are reported separately with their actual units. No diversity inflation was introduced.

Reviewer-only inspection initially assumed the old sample shape and encountered a KeyError; the fresh sample wrappers use `row`/`provenance`. An initial reviewer assertion that the supplied pack contained all 17 repaired surfaces failed because it contains 15; selecting the two missing surfaces from full rows resolved coverage. These were reviewer-harness assumptions, not candidate validation failures. The final check scripts pass.

## Nonblocking limits

The source-association checker intentionally covers distinctive supplied anchors in clauses naming exactly one source. Clauses naming several sources and other paraphrases are not a general entailment proof. This limit is stated in code and the data card, and the repaired references were independently inspected; no broad scorer bypass was introduced. Single-source answers can rely on fixture context, and mixed-source singular/plural labels are handled naturally.

Initial family/split and routing conclusions remain applicable because those inputs, provenance, and answers are unchanged. Lexical deduplication and declared scene-family separation still cannot establish universal semantic independence or real-world generalization. Tokenizer target limits, actual final-response-only trainer masking, trainer compatibility, measured model uplift, production behavior, and activation/export readiness remain unqualified.

All writes were confined to `/private/tmp/ling-dataset-v2-review/v2/**`. No Git, candidate/snapshot write, new helper, product import/execution, model, network, native source, cloud, old final/evaluation/private, or real-user-data operation occurred. Reviewer is quiescent after delivery.
