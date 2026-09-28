'use strict';
// Synthetic D1 live-capture boundary tests. No browser, network or user state.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ROOT = path.resolve(__dirname, '../..');
const L = require('../../browser-extension/shared/live-acquisition.js');
const C = require('../../browser-extension/shared/contracts.js');
const D = require('../../test_fixtures/browser_pages/live/fake-dom.cjs');
const FIXTURES = path.join(ROOT, 'test_fixtures/browser_pages/live');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIXTURES, name + '.json'), 'utf8'));

function context(fx, overrides = {}) {
  return Object.assign({trigger: 'user_click', task_id: 'synthetic.task', tab_id: 7,
    document_id: 'doc.synthetic', url: fx.url, enabled: true, site_permission: 'granted',
    extension_incognito: false, native_private_context: false, tab_role: 'active'}, overrides);
}
function session(start = 1000) {
  let clock = start, n = 0;
  const live = L.createLiveCapture({now: () => clock,
    randomUUID: () => '00000000-0000-4000-8000-' + String(++n).padStart(12, '0')});
  return {live, tick: ms => { clock += ms; }};
}
function run(fx, {ctx = context(fx), options, s = session()} = {}) {
  const {document, window} = D.build(fx, options);
  const collected = L.collect(document, window);
  const grant = s.live.issueGrant(JSON.stringify(ctx));
  return {result: s.live.capture(grant, collected, JSON.stringify(ctx)), collected, document, s};
}
const code = (fn, expected) => assert.throws(fn, e => e instanceof L.LiveCaptureError && e.code === expected);

test('assignment page: rendered text, headings, table, links and A01 records', () => {
  const fx = fixture('assignment-page');
  const {result, document} = run(fx, {ctx: context(fx, {tab_role: 'background'})});
  assert.deepEqual(document.forbiddenReads, []);
  C.validate('SourceObservation', result.observation);
  C.validate('BrowserSnapshot', result.snapshot);
  assert.equal(result.snapshot.observation_id, result.observation.id);
  assert.equal(result.observation.source_url, fx.url);
  assert.equal(result.observation.revision, result.revision);
  assert.equal(result.observation.private_context, false);
  assert.match(result.observation.text, /^Assignment 4: Lab report$/m);
  assert.match(result.observation.text, /^Due \| For \| Available from \| Until$/m);
  assert.match(result.observation.text, /Follow the lab report rubric and include your data table\./);
  assert.deepEqual(result.headings.map(h => [h.level, h.text]), [[1, 'Assignment 4: Lab report'], [2, 'Instructions']]);
  assert.deepEqual(result.tables[0].rows[1], ['Oct 2 by 5pm', 'Everyone', 'Sep 25 at 12am', 'Oct 9 at 11:59pm']);
  const byLabel = Object.fromEntries(result.links.map(l => [l.label, l.url]));
  assert.equal(byLabel.Modules, 'https://canvas.course.invalid/courses/101/modules');
  assert.equal(byLabel['Next module item'], 'https://canvas.course.invalid/courses/101/modules/items/56');
  assert.equal(byLabel['Join live session'], null, 'token-bearing URL is withheld');
  assert.equal(byLabel['Show more'], null, 'javascript: URL is not carried');
  assert.equal(result.elements.length, result.snapshot.elements.length);
  assert.ok(result.elements.every(e => e.editable === false));
  assert.ok(result.elements.some(e => e.role === 'button' && e.label === 'Submit Assignment'));
  for (const block of result.blocks) assert.ok(block.id.startsWith(result.revision + '.n'));
  assert.equal(result.coverage.scope, 'rendered_visible');
  assert.equal(result.coverage.page_complete, false);
  assert.equal(result.coverage.truncated, false);
  assert.equal(result.coverage.urls_withheld, 2);
  assert.ok(!JSON.stringify(result).includes('SENTINEL'));
  assert.ok(Object.isFrozen(result) && Object.isFrozen(result.blocks[0]));
});

test('active-tab user read yields an observation only; elements stay A01-valid', () => {
  const fx = fixture('assignment-page');
  const {result} = run(fx);
  assert.equal(result.snapshot, null);
  assert.equal(result.context.tab_role, 'active');
  for (const element of result.elements) C.validate('BrowserElement', element);
  C.validate('SourceObservation', result.observation);
});

