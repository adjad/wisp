# dsh-llm-omlx (prototype)

An `LlmAdapter` that registers a `omlx-local` provider route on `@deepseek-ai/dsh-llm`,
pointing [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`)
at a local **oMLX** OpenAI-compatible server instead of DeepSeek's hosted API.
No cloud account, no API key (unless you've put one in front of oMLX yourself),
everything stays on-device.

**Status: prototype, unrun against a live `dsh` + oMLX pair.** It typechecks
cleanly against the real published harness packages (see below), and its wire
translation is adapted line-for-line from `@deepseek-ai/dsh-llm-deepseek`'s
adapter, which speaks the same OpenAI-compatible chat-completions dialect
DeepSeek uses. What it has *not* had yet: an actual `dsh web` session pointed
at an actual running oMLX instance. Smoke-test before trusting it.

## What's here

| File | Role |
|---|---|
| `src/adapter.ts` | `OmlxAdapter extends LlmAdapter` — the one required method (`stream`), plus `resolveModel`/`listModels`/`providerInfo` |
| `src/serialize.ts` | Harness `Message[]` → OpenAI-style chat-completions request body (text-only) |
| `src/translate.ts` | SSE deltas → the harness `StreamChunk` protocol (text, reasoning, tool-calls, usage, finish) |
| `src/sse.ts` | `[DONE]`-terminated SSE framing via `eventsource-parser` |
| `src/index.ts` | The Cordis plugin: `name`, `inject`, a `Config` schema, and `apply(ctx, config)` calling `ctx.llm.registerAdapter(...)` |

This mirrors the real `packages/llm/llm-deepseek` package structure in the
harness repo, minus everything that's DeepSeek-account-specific: no Files API,
no image upload/pricing, no `dsh-settings`/`dsh-credentials` live-reload
integration. Config is resolved once at `apply()` time.

## Why this shape

Read straight from the harness source (`@deepseek-ai/dsh-llm@0.1.5-alpha.1`,
matching what's on `master` as of 2026-09-08):

- **The seam.** `LlmAdapter` is an abstract class with exactly one required
  method: `stream(options: GenerateOptions): AsyncIterable<StreamChunk>`.
  Everything else (`resolveModel`, `listModels`, `providerInfo`,
  `providerRetryPolicy`, `imageRequestPricing`) has a default and is
  overridden only when the provider has something to say.
- **The wire is genuinely OpenAI-shaped.** DeepSeek's own chat-completions API
  — and by extension `dsh-llm-deepseek` — uses `delta.content`,
  `delta.reasoning_content`, `delta.tool_calls[].function.{name,arguments}`,
  `choices[].finish_reason`, `usage.{prompt_tokens,completion_tokens}`, and a
  `[DONE]` SSE sentinel. That's the same shape any OpenAI-compatible local
  server (oMLX included) speaks, so `translate.ts`/`serialize.ts` here are a
  near-verbatim port, not a reinvention.
- **Registration.** A plugin exports `name`, `inject = ['llm']`, a
  schemastery `Config`, and `apply(ctx, config)`, which builds an adapter
  instance and calls `ctx.llm.registerAdapter([PROVIDER], adapter)`.
  `ctx.llm.registerConfigurableProviders([...])` additionally lists the route
  in `ctx.llm.listConfigurableProviders()` for config UIs — harmless to
  include, safe to drop.

## Try it

This package isn't published to npm. Point `dsh` at the local source directly
— `dsh-app-boot`'s docs say an inserted plugin name may be "absolute
filesystem paths, file URLs, or package specifiers," so no publish step is
required:

```yaml
# in $DSH_HOME/profiles/<profile>/cordis.patch.yml (or wherever your
# composition/bundle lists plugin entries — see docs/cordis-primer.md and
# docs/cordis-tutorial/ in the harness repo for the authoritative patch
# syntax; I did not verify the exact insert/replace YAML keys against a
# running instance)
- name: /Users/adijain/Desktop/MOE_Project/integrations/dsh-llm-omlx/src/index.ts
  config:
    baseURL: http://127.0.0.1:PORT/v1   # oMLX's actual OpenAI-compatible base — no default on purpose
    # apiKeyEnv: OMLX_API_KEY           # only if you've put auth in front of oMLX
    defaultContextWindow: 32000          # match whatever you're actually loading
    models:
      - id: your-omlx-model-id
        contextWindow: 32000
```

Then select it in a request as `provider: 'omlx-local'`, `model: 'your-omlx-model-id'`.

## Verified so far

```bash
npm install   # pulls @deepseek-ai/dsh-llm, dsh-timeout, cordis, schemastery from npm — all published under the `alpha` dist-tag
npx tsc --noEmit   # clean — every LlmAdapter method, StreamChunk variant, and registry call matches the real published types
```

Not yet done: an actual run. Before relying on this:

1. Boot a `dsh` profile with the plugin inserted and confirm
   `ctx.llm.listProviders()` includes `omlx-local`.
2. Send one non-tool-calling turn, confirm text streams back.
3. Send one tool-calling turn against whatever tool-capable model you're
   routing to it, confirm `tool-call` blocks assemble correctly — this is
   the part most likely to break against a given oMLX/model combination.

## Known gaps and Wisp-specific caveats

- **No settings/credentials seam.** Config is static after `apply()`; a real
  integration should mirror `dsh-llm-deepseek`'s pattern (`ctx.inject(['settings'], ...)`,
  `settingsCtx.settings.installSection(...)`, credential resolution through
  `ctx.credentials` instead of a bare `process.env` read) so it live-reloads.
- **Text-only.** Any image content throws `UNSUPPORTED_CONTENT`. Wisp's own
  VLM usage is out of scope here; adding image support means declaring
  per-model `inputModalities` and porting the image-pricing/offload machinery
  `dsh-llm-deepseek` has and this prototype deliberately skipped.
- **No reasoning-effort declaration**, so `resolveModel` never offers one —
  the harness's own call-config validation rejects a caller-supplied
  `reasoningEffort` before it would ever reach this adapter. That sidesteps
  rather than fixes the model-specific reasoning quirks already logged for
  this project (LFM2.5's unclosed `<think>` leaking into `content`, a
  thinking-budget cap force-closing the tag early, gpt-oss needing
  `reasoning_effort` set or it truncates). If you route a reasoning model
  through this adapter, that model's specific quirks are still live and
  unhandled here.
- **`streamIdleTimeoutMs` defaults to 10 minutes**, deliberately generous —
  this project has measured background-scheduler contention on a single
  resident oMLX model pushing one call from ~13s to ~320s. A production
  version should probably surface this as a config knob per profile rather
  than a single hardcoded default.
- **One adapter instance, one oMLX server.** oMLX serves one resident model
  with no request parallelism; running `dsh` and Wisp against the same oMLX
  instance concurrently will contend for it exactly the way any two
  simultaneous local callers would.
- **Model catalog is advisory only**, same caveat as this project's own
  finding that `/v1/models` doesn't imply loadable — a listed model can still
  fail to load when the request actually dispatches.
