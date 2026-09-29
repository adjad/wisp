/*
 * ModelView builder (A05 R1/R2, A10 WP2). Deterministic, pure, shared by
 * Chrome and Safari. Service-internal: NOT an A01 wire record.
 *
 * A ModelView is the bounded, ranked, numbered projection of a live capture
 * that a local model actually sees:
 *   - numbered elements, each mapped to a capture/snapshot target ID;
 *   - `url_ref` only for in-scope HTTP(S) anchors (the model never sees or
 *     emits a URL; code resolves a choice number back to target and URL);
 *   - consequential links (logout, delete, unsubscribe, submit, confirm,
 *     accept, pay, token/nonce query parameters and, for Canvas, submission,
 *     quiz-start and mark-as-done endpoints) are removed before ranking;
 *   - a page summary of headings plus ranked text;
 *   - token budgets from the plan's Model runtime contract;
 *   - coverage `complete` only when nothing was cut, skipped, redacted or removed. A truncated view is a
 *     partial read and must never support completion or deletion inferences.
 *
 * Private or unknown context yields no output. The consequential pattern
 * list is owned by A13 and reviewed by the Release Auditor; this module's
 * copy is the A05 capture-side exclusion (defense in depth), not approval.
 */
'use strict';

(function () {
  const NODE = typeof module !== 'undefined' && module.exports;
  const C = NODE ? require('./contracts.js') : globalThis.WispBrowserContracts;

  /*
   * Budgets in estimated tokens. `a14c_tier2` is R2's proposed Tier 2 (Ling)
   * budget: 5 candidates in 1,500 tokens and a 1,000-token page summary.
   * `generic_r1` is the deferred R1 generic budget: 60 elements plus 2,500
   * text tokens within a 6,650 total; its element share is derived from the
   * same fixed segments (6,650 - 2,500 - 1,200 - 200 - 600 - 60 = 2,090).
   */
  const PROFILES = Object.freeze({
    a14c_tier2: Object.freeze({maxElements: 5, elementTokens: 1500, summaryTokens: 1000,
      navigableOnly: true, labelUnits: 200, headingUnits: 160}),
    generic_r1: Object.freeze({maxElements: 60, elementTokens: 2090, summaryTokens: 2500,
      navigableOnly: false, labelUnits: 120, headingUnits: 80}),
  });
  // Conservative estimate (about 3 UTF-16 units per token plus one per line);
  // real tokenizers average closer to 4 for English, so this over-counts.
  const tokens = value => Math.ceil(value.length / 3) + 1;

  class ModelViewError extends Error {
    constructor(code) {
      super('ModelView unavailable');
      this.name = 'ModelViewError';
      this.code = code === 'private_context' ? code : 'invalid_payload';
    }
  }
  const check = (ok, code) => { if (!ok) throw new ModelViewError(code); };

  // ---------------------------------------------------- consequential links --
  // Verbs match as substrings so concatenated and camelCase segments such as
  // /deleteAccount, /removeItem/5, /submitForm, /confirmOrder, /user/logoutAll
  // are caught. Over-exclusion is the safe direction for GET navigation.
  const SEP = '(?:^|[\\/_.?&=;:\\s-])';
  const GENERAL_TARGET = [
    ['logout', /log[-_ ]?out|sign[-_ ]?out|signoff|log[-_ ]?off/i],
    ['delete', /delete|destroy|remove|erase/i],
    ['unsubscribe', /unsubscribe|opt[-_ ]?out/i],
    ['submit', /submit|submission/i],
    ['confirm', /confirm/i],
    ['accept', /accept|approve/i],
    // "pay" only at a segment start or a camelCase hump, so display/repay pass.
    ['pay', new RegExp(SEP + 'pay', 'i')],
    ['pay', /[a-z0-9]Pay(?![a-z])/],
    ['pay', /checkout|purchase|billing|donate|payment/i],
    ['enroll', /enroll|unenroll|register|cancel|withdraw/i],
  ];
  const CANVAS_TARGET = [
    ['canvas_submission', /\/(?:assignments|quizzes|discussion_topics)\/\d+\/submissions?(?:$|[/?])/i],
    ['canvas_quiz_start', /\/quizzes\/\d+\/(?:take|start|launch|resume)(?:$|[/?])|[?&](?:take|start_quiz|take_quiz)=|take_quiz|start_quiz/i],
    ['canvas_mark_done', /\/modules\/items\/\d+\/(?:done|mark_as_done|mark_done)(?:$|[/?])|mark_as_done|mark_done|must_mark_done/i],
    ['canvas_join_reset', /\/conferences\/\d+\/join(?:$|[/?])|\/(?:reset|reject)(?:$|[/?_])|turnitin|\/upload(?:$|[/?])/i],
    ['canvas_external_tool', /\/external_tools\/|\/lti\/|\/launch(?:$|[/?])/i],
  ];
  const LABEL = /\b(?:log ?out|log ?off|sign ?out|delete|remove|unsubscribe|submit|confirm|accept|pay|payment|checkout|purchase|mark as (?:done|complete)|start (?:the )?quiz|take (?:the )?quiz|begin (?:the )?quiz|resume (?:the )?quiz|retake)\b/i;
  const SENSITIVE_PARAM = /token|nonce|csrf|xsrf|authenticity|secret|password|passwd|signature|credential|session|^(?:sig|sid|key|api_?key|apikey|auth|code|state|otp)$/i;

  function decode(value) {
    // Decode repeatedly so double/triple-encoded segments (%2564) cannot hide.
    let out = value;
    for (let i = 0; i < 4; i++) {
      let next;
      try { next = decodeURIComponent(out); } catch (_) { break; }
      if (next === out) break;
      out = next;
    }
    return out;
  }
  /*
   * Returns the list of matched pattern names (empty when not consequential).
   * `site` is 'canvas' or 'generic'; Canvas patterns are always checked for
   * Canvas and additionally applied when a path looks like a Canvas course.
   */
  function consequentialReasons(url, label, site) {
    const reasons = [];
    if (typeof label === 'string' && LABEL.test(label)) reasons.push('label');
    if (typeof url === 'string') {
      let parsed = null;
      try { parsed = new URL(url); } catch (_) { reasons.push('unparsable'); }
      if (parsed) {
        const target = decode(parsed.pathname) + decode(parsed.search);
        for (const [name, pattern] of GENERAL_TARGET) if (pattern.test(target)) reasons.push(name);
        for (const [name] of parsed.searchParams) {
          if (SENSITIVE_PARAM.test(name)) { reasons.push('sensitive_param'); break; }
        }
        if (site === 'canvas' || /\/courses\/\d+/.test(parsed.pathname)) {
          for (const [name, pattern] of CANVAS_TARGET) if (pattern.test(target)) reasons.push(name);
        }
      }
    }
    return reasons;
  }

  // ------------------------------------------------------------- ranking ----
  const RELEVANT = /assignment|due|deadline|syllabus|module|quiz|exam|midterm|final|homework|project|lab\b|discussion|week|unit|reading|schedule|calendar|page/i;
  const CANVAS_PATH = /\/courses\/\d+\/(?:assignments|modules|pages|syllabus|quizzes|discussion_topics|assignments\/syllabus)(?:$|\/)/i;
  const DATE_TEXT = /\b(?:due|deadline|available|until|closes|opens|by)\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b/i;

  function heuristic(element) {
    let score = 0;
    if (RELEVANT.test(element.label)) score += 0.5;
    if (element.path && RELEVANT.test(element.path)) score += 0.2;
    if (element.path && CANVAS_PATH.test(element.path)) score += 0.3;
    return score;
  }
  function cut(value, max) {
    if (value.length <= max) return value;
    let out = value.slice(0, max - 1);
    const last = out.charCodeAt(out.length - 1);
    if (last >= 0xD800 && last <= 0xDBFF) out = out.slice(0, -1);
    return out.trimEnd() + '…';
  }

  // ------------------------------------------------------------- builder ----
  const views = new WeakMap();
  const ORIGIN = /^https?:\/\/[A-Za-z0-9.-]+(?::[0-9]{1,5})?$/;

  function validateCapture(capture) {
    check(capture !== null && typeof capture === 'object', 'invalid_payload');
    // Private or unknown context: no output, before touching any content.
    const context = capture.context;
    check(context !== null && typeof context === 'object' && context.private_context === false, 'private_context');
    check(capture.source === 'live_capture', 'invalid_payload');
    let observation;
    try {
      observation = C.validate('SourceObservation', capture.observation);
      if (capture.snapshot !== null) {
        const snapshot = C.validate('BrowserSnapshot', capture.snapshot);
        check(snapshot.observation_id === observation.id, 'invalid_payload');
      }
    } catch (error) {
      if (error instanceof ModelViewError) throw error;
      throw new ModelViewError('invalid_payload');
    }
    check(observation.source_kind === 'browser' && observation.private_context === false, 'private_context');
    check(Array.isArray(capture.blocks) && capture.blocks.length <= 512, 'invalid_payload');
    const coverage = capture.coverage;
    check(coverage && typeof coverage === 'object' && typeof coverage.truncated === 'boolean' &&
      coverage.page_complete === false, 'invalid_payload');
    return observation;
  }

  const isText = value => typeof value === 'string' && value.length > 0 && value.length <= 4096;
  function validBlock(block) {
    if (!block || typeof block !== 'object' || typeof block.id !== 'string' || block.id.length > 128) return false;
    switch (block.kind) {
      case 'heading': return isText(block.text) && Number.isInteger(block.level) && block.level >= 1 && block.level <= 6;
      case 'text': return isText(block.text);
      case 'table': return Array.isArray(block.rows) && block.rows.every(row => Array.isArray(row) &&
        row.every(cell => typeof cell === 'string' && cell.length <= 4096));
      case 'link': return isText(block.label) && (block.url === null || typeof block.url === 'string');
      case 'control': return isText(block.label) && typeof block.role === 'string';
      default: return false;
    }
  }

  /*
   * buildModelView(capture, {profile, site, scope_origins, scores})
   *   profile: 'a14c_tier2' (default) | 'generic_r1'
   *   site: 'canvas' | 'generic' (default 'generic')
   *   scope_origins: granted site scope; defaults to the page's own origin
   *   scores: optional {target_id: number in [0, 1]} from a trusted ranker
   *     (for example Laya link relevance). Unknown IDs or non-finite values
   *     are rejected. Ties break by document order.
   */
  function buildModelView(capture, options = {}) {
    const observation = validateCapture(capture);
    const profileName = options.profile === undefined ? 'a14c_tier2' : options.profile;
    check(Object.hasOwn(PROFILES, profileName), 'invalid_payload');
    const profile = PROFILES[profileName];
    const site = options.site === undefined ? 'generic' : options.site;
    check(site === 'canvas' || site === 'generic', 'invalid_payload');
    const pageOrigin = new URL(observation.source_url).origin;
    const scope = options.scope_origins === undefined ? [pageOrigin] : options.scope_origins;
    check(Array.isArray(scope) && scope.length >= 1 && scope.length <= 16 &&
      scope.every(o => typeof o === 'string' && ORIGIN.test(o)), 'invalid_payload');

    const targets = new Set();
    const candidates = [];
    let heading = null;
    capture.blocks.forEach((block, index) => {
      check(validBlock(block), 'invalid_payload');
      if (block.kind === 'heading') heading = block.text;
      if (block.kind !== 'link' && block.kind !== 'control') return;
      check(!targets.has(block.id), 'invalid_payload');
      targets.add(block.id);
      candidates.push({index, block, heading});
    });
    let scores = null;
    if (options.scores !== undefined) {
      check(options.scores !== null && typeof options.scores === 'object' && !Array.isArray(options.scores), 'invalid_payload');
      scores = new Map();
      for (const [id, value] of Object.entries(options.scores)) {
        check(targets.has(id) && typeof value === 'number' && Number.isFinite(value) &&
          value >= 0 && value <= 1, 'invalid_payload');
        scores.set(id, value);
      }
    }

    const excluded = {consequential: 0, out_of_scope: 0, not_navigable: 0, over_budget: 0};
    const pool = [];
    for (const {index, block, heading: near} of candidates) {
      const isLink = block.kind === 'link';
      const url = isLink ? block.url : null;
      if (consequentialReasons(url, block.label, site).length) { excluded.consequential += 1; continue; }
      let inScope = false, path = null;
      if (url !== null) {
        try {
          const parsed = new URL(url);
          inScope = (parsed.protocol === 'http:' || parsed.protocol === 'https:') && scope.includes(parsed.origin);
          if (inScope) path = parsed.pathname;
        } catch (_) { inScope = false; }
        if (!inScope) excluded.out_of_scope += 1;
      }
      const navigable = isLink && inScope;
      if (profile.navigableOnly && !navigable) {
        if (!(url !== null && !inScope)) excluded.not_navigable += 1;
        continue;
      }
      const element = {target_id: block.id, role: isLink ? 'link' : block.role,
        label: cut(block.label, profile.labelUnits), heading: near ? cut(near, profile.headingUnits) : null,
        path, url: navigable ? url : null, navigable};
      const score = scores && scores.has(block.id) ? scores.get(block.id) : (scores ? 0 : heuristic(element));
      pool.push({element, score, index});
    }
    pool.sort((a, b) => (Number(b.element.navigable) - Number(a.element.navigable)) ||
      (b.score - a.score) || (a.index - b.index));

    const elements = [], urlRefs = {};
    let elementTokens = 0, elementsTruncated = false;
    for (const {element} of pool) {
      const n = elements.length + 1;
      const line = renderElement(Object.assign({n, url_ref: element.navigable ? 'u' + n : null}, element));
      const cost = tokens(line);
      if (elements.length >= profile.maxElements || elementTokens + cost > profile.elementTokens) {
        elementsTruncated = true; excluded.over_budget += 1; continue;
      }
      elementTokens += cost;
      const ref = element.navigable ? 'u' + n : null;
      if (ref) urlRefs[ref] = element.url;
      elements.push(Object.freeze({n, target_id: element.target_id, role: element.role,
        label: element.label, heading: element.heading, path: element.path,
        url_ref: ref, navigable: element.navigable}));
    }

    // Summary: title and headings in document order, then text ranked with
    // date-bearing blocks first; ties by document order.
    const title = cut(observation.title, 200);
    const headings = [], texts = [];
    capture.blocks.forEach((block, index) => {
      if (block.kind === 'heading') headings.push({kind: 'heading', level: block.level, text: block.text});
      else if (block.kind === 'text') texts.push({kind: 'text', text: block.text, index});
      else if (block.kind === 'table') {
        for (const row of block.rows) texts.push({kind: 'table_row', text: row.join(' | '), index});
      }
    });
    texts.sort((a, b) => (Number(DATE_TEXT.test(b.text)) - Number(DATE_TEXT.test(a.text))) || (a.index - b.index));
    const summary = [];
    let summaryTokens = tokens('Page: ' + title), summaryTruncated = false;
    for (const item of [...headings, ...texts]) {
      const prefix = item.kind === 'heading' ? '#'.repeat(item.level) + ' ' : '';
      const cost = tokens(prefix + item.text);
      if (summaryTokens + cost <= profile.summaryTokens) {
        summaryTokens += cost;
        summary.push(Object.freeze(item.kind === 'heading'
          ? {kind: 'heading', level: item.level, text: item.text} : {kind: item.kind, text: item.text}));
        continue;
      }
      summaryTruncated = true;
      const room = (profile.summaryTokens - summaryTokens - 2) * 3 - prefix.length;
      if (room >= 60) {
        const text = cut(item.text, room);
        summaryTokens += tokens(prefix + text);
        summary.push(Object.freeze(item.kind === 'heading'
          ? {kind: 'heading', level: item.level, text} : {kind: item.kind, text}));
      }
      break;
    }

    const captureTruncated = capture.coverage.truncated === true;
    // Known gaps: a capture never proves the page was fully read, so any
    // skipped frame/shadow root, redaction, withheld URL or removed element
    // makes the view partial. The counters travel with the view.
    const cov = capture.coverage;
    const num = v => (Number.isSafeInteger(v) && v >= 0 ? v : 0);
    const gaps = Object.freeze({page_complete: false,
      frames_skipped: num(cov.frames_skipped), shadow_roots_skipped: num(cov.shadow_roots_skipped),
      redactions: num(cov.redactions), urls_withheld: num(cov.urls_withheld),
      unlabeled_controls: num(cov.unlabeled_controls),
      consequential_removed: excluded.consequential, out_of_scope_removed: excluded.out_of_scope});
    const knownGap = gaps.frames_skipped + gaps.shadow_roots_skipped + gaps.redactions +
      gaps.urls_withheld + gaps.unlabeled_controls + gaps.consequential_removed +
      gaps.out_of_scope_removed > 0;
    const view = Object.freeze({
      profile: profileName, site,
      document_id: String(capture.document_id), revision: observation.revision,
      observation_id: observation.id, snapshot_id: capture.snapshot ? capture.snapshot.id : null,
      title,
      coverage: captureTruncated || summaryTruncated || elementsTruncated || knownGap ? 'partial' : 'complete',
      gaps,
      truncated: Object.freeze({capture: captureTruncated, summary: summaryTruncated, elements: elementsTruncated}),
      summary: Object.freeze(summary),
      elements: Object.freeze(elements),
      excluded: Object.freeze(excluded),
      tokens: Object.freeze({summary: summaryTokens, elements: elementTokens}),
      budget: profile,
    });
    views.set(view, Object.freeze(Object.assign(Object.create(null), urlRefs)));
    return view;
  }

  function renderElement(e) {
    let line = '[' + e.n + '] ' + e.role + ' "' + e.label.replace(/"/g, "'") + '"';
    if (e.path) line += ' ' + e.path;
    if (e.heading) line += ' (section: ' + e.heading.replace(/[()]/g, '') + ')';
    return line;
  }
  // Deterministic text given to the model. Never contains a full URL.
  function renderModelView(view) {
    check(views.has(view), 'invalid_payload');
    const lines = ['Page: ' + view.title, 'Coverage: ' + view.coverage];
    for (const item of view.summary) {
      lines.push(item.kind === 'heading' ? '#'.repeat(item.level) + ' ' + item.text : item.text);
    }
    lines.push('Elements:');
    for (const element of view.elements) lines.push(renderElement(element));
    return lines.join('\n');
  }
  /*
   * Maps a model choice back to a target. Only integers that name a
   * navigable element in THIS view resolve; everything else (out-of-view
   * numbers, strings, `back`/`handoff`, forged views) returns null and is
   * handled by the executor's own mapping.
   */
  function resolveChoice(view, choice) {
    const refs = views.get(view);
    if (!refs || !Number.isSafeInteger(choice) || choice < 1 || choice > view.elements.length) return null;
    const element = view.elements[choice - 1];
    if (!element.navigable || !element.url_ref || !Object.hasOwn(refs, element.url_ref)) return null;
    return Object.freeze({command: 'navigate', target_id: element.target_id, url: refs[element.url_ref]});
  }

  const api = Object.freeze({buildModelView, renderModelView, resolveChoice, consequentialReasons,
    estimateTokens: tokens, PROFILES, ModelViewError});
  if (NODE) module.exports = api;
  else globalThis.WispModelView = api;
})();
