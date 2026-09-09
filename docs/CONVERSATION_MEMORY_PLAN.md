# Wisp conversation memory plan

Status: proposed implementation; no memory extraction or migration has run.

## Intended behavior

Wisp should carry useful facts from one conversation into another without requiring the user to say “remember this.” It should retrieve relevant facts automatically, distinguish older information from current information, and let the user inspect, correct, and forget what it knows.

Example: “I prefer numbered notes for Project Cedar” in one chat should influence a later request to draft Cedar meeting notes, including after Wisp and oMLX restart. A later correction should replace that preference. Unrelated requests should not receive Cedar context.

Keep SQLite as the durable source of truth. oMLX's SSD KV cache remains an inference optimization; neither memory correctness nor retention may depend on its contents.

## Existing foundations and gaps

- `service/memory/store.py` stores transcripts and per-session summaries in `sessions.db`. Summaries do not carry into new sessions automatically.
- `service/memory/facts.py` stores facts in `facts.db`. Current automatic selection is pinned/recency/category based, with a nominal 2,000-character budget. It is not query-aware.
- `service/tools/memory_tools.py` provides remember, recall, forget, and transcript search. Transcript search currently uses substring matching across the latest 20 sessions by default.
- `service/main.py` and `service/agent/loop.py` inject remembered facts into ordinary model requests. Some grounded delivery workflows intentionally exclude this context.
- Automatic saving currently depends on the model choosing `remember` on a route where that tool is available.
- Existing fact records can lack source links; all 11 records inspected during the audit lacked a session ID. Preserve these as legacy records rather than inventing provenance.
- `service/search/embedder.py` provides local embedding calls, but its document-vector cache is in memory. Persistent conversation retrieval needs its own stored index and model-version metadata.

## Phase 1: Make facts attributable, correctable, and searchable

Extend the fact store through a versioned, transactional migration. Preserve existing facts and the public remember/recall/forget interfaces.

Store a standalone statement, category, subject, fact type, original event time, extraction time, origin (explicit, automatic, legacy), and status (active, superseded, forgotten). Represent evidence separately so one fact can cite multiple source session IDs, turn indices, and exact supporting spans. Track extraction version and source fingerprints for repeatable processing.

Use conservative subject matching: “my sister,” a named contact, and the user's own identity must not be merged based on proximity alone. Unknown people remain unresolved. Do not reconstruct another person's contact details from the user's identity block.

Replace substring-based duplicate handling with exact normalized deduplication plus explicit update handling for confidently identified subject/fact-type pairs. Extraction time must never make an old backfilled statement outrank a newer statement. Explicit corrections supersede previous versions; ambiguous contradictions remain visible as uncertain and can trigger a targeted question when relevant.

Keep forgetting effective across both fact recall and source-transcript retrieval. In the same transaction, remove the fact from active indexes and record a minimal suppression marker so background extraction cannot silently restore it from old evidence. A future explicit request to remember it can override that marker. Define conversation deletion separately: remove derived evidence and indexes; preserve independently saved/pinned facts only under an explicit, visible retention rule.

## Phase 2: Recover facts from existing conversations

Build a resumable backfill worker that processes bounded groups of adjacent turns. Extract from original user messages; use surrounding dialogue only to resolve references. Assistant claims, quoted documents, hypothetical examples, and tool output must not become user facts simply because they occur in a transcript.

Target durable preferences, people and relationships, ongoing projects, stable personal context, and recurring routines. Preserve temporary decisions as dated conversation context rather than permanent personal traits. Do not store credentials. For unusually sensitive details, use an explicit-save policy rather than automatic extraction.

For each candidate, require a structured statement and an exact supporting user-text span. Validate that the source span exists and is attributable to the user; this is necessary but not sufficient, so evaluate factual entailment during the quality pilot too. Abstain when the source does not establish the fact.

