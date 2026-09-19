# Orchestra / Understudy bounded pilot — 2026-09-19

Recommendation: use selected offline dataset/evaluation preparation tools experimentally; do not adopt the hosted gateway or replace Wisp's runtime based on this pilot. The tested component is the real Understudy open-source CLI, not an Orchestra-hosted model, desktop app, agent runner, or model-training service. No quality, latency, or savings improvement was measured.

## Product and supported integration

Orchestra describes a workflow-improvement platform: observe work, evaluate outcomes, improve prompts/models, and deploy a selected route. Its public integration uses existing OpenAI or Anthropic SDKs with an Understudy key and gateway URL (`https://api.understudylabs.com/v1` for OpenAI; origin without `/v1` for Anthropic). This is not evidence of a general-purpose agent-orchestration SDK. [Official platform](https://orchestra.ai/platform).

The linked Understudy project provides an MIT local CLI and coding-agent playbooks. Its offline evidence workflow does not require registration. [Official agent-tools documentation](https://docs.understudylabs.com/open-source/agent-tools). Tested source: [understudy-agent-tools at c70c889e5167a6e7a5f94f901411d42108c4ef2e](https://github.com/understudylabs/understudy-agent-tools/tree/c70c889e5167a6e7a5f94f901411d42108c4ef2e), package 0.6.41, Node v24.21.0.

## Scope and test plan

Sole writer: this pilot task, acknowledged by Wisp Hub. Base: `014aee2c9b73658e439c820f9e649ff926fabdb9`. Owned paths: `experiments/orchestra-pilot/**`. No integration dependency or ownership overlap. The Hub explicitly superseded the stale Orchestrator acknowledgement policy before pilot edits; its current policy was read from Local without modifying it.

Plan: generate a synthetic intent-classification table, compile and inspect it, prepare deterministic grouped train/dev/holdout splits, test invalid inputs, compare default/local route plans, and check that missing evaluation evidence blocks optimization. This is tooling validation, not a classifier benchmark. Labels in the synthetic text intentionally make these examples unsuitable for any accuracy claim.

Public source/dependencies were fetched into temporary directories. `npm ci --ignore-scripts --no-audit --no-fund` succeeded, then `npm run build` succeeded. No global installer or plugin registration ran. CLI calls used an environment allowlist, telemetry disabled, Node filesystem permissions limited to the toolkit and temporary run directory, and a guard against common Node network entrypoints. The guard is defense in depth, not a comprehensive egress audit. No credentials were read; no model/provider commands ran.

## Actual results

`python3 experiments/orchestra-pilot/run_pilot.py /private/tmp/wisp-orchestra-pilot-tools` passed all assertions. It ran nine real CLI commands, including three intentional rejections:

| Check | Result |
| --- | --- |
| Metadata-only compile | Passed; payload_read=false |
| CSV inspection and preparation | 122 rows submitted, 120 retained; one duplicate and one unlabeled row removed |
| Group-aware split | 72 train / 24 dev / 24 holdout, all four labels in each, zero group overlap |
| Repeated preparation | Identical split content hashes |
| Label used as input | Rejected with exit 1 |
| Source changed after inspection | Rejected with exit 1 |
| Default route plan | evaluate-first, but cloud candidate despite local-only card |
| Explicit local fallback | Local candidate retained; no model executed |
| Optimization without evidence | Rejected with exit 1; missing harness/environment/metric/splits/baseline |

The nominal split policy is 70/15/15; this small grouped fixture produced 60/20/20. Consumers should inspect actual counts. Independent checks verified hashes, labels, and disjoint groups rather than trusting the manifest alone.

One pilot-harness attempt failed before any CLI work because Node denied resolution through macOS's `/var` symlink. Using a canonical `/private/tmp` directory fixed the harness. The final run passed. Initial sandboxed Git download also failed DNS; the permitted network-enabled public-source fetch succeeded. Dependency installation emitted a node-domexception deprecation warning.

Evidence: `summary.json` and `commands.json` preserve results/stdout/stderr, with the temporary directory replaced by `<PILOT_TMP>`. Raw final artifacts remain at `/private/tmp/wisp-orchestra-run-ktou7pqy`. `run_pilot.py` regenerates synthetic inputs and asserts the pinned vendor commit. External source, dependencies, generated raw datasets and model weights are not vendored. `git diff --check` passed. Production tests/CI were not run: no production changes and no release candidate. No independent release approval is claimed.

## Material finding and privacy

Observed: the default workload card says `mode=local-only`, yet the route packet suggests Understudy managed cloud with `approval_required=false`. `src/route-decision.ts:144-158` explains this default when no fallback exists. This command only writes planning output; it did not transmit data or promote a route. For Wisp, require explicit local constraints and independent destination enforcement before consuming such output.

The public privacy notice says gateway payloads are not stored by default, while metadata is retained and routed requests reach upstream providers. It gives no fixed metadata-retention duration. [Privacy notice](https://orchestra.ai/privacy). These are vendor statements, not audited properties.

The pinned source's `docs/privacy-and-data-boundaries.md` adds a critical distinction: Desktop defaults may use managed cloud, and dropping a dataset can send all or representative rows to the active model. This pilot did not use Desktop. Metadata artifacts include paths; classification splits contain transformed text, not anonymized data. Keep real traces private. A read-only helper independently reviewed these boundaries and identified the route default before execution confirmed it.

## Access, costs, and next decision

Actual API spend: **$0**, provider calls: **0**. Ordinary local compute/storage and development time remain costs; neither was priced. The public MIT tools require no paid subscription for this workflow.

Hosted testing is blocked by the absence of an approved Understudy credential/project and provider or managed-service access/budget. No existing credential stores were inspected and no signup was attempted. Current docs support both BYO and managed modes, while the older service terms still describe early-access BYO; verify current account entitlement rather than assuming it. [Modes](https://docs.understudylabs.com/concepts/modes), [CLI authentication](https://docs.understudylabs.com/open-source/cli). Fees depend on an agreement/order/checkout; upstream charges can apply and early access has no SLA. [Terms](https://orchestra.ai/terms). No published rate was substituted for a quote.

No hosted mock was needed because a supported local workflow actually ran. Hosted routing, capture, fallback reliability, retention enforcement, model training, local MLX compatibility, inference latency, and model accuracy remain untested. A future hosted pilot would require explicit credential provisioning and spend approval. A better next step for Wisp is an offline evaluation against independently labeled held-out intents using its existing local model, scheduled separately from active inference experiments. Adopt further only if that shows incremental value beyond Wisp's existing tests and EVO workflow; keep production traffic unchanged until then.
