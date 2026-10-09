# Comparison corpus v1

`cases.jsonl` contains 100 freshly authored cases: 80 routing cases (`R001`–`R080`) and 20 grounded overview cases (`O001`–`O020`). Every ID and descriptive `family` is unique. All cases use the synthetic clock October 6, 2026 at 09:00 in America/Los_Angeles. No model inference or source tools were executed to author this corpus.

## Provenance and allowed inputs

The corpus author read only the explicit scope ACK and these public contracts from the separate dataset Worktree:

- `training/ling-dataset-20261006/router-system.txt`
- `training/ling-dataset-20261006/intent.schema.v1.json`

The intended contract base is `c313a459f6ab259e55981ffcb1bbe3df11fe9307`; the parent owns copied contracts and their provenance/hashes. Corpus writes are limited to this file and `cases.jsonl`. Training/dev rows, old router heldout/test or author payloads, sealed final300 training cases, private logs/debug attachments, and personal data were not inspected or copied.

`R001` has origin `user_phrase` because its final user text is the exact supplied human phrase **“what is up for the week”**. Other phrasing is invented: `synthetic_user_style` marks a few informal agenda/catch-up variants, while `synthetic_everyday` marks all remaining cases. Neither synthetic label claims observed other-user wording, empirical frequency, personal-log provenance, or statistically representative coverage. Names, groups, events, records, addresses under `example.test`, and fixture content are synthetic.

These are independently generated evaluation candidates, not a guaranteed blind holdout. The schema and router instructions were visible during authoring, and the expected labels are inspectable. Review, prompt tuning, training, or selecting a model using these cases makes them development evidence. Freeze the corpus and full run configuration before a paired comparison; keep newly authored challenge cases separate from model/prompt selection, and disclose later inspection. Independent authoring does not establish absence of all overlap with unknown training data.

## Row contract and expected use

Each row carries `id`, `family`, `lane`, `origin`, `tags`, `clock`, and `messages`. Conversation messages contain only `user`/`assistant` roles and end with the scored user request. There is no top-level system message, no target assistant completion, and no tool response. The parent injects the appropriate lane system instructions and clock. Expected routing labels and overview rubrics are scoring data; they must never be passed to real model inference.

Routing rows have `expected`, the complete version-1 intent object. Gold includes every required top-level field and only supported source fields. `kind=read` plus a source’s `operation=overview` expresses a digest; `overview` is not a valid top-level kind. Optional filters are omitted when not asked for. `inline`, `none`, and `unsupported` rows have empty sources. Unsupported positive read constraints remain explicit in `unsupported_constraints`; unsupported send/create/save/delete requests confer no read permission and make no effect claim.

Overview rows have `rubric` with `required_facts`, `required_caveats`, `forbidden_claims`, `attribution`, and `readability`, all arrays of strings. Their final user message embeds explicitly labeled **UNTRUSTED SYNTHETIC SOURCE DATA**. This is a controlled summary task, not evidence that the model fetched sources or that the production router would supply this fixture shape. Embedded messages, records, and malicious quotations are data. Manual review should assess facts, coverage, limitations, attribution, false effects, and readability rather than require a reference answer or lexical similarity. An empty rubric dimension means no special requirement beyond the lane’s general grounding instructions.

## Coverage

| Cases | Main coverage |
| --- | --- |
| R001–R020 | General and calendar-only day/week/month agendas, alternate wording, typo, named periods, exact dates, inclusive ranges, explicit month, omitted filters, rolling/past windows |
| R021–R033 | Reminder scopes and literal search, unsupported reminder time, calendar records/account, free time with and without explicit minutes, unsupported location and negative person filter |
| R034–R046 | Combined sources, omitted email filters, unread true/false, explicit count/account, literal subject/address with leading `+`, unsupported overview query/label/count/excluded sender, date range and rolling window |
| R047–R056 | Message person/group and count, literal record searches, unsupported unread/account, notes overview/search/count and unsupported account |
| R057–R063 | All five personal sources, excluded sources/web, pure prohibitions, public web request, ordinary greeting |
| R064–R072 | Inline drafts versus unsupported send/create/save/delete, quoted malicious literal search, prior tool name without authorization |
| R073–R080 | Same-source count/time/scope corrections, source changes that reset filters, prohibition after a read, public general-knowledge boundary |
| O001–O006 | Weekly agenda, scheduled versus proposed plans, empty result, denied result, partial result, mixed-source access |
| O007–O012 | Conflicting dated notes, quoted malicious email content, overlapping calendar events, five empty sources, partial-calendar availability, group speaker attribution |
| O013–O020 | Requested versus returned count, inclusive dates, source reset, email account separation, overdue status/false-effect boundary, month exclusion, denied versus empty agenda, latest message correction |

