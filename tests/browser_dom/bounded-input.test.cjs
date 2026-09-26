'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { makeDocument, fakeRuntime } = require('./synthetic-dom.cjs');
const source = readFileSync(require.resolve('../../browser-extension/shared/page-extractor.js'), 'utf8');
const element = (tag, children = [], attrs = {}) => ({ tag, children, attrs });
const large = 'x '.repeat(1_000_000);

// Deterministic work-bound oracle, not a timing/RSS benchmark. The guard runs
// only in an isolated realm and records violations even if collection catches
// an exception. Large input creation is deliberately outside the measured work.
function guardedCollector() {
  const stats = { violations: [], maximum: 0, transformedChars: 0, urlCalls: 0 };
  function check(operation, value) {
    if (typeof value !== 'string') return;
    stats.maximum = Math.max(stats.maximum, value.length);
    stats.transformedChars += value.length;
    if (value.length > 2048) {
      stats.violations.push(`${operation}: ${value.length}`);
      throw new Error('Oversized string reached an expensive operation');
    }
  }
  class GuardedURL extends URL {
    constructor(input, base) {
      stats.urlCalls++;
      check('URL input', input); check('URL base', base);
      super(input, base);
    }
  }
  const context = vm.createContext({ module: { exports: {} }, URL: GuardedURL, check });
  vm.runInContext(`
    for (const name of ['replace', 'split', 'trim', 'toLowerCase', 'toUpperCase']) {
      const original = String.prototype[name];
      String.prototype[name] = function (...args) {
        check(name, String(this));
        return original.apply(this, args);
      };
    }
    for (const name of ['has', 'get', 'set']) {
      const original = Map.prototype[name];
      Map.prototype[name] = function (key, ...args) {
        check('Map.' + name, key);
        return original.call(this, key, ...args);
      };
    }
  `, context);
  vm.runInContext(source, context);
  return { createPageExtractor: context.module.exports.createPageExtractor, stats };
}

function assertBounded(stats) {
  assert.deepEqual(stats.violations, []);
  assert.ok(stats.maximum <= 2048);
}

test('8M text is bounded before normalization and text geometry with a one-character budget', () => {
  const raw = 'A '.repeat(4_000_000);
  const document = makeDocument([element('p', [raw])]);
  const measuredRanges = [];
  const originalRange = document.createRange;
  document.createRange = () => {
    const range = originalRange();
    const setEnd = range.setEnd;
    range.setEnd = (node, offset) => { measuredRanges.push(offset); setEnd(node, offset); };
    return range;
  };
  const { createPageExtractor, stats } = guardedCollector();
  const result = createPageExtractor(document, { maxTextChars: 1, maxStringChars: 1 }).collect();
  assert.equal(result.text, 'A');
  assert.ok(result.coverage.reasons.includes('text-input-limit'));
  assert.ok(result.coverage.reasons.includes('text-limit'));
  assert.deepEqual(measuredRanges, [1]);
  assertBounded(stats);
});

test('raw whitespace scanning shares a total text budget across nodes', () => {
  const document = makeDocument([element('p', [' '.repeat(100), 'A'.repeat(100), 'B'.repeat(100)])]);
  let ranges = 0;
  const originalRange = document.createRange;
  document.createRange = () => { ranges++; return originalRange(); };
  const { createPageExtractor, stats } = guardedCollector();
  const result = createPageExtractor(document, { maxTextChars: 1, maxStringChars: 1 }).collect();
  assert.equal(result.text, '');
  assert.equal(ranges, 1);
  assert.ok(result.coverage.reasons.includes('text-input-limit'));
  assertBounded(stats);
});

test('oversized ARIA labels/references are skipped before normalization or splitting without fallback', () => {
  const document = makeDocument([
    element('button', [], { 'aria-labelledby': large, 'aria-label': 'SECRET_FALLBACK' }),
    element('button', [], { 'aria-label': large }),
  ]);
  const { createPageExtractor, stats } = guardedCollector();
  const result = createPageExtractor(document, { maxTextChars: 1, maxStringChars: 1 }).collect();
  assert.equal(result.controls.length, 2);
  assert.ok(result.controls.every(control => control.label === ''));
  assert.ok(result.coverage.reasons.includes('attribute-input-limit'));
  assert.ok(result.coverage.reasons.includes('label-limit'));
  assert.ok(result.coverage.reasons.includes('string-limit'));
  assert.doesNotMatch(JSON.stringify(result), /SECRET_FALLBACK/u);
  assertBounded(stats);
});

