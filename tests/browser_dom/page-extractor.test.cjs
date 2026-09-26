'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createPageExtractor } = require('../../browser-extension/shared/page-extractor.js');
const { makeDocument, fakeRuntime } = require('./synthetic-dom.cjs');
const readingPage = require('../../test_fixtures/browser_pages/reading-page.cjs');
const extract = (spec, options) => createPageExtractor(makeDocument(spec), options).collect();
const element = (tag, children = [], attrs = {}) => ({ tag, children, attrs });

test('extracts visible prose, headings, table structure, destinations and metadata-only controls', () => {
  const document = makeDocument(readingPage);
  // A read of either property is a regression, even if subsequently redacted.
  for (const id of ['search', 'draft']) for (const property of ['value', 'defaultValue', 'innerHTML', 'outerHTML', 'textContent']) {
    Object.defineProperty(document.find(id), property, { get() { throw new Error(`Forbidden ${property} read`); } });
  }
  const result = createPageExtractor(document).collect();
  assert.deepEqual(result.headings.map(({ level, text }) => ({ level, text })), [{ level: 1, text: 'Quarterly results' }]);
  assert.match(result.text, /Revenue grew 12% this quarter\./u);
  assert.equal(result.links[0].destination, 'https://fixture.invalid/report');
  assert.equal(result.links[0].text, 'Read report');
  assert.equal(result.tables[0].caption, 'Revenue');
  assert.deepEqual(result.tables[0].rows.map(row => row.cells.map(cell => [cell.header, cell.text])), [
    [[true, 'Region'], [true, 'Total']], [[false, 'West'], [false, '$12']],
  ]);
  assert.deepEqual(result.controls.map(({ kind, label, disabled }) => ({ kind, label, disabled })), [
    { kind: 'search', label: 'Search reports', disabled: false },
    { kind: 'textarea', label: 'Draft note', disabled: false },
    { kind: 'button', label: 'Export report', disabled: true },
  ]);
  assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
  assert.equal(result.coverage.complete, false);
});

test('ancestor exclusions cannot leak through semantics or label references', () => {
  const exclusions = [
    { attrs: { hidden: '' } }, { attrs: { 'aria-hidden': 'true' } }, { attrs: { inert: '' } },
    { attrs: { 'data-private': '' } }, { attrs: { 'data-draft': '' } }, { attrs: { 'data-wisp-private': '' } },
    { attrs: { contenteditable: '' } }, { attrs: { contenteditable: 'plaintext-only' } },
    { style: { display: 'none' } }, { style: { visibility: 'hidden' } }, { style: { visibility: 'collapse' } },
    { style: { opacity: '0' } }, { style: { contentVisibility: 'hidden' } },
  ];
  for (const exclusion of exclusions) {
    const result = extract([
      { tag: 'div', ...exclusion, children: [element('h2', ['SECRET_HEADING'], { id: 'secret-label' }), element('a', ['SECRET_LINK'], { href: '/secret' }), element('button', ['SECRET_BUTTON'])] },
      element('input', [], { 'aria-labelledby': 'secret-label', 'aria-label': 'SECRET_FALLBACK' }),
    ]);
    assert.equal(result.text, '');
    assert.equal(result.controls[0].label, '');
    assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
  }
});

test('editable values, selection options, and secret autocomplete fields are omitted', () => {
  const result = extract([
    element('select', [element('option', ['SECRET_OPTION'])], { 'aria-label': 'Choose region' }),
    element('div', ['SECRET_RICH'], { role: 'textbox', 'aria-label': 'Message' }),
    element('input', [], { type: 'hidden', value: 'SECRET_HIDDEN' }),
    element('input', [], { autocomplete: 'one-time-code', 'aria-label': 'SECRET_OTP' }),
    element('input', [], { autocomplete: 'new-password', 'aria-label': 'SECRET_NEW_PASSWORD' }),
    element('textarea', ['SECRET_DRAFT'], { id: 'draft-label' }),
    element('button', ['Public button'], { 'aria-labelledby': 'draft-label' }),
  ]);
  assert.equal(result.controls.length, 4);
  assert.deepEqual(result.controls.map(control => control.label), ['Choose region', 'Message', '', '']);
  assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
});