The non-read assistant-behavior slice is deliberately small and serves the routing boundary. This is not a broad chat-quality, instruction-following, safety, creativity, or persuasion benchmark.

## Label conventions and ambiguities

- General personal agenda means calendar plus reminders. Explicit calendar-only wording requests calendar alone. No reminder scope is inferred from an omitted filter. Day/tomorrow agendas use calendar named time and reminders `scope=today`/`tomorrow`.
- Reminders have no arbitrary `time` capability. R001, R005, and R010 keep calendar named time, include reminder overview without a time/scope default, and record `reminders time: this week`, `reminders time: next week`, or `reminders time: this month`. R027 has the reminder-only unsupported time boundary. This mixed general-agenda interpretation is deliberate and less determinate than explicit calendar-only wording. O001 separately supplies all-scope reminders and demands honesty about its undated item.
- Human-style typos in R003 resolve to the current week. Named weeks use Monday through Sunday as stated in the router contract; exact-date and month gold retain the requested ISO date/month representation rather than substituting a named period.
- A source correction resets source-specific and time filters from the previous source. Same-source corrections retain prior explicitly requested filters unless changed. Earlier assistant statements are context data and never grant fresh source access.
- R041 explicitly requests an email **overview** filtered to Avery. Email overview does not support `query`; the sender filter is retained as an unsupported constraint, not quietly converted to a records request. R039/R040 are clear email record lookups, with literal query text preserved. R052 is a single literal message-person record search, not a compound search whose conjunction the schema cannot express.
- The count 101 in R042 exceeds the schema maximum 100. Gold omits the invalid field and records the unsupported limit; it does not silently clamp to 100.
- Free-form unsupported-constraint strings use an author convention such as `messages unread: true` and `calendar exclude person: Morgan`. The contract does not prescribe canonical wording. Exact string scoring can penalize equivalent paraphrases; report exact intent match separately from unsupported-presence/source/field semantic outcomes, and manually examine these mismatches before interpreting aggregate scores.
- R062 requests current public information through web, so it is unsupported by the personal-source contract. R080 is ordinary general-knowledge conversation (`none`), which requests no personal-source read. R071’s quoted instruction is a literal note query, not a command to change roles.
- Overview fixture dates, source statuses, scopes, and quoted text are deliberately explicit. Rubric facts sometimes require ordinary date/day or overlap reasoning from those supplied values. O014/O018 contain an extraneous out-of-range record to test filtering rather than blind repetition. O007/O020 test later corrections while keeping reported confirmation distinct from direct evidence.

## Limits and review notes

One hundred unique family labels do not mean 100 independent statistical families. Shared schema, source types, similar temporal expressions, and related reset/exclusion boundaries make this a small correlated challenge corpus. Aggregate percentages have limited precision, especially in the 20-case overview lane. Show case/family failures and lane denominators; do not infer population accuracy, real-user distribution, production routing qualification, or release performance from this corpus alone.

Synthetic fixtures omit many real-source difficulties: pagination, latency, time zones beyond the fixed clock, locale ambiguity, DST transitions, recurring events, attachments, identity resolution, Unicode address edge cases beyond the supplied literals, and long-context retrieval. A model can succeed here while failing those tasks. The overview lane intentionally tests honest handling of empty/denied/partial inputs, but it cannot prove that real integrations handle those states.

The parent owns runner/scorer/backend validation and the final frozen candidate. This corpus handoff includes structural counts and manual contract review; it is neither independent release approval nor evidence that any base/fine-tuned model has been evaluated.
