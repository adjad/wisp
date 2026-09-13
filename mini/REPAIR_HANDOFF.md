# PR 46 boundary repair handoff

Base: `aeeba45f9416f64e0f17ebb904713b55d296ae83` (`codex/primary-mini-combined`).
Branch: `codex/pr46-boundary-repair`. The final task handoff records the exact
committed head and its subsequent mechanical results; this document is part of
that candidate, not an independent approval.

The repair keeps node content literal, reconstructs gateway responses, requires
terminal inference completion, binds fixed native credentials to reviewed roles
and peer origins, checks native ACL/helper identity, verifies the owned SQLite
catalog, and stages a complete offline artifact transactionally. Jobs, connectors,
and effects remain disabled. No app was installed, no service was activated, and
no live Keychain, Tailscale, firewall, launchd, oMLX, or user data was accessed.

## Finding-to-test matrix

| Finding | Regression evidence |
|---|---|
| 1: node presentation | `NodePresentationChecks.swift`: adversarial title/body, shell/AppleScript fences and unsafe links remain literal; malformed events rejected. `OverlayView` bypasses Markdown for node turns. Notification delivery remains outside transcript deduplication so failed notifications retry. |
| 2: artifact provenance/closure | `test_mini_contract`: full hash-locked closure, exact source mismatch, source-only artifact rejection, digests. `mini_artifact.py`: independent builds, offline wheel installation/check, pinned Python/compiler metadata, relocated health. CI checks out and labels the exact PR head. |
| 3: transactional staging/reuse | `test_node_prep`: interrupted health/import, no premature receipt, safe retry, changed binary rejection and non-disclosure. Runtime inventory reconstructs every type/mode/hash from pinned bytes; helper signatures and health are rechecked. |
| 4: rollback ownership | `test_node_prep`: loaded job with absent state refuses any unowned bootout. Receipt, release marker and inventory must agree before unloading. |
| 5: gateway schemas | `test_mini_http`: route-specific chat/embedding/rerank/health shape failures; nested/top-level diagnostics omitted; finite numeric bounds; strictly reconstructed terminal usage trailers. |
| 6: stream terminal | `test_inference_endpoints`: direct plain streams with premature DONE or unknown/tool-only finish reasons fail. `test_mini_contract` exercises premature DONE through the gateway. |
| 7: credential binding | `test_primary_credentials`: cross-role swaps, arbitrary hosts, ports, node IDs and normalization variants; private receipt schema failures; inherited bridge removal. Legacy unrelated env references retain compatibility. |
| 8: actual health | `test_mini_http`: absent/starting/error/malformed health cannot advertise readiness. `test_inference_endpoints` supplies actual health separately from model availability. |
| 9: ACL/helper identity | `BackendCredentialChecks.swift`: nil/trust-all, missing/extra/changed reader sets fail. `test_node_prep`: binary digest, mode, symlink, receipt and protocol mismatch refuse helper reuse. Native code validates signatures and decrypt/ANY ACL identities before reading. |
| 10: SQLite ownership | `test_mini_store`: dropped immutable triggers, added indexes/tables fail startup; existing durability, collision, corruption, concurrency and immutable-result tests retained. |
| 11: capacity/Serve posture | `test_mini_store`: occurrence limit advertised. `test_node_prep`: exact Serve host, ports, root paths and loopback backends; unknown alternatives rejected. |

## Validation and limits

Mechanical commands: focused pytest suites; `scripts/test_replay_failure_fixes.py`;
Swift app build; credential/presentation fixture compilation and execution;
`build-support/simulation.py` native gates; `pipeline.check_locks()`;
`build-support/mini_artifact.py --source-sha HEAD --development`; diff and
secret/redaction checks. Final counts and exact-head logs are reported separately.
Initial fixture assumptions (health returning model JSON and accepting malformed
chat/retrieval responses) failed after tightening boundaries and were corrected.
A development artifact health check initially rejected a symlinked temporary
parent; it now resolves only its disposable synthetic state path before use.

Local Swift 6.2 differs from pinned CI Swift 6.1.2/Xcode 16.4. Local artifacts are
explicitly development-only and activation rejects them. Exact-head CI must build
the strict artifact. Full independent Release Auditor and applicable specialist
QA remain required. The deprecated Security ACL API compiles, but live signed
reader/OS behavior has not been qualified by this task.

The threat model trusts reviewed artifact hashes, CI build inputs, the OS account,
and the private provisioning receipt. It does not isolate credentials from code
already controlling the same OS user: FIFO export is not caller authentication.
That pre-existing limitation remains explicit and requires a release threat-model
decision or a separately reviewed authenticated broker. No self-approval, shipping
approval, deployment, or live specialist evidence is claimed.

Integration must apply this branch after the exact combined base. Native UI,
credentials, endpoint config, mini runtime, provisioning and build workflow changes
must move together; partial cherry-picks would break their contracts. Rebase or
reconciliation creates a new candidate requiring fresh mechanical and independent
evidence. Retain the original PR and its Worktree untouched.

## Follow-up repair from 40d027d

