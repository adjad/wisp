# Ling direct MLX prototype

This is a one-model, one-process inference prototype for the local
`Ling-3.0-tiny-oQ4e` checkpoint. It calls bundled `mlx-lm` directly. It uses
the installed oMLX bundle's Ling architecture adapter and M5 gather workaround
when loading, because those are part of the checkpoint's correct execution on
this machine. It does not contact the oMLX server, change Wisp settings, or
modify the checkpoint.

Run metadata inspection without loading weights:

```sh
tools/ling_engine/run.sh inspect
```

When exclusive GPU access is available, load the model and generate:

```sh
tools/ling_engine/run.sh generate --prompt 'Say hello.' --max-tokens 64
```

For the loopback API, run `tools/ling_engine/run.sh serve`. It listens on
`127.0.0.1:8767` by default. The OpenAI-style base URL is
`http://127.0.0.1:8767/v1`; the sole model ID is `Ling-3.0-tiny-oQ4e`.
`GET /health` reports readiness and capabilities, `GET /v1/models` lists the
model, and `POST /v1/chat/completions` supports nonstream JSON or genuine SSE
with incremental text, a terminal finish chunk, optional usage, and `[DONE]`.
Use `stream_options: {"include_usage": true}` for the usage chunk. Requests
are bounded to 1 MiB, 64 text messages, 8,192 prompt tokens, and 2,048 output
tokens. Only a numeric loopback bind is allowed. Set `LING_ENGINE_API_TOKEN`
in the server environment to require `Authorization: Bearer ...`; when unset,
the local endpoint has no token authentication. No wildcard CORS is enabled.
Tool definitions and tool-call history are rejected; tool calls are not
advertised or parsed. The Python API can render native tool prompts for
comparison, but the HTTP API does not claim tool support.

The Python API is `Engine(model_path, optimized=False)`, with
`render_prompt(messages, tools=None, thinking=False)`, `tokenize(...)`, and
`generate_tokens(prompt_ids, max_tokens=128, temperature=0, seed=0,
use_cache=False)`. `.model` and `.tokenizer` remain accessible for comparison.
Both `optimized` values use stock mlx-lm prefill chunks of 2,048 tokens and
report `implementation: stock`. The experimental 4,096-token setting was
excluded after an independent exact-output mismatch. Prefix cache reuse is
disabled because Ling's KDA layers contain recurrent state that cannot be
sliced like a KV cache.

`generate_tokens` returns visible token IDs and decodes them into `text`.
`stop_token_id` exposes a terminal EOS separately when present;
`sampled_tokens` includes that EOS, while `generation_tokens` counts visible
tokens and `cached_tokens` is always zero. `prefill_seconds` measures time
through the first sampled token, including its decode; `decode_seconds` is the
remaining time. These are wall-clock timings, not isolated GPU kernel times.
The result also reports total time and MLX peak process memory. Requests are
serialized within the engine; prompt length is capped at 8,192 and generation
at 2,048 tokens. Hardware use and comparative speed require separate runs and
measurement. No Apple Neural Engine path is implemented or claimed.
`stream_text` calls a supplied callback with valid Unicode segments as tokens
arrive. A client disconnect stops the request and releases the model lock.

Runtime is supplied by `/Applications/oMLX.app`; no dependency installation
or model download occurs. The launcher mirrors the app's bundled Python
bootstrap. An app update can change the adapter or `mlx-lm` API and should
trigger fresh parity tests before relying on the prototype.
