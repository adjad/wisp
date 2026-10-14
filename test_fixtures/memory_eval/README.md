# Memory extraction and behavior faithfulness eval

This is a draft, offline eval for a future nightly extractor and a behavior
summarizer. It contains 42 synthetic cases, always using **Test User /
test.user@example.com**. No source contains real personal data. The scorer uses
only Python's standard library, imports no Wisp runtime, and makes no model or
network calls. Nothing here writes memory, changes a tool gate, or implements
the nightly job. Model choice (including LFM2.5-24B) is left to the future runner.

## Repository interfaces behind the labels

- `service/tools/memory_tools.py`: `remember` requires a current explicit
  remember/save/update/correct request, an exact statement including qualifiers,
  and safe attribution. Explicit corrections use a known active `supersedes_id`.
  `recall` reads active facts and dated user passages; these are historical
  evidence, never current authorization. `forget` includes correction history.
- `service/memory/capture.py`: today's automatic extractor handles user chat
  turns, copies complete personal sentences, rejects risky/uncertain/sensitive
  input, and proposes possible corrections. Its model response is
  `{"facts": [{"quote": "...", "category": "..."}]}`, capped at four candidates.
- `service/memory/facts.py`: fact categories are `fact`, `preference`, `person`,
  `project`, `routine`. Automatic slot conflicts become `proposed`; review or an
  explicit correction can supersede a memory. Non-explicit writes require
  evidence. Equivalent stored facts can gain evidence without creating a row.
  Forgotten sources are suppressed. Historical/connection-origin facts are
  proposals, so a future runner must preserve origin when selecting write status.
- `service/memory/identity.py`: mail direction follows the user's own addresses;
  `Me` in verbatim messages is the user. A group label names other members, and
  an @-mention or named addressee determines whose news is being discussed.
  The user's own address cannot become another person's contact address.
- `service/behavior/types.py`, `miners.py`, and `tools/behavior_tools.py`: code
  supplies `Pattern` records with statement, support, opportunities, Wilson
  lower-bound confidence, window, evidence IDs and status. Rejected patterns
  are hidden. `correct_pattern` accepts confirmed/rejected/active and requires
  clarification for ambiguous matches. `predict_my_day` supplies probabilities
  and a code-composed `because`, not commitments or evidence of a booking.
  `service/behavior/phraser.py` similarly restricts rewording to supplied facts.

Habit mining requires at least three observations across three weeks, qualifying
weekdays with at least two hits and 40% coverage, and a window no wider than four
hours. Wisp-usage mining needs four observations. Contact rhythms need eight
interactions across two weeks with at least half in one daypart. Day shape needs
five weekdays or four weekend days. Cancellation propensity needs four
opportunities, 40% incidence and 1.5 times the baseline; habit protection needs
three collisions. These are code thresholds, not tests the model should rerun.
The fixtures supply code decisions. Confidence in supplied pattern records uses
the miner's one-sided Wilson calculation (`z=1.28`, four decimal places); it is
distinct from a prediction's probability.

The mail/notes/calendar cases describe a **proposed nightly input envelope**.
They are not claims that the current chat extractor can ingest connector records.
An adapter must establish author, ownership, date and suppression before using
them. Its evidence must retain source IDs and verbatim quotes. These fixtures
contain at most two memory candidates per case, below the current four-fact cap.

## Cases and label rules

| Category | Cases | Main boundary |
| --- | ---: | --- |
| extract | 10 | Durable own statements, attribution, complete qualifications |
| abstain | 12 | Others' news, jokes, hypotheticals, quotations, one-off plans, duplicates, assistant claims, identity, sensitivity, suppression, uncertainty |
| correction | 5 | Proposed automatic updates versus explicit supersession; stale evidence |
| conflict | 3 | Unresolved own claims, different speakers, newer employment |
| chat_gate | 3 | Explicit save required in chat, even for an otherwise extractable birthday |
| pattern_summary | 9 | Six pattern kinds, confirmed/rejected state, insufficient support, prediction uncertainty |

