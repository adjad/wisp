'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {webcrypto} = require('node:crypto');
const ROOT = path.resolve(__dirname, '../..');
const E = require('../../browser-extension/shared/page-extractor.js');
const C = require('../../browser-extension/shared/contracts.js');
const fixture = JSON.parse(fs.readFileSync(path.join(ROOT,
  'test_fixtures/browser_pages/public-assignment.json'), 'utf8'));
const clone = value => JSON.parse(JSON.stringify(value));
const context = () => ({task_id: 'synthetic.task', captured_at_ms: 1000,
  enabled: true, site_permission: 'granted', private_context: false, tab_role: 'background'});
function setup() {
  const extractor = E.createPublicExtractor(JSON.stringify(fixture.catalog));
  const doc = extractor.createDocument(JSON.stringify(fixture.manifest));
  const capture = (ctx = context()) => extractor.capture(doc, JSON.stringify(ctx));
  return {extractor, doc, capture};
}
const rejected = operation => assert.throws(operation, E.ExtractionError);

test('public synthetic assignment preserves semantics and maps exactly to A01', () => {
  const {capture} = setup();
  const result = capture();
  assert.equal(result.observation.text, 'Assignment 4\nRead chapters 3 and 4. Bring two questions.\nDue | October 2 at 5 PM\nAssignment instructions\nSearch course');
  assert.deepEqual(result.headings.map(h => [h.level, h.text]), [[1, 'Assignment 4']]);
  assert.deepEqual(result.tables[0].rows, [['Due', 'October 2 at 5 PM']]);
  assert.equal(result.links[0].url, 'https://course.invalid/instructions');
  assert.equal(result.snapshot.observation_id, result.observation.id);
  assert.equal(result.observation.revision, result.revision);
  assert.equal(result.snapshot.elements[1].editable, false);
  assert.equal(result.snapshot.elements[1].role, 'textbox');
  assert.equal(result.coverage.page_complete, false);
  assert.equal(result.coverage.manifest_complete, true);
  assert.equal(result.coverage.scope, 'approved_manifest');
  C.validate('SourceObservation', result.observation);
  C.validate('BrowserSnapshot', result.snapshot);
});

test('document and revision scoped targets, fresh captures and dedupe', () => {
  const {extractor, doc, capture} = setup();
  const first = capture(), second = capture({...context(), captured_at_ms: 2000});
  assert.equal(first.duplicate, false);
  assert.equal(second.duplicate, true);
  assert.equal(first.revision, second.revision);
  assert.deepEqual(first.snapshot.elements, second.snapshot.elements);
  assert.notEqual(first.snapshot.id, second.snapshot.id);
  assert.notEqual(first.observation.id, second.observation.id);
  assert.equal(second.observation.observed_at_ms, 2000);
  const equivalent = {nodes: fixture.manifest.nodes, url: 'page', title: 'title'};
  assert.equal(extractor.updateDocument(doc, JSON.stringify(equivalent)), false);
  const changed = clone(fixture.manifest);
  changed.nodes[0].level = 2;
  assert.equal(extractor.updateDocument(doc, JSON.stringify(changed)), true);
  const third = capture();
  assert.notEqual(first.revision, third.revision);
  assert.notEqual(first.snapshot.elements[0].target_id, third.snapshot.elements[0].target_id);
  assert.equal(third.duplicate, false);
  const other = extractor.createDocument(JSON.stringify(fixture.manifest));
  const otherCapture = extractor.capture(other, JSON.stringify(context()));
  assert.notEqual(first.document_id, otherCapture.document_id);
  assert.notEqual(first.snapshot.elements[0].target_id, otherCapture.snapshot.elements[0].target_id);
});

test('live DOM, closed-root ancestor and getter/proxy lookalikes are never read', () => {
  const {extractor} = setup();
  let reads = 0;
  // Public DOM APIs could report these same properties for a closed root.
  const live = {nodeType: 9, body: {shadowRoot: null, assignedSlot: null}};
  Object.defineProperty(live, 'documentElement', {get() { reads++; throw Error('PRIVATE'); }});
  const proxy = new Proxy({}, {get() { reads++; throw Error('PRIVATE'); },
    ownKeys() { reads++; throw Error('PRIVATE'); }});
  for (const input of [live, proxy, null, {}, [], 1, true]) {
    rejected(() => extractor.createDocument(input));
    rejected(() => extractor.capture(input, JSON.stringify(context())));
    rejected(() => E.createPublicExtractor(input));
  }
  assert.equal(reads, 0);
});

