# OpenRouter optional-cloud pilot

Date: 2026-09-19. Base: `014aee2c9b73658e439c820f9e649ff926fabdb9`.
Sole writer: OpenRouter pilot task, isolated worktree `2ead`.
Owned paths: `experiments/openrouter_pilot/**`, acknowledged by Wisp Hub.
The Hub supplied the newer Local AGENTS policy, which supersedes the retired
Orchestrator registry. No production files or existing repair scopes are owned here.

## Decision

**Worth a small, approved live evaluation as an optional cloud path; insufficient
evidence to adopt in production or replace local inference.** OpenRouter provides
a practical common API for comparing cloud models. Wisp would still need a distinct
client, explicit data consent, isolated credentials, and billing controls. This
pilot establishes local protocol-handling feasibility only. No result demonstrates
live OpenRouter compatibility, model correctness, latency, or provider privacy.

## Plain test plan and what ran

1. Read current official API, tools, routing, streaming, errors, usage, pricing,
   and privacy documentation (links below).
2. Inspect Wisp's client/configuration/readiness without running Wisp.
3. Build an isolated, standard-library-only protocol prototype, disabled by
   default, with no network transport, key reader, subprocess, or tool executor.
4. Feed handwritten synthetic request/response fixtures through it. Exercise
   successful text and tool turns plus malformed/error/truncated responses.
5. Report local evidence separately from the proposed live experiment.

Run from the repository root:

```sh
python3 -B -m unittest discover -s experiments/openrouter_pilot -p 'test_*.py' -v
```

Final result: **22 tests passed**, Python 3.14.3, 0.007 seconds. That duration is
fixture processing time, not inference latency. Tests replace `socket.socket`
with a raising guard. No package installation was needed. All response fixtures
are invented; `fixture/model` and `fixture-provider` are not catalog selections.

A bounded read-only helper reviewed the prototype and found falsey malformed
tool-call containers were being treated as absent, plus insufficient provider
type checking. Both were tightened and covered by regressions. The first added
test run had a test-edit `NameError` (a missing-ID assertion was moved into the
wrong method); this was corrected and the final 22-test run passed. The helper's
review is not a Release Auditor verdict. `git diff --check` also passed.

| Local contract exercised | Result |
| --- | --- |
| Disabled default; fixed destination; no authorization header or local extra kwargs | Pass |
| Explicit provider allowlist, no fallback, required parameter support, data-policy/ZDR fields | Pass: fields constructed, server enforcement untested |
| Tool schema preserved across a two-turn synthetic exchange; matching tool result IDs; opaque non-streaming reasoning details preserved | Pass |
| Text, token usage, zero versus missing cost, invalid cost/token counts | Pass |
| Redirect and 400/401/402/403/408/429/500/502/503 statuses; HTTP-200 error body | Pass: reject, no retry, fixed error text |
| Malformed JSON/envelopes, empty/filtered/length-truncated completions | Pass |
| Unknown tool names, missing/duplicate call IDs, invalid/non-object arguments | Pass |
| CRLF SSE, keepalive comments, every two-chunk byte split including UTF-8, one-byte chunks | Pass |
| Tool argument fragments; empty-choice usage and OpenRouter repeated-terminal usage fixtures | Pass |
| Missing DONE/finish, conflicting finish, late data, provider stream errors, oversized buffers | Pass |
| Reasoning streams | Explicitly rejected as outside this pilot |

`python3 -B -m pytest -q tests/test_inference_endpoints.py tests/test_lazy_inference_readiness.py`
could not run: the available Python has **no pytest** (and inspection also found
no httpx or pytest-asyncio). The full repository regression gate was not run.
Its existing command is `python scripts/test_replay_failure_fixes.py`, used in
`.github/workflows/regression-gate.yml`. No production code changed. This pilot
is outside the repository's automatic test-discovery roots; use the explicit
command above. GitHub CI and independent Release Auditor review are not supplied
by this offline run; this is not a shipping candidate or release approval.

## Wisp fit and integration dependencies

Read-only exploration found concrete reasons to keep this separate:

- `service/config/endpoints.py:79` rejects base-URL paths; OpenRouter uses
  `/api/v1/chat/completions`. `OMLXClient` hardcodes `/v1/chat/completions` and
  expects `/health` and model-readiness operations (`omlx_client.py:215,387,445`).
- `service/main.py:760` constructs OMLXClient for remote targets. An explicit
  provider/client factory and separate cloud readiness contract would be needed.
- `service/inference/readiness.py:45` couples generation to readiness. Preserve
  its pre-generation, tool-free-only local fallback restrictions.
- `service/config/endpoints.py:104` keeps fast/router local. Preserve those role
  constraints and the default local path.
- `service/config/credentials.py:65` binds mini credentials to the reviewed mini
  origin/purpose. Cloud must receive its own approved key source. Retain strict
  origin binding, disabled redirects and proxy/environment isolation from the
  existing transport design; do not reuse that transport by assumption.
- `service/main.py:777` includes session history in model messages. A cloud
  selection could expose much more than the latest prompt. Consent must cover
  conversation history, tool schemas, retrieved context, and tool results.

