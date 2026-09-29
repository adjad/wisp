'use strict';
// Deterministic ModelView tests over synthetic live captures.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ROOT = path.resolve(__dirname, '../..');
const L = require('../../browser-extension/shared/live-acquisition.js');
const M = require('../../browser-extension/shared/model-view.js');
const D = require('../../test_fixtures/browser_pages/live/fake-dom.cjs');
const fixture = name => JSON.parse(fs.readFileSync(
  path.join(ROOT, 'test_fixtures/browser_pages/live', name + '.json'), 'utf8'));

function capture(fx, tabRole = 'background') {
  let n = 0;
  const live = L.createLiveCapture({now: () => 5000,
    randomUUID: () => '00000000-0000-4000-8000-' + String(++n).padStart(12, '0')});
  const ctx = JSON.stringify({trigger: 'user_click', task_id: 'synthetic.task', tab_id: 1,
    document_id: 'doc.synthetic', url: fx.url, enabled: true, site_permission: 'granted',
    extension_incognito: false, native_private_context: false, tab_role: tabRole});
  const {document, window} = D.build(fx);
  return live.capture(live.issueGrant(ctx), L.collect(document, window), ctx);
}
const err = (fn, code) => assert.throws(fn, e => e instanceof M.ModelViewError && e.code === code);
const clone = value => JSON.parse(JSON.stringify(value));

test('Tier 2 view: five navigable in-scope candidates within the R2 budget', () => {
  const cap = capture(fixture('assignment-page'));
  const view = M.buildModelView(cap, {site: 'canvas'});
  assert.equal(view.profile, 'a14c_tier2');
  assert.equal(view.elements.length, 5);
  assert.deepEqual(view.elements.map(e => e.n), [1, 2, 3, 4, 5]);
  const targets = new Set(cap.snapshot.elements.map(e => e.target_id));
  for (const element of view.elements) {
    assert.ok(targets.has(element.target_id));
    assert.equal(element.navigable, true);
    assert.equal(element.url_ref, 'u' + element.n);
    assert.ok(element.path.startsWith('/courses/101'));
  }
  assert.ok(view.tokens.elements <= 1500 && view.tokens.summary <= 1000);
  assert.equal(view.coverage, 'partial', 'candidates cut by the five-element budget');
  assert.equal(view.truncated.elements, true);
  assert.equal(view.snapshot_id, cap.snapshot.id);
  assert.equal(view.revision, cap.revision);
  assert.equal(view.excluded.consequential, 11);
  assert.ok(Object.isFrozen(view) && Object.isFrozen(view.elements[0]));
});

test('consequential links never reach any view', () => {
  const cap = capture(fixture('assignment-page'));
  for (const profile of ['a14c_tier2', 'generic_r1']) {
    const view = M.buildModelView(cap, {site: 'canvas', profile});
    const labels = view.elements.map(e => e.label);
    for (const banned of ['Log out', 'View submission', 'Mark as done', 'Quiz 7', 'Announcements feed',
      'Old handout', 'Enrollment', 'Lab fee', 'Grades', 'Publisher tool', 'Submit Assignment']) {
      assert.ok(!labels.includes(banned), profile + ': ' + banned);
    }
  }
});

