# Mini preparation and arrival qualification

This candidate prepares a runtime. It does not qualify hardware, load weights,
activate a service, fetch provider data, or authorize effects. The shipped
`resource-contract.json` has no qualified models and intentionally refuses
inference. Jobs and all four connector adapters are disabled. Gateway health is
**liveness only**, not a model/resource qualification signal.

## Version 1 resource/model contract

All sizes are decimal GB (1,000,000,000 bytes). The contract requires exactly:
60 GB total measured memory guard, 2 GB hot cache, 20 GB paged SSD KV cache,
150 GB free at startup, a permanent 50 GB free reserve, one active gateway
request, expert offload disabled, and quantization of at least four bits.
Unknown/missing fields, booleans in integer fields, overflow, mutable revision
labels, invalid qualification order, unavailable/stale telemetry and capacity
excess all refuse explicitly. No eviction, silent deletion or overcommit path
exists. Store growth and backup/restore have reserve checks; write failure rolls back
SQLite transactions. Every owned write and qualified inference request holds
the same uid/device-scoped volume lease, preventing their projected growth from
spending the same headroom concurrently. Busy lease acquisition refuses; store
transactions allow a bounded 250 ms wait. Read-only polling stays available.

`python -m mini` defaults to the gateway. Without a resource configuration it
can serve authenticated liveness/model metadata; every inference POST returns
503 `resource_unavailable` before reaching oMLX. For qualified bringup, supply
both `--resource-contract ABSOLUTE_PRIVATE_JSON` and
`--resource-telemetry ABSOLUTE_PRIVATE_JSON`. Startup runs the 150 GB preflight.
The supervisor must keep the telemetry parent private (0700) and files 0600,
regular, owned, single-link and non-symlink. Publish telemetry by atomic file
replacement, not in-place edits. Contract changes require a fresh process.

A model entry has exactly these fields:

```json
{
  "model_id": "EXACT_MODEL_ID",
  "revision": "EXACT_40_OR_64_LOWERCASE_HEX_REVISION",
  "tokenizer_revision": "EXACT_40_OR_64_LOWERCASE_HEX_REVISION",
  "runtime_revision": "EXACT_40_OR_64_LOWERCASE_HEX_REVISION",
  "profile_sha256": "EXACT_64_LOWERCASE_HEX_PROFILE_DIGEST",
  "quantization_bits": 4,
  "context_tokens": 8192,
  "qualified_contexts": [8192],
  "request_memory_bytes": 1000000000,
  "request_paged_bytes": 1000000000
}
```

The one-GB request estimates above are illustrative and are **not** measured
qualification values. Record conservative maximum incremental memory/paged
allocation for each exact deployment. Qualification must verify the gateway's
UTF-8-byte plus template overhead input-token upper bound for its tokenizer.
The gateway counts serialized request bytes plus 256 tokens plus `max_tokens`,
default 1024, against the qualified context. This deliberately admits less than
the nominal tokenizer window; no unsupported tokenization guesses are made.

Begin with 8192 tokens. Only after successful exact-model 8k qualification may
an operator qualify 16384 and record `[8192,16384]` with `context_tokens=16384`.
A direct `[16384]`, reversed, duplicated or larger window refuses. Records are
trusted operator qualification declarations, not proof of benchmarks performed
by this package; changing any model/revision/profile requires new qualification.
Never copy declarations from a different deployment.

Supervisor telemetry contains exactly:

```json
{
  "schema_version": 1,
  "contract_sha256": "SHA256_OF_CANONICAL_SORTED_COMPACT_CONTRACT_JSON",
  "sampled_at_ns": 0,
  "memory_used_bytes": 0,
  "hot_cache_used_bytes": 0,
  "paged_kv_used_bytes": 0,
  "engine_limits_enforced": true
}
```

The zero measurements/timestamp are placeholders, never usable evidence. Use
same-host monotonic nanoseconds and publish at least once per second; samples
older than two seconds or from the future refuse. The supervisor must verify
exact resident model/tokenizer/runtime/profile revisions and enforce the engine
limits before attesting to the contract digest. The runtime checks real free
space on the telemetry filesystem, which **must be the same volume as model,
cache and state storage**. Model/cache-volume mapping remains an arrival gate.
This package does not include an oMLX settings/telemetry adapter and cannot
attest an unknown engine. Missing that integration keeps inference unavailable.

