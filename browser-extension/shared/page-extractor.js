/*
 * A05 data-only extraction boundary. This module never reads a browser DOM.
 *
 * TRUST: createPublicExtractor(catalogJSON) is application configuration, not a
 * page/message handler. The application must supply a reviewed public-content
 * catalog in its own isolated realm and keep the constructor/capture capability
 * away from page code. Catalog membership is an explicit disclosure allowlist,
 * NOT a classifier for arbitrary prose. Do not build it from innerHTML, a live
 * Document, accessibility text, form values, or an unreviewed page export.
 * No production catalog/acquisition adapter is installed by this module.
 *
 * Manifests contain references into that catalog, never free text, markup,
 * attributes, values, styles, scripts, slots or shadow trees. Their private
 * immutable representation is the inert document; no native DOM is created.
 * Unknown input is rejected atomically before any output. Public content means
 * approved for disclosure, not merely visible. Rendered-page coverage and live
 * action target resolution require a separately reviewed A06/A07 boundary.
 *
 * A01 mapping is serialization only: caller-supplied runtime context does NOT
 * authenticate permissions. A trusted adapter must establish and recheck it.
 */
'use strict';

(function () {
  const C = typeof module !== 'undefined' && module.exports
    ? require('./contracts.js') : globalThis.WispBrowserContracts;
  const LIMITS = Object.freeze({jsonUnits: 131072, catalogEntries: 512,
    nodes: 256, textUnits: 32768, fieldUnits: 512, rows: 32, columns: 16});
  const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;

  class ExtractionError extends Error {
    constructor() { super('Unsupported or undisclosed extraction input'); this.name = 'ExtractionError'; }
  }
  const fail = () => { throw new ExtractionError(); };
  function check(ok) { if (!ok) fail(); }
  function keys(value, expected) {
    check(value !== null && typeof value === 'object' && !Array.isArray(value));
    const actual = Object.keys(value);
    check(actual.length === expected.length && expected.every(k => Object.hasOwn(value, k)));
  }
  function parse(raw) {
    // Never coerce an object: live DOM/proxies/accessors must remain unread.
    check(typeof raw === 'string' && raw.length <= LIMITS.jsonUnits);
    try { return JSON.parse(raw); } catch (_) { return fail(); }
  }
  function text(value, max) {
    check(typeof value === 'string' && value.length > 0 && value.length <= max);
    // Reject controls and malformed Unicode, rather than silently changing a
    // disclosed value. UTF-16 unit limits are stricter than A01 scalar limits.
    check(!/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value));
    check(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value));
    const normalized = value.replace(/\s+/gu, ' ').trim();
    check(normalized.length > 0);
    return normalized;
  }
  function array(value, max) { check(Array.isArray(value) && value.length <= max); return value; }
  function freeze(value) {
    if (value && typeof value === 'object') {
      Object.values(value).forEach(freeze);
      Object.freeze(value);
    }
    return value;
  }
  function publicURL(value) {
    check(typeof value === 'string' && value.length <= 4096);
    // Queries, fragments and user-info are never carried, even in a catalog.
    // Paths still require explicit disclosure review; syntax is not privacy.
    check(/^https?:\/\/[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?$/u.test(value));
    let url;
    try { url = new URL(value); } catch (_) { return fail(); }
    check(!url.username && !url.password && !url.search && !url.hash && url.origin.length <= 512);
    return url.href;
  }
  function createPublicExtractor(catalogJSON) {
    check(C && typeof C.validate === 'function');
    const catalog = parse(catalogJSON);
    keys(catalog, ['texts', 'urls']);
    const texts = new Map(), urls = new Map();
    function entries(list, target, convert) {
      for (const item of array(list, LIMITS.catalogEntries)) {
        keys(item, ['id', 'value']);
        check(typeof item.id === 'string' && ID.test(item.id) && !target.has(item.id));
        target.set(item.id, convert(item.value));
      }
    }
    entries(catalog.texts, texts, value => text(value, LIMITS.fieldUnits));
    entries(catalog.urls, urls, publicURL);
    function ref(map, id) {
      check(typeof id === 'string' && ID.test(id) && map.has(id));
      return map.get(id);
    }
    const documents = new WeakMap();
    function materialize(raw) {
      const input = parse(raw);
      keys(input, ['title', 'url', 'nodes']);
      const title = ref(texts, input.title), url = ref(urls, input.url);
      let work = 0, units = 0;
      const lines = [], nodes = [];
      function charge() { check(++work <= LIMITS.nodes); }
      function line(value) {
        units += value.length + (lines.length ? 1 : 0);
        check(units <= LIMITS.textUnits);
        lines.push(value);
      }
      for (const node of array(input.nodes, LIMITS.nodes)) {
        charge();
        check(node && typeof node === 'object' && !Array.isArray(node));
        switch (node.kind) {
          case 'text':
            keys(node, ['kind', 'text']);
            nodes.push({kind: 'text', text: ref(texts, node.text)});
            line(nodes.at(-1).text);
            break;
          case 'heading':
            keys(node, ['kind', 'text', 'level']);
            check(Number.isInteger(node.level) && node.level >= 1 && node.level <= 6);
            nodes.push({kind: 'heading', text: ref(texts, node.text), level: node.level});
            line(nodes.at(-1).text);
            break;
          case 'link':
            keys(node, ['kind', 'label', 'url']);
            nodes.push({kind: 'link', label: ref(texts, node.label), url: ref(urls, node.url)});
            line(nodes.at(-1).label);
            break;
          case 'control':
            keys(node, ['kind', 'label', 'role']);
            // Only descriptive labels: never values/options/checked state or
            // editable targets. Controls cannot be used as live DOM handles.
            check(['button', 'checkbox', 'radio', 'combobox', 'textbox'].includes(node.role));
            nodes.push({kind: 'control', label: ref(texts, node.label), role: node.role});
            line(nodes.at(-1).label);
            break;
          case 'table': {
            keys(node, ['kind', 'rows']);
            const rows = array(node.rows, LIMITS.rows).map(row => {
              charge();
              return array(row, LIMITS.columns).map(cell => { charge(); return ref(texts, cell); });
            });
            for (const row of rows) line(row.join(' | '));
            nodes.push({kind: 'table', rows});
            break;
          }
          default: fail();
        }
      }
      return freeze({title, url, nodes, text: lines.join('\n'), work});
    }
    function state(handle) {
      // WeakMap identity is the sole document provenance check, not a property
      // or instanceof test an external document could imitate.
      const result = documents.get(handle);
      check(result !== undefined);
      return result;
    }
    function createDocument(manifestJSON) {
      const data = materialize(manifestJSON);
      const crypto = typeof module !== 'undefined' && module.exports
        ? require('node:crypto') : globalThis.crypto;
      check(crypto && typeof crypto.randomUUID === 'function');
      const handle = Object.freeze(Object.create(null));
      documents.set(handle, {id: 'd.' + crypto.randomUUID(), revision: 1, captures: 0,
        data, signature: JSON.stringify(data), lastCapture: null});
      return handle;
    }
    function updateDocument(handle, manifestJSON) {
      const s = state(handle), data = materialize(manifestJSON), signature = JSON.stringify(data);
      if (signature === s.signature) return false;
      check(s.revision < Number.MAX_SAFE_INTEGER);
      s.data = data; s.signature = signature; s.revision += 1;
      return true;
    }
    function capture(handle, contextJSON) {
      const s = state(handle), context = parse(contextJSON);
      keys(context, ['task_id', 'captured_at_ms', 'enabled', 'site_permission', 'private_context', 'tab_role']);
      // Fail before constructing output, even when no content is present.
      check(context.enabled === true && context.site_permission === 'granted' &&
        context.private_context === false && context.tab_role === 'background');
      check(s.captures < Number.MAX_SAFE_INTEGER);
      const revision = s.id + '.r' + s.revision, captureID = s.id + '.c' + (s.captures + 1);
      const elements = [], headings = [], tables = [], links = [];
      s.data.nodes.forEach((node, index) => {
        const id = revision + '.n' + index;
        if (node.kind === 'heading') headings.push({id, text: node.text, level: node.level});
        if (node.kind === 'table') tables.push({id, rows: node.rows});
        if (node.kind === 'link') links.push({id, label: node.label, url: node.url});
        if (node.kind === 'link' || node.kind === 'control') {
          elements.push({target_id: id, role: node.kind === 'link' ? 'link' : node.role,
            label: node.label, editable: false});
        }
      });
      const observation = C.validate('SourceObservation', {
        schema_version: '1.0', id: captureID + '.o', source_kind: 'browser',
        source_url: s.data.url, source_record_id: s.id, revision,
        observed_at_ms: context.captured_at_ms, title: s.data.title,
        text: s.data.text, private_context: false,
      });
      const snapshot = C.validate('BrowserSnapshot', {
        schema_version: '1.0', id: captureID + '.s', task_id: context.task_id,
        observation_id: observation.id, url: s.data.url, origin: new URL(s.data.url).origin,
        captured_at_ms: context.captured_at_ms, enabled: true, site_permission: 'granted',
        private_context: false, tab_role: 'background', content_mode: 'dom_text', elements,
      });
      const duplicate = s.lastCapture === s.signature;
      s.captures += 1; s.lastCapture = s.signature;
      return freeze({document_id: s.id, revision, duplicate, observation, snapshot,
        headings, tables, links, coverage: {scope: 'approved_manifest',
          manifest_complete: true, page_complete: false, work_items: s.data.work,
          limitation: 'No live-page acquisition or action target resolution'}});
    }
    function dispose(handle) { state(handle); documents.delete(handle); }
    return Object.freeze({createDocument, updateDocument, capture, dispose});
  }
  const api = Object.freeze({createPublicExtractor, ExtractionError, LIMITS});
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.WispPageExtractor = api;
})();
