'use strict';
// A13 R2: ModelView index mapping, link navigation rule and Canvas exclusions.
// Synthetic .invalid fixtures only.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const A = require('../../browser-extension/shared/action-executor.js');
const M = require('../../browser-extension/shared/model-view.js');
const L = require('../../browser-extension/shared/live-acquisition.js');
const D = require('../../test_fixtures/browser_pages/live/fake-dom.cjs');
const json = JSON.stringify;
const ROOT = path.join(__dirname, '..', '..');
const fixture = name => JSON.parse(fs.readFileSync(
  path.join(ROOT, 'test_fixtures/browser_pages/live', name + '.json'), 'utf8'));

function capture(customFx = null) {
  let n = 0;
  const fx = customFx || fixture('assignment-page');
  const live = L.createLiveCapture({now: () => 5000,
    randomUUID: () => '00000000-0000-4000-8000-' + String(++n).padStart(12, '0')});
  const ctx = json({trigger: 'user_click', task_id: 'synthetic.task', tab_id: 1,
    document_id: 'doc.synthetic', url: fx.url, enabled: true, site_permission: 'granted',
    extension_incognito: false, native_private_context: false, tab_role: 'background'});
  const {document, window} = D.build(fx);
  return live.capture(live.issueGrant(ctx), L.collect(document, window), ctx);
}
function setup(profile) {
  const cap = capture();
  const view = M.buildModelView(cap, {site: 'canvas'});
  const executor = A.createSyntheticExecutor(json(cap.snapshot), profile ? {profile} : {});
  return {cap, view, executor, snap: cap.snapshot};
}
const ctxOf = (snap, id = 'act1') => ({snapshot: snap, action_id: id});

test('choice numbers map to navigate actions with the view URL and snapshot target', () => {
  const {view, snap} = setup();
  for (const e of view.elements) {
    const m = A.mapModelViewChoice(view, e.n, ctxOf(snap, 'a' + e.n));
    assert.equal(m.kind, 'navigate');
    assert.equal(m.target_id, e.target_id);
    assert.equal(m.action.intent.url, M.resolveChoice(view, e.n).url);
    assert.equal(m.action.intent.snapshot_id, snap.id);
    assert.equal(m.action.approval, null);
  }
});

test('out-of-view, malformed and forged references are rejected', () => {
  const {view, snap} = setup();
  for (const bad of [0, -1, view.elements.length + 1, 1.5, '1', null, NaN, 'open', undefined]) {
    assert.throws(() => A.mapModelViewChoice(view, bad, ctxOf(snap)), /out_of_view|invalid_payload/, String(bad));
  }
  assert.throws(() => A.mapModelViewChoice(JSON.parse(json(view)), 1, ctxOf(snap)), /out_of_view/);
  const other = json(snap).replace(snap.id, 'other.snap');
  assert.throws(() => A.mapModelViewChoice(view, 1, ctxOf(JSON.parse(other))), /stale_snapshot/);
  assert.throws(() => A.mapModelViewChoice(view, 1, {snapshot: snap, action_id: 'a', now_ms: snap.captured_at_ms + 60001}), /stale_snapshot/);
});

test('scroll/back/wait/handoff map; handoff has no action', () => {
  const {view, snap} = setup();
  for (const c of ['scroll', 'back', 'wait']) {
    assert.equal(A.mapModelViewChoice(view, c, ctxOf(snap, 'x' + c)).action.intent.command, c);
  }
  const h = A.mapModelViewChoice(view, 'handoff', ctxOf(snap));
  assert.equal(h.kind, 'handoff'); assert.equal(h.action, null);
});

test('in-scope mapped link navigates without approval; unmapped navigate still needs it', () => {
  const {view, snap, executor} = setup();
  const m = A.mapModelViewChoice(view, 1, ctxOf(snap));
  assert.equal(A.prepare(json(m.action), json(snap), m).approvalRequired, false);
  assert.equal(A.prepare(json(m.action), json(snap)).approvalRequired, true);
  assert.equal(executor.execute(json(m.action)).code, 'approval_required');
  const r = executor.execute(json(m.action), null, m);
  assert.equal(r.status, 'simulated');
  assert.equal(executor.execute(json(m.action), null, m).status, 'rejected', 'replay refused');
  assert.throws(() => A.prepare(json(m.action), json(snap), {}), /invalid_payload/, 'forged mapping');
});

