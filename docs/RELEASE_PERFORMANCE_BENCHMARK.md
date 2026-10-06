# Release performance benchmark

Every Wisp release is measured before it ships. This document says what is measured, how a result is
turned into a verdict, and exactly what a coordinator runs and checks. The harness is
`scripts/release_performance.py`; the corpus (`release_v2`) and policy (`release_policy_v2`) are one bundle,
`test_fixtures/performance/release_v1.json` (the file keeps its name; the versions inside it are what bind).

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
| 1 | `BLOCK`: a correctness failure, a new refusal, a material regression, or a refused or state-changing effect anywhere in the candidate backend's lifetime |
| 2 | `INCONCLUSIVE`: nothing blocks, but a pass cannot be established (no approved baseline, a new cohort, an unverifiable expectation, an `UNKNOWN` gating metric, a lane that is not a release gate, a measurement that is not comparable or not verifiable, see "Integrity") |
| 3 | Refused: the evidence is stale, wrong-head, incomplete, tampered with, incomparable, offline, fake, not from the live measurement source, or not bound to the recorded receipt digest |
| 4 | Usage error (a missing coordinator expectation, including the receipt digest) |

Only 0 may let a release proceed. BLOCK outranks INCONCLUSIVE. A fake-model or offline-fixture result is
never a PASS: `check` refuses it with exit 3, and a run that is not real caps its own verdict at
INCONCLUSIVE.

## Scenarios (corpus `release_v2`)

All data is fictional (reserved `+1555555xxxx` numbers, invented people and places). The corpus is
posted to the isolated backend's real `/assistant/sync/*` endpoints, so the real tools read it.

| Scenario | What it proves | Required to be correct before timing counts |
|---|---|---|
| `greeting_warm` | Warm greeting, streamed | Non-empty answer, no tools, streamed deltas, at least one model call, every engine call ended normally |
| `bounded_reasoning` | Short reasoning answer | The right arrival time, bounded length, no tools, every engine call ended normally |
| `scoped_tool_selection` | The model chooses a scoped synthetic read | The route did not pre-resolve it, `search_notes` was offered with a `query` parameter and called with a matching query, the answer holds the note's facts and none of the distractor's, no effect tool |
| `deterministic_read` | A read that needs zero generation | Direct route, `summarize_messages` called, answer from the synthetic messages, **exactly zero engine chat calls** |
| `outbound_approval_boundary` | An unapproved outbound request stops at the real approval boundary | Exactly one real `confirm` for `send_message` naming Alex and the ten-minute message; one **accepted denial** bound to that action and session; a terminal tool result that says it was denied; **no claim that it was sent** |
| `health_status_overhead` | Cost of the readiness path | `/health` returned 200 `{"status":"ok"}` |

Each scenario has several paraphrase variants that rotate across repetitions, so the prefix cache is
exercised the way varied real prompts would, not by one repeated prompt.

Version 2 supersedes version 1, which an independent audit blocked. It adds the engine completion-status
gate and the complete denial lifecycle. It has not been exercised against a live attributed environment. The
first real run is its qualification run: it has no approved baseline, so it cannot pass. A corrected
expectation is a new corpus version, never an edit of v2.

### Completion status

A terminal `done` proves the agent loop finished, not that the model's answer was complete. For every engine
chat call in a sample window the harness records the engine's own `finish_reason`, observed passively as the
response passes through (see "Measurement boundary"). A reason outside the corpus's acceptable list
(`stop`, `tool_calls`, `function_call`), such as `length`, fails the sample as a truncated answer even if the
answer already contains every expected fact. If a required completion status cannot be observed (the surface
is missing, a record is absent, the reason is empty), the sample is **unverifiable**, which makes the verdict
INCONCLUSIVE; it is never inferred from `done`.

### The denial lifecycle

The outbound scenario passes only when the denial is a complete, matched sequence: one `confirm` for the
expected tool, the harness's denial POSTed to the real `/agent/approve` with that action and session id, the
endpoint answering `ok: true` (a denial it matched to nothing is `accepted: false`), and a terminal tool result
for the same action that says it was denied. A missing, unaccepted, duplicated, mismatched-action or
mismatched-session denial, a missing or non-denial result, a duplicate confirmation, and arguments that do not
name the intended recipient and message are each a failure.

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

