# Primary Mac and future mini provisioning

This candidate prepares a primary Mac and stages a future mini runtime. It never
switches inference roles or enables proactive work. Validation for this change is
synthetic: no live Keychain, Tailnet, firewall, oMLX, system setting, installation,
or deployment operation was performed.

## Commands and effect boundaries

Run `scripts/wisp-node-prep --help` with Python 3.13 or 3.14. The default mode
requires synthetic fixture input for posture checks. `--live` permits system
reads; `--live --apply` is required for the three mutation commands. Never combine
fixtures with live/apply flags. Reports contain fixed checks and statuses only;
raw command output, Serve configuration and exception details are suppressed.

| Command | Default | Explicit live behavior |
| --- | --- | --- |
| `init-primary` | Lists the three planned accounts; no reads or writes | `--live` checks the installed helper; `--live --apply` installs a stable native helper and initializes Keychain entries |
| `policy-render` | Renders an additive policy, warns/returns failure for unresolved access | Does not publish or replace Tailnet policy |
| `preflight` | Evaluates the fixture and complete policy | Reads primary/peer identity, Keychain presence, effective config, firewall, oMLX listeners, remote posture through Tailscale SSH |
| `activate` | Validates the fixture plus pinned versioned bundle | With `--apply`, streams only mini credentials and bundle to stage an inert runtime and disabled launchd templates |
| `doctor` | Same invariant checks as preflight | Reports current posture; does not repair or enable anything |
| `rollback` | Verifies target identity | With `--apply`, unloads only recorded Wisp jobs, restores known primary local bindings and disables proactive polling; preserves keys, state, releases, firewall and oMLX |

The JSON examples are fictional. Replace the target with the mini's exact
MagicDNS FQDN, stable node ID, Tailscale IP, a designated existing non-root macOS
account, and its authorized Tailnet user identity. The primary IP is fixed at
`100.94.211.115`. Never use `root`, an SSH wildcard, or `autogroup:nonroot`.

Safe checks from a checkout:

```sh
scripts/wisp-node-prep init-primary
scripts/wisp-node-prep preflight --plan infra/mac-mini/plan.example.json --policy infra/mac-mini/policy.example.json --fixture infra/mac-mini/preflight.fixture.json
scripts/wisp-node-prep doctor --plan infra/mac-mini/plan.example.json --policy infra/mac-mini/policy.example.json --fixture infra/mac-mini/preflight.fixture.json
scripts/wisp-node-prep rollback --plan infra/mac-mini/plan.example.json --policy infra/mac-mini/policy.example.json --fixture infra/mac-mini/preflight.fixture.json
```

For fixture activation add `--bundle /path/to/reviewed-mini.tar.gz` and
`--bundle-sha256 <reviewed archive SHA-256>` and, for live staging,
`--source-sha <reviewed exact candidate SHA>`. Applied rollback also requires
`--source-sha <reviewed rollback-code SHA>` and a clean checkout at that exact SHA.
Rollback reads its receiver from immutable Git blobs, rechecks the checkout before
SSH, and verifies the remote release ownership record before unloading jobs. The
rollback-code pin may be newer than the recorded release; it authorizes recovery
code, not a different target or ownership record. Obtain that hash from the reviewed
mini release handoff. Live staging additionally requires publisher-authenticated metadata and independently
approved public trust/key/sequence pins, described below. A digest alone cannot authorize export.

## Credentials and local compatibility

The file-based macOS Keychain service is `com.wisp.inference`:

| Account | Native/backend reference | Scope |
| --- | --- | --- |
| `local-omlx` | `WISP_LOCAL_OMLX_KEY` | That Mac's loopback oMLX only |
| `mini-inference` | `WISP_MINI_INFERENCE_KEY` | Mini HTTPS gateway |
| `mini-node` | `WISP_MINI_NODE_KEY` | Mini HTTPS results API |

All values are canonical 64 lowercase hexadecimal characters representing 32
bytes. Remote credentials use Security.framework cryptographic randomness.
Initialization imports the **already configured** local oMLX token only if it
has that format, validates existing entries, and never rotates a key. An absent,
short, differently encoded, denied or mismatched local key blocks initialization
before credential writes. A local authentication migration or rotation uses the separately authorized
`migrate-local` / `rotate-local` transaction described below; initialization remains
a conservative importer. Legacy installations without an injected
local credential retain their existing oMLX settings fallback.

