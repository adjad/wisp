# EVO development CLI pilot — 2026-09-19

Recommendation: adopt selectively for bounded, measurable development experiments. The local CLI passed this synthetic acceptance pilot, with a dashboard startup limitation. This is not evidence that EVO improves Wisp quality, latency, cost, or shipping safety. Keep the Hub, independent release review, and required GitHub CI as delivery authority.

## Scope and provenance

Sole writer: this EVO CLI pilot task. Wisp base: `014aee2c9b73658e439c820f9e649ff926fabdb9`. Owned files: `research/evo-cli-pilot/` only. The test creates its own temporary Git repository and experiment worktree; no Wisp files are imported. It excludes the existing routing experiment, local oMLX, installed Wisp, other worktrees, main, existing PRs, deployment and account changes. No integration dependencies.

The installed CLI and discover skill both report `0.8.0`. Read the installed `evo:discover` skill, benchmark-construction reference, instrumentation contract, CLI reference, and copied Python inline helper unchanged. This is a finite CLI acceptance test, not an autonomous optimization search; optimize/ship were not invoked. Inline instrumentation avoids a package installation.

Official sources reviewed:

- [EVO docs](https://evo-hq.com/docs/): local experiment backends, command/result contract, exit-code gates, check mode, and telemetry controls. Gates inherit through experiment ancestry; check mode runs validation without consuming an experiment attempt. Telemetry is enabled by default and supports a process-local opt-out. Agent-driven optimization is a separate workflow from the CLI.
- [EVO public repository](https://github.com/evo-hq/evo): CLI/plugin installation, tree search, shared experiment history, host support, and Apache-2.0 licensing. Repository code licensing does not price hosted services or model usage.
- [Company homepage](https://evo-hq.com/): broader product context. Its inference-savings marketing was not tested or used as evidence for this pilot.

## Plain test plan

Parse lists of synthetic integers. Score 12 fixed cases; require six separate validation cases to pass as an inherited gate. Check the same source twice, compare all task scores, then inject failures to ensure errors are rejected. Restore the source and run one accepted baseline. Verify the synthetic base commit is unchanged and both synthetic working trees are clean. All benchmark subprocesses have a 15-second EVO limit; runner commands have a 45-second limit. No autonomous continuation.

## What actually ran

See `evidence.json` for complete commands, exit codes, durations, state snapshots, fixture SHA-256 hashes, interpreter/platform, source commit, results, and all 12 baseline traces.

| Test | Observed result |
| --- | --- |
| CLI/skill version | 0.8.0 matched |
| Standard worktree initialization | Local state created; exit 1: no free port in 18880..18899 |
| Headless continuation | Gate registration, experiment creation, run/check/status/tree worked |
| Two positive `run --check` calls | Both 1.0, identical 12 task scores |
| Separate validation gate | Six of six passed; exit 0 |
| Empty-list replacement parser | Standalone gate and run-check rejected, exit 1 |
| Missing result artifact | Rejected with `missing_result_json` |
| Nonnumeric score | Rejected with float-conversion error |
| Duplicate result writer | Rejected with benchmark exit 1 |
| Check-only state preservation | Pending status, null score, empty attempts remained unchanged |
| Restored baseline `evo run` | Committed, score 1.0; 12 successful per-case traces |
| Synthetic base preservation | Original HEAD unchanged; base and experiment working trees clean |

The port failure is consistent with this sandbox's socket restrictions; it is not proof of 20 occupied ports. Dashboard UI/HTTP behavior was not validated. The runner tolerates only this specific init error, verifies the initialized config exists, and continues with standard worktree CLI commands. It invokes EVO's internal cleanup helper solely on the newly created repository; this version-sensitive use is another reason to pin 0.8.0. No working dashboard URL was obtained.

Initial runner development also exposed two harness mistakes, corrected before final validation: config is under `.evo/run_0000/`, and `evo path` displays ancestry, not a filesystem path (`evo get` provides `worktree`). The first startup failure is retained in `evidence-worktree-init.json`. Repeated full runs were for these fixes and stronger reviewer-requested assertions, not optimization attempts.

## Reproduction

With existing EVO 0.8.0 and Python available:

```sh
python3 research/evo-cli-pilot/run_pilot.py
```

The runner does not install dependencies. Every execution creates a new temporary repository and overwrites this directory's latest `evidence.json`. Temporary experiment state is deliberately retained; its absolute path is recorded in that file. The original result artifact paths are machine-specific. Committed evidence includes portable JSON content and fixture hashes. Exact timestamps, UUIDs and synthetic commit hashes may vary; reproducibility assertions compare score/task outcomes, not byte-identical metadata. The inline helper is copied from the installed EVO plugin 0.8.0, `skills/discover/references/inline_instrumentation.py` (upstream Apache-2.0 project).

## Costs and privacy

No paid API requests, purchases, subscriptions, credential provisioning, GPU use, model loads or cloud execution occurred. Incremental external service spend for the pilot was $0; ordinary Codex session/reviewer usage and local electricity are not measured or included in that figure. Captured subprocess time is a few seconds per completed acceptance run, not an inference-performance benchmark. Future cost is host-agent usage plus benchmark/model/provider calls and optional compute; attempt limits are not monetary caps. Establish a dollar cap separately before any paid experiment.

The final runner uses an environment allowlist to avoid inheriting credentials or host session identifiers. It sets `EVO_TELEMETRY=0`, `DO_NOT_TRACK=1`, and `EVO_SKIP_VERSION_CHECK=1`, with `EVO_HOME` in a temporary sibling directory. Global/system Git configuration is disabled for synthetic subprocesses. No global preference is changed. Early version/help probes preceded the full isolation wrapper; no Wisp content was supplied to them, and this pilot did not packet-capture their network behavior.

Read-only inspection of installed source confirmed telemetry and version-check controls are separate. Normal benchmark processes inherit shell environment unless the caller constrains it; gates remove EVO artifact variables. Benchmark execution uses the base root as cwd, making `{worktree}/benchmark.py` important. Accepted experiments can auto-commit unignored files. Local `.evo` state includes traces/logs and a workspace key; retain it deliberately and never assume pushing experiment Git commits also preserves evaluation history. The test used only synthetic inputs. Packet-level absence of network traffic was not independently audited.

## Limits and adoption decision

The fixed validation cases are visible to an agent and do not prevent memorization. This score demonstrates bookkeeping/correctness mechanics, not a useful optimization win. No model quality, Wisp integration, statistical performance, optimizer convergence, cloud backend, Router, SDK package, crash recovery, full-run rejection of a bad candidate, or dashboard UI was tested. Negative controls used check mode; one good baseline exercised actual commit mode.

Use EVO when Wisp has a stable measurable objective, protected fixtures/scoring/gates, explicit file ownership, and bounded resource/spend limits. Its useful contribution is per-experiment evidence, repeatable command execution, and failed-check diagnostics. For an ordinary feature or deterministic bug fix, the extra experiment machinery offers little benefit. Do not replace GitHub CI, independent review, task-specific shipping approval, or credential/environment hygiene with EVO gates.

Independent read-only helper reviewed installed side effects and benchmark logic. Its requests for explicit score/task/state/failure assertions were incorporated. This is not the Wisp Release Auditor's release approval. Repository regression tests and required GitHub checks were not run: no product code was changed and no release candidate/PR is proposed. CI is unavailable/non-passing for shipping purposes. Next action is to use this evidence alongside the separately owned routing experiment before broader adoption; no new inference workload is needed.