Inventory the stored sessions before scanning: the 1,088 local records may include development or test conversations. Exclude records with known test provenance; keep uncertain candidates out of automatic import until the pilot establishes reasonable filters. Do not assume every first-person statement in every record is biographical.

Start with a small representative pilot, including long chats, corrections, quotations, and repeated facts. Produce a source-linked candidate report before bulk promotion. Use its results to tune extraction. Then process the remaining eligible history in the background, with progress, cancellation, and durable checkpoints. Do not mass-import before assessing the pilot.

## Phase 3: Capture new facts independently of routing

After a user turn is committed, enqueue its source ID in a durable memory-work queue. Cover plain chat, tool routes, and direct-response paths. Extract after foreground generation finishes; never wait for extraction before completing the response stream.

Share the existing model resource coordination. Use bounded requests and the resident model when suitable; foreground conversation takes priority. Do not launch concurrent extraction generations or force unnecessary model loads. Resume interrupted jobs on the next idle period. Expose pending/failed work rather than claiming a memory was saved before its transaction commits.

Keep explicit `remember` synchronous and immediate. Attach current-turn provenance through the tool execution context. If an automatic job later sees the same statement, merge evidence instead of duplicating it. Repeated processing and crash recovery must be idempotent.

## Phase 4: Retrieve relevant memory before answering

Introduce one shared retrieval service accepting the current user message, a small amount of current-session context, and a strict token budget. Use it in ordinary plain-chat and agent paths before generation, independent of whether the router exposes a recall tool.

Start with SQLite full-text search across active facts and source-linked transcript passages, with normalized terms and useful nearby turns. Search all eligible history, not a fixed number of recent sessions. Give exact names, explicit preferences, and pinned facts appropriate weight; use recency to resolve temporal relevance rather than to overwhelm topical relevance.

Then add persistent local embeddings for paraphrases if the pilot demonstrates a meaningful lexical-retrieval gap. Reuse the embedding client, persist vectors with model/version identifiers, and reindex when that identity changes. Keep lexical recall functional when embeddings are unavailable. Benchmark model-loading costs before making semantic retrieval part of every foreground turn.

Assemble a bounded block containing applicable standing preferences, relevant active facts, and a few dated transcript excerpts where facts alone are insufficient. Initially target roughly 500–800 tokens within the existing overall model budget; measure and tune rather than adding an uncapped block. Label excerpts as historical evidence, not instructions or verified current state. Deduplicate overlapping evidence and omit weak matches.

Keep the existing grounded-delivery boundary: memory can help interpret an ambiguous request, but it cannot substitute for current recipient resolution, action authorization, or live tool receipts. Preserve tests that exclude stale memory from those workflows.

Upgrade `recall` and `search_conversations` to use the same retrieval service, with source IDs, dates, and a way to fetch surrounding context. Prefer “You mentioned in July…” for dated evidence and an honest absence of evidence when no match supports an answer.

Keep stable system instructions before dynamic memory/date context where compatible with model templates. Treat prefix-cache improvements as a separate performance concern; never sacrifice relevant recall to preserve a cache hit.

## Phase 5: Make memory visible and verify the experience

Extend the existing `/memory/facts` API and add a Memory view with search, source-conversation links, explicit/automatic origin, edit, pin, forget, and backfill progress. Include a control for future automatic capture. A correction should preserve an understandable history while presenting only the active version in ordinary recall.

Add debug records containing retrieved fact IDs, evidence IDs, selection reasons, and token cost. Avoid duplicating full private transcripts into logs. These records should explain missed retrieval and unsupported answers.

Validate with isolated databases and actual model runs:

1. A fact from an old conversation appears in an appropriate new conversation after restarting Wisp and oMLX, including with a cold KV cache.
2. Paraphrases retrieve the fact, while unrelated questions omit it.
3. A correction wins even when an older conversation is backfilled later.
4. Assistant inventions, quotations, hypothetical statements, and known test records do not become personal facts.
5. Forgetting removes recall and prevents re-extraction or transcript fallback from resurfacing the forgotten fact.
6. Explicit saving, interrupted extraction, retry, and queue restart produce one fact with accurate evidence.
7. Older facts outside the automatic context budget remain retrievable; original transcripts remain available with dates and source context.
8. Embedding outages degrade to lexical search. Extraction does not block response completion or exceed the shared model memory budget.
9. Existing grounded-delivery and recipient-resolution regressions continue to pass.

