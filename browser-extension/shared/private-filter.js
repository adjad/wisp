/*
 * D1 live-capture private-content filter (A05/A10 WP2). Deterministic, pure,
 * no DOM, network, storage or model access. Shared by Chrome and Safari.
 *
 * Two jobs:
 *   1. Element exclusion: decide from an element's tag, attributes and
 *      computed style whether its whole subtree must never be read. Form
 *      controls, drafts, secret inputs, hidden and visually-hidden content,
 *      frames, scripts and styles are excluded.
 *   2. Text redaction: replace substrings that look like secrets or tokens
 *      (bearer/JWT/API keys/key=value secrets/one-time codes/card numbers/
 *      long high-entropy runs) with a fixed marker, and count them.
 *
 * This is a conservative filter, NOT a proof of privacy. Over-redaction is
 * acceptable; the ModelView and extraction treat any capture as partial.
 * It never decides whether a browsing context is private: that comes only
 * from browser/OS APIs through the trusted grant (plan D1/D8).
 */
'use strict';

(function () {
  const MARK = '[redacted]';

  // Elements whose subtree is never read. Values/options/drafts live here.
  const EXCLUDED_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME',
    'FRAME', 'FRAMESET', 'OBJECT', 'EMBED', 'CANVAS', 'SVG', 'VIDEO', 'AUDIO',
    'SELECT', 'OPTION', 'OPTGROUP', 'DATALIST', 'TEXTAREA', 'OUTPUT', 'METER',
    'PROGRESS', 'HEAD', 'META', 'LINK', 'BASE', 'TITLE', 'MAP', 'AREA', 'SLOT',
    'PORTAL', 'FENCEDFRAME']);
  const FRAME_TAGS = new Set(['IFRAME', 'FRAME', 'FRAMESET', 'OBJECT', 'EMBED',
    'PORTAL', 'FENCEDFRAME']);
  const FORM_TAGS = new Set(['SELECT', 'OPTION', 'OPTGROUP', 'DATALIST', 'TEXTAREA', 'OUTPUT']);
  const VISUALLY_HIDDEN_CLASS =
    /(?:^|\s)(?:sr-only|sr_only|visually-hidden|visuallyhidden|screenreader-only|screen-reader-only|screen-reader-text|a11y-hidden|offscreen|hidden-accessible)(?:\s|$)/i;
  // Input types that are never represented at all, not even by label.
  const SECRET_INPUT_TYPES = new Set(['password', 'hidden', 'file']);
  const SECRET_AUTOCOMPLETE =
    /(?:^|\s)(?:cc-[a-z-]+|one-time-code|current-password|new-password|webauthn|transaction-[a-z-]+)(?:\s|$)/i;
  const SECRET_FIELD_NAME =
    /pass(?:word|wd|code)?|pwd|secret|token|otp|one.?time|2fa|mfa|totp|cvv|cvc|csc|card.?(?:number|num|no)|cc.?(?:num|number|exp|csc)|ssn|social.?security|pin\b|api.?key|private.?key/i;

  const SENSITIVE_PARAM =
    /^(?:access_?token|refresh_?token|id_?token|auth|authorization|auth_?token|authenticity_token|csrf(?:_?token|middlewaretoken)?|_csrf|xsrf(?:_?token)?|nonce|state|code|code_verifier|session(?:_?id)?|sid|jsessionid|phpsessid|sig|signature|key|api_?key|apikey|secret|client_secret|password|passwd|pass|pwd|otp|verifier|ticket|saml(?:request|response)|x-amz-(?:signature|credential|security-token)|x-goog-signature|login_hint)$/i;
  const SENSITIVE_PARAM_PART = /token|nonce|secret|password|passwd|signature|credential|session/i;

  function attr(el, name) {
    try {
      const value = el.getAttribute(name);
      return typeof value === 'string' ? value : null;
    } catch (_) { return null; }
  }
  function tagName(el) {
    const name = typeof el.tagName === 'string' ? el.tagName : String(el.nodeName || '');
    return name.toUpperCase();
  }

  /*
   * Returns null when the element may be read, or a stable reason string when
   * its subtree must be skipped. `style` is the element's computed style (may
   * be null when unavailable, in which case only structural checks apply and
   * callers must add their own rendered-box checks).
   */
  function exclusionReason(el, style) {
    const tag = tagName(el);
    if (FRAME_TAGS.has(tag)) return 'frame';
    if (FORM_TAGS.has(tag)) return 'form_control';
    if (EXCLUDED_TAGS.has(tag)) return 'non_content';
    if (tag === 'INPUT') {
      const type = (attr(el, 'type') || 'text').toLowerCase();
      if (SECRET_INPUT_TYPES.has(type)) return 'secret_control';
      const auto = attr(el, 'autocomplete') || '';
      if (SECRET_AUTOCOMPLETE.test(auto)) return 'secret_control';
      const names = [attr(el, 'name'), attr(el, 'id'), attr(el, 'aria-label'), attr(el, 'placeholder')]
        .filter(Boolean).join(' ');
      if (SECRET_FIELD_NAME.test(names)) return 'secret_control';
    }
    if (el.hasAttribute && el.hasAttribute('hidden')) return 'hidden';
    if (el.hasAttribute && el.hasAttribute('inert')) return 'hidden';
    const ariaHidden = attr(el, 'aria-hidden');
    if (ariaHidden !== null && ariaHidden.trim().toLowerCase() === 'true') return 'aria_hidden';
    const editable = attr(el, 'contenteditable');
    if (editable !== null && editable.trim().toLowerCase() !== 'false') return 'editable';
    if (tag === 'DIALOG' && !(el.hasAttribute && el.hasAttribute('open'))) return 'hidden';
    const cls = attr(el, 'class');
    if (cls && VISUALLY_HIDDEN_CLASS.test(cls)) return 'visually_hidden';
    if (style) {
      const display = String(style.display || '');
      if (display === 'none') return 'hidden';
      const visibility = String(style.visibility || '');
      if (visibility === 'hidden' || visibility === 'collapse') return 'hidden';
      if (String(style.contentVisibility || '') === 'hidden') return 'hidden';
      const opacity = Number.parseFloat(style.opacity);
      if (Number.isFinite(opacity) && opacity <= 0) return 'hidden';
      if (clipsToNothing(style)) return 'visually_hidden';
    }
    return null;
  }
  function clipsToNothing(style) {
    const clip = String(style.clip || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (/^rect\(\s*0(?:px)?[ ,]+0(?:px)?[ ,]+0(?:px)?[ ,]+0(?:px)?\s*\)$/.test(clip)) return true;
    const clipPath = String(style.clipPath || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (/^inset\(\s*(?:50|100)%\s*\)$/.test(clipPath)) return true;
    if (/^(?:circle|ellipse)\(\s*0(?:px|%)?/.test(clipPath)) return true;
    return false;
  }

  // ---------------------------------------------------------------- text ----
  function wellFormed(value) {
    // Replace lone surrogates without lookbehind (older Safari support).
    let out = '';
    for (let i = 0; i < value.length; i++) {
      const c = value.charCodeAt(i);
      if (c >= 0xD800 && c <= 0xDBFF) {
        const d = i + 1 < value.length ? value.charCodeAt(i + 1) : 0;
        if (d >= 0xDC00 && d <= 0xDFFF) { out += value[i] + value[i + 1]; i++; continue; }
        out += '�'; continue;
      }
      if (c >= 0xDC00 && c <= 0xDFFF) { out += '�'; continue; }
      out += value[i];
    }
    return out;
  }
  function normalize(value) {
    if (typeof value !== 'string') return '';
    return wellFormed(value)
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‍⁠﻿]/g, '')
      .replace(/\s+/g, ' ').trim();
  }
  function luhn(digits) {
    let sum = 0, double = false;
    for (let i = digits.length - 1; i >= 0; i--) {
      let d = digits.charCodeAt(i) - 48;
      if (double) { d *= 2; if (d > 9) d -= 9; }
      sum += d; double = !double;
    }
    return sum % 10 === 0;
  }
  function entropy(value) {
    const counts = new Map();
    for (const c of value) counts.set(c, (counts.get(c) || 0) + 1);
    let bits = 0;
    for (const n of counts.values()) { const p = n / value.length; bits -= p * Math.log2(p); }
    return bits;
  }
  function highEntropyRun(run) {
    if (/^[0-9a-fA-F-]{32,}$/.test(run) && (run.match(/[0-9a-fA-F]/g) || []).length >= 32) return true;
    const lower = (run.match(/[a-z]/g) || []).length, upper = (run.match(/[A-Z]/g) || []).length;
    const digits = (run.match(/[0-9]/g) || []).length;
    // Tokens mix digits with letters, or many capitals with many lowercase;
    // a long natural word (one leading capital) does not qualify.
    const mixed = (digits >= 2 && lower + upper >= 2) || (upper >= 4 && lower >= 4);
    return mixed && entropy(run) >= 3.8;
  }

  // Ordered: specific patterns first so counts are attributed precisely.
  const RULES = [
    ['private_key', /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)/g, null],
    ['jwt', /\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}/g, null],
    ['auth_header', /\b(Bearer|Basic|Token|Digest)(\s+)([A-Za-z0-9._~+/=-]{8,})/gi, (m, a, s) => a + s + MARK],
    ['api_key', /\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}|\bglpat-[A-Za-z0-9_-]{16,}|\bnpm_[A-Za-z0-9]{30,}|\bhf_[A-Za-z0-9]{30,}/g, null],
    ['url_secret', /([?&#;](?:access_?token|refresh_?token|id_?token|token|auth_?token|authenticity_token|csrf_?token|nonce|code|session(?:_?id)?|sid|sig|signature|key|api_?key|apikey|secret|client_secret|password|passwd|pwd|otp|x-amz-signature|x-amz-credential|x-amz-security-token)=)([^&#\s"'<>]+)/gi, (m, k) => k + MARK],
    ['assignment', /\b(password|passwd|pwd|passcode|pass ?phrase|secret|api[ _-]?key|access[ _-]?token|auth[ _-]?token|refresh[ _-]?token|client[ _-]?secret|private[ _-]?key|session[ _-]?id|token|pin)(\s*[:=]\s*|\s+is\s+)("?)([^\s"',;]{3,})/gi,
      (m, k, sep, q) => k + sep + q + MARK],
    ['one_time_code', /\b(one[- ]time (?:pass)?code|verification code|security code|login code|sign[- ]in code|authentication code|access code|2fa code|mfa code|otp|passcode)(\b[^0-9\n]{0,24})(\d[\d -]{2,10}\d)/gi,
      (m, k, gap) => k + gap + MARK],
    ['code_first', /(?<![\d-])(\d[\d -]{2,10}\d)(\s+is\s+(?:your|the|my)\b[^.\n]{0,40}?\b(?:code|passcode|otp|pin)\b)/gi, (m, d, tail) => MARK + tail],
    ['code_label', /\b((?:verification|security|login|sign[- ]in|authentication|access|recovery|backup|reset|confirmation|one[- ]time|2fa|mfa)?\s?code)(\s*[:=]\s*|\s+)(\d[\d -]{2,10}\d|(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{8,32})(?![\w-])/gi,
      (m, k, sep, v) => (/\d{4,}/.test(v.replace(/[ -]/g, '')) || /[:=]/.test(sep) || /recovery|backup/i.test(k)) ? k + sep + MARK : m],
    ['bare_secret', /\b(password|passwd|passcode|secret|api[ _-]?key)(\s+)(?=[^\s]*\d)(?=[^\s]*[A-Za-z])([^\s"',;]{8,})/gi, (m, k, s) => k + s + MARK],
    // AWS-style 40-char secret keys: base64 with '/' or '+' and all three classes.
    // '/' stays out of RUN so path prefixes never hide hex tokens.
    ['aws_secret', /(?<![A-Za-z0-9+\/=_-])(?=[A-Za-z0-9+\/]{40}(?![A-Za-z0-9+\/=_-]))(?=[^\/+]*[\/+])(?=[^a-z]*[a-z])(?=[^A-Z]*[A-Z])(?=[^0-9]*[0-9])[A-Za-z0-9+\/]{40}/g, null],
    ['ssn', /(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)/g, null],
  ];
  // A run of digits joined only by single spaces or dashes. Card numbers are
  // searched for INSIDE each run (sub-windows of 13-19 digits) so an adjacent
  // CVV, expiry, quantity or date can never shield a Luhn-valid number.
  const DIGIT_RUN = /\d(?:[ -]?\d)*/g;
  const RUN = /[A-Za-z0-9_+=-]{32,}/g;

  /*
   * Normalizes and redacts one string. Always redact the complete string
   * BEFORE any truncation so a cut can never expose a partial secret that no
   * longer matches a pattern.
   */
  /*
   * Candidate card windows are unions of whole digit groups (split by the
   * single space/dash separators) holding 13-19 digits, plus, for a glued run
   * of 17+ digits, any 13-19 digit sub-window. EVERY Luhn-valid window is
   * redacted and overlapping spans are merged, so a coincidentally valid
   * window that cuts the card short can never leave part of it readable.
   */
  function redactCards(text, bump) {
    return text.replace(DIGIT_RUN, run => {
      const groups = [];
      const re = /\d+/g;
      let m;
      while ((m = re.exec(run)) !== null) groups.push({a: m.index, b: m.index + m[0].length, d: m[0]});
      const spans = [];
      const test = (a, b, digits) => { if (luhn(digits)) spans.push([a, b]); };
      for (let i = 0; i < groups.length; i++) {
        let digits = '';
        for (let j = i; j < groups.length; j++) {
          digits += groups[j].d;
          if (digits.length > 19) break;
          if (digits.length >= 13) test(groups[i].a, groups[j].b, digits);
        }
        const g = groups[i];
        if (g.d.length >= 17) {
          for (let start = 0; start + 13 <= g.d.length; start++) {
            for (let len = 13; len <= 19 && start + len <= g.d.length; len++) {
              test(g.a + start, g.a + start + len, g.d.slice(start, start + len));
            }
          }
        }
      }
      if (spans.length === 0) return run;
      spans.sort((x, y) => x[0] - y[0] || y[1] - x[1]);
      const merged = [spans[0].slice()];
      for (const [a, b] of spans.slice(1)) {
        const last = merged[merged.length - 1];
        if (a <= last[1]) last[1] = Math.max(last[1], b); else merged.push([a, b]);
      }
      let out = '', pos = 0;
      for (const [a, b] of merged) { bump('card_number'); out += run.slice(pos, a) + MARK; pos = b; }
      return out + run.slice(pos);
    });
  }
  function redact(value, counts, options) {
    let text = normalize(value);
    if (!text) return '';
    const bump = kind => { if (counts) counts[kind] = (counts[kind] || 0) + 1; };
    for (const [kind, pattern, replace] of RULES) {
      text = text.replace(pattern, (...args) => {
        bump(kind);
        return replace ? replace(...args) : MARK;
      });
    }
    text = redactCards(text, bump);
    // Dotted card groups (4111.1111.1111.1111) only in the 4-digit-group shape,
    // so decimal grades and versions are never Luhn-tested.
    text = text.replace(/(?<![\d.])\d{4}(?:\.\d{4}){2,3}(?![\d.])/g, m => {
      if (!luhn(m.replace(/\./g, ''))) return m;
      bump('card_number');
      return MARK;
    });
    if (options && options.skipEntropy === true) return text;
    text = text.replace(RUN, match => {
      if (highEntropyRun(match)) { bump('high_entropy'); return MARK; }
      return match;
    });
    return text;
  }
  function looksSecret(value) {
    const counts = {};
    redact(value, counts);
    return Object.keys(counts).length > 0;
  }

  // ----------------------------------------------------------------- URLs ---
  const WIRE_URL =
    /^https?:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$/;
  function sensitiveParam(name) {
    return SENSITIVE_PARAM.test(name) || SENSITIVE_PARAM_PART.test(name);
  }
  /*
   * Resolves an href against a base and returns an A01 wire-grammar HTTP(S)
   * URL with no user-info and no fragment, or null. A URL carrying a
   * sensitive query parameter (by name or secret-like value) is withheld
   * entirely (`withheld: true`) rather than silently rewritten, so a link can
   * never navigate somewhere different from what the page offered.
   */
  function safeURL(href, base) {
    if (typeof href !== 'string' || href.length === 0 || href.length > 4096) return {url: null, withheld: false};
    let url;
    try { url = base === undefined ? new URL(href) : new URL(href, base); } catch (_) { return {url: null, withheld: false}; }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return {url: null, withheld: false};
    if (url.username || url.password) return {url: null, withheld: true};
    let secret = false;
    try {
      for (const [name, value] of url.searchParams) {
        if (sensitiveParam(name) || looksSecret(value)) { secret = true; break; }
      }
    } catch (_) { secret = true; }
    if (secret || pathLooksSecret(url.pathname)) return {url: null, withheld: true};
    url.hash = '';
    const out = url.href;
    if (out.length > 4096 || !WIRE_URL.test(out)) return {url: null, withheld: false};
    return {url: out, withheld: false};
  }
  /*
   * A page's own URL as a source identity: fragment dropped, a query with any
   * sensitive parameter dropped as a whole. Returns null for user-info,
   * secret-like paths, non-HTTP(S) and non-wire URLs.
   */
  function pageURL(raw) {
    if (typeof raw !== 'string' || raw.length === 0 || raw.length > 4096) return null;
    let url;
    try { url = new URL(raw); } catch (_) { return null; }
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.username || url.password) return null;
    url.hash = '';
    const direct = safeURL(url.href);
    if (direct.url) return direct.url;
    url.search = '';
    return safeURL(url.href).url;
  }
  /*
   * Path check: every secret rule applies, but the high-entropy rule ignores
   * hyphen/underscore-separated title slugs (Canvas Pages use them, for
   * example week-3-reading-and-discussion-prompts-for-unit-2). A slug is a
   * run whose pieces are mostly plain lowercase words or plain numbers; a
   * token-shaped run (mixed-case or digit-mixed pieces) is still refused.
   */
  function isSlug(run) {
    const pieces = run.split(/[-_]/).filter(Boolean);
    if (pieces.length < 3 || pieces.some(p => p.length > 24)) return false;
    // Real words have vowels; consonant-only pieces are token-shaped.
    if (pieces.some(p => /^[a-z]+$/.test(p) && p.length > 3 && !/[aeiouy]/.test(p))) return false;
    const plain = pieces.filter(p => /^[a-z]+$/.test(p) || /^[0-9]+$/.test(p)).length;
    return plain / pieces.length >= 0.75;
  }
  function vowelless(run) {
    const pieces = run.split(/[-_]/).filter(Boolean);
    return pieces.length >= 3 && pieces.filter(p => /^[a-z]+$/.test(p) && p.length > 3 && !/[aeiouy]/.test(p)).length >= 2;
  }
  function pathLooksSecret(pathname) {
    const decoded = decodeSafe(pathname);
    const counts = {};
    redact(decoded, counts, {skipEntropy: true});
    if (Object.keys(counts).length > 0) return true;
    const runs = normalize(decoded).match(RUN) || [];
    return runs.some(run => (!isSlug(run) && highEntropyRun(run)) || vowelless(run));
  }
  function decodeSafe(path) {
    try { return decodeURIComponent(path).replace(/\//g, ' '); } catch (_) { return path.replace(/\//g, ' '); }
  }

  const api = Object.freeze({MARK, exclusionReason, clipsToNothing, normalize, redact,
    looksSecret, safeURL, pageURL, sensitiveParam, WIRE_URL, VISUALLY_HIDDEN_CLASS});
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.WispPrivateFilter = api;
})();
