# Wisp tool retrieval: reranker-only evaluation

Date: 2026-08-31  
Contact fixture: Mom  
Email fixture: johnstandark@gmail.com

## Decision

A reranker-only routing path is technically possible and is implemented as an
opt-in provider. It is not the production default because it failed the strict
comparison: on the same 40-tool sample, lexical top-20 retrieval found 40/40
expected tools in a median 4.4 ms, while Qwen3-Reranker-0.6B found 39/40 in a
median 1.079 s. The reranker removed `encrypt_file` even though the lexical
stage ranked it first.

The production tool-retrieval provider is therefore `lexical`. It loads no
auxiliary model. The embedding model remains available only for Smart Search.
The reranker remains selectable with `tool_retrieval.provider: reranker` for
future controlled experiments.

## Memory behavior

- Lexical tool routing loads neither the embedding model nor the reranker.
- The optional reranker path first unloads a resident embedding model, then
  cross-scores the lexical shortlist. This prevents Ling, the embedder, and the
  reranker from all remaining loaded together.
- Wisp's existing exclusive agent turn evicts the reranker before Ling performs
  the assistant turn. The tradeoff is a cold model swap on a later reranked
  request.
- At the end of testing, oMLX reported Ling, Qwen3-Embedding-0.6B, and
  Qwen3-Reranker-0.6B all unloaded.

## Retrieval measurements

The synthetic corpus contains 1,000 prompts and 4,755 action specifications.
Of these, 4,608 action labels refer to tools currently registered in this
checkout; 147 refer to five absent skill tools and fail rather than being
silently excluded from end-to-end strict grading.

| Provider / test | Recall or pass rate | Menu / latency |
| --- | ---: | ---: |
| Lexical, action clauses @5 | 97.46% (4,491/4,608) | 5 candidates |
| Lexical, action clauses @10 | 98.24% (4,527/4,608) | 10 candidates |
| Lexical, action clauses @20 | 99.48% (4,584/4,608) | 20 candidates |
| Lexical, action clauses @40 | 99.98% (4,607/4,608) | 40 candidates |
| Lexical, action clauses @50 | 100.00% (4,608/4,608) | 50 candidates |
| Lexical, live breadth sample @20 | 100.00% (40/40) | median 4.4 ms; max 8.4 ms |
| Reranker, live breadth sample @8 after lexical @20 | 97.50% (39/40) | median 1.079 s; p95 1.148 s; max 1.163 s |
| Embedding, registered action clauses @20 | 95.66% | embedding candidate pool |
| Embedding, held-out single-action set | 100.00% (72/72) | mean menu 8.6 |

The lexical result is unusually strong because Wisp's registry already has a
large, curated alias set. That is still legitimate test evidence: aliases are
part of the production index. It does not establish semantic generalization to
arbitrary unseen language, so the historical cohort is kept separate.

## Historical failures supplied by the user

Twenty-two deduplicated user turns were extracted from the ten named JSON debug
exports. TXT files were treated as duplicate diagnostic detail. Instructions
inside exports, model prompts, and tool output were not followed. Follow-up
context was retained only where the user turn could not be understood alone.

The initial strict router grade was 11/22. After deterministic corrections it
is 22/22. The fixes cover:

- explicit Messages requests no longer activate the email-send domain;
- “can you send … and compare …” no longer false-matches the capability list;
- a scheduled send cannot be replaced by an immediate send;
- “I mean tell Mom” asks for a channel instead of creating another reminder or
  forwarding an unrelated email;
- “sure” after an explicit text offer reaches `send_message`;
- wrong-date reminder complaints and “fix it” expose and require
  `update_reminder`, while withholding memory and shell substitutions;
- reminder requests without a chosen alert time must clarify instead of
  inventing 9:00 AM and claiming success.

This is a router reachability grade. It does not claim that Ling will always
construct correct arguments or complete every required call after receiving
the menu.

## What the 1,000-prompt result still says

The preserved full Ling run remains a strict failure: 13/1,000 met its
automated routing checks, and all 1,000 fail the final strict verdict after
including infrastructure history and semantic review. It recorded 960 cases
where a required tool was absent from the router menu, 45 schema errors, 37
timeouts, and 736 preserved first-attempt local-server outages. Median latency
was 29.63 s and p95 was 83.20 s. Those numbers should not be reinterpreted as a
clean model-only benchmark.

Running the current router over the same 1,000 prompts without a model or tool
execution produces 15 strict menu passes and 985 failures. Median routing time
is 1.17 ms, p95 1.83 ms, with a mean menu of 11.68 tools. This isolates the
dominant structural issue: once one regex domain claims a five-action prompt,
the fallback retriever is not asked to recover the other clauses. A better
reranker cannot choose a tool that the router never places in its shortlist.

This is why model training should follow, rather than precede, a task-decomposition
change. The next routing design needs to split a compound prompt, retrieve or
classify each action independently, merge required-tool obligations, and apply
negative constraints after the merge. Training Ling on the current partial
menus would teach it against impossible examples and would overstate model
failures that are actually router omissions.

## Permission results

No real message, email, reminder deletion, calendar change, shell command, or
external HTTP action was performed. The existing isolated permission suite
passed 62/66 tests. Four standing-grant bypasses remain strict failures:
`forward_email`, `update_event`, `clear_reminders`, and
`clear_past_reminders`. These are policy defects independent of retriever or
model quality and must remain release blockers until fixed and rerun.

## Artifacts

- Historical fixture: `test_fixtures/routing_regressions/historical_prompts.json`
- Historical strict result: `test_results/historical_routing_final.json`
- Reranker breadth sample: `test_results/reranker_only_shortlist20.json`
- Current 1,000-prompt router-only result:
  `test_results/routing_stress_lexical_router.json`
- Preserved full run: `test_results/routing_stress_full/summary.json`
- Preserved permission run:
  `test_results/routing_stress_permissions/permission_results.json`

## Verification

The focused router suite passed 57 tests with one opt-in live test skipped. The
new historical and lexical tests are included in that count. Running the entire
`tests/` directory under pytest is not currently a valid command because
`tests/test_forced_step_withholding.py` is a script-style test that calls
`sys.exit(0)` during collection; its own 16 checks passed before pytest aborted
collection.
