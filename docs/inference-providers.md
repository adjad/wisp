# Inference providers

Wisp can bind generation roles to managed local oMLX, another local inference app
(Ollama, LM Studio, llama.cpp, MTPLX or any OpenAI-compatible server on this Mac), a
remote oMLX server, OpenRouter, or another authenticated OpenAI-compatible chat API. Tool selection,
confirmation, and execution remain Wisp's existing routing policy. Choosing a
cloud endpoint sends that role's prompts, conversation context, and any supplied
tool schemas to the provider; generation may incur provider charges.

This configuration-only candidate does not add a settings screen or modify the
installed app. Edit the `inference` section of `~/.moe/config.yaml`, preserving
other settings, and restart the service after changing it. Never put a key,
authorization header, password, or token in that file. Only credential references
belong there.

| Provider | Default API prefix | Readiness | Model management |
| --- | --- | --- | --- |
| `omlx` (default) | `/v1` | `/health`, then `/v1/models` | Managed local endpoint only |
| `openai-compatible` | `/v1` | Model inventory | None |
| `openrouter` | `/api/v1` | Model inventory | None |

`base_url` is an origin without a path, query, fragment, or user information.
Named remote endpoints require HTTPS. Generic chat providers can set `api_prefix`
to an absolute path consisting of letters, digits, underscores, hyphens, and
slash-separated segments, such as `/gateway/v1`. Unknown providers and malformed
configuration fail closed. Provider profiles are an immutable registry in
`service/inference/providers.py`; adding one does not require routing changes.

## OpenRouter example

```yaml
inference:
  endpoints:
    cloud:
      provider: openrouter
      base_url: https://openrouter.ai
      credential_ref: keychain:openrouter
      readiness_timeout: 10
  bindings:
    coding:
      endpoint: cloud
      model_id: vendor/model-slug
      context_window: 8192
      qualified_capabilities: []
```