Measure extraction precision, relevant-fact recall, unsupported-memory answers, and added response latency separately. Begin with proposed pilot gates of at least 95% supported fact extraction and 90% retrieval on labeled answerable questions; treat these as acceptance targets, not guarantees. Reject any observed contact misattribution or forgotten-fact resurrection in the regression suite. Measure a device-specific latency budget before wider rollout.

## Delivery sequence

First deliver the migration, provenance, correction/forget semantics, and lexical retrieval. Next validate a small historical extraction pilot. Then enable shared prompt retrieval and durable idle capture, run the full historical backfill, and add semantic retrieval only after measuring its benefit and foreground cost. Ship the Memory view with automatic capture so the feature is inspectable from the outset.

No new cloud service or oMLX cache-format change is required. The desired result is selective, attributable recall across conversations, with SQLite holding durable meaning and oMLX accelerating inference.

## Extension: connect facts across sources

The user also wants Wisp to connect records from contacts, emails, messages, calendar, and documents, including ambiguous matches. Add an evidence-backed relationship layer rather than flattening every inferred connection into a factual sentence.

### Storage location

Use the existing local `~/.moe/facts.db` for facts, entities, relationships, evidence references, candidate matches, and correction/suppression metadata. SQLite tables can represent a graph without a separate graph server. Keep original Wisp transcripts in `~/.moe/sessions.db` and connector records in their existing stores. Store source IDs, versions, dates, and minimal supporting excerpts in memory rather than copying entire mailboxes.

Put rebuildable full-text/embedding indexes in a proposed `~/.moe/memory_index.db`. The durable facts/evidence remain authoritative if this index is deleted. Jobs can live with the fact store; checkpoint by source ID/version. `WISP_HOME` redirects all these Wisp-owned paths in tests. oMLX's existing `~/.omlx/cache` remains separate.

### Distinguish records, people, observations, and hypotheses

Give entities stable IDs: a person, email address, phone number, project, organization, event, or document. Record typed edges with source evidence and temporal validity, such as `user --mother--> person_17`, `contact_record_42 --lists_phone--> phone_8`, or `person_17 --possibly_uses_email--> address_12`.

A Contacts record labeled Mom establishes what the address book says; it does not prove every matching surname belongs to that person or that an old number remains current. Likewise, an email From field establishes the observed sender address; it does not by itself identify the sender as the user's mother.

Keep direct observations, user-confirmed relationships, supported inferences, and unresolved candidates separate. Retrieval similarity is a way to find evidence, not a probability that two people are identical. Do not invent a numerical confidence from a model's assertion; calibrate any future score on labeled cases.

### Example: find a possible email address for Mom

1. Anchor Mom to an existing contact ID or explicit user statement, preserving aliases and known identifiers.
2. Search accessible source records for candidates using name variants, signatures, known phone numbers, and relevant correspondence. A surname substring is a weak candidate-generation signal; its absence is not disproof.
3. Inspect actual sender/recipient fields and the original, unquoted message body. Distinguish a signature belonging to the author from one inside a forwarded chain. Repetition of one quoted message counts as one piece of evidence.
4. Seek stronger connections: a signature with the exact known phone number, an already linked account announcing a new address, a message from the known phone sharing an address, or a user reply clearly identifying the correspondent as Mom. Shared-account, conflicting-identity, and stale-source evidence can weaken the match. Name and email-name agreement are correlated and must not count as independent confirmations.
5. Save the address as a candidate with an explanation and competing candidates until sufficiently supported. Wisp can answer “This appears likely because…” during exploration. If identification remains ambiguous when the user wants to send, ask a narrow confirmation naming the candidate; do not silently use a surname guess as the recipient.
6. Store a confirmed link so later requests can reuse it, with revalidation if conflicting or newer evidence arrives. A memory link must still resolve to the actual address/source record during action execution and is not authorization to send.

