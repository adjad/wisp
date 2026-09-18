# Combined preparation completion

## Runtime security and remote-readiness follow-up

Repair base: `be8a735aabfe3fd860c003f810cc87684cb6ab38`. The final committed
candidate, changed-file inventory, exact commands/results, CI and artifact hashes
are recorded in `/private/tmp/wisp-integration-<shortsha>-evidence.md` after commit.
This implementation does not approve its own release.

[MacBook/mini architecture and remote operations](MACBOOK_MINI_ARCHITECTURE.md)
is included in the mini source and offline artifact contract. It describes the
actual connection boundaries, disabled state, RC-preferred update requirement,
and limits of remote recovery. The current follow-up is not release-ready:
recovery races/replay, management serialization, and Tailnet inventory binding
are repaired and need fresh exact-commit validation and independent review.
No deployed or unattended-ready state is claimed. Update machinery remains
disabled and must follow the user's explicit
no-compatibility/canary-test policy.

The first follow-up candidate `f88976c` passed direct regression/adversarial
checks but failed build Simulation because its outer sandbox correctly denied
the new native socket/inspection fixtures. The scoped runner repair retains
that original sandbox and executes those fixtures as a separate mandatory gate
restricted to pre-reserved ports and fixed inspection executables. Full evidence
requires its exact nine cases with no skip/xfail/failure, including external and
unapproved-loopback network, executable and synthetic-private-read denials.
The combined report embeds the actual separately executed native results; a
missing, stale, duplicated or incomplete report blocks artifact acceptance.

- Shared primary/gateway transport attributes the established Darwin connection
  before nonempty writes, refuses epoch changes, and binds the complete
  materialized interpreter/package tree. Disposable spare-port tests cover rogue
  health/generation/ensure paths, gateway requests, a positive process and a
  prewrite invalidation race. Port 8000 and installed oMLX are never test targets.
- Native launchers deliver role/PID/UID/FD/generation-bound framed JSON over a
  one-use pipe. Environment credentials refuse. Native synthetic inspection
  verifies absent credentials in process arguments/environment, closed consumer
  descriptors and absent descendant inheritance. Stable Developer ID and actual
  login-Keychain adoption remain external.
- The exact authenticated invalid-cursor response resets only one node cursor
  by compare-and-swap. Actual isolated backup/rotated restore through the node
  API replays without duplicate publication. Other failures preserve progress.
- Database recovery preserves pinned source evidence and publishes new state;
  complete WAL reconstruction rejects corruption and ambiguity. Ledger recovery
  consumes nonce/history-bound authorization before publication. Reclamation
  pins full tree identities/hashes, uses exclusive rename, preserves retention,
  and supports explicit journal-bound resumption. Recovery/restart serialize
  with publication and retain operation receipts.
- Additive policy review preserves a decidable subset of unrelated concrete-IP
  rules. Fresh independent complete-export approval binds the exact target,
  stable security projection, inventory and exported policy bytes; all addresses
  of the primary and inference-tagged peers remain protected.
- Independent SSH management has bounded status/recovery preparation. The
  RC-first updater has disabled live routes and an isolated operational protocol;
  no compatibility/model/API/performance/canary tests are part of updates.

Remaining deployment gates are the actual mini host/storage/model/resource
measurements, independently trusted publisher and artifact authenticity, real
Tailnet export/publication, stable signing/Keychain/SSH behavior, and a live
supervisor that implements immutable materialization, atomic generation binding,
credential recovery and remote oMLX rollback. The updater's adapter deadline is
a cooperative protocol requirement until bounded live execution is supplied.
Physical power/network/startup-unlock failures require the contingency described
in the architecture guide. No user data, real communication, installed app,
service, Tailnet policy, or production model was changed by this task.

## Follow-up independent audit repair

Repair base `d237d90e51e1f496fdd06e7ba203b31a7cf23f5f` was independently blocked.
The new exact SHA and full validation/artifact evidence are recorded after commit
in `/private/tmp/wisp-integration-<shortsha>-evidence.md`.

- P1: token probes bind the already-established server TCP four-tuple to the approved
  launchd PID/UID, not merely the listener. Strict all-owner lsof inventory and kernel
  TCP state must agree on one server descriptor and the retained client socket. Stable
  process incarnation, socket/FD/tuple and owner are checked before Authorization;
  automatic reconnect is disabled. Synthetic accepted-connection/listener-handoff,
  ambiguity, disappearance, wrong owner/tuple and PID-reuse tests record zero token writes.
  Deliberate accepted-FD transfer by the approved process and same-UID host compromise
  remain outside observational peer qualification; no cryptographic claim is made.