No current application callers import this prototype. No production changes,
installed-app changes, other-worktree writes, global settings, or PR changes
were made. Future integration touches shared configuration/main/readiness paths
and requires a fresh ownership assignment, independent review and exact-head CI.

## Documentation findings

OpenRouter documents a chat-completions endpoint and client-side tool calling:
the application supplies schemas, receives calls, and returns tool results in
later messages. That maps to Wisp's high-level flow but does not validate any
specific model/tool combination. [Quickstart](https://openrouter.ai/docs/quickstart),
[client tools](https://openrouter.ai/docs/guides/features/tool-calling).

Routing defaults can accept unsupported parameters. The pilot explicitly sets
`require_parameters`, `only`, `allow_fallbacks: false`, `data_collection: deny`,
and `zdr: true`. A future live test must verify that the chosen endpoint satisfies
all constraints; availability may shrink. `max_price` is a unit-price filter,
not a total spending budget. Tool strictness is provider-dependent; for example,
the docs describe an additional Anthropic header requirement. This prototype
does not depend on provider-side strict validation.
[Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

SSE can contain keepalive comments, errors after HTTP 200, and a final usage
chunk repeating the terminal finish reason. These motivate the failure fixtures.
The prototype emits a completed object only after parsing the whole bounded
stream; it is not Wisp's incremental content/reasoning/final event interface.
[Streaming](https://openrouter.ai/docs/api_reference/streaming),
[errors](https://openrouter.ai/docs/api_reference/errors-and-debugging).

Usage is documented as automatically included, with token counts and charged
cost; older opt-in usage parameters are deprecated. Missing cost remains unknown
in the prototype, not free usage. [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

## Cost and privacy

**Actual provider spend: $0. Actual inference requests: 0.** No credentials were
read, provisioned, reused, or transmitted; no subscription/credit purchase or
external account change occurred. Only public documentation was retrieved.

At review time the official pricing table lists Standard platform fees of 5.5%
and Business 8%; model usage varies. The FAQ describes underlying provider
pricing passthrough and fees when buying credits. Exact taxes, funding minimums,
and the chosen model's current rates must be checked before approval. The Free
plan table lists no data-policy routing or spend controls, so free models are
not an assumed substitute for a controlled evaluation.
[Pricing](https://openrouter.ai/pricing), [FAQ](https://openrouter.ai/docs/faq).

Illustration only, **not a quote or measured bill**: 12 requests each using
1,024 input and 128 output tokens at assumed rates of $1/$4 per million tokens
would consume $0.018432 of inference; a simplified 5.5% fee allocation gives
about $0.019446. Tool follow-ups count as additional requests and resend context.
Reasoning, retries, cache behavior, fixed/request charges and funding minimums
can change costs. This prototype implements neither a budget ledger nor billing
reconciliation, and no live cost ceiling is currently enforced by it.

Cloud prompts traverse OpenRouter and a downstream provider. OpenRouter states
prompt/response storage is opt-in; downstream policies vary. Routing flags are
policy requests, not evidence obtained by these tests. ZDR does not mean local
processing or establish geography/compliance. Account logging, caching, provider
terms and selected endpoints need review before any real Wisp data is considered.
[Data collection](https://openrouter.ai/docs/guides/privacy/data-collection),
[provider logging](https://openrouter.ai/docs/guides/privacy/provider-logging),
[ZDR](https://openrouter.ai/docs/guides/features/zdr).

## Limitations and proposed next decision

Not tested: live auth/TLS/routing enforcement, model availability, quality,
latency, rate-limit behavior, cancellation, network timeout/retry handling,
actual billed usage, SDK compatibility, or a running Wisp application. No network
transport exists. Request inputs are trusted synthetic fixtures, not a full
request validator. Tool arguments are checked for JSON-object shape and advertised
name only, **not full JSON Schema conformance or authorization**. No tools execute.
Streaming reasoning, multimodal content, full SSE variants, and incremental
backpressure are outside scope. Wire size bounds are not token budget bounds.

A useful next experiment would require explicit user approval for dedicated
credential use and a **maximum $0.25 incremental inference spend**, using an
existing funded account only (no purchase authority). Proposed limits: at most
12 serial HTTP inference attempts total, 128 output tokens per attempt, small
synthetic inputs, no automatic retries, one explicitly selected tool-capable
model/provider satisfying data policies. Verify current catalog rates, input
token accounting and provider-side key budget controls first; reserve a
conservative worst-case cost before each request and stop on unknown billing.
No key should be pasted into this report or conversation. Provisioning or account
changes require their own approval.

The live matrix would cover plain text, automatic versus forced synthetic tool
selection, tool result round trips, and streamed equivalents. Record requested
and served model/provider, schema validity, finish status, total latency,
time-to-first-token, tokens, billed cost, and failures. Use prewritten synthetic
tool results. Compare quality/latency with a separately authorized local baseline
before any adoption decision; do not interrupt other local inference experiments.

Delivery here is the bounded offline evaluation. Live execution and production
adoption remain separate decisions; merge/deploy are not authorized.