Admission reserves model-specific incremental capacity before forwarding; a
100 ms watchdog rechecks telemetry during stalled requests and streams. The
shared `inference.lock` prevents cooperating gateway processes using this
telemetry directory from overlapping inference. The in-process request latch
remains held until upstream cleanup really finishes, including cancellation-
resistant transports. This is admission plus fail-closed cancellation, not an OS
memory limiter: the attesting supervisor's engine caps remain mandatory. It
must own the sole backend endpoint, prevent independent bypass callers, and
enforce an independently qualified storage quota/reservation against unrelated
processes. Software leases coordinate these owned components; they cannot
reserve disk against noncooperating operating-system processes. The fixed
private lease directory under the resolved system `/tmp` is keyed by uid and
filesystem device; never unlink live lock files or change lock identity.

## Disabled scheduler and portable connectors

Node constructs a disabled `Runtime`; it never starts its optional loop. There
is no HTTP enable/write/run route, environment enable flag, dynamic tool
registry, native import, or provider credential field. `Runtime.tick` is the
owned preparation/qualification seam. Explicit enablement names each job and
requires its corresponding `SnapshotAdapter` to be separately enabled and
qualified for exact `mini-portable-v1`, scope `portable-snapshot-only`.

Canvas, study, stocks and research adapters accept only supplied `{items:
[{title,text}]}` snapshots, then emit presentation text. They do not collect
snapshots or invoke providers. Their provider-fetch method always refuses.
Even individually enabled portable adapters cannot access Mail, Messages,
Calendar, TCC, filesystem, shell, native effects, private providers or network.
The complete portable-tool allowlist is `text.outline`; it is pure text
normalization. URLs, commands and apparent instructions in text remain data.
Future provider fetching requires its own qualified, reviewed implementation;
portable qualification does not enable it.

Schedules use integer UTC seconds, an immutable first due time and interval.
Catch-up stages **all** missing due occurrences through `now`, without skipping
or coalescing. An outage larger than remaining occurrence capacity refuses the
whole new batch. Previously staged pending work completes before staging new
catch-up work. Clock rollback creates no new occurrences and does not remove
anything. Each occurrence's original input snapshot and adapter revision are
persisted atomically with staging; later snapshots cannot replace them.
Computation may repeat after a crash, but immutable completion/publication is
one transaction with deterministic node/job/due/result IDs. Conflicting payloads
refuse. No exactly-once provider or effect execution is claimed.

Schema v2 adds immutable `runtime_inputs`. Startup migrates only the exact v1
owned schema in one transaction, preserving instance/key/results/cursors.
Unknown schema drift refuses. Legacy manually staged occurrences without
runtime snapshots stay operator-owned and are not synthesized or fetched.
All database writes retain the permanent reserve and refuse capacity failures.

## Back up and restore

Use a private backup parent on a volume with at least 150 GB free. These tools
have no HTTP surface and require explicit operator invocation:

```text
python -m mini.backup backup --state-dir ABSOLUTE_EXISTING_STATE --node-id STABLE_ID --snapshot ABSOLUTE_NEW_BACKUP
python -m mini.backup restore --state-dir ABSOLUTE_NEW_STATE --node-id STABLE_ID --snapshot ABSOLUTE_BACKUP --identity rotate
```

Backup uses SQLite's online backup API and a pinned read transaction, including
committed WAL content. A private lock serializes snapshot publication in each
parent and the shared volume lease excludes other owned growth; growth is checked before and during copy. A fully validated, fsynced
snapshot is published with an atomic no-replace rename. Existing destinations
always refuse; no active database is copied as a raw main file.

Restore accepts standalone backups only. It checks schema version, exact DDL,
integrity, relationships, capacities, canonical result/snapshot contents,
immutable IDs, occurrence state and cursor identity. It validates and copies
through SQLite to a private staging directory, then atomically publishes a
**new** state directory. It never overwrites, deletes or swaps an existing store,
even an empty destination. macOS `renamex_np(RENAME_EXCL)` and Linux
`renameat2(RENAME_NOREPLACE)` are supported; unavailable atomic publication
refuses without a replacement fallback.

A crash before publication leaves only a private `.backup-*` or `.restore-*`
staging artifact. A crash after publication leaves a complete, single-link
snapshot or full state directory. Retry with the same destination: if it exists,
validate and use the published artifact; do not overwrite it. If no destination
exists, retry with a fresh staging operation. Orphan staging artifacts are not
used or automatically deleted by later invocations; an operator may inspect
and remove those exact artifacts once no operation is active. Power-loss
persistence is guaranteed only after the parent-directory fsync completes; a
crash before that may require retry, never treating a missing result as success.