test('mapping cannot be reused for a different URL or after the snapshot changes', () => {
  const {view, snap, executor} = setup();
  const m = A.mapModelViewChoice(view, 1, ctxOf(snap));
  const tampered = JSON.parse(json(m.action)); tampered.intent.url = 'https://canvas.course.invalid/courses/101/other';
  assert.equal(executor.execute(json(tampered), null, m).code, 'stale_approval');
  const w = A.createSyntheticExecutor(json(snap));
  const scroll = A.mapModelViewChoice(view, 'scroll', ctxOf(snap, 'sc'));
  assert.equal(w.execute(json(scroll.action)).status, 'simulated');
  assert.equal(w.execute(json(m.action), null, m).code, 'stale_snapshot');
});

test('freshness and occlusion recheck before execution', () => {
  const {view, snap, executor} = setup();
  const m = A.mapModelViewChoice(view, 1, ctxOf(snap));
  executor.setOccludedForTest(json([m.target_id]));
  const r = executor.execute(json(m.action), null, m);
  assert.equal(r.status, 'rejected'); assert.equal(r.code, 'target_occluded');
  executor.setOccludedForTest(json([]));
  executor.setContextForTest(json({enabled: true, permission: true, privateContext: false,
    foreground: false, cancelled: false, now: snap.captured_at_ms + 60001}));
  assert.equal(executor.execute(json(m.action), null, m).code, 'stale_snapshot');
});

test('consequential Canvas and general links are never mapped or auto-navigable', () => {
  const {view, snap} = setup();
  const bad = [
    'https://canvas.course.invalid/courses/101/assignments/5/submissions/1',
    'https://canvas.course.invalid/courses/101/quizzes/7/take',
    'https://canvas.course.invalid/courses/101/modules/items/9/done',
    'https://canvas.course.invalid/logout',
    'https://canvas.course.invalid/courses/101/pages/x?token=abc',
  ];
  for (const url of bad) {
    assert.ok(M.consequentialReasons(url, 'Open', 'canvas').length > 0, url);
    // A view forged around a bad URL cannot resolve; direct navigate stays gated.
    const a = {schema_version: '1.0', approval: null, intent: {action_id: 'z', task_id: snap.task_id,
      snapshot_id: snap.id, command: 'navigate', target_id: null, url, text: null,
      private_data: false, consequential: false}};
    assert.equal(A.prepare(json(a), json(snap)).approvalRequired, true, url);
  }
  // Labels alone also exclude.
  assert.ok(M.consequentialReasons('https://canvas.course.invalid/courses/101/pages/p', 'Mark as done', 'canvas').length > 0);
  for (const e of view.elements) {
    assert.equal(M.consequentialReasons(M.resolveChoice(view, e.n).url, e.label, 'canvas').length, 0);
  }
});

test('encoded tokens and hash action routes disappear before mapping; a safe link still navigates', () => {
  const paths = [
    ['/courses/101/pages/x?%2574oken=abc', 'Encoded token'],
    ['/courses/101/pages/x?%256Eonce=abc', 'Encoded nonce'],
    ['/courses/101/pages/x#/submit', 'Hash submit'],
    ['/courses/101/pages/x#%2573ubmit', 'Encoded hash submit'],
    ['/courses/101/pages/week-3#notes', 'Week 3 notes'],
  ];
  const fx = {url: 'https://canvas.course.invalid/courses/101/modules', title: 'Modules',
    body: {tag: 'body', children: paths.map(([href, label]) =>
      ({tag: 'a', attrs: {href}, children: [label]}))}};
  const cap = capture(fx), view = M.buildModelView(cap, {site: 'canvas'});
  const labels = view.elements.map(e => e.label);
  for (const [, label] of paths.slice(0, 4)) assert.ok(!labels.includes(label), label);
  assert.ok(view.excluded.consequential >= 2, 'effect links removed during view construction');
  const safe = view.elements.find(e => e.label === 'Week 3 notes');
  assert.ok(safe, 'ordinary same-origin hash link survives');
  const m = A.mapModelViewChoice(view, safe.n, ctxOf(cap.snapshot));
  assert.equal(A.createSyntheticExecutor(json(cap.snapshot), {profile: 'a14c'})
    .execute(json(m.action), null, m).status, 'simulated');
});

