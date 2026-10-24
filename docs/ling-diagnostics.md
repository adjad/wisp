# Ling diagnostic evaluation

This suite answers a bounded question: can the selected Ling model follow
instructions, use a small tool menu, and answer from fixed synthetic evidence?
It separates failures in inference, tool-call protocol, tool selection, exact
arguments, generation/step budgets, and answer contracts. It does not exercise
Wisp's router, native tools, app UI, real accounts, or production safety policy.

## Coverage and scoring

There are 44 wholly synthetic held-out cases across 13 categories: format,
reasoning, grounding, attribution, temporal filtering, follow-ups, uncertainty,
untrusted sources, tool arguments, tool selection, failure recovery, multi-step
tasks, and writing. Provenance is recorded on every case. Patterns are adapted
from `scripts/eval_ling_tasks.py` and the original synthetic ATC corpus; no
private live-corpus targets, chat logs, or user records are used.

The cases add exact argument and answer checks to the existing evaluation
coverage. The existing `eval_ling` human grading remains useful for prose
quality; this suite does not replace it or claim that correct tool names prove
a correct answer. Fixture schemas are deliberately small and explicitly defined
per case; they are not a copy of the full production Wisp tool registry.

Each row has one of three outcomes:

- `contract_pass`: every objective check passed and no manual rubric remains.
  This is a strict fixture-contract result, not a general quality grade.
- `fail`: an objective check or execution boundary failed. Exact answers and
  argument lists are intentionally strict; alternate wording/order may need
  review before drawing a model-capability conclusion.
- `needs_review`: objective checks passed but semantic quality needs review.
  A vague or hallucinated answer to a manual case can remain here; it is never
  counted as a pass. Review the rubric and actual answer separately.

Wrong answers cannot pass merely by containing the expected number. JSON checks
compare the entire parsed object, including types. Tool checks compare the
complete ordered call list and exact arguments. Unknown tools or unmatched
fixture arguments stop a case before another generation. Fixture dispatch is a
dictionary match; it never resolves or executes a production tool. Even the
proposed reminder case only returns a fixed denial and creates nothing.

Expected answers, grading rubrics and fixture results are excluded from the
initial model request. A fixture result enters the conversation only after an
exact matching tool call. Models see only the prompt, conversation history,
system variant, offered schemas, and then the result they requested.

## Offline setup and harness checks

Use Python 3.11 or newer from the repository's existing environment. Offline
commands use the standard library and never import the `service` package:

```sh
python -B -m scripts.ling_diagnostics validate
python -B -m scripts.ling_diagnostics list
python -B -m scripts.ling_diagnostics list --categories attribution,tool_arguments
python -B -m unittest discover -s tests -p test_ling_diagnostics.py
```

The included replay demonstrates one formatting, arithmetic, and tool round-trip
case without contacting a model:

```sh
python -B -m scripts.ling_diagnostics replay \
  --ids FMT-001,MATH-001,TOOL-001 \
  --responses test_fixtures/ling_diagnostics/offline-demo.json \
  --out /tmp/wisp-ling-offline-demo-NEW
```

The output directory must not exist. Reports say **OFFLINE REPLAY** and are
never evidence of Ling quality. Other recordings use the same JSON schema:
each case/variant/repetition declares its ordered raw chat-completion responses.
Missing, duplicate, or unused recordings stop the suite and retain an incomplete
manifest. The runner never manufactures responses from expected answers.

## Live baseline and paired improvement experiment

Live execution is optional and must run in a separately reviewed macOS OS
sandbox, with isolated `HOME`/`WISP_HOME` and a coordinator-granted model/host
slot. The adapter checks actual real-home and external-network denials before
service imports. An unsandboxed invocation is refused. The outer policy must
allow only the approved managed loopback inference endpoint, required code and
system reads, scoped existing engine authentication, and owned temporary state.
There is deliberately no generic sandbox profile here: attestation, engine mode,
runtime locations and credential scope must be reviewed for the actual host.

Inside that approved environment, the command shape is:

```sh
python -B -m scripts.ling_diagnostics live \
  --model EXACT_ALREADY_RESIDENT_LING_ID \
  --original-home ORIGINAL_HOME \
  --ids FMT-001,MATH-001,ATTR-001,TOOL-001,TOOL-009,CHAIN-003 \
  --variants baseline,grounded --reps 3 \
  --timeout 45 --wall-timeout 600 --max-steps 3 --max-tokens 512 \
  --out OWNED_EXTERNAL_RESULTS_DIRECTORY
```

The production guarded `OMLXClient` handles existing local authentication. The
adapter accepts only a managed local oMLX endpoint, checks that the exact Ling
model is already resident before starting and before each completion, and
requires the response model ID to match. No engine launch, load, unload, model
download, config edit, remote fallback or model promotion is available. A model
can still be evicted by another process between a residency read and inference;
the runtime slot is necessary, and this is not an atomic engine lease. The
approved run must record endpoint/engine identity and actual model revision
outside this diagnostic when available; a model ID is not a weights checksum.

Start with the six-case pilot, inspect outputs and actual request counts, then
run all cases with paired variants and three repetitions (264 rows exceeds the
256-row bound; use category batches or two repetitions for the full 44 cases).
The baseline is intentionally short. The grounded variant adds general evidence,
attribution and action-result instructions, never case-specific answers. Variant
order reverses on alternate repetitions. The report compares only matched case,
model, mode, case-hash and repetition pairs; changed contract outcomes are not
automatic semantic improvements. Temperature is zero, but repeated runs can
still differ. This is a diagnostic sample, not a statistical production benchmark.

## Results and improvement decisions

Each run writes `manifest.json`, append-only `results.jsonl`, `summary.json`, and
`report.md`. The manifest records source commit, corpus hash, selection, prompt
variants, budgets and run completion. Rows retain input/case hashes, synthetic
answers and call traces, timing, finish reasons, usage, check outcomes and
failure stages. Private reasoning fields and raw transport exception messages
are not saved. Cancelled, timed-out or malformed runs retain partial evidence;
unexecuted rows have no score. Stop inference/environment failures before
interpreting model-quality differences.

Read a saved report without inference:

```sh
python -B -m scripts.ling_diagnostics report --out RESULTS_DIRECTORY
```

Triage in this order:

1. Inference identity, availability, timeout and protocol failures: fix the
   environment or chat-template compatibility first.
2. Truncation or step limits: inspect budgets and repeated calls.
3. Wrong tool/arguments: inspect the fixture menu and exact trace; compare with
   a separately captured Wisp router trace before assigning a production defect.
4. Wrong grounded answer, attribution, date or reasoning: inspect the source and
   compare paired prompt variants. Apply human rubrics to pending rows.
5. Persistent failures under both variants: develop new training examples on a
   separate development set, then retest this held-out suite and a fresh set.

This task sets up measurement. It does not change prompts in production, train
weights, or claim that a proposed improvement has worked before a live comparison.
