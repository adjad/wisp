# Release performance benchmark

Every Wisp release is measured before it ships. This document says what is measured, how a result is
turned into a verdict, and exactly what a coordinator runs and checks. The harness is
`scripts/release_performance.py`; the corpus and policy are `test_fixtures/performance/release_v1.json`.

**The rule that runs through all of it:** a sample that is empty, truncated, failed, refused, cancelled,
past its deadline or a false success is a failure. It never contributes a latency, so it can only make a
release look worse, never faster. A metric that was not measured is `UNKNOWN`, never zero.

## What this is, and what it is not

| | Release benchmark | `bench_latency_changes.py capture` / `compare` |
|---|---|---|
| Model | The real resident engine, through Wisp's real attributed transport | A fake model, or saved synthetic requests |
| Code under test | The candidate's own routing, approval, receipt and publication code, from a clean worktree | Prompt layout only |
| Release evidence | Yes, once `check` passes it | **No.** `check` refuses its output |

The legacy `capture` and `compare` commands are unchanged and remain useful for prompt-layout work. The
release mode is reached as `scripts/bench_latency_changes.py release ...`, which hands every argument to
`release_performance.py` untouched, or by running `release_performance.py` directly.

## Verdicts and exit codes

| Exit | Meaning |
|---|---|
| 0 | `PASS`: the evidence verified and the candidate has no blocking finding |
| 1 | `BLOCK`: a correctness failure, a new refusal or a material regression |
| 2 | `INCONCLUSIVE`: nothing blocks, but a pass cannot be established (no approved baseline, a new cohort, an unverifiable expectation, an `UNKNOWN` gating metric, a lane that is not a release gate) |
| 3 | Refused: the evidence is stale, wrong-head, incomplete, tampered with, incomparable, offline or fake |
| 4 | Usage error (a missing coordinator expectation, for example) |

Only 0 may let a release proceed. BLOCK outranks INCONCLUSIVE. A fake-model or offline-fixture result is
never a PASS: `check` refuses it with exit 3, and a run that is not real caps its own verdict at
INCONCLUSIVE.

## Scenarios (corpus `release_v1`)

All data is fictional (reserved `+1555555xxxx` numbers, invented people and places). The corpus is
posted to the isolated backend's real `/assistant/sync/*` endpoints, so the real tools read it.

| Scenario | What it proves | Required to be correct before timing counts |
|---|---|---|
| `greeting_warm` | Warm greeting, streamed | Non-empty answer, no tools, streamed deltas, at least one model call |
| `bounded_reasoning` | Short reasoning answer | The right arrival time, bounded length, no tools |
| `scoped_tool_selection` | The model chooses a scoped synthetic read | The route did not pre-resolve it, `search_notes` was offered with a `query` parameter and called with a matching query, the answer holds the note's facts and none of the distractor's, no effect tool |
| `deterministic_read` | A read that needs zero generation | Direct route, `summarize_messages` called, answer from the synthetic messages, **exactly zero engine chat calls** |
| `outbound_approval_boundary` | An unapproved outbound request stops at the real approval boundary | A real `confirm` event for `send_message`, the harness denied it, no tool result without a denial, **no claim that it was sent** |
| `health_status_overhead` | Cost of the readiness path | `/health` returned 200 `{"status":"ok"}` |

Each scenario has several paraphrase variants that rotate across repetitions, so the prefix cache is
exercised the way varied real prompts would, not by one repeated prompt.

Version 1 has not been exercised against a live attributed environment. The first real run is its
qualification run: it has no approved baseline, so it cannot pass. A corrected expectation is a new corpus
version, never an edit of v1.

## Timings

All timings are integer nanoseconds measured at the client of `/agent`.

| Metric | Definition |
|---|---|
| `first_visible_answer_s` | First non-blank answer `delta` or `text` after the last `clear_answer`. A retracted preamble does not count. |
| `first_model_delta_s` | First non-blank `delta` or `reasoning` event |
| `total_completion_s` | The first `done` event |
| `approval_boundary_s` | The first `confirm` event |
| `request_latency_s` | Whole-request time of a plain HTTP scenario |

Agent-loop routes publish their answer once, at the end, so for them first visible answer and total
completion are close together. That is what the product does, and the benchmark reports it as such.

Reported alongside, **never gating in policy v1**: engine HTTP call count, chat call count, attribution
`authority_load`, process-inspection (`lsof`/`ps`), `binding` and `connected_peer` check counts, and the CPU
seconds of each owned backend process. They come only from delegating wrappers around the candidate's own
functions (the wrappers pass arguments, results and exceptions through unchanged). If the candidate does
not have a surface, or the instrumentation is missing, the count is `UNKNOWN`.

