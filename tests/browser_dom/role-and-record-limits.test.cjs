'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { createPageExtractor } = require('../../browser-extension/shared/page-extractor.js');
const { makeDocument, fakeRuntime } = require('./synthetic-dom.cjs');
const element = (tag, children = [], attrs = {}) => ({ tag, children, attrs });

test('draft privacy examines every bounded ARIA fallback token conservatively', () => {
  for (const draftRole of ['textbox', 'searchbox', 'combobox', 'spinbutton']) {
    for (const prefix of ['unknown', 'roletype', 'widget', 'button']) {
      const document = makeDocument([element('div', [
        'SECRET_DRAFT', element('h1', ['SECRET_HEADING']),
        element('a', ['SECRET_LINK'], { href: '/draft' }), element('button', ['SECRET_BUTTON']),
      ], { role: `${prefix}\t\n\f${draftRole.toUpperCase()}` })]);
      const result = createPageExtractor(document).collect();
      assert.equal(result.text, '', `${prefix} ${draftRole}`);
      assert.equal(result.headings.length + result.links.length, 0);
      assert.ok(result.coverage.reasons.includes('draft-values-excluded'));
      assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
    }
  }
});

test('fallback draft roles cannot leak through label references or scoped ancestor scans', () => {
  const document = makeDocument([
    element('div', [element('p', ['SECRET_DRAFT'], { id: 'inside' })], { id: 'draft', role: 'unknown textbox' }),
    element('button', [], { 'aria-labelledby': 'draft', 'aria-label': 'SECRET_FALLBACK' }),
  ]);
  const result = createPageExtractor(document).collect();
  assert.equal(result.controls[0].label, '');
  assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
  const scoped = createPageExtractor(document, { root: document.find('inside') }).collect();
  assert.equal(scoped.text, '');
  assert.ok(scoped.coverage.reasons.includes('root-ancestor-excluded'));
});

test('fallback role privacy retains the raw role boundary and exact-token matching', () => {
  const roleAtLimit = `${'x'.repeat(248)} textbox`;
  assert.equal(roleAtLimit.length, 256);
  const atLimit = createPageExtractor(makeDocument([
    element('div', ['SECRET_DRAFT'], { role: roleAtLimit }),
  ]), { maxStringChars: 1 }).collect();
  assert.equal(atLimit.text, '');
  assert.ok(atLimit.coverage.reasons.includes('draft-values-excluded'));
  assert.ok(!atLimit.coverage.reasons.includes('privacy-metadata-excluded'));
  const overLimit = createPageExtractor(makeDocument([
    element('div', ['SECRET_DRAFT'], { role: `x${roleAtLimit}` }),
  ]), { maxStringChars: 1 }).collect();
  assert.equal(overLimit.text, '');
  assert.ok(overLimit.coverage.reasons.includes('privacy-metadata-excluded'));
  const publicContent = createPageExtractor(makeDocument([
    element('p', ['Public'], { role: 'unknown nottextbox textbox-like' }),
  ])).collect();
  assert.equal(publicContent.text, 'Public');
});

test('mutation into a fallback draft role removes text and subsequent draft mutations dedupe', () => {
  const runtime = fakeRuntime();
  const document = makeDocument([element('div', ['Public'], { role: 'region' })]);
  const snapshots = [];
  const watcher = createPageExtractor(document).observe(value => snapshots.push(value), runtime);
  document.body.children[0].setAttribute('role', 'unknown textbox');
  document.body.children[0].childNodes[0].nodeValue = 'SECRET_DRAFT';
  runtime.mutate(); runtime.tick(50);
  assert.equal(snapshots.length, 2);
  assert.equal(snapshots[1].text, '');
  assert.ok(snapshots[1].coverage.reasons.includes('draft-values-excluded'));
  document.body.children[0].childNodes[0].nodeValue = 'SECRET_CHANGED';
  runtime.mutate(); runtime.tick(50);
  assert.equal(snapshots.length, 2);
  assert.doesNotMatch(JSON.stringify(snapshots), /SECRET_/u);
  watcher.stop();
});

test('a full record budget avoids repeated long-label work on collection and rescans', () => {
  const runtime = fakeRuntime();
  const document = makeDocument([
    element('span', Array.from({ length: 1000 }, () => 'a'.repeat(98)), { id: 'shared' }),
    ...Array.from({ length: 18000 }, () => element('button', [], { 'aria-labelledby': 'shared' })),
  ]);
  let labelReads = 0;
  for (const button of document.body.children.slice(1)) {
    const getAttribute = button.getAttribute;
    button.getAttribute = function (name) {
      if (name === 'aria-labelledby' || name === 'aria-label') labelReads++;
      return getAttribute.call(this, name);
    };
  }
  const snapshots = [];
  const watcher = createPageExtractor(document, { maxRecords: 1, maxStringChars: 10 }).observe(value => snapshots.push(value), runtime);
  assert.equal(snapshots[0].controls.length, 1);
  assert.equal(snapshots[0].controls[0].label, 'a'.repeat(10));
  assert.equal(labelReads, 1);
  assert.ok(snapshots[0].coverage.reasons.includes('record-limit'));
  runtime.mutate(); runtime.tick(50);
  assert.equal(labelReads, 2);
  assert.equal(snapshots.length, 1);
  watcher.stop();
});

test('record budgets apply before a second semantic payload on the same element', () => {
  const document = makeDocument([element('h1', ['Public heading'], { role: 'button', 'aria-label': 'Unused label' })]);
  let labelReads = 0;
  const node = document.body.children[0];
  const getAttribute = node.getAttribute;
  node.getAttribute = function (name) {
    if (name === 'aria-labelledby' || name === 'aria-label') labelReads++;
    return getAttribute.call(this, name);
  };
  const result = createPageExtractor(document, { maxRecords: 1 }).collect();
  assert.equal(result.headings.length, 1);
  assert.equal(result.controls.length, 0);
  assert.equal(labelReads, 0);
  assert.ok(result.coverage.reasons.includes('record-limit'));
});

test('rejected link records never parse destinations after the record budget is full', () => {
  let parses = 0;
  class CountedURL extends URL { constructor(...args) { parses++; super(...args); } }
  const context = vm.createContext({ module: { exports: {} }, URL: CountedURL });
  vm.runInContext(readFileSync(require.resolve('../../browser-extension/shared/page-extractor.js'), 'utf8'), context);
  const document = makeDocument([
    element('button', ['First']), element('a', ['Second'], { href: '/second' }),
  ]);
  const result = context.module.exports.createPageExtractor(document, { maxRecords: 1 }).collect();
  assert.equal(result.controls.length, 1);
  assert.equal(result.links.length, 0);
  assert.equal(parses, 0);
  assert.ok(result.coverage.reasons.includes('record-limit'));
});

test('an emitted table retains its caption at the record limit without false truncation', () => {
  const result = createPageExtractor(makeDocument([
    element('table', [element('caption', ['Public caption'])]),
  ]), { maxRecords: 1 }).collect();
  assert.equal(result.tables.length, 1);
  assert.equal(result.tables[0].caption, 'Public caption');
  assert.ok(!result.coverage.reasons.includes('record-limit'));
});
