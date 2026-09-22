# Cloud-default routing and web synthesis handoff

- Outcome: route ordinary non-sensitive questions and public read-only web,
  weather, and stock requests through the configured Super Model, while keeping
  private data, Mac access, context-dependent follow-ups, and effects local.
- Exact base SHA: `34275d2fbf5b6df5ed3d083ab5c7fe3ccd7e8ed2`
  (`origin/main`, merged PR #72).
- Branch: `codex/cloud-default-web-synthesis`.
- Sole writer: Wisp Hub in the isolated `cloud-default-web-synthesis` worktree.
- Owned paths: `service/inference/super_model.py`, `service/agent/loop.py`,
  `service/main.py`, `service/tools/web_tools.py`,
  `app/Sources/WispApp/SettingsView.swift`, focused routing/web tests, and this
  handoff.
- PR #67 disposition: retain its unread-message behavior already present on
  current main, but replace its display-only news endpoint with bounded cloud
  synthesis over sanitized public evidence. PR #69 is explicitly out of scope.
- Privacy boundary: deterministic secret/path checks and Laya remain before
  cloud client creation. Laya uncertainty or unavailability fails local. Only
  `web_search`, `web_fetch`, `get_stock_price`, and `get_weather` may accompany
  a cloud turn. Wisp memory and conversation history are excluded from those
  requests. Mixed public/private or public/effect routes remain local.
- News evidence: cloud news turns may fetch up to two validated publisher
  article links through Wisp's existing public-page fetcher. Each read has an
  eight-second deadline; redirects to another host and instruction-like page
  text are discarded. Failed reads leave the feed evidence available. The
  article body is bounded before it reaches the cloud model and is not added
  to the compact source cards.
- Validation plan: focused Super Model and web-response regressions; repository
  CI (`python-regressions` and `Verified macOS artifact`) on the exact remote
  candidate; independent Release Auditor review; security/privacy Simulation QA
  because the candidate changes the cloud-data boundary.
- Deployment: no merge, package, installed-app replacement, or relaunch is
  authorized by this implementation request alone.
