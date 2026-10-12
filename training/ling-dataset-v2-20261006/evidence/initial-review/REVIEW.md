# Dataset V2 immutable snapshot v1 advisory review

Verdict: **BLOCK pending the bounded attribution/checker repairs below.** This is independent dataset advice, not formal Release Auditor credit, a model-quality claim, or training readiness approval. Routing semantics and supplied grounded facts are otherwise sound in the reviewed patterns.

Scope: read-only snapshot `/private/tmp/ling-dataset-v2-review-input-v1`, source base `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`, snapshot-manifest SHA256 `3f7c3f7b84cf8c23fcaeb38322e72f41d460f76924ddb665258f43d1ec115ef0`. Only advisory scripts and outputs under `/private/tmp/ling-dataset-v2-review` were written. No Git, old data, final/evaluation/private payloads, product imports/execution, model, network, native-source, or cloud operations were used.

## Review coverage and checks

- Parsed all 4,400 rows/provenance records and all 497 provided compact sample records (193 argument, 212 context, 92 grounded samples).
- Semantically inspected one representative per 186 declared families: 152 routing and 34 grounded. Reviewed all routing generator family/contrast branch definitions, slot-expansion behavior, and all 102 grounded summary/request surface patterns in the grounded generator. Extra targeted inspections covered mixed-source surfaces and their fixtures. The 497 records were not each independently human-screened; pattern review plus representative inspection and full-row checks is the method.
- Independent standard-library CPU script `fullrow_checks.py`: all 3,740 routing targets passed frozen schema, source capability, canonical provenance reconstruction, literal evidence, time-form/date, and final-only annotation checks; all 880 follow-up history replays passed. All 660 grounded rows passed fixture equality, rubric fact membership, supplied counts/statuses, required/forbidden phrases, and no first-person action checks. These are structural/sidecar checks, not universal entailment proof or a pass of the candidate QA.
- Exact and candidate-method normalized cross-split prompt overlap, family overlap, scenario overlap, and ID overlap were zero. The 2,000 pilot rows exactly match unique train rows. The 4,000/400 split and seven competency totals match SPEC.
- Snapshot file hashes were verified before review and again at completion, including the manifest hash. `hashes-before.json` and `hashes-after.json` record equality for every listed file.

## F1 — P2: mixed-source source provenance should be explicit where omitted

No wrong speaker, swapped fact, invented deadline, false confirmation, draft-send claim, or false source-availability fact was found. The bounded defect is loss of visible source provenance in some mixed-source answers. This matters most where sources share a record title or a neutral verb could refer to several media.

Examples with supplied evidence:

- `v2-grounded-train-0578`, `three_source_preparation-surface-2`: calendar `c1.start=14:30`, email `e1.sender=Nia` and `e1.body=Please bring the sample board.`, and notes `n1.text=Checklist: sample board and ruler.` all use title/subject `Harbor 3 review` and date `2029-03-23`. Gold introduces the email quote only as `Nia wrote on 2029-03-23`, so speaker attribution survives but email provenance disappears. Use `Nia's email ...`; introduce the timing as the calendar entry.
- `v2-grounded-train-0501`, `note_message_proposal-surface-0`: notes `n1.status=confirmed` and `n1.text=Use the green cover.` versus messages `m1.speaker=Jonah`, `m1.status=proposed`, `m1.text=Could we try the blue cover instead?`. Gold names a confirmed decision and says Jonah offered a change without identifying the note or message. Use `The confirmed note ...` and `Jonah's message ...`; keep status exactly as supplied.
- `v2-grounded-train-0478`, `mail_calendar_disagreement-surface-2`: the same `Harbor 3 session` appears in calendar `c1.start=15:00` and email `e1.body=The session starts at 16:00.` Gold identifies the email, but introduces the other time only as `is listed`. Use `is listed on the calendar` so both conflicting times have visible provenance.
- `v2-grounded-dev-0043`, `travel_draft_overview-surface-0`: confirmed calendar `c1`, draft email `e1`, and notes `n1` appear as a train, arrival reply, and bag list, with no source labels. Keep the truthful draft/confirmed facts and add brief calendar/email/note labels.

These four patterns account for **37 rows** with the strongest source-provenance weakness. A consistent explicit natural-label gate for all mixed-source summaries identifies another **61 rows** with intelligible event/task associations but omitted source labels. Those 61 are conservative attribution-completeness improvements, not demonstrated false factual attribution. The smallest uniform repair under the parent's proposed mixed-source rule is **98 rows across 17 surfaces in 8 families**:

