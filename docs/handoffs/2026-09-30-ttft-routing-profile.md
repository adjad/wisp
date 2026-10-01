# Wisp routing overhead and shortcut eligibility

Base: `d421519bcc28f818702554d1767a79c01233b5ec`. Branch: `codex/lexical-ranking-reuse`.

## Delivered change and ownership

The default lexical fallback evaluated an identical query twice when its full
request was also its only action clause. `lexical_shortlist` now reuses rankings
by exact query string within that one call. Both rank-fusion contributions still
run, with the original depth, order, weights, tie ordering and limit. Repeated
clauses retain every vote. No result is cached across requests, so registrations,
availability, descriptions and in-place alias edits remain visible.

The Orchestrator acknowledged this worker as sole writer for only:

- `service/router/reranker.py`, `lexical_shortlist` only;
- `tests/test_lexical_routing_reuse.py`;
- `scripts/bench_routing_overhead.py`;
- this handoff.

Claude remains sole repair owner of shared deterministic routing and structured
read findings. This change preserves existing routing decisions, including their
known defects. It does not establish intent correctness for the corpus.

## Paired measurements

The checked-in benchmark uses the actual 168-tool registry, 155 routable tools,
and packaged `tool_retrieval.provider: lexical`. It alternates the uncached
reference and candidate for 30 pairs per scenario. Every pair must have an
identical tool menu and full route contract. Socket connections, subprocesses,
credentials and tool execution are blocked during probes; `WISP_HOME` is a fresh
temporary directory. One fixed read-only Git command records source provenance
before those guards. Source SHA and relevant file hashes accompany the output.

Run from a fresh process:

```sh
python -B scripts/bench_routing_overhead.py --reps 30 --output /tmp/wisp-routing.json
```

These are Python routing times in milliseconds, measured before the final
commit with unchanged candidate source. The pinned existing build test runtime
reported Python **3.13.14**, matching the release pin. The initial development
measurement used Python **3.14.3**. No dependencies, model settings or installed
applications were changed.

| Actual route, Python 3.13.14 | Reference p50 / p95 | Candidate p50 / p95 |
| --- | ---: | ---: |
| `I could use some help` | 11.969 / 12.176 | 6.243 / 6.430 |
| `open Safari` | 2.422 / 2.578 | 1.356 / 1.462 |
| `run the tests` | 3.728 / 4.034 | 2.036 / 2.111 |

The corresponding Python 3.14.3 route medians were 12.730 → 6.541,
2.614 → 1.431, and 3.998 → 2.147 ms. The benchmark matched menus and route
contracts in all 29 scenarios, with one rank evaluation instead of two for a
simple fallback request. Deterministic routes that do not call retrieval have
no expected benefit. Forced standalone retrieval timings on those prompts are
reported separately; they must not be presented as actual route savings.

Controlled cold state means **resetting the in-process BM25 index**, excluding
imports and any engine load. For standalone `open Safari` candidates on Python
3.13.14, p50 was 9.471 → 8.267 ms and p95 11.542 → 8.442 ms, 30 samples per arm.
The warm common-prefix and index cache remain otherwise unchanged.

An earlier disposable prototype at the same base also tested a request with
100 identical synthetic sentences. Python 3.14.3 route p50 was 953.992 →
691.080 ms; standalone retrieval was 391.067 → 138.217 ms. This is an adversarial
long **request**, not normal conversation history. `--stress` reproduces that
expensive case; ordinary repeated-clause coverage is in the default corpus.

## Engine TTFT versus useful answer latency

**Real engine TTFT, scheduler wait, prefill, cache hit/miss, cold model load,
client prompt queue and rendered UI latency are unmeasured.** No isolated real
backend experiment was acknowledged, so no live engine, installed/development
Wisp, production settings, private history or native effects were used.

The existing device benchmark measures model request to first reasoning or
content output. It excludes service admission, routing, readiness and tools.
Dividing prompt tokens by that interval does not isolate prefill throughput.
The production streaming client discards usage-only chunks. The UI stamps a
received reasoning/delta/text event after dispatch, excludes its prompt queue,
and batches deltas for up to 60 ms before display. These are different clocks.

Agent steps currently use `stream=False` and withhold text until completion and
receipt/obligation verification. A first status event, a tool activity line or
discarded prose is therefore not a first useful answer. Removing those
verification boundaries is outside this optimization.

A separate disposable full-service probe called `main.agent` directly and
consumed SSE in normal mode, with a zero-delay scripted model, synthetic stores,
fixed clock, empty memory/identity/skills, inert tools and auto-denial. It did not
run lifespan or a server. Native typed-task preparation and rolling summaries
were explicitly stubbed. Timed stages include workflow/read compilation,
routing, prompt construction/fitting, mock readiness/generation and retained
service text. Inclusive stages overlap and must not be added together.

