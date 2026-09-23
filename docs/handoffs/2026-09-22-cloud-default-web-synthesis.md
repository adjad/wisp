# Cloud-default routing and web synthesis handoff

- Outcome: route ordinary non-sensitive questions and public read-only web,
  weather, and stock requests through the configured Super Model, while keeping
  private data, Mac access, context-dependent follow-ups, and effects local.
- Exact base SHA: `34275d2fbf5b6df5ed3d083ab5c7fe3ccd7e8ed2`
  (`origin/main`, merged PR #72).
- Branch: `codex/cloud-default-web-synthesis`.
- Sole writer: Wisp Hub in the isolated `cloud-default-web-synthesis` worktree.
- Owned paths: `service/inference/super_model.py`, `service/agent/loop.py`,
  `service/main.py`, `service/tools/web_tools.py`, `service/tools/registry.py`,
  `app/Sources/WispApp/SettingsView.swift`, focused routing/web tests, and this
  handoff.
- PR #67 disposition: retain its unread-message behavior already present on
  current main, but replace its display-only news endpoint with bounded cloud
  synthesis over sanitized public evidence. PR #69 is explicitly out of scope.
- Privacy boundary: deterministic secret/path and personal-scope checks and
  Laya remain before cloud client creation. Ambiguous standalone turns retain
  the original 5% cutoff across all three Laya scores. Scoped public tool
  routes also require private-content and context scores below 5%; Laya's
  computer score is discounted there because offline synthetic probes scored
  public news/weather at 67%/53%. In 10 public and 10 synthetic private
  prompts, some private requests also scored below 20%, so the global 20%
  threshold was removed rather than treated as calibrated. Unavailable or
  malformed Laya results fail
  local. Only curated public search, stock, and weather tools may accompany
  a cloud turn. Wisp memory and conversation history are excluded from those
  requests. Mixed public/private or public/effect routes remain local.
- Known conservative fallback: an ambiguous creative prompt such as "Write a
  haiku about the moon" still routes locally when Laya's computer score is
  high. This PR does not claim universal cloud coverage of every safe prompt.
- Default-route repair: the actual router marked "How does a rocket work?" as
  ambiguous and offered nine local tools, including Mac/effect tools, even
  though the cached Laya model scored private/computer/context risk at
  0.0009/0.0327/0.0061. A default route with no bound or required tools can
  now become tool-free cloud generation only after the normal deterministic
  checks and Laya approval, provided its retrieved menu contains no installed
  skill tool or `use_skill`. Scheme URLs, Unicode and ASCII domains, IP
  addresses, and even root or short host/path references stay local for page
  retrieval after bounded HTML/percent decoding and Unicode normalization.
  Ordinary fractions and `and/or` remain eligible. A
  rejected turn keeps its original local route.
- News evidence: cloud news turns synthesize sanitized publisher headlines and
  feed summaries while the UI keeps the linked source cards. Automatic article
  reads and cloud `web_fetch` are excluded. Simulation QA found a pre-existing
  DNS validation/connection race and HTTP redirect downgrade in Wisp's shared
  arbitrary-URL fetcher; that needs a separate connection-bound repair before
  page bodies can safely be part of cloud synthesis.
- Ordinary web search returns its original local result text, but cloud turns
  receive a separate bounded, sentence-filtered view of titles/snippets and
  source hosts. Raw provider URLs and exception text are excluded from the
  model-facing view. The visible source results remain available to the user.
- Validation plan: focused Super Model and web-response regressions; repository
  CI (`python-regressions` and `Verified macOS artifact`) on the exact remote
  candidate; independent Release Auditor review; security/privacy Simulation QA
  because the candidate changes the cloud-data boundary.
- Local evidence before final push: focused routing/web suite 238 passed with
  2,621 subtests; full replay gate 116/116 test modules passed.
- Deployment: no merge, package, installed-app replacement, or relaunch is
  authorized by this implementation request alone.