test('labels include explicit, wrapping, safe ordered references and button text', () => {
  const result = extract([
    element('span', ['First'], { id: 'one' }), element('span', ['Second'], { id: 'two' }),
    element('input', [], { 'aria-labelledby': 'two one missing' }),
    element('label', ['Remember me', element('input', [], { type: 'checkbox', value: 'SECRET_CHECKBOX' })]),
    element('button', ['Send'], { 'aria-disabled': 'true' }),
  ]);
  assert.deepEqual(result.controls.map(control => control.label), ['Second First', 'Remember me', 'Send']);
  assert.equal(result.controls[2].disabled, true);
  assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
});

test('closed details expose only the first summary and retain rendered offscreen text', () => {
  const result = extract([
    element('details', [element('summary', ['Overview']), element('p', ['SECRET_CLOSED']), element('summary', ['SECRET_SECOND'])]),
    { tag: 'p', children: ['Below the fold'], rects: [{ width: 100, height: 20, y: 50000 }] },
    { tag: 'div', rects: [], children: [{ tag: 'p', children: ['Visible child'] }] },
    { tag: 'p', rects: [], children: ['SECRET_ZERO_GEOMETRY'] },
  ]);
  assert.equal(result.text, 'Overview Below the fold Visible child');
});

test('link destinations resolve locally and discard credentials, queries and fragments', () => {
  const result = extract([
    element('a', ['Credential URL'], { href: 'https://SECRET_USER:SECRET_PASSWORD@example.invalid/path?SECRET_TOKEN#SECRET_HASH' }),
    element('a', ['Fragment'], { href: '#SECRET_FRAGMENT' }),
    ...['javascript:SECRET_JS()', 'data:text/plain,SECRET_DATA', 'mailto:SECRET_EMAIL@example.invalid', 'https://['].map(href => element('a', ['Excluded'], { href })),
  ]);
  assert.deepEqual(result.links.map(link => link.destination), ['https://example.invalid/path', 'https://fixture.invalid/articles/current', null, null, null, null]);
  assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
});

test('identity is stable for a node, opaque, shared per Document and different after replacement/navigation', () => {
  const document = makeDocument([element('button', ['Sensitive DOM identity'], { id: 'business-secret-id' })]);
  const first = createPageExtractor(document);
  const before = first.collect();
  assert.equal(first.collect().controls[0].id, before.controls[0].id);
  assert.equal(createPageExtractor(document).collect().controls[0].id, before.controls[0].id);
  const old = document.body.childNodes.pop(); old.parentElement = null;
  document.body.append(document.build(element('button', ['Sensitive DOM identity'], { id: 'business-secret-id' })));
  assert.notEqual(first.collect().controls[0].id, before.controls[0].id);
  const navigated = extract([element('button', ['Sensitive DOM identity'], { id: 'business-secret-id' })]);
  assert.notEqual(navigated.documentId, before.documentId);
  assert.notEqual(navigated.controls[0].id, before.controls[0].id);
  assert.match(before.controls[0].id, /^d-[0-9a-f-]{36}:e\d+$/u);
});

test('reports unread rendered surfaces and shadow roots without inspecting them', () => {
  const document = makeDocument([
    ...['iframe', 'canvas', 'svg', 'object', 'video'].map(name => element(name, ['SECRET_SURFACE'])),
    { tag: 'section', shadow: true },
  ]);
  Object.defineProperty(document.body.children[0], 'contentDocument', { get() { throw new Error('Frame access'); } });
  const result = createPageExtractor(document).collect();
  assert.equal(result.text, '');
  for (const reason of ['frames-unread', 'rendered-surface-unread', 'shadow-dom-unread', 'closed-shadow-roots-unobservable']) assert.ok(result.coverage.reasons.includes(reason));
});

test('fails closed without visibility or geometry, and on an editable document', () => {
  const document = makeDocument([element('p', ['SECRET_UNCERTAIN'])]);
  document.defaultView.getComputedStyle = () => { throw new Error('Unavailable'); };
  const collector = createPageExtractor(document);
  assert.equal(collector.collect().text, '');
  assert.ok(collector.collect().coverage.reasons.includes('visibility-unavailable'));
  document.defaultView.getComputedStyle = () => ({ opacity: '1' });
  document.createRange = () => { throw new Error('No geometry'); };
  assert.equal(collector.collect().text, '');
  assert.ok(collector.collect().coverage.reasons.includes('geometry-unavailable'));
  document.designMode = 'on';
  assert.ok(collector.collect().coverage.reasons.includes('editable-document-excluded'));
});

