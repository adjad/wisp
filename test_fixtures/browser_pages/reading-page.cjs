'use strict';
// Synthetic public content and sentinel secrets only. No captured user data.
module.exports = [
  { tag: 'h1', attrs: { id: 'page-title' }, children: ['Quarterly ', { tag: 'em', children: ['results'] }] },
  { tag: 'p', children: ['Revenue grew ', { tag: 'strong', children: ['12%'] }, ' this quarter.'] },
  { tag: 'a', attrs: { id: 'report-link', href: '../report?token=SECRET_QUERY#SECRET_FRAGMENT' }, children: ['Read report'] },
  { tag: 'table', children: [
    { tag: 'caption', children: ['Revenue'] },
    { tag: 'thead', children: [{ tag: 'tr', children: [{ tag: 'th', children: ['Region'] }, { tag: 'th', children: ['Total'] }] }] },
    { tag: 'tbody', children: [{ tag: 'tr', children: [{ tag: 'td', children: ['West'] }, { tag: 'td', children: ['$12'] }] }] },
  ] },
  { tag: 'label', attrs: { for: 'search' }, children: ['Search reports'] },
  { tag: 'input', attrs: { id: 'search', type: 'search', value: 'SECRET_INPUT' } },
  { tag: 'textarea', attrs: { id: 'draft', 'aria-label': 'Draft note' }, children: ['SECRET_TEXTAREA'] },
  { tag: 'button', attrs: { disabled: '' }, children: ['Export report'] },
  { tag: 'input', attrs: { type: 'password', 'aria-label': 'SECRET_PASSWORD_LABEL', value: 'SECRET_PASSWORD' } },
  { tag: 'input', attrs: { autocomplete: 'section-checkout cc-number', value: 'SECRET_CARD' } },
  { tag: 'div', attrs: { contenteditable: 'true' }, children: ['SECRET_DRAFT', { tag: 'h2', children: ['SECRET_DRAFT_HEADING'] }] },
  { tag: 'div', attrs: { hidden: '' }, children: [{ tag: 'h2', children: ['SECRET_HIDDEN'] }] },
  { tag: 'div', attrs: { 'data-sensitive': '' }, children: ['SECRET_MARKED'] },
  { tag: 'script', children: ['SECRET_SCRIPT'] },
  { tag: 'style', children: ['SECRET_STYLE'] },
  { tag: 'template', children: ['SECRET_TEMPLATE'] },
];
