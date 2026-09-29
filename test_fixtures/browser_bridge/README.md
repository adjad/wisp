# A04 authenticated bridge boundary

These fixtures use synthetic fixed keys and disposable SQLite databases. They
never read Keychain, install an extension, launch Wisp, access a browser, or send a
communication. Native tests compile and run disposable signed fixture executables
and connect only through private temporary Unix sockets. Run:

```sh
python -m pytest -q tests/test_browser_contracts.py tests/test_browser_transport_native.py
python scripts/check_browser_contracts.py
swift build --package-path app
```

The contract/lifecycle cases live in `tests/test_browser_contracts.py`. The 11
signed socket cases live in `tests/test_browser_transport_native.py`. Both are
explicitly classified in the reviewed full-profile Simulation manifest.

On macOS, the pytest suite compiles the actual Swift client, credentials adapter
and A01 contracts with `Harness.swift`, then exchanges signed frames with the
actual Python backend and A03 approval store. Keychain methods are compiled but
never invoked. Other platforms explicitly skip native Swift cases.

## Integration contract

The bridge is deliberately inactive. A06/A07 adapter/transport owners and a
separately assigned startup owner must supply these pieces before enabling it:

1. The trusted app bootstrap creates separate random credentials for each
   profile/peer/role using `BrowserBridgeCredentials`. The Keychain record binds
   all identity fields, disallows synchronization, and is device-local. All
   add/lookup/delete queries explicitly select the Data Protection Keychain;
   there is no legacy-keychain fallback. Reads
   fail without prompting when unavailable. Adapter credentials must never be
   given the app approval role or shared with another peer/profile.
2. Bootstrap transfers credentials to the backend through protected inherited
   IPC. Backend `provision` is an in-process bootstrap API, never a public RPC.
   No registration endpoint accepts a new key, peer identity or approval context.
   Backend restart loses all registrations/sessions; bootstrap explicitly
   reprovisions still-valid keys. Revoke the backend registration first, close
   its native channel, then delete the Keychain record; on deletion failure,
   keep the bridge disabled. Rotation always uses a fresh ID and key. The
   backend rejects reissued IDs/keys within its lifetime.
3. Bootstrap creates one A03 `AppApprovalContext`, installs that same object in
   `ApprovalStore` and `BrowserBridge`, and keeps it outside all wire APIs.
   Only the separately authenticated app approval channel can call `decide`.
   Both Swift and backend refresh trusted review scope before each decision:
   the provisioned profile, expected approval proposal ID, task and snapshot
   must match. The expected proposal is `approvalProposalID` in Swift and
   `approval_proposal_id` in Python; missing scope denies the decision. The
   provider must derive it from the app's profile-scoped proposal review state,
   never from the decision payload. A03 then validates revisions, evidence and
   the complete exact intent before recording any decision. An extension-supplied
   `ExactApproval` cannot create or consume consent.
   Approval/rejection review is separate from browser capture policy: the app
   can record a scoped decision while capture is disabled or foregrounded, but
   execution still rechecks all browser policy and context requirements.
4. Supply a native-owned runtime-context provider on both sides. It must refresh
   profile, explicit enablement, private mode, background ownership, current
   URL, site grants, task, snapshot and the app's expected approval proposal.
   It must never decode these assertions
   from the incoming adapter payload or echo the payload into the provider.
   Unknown context denies capture and command dispatch. Capture must exclude
   private data *before* transmission; the service checks independently.
5. Use a private inherited channel, with transport peer binding established by
   trusted bootstrap. HMAC provides integrity and role binding, **not
   encryption**. Do not put these frames on generic Hub broadcast/replay,
   unauthenticated loopback, logs or durable event storage. A future network
   transport requires its own confidentiality and OS-peer qualification.
6. The backend issues a random 30-second challenge signed for the credential.
   Swift authenticates it before replying with a signed registration containing
   an A01 handshake and a fresh 256-bit client nonce. The backend binds that
   handshake to the provisioned identity and returns a signed capability
   agreement echoing the client nonce. Swift checks the nonce before accepting
   commands, so a fresh client cannot accept a recorded old server transcript.
   Only authenticated successful replacement
   registration evicts an old channel. Unauthenticated challenge requests have
   bounded capacity and cannot evict an active session.
