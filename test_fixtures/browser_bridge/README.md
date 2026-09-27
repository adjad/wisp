# A04 authenticated bridge boundary

These fixtures use synthetic fixed keys and disposable SQLite databases. They
never read Keychain, install an extension, launch Wisp, access a browser, send a
communication, or execute a command. Run:

```sh
python -m pytest -q tests/test_browser_bridge.py
python scripts/check_browser_contracts.py
swift build --package-path app
```

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
   The wire decision resolves A03 revisions, evidence and exact intent. An
   extension-supplied `ExactApproval` cannot create or consume consent.
4. Supply a native-owned runtime-context provider on both sides. It must refresh
   profile, explicit enablement, private mode, background ownership, current
   URL, site grants, task and snapshot. It must never decode these assertions
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

No changes to `AppDelegate.swift`, `service/main.py`, A01 contracts, A03 authority,
existing credential namespaces or installed app state are part of this slice.
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
signed native helper identity, private-mode
capture in a real browser, extension packaging or a live IPC transport. Those
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
