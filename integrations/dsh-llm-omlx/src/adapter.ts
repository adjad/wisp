/**
 * `LlmAdapter` implementation for a local oMLX server: a plain
 * OpenAI-compatible chat-completions endpoint with no cloud account, no
 * quota, and (usually) no credential. One instance serves every model name
 * it is registered under — the harness model name IS the oMLX model id.
 */

import { attributionHeaders, LlmError } from '@deepseek-ai/dsh-llm'
import type {
  GenerateOptions,
  LlmModelInfo,
  LlmProviderInfo,
  LlmResolvedModelInfo,
  StreamChunk,
} from '@deepseek-ai/dsh-llm'
import { LlmAdapter } from '@deepseek-ai/dsh-llm'
import { idleWatchdog, timeoutOf } from '@deepseek-ai/dsh-timeout'
import { parseSse } from './sse.ts'
import { serializeRequest } from './serialize.ts'
import { translate } from './translate.ts'
import type { WireError, WireModelsResponse } from './types.ts'

const STREAM_IDLE_TIMEOUT_CODE = 'OMLX_STREAM_IDLE_TIMEOUT'

export interface OmlxCatalogModel {
  id: string
  name?: string
  contextWindow?: number
  maxTokens?: number
}

export interface OmlxConnectionOptions {
  /** oMLX's OpenAI-compatible base, e.g. `http://127.0.0.1:8000/v1` (no trailing slash). */
  baseURL: string
  /** Sent as `Authorization: Bearer <key>` when non-empty; oMLX typically needs none. */
  apiKey?: string
  models: OmlxCatalogModel[]
  defaultContextWindow: number
  defaultMaxTokens?: number
  streamIdleTimeoutMs: number
}

export interface OmlxAdapterOptions {
  /** Resolve current connection facts per request, so a settings change reaches the next call without a restart. */
  options: () => OmlxConnectionOptions
}

/** Map an HTTP status to a stable LlmError code, same taxonomy dsh-llm-deepseek uses. */
function httpErrorCode(status: number): string {
  if (status === 401 || status === 403) return 'AUTH'
  if (status === 413) return 'INVALID_REQUEST'
  if (status === 429) return 'RATE_LIMIT'
  if (status === 400) return 'INVALID_REQUEST'
  if (status >= 500) return 'SERVER'
  return `HTTP_${status}`
}

function modelInfo(provider: string, model: OmlxCatalogModel): LlmModelInfo {
  return {
    provider,
    id: model.id,
    name: model.name ?? model.id,
    inputModalities: ['text'],
  }
}

export class OmlxAdapter extends LlmAdapter {
  constructor(private readonly config: OmlxAdapterOptions) {
    super()
  }

  override providerInfo(provider: string): LlmProviderInfo {
    return { id: provider, name: 'oMLX (local)' }
  }

  override listModels(provider: string): Promise<readonly LlmModelInfo[]> {
    return Promise.resolve(this.config.options().models.map(model => modelInfo(provider, model)))
  }

  /**
   * Best-effort live discovery via `GET /models`. Advisory only, same as the
   * configured catalog: oMLX listing a model does not mean it currently fits
   * in the resident-model budget (`/v1/models` != loadable) — a caller must
   * still handle a load failure from the actual chat-completions call.
   */
  async discoverModels(provider: string, signal?: AbortSignal): Promise<readonly LlmModelInfo[]> {
    const connection = this.config.options()
    try {
      const response = await fetch(`${connection.baseURL}/models`, {
        headers: { ...attributionHeaders() },
        signal,
      })
      if (!response.ok) return []
      const body = await response.json() as WireModelsResponse
      return (body.data ?? []).map(entry => ({
        provider,
        id: entry.id,
        name: entry.id,
        inputModalities: ['text' as const],
      }))
    } catch {
      return []
    }
  }