### Generalize beyond email

- Link a project nickname in chat to a folder, calendar meeting, and email thread using shared participants, distinctive document references, and explicit associations. Common words alone leave separate candidates.
- Connect a flight receipt and hotel reservation to a trip through overlapping dates, destination, and named traveler; distinguish suggestions from booked reservations.
- Resolve “Alex from work” using organization, team, calendar participants, and past user references while retaining other Alex contacts separately.
- Link an invoice, purchase receipt, and delivery update using exact order IDs and merchant identity; do not infer that a billed invoice was paid.
- Link a standing communication preference to the correct project/person and scope. A preference for concise work updates need not become a global preference for every answer.

Use bounded graph traversal to retrieve the related evidence needed by the question. Uncertainty must propagate through the chain: a possible Mom-to-address link cannot produce a certain Mom-to-employer claim. Track derived dependencies so corrections and forgetting invalidate downstream claims and index entries.

### Rollout and validation

Build the first version around confirmed identities and exact cross-source identifiers. Add ambiguous candidate generation and evidence ranking after that foundation works. Run historical linking in the background over already authorized, available connector data; this plan update does not initiate an email scan or expand connector access.

Add cases for shared surnames, duplicate names, married names, shared inboxes, forwarded signatures, old addresses, correlated evidence, multiple candidates, and a mistaken link later corrected. Measure false merges separately from missed links. A useful unresolved suggestion is preferable to permanently merging two different people.

## Extension: communicate uncertainty and choose the investigation model

The user explicitly wants to be informed about uncertain connections and is considering Ornith Research versus Ling for investigating them.

### User-visible uncertain connections

Show proposed links in a “Possible connections” area of Memory, visibly separate from established facts. Each card names the proposed relationship, its supporting sources, what remains uncertain, and alternatives or contradictory evidence. Provide Confirm, Incorrect, and Later controls. A rejection suppresses the same proposal unless materially new evidence arrives; Later preserves it as unresolved and does not authorize use as fact.

If an uncertain connection is relevant to the current answer, disclose it inline before relying on it. For example: “This may be Mom's email: the surname matches, but I haven't found a phone-number match or another direct link.” Ask a focused confirmation if an action depends on resolving it. Discoveries made in a background pass appear in a grouped review notice, not a succession of interrupting popups. Every retained low-confidence proposal is inspectable; mere weak search hits need not become proposed relationships.

Use evidence labels such as Confirmed, Supported inference, and Possible match initially. Do not present model-generated percentages as probabilities. Numerical match probabilities require a calibrated matching system, held-out labeled examples, and calibration/error measurements on the installed pipeline. If that is introduced, explain the evidence alongside the number and retain conservative action requirements.

### Proposed division of work

Use deterministic normalization, exact identifiers, and indexed retrieval for inexpensive matching. Use resident Ling for routine fact extraction, resolving straightforward references, and identifying which evidence is missing. Escalate a bounded set of unresolved, useful questions to an idle investigation workflow. Ornith is a candidate for that role because Wisp already assigns it Research, not because a head-to-head memory evaluation has established superiority.

Adapt the research orchestration pattern to local evidence: candidate question -> targeted connector reads -> supporting and contradicting evidence -> revised candidates -> evidence-backed result or unresolved stop. The current Research implementation searches the public web and generates reports. It is not already a private-data relationship investigator. Reuse its resumable state, budgets, and evidence tracking, but implement a local-source adapter and a structured relationship result. Do not send private identity clues to public-web search as a consequence of model routing.