test('caller scope cannot grant an external origin to the approval-free mapping', () => {
  const {cap, snap} = setup();
  const origin = new URL(snap.url).origin;
  const view = M.buildModelView(cap, {site: 'canvas', profile: 'generic_r1',
    scope_origins: [origin, 'https://publisher.invalid']});
  const outside = view.elements.find(e => e.label === 'textbook site');
  assert.ok(outside && outside.url_ref, 'source view contains the widened-scope link');
  assert.throws(() => A.mapModelViewChoice(view, outside.n,
    {...ctxOf(snap), scope_origins: [origin, 'https://publisher.invalid']}), /out_of_scope/);
  const inside = view.elements.find(e => e.label === 'Modules');
  assert.equal(A.mapModelViewChoice(view, inside.n,
    {...ctxOf(snap), scope_origins: [origin]}).kind, 'navigate');
});

test('single source of truth: exclusion list is imported, not duplicated', () => {
  const src = fs.readFileSync(path.join(ROOT, 'browser-extension/shared/action-executor.js'), 'utf8');
  assert.ok(/require\('\.\/model-view\.js'\)/.test(src));
  assert.ok(!/canvas_submission|canvas_quiz_start|canvas_mark_done/.test(src));
});

test('a14c profile is read-only and rejects consequential URLs even with approval', () => {
  const {view, snap, executor} = setup('a14c');
  assert.deepEqual([...A.A14C_VERBS], ['navigate', 'scroll', 'back', 'wait', 'handoff']);
  for (const command of ['click', 'fill', 'select', 'open_tab']) {
    const intent = {action_id: 'r' + command, task_id: snap.task_id, snapshot_id: snap.id, command,
      target_id: ({click: 'x', fill: 'x', select: 'x'})[command] || null,
      url: command === 'open_tab' ? 'https://canvas.course.invalid/courses/101' : null,
      text: ['fill', 'select'].includes(command) ? 't' : null, private_data: false, consequential: false};
    const raw = json({schema_version: '1.0', intent, approval: {proposal_id: 'p', authority: 'app',
      approved_at_ms: 0, expires_at_ms: 9999999, intent: {...intent}}});
    assert.equal(executor.execute(raw).code, 'unsupported_control', command);
  }
  const intent = {action_id: 'q', task_id: snap.task_id, snapshot_id: snap.id, command: 'navigate',
    target_id: null, url: 'https://canvas.course.invalid/courses/101/quizzes/7/take',
    text: null, private_data: false, consequential: false};
  const raw = json({schema_version: '1.0', intent, approval: {proposal_id: 'p', authority: 'app',
    approved_at_ms: 0, expires_at_ms: 9999999, intent: {...intent}}});
  const h = executor.approveForTest(raw, 9000000);
  assert.equal(executor.execute(raw, h).code, 'consequential_link');
  const outside = {...intent, action_id: 'outside', url: 'https://other.invalid/next'};
  const outsideRaw = json({schema_version: '1.0', intent: outside,
    approval: {proposal_id: 'p', authority: 'app', approved_at_ms: 0,
      expires_at_ms: 9999999, intent: {...outside}}});
  const outsideGrant = executor.approveForTest(outsideRaw, 9000000);
  assert.equal(executor.execute(outsideRaw, outsideGrant).code, 'out_of_scope');
  assert.equal(executor.snapshotForTest().url, snap.url);
  const m = A.mapModelViewChoice(view, 1, ctxOf(snap));
  assert.equal(executor.execute(json(m.action), null, m).status, 'simulated');
  assert.throws(() => A.createSyntheticExecutor(json(snap), {profile: 'other'}));
});
