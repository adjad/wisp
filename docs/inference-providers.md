# Inference providers

Wisp can bind generation roles to managed local oMLX, a remote oMLX server,
OpenRouter, or another authenticated OpenAI-compatible chat API. Tool selection,
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
requested output-token limit, plus hard ceilings. Wisp rejects compressed remote
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
