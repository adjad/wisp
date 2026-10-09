# S2a: inert bounded synchronous metadata store

This slice adds an unused store and deterministic synthetic transition tests at
base `cbf6bd7a5c0c879e473956c2f365c99043f19ac0`. It depends only on the S1
`attestation_lease.py` records and predicates. No production caller imports the
store. Optimization remains **OFF**. The original 45 cases and N1 remain
**NOT_RUN**; D1–D4 remain unaccepted. There is no performance claim.

The caller supplies qualified immutable facts, absolute integer timestamps and
an explicitly named clock domain. The store does not establish their truth,
select a freshness window, read a clock, perform native inspection, start work,
cancel work, wait for work, or authorize dispatch or output. Each new store must
receive a fresh process/store lifetime ID, including after restart or fork.
Transport IDs must identify lifetimes rather than reusable addresses or names.

## Retention and serialization

`StoreLimits` requires explicit positive integer key, transport, nonce, counter,
record-unit and nesting caps. There are no production defaults. Closed transport
IDs remain tombstones, and keys retain generation/replacement history. Capacity
exhaustion refuses admission without eviction. Nonces increase monotonically
within the injected store lifetime and never wrap. Generation or replacement
counter exhaustion disables the store permanently.

The hard bookkeeping bounds are two outstanding actual jobs and one outstanding
refresh job globally. A refresh is inferred from a currently usable published
lease; the caller cannot label it foreground. No queue or worker is created.

Caller inputs are checked before the internal builtin lock. Only exact immutable
scalars, tuples and S1 records are accepted. The key and the remaining token or
lease fields each have a `record_units` budget, so each retained combined record
has at most twice that budget. Strings count four units per character plus one;
bytes and integers count their payload size plus one; tuples count one plus
their elements. Nesting has a separate cap. These are finite conservative
content bounds, not an exact allocator-memory measurement. Counts, IDs and clock
samples are bounded too. The short lock covers only these bounded metadata
operations and pure S1 predicates; no callbacks, awaits or qualification run
under it. Snapshots are immutable values.

## Authority and actual-job accounting

`reserve` inserts actual-job accounting before returning a new store-issued
token. Joining a current unexpired token returns `started=False`, retains its
original deadline and initiator, and starts no additional job. Both initiating
and joining transports must be registered and open.

`publish` compares the complete current token, key, process lifetime, policy,
global/key generations, expected replacement revision and supplied clock. It
uses the original qualification-start interval, including strict expiry at
equality. Same-incarnation replacement increments replacement revision without
revoking older streams of that generation. Changed facts reject publication and
revoke the key, requiring a fresh generation before acceptance. Current qualified
failure, cancellation and deadline expiration retire authority. Stale results,
failures, cancellations and timeouts do not observe time or modify a successor.

`job_finished` is the separate exact-token, idempotent actual-job settlement
operation. Call it only once the real job has ended, normally after reporting its
result. Timeout, close, cancellation and permanent disable never free running
capacity. A stale finished job releases only its own accounting. Finishing a
current job without a result conservatively revokes its unfinished authority.

`close_transport` synchronously tombstones the ID and detaches its current
builds; it does not wait. An independently published cache value survives a
close with no current build. Closing a current refresh conservatively revokes
that key. Closing a joiner does not transfer or revoke the initiator's token.

`revoke_lease` compares the stream's captured global/key generation and lifetime.
An older generation cannot revoke a successor. An older replacement within the
same generation can still revoke that generation, even after its lease expires.
Rollback, quarantine and manifest transition disable one way with no enable
operation. Store-observed clock regression also disables permanently. A sample
in another clock domain is refused.

## Validation boundary

`tests/test_attestation_store.py` contains 32 synthetic test methods with manual
interleavings. The tests load only the two leaf modules under a synthetic package,
without running `service` initialization. Coverage includes strict expiry,
changed facts, full key/token comparison, stale outcomes, captured revocation,
joins, hung-job capacity, refresh accounting, close tombstones, finite retention,
exhaustion, one-way disable and immutable bounded inputs.

At this preparation checkpoint these tests are **NOT_RUN**. The private frozen
execution packet must receive Root admission before a target import or test run.
Manual interleavings do not prove thread scheduling or linearizability. Future
integration still needs qualified lifecycle/clock adapters, real dispatch gates,
bounded worker cancellation/settlement, concurrency review and any triggered
specialist QA. None is implemented or accepted by this slice.

The simulation manifest received only the recorded serial two-line addition
described in `manifest-dependency.md`. That write window is now closed.