for (const [name, options] of [['with checkVisibility', {}], ['fallback boxes only', {checkVisibility: false}]]) {
  test('private fields, drafts and secrets never leave the page (' + name + ')', () => {
    const fx = fixture('private-fields');
    const {result, collected, document} = run(fx, {options});
    assert.deepEqual(document.forbiddenReads, [], 'no value/textContent/innerHTML reads');
    assert.ok(!collected.includes('SENTINEL'), 'collector output has no private sentinel');
    assert.ok(!JSON.stringify(result).includes('SENTINEL'));
    for (const secret of ['4111', '482913', 'hunter2', 'a8f9d7c6b5e4', 'AKIA']) {
      assert.ok(!JSON.stringify(result).includes(secret), secret);
    }
    assert.equal(result.observation.source_url, 'https://portal.invalid/account/settings');
    const controls = result.elements.map(e => [e.role, e.label]);
    assert.deepEqual(controls.filter(([role]) => role !== 'link'), [
      ['textbox', 'Username'], ['textbox', 'Comment'], ['checkbox', 'Remember me'],
      ['textbox', 'Search settings'], ['button', 'Save changes']]);
    assert.ok(result.coverage.excluded.secret_control >= 5);
    assert.ok(result.coverage.excluded.editable >= 2);
    assert.ok(result.coverage.redactions >= 9);
    assert.match(result.observation.text, /Contact support at help@portal\.invalid on Oct 2\./);
    assert.equal(result.links.find(l => l.label === 'Reset link').url, null);
    assert.equal(result.links.find(l => l.label === 'Credential link').url, null);
  });

  test('hidden, collapsed, framed and shadow content is excluded (' + name + ')', () => {
    const fx = fixture('hidden-content');
    const {result, collected, document} = run(fx, {options});
    assert.deepEqual(document.forbiddenReads, []);
    assert.ok(!collected.includes('SENTINEL'));
    assert.equal(result.observation.text, ['Visible headline', 'Visible paragraph.', 'Show answer',
      'Open details', 'Open body text', 'Contents child text.', 'Final visible line.'].join('\n'));
    assert.equal(result.coverage.shadow_roots_skipped, 1);
    assert.equal(result.coverage.frames_skipped, 1);
    assert.ok(result.coverage.excluded.aria_hidden >= 1);
    assert.ok(result.coverage.excluded.visually_hidden >= 3);
  });
}

test('private or unknown context yields no output and no grant', () => {
  const fx = fixture('assignment-page');
  const s = session();
  for (const overrides of [{extension_incognito: true}, {native_private_context: true},
    {extension_incognito: null}, {native_private_context: 'false'}, {native_private_context: 0}]) {
    code(() => s.live.issueGrant(JSON.stringify(context(fx, overrides))), 'private_context');
  }
  const missing = context(fx);
  delete missing.native_private_context;
  code(() => s.live.issueGrant(JSON.stringify(missing)), 'invalid_payload');
  code(() => s.live.issueGrant(JSON.stringify(context(fx, {enabled: false}))), 'disabled');
  code(() => s.live.issueGrant(JSON.stringify(context(fx, {site_permission: 'denied'}))), 'site_permission_denied');
  code(() => s.live.issueGrant(JSON.stringify(context(fx, {trigger: 'script'}))), 'invalid_payload');
  code(() => s.live.issueGrant(JSON.stringify(context(fx, {url: 'file:///etc/hosts'}))), 'site_permission_denied');
  code(() => s.live.issueGrant(context(fx)), 'invalid_payload'); // objects are never coerced
});

test('a context that turns private between grant and capture denies the capture', () => {
  const fx = fixture('assignment-page');
  const s = session();
  const {document, window} = D.build(fx);
  const collected = L.collect(document, window);
  const grant = s.live.issueGrant(JSON.stringify(context(fx)));
  code(() => s.live.capture(grant, collected, JSON.stringify(context(fx, {native_private_context: null}))), 'private_context');
  // The grant was consumed by the failed attempt.
  code(() => s.live.capture(grant, collected, JSON.stringify(context(fx))), 'stale_snapshot');
});