The six follow-up findings are addressed together: credential-receiving code is
packaged in the pinned artifact and selected from its authenticated in-memory
bytes before credential export; initialization can tighten an owned 0755 `.moe`
directory and atomically replace a legacy helper/receipt pair while retaining the
old pair; HTTP-200 readiness shape failures become `ModelLoadError`; disabled
staging requires kernel-confirmed silence on ports 8765/8766; reranking bounds
`top_n` by the document count and 1000; readiness circuits use the complete frozen
target and requested model. The shared kernel inventory also prevents another
user's port-8000 listener from being hidden by process inspection permissions.

Helper verification and credential use share the stable upgrade lock with binding
receipt writes. Invalid local authentication is rejected before helper replacement.
Only regular, owned, non-writable-by-others prior provisioning files are migrated;
unknown linked or nested state is refused without deleting it. Old helper binaries
are not executed. macOS atomic directory exchange is exercised on synthetic files.
An existing Keychain ACL may still reject a newly built reader: this change neither
migrates nor qualifies live ACL state. Tests use subprocess byte results, synthetic
secrets, files, HTTP transports, socket inventories, and injected failures.

## Acceptance-transaction follow-up from 42d54ad

Helper publication now remains provisional through native initialization, signature
and ACL/status acceptance. Failures atomically restore the exact prior directory
(or remove the first-install active path into its recovery slot), verify the saved
inventory, and retain both states. Directory-synced journaling blocks credential
operations after interruption, restore failure, or potentially partial Keychain
writes. No automatic Keychain deletion or ACL migration is attempted.

Applied rollback requires clean, explicitly pinned recovery-code identity and
loads its remote receiver from immutable Git objects. Generic readiness uses bounded
streaming before parsing, rejects compression, and enforces per-read deadlines.
README documents migration, recovery markers, rollback pins, and response limits.

A new scoped native ACL fixture and driver compile/sign without invoking Keychain
by default. The compile-only result is UNAVAILABLE_NOT_EXECUTED
for qualification. A separately isolated disposable macOS environment is required
to execute its synthetic temporary-Keychain replacement/recovery/lock cases. Neither
a compile pass nor synthetic policy-denial evidence qualifies production/login
Keychain ACL behavior. Typed fixture outcomes reject inconclusive denials.


## Mandatory disposable-runner ACL qualification from a2e4842

The strict GitHub-hosted macOS workflow now runs the scoped fixture with
--ephemeral-macos and the exact expected SHA as a mandatory step. Local behavior
remains compile-only; no shared-host Keychain execution is permitted. Metadata-only
snapshots of default/search-list identities/status and login-Keychain file metadata
must remain equal before creation, after creation, before cleanup and after cleanup.
Explicit synthetic-store cleanup runs on failure as well as success. A machine-readable
report requires every allow/deny/replacement/restore/recovery/lock assertion, complete
snapshots, unchanged ambient state, deletion and exact clean source identity. Any
unavailable/incomplete result fails CI. The report alone is uploaded; dummy values
and temporary Keychain files are never artifacts. Qualification remains limited to
ad-hoc identities and synthetic stores, not production credentials or deployment.

### Disposable ACL gate path verification follow-up

The first mandatory runtime candidate (3dfc709) blocked at temporary-store identity validation; explicit deletion was not proven. Available ambient snapshots were equal and temporary files were removed. Store validation now permits only canonical aliases of the exact expected private file, with matching device/inode and retained regular-file, owner and single-link constraints. Fixed path-retrieval/path-mismatch/file-mismatch diagnostics remain blocking and cannot satisfy denial cases. Runtime qualification is pending a fresh disposable CI run; local validation remains compile-only.



The schema-version-2 qualifier follows the shipped fail-closed policy. It executes the production `prepare_helper` directory/receipt transaction with a signed synthetic-binary build adapter and a private synthetic home. The denied replacement must restore the exact original directory, binary and receipt; the original reader must still work. The durable `helper_restored_keychain_unverified` marker must remain and the production status/export/init gate must refuse before any helper execution. ACL migration is unsupported; no ACL-edit API or synthetic migration case exists. All seven cases, exact snapshots, cleanup, and ambient-state equality are mandatory. CI runs this gate before the longer source build; later strict build/artifact checks remain required.

The0d7e30b disposable run passed all six recovery assertions, cleanup and ambient equality, but ACL inspection became unavailable after locking. The final locked-store case now explicitly confirms the scoped store is locked and requires OS authentication/interaction denial from a data query scoped to that store. A successful read, an unlocked store, or any other unavailable OSStatus blocks qualification. Normal reader cases retain production ACL inspection before reading.

## Exact-source provenance and credential quarantine audit repair

Applied primary initialization and direct helper publication now require a reviewed full SHA and clean exact checkout. Immutable Git-object Swift/toolchain bytes are staged privately, compiled, rechecked and recorded in a v3 receipt with commit/input/binary identities. Legacy receipts cannot authorize execution. Regression CI explicitly checks out and verifies the PR head.

Recovery now gates native app loads and running Python credential transports. A durable generation rotates under the exclusive provisioning lock; shared request/stream leases drain before marker publication. Markers/unsafe state/generation changes latch cached clients closed, clear bridge/header credentials, cancel active work and return unavailable readiness. The app monitor stops stale owned backends; marker removal requires fresh process/native reload. Automatic ACL migration remains unsupported. Synthetic adversarial tests cover source/payload races, marker types/read races, cached sends/streams, queued batches, lock exclusion, cancellation, recovery and redaction. Local native qualification remains compile-only; disposable CI runtime must be regenerated for the new SHA.
