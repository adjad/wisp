# Wisp provider operations

Start with the [dashboard](DASHBOARD.md), follow the [setup checklist](SETUP.md), and update it using the [reporting runbook](REPORTING.md). This is a human-maintained operations workspace, not a live monitor or spending-control implementation. No scheduled jobs or account connections were created.

Snapshot: 2026-09-20 UTC (2026-09-19 Pacific). Sole writer: operations task `01a0bd6e-8d2c-7ab0-b919-17671becef4c`, isolated worktree `f9ef`. Base: `014aee2c9b73658e439c820f9e649ff926fabdb9`. Hub acknowledged `docs/provider-operations/**`; it confirmed the retired Orchestrator is not an acknowledgement gate. This workspace mirrors evidence, not ownership authority.

## Roles and decisions

| Service | Role for Wisp | Current boundary |
| --- | --- | --- |
| Orchestra / Understudy | Prepare evaluations, compare models and routes; potentially train workload specialists | Local pilot validated preparation. Hosted quality, account entitlement and price remain unverified here. |
| OpenRouter | Access and route to provider-hosted models through a common API | Offline protocol pilot only. User wants a chosen model to replace Ling across chat/tool roles eventually; embeddings/reranking remain local. No replacement implemented here. |
| EVO | Track bounded development experiments, scores, attempts and gates | Local CLI mechanics tested; no demonstrated Wisp improvement. EVO is not the serving provider or billing authority. |

The source chats record acceptance of personal data being sent to Orchestra and OpenRouter. Preserve that consent; do not falsely report a blanket local-only user preference. It does not authorize credential provisioning, paid runs, billing changes, arbitrary dataset capture or this task changing Wisp runtime. Real credentials remain in Keychain or provider-supported secret storage, never this workspace.

A future experiment may use Orchestra for comparison, OpenRouter for inference and EVO for experiment bookkeeping. Do not chain gateways by default or count the same provider charge three times. Deployment, account connection and paid experimentation remain separate actions.

## Authoritative source tasks

Inspect these tasks again before account setup or changing status. Titles below are exact at retrieval; IDs are durable.

| Task | ID | Evidence inspected |
| --- | --- | --- |
| Test Orchestra for Wisp | `01a0bae2-31be-7ac3-87b4-da9172ee3f0a` | Chat decisions; original pilot; newer committed routing harness |
| Test OpenRouter for Wisp | `01a0bae2-525d-73b0-ab68-301006bd331b` | Chat decisions; offline pilot report |
| Test EVO for Wisp | `01a0bae2-1452-75b1-8a9a-c61dfa7bf7fc` | Chat handoff; CLI pilot report |

Portable repository evidence links:

- [Orchestra original pilot at b80d10e](https://github.com/adjad/wisp/blob/b80d10e/experiments/orchestra-pilot/README.md): nine CLI commands, 120 usable examples, deterministic disjoint groups, invalid-data rejection. A local-only card produced an advisory cloud candidate; that is not transmission permission.
- [Orchestra setup at e6808bdb5c4d79b0055e50cea41c85440fef2b9c](https://github.com/adjad/wisp/blob/e6808bdb5c4d79b0055e50cea41c85440fef2b9c/experiments/orchestra-wisp-routing/README.md): disk-observed clean source worktree HEAD; durable pinned toolkit wrapper, 85-case corpus and routing evaluator. Remote availability of this newer commit was not checked. No setup command was rerun here.
- [OpenRouter pilot at ee950e7387f6d279a6f79b6742ab72d4fb5d2438](https://github.com/adjad/wisp/blob/ee950e7387f6d279a6f79b6742ab72d4fb5d2438/experiments/openrouter_pilot/README.md): 22 synthetic tests; live compatibility untested; repository pytest check blocked by missing dependencies.
- [EVO pilot at e27fca2aa71eef1f3b6043139493ca86e67b5e4d](https://github.com/adjad/wisp/blob/e27fca2aa71eef1f3b6043139493ca86e67b5e4d/research/evo-cli-pilot/README.md): CLI 0.8.0; 12 scoring and six validation cases; dashboard initialization failed in sandbox, headless CLI worked.

Recent Orchestra/OpenRouter turns returned empty item lists; Orchestra also had an active turn. This is a snapshot gap, not proof nothing happened. Account state, new approvals, later usage and newer setup execution may exist outside readable evidence. No Keychain, auth file, raw private evaluation output or account dashboard was inspected. Original pilot results are source-reported, not independently rerun or billing-audited in this task.
