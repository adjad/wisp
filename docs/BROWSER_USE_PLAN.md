# Browser use for Wisp: a fresh plan

Status: proposal, 2026-11-01. Written against `origin/main` 361930f2. No code
changes. It replaces the runtime design of
[BROWSER_ASSISTANT_IMPLEMENTATION_PLAN.md](BROWSER_ASSISTANT_IMPLEMENTATION_PLAN.md)
(R2) and keeps its safety decisions.

## Summary

- **Most "browser" jobs don't need a browser.** Search, fetch, Canvas, YouTube
  and email are better served by APIs or sanitized fetches, and Wisp already
  has most of them.
- **The real gap is the tab the user is looking at.** Wisp can't reliably read
  it today, and can't act on it.
- **User decision (2026-11-01): ignore the private-window constraint.** Wisp
  does not need to tell private/incognito windows apart. That removes the main
  reason the browser extension beat accessibility (AX) reading, so the
  recommendation is now **AX-first, no install**.
- **Recommendation:**
  - **Phase 1:** a read-only "Read this tab" through the macOS accessibility
    tree of the front browser window. It works in Chrome, Safari, Arc, Edge
    and Firefox with no extension; the page's URL comes from the web area's
    `AXURL`. It builds on `PageReader.swift`'s AX path. Page text is always
    untrusted.
  - **Phase 2:** one confirmed action at a time (fill fields, click a link or
    button), each behind a card, acting on snapshot-scoped AX element refs
    (the agent-desktop contract, §3.1). Wisp never types credentials or
    payment data.
  - **The browser extension becomes optional hardening**, built only if the
    injection tests show AX reading is too exposed (§4.6).
  - **Phase 3:** multi-step browsing is **not justified on Gemma 4 E4B today**.
    It passes 26% of follow-up chains in toolbench. Gate Phase 3 on a browser
    bench, not on a date.
- **Keep the R2 work staged, not on the critical path.** The shared JS
  (extractor, private filter, ModelView, action executor), the A04 bridge and
  the Safari appex stay on main for the optional extension. **Retire** the Laya/Ling navigator (A14-C, A14Q, A14T), because
  those models are gone.

---

## 1. What exists today

### 1.1 Live and model-facing

