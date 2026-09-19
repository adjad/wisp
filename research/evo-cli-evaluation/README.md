# EVO CLI: incremental Wisp evaluation

Use EVO selectively as an experiment ledger and runner for measurable, bounded
work. Do not let a successful exit code, a high score, or an EVO commit authorize
Wisp delivery. This evaluation demonstrates those distinctions with five
synthetic candidates; it makes no claim of a Wisp performance improvement.

## Scope and prior work

Wisp base: `014aee2c9b73658e439c820f9e649ff926fabdb9`.
Prior pilot: `e27fca2aa71eef1f3b6043139493ca86e67b5e4d`, branch
`codex/evo-cli-synthetic-pilot`, directory `research/evo-cli-pilot/`.
That pilot already established check-mode rejection, trace completeness,
repeatability, and one successful full run. Its report explicitly left bad
candidate full runs untested. This directory fills that gap without copying or
changing the pilot or the separately owned routing benchmark.

Read before implementation: prior pilot runner/report/evidence/handoff, repository
AGENTS.md, TESTING.md, Wisp coordination skill as workflow documentation,
installed EVO CLI reference, reporting and shipping guidance. The Hub explicitly
confirmed ownership of this directory and superseded the stale Orchestrator
acknowledgment requirement. The coordination/ship skills were not invoked to
dispatch or ship anything. Official [EVO documentation](https://evo-hq.com/docs/)
was checked on 2026-09-19; actual conclusions below are pinned to installed CLI
0.8.0 and the recorded local executions.

Only `research/evo-cli-evaluation/` changes. No production code, Wisp benchmark
fixtures, safety gates, CI files, or Local checkout changes. No product imports,
provider calls, model loads, package installation, deployment, or merge.

## Experiment and observed results

The toy target returns unique integers in first-occurrence order. A fixed
benchmark scores three input lengths (16, 32, 64) using the mean inverse of a
deterministic operation count. A separate fixed gate checks four correctness
cases: empty input, ordering/duplicates, negatives, and singleton input.
The runner hashes the benchmark, gate, helper, and ignore file and verifies
those exact bytes in every candidate. This detects accidental harness drift;
it is not an adversarial sandbox or protection against a malicious program.

| Candidate | Score | Gate | EVO state | Full-run exit |
| --- | ---: | --- | --- | ---: |
| Baseline list scan | 0.0036 | pass | committed | 0 |
| Set-based deduplication | 0.0348 | pass | committed | 0 |
| Always return empty | 1.0000 | fail | evaluated | 0 |
| Correct, doubled operation count | 0.0018 | pass | evaluated | 0 |
| Equal baseline | 0.0036 | pass | committed | 0 |

These are synthetic proxy scores rounded by instrumentation, not measured
latencies. They compare counted list comparisons with counted set probes; they
do not model memory, hash costs, or production distributions. Counts are
candidate-reported; the doubled count changes accounting, not actual work.
The contract covers integer lists only, not arbitrary unhashable values. The intentionally
incorrect candidate obtains the highest raw score, which makes the independent
correctness gate meaningful. No optimization agent or search loop was used.

The gate-rejected and slower candidates did not change their Git HEAD. Their raw
nodes still contain an inherited parent commit, so a nonempty `commit` field is
not sufficient evidence either. `evo get` exposes the gate fields needed here;
the summarized `evo show` output did not contain `gate_result` in this version.
The accepted tie is a no-op with the baseline SHA, not a newly created commit.
Status identified 0.0348 as best, argmax frontier selected `exp_0001`, and the
report's top-experiments table excluded the invalid 1.0 result. The plot still
displays that rejected point; in colorless output, inspect state explicitly.

The accepted improvement also contained a harmless newly created
`scope-probe.txt`. Default auto-commit included it even though `--target` named
only `target.py`. `evo diff exp_0000 exp_0001` omitted that file, while the full
Git commit diff exposed it. The attempt outcome's `change_files` also listed
only `target.py`, so it is not a complete shipping inventory. This is why
candidate review must inspect all paths.
The probe exists only in ignored synthetic repositories, never in Wisp code.

## Reproduce and inspect evidence

Requires existing Python, Git, installed EVO **0.8.0**, and the prior pilot commit
object. The runner obtains the unchanged inline helper from that exact commit;
its upstream license/notice remain in the prior pilot's fixture directory.
No dependency is installed or fetched automatically.

```sh
python3 research/evo-cli-evaluation/run_evaluation.py
git diff --check
```

Run Python normally, without `-O`, because acceptance checks use assertions.
Each run creates fresh repositories under ignored `.runs/` and overwrites
`evidence.json`. It retains synthetic state for inspection. Evidence contains
commands, exits, raw nodes, benchmark task maps, attempt outcomes, protected
hashes, report, frontier, target diff, and complete candidate file list.
`repeatability.json` compares two completed fresh runs: every candidate's state,
score, gate result, and three task scores matched. Timestamps and synthetic
commit hashes intentionally differ. Re-running the runner alone does not
regenerate the separately recorded two-run comparison.

Validation performed: two successful five-candidate runs; repeatability
comparison; AST parsing; Git whitespace check. One initial harness execution
stopped after the baseline because it expected `gate_result` in `show`; the
runner was corrected to use `get`. Independent read-only source review confirmed
full-run exit semantics, tie acceptance, commit scope, and selection caveats.
This helper review is not Wisp Release Auditor approval.

Initialization returned exit 1 with `no free port` in the execution sandbox,
as in the prior pilot. The runner accepts only that specific initialization
error with an existing config and then uses the working headless CLI. It calls
the version-sensitive internal dashboard cleanup helper only in the newly
created synthetic repository. Its exit was 0 in the completed runs, but cleanup
is best effort and no independent process-absence check was performed.
Dashboard UI/network behavior is unvalidated.

Subprocesses receive an environment allowlist, synthetic HOME/EVO_HOME, disabled
Git global/system config, telemetry disabled, and version checks disabled.
Benchmarks have a ten-second configured timeout and commands a 45-second outer
timeout. This is finite CPU work, not a monetary budget enforcement mechanism.
No paid service was used. The runner is not a network sandbox; no packet-level
audit was performed. On a host allowing sockets, EVO init may start its local
dashboard before cleanup. No secrets or personal inputs are supplied.

## Concrete Hub use

| Wisp activity | Recommended EVO use |
| --- | --- |
| Ordinary feature or known deterministic bug | Existing worktree/tests; EVO adds little unless alternatives need comparison. |
| Bounded optimization | One owned target, fixed representative corpus, correctness gate, baseline, finite candidate budget; record per-case traces and actual performance separately. |
| Regression discovery | Inject known failures first to validate gate sensitivity, then inspect named failed cases across variants. EVO records supplied tests; it does not invent coverage. |
| Progress reporting | Read status/report and stored outcomes without rerunning expensive benchmarks. Frontier is for choosing the next branch, not necessarily the global shipping winner. |
| Candidate delivery | Export candidate SHA and complete Git diff plus evidence to the Hub's existing independent review/CI path. Do not auto-ship an EVO winner. |

For the next real Wisp experiment, reuse the separately owned routing corpus
rather than launch a duplicate benchmark. Require objective improvement on
representative held-out inputs and no accuracy/safety loss; visible synthetic
cases cannot establish generalization. Keep fixtures, scoring, gates and CI
outside the candidate write scope. Hash checks help detect drift but do not
enforce OS-level access control. An explicit file allowlist must cover the full
candidate diff; `tracked-only` also does not enforce path ownership.

Before handing a candidate to review, record its valid state, gate outcomes,
exact base/head, complete changed-file list, benchmark contract and evidence.
Re-derive a minimal change in its delivery worktree, rerun Wisp mechanical
checks on that exact new SHA, require independent Release Auditor approval and
required CI, then honor task-specific shipping authorization. There was no
shipping execution in this evaluation. CI is **unavailable/non-passing** here;
no product tests or release checks were run for these research-only files.

Limitations: no real Wisp quality/latency/cost evidence, model-backed search,
remote compute, crash recovery, hostile code containment, timeout behavior, or
actual PR/merge validation. No production integration dependency. Reproduction
depends on the prior pilot Git object; retain it with this evidence. This
research candidate still requires the Hub's normal independent review before
repository integration.