7. Native adapters may publish browser observations, snapshots and correlated
   results. App approval peers may publish decisions. Neither may submit
   commands to the backend. Trusted executors call `dispatch` only after policy
   and durable task-budget checks. Dispatch rechecks native context, validates
   A01 action shape and consumes required A03 consent before returning a frame.
   The native executor must recheck live context immediately before acting.
   Returned command data never executes automatically.
8. Results must match a pending action/task/proposal on the same session and
   cannot complete obligations. A `verified` status remains an adapter claim:
   A13 must independently verify evidence and reconcile through the discovery
   repository. This boundary does not persist observations/results or mark an
   obligation complete. A02/A14 own durable budgets across reconnect/restart;
   the per-session 25-action bound is only defense in depth.
9. Disconnect/revocation/reconnect discard pending work and report uncertain
   action IDs. Python `BridgeSessionClosed.uncertain_actions` and Swift
   `BrowserBridgeClosed.uncertainActionIDs` preserve those IDs on authenticated
   parse/validation failures. Use the trusted `close` hook on EOF, timeout or
   send failure. Native `disconnect` returns both its frame and uncertain IDs;
   a received disconnect returns `uncertain_action_ids` in the local event.
   Never replay an uncertain effect or release its A03 consumption. The
   integration caller owns recovery/reconciliation and transport lifetime.
   Native `publish(result)` now retains its action and receipt ID until a signed
   `result_ack` with exactly matching `action_id` and `receipt_id` arrives on the
   same authenticated session. A second result or command before ACK fails
   closed. Backend receipt routing retains pending state; trusted integration
   calls `acknowledge_result(..., persist=...)`, which must synchronously commit
   recovery state before the ACK is serialized. Persistence failure emits no ACK
   and preserves pending uncertainty. Lost ACKs leave native uncertainty even if
   backend acceptance succeeded. ACK means accepted transport delivery, never
   independently verified evidence or obligation completion. A13/A14 still own
   the durable action claim and crash/restart reconciliation; this boundary does
   not persist an outbox or authorize replay.

## Operational follow-on (still inactive)

`BrowserBridgeTransport` owns an already-connected Unix-stream descriptor and
closes it on any peer, framing, EOF, or deadline failure. Its trusted bootstrap
must create a private endpoint and supply an explicit code-signing requirement.
The implementation checks same effective UID, obtains `LOCAL_PEERTOKEN` from the
kernel, and uses `kSecGuestAttributeAudit` / `SecCodeCopyGuestWithAttributes` /
`SecCodeCheckValidity` before sending or accepting frames. No peer-supplied PID,
path, identifier, or signature assertion is accepted. Requirements must pin the
production signing authority and app identity; a generic valid-signature or
identifier-only requirement is insufficient. Only isolated tests use exact
ad-hoc code hashes. A pre-fork socketpair is unsuitable for this peer check
because the token can identify its creator rather than the eventual helper.

Frames use a four-byte unsigned big-endian length followed by 1–262144 bytes.
Partial reads/writes share one monotonic per-operation deadline. Descriptors are
nonblocking, close-on-exec, and suppress SIGPIPE. The caller must serialize all
transport operations and forward failures to the bridge's `close()` hook before
discarding uncertainty. This component has no listener, bootstrap registration,
Python service integration, or app startup wiring.

`BrowserBridgeCredentialLifecycle` serializes create/rotate/revoke with an
injected store and trusted backend hooks. Rotation requires the same
peer/role/profile and a fresh credential ID; it revokes backend sessions and
closes their transports before deleting the old record and creating the new
key. Any store/provision/revoke failure leaves the owner disabled. Provision failure
triggers immediate best-effort revocation; failed cleanup retains ownership and
uncertain IDs, including when provision might have succeeded
before its response was lost. Explicit revoke retries cleanup. The lifecycle
cannot adopt existing persisted identities on restart: startup reconciliation
and a durable credential registry remain separate activation prerequisites.

