# Mac Pro infrastructure implementation response

Base: `3b2a259cf898735b74cf05cb8db2174b4daf58a4`  
Branch: `codex/mac-pro-inference-foundation`

The user asked to prepare the Mac Pro first and work on the mini afterward. This candidate prepares the Pro while preserving local defaults. It does not configure a real remote host, copy private data, download models, install an application, or deploy a mini service. The user explicitly waived the unavailable Orchestrator acknowledgement for this task only; testing, independent review, and shipping gates remain.

## Decisions

- The future proactive runtime should be a separate `mini/` process. The full Wisp backend has primary-host stores, native permissions, process ownership, and UI assumptions. Portable tools must be explicitly reviewed before being admitted to the future service.
- Keep the REAP model excluded from v1. This implementation requires neither expert offload nor hardware capacity assumptions.
- Endpoint identity is an immutable `Target`: endpoint name/URL, role, model ID, revision, profile, context window, qualified capabilities, and embedding dimensions. Roles sharing a model ID can use different endpoints and credentials.
- Credentials are references: `local_omlx` is restricted to the managed loopback endpoint; remote credentials use `env:VARIABLE_NAME`. No remote endpoint inherits the Pro's key. A packaged app's backend does not inherit an interactive terminal's environment: deployment must provision references in the actual backend launch environment. No credentials are provisioned here.
- Remote settings/profile administration is not implemented through bearer inference credentials. Local model settings remain local-only.
- Pending effects are review proposals, never executable remote commands. The current Pro inbox displays them; a future approval/execution UI must bind approval to the exact proposal/content and use existing typed local workflows. No direct remote send, calendar write, reminder write, or arbitrary tool invocation is available.

## Implemented on the Pro

1. Named endpoint resolution, credential isolation, explicit HTTP proxy behavior, conservative context metadata, and atomic locked overlay replacement.
2. Local-only engine lifecycle, bounded readiness that includes HTTP waits, checked load/unload errors, and remote server-owned residency.
3. Heartbeats that preserve a silent producer; incomplete streams and truncated tool generations fail instead of becoming executable results.
4. Opt-in pre-generation fallback to local Reflex for tool-free foreground requests. Remote readiness has a configurable deadline (five seconds by default) and a 30-second circuit cooldown. Generation failures and tool workflows are not automatically replayed. The local fallback may still need its normal local startup time.
5. Foreground, `/chat`, research, tool-authoring, and explicit long-document upgrade paths resolve the appropriate endpoint. Tool authoring preserves the agent-role policy. Fast summaries and memory capture stay local.
6. Document/query embedding requests capture one target; memory and persisted tool-vector caches include model-space identity. Remote reranking never evicts the Pro's embedder. Optional retrieval failure uses lexical fallback, with static core tools as the final fallback.
7. A disabled-by-default durable node inbox. Result imports and opaque cursor advancement commit together; conflicting result IDs are rejected; publication uses the existing durable hub's dedupe keys. The app displays a dedicated presentation-only result event.
8. Settings model selection restores affected roles to local and clears remote metadata, preserving fast/router and general/agent coupling. Configured role pins resolve through current bindings on rollback; chats are retained.

## Configuration contract

No new configuration is enabled by this change. The existing `roles` and local `omlx` configuration remain supported. The following overlay is an example for later hardware qualification, not a command to activate a node now:

```yaml
inference:
  endpoints:
    mini:
      enabled: true
      base_url: https://VERIFIED-MINI-HOST.ts.net
      credential_ref: env:WISP_MINI_INFERENCE_KEY
      readiness_timeout: 5
    mini-node:
      enabled: true
      base_url: https://VERIFIED-NODE-HOST.ts.net
      credential_ref: env:WISP_MINI_NODE_KEY
  bindings:
    coding:
      endpoint: mini
      model_id: EXACT_QUALIFIED_MODEL_ID
      revision: EXACT_MODEL_REVISION
      profile: QUALIFIED_PROFILE_NAME
      context_window: 8192
      qualified_capabilities: []
      fallback_role: fast
    embedding:
      endpoint: mini
      model_id: EXACT_EMBEDDING_MODEL_ID
      revision: EXACT_EMBEDDING_REVISION
      dimensions: 1024
    reranker:
      endpoint: mini
      model_id: EXACT_RERANKER_MODEL_ID
      revision: EXACT_RERANKER_REVISION
proactive_node:
  enabled: false
  endpoint: mini-node
  node_id: STABLE_NODE_ID
```

Configure `general`/`agent` only after tool qualification; a remote tool target requires `qualified_capabilities: [tools]`. This is an explicit operator qualification declaration, not a benchmark performed by Wisp. Model revision/profile metadata identifies the operator-qualified deployment; oMLX's API does not attest that on-disk weights or an active profile match it. Verify that during bringup.

Remote URLs require HTTPS without embedded credentials, query strings, or path components. Tailnet reachability, narrow grants, authenticated listeners, and listener exposure must be verified on the actual mini. Arbitrary HTTPS is not automatically evidence of a private channel.

After manually editing the overlay or backend credential environment, restart the backend so configuration caches reload. Existing Settings role changes use atomic persistence and invalidate config caches. This candidate does not add a remote-settings editor or store plaintext keys in YAML.

To roll back a configured role, choose its known local model in Settings. That writes a local binding and clears remote metadata; the general/agent and fast/router pairs remain coupled. Disable `proactive_node.enabled`, restore known local bindings, and restart after manual changes. Preserve unrelated settings and all conversations.

## Future mini results protocol

The Pro polls the separately configured node endpoint every 30 seconds while enabled, starting when its backend starts and resuming when its event loop wakes. It never wakes the Pro. The mini must implement:

