# Synthetic grounded Wisp overviews, V2

Delivered scope: `training/ling-dataset-v2-20261006/shards/grounded/**` only, on base `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe` in worktree branch `codex/ling-dataset-v2`. The author performed no Git staging, commit, push, or other Git operations. Parent owns integration and independent review.

The shard contains 600 train rows and 60 dev rows, with matching provenance. The generator defines 24 train composition families and 10 disjoint dev composition families before expanding slots. Each family has three independently authored request surfaces and three corresponding summary surfaces: 72 train surface IDs and 30 dev surface IDs. Expansion uses 25 train variants or 6 dev variants per family; it produces 183 distinct expanded train request strings and 30 distinct expanded dev request strings. These are authored template/slot examples, not 660 independently authored situations. Shard seed identity is 610220261313; generation is deterministic enumerative expansion and uses no PRNG.

Train situations cover day timing, overlap, distinct timezones, empty calendar results, dated/overdue/undated/partial reminders, email decisions and drafts, timeout and truncated inbox results, speaker attribution, proposed meetings, corrections, actionable notes, conflicting notes, truncated excerpts, and mixed-source agendas and preparation. Dev uses separately authored compositions: cross-date fixed-offset handover plus a task, truncated nonconsecutive week events, canceled versus confirmed appointments, partial reminder dependencies, revised email estimates, three-speaker roles, note decision history, travel with a draft, five-source availability, and an unlinked event/note pair. Broad competencies are shared; composition families are not.

Every example is a fixture-only overview. Its user message contains `Synthetic source results:\n` followed by the exact synthetic fixture. There are no real source reads or effects. Complete empty responses are distinct from denied, missing, and timeout responses; partial and truncated coverage are stated. Both splits contain all six source statuses. Every provenance entry provides item-field facts, source statuses, required literal phrases, and general plus scenario-specific forbidden unsupported claims. Required facts are independently resolvable by source and item ID in the fixture. Count/coverage wording that has no item ID is checked through required phrases and source coverage metadata. Outputs are short paragraphs or natural short bullets; they preserve speakers, dates, timezones, draft status, and proposed versus confirmed plans.

## Reproduction and inert QA

From the dataset worktree:

```sh
python3 training/ling-dataset-v2-20261006/shards/grounded/generate.py
python3 /private/tmp/ling_v2_grounded_qa.py
```

The second command is the scratch audit used for this author handoff, not a permanent repository dependency. It checks all 660 row/provenance pairs: IDs, message shape and final-only training flag, exact fixture extraction, source count/status consistency, required fact references/values, required/forbidden phrases, all six statuses in each split, disjoint family sets, and byte-identical reproduction of all four JSONL files via a fresh `--output-dir` temporary directory. For a separate reproduction:

```sh
python3 training/ling-dataset-v2-20261006/shards/grounded/generate.py --output-dir /private/tmp/ling-v2-grounded-reproduction
```

Final commands passed. Two earlier generation attempts failed on case-sensitive literal-rubric mismatches (`complete` versus `Complete`, then `confirmed` versus `confirms`); both gold surfaces were repaired and full generation/audit rerun. No unresolved failures remain in these inert checks. Fixed-offset Asia/Kolkata and Asia/Tokyo timestamps avoid DST conversion ambiguity in the dev handover fixture.

Measured maxima: train user 821 characters, train target 245 characters; dev user 766 characters, dev target 294 characters. These are character counts, not model token measurements. No tokenizer, weights, inference, native source tools, GPU, cloud, or network were used. Final target token bounds, real trainer label masking/compatibility, independent semantic review, and actual quality/uplift evaluation remain pending. This handoff does not claim training readiness or model improvement. The rubric cannot by itself prove arbitrary paraphrases or exhaustively forbid every possible unsupported statement.

| File | SHA-256 |
| --- | --- |
| train.jsonl | e4d114eab17ab006e77da4a18ac9ce1acf1f105bbd458f9ca801939c1267ea5d |
| train.provenance.jsonl | 593e75b54318df0347625570b2424b663912c121b4e55e5ae83f74a8dc1006f1 |
| dev.jsonl | c4b3687826fcfe2f3512521bcc1aeaae88ceb0f13e0469c64af7ab4753dffe39 |
| dev.provenance.jsonl | 0fca7e119d17a54c495ef1db3129a6d2670f9b9b8feefc249646bdf393c157f2 |

Known integration dependency: generator expects the frozen parent `SPEC.md`, `intent.schema.v1.json`, and `router-system.txt` two directory levels above its source directory. It locates those contracts by source location even when output is redirected. No ownership overlap is expected; all repository writes were confined to the grounded shard. Author is quiescent after this handoff: no further writes unless parent explicitly assigns a repair.


## Bounded G1 attribution repair

The independent initial snapshot review blocked 17 mixed-source summary surfaces in 8 families for omitted visible source labels. Under the acknowledged same-owner reopen and parent release, those surfaces now attach natural calendar, reminder, email, message, or note references directly to their corresponding facts. Labels are part of the relevant sentence, not appended boilerplate. No fact, speaker, date, status, count, fixture, request, split, schema, or frozen contract changed.

Comparison with the read-only initial snapshot `/private/tmp/ling-dataset-v2-review-input-v1/shards/grounded` and its exact findings inventory verified **98 changed final answers: 84 train, 14 dev**, across exactly the assigned 17 surfaces. All other final answers are identical. System/user messages are unchanged, and both provenance files remain byte-identical. The initial snapshot and review evidence were not edited. Current train/dev hashes above supersede initial raw-answer hashes; provenance hashes are unchanged.

Repair validation passed: generation, all 660 fact/status/phrase/message checks through `python3 /private/tmp/ling_v2_grounded_qa.py`, fresh temporary-directory byte reproduction of all four JSONLs, exact expected-ID/surface change inventory, unchanged provenance/prompt/fixture comparison, and natural singular/plural source-label presence in every mixed-source answer. This local presence check alone cannot prove factual associations; the generator's repaired clauses explicitly bind each label to its own item fact, and independent targeted re-review remains the parent's next gate. No additional helper was created, no Git operations occurred, and no parent QA file was edited. No repair-check failures remain. Token bounds and actual trainer masking/compatibility are still pending.

G1 owner is quiescent after this repair handoff: no further writes unless explicitly assigned another bounded repair.
