/**
 * OpenAI-compatible chat-completions wire vocabulary as served by oMLX.
 * Mirrors the shapes dsh-llm-deepseek uses for the same DeepSeek wire
 * protocol, trimmed to text-only, tool-calling, streaming chat completions.
 */

export interface WireTextMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  tool_call_id?: string
  tool_calls?: Array<{
    id: string
    type: 'function'
    function: { name: string, arguments: string }
  }>
  reasoning_content?: string
}

export interface WireTool {
  type: 'function'
  function: {
    name: string
    description: string
    parameters: Record<string, unknown>
  }
}

export interface WireRequest {
  model: string
  messages: WireTextMessage[]
  stream: true
  stream_options: { include_usage: true }
  tools?: WireTool[]
  temperature?: number
  max_tokens?: number
  stop?: string[]
}

export interface WireToolCallDelta {
  index: number
  id?: string
  function?: { name?: string, arguments?: string }
}

export interface WireDelta {
  content?: string | null
  reasoning_content?: string | null
  tool_calls?: WireToolCallDelta[]
}

export interface WireUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens?: number
  prompt_tokens_details?: { cached_tokens?: number }
  completion_tokens_details?: { reasoning_tokens?: number }
}

export interface WireChunk {
  choices?: Array<{ delta?: WireDelta, finish_reason?: string | null }>
  usage?: WireUsage
}

export interface WireError {
  error?: { message?: string, code?: string, type?: string }
}

export interface WireModel {
  id: string
  context_length?: number
}

export interface WireModelsResponse {
  data?: WireModel[]
}
