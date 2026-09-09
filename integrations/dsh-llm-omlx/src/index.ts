/**
 * Register an {@link OmlxAdapter} for the `omlx-local` provider route on
 * `ctx.llm`. Mount this beside `@deepseek-ai/dsh-llm` in a dsh composition to
 * point the harness at a local oMLX server instead of DeepSeek's hosted API —
 * no cloud account, no API key, model resolution stays entirely on-device.
 *
 * Prototype scope: static config only (no `dsh-settings`/`dsh-credentials`
 * integration, no image input, no retry-policy tuning). See README.md for
 * what a production version would still need to add.
 * @module dsh-llm-omlx
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { LlmError } from '@deepseek-ai/dsh-llm'
import { OmlxAdapter } from './adapter.ts'
import type { OmlxCatalogModel, OmlxConnectionOptions } from './adapter.ts'

export { OmlxAdapter } from './adapter.ts'
export type { OmlxAdapterOptions, OmlxCatalogModel, OmlxConnectionOptions } from './adapter.ts'

export const name = 'llm-omlx'
export const inject = ['llm']

/** The single provider route this plugin owns; select it as `provider: 'omlx-local'` in a request. */
export const PROVIDER = 'omlx-local'

const DEFAULT_CONTEXT_WINDOW = 32_000
// Background-scheduler contention on a single resident model has been
// observed to push one call from ~13s to ~320s on the reference hardware
// (M-series, one resident oMLX model, no request parallelism) — default
// generously so a busy-but-alive server doesn't read as a hang.
const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 600_000

export interface Config {
  /** oMLX's OpenAI-compatible base URL, e.g. `http://127.0.0.1:8000/v1`. No default — get this wrong and every request 404s or connects to the wrong server. */
  baseURL: string
  /**
   * Environment-variable name holding the credential, resolved at request
   * time (never stored raw in this config) — mirrors llm-deepseek's
   * `apiKeyEnv` convention. Unset by default: most local oMLX deployments
   * enforce no auth at all, so a request sends no `Authorization` header.
   * A production adapter should resolve this through `ctx.credentials` /
   * `launchEnvironmentOf(ctx)` instead of the bare `process.env` read this
   * prototype uses.
   */
  apiKeyEnv?: string
  /** Advisory model catalog; an unlisted model id still works, it just resolves with generic (non-catalog) capability metadata. */
  models?: OmlxCatalogModel[]
  /** Context window assumed for a model absent from `models` (default 32,000 — set this to match what you actually load). */
  defaultContextWindow?: number
  /** Per-request output cap applied when a request omits one. */
  defaultMaxTokens?: number
  /** Maximum idle time between stream reads before the call fails with `TIMEOUT` (default 10 minutes — see the default's rationale above). */
  streamIdleTimeoutMs?: number
}

const catalogModel: z<OmlxCatalogModel> = z.object({
  id: z.string().required(),
  name: z.string(),
  contextWindow: z.number().step(1).min(1),
  maxTokens: z.number().step(1).min(1),
})

export const Config: z<Config> = z.object({
  baseURL: z.string().required(),
  apiKeyEnv: z.string().role('credential-ref'),
  models: z.array(catalogModel).default([]),
  defaultContextWindow: z.number().step(1).min(1).default(DEFAULT_CONTEXT_WINDOW),
  defaultMaxTokens: z.number().step(1).min(1),
  streamIdleTimeoutMs: z.number().min(1).default(DEFAULT_STREAM_IDLE_TIMEOUT_MS),
})

function resolveOptions(config: Config): OmlxConnectionOptions {
  const baseURL = config.baseURL.replace(/\/+$/, '')
  if (baseURL.length === 0) {
    throw new LlmError('llm-omlx: baseURL must not be empty', 'INVALID_CONFIG')
  }
  // Prototype-simple credential resolution: a real deployment should route
  // this through ctx.credentials (or launchEnvironmentOf(ctx), which only
  // honors trusted environment layers) the way llm-deepseek does, instead of
  // reading process.env directly here.
  const apiKey = config.apiKeyEnv === undefined ? undefined : process.env[config.apiKeyEnv]
  return {
    baseURL,
    apiKey: apiKey && apiKey.length > 0 ? apiKey : undefined,
    models: config.models ?? [],
    defaultContextWindow: config.defaultContextWindow ?? DEFAULT_CONTEXT_WINDOW,
    defaultMaxTokens: config.defaultMaxTokens,
    streamIdleTimeoutMs: config.streamIdleTimeoutMs ?? DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  }
}

export function apply(ctx: Context, config: Config): void {
  const adapter = new OmlxAdapter({ options: () => resolveOptions(config) })
  ctx.llm.registerConfigurableProviders([
    { provider: PROVIDER, displayName: 'oMLX (local)', settingsNs: name, settingsPath: [] },
  ])
  ctx.llm.registerAdapter([PROVIDER], adapter)
}