- P2: every package install in release-producing workflow steps uses the checked-in
  strict hashed binary-only lock. Workflow inventory tests reject unpinned actions,
  unhashed/dev requirements, source builds and extra package arguments.
- P2: generated Tailnet assertions are mandatory unique subsets. Bounded additional
  tests may assert only concrete non-primary peer/network or account/SSH denials
  against the fixed mini tag. Wildcards, positive access, contradictory approved-owner
  denial, unknown ports, duplicate or altered assertions refuse. Valid reviewed
  additions survive rendering and live preflight; actual publication remains external.
- P3: README helper credential recovery has one authoritative section for all three
  decisions and independent historical-helper authorization.

Prior atomic release ordering, exact ACLs, historical recovery, interruption-safe
arrival, disabled defaults and secret handling remain required. This builder does
not approve the repair; fresh independent review is required.

## Independent audit repair

Repair base: `e69b4cfe363ebcf4df5f89bd9fd7426a0934c346`, blocked by the independent
release and plan-completion audits. The final repair SHA and exact mechanical,
CI and artifact evidence are in `/private/tmp/wisp-integration-<shortsha>-evidence.md`.
The builder does not approve its own repair.

- Signed sequence consumption and receiver publication/credential import/receipt
  replacement now share the install operation lock. Primary transfer and binding
  share a separate activation lock. Concurrent losers cannot consume or overtake;
  authenticated sequence and statement digest bind owners, receipts and primary
  endpoint metadata. Deterministic tests cover both orderings and replay.
- Every local oMLX HTTP probe requires the exact running launchd PID, sole
  loopback port-8000 listener and authorized numeric UID. Checks bracket connection,
  header transmission, response and restart; a PID change is accepted only at an
  explicit restart boundary. Impersonators, extra listeners and ambiguous evidence
  refuse before any token transmission.
- `restore-reviewed-prior` resolves replacement-denied quarantine using an
  independently hash-pinned private authorization for the exact historical source,
  prior/candidate inventories, journal digest and UID. Immutable historical inputs,
  signatures, final-path ACL acceptance and settings agreement are rechecked before
  renewing generation and clearing quarantine. Credential values and exact ACLs are
  retained; the replacement is not accepted. Backend refresh is required. Automatic
  ACL migration remains unsupported. Disposable qualification now exercises real
  rejection through authorized production recovery and continued replacement denial.
- Full Tailnet policy review requires the generated `tests` and `sshTests` exactly
  once as mandatory subsets. Missing or altered samples and unsafe additions refuse;
  concrete deny-only inventory additions are validated by the follow-up repair above.
  Real inventory approval and policy publication remain external.
- Arrival catches control-flow interruptions, attempts every reverse restoration,
  and reraises an interruption only after verified recovery. Inconclusive restoration
  reports recovery-required. The shipped adapter remains in-memory and simulation-only;
  durable live-adapter journaling is external.
- Supplemental regression CI now pins action commits and installs the checked-in
  hashed strict test lock with binary-only dependencies.

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
| Disabled acquisition/job adapters and portable allowlist | COMPLETE_NOW: `mini/acquisition.py` defines strict request/result/error, connector identity/version, origin/role/classification/capability/limit contracts and pinned synthetic qualification. Injected disabled/fixture adapters feed all four processors and atomically persisted occurrence/snapshot receipts; replay, backup, restart, timeout and cancellation tests | Actual vendor/tenant/OAuth selection, scopes, credentials and independently qualified live transport remain external; no live provider capability is granted |
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
supplemental CI P3 (floating action tags/dev dependencies) is repaired with full
action SHA pins and the same hashed test lock as the strict artifact workflow.

## Limits and release gates

No real host, Keychain, firewall, Tailnet, provider, model or user data is touched.
No app installation, service activation, production action or merge is included.
Hardware measurements, real administrative actions, signing and credential
qualification cannot be inferred from rehearsal assertions. The live adapter is
deliberately unavailable until independently qualified. Software volume locks only
serialize cooperating components; real quota/sole-engine enforcement is external.

The repaired preparation must receive fresh independent review before its plan
completion can be accepted. Provider-neutral acquisition is prepared and exercised with
synthetic adapters; real provider binding and actual deployment remain deferred.
Fresh Release Auditor and triggered Simulation/Live QA must assess the
full main-to-candidate diff; builders and integration helpers do not self-approve.