  override resolveModel(provider: string, model: string): Promise<LlmResolvedModelInfo> {
    const connection = this.config.options()
    const configured = connection.models.find(entry => entry.id === model)
    const defaultMaxTokens = configured?.maxTokens ?? connection.defaultMaxTokens
    return Promise.resolve({
      ...configured === undefined
        ? { provider, id: model, name: model, inputModalities: ['text' as const] }
        : modelInfo(provider, configured),
      context: { contextWindow: configured?.contextWindow ?? connection.defaultContextWindow },
      ...defaultMaxTokens !== undefined ? { defaultMaxTokens } : {},
    })
  }

  stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    return this.streamWithConnection(options, this.config.options())
  }

  private async* streamWithConnection(
    options: GenerateOptions,
    connection: OmlxConnectionOptions,
  ): AsyncIterable<StreamChunk> {
    const consumer = new AbortController()
    const upstream = options.signal === undefined
      ? consumer.signal
      : AbortSignal.any([options.signal, consumer.signal])
    using watchdog = idleWatchdog(upstream, connection.streamIdleTimeoutMs, STREAM_IDLE_TIMEOUT_CODE)
    const iterator = this.request(options, watchdog.signal, connection)[Symbol.asyncIterator]()
    let exhausted = false
    try {
      while (true) {
        const result = await watchdog.next(iterator)
        if (result.done) {
          exhausted = true
          return
        }
        yield result.value
      }
    } catch (error: unknown) {
      if (timeoutOf(watchdog.signal, STREAM_IDLE_TIMEOUT_CODE) !== undefined) {
        throw new LlmError(
          `oMLX stream idle timeout after ${connection.streamIdleTimeoutMs}ms`
          + ' (a busy resident model or cold model-swap can legitimately take this long — raise streamIdleTimeoutMs before assuming the server hung)',
          'TIMEOUT',
          { cause: error },
        )
      }
      if (options.signal?.aborted) {
        throw new LlmError('oMLX request aborted by caller', 'ABORTED', { cause: error })
      }
      if (error instanceof LlmError) throw error
      throw new LlmError(`oMLX request to ${connection.baseURL} failed`, 'TRANSPORT', { cause: error })
    } finally {
      consumer.abort('oMLX stream consumer stopped')
      if (!exhausted && iterator.return !== undefined) {
        try {
          await iterator.return()
        } catch {
          // The consumer controller already owns termination.
        }
      }
    }
  }

  private async* request(
    options: GenerateOptions,
    signal: AbortSignal,
    connection: OmlxConnectionOptions,
  ): AsyncIterable<StreamChunk> {
    const headers: Record<string, string> = {
      'content-type': 'application/json',
      'accept': 'text/event-stream',
      ...attributionHeaders(),
      ...connection.apiKey ? { authorization: `Bearer ${connection.apiKey}` } : {},
    }

    const body = serializeRequest({
      ...options,
      maxTokens: options.maxTokens ?? connection.defaultMaxTokens,
    })

    let response: Response
    try {
      response = await fetch(`${connection.baseURL}/chat/completions`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal,
      })
    } catch (error: unknown) {
      if (signal.aborted) throw error
      throw new LlmError(`oMLX request to ${connection.baseURL} failed`, 'TRANSPORT', { cause: error })
    }

    if (!response.ok) {
      let message = `oMLX server error (HTTP ${response.status})`
      const rawResponse = await response.text()
      try {
        const parsed = JSON.parse(rawResponse) as WireError
        if (parsed.error?.message) message = parsed.error.message
      } catch {
        // The HTTP status remains authoritative when the response isn't JSON.
      }
      throw new LlmError(message, httpErrorCode(response.status), {
        cause: new Error(rawResponse.length > 0 ? rawResponse : `oMLX HTTP ${response.status}`),
        status: response.status,
      })
    }
    if (response.body === null) {
      throw new LlmError('oMLX response carried no body', 'TRANSPORT')
    }

    yield* translate(parseSse(response.body))
  }
}