Reported alongside, **never gating in the policy**: engine HTTP call count, chat call count, attribution
`authority_load`, process-inspection (`lsof`/`ps`), `binding` and `connected_peer` check counts, the number of
instrumentation records in the window, and the CPU seconds of each owned backend process. They come only from
delegating wrappers around the candidate's own functions (the wrappers pass arguments, results and exceptions
through unchanged). If the candidate does not have a surface, or the instrumentation is missing, the count is
`UNKNOWN`.

Pure prefill and decode throughput are **not reported**. Wisp's client discards the engine's usage chunk, so
the only honest source is the engine itself, and dividing client wall time would be a different, wrong number.

## Measurement boundary

**What is timed is instrumented end-to-end latency.** Every request is measured at the client of the
candidate's backend while the harness's delegating wrappers, its effect guard and its response tee are
installed inside that backend. The numbers are therefore latency of the instrumented application and are
**not uninstrumented application latency**, nor engine-only time.

* The wrappers around `handle_async_request`, the attribution checks and `inspect_command` write one JSON
  record each, synchronously, on the request path. The cost grows with the number of such calls, so a
  candidate that makes more attribution calls pays slightly more instrumentation cost as well.
* The **response tee** observes each engine chat response to read `finish_reason`. Bytes pass through
  unchanged and in order; the observer parses incrementally with bounded memory. It adds a small amount of
  CPU per chunk.
* The effect guard adds a path check to file opens, process starts and outbound requests.
* All of this applies equally to candidate and baseline, so **identically instrumented** end-to-end
  comparisons are meaningful. Small gains are not: an effect smaller than the instrumentation's own variation
  must not be reported as a performance improvement.
* CPU seconds, attribution counts, the instrumentation record count and the other-side request count are
  **diagnostics**. They explain a difference; they never gate a release.
* This benchmark **does not characterize** its own overhead. Measuring it (for example against the same run
  with the wrappers removed) needs a separate, authorized measurement, and it would never be done by
  disabling attribution: attribution is never disabled, and there is no option to disable it.
* A synthetic fixture timing from the offline tests is not performance evidence of any kind.

## Statistics and policy (`release_policy_v2`)

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
  baseline's), a measured material regression, and any refused effect, engine model load or unload, or other
  engine mutation anywhere in the **candidate** backend's whole lifetime (startup, warmup, idle intervals,
  teardown), not only inside measured windows.
* **INCONCLUSIVE** on: no approved baseline, a new cohort, baseline correctness failures, an expectation that
  could not be verified, an `UNKNOWN` gating metric, a cohort identity containing `UNKNOWN`, a lane that is not
  a release gate, a refused or state-changing effect on the **baseline** backend, an idle backend's engine
  request during a sample, and every integrity finding below.
* The thresholds above are a reviewable, versioned policy with their own hash. They are not harness
  constants, and changing any of them is a new policy version.

The performance audit suggested 30 to 40 samples per gating scenario and bootstrap intervals to make p95
steadier. They are recorded in the policy as **advisory and not applied**; adopting them is a deliberate new
policy version.

### Integrity (INCONCLUSIVE, never PASS)

These are reasons a set of correct samples still cannot be a release measurement:

