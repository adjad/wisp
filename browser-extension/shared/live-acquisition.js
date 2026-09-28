/*
 * D1 per-click live capture (A05/A10 WP2). Shared by Chrome and Safari.
 *
 * This is a NEW boundary alongside page-extractor.js's reviewed-catalog path;
 * it does not relax or reuse that path. It has two halves:
 *
 *  1. collect(document, window) / collectOnce(): the fixed in-page collector.
 *     The extension injects private-filter.js and this file into the ISOLATED
 *     world of the one tab the user clicked, then calls collectOnce() with no
 *     arguments. collectOnce() deletes its own globals before reading anything,
 *     so it runs at most once per injection and page scripts never see it. It
 *     takes one batched pass over the rendered, visible DOM and returns a JSON
 *     string of filtered headings, text, tables, links and labeled controls.
 *     It never reads form values, drafts, hidden/visually hidden/aria-hidden
 *     content, frames, shadow trees, scripts or styles, and redacts secret-like
 *     text before any truncation.
 *
 *  2. createLiveCapture(): the trusted extension-side half. Trusted UI code
 *     issues a one-use, 30-second grant after a trusted user click. capture()
 *     consumes the grant, re-reads the trusted runtime context (which must be
 *     identical), re-validates and re-filters the collected data, and emits
 *     A01-valid SourceObservation (and, for a background tab, BrowserSnapshot)
 *     records with document-scoped IDs, revision and coverage.
 *
 * Private or unknown context yields no output: both the extension's own
 * incognito state and the native handler's private-context answer must be
 * exactly `false` (plan D1/D8). Nothing here decides privacy from content.
 * Coverage is always `page_complete: false`; absence is never evidence.
 */
'use strict';

