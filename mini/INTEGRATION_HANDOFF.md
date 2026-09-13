# Combined preparation completion

Integration base: `a8eff22332fc8b5ecbccf8a67a0e7c4d9f742a93`.
Mini component: `3d27d52015d265836e7261f29e382b61c8e0c6d6`, merged with
`05e69a2c05a373c8a55d2c0af468b1d5415bf843`.
Provisioning component: `3e5265c0ba438afe2c601aeaa4def4b0b4a000d5`, merged with
`18d939b84136ffd971f30381861a0378a749f6cc`.
Both merges preserved complete histories and had no textual conflicts. The final
candidate SHA, exact commands/counts, CI IDs, artifact hashes and clean-state
evidence are recorded in the task's final handoff after committing this document.

## Original-plan preparation matrix

This matrix resolves the preparation findings in
`/private/tmp/wisp-plan-completion-audit-a8eff22.md`. COMPLETE_NOW describes code
and synthetic preparation, never production rollout or independent gate approval.

| Original missing/contradicted outcome | Preparation disposition and evidence | External realization |
| --- | --- | --- |
| 60 GB memory, 2 GB cache, 20 GB paged KV; one request | COMPLETE_NOW: exact `mini.resources.POLICY`, ResourceGuard admission/watchdog and gateway cancellation latch; `test_mini_resources`, `test_mini_http` | Real engine limits and sole-backend attestation |
| 150 GB initial free, permanent 50 GB reserve | COMPLETE_NOW: resource/store/backup admission, predicted growth, cooperating volume leases; resource/backup/store suites | Real disk layout and OS quota against unrelated processes |
| No expert offload; at least 4-bit models | COMPLETE_NOW: exact contract policy and per-model quantization validation, bound arrival evidence | Model weights, expert residency and engine profile inspection |
| 8k before 16k, pinned roster/profile | COMPLETE_NOW: fillable revision/tokenizer/runtime/profile contract, ordered context/report binding and synthetic rejection tests | Select concrete deployment roster; measure hardware profiles and benchmarks. Empty defaults never qualify |
| Scheduler, missed occurrences, restart/catch-up | COMPLETE_NOW: `mini/runtime.py`, immutable snapshot/occurrence staging and pending-first recovery; `test_node_runtime_completion` | Explicit rollout enablement; jobs remain disabled |
| Disabled portable job adapters/allowlist | COMPLETE_NOW for snapshot processing: four typed job kinds, per-kind portable qualification, fixed `text.outline` allowlist; runtime tests | Provider acquisition is not implemented. Actual Canvas/stocks/research service selection, scopes, credentials and separately authorized provider integration remain external decisions; no provider capability is claimed |
| Signed transfer | COMPLETE_NOW: publisher signature verification before export/staging, source/archive/sequence binding and replay refusal; `test_artifact_signature` | Independently provision publisher identity/private signing key and release approval |
| Local rotation without shell history | COMPLETE_NOW: explicit local auth migration/rotation, descriptor/native CAS, rollback quarantine; `test_local_auth_completion` | Real oMLX and Keychain agreement/ACL qualification |
| No inbound firewall exceptions; non-root admin | COMPLETE_NOW: conservative full exception inventory and admin-membership assertion; `test_node_prep` | Actual host/admin/firewall and Tailnet observations |
| oMLX install/config/supervision assets | COMPLETE_NOW: versioned disabled templates, verified-artifact renderer and mandatory packaged preparation assets; arrival tests | Real version/signature/executable and upstream settings qualification |
| Backup/restore and storage runbook | COMPLETE_NOW: SQLite online snapshots, integrity/schema validation, atomic new-directory publication and identity rotation; `test_mini_backup`, RUNBOOK | Operator-approved actual state backup/restore and consumer coordination |
| Credential transaction recovery | COMPLETE_NOW: immutable-source inventory/ACL/token/settings agreement, explicit recovery command and fresh generation; `test_credential_recovery_completion` | Real private Keychain and installed identity |
| Complete rollback refresh | COMPLETE_NOW: generation/config/listener-bound refresh receipt and native acknowledgement; `test_primary_runtime_completion` | Real owned backend refresh |
| Arrival/model/storage/backup binding and migration workflow | COMPLETE_NOW for preparation: integrated evidence validator and in-memory ordered adapter, reverse verification, independent approval pins, replay refusal, explicit simulation/live separation; `test_arrival_completion` | Qualified live adapter, real host/network/credential/hardware/model evidence and explicit apply permission |
| Bundle and central test registration | COMPLETE_NOW: full mini FILES mandatory, preparation assets mandatory for offline runtime; all three new tests unconditionally registered | Fresh exact-SHA mechanical/CI and independent review still required for every candidate |

Existing fixed routes, literal presentation, local fallback, signed helper source,
credential quarantine, disabled jobs/providers, rollback and conservative network
boundaries are retained and included in the broad regression suites. The prior
supplemental CI P3 (floating action tags/dev dependencies) remains a documented
note; the strict artifact workflow is independently pinned and hash-locked.

## Limits and release gates

No real host, Keychain, firewall, Tailnet, provider, model or user data is touched.
No app installation, service activation, production action or merge is included.
Hardware measurements, real administrative actions, signing and credential
qualification cannot be inferred from rehearsal assertions. The live adapter is
deliberately unavailable until independently qualified. Software volume locks only
serialize cooperating components; real quota/sole-engine enforcement is external.

The original plan is complete as a disabled preparation workflow only within these
explicit boundaries. Provider acquisition and actual deployment are not claimed
complete. Fresh Release Auditor and triggered Simulation/Live QA must assess the
full main-to-candidate diff; builders and integration helpers do not self-approve.
