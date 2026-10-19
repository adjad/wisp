# Wisp assistant task corpus v1

This is a compact, human-readable acceptance corpus for testing Wisp with the kinds of requests people make in chat. It covers everyday reads, grounded answers, follow-up corrections, clarification, and actions that must stay behind a confirmation. All people, records, paths, and message contents in the fixtures are synthetic.

The corpus contains 48 cases in JSON Lines format. The `smoke` split is a short product check, `regression` covers common multi-step and follow-up behavior, and `challenge` targets live-source grounding, ambiguity, unsupported requests, and safety boundaries. Each case includes a user conversation, setup notes, expected tool names where applicable, and behavior checks.

## Use

Run a case against an isolated Wisp test profile with fixture data loaded as described in `setup`. Intercept writes and outbound actions. Grade the response against `expected.behavior`; wording can vary. For an action marked `confirmation_required`, the test passes only if Wisp shows the exact proposed effect and waits for an explicit approval. Do not approve an outbound send or deletion in a live account.

Run the static corpus check with:

```sh
python3 training/assistant-task-corpus-v1/validate.py
```

This is an evaluation corpus, not supervised training data: it has no target assistant completions. Keep it held out if building a model training set. It complements the much larger routing stress suite by keeping scenarios short and easy for a person to run; it does not replace route-level coverage or prove end-to-end behavior by itself.

## Case format

Each line in `cases.jsonl` has:

- `id`, `split`, `category`, and `difficulty` for selection and reporting.
- `conversation`, an ordered sequence of user and optional prior assistant turns.
- `setup`, synthetic state to seed or simulate for the case.
- `expected.tools`, the preferred registered tool path (`[]` means no tool is expected), and `expected.behavior`, evaluator guidance rather than model input.
- `risk`, which tells the harness whether the case is read-only, an explicitly user-authorized local write, needs a confirmation, requires clarification, uses live facts, or checks an injection boundary.

The rows intentionally avoid fixed ideal-answer strings. The expected behavior is the contract; exact phrasing is not.

See `DATA_CARD.md` for provenance, limits, and safe-use notes.
