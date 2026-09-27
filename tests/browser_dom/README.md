# A05 public-manifest extraction boundary

PR #87 was closed unmerged because arbitrary live DOM cannot prove exclusion of
closed shadow roots. Ordinary ancestors such as `body` can host an undetectable
closed root. Geometry, `shadowRoot === null`, `assignedSlot`, and custom-element
filters do not establish privacy. This implementation does not use those tests.

## What runs

The core creates a private immutable data tree (an **inert document**, not a
native `Document`) from a bounded JSON manifest. It extracts approved text,
headings, tables, links and descriptive control labels. It never renders HTML,
reads a DOM, loads resources, activates a browser, observes mutations, runs page
scripts or executes actions. All externally supplied documents are rejected
without reading their properties. Handles are private WeakMap identities.

`createPublicExtractor(catalogJSON)` is a trusted **application configuration**
entry point. The catalog explicitly lists each text and URL approved for
disclosure. Manifests can only reference existing entries and fixed semantic
kinds. They cannot introduce arbitrary text, values, attributes or subtrees.
Unknown kinds/fields/references reject the whole transaction. No `safe` flag,
HTML parser, sanitization regex, page assertion or visibility test can authorize
content. Catalogs are copied, bounded and immutable for the extractor lifetime.

The catalog is a real trust dependency: a malicious or misconfigured application
can put private prose in it. This core does not detect that and must not be
advertised as doing so. No production catalog loader or browser acquisition
path is supplied. A06/A07 must independently qualify who authors the catalog,
its provenance, fresh site/private-context policy and the acquisition/disclosure
process. Do not construct catalogs from live-page text or expose the constructor
to page scripts/messages. Run in an application-owned isolated realm. Synthetic
fixture membership proves only the fixture's explicitly reviewed disclosure.

Hidden, draft, form, password, slot and closed-root data have no accepted input
representation. Tests put secret sentinels in those rejected representations and
prove no property reads or partial output. These are data-boundary tests; they
are not browser rendering or comprehensive natural-language secret detection.

## API

```js
const E = require('../../browser-extension/shared/page-extractor.js');
// Application-owned configuration, never supplied by a web page:
const extractor = E.createPublicExtractor(JSON.stringify(publicCatalog));
const document = extractor.createDocument(JSON.stringify(manifest));
const result = extractor.capture(document, JSON.stringify(trustedRuntimeContext));
const changed = extractor.updateDocument(document, JSON.stringify(nextManifest));
extractor.dispose(document);
```

The synthetic catalog and manifest in
`test_fixtures/browser_pages/public-assignment.json` document the closed format.
Runtime context has exactly `task_id`, `captured_at_ms`, `enabled`,
`site_permission`, `private_context`, `tab_role`. A capture rejects disabled,
denied, private or foreground contexts. These values are serialization inputs,
**not authentication authority**; trusted adapters must establish them from
runtime state. A01 validation does not authenticate them either.

Output includes validated A01 `SourceObservation` and `BrowserSnapshot` records.
Extra semantic fields live in the envelope. Coverage always says
`scope: approved_manifest`, `page_complete: false`; complete means only the
accepted manifest. Never infer deletion/completion from absent content.
Control descriptors are metadata-only and always `editable: false`. Target IDs
have no live-node resolver and grant no permission to click/fill/select.

Each document receives a cryptographic random namespace. Material changes bump
its revision and all target IDs, preventing index reuse across revisions.
Equivalent normalized manifests preserve the revision. Captures have unique
snapshot/observation IDs and timestamps; `duplicate` reports equality with the
last successfully captured content, allowing downstream coalescing. Returning to
an older state still advances the revision. Rejected updates/captures leave state
intact. Disposed/foreign/forged handles fail closed.

## Bounds and validation

- JSON input: 131,072 UTF-16 units per call, checked before parsing. Object inputs
  are never inspected/coerced. JSON parsing itself is bounded by input length.
- Catalog: at most 512 texts and 512 URLs; text fields at most 512 UTF-16 units.
- Manifest: at most 256 work items including table rows and cells; no nesting.
  Tables permit at most 32 rows and 16 cells per row.
- Flattened text: at most 32,768 UTF-16 units including separators, stricter than
  A01's Unicode scalar limit. No truncation can turn a partial secret into output.
- URL query/fragment/user-info and non-HTTP(S) schemes are rejected. URL paths
  still need explicit disclosure review; syntax validation is not privacy.
- No traversal of external DOM, timers, retained DOM references or observers.
  Invalid/oversized input returns an error, never a misleading complete fragment.

Run `node --test tests/browser_dom/*.test.cjs` and
`python -m pytest -q tests/browser_dom tests/test_browser_contracts.py tests/test_discovery_contracts.py`.
The Python wrapper is discovered by `scripts/test_replay_failure_fixes.py` and
fails if Node is missing. Also run `python3 scripts/check_browser_contracts.py`.
The macOS QA wrapper shares a fixed-path runtime resolver with the build:
`/usr/local/bin/node`, then `/opt/homebrew/bin/node`. It requires a native,
executable canonical binary within those installation prefixes, outside HOME.
`QA_NODE_RUNTIME` can pin that selection but cannot choose a different path.
The build passes the canonical path explicitly and grants only its exact
process-exec literal. PATH stays sanitized; NODE_OPTIONS/NODE_PATH stay stripped.
Missing/unsupported Node is a failed gate. Direct Node tests remain portable.
Full QA first runs a separate mandatory Node-only sandbox qualification. Its
only executable permission is the canonical Node literal: no shell, Python,
developer, scratch or runtime executable subpaths. It executes all Node fixtures
and proves synthetic private-home reads, outside-scratch writes, network,
`/bin/sh`, `/bin/bash`, `/usr/bin/env`, Python and scratch-executable launches
remain denied. The shared full-QA sandbox separately retains its existing shell
and tool permissions for legacy tests; it does not promise per-Node isolation.
Native-only builds neither require Node nor run the Node qualification.
Independent exact-head privacy Auditor and synthetic Simulation QA are required.
Real-page capture and the A10 Chrome-page-to-Today milestone remain blocked on
the separate browser acquisition/disclosure boundary.
