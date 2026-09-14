# Wisp mini runtime

Standalone Python 3.13/3.14 package based on PR #42 head
`c23e9c9e8860222a8aa4b070364a7e17236b3ec8`. No Wisp.app, backend, tool
registry, native bridges, provider transports, shell execution, or effect dispatcher is
imported. All node jobs remain disabled. No HTTP write endpoint exists.

## Provisioning contract

The provisioning owner installs the isolated dependencies in `mini/requirements.txt`
and runs from the bundle root. `python -m mini gateway` binds only
`127.0.0.1:8765`; its sole upstream is `http://127.0.0.1:8000`.
`python -m mini node --state-dir ABSOLUTE_PRIVATE_PATH --node-id STABLE_ID`
binds only `127.0.0.1:8766`. There is no host/port override. Intended Tailscale
Serve mappings are HTTPS 443 → gateway and HTTPS 8443 → node. The runtime does
not configure Serve, Funnel, LAN listeners, launchd, Keychain, or oMLX itself.

The supervisor supplies `WISP_MINI_INFERENCE_KEY` + `WISP_LOCAL_OMLX_KEY` only to
the gateway and `WISP_MINI_NODE_KEY` only to the node. They are opaque distinct
32–512 printable ASCII characters; the provisioning contract uses 64 lowercase
hex characters. `WISP_LOCAL_OMLX_KEY` must be independently provisioned on the
mini, never copied from the Pro. Native startup sends canonical 64-character
hex credentials through a one-use private inherited pipe bound to the child's
PID, UID, role, descriptor identity, and generation. Secret environment values
are refused; no launchd plist or bundle contains them. Missing or
invalid keys fail before a listener starts. The supervisor must enforce separate
gateway/node credentials since separate processes cannot compare their keys.

Every route, including health, requires its service's bearer credential.
Access/HTTP exception logs and proxy-header trust are disabled. Errors contain
fixed codes only. Operators see counts/availability, never result contents,
tokens, database paths, job identifiers or exception messages in status.

The exact gateway allowlist is `GET /health`, `GET /v1/models`,
`POST /v1/chat/completions`, `POST /v1/embeddings`, `POST /v1/rerank`.
The first three serve `service/inference/omlx_client.py`; the last two match
`service/search/embedder.py::_embed` and `service/router/reranker.py::_rank_one`
at the base SHA. Remote model status, load/unload, settings, admin, arbitrary
paths, URL aliases and query strings are rejected. Chat accepts text and tool
messages; multimodal parts/URL fetching are unsupported. Tools in chat are
generation data only. The gateway rebuilds upstream headers and never forwards
caller auth/identity/forwarding/cookie headers or upstream cookies/redirects.

Default gateway bounds: exactly one active request, no queue, 120 seconds total
including upload/upstream/stream/downstream writes, 2,000,000 body bytes,
16,384 header bytes, 16,000,000 response bytes. Busy returns 429, oversized
requests 413, deadlines 504 before headers; failures after SSE starts emit a
fixed SSE error without a success terminal. Successful streams preserve bytes
of data lines (comments are dropped) and downstream cancellation closes upstream.
Cleanup has a separate one-second grace with a second cancellation on expiry.
Partial or oversized SSE lines and upstream diagnostic error frames fail closed.
Nonstream/health/model errors
are sanitized; health and model-list success strip upstream metadata.

## Durable node protocol

Routes: `GET /healthz`, `GET /v1/status`, and
`GET /v1/results?cursor=OPAQUE&limit=1..100`. Defaults are empty cursor and
limit 100. The v1 result/page schemas match PR #42 exactly. Supported kinds:
`canvas.sync`, `study.generate`, `stocks.watch`, `research.run`, `effect.proposal`.
An effect proposal can describe `email.send`, `message.send` or `calendar.write`;
it remains untrusted stored/presented data. It has no approval or execution path.

