# Remote inference

Remote inference sends every model role to one OpenAI-compatible server that
you run and configure. Your data, the tools and every approval stay on the Mac.
It is for a Mac that cannot run Wisp's local models. It is off by default.
Wisp ships no server address, model or credential for it.

Policy: `service/inference/remote_policy.py`. Tests:
`tests/test_remote_inference_*.py`, which use the fictional profile in
`tests/fixtures/remote_inference.py`.

## Enable it

1. **Describe the server** in `~/.moe/config.yaml` (or `$WISP_HOME/config.yaml`).
   Every field below is required. Unknown fields are refused, including inline
   secrets such as `api_key`, `token` or `headers`.

   ```yaml
   inference:
     remote_only:
       base_url: https://inference.example   # HTTPS origin only: no path, query or trailing slash
       model_id: example-model               # sent unchanged for every role
       credential_ref: keychain:example      # the name of a Keychain item; never the token itself
       context_window: 32000                 # 2048 to 1,048,576 tokens
       max_tokens: 8192                      # output cap per request; at most context_window
       request_bytes: 524288                 # whole request body
       messages: 200                         # messages per request
       aggregate_bytes: 131072               # messages + tool schemas; at most request_bytes
       tools: 32                             # tool schemas per request (1 to 128)
       response_bytes: 4194304               # streamed response cap
       # Optional: api_prefix (default /v1), readiness_timeout (seconds, default 5, max 30),
       # enabled (default true; false refuses to start)
   ```

2. **Store the bearer token in Keychain.** Use service `com.wisp.inference` and
   account `<name>:<sha256 of base_url>`, where `<name>` is the part after
   `keychain:`. Hashing the URL binds the token to that one origin: a copied
   `credential_ref` cannot send it anywhere else.

   ```sh
   URL=https://inference.example
   ACCOUNT="example:$(printf %s "$URL" | shasum -a 256 | cut -d' ' -f1)"
   security add-generic-password -U -s com.wisp.inference -a "$ACCOUNT" -w   # prompts for the token
   ```

3. **Launch Wisp with the flag.** The app passes its own launch environment to
   the backend it starts. Quit Wisp first, then do one of the following:

   ```sh
   # Read-only mode
   WISP_REMOTE_ONLY=1 /Applications/Wisp.app/Contents/MacOS/Wisp
   # Full mode
   WISP_REMOTE_ONLY=1 WISP_REMOTE_FULL=1 /Applications/Wisp.app/Contents/MacOS/Wisp
   # Or, through Launch Services
   open -a Wisp --env WISP_REMOTE_ONLY=1 --env WISP_REMOTE_FULL=1
   ```

   For a development backend, set the same variables in the environment of
   `python -m uvicorn service.main:app`.

The configuration is read once at startup. Editing it, or changing the
environment, takes effect only after a relaunch. Settings cannot change the
remote target while Wisp runs.

## Startup rules

| Launch | Result |
| --- | --- |
| no `WISP_REMOTE_ONLY` | Normal local inference. A `remote_only` profile in config is ignored. |
| `WISP_REMOTE_ONLY=1` with a complete, valid profile | Remote inference, read-only mode. |
| `WISP_REMOTE_ONLY=1 WISP_REMOTE_FULL=1` with a valid profile | Remote inference, full mode. |
| `WISP_REMOTE_ONLY=1` with no profile, an incomplete one, or an invalid one | The backend refuses to start and names the problem. |
| `WISP_REMOTE_FULL=1` without `WISP_REMOTE_ONLY=1`, or any value other than `1` | The backend refuses to start. |

The server must answer a health check within `readiness_timeout`, or startup
fails.

## Modes

- **Read-only** (`WISP_REMOTE_ONLY=1`): the model can call only the seven
  native reads (`get_upcoming`, `get_past_events`, `search_reminders`,
  `view_emails`, `summarize_emails`, `view_messages`, `summarize_messages`)
  and, when `WISP_BEHAVIOR=1`, the four behavior tools. Every write endpoint,
  approval and other tool is refused, even under full access or a standing
  grant. The prompt is sent as typed, with no memory facts, history or skill
  context. The behavior profile card is the only personal context added. No
  assistant scheduler runs, though behavior learning keeps its own loop.
- **Full** (`+ WISP_REMOTE_FULL=1`): every built-in tool except those that
  need Wisp's local models or extension runtimes (`create_tool`, `use_skill`,
  `wisp_skills`, `wisp_mcp`, skill and MCP tools). Each call goes through the
  normal safety policy and grants, as on a Mac with local models. The
  assistant scheduler runs (reminders, the daily brief, scheduled sends,
  behavior ticks).

## Safety boundaries (both modes)

- **One pinned origin.** Every role resolves to the profile's `base_url`,
  `model_id` and credential. A request built for another target, model or
  credential is refused before it is sent.
- **Keychain-only token.** The credential is read from Keychain when a request
  is made. It is never stored in config, never put in an environment variable
  and never logged.
- **Bounded payloads.** Every request is checked against the profile before it
  is sent: only `model`, `messages`, `max_tokens`, `stream`, `temperature`,
  `tools` and `tool_choice`; the exact model id; text and tool transcripts
  only; and the message, tool, output and byte limits. History is never
  trimmed silently: an oversized turn fails instead.
- **No local models.** oMLX is never started or loaded, there is no local
  fallback, and Settings cannot add a local, provider or cloud target.
  Embeddings, rerank, vision and the cloud Super Model are unavailable.
- **Sends always confirm.** Email, messages, replies, forwards and scheduled
  sends ask every time. A standing grant or full access does not change that.

## What leaves the Mac

Everything the model needs to answer goes to your server: your prompt, the
system prompt and the tool schemas. Read-only mode also sends the results of
the reads the model calls (calendar, reminders, mail and message text). Full
mode adds memory facts, recent conversation history (bounded by
`aggregate_bytes`) and the results of any tool it runs. Choose a server you
trust with that data.

`wisp_status` ("which model are you using?") names the remote model and host.