test('scope validation and ancestor policy prevent draft or hidden root bypass', () => {
  const document = makeDocument([element('div', [element('p', ['SECRET_SCOPED'], { id: 'inside' })], { contenteditable: 'true' })]);
  assert.equal(createPageExtractor(document, { root: document.find('inside') }).collect().text, '');
  assert.throws(() => createPageExtractor(document, { root: makeDocument().body }), /root/u);
  const detached = document.build(element('p', ['SECRET_DETACHED']));
  assert.ok(createPageExtractor(document, { root: detached }).collect().coverage.reasons.includes('root-unavailable'));
  const closed = makeDocument([element('details', [element('summary', ['Allowed'], { id: 'summary' }), element('p', ['SECRET_CLOSED'], { id: 'inside' })])]);
  assert.equal(createPageExtractor(closed, { root: closed.find('summary') }).collect().text, 'Allowed');
  assert.equal(createPageExtractor(closed, { root: closed.find('inside') }).collect().text, '');
});

test('all collection budgets report incomplete output', () => {
  const specs = [element('h1', ['ABCDEFGHIJK']), element('button', ['Second']), element('p', [element('span', ['Deep'])])];
  for (const [options, reason] of [
    [{ maxNodes: 2 }, 'node-limit'], [{ maxDepth: 1 }, 'depth-limit'], [{ maxTextChars: 4 }, 'text-limit'],
    [{ maxRecords: 1 }, 'record-limit'], [{ maxStringChars: 4 }, 'string-limit'],
  ]) assert.ok(extract(specs, options).coverage.reasons.includes(reason), reason);
  assert.equal(extract(specs, { maxTextChars: 4 }).text, 'ABCD');
  assert.equal(extract(specs, { maxRecords: 1 }).controls.length, 0);
});

test('nested tables retain independent rows without contaminating parent cells', () => {
  const result = extract([element('table', [element('tr', [element('td', ['Outer', element('table', [element('tr', [element('td', ['Inner'])])])])])])]);
  assert.equal(result.tables.length, 2);
  assert.equal(result.tables[0].rows[0].cells[0].text, 'Outer');
  assert.equal(result.tables[1].rows[0].cells[0].text, 'Inner');
});

test('mutation bursts coalesce, unchanged output dedupes, and no old values are serialized', () => {
  const runtime = fakeRuntime();
  const document = makeDocument([element('p', ['Initial']), element('input', [], { value: 'SECRET_VALUE' })]);
  const snapshots = [];
  const watcher = createPageExtractor(document).observe(snapshot => snapshots.push(snapshot), { ...runtime, debounceMs: 20, maxWaitMs: 80 });
  assert.equal(snapshots.length, 1);
  assert.equal(runtime.observers[0].target, document);
  assert.equal(runtime.observers[0].options.attributeOldValue, undefined);
  document.body.children[0].childNodes[0].nodeValue = 'Changed';
  for (let i = 0; i < 5; i++) runtime.mutate([{ oldValue: 'SECRET_OLD' }]);
  runtime.tick(19); assert.equal(snapshots.length, 1);
  runtime.tick(1); assert.equal(snapshots.length, 2);
  assert.equal(snapshots[1].text, 'Changed');
  document.body.children[1].setAttribute('value', 'SECRET_NEW');
  runtime.mutate(); runtime.tick(20);
  assert.equal(snapshots.length, 2);
  watcher.flush(); assert.equal(snapshots.length, 2);
  assert.doesNotMatch(JSON.stringify(snapshots), /SECRET_/u);
  watcher.stop();
});

