'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {createNativeHelper, AcquisitionError} = require('../../browser-extension/chrome/native-helper.js');
const {install} = require('../../browser-extension/chrome/service-worker.js');
const {mount} = require('../../browser-extension/chrome/disclosure.js');
const fixture = require('../../test_fixtures/browser_pages/public-assignment.json');
const runtime = require('../../test_fixtures/browser_chrome/runtime.json');
function host(options = {}) {
  let state = {...runtime}, time = 1000, counter = 0;
  const delivered = [];
  const helper = createNativeHelper({catalogJSON: JSON.stringify(fixture.catalog),
    manifestJSON: JSON.stringify(fixture.manifest),
    sourceJSON: JSON.stringify({source_id: 'synthetic.course', profile_id: runtime.profile_id,
      catalog_revision: 'review.1', url: runtime.url, session_id: runtime.session_id,
      document_id: runtime.document_id, permission_epoch: runtime.permission_epoch, tab_id: runtime.tab_id}),
    readRuntime: () => JSON.stringify(state), now: () => time,
    nonce: () => 'disclosure.' + ++counter,
    deliver: raw => { delivered.push(JSON.parse(raw)); return true; }, ...options});
  return {helper, delivered, set: change => {state = {...state, ...change};},
    time: next => {time = next;}};
}
const ticket = (h, c) => JSON.stringify({disclosure_id: JSON.parse(h.prepare(c)).disclosure_id});
test('approved public manifest captures only after disclosure, with complete provenance and partial coverage', () => {
  const f = host(), c = f.helper.connect();
  const disclosure = JSON.parse(f.helper.prepare(c));
  assert.equal(f.delivered.length, 0);
  assert.deepEqual(disclosure.preview.texts, fixture.catalog.texts.map(x => x.value));
  f.helper.capture(c, JSON.stringify({disclosure_id: disclosure.disclosure_id}));
  assert.equal(f.delivered.length, 1);
  const {binding, result} = f.delivered[0];
  assert.equal(binding.profile_id, runtime.profile_id);
  assert.equal(binding.session_id, runtime.session_id);
  assert.equal(binding.source_id, 'synthetic.course');
  assert.equal(binding.catalog_revision, 'review.1');
  assert.equal(result.coverage.page_complete, false);
  assert.equal(result.coverage.scope, 'approved_manifest');
  assert.match(result.observation.text, /Read chapters/);
  assert.throws(() => f.helper.capture(c, JSON.stringify({disclosure_id: disclosure.disclosure_id})), AcquisitionError);
});
for (const change of [{profile_id: 'other'}, {session_id: 'other'}, {permission_epoch: 'grant.2'},
  {document_id: 'navigation.2'}, {tab_id: 9}, {url: 'https://course.invalid/other'},
  {enabled: false}, {private_context: true}, {private_context: null}, {private_context: 'false'},
  {site_permission: 'denied'}, {tab_role: 'foreground'}, {task_id: 'other'}, {unknown: 'secret'}]) {
  test('context change cancels consent: ' + JSON.stringify(change), () => {
    const f = host(), c = f.helper.connect(), request = ticket(f.helper, c);
    f.set(change);
    assert.throws(() => f.helper.capture(c, request), AcquisitionError);
    assert.equal(f.delivered.length, 0);
    f.set(runtime);
    assert.throws(() => f.helper.capture(c, request), AcquisitionError);
  });
}
test('incognito and unknown private state fail before disclosure', () => {
  for (const private_context of [true, null, undefined, 0]) {
    const f = host(); f.set({private_context});
    assert.throws(() => f.helper.connect(), AcquisitionError);
    assert.equal(f.delivered.length, 0);
  }
});
for (const change of [{profile_id: 'other'}, {session_id: 'other'}, {document_id: 'other'},
  {permission_epoch: 'other'}, {tab_id: 5}]) {
  test('fixed manifest cannot be relabeled before prepare or after reconnect: ' + JSON.stringify(change), () => {
    const f = host(), c = f.helper.connect();
    f.set(change);
    assert.throws(() => f.helper.prepare(c), AcquisitionError);
    assert.throws(() => f.helper.connect(), AcquisitionError);
    assert.equal(f.delivered.length, 0);
  });
}
test('expiry, clock rollback, replaced disclosure and forged connection fail closed', () => {
  for (const time of [999, 31001, NaN]) {
    const f = host(), c = f.helper.connect(), request = ticket(f.helper, c);
    f.time(time);
    assert.throws(() => f.helper.capture(c, request), AcquisitionError);
    assert.equal(f.delivered.length, 0);
  }
  const f = host(), c = f.helper.connect(), old = ticket(f.helper, c);
  ticket(f.helper, c);
  assert.throws(() => f.helper.capture(c, old), AcquisitionError);
  assert.throws(() => f.helper.prepare({}), AcquisitionError);
});
test('disconnect/reconnect invalidates handles, consent and document namespace', () => {
  const f = host(), first = f.helper.connect();
  f.helper.capture(first, ticket(f.helper, first));
  const pending = ticket(f.helper, first);
  f.helper.disconnect(first);
  const second = f.helper.connect();
  assert.throws(() => f.helper.capture(first, pending), AcquisitionError);
  assert.throws(() => f.helper.capture(second, pending), AcquisitionError);
  f.helper.capture(second, ticket(f.helper, second));
  assert.notEqual(f.delivered[0].result.document_id, f.delivered[1].result.document_id);
  assert.equal(f.delivered.length, 2);
});
test('delivery failure is uncertain and cannot replay; async sinks are unsupported', () => {
  for (const deliver of [() => false, () => {throw Error('private detail');}, () => Promise.resolve(true)]) {
    const f = host({deliver}), c = f.helper.connect(), request = ticket(f.helper, c);
    assert.throws(() => f.helper.capture(c, request), e => e instanceof AcquisitionError && !e.message.includes('private'));
    assert.throws(() => f.helper.capture(c, request), AcquisitionError);
  }
});
test('fresh context is checked again before delivery', () => {
  let reads = 0;
  const f = host({readRuntime: () => JSON.stringify({...runtime, private_context: ++reads >= 4})});
  const c = f.helper.connect(), request = ticket(f.helper, c);
  assert.throws(() => f.helper.capture(c, request), AcquisitionError);
  assert.equal(f.delivered.length, 0);
});
for (const raw of ['null', '[]', '{', '{"disclosure_id":"x","catalog":"SECRET"}', 'x'.repeat(131073)]) {
  test('request rejects malformed/free-content input: ' + raw.slice(0, 30), () => {
    const f = host(), c = f.helper.connect(); ticket(f.helper, c);
    assert.throws(() => f.helper.capture(c, raw), AcquisitionError);
    assert.equal(f.delivered.length, 0);
  });
}
test('DOM/getter objects are never inspected as capture requests', () => {
  const f = host(), c = f.helper.connect(); ticket(f.helper, c);
  const input = new Proxy({}, {get() {assert.fail('read secret getter');}, ownKeys() {assert.fail('read DOM');}});
  assert.throws(() => f.helper.capture(c, input), AcquisitionError);
  assert.equal(f.delivered.length, 0);
});
function event() {
  const listeners = [];
  return {addListener: fn => listeners.push(fn), emit: value => listeners.forEach(fn => fn(value))};
}
function browser(f, senderChange = {}) {
  const onConnect = event(), id = 'syntheticextension';
  const chrome = {runtime: {id, getURL: p => 'chrome-extension://' + id + '/' + p, onConnect}};
  install(chrome, f.helper);
  const sent = [];
  const port = {name: 'wisp-public-disclosure', sender: {id, url: chrome.runtime.getURL('chrome/disclosure.html'),
    origin: 'chrome-extension://' + id, ...senderChange}, onMessage: event(), onDisconnect: event(),
    disconnected: false, postMessage: m => sent.push(m),
    disconnect() {this.disconnected = true; this.onDisconnect.emit();}};
  onConnect.emit(port);
  return {chrome, port, sent};
}
test('worker accepts only fixed two-step popup protocol and never receives runtime claims', () => {
  const f = host(), b = browser(f);
  b.port.onMessage.emit({op: 'prepare'});
  assert.equal(b.sent[0].type, 'disclosure');
  assert.equal(f.delivered.length, 0);
  b.port.onMessage.emit({op: 'capture'});
  assert.equal(f.delivered.length, 1);
  b.port.onMessage.emit({op: 'capture'});
  assert.equal(b.port.disconnected, true);
  assert.equal(f.delivered.length, 1);
});
for (const sender of [{id: 'other'}, {url: 'https://course.invalid'}, {tab: {id: 4}},
  {origin: 'null'}, {origin: 'https://course.invalid'}, {url: 'chrome-extension://syntheticextension/chrome/disclosure.html?x'}]) {
  test('worker denies content scripts, foreign origins and wrong UI: ' + JSON.stringify(sender), () => {
    const f = host(), b = browser(f, sender);
    assert.equal(b.port.disconnected, true);
    assert.equal(f.delivered.length, 0);
  });
}
for (const message of [{op: 'capture'}, {op: 'prepare', private_context: false}, {op: 'approve'}, null, []]) {
  test('worker rejects out of order or authority-bearing messages: ' + JSON.stringify(message), () => {
    const f = host(), b = browser(f); b.port.onMessage.emit(message);
    assert.equal(b.port.disconnected, true);
    assert.equal(f.delivered.length, 0);
  });
}
function panel() {
  const nodes = Object.fromEntries(['review','capture','disclosure','preview','status'].map(id => [id,
    {disabled: id === 'capture', hidden: id === 'disclosure', textContent: '', events: {},
      addEventListener(name, fn) {this.events[name] = fn;}}]));
  const sent = [], port = {onMessage: event(), onDisconnect: event(), postMessage: m => sent.push(m)};
  let connects = 0;
  mount({getElementById: id => nodes[id]}, {runtime: {connect() {connects++; return port;}}});
  return {nodes, port, sent, connects: () => connects};
}
test('popup requires genuine review and capture clicks, with disclosure visible first', () => {
  const p = panel(), n = p.nodes;
  n.review.events.click({isTrusted: false}); assert.equal(p.connects(), 0);
  n.capture.events.click({isTrusted: true}); assert.equal(p.sent.length, 0);
  n.review.events.click({isTrusted: true}); assert.deepEqual(p.sent, [{op: 'prepare'}]);
  p.port.onMessage.emit({type: 'disclosure', preview: {texts: ['<img src=x onerror=secret()>']}});
  assert.equal(n.disclosure.hidden, false); assert.equal(n.capture.disabled, false);
  assert.match(n.preview.textContent, /<img/);
  n.capture.events.click({isTrusted: false}); assert.equal(p.sent.length, 1);
  n.capture.events.click({isTrusted: true}); assert.deepEqual(p.sent[1], {op: 'capture'});
  assert.equal(n.capture.disabled, true);
  n.capture.events.click({isTrusted: true}); assert.equal(p.sent.length, 2);
});
test('popup disconnect removes disclosure and disables stale capture', () => {
  const p = panel(); p.nodes.review.events.click({isTrusted: true});
  p.port.onMessage.emit({type: 'disclosure', preview: {texts: ['public']}});
  p.port.onDisconnect.emit(); p.nodes.capture.events.click({isTrusted: true});
  assert.equal(p.nodes.preview.textContent, ''); assert.equal(p.nodes.capture.disabled, true);
  assert.equal(p.sent.length, 1);
  p.port.onMessage.emit({type: 'disclosure', preview: {texts: ['late public']}});
  assert.equal(p.nodes.capture.disabled, true); assert.equal(p.nodes.disclosure.hidden, true);
});
test('late disclosure after capture cannot re-enable popup', () => {
  const p = panel(); p.nodes.review.events.click({isTrusted: true});
  p.port.onMessage.emit({type: 'disclosure', preview: {texts: ['public']}});
  p.nodes.capture.events.click({isTrusted: true});
  p.port.onMessage.emit({type: 'captured'});
  p.port.onMessage.emit({type: 'disclosure', preview: {texts: ['late public']}});
  assert.equal(p.nodes.capture.disabled, true);
  p.nodes.capture.events.click({isTrusted: true}); assert.equal(p.sent.length, 2);
});
test('combined popup/worker/host requires visible disclosure and delivers once', () => {
  const f = host(), onConnect = event(), id = 'syntheticextension';
  let serverPort;
  const chrome = {runtime: {id, getURL: p => 'chrome-extension://' + id + '/' + p, onConnect,
    connect({name}) {
      const client = {onMessage: event(), onDisconnect: event(), postMessage: m => serverPort.onMessage.emit(m)};
      serverPort = {name, sender: {id, url: this.getURL('chrome/disclosure.html'), origin: 'chrome-extension://' + id},
        onMessage: event(), onDisconnect: event(), postMessage: m => client.onMessage.emit(m),
        disconnect() {this.onDisconnect.emit(); client.onDisconnect.emit();}};
      onConnect.emit(serverPort); return client;
    }}};
  install(chrome, f.helper);
  const nodes = Object.fromEntries(['review','capture','disclosure','preview','status'].map(id => [id,
    {disabled: id === 'capture', hidden: id === 'disclosure', textContent: '', events: {},
      addEventListener(name, fn) {this.events[name] = fn;}}]));
  mount({getElementById: id => nodes[id]}, chrome);
  nodes.review.events.click({isTrusted: true});
  assert.equal(nodes.disclosure.hidden, false); assert.match(nodes.preview.textContent, /Read chapters/);
  assert.equal(f.delivered.length, 0);
  nodes.capture.events.click({isTrusted: true});
  assert.equal(f.delivered.length, 1); assert.match(nodes.status.textContent, /manifest captured/);
  serverPort.postMessage({type: 'disclosure', preview: {texts: ['late']}});
  assert.equal(nodes.capture.disabled, true);
  serverPort.disconnect(); nodes.capture.events.click({isTrusted: true});
  assert.equal(f.delivered.length, 1);
});
test('HTML has inactive capture control and no external resources', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../browser-extension/chrome/disclosure.html'), 'utf8');
  assert.match(html, /id="capture"[^>]*disabled/);
  assert.match(html, /id="disclosure" hidden/);
  assert.doesNotMatch(html, /https?:|on\w+=|<iframe/i);
});