test('consequential pattern table', () => {
  const yes = [
    ['https://c.invalid/logout', ''], ['https://c.invalid/users/sign_out', ''],
    ['https://c.invalid/items/3/delete', ''], ['https://c.invalid/mail/unsubscribe?list=1', ''],
    ['https://c.invalid/form/submit', ''], ['https://c.invalid/order/confirm', ''],
    ['https://c.invalid/invite/accept', ''], ['https://c.invalid/billing/pay', ''],
    ['https://c.invalid/checkout', ''], ['https://c.invalid/page?token=1', ''],
    ['https://c.invalid/page?nonce=1', ''], ['https://c.invalid/page?csrf_token=1', ''],
    ['https://c.invalid/courses/1/assignments/2/submissions', ''],
    ['https://c.invalid/courses/1/quizzes/3/take', ''], ['https://c.invalid/courses/1/quizzes/3/take?user_id=1', ''],
    ['https://c.invalid/courses/1/quizzes/3/start', ''],
    ['https://c.invalid/courses/1/modules/items/9/done', ''],
    ['https://c.invalid/courses/1/modules/items/9/mark_as_done', ''],
    ['https://c.invalid/courses/1/external_tools/4', ''],
    ['https://c.invalid/courses/1/pages/intro', 'Submit your work'],
    ['https://c.invalid/courses/1/pages/intro', 'Mark as done'],
    ['https://c.invalid/courses/1/pages/intro', 'Take the Quiz'],
    ['https://c.invalid/courses/1/pages/intro', 'Sign out'],
    [null, 'Delete draft'], [null, 'Accept invitation'], [null, 'Pay now'], [null, 'Confirm'],
  ];
  for (const [url, label] of yes) {
    assert.ok(M.consequentialReasons(url, label, 'canvas').length > 0, url + ' ' + label);
  }
  const no = [
    ['https://c.invalid/courses/1/assignments/2', 'Assignment 2'],
    ['https://c.invalid/courses/1/modules', 'Modules'],
    ['https://c.invalid/courses/1/pages/week-3-reading', 'Week 3 reading'],
    ['https://c.invalid/courses/1/assignments/syllabus', 'Syllabus'],
    ['https://c.invalid/courses/1/quizzes/3', 'Quiz 3 details'],
    ['https://c.invalid/courses/1/modules/items/9', 'Next'],
    ['https://c.invalid/courses/1/pages/intro?module_item_id=9', 'Intro'],
  ];
  for (const [url, label] of no) {
    assert.deepEqual(M.consequentialReasons(url, label, 'canvas'), [], url + ' ' + label);
  }
});

test('url_ref only for in-scope HTTP(S) anchors; resolution stays inside the view', () => {
  const cap = capture(fixture('assignment-page'));
  const view = M.buildModelView(cap, {site: 'canvas', profile: 'generic_r1'});
  for (const element of view.elements) {
    if (element.url_ref) {
      assert.equal(element.navigable, true);
      const resolved = M.resolveChoice(view, element.n);
      assert.equal(resolved.command, 'navigate');
      assert.equal(resolved.target_id, element.target_id);
      assert.equal(new URL(resolved.url).origin, 'https://canvas.course.invalid');
    } else {
      assert.equal(M.resolveChoice(view, element.n), null);
    }
  }
  const textbook = view.elements.find(e => e.label === 'textbook site');
  assert.equal(textbook.url_ref, null);
  assert.equal(textbook.path, null);
  assert.equal(view.elements.find(e => e.label === 'Join live session').url_ref, null);
  for (const bad of [0, -1, view.elements.length + 1, 1.5, '1', 'back', 'handoff', null, NaN]) {
    assert.equal(M.resolveChoice(view, bad), null, String(bad));
  }
  assert.equal(M.resolveChoice(clone(view), 1), null, 'a copied or forged view resolves nothing');
  const widened = M.buildModelView(cap, {profile: 'generic_r1',
    scope_origins: ['https://canvas.course.invalid', 'https://publisher.invalid']});
  assert.ok(widened.elements.find(e => e.label === 'textbook site').url_ref);
});