The native launcher reads fixed accounts through Security.framework. It removes
inherited values for the three bridge names and injects only validated entries.
The Python configuration package immediately consumes/removes those names from
`os.environ`, so ordinary shell, skill and MCP children cannot inherit them.
Only fixed credential references resolve from the private store. A remote
endpoint cannot request the local key through either `local_omlx` or the fixed
environment alias. Unrelated legacy `env:` references retain their behavior.

`init-primary --live --apply --source-sha <reviewed full SHA>` uses the installed `/Applications/Wisp.app` as an
explicit trusted reader and places a stable helper at
`~/.moe/provisioning/wisp-keychain-helper`. Keychain ACLs name the helper and app;
there is no trust-all setting. Explicit initialization can tighten an owned,
non-symlink `~/.moe` directory from normal umask-022 mode 0755 to 0700. Other unsafe
permissions, owners, links, or unknown nested provisioning files are refused.

Applied initialization requires a reviewed full 40-character source SHA and a clean
checkout whose HEAD matches it, before settings reads or helper publication. Both
Swift inputs and toolchain configuration are captured from immutable Git objects.
The compiler receives private copies of those captured bytes; the version-3 helper
receipt binds the reviewed commit, exact input hashes, compiler, and binary hash.
Working-tree files are never rehashed as evidence for already-compiled bytes.
Source identity/cleanliness and the staged inputs/candidate are rechecked before
publication and acceptance. Legacy receipts can be restored but cannot authorize
credential execution. Dry-run initialization remains read-only and needs no pin.

Initialization builds and verifies a fresh helper/receipt pair, preserves unrelated
receipts, then atomically exchanges the complete provisioning directory. The old
pair remains in a private `.helper-previous-*` recovery slot. Publication and the
new helper's initialization, signature/ACL checks, and status acceptance share one
lock with binding receipt writes. A secret-free, directory-synced
`~/.moe/.helper-transaction.json` blocks credential use during interrupted recovery.
Before publication, the writer also atomically rotates a durable 64-hex
`.credential-generation` value under the same exclusive lock; it persists after
failure or marker removal.

Native `BackendCredentials.load()` checks the marker and generation around every
read. Backend launch carries the generation captured with those credentials.
Running credentialed HTTP requests hold shared provisioning leases through their
response/stream lifetime, so a cooperating writer waits for them to drain before
publishing a marker. Cached inference, embedding, reranking, node, and synchronous
settings clients check quarantine again at dispatch; streams check before yielding
more data or completion. Quarantine does not fall back to legacy/local credentials.
A marker, changed generation, or unsafe/inconclusive state latches the process
closed, clears bridge credentials/authorization headers, cancels active work, and
makes backend HTTP readiness return 503. The app monitor terminates its stale owned
backend and latches recovery-needed even when Wisp starts with a marker already
present and has never launched a backend. It stays unavailable until authorized
recovery removes the marker and exposes a valid 64-hex generation; absent, malformed,
or previously invalidated process generations cannot authorize recovery. With no
prior process, the post-recovery generation is validated without claiming knowledge
of the epoch hidden by the marker. Once the old owned process has exited, one monitor
tick reserves a fresh launch and revalidates native credentials. Repeated ticks do
not launch duplicates, and an existing healthy endpoint cannot satisfy that recovery
launch. Removing a marker cannot revive an old cached client; a fresh backend
process is required. A brief exclusive read-only provisioning lock refuses a
concurrent dispatch without permanently invalidating unchanged credentials.
An externally introduced marker is handled by per-dispatch/stream checks plus the
watcher; already transmitted requests cannot be undone. Provisioning's lock protocol
prevents that overlap for normal helper transactions.