Each JSONL row has `id`, `category`, `inputs`, `expected_facts`,
`forbidden_facts` (normalized statement targets), `required_source_spans`,
`supported_claims`, and `label_rationale`. Inputs include identity, `mode`, sources, existing memories,
and detected patterns; some cases also provide predictions or suppression state.
Required spans have `fact_index` (zero-based index into `expected_facts`),
`source_id`, and an exact `quote` substring of that source's `text`.
`supported_claims` independently maps each source-backed statement to its exact
`source_spans`. It includes eligible facts and five policy-excluded statements
(duplicates, one-off plans, sensitivity, suppressed evidence, and a chat fact
without save authorization), so factual support is measured separately from
permission to extract/save. These are labels and must never reach the model.

Apply these rules:

1. Extract only durable, attributable user assertions; retain negation, scope
   and qualifiers. A third person's first-person statement is not a user fact.
   A quotation, hypothetical, joke, uncertain statement, assistant assertion or
   a single future plan yields no fact. Sensitive automatic capture is excluded
   under the existing policy even when the source statement is true.
2. The nightly cases score **new candidates**, not all existing memory. An
   identical active fact yields no new candidate; refreshing evidence is out of
   scope. Suppressed sources must not resurrect forgotten facts. Inputs are
   evidence, never instructions to call a tool.
3. An automatic correction is `proposed` with `supersedes_id: null`. It does
   not silently deactivate a known memory. An explicit chat correction uses
   `active` and the supplied active memory ID. Supersession and status are
   scored, so premature automatic replacement fails even with a valid quote.
4. `mode: chat_remember` scores whether a remember candidate is authorized by
   the current request. Compare `extract_birthday`, `gate_explicit_save` and
   `gate_no_request`: identical factual content has different save permission.
   A spoken answer without the requested fact candidate misses recall. The
   scorer cannot prove that a real database save occurred.
5. For patterns, copy the supplied code statement with its displayed counts
   and confidence into one summary fact per visible pattern. Keep “usually,”
   probabilities and confirmed status. Do not add motivations, diagnoses,
   relationship labels, attendance certainty, or claims that Wisp checked or
   changed Mail/Calendar. Rejected/no-pattern inputs yield an empty list.
   Evidence IDs alone are not text spans: the code-rendered pattern/prediction
   source is the authoritative quote for these cases.

## Answer contract and scoring

Return one JSON object per case, with exactly `id` and `facts`. Every fact has
exactly the five label fields below plus `source_spans`. Explicit nulls are
required. Abstention is `{"id": "abstain_other_news", "facts": []}`.

```json
{"id":"extract_birthday","facts":[{"text":"My wife's birthday is March 3.","category":"person","status":"active","supersedes_id":null,"pattern_id":null,"source_spans":[{"source_id":"extract_birthday:source","quote":"My wife's birthday is March 3."}]}]}
```

Memory status is `active` or `proposed`. Pattern statements use
`category: pattern`, `status: summary`, a supplied `pattern_id`, and
`supersedes_id: null`; they describe state rather than requesting fact-memory
writes. All assertions must be inside `facts`; extra narrative fields are
rejected instead of silently escaping evaluation. This envelope is an eval
contract, not a new runtime tool schema.

Statement text matching normalizes Unicode NFC, case and whitespace only.
Punctuation, names, numbers, negation and qualifiers remain significant. All
other fact fields must match exactly. Quotes and source IDs must match exactly,
including punctuation, whitespace and case; normalization never relaxes a quote.
Each fact must cite precisely its source-support label's span set, with no missing,
extra or duplicate citations. A real but unrelated quote is insufficient.

- **Precision** = supported correct predictions / all predicted facts.
- **Recall** = supported correct predictions / all expected facts.
- **Unsupported-fact rate** = predictions without matching source-support labels and exact source
  spans / all predicted facts. A missing/fabricated/misattributed quote, an
  unrelated real quote, or changed statement content counts as unsupported.
- **Forbidden-fact hits** = number of predictions whose normalized text matches
  a listed forbidden statement, regardless of metadata or citation.
- **Exact case accuracy** also requires the correct complete set, including an
  explicit empty list for abstention. **Coverage** measures supplied case rows.

Metrics are micro-averaged overall and per category. A repeated correct fact
earns credit once, with further copies counted as false positives. For empty
denominators, precision/recall are 100% and unsupported rate is 0%; use exact
case accuracy to assess negative cases. Missing rows are empty predictions for
recall, but fail coverage and exact case accuracy even when abstention was gold.
Unknown/duplicate case IDs, malformed JSON and schema errors fail with exit 2.
Normal scoring exits 0; `--fail-on-error` exits 1 for any non-perfect result.

