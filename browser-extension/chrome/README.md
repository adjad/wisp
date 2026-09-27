# A06 preparatory capture and disclosure boundary

This is an inactive policy and UI slice on the independently audited A04+A05
composite `98ff060a42e694da5898e1a6113205954441b0b0`. It does not complete A06 or
A10. No extension manifest, native-host registration, installer, app startup,
transport, page acquisition, or production catalog is supplied. Importing the
worker module registers nothing; trusted bootstrap must call `install`.

## Implemented boundary

- `disclosure.html` / `disclosure.js`: a trusted click requests a public-content
  preview. A second trusted click, after the preview is visible, authorizes one
  capture. Catalog strings use `textContent`, never HTML. Disconnect clears the
  preview and disables capture. The panel cannot submit text, URLs, runtime
  assertions, app approvals, or arbitrary commands.
- `service-worker.js`: the MV3 wiring seam accepts only the exact extension
  disclosure page, extension identity, origin, and expected port name. Tab and
  content-script senders are rejected. Its closed protocol has only `prepare`
  and `capture`, in that order. The private disclosure ID stays in the worker.
- `native-helper.js`: a Node policy core, **not an installed native host**.
  Trusted bootstrap supplies one immutable, reviewed A05 catalog and manifest,
  a configured source/catalog revision and exact profile/session/document/tab/
  permission epoch, a native-owned runtime provider,
  and a synchronous delivery sink. These dependencies are not wire arguments.
  Opaque connection identities and one-use, 30-second disclosure IDs prevent
  stale capture. Reconnect creates a fresh A05 document namespace and invalidates
  prior consent; it never queues or replays a delivery.

Before capture and again before delivery, the host compares the exact profile,
session, permission epoch, navigation/document identity, tab, source URL,
enablement, private state, site grant, background role and task with the preview
binding. Unknown or private context denies capture. Native providers must advance
the permission epoch on every revoke/regrant and document identity on every
navigation, including returning to the same URL; equality of URL alone is not
freshness. Providers must report revocation/reconnect transitions even if they
occur between calls. Foreground capture remains denied under the A05 contract.
The immutable configuration is pinned to these identities at trusted bootstrap.
A different session, document, tab, profile or permission epoch requires a new
trusted bootstrap and newly qualified catalog; a new preview cannot relabel the
old catalog with the changed identity. Reconnecting only the popup to the same
native runtime identity still requires fresh disclosure.

The sink receives `{binding, result}`. `result` is unchanged A05 extraction,
including `coverage.scope: approved_manifest` and `page_complete: false`.
`binding` supplies source/profile/session/catalog provenance for future consumers;
it is not an A01 wire record and must not be blindly sent to A04. Capturing this
fixed, reviewed catalog does not prove that its text is currently on a page.
No resulting item is persisted or projected into Today.

Every capture attempt consumes consent before delivery. A false return, exception,
or unsupported asynchronous return denies success and cannot be retried with the
same consent. It may represent uncertain delivery. This seam has no delivery
acknowledgement protocol or durable recovery record. A later transport must add
and qualify those before activation; it must never interpret denial as proof
that no bytes arrived or automatically retry an observation.

## Trust limits and remaining work

The UI, worker, bootstrap and native runtime provider are trusted application
code. Synthetic fixtures establish control flow, not Chrome renderer identity,
real user presence, OS identity, or live private-mode detection. `isTrusted`
filters script-generated clicks in this UI; it is not authority that a sender
can pass over the wire. The wire endpoint authenticates the installed extension
page and relies on that trusted page to show disclosure. A compromised extension
or bootstrap is outside this seam's trust boundary.

Operational A06 still needs:

1. Independently qualified public-catalog authorship/provenance and acquisition.
   Never turn raw DOM, accessibility text, drafts, password fields, shadow roots,
   page messages or arbitrary prose into a catalog. The synthetic reviewed
   fixture is the only demonstrated content source.
2. Native-owned, fresh Chrome profile/session/permission/private/tab/document
   authority, separate background-tab ownership, and qualification of the exact
   extension sender identity and visible disclosure in real Chrome.
3. MV3 packaging, resource layout/CSP, permissions, incognito exclusion,
   suspend/restart behavior, signed native-helper identity, protected inherited
   IPC, credential bootstrap/Keychain and revocation. The synchronous interface
   here must be redesigned and requalified for asynchronous native messaging;
   importing Node code into a worker is not supported.
4. A04 authenticated transport mapping, confidentiality, uncertainty tracking,
   disconnect acknowledgement and durable delivery reconciliation. No app
   approval credential, action executor or effect permission belongs here.
5. Exact-head CI, independent privacy audit, synthetic QA, and reconciliation to
   main after dependency PRs #103/#105 land. Installation and deployment require
   separate authority and qualification.

A10 additionally requires A09 integration, durable sourced evidence/items,
uncertainty and change handling, Today projections and controls, and the real
Chrome assignment-page-to-Today milestone. None is provided by this slice.

## Validation

Run `node --test tests/browser_chrome/acquisition.test.cjs` and
`python -m pytest -q tests/browser_chrome tests/browser_dom tests/test_browser_contracts.py`.
All browser events, DOM elements, host context, clock and delivery are synthetic;
tests neither launch a browser nor install or contact a native host. The wrapper
uses the existing validated Node resolver and strips preload variables.
Full simulation classification must be coordinated with the A18 runner owner;
an unclassified test is a blocking gate, never an exemption.

Chrome API reference for the future integration:
[runtime sender identity and ports](https://developer.chrome.com/docs/extensions/reference/api/runtime).
