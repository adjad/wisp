# Data card: Wisp assistant task corpus v1

## Purpose

This dataset supports manual acceptance checks and isolated end-to-end evaluation of Wisp's user-facing assistant tasks. It is meant to answer: did Wisp understand a realistic request, use grounded state where needed, handle missing details honestly, and respect the action boundary?

## Contents

- 48 synthetic scenarios in `cases.jsonl`.
- 12 smoke, 24 regression, and 12 challenge cases.
- Ten categories: calendar, mail, messages, memory, files, research, system, utilities, ambiguity, and safety.
- A JSON Schema and standard-library structural validator.
- Evaluator rubrics and setup descriptions; no target assistant responses.

## Construction and provenance

Prompts and rubrics were newly written from Wisp's documented capabilities and current tool descriptions in the repository. The existing demo prompt list and routing stress suite were used to avoid exact prompt duplication and to identify the need for a shorter acceptance set. No user conversation, mailbox, calendar, message, memory, file, telemetry, or private debug data was used. Names, addresses under `.invalid`, dates, record text, and file paths are synthetic.

## Intended use

Load each case's setup into an isolated test profile or simulate the stated source results. Replay `conversation` in order and grade the final turn with `expected.behavior`. `expected.tools` lists the preferred registered tool path, not a requirement to use an exact wording or call sequence when another documented path is equivalent. An empty tool list means the assistant should clarify, refuse safely, or report a missing capability without acting.

Intercept all outbound messages, email, calendar writes, file moves/deletes, and other persistent actions. Cases with `risk: confirmation_required` pass only when the assistant shows the exact action details and waits for approval. Do not approve those actions against real user accounts or files.

## Limitations

This is a compact, curated set rather than a statistically sampled or human-annotated dataset. The rubrics have not been validated against a population of users or measured model runs. It does not cover every Wisp tool, prove safety, or replace the larger tool-routing stress suite. Live-source cases can change over time and should record the date and sources used during a run. Tool names and expected behavior may need revision when Wisp's capabilities change.

There are no assistant completion targets. Do not use these rows as supervised fine-tuning examples without separately authoring and reviewing target responses; keep the corpus held out from model training when using it to measure quality.

## Versioning

- Corpus version: `1.0.0`
- Format: JSON Lines, UTF-8
- Case IDs: `ATC-001` through `ATC-048`