The host chooses allowed read operations and validates source references; the model proposes bounded searches and interprets the returned evidence. Neither model may turn agreement between two model outputs into independent source corroboration. Stop when evidence is adequate, further searches add nothing, or the budget is exhausted. More reasoning on the same surname match is not new evidence.

Current local research code runs Ornith exclusively and waits for foreground work at call boundaries. Batch investigations into idle periods to avoid repeated Ling/Ornith swaps, keep individual calls bounded, and measure interruption latency. Ornith's structured extraction currently needs thinking disabled after a recorded failure to return JSON within its token budget; retain format validation and evaluate that setting separately from evidence-comparison reasoning.

### Decide with a task-specific comparison

Evaluate the exact installed Ling and Ornith variants on the same labeled evidence packets, then separately compare their search decisions in a controlled local-source harness. Include straightforward exact matches, genuine ambiguous matches, insufficient evidence, misleading surnames, quoted signatures, shared inboxes, corrections, and contradictions.

Score supported extraction, false identity merges, retrieval coverage, appropriate abstention, explanation quality, valid structured output, latency, and model-swap cost. Prefer Ling alone if Ornith adds no meaningful improvement. Route only the categories where Ornith demonstrates a worthwhile advantage; do not assume the Research label establishes that advantage. No live model evaluation or investigation was initiated by this plan amendment.

## Worktree implementation checkpoint (2026-09-08)

This checkpoint supersedes the proposal-only status above for the implemented
conversation-memory core. Base: `efbca1f`, from the requested typed-task-engine
checkpoint. Implementation and validation run in a separate Codex worktree.
No live Wisp database was opened or changed, and no connector scan, historical
extraction, or live model investigation was performed during this work.

Delivered behavior:

- Fact migration is transactional and versioned (`memory_schema` version 2).
  Existing IDs, text, timestamps, pins, and unknown legacy provenance remain.
  Future schema versions fail without a partial downgrade. Evidence records
  carry exact source fingerprints and extractor versions for newly saved data.
- Automatic capture accepts complete verbatim user statements, applies local
  attribution/sensitivity guards, and validates the entire extraction before
  committing its batch. Explicit tool saves require a current user save request
  and exact, qualified wording. Inferred identities and assistant statements
  cannot become explicit memories through a free-form tool paraphrase.
- Correction links preserve prior versions. Explicit review selects which
  active statement is superseded; dates alone do not overwrite it. Conflicting
  residence/employment/name/preference slots become proposals. Historical
  extraction always produces proposals, regardless of extraction time.
- Forgetting clears every version and its evidence in one fact-store transaction,
  retains only suppression hashes/source identifiers, and suppresses matching
  transcript fallback and assistant paraphrases of suppressed user turns.
  A new explicit save may restore a statement without unblocking old sources.
- Conversation deletion is a separate retention action: source evidence and
  unreviewed, unpinned derived records are removed; explicit saves, confirmed
  memories and pinned memories remain. Deleting an old source does not delete
  its separately confirmed correction. Original chat history is retained by
  Forget, and the UI explains this distinction.
- Turn persistence transactionally indexes/enqueues user turns across the
  existing `/agent` shortcuts and normal chat/agent paths. Edited turns advance
  a generation. Claims are atomic across SQLite connections and carry unique
  tokens, so recovered/stale workers cannot complete or overwrite newer jobs.
  Batch replay deduplicates facts and evidence. Failed jobs can be retried;
  bounded historical pilots can be cancelled, including running claims.
- Background extraction uses an already resident model, never loads/swaps one,
  and yields to foreground work through cancellation and durable requeueing.
  Request-source evidence is bound to the eventual persisted user turn in
  `service/main.py`; that helper also prevents duplicate user-turn persistence
  when a later stage fails. The low-level stateless `/chat` completion API
  remains stateless.