If acceptance fails, the exact old directory is atomically restored and its modes,
ownership, inode, file set and hashes are checked without executing the old helper.
For a first installation, the rejected new pair moves out of the active path.
Both states are retained. Because initialization may have created some missing
Keychain entries before failing, the recovery marker remains even after successful
file restoration. Do not delete the marker or recovery slot to retry: a separately
authorized recovery must verify the recorded inventories and Keychain/ACL state.
No automatic credential deletion, ACL widening, rotation, or forced migration occurs.
Helper/app replacement, signed identities, locked Keychain behavior and ACL
access across executables require isolated native qualification before shipping.
The file-based ACL APIs produce macOS deprecation warnings; moving to a signed
shared access group is a separate signing/entitlement decision.

The primary's local key is never transported. Remote import accepts exactly the
two mini accounts through a pipe, checks distinctness, and refuses rotation or
reuse of the mini's local key. After staging, the mini's own trusted helper has
an `init-mini-local` operation that imports its existing canonical local oMLX
token into its own Keychain, with the mini launcher as trusted reader. It creates
no new oMLX token. This is a separate live credential step, not part of staging.
Do not run it during Worktree validation. Qualify authenticated local oMLX and
both mini services before considering service enablement.

Secrets never appear in generated YAML, plists, command arguments, reports or
logs. Native helper export is a machine pipe operation; do not invoke it by hand
or redirect it. The mini launcher passes just the gateway's two keys or the node
API's one key in memory and suppresses arbitrary child stdout/stderr. This is not
protection against code already executing as the same OS user, a debugger, or
an administrator. Do not enable environment/local-variable dumps in diagnostics.

## Tailnet prerequisites and additive policy

Install/configure the **open-source CLI `tailscale` + `tailscaled` variant** on
the mini. The macOS App Store and standalone system-extension variants cannot
host Tailscale SSH. This implementation expects the Apple Silicon Homebrew CLI
at `/opt/homebrew/bin/tailscale` and Python at `/opt/homebrew/bin/python3.13`.
It does not install/enroll Tailscale, create users, enable SSH, or approve devices.

An administrator must enable device approval, approve the exact mini, assign
`tag:wisp-inference`, enable MagicDNS and HTTPS, and configure Tailscale SSH.
Keep the primary user-owned so SSH check-mode reauthentication can work. HTTPS
certificate issuance exposes the device FQDN in public certificate-transparency
logs; choose a non-sensitive machine name. These control-plane prerequisites
must be reviewed in the admin console; a local status response does not prove
the global device-approval setting or that an exported policy is currently
published. Preflight does not claim to audit those admin settings.

Export the **complete** existing Tailnet policy as strict JSON (not HuJSON with
comments), then use `policy-render --plan PLAN --policy COMPLETE_POLICY
--policy-backup-dir ABSOLUTE_NEW_PRIVATE_DIRECTORY`.
The renderer preserves existing entries and adds exactly:

- Network grant: `100.94.211.115` → `tag:wisp-inference`, TCP 22/443/8443.
- SSH rule: designated Tailnet user → mini tag, `action: check`,
  `checkPeriod: always`, one designated non-root macOS account.
- TCP/UDP network assertions and SSH check/root-denial assertions.

SSH rules cannot use literal IP sources. The network grant and identity-based
SSH rule must both match. Add actual denied peer IPs and alternate account tests
from your Tailnet inventory before publishing. Never describe sample test peers
as proof against every real device.

Generated assertions are mandatory subsets, each present exactly once. Additional
network tests may contain only `src`, `proto` and a nonempty `deny` list: use a
canonical non-primary IPv4 address in the Tailnet range, `tcp` or `udp`, and the
fixed mini tag with explicit ports 22/80/443/8000/8443/8765/8766. Additional SSH
tests may contain only a concrete Tailnet email `src`, `dst: ["tag:wisp-inference"]`
and concrete account names in `deny`. The designated owner cannot deny its approved
account, since that contradicts the mandatory check assertion. Wildcards, CIDRs,
accept/check/grant fields, unknown ports, duplicated assertions and altered samples
refuse. These validated deny-only additions survive render and live preflight;
they never authorize access. Their real inventory and publication still require review.

**Grants are additive.** A narrow rule cannot override a wildcard ACL, broader
CIDR, another matching tag/group/IP set, broader SSH account, or Funnel attribute.
The checker preserves provably disjoint rules for concrete inventoried addresses
outside every inference-tagged device and every address of the primary. This
includes bounded network/SSH/legacy ACL rules and their tests. Tags, aliases,
groups, CIDRs, wildcards and indirect selectors cannot prove disjointness.
Unknown policy syntax and overlapping rules refuse. Do not replace the entire
Tailnet policy with this example to make a check pass.