(function () {
  const NODE = typeof module !== 'undefined' && module.exports;
  const F = NODE ? require('./private-filter.js') : globalThis.WispPrivateFilter;
  const contracts = () => (NODE ? require('./contracts.js') : globalThis.WispBrowserContracts);

  const LIMITS = Object.freeze({visit: 20000, depth: 192, blocks: 512, elements: 256,
    textUnits: 32768, blockUnits: 4096, labelUnits: 512, titleUnits: 512, cellUnits: 512,
    rows: 32, columns: 16, jsonUnits: 524288, grantMs: 30000, documents: 64});
  const FORMAT = 'wisp.live.v1';
  const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
  const ERROR_CODES = new Set(['invalid_payload', 'disabled', 'site_permission_denied',
    'private_context', 'stale_snapshot', 'unsupported_control']);

  class LiveCaptureError extends Error {
    constructor(code) {
      super('Live capture unavailable');
      this.name = 'LiveCaptureError';
      this.code = ERROR_CODES.has(code) ? code : 'invalid_payload';
    }
  }
  const fail = code => { throw new LiveCaptureError(code); };
  const check = (ok, code = 'invalid_payload') => { if (!ok) fail(code); };

  function truncate(value, max) {
    if (value.length <= max) return value;
    let cut = value.slice(0, max);
    const last = cut.charCodeAt(cut.length - 1);
    if (last >= 0xD800 && last <= 0xDBFF) cut = cut.slice(0, -1);
    const space = cut.lastIndexOf(' ');
    if (space > max * 0.6) cut = cut.slice(0, space);
    return cut.trim();
  }

  const BLOCK_DISPLAYS = /^(?:block|flex|grid|table|list-item|flow-root|table-row|table-cell|table-caption|table-row-group|table-header-group|table-footer-group|-webkit-box)/;
  const BLOCK_TAGS = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'BODY', 'DD', 'DETAILS',
    'DIALOG', 'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'HEADER',
    'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'SUMMARY', 'UL', 'LEGEND', 'CAPTION']);
  const CONTROL_ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem',
    'switch', 'combobox', 'textbox', 'searchbox', 'option']);
  const TEXT_INPUTS = new Set(['text', 'search', 'email', 'tel', 'url', 'number', 'date',
    'datetime-local', 'month', 'week', 'time', 'color', 'range']);

  // ------------------------------------------------------------ collector ---
  /*
   * One batched pass. Returns a JSON string (never live objects) so the
   * trusted side parses bounded data rather than coercing page-realm values.
   */
  function collect(doc, view) {
    const stats = {visited: 0, excluded: {}, redactions: {}, truncated: false, truncation: [],
      shadow_roots: 0, frames: 0, urls_withheld: 0, unlabeled_controls: 0,
      editable_document: false};
    const blocks = [];
    let elements = 0, textUnits = 0, stop = false;
    const labelsFor = new Map();
    const pending = [];
    const location = doc && doc.location ? String(doc.location.href) : '';
    const base = doc && typeof doc.baseURI === 'string' && doc.baseURI ? doc.baseURI : location;
    const clean = (value, max) => truncate(F.redact(value, stats.redactions), max);
    const trunc = reason => {
      stats.truncated = true;
      if (!stats.truncation.includes(reason)) stats.truncation.push(reason);
    };
    const exclude = reason => { stats.excluded[reason] = (stats.excluded[reason] || 0) + 1; };
    const attr = (el, name) => {
      try { const v = el.getAttribute(name); return typeof v === 'string' ? v : null; } catch (_) { return null; }
    };
    const tag = el => String(el.tagName || el.nodeName || '').toUpperCase();

    function emitText(kind, block, units) {
      if (stop) return false;
      if (blocks.length >= LIMITS.blocks) { trunc('blocks'); stop = true; return false; }
      const joined = units + (textUnits > 0 ? 1 : 0);
      if (textUnits + joined > LIMITS.textUnits) { trunc('text'); stop = true; return false; }
      textUnits += joined;
      blocks.push(Object.assign({kind}, block));
      return true;
    }
    function emitElement(block) {
      if (stop) return null;
      if (elements >= LIMITS.elements || blocks.length >= LIMITS.blocks) {
        trunc('elements'); stop = true; return null;
      }
      elements += 1;
      blocks.push(block);
      return block;
    }

    function style(el) {
      try { return view.getComputedStyle(el); } catch (_) { return null; }
    }
    // Returns a skip reason or null. Every element passes through here.
    function skipReason(el) {
      const s = style(el);
      if (!s) return 'unknown_style';
      const reason = F.exclusionReason(el, s);
      if (reason) return reason;
      const display = String(s.display || '');
      if (display === 'contents') return null; // No box of its own; children are checked.
      if (typeof el.checkVisibility === 'function') {
        let visible = false;
        try { visible = el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) === true; } catch (_) {}
        if (!visible) return 'hidden';
      } else if (typeof el.getClientRects === 'function') {
        let boxes = 0;
        try { boxes = el.getClientRects().length; } catch (_) {}
        if (boxes === 0) return 'hidden';
      }
      let rect = null;
      try { rect = el.getBoundingClientRect(); } catch (_) {}
      if (rect) {
        const overflow = String(s.overflow || '') + ' ' + String(s.overflowX || '') + ' ' + String(s.overflowY || '');
        if (/hidden|clip/.test(overflow) && rect.width <= 1 && rect.height <= 1) return 'visually_hidden';
        const sx = Number(view.scrollX) || 0, sy = Number(view.scrollY) || 0;
        if ((rect.width > 0 && rect.right + sx <= 0) || (rect.height > 0 && rect.bottom + sy <= 0)) {
          return 'visually_hidden';
        }
      }
      return null;
    }
    function isBlock(el) {
      const s = style(el);
      const display = s ? String(s.display || '') : '';
      return display ? BLOCK_DISPLAYS.test(display) : BLOCK_TAGS.has(tag(el));
    }
    function children(node) {
      const list = node && node.childNodes;
      return list ? Array.prototype.slice.call(list) : [];
    }
    function role(el) { return (attr(el, 'role') || '').trim().toLowerCase().split(/\s+/)[0]; }

    // A sink is {main: boolean, parts: string[]}.
    function flush(sink) {
      if (!sink.main || sink.parts.length === 0) return;
      const raw = sink.parts.join('');
      sink.parts.length = 0;
      const text = clean(raw, LIMITS.blockUnits);
      if (text) emitText('text', {text}, text.length);
    }
    function walkChildren(node, sink, depth) {
      for (const child of children(node)) { if (stop) return; walk(child, sink, depth + 1); }
    }
    function sub(node, depth) {
      const s = {main: false, parts: []};
      walkChildren(node, s, depth);
      return s.parts.join('');
    }
    function registerControl(el, controlRole, label, sink, visibleText) {
      const block = {kind: 'control', role: controlRole, label: label ? clean(label, LIMITS.labelUnits) : ''};
      if (visibleText) sink.parts.push(' ', visibleText, ' ');
      if (!emitElement(block)) return;
      if (!block.label) pending.push({block, id: attr(el, 'id')});
    }

    function walk(node, sink, depth) {
      if (stop) return;
      if (++stats.visited > LIMITS.visit) { trunc('visit_budget'); stop = true; return; }
      if (depth > LIMITS.depth) { trunc('depth'); return; }
      if (node.nodeType === 3) {
        if (typeof node.nodeValue === 'string') sink.parts.push(node.nodeValue);
        return;
      }
      if (node.nodeType !== 1) return;
      const el = node, t = tag(el);
      const reason = skipReason(el);
      if (reason) {
        exclude(reason);
        if (reason === 'frame') stats.frames += 1;
        return;
      }
      if (el.shadowRoot) {
        // Shadow trees are out of scope (R2 limits); light children of a
        // host may be unslotted and unrendered, so the whole host is skipped.
        stats.shadow_roots += 1; exclude('shadow_root');
        return;
      }
      const r = role(el);
      if (t === 'BR') { if (sink.main) flush(sink); else sink.parts.push(' '); return; }
      if (t === 'DETAILS' && !(el.hasAttribute && el.hasAttribute('open'))) {
        // Closed details: only the summary is rendered.
        if (sink.main) flush(sink);
        for (const child of children(el)) {
          if (child.nodeType === 1 && tag(child) === 'SUMMARY') walk(child, sink, depth + 1);
          else if (child.nodeType === 1 || (child.nodeType === 3 && String(child.nodeValue).trim())) exclude('collapsed');
        }
        if (sink.main) flush(sink);
        return;
      }
      if (/^H[1-6]$/.test(t) || r === 'heading') {
        let level = /^H[1-6]$/.test(t) ? Number(t[1]) : Number.parseInt(attr(el, 'aria-level') || '2', 10);
        if (!Number.isInteger(level) || level < 1 || level > 6) level = 2;
        if (!sink.main) { sink.parts.push(' '); walkChildren(el, sink, depth); sink.parts.push(' '); return; }
        flush(sink);
        const text = clean(sub(el, depth), LIMITS.blockUnits);
        if (text) emitText('heading', {text, level}, text.length);
        return;
      }
      if (t === 'TABLE' && sink.main) { flush(sink); table(el, depth); return; }
      if ((t === 'A' || t === 'AREA') && attr(el, 'href') !== null) {
        const raw = sub(el, depth);
        let label = clean(raw, LIMITS.labelUnits);
        if (!label) label = clean(attr(el, 'aria-label') || attr(el, 'title') || '', LIMITS.labelUnits);
        const resolved = F.safeURL(attr(el, 'href'), base || undefined);
        if (resolved.withheld) stats.urls_withheld += 1;
        // Inline links render inside their sentence; block-like links separate.
        if (isBlock(el) || /inline-(?:block|flex|grid)/.test(String((style(el) || {}).display || ''))) {
          sink.parts.push(' ', raw, ' ');
        } else sink.parts.push(raw);
        if (label) emitElement({kind: 'link', label, url: resolved.url});
        return;
      }
      if (t === 'INPUT') {
        const type = (attr(el, 'type') || 'text').toLowerCase();
        if (['button', 'submit', 'reset', 'image'].includes(type)) {
          // A button's value attribute is its authored caption, never user input.
          const caption = attr(el, 'aria-label') || attr(el, 'value') || attr(el, 'alt') ||
            (type === 'submit' ? 'Submit' : type === 'reset' ? 'Reset' : '');
          registerControl(el, 'button', caption, sink, caption);
        } else if (type === 'checkbox' || type === 'radio') {
          registerControl(el, type, attr(el, 'aria-label') || attr(el, 'title') || '', sink, '');
        } else if (TEXT_INPUTS.has(type)) {
          registerControl(el, 'textbox', attr(el, 'aria-label') || attr(el, 'placeholder') || attr(el, 'title') || '', sink, '');
        } else exclude('form_control');
        return;
      }
      if (r === 'textbox' || r === 'searchbox' || r === 'combobox') {
        // Custom editors may hold drafts: label only, never their contents.
        registerControl(el, r === 'combobox' ? 'combobox' : 'textbox', attr(el, 'aria-label') || attr(el, 'title') || '', sink, '');
        exclude('form_control');
        return;
      }
      if (t === 'BUTTON' || (CONTROL_ROLES.has(r) && r !== 'link')) {
        const text = sub(el, depth);
        const label = text.trim() ? text : (attr(el, 'aria-label') || attr(el, 'title') || '');
        const controlRole = t === 'BUTTON' ? 'button' : (r === 'searchbox' ? 'textbox' : r);
        registerControl(el, controlRole, label, sink, text);
        return;
      }
      if (t === 'LABEL') {
        const before = pending.length;
        const text = sub(el, depth);
        const label = clean(text, LIMITS.labelUnits);
        for (let i = before; i < pending.length; i++) if (!pending[i].block.label) pending[i].block.label = label;
        const target = attr(el, 'for');
        if (target && label && !labelsFor.has(target)) labelsFor.set(target, label);
        sink.parts.push(' ', text, ' ');
        return;
      }
      const block = isBlock(el);
      if (block) { if (sink.main) flush(sink); else sink.parts.push(' '); }
      walkChildren(el, sink, depth);
      if (block) { if (sink.main) flush(sink); else sink.parts.push(' '); }
    }

    function table(el, depth) {
      const rows = [];
      const rowEls = [];
      let caption = '';
      for (const child of children(el)) {
        if (child.nodeType !== 1) continue;
        const ct = tag(child);
        if (ct === 'CAPTION') {
          const reason = skipReason(child);
          if (reason) exclude(reason); else caption = clean(sub(child, depth + 1), LIMITS.blockUnits);
        } else if (ct === 'TR') rowEls.push([child, depth + 1]);
        else if (ct === 'THEAD' || ct === 'TBODY' || ct === 'TFOOT') {
          const reason = skipReason(child);
          if (reason) { exclude(reason); continue; }
          for (const row of children(child)) if (row.nodeType === 1 && tag(row) === 'TR') rowEls.push([row, depth + 2]);
        }
      }
      if (caption) emitText('text', {text: caption}, caption.length);
      for (const [row, rowDepth] of rowEls) {
        if (stop) return;
        if (++stats.visited > LIMITS.visit) { trunc('visit_budget'); stop = true; return; }
        const reason = skipReason(row);
        if (reason) { exclude(reason); continue; }
        if (rows.length >= LIMITS.rows) { trunc('table_rows'); break; }
        const cells = [];
        for (const cell of children(row)) {
          if (cell.nodeType !== 1 || !['TD', 'TH'].includes(tag(cell))) continue;
          if (++stats.visited > LIMITS.visit) { trunc('visit_budget'); stop = true; return; }
          const cellReason = skipReason(cell);
          if (cellReason) { exclude(cellReason); continue; }
          if (cells.length >= LIMITS.columns) { trunc('table_columns'); break; }
          cells.push(clean(sub(cell, rowDepth + 1), LIMITS.cellUnits));
        }
        if (cells.some(Boolean)) rows.push(cells);
      }
      if (rows.length) {
        const units = rows.reduce((n, row, i) => n + row.join(' | ').length + (i ? 1 : 0), 0);
        emitText('table', {rows}, units);
      }
    }

    let title = '';
    if (doc && String(doc.designMode || '').toLowerCase() === 'on') {
      stats.editable_document = true;
    } else if (doc) {
      title = clean(typeof doc.title === 'string' ? doc.title : '', LIMITS.titleUnits);
      const root = doc.body || doc.documentElement;
      if (root) {
        const main = {main: true, parts: []};
        walk(root, main, 0);
        flush(main);
      }
      for (const {block, id} of pending) {
        if (!block.label && id && labelsFor.has(id)) block.label = labelsFor.get(id);
      }
      for (let i = blocks.length - 1; i >= 0; i--) {
        if (blocks[i].kind === 'control' && !blocks[i].label) { blocks.splice(i, 1); stats.unlabeled_controls += 1; }
      }
    }
    // The page URL is reduced to a source identity before it leaves the page.
    return JSON.stringify({format: FORMAT, url: F.pageURL(location), title, blocks, stats});
  }

  function collectOnce() {
    // Remove every entry point before reading the page: one run per injection.
    try { delete globalThis.WispLiveAcquisition; } catch (_) {}
    try { delete globalThis.WispPrivateFilter; } catch (_) {}
    return collect(globalThis.document, globalThis);
  }

  // --------------------------------------------------------- trusted side ---
  const CONTEXT_KEYS = ['trigger', 'task_id', 'tab_id', 'document_id', 'url', 'enabled',
    'site_permission', 'extension_incognito', 'native_private_context', 'tab_role'];
  function parseJSON(raw, max) {
    check(typeof raw === 'string' && raw.length <= max);
    try { return JSON.parse(raw); } catch (_) { return fail('invalid_payload'); }
  }
  function closed(value, keys) {
    check(value !== null && typeof value === 'object' && !Array.isArray(value));
    const actual = Object.keys(value);
    check(actual.length === keys.length && keys.every(k => Object.hasOwn(value, k)));
  }
  /*
   * A page URL becomes the source identity. The fragment is dropped; a query
   * carrying a sensitive parameter is dropped as a whole; user-info, secret-
   * like paths and non-wire URLs refuse the capture.
   */
  function pageURL(raw) {
    const url = F.pageURL(raw);
    check(url !== null, 'site_permission_denied');
    return url;
  }
  function parseContext(raw) {
    const c = parseJSON(raw, 8192);
    closed(c, CONTEXT_KEYS);
    // Private or unknown context yields no output: exact `false` only, from
    // both the extension (incognito) and the native handler (plan D8).
    check(c.extension_incognito === false && c.native_private_context === false, 'private_context');
    check(c.enabled === true, 'disabled');
    check(c.site_permission === 'granted', 'site_permission_denied');
    check(c.trigger === 'user_click');
    check(typeof c.task_id === 'string' && ID.test(c.task_id));
    check(typeof c.document_id === 'string' && ID.test(c.document_id));
    check(Number.isSafeInteger(c.tab_id) && c.tab_id >= 0);
    check(c.tab_role === 'active' || c.tab_role === 'background');
    c.url = pageURL(c.url);
    return c;
  }
  function sameContext(a, b) { return CONTEXT_KEYS.every(k => a[k] === b[k]); }

  function text(value, max) {
    check(typeof value === 'string' && value.length <= max * 4);
    return value;
  }
  // Re-validate and re-filter collected data; the page realm is untrusted input.
  function sanitize(collectedJSON) {
    const data = parseJSON(collectedJSON, LIMITS.jsonUnits);
    closed(data, ['format', 'url', 'title', 'blocks', 'stats']);
    check(data.format === FORMAT && Array.isArray(data.blocks) && data.blocks.length <= LIMITS.blocks);
    const counts = {};
    const again = (value, max) => truncate(F.redact(text(value, max), counts), max);
    const stats = data.stats;
    closed(stats, ['visited', 'excluded', 'redactions', 'truncated', 'truncation', 'shadow_roots',
      'frames', 'urls_withheld', 'unlabeled_controls', 'editable_document']);
    const count = v => { check(Number.isSafeInteger(v) && v >= 0); return v; };
    const tally = obj => {
      check(obj !== null && typeof obj === 'object' && !Array.isArray(obj) && Object.keys(obj).length <= 32);
      const out = {};
      for (const key of Object.keys(obj).sort()) { check(/^[a-z_]{1,32}$/.test(key)); out[key] = count(obj[key]); }
      return out;
    };
    check(typeof stats.truncated === 'boolean' && typeof stats.editable_document === 'boolean');
    check(Array.isArray(stats.truncation) && stats.truncation.length <= 16 &&
      stats.truncation.every(r => typeof r === 'string' && /^[a-z_]{1,32}$/.test(r)));
    check(stats.editable_document === false, 'unsupported_control');
    const cleanStats = {visited: count(stats.visited), excluded: tally(stats.excluded),
      redactions: tally(stats.redactions), truncated: stats.truncated, truncation: stats.truncation.slice(),
      shadow_roots: count(stats.shadow_roots), frames: count(stats.frames),
      urls_withheld: count(stats.urls_withheld), unlabeled_controls: count(stats.unlabeled_controls)};
    const blocks = [];
    let elements = 0, textUnits = 0, truncatedHere = false;
    for (const b of data.blocks) {
      check(b !== null && typeof b === 'object' && !Array.isArray(b) && typeof b.kind === 'string');
      let out = null, units = 0;
      if (b.kind === 'heading') {
        closed(b, ['kind', 'text', 'level']);
        check(Number.isInteger(b.level) && b.level >= 1 && b.level <= 6);
        out = {kind: 'heading', text: again(b.text, LIMITS.blockUnits), level: b.level};
        units = out.text.length;
      } else if (b.kind === 'text') {
        closed(b, ['kind', 'text']);
        out = {kind: 'text', text: again(b.text, LIMITS.blockUnits)};
        units = out.text.length;
      } else if (b.kind === 'table') {
        closed(b, ['kind', 'rows']);
        check(Array.isArray(b.rows) && b.rows.length >= 1 && b.rows.length <= LIMITS.rows);
        const rows = b.rows.map(row => {
          check(Array.isArray(row) && row.length <= LIMITS.columns);
          return row.map(cell => again(cell, LIMITS.cellUnits));
        }).filter(row => row.some(Boolean));
        out = rows.length ? {kind: 'table', rows} : null;
        units = rows.reduce((n, row, i) => n + row.join(' | ').length + (i ? 1 : 0), 0);
      } else if (b.kind === 'link') {
        closed(b, ['kind', 'label', 'url']);
        check(b.url === null || typeof b.url === 'string');
        const resolved = b.url === null ? {url: null} : F.safeURL(b.url);
        out = {kind: 'link', label: again(b.label, LIMITS.labelUnits), url: resolved.url};
      } else if (b.kind === 'control') {
        closed(b, ['kind', 'role', 'label']);
        check(typeof b.role === 'string' && CONTROL_ROLES.has(b.role) && b.role !== 'link');
        out = {kind: 'control', role: b.role, label: again(b.label, LIMITS.labelUnits)};
      } else fail('invalid_payload');
      if (!out || (('text' in out) && !out.text) || (('label' in out) && !out.label)) continue;
      if (out.kind === 'link' || out.kind === 'control') {
        if (elements >= LIMITS.elements) { truncatedHere = true; break; }
        elements += 1;
      } else {
        const joined = units + (textUnits > 0 ? 1 : 0);
        if (textUnits + joined > LIMITS.textUnits) { truncatedHere = true; break; }
        textUnits += joined;
      }
      blocks.push(out);
    }
    if (truncatedHere) {
      cleanStats.truncated = true;
      if (!cleanStats.truncation.includes('trusted_bounds')) cleanStats.truncation.push('trusted_bounds');
    }
    for (const [k, v] of Object.entries(counts)) cleanStats.redactions[k] = (cleanStats.redactions[k] || 0) + v;
    const title = again(data.title, LIMITS.titleUnits);
    return {url: data.url, title, blocks, stats: cleanStats};
  }
  function deepFreeze(value) {
    if (value && typeof value === 'object' && !Object.isFrozen(value)) {
      Object.values(value).forEach(deepFreeze);
      Object.freeze(value);
    }
    return value;
  }

  function createLiveCapture(options = {}) {
    const C = contracts();
    check(C && typeof C.validate === 'function');
    const now = typeof options.now === 'function' ? options.now : Date.now;
    const uuid = typeof options.randomUUID === 'function' ? options.randomUUID
      : (NODE ? require('node:crypto').randomUUID : () => globalThis.crypto.randomUUID());
    const grants = new WeakMap();
    const documents = new Map();

    function clock() {
      const t = now();
      check(Number.isSafeInteger(t) && t >= 0);
      return t;
    }
    // Called by trusted extension UI code on a trusted (isTrusted) user click.
    function issueGrant(contextJSON) {
      const context = parseContext(contextJSON);
      const handle = Object.freeze(Object.create(null));
      grants.set(handle, {context, issued: clock()});
      return handle;
    }
    function documentState(context) {
      const key = context.tab_id + '|' + context.document_id;
      let s = documents.get(key);
      if (!s) {
        const id = 'lv.' + uuid();
        check(ID.test(id));
        s = {id, revision: 1, signature: null, captures: 0, lastCapture: null};
        documents.set(key, s);
        while (documents.size > LIMITS.documents) documents.delete(documents.keys().next().value);
      } else {
        documents.delete(key); documents.set(key, s); // LRU refresh
      }
      return s;
    }
    /*
     * Consumes the grant (even on failure). `currentContextJSON` must be read
     * again from browser/native APIs after the in-page collector returned.
     */
    function capture(handle, collectedJSON, currentContextJSON) {
      const grant = grants.get(handle);
      if (handle !== null && typeof handle === 'object') grants.delete(handle);
      check(grant !== undefined, 'stale_snapshot');
      const current = parseContext(currentContextJSON);
      const t = clock();
      check(t >= grant.issued && t - grant.issued <= LIMITS.grantMs, 'stale_snapshot');
      check(sameContext(grant.context, current), 'stale_snapshot');
      const data = sanitize(collectedJSON);
      check(typeof data.url === 'string' && F.pageURL(data.url) === current.url, 'stale_snapshot');
      const s = documentState(current);
      const signature = JSON.stringify([current.url, data.title, data.blocks]);
      if (s.signature !== null && s.signature !== signature) {
        check(s.revision < Number.MAX_SAFE_INTEGER);
        s.revision += 1;
      }
      s.signature = signature;
      check(s.captures < Number.MAX_SAFE_INTEGER);
      const revision = s.id + '.r' + s.revision, captureID = s.id + '.c' + (s.captures + 1);
      const blocks = data.blocks.map((b, i) => Object.assign({id: revision + '.n' + i}, b));
      const elements = blocks.filter(b => b.kind === 'link' || b.kind === 'control')
        .map(b => C.validate('BrowserElement', {target_id: b.id, role: b.kind === 'link' ? 'link' : b.role,
          label: b.label, editable: false}));
      const lines = [];
      for (const b of blocks) {
        if (b.kind === 'heading' || b.kind === 'text') lines.push(b.text);
        if (b.kind === 'table') for (const row of b.rows) lines.push(row.join(' | '));
      }
      const title = data.title || new URL(current.url).hostname;
      const observation = C.validate('SourceObservation', {
        schema_version: '1.0', id: captureID + '.o', source_kind: 'browser',
        source_url: current.url, source_record_id: s.id, revision, observed_at_ms: t,
        title: truncate(title, LIMITS.titleUnits), text: lines.join('\n'), private_context: false,
      });
      // A01 snapshots are background-tab records; a user's active tab read is
      // an observation only, with elements still A01 BrowserElement-valid.
      const snapshot = current.tab_role === 'background' ? C.validate('BrowserSnapshot', {
        schema_version: '1.0', id: captureID + '.s', task_id: current.task_id,
        observation_id: observation.id, url: current.url, origin: new URL(current.url).origin,
        captured_at_ms: t, enabled: true, site_permission: 'granted', private_context: false,
        tab_role: 'background', content_mode: 'dom_text', elements,
      }) : null;
      const duplicate = s.lastCapture === signature;
      s.captures += 1; s.lastCapture = signature;
      const stats = data.stats;
      const redactionTotal = Object.values(stats.redactions).reduce((a, b) => a + b, 0);
      return deepFreeze({
        source: 'live_capture', document_id: s.id, revision, duplicate,
        context: {trigger: 'user_click', tab_role: current.tab_role, private_context: false},
        observation, snapshot, blocks, elements,
        headings: blocks.filter(b => b.kind === 'heading').map(b => ({id: b.id, text: b.text, level: b.level})),
        tables: blocks.filter(b => b.kind === 'table').map(b => ({id: b.id, rows: b.rows})),
        links: blocks.filter(b => b.kind === 'link').map(b => ({id: b.id, label: b.label, url: b.url})),
        coverage: {scope: 'rendered_visible', page_complete: false, truncated: stats.truncated,
          truncation: stats.truncation, excluded: stats.excluded, redactions: redactionTotal,
          redaction_kinds: stats.redactions, shadow_roots_skipped: stats.shadow_roots,
          frames_skipped: stats.frames, urls_withheld: stats.urls_withheld,
          unlabeled_controls: stats.unlabeled_controls, visited: stats.visited,
          limitation: 'Filtered rendered visible text only; forms, drafts, hidden, frame and shadow content are excluded'},
      });
    }
    function forgetDocument(tabID, documentID) {
      return documents.delete(tabID + '|' + documentID);
    }
    return Object.freeze({issueGrant, capture, forgetDocument});
  }

  const api = Object.freeze({collect, collectOnce, createLiveCapture, LiveCaptureError, LIMITS, FORMAT});
  if (NODE) module.exports = api;
  else globalThis.WispLiveAcquisition = api;
})();