Default `rotate` preserves node ID and immutable result IDs but creates a new
instance and cursor key, invalidating all old cursors. The consumer must reset
its cursor and replay; the primary consumer now does so automatically only on
the exact authenticated 400 `invalid_cursor_or_query` response, once per poll.
Other errors do not reset progress. Existing result IDs deduplicate on the Pro.
`--identity preserve` retains instance/key and accepts only cursors actually
anchored in the snapshot. Use it only for coordinated producer/consumer resume
where the consumer is not ahead. A full producer rollback cannot be detected
without an external monotonic anchor. Never run old and restored producers
concurrently under the same node ID. Stop the producer and coordinate the
consumer cursor before changing the supervisor's state directory; no switch,
consumer reset, service start or destructive cleanup is performed by these tools.

## Arrival gates (all still outstanding)

1. Verify actual hardware/storage volume, 150 GB free and reserve behavior under
   pressure; exact oMLX revisions, four-bit-or-better quantization, offload off,
   60/2/20 GB enforced caps and fresh supervisor telemetry.
2. Qualify 8k first, then 16k separately for memory, tokenizer bound, tool quality,
   throughput, request cancellation, cache growth and SSD reserve; keep failed
   or inconclusive profiles unavailable.
3. Use synthetic state to qualify restart, sleep/wake, abrupt process death,
   backup/restore and coordinated cursor replay on the actual mini filesystem.
4. Complete independent release audit, applicable persistence/privacy QA,
   central gate registration, exact-head CI and reproducible artifact builds.
5. Qualify each portable adapter individually using synthetic snapshots. Provider
   integration and any primary-host proposal execution are separate review
   gates. Keep jobs/provider connectors disabled until that work is approved.
# Provider-neutral acquisition rehearsal

`mini.acquisition` defines the disabled acquisition boundary for Canvas, study,
stocks and research. It accepts no raw credentials or arbitrary transport. The
reviewed contract declares connector identity and immutable version, exact
`.invalid` synthetic origin, read-only credential role, synthetic classification,
no private-data transmission, the two fixed capabilities, and bounded request
rate/deadline/request/result sizes. Production constructors remain disabled.

The injected `AcquisitionAdapter` protocol has a fail-closed `DisabledAdapter`.
Only the owned `FixtureAdapter` may be selected by an explicit `simulate=True`
constructor with an independently pinned qualification receipt. A receipt or
origin declaration never supplies network, filesystem, shell, native/TCC,
credential-store or effect capability. Live provider/OAuth/tenant/credential
selection and transport implementation require separate authorization and review.

`acquire_tick` feeds each synthetic result through its snapshot processor, then
stages the complete batch of immutable snapshots, connector/request/snapshot
digests and deterministic occurrences in one transaction. Failure before staging
persists none of the new batch. Same-batch conflicting replay refuses; later
catch-up preserves already committed snapshots. Pending work resumes from durable
inputs before another acquisition. Backup/restore validates acquisition receipts.

Error messages are fixed codes. Raw credential forms, resource URLs, redirects,
executable representations, undeclared capabilities and private classification
refuse. A timeout/cancellation retains admission until the underlying fixture
actually exits; attempts are rate limited. No access or provider error log exists.
Every production connector and provider job stays disabled; the synthetic tests
demonstrate preparation, not live acquisition or private-data transmission.

## Remote administration and incident recovery

Read [MacBook/mini architecture](MACBOOK_MINI_ARCHITECTURE.md) before relying on
unattended operation. Management uses exact-host Tailscale SSH, independently
of the gateway, node and oMLX HTTP services. The host, network, approved SSH
account and usable user session must still be available. Live adapter, signing,
Keychain, reboot and external-network recovery validation remain arrival gates.

From the clean reviewed source checkout, `node_prep.py manage --plan PLAN
--management-request REQUEST --live --source-sha EXACT_SHA` sends a bounded
request to the verified host. Mutations additionally require `--apply`.
The request has exactly `operation` and `arguments`; the trusted plan supplies
host/node/IP identity. Routine diagnostics contain no credentials or database
contents. Do not enable shell environment or raw process-argument dumps.

