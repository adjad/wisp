'use strict';
// Deterministic private-filter unit tests. Synthetic strings only.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const F = require('../../browser-extension/shared/private-filter.js');

const el = (tag, attrs = {}) => ({tagName: tag.toUpperCase(),
  getAttribute: n => (Object.hasOwn(attrs, n) ? attrs[n] : null), hasAttribute: n => Object.hasOwn(attrs, n)});
const style = (s = {}) => Object.assign({display: 'block', visibility: 'visible', opacity: '1',
  clip: 'auto', clipPath: 'none', contentVisibility: 'visible'}, s);

test('secret-like substrings are redacted and counted by kind', () => {
  const cases = [
    ['Authorization: Bearer abcDEF123456789xyz', 'auth_header'],
    ['token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMQ', 'jwt'],
    ['key sk-ant-api03-abcdefghijklmnopqrstu', 'api_key'],
    ['ghs_abcdefghijklmnopqrstuvwxyz0123', 'api_key'],
    ['xoxb-1234567890-abcdefghij', 'api_key'],
    ['AKIAABCDEFGHIJKLMNOP', 'api_key'],
    ['AIzaSyA1234567890abcdefghijklmnopqrstu', 'api_key'],
    ['open https://x.invalid/cb?access_token=abc123def&x=1', 'url_secret'],
    ['password = correct-horse', 'assignment'],
    ['client_secret: s3cr3tvalue', 'assignment'],
    ['Your verification code is 123-456', 'one_time_code'],
    ['Visa 4242 4242 4242 4242', 'card_number'],
    ['SSN 123-45-6789', 'ssn'],
    ['blob Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg', 'high_entropy'],
    ['hash 0123456789abcdef0123456789abcdef', 'high_entropy'],
    ['-----BEGIN RSA PRIVATE KEY----- MIIB abc -----END RSA PRIVATE KEY-----', 'private_key'],
  ];
  for (const [input, kind] of cases) {
    const counts = {};
    const out = F.redact(input, counts);
    assert.ok(out.includes(F.MARK), input + ' -> ' + out);
    assert.ok(counts[kind] >= 1, input + ' counted as ' + JSON.stringify(counts));
    assert.equal(F.looksSecret(input), true);
  }
});

test('ordinary course text is preserved', () => {
  const benign = [
    'Assignment 4 is due Oct 2 by 5pm.',
    'Read chapters 3 and 4; bring two questions.',
    'See /courses/101/assignments/4 for details.',
    'Contact help@course.invalid or call 555-0100.',
    'Points 20 | Available until Oct 9 at 11:59pm',
    'Room 1203, building 42, ISBN 0-306-40615-2',
    'Supercalifragilisticexpialidociousness',
    'Order number 1234 5678 9012 3456',
    'Tokenization and passwords are discussed in week 5.',
  ];
  for (const text of benign) {
    assert.equal(F.redact(text), text.replace(/\s+/g, ' ').trim(), text);
    assert.equal(F.looksSecret(text), false, text);
  }
});

test('normalization removes controls and lone surrogates without changing content', () => {
  assert.equal(F.normalize('  a\u0000b​ c\n\t d  '), 'ab c d');
  assert.equal(F.normalize('x\uD800y\uDC00z'), 'x�y�z');
  assert.equal(F.normalize('emoji 😀 ok'), 'emoji 😀 ok');
  assert.equal(F.normalize(42), '');
});

test('element exclusion covers controls, drafts, hidden and frames', () => {
  const expect = [
    [el('input', {type: 'password'}), style(), 'secret_control'],
    [el('input', {type: 'hidden'}), style(), 'secret_control'],
    [el('input', {type: 'text', autocomplete: 'cc-number'}), style(), 'secret_control'],
    [el('input', {type: 'text', autocomplete: 'one-time-code'}), style(), 'secret_control'],
    [el('input', {type: 'text', name: 'user_password'}), style(), 'secret_control'],
    [el('input', {type: 'tel', 'aria-label': 'Card number'}), style(), 'secret_control'],
    [el('textarea'), style(), 'form_control'],
    [el('select'), style(), 'form_control'],
    [el('div', {contenteditable: 'true'}), style(), 'editable'],
    [el('div', {contenteditable: 'plaintext-only'}), style(), 'editable'],
    [el('div', {'aria-hidden': 'true'}), style(), 'aria_hidden'],
    [el('div', {hidden: ''}), style(), 'hidden'],
    [el('div', {inert: ''}), style(), 'hidden'],
    [el('span', {class: 'x screenreader-only'}), style(), 'visually_hidden'],
    [el('div'), style({display: 'none'}), 'hidden'],
    [el('div'), style({visibility: 'collapse'}), 'hidden'],
    [el('div'), style({opacity: '0'}), 'hidden'],
    [el('div'), style({contentVisibility: 'hidden'}), 'hidden'],
    [el('div'), style({clip: 'rect(0px 0px 0px 0px)'}), 'visually_hidden'],
    [el('div'), style({clipPath: 'inset(100%)'}), 'visually_hidden'],
    [el('iframe'), style(), 'frame'],
    [el('script'), style(), 'non_content'],
    [el('dialog'), style(), 'hidden'],
  ];
  for (const [node, s, reason] of expect) assert.equal(F.exclusionReason(node, s), reason, node.tagName);
  for (const [node, s] of [[el('p'), style()], [el('input', {type: 'text', name: 'q'}), style()],
    [el('div', {contenteditable: 'false'}), style()], [el('div', {'aria-hidden': 'false'}), style()],
    [el('dialog', {open: ''}), style()]]) {
    assert.equal(F.exclusionReason(node, s), null, node.tagName);
  }
});

test('safeURL keeps wire-grammar HTTP(S) links and withholds secret-bearing ones', () => {
  const base = 'https://course.invalid/courses/1/assignments/2';
  assert.deepEqual(F.safeURL('/courses/1/modules#x', base), {url: 'https://course.invalid/courses/1/modules', withheld: false});
  assert.deepEqual(F.safeURL('../pages/intro?module_item_id=5', base),
    {url: 'https://course.invalid/courses/1/pages/intro?module_item_id=5', withheld: false});
  for (const href of ['/x?token=abc', '/x?nonce=1', '/x?authenticity_token=a', '/x?X-Amz-Signature=a',
    '/x?q=Bearer%20abcdefghijkl', 'https://u:p@course.invalid/', '/reset/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMQ']) {
    assert.deepEqual(F.safeURL(href, base), {url: null, withheld: true}, href);
  }
  for (const href of ['javascript:alert(1)', 'mailto:a@b.invalid', 'data:text/html,x', 'ftp://x.invalid/',
    'http://[::1]/', '', 'x'.repeat(5000)]) {
    assert.equal(F.safeURL(href, base).url, null, href.slice(0, 40));
  }
  assert.equal(F.pageURL('https://portal.invalid/a?session=abc&tab=1#f'), 'https://portal.invalid/a');
  assert.equal(F.pageURL('https://portal.invalid/a?tab=1#f'), 'https://portal.invalid/a?tab=1');
  assert.equal(F.pageURL('https://u:p@portal.invalid/a'), null);
  assert.equal(F.pageURL('chrome://settings'), null);
});
