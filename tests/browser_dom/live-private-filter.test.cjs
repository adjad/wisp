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

test('a Luhn-valid card is redacted even beside CVV, expiry, dates or small numbers', () => {
  const cases = ['Card 4111 1111 1111 1111 123', '4111111111111111 123', 'Visa 4111-1111-1111-1111 09',
    'Qty 3 4111111111111111', 'Paid 2026-09-28 4111111111111111 thanks', '123 4111 1111 1111 1111',
    'Card 4111 1111 1111 1111 exp 09 30', 'card 4111 1111 1111 1111'];
  for (const input of cases) {
    const counts = {};
    const out = F.redact(input, counts);
    assert.ok(!out.replace(/[ -]/g, '').includes('4111111111111111'), input + ' -> ' + out);
    assert.ok(counts.card_number >= 1, input);
  }
  assert.equal(F.redact('Qty 3 4111111111111111'), 'Qty 3 [redacted]');
  assert.equal(F.redact('Card 4111 1111 1111 1111 123'), 'Card [redacted] 123');
  assert.equal(F.redact('Order 2026-09-28 build 12345 room 7'), 'Order 2026-09-28 build 12345 room 7');
});

test('title-slug page URLs are kept; token-shaped path runs are still withheld', () => {
  const base = 'https://s.instructure.invalid/courses/123/pages/x';
  for (const slug of ['week-3-reading-and-discussion-prompts-for-unit-2',
    'introduction-to-machine-learning-2024-syllabus-final']) {
    const href = '/courses/123/pages/' + slug;
    assert.equal(F.safeURL(href, base).url, 'https://s.instructure.invalid' + href, slug);
    assert.equal(F.pageURL('https://s.instructure.invalid' + href), 'https://s.instructure.invalid' + href, slug);
  }
  for (const run of ['aB3xY9zLmQ2rT7vW5nK8pD4fH6jC1gS0eU', 'a8Fk2-xQ9zLm_p3Rt7Yw5Nv1Bc4Hd6Jg0Ks']) {
    assert.deepEqual(F.safeURL('/reset/' + run, base), {url: null, withheld: true}, run);
  }
});

test('code-first, unlabeled-separator and unusual secret shapes are redacted', () => {
  const gone = [
    ['123456 is your Google verification code', '123456'],
    ['code: 123456', '123456'],
    ['Password hunter2hunter2', 'hunter2hunter2'],
    ['recovery code a1b2c3d4e5', 'a1b2c3d4e5'],
    ['Card 4111.1111.1111.1111 on file', '4111.1111'],
    ['SSN 123 45 6789', '123 45 6789'],
    ['key ' + 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 'bPxRfiCYEXAMPLEKEY'],
  ];
  for (const [input, secret] of gone) {
    const out = F.redact(input);
    assert.ok(!out.includes(secret), input + ' -> ' + out);
    assert.ok(out.includes(F.MARK));
  }
  for (const ordinary of ['Password reset instructions', 'Enter the code of conduct section', 'Room 101 is open']) {
    assert.equal(F.redact(ordinary), ordinary);
  }
});

test('consonant-only dash tokens in a path are not exempt slugs', () => {
  assert.equal(F.safeURL('https://school.invalid/reset/kqzmwvhb-pfxtrlcd-ndsgjyae-vbxkzqrt').url, null);
  assert.ok(F.safeURL('https://school.invalid/pages/week-3-reading-and-discussion-prompts').url);
});

test('path-prefixed hex tokens are still redacted and URL false positives stay readable', () => {
  const hex = '5d41402abc4b2a76b9719d911017c592';
  assert.ok(!F.redact('open /api/' + hex + ' now').includes(hex));
  assert.ok(!F.redact('path /a/' + hex + hex).includes(hex));
  assert.equal(F.safeURL('https://school.invalid/x?next=/f/' + hex).withheld, true);
  const canvas = 'See https://canvas.school.invalid/courses/123456/assignments/7891011 for details';
  assert.equal(F.redact(canvas), canvas);
  const path = 'Open /courses/123456/assignments/7891011/submissions/4455 now';
  assert.equal(F.redact(path), path);
  assert.ok(F.safeURL('https://c.invalid/x?return_to=%2Fcourses%2F123456%2Fassignments%2F7891011%2Fdetails').url);
  const grades = '92.5 88.25 91.0 77.75 80.5 99.125';
  assert.equal(F.redact(grades), grades);
});