| Operation | Arguments and meaning |
| --- | --- |
| `diagnose`, `releases`, `credential-status`, `database-doctor` | Empty arguments. Diagnose remains independent of services; database doctor requires an intact installed runtime and release inventory. |
| `restart` | `service` is `gateway` or `node`; `operation_id` is a fresh 64-character lowercase hex nonce. Only an existing, exactly owned loaded job can restart. |
| `rollback` | `operation_id`. Stops only owned Wisp jobs; preserves state, keys and releases. This does not switch oMLX versions. |
| `recover-state` | `operation_id`, a new `destination` named `recovered-...`, doctor `inventory_sha256`, and boolean `fresh_empty`. Requires stopped jobs and a new destination; does not activate it. |
| `recover-ledger` | Independently reviewed `authorization` plus its `authorization_sha256`; see below. |
| `reclaim` | Exact eligible release `ids` and approved `inventory_sha256`. Retains current and at least the newest two releases, requires age of at least one day and no active jobs/process use. |
| `omlx-status`, `omlx-select` | Status uses empty arguments; select accepts a complete official `catalog`. Neither requires oMLX to answer. |
| `omlx-stage`, `omlx-update`, `omlx-rollback` | Explicitly blocked with `live_update_adapter_unavailable`; no live updater is enabled. |

Recovery, restart, install and cleanup serialize on the same publication lock.
Restart/recovery writes durable operation intent before effects. Repeating a
completed operation ID returns its recorded result without repeating effects;
interrupted intent reports recovery-required. Inspect actual state before a new
operation. Completion records describe that operation, not current liveness.

For a zero-byte main database without any journal, explicit `fresh_empty` creates
new state. With WAL, recovery uses only pinned, private source descriptors and
preserves original bytes. It accepts a complete checksum-valid committed page
set under the [SQLite WAL format](https://www.sqlite.org/fileformat.html#wal_file_format),
then validates the reconstructed database through SQLite. Corruption, missing
pages, uncommitted trailing frames, changed sources or ambiguity refuse. Recovery
publishes only a new directory. Switching the stopped producer to that directory
requires the live supervisor; that adapter is not supplied here.

Ledger authorization contains schema version 1, operation `recover-ledger`, exact
absolute root and UID, doctor `sentinel_sha256` and `recovery_history_sha256`, a
fresh 64-character hex `nonce`, independently reviewed positive `sequence_floor`,
`statement_sha256`, and `independent_high_water_review: true`. The sequence floor
must account for every previously consumed release, not just the last receipt.
A complete authorization-consumption record is atomically published and fsynced
before repairing the missing ledger. A later incident cannot reuse it. If an
intent was consumed before interruption, obtain a fresh approval bound to the
new history. Never delete the sentinel or authorization history to reset replay
protection. Orphan `.pending-ledger-intent-*` files are not approval records and
are not automatically reclaimed.

Reclamation records the approved complete tree identities and hashes before an
exclusive rename to `.reclaim-*`. The same pinned request can resume after an
interruption; only missing approved entries are tolerated, never replacements
or additions. Current/previous releases, state links and release ledgers remain
protected. Unjournaled staging artifacts remain inspection-only. Unknown release
ownership or insufficient metadata-write space refuses cleanup.

Credential status is not credential recovery. Stable Developer ID adoption and
login-Keychain/SSH behavior still need real-host evidence. Primary credential
transaction recovery follows the provisioning guide's authoritative
`recover-credentials` procedure; no remote command silently widens ACLs,
replaces credentials, or adopts a new helper. A mini credential incident without
an independently authorized known helper/binding remains blocked until that
integration is supplied. This limitation must be resolved before unattended use.

## RC-first oMLX lifecycle

The prepared updater selects the newest official RC by release publication order,
using latest stable only if no RC exists in the complete official catalog.
Development/alpha/beta versions are not silently treated as RCs. A selected RC
that fails origin/authenticity/download integrity or staging safeguards stops the
update; it does not silently fall back to stable. The official source is
[the oMLX release repository](https://github.com/jundot/omlx/releases).

The user accepts RC incompatibility and performance risk. This lifecycle does
not run preactivation canaries, synthetic prompts, model/API compatibility,
performance or qualification tests. Operational controls cover artifact
integrity, immutable installation and complete tree receipts, disk admission,
active/previous retention, atomic supervisor generation switching, and bounded
process startup/binding observation. Automatic rollback is permitted only for
failure to start, stay running, or bind to the supervisor. Inconclusive observation
requires recovery; it is not permission for other automatic rollback criteria.
Successful startup means process running, application behavior unverified.

The isolated adapter exercises this operational protocol only. Its receipts
cannot enable live effects. Actual artifact-authenticity verification,
materialization, atomic supervisor/runtime-authorization switching, and remote
rollback require the unavailable live adapter. All update machinery is disabled
for deployment now; existing resource protections remain required.
Staged authenticated receipts and consumed operation records are durable; pending
records retain the prior/target releases and both generations for inspection
after interruption. A returned result past its deadline refuses. Deadlines are
cooperative in this prepared protocol: a hung adapter cannot be interrupted by
the controller. The live adapter must provide bounded execution and recovery.
