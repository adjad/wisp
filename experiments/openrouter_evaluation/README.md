# OpenRouter transport evaluation — offline only

Date: 2026-09-19. Base: `014aee2c9b73658e439c820f9e649ff926fabdb9`.
Owner: Hub-dispatched evaluation in isolated worktree `2015`.
Scope: `experiments/openrouter_evaluation/**`, explicitly acknowledged by Hub.
Branch: `codex/openrouter-transport-evaluation`.
Exact delivery head is in the task handoff; obtain it with `git rev-parse HEAD`.

## Outcome and recommendation

**Feasible as a separate, opt-in transport; not ready for production enablement.**
Keep Wisp local-first. The existing pilot proves limited protocol parsing, not a
working cloud client. This evaluation reproduces its limits and tests the actual
Wisp endpoint/readiness code with fake dependencies. No production code changed.

43 synthetic tests pass: 22 unchanged pilot contracts plus 21 new characterization
tests. A passing `test_gap_*` means a limitation was successfully reproduced, **not
that the limitation was fixed**. Do not merge this as cloud enablement or claim
model quality, live compatibility, privacy compliance, or billing correctness.

No provider requests, keys, personal data, tool execution, installed-app changes,
packaging, deployment, or merge occurred. Provider spend is zero. Public vendor
documentation was read; all model IDs and response bodies in tests are invented.

## Reproducible evidence

Run these commands from the repository root; default pytest discovery excludes
this experimental directory. Standard library only, Python 3.14.3 used here.

```sh
python3 -B -m unittest discover -s experiments/openrouter_evaluation/vendor -p 'test_*.py' -v
python3 -B -m unittest discover -s experiments/openrouter_evaluation -p 'test_*.py' -v
git diff --check
```

Results: **22/22**, **21/21**, and whitespace check passed. The ordinary regression
command attempted was:

```sh
python3 -B -m pytest -q tests/test_inference_endpoints.py tests/test_lazy_inference_readiness.py
```

It failed before collection: `No module named pytest`. `httpx` and `pytest_asyncio`
are also unavailable. No dependencies were installed. The full repository gate
(`python scripts/test_replay_failure_fixes.py`) was not run. CI is **unavailable /
non-passing for this evaluation**, not a CI pass; no PR or release approval is
provided. The bounded read-only architecture helper is not the Release Auditor.

The new suite loads the real `endpoints.py`, `readiness.py`, and exception class
by file path, temporarily substitutes dependency modules, and never initializes
the Wisp service or its stores. Async fake methods run on a real event loop.
Socket connect/bind operations are denied in async tests; pilot tests deny socket
construction. This is evidence about wrapper behavior, not a real HTTP client's
cleanup, TLS, credential binding, timeout implementation, or cancellation billing.

### Historical pilot provenance

The two vendor files are byte-for-byte snapshots from
`ee950e7387f6d279a6f79b6742ab72d4fb5d2438`, originally under
`experiments/openrouter_pilot/`. They are copied into the owned directory so the
suite remains runnable without checking out or modifying the earlier worker.

| File | Git blob SHA |
| --- | --- |
| `vendor/adapter.py` | `bb87172ba004573da6074be6506a708cf5054afb` |
| `vendor/test_contract.py` | `d2ad74e840a8deef1977445198f376c91ab90de5` |

Both match `git hash-object`. The prior branch itself is not merged or cherry-picked.

## Test matrix and interpretation

| Area | Synthetic result | Remaining requirement |
| --- | --- | --- |
| Provider/model selection | Explicit provider-only request with fallbacks off; unqualified IDs still accepted and returned identity discarded | Approved model/provider list, capability qualification, retain requested/served identity and reject unexpected routing |
| Requests | Fixed URL, bounded output and bytes, no arbitrary local kwargs; fixture tool schemas retained | Full request schema, cloud parameter allowlist, input/context token accounting, per-send authorization |
| Responses | Text/tool round trip, exact tool-result IDs, malformed JSON, unknown tools and bad arguments rejected | Full tool schema validation and existing Wisp approval/dispatch gates; `message/usage` pilot result needs Wisp `choices` normalization |
| Streaming | Byte-split UTF-8, CRLF, comments, tool fragments, usage frames, missing DONE, truncation and midstream errors tested | Incremental bounded SSE parser; reasoning normalization, backpressure, safe final event; pilot buffers everything and rejects reasoning streams |
| Cancellation | Actual Wisp wrapper closes fake inner generator on cancellation and early consumer close | Qualify new HTTP adapter; closure alone does not prove upstream billing stops |
| Timeout/fallback | Readiness timeout selects local for tool-free work only; tool requests, cancellation and generation timeout never fall back; partial-stream failure has no final | Cloud absolute deadline plus connect/read-idle deadlines, bounded buffers and cleanup; current wrapper applies timeout only to readiness |
| Privacy/redaction | Synthetic private-history and tool-result markers pass through pilot unchanged | Block unapproved context before every send; explicit consent and provenance policy; safe diagnostics |
| Cost | Missing usage stays unknown; malformed/negative costs rejected; repeated requests and a cost of 999 accepted | Durable atomic budget reservations and settlement; no cap exists in pilot |
| Errors | HTTP redirects/400/401/402/403/408/429/5xx, HTTP-200 error, filtered/empty/truncated completion rejected without provider-body echo | Iterator transport exceptions currently propagate unsanitized; map them to safe categories without accidental retries |
| Disabled default | Pilot requires literal `True`; real role defaults remain local; fast/router reject remote; explicit endpoint disable works | New cloud switch must default false: generic named endpoints currently default enabled when configured |