`GET /v1/results?cursor=OPAQUE_CURSOR&limit=100`

```json
{
  "schema_version": 1,
  "results": [{
    "schema_version": 1,
    "node_id": "STABLE_NODE_ID",
    "result_id": "STABLE_RESULT_ID",
    "job_id": "JOB_ID",
    "occurrence_id": "SCHEDULED_OCCURRENCE_ID",
    "kind": "study.generate",
    "title": "Study guide ready",
    "text": "The generated material appears here.",
    "proposal": null
  }],
  "next_cursor": "NEXT_OPAQUE_CURSOR"
}
```

Supported result kinds: `canvas.sync`, `study.generate`, `stocks.watch`, `research.run`, `effect.proposal`. A nonempty page must advance its cursor. An empty page must retain the current cursor or advance it; the producer must not reset it to an earlier position. The Pro rejects oversized pages and stores its cursor with the result transaction.

**Producer obligation:** `result_id` must remain stable across job retries/restarts, and its payload is immutable. Pro deduplication is per `(node_id, result_id)`, not arbitrary semantically equivalent text or different IDs for one occurrence. The mini needs transactional job/output publication to fulfill this obligation.

An effect proposal has `kind: effect.proposal` and:

```json
{"effect_id":"STABLE_PROPOSAL_ID","kind":"email.send","arguments":{"to":"reviewed-recipient","body":"draft"}}
```

Only `email.send`, `message.send`, and `calendar.write` proposals are admitted in this version. Their arguments remain untrusted stored/displayed data. Unknown envelope fields, incoming `approved` flags, raw action events, and node-identity mismatches are rejected. The Pro generates its own `node_result` event; it never forwards the remote payload to the native action dispatcher.

Current inbox publication survives interruption between local import and hub publication. The app uses its existing delivery receipts and a stable event ID to avoid duplicate transcript insertions during notification retries. This is durable deduplicated presentation, not a promise of exactly-once network transport or effect execution.

## Validation and release boundary

Tests use temporary HOME/Wisp state and synthetic HTTP transports. They cover endpoint/key separation, invalid configuration, atomic write failure, remote residency, bounded readiness, interrupted tool generation, heartbeat cancellation, pre-generation-only fallback, embedding identities, lexical fallback, node payload rejection, cursor rollback, publication recovery, and dedupe identity collisions. Existing local regression suites remain mandatory.

The repository's packaging lock drift predates this candidate and is not repaired here. The installed application is not replaced or relaunched. Native fixture/build results, final regression results, exact candidate SHA, and independent review status are recorded in the task handoff; a dirty development compile is not a release artifact.

## Work that follows this Pro candidate

- Implement and qualify the separate mini scheduler, durable job store, transactional result publication, Canvas/study/stock/research jobs, and explicit portable-tool allowlist.
- Build a proposal review/execution contract using normal Pro approvals and typed effect receipts before enabling queued effect execution. Reminder/iCloud behavior requires its own native permission and duplicate-publication qualification.
- Provision actual hardware, private transport, backend credentials, authentication, conservative model roster, and cache policy. No attached-document claim substitutes for actual qualification.
- Measure model/tool quality, long-context capacity, latency, sleep/wake, restart, and rollback on the real mini.
- Resolve the independent packaging-lock failure and obtain exact-head mechanical evidence, independent Release Auditor approval, and applicable security/native/persistence QA before task-specific shipping approval.

## Recorded builder evidence

- Base re-fetched before commit: `origin/main` remained `3b2a259cf898735b74cf05cb8db2174b4daf58a4`.
- `/Users/adijain/Desktop/MOE_Project/.venv/bin/python -B scripts/test_replay_failure_fixes.py`: **88/88 modules passed**. This recursively includes the routing scoping/no-vision tests and the new endpoint/node tests. The available interpreter is Python 3.14.3; packaged Python 3.13.14 was not exercised.
- Isolated targeted pytest after the final endpoint cleanup and configuration-response changes: **71 passed** across lazy readiness, inference endpoints, and node inbox tests. Earlier combined retrieval/context/manifest run: 144 passed, 28 subtests passed, one installed Ling template test skipped because the isolated environment deliberately has no installed model.
- `./scripts/wisp-build swift --test-python /Users/adijain/Desktop/MOE_Project/.venv/bin/python --allow-dirty --offline`: **exit 0**, native fixture commands and release-mode Swift compilation passed. Final staged `OverlayModel.swift` matched the working source. Native report: 182 reported assertions passed; seven legacy compile/contract gates did not provide complete counts. No packaged app was installed or launched.
- First native attempt failed because the outer sandbox prevented `sandbox-exec`; rerun with approved scoped escalation succeeded. Command Line Tools were available; full Xcode/strict CI toolchain qualification was not performed. Compilation produced remapped module-cache warnings, with a successful link.
- `git diff --check`: passed.
- `python3 -B -c 'import sys; sys.path.insert(0,"build-support"); import pipeline; pipeline.check_locks()'`: **failed**, pre-existing `requirements-runtime.txt` lock drift. Lock repair remains outside this branch.
- Earlier regression iterations exposed an accidentally removed helper, stale identity fixtures, new-test manifest classification, and obsolete static-core expectations. Those were repaired; the final full gate is passing. A final endpoint cancellation regression also passed after adding runner cleanup.
- Read-only helper reviews checked routing, retrieval, stream/context behavior, and the node trust boundary. These do not substitute for the required independent top-level Release Auditor or specialist QA.
- Independent release approval, required PR CI, full packaged artifact verification, actual model benchmarks, and real mini transport/reboot qualification remain outstanding. This is an implementation candidate, not a release approval.