test('oversized privacy metadata excludes subtrees without normalizing safe-looking prefixes', () => {
  const cases = [
    ['role', `button ${large}textbox`], ['autocomplete', `off ${large}current-password`],
    ['type', `${large}password`], ['contenteditable', `false${large}`],
    ['aria-hidden', `false${large}`], ['aria-disabled', `false${large}`],
  ];
  const { createPageExtractor, stats } = guardedCollector();
  for (const [name, value] of cases) {
    const document = makeDocument([element('div', [element('h1', ['SECRET_DESCENDANT']), element('button', ['SECRET_CONTROL'])], { [name]: value })]);
    const result = createPageExtractor(document, { maxTextChars: 1, maxStringChars: 1 }).collect();
    assert.equal(result.text, '');
    assert.equal(result.controls.length, 0);
    assert.ok(result.coverage.reasons.includes('attribute-input-limit'), name);
    assert.ok(result.coverage.reasons.includes('privacy-metadata-excluded'), name);
    assert.doesNotMatch(JSON.stringify(result), /SECRET_/u);
  }
  assertBounded(stats);
});

test('large identifiers and label targets are rejected before hashing and never alias by prefix', () => {
  const document = makeDocument([
    element('label', ['Z'], { for: `${large}one` }),
    element('span', [], { id: `${large}one` }),
    element('input', [], { id: `${large}two` }),
    element('input', [], { 'aria-labelledby': `${large}two`, 'aria-label': 'SECRET_FALLBACK' }),
  ]);
  const { createPageExtractor, stats } = guardedCollector();
  const result = createPageExtractor(document, { maxTextChars: 1, maxStringChars: 1 }).collect();
  assert.equal(result.controls.length, 2);
  assert.ok(result.controls.every(control => control.label === ''));
  assert.ok(result.coverage.reasons.includes('attribute-input-limit'));
  assertBounded(stats);
});

test('oversized href and base URI never reach URL parsing', () => {
  const { createPageExtractor, stats } = guardedCollector();
  for (const document of [
    makeDocument([element('a', [], { href: `https://fixture.invalid/?${large}` })]),
    makeDocument([element('a', [], { href: '/' })], `https://fixture.invalid/${large}`),
  ]) {
    const result = createPageExtractor(document, { maxTextChars: 1, maxStringChars: 64 }).collect();
    assert.equal(result.links[0].destination, null);
    assert.ok(result.coverage.reasons.includes('url-input-limit'));
    assert.ok(result.coverage.reasons.includes('link-destination-excluded'));
  }
  assert.equal(stats.urlCalls, 0);
  const normal = createPageExtractor(makeDocument([element('a', [], { href: '/' })]), { maxStringChars: 64 }).collect();
  assert.equal(normal.links[0].destination, 'https://fixture.invalid/');
  assert.equal(stats.urlCalls, 1);
  assertBounded(stats);
});

test('page-controlled oversized tag names do not reach case conversion', () => {
  const document = makeDocument([element('x-widget', ['SECRET_CUSTOM'])]);
  // The DOM double uppercases names during construction; assign afterwards to
  // measure only the collector's handling of this otherwise legal DOM string.
  document.body.children[0].tagName = `X-${large}`;
  const { createPageExtractor, stats } = guardedCollector();
  const result = createPageExtractor(document).collect();
  assert.equal(result.text, '');
  assert.ok(result.coverage.reasons.includes('tag-name-input-limit'));
  assertBounded(stats);
});

test('mutation rescans retain early input bounds, coalescing, and unchanged-tail deduplication', () => {
  const runtime = fakeRuntime();
  const document = makeDocument([
    element('p', [`A${large}`]),
    element('button', [], { 'aria-labelledby': large, 'aria-label': 'SECRET_FALLBACK' }),
    element('a', [], { href: `https://fixture.invalid/${large}` }),
  ]);
  const { createPageExtractor, stats } = guardedCollector();
  const snapshots = [];
  const watcher = createPageExtractor(document, { maxTextChars: 1, maxStringChars: 1 }).observe(value => snapshots.push(value), runtime);
  const initialWork = stats.transformedChars;
  assert.equal(snapshots.length, 1);
  for (let i = 0; i < 5; i++) {
    document.body.children[0].childNodes[0].nodeValue = `A${large}${i}`;
    document.body.children[1].setAttribute('aria-labelledby', `${large}${i}`);
    document.body.children[2].setAttribute('href', `https://fixture.invalid/${large}${i}`);
    runtime.mutate();
  }
  runtime.tick(50);
  assert.equal(snapshots.length, 1);
  assert.ok(stats.transformedChars <= initialWork * 2 + 100);
  document.body.children[0].childNodes[0].nodeValue = `B${large}`;
  runtime.mutate(); runtime.tick(50);
  assert.equal(snapshots.length, 2);
  assert.equal(snapshots[1].text, 'B');
  assert.ok(snapshots.every(snapshot => snapshot.coverage.reasons.includes('text-input-limit') && snapshot.coverage.reasons.includes('attribute-input-limit') && snapshot.coverage.reasons.includes('url-input-limit')));
  assert.doesNotMatch(JSON.stringify(snapshots), /SECRET_/u);
  assert.equal(stats.urlCalls, 0);
  assertBounded(stats);
  watcher.stop();
  assert.equal(runtime.pending(), 0);
});