test('grants are one-use, expire after 30 seconds and bind the exact context', () => {
  const fx = fixture('assignment-page');
  const s = session();
  const {document, window} = D.build(fx);
  const collected = L.collect(document, window);
  const ctx = JSON.stringify(context(fx));
  const grant = s.live.issueGrant(ctx);
  s.live.capture(grant, collected, ctx);
  code(() => s.live.capture(grant, collected, ctx), 'stale_snapshot');
  code(() => s.live.capture(Object.freeze(Object.create(null)), collected, ctx), 'stale_snapshot');
  const late = s.live.issueGrant(ctx);
  s.tick(30001);
  code(() => s.live.capture(late, collected, ctx), 'stale_snapshot');
  for (const overrides of [{tab_id: 8}, {document_id: 'doc.other'}, {task_id: 'other.task'},
    {url: 'https://canvas.course.invalid/courses/101/assignments/5'}, {tab_role: 'background'}]) {
    const g = s.live.issueGrant(ctx);
    code(() => s.live.capture(g, collected, JSON.stringify(context(fx, overrides))), 'stale_snapshot');
  }
  const other = fixture('hidden-content');
  const {document: d2, window: w2} = D.build(other);
  const g = s.live.issueGrant(ctx);
  code(() => s.live.capture(g, L.collect(d2, w2), ctx), 'stale_snapshot');
});

test('revisions are document scoped, content addressed and deduplicated', () => {
  const fx = fixture('assignment-page');
  const s = session();
  const first = run(fx, {s}).result;
  s.tick(10);
  const second = run(fx, {s}).result;
  assert.equal(first.revision, second.revision);
  assert.equal(first.duplicate, false);
  assert.equal(second.duplicate, true);
  assert.notEqual(first.observation.id, second.observation.id);
  assert.deepEqual(first.elements, second.elements);
  const changed = JSON.parse(JSON.stringify(fx));
  changed.body.children[1].children[0].children = ['Assignment 4: Lab report (updated)'];
  const third = run(changed, {s}).result;
  assert.notEqual(third.revision, first.revision);
  assert.equal(third.document_id, first.document_id);
  assert.notEqual(third.elements[0].target_id, first.elements[0].target_id);
  assert.equal(third.duplicate, false);
  const navigated = run(fx, {s, ctx: context(fx, {document_id: 'doc.after.navigation'})}).result;
  assert.notEqual(navigated.document_id, first.document_id, 'navigation starts a new namespace');
});

test('large pages truncate at block boundaries within A01 limits', () => {
  const paragraphs = [];
  for (let i = 0; i < 3000; i++) {
    paragraphs.push({tag: 'p', children: ['Paragraph ' + i + ' ' + 'lorem ipsum dolor sit amet '.repeat(3)]});
    if (i % 5 === 0) paragraphs.push({tag: 'a', attrs: {href: '/item/' + i}, children: ['Item ' + i]});
  }
  const fx = {url: 'https://big.invalid/page', title: 'Big page', body: {tag: 'body', children: paragraphs}};
  const {result} = run(fx);
  assert.equal(result.coverage.truncated, true);
  assert.ok(result.coverage.truncation.length >= 1);
  assert.ok(result.observation.text.length <= L.LIMITS.textUnits);
  assert.ok(result.elements.length <= 256);
  C.validate('SourceObservation', result.observation);
});

test('visit and depth budgets bound work on pathological trees', () => {
  let node = 'deep text';
  for (let i = 0; i < 400; i++) node = {tag: 'span', children: [node]};
  const deep = run({url: 'https://deep.invalid/', title: 'Deep', body: {tag: 'body', children: [node, {tag: 'p', children: ['after']}]}}).result;
  assert.ok(deep.coverage.truncation.includes('depth'));
  const wide = [];
  for (let i = 0; i < 25000; i++) wide.push({tag: 'span', children: ['w']});
  const broad = run({url: 'https://wide.invalid/', title: 'Wide', body: {tag: 'body', children: wide}}).result;
  assert.ok(broad.coverage.truncation.includes('visit_budget'));
  assert.ok(broad.coverage.visited <= L.LIMITS.visit + 1);
});