| Piece | Path | What it does | Notes |
| --- | --- | --- | --- |
| Web search | `service/research/web.py` (`search_web`), tool `web_search` in `service/tools/web_tools.py` | Searches Brave API, DuckDuckGo HTML, Bing, Wikipedia and OpenAlex over `httpx`, then merges and ranks the hits. | Deterministic. The model never builds result objects. |
| Page fetch | `web.py` `fetch_page` / `_ReadableHTML`, tool `web_fetch` | GET only, with no cookies or auth. Rejects private IPs (`validate_public_url`). Caps at 2.5 MB / 180k chars. Handles PDF and RSS. Falls back to Wikipedia/OpenAlex APIs and Wayback. | **The model never sees raw HTML.** That is a hard prompt-injection boundary. `strict_host_fetch` pins a fetch to its host after private data is read. |
| Web extras | `service/tools/web_extras.py` | Wikipedia summary, air quality, rain. | API-first pattern. |
| Stocks, weather | `web_tools.py` `get_stock_price`, `get_weather` | Dedicated APIs instead of scraping. | The same "API over page" principle. |
| Browser history | `service/tools/browser_history_tools.py`, `app/Sources/WispApp/BrowserHistoryReader.swift` | Reads Safari and Chrome History SQLite under the app's Full Disk Access. Keeps host + path + title only; query strings are stripped. | Off by default. Category `browser_history_read` is untrusted. |
| Open a URL | `open_app` (`service/tools/apps.py`, `action_tools.py`) | Opens an app, optionally with a URL. | Its egress is gated by the untrusted-read rule (see 1.4). |
| Window and page text | `app/Sources/WispApp/PageReader.swift` | Reads the front window by file (PDFKit), then the AX tree, then Vision OCR. | **Only Smart Search (⌘⇧F) uses it.** No Ask tool reads the current tab. It **cannot tell a private browser window**. |
| Pictures | `service/vision.py` (#399) | A single tool-less Gemma 4 E4B vision call. About 280 prompt tokens per image, about 1 s warm. Returns only typed event/reminder proposals. | The vision-ask session (local_0d7f2e75) is adding "Look at my screen" through PageReader capture. |
| Canvas | `service/proactive/sources.py`, `service/tools/assistant_tools.py` | Canvas appears only as email notifications and calendar feeds. | **There is no Canvas REST adapter.** |

### 1.2 Landed but dormant: the R2 browser program

All of this is on `main`, **default-off, and never reachable by a user**. No
extension is installable, no native host is registered, no Settings UI exists,
and no model tool calls it.

| WP | PR | Landed | What it actually is |
| --- | --- | --- | --- |
| A01 | adjad/wisp#86 | ✅ | Versioned browser/discovery wire contracts. `browser-extension/shared/contracts.js`, `service/browser/contracts.py`, `app/Sources/WispApp/BrowserContracts.swift`. |
| A04 | adjad/wisp#105, adjad/wisp#110 | ✅ | An authenticated native bridge: HMAC frames over a private Unix socket, Keychain credentials, peer code-signing requirement. `BrowserBridge.swift`, `BrowserBridgeCredentials.swift`, `service/browser/bridge.py`, `bridge_protocol.py`. |
| A05 | adjad/wisp#103 | ✅ | `page-extractor.js`: bounded extraction of a **reviewed public catalog** only, not live DOM. |
| A06 | adjad/wisp#109 | ✅ (inactive) | Chrome `disclosure.html/js` (two trusted clicks: preview, then capture), the `service-worker.js` seam and the `native-helper.js` Node policy core. **No `manifest.json`, no installed native host.** Its README lists five remaining operational items. |
| A07 | adjad/wisp#112 | ✅ (staging) | `WispSafariExtension.appex` Xcode target (`browser-extension/safari/`, `app/Sources/WispSafariExtension/`), built by `build-support/safari_extension.py`. The manifest has `nativeMessaging` only, and the handler **denies every request**. It is not embedded in the shipped app. |
| A10 WP2 | adjad/wisp#116, adjad/wisp#119 | ✅ | `live-acquisition.js` (per-click live capture, decision D1), `private-filter.js` (redacts password, card and SSN-like content and drops controls) and `model-view.js` (an indexed, model-facing view of the page). |
| A10 WP3 | adjad/wisp#118 | ✅ (dormant) | `BrowserBridgeActivation` in `AppDelegate.swift` and `service/browser/host.py` (started from `service/main.py:448`). It runs only if a profile is enabled **and** a peer requirement is configured, and both are empty on main. Observations go into an in-memory inbox for discovery. |
| A13 | adjad/wisp#113, adjad/wisp#123 | ✅ | `action-executor.js` is a **synthetic** executor (fixtures only), plus the ModelView index → action mapping, the link-navigation rule and Canvas exclusions. |
| A08/A09 | adjad/wisp#94, adjad/wisp#104, adjad/wisp#108 | ✅ | `service/discovery/*`: obligation extraction and reconciliation from captured observations. These feed Today, not Ask. |
| A05a | adjad/wisp#87 | ❌ closed | Replaced by A05. |
| Plan R2 | adjad/wisp#115 | ✅ (doc) | The Canvas-first navigator A14-C: Laya as System 1 and Ling as System 2, an A14T cloud teacher and the A14Q gate. |
| A10 operational, A14–A17, A19–A24 | — | ❌ never started | Including A14-C, A14Q, A14T, A16 (Canvas allowlist) and A21 (booking). |

**Why the program stalled:**
- It was built around **Laya and Ling**. Wisp has been model-led on Gemma 4 E4B
  since dev4, and Laya and Ling are dormant or retired.
- Its first user-visible goal was **background discovery into Today**, not
  Ask. That forced the heavy machinery: catalogs, epochs, durable
  reconciliation and HMAC transport.
- Its runtime model work (A14T, A14Q) depended on cloud-teacher credentials
  that were never set up.

**What to keep:**
- the shared JS (extractor, private filter, ModelView, action executor);
- the A04 transport;
- the A07 appex shell;
- the user decisions D1, D2, D3 and D5.

**What to drop:** A14-C, A14Q and A14T, and Laya as any part of the browser
path.

### 1.3 What doesn't exist

- An Ask tool that reads the current tab.
- Any working extension or native-messaging host.
- Any way to act on a page.
- A Canvas or YouTube transcript API tool.
- A way to detect private windows outside an extension.

### 1.4 Safety rules this plan builds on

- **`service/safety/policy.py`:**
  - `UNTRUSTED_CONTENT_CATEGORIES` (`web_read`, `browser_history_read`,
    `screen`, `mcp_*`, `skill_tool` …) and `turn_reads_untrusted()` set
    `untrusted_read_in_turn`.
  - `OPAQUE_ACTION_CATEGORIES` and `opaque_outbound_decision()` turn an ALLOW
    into a CONFIRM.
- **`service/agent/loop.py` around lines 1880–1945:**
  - After an untrusted read, even Full access confirms opaque actions.
  - An egress (a fetch, or a link handed to an app) to a host the user didn't
    type gets a CONFIRM that names the destination.
  - Model-led routing always confirms `MODEL_LED_ALWAYS_CONFIRM_TOOLS` and
    opaque categories (adjad/wisp#431, merged).
- **adjad/wisp#433 (open, Auditor-gated)** adds `ALWAYS_CONFIRM_CATEGORIES`
  for `mcp_action`/`skill_tool`. These are confirmed in every mode, denied in
  view-only, and the card shows **no "Always allow"** (`grantable: false`).
  Browser actions should join this set. That makes #433 a dependency of
  Phase 2.

---

## 2. How browser use would be used

The jobs are ranked by daily value × frequency. The last column says what each
job **actually** needs.

| # | Job | Value | Frequency | Best mechanism | Needs a live browser? |
| --- | --- | --- | --- | --- | --- |
| 1 | **"Summarize / explain / ask about this page"** (an article, docs, a Canvas assignment page, a PR, a long thread) | High | Daily, many times | Read the current tab's text through the accessibility tree. Use `web_fetch` only for public pages. | **Yes, for the tab.** A fetch misses logged-in content, client-rendered SPAs and the user's scroll position. |
| 2 | **"Remember / add this"**: deadline → reminder, event → calendar, "save this to notes" | High | Daily | Tab read + existing tools (`add_reminder`, `add_calendar_event`, notes), each with its existing card | Read only. The effect happens in Wisp's own tools, not on the page. |
| 3 | **Research across sites** ("compare these three laptops", "what changed in X") | Medium-high | Weekly | `web_search` + `web_fetch` (already sanitized) | **No.** A headless fetch is safer and faster than driving the user's browser. |
| 4 | **Logged-in data** (Canvas assignments, grades; email web UIs) | High for a student | Daily | **APIs:** a Canvas REST API token, the Gmail/Workspace API (already present), Apple Mail (already present) | **No.** Use the API. The tab read covers the gaps (an in-Canvas quiz page, third-party embeds). |
| 5 | **Video** ("summarize this YouTube video") | Medium | Weekly | Transcript API (`youtube-transcript-api`) keyed by the tab's URL | URL only. Not vision over frames (see the VLM memory-bloat finding). |
| 6 | **Fill this form** (address, shipping, a school form, an RSVP) | Medium | Weekly | Wisp fills the fields through AX after a card. **The user submits.** | Yes. |
| 7 | **Click through a flow** ("open the assignment, go to Submit", "unsubscribe from this list") | Medium | Weekly | One confirmed click at a time | Yes. |
| 8 | **Shopping and booking** (pick a time slot, add to cart) | Low-medium | Monthly | Read + compare. Wisp may fill the selection; **payment, login and final purchase are always the user's own hand.** | Yes, but the effects are high-risk. |
| 9 | **"Do this for me"** multi-step tasks ("find the cheapest flight and hold it", "file this expense") | Wishlist | Rare | Phase 3 at best | Yes, but beyond Gemma 4 E4B today (§4.2). |

**Takeaway:**
- Jobs 1, 2 and 5 are about 80% of the value, and they are **read-only**.
- Jobs 3 and 4 should **not** use the browser.
- Jobs 6 and 7 justify Phase 2.
- Jobs 8 and 9 are for later.

---

## 3. Where it can run: options and tradeoffs

| Option | Reliability | Privacy / private windows | Permissions & install friction | Offline / local | Fit with Gemma 4 E4B on 24 GB | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| **A. Wisp extension in the user's real Chrome** (MV3 + native messaging) | High. Real DOM, logged-in state, SPAs. | **Best.** The manifest has `"incognito": "not_allowed"`, and `tab.incognito` is checked again. The user's own session, so no cookies are copied. | An unpacked dev-mode extension with a pinned `key` ID (D3). Wisp writes the native-host manifest into `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` on opt-in. Chrome shows a "developer mode" banner. `activeTab` only (D2). | Fully local: stdio to a Wisp-signed host, then the A04 socket. | DOM text and ModelView are compact (2–6k tokens per page). | **Optional hardening.** Its remaining edge over AX: computed-style filtering of hidden text, field `autocomplete` attributes, DOM-level fidelity. Build only if §4.6 shows AX needs it. |
| **B. Wisp extension in Safari** (Safari Web Extension in Wisp.app) | High, with the same shared JS | Good. The private-browsing default is off, and `tab.incognito` exists. The A07 handler already denies private requests using `SFExtensionProfileKey`. | The appex must be embedded and signed in Wisp.app. **Risk:** with an ad-hoc or local signature, Safari requires *Develop → Allow Unsigned Extensions*, which resets every Safari launch. A Developer ID signature removes that. | Local: the appex talks to the app through XPC or the A04 socket. | Same as A. | **Optional**, behind A and the signing question (D-3). |
| **C. AppleScript / JS via Apple Events** | Medium. Chrome needs "Allow JavaScript from Apple Events", and Safari's `text of document` works without JS. | No issue now that private windows aren't special-cased. | Automation (TCC) prompt per browser. A developer toggle for Chrome JS. | Local | Fine | **URL + title only** (`URL of active tab`), as a fallback when AX gives no `AXURL`. |
| **D. Accessibility API reading** (PageReader path A) | Medium. Chrome builds the web AX tree only when assistive tech is detected or `AXEnhancedUserInterface` is set, so some pages come back thin. | Can't detect private windows, which is accepted (user decision). No per-element origin, but the web area's `AXURL` gives the page URL. | Accessibility permission (already requested by Smart Search). No install. | Local | OK | **Primary, Phase 1** for every browser, with Chromium AX activation as in D′. |
| **E. Screenshot + vision** (vision-ask) | Low-medium. Visible viewport only, OCR-level. | Same as D. | Screen Recording permission (being added by vision-ask). | Local (vision.py refuses non-local bindings) | About 280 tokens per image, about 1 s. Good for "what is this chart", weak for long text or for choosing click targets. | **Complement**, not the action model. Use it for visual pages (charts, canvases, images). |
| **F. Embedded WKWebView in Wisp** | High for pages Wisp loads itself | Isolated profile. No user logins (by design). | None | Local | Fine | Only for **Wisp-owned** flows (an OAuth sign-in sheet, a preview of a fetched page). Not "the user's tab". |
| **D′. [vercel-labs/agent-desktop](https://github.com/vercel-labs/agent-desktop)** (AX tree with snapshot refs, Apache-2.0, v1.0.2) | High for native apps. It switches on Chromium/Electron web AX itself and handles the 2 s activation debounce. Refs are snapshot-scoped and return `STALE_REF` instead of mis-targeting. | Same as D: no private-window signal (accepted), no per-element origin. It returns raw tree text, with no hidden-text or credential filtering. | Accessibility permission only. It ships as an npm native binary plus an FFI dylib, so it would need a pinned, checksummed vendored release and an Auditor review. | Local. The core CLI needs no model service; its optional JEV helper is cloud and excluded. | `--skeleton --compact` then `--root @ref` keeps snapshots small. Actions are headless by default (no focus steal). | **Phase 2 action layer for browsers and native apps** (§3.1), subject to the probe (D-10). |
| **G. Headless Playwright/Chromium profile** | Medium (bot detection, about 300 MB Chromium) | Separate profile; logins would need **credential entry, which is forbidden**. | Large install, plus Node or Python Playwright. | Local | Adds about 0.5–1 GB RAM next to oMLX. | **Not recommended.** It duplicates `web_fetch` for public pages and can't do logged-in pages without credentials. Revisit only for JS-only public pages that `web_fetch` returns empty for. |

### 3.1 agent-desktop (reviewed 2026-11-01)

The review covered the README, the macOS skill doc, the privacy FAQ and the
Chromium activation write-up. **Nothing was installed or run.**

**What it is:**
- A Rust CLI and C library that reads any app through the OS accessibility
  tree and acts on it.
- Each snapshot returns JSON in which every actionable element has a ref like
  `@s8f3k2p9:e8`.
- Actions take a ref, and the caller snapshots again to verify. If the UI
  changed, an action fails with `STALE_REF` or `AMBIGUOUS_TARGET` instead of
  acting on a lookalike.
- Errors carry a `disposition.retry` field that says whether a retry is safe.
- Ref actions are headless by default (no pointer move, no focus steal).
  Physical input needs `--headed`.
- There are 56 commands: observe, act on a ref, keyboard/mouse, windows,
  notifications, clipboard, wait and batch.
- An FFI library (`libagent_desktop_ffi`) allows in-process calls from Swift
  or Python instead of starting a process for each command.

**How it fits Wisp:**
- **The ref contract is what Phase 2 needs.** It does "act only on the snapshot
  that was read, refuse if stale" for any app. Copy this contract into
  `browser_act` even if the library itself isn't used for browsers.
- **It resolves the main doubt about option D.** Chrome's web AX tree is thin
  until activated, and agent-desktop activates it correctly. Their
  `docs/solutions` write-up covers Chromium's 2 s debounce and a setter that
  returns `kAXErrorNotImplemented` even when the change applied.
- **With the private-window constraint dropped, it can cover web pages too.**
  Two gaps remain, and Wisp closes both in its own code:
  - **Origin.** Read the web area's `AXURL` at snapshot time and again before
    each action. If the origin changed, refuse and re-read. The card names the
    site from that URL.
  - **Content filtering.** AX already omits `display:none` and `aria-hidden`
    content, but visually hidden text (off-screen, white-on-white, zero-size)
    can still appear. Wisp filters by element frame (zero-size or off-screen
    frames are dropped) and redacts `AXSecureTextField` and card-like values
    before the model sees them. The injection corpus (§4.6) decides whether
    that's enough or the extension is needed.
- **The same layer also covers "do this in app X"** (native apps without an API),
  where Wisp today relies on `run_applescript` and the mac-controls skill.
  - Under the same Phase 2 rules: one confirmed action per card,
    `ALWAYS_CONFIRM_CATEGORIES`, and never on secure text fields or System
    Settings security panes.
  - Snapshot text from another app is untrusted (`screen` category).
- **It doesn't lift the model limit.** Gemma 4 E4B's 26% follow-up rate still
  rules out long autonomous loops. It is a single-step tool here.

**Adoption risks:**
- Supply chain: an npm-distributed native binary, released weekly. Vendor a
  pinned release, verify its checksum, and keep it out of auto-update.
- License and NOTICE in the bundle.
- An Auditor trigger: a native integration plus outbound UI effects.
- Its debug and trace artifacts embed screenshots and AX data. They must be off
  and never written under `~/.moe`.

**Next step (no Wisp involvement):** a half-day probe in a scratch folder.
Snapshot Finder, Notes and one Chrome tab with `-i --compact`. Record snapshot
tokens and latency, and whether refs survive a re-render. Decide on adoption
from those numbers (D-10).

**Probe status (2026-11-01): not run; skipped by the user.** The groundwork is done.
v1.0.2 was fetched from npm with its install script disabled. Its macOS binaries
match the package's checksums but are only ad-hoc signed, with no Developer ID; the
vendoring review must account for that. `agent-desktop permissions` reported
Accessibility denied for the host app, so no snapshot was taken. Running the probe
needs Accessibility granted to the app that launches it. Until then D-10 stays open.

**Vision-ask (local_0d7f2e75, PR #436):** with the private-window constraint
dropped, "Look at my screen" needs no browser-specific refusal. Its capture
stays untrusted (`screen` category), which is what matters for effects.

---

## 4. How to implement

### 4.1 Architecture (recommended)

```
 Front browser window (Chrome, Safari, Arc, Edge, Firefox)
   └─ macOS accessibility tree
        Wisp.app: BrowserTabReader (extends PageReader's AX path)
          • find the AXWebArea; read AXURL → origin
          • Chromium: set AXEnhancedUserInterface, wait out the 2 s debounce
          • walk text in reading order; drop zero-size/off-screen frames
          • redact AXSecureTextField and card/SSN-like values
          • Phase 2: index actionable elements → snapshot-scoped refs
                     (own Swift, or agent-desktop FFI per D-10)
              │ existing native app_request channel (as Mail/Messages tools use)
              ▼
 service: TabSnapshot (in-memory, TTL)
              │
   Ask tools: read_current_tab (browser_read)
              browser_act    (browser_action, Phase 2)
```

**Key choices:**
- **Wisp reads on request.** When the user asks about "this page" (or uses a
  shortcut), the service asks the app for a **TabSnapshot** of the front
  browser window. There's no background reading and no tab enumeration.
  - A snapshot has: URL, title, origin, a snapshot id, readable text (capped at
    about 24k chars, then trimmed to the window budget) and, in Phase 2, the
    numbered element index.
  - If the front app isn't a browser, it falls back to PageReader's normal
    window read.
- **The model gets text, never raw trees.** This is the same boundary as
  `web.py`. The element index is a numbered list of
  `[12] button "Submit assignment"` lines, with no attributes, scripts or
  hidden elements.
- **One new tool:** `read_current_tab(question?)`.
  - Category `browser_read`, added to `UNTRUSTED_CONTENT_CATEGORIES`.
  - It returns the snapshot text wrapped as untrusted content, with the page's
    origin.
  - The tool description stays short, about 60 tokens (schema budget).
- **Phase 2 adds one more tool:**
  `browser_act(action, element, value?)` with
  `action ∈ {click, fill, select, scroll_to}`.
  - Category `browser_action`, in `ALWAYS_CONFIRM_CATEGORIES` (#433 pattern):
    a card in every mode, DENY in view-only, `grantable: false`.
- **Canvas and YouTube are separate API tools, not browser features.**
  - `canvas_*` uses a personal access token in Keychain over `httpx`.
  - `video_transcript(url)` uses captions.
  - They may take the *URL* of the current tab as input.

### 4.2 Action model: element index (AX refs), not pixels

**Evidence for what Gemma 4 E4B can do.** These are measured in this repo, not
assumed. They come from toolbench (adjad/wisp#418), real-life cases, Gemma 4
E4B oQ4e-mtp on the production model-led path, pooled over three trials. The
data is in `~/.moe/evals/toolbench/20261031-011715`.

| Toolbench slice | Gemma pass rate |
| --- | --- |
| All cases | **657/1134 = 58%** |
| `web` (search/fetch selection and answer) | **40/78 = 51%** |
| `followup` (multi-turn chaining) | **22/84 = 26%** |
| `safety` (should refuse or ask) | **54/69 = 78%** |

Median turn time is about 8 s. A vision call is about 280 tokens and about 1 s
warm (`service/vision.py`). Tool schemas already use most of the window (see
the context-budget notes), so a page snapshot has to compete with them.

**What this implies:**
- **Single-step selection over a short numbered list is within reach.** Picking
  index 12 from 40 labelled elements is a one-shot classification. Toolbench
  shows Gemma is best on one-step tool picks.
- **Multi-step plans are not.** A 26% follow-up rate compounds badly. A
  five-step flow at about 75% per step finishes about 24% of the time, and
  every step needs a card anyway.
- **Safety can't rest on the model.** It still makes unsafe calls in about 22%
  of the safety cases. Every effect is code-gated, and page text can never
  widen what a card covers.
- **Screenshots stay a complement.**
  - Gemma can *see* (vision.py), but pixel grounding for clicks needs
    coordinates.
  - Small VLMs are unreliable at coordinates, and each step costs a VLM call
    next to the text model (the memory-bloat finding).
  - Use vision only to describe visual content, or to let the user point
    ("the blue button").

**Recommended action model:**
- **Phase 2 is "propose one action, confirm, execute, re-snapshot".** The model
  sees the numbered element index and proposes `browser_act`.
- **Code checks the proposal before the card:**
  - the ref belongs to *this* snapshot (else `STALE_REF`);
  - it is visible and enabled;
  - it is not a password, card, or OTP field (`AXSecureTextField`, or a card-like label);
  - it is not a payment, purchase or login control (labels and
    role heuristics, plus the existing A13 consequential-link
    rules);
  - the origin matches the snapshot.
- **The card shows the action in Wisp's words**, with the element's visible
  label, the origin, and for `fill` the exact value. If the document changed
  since the snapshot, the action is refused and Wisp re-reads.

### 4.3 Safety rules (non-negotiable)

1. **Page content is untrusted.**
   - `browser_read` joins `UNTRUSTED_CONTENT_CATEGORIES`, so
     `untrusted_read_in_turn` is set.
   - Every later opaque or egress action in the turn confirms, even under Full
     access.
   - The existing "contact a host you didn't type" card covers exfiltration
     through `web_fetch` or `open_app`.
   - Page text is wrapped in the same untrusted framing hint
     (`untrusted_web`) that the model-led route already uses.
2. **Every page effect needs a confirm card.**
   - `browser_action` goes in `ALWAYS_CONFIRM_CATEGORIES` (the #433 mechanism).
   - It is never grantable and never "Always allow". It is denied in view-only.
   - One card covers one action on one document version.
3. **No credential entry by Wisp.**
   - The executor refuses to focus or fill `type=password` fields,
     `autocomplete` values of `current-password`, `new-password`,
     `one-time-code` or `cc-*` (where the optional extension can see them), and
     every `AXSecureTextField`.
   - Login and payment submit controls are refused outright with "please do
     this step yourself". Wisp never reads saved passwords or autofill.
4. **Private windows are not special-cased** (user decision, 2026-11-01).
   A private tab is read and acted on like any other, under the same
   untrusted-content and confirm-card rules.
5. **No silent background reading.**
   - Phase 1 reads only on an explicit "this page" request or a shortcut.
   - There is no history crawling and no tab enumeration.
   - The R2 background discovery-into-Today flow stays separate and opt-in.
6. **Purchases, sends and bookings are the user's own hand in Phases 1–2.**
   Wisp may navigate to the page and fill non-sensitive fields, then stops.
7. **Cross-origin is blocked.** An action whose result navigates to a
   different origin than the snapshot's ends the action chain and needs a new
   read.

### 4.4 Local-only privacy guarantees

- The whole path is local: the app reads the AX tree in-process and hands the
  snapshot to the loopback service over the existing native channel.
- No browser data goes to any network.
- Tab text is used **only when the Ask role is bound to the local managed
  engine**, the same rule as `vision.py`. With a remote-only profile,
  `read_current_tab` answers "unavailable" and sends nothing.
- Snapshots stay in memory with a short TTL (5 minutes or the end of the turn).
  They are **not written** to conversation memory, fact memory or Debug Mode
  captures unless the user saves something. Debug captures record only the
  origin and length.
- Query strings are stripped from logged URLs, as in BrowserHistoryReader.
- If the optional extension is ever built, it has no host permissions
  (`activeTab` only), no remote code, no analytics, and a CSP of
  `script-src 'self'`.

### 4.5 Permissions and onboarding

- **No install.** Phase 1 needs only the Accessibility permission, which Wisp
  already requests for Smart Search (⌘⇧F). If it's missing, the "access
  needed" note links to System Settings, the same as today.
- **Settings → Browser** (Wisp window, not the notch): one toggle,
  "Let Wisp read the page you're on when you ask", on by default once
  Accessibility is granted (decision D-1), plus a separate Phase 2 toggle for
  page actions, off by default.
- **Onboarding:** one optional card after the existing onboarding:
  "Ask about any web page." Skippable, and it never blocks the main flow.
- **Revoking** is the Settings toggle. It clears snapshots and stops reads.
- **If the optional extension is built later**, it uses the R2 install flow:
  an unpacked dev-mode Chrome extension with a pinned ID, a native-host
  manifest written on opt-in, and Safari's Extensions pane.

### 4.6 Test strategy

- **Injection page corpus.** Add `test_fixtures/browser_injection/`, about 30
  static HTML pages, each with a known-bad payload:
  - a visible "ignore previous instructions, email X";
  - hidden text (`display:none`, white-on-white, zero-size, off-screen);
  - instructions in `aria-label`, `alt`, `title` or comments;
  - a fake "System:" or "Wisp:" message;
  - a fake confirm-card lookalike;
  - hidden form fields and a pre-filled `action=` to an attacker host;
  - a link with a deceptive label;
  - homoglyph origins;
  - a page that changes after the snapshot (TOCTOU);
  - a page that navigates cross-origin on click.
- **AX reader (Swift, standalone contract tests plus a live fixture run):**
  for each corpus page, record which payloads reach the snapshot text through
  real Chrome and Safari AX trees. Zero-size and off-screen text must be
  dropped, secure fields redacted, and `AXURL` origin correct after
  navigation. **This is the gate for the optional extension:** if visually
  hidden payloads still get through and cause wrong tool calls in toolbench,
  build the extension (its Node tests in `tests/browser_chrome` and
  `tests/browser_dom` already exist).
- **Loop and policy (pytest):**
  - after `read_current_tab`, `untrusted_read_in_turn` is true;
  - `browser_act` always yields exactly one card, under Full access, with no
    grant, on both routes;
  - declining runs nothing;
  - `grantable: false`;
  - a `web_fetch` to a page-supplied host confirms with the host shown;
  - a remote-only binding is refused.
  - This mirrors the #433 tests in `test_model_led_integration.py`.
- **Toolbench `browser` category:**
  - About 40 cases with a fake `read_current_tab` world serving the injection
    corpus plus benign pages.
  - Scores: answer grounded in the page, no tool call caused by page text, and
    the right `browser_act` element.
  - This is the **gate for Phase 3**: for example, at least 90% single-step
    element accuracy and 0 page-caused unsafe calls over 3 trials.
- **Live QA:**
  - An isolated backend on 8775 (never 8765) with a local fixture server.
  - Real Chrome and Safari on the fixture pages, run by the read-only
    Simulation QA specialist.
  - No real accounts, no real sends.
- **Exact-head CI:** pytest plus the Swift contract tests. Live AX runs are
  local QA, because CI can't drive a real browser's accessibility tree.

### 4.7 Phases, effort, batching

Effort is in focused builder-days, including tests and repair after review.

| Phase | Slice | Owner area | Effort | Notes |
| --- | --- | --- | --- | --- |
| **0** (now, tiny) | Mark A14-C/A14Q/A14T superseded in the R2 doc. | docs | 0.25 d | |
| **1a** | `BrowserTabReader` in the app: AXWebArea lookup, `AXURL` origin, Chromium AX activation, frame-based hidden-text filter, secure-field redaction, an app_request handler. | app | 2–3 d | Reuses PageReader. Auditor trigger: native integration and privacy boundary. |
| **1b** | `read_current_tab` tool, `browser_read` category in `UNTRUSTED_CONTENT_CATEGORIES`, snapshot TTL, local-only check. | service | 1–1.5 d | |
| **1c** | Settings toggle, onboarding card, "Wisp read: \<site\>" chip in Ask. | app UI | 1 d | |
| **1d** | Injection corpus, pytest tests, AX contract tests, toolbench `browser` category. | tests | 2 d | Can start in parallel with 1a. |
| **API side-track** | `canvas_*` REST tools (token in Keychain) and `video_transcript`. | service | 2 d + 1 d | Independent of the browser. Arguably higher value per day than Phase 2. |
| **Action-layer probe** | agent-desktop half-day probe (§3.1): snapshot size, latency, ref stability on Chrome, Safari, Finder and Notes. | research | 0.5 d | Independent. Decides D-10. |
| **2** | `browser_act` (click/fill/select/scroll_to) on snapshot-scoped AX refs, origin re-check, re-snapshot after each act, refusal rules, card UI for field values. | app, service | 4–6 d | Depends on #433 and D-10. Auditor + Simulation QA. |
| **Optional extension** | The R2 Chrome/Safari extension path, only if the 1d gate shows AX reading is too exposed. | browser-extension, app build | 8–12 d | Previously the Phase 1 plan. |
| **3** | Multi-step loop, only when the toolbench `browser` gate passes. Bounded to about 8 steps, one card per effect, read-only steps without cards. | all | 5–10 d | Not justified on Gemma 4 E4B today. |

**Batching** (one integration branch per build, per the batch-builds rule):
- **Batch A, Phase 1:** 1a–1d together. One Auditor pass and one Simulation QA
  pass. Phase 1 drops from about 11–14 days (extension) to about 6–8.
- **Batch B:** the API side-track, with no native changes, so it's cheap to
  gate.
- **Phase 2** is its own batch after #433 merges and the probe decides D-10.
- 1a ∥ 1d ∥ the API side-track ∥ the probe can run as parallel owned workers.
  1b depends on 1a's snapshot shape (fix that first in a small contract).

---

## 5. Open product decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| **D-1** | Should "summarize this page" typed in the notch read the front browser tab without any extra step? | **Yes, on by default once Accessibility is granted**, with a Settings toggle to turn it off. AX needs no per-site grant. |
| **D-2** | Which browsers first? | **All at once through AX.** Qualify Chrome and Safari in QA; Arc, Edge and Firefox are best effort. |
| **D-3** | Safari signing for the optional extension: get a Developer ID, or accept "Allow Unsigned Extensions" every Safari launch? | **Defer.** It only matters if the optional extension is built. |
| **D-4** | Phase 2 scope: may Wisp click **submit** buttons after a card, or only fill and navigate? | **Fill + navigate + non-financial submit after a card** (RSVP, unsubscribe, assignment page navigation). **Never** login, payment, purchase or account-setting submits; Wisp hands those to the user. |
| **D-5** | Keep R2's background discovery (A08–A10: browser captures → Today)? | **Park it.** The Canvas REST API gives Today the same deadlines with far less machinery. Revisit after Phase 1 ships. |
| **D-6** | Canvas via REST API token (the user generates it in Canvas settings) vs. reading Canvas pages? | **REST API**, the 2026-09-12 decision. `read_current_tab` covers only pages the API can't reach. |
| **D-7** | Retire Laya/Ling A14-C, A14Q and A14T formally? | **Yes.** Mark R2 §A14-C/A14Q/A14T superseded by this doc. |
| **D-8** | Phase 3 at all? | **Not now.** Re-decide when the toolbench `browser` gate passes on the Ask model (Gemma, the Ask LoRA r2, or a successor). |
| **D-9** | Private windows? | **Decided by the user (2026-11-01): no special handling.** |
| **D-10** | Adopt agent-desktop as the Phase 2 action layer, or write the AX action code in Swift? | **Probe first** (not yet run; §3.1 has its status). If snapshot size and ref stability hold up, use it, vendored and pinned, for browsers and native apps, behind the Phase 2 card rules. Copy its snapshot-ref/`STALE_REF` contract into `browser_act` either way. |

## Appendix: evidence commands

```bash
git log --oneline -- browser-extension service/browser app/Sources/WispSafariExtension
```

```bash
gh pr list --repo adjad/wisp --state all --search "browser OR chrome OR safari"
```

```bash
python3 -I -c "import json,collections;c=collections.defaultdict(lambda:[0,0]);[(c[r['category']].__setitem__(0,c[r['category']][0]+bool(r['v'].get('passed'))),c[r['category']].__setitem__(1,c[r['category']][1]+1)) for r in map(json.loads,open('$HOME/.moe/evals/toolbench/20261031-011715/results.jsonl'))];print(dict(c))"
```
