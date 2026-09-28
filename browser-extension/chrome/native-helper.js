/* Inactive A06 native-host policy core. Bootstrap dependencies are trusted code,
 * never Chrome messages. No IPC, Keychain, DOM, network or persistence here. */
'use strict';
const E = require('../shared/page-extractor.js');
const {randomUUID} = require('node:crypto');
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const RUNTIME_KEYS = ['profile_id', 'session_id', 'permission_epoch', 'document_id',
  'tab_id', 'url', 'enabled', 'private_context', 'site_permission', 'tab_role', 'task_id'];
class AcquisitionError extends Error {
  constructor() { super('Capture unavailable; start a fresh disclosure'); this.name = 'AcquisitionError'; }
}
function check(value) { if (!value) throw new AcquisitionError(); }
function closed(value, keys) {
  check(value && typeof value === 'object' && !Array.isArray(value));
  check(Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k)));
}
function parse(raw) {
  check(typeof raw === 'string' && raw.length <= 131072);
  return JSON.parse(raw);
}
function createNativeHelper({catalogJSON, manifestJSON, sourceJSON, readRuntime,
  deliver, now = Date.now, nonce = randomUUID}) {
  // Factory is a protected application bootstrap seam, not a wire API.
  const source = parse(sourceJSON);
  const identityKeys = ['profile_id', 'session_id', 'permission_epoch', 'document_id', 'tab_id'];
  closed(source, ['source_id', 'catalog_revision', 'url', ...identityKeys]);
  for (const key of ['source_id', 'catalog_revision', ...identityKeys.filter(k => k !== 'tab_id')]) {
    check(typeof source[key] === 'string' && ID.test(source[key]));
  }
  check(Number.isSafeInteger(source.tab_id) && source.tab_id >= 0);
  const extractor = E.createPublicExtractor(catalogJSON);
  const catalog = parse(catalogJSON), manifest = parse(manifestJSON);
  const url = catalog.urls.find(entry => entry.id === manifest.url)?.value;
  check(source.url === url);
  let document = extractor.createDocument(manifestJSON);
  let connection = null, pending = null, epoch = 0, lastTime = -1;
  const previewJSON = JSON.stringify({source_id: source.source_id, url: source.url,
    catalog_revision: source.catalog_revision,
    texts: catalog.texts.map(entry => entry.value), urls: catalog.urls.map(entry => entry.value),
    notice: 'Only the listed approved public content will be captured into Wisp. This is a partial manifest, not a complete page. No forms, drafts, private browsing, or live page text are read.'});
  function clear() { pending = null; }
  function timestamp() {
    const time = now();
    check(Number.isSafeInteger(time) && time >= 0 && time >= lastTime);
    lastTime = time;
    return time;
  }
  function runtime() {
    // JSON comes ONLY from native-owned browser state, never incoming messages.
    const value = parse(readRuntime());
    closed(value, RUNTIME_KEYS);
    for (const key of ['profile_id', 'session_id', 'permission_epoch', 'document_id', 'task_id']) {
      check(typeof value[key] === 'string' && ID.test(value[key]));
    }
    check(identityKeys.every(key => value[key] === source[key]) && value.url === source.url &&
      Number.isSafeInteger(value.tab_id) && value.tab_id >= 0 && value.enabled === true &&
      value.private_context === false && value.site_permission === 'granted' && value.tab_role === 'background');
    return JSON.stringify(value);
  }
  function guarded(fn) {
    try { return fn(); } catch (_) { clear(); throw new AcquisitionError(); }
  }
  function requireConnection(handle) { check(connection !== null && handle === connection); }
  return Object.freeze({
    connect() {
      return guarded(() => {
        clear();
        connection = null;
        runtime();
        extractor.dispose(document);
        document = extractor.createDocument(manifestJSON);
        check(epoch < Number.MAX_SAFE_INTEGER);
        epoch += 1;
        connection = Object.freeze({});
        return connection;
      });
    },
    disconnect(handle) {
      if (handle === connection) { clear(); connection = null; }
    },
    prepare(handle) {
      return guarded(() => {
        requireConnection(handle);
        clear();
        const binding = runtime(), created = timestamp(), id = nonce();
        check(typeof id === 'string' && ID.test(id));
        pending = {binding, created, id, epoch};
        return JSON.stringify({disclosure_id: id, preview: JSON.parse(previewJSON)});
      });
    },
    capture(handle, requestJSON) {
      return guarded(() => {
        requireConnection(handle);
        const request = parse(requestJSON);
        closed(request, ['disclosure_id']);
        const ticket = pending;
        clear(); // Every attempt consumes the ticket, including delivery failure.
        check(ticket && request.disclosure_id === ticket.id && ticket.epoch === epoch);
        const captured = timestamp();
        check(captured - ticket.created <= 30000 && runtime() === ticket.binding);
        const context = JSON.parse(ticket.binding);
        const result = extractor.capture(document, JSON.stringify({task_id: context.task_id,
          captured_at_ms: captured, enabled: true, private_context: false,
          site_permission: 'granted', tab_role: 'background'}));
        check(runtime() === ticket.binding);
        // Synchronous trusted sink only. No queue, retry or reconnect replay.
        const accepted = deliver(JSON.stringify({binding: {...context, source_id: source.source_id,
          catalog_revision: source.catalog_revision}, result}));
        check(accepted === true);
        return Object.freeze({captured: true});
      });
    },
  });
}
module.exports = Object.freeze({createNativeHelper, AcquisitionError});
