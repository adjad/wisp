# Corrected tool traces and scorer

The supplied debug exports show model/tool behavior but contain no trusted
corrections. `scripts.trace_eval.inventory` deduplicates them into a local
content-free annotation queue. A corrected case requires independent evidence
for the intended action and outcome. Unknown outcomes stay ungraded.

## Current pilot

- Six tracked, synthetic development contracts:
  `test_fixtures/trace_eval/dev_cases.json`.
- Four frozen local transfer cases at `.evo/trace_eval/holdout_cases.json`.
  The tracked `holdout_manifest.json` records their SHA-256. They are excluded
  from Git and from candidate policy editing. Four cases are a pilot gate, not
  a statistical reliability claim.
- The Wisp replay adapter calls the real router and agent loop with registered
  schemas and safety decisions, but replaces every tool body with an
  argument-sensitive synthetic fixture. It allows only the local model port.
  It writes a minimal synthetic chat-role overlay before startup so the
  selected model is actually used; it never copies the user's Wisp config or
  personal skills.
- The scorer checks raw model proposals and effective Wisp calls separately.
  Wrong arguments, unsupported fixtures, premature/unauthorized/duplicate
  effects, missing exact simulated approval, and contradictory delivery claims
  fail mechanically. A mechanical success remains `UNVERIFIED` until a human
  semantic review names the case, reviewer, verdict, and exact trace and case
  SHA-256 values.

## Local commands

From the repository root:

```bash
python3 -m scripts.trace_eval.inventory /path/to/wisp-debug-*.json \
  --output .evo/trace_eval/annotation_queue.json
python3 -m scripts.trace_eval.run \
  --cases test_fixtures/trace_eval/dev_cases.json \
  --output .evo/trace_eval/dev_run --model Ling-3.0-tiny-oQ6e \
  --model-revision '<stable local artifact ID or checksum>'
python3 -m scripts.trace_eval.score \
  --cases test_fixtures/trace_eval/dev_cases.json \
  --traces .evo/trace_eval/dev_run \
  --reviews .evo/trace_eval/semantic_reviews.json \
  --output .evo/trace_eval/dev_report.json
```

`semantic_reviews.json` is a local object keyed by case ID. Each value is:

```json
{"case_id":"calendar_busy_no_send","trace_sha256":"<trace hash from score output>","case_sha256":"<case hash from score output>","reviewer":"<name>","verdict":"pass","note":"<evidence for the verdict>"}
```

The runner executes cases serially. It writes generated traces only inside
the requested run directory, which must be an empty child of
`.evo/trace_eval/` (or use `--resume`). The raw debug exports and annotation
queue must stay local; do not commit them. Run the frozen holdout only after a
candidate is selected, passing `--manifest
test_fixtures/trace_eval/holdout_manifest.json` so a changed case file fails
before the model runs. Compare with the baseline using the same
model/configuration and review rule.
Resume checks the complete case-file hash, selected IDs, requested model and
revision, synthetic overlay, frozen clock, timeout, and all replay-affecting
Wisp service source hashes before reusing a trace. It also
checks that a saved score matches the trace bytes and re-scores every retained
trace. If no stable model artifact ID is available, the default records that
the artifact is unverified; do not use that run for a training comparison.
Run `inventory` again with `--replace` when you add new exports. The queue
contains only hashes, allowlisted role/model labels, and counts; it never
copies log-supplied filenames, tool names, prompts, answers, or results.

## Label contract

Each corrected case records: user prompt, frozen clock, allowed tools,
required tool/argument predicates, ordered dependencies, allowed effects,
synthetic tool results, simulated receipts, and final-answer evidence. The
reference specifies acceptable behavior, not one exact prose answer. An
effect is never considered safe merely because the model proposed it or
because a debug `confirm` decision appears; a matching simulated approval
and receipt are required. A human reviewer adjudicates semantic facts that
the mechanical predicates cannot prove.