| Synthetic path, Python 3.14.3, 30 samples | First retained service text p50 / p95 ms | Observed path |
| --- | ---: | --- |
| Greeting | 0.673 / 0.814 | One scripted model request, no tools |
| `summarize my messages` | 1.922 / 3.952 | One fake summary tool, no model request/start |
| Battery read | 1.981 / 2.156 | Direct fake read, one scripted narration |
| Ambiguous `I could use some help` | 14.230 / 15.761 | Lexical menu, optional Ling decision, no forced tool |
| Notes lookup | 2.478 / 3.109 | Two scripted model requests around one fake read |
| 20 synthetic history pairs + ambiguous prompt | 17.554 / 18.092 | Scoped optional menu and scripted answer |
| Controlled cold readiness fixture | 12.815 / 13.027 | Injected 10 ms async readiness delay; scheduler added overhead |
| Calendar + Messages | 1.219 / 2.353 | **FAIL:** only calendar read, Messages clause lost |

The last row is not a performance success. The injected readiness delay is not
hardware cold-load evidence. A separate direct `run_agent` send fixture reached
one auto-denied confirmation per sample, executed zero send bodies and made one
scripted model request; it intentionally excluded endpoint/workflow routing.
The endpoint data-delivery fixture returned a content clarification, with no
send/model execution. Neither result establishes live-model routing accuracy.

Disposable evidence is retained at `/private/tmp/wisp-ttft-routing-profile.json`,
`/private/tmp/wisp-ttft-pipeline-profile.json`,
`/private/tmp/wisp-ttft-candidate-routing.json` and
`/private/tmp/wisp-ttft-candidate-routing-py313.json`. The endpoint harness is
`/private/tmp/wisp_pipeline_profile.py`; its times are discovery evidence, not
candidate release-gate evidence.

## Routing correctness findings and dependencies

Thirty-six inert boundary probes were run on main and a disposable copy with
only three files' PR129/131 patch hunks. That copy is **not an exact full PR
candidate**. Its retrieval fallback was mocked; direct/scoped/forced decisions
and compiler arguments used actual source. Exact candidate verification remains
with the independent Auditor and Claude's acknowledged repair scope.

- `Do not lock my screen.` and an explanation quoting `lock my screen` directly
  selected `lock_screen({})`. Quoted battery/clipboard wording also selected
  device reads. Positive device controls selected their intended tools.
- Calendar `tomorrow → days:2` and `next week → days:14` can be useful supersets,
  but discard exact scope. `next month → days:30` can omit the end of the next
  calendar month. The tool supports exact `period` values.
- Explicit Calendar-only wording still lacked supported `calendar_only=True`,
  allowing `get_upcoming` to synchronize/read Reminders. Date and source scope
  must survive dispatch as arguments, not just tool-schema exclusions.
- An inbox query consumed a following delete action or a `yesterday` modifier
  into the search string. Negated email/Messages sources still appeared in
  several scoped menus.
- Some named-recipient authored `saying` forms still compiled as calendar
  deliveries; recognized `asking` forms avoided that workflow after PR129 but
  full routing could still force a calendar read. Quoted reminders could create
  reminder obligations. Source wording inside authored text needs separate
  interpretation from permission to read that source.
- Full-endpoint `Check my calendar and summarize my messages` executed only
  `get_upcoming(days=7)` and returned before full routing. A router-only test
  would miss this structured-read bypass.

Orchestrator acknowledgements keep Claude as sole repair owner for direct-device
eligibility and narrow whole-request structured-read eligibility in PR131.
Other findings were reported for its owner/review record, without duplicate
repairs. A valid shortcut must consume supported positive intent, source scope,
recipient/target, dates and all action clauses. Otherwise preserve Ling selection
or an honest clarification, while keeping prohibitions and approval boundaries.
No growing keyword list, new model or MCP service is proposed.

## Validation and remaining gates

Focused development checks: **47 passed** on Python 3.14.3. Focused pinned-runtime
checks across new reuse, existing lexical retrieval, lazy readiness and
meaning-based routing: **63 passed, 194 subtests passed** on Python 3.13.14.
They cover duplicate votes/depths, distinct exact keys, tie order/limits, real
registry aliases, gated/pinned tools and changes between requests.

The first sandboxed full regression preflight encountered **14 failures** in
`test_browser_transport_native.py`: local AF_UNIX binds were rejected with
`PermissionError: Operation not permitted`, and dependent signed-fixture
activations timed out. These are disposable fixtures; no browser or Keychain
is used. This is not recorded as a regression pass. Final exact-SHA mechanical
results, environment reruns and configured CI belong in the PR/coordinator
handoff after candidate freeze; no failed or unavailable check is a CI pass.

Independent Release Audit is triggered by performance/routing risk. Any needed
specialist QA is read-only and synthetic. The worker does not self-approve,
merge, install or relaunch. The broader shortcut correctness outcome remains
incomplete until Claude's repairs pass exact-candidate gates.
