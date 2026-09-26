/* Internal A05a DOM primitives. Deliberately not an A01 wire contract.
 * No ambient document access, installation, actions, network, or value capture.
 * A later adapter must apply authorization and map these records to A01.
 */
(function (host, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else host.WispPageExtractor = api;
})(globalThis, function () {
  'use strict';

  const identities = new WeakMap();
  const ignoredTags = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD']);
  const surfaceTags = new Set(['IFRAME', 'FRAME', 'CANVAS', 'OBJECT', 'EMBED', 'VIDEO', 'AUDIO', 'SVG']);
  const draftRoles = new Set(['textbox', 'searchbox', 'combobox', 'spinbutton']);
  const controlRoles = new Set(['button', 'checkbox', 'radio', 'switch', 'slider', 'tab', ...draftRoles]);
  const inputTypes = new Set(['text', 'search', 'email', 'tel', 'url', 'number', 'date', 'time', 'datetime-local', 'month', 'week', 'checkbox', 'radio', 'range', 'color', 'file', 'submit', 'reset', 'button']);
  const normalize = value => String(value || '').replace(/\s+/gu, ' ').trim();
  const attr = (element, name) => element.getAttribute(name);
  // Native names are small; custom names are page-controlled too.
  const tag = element => element.tagName.length <= 128 ? element.tagName.toUpperCase() : '';
  const oversized = Symbol('oversized input');
  const policyCaps = { role: 256, autocomplete: 1024, type: 64, contenteditable: 32, 'aria-hidden': 16, 'aria-disabled': 16 };
  const bounded = (value, fallback, ceiling) => Number.isInteger(value) && value > 0 ? Math.min(value, ceiling) : fallback;

  function documentIdentity(document) {
    let state = identities.get(document);
    if (!state) {
      // A random document namespace prevents identifiers from being mistaken for
      // stable locators across navigation. There is intentionally no DOM resolver.
      const crypto = document.defaultView?.crypto || globalThis.crypto;
      if (!crypto?.randomUUID) throw new Error('Opaque document identity requires crypto.randomUUID');
      state = { id: `d-${crypto.randomUUID()}`, elements: new WeakMap(), next: 0 };
      identities.set(document, state);
    }
    return state;
  }

  function createPageExtractor(document, options = {}) {
    if (!document || document.nodeType !== 9) throw new TypeError('Expected an explicit Document');
    if (options.root && (options.root.nodeType !== 1 || options.root.ownerDocument !== document)) {
      throw new TypeError('The extraction root must be an element in this Document');
    }
    const identity = documentIdentity(document);
    const limits = {
      nodes: bounded(options.maxNodes, 20000, 100000),
      depth: bounded(options.maxDepth, 128, 512),
      text: bounded(options.maxTextChars, 100000, 1000000),
      records: bounded(options.maxRecords, 500, 5000),
      string: bounded(options.maxStringChars, 1000, 10000),
    };
    const elementId = element => {
      if (!identity.elements.has(element)) identity.elements.set(element, `${identity.id}:e${++identity.next}`);
      return identity.elements.get(element);
    };

    function collect() {
      const reasons = new Set(['dom-only', 'generated-content-unread', 'closed-shadow-roots-unobservable', 'occlusion-unchecked']);
      const result = { documentId: identity.id, text: '', headings: [], tables: [], links: [], controls: [], coverage: null };
      // Reject semantic identifiers/URLs rather than truncating them into a
      // different meaning. Only prose uses a bounded, explicitly partial prefix.
      function readBounded(value, cap, reason = 'attribute-input-limit') {
        if (value !== null && value.length > cap) { reasons.add(reason); return oversized; }
        return value;
      }
      const readAttribute = (element, name, cap = limits.string) => readBounded(attr(element, name), cap);
      const policies = new WeakMap();
      function policy(element) {
        if (policies.has(element)) return policies.get(element);
        const fields = {};
        for (const [name, cap] of Object.entries(policyCaps)) {
          const value = readAttribute(element, name, cap);
          if (value === oversized) {
            reasons.add('privacy-metadata-excluded');
            policies.set(element, null);
            return null;
          }
          fields[name] = name === 'contenteditable' ? (value || '').toLowerCase() : normalize(value).toLowerCase();
        }
        const roleTokens = fields.role.split(' ');
        // Privacy is deliberately more conservative than effective-role
        // resolution: any bounded fallback token may denote an editable draft.
        fields.draftRole = roleTokens.some(token => draftRoles.has(token));
        fields.role = roleTokens[0];
        fields.autocomplete = fields.autocomplete.split(' ');
        policies.set(element, fields);
        return fields;
      }
      const role = element => policy(element)?.role || '';
      function sensitive(element) {
        const fields = policy(element);
        return !fields || ['data-wisp-private', 'data-sensitive', 'data-private', 'data-draft'].some(name => element.hasAttribute(name)) ||
          (tag(element) === 'INPUT' && ['password', 'hidden'].includes(fields.type)) ||
          fields.autocomplete.some(token => ['current-password', 'new-password', 'one-time-code'].includes(token) || token.startsWith('cc-'));
      }
      function draft(element) {
        const fields = policy(element);
        return !fields || (element.hasAttribute('contenteditable') && fields.contenteditable !== 'false') ||
          ['INPUT', 'TEXTAREA', 'SELECT', 'OPTION'].includes(tag(element)) || fields.draftRole;
      }

      const finish = () => {
        result.coverage = { complete: false, reasons: [...reasons].sort() };
        return result;
      };
      const root = options.root || document.body;
      if (!root || !document.documentElement?.contains(root)) {
        reasons.add('root-unavailable');
        return finish();
      }
      if (String(document.designMode).toLowerCase() === 'on') {
        reasons.add('editable-document-excluded');
        return finish();
      }
      const view = document.defaultView;
      if (!view?.getComputedStyle) {
        reasons.add('visibility-unavailable');
        return finish();
      }
      function excluded(element) {
        if (!tag(element)) { reasons.add('tag-name-input-limit'); return true; }
        if (ignoredTags.has(tag(element)) || sensitive(element) || element.hasAttribute('hidden') ||
            policy(element)['aria-hidden'] === 'true' || element.hasAttribute('inert')) return true;
        try {
          const style = view.getComputedStyle(element);
          if (!style) throw new Error('No style');
          return style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
            style.contentVisibility === 'hidden' || Number(style.opacity) === 0;
        } catch (_) {
          reasons.add('visibility-unavailable');
          return true;
        }
      }
      // A scoped root never bypasses the privacy/visibility of its ancestors.
      for (let parent = root.parentElement; parent; parent = parent.parentElement) {
        if (excluded(parent) || draft(parent) || surfaceTags.has(tag(parent))) {
          reasons.add('root-ancestor-excluded');
          return finish();
        }
        if (tag(parent) === 'DETAILS' && !parent.hasAttribute('open')) {
          const summary = Array.prototype.find.call(parent.children, child => tag(child) === 'SUMMARY');
          if (!summary?.contains(root)) { reasons.add('root-ancestor-excluded'); return finish(); }
        }
      }
      function hasRect(node, textEnd) {
        let range;
        try {
          let rects;
          if (node.nodeType === 3) {
            range = document.createRange();
            range.setStart(node, 0);
            range.setEnd(node, textEnd);
            rects = range.getClientRects();
          } else rects = node.getClientRects();
          return Array.prototype.some.call(rects, rect => rect.width > 0 && rect.height > 0);
        } catch (_) {
          reasons.add('geometry-unavailable');
          return false;
        } finally { range?.detach?.(); }
      }
      const records = [];
      const textParts = [];
      const byId = new Map();
      let nodes = 0;
      let chars = 0;
      let textInputRemaining = limits.text;
      // Cursor frames bound memory even for very wide or deeply nested trees.
      const stack = [{ node: root, parent: null, depth: 0, entered: false }];
      while (stack.length) {
        const frame = stack[stack.length - 1];
        const node = frame.node;
        if (!frame.entered) {
          if (++nodes > limits.nodes) { reasons.add('node-limit'); break; }
          if (frame.depth > limits.depth) { reasons.add('depth-limit'); stack.pop(); continue; }
          frame.entered = true;
          if (node.nodeType === 3) {
            const raw = node.nodeValue || '';
            const remaining = Math.max(0, limits.text - chars - (textParts.length ? 1 : 0));
            const inputLength = Math.min(raw.length, remaining, textInputRemaining);
            if (raw.length > inputLength) { reasons.add('text-input-limit'); reasons.add('text-limit'); }
            textInputRemaining -= inputLength;
            if (inputLength && hasRect(node, inputLength)) {
              const value = normalize(raw.slice(0, inputLength));
              if (value) {
                textParts.push(value);
                chars += value.length + (textParts.length > 1 ? 1 : 0);
              }
            }
            stack.pop();
            continue;
          }
          if (node.nodeType !== 1 || excluded(node)) { stack.pop(); continue; }
          if (surfaceTags.has(tag(node))) {
            reasons.add(tag(node) === 'IFRAME' || tag(node) === 'FRAME' ? 'frames-unread' : 'rendered-surface-unread');
            stack.pop();
            continue;
          }
          if (node.shadowRoot) reasons.add('shadow-dom-unread');
          const record = { element: node, parent: frame.parent, start: textParts.length, end: textParts.length };
          records.push(record);
          frame.record = record;
          const domId = readAttribute(node, 'id');
          if (typeof domId === 'string' && domId && !byId.has(domId)) byId.set(domId, record);
          if (draft(node)) {
            reasons.add('draft-values-excluded');
            stack.pop();
            continue;
          }
          frame.child = 0;
          frame.closedDetails = tag(node) === 'DETAILS' && !node.hasAttribute('open');
          frame.seenSummary = false;
        }
        const child = node.childNodes[frame.child++];
        if (child) {
          if (frame.closedDetails) {
            if (child.nodeType !== 1 || tag(child) !== 'SUMMARY' || frame.seenSummary) continue;
            frame.seenSummary = true;
          }
          stack.push({ node: child, parent: frame.record, depth: frame.depth + 1, entered: false });
        } else {
          frame.record.end = textParts.length;
          stack.pop();
        }
      }
      // Close ranges for ancestors when a traversal budget is exhausted.
      for (const frame of stack) if (frame.record) frame.record.end = textParts.length;
      result.text = textParts.join(' ');
      const clip = value => {
        if (value.length > limits.string) reasons.add('string-limit');
        return value.slice(0, limits.string);
      };
      const textOf = record => clip(textParts.slice(record.start, record.end).join(' '));
      let count = 0;
      // Check the budget before materializing labels, cell text, or URLs.
      function add(array, createRecord) {
        if (count >= limits.records) { reasons.add('record-limit'); return null; }
        const value = createRecord();
        array.push(value);
        count++;
        return value;
      }
      const labels = new Map();
      for (const record of records) {
        if (tag(record.element) === 'LABEL') {
          const target = readAttribute(record.element, 'for');
          if (typeof target === 'string' && target) {
            if (!labels.has(target)) labels.set(target, []);
            labels.get(target).push(record);
          }
        }
      }
      function labelOf(record) {
        const node = record.element;
        const rawReferences = readAttribute(node, 'aria-labelledby');
        if (rawReferences === oversized) { reasons.add('label-limit'); return ''; }
        const references = normalize(rawReferences);
        if (references) {
          const ids = references.split(' ');
          if (ids.length > 32) reasons.add('label-limit');
          const safe = ids.slice(0, 32).map(id => byId.get(id)).filter(Boolean).map(textOf).filter(Boolean);
          // Do not fall back to an unrelated aria-label if references are excluded.
          return clip(safe.join(' '));
        }
        const rawAria = readAttribute(node, 'aria-label');
        if (rawAria === oversized) { reasons.add('string-limit'); return ''; }
        const aria = normalize(rawAria);
        if (aria) return clip(aria);
        const id = readAttribute(node, 'id');
        const explicit = typeof id === 'string' ? labels.get(id) : null;
        if (explicit) {
          if (explicit.length > 32) reasons.add('label-limit');
          return clip(explicit.slice(0, 32).map(textOf).join(' '));
        }
        for (let parent = record.parent; parent; parent = parent.parent) {
          if (tag(parent.element) === 'LABEL') return textOf(parent);
        }
        return draft(node) ? '' : textOf(record);
      }
      let baseURI;
      function destination(node) {
        const raw = readBounded(attr(node, 'href'), limits.string, 'url-input-limit');
        if (!raw) return null;
        if (baseURI === undefined) baseURI = readBounded(document.baseURI, limits.string, 'url-input-limit');
        if (raw === oversized || baseURI === oversized) { reasons.add('link-destination-excluded'); return null; }
        try {
          const url = new URL(raw, baseURI);
          if (!['https:', 'http:'].includes(url.protocol)) { reasons.add('link-destination-excluded'); return null; }
          if (url.username || url.password || url.search || url.hash) reasons.add('link-destination-redacted');
          url.username = ''; url.password = ''; url.search = ''; url.hash = '';
          if (url.href.length > limits.string) { reasons.add('link-destination-excluded'); return null; }
          return url.href;
        } catch (_) { reasons.add('link-destination-excluded'); return null; }
      }
      const tables = new Map();
      const rows = new Map();
      const nearest = (record, name) => {
        for (let parent = record.parent; parent; parent = parent.parent) if (tag(parent.element) === name) return parent;
        return null;
      };
      const nestedByCell = new Map();
      for (const record of records) {
        if (tag(record.element) !== 'TABLE') continue;
        for (let parent = record.parent; parent; parent = parent.parent) {
          if (['TD', 'TH'].includes(tag(parent.element))) {
            if (!nestedByCell.has(parent)) nestedByCell.set(parent, []);
            nestedByCell.get(parent).push(record);
            break;
          }
        }
      }
      for (const record of records) {
        const node = record.element;
        const name = tag(node);
        const nodeRole = role(node);
        if (/^H[1-6]$/u.test(name) && record.end > record.start) {
          add(result.headings, () => ({ id: elementId(node), level: Number(name[1]), text: textOf(record) }));
        }
        if (name === 'TABLE') {
          const table = add(result.tables, () => ({ id: elementId(node), caption: '', rows: [] }));
          if (table) tables.set(record, table);
        }
        if (name === 'CAPTION') {
          const table = tables.get(nearest(record, 'TABLE'));
          if (table) table.caption = textOf(record);
        }
        if (name === 'TR') {
          const table = tables.get(nearest(record, 'TABLE'));
          const row = table && add(table.rows, () => ({ id: elementId(node), cells: [] }));
          if (row) rows.set(record, row);
        }
        if (name === 'TD' || name === 'TH') {
          const row = rows.get(nearest(record, 'TR'));
          // Flatten cell text but do not duplicate a nested table's text here.
          if (row) {
            add(row.cells, () => {
              const nested = nestedByCell.get(record) || [];
              const parts = textParts.slice(record.start, record.end);
              for (const child of nested) parts.fill('', child.start - record.start, child.end - record.start);
              return { id: elementId(node), header: name === 'TH', text: clip(normalize(parts.join(' '))) };
            });
          }
        }
        if (name === 'A' && attr(node, 'href') !== null && (record.end > record.start || hasRect(node))) {
          add(result.links, () => ({ id: elementId(node), text: textOf(record), destination: destination(node) }));
        }
        if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(name) || controlRoles.has(nodeRole)) {
          if (!hasRect(node)) continue;
          let kind = controlRoles.has(nodeRole) ? nodeRole : name.toLowerCase();
          if (name === 'INPUT') {
            const type = policy(node).type;
            kind = inputTypes.has(type) ? type : 'text';
          }
          add(result.controls, () => ({ id: elementId(node), kind, label: labelOf(record),
            disabled: node.disabled === true || policy(node)['aria-disabled'] === 'true' }));
        }
      }
      return finish();
    }

    // Explicit opt-in. Mutations are invalidation signals only: never serialize
    // MutationRecord.oldValue, target attributes, or any form values.
    function observe(onChange, settings = {}) {
      if (typeof onChange !== 'function') throw new TypeError('Expected a change callback');
      const Observer = settings.MutationObserver || document.defaultView?.MutationObserver;
      if (!Observer) throw new Error('MutationObserver unavailable');
      const timers = settings.timers || globalThis;
      const delay = bounded(settings.debounceMs, 50, 60000);
      const maximum = Math.max(delay, bounded(settings.maxWaitMs, 250, 60000));
      let trailing = null;
      let deadline = null;
      let stopped = false;
      let fingerprint = null;
      function cancel() {
        if (trailing !== null) timers.clearTimeout(trailing);
        if (deadline !== null) timers.clearTimeout(deadline);
        trailing = deadline = null;
      }
      function flush() {
        cancel();
        if (stopped) return;
        const snapshot = collect();
        const next = JSON.stringify(snapshot);
        if (next !== fingerprint) {
          fingerprint = next;
          onChange(snapshot);
        }
      }
      const observer = new Observer(() => {
        if (stopped) return;
        if (trailing !== null) timers.clearTimeout(trailing);
        trailing = timers.setTimeout(flush, delay);
        if (deadline === null) deadline = timers.setTimeout(flush, maximum);
      });
      observer.observe(document, { subtree: true, childList: true, characterData: true, attributes: true });
      const stop = () => { stopped = true; cancel(); observer.disconnect(); };
      try { flush(); } catch (error) { stop(); throw error; }
      return Object.freeze({ flush, stop });
    }

    return Object.freeze({ collect, observe });
  }
  return Object.freeze({ createPageExtractor });
});
