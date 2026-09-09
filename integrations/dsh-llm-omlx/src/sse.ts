/**
 * Decode an SSE byte stream into event `data` payloads. Same framing contract
 * dsh-llm-deepseek relies on: `eventsource-parser` owns chunk reassembly and
 * UTF-8/CRLF/BOM handling; this module only keeps the OpenAI-style protocol
 * detail — the literal `[DONE]` sentinel is yielded so the caller owns final
 * flushing, and EOF before it is truncation, reported as a thrown LlmError.
 */

import { EventSourceParserStream } from 'eventsource-parser/stream'
import { LlmError } from '@deepseek-ai/dsh-llm'

export const DONE = '[DONE]'

export async function* parseSse(
  stream: ReadableStream<BufferSource>,
): AsyncGenerator<string> {
  const events = stream
    .pipeThrough(new TextDecoderStream())
    .pipeThrough(new EventSourceParserStream())
  for await (const { data } of events) {
    yield data
    if (data === DONE) return
  }
  throw new LlmError('SSE stream ended without [DONE]', 'STREAM_CLOSED')
}