Additional rules require `plan.policy_inventory` containing complete device
IDs, canonical address lists and tags. Preflight binds all observed identities,
addresses, tags, hostnames and SSH keys to a canonical security projection;
traffic counters do not invalidate it. Missing/new/changed peers refuse.
Local visibility alone is not proof of completeness. Supply a separately reviewed
`--policy-inventory-approval` pinned by `plan.policy_inventory_approval_sha256`.
Its strict schema binds the target host/node/IP, full inventory hash, security
projection hash, exported-before-policy hash, and a maximum 900-second validity
window. `source` is `independent-admin-export`; `complete_export_reviewed` must
be true. These declarations require an actual independent complete export review,
not generation from local peer output. Before/after backups and their receipt
bind exact bytes and the plan, but do not authenticate policy publication.

Publish only after the full policy passes Tailnet's own tests and review. Keep
the exported policy used by live preflight synchronized with that approved
publication. The CLI does not call the admin API or alter policy.

## Serve, ports, firewall and disabled services

The primary and mini oMLX must remain bound to loopback on port 8000. Preflight
uses the kernel TCP socket inventory and fails on wildcard binds or unknown state.
Disabled staging additionally requires no listeners on ports 8765 and 8766,
including other users' listeners; the receiver checks again during staging.
Both firewalls must already be enabled. Read-only `socketfilterfw` queries inspect
global state, application exceptions, and automatic built-in/downloaded signed-app
allow rules. Unknown, localized, truncated or ambiguous inventories fail closed.
Because service executables may be shared interpreters, every allowed inbound
exception is conservatively unresolved, even when its name appears unrelated.
Blocked exceptions are counted and accepted only with automatic allow rules disabled.
Reports contain completeness/absence booleans rather than application paths.
No command enables, disables, edits or resets the firewall. An incompatible existing
firewall configuration blocks arrival; provisioning never repairs it.