Replace `vendor/model-slug` with an available model's exact identifier and set a
verified context limit. Wisp checks the inventory before generation; inventory
presence is not a guarantee of quota, model availability, or tool compatibility.
The paths follow the [OpenRouter quickstart](https://openrouter.ai/docs/quickstart).
This implementation was tested with synthetic responses, not a live cloud key.

Create a generic password item in **Keychain Access** with item name/service
`com.wisp.inference`. Its account is the reference name followed by a colon and
the SHA-256 of the normalized `base_url` (no trailing slash). To print this
non-secret account identifier for the example:

```bash
python3 -c 'import hashlib; print("openrouter:" + hashlib.sha256(b"https://openrouter.ai").hexdigest())'
```

Enter the API key only in the item's password field using Keychain Access.
Do not pass it to shell commands, paste it into logs, or save it in fixtures.
Missing, denied, empty, invalid, or timed-out Keychain access stops requests
before networking. Lookup runs off the event loop so cancellation remains
responsive. An endpoint origin change requires a separately
provisioned Keychain account. The reader uses the system `security` utility;
its arguments contain only the service and account identifier. It does not
modify the Keychain or existing native credential bridge.

Existing `env:NAME` references remain compatible for previously configured
endpoints. Prefer `keychain:NAME` for new provider credentials because environment
variables can be inherited by child processes. Names accept a leading letter
followed by letters, digits, underscores, or hyphens, up to 64 characters.

## Other local apps, including tools

Settings > Models > Local (or Set Up Inference > Other local apps) connects one
app on a numeric `127.0.0.1` port other than 8000 and 8765. Choose what it is for:

| Workload | What it does | Needs |
| --- | --- | --- |
| Reasoning | Answers without tools | A working streamed reply |
| Agent | Runs Wisp's tool loop (calendar, mail, messages, files, web, ...) | A passing tool-calling test |
| Coding | Code questions, with tools when needed | A passing tool-calling test |

`fast`, `router`, `embedding` and `reranker` stay on managed oMLX.

**Tool use is earned, not declared.** Choosing Agent or Coding runs the server's
qualification (`service/inference/qualify.py`, also `POST
/inference/local-provider/qualify` for a dry run). It uses only synthetic prompts:

1. **Real context window.** Wisp calibrates probe size using reported `prompt_tokens`,
   then sends an oversized prompt and records only the reported token count, capped by
   your claim and 16,384, rounded down to a multiple of 256. If the app refuses the
   prompt, Wisp tries smaller prompts and records what the app reports accepting.
   Some apps silently drop
   the start of an over-long prompt, which is where Wisp's system prompt and tool
   schemas live; the model then says things like "I don't have access to your calendar".
   Ollama, for example, loads a model with a small default window. The measured window
   must be at least 8,192 tokens (16,384 recommended) and is what Wisp saves, never more
   than you typed. Missing or unusable token usage leaves the window unknown and
   Agent/Coding unqualified; recalling text or estimating characters cannot verify a
   token minimum for an unknown tokenizer. Reasoning-only connections remain available.
2. **Tool calling**, through the same client Wisp uses: a structured call with typed
   arguments, a follow-up turn that uses the tool result, a streamed call with
   fragmented arguments, and a no-tool control (advisory only). A model that writes the
   call as text fails with the fix (for llama.cpp, start with `--jinja`).

On failure Settings shows the failing check and the engine-specific fix, for example
`OLLAMA_CONTEXT_LENGTH=16384 ollama serve` for Ollama, or Context Length for LM Studio.

The server, not the app UI, decides: a client cannot assert a qualification. A recorded
qualification counts only for the exact app URL, API prefix and model it was run
against. Changing any of them (or hand-editing `qualified_capabilities`) returns the
role to no-tools until it is tested again. Older qualification records that did not
record the tested API prefix require a fresh test; Wisp does not assume `/v1` for
missing evidence. Disconnecting clears the saved record.

**What is sent.** With Agent or Coding selected, tool results, meaning your calendar, mail,
messages, notes and file contents, are sent to the app to answer you. A loopback app without
an API key is not identity-verified, so connect only one you trust. Skills and active
workflows stay on managed oMLX.

## Local models and role behavior

With no new configuration, local oMLX and existing remote oMLX generation
bindings retain their defaults. The reserved `local` endpoint remains the managed, attributed
loopback oMLX process using its existing credential bridge. This candidate does
not authorize arbitrary local processes to receive that credential. A separately
managed open-model server can use the `openai-compatible` profile behind an
authenticated HTTPS endpoint with its own credential.

Set `inference.bindings.<role>` independently for supported generation roles,
including `agent` or `coding`; configuring an endpoint alone sends nothing to it.
The `fast` and `router` roles must remain local. Every unmanaged endpoint,
including remote oMLX, is rejected for `embedding` and `reranker`; those operation
adapters do not yet use the protected transport. Managed local oMLX
embedding/reranker behavior remains unchanged.

`revision`, `profile`, `context_window`, and `qualified_capabilities` stay attached
to each role's model. Tool calling requires `tools` in `qualified_capabilities`
for the exact remote target after independent compatibility validation. Leave
that list empty until then; Wisp refuses tool-bearing requests to an unqualified
target. Provider selection does not automatically claim tool support.

Any unmanaged endpoint, including remote oMLX, that returns `reasoning_details`
alongside tool calls is rejected before a completed tool result is delivered.
Some models require those details on subsequent turns, but Wisp's existing agent
history does not replay them. Qualify a model without that requirement for tool
use. Plain OpenAI-compatible `reasoning` is normalized for display; valid
truncated remote answers are retained.

Every unmanaged endpoint also uses the same response boundary: redirect targets,
headers, bodies, request prompts, and HTTP-200 provider error objects are removed
before an error reaches logs, the UI, or a debug export. Only a safe HTTP status
and endpoint label remain. This rule follows endpoint ownership, not the selected
protocol profile, so a remote oMLX server receives the same treatment as a cloud
provider. Managed loopback oMLX keeps its local diagnostic behavior.

Remote completion bodies and streams have explicit byte budgets derived from the
requested output-token limit (output bytes per token, and a larger per-token allowance
for the ~240-byte SSE event each streamed token travels in), plus hard ceilings. Wisp rejects compressed remote
responses, oversized declared or incremental bodies, unbounded SSE streams, and
oversized cumulative content, reasoning, structured reasoning metadata, or tool
arguments with sanitized errors.
Managed loopback oMLX keeps its existing local transport behavior.

Streaming and cancellation share the existing inference client implementation.
Cloud endpoints never receive load/unload requests. Existing fallback remains
limited to tool-free readiness failure before generation starts; interrupted
generation is never replayed locally. No new fallback or automatic provider
switching is introduced. To restore a role, set its binding to `endpoint: local`
and its installed local `model_id`, removing remote metadata, then restart.

## Validation

Run from the repository with its development Python environment:

```bash
python -m pytest -q tests/test_inference_providers.py tests/test_inference_endpoints.py tests/test_lazy_inference_readiness.py tests/test_primary_credentials.py tests/test_credential_quarantine.py
```

The provider tests mock all HTTP and Keychain operations. They cover API paths,
bounded readiness, tool fragments, incomplete streams, cancellation cleanup,
credential and provider-response redaction, invalid configuration, remote oMLX
trust boundaries, and restricted roles. An independent
Release Auditor and applicable security/native-integration QA remain required
before shipping. Live cloud calls require separate authorization.