Pure prefill and decode throughput are **not reported**. Wisp's client discards the engine's usage chunk, so
the only honest source is the engine itself, and dividing client wall time would be a different, wrong
number. `finish_reason` is likewise not visible to the client; truncation is detected through the product's
own truncated-reasoning marker and its fallback sentences.

## Statistics and policy (`release_policy_v1`)

* At least **20 measured samples** per required scenario per side. The floor is a constant in the harness
  that a policy file cannot lower.
* **2 warmups** per scenario per side, run first, stored with `phase: warmup`, counted and labelled, and
  never part of any percentile.
* **p50 and p95 by nearest rank** on integers: p50 is the ceil(0.50 n)-th and p95 the ceil(0.95 n)-th value
  in order. With n = 20 that is the 10th and 19th. The result does not depend on input order.
* Only correct measured samples contribute latencies. Failures are counted separately.
* **Regression:** a metric regresses at p50 or p95 only if the candidate is slower than the baseline by
  **both** more than 20% **and** more than 250 ms. Either alone is not a regression. The comparison uses exact
  fractions, not floating point. Faster is always acceptable.
* **BLOCK** on: any measured candidate correctness failure, a new refusal (candidate refusals above the
  baseline's), a measured material regression.
* **INCONCLUSIVE** on: no approved baseline, a new cohort, baseline correctness failures, an expectation that
  could not be verified, an `UNKNOWN` gating metric, a cohort identity containing `UNKNOWN`, a lane that is not
  a release gate.
* The thresholds above are a reviewable, versioned policy with their own hash. They are not harness
  constants, and changing any of them is a new policy version.

The performance audit suggested 30 to 40 samples per gating scenario and bootstrap intervals to make p95
steadier. They are recorded in the policy as **advisory and not applied**; adopting them is a deliberate new
policy version.

## Baselines and cohorts

Candidate and baseline are measured **in the same run, in alternating order** (repetition 1 candidate then
baseline, repetition 2 baseline then candidate, and so on), on two isolated backends spawned from two clean
worktrees. They must be **different subjects**: different commits and different trees. Each is verified from
git by the harness before and after the run.

A comparison is allowed only inside one **cohort**. The cohort key is: lane, execution mode, hardware, OS,
engine, model (id, weights manifest, quantization, tokenizer, config), generation settings, cache and warmup
treatment, harness hash, corpus hash and policy hash. It deliberately does **not** include the Wisp commit,
because candidate and baseline are different commits by design.

A baseline must be **approved by a reviewer**. An approval file records the baseline subject and the cohort it
was reviewed for, `status: approved`, `approved_by`, `review_ref` and `approved_at`. The coordinator passes
its SHA-256 to `check` out of band. The harness never creates, upgrades or advances an approval:
`propose-baseline` writes an unapproved proposal and refuses to overwrite a file. A missing, unapproved or
different-cohort baseline is INCONCLUSIVE.

## Lanes

* **`desktop`**: Desktop qualified inference. This is the release gate.
* **`managed_mini`**: the managed or mini path. It is its own cohort, labelled
  `compatibility_measurement_only`. It never inherits Desktop timing and can never satisfy the Desktop gate;
  `check` refuses a lane mismatch, and a managed receipt checked as managed is capped at INCONCLUSIVE.

The harness does not weaken engine attribution: the engine port is fixed at 8000 and there is no option to
change it or to switch attribution off.

## Evidence directory

`run` writes a new directory (it refuses to write into one that has content):

```text
<out>/receipt.json                       verdict, summary, comparison, identities, raw digests
<out>/raw/samples.jsonl                  one record per sample: grade, timings, counts, outcome
<out>/raw/events.jsonl                   every /agent event with its timestamp, plus the instrumentation window
<out>/raw/instrumentation.candidate.jsonl, instrumentation.baseline.jsonl   what each backend recorded
<out>/raw/provenance.json                schedule, preflight, subject records before and after
```

Every raw file is bound by SHA-256 in the receipt, and the receipt is bound to the candidate and baseline
SHA and tree, clean-worktree records taken **before and after** the run, the harness, corpus and policy
hashes, the runtime identity of each backend, the hardware, OS, engine, model, quantization, tokenizer,
generation settings, and the cache and warmup treatment. A caller-supplied SHA is never provenance: the
harness derives identity from git, and `check` confirms each SHA and its tree against a repository.

`check` accepts nothing on the receipt's word. It recomputes every timing, grade, count, summary and the
verdict from the raw files, re-verifies the schedule and the alternation, and refuses on any difference.
Edited raw files fail the digest check, and edited-and-resealed raw files fail re-derivation.

What that does **not** give you: receipts carry no cryptographic signature, so someone able to rewrite the raw
files and the receipt consistently, and to fabricate matching instrumentation, could forge one. Bind the
receipt's SHA-256 out of band (`--expect-receipt-sha256`, taken from the run's own output) and treat the
independent audit as the control for that.

