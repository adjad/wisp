'use strict';

// Deliberately small deterministic DOM double, not a browser layout engine.
const { webcrypto } = require('node:crypto');
const box = { width: 100, height: 20, x: 0, y: 0 };
class Text {
  constructor(value, document) { this.nodeType = 3; this.nodeValue = value; this.ownerDocument = document; }
}
class Element {
  constructor(name, attributes, children, document, style = {}, rects = [box]) {
    this.nodeType = 1;
    this.tagName = name.toUpperCase();
    this.ownerDocument = document;
    this.attributes = new Map(Object.entries(attributes));
    this.childNodes = [];
    this.style = style;
    this.rects = rects;
    for (const child of children) this.append(child);
  }
  append(child) {
    child.parentElement = this;
    this.childNodes.push(child);
    return child;
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  getAttribute(name) { return this.attributes.has(name) ? String(this.attributes.get(name)) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  getClientRects() { return this.rects; }
  contains(node) {
    for (let current = node; current; current = current.parentElement) if (current === this) return true;
    return false;
  }
  get disabled() { return this.hasAttribute('disabled'); }
}
function makeDocument(children = [], baseURI = 'https://fixture.invalid/articles/current') {
  const document = { nodeType: 9, baseURI, designMode: 'off' };
  document.defaultView = {
    crypto: webcrypto,
    getComputedStyle: element => ({ display: 'block', visibility: 'visible', opacity: '1', contentVisibility: 'visible', ...element.style }),
  };
  document.createRange = () => {
    let node;
    return { selectNodeContents(value) { node = value; }, getClientRects() { return node.parentElement.getClientRects(); }, detach() {} };
  };
  document.build = spec => {
    if (typeof spec === 'string') return new Text(spec, document);
    const element = new Element(spec.tag, spec.attrs || {}, (spec.children || []).map(document.build), document, spec.style, spec.rects);
    if (spec.shadow) element.shadowRoot = {};
    return element;
  };
  document.documentElement = document.build({ tag: 'html' });
  document.body = document.documentElement.append(document.build({ tag: 'body', children }));
  document.find = id => {
    const queue = [document.documentElement];
    while (queue.length) {
      const element = queue.shift();
      if (element.getAttribute('id') === id) return element;
      queue.push(...element.children);
    }
    return null;
  };
  return document;
}
function fakeRuntime() {
  let time = 0;
  let serial = 0;
  const jobs = new Map();
  const observers = [];
  const timers = {
    setTimeout(callback, delay) { const id = ++serial; jobs.set(id, { at: time + delay, callback }); return id; },
    clearTimeout(id) { jobs.delete(id); },
  };
  class MutationObserver {
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe(target, options) { this.target = target; this.options = options; }
    disconnect() { this.disconnected = true; }
  }
  return {
    timers, MutationObserver, observers,
    mutate(records = []) { for (const observer of observers) if (!observer.disconnected) observer.callback(records); },
    tick(duration) {
      const target = time + duration;
      while (true) {
        const next = [...jobs].filter(([, job]) => job.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        jobs.delete(next[0]); time = next[1].at; next[1].callback();
      }
      time = target;
    },
    pending() { return jobs.size; },
  };
}
module.exports = { makeDocument, fakeRuntime };