The signed transport fixture pins the exact disposable executable hash and
proves mutual accepted peer/framed echo, wrong client/server requirement and
nonmatching process rejection, fragmentation, oversize/zero length, truncation,
non-socket descriptor closure, and read/write deadline failure.
It creates sockets inside a 0700 temporary directory with a 0600 endpoint.
Credential tests inject memory storage and failure points, never Security
Keychain operations. These results do **not** establish Developer-ID trust or
Data Protection Keychain eligibility.


The A04 slice itself changed no `AppDelegate.swift`, `service/main.py`, A01
contracts, A03 authority, existing credential namespaces or installed app state.
A10 WP3 (below) is the activation that adds the bootstrap hooks.
Mail/Messages adapters and persisted observations are separate milestones.

## Wire format

An envelope is a closed JSON object with exactly `version`, `credential_id`,
`session_id`, `sequence`, `kind`, `payload`, and `mac`. Version is the string `1`.
Payload is canonical padded base64 of UTF-8 JSON; the MAC covers the **original
payload bytes** through that base64, avoiding cross-language JSON serialization
assumptions. Identity/session IDs contain only the closed ASCII ID alphabet.

HMAC-SHA256 uses a unique 32-byte key. Its message is these newline-joined fields,
without a trailing newline:

```text
wisp-browser-bridge/1
<to_service or to_peer>
<credential_id>
<session_id>
<sequence as decimal integer>
<kind>
<base64 payload>
```

`mac` is lowercase hex. Direction prevents reflected frames. Sequence numbers
start at zero independently in each direction and must match the exact next
number. Challenge is service sequence 0, registration peer sequence 0, agreement
service sequence 1. New connections use fresh random session IDs. No old result,
command or approval decision is replayed after reconnect.

Registration payload is the closed object `{handshake, client_nonce}`; agreement
payload is `{negotiated, client_nonce}`. The nonce is 64 lowercase hex characters.
Handshake/negotiated values use unchanged A01 schemas. Other message payloads
use their A01 contract directly; disconnect payload is an empty object.
`result_ack` is the closed object `{action_id, receipt_id}`. It is a bridge
protocol extension within this inactive implementation; both endpoints must
ship together. An old endpoint rejects the new kind rather than silently
clearing native recovery state.

Limits: 256 KiB envelope, 128 KiB decoded payload, depth 32 (root depth zero),
32 active credentials, 64 active/pending sessions, and 1,024 issued credential
IDs per backend lifetime. Duplicate keys, escaped duplicate keys, unknown
fields, invalid Unicode/base64, unsupported roles/versions/capabilities, private
or stale runtime context, malformed payloads and out-of-order frames fail closed.

## Qualification still required