## Safety of the measurement

* **Isolated state:** each backend gets a throwaway `WISP_HOME` containing only a role map that points every
  text role at the one resident model. No user data is read.
* **Synthetic leaves only:** no real contacts, calendar, Mail or Messages.
* **Effect guard (inside the spawned backend):** any process other than `/usr/sbin/lsof` and `/bin/ps` (the
  attribution inspectors), any connection other than loopback to the engine port, and any `httpx` request
  elsewhere is blocked and recorded. A blocked attempt during a sample fails it. The guard covers Python's own
  sockets and `httpx`; a raw socket opened by a C extension cannot be intercepted from Python.
* **Approval boundary:** the real approver runs. The harness answers every `confirm` with a denial through
  `/agent/approve`; it never approves.
* **No engine changes:** a model load or unload call, or any engine state change, during a sample fails it.
  The harness itself never loads, unloads or configures anything.
* **Known contamination to watch:** both backends stay resident during the run, so the idle one's background
  pollers run. Their CPU is reported separately, and any engine chat call they make in a sample window is
  counted in that sample.

## Prerequisites for a live run

`run` checks these and, if any is missing, writes an INCONCLUSIVE receipt listing exactly which:

1. A clean worktree at the exact candidate SHA and another at the exact baseline SHA, different commits.
2. The Desktop lane: no managed authorization manifest at `~/.moe/omlx-runtime-authorization.json`
   (the managed lane requires it to be present).
3. The oMLX engine reachable on `127.0.0.1:8000` with exactly the named model resident and nothing else.
4. Two free ports (default 18775 and 18776) and a Python interpreter (`--python`) that can import each
   worktree's backend dependencies.
5. A new, empty output directory.
6. An approved baseline file and its digest, to be able to PASS at all.

## Operating procedure for a release

```bash
# 1. The values a coordinator binds. Record them; do not read them from the run's own output.
python scripts/release_performance.py info

# 2. Measure (a live step: it uses the resident engine and spawns two backends).
python scripts/release_performance.py run \
  --candidate-worktree <clean worktree> --candidate-sha <40-hex> \
  --baseline-worktree  <clean worktree> --baseline-sha  <40-hex> \
  --model-id <resident model id> --output-dir <new directory> \
  --approved-baseline <approval.json> --approved-baseline-sha256 <digest> \
  --execute-live-release-measurement

# 3. Gate. Nonzero unless the evidence verifies and PASSES.
python scripts/release_performance.py check \
  --receipt <out>/receipt.json \
  --expect-candidate-sha <40-hex> \
  --expect-corpus-sha256 <from info> --expect-policy-sha256 <from info> --expect-harness-sha256 <from info> \
  --approved-baseline <approval.json> --approved-baseline-sha256 <digest> \
  --expect-receipt-sha256 <from the run output>
```

For a first run in a cohort (no approval yet) the run is INCONCLUSIVE by design. Review the receipt, then
`propose-baseline --receipt <receipt> --output <proposal>`, have a reviewer set `status: approved` and fill in
`approved_by`, `review_ref` and `approved_at`, and rerun with that approval bound. The cohort key is stable on
one machine and engine configuration, so the approval carries to the rerun.

`--diagnostic`, `--scenarios` and `--samples` below the floor produce a partial, non-gating receipt that
`check` refuses.

## Known limits

* **The live path has never been executed.** `run` against a real engine, the spawned backend's effect guard
  and instrumentation under a real candidate, and the real preflight were written and tested only with
  stand-ins (scripted drivers, stub modules, a loopback server). The first live run may expose a mismatch
  with the real backend, which is one more reason it is a qualification run.
* Engine throughput and `finish_reason` are not observable from the client (see above).
* Corpus v1 expectations are untested against a live engine; a scenario whose route differs from its
  expectation will fail its grade and block until a reviewed corpus version corrects it.
* The harness cannot prove a run happened; it proves the evidence is internally consistent, bound, and
  re-derivable.
* A sample counts every engine chat call in its window, including any made by the idle backend's background
  work.

## Integration

* **Release publication** (Hub): run `check` as a required step with coordinator-supplied expectations. Exit
  nonzero must stop signed and ad-hoc publication alike. A missing receipt is blocking.
* **Simulation QA manifest:** `scripts/run_simulation_qa.py` keeps an allowlist of reviewed full-profile
  tests, and `tests/test_release_performance.py` must be added to it by its owner. Until then
  `tests/test_simulation_qa_runner.py::test_full_manifest_covers_the_reviewed_deterministic_test_tree` fails
  with "unclassified tests".