test('rendered text is deterministic and never contains a full URL', () => {
  const cap = capture(fixture('assignment-page'));
  const a = M.renderModelView(M.buildModelView(cap, {site: 'canvas'}));
  const b = M.renderModelView(M.buildModelView(cap, {site: 'canvas'}));
  assert.equal(a, b);
  assert.ok(!/https?:\/\//.test(a));
  assert.ok(!a.includes('?'));
  assert.match(a, /^Page: Assignment 4: Lab report\nCoverage: partial\n# Assignment 4: Lab report/);
  assert.match(a, /\[1\] link "Modules" \/courses\/101\/modules/);
  assert.deepEqual(JSON.parse(JSON.stringify(M.buildModelView(cap, {site: 'canvas'}))),
    JSON.parse(JSON.stringify(M.buildModelView(cap, {site: 'canvas'}))));
  err(() => M.renderModelView(clone(M.buildModelView(cap))), 'invalid_payload');
});

test('small page fits the generic profile completely; date text ranks first', () => {
  const cap = capture(fixture('assignment-page'));
  const view = M.buildModelView(cap, {site: 'canvas', profile: 'generic_r1'});
  // Links were removed from the view (consequential/out of scope): partial.
  assert.equal(view.coverage, 'partial');
  assert.ok(view.gaps.consequential_removed + view.gaps.out_of_scope_removed > 0);
  assert.deepEqual(view.truncated, {capture: false, summary: false, elements: false});
  // Nothing skipped, redacted or removed: complete.
  const clean = clone(cap);
  clean.blocks = clean.blocks.filter(b => b.kind === 'heading' || b.kind === 'text');
  clean.coverage = Object.assign({}, clean.coverage, {frames_skipped: 0, shadow_roots_skipped: 0,
    redactions: 0, urls_withheld: 0, unlabeled_controls: 0});
  assert.equal(M.buildModelView(clean, {site: 'canvas', profile: 'generic_r1'}).coverage, 'complete');
  const texts = view.summary.filter(s => s.kind !== 'heading').map(s => s.text);
  assert.ok(/Oct 2 by 5pm/.test(texts[0]));
  assert.deepEqual(view.summary.filter(s => s.kind === 'heading').map(s => s.text),
    ['Assignment 4: Lab report', 'Instructions']);
});

test('summary budget truncation makes the view partial', () => {
  const children = [{tag: 'h1', children: ['Long syllabus']}];
  for (let i = 0; i < 400; i++) children.push({tag: 'p', children: ['Week ' + i + ' topic notes and readings for the unit.']});
  const cap = capture({url: 'https://canvas.course.invalid/courses/1/assignments/syllabus', title: 'Syllabus',
    body: {tag: 'body', children}});
  for (const [profile, limit] of [['a14c_tier2', 1000], ['generic_r1', 2500]]) {
    const view = M.buildModelView(cap, {profile});
    assert.equal(view.coverage, 'partial');
    assert.equal(view.truncated.summary, true);
    assert.ok(view.tokens.summary <= limit, profile + ' ' + view.tokens.summary);
    const rendered = M.renderModelView(view);
    assert.ok(M.estimateTokens(rendered) <= limit + M.PROFILES[profile].elementTokens + 20);
  }
});

test('a truncated capture is a partial read even when the view fits', () => {
  const cap = clone(capture(fixture('hidden-content')));
  cap.coverage.truncated = true;
  const view = M.buildModelView(cap, {profile: 'generic_r1'});
  assert.equal(view.coverage, 'partial');
  assert.equal(view.truncated.capture, true);
});

test('element budget bounds long pages of links', () => {
  const children = [];
  for (let i = 0; i < 200; i++) {
    children.push({tag: 'a', attrs: {href: '/courses/1/assignments/' + i}, children: ['Assignment ' + i + ' ' + 'detail '.repeat(20)]});
  }
  const cap = capture({url: 'https://canvas.course.invalid/courses/1/assignments', title: 'Assignments',
    body: {tag: 'body', children}});
  const view = M.buildModelView(cap, {profile: 'generic_r1'});
  assert.ok(view.elements.length <= 60);
  assert.ok(view.tokens.elements <= 2090);
  assert.equal(view.coverage, 'partial');
  assert.ok(view.excluded.over_budget > 0);
});

test('trusted scores reorder candidates; invalid scores are rejected', () => {
  const cap = capture(fixture('assignment-page'));
  const next = cap.links.find(l => l.label === 'Next module item').id;
  const view = M.buildModelView(cap, {site: 'canvas', scores: {[next]: 0.99}});
  assert.equal(view.elements[0].target_id, next);
  for (const scores of [{[next]: NaN}, {[next]: 2}, {[next]: -0.1}, {'lv.unknown.n1': 0.5}, [0.5], null]) {
    err(() => M.buildModelView(cap, {scores}), 'invalid_payload');
  }
});

test('private or unknown context yields no view', () => {
  const cap = capture(fixture('assignment-page'));
  for (const mutate of [c => { c.context.private_context = true; }, c => { c.context.private_context = null; },
    c => { delete c.context; }, c => { c.context = 'ok'; }, c => { c.observation.private_context = true; }]) {
    const forged = clone(cap);
    mutate(forged);
    err(() => M.buildModelView(forged), forged.context && forged.context.private_context === false ? 'invalid_payload' : 'private_context');
  }
  for (const mutate of [c => { c.source = 'approved_manifest'; }, c => { c.coverage.page_complete = true; },
    c => { c.blocks[0].id = c.blocks[1].id; c.blocks[0].kind = 'link'; c.blocks[1].kind = 'link'; }]) {
    const forged = clone(cap);
    mutate(forged);
    err(() => M.buildModelView(forged), 'invalid_payload');
  }
  err(() => M.buildModelView(null), 'invalid_payload');
  err(() => M.buildModelView(cap, {profile: 'unbounded'}), 'invalid_payload');
  err(() => M.buildModelView(cap, {scope_origins: ['*']}), 'invalid_payload');
});

test('active-tab captures (no snapshot) still build a view mapped to capture targets', () => {
  const cap = capture(fixture('assignment-page'), 'active');
  assert.equal(cap.snapshot, null);
  const view = M.buildModelView(cap, {site: 'canvas'});
  assert.equal(view.snapshot_id, null);
  const ids = new Set(cap.elements.map(e => e.target_id));
  assert.ok(view.elements.every(e => ids.has(e.target_id)));
});

test('budgets follow the R2 Model runtime contract', () => {
  assert.deepEqual({...M.PROFILES.a14c_tier2}, {maxElements: 5, elementTokens: 1500, summaryTokens: 1000,
    navigableOnly: true, labelUnits: 200, headingUnits: 160});
  assert.equal(M.PROFILES.generic_r1.maxElements, 60);
  assert.equal(M.PROFILES.generic_r1.summaryTokens, 2500);
  // R2 Tier 2 total: 1200 + 200 + 600 + 1500 + 1000 + 60 = 4560.
  assert.equal(1200 + 200 + 600 + M.PROFILES.a14c_tier2.elementTokens + M.PROFILES.a14c_tier2.summaryTokens + 60, 4560);
  assert.equal(1200 + 200 + 600 + M.PROFILES.generic_r1.elementTokens + M.PROFILES.generic_r1.summaryTokens + 60, 6650);
  assert.ok(M.estimateTokens('x'.repeat(300)) >= 75, 'estimate is conservative versus ~4 chars/token');
});

test('concatenated and camelCase consequential path segments are excluded', () => {
  const yes = ['/deleteAccount', '/removeItem/5', '/submitForm', '/confirmOrder', '/acceptInvite',
    '/payNow', '/user/logoutAll', '/enroll', '/cancel_registration', '/account/signOut', '/doUnsubscribe'];
  for (const p of yes) {
    assert.ok(M.consequentialReasons('https://x.invalid' + p, '', 'generic').length > 0, p);
  }
  for (const p of ['/display/settings', '/courses/1/pages/repay-policy-overview', '/paragraph', '/courses/1/grades']) {
    assert.deepEqual(M.consequentialReasons('https://x.invalid' + p, '', 'generic'), [], p);
  }
});

test('known capture gaps make the view partial and travel with it', () => {
  const cap = capture(fixture('hidden-content'));
  assert.ok(cap.coverage.frames_skipped >= 1 && cap.coverage.shadow_roots_skipped >= 1);
  const view = M.buildModelView(cap);
  assert.equal(view.coverage, 'partial');
  assert.equal(view.gaps.page_complete, false);
  assert.equal(view.gaps.frames_skipped, cap.coverage.frames_skipped);
  assert.equal(view.gaps.shadow_roots_skipped, cap.coverage.shadow_roots_skipped);
  assert.match(M.renderModelView(view), /Coverage: partial/);
  // A redaction alone, or a removed consequential link alone, also counts.
  const redacted = clone(cap);
  redacted.coverage = Object.assign({}, redacted.coverage, {frames_skipped: 0, shadow_roots_skipped: 0, redactions: 2});
  assert.equal(M.buildModelView(redacted).coverage, 'partial');
});

test('encoded and additional consequential endpoints are caught', () => {
  for (const url of ['https://c.invalid/a%2564elete/1', 'https://c.invalid/courses/1/conferences/9/join',
    'https://c.invalid/courses/1/turnitin/upload']) {
    assert.ok(M.consequentialReasons(url, 'Open', 'canvas').length > 0, url);
  }
});