After future service qualification, an administrator may configure **Serve**:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve --bg --https=8443 http://127.0.0.1:8766
```

These are the exact routes in the ordered arrival contract; ordinary staging does
not execute them. Live arrival requires a separately qualified adapter and all
independent approval gates. Never run
`tailscale funnel`; do not grant the `funnel` node attribute. The probe checks
`AllowFunnel` recursively, including foreground sessions, and treats unknown
Serve schemas as inconclusive. Check any separate Tailscale Services inventory
in the admin console too. Backend ports 8000/8765/8766 remain inaccessible through
the proposed network grant; only Serve terminates HTTPS on 443/8443.

Activation verifies the exact peer hostname/IP/node ID/tag, online status and
advertised SSH host keys, then rechecks identity immediately before secret
access. `tailscale ssh` verifies advertised host keys. Interactive check-mode
authentication may need operator completion; timeout/failure blocks staging and
never prints remote diagnostics.

The receiver verifies archive and per-file hashes, version, exact runtime file
set, service routes/ports/argv/keys, and rejects duplicate, noncanonical,
traversal and non-regular archive entries. A private release directory under
`~/.wisp-mini` is identified by runtime digest, provisioning assets and node ID.
It accepts only an offline runtime artifact built for the reviewed candidate SHA
by `build-support/mini_artifact.py`; source-only bundles cannot be staged. CI uses
the pinned Python archive and compiler plus a complete hash-locked wheel closure.
Dependencies install with `--no-index --require-hashes --only-binary=:all:` during
artifact creation. The mini performs no compilation, dependency resolution, or
network installation. The `venv` directory contains a relocatable standalone
Python; only `python -m` entrypoints are supported.

The receiver materializes the complete artifact into a private temporary directory,
checks helper signatures/protocol versions, imports, and synthetic runtime health,
then atomically publishes it. Repeat staging reconstructs the expected inventory
from the externally pinned bundle and checks every file's digest, type and mode.
Unknown directories, marker-only state, and changed runtime files are refused.
Disabled launchd templates remain inside the release; no job is installed or loaded.
Rollback checks both
`gui/<uid>` and `user/<uid>` launchd domains and unloads only recorded labels,
restores known primary local bindings, and disables primary proactive polling.
Applied rollback rotates the credential generation and waits for a fresh native
backend receipt after restoring the local bindings. Unknown roles require manual resolution;
rollback does not guess a local model. It retains user state and credentials. Applied rollback reports completion only when the native receipt binds the new
generation, unchanged local overlay hash, and the currently sole owned listener PID.
A timeout, unowned healthy endpoint, stale receipt, unsafe file or changed generation
keeps a backend-restore quarantine marker and fails. Retry the same reviewed rollback
command after resolving the installed-app qualification gate; no stale epoch revives.

## Release evidence still required

Synthetic fixture runs do not qualify real Tailscale SSH, Keychain ACLs, device
approval, HTTPS issuance, dependency installation, native service lifetime,
installed-app packaging, or model health/performance. These are explicit
specialist gates for a separately approved isolated/live environment, followed
by the Release Auditor at the exact candidate SHA. Do not deploy or enable
services from a successful fixture report.

References: [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh),
[grants](https://tailscale.com/docs/reference/syntax/grants),
[policy tests](https://tailscale.com/kb/1337/policy-syntax),
[Serve](https://tailscale.com/docs/reference/tailscale-cli/serve),
[device approval](https://tailscale.com/docs/features/access-control/device-management/device-approval),
[HTTPS](https://tailscale.com/docs/how-to/set-up-https-certificates),
[Apple Keychain ACLs](https://developer.apple.com/documentation/security/access-control-lists).


Repair qualification: native reads inspect decrypt/ANY ACLs and require the exact
trusted reader identity set before readiness or reuse. Unknown/broad existing ACLs
are refused, never silently rewritten. Primary helpers require a private owner,
regular executable, source/hash receipt, valid signature, and protocol v2. Explicit
staging records a separate reviewed host/node receipt: model configuration cannot
swap native node/inference credentials or redirect them to another HTTPS origin.
Local Swift 6.2 development artifacts cannot satisfy the CI Swift 6.1.2 release
contract; those artifacts are marked unqualified and cannot be activated.


## Isolated signed ACL qualification fixture

`python3 build-support/isolated_acl_fixture.py --output /private/tmp/acl-report`
compiles and ad-hoc signs four temporary executables, verifies their signatures,
and writes a report. This default mode **does not run any Keychain operation**;
its qualification status is `UNAVAILABLE_NOT_EXECUTED`, even when compilation
passes. The strict GitHub workflow instead makes actual isolated execution a
mandatory step and uploads only its machine-readable qualification report.

Execution with `--ephemeral-macos --expected-sha SHA` is restricted to the strict
GitHub-hosted disposable macOS runner, with no user data or imported signing
identities. The driver checks hosted-runner environment markers, exact source SHA,
cleanliness, and supported macOS/architecture. These checks do not make a shared
host disposable: never spoof them to run locally. Ordinary local simulation does
not contain `securityd` and must remain compile-only.

Runtime qualification snapshots the default Keychain identity, ordered search list,
status bits, and login-Keychain file/sidecar metadata without reading credential
contents. It requires equality before creation, immediately after creation, after
test cases, and after cleanup. It never resets default/search-list state to make
a comparison pass. Scoped cleanup runs in `finally`, unlocks only the synthetic
store using its dummy password from stdin, deletes it through Security.framework,
and verifies temporary files are removed. Missing snapshots, changed ambient
metadata, unavailable/incomplete execution, failed allow/deny checks or cleanup
fail the job. Only report JSON is uploaded; temporary stores and values are not.

The fixture creates a unique private temporary Keychain and scopes every read/write
to its explicit reference; it never queries ambient credentials, changes default/search lists or calls
production initialization. Only synthetic values pass through stdin. It checks the
original reader, unrelated-reader denial, signed replacement refusal, exact helper
and receipt restoration, continued original-reader access, recovery/readiness blocking,
and locked-store denial. Fixed typed outcomes
distinguish policy/OS denial from unavailable or isolation failures; inconclusive
results block qualification. The report records exact SHA, OS build, architecture,
compiler and ad-hoc signature identities. A pass covers only these synthetic ad-hoc
identities, not Developer ID upgrades, login Keychain behavior or production rollout.

Generic remote readiness reads also have deadlines and pre-parse byte limits:
64 KiB for health, 1 MiB for model/status inventory. Compressed, oversized, malformed,
or interrupted responses cannot advertise readiness; optional tool-free generation
may choose the existing local fallback before any generation request is sent.


The schema-version-2 qualifier follows the shipped fail-closed policy. It executes the production `prepare_helper` directory/receipt transaction with a signed synthetic-binary build adapter and a private synthetic home. The denied replacement must restore the exact original directory, binary and receipt; the original reader must still work. The durable `helper_restored_keychain_unverified` marker must remain and the production status/export/init gate must refuse before any helper execution. ACL migration is unsupported; no ACL-edit API or synthetic migration case exists. All seven cases, exact snapshots, cleanup, and ambient-state equality are mandatory. CI runs this gate before the longer source build; later strict build/artifact checks remain required.


## Completion preparation contracts

All new commands default to zero-effect preparation. `--live --apply` is necessary
for effects; local credential commands additionally require `--approve-keychain`,
and local oMLX restart requires `--approve-local-omlx` plus an independently reviewed
`--authorization` document and its `--authorization-sha256`. These switches are
operator authorization, never inferred from fixtures. Real Keychain, firewall,
Tailnet, publisher identity, installed-app and hardware qualification remain external.

### Local authentication and recovery

`rotate-local` requires an already valid canonical token, matching Keychain value,
and valid/missing/invalid HTTP authentication behavior. `migrate-local` handles an
absent Keychain local entry and a missing/legacy settings token. Malformed or denied
Keychain state always refuses. Both generate the new token in memory, use native
compare-and-swap through stdin, preserve existing verified ACLs, and atomically write
only private `~/.omlx/settings.json`. No token enters argv, YAML, plists, reports,
child diagnostics, or source files. Settings and their parent must be owned and
private; symlinks, hardlinks and concurrent settings changes refuse.

A local authorization is strict JSON with `schema_version: 1`, exact `source_commit`,
`action: "local-auth"`, current non-root `uid`, `plist_sha256`, `executable_sha256`,
`runtime_manifest_sha256`,
and `native_qualified: true`. Its digest must come from the separate approval channel.
The approved installed plist is `~/Library/LaunchAgents/com.wisp.omlx.plist`; its
versioned executable must match the rendered template layout. The loaded launchd
arguments, environment, output paths, executable hash and on-disk plist are rechecked
before restart. Unknown launchctl text refuses. Qualification must cover that exact
oMLX version and its dependencies; this repository does not install it. The
runtime manifest hashes the complete materialized interpreter/package tree.
External symlinks, external interpreter shims, venv base references, executable
`.pth` startup imports, and mutable code refuse. Merely hashing the CLI shim
does not establish provenance.

Before any HTTP Authorization bytes, the explicitly connected socket is bound to
the approved launchd process using its exact TCP four-tuple. Unfiltered established
connection inventory must show one server descriptor owned by the approved PID/UID
and one client descriptor matching the retained socket. Kernel TCP state must agree;
process start identity and effective/real/saved UID must stay fixed. The socket,
descriptor, tuple and owner are rechecked before sending and after response. Automatic
reconnect is disabled. Missing, duplicate, unsupported or racing evidence refuses.
Responses that close or detach the connection fail qualification; the approved oMLX
version must preserve the connection through the post-response inspection.
Restart permits a new PID only at its explicit boundary and probes use fresh connections.
These observations reject listener-handoff impersonation; they are not cryptographic
peer authentication against an approved process deliberately transferring its accepted
descriptor after inspection or a compromised same-UID host.

The primary runtime and mini gateway use the same established-connection
attribution implementation before every nonempty HTTP write. Their generic
clients do not retain bearer headers. A fresh connection must be attributed;
connection invalidation during inspection refuses before writing. Runtime trust
comes from private `~/.moe/omlx-runtime-authorization.json`, installed by the
independent host adapter, never inferred from a health response. Preparatory
auth probes additionally require post-response connection inspection as above.

The transaction holds the shared provisioning lock, invalidates the old generation,
and publishes a secret-free quarantine journal before mutation. Acceptance requires
valid-token 200 and missing/wrong/revoked-token 401/403 on loopback `/v1/models`,
plus native agreement and unchanged settings. Rollback restores only this transaction's
unchanged settings bytes and compare-and-swap token; it never overwrites a concurrent
writer. An uncertain restoration retains quarantine and both auth states remain
unavailable to Wisp until explicit recovery.

Use `recover-local --decision verify-agreement` for a verified current state or
`--decision repair-agreement` to explicitly authorize completing an interrupted
settings/Keychain transaction. Repair reconciles to the private settings token, or
creates a fresh token for missing/legacy settings, through the same verified helper.
It does not reconstruct an old secret from a journal. Failed verification keeps the
journal. Successful recovery requires unchanged source/helper/settings, native
agreement, qualified restart and authentication checks, then another fresh generation.

### Credential helper recovery

`recover-credentials` handles version-2 helper journals and always requires
`--live --apply --approve-keychain --source-sha SHA`. The decision `accept-current`
or `restore-prior` requires the selected helper to match that current reviewed
source. An old helper is not implicitly authorized by a retained journal.

After exact-reader ACL replacement denial, `--decision restore-reviewed-prior`
can recover an independently approved historical helper. It additionally requires
`--authorization` and an independent `--authorization-sha256`. The private JSON
has exactly: `schema_version: 1`, `action: "restore-reviewed-prior"`, current
`recovery_source`, reviewed historical `helper_source`, SHA-256 of the exact journal
bytes in `journal_sha256`, exact journal `prior` and `candidate` inventories, and
numeric owner `uid`. Do not infer approval from the journal. Phase or inventory
changes require fresh review and authorization.

All decisions check immutable Git provenance, signature, final-path inventory,
recorded phase, exact native ACL readers and settings/token agreement. Only verified
agreement and a durable fresh generation allow quarantine to clear. Historical
recovery retains credential values and exact ACLs; it does not adopt the replacement
or claim new tokens. Backend refresh is required. Native denial/interruption,
unknown journals, unreviewed old helpers and inconclusive first installations remain
quarantined; never remove journals manually. New-helper adoption, actual rotation
and real installed-app/oMLX qualification remain external. Disposable CI tests this
path only with private synthetic Keychains and signed fixture readers.

### Publisher-authenticated transfer

Signed staging serializes release-sequence consumption through publication and
receipt replacement. A consumed sequence cannot be retried after interruption;
obtain independently signed higher-sequence metadata. Owner, receipt and primary
binding metadata include the authenticated sequence and statement digest.

Live `activate` additionally requires `--signature`, `--publisher-trust`,
`--publisher-trust-sha256`, `--publisher-key-id`, and `--release-sequence`.
The public trust file is `{ "schema_version": 1, "keys": { "SHA256_OF_PEM": "PUBLIC_PEM" } }`.
There is no default production key. Independently approve its hash, key identity,
exact source/archive hashes and sequence; never copy expected pins from an untrusted
envelope. Version-1 metadata binds purpose, RSA/SHA-256 algorithm, key ID, exact
source commit, archive SHA-256, monotonic sequence and a maximum seven-day lifetime.
Unknown fields, duplicate JSON keys, downgrade, substitution, expiry and replay fail.

Verification occurs before credential export and again before receiver staging.
Private, locked, durable sequence ledgers on both machines consume a sequence before
action. An interrupted attempt therefore requires a newly signed higher sequence,
even for the same artifact; a consumed sequence is never silently retried. Staging
can resume its immutable inventory with that newly authorized envelope. The primary's
SSH-authenticated request carries its reviewed trust pins to the receiver; this is
not a separate trust root against a compromised primary account.

`build-support/sign_mini_artifact.py` supports a separately approved publisher runner.
Private signing material arrives through an inherited descriptor (`--key-fd`), never
a key command argument or committed file. It requires `--approved`, the explicit
publisher approval environment flag, clean exact source and archive pins, and
self-verification before output. Ordinary PR artifacts remain unsigned and cannot
activate. Synthetic CI signatures use disposable generated keys and prove only the
cryptographic contracts, not a real publisher identity.

### Arrival and oMLX candidates

`python3 infra/mac-mini/arrival.py --binding BINDING.json --dry-run` renders the
versioned ordered plan. Binding pins cover source content SHA-256, artifact, model,
resource contract, node, task and account. The approved plan digest and independent
adapter qualification digest cannot be supplied by an untrusted plan. All seven
gates—identity, hardware, model, resource, network, credential and task—must pass.
The order is qualified user services, Serve 443 → 127.0.0.1:8765, Serve 8443 →
127.0.0.1:8766, qualified model/resource binding, then disabled staged role migration.
The executor captures each prior state before action, verifies every action, and
restores/verifies in reverse order on partial failure. Inconclusive recovery blocks.

`templates/omlx-v1.json` and `omlx-launchagent-v1.plist` are disabled candidates.
`prepare_omlx` requires a publisher-verified version/archive pin; `render_omlx` adds
an independently verified executable pin. No package installation occurs. The config
candidate is a Wisp qualification contract, not an assertion that every upstream
version accepts the same settings schema. The pinned version must be qualified
against [upstream oMLX configuration](https://github.com/jundot/omlx/blob/main/README.md).

The live arrival adapter remains an explicit integration/qualification dependency:
the CLI refuses `--apply` without it. The tested ordered executor accepts only a
separately qualified adapter; it never loads arbitrary plugins or guesses live
service/resource contracts. Mini Runtime's resource contract, telemetry and model
qualification must be integrated before that adapter can be released. This is
preparation evidence, not a claim that arrival can be executed on current hardware.

### Validation and integration

Synthetic tests are registered in Simulation QA and CI. Native recovery checks
compile and run without app launch or Keychain calls. Final candidate evidence must
record the exact commit; no CI pass is inferred from local tests or missing checks.
Mini Runtime owns `mini/**` and its tests. Its three named completion tests are
explicitly registered when those files are integrated; their absence here is not
counted as a run. Reconcile the final bundle file contract and run combined gates
before independent Release Audit and any required isolated/live qualification.
# Integrated arrival preparation

The combined artifact requires every mini resource/runtime/backup module and
ships `payload/preparation/arrival.py` plus both disabled oMLX templates. These
files are authenticated by the same archive manifest and publisher signature as
the runtime. They are never automatically executed during staging.

`arrival.py --binding BINDING --dry-run --resource-contract CONTRACT
--preparation-evidence EVIDENCE --trusted-evidence-sha256 PIN` validates an
explicitly synthetic rehearsal. Use absolute private 0600 paths for the contract
and evidence. `PIN` comes from the independent evidence review channel. The
command works from a foreign directory using only its repository or signed
sibling runtime; it does not import an input-selected module.

`prepare_integrated` binds the exact plan to all model/tokenizer/runtime revisions,
profile digests, quantization and ordered 8k/16k reports, fresh resource telemetry,
the exact 60/2/20 GB limits, 150 GB startup free space and a permanent 50 GB reserve.
It requires one storage device for model/cache/telemetry/state/backup, a sole
backend and quota assertion, SQLite online/verified/new-directory restore with
cursor rotation, exact host/account/policy/Serve and publisher-signature evidence.
Tests supply synthetic observations; passing them is not hardware, cryptographic,
backup-restoration or administrator qualification. Actual signature and SQLite
backup/restore behavior have separate adversarial suites.

`PreparedArrivalAdapter` rehearses the fixed ordered actions and reverse rollback
in memory only. `arrival.apply(..., simulate=True)` must be explicit; a prepared
adapter is refused by live apply even with valid approval and adapter pins. The
normal CLI still has no live adapter. Jobs/providers and role migration remain
disabled. A live adapter requires independent exact-host, administrator, Tailnet,
Serve/firewall, publisher, credential, engine/quota, model and benchmark approval
plus explicit task authorization. No synthetic flag can grant that capability.

For qualified gateway launch, the native launcher passes the fixed private files
`state/qualification/resource-contract.json` and `resource-telemetry.json` beside
the release directories. Missing, unsafe, stale or mismatched files refuse startup.
The authorized live supervisor must publish/refresh these files and enforce engine
limits; staged installation never fabricates them. An empty default model roster
is intentionally unqualified. Concrete deployment revisions and profiles must be
selected and verified for the actual hardware before inference can start.
