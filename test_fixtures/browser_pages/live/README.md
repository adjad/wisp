# Live-capture synthetic fixtures (A05/A10 WP2)

Synthetic pages for `browser-extension/shared/live-acquisition.js`,
`private-filter.js` and `model-view.js`. All hosts are reserved `.invalid`
names; nothing here is a real page, account or credential.

- `fake-dom.cjs` builds a minimal DOM (elements, text, computed style, boxes,
  `checkVisibility`, open shadow roots) and records any read of a property the
  collector must never touch (`value`, `textContent`, `innerHTML`, ...).
- `assignment-page.json` is a Canvas-like assignment page with in-scope,
  out-of-scope, withheld and consequential links.
- `private-fields.json` holds every private input kind, drafts and
  secret-like text.
- `hidden-content.json` holds hidden, visually hidden, collapsed, framed,
  shadow and non-content representations.

Any string containing `SENTINEL` must never appear in collector, capture or
ModelView output. Run with `node --test tests/browser_dom/live-*.test.cjs
tests/browser_dom/model-view*.test.cjs`.