test('handles cannot be forged, copied, imported or reused after disposal', () => {
  const {extractor, doc} = setup(), other = setup().extractor;
  for (const fake of [{...doc}, JSON.stringify(doc), {safe: true}, Object.create(doc)]) {
    rejected(() => extractor.capture(fake, JSON.stringify(context())));
  }
  rejected(() => other.capture(doc, JSON.stringify(context())));
  extractor.dispose(doc);
  rejected(() => extractor.capture(doc, JSON.stringify(context())));
  rejected(() => extractor.updateDocument(doc, JSON.stringify(fixture.manifest)));
});

for (const field of ['safe', 'hidden', 'style', 'attributes', 'innerHTML', 'innerText',
  'textContent', 'shadowRoot', 'closedRoot', 'assignedSlot', 'slot', 'value', 'draft',
  'password', 'aria-label', 'aria-labelledby', 'title', 'private', 'contenteditable']) {
  test(`rejects ${field} payload without echo or partial output`, () => {
    const {extractor, doc, capture} = setup();
    const before = capture(), manifest = clone(fixture.manifest);
    manifest.nodes[0][field] = 'PRIVATE_SENTINEL';
    let error;
    try { extractor.updateDocument(doc, JSON.stringify(manifest)); } catch (e) { error = e; }
    assert.ok(error instanceof E.ExtractionError);
    assert.ok(!String(error).includes('PRIVATE_SENTINEL'));
    const after = capture();
    assert.equal(before.revision, after.revision);
    assert.ok(!JSON.stringify(after).includes('PRIVATE_SENTINEL'));
  });
}

for (const kind of ['input', 'password', 'textarea', 'form', 'draft', 'slot', 'template',
  'script', 'style', 'iframe', 'shadow', 'custom-element', 'html', 'document']) {
  test(`unsupported ${kind} trees are rejected as a whole`, () => {
    const {extractor} = setup(), manifest = clone(fixture.manifest);
    manifest.nodes.push({kind, text: 'PRIVATE_SENTINEL'});
    rejected(() => extractor.createDocument(JSON.stringify(manifest)));
  });
}

test('free text and unknown references cannot enter any output channel', () => {
  const {extractor} = setup();
  const variants = [
    m => { m.title = 'PRIVATE_SENTINEL'; }, m => { m.url = 'https://private.invalid/secret'; },
    m => { m.nodes[0].text = 'PRIVATE_SENTINEL'; },
    m => { m.nodes[2].rows[0][0] = 'PRIVATE_SENTINEL'; },
    m => { m.nodes[3].label = 'PRIVATE_SENTINEL'; },
    m => { m.nodes[3].url = 'PRIVATE_SENTINEL'; },
    m => { m.nodes[4].label = 'PRIVATE_SENTINEL'; },
    m => { m.nodes[4].role = 'password'; },
    m => { m.safe = true; }, m => { m.nodes[0].text = '__proto__'; },
  ];
  for (const change of variants) {
    const manifest = clone(fixture.manifest); change(manifest);
    rejected(() => extractor.createDocument(JSON.stringify(manifest)));
  }
  rejected(() => extractor.createDocument('<body><p>PRIVATE_SENTINEL</p></body>'));
});

test('catalog is explicit application disclosure policy, never inferred from manifest', () => {
  const catalog = {texts: [], urls: []};
  const extractor = E.createPublicExtractor(JSON.stringify(catalog));
  rejected(() => extractor.createDocument(JSON.stringify(fixture.manifest)));
  // This core cannot classify arbitrary prose. It intentionally has no catalog
  // learning, DOM import or safe=true override. Trusted catalog authoring is a
  // separate privacy boundary, as documented in README.md.
  assert.deepEqual(Object.keys(extractor).sort(), ['capture', 'createDocument', 'dispose', 'updateDocument']);
});

for (const url of ['javascript:alert(1)', 'data:text/plain,secret', 'file:///private',
  'https://user:password@example.invalid/', 'https://example.invalid/?token=secret',
  'https://example.invalid/#secret', '//example.invalid/path']) {
  test(`catalog rejects URL channel ${url.split(':')[0]}`, () => {
    const catalog = clone(fixture.catalog); catalog.urls[0].value = url;
    rejected(() => E.createPublicExtractor(JSON.stringify(catalog)));
  });
}

