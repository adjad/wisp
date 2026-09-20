# Setup checklist

Checked items mean evidence inspected, not independently reproduced. Follow the authoritative source task for each service; do not copy or edit its harness from this workspace.

## Common preparation

- [x] Inspect all three source tasks and available pilot reports; record snapshot gaps.
- [x] Separate provider billing from local experiment metrics; leave unknowns unknown.
- [x] Preserve recorded consent to personal-data routing and the eventual Ling replacement goal.
- [ ] Refresh recent source-task turns before connection; check for newer approvals/configuration.
- [ ] User signs in or explicitly approves use of an existing dedicated credential. Do not infer lack of an account from a pilot that never checked it.
- [ ] Store secrets in macOS Keychain or provider-supported storage. Record only a neutral local alias such as `wisp-reporting`; no key fragments, emails or account IDs in Git.
- [ ] Confirm account plan, billing currency, reporting scope, period/timezone, available export/API and permissions. Do not broaden permissions merely to fill a dashboard.
- [ ] Approve any actual workload separately, with model/provider, data scope, request/token/wall-time caps and maximum spend. Existing credit balance is not approval.

## Orchestra / Understudy

- [x] Original offline CLI pilot completed, source-reported; tested vendor commit `c70c889e5167a6e7a5f94f901411d42108c4ef2e` / package 0.6.41.
- [x] Newer source setup describes `.understudy/vendor/understudy-agent-tools` and `experiments/orchestra-wisp-routing/understudy`; observed files and source commit, no execution here.
- [ ] In the source task, verify installation status without printing credentials. User completes supported sign-in at [Understudy](https://app.understudylabs.com/), or approves the documented CLI login flow. Do not put one-time codes in committed logs.
- [ ] Confirm the actual credential storage backend before login. The setup README says outside the repo; that alone does not prove Keychain or protected storage.
- [ ] Verify hosted access and existing project/workload; reuse an appropriate existing workload. Creating a project/workload and enabling capture are account/data changes requiring explicit scope.
- [ ] Establish managed versus BYO entitlement. Public docs and older service terms differ; the account/order governs. Obtain inference, evaluation, training, serving/minimum, storage and upstream charge units plus ceilings in writing.
- [ ] Confirm data retention, upstream providers and deletion terms. Generic gateway defaults do not cover an authorized capture/training workflow.
- [ ] Review the plan-only evaluation in its source task. Hosted `--execute`, `workloads create --capture` and dataset upload are not reporting steps. The harness lacks an enforceable dollar cap; do not run it on a presumed price.

[CLI documentation](https://docs.understudylabs.com/open-source/cli), [service terms](https://orchestra.ai/terms), [privacy](https://orchestra.ai/privacy). Hosted metadata is retained and routed requests reach upstream providers; training/evaluation retention needs explicit agreement. Public benchmark economics are not a price quote.

## OpenRouter

- [x] Offline protocol prototype and 22-test source result inspected; no production integration demonstrated.
- [ ] User signs in to [OpenRouter](https://openrouter.ai/) and approves a dedicated existing key/reporting connection or a sanitized Activity export.
- [ ] Store the key securely; restrict its purpose, expiry and budget through separately approved account settings. No credit purchase, plan change or auto-top-up setup in this task.
- [ ] Prefer current-key usage for a dedicated Wisp key. Account credits require a management key; use manual account export rather than acquiring broad access unnecessarily.
- [ ] Record the selected model slug and provider, tools/streaming/context capabilities and data-policy support. The full Ling replacement is a future provider-adapter/configuration/readiness workstream, not this operations workspace.
- [ ] Verify available spend controls and routing constraints for the actual plan. Current public pricing lists Standard 5.5% and Business 8% platform fees; do not automatically multiply each reported inference charge by these rates. Funding fees and model charges require distinct reconciliation. [Pricing](https://openrouter.ai/pricing).
- [ ] If a live trial is later approved, enforce a conservative pre-call reservation, count follow-ups/retries, and stop on unknown price/usage or exhausted allowance. The source's $0.25/12-attempt proposal is not a funded or enforced budget.

## EVO

- [x] Source pilot reports installed CLI/plugin 0.8.0 and successful isolated acceptance checks.
- [ ] Reconfirm version compatibility before future use; retain the source experiment artifacts privately. No fresh run is needed for this dashboard.
- [ ] Keep experiment state, attempts, scores and gates in EVO; summarize only allowlisted aggregates here. Do not invent a parallel ownership registry.
- [ ] For a future authorized experiment, protect evaluation fixtures/gates, bound attempts and elapsed time, serialize local inference, and account separately for host-agent, model-provider and optional compute charges.
- [ ] Use a constrained process environment. Source pilot used `EVO_TELEMETRY=0`, `DO_NOT_TRACK=1`, and `EVO_SKIP_VERSION_CHECK=1`; telemetry and version checks are separate. Do not copy `.evo` wholesale: it can contain traces, logs and workspace keys.

The demonstrated local workflow needs no hosted account. Paid backend credentials are required only if that backend is selected and approved. EVO gates and scores do not replace release review or required CI. [EVO documentation](https://evo-hq.com/docs/).