test('continuous mutations flush by max-wait, stop cancels work and is idempotent', () => {
  const runtime = fakeRuntime();
  const document = makeDocument([element('p', ['Initial'])]);
  const snapshots = [];
  const watcher = createPageExtractor(document).observe(value => snapshots.push(value), { ...runtime, debounceMs: 30, maxWaitMs: 80 });
  for (let i = 0; i < 4; i++) {
    document.body.children[0].childNodes[0].nodeValue = `Change ${i}`;
    runtime.mutate(); runtime.tick(20);
  }
  assert.equal(snapshots.length, 2);
  assert.equal(snapshots[1].text, 'Change 3');
  runtime.mutate(); assert.equal(runtime.pending(), 2);
  watcher.stop(); watcher.stop(); watcher.flush(); runtime.tick(100);
  assert.equal(runtime.pending(), 0);
  assert.equal(runtime.observers[0].disconnected, true);
  assert.equal(snapshots.length, 2);
});

test('observer covers body replacement and disconnects when initial callback fails', () => {
  const runtime = fakeRuntime();
  const document = makeDocument([element('p', ['Old'])]);
  const snapshots = [];
  const watcher = createPageExtractor(document).observe(value => snapshots.push(value), runtime);
  document.documentElement.childNodes = [];
  document.body.parentElement = null;
  document.body = document.documentElement.append(document.build(element('body', [element('p', ['New'])])));
  runtime.mutate(); runtime.tick(50);
  assert.equal(snapshots[1].text, 'New');
  watcher.stop();
  assert.throws(() => createPageExtractor(document).observe(() => { throw new Error('Consumer error'); }, runtime), /Consumer error/u);
  assert.equal(runtime.observers[1].disconnected, true);
});

test('control kinds are allowlisted and nonrendered controls are not described', () => {
  const result = extract([
    element('button', ['Public'], { role: 'SECRET_ROLE' }),
    element('input', [], { type: 'SECRET_TYPE', name: 'SECRET_NAME', placeholder: 'SECRET_PLACEHOLDER' }),
    { tag: 'button', attrs: { 'aria-label': 'SECRET_GEOMETRY' }, rects: [] },
  ]);
  assert.deepEqual(result.controls.map(control => control.kind), ['button', 'text']);
  assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
});

test('long labels and destinations expose explicit coverage limits', () => {
  const labels = Array.from({ length: 33 }, (_, index) => element('span', [`Label ${index}`], { id: `label-${index}` }));
  const result = extract([
    ...labels,
    element('input', [], { 'aria-labelledby': labels.map(label => label.attrs.id).join(' ') }),
    element('a', ['Long URL'], { href: `https://fixture.invalid/${'x'.repeat(1500)}` }),
  ]);
  assert.ok(result.coverage.reasons.includes('label-limit'));
  assert.ok(result.coverage.reasons.includes('link-destination-excluded'));
  assert.equal(result.links[0].destination, null);
  assert.doesNotMatch(result.controls[0].label, /Label 32/u);
});

test('hidden mutation bursts dedupe and visibility changes produce sanitized snapshots', () => {
  const runtime = fakeRuntime();
  const document = makeDocument([element('section', [element('p', ['SECRET_HIDDEN'])], { hidden: '' })]);
  const snapshots = [];
  const watcher = createPageExtractor(document).observe(value => snapshots.push(value), runtime);
  document.body.children[0].children[0].childNodes[0].nodeValue = 'SECRET_CHANGED';
  runtime.mutate(); runtime.tick(50);
  assert.equal(snapshots.length, 1);
  document.body.children[0].children[0].childNodes[0].nodeValue = 'Public update';
  document.body.children[0].removeAttribute('hidden');
  runtime.mutate(); runtime.tick(50);
  assert.equal(snapshots.length, 2);
  assert.equal(snapshots[1].text, 'Public update');
  assert.doesNotMatch(JSON.stringify(snapshots), /SECRET_/u);
  watcher.stop();
});

test('deep and wide synthetic trees terminate at collection budgets', () => {
  const document = makeDocument();
  let parent = document.body;
  for (let i = 0; i < 300; i++) parent = parent.append(document.build(element('div')));
  parent.append(document.build('Too deep'));
  const deep = createPageExtractor(document).collect();
  assert.equal(deep.text, '');
  assert.ok(deep.coverage.reasons.includes('depth-limit'));
  const wide = makeDocument(Array.from({ length: 10000 }, () => element('p', ['Bounded'])));
  const result = createPageExtractor(wide, { maxNodes: 100 }).collect();
  assert.ok(result.coverage.reasons.includes('node-limit'));
  assert.ok(result.text.length < 1000);
});