State resides in a private 0700 directory, with a 0600 SQLite database, WAL,
`synchronous=FULL`, foreign keys, bounded busy waits and explicit transactions.
Node identity, instance UUID and a random cursor HMAC key are persisted in the
database. Corrupt, incompatible or wrong-identity state fails closed; runtime
state disappearance cannot silently recreate an empty store. Back up using
`python -m mini.backup` using SQLite's online backup API; never copy an active DB without WAL.

Immutable schedules store UTC anchor/interval, and explicitly staged occurrences
derive stable IDs from node/job/due time. Pending occurrences survive restart.
The running node registers five **disabled** schedules and never starts a worker.
The owned disabled scheduler supports transactional catch-up and restart recovery
with immutable input snapshots. Pure Canvas/study/stocks/research snapshot
adapters require individual portable qualification and enablement; no provider
transport exists. See [RUNBOOK.md](RUNBOOK.md) for the exact preparation contract.

Completion atomically inserts an immutable result and marks its occurrence
complete. Result IDs derive from node/job/occurrence. Identical retries succeed;
changed payloads, reused occurrence/result/effect IDs and job mutations fail.
Default limits are 10,000 results, 100MB canonical payloads, 20,000 occurrences
and 100 jobs. Capacity refuses new writes without eviction or partial completion.
There is deliberately no deletion/reset/maintenance API. Capacity refuses without deletion. Tested online backup and atomic new-directory
restore tools are described in [RUNBOOK.md](RUNBOOK.md).

Pages are count- and byte-bounded below Pro's 2MB limit. HMAC cursors bind the
database instance, sequence and immutable anchor digest. Empty pages preserve
the cursor. Tampered, foreign, ahead-of-database or anchor-mismatched cursors
fail. Replaying an earlier valid cursor intentionally replays immutable results
for delivery retries; the Pro deduplicates them. A complete database/key rollback
cannot always be detected without an external monotonic anchor. Do not roll back
producer state independently of consumers or reuse a node ID for a fresh database.

## Bundle and validation

`python -m mini.build_bundle --output NEW_ARCHIVE_PATH` creates a deterministic
tar.gz containing only explicit source/dependency/documentation files and
`mini/bundle.json`. The source-only manifest has version/kind/exact source commit, Python range, requirements,
services and `files: [{path, sha256, mode}]`. The source manifest's empty files array is
a template; the generated manifest lists every archived file except itself.
Archive members are regular files under `mini/`, never symlinks, secrets or state.
SHA-256 entries provide integrity, not publisher authentication; provisioning
must separately pin the reviewed archive digest and candidate Git SHA.

Run `python -m pytest -q tests/test_mini_*.py` and the repository regression
gate using isolated synthetic state. Hardware qualification (real oMLX behavior,
Serve exposure, Keychain, restart/sleep-wake, capacity, packaging and latency)
is a later release gate. SQLite lock contention has a 250ms timeout; an operating
system disk stall cannot be interrupted by the node's asyncio deadline.
This package does not deploy, install, enable jobs,
qualify models, or authorize effects.


Installable artifacts are produced separately by `build-support/mini_artifact.py`
from a clean exact candidate. They contain the pinned standalone Python, complete
hash-locked wheels, installed dependencies, and verified native helpers. Source-only
archives are explicitly non-installable. The build checks independent artifact
reproducibility and relocated synthetic health; development artifacts are labeled
and rejected by activation. Console scripts are omitted: use `python3 -m mini`.

## Preparation completion

The default entrypoint is the one-request inference gateway. Unconfigured
inference refuses until an exact model/resource contract and fresh telemetry
are supplied; authenticated health is liveness only. Read [RUNBOOK.md](RUNBOOK.md)
for the 60/2/20 GB limits, 150 GB preflight, 50 GB reserve, 8k-to-16k progression,
disabled scheduler/adapters, schema v2 migration and backup/restore procedure.