test('secrets are redacted before any truncation', () => {
  const secret = 'ghp_' + 'Q'.repeat(10) + 'SENTINEL' + '7'.repeat(10);
  const text = 'word '.repeat(815) + secret + ' tail';
  const fx = {url: 'https://cut.invalid/', title: 'Cut', body: {tag: 'body', children: [{tag: 'p', children: [text]}]}};
  const {result, collected} = run(fx);
  assert.ok(!collected.includes('SENTINEL') && !collected.includes('QQQQ'));
  assert.ok(!JSON.stringify(result).includes('QQQQ'));
});

test('designMode documents are refused rather than read', () => {
  const fx = Object.assign(fixture('hidden-content'), {designMode: 'on'});
  const s = session();
  const {document, window} = D.build(fx);
  const collected = L.collect(document, window);
  assert.ok(!collected.includes('Visible headline'));
  const grant = s.live.issueGrant(JSON.stringify(context(fx)));
  code(() => s.live.capture(grant, collected, JSON.stringify(context(fx))), 'unsupported_control');
});

test('trusted side rejects tampered collector data and re-filters strings', () => {
  const fx = fixture('assignment-page');
  const {document, window} = D.build(fx);
  const base = JSON.parse(L.collect(document, window));
  const ctx = JSON.stringify(context(fx));
  const attempt = mutate => {
    const s = session();
    const data = JSON.parse(JSON.stringify(base));
    mutate(data);
    return () => s.live.capture(s.live.issueGrant(ctx), JSON.stringify(data), ctx);
  };
  code(attempt(d => { d.extra = 1; }), 'invalid_payload');
  code(attempt(d => { d.format = 'wisp.live.v2'; }), 'invalid_payload');
  code(attempt(d => { d.blocks.push({kind: 'script', text: 'x'}); }), 'invalid_payload');
  code(attempt(d => { d.blocks.push({kind: 'text', text: 'x', value: 'y'}); }), 'invalid_payload');
  code(attempt(d => { d.blocks.push({kind: 'control', role: 'password', label: 'x'}); }), 'invalid_payload');
  code(attempt(d => { d.blocks.push({kind: 'heading', text: 'x', level: 9}); }), 'invalid_payload');
  code(attempt(d => { d.stats.visited = -1; }), 'invalid_payload');
  code(attempt(d => { d.url = null; }), 'stale_snapshot');
  const s = session();
  const data = JSON.parse(JSON.stringify(base));
  data.blocks.push({kind: 'text', text: 'leaked Bearer SENTINELabcdefgh123'});
  data.blocks.push({kind: 'link', label: 'x', url: 'https://canvas.course.invalid/a?access_token=SENTINEL'});
  const result = s.live.capture(s.live.issueGrant(ctx), JSON.stringify(data), ctx);
  assert.ok(!JSON.stringify(result).includes('SENTINEL'));
  const s2 = session();
  code(() => s2.live.capture(s2.live.issueGrant(ctx), 'x'.repeat(L.LIMITS.jsonUnits + 1), ctx), 'invalid_payload');
  code(() => s2.live.capture(s2.live.issueGrant(ctx), base, ctx), 'invalid_payload');
});

test('in-page entry point runs once in a browser-like realm and removes itself', () => {
  const fx = fixture('private-fields');
  const {document, window} = D.build(fx);
  const sandbox = {URL, document, getComputedStyle: el => window.getComputedStyle(el), scrollX: 0, scrollY: 0};
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['private-filter.js', 'live-acquisition.js']) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'browser-extension/shared', file), 'utf8'), sandbox, {filename: file});
  }
  const entry = sandbox.WispLiveAcquisition.collectOnce;
  const out = entry();
  assert.equal(typeof out, 'string');
  assert.ok(!out.includes('SENTINEL'));
  assert.equal(sandbox.WispLiveAcquisition, undefined);
  assert.equal(sandbox.WispPrivateFilter, undefined);
  assert.equal(JSON.parse(out).format, L.FORMAT);
});

test('the A05 catalog extractor is unchanged and independent', () => {
  const E = require('../../browser-extension/shared/page-extractor.js');
  assert.equal(typeof E.createPublicExtractor, 'function');
  assert.equal(Object.keys(E).sort().join(','), 'ExtractionError,LIMITS,createPublicExtractor');
  const source = fs.readFileSync(path.join(ROOT, 'browser-extension/shared/page-extractor.js'), 'utf8');
  assert.ok(!source.includes('live-acquisition') && !source.includes('private-filter'));
});