* the engine, model, settings, hardware or OS identity differs before and after the run;
* the model is not **exactly** the named one, alone, on both backends at the start and at the end (read through
  each candidate's own attributed client);
* the two backends ran different interpreters, or an interpreter identity was not reported;
* the two backends ran under different containment policies, or one was not reported;
* the engine chat requests named a different model than the declared one, sent different sampling settings on
  the two sides, or their settings were not observed;
* a backend's teardown did not complete cleanly.

## Baselines and cohorts

Candidate and baseline are measured **in the same run, in alternating order** (repetition 1 candidate then
baseline, repetition 2 baseline then candidate, and so on), on two isolated backends spawned from two clean
worktrees. They must be **different subjects**: different commits and different trees. Each is verified from
git by the harness before and after the run.

A comparison is allowed only inside one **cohort**. The cohort key is: lane, execution mode, hardware, OS,
engine, model (id, config, tokenizer, quantization and a **full SHA-256 of every weight file**), generation
settings, cache and warmup treatment, the **child interpreter identity** (version, implementation, executable
digest, platform, as the child reported about itself), the **containment policy identity**, harness hash,
corpus hash and policy hash. It deliberately does **not** include the Wisp commit, because candidate and
baseline are different commits by design.

A baseline must be **approved by a reviewer**. An approval file records the baseline subject and the cohort it
was reviewed for, `status: approved`, `approved_by`, `review_ref` and `approved_at`. The coordinator passes
its SHA-256 to `check` out of band. The harness never creates, upgrades or advances an approval:
`propose-baseline` writes an unapproved proposal and refuses to overwrite a file. A missing, unapproved or
different-cohort baseline is INCONCLUSIVE.

## Lanes

* **`desktop`**: Desktop qualified inference. This is the release gate.
* **`managed_mini`**: the managed or mini path. It is its own cohort, labelled
  `compatibility_measurement_only`. It never inherits Desktop timing and can never satisfy the Desktop gate;
  `check` refuses a lane mismatch, and a managed receipt checked as managed is capped at INCONCLUSIVE. The
  effect guard does not permit the managed lane's `launchctl` inspection, so a managed run records that as a
  refused effect; the managed lane is outside this release gate.

The harness does not weaken engine attribution: the engine port is fixed at 8000 and there is no option to
change it or to switch attribution off.

## Evidence directory

`run` writes a new directory (it refuses to write into one that has content):

```text
<out>/receipt.json                       verdict, summary, comparison, identities, raw digests
<out>/raw/started.jsonl                  one record per sample, written BEFORE dispatch
<out>/raw/events.jsonl                   every /agent event of every sample, with its timestamp and its window bounds
<out>/raw/samples.jsonl                  one graded record per sample, derived after teardown from the complete logs
<out>/raw/instrumentation.candidate.jsonl, instrumentation.baseline.jsonl   each backend's complete lifetime log
<out>/raw/environment.json               the environment before and after, and the residency of both backends
<out>/raw/provenance.json                schedule, preflight, subject records before and after, teardown outcomes
```

Each instrumentation log begins with one `header` record (run id, side, worktree root, the child's own
interpreter identity, the containment policy digest) and a `surfaces` record, followed by gap-free sequenced
records from a single process in non-decreasing time. A sample's window is **not stored**: both the run and the
checker rebuild it from the complete logs and the persisted window bounds, so an event cannot be omitted from a
window while remaining in the log, and effects outside every window are still seen.

Every raw file is bound by SHA-256 in the receipt, and the receipt is bound to the candidate and baseline
SHA and tree, clean-worktree records taken **before and after** the run, the harness, corpus and policy
hashes, the runtime identity of each backend, the hardware, OS, engine, model, quantization, tokenizer,
generation settings, and the cache and warmup treatment. A caller-supplied SHA is never provenance: the
harness derives identity from git, and `check` confirms each SHA and its tree against a repository.

`check` accepts nothing on the receipt's word. It recomputes every timing, grade, count, summary and the
verdict from the raw files, re-verifies the schedule, binds **every sample to the exact scheduled position,
variant, order slot and prompt bytes**, validates every clock (integers, non-negative, ordered, inside the
sample's own interval, no overlap between samples, within the deadline), rejects duplicate records, validates
each log's header, continuity and run binding, and refuses on any difference.

### What authenticates a run

The receipt carries no cryptographic signature. What ties a package to a run is the receipt's SHA-256, which the
**measurement owner records from the run's own output when it finishes**:

* `check` requires `--expect-receipt-sha256` (omitting it is a usage error, exit 4) and refuses any other digest.
  A package whose raw files, receipt and manifest were all rewritten consistently has different bytes, so it
  fails this binding.
* `check` accepts only a receipt produced by the live measurement class (`LiveDeps`). Constructed or offline
  evidence, even one that labels itself real, is refused. The offline tests verify constructed evidence through
  an API-only seam (`accept_fixture_source`) that the command line cannot reach, and a result obtained that way
  reports `authorizes_release: false`.
* The result of a passing production check reports `authorizes_release: true`.

What this still does not give you: if someone controls **both** the package and the digest the checker is
shown, nothing in it can stop a forgery. The recorded digest must come from the measurement owner and not from
the package, and the independent audit and live QA remain the control.

## Safety of the measurement

* **Isolated state:** each backend gets a throwaway `WISP_HOME` (which also holds its `TMPDIR`) containing only
  a role map that points every text role at the one resident model. No user data is read.
* **Explicit child environment:** a backend inherits only `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL` and
  `LC_CTYPE`, plus a fixed `PATH`, its `WISP_HOME` and `TMPDIR`, an owned `CODEX_HOME`, and `PYTHONDONTWRITEBYTECODE`. No credential,
  proxy, loader (`DYLD_*`), bridge or interpreter-path variable crosses. `HOME` is kept on purpose: the engine
  settings and authorization record the attributed path needs live under the real `HOME`, and a fake `HOME`
  would not be a real measurement. `CODEX_HOME` is always derived as `<child WISP_HOME>/codex`, never inherited,
  and created empty before backend imports. Codex monitoring reports unavailable there without reading real
  Codex task databases; direct `serve` entry also enforces this derived root before candidate imports.
* **Synthetic leaves only:** no real contacts, calendar, Mail or Messages.
* **No credential is sent by the harness.** The harness never reads the engine settings' key and never contacts
  the engine itself. Its only engine-facing step is a bare TCP connect to confirm something listens. Whether the
  engine holds exactly the named model is read by each spawned backend through the **candidate's own attributed
  client** (a private route added to the child's app), which releases the credential only to a qualified
  connected peer.
* **Effect guard (inside the spawned backend, installed before the candidate's code is imported):**
  * **Processes:** only the exact `lsof` and `ps` argument shapes the engine attribution uses, plus the product's
    one identity lookup `id -F` (read-only; `service/memory/identity.py` runs it once per process), are allowed. Any
    other program, any other arguments to those programs, a shell, or an `executable=` override is refused. All
    `exec`, `spawn` and `fork` families are refused. `id` is pinned to `/usr/bin/id`: the product calls it by bare
    name, and `subprocess` would resolve a bare name through `PATH` (`os.environ` or `env=`), so a program named
    `id` in the writable throwaway home could run outside the guard. The guard therefore never lets a bare name
    run: it rewrites `argv[0]` (and `executable=`, when given) to the absolute path before the real `Popen`
    starts, whatever `PATH` says. Every argument is converted to a string once, and the guard runs exactly those
    strings, so an object whose `__fspath__` changes between the check and the start cannot become another
    program. The alias table is part of the policy digest.
  * **Network:** loopback to the engine port only. Every other connection is refused. Python socket
    `send`, `sendall` and `sendmsg` to that port require the task-local capability scoped to a reviewed
    `httpx.send` or `http.client.endheaders` operation; raw socket writes and datagrams are refused before bytes
    leave. Accepted connections to the backend can still receive their responses. This is a Python guard,
    not a defense against malicious code recovering original methods, inheriting a capability inside a
    reviewed transport, or using native I/O.
  * **Engine operations:** only `GET /health`, `GET /v1/models`, `GET /v1/models/status`,
    `POST /v1/chat/completions`, `POST /v1/embeddings` and `POST /v1/rerank` are transmitted, through `httpx`
    and through `http.client` alike. A model load or unload, a settings change or any other request to the engine
    port is refused **before transmission** and recorded.
  * **Filesystem:** writes are allowed only inside the throwaway home. The product may acquire its empty
    credential lock (`~/.moe/.provisioning.lock`) using exactly `os.open(O_CREAT | O_RDWR | O_NOFOLLOW, 0600)`
    and may create `~/.moe`. General path mutations and descriptor writes/truncation/chmod on that lock are
    refused; untracked descriptor opens and relative `dir_fd` operations are refused. Read checks resolve
    symlinks before applying the HOME boundary. Arbitrary `sys.path` additions never grant read access;
    only the explicit child worktree and interpreter prefixes qualify. Under the real `HOME`, reads
    are limited to the engine settings files, the authorization record, the credential-generation record and
    the worktree and interpreter the child runs from. Everything else under the real `HOME` is refused.
  * **Pipes of an allowed inspector:** `subprocess` wraps the pipes it creates with `io.open(<fd>, "rb"|"wb")`,
    which the descriptor rule would otherwise refuse as untracked. Exactly those descriptors are recognised: made by
    `os.pipe()` on the same thread while one argv-checked `Popen.__init__` is running, opened `rb` or `wb`, and
    still the same pipe (device and inode recorded at creation, so a `dup2` of a file, socket or another FIFO onto
    the number is refused). Each recorded descriptor is good for one open, and the window closes when that call
    returns. A pipe made elsewhere, a regular-file or socket descriptor, any other mode, a second open, or another
    thread's open is refused as before. The rule is part of the guard policy digest (`popen_pipe_descriptors`), so
    changing it changes the containment identity. Residual: code running inside that window (for example another
    argument's `__fspath__`) can still wrap a pipe it just created itself; that gives it a pipe object and no
    access to any path. `Popen.communicate(input=...)` still refuses (an `os.write` on an untracked descriptor);
    the product's `inspect_command` passes no input.
  * **The harness's own directory is not on the child's `sys.path`.** `python scripts/release_performance.py serve`
    puts that directory first on `sys.path`, and `importlib.metadata` lists every `sys.path` entry, which the guard
    refuses when the directory is under the real `HOME` (it belongs to neither the worktree nor the interpreter).
    `serve` drops that one entry before the candidate imports anything; no read allowance was added.
  * Every refusal is recorded in the log whatever the caller does with the exception. A refusal anywhere in
    the candidate's lifetime is a BLOCK.
* **Approval boundary:** the real approver runs. The harness answers every `confirm` with a denial through
  `/agent/approve`; it never approves.
* **No engine changes:** a model load or unload call, or any engine state change, anywhere in a backend's
  lifetime is recorded and blocks the candidate. The harness itself never loads, unloads or configures anything.
* **Teardown:** each backend is its own process-group leader. Teardown is attempted for **every** owned child
  independently, bounded (terminate, then kill), ends the group it leads without touching any process the
  harness does not own, and records each outcome. A teardown that fails for one backend never skips the other,
  and the receipt is sealed regardless.
* **Malformed evidence is refused.** Scalar SSE is retained as an unparseable event; invalid receipt/raw
  JSON shapes produce a structured non-authorizing refusal. A driver exception retains its partial sample,
  marks the run aborted and seals a non-passing receipt after teardown.
* **An interrupted sample is kept.** A started-sample record is written before dispatch, and the partial
  stream of an interrupted sample is persisted before the interruption propagates. It is graded `cancelled` and
  never counted as a success.
* **Shared engine:** both backends stay resident during the run, so the idle one's pollers run. Any request
  other than a status read that the idle backend sends to the engine during a sample window makes that sample
  unverifiable (INCONCLUSIVE). Its CPU is reported separately.

## Prerequisites for a live run

The measurement owner must first establish and record a suitable quiet measurement environment and available
resources. An occupied or memory-pressured host is not a substitute for a comparable qualified cohort; the
owner must defer that measurement rather than treat synthetic checks as a pass. This policy adds no arbitrary
RAM or swap threshold and grants no model load/unload or settings-change authority.

`run` checks these and, if any is missing, writes an INCONCLUSIVE receipt listing exactly which:

1. A clean worktree at the exact candidate SHA and another at the exact baseline SHA, different commits.
2. The Desktop lane: no managed authorization manifest at `~/.moe/omlx-runtime-authorization.json`
   (the managed lane requires it to be present).
3. The oMLX engine listening on `127.0.0.1:8000`, and, once the backends are up, exactly the named model
   resident and nothing else, as each candidate's own client reports it. A read the client's peer attribution
   refuses is retried (three attempts, one second apart); a set that is still unreadable is INCONCLUSIVE.
4. Two free ports (default 18775 and 18776) and a Python interpreter (`--python`) that can import each
   worktree's backend dependencies.
5. A new, empty output directory.
6. An approved baseline file and its digest, to be able to PASS at all.

Hashing every weight file takes time proportional to the model's size, once before and once after the run.

## Operating procedure for a release

```bash
# 1. The values a coordinator binds. Record them; do not read them from the run's own output.
python scripts/release_performance.py info

# 2. Measure (a live step: it uses the resident engine and spawns two backends).
#    Record the receipt_sha256 it prints: that digest is what authenticates this run.
python scripts/release_performance.py run \
  --candidate-worktree <clean worktree> --candidate-sha <40-hex> \
  --baseline-worktree  <clean worktree> --baseline-sha  <40-hex> \
  --model-id <resident model id> --output-dir <new directory> \
  --approved-baseline <approval.json> --approved-baseline-sha256 <digest> \
  --execute-live-release-measurement

# 3. Gate. Nonzero unless the evidence verifies and PASSES. The receipt digest is REQUIRED.
python scripts/release_performance.py check \
  --receipt <out>/receipt.json \
  --expect-candidate-sha <40-hex> \
  --expect-corpus-sha256 <from info> --expect-policy-sha256 <from info> --expect-harness-sha256 <from info> \
  --approved-baseline <approval.json> --approved-baseline-sha256 <digest> \
  --expect-receipt-sha256 <the digest the measurement owner recorded>
```

For a first run in a cohort (no approval yet) the run is INCONCLUSIVE by design. Review the receipt, then
`propose-baseline --receipt <receipt> --output <proposal>`, have a reviewer set `status: approved` and fill in
`approved_by`, `review_ref` and `approved_at`, and rerun with that approval bound. The cohort key is stable on
one machine and engine configuration, so the approval carries to the rerun.

`--diagnostic`, `--scenarios` and `--samples` below the floor produce a partial, non-gating receipt that
`check` refuses.

## Known limits

* **The live path has had diagnostic smokes only.** A first live attempt exposed three harness faults, all fixed
  here: the filesystem guard refused `subprocess`'s own pipe descriptors for an allowed inspector, the harness's
  own directory on the child's `sys.path` was listed (and refused) by `importlib.metadata`, and the guard's
  per-call path resolution roughly doubled the cost of the product's runtime-authority load, which pushed a
  health check past its two-second budget and made the candidate try `omlx-cli start` (correctly refused). No
  full, non-diagnostic run has been made. Two behaviours seen only on the v1.1.5 baseline are product traits, not
  harness faults, and are reported as they happen: its peer attribution costs about 1.7 s per engine call, so its
  2 s startup health check sometimes times out and tries `omlx-cli start` (refused and recorded as a blocked
  effect), and it sometimes refuses one attribution (outcome `refused`).
* **Containment is Python-level, not an operating-system boundary.** The guard patches Python's file, process
  and network entry points. A C extension or native call that opens a file, process or socket itself is not
  covered, and reads of file metadata (`stat`) are not restricted. An OS-level sandbox around the child would be
  the stronger boundary and needs an environment this offline work cannot validate. The guard is strict by
  design: a legitimate candidate read or write outside the policy is refused and recorded, which makes the run
  non-passing until a reviewed harness change adds it.
* The credential lease file under `~/.moe` is the one qualified write outside the throwaway home: it is the
  product's own, empty, lock file, and refusing it would make real engine calls impossible.
* The harness cannot prove a run happened; it proves the evidence is internally consistent, bound, and
  re-derivable, and (through the recorded digest) that it is the package the measurement owner recorded.
* Corpus v2 expectations are untested against a live engine; a scenario whose route differs from its
  expectation will fail its grade and block until a reviewed corpus version corrects it. The recipient check on
  the outbound scenario accepts the contact's name or number and may need a reviewed correction.
* Engine throughput is not observable from the client. Instrumentation overhead is not characterized (see
  "Measurement boundary").
* A sample counts every engine chat call in its window, including any made by the idle backend's background
  work, and flags the idle backend's non-status requests as interference.

## Release integration

Both `scripts/wisp-build release` and `release-ad-hoc` call the same mandatory gate (the ad-hoc command alone may instead take a committed, digest-bound [waiver](#waiver)) before any signing,
GitHub draft, upload or publication step. A missing receipt or approval, missing digest, stale or malformed
receipt, nonzero checker exit, or any result other than `PASS` with `authorizes_release: true` stops delivery.
The gate uses Desktop only and derives the candidate SHA, harness, corpus and policy hashes from the tagged
checkout. It never accepts the offline fixture seam. It writes `release-performance-gate.json` into the
pipeline diagnostics directory.

Pass these four arguments to either release command:

```bash
--performance-receipt <evidence>/receipt.json \
--performance-receipt-sha256 <digest recorded by measurement owner> \
--performance-baseline <evidence>/approved-baseline.json \
--performance-baseline-sha256 <independently reviewed approval digest>
```

The GitHub release workflow requires three explicit dispatch inputs: `performance_run_id`,
`performance_receipt_sha256` and `performance_baseline_sha256`. The selected run in this same repository must
contain an artifact named `wisp-release-performance-<exact candidate SHA>` with this layout:

```text
receipt.json
raw/started.jsonl
raw/events.jsonl
raw/samples.jsonl
raw/instrumentation.candidate.jsonl
raw/instrumentation.baseline.jsonl
raw/environment.json
raw/provenance.json
approved-baseline.json
```

The measurement owner uploads this reviewed package through an authorized evidence workflow; this
release workflow requires that run to be completed successfully, from this repository and its own source
repository, at the exact candidate SHA. It then downloads it into `.wisp-build/performance` and verifies it. The two digests must be
copied from the owner's independent run/review record, never calculated from the downloaded package to grant
it authority. Selecting an artifact run or approving a GitHub environment does not approve a new baseline.
Measurement production/upload and baseline review are separate procedures; this change does not add an
engine runner or automate that approval. The receipt's 24-hour freshness rule still applies after the release
job's rebuild. Release diagnostics retain the raw evidence; public downloads remain the app ZIP only.

`tests/test_release_performance.py` is registered in the reviewed full Simulation QA manifest. The build
pipeline runs its complete pinned set of 361 cases in a separate sandbox using one parent-held reserved
loopback socket, then imports the exact-SHA report by digest. Its standalone profile permits only the
canonical selected interpreter (including its exact framework launcher when necessary) and the literal Git
shim and system-selected native Git executable, source/runtime reads and scratch writes. It grants no shell, compiler or executable directory
access. Unrelated loopback connections, unrelated process execution, shells, compilers, owned scripts,
copied executables and synthetic private-canary reads must be denied. The generic Simulation sandbox
keeps its network denial. Missing, changed, skipped, duplicate or partial case evidence blocks artifact
validation. Tests use synthetic evidence, temporary repositories and owned Python child process groups;
registration never selects the live benchmark command. Independent exact-head review, configured
CI, specialist QA, live qualification and a reviewed baseline remain required before a real release.

Publication evidence intake opens every directory component and file with no-follow descriptors.
Receipt and raw files share a retained package directory; symbolic/hard links, special files, mutation during reads
and oversized evidence are refused. Hashing and JSON parsing use the same immutable byte snapshots.
Limits are 4 MiB per receipt, 1 MiB per approval, 64 MiB per raw file, 256 MiB total raw data, 64 raw
files, 128 raw entries and four nested raw directory levels. Relative paths are anchored to the current
directory; callers must supply canonical paths without linked ancestors (for example `/private/tmp`,
rather than `/tmp` on macOS). These limits bound intake; the external receipt and approval digests must
still come from the measurement owner outside the downloaded package.

The Python file-descriptor guard binds descriptors registered by `os.open` to their device, inode,
file type and device identity. Every integer-descriptor mutation or file-object open rechecks that
identity; unknown, closed or reused descriptors are refused. C-level FileIO close can leave a stale
registry entry, but the old descriptor number cannot grant a newly created socket or other file
permission. Valid registered owned regular files retain their ordinary descriptor operations.

During the guard, `socket.sendfile`, `os.sendfile` and exposed `os.splice` are refused outright,
before their underlying implementations run. Reviewed JSON inference and status requests have no
file-transfer path, including inside their scoped HTTP capability. Refusals are recorded as blocked
effects and make the candidate nonpassing; permitted reviewed HTTP requests remain available.

Approval intake requires an externally supplied matching digest and a valid subject SHA/tree.
Run and check share the binding to the selected verified baseline; the candidate cannot substitute
for that baseline. An explicitly invalid/unapproved document is refused before measurement
preflight, environment collection or backend startup. No approval supplied remains a qualification
run and cannot pass.

## Waiver

A waiver is an explicit, recorded decision to publish one version without a passing release benchmark. It is
not a pass and it never produces a `PASS` verdict.

* **Who approves.** Only the project owner. The decision is the reviewed, merged commit that adds the record;
  nobody can waive from a workflow input alone.
* **Per version.** The record is `docs/releases/<version>-performance-waiver.json` (schema
  `wisp.release_performance.waiver/1`) and applies to exactly the version it names. `version` must equal the
  configured toolchain version, so a record for one version can never authorize another. Wisp 1.2.0 is the only
  waived version; 1.3.0 must restore a passing benchmark.
* **Ad-hoc only.** `scope` must be `ad_hoc_release_only`. Only `release-ad-hoc` (the workflow's
  `publish_ad_hoc` path) accepts `--performance-waiver` and `--performance-waiver-sha256`. The signed,
  notarized `release` command and the `publish` workflow path refuse any waiver and keep requiring complete
  evidence.
* **Committed and digest-bound.** The path must be exactly the record above, a tracked regular file (not a
  symlink, at most 16 KiB) that is byte-identical to `HEAD` with no staged, unstaged or untracked change. Its
  SHA-256 must equal the digest passed as the `performance_waiver_sha256` workflow dispatch input, recorded by
  the owner outside the checkout. The record must be a strict JSON object with exactly the keys `schema`,
  `version`, `decision` (`waived`), `approved_by`, `approved_on` (`YYYY-MM-DD`), `scope`, `reason` (at least 80
  characters), `evidence_ref` and `follow_up`. The dispatch ref must be the exact `v<version>` tag, which must
  already be on `main`.
* **Mutually exclusive with evidence.** Supplying a waiver together with any receipt, baseline or digest
  argument (or the `performance_run_id`, `performance_receipt_sha256` or `performance_baseline_sha256`
  workflow inputs) is refused. With no waiver the evidence gate is unchanged.
* **What the gate records.** `release-performance-gate.json` in the pipeline diagnostics, uploaded with the
  other publication diagnostics:

  ```json
  {"verdict": "WAIVED", "waived": true, "authorizes_release": true, "waiver_sha256": "...",
   "version": "...", "candidate_sha": "<HEAD>", "approved_by": "...", "approved_on": "..."}
  ```

  Any failure raises a build error that names the failed check but never echoes file content.
* **What it weakens.** Anyone who can merge a waiver record and dispatch the workflow can publish an ad-hoc
  build without benchmark evidence. The control is that the record is committed, reviewed, digest-bound and
  limited to one version.

Wisp 1.2.0 waiver: `docs/releases/1.2.0-performance-waiver.json` (the benchmark's live path was first
exercised on 2026-10-06 and its harness guard plus a memory-pressured host produced intermittent engine
health-check timeouts in the candidate, which the strict candidate rule treats as BLOCK). Equivalent 1.2.0
evidence is the live diagnostic smoke comparisons and an end-to-end A/B against v1.1.5, retained as release notes
and CI artifacts.
