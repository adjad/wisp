# A05a internal DOM extraction

Run from the repository root, without installing dependencies:

```sh
node --test tests/browser_dom/*.test.cjs
node --check browser-extension/shared/page-extractor.js
```

The collector is an internal primitive, **not an A01 public protocol**. A05 must
map its results to A01 after that contract lands. Nothing installs or invokes
this collector in an extension, bridge, browser session, or native host.

## Use and output

Load `browser-extension/shared/page-extractor.js` as a classic script to obtain
`globalThis.WispPageExtractor`, or require it in Node tests. Call
`createPageExtractor(document, options)` with an explicit document. `collect()`
reads it synchronously and returns normalized text, headings, tables (caption,
ordered rows, header/data cells), HTTP(S) link destinations, and labeled control
descriptors. There is no value, HTML, DOM selector, arbitrary code, action,
network, or element-resolution API. Input types and control roles are allowlisted.

IDs contain a random document namespace and an opaque element counter. They
remain stable for the same node within a module instance and Document, including
across collectors. Replacement nodes and other Documents get different IDs.
The WeakMaps do not retain discarded nodes/documents. These IDs are not persistent
locators, authorization tokens, or public contract identifiers. UUID generation
must be available; the collector does not fall back to predictable document IDs.

The default scope is the current document body. An optional same-document `root`
limits extraction and still inherits ancestor exclusions. Offscreen rendered
content is included. Scoped roots must remain attached. Limits can be lowered
with `maxNodes`, `maxDepth`, `maxTextChars`, `maxRecords`, `maxStringChars`;
values above the implementation ceilings are clamped. A record budget includes
table rows/cells as well as top-level semantic records. Record payloads are created
only after a budget slot is available, so rejected records do not materialize
labels, cell text, or URLs. An already emitted table can still receive its caption.
The earlier DOM traversal still runs. Truncated structures must not be interpreted
as complete. Whitespace is normalized, not layout-preserved.

Input work is bounded before string processing, not just at serialization:

- `maxTextChars` also limits the cumulative raw text prefix inspected per scan.
  Whitespace consumes that input budget even if normalization emits nothing.
  Text ranges cover only the retained prefix. Oversized text adds
  `text-input-limit` and `text-limit`; later content may be omitted conservatively.
- IDs, label targets, ARIA labels/references, hrefs and the base URI must fit
  `maxStringChars` before normalization, splitting, identifier hashing, or URL
  parsing. Oversized attributes/URLs are skipped with `attribute-input-limit`
  or `url-input-limit`; identifiers and destinations are never prefix-truncated
  into a different meaning. Oversized `aria-labelledby` suppresses fallback labels.
- Privacy metadata has separate small raw caps so tiny output limits cannot
  disable privacy checks: role 256, autocomplete 1024, type 64, contenteditable
  32, and aria-hidden/aria-disabled 16 UTF-16 code units. An oversized field
  excludes its subtree with `privacy-metadata-excluded`. Element names above
  128 code units are excluded with `tag-name-input-limit` before case conversion.

These limits bound collector-side string processing. They cannot bound the
browser's cost of DOM string retrieval, style calculation or layout. Mutation
rescans apply the same limits. Synthetic tests instrument expensive string
operations, identifier Maps and URL parsing in an isolated Node VM, including
multi-megabyte inputs and one-character budgets, without timing-dependent tests.

## Exclusions and limitations

- Hidden, inert, `aria-hidden`, CSS display/visibility/opacity-hidden content and
  ancestors are excluded. Missing style or text geometry fails closed. Text
  geometry uses DOM Ranges; `display: contents` parents do not suppress visible
  descendants. Closed details include only the first summary.
- Input values, textarea contents, selected option text, contenteditable trees,
  and textbox/searchbox/combobox/spinbutton contents are always excluded. Controls
  can retain their public labels. Every token in the bounded ARIA role list is
  checked for a draft-capable role, including unknown-first and abstract-first
  fallback lists. This deliberately excludes contents even for `button textbox`,
  without claiming to implement browser effective-role resolution. Control kinds
  remain advisory first-token hints. A document in designMode is excluded entirely.
- Password/hidden inputs, password/OTP/payment autocomplete fields, and subtrees
  marked `data-wisp-private`, `data-private`, `data-sensitive`, or `data-draft`
  are excluded entirely. These markers are a conservative internal convention,
  not a new public contract. Script, style, template, and noscript are omitted.
- Labels come only from visible collected text or explicit `aria-label` metadata;
  hidden or editable `aria-labelledby` targets cannot bypass the exclusions.
  Neither values nor placeholders are label fallbacks. Labels from outside a
  scoped root are not collected. At most 32 referenced or explicit labels
  contribute to a descriptor.
- Destinations use URL parsing without navigation. Credentials, **all** query
  parameters and fragments are removed. Unsupported/malformed/overlong URLs
  produce a null destination. Ordinary rendered prose, ARIA labels and URL paths
  can themselves contain secrets; this is structural exclusion, not a general
  secret detector. Later policy must still constrain page access and disclosure.
- Frames, shadow contents, canvas, SVG, media and embedded surfaces are unread.
  Closed shadow roots cannot be detected reliably. Generated CSS content,
  clipping/occlusion, exact whitespace layout, and accessibility-tree semantics
  are not established. Every result therefore reports `coverage.complete: false`
  plus stable reason strings, including exhausted budgets and redactions.
- No virtualized content scrolling, focus, clicks, script evaluation, event
  dispatch, storage, or external requests occur. The adapter must interpret
  control labels/kinds as descriptive hints, never as sufficient action authority.

## Mutation lifecycle

`observe(onChange, settings)` explicitly installs a MutationObserver on the given
Document and immediately emits the initial snapshot. Child, text and attribute
mutations invalidate the snapshot. Records/old values are never serialized.
Trailing debounce defaults to 50 ms; a 250 ms maximum wait bounds continuous
bursts. `debounceMs` and `maxWaitMs` are positive integer settings (maximum wait
is at least the debounce). Equal serialized snapshots do not emit again.
The returned `flush()` performs an immediate deduplicated scan; `stop()` disconnects
and cancels all pending work, is idempotent, and makes future flushes no-ops.
The caller owns disposal on navigation or teardown.

DOM mutation observation does not detect every layout change: viewport resizing,
CSSOM stylesheet edits, stylesheet loading, animation, or property-only state
changes may need an explicit `flush()` from the future adapter. Hidden mutations
may still schedule a scan, but unchanged results are deduplicated. Optional timer
and observer dependencies support deterministic synthetic tests; they are not
page-provided execution requests. Consumer callback errors propagate; an initial
callback failure disconnects the observer.

## Validation boundary and follow-up

`synthetic-dom.cjs` is a narrow DOM/geometry test double. Fixtures contain invented
content and sentinel secrets only. Tests exercise collection policy, structure,
identity and scheduling without opening a browser or touching user state. They
**do not prove real browser layout compatibility**. Browser-hosted synthetic QA,
independent privacy review, and exact-head required repository CI remain release
gates managed by the Hub/Orchestrator. No package or CI file is changed here, so
the Node suite must be invoked explicitly by that gate until integration adds it.