- `service/memory/retrieval.py` supplies query-aware facts and user passages to
  plain/agent prompt memory, recall, conversation search and the review search
  API. Retrieval covers full history, checks source validity, respects scoped
  preferences, and filters forgotten evidence. FTS5 has a deterministic lexical
  fallback. Prompt context has a conservative 800 UTF-8-byte/token-upper-bound
  budget (also honoring a smaller caller character cap), including provenance
  labels. Whole statements retain qualifiers. Debug output contains IDs,
  selection reasons and budgets, not another copy of private text. Tool results
  have a separate 12 KB budget and can retrieve filtered surrounding context.
- The Memory window searches on the server, shows evidence and correction
  history, supports explicit replacement selection, pinning, forgetting and
  capture control, and reports queue progress/failures. Source inspection uses
  filtered excerpts instead of fetching the complete raw transcript.

Separate rollout gates and known limits:

- Connection investigations are disabled by default in both the API and worker
  (`investigations_enabled`); the standard review window does not initiate them.
  Existing synthetic investigation tests cover proposal mechanics only, not
  model quality or contact ownership. The experimental path is not approved
  for production identity resolution.
- Historical capture is manual and bounded to 1-100 selected recent sessions,
  at most 2,000 user turns per request, and review-only. No automatic historical
  pilot or mass backfill is enabled. Conversation provenance remains incomplete
  for old data, so historical first-person statements are never auto-confirmed.
- Retrieval is lexical, with conservative scoping and byte budgeting. Persistent
  embeddings, semantic recall quality, and live model extraction precision are
  unvalidated and remain separate work. Local sensitivity guards deliberately
  abstain on suspicious content but are not a complete semantic classifier.
- Functional forgetting suppresses exact normalized statement fragments and
  source-linked replies. It cannot identify every independently paraphrased
  historical statement without provenance, and it does not promise forensic
  erasure from SQLite free pages, backups, or original chat history.
- The deployed service owns one background memory worker. Claim tokens protect
  duplicate/stale completion, but this is not a distributed lease scheduler.
- Source deletion and fact retention use separate SQLite databases. Normal
  deletion is coordinated; automatic retrieval also rejects missing/changed
  sources. Startup reconciliation completes retention if a crash interrupts
  the two database commits.

Integration dependencies: `service/main.py` is the only shared integration path
changed in this checkpoint. Preserve the `persist_user_turn` helper and the
source binding when merging concurrent task-engine changes. No edits are made
to `service/agent/loop.py` or `service/memory/store.py`; their existing prompt
and turn-persistence hooks are reused. Grounded delivery keeps its existing
memory-exclusion guard.

Validation for this checkpoint:

- 158 isolated Python tests passed across `test_conversation_memory.py`,
  `test_workflow_engine.py`, `test_typed_message_send.py`,
  `test_typed_email_reply.py`, and `test_typed_task_engine.py`. The single
  `test_agent_endpoint_dry_run_uses_typed_plan_without_model_or_effect` case
  was deselected because its setup does not guarantee offline model access.
  `WISP_HOME` was created with `mktemp` before every Python process; bytecode
  writes and pytest cache writes were disabled. Existing Starlette/httpx
  deprecation warning remains.
- Swift debug build passed using temporary scratch/module/cache directories
  and temporary `WISP_HOME`. Existing macOS API deprecation and Vision
  Sendable warnings remain outside the owned memory view.
- Initial checks exposed a missing public `key` compatibility alias and Swift
  actor-await errors; both were corrected. A new retention fixture initially
  reused an intentionally suppressed source; it was corrected to use a real,
  separate source turn. Final checks passed after those changes.
- No live model, connector, real conversation database, or packaged app launch
  was used. Manual UI interaction and live extraction quality remain rollout
  validation, not claims established by these checks.

Changed paths for integration: `service/memory/facts.py`,
`service/memory/queue.py`, `service/memory/capture.py`,
`service/memory/retrieval.py` (new), `service/memory/api.py`,
`service/tools/memory_tools.py`, `service/main.py`,
`app/Sources/WispApp/MemoryView.swift`, `tests/test_conversation_memory.py`,
and this plan. Settings and overlay entry points are reused without changes.