## Architecture and security/privacy findings

1. **A URL swap cannot work.** `service/config/endpoints.py:81` rejects base URL
   paths, while `service/inference/omlx_client.py` hardcodes `/v1/chat/completions`,
   `/health`, and `/v1/models`. OpenRouter's pilot destination is
   `/api/v1/chat/completions`. `service/main.py:764` constructs OMLXClient for named
   remote targets. Use a separate client factory and cloud readiness semantics;
   do not relax native endpoint security to fit it.
2. **Cloud egress lacks a data boundary.** `service/main.py:783` assembles session
   history and `:892` inserts memory; `service/agent/loop.py:1216` adds memory and
   `:1563`/`:2114` append tool results. Redacting only the latest user text would
   miss these paths, schemas and follow-up turns. Prefer an explicit text-only
   first integration with no implicit history, memory, retrieved context or tools.
   Regex secret masking is defense in depth, not consent or a complete classifier.
3. **Preserve credential isolation.** Existing transport disables redirects and
   proxy-environment inheritance and binds native credentials to reviewed origins.
   Cloud requires its own credential source, exact HTTPS origin/path allowlist,
   revocation/quarantine handling and non-sensitive diagnostics. Never reuse local
   or mini keys. Generic environment references are not a cloud consent mechanism.
4. **Preserve tool security.** The pilot checks advertised name and JSON-object
   arguments only. It neither validates the whole schema nor executes anything.
   Provider output remains untrusted; existing capability, safety, confirmation,
   and executor gates must remain downstream. Initial cloud mode should have no
   tool capability. A transport must never turn provider errors into executable
   partial calls or trigger cloud fallback from local failure.
5. **Normalize errors and accounting before agent retries.** OMLX `_ensure_choices`
   can synthesize an empty completion for an invalid envelope; agent-loop empty
   output retry paths (`:1804`, `:1896`) can then issue more generations. Streaming
   drops usage-only frames and does not retain opaque reasoning details. Cloud
   must classify failures explicitly, preserve usage and account for each attempt,
   including tool follow-ups and model-driven resampling. Transport retry disabled
   alone does not bound spending.

Current public docs support these design constraints, not operational proof:

- Provider `only`, `allow_fallbacks: false`, `require_parameters`, data policy and
  ZDR restrictions should be explicit. `max_price` limits unit pricing, not the
  total bill. [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
- Streaming can carry an error after HTTP 200 and a repeated terminal usage frame.
  Aborting does not stop processing/billing for all providers or non-streaming
  requests. Keep budget reserved on cancellation until reconciliation.
  [Streaming](https://openrouter.ai/docs/api_reference/streaming).
- Per-request `provider.zdr` does not exclude response caching; account-level ZDR
  does. Review account/preset logging and caching as well as downstream policy.
  ZDR does not mean local execution. No account settings were inspected or changed.
  [Response caching](https://openrouter.ai/docs/guides/features/response-caching).

## Estimated integration scope

Engineering estimate, not benchmark evidence: **medium-to-large**, approximately
2–4 engineer-weeks for a reviewed text-only production path, with tool/memory use
a separate qualification. Estimate assumes available credential/settings UI and
one deliberately selected model/provider; review or native integration can add time.

| Work package | Likely paths / dependency | Acceptance boundary |
| --- | --- | --- |
| Opt-in settings and client factory | config, main, settings UI; shared routing ownership | Absent/false cloud switch never reads a cloud key or constructs a cloud client; fast/router remain local; visible per-turn selection |
| Dedicated transport | new inference adapter; credential and quarantine owners | Fixed destination, no redirects/proxies, bounded request/stream size and deadlines, cancellation cleanup, normalized Wisp event/response shape |
| Egress and accounting | context assembly, agent loop, privacy controls, new ledger | Explicit data scope every attempt; atomic worst-case reservations for input/output/reasoning and request fees; unknown rates/usage fail closed; no reserve refund on mere disconnect |
| Qualification | contract tests, auditor and specialist security/privacy review | Synthetic adversarial tests and existing regressions at exact SHA; any live qualification separately authorized |

For concurrency, reserve before sending under one transaction/lock across callers,
count every attempt, and retain unresolved reservations across restart. Stop new
sends on missing usage or overruns; reconcile rather than assume zero. A provider
key/account cap is additional protection, not a replacement for the local ledger.
No such enforcement is implemented by this evaluation.

Integration dependencies/conflicts: this additive directory has no production
overlap; future work would overlap routing/model selection, native credential
provisioning and agent-loop owners. Reassign ownership before such edits. No
adoption-critical issue is repaired here. Keep this experiment disabled and use
its failing-behavior characterizations to scope the next separately owned change.
