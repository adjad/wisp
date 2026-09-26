'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createPageExtractor } = require('../../browser-extension/shared/page-extractor.js');
const { makeDocument, fakeRuntime } = require('./synthetic-dom.cjs');
const element = (tag, children = [], attrs = {}) => ({ tag, children, attrs });

function shadowFixture(slotAttributes = {}) {
  const document = makeDocument([
    element('p', ['Public before']),
    element('a', [
      'UNSLOTTED_SECRET',
      element('section', [
        element('h1', ['SLOTTED_HIDDEN_SECRET'], { id: 'heading' }),
        element('p', ['SLOTTED_PRIVATE_SECRET'], { id: 'nested' }),
        element('button', ['CONTROL_SECRET']),
        element('table', [element('tr', [element('td', ['TABLE_SECRET'])])]),
      ], { slot: 'private', id: 'assigned' }),
      element('input', [], { 'aria-label': 'LABEL_SECRET' }),
      element('label', ['EXPLICIT_LABEL_SECRET'], { for: 'outside' }),
    ], { id: 'host', href: '/HOST_SECRET', role: 'button', 'aria-label': 'HOST_LABEL_SECRET' }),
    element('button', [], { 'aria-labelledby': 'heading nested host', 'aria-label': 'FALLBACK_SECRET' }),
    element('input', [], { id: 'outside' }),
    element('p', ['Public after']),
  ]);
  const host = document.find('host');
  const slot = document.build(element('slot', [], { name: 'private', ...slotAttributes }));
  const shadow = { nodeType: 11, host, childNodes: [slot] };
  host.shadowRoot = shadow;
  document.find('assigned').assignedSlot = slot;
  return { document, host, slot, shadow };
}

function assertExcluded(snapshot) {
  assert.doesNotMatch(JSON.stringify(snapshot), /SECRET/u);
  assert.ok(snapshot.coverage.reasons.includes('shadow-dom-unread'));
  assert.ok(snapshot.coverage.reasons.includes('shadow-content-excluded'));
}

test('observable shadow hosts exclude all metadata and slotted/unslotted light content regardless of geometry', () => {
  for (const attributes of [{ 'aria-hidden': 'true' }, { 'data-private': '' }, {}]) {
    const { document } = shadowFixture(attributes);
    const result = createPageExtractor(document).collect();
    assert.equal(result.text, 'Public before Public after');
    assert.equal(result.headings.length + result.tables.length + result.links.length, 0);
    assert.equal(result.controls.length, 2);
    assert.ok(result.controls.every(control => control.label === ''));
    assertExcluded(result);
  }
});

test('scoped host, assigned child and nested roots cannot bypass the shadow exclusion', () => {
  const { document } = shadowFixture({ 'data-private': '' });
  for (const id of ['host', 'assigned', 'nested']) {
    const result = createPageExtractor(document, { root: document.find(id) }).collect();
    assert.equal(result.text, '');
    assert.equal(result.controls.length + result.links.length + result.headings.length, 0);
    assertExcluded(result);
    if (id !== 'host') assert.ok(result.coverage.reasons.includes('root-ancestor-excluded'));
  }
});

test('observable slot assignments fail closed even without an inspectable host root', () => {
  const { document, host } = shadowFixture({ 'aria-hidden': 'true' });
  host.shadowRoot = null;
  // Only the assigned subtree is observable here; this fixture does not claim
  // to model closed shadow roots, for which browsers may hide assignedSlot too.
  const result = createPageExtractor(document, { root: document.find('assigned') }).collect();
  assert.equal(result.text, '');
  assertExcluded(result);
  const nested = createPageExtractor(document, { root: document.find('nested') }).collect();
  assert.equal(nested.text, '');
  assertExcluded(nested);
});

test('exclusion does not inspect shadow internals or invoke host/descendant geometry', () => {
  const { document, host, shadow, slot } = shadowFixture({ 'data-private': '' });
  let forbiddenReads = 0;
  const forbid = () => { forbiddenReads++; throw new Error('Untrusted shadow/layout inspection'); };
  Object.defineProperty(shadow, 'childNodes', { get: forbid });
  Object.defineProperty(slot, 'parentElement', { get: forbid });
  const excludedNodes = [host, ...['assigned', 'heading', 'nested'].map(id => document.find(id))];
  for (const node of excludedNodes) { node.getClientRects = forbid; node.getAttribute = forbid; }
  const result = createPageExtractor(document).collect();
  assert.equal(forbiddenReads, 0);
  assertExcluded(result);
});

test('shadow-host light content is skipped before consuming a tiny collection budget', () => {
  const document = makeDocument([
    { tag: 'section', shadow: true, children: ['SECRET'.repeat(1_000_000)] },
    element('p', ['OK']),
  ]);
  const result = createPageExtractor(document, { maxTextChars: 2, maxStringChars: 2, maxNodes: 4 }).collect();
  assert.equal(result.text, 'OK');
  assert.ok(!result.coverage.reasons.includes('text-input-limit'));
  assertExcluded(result);
});

test('shadow descendant mutations dedupe and explicit flush removes newly shadow-hosted text', () => {
  const runtime = fakeRuntime();
  const { document } = shadowFixture({ 'aria-hidden': 'true' });
  const snapshots = [];
  const watcher = createPageExtractor(document).observe(value => snapshots.push(value), runtime);
  document.find('nested').childNodes[0].nodeValue = 'CHANGED_SECRET';
  for (let i = 0; i < 4; i++) runtime.mutate();
  runtime.tick(50);
  assert.equal(snapshots.length, 1);
  assertExcluded(snapshots[0]);
  watcher.stop();

  // attachShadow itself is not a light-DOM MutationRecord; the caller must
  // invalidate explicitly. No browser event delivery is simulated here.
  const other = makeDocument([element('section', ['Previously public'])]);
  const later = [];
  const otherWatcher = createPageExtractor(other).observe(value => later.push(value), runtime);
  other.body.children[0].shadowRoot = { host: other.body.children[0] };
  otherWatcher.flush();
  assert.equal(later.length, 2);
  assert.equal(later[1].text, '');
  assertExcluded(later[1]);
  otherWatcher.stop();
  assert.equal(runtime.pending(), 0);
});


test('observable text-node slot assignments are excluded before reading text or geometry', () => {
  const document = makeDocument(['ASSIGNED_TEXT_SECRET', element('p', ['Public'])]);
  const node = document.body.childNodes[0];
  node.assignedSlot = {};
  let reads = 0;
  Object.defineProperty(node, 'nodeValue', { get() { reads++; throw new Error('Excluded text read'); } });
  const result = createPageExtractor(document).collect();
  assert.equal(result.text, 'Public');
  assert.equal(reads, 0);
  assertExcluded(result);
});