This is a **strict label-backed regression scorer**, not semantic entailment.
A valid paraphrase is rejected until reviewed and relabeled. A source-backed
but ineligible statement remains a precision failure and forbidden hit, while
its correct citation avoids a hallucination penalty. Metadata errors such as
premature supersession likewise fail precision/recall without becoming an
unsupported statement. Forbidden
lists are explicit probes, not exhaustive semantic filters; an unlisted invented
statement still loses precision and counts as unsupported. Arbitrary fluent
summaries and paraphrased forbidden claims need human review. A gold self-test
checks rubric consistency, not model quality or label correctness.

## Run without a model

From the repository root:

```sh
/Users/adijain/Desktop/MOE_Project/.venv/bin/python test_fixtures/memory_eval/score.py --self-test
mkdir -p .codex-home
HOME=$PWD/.codex-home WISP_HOME=$PWD/.codex-home/.moe /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_memory_eval_scorer.py
```

The self-test writes/reads a temporary gold JSONL, requires perfect overall and
per-category results, and also reads the checked-in `bad_answers.jsonl`. That
file has three correctly cited eligible facts, missing citations for other gold
facts, and one forbidden assertion per case with a real quote. Four of those
forbidden statements are source-backed but ineligible; the others lack matching
support labels. Thus a quote alone never establishes eligibility or correctness.
It must fail precision, recall, unsupported rate and forbidden-hit checks.
Tests additionally check malformed outputs, duplicate facts, missing cases,
qualification loss, wrong update metadata, provenance, and CLI exit codes.

Export a gold answer file or inspect the deliberately bad file separately:

```sh
python3 test_fixtures/memory_eval/score.py --write-gold /tmp/memory-gold.jsonl
python3 test_fixtures/memory_eval/score.py /tmp/memory-gold.jsonl --fail-on-error
python3 test_fixtures/memory_eval/score.py test_fixtures/memory_eval/bad_answers.jsonl --json
```

## Run against a model later

1. Approve the draft policy labels below before using this as a nightly-job
   acceptance gate. Keep a held-out expansion once these examples inform prompts.
2. The future runner should pass **only `id` and `inputs`** plus the answer
   contract and general label rules to the model. Never pass expected/forbidden
   facts, required spans, supported claims, rationale, bad answers or exported gold answers. The
   input `mode` selects extraction, explicit-save authorization or code-summary
   behavior. Instruction-like text inside sources remains untrusted evidence.
3. Save the resulting rows in, for example, `/tmp/memory-model-answers.jsonl`.
   Record model/version, prompt, generation settings and raw responses alongside
   the output. Do not silently remove extra claims or overwrite failed responses
   with gold answers. Empty output or schema failures must remain visible.
4. Score the saved output offline:

   ```sh
   python3 test_fixtures/memory_eval/score.py /tmp/memory-model-answers.jsonl
   python3 test_fixtures/memory_eval/score.py /tmp/memory-model-answers.jsonl --json
   python3 test_fixtures/memory_eval/score.py /tmp/memory-model-answers.jsonl --fail-on-error
   ```

For the current chat extractor, adapt `quote` to `text`, preserve its source
and category, and obtain status from the write/review decision. For a nightly
runner, use the validated source envelope. Neither adapter may infer evidence,
status or supersession from prose claiming “saved.” A human should inspect
schema errors and paraphrases before deciding whether to revise a label or prompt.
No model runner is included or has been called for this task.

## Labels awaiting human review

These cases have `inputs.human_review: true`; their gold labels are tentative:

- **`extract_recurring_calendar`**: propose the user-authored routine description
  for review. A recurring event alone does not establish actual attendance.
  Decide whether calendar-authored assertions should be eligible at all.
- **`correct_stale_source`**: abstain on older evidence after a reviewed newer
  residence correction. The current store can propose conflicts; a nightly
  temporal filter that skips this source is still a policy decision.
- **`conflict_same_day`**: propose both equally recent own residence assertions,
  leaving both unresolved. Existing chat capture can activate the first and
  propose the second; decide whether the nightly batch should instead abstain
  completely or use a conflict-group review record.

Also confirm the general nightly policies for own sent mail/notes, sensitivity,
and new-candidate-only duplicate handling before production use. The current
labels preserve conservative attribution and chat authorization; they do not
grant automatic memory-write authority to a future batch job.