| Family | Surface suffixes | Rows | Labels to add |
|---|---|---:|---|
| `calendar_reminder_agenda` | 0, 1, 2 | 25 | reminder on 0; calendar/reminder on 1; calendar on 2 |
| `mail_calendar_disagreement` | 2 | 8 | calendar |
| `note_message_proposal` | 0, 2 | 17 | note/message on 0; note on 2 |
| `five_source_partial` | 0, 1 | 17 | calendar |
| `three_source_preparation` | 0, 2 | 17 | calendar on 0; calendar/email on 2 |
| `zone_handover_with_task` | 0, 1, 2 | 6 | calendar/reminder |
| `travel_draft_overview` | 0, 1 | 4 | calendar/email/note on 0; calendar/email on 1 |
| `calendar_note_unlinked` | 0, 2 | 4 | calendar |

Every exact affected ID, surface, omitted label, example answer, and source fixture is recorded in `mixed-source-findings.json`. Repair the corresponding generator surfaces and regenerate; do not append boilerplate source names solely to satisfy a substring count. Preserve the association between each label and its own fact. No routing gold repair is indicated.

## F2 — P2: the exact source-name substring gate rejects valid references

The candidate `qa.py` requires each literal domain string inside every grounded answer. Independently reproduced: **477 rejected rows = 357 single-source answers + 120 mixed-source answers**.

The 357 single-source rejections include valid concise responses such as `2 events` for a calendar fixture, `open tasks` for reminders, and a directly attributed speaker exchange. Repeating a canonical source name contributes nothing when the fixture has one source. Of the 120 mixed-source rejections, **22 already identify all sources naturally**, including singular `note`, `message`, or `reminder`, while the checker demands plural domain spelling. For example, `note_message_proposal-surface-1` says `confirmed note` and `<speaker>'s ... message` and preserves both facts/statuses; it is not missing attribution. The remaining 98 match the explicit mixed-source label repair inventory above.

Minimal honest checker correction: single-source fixture answers need not repeat a domain; mixed-source answers should accept word-boundary singular/plural `reminder(s)`, `email(s)`, `message(s)`, and `note(s)`, plus `calendar`. Do not treat `open`, `confirmed`, or an undifferentiated `listed` as a source label in mixed-source answers. Label presence is necessary under this rule but not sufficient to prove fact-to-source association; retain fact/status checks and fixture-grounded manual review. Add focused controls for a valid singular label, a missing mixed-source label, a wrong-source factual association, and invented action/status/coverage claims. Do not globally bypass attribution or mark QA passing from a diagnostic run.

## F3 — P3: expanded template identifiers are not authored template counts

Context `template_id` encodes family + opening surface + contrast. In this expansion each context row has a distinct expanded ID (1,650 train / 165 dev), although the README correctly reports 54/25 scene families, 20/5 authored opening prefixes, five assistant-context phrasings, and two contrast clauses per family. Combined unique ID counts are 1,863 train / 273 dev. Rename the QA statistic `templates` to `expanded_template_ids` and keep authored counts separately by shard and their actual unit:

- Arguments: 47/26 families; 141/78 full authored request surfaces.
- Context: 54/25 families; 20/5 opening prefixes, five context replies, two family-specific contrast branches per family. These units should not be summed as if they were equivalent full templates.
- Grounded: 24/10 composition families, each with three request and three answer surfaces: 72/30 surface IDs.

This naming issue does not establish split leakage. Families are declared separately before expansion; slot/paraphrase/pair variants stay inside their family split. Train/dev share competencies and closely related contract transformations by design. Inspection found different authored phrasing and compositions rather than per-row family IDs used to hide cross-split slot variants. Lexical normalization is still a heuristic and family naming alone cannot prove universal semantic independence; retain that existing limitation. No request to inflate authored diversity or claim model generalization is justified.

## Repair and re-review boundary

Record a sole repair owner before edits. Repair only the applicable grounded generator surfaces, source-attribution checker/controls, and expanded-ID reporting; regenerate associated new payloads/provenance/manifests. A new frozen snapshot must receive fresh exact-byte checks and targeted independent review of all affected surfaces, associations, and checker controls. Tokenizer target bounds, actual trainer masking/compatibility, and measured model quality remain pending and were not qualified here.

Reviewer is quiescent after this report. No candidate or snapshot files changed, no Git branch/commit was created by this helper, and all outputs are advisory files in the assigned review directory.
