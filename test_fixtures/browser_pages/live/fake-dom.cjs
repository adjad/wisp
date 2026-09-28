/*
 * Minimal synthetic DOM for the live-capture tests. No browser, network or
 * user state. It models exactly what the collector may use (nodeType,
 * tagName, childNodes, getAttribute, computed style, boxes, checkVisibility,
 * open shadow roots) and records any read of a property the collector must
 * never touch (value, textContent, innerText, innerHTML, outerHTML).
 *
 * Fixture node format: a string is a text node; {"comment": "..."} is a
 * comment; otherwise {"tag", "attrs", "style", "rect": [x, y, w, h],
 * "shadow": "open", "unrendered": true, "children": [...]}. "unrendered"
 * models light DOM that has no box (for example unslotted children of a
 * closed shadow host), which the collector cannot see structurally.
 */
'use strict';

const DISPLAY = {
  HTML: 'block', BODY: 'block', DIV: 'block', P: 'block', SECTION: 'block', MAIN: 'block',
  NAV: 'block', HEADER: 'block', FOOTER: 'block', ARTICLE: 'block', ASIDE: 'block', FORM: 'block',
  H1: 'block', H2: 'block', H3: 'block', H4: 'block', H5: 'block', H6: 'block', UL: 'block',
  OL: 'block', LI: 'list-item', DL: 'block', DT: 'block', DD: 'block', PRE: 'block',
  BLOCKQUOTE: 'block', FIELDSET: 'block', LEGEND: 'block', DETAILS: 'block', SUMMARY: 'block',
  TABLE: 'table', CAPTION: 'table-caption', THEAD: 'table-header-group', TBODY: 'table-row-group',
  TFOOT: 'table-footer-group', TR: 'table-row', TD: 'table-cell', TH: 'table-cell',
  BUTTON: 'inline-block', INPUT: 'inline-block', TEXTAREA: 'inline-block', SELECT: 'inline-block',
  OPTION: 'block', SCRIPT: 'none', STYLE: 'none', TEMPLATE: 'none', HEAD: 'none', NOSCRIPT: 'none',
  DIALOG: 'none', IFRAME: 'inline',
};
const FORBIDDEN = ['value', 'textContent', 'innerText', 'innerHTML', 'outerHTML', 'defaultValue',
  'checked', 'selectedIndex', 'selectedOptions', 'files', 'isContentEditable'];

class FakeText {
  constructor(doc, value) { this.nodeType = 3; this.nodeName = '#text'; this.nodeValue = value; this.ownerDocument = doc; this.parentNode = null; }
}
class FakeComment {
  constructor(doc, value) { this.nodeType = 8; this.nodeName = '#comment'; this.nodeValue = value; this.ownerDocument = doc; this.parentNode = null; }
}
class FakeElement {
  constructor(doc, spec) {
    this.nodeType = 1;
    this.tagName = String(spec.tag).toUpperCase();
    this.nodeName = this.tagName;
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
    this._attrs = Object.assign({}, spec.attrs || {});
    this._style = Object.assign({}, spec.style || {});
    this._rect = spec.rect || [0, 0, 100, 20];
    this._unrendered = spec.unrendered === true;
    this.shadowRoot = spec.shadow === 'open' ? {mode: 'open', childNodes: []} : null;
    if (doc._checkVisibility) this.checkVisibility = opts => this._checkVisibility(opts || {});
    for (const name of FORBIDDEN) {
      Object.defineProperty(this, name, {get() { doc.forbiddenReads.push(name); return 'SENTINEL-FORBIDDEN-' + name; }});
    }
  }
  getAttribute(name) { return Object.hasOwn(this._attrs, name) ? String(this._attrs[name]) : null; }
  hasAttribute(name) { return Object.hasOwn(this._attrs, name); }
  _computed() { return this.ownerDocument.defaultView.getComputedStyle(this); }
  _rendered() {
    for (let el = this; el && el.nodeType === 1; el = el.parentNode) {
      if (el._unrendered) return false;
      if (el._computed().display === 'none') return false;
      const parent = el.parentNode;
      if (parent && parent.nodeType === 1 && parent.tagName === 'DETAILS' && !parent.hasAttribute('open') &&
          el.tagName !== 'SUMMARY') return false;
    }
    return true;
  }
  _checkVisibility(opts) {
    if (!this._rendered()) return false;
    if (this._computed().display === 'contents') return false;
    if (opts.checkVisibilityCSS && this._computed().visibility !== 'visible') return false;
    if (opts.checkOpacity) {
      for (let el = this; el && el.nodeType === 1; el = el.parentNode) {
        if (Number(el._computed().opacity) === 0) return false;
      }
    }
    return true;
  }
  getBoundingClientRect() {
    const [x, y, w, h] = this._rendered() ? this._rect : [0, 0, 0, 0];
    return {x, y, left: x, top: y, width: w, height: h, right: x + w, bottom: y + h};
  }
  getClientRects() {
    return this._rendered() && this._computed().display !== 'contents' ? [this.getBoundingClientRect()] : [];
  }
}

function computedStyle(el) {
  const s = el._style;
  const parent = el.parentNode && el.parentNode.nodeType === 1 ? computedStyle(el.parentNode) : null;
  let display = s.display || DISPLAY[el.tagName] || 'inline';
  if (el.tagName === 'DIALOG' && el.hasAttribute('open')) display = s.display || 'block';
  return {
    display,
    visibility: s.visibility || (parent ? parent.visibility : 'visible'),
    opacity: s.opacity === undefined ? '1' : String(s.opacity),
    clip: s.clip || 'auto',
    clipPath: s.clipPath || 'none',
    contentVisibility: s.contentVisibility || 'visible',
    overflow: s.overflow || 'visible',
    overflowX: s.overflow || 'visible',
    overflowY: s.overflow || 'visible',
    position: s.position || 'static',
  };
}

function build(fixture, options = {}) {
  const doc = {
    nodeType: 9, title: fixture.title, designMode: fixture.designMode || 'off',
    location: {href: fixture.url}, baseURI: fixture.url, forbiddenReads: [], styleReads: 0,
    _checkVisibility: options.checkVisibility !== false,
  };
  const view = {
    scrollX: 0, scrollY: 0, document: doc,
    getComputedStyle(el) {
      doc.styleReads += 1;
      if (!(el instanceof FakeElement)) throw new TypeError('not an element');
      return computedStyle(el);
    },
  };
  doc.defaultView = view;
  const ids = new Map();
  function make(spec, parent) {
    let node;
    if (typeof spec === 'string') node = new FakeText(doc, spec);
    else if (spec && typeof spec.comment === 'string') node = new FakeComment(doc, spec.comment);
    else {
      node = new FakeElement(doc, spec);
      for (const child of spec.children || []) node.childNodes.push(make(child, node));
      if (node.hasAttribute('id')) ids.set(node.getAttribute('id'), node);
    }
    node.parentNode = parent;
    return node;
  }
  doc.body = make(fixture.body, null);
  doc.documentElement = doc.body;
  doc.getElementById = id => ids.get(id) || null;
  return {document: doc, window: view};
}

module.exports = {build, FakeElement};