The synthetic suite tests all three Keychain query configurations without
calling Keychain APIs. It also tests authentication, role/capability separation, runtime
privacy checks, A03 decisions/consumption, message correlation, revocation,
reconnect, replay and malformed input in Python and Swift. It does not qualify
live Keychain access control, signed-app Data Protection Keychain eligibility,
production signed native helper identity, private-mode
capture in a real browser, extension packaging or integrated app/service IPC.
Private connected IPC and exact-hash signed synthetic peer acceptance are tested
separately as described above. Those
remain disabled and require their owners' isolated native QA before activation.
The Data Protection Keychain selector follows
[Apple's macOS guidance](https://developer.apple.com/documentation/security/ksecusedataprotectionkeychain).
Lookup explicitly uses
[`kSecUseAuthenticationUIFail`](https://developer.apple.com/documentation/security/ksecuseauthenticationuifail)
to fail when authentication would require UI. This value is available but
deprecated in favor of `LAContext`. The isolated native fixture observed
`interactionNotAllowed` reading false after assignment, so this boundary does
not rely on that unchecked flag. Signed-app qualification must confirm
noninteractive behavior on supported macOS releases before runtime wiring.

### Activation-blocking signing dependency

At base `32135e50df1149341d7e3380a5936d845cca1da7`, the app entitlement file
contains Apple Events only; ad-hoc packaging does not supply app entitlements.
Protected Developer-ID signing exists only in the approved release workflow,
not PR CI. No approved disposable signed DP Keychain environment or identity was
available for this follow-on. Actual signed-host create/load/rotate/revoke,
noninteractive locked/unavailable behavior, production requirement selection,
and cross-process secret provisioning remain **UNQUALIFIED**. If backend revoke
also fails, a locally disabled lifecycle cannot prove the remote registration
is inactive; transport teardown and durable cleanup recovery are still required
from the eventual bootstrap owner. Do not enable the
bridge based on these synthetic checks. No build/entitlement/CI configuration,
installed app, signing secret, or user Keychain was changed.

### Dedicated artifact-CI qualification

The main artifact Simulation sandbox continues to deny `codesign` and network
access. `build-support/browser_bridge_gate.py` runs the 11 A04 native transport
cases (plus the 5 WP3 activation cases) first in a separate sandbox with a fresh 0700 short scratch directory.
Only AF_UNIX endpoints inside that run's socket subtree are permitted; IP
networking and unrelated Unix endpoints remain denied. Writes stay in scratch,
and signing tooling cannot modify production files. Private synthetic canary
reads, Keychain tooling, and Apple Events remain denied. No Keychain API access
is tested or qualified by these probes.

The runner owns a process group, enforces timeout/descendant cleanup, and checks
that every exact case (the 11 A04 transport cases plus the 5 WP3 activation cases)
has 3 unique passing setup/call/teardown rows, with no
skip or xfail. The report binds starting/ending SHA and clean-source status.
Generic sandbox denials and dedicated boundary denials are mandatory probes.
The build imports that actual report by hash into combined Simulation QA;
`validate_simulation` and final artifact verification reject missing, duplicate,
stale or incomplete evidence. The ordinary browser-contract cases still run
inside the generic sandbox. Nothing is deselected or treated as a synthetic pass.

## A10 WP3: activation with native-owned runtime context

Owned code: `BrowserBridge.swift` (activation at the bottom),
`BrowserBridgeCredentials.swift` (per-profile Keychain lifecycle and enablement),
`service/browser/host.py`. Bootstrap hooks: `AppDelegate.applicationDidFinishLaunching`
starts `BrowserBridgeActivation.shared` and gives `BackendManager` two seams
(`extraEnvironment`, `didLaunchBackend`); `service/main.py` lifespan calls
`host.start_from_environment`. `BackendManager.backendEnvironment` only strips an
inherited `WISP_BROWSER_BRIDGE_CONTROL`, so the endpoint is chosen by the app per launch.

- **Off by default.** With no enabled profile (D4/D7) nothing exists: no directory,
  socket, thread, Keychain call or backend traffic. `controlPathForBackend()` is nil,
  so the backend gets no variable and `start_from_environment` returns None.
- **Peer requirements are configuration, not code.** `BrowserBridgeConfiguration.production()`
  ships with no peer requirement, so no browser can be enabled until WP1 (Chrome host)
  and WP7 (Safari appex) register the code requirement of their signed binaries. Chrome and
  Safari have separate sockets, requirements and profile-ID namespaces (`chrome:` and
  `safari:`); a peer that names another browser's profile is refused.
- **Backend link.** The app listens on an owner-only socket; the backend connects. The app
  accepts only the exact PID it launched; the backend accepts only its parent PID.
  Credentials are provisioned over this link and never leave the app. On any drop both
  sides discard registrations and the app reissues fresh credentials.
- **Native-owned context.** `BridgeRuntimeContext` is built only from a verified native
  peer's `begin` report, checked (http/https, ASCII, no credentials, `private_context` and
  `extension_incognito` exactly false from both sides), bounded to 30 seconds, and spent by
  one observation. It is never decoded from an observation frame. Missing, expired or
  unknown context denies.
- **Revocation.** Disabling a profile revokes its credential, closes live peer sessions and
  removes endpoints when no profile remains; "forget" (D7) also deletes the Keychain record
  and issues a fresh key on re-enable.
- **No authority.** Only `extension`/`native_bridge` credentials exist here; the
  `app_approval` role and every decision, snapshot, result and command are refused. No
  effects: observations land in a bounded in-memory inbox for the discovery pipeline
  (WP4 owns ingest).

`ActivationHarness.swift` drives the real activation over real private sockets against the
real Python `BridgeHost`. Not qualified here: signed Developer-ID peer identity, live
Keychain eligibility, real browser hosts (WP1/WP7), and persistence of observations.