test('bounded input, node count, table work, strings and aggregate output', () => {
  const {extractor} = setup();
  rejected(() => extractor.createDocument(' '.repeat(E.LIMITS.jsonUnits + 1)));
  const wide = clone(fixture.manifest);
  wide.nodes = Array(257).fill({kind: 'text', text: 'title'});
  rejected(() => extractor.createDocument(JSON.stringify(wide)));
  const table = clone(fixture.manifest);
  table.nodes = [{kind: 'table', rows: Array.from({length: 32}, () => Array(16).fill('title'))}];
  rejected(() => extractor.createDocument(JSON.stringify(table)));
  table.nodes[0].rows = [Array(17).fill('title')];
  rejected(() => extractor.createDocument(JSON.stringify(table)));
  table.nodes[0].rows = Array(33).fill([]);
  rejected(() => extractor.createDocument(JSON.stringify(table)));
  const catalog = clone(fixture.catalog); catalog.texts[0].value = 'x'.repeat(513);
  rejected(() => E.createPublicExtractor(JSON.stringify(catalog)));
  catalog.texts[0].value = 'x'.repeat(512);
  const bounded = E.createPublicExtractor(JSON.stringify(catalog));
  wide.nodes = Array(64).fill({kind: 'text', text: 'title'});
  rejected(() => bounded.createDocument(JSON.stringify(wide)));
  wide.nodes.pop();
  assert.ok(bounded.createDocument(JSON.stringify(wide)));
});

test('invalid URL ports produce generic errors with no native input metadata', () => {
  for (const value of ['https://example.invalid:99999/PRIVATE_SENTINEL',
    'https://999.999.999.999/PRIVATE_SENTINEL']) {
    const catalog = clone(fixture.catalog); catalog.urls[0].value = value;
    let error;
    try { E.createPublicExtractor(JSON.stringify(catalog)); } catch (e) { error = e; }
    assert.ok(error instanceof E.ExtractionError);
    assert.equal(error.input, undefined);
    assert.equal(error.cause, undefined);
    assert.ok(!String(error).includes('PRIVATE_SENTINEL'));
    assert.ok(!JSON.stringify(error).includes('PRIVATE_SENTINEL'));
  }
});

test('malformed, deeply nested, scalar, unknown-key and invalid-Unicode inputs fail', () => {
  const {extractor} = setup();
  for (const raw of ['{', 'null', 'true', '[]', '"html"', '['.repeat(10000) + ']'.repeat(10000)]) {
    rejected(() => extractor.createDocument(raw));
  }
  for (const value of ['', '\ud800', '\udfff', '\u0000', ' \n\t ']) {
    const catalog = clone(fixture.catalog); catalog.texts[0].value = value;
    rejected(() => E.createPublicExtractor(JSON.stringify(catalog)));
  }
  const catalog = clone(fixture.catalog);
  catalog.texts.push(clone(catalog.texts[0]));
  rejected(() => E.createPublicExtractor(JSON.stringify(catalog)));
});

test('runtime context fails closed; invalid captures do not consume capture IDs', () => {
  const {capture} = setup();
  for (const delta of [{enabled: false}, {site_permission: 'denied'}, {private_context: true},
    {tab_role: 'foreground'}, {safe: true}, {task_id: ''}, {captured_at_ms: -1}]) {
    assert.throws(() => capture({...context(), ...delta}));
  }
  const result = capture();
  assert.ok(result.snapshot.id.endsWith('.c1.s'));
  assert.equal(result.duplicate, false);
});

test('immutable results cannot corrupt subsequent state; normalization is deterministic', () => {
  const {capture} = setup(), result = capture();
  assert.throws(() => { result.tables[0].rows[0][0] = 'PRIVATE'; }, TypeError);
  assert.throws(() => { result.snapshot.elements.push({}); }, TypeError);
  assert.equal(capture().tables[0].rows[0][0], 'Due');
  const catalog = clone(fixture.catalog); catalog.texts[0].value = '  A\n\t public 😀 title  ';
  const extractor = E.createPublicExtractor(JSON.stringify(catalog));
  const doc = extractor.createDocument(JSON.stringify(fixture.manifest));
  assert.equal(extractor.capture(doc, JSON.stringify(context())).observation.title, 'A public 😀 title');
});

test('browser-style module loading needs no document, browser APIs, timers or network', () => {
  const sandbox = {crypto: webcrypto, URL};
  vm.createContext(sandbox);
  for (const name of ['contracts.js', 'page-extractor.js']) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'browser-extension/shared', name), 'utf8'), sandbox);
  }
  const extractor = sandbox.WispPageExtractor.createPublicExtractor(JSON.stringify(fixture.catalog));
  const doc = extractor.createDocument(JSON.stringify(fixture.manifest));
  assert.equal(extractor.capture(doc, JSON.stringify(context())).headings[0].text, 'Assignment 4');
});
