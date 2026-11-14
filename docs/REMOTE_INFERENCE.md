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
       base_url: https://inference.example   # HTTPS origin only: no path, query, trailing slash, space or non-ASCII character
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
   WISP_REMOTE_ONLY=1 /Applications/Wisp.app/Contents/MacOS/Wisp
   # Or, through Launch Services
   open -a Wisp --env WISP_REMOTE_ONLY=1
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
| `WISP_REMOTE_ONLY=1` with a complete, valid profile | Remote inference (read/write; see Mode). |
| `WISP_REMOTE_ONLY=1` with no profile, an incomplete one, or an invalid one | The backend refuses to start and names the problem. |
| any `WISP_REMOTE_ONLY` value other than `1` | The backend refuses to start. |
| `WISP_REMOTE_FULL` set to anything | Ignored, with one deprecation line in the log. It no longer selects a mode. |

The server must answer a health check within `readiness_timeout`, or startup
fails.

## Mode

There is one mode, read/write. The model can use every built-in tool except
those that need Wisp's local models or extension runtimes (`create_tool`,
`use_skill`, `wisp_skills`, `wisp_mcp`, skill and MCP tools). Each call goes
through the normal safety policy and grants, as on a Mac with local models:
sends always confirm and can never be granted, and writes ask once, always or
never. The assistant scheduler runs (reminders, the daily brief, scheduled
sends, behavior ticks).

To keep a remote-inference Mac read-only, use Wisp's **View Only** mode in
Settings. It denies writes here exactly as it does with local models.

## Safety boundaries

- **One pinned origin.** Every role resolves to the profile's `base_url`,
  `model_id` and credential. A request built for another target, model or
  credential is refused before it is sent.
- **Keychain-only token.** The credential is read from Keychain when a request
  is made. It is never stored in config, never put in an environment variable
  and never logged.
- **Bounded payloads.** Every request is checked against the profile before it
  is sent: only `model`, `messages`, `max_tokens`, `stream`, `temperature`,
  `tools` and `tool_choice`; the exact model id; text and tool transcripts
  only; and the message, tool, output and byte limits. Two narrow additions:
  a top-level `reasoning` option, only `{"effort": "medium"}` and only for a
  model Wisp lists for it (`REASONING_EFFORT_MODELS` in `remote_policy.py`),
  with `max_tokens` of at least 2049; and
  `reasoning_details` on an assistant message that carries tool calls, only
  as a list of objects. Those details are the provider's own reasoning from
  the reply that made the calls, sent back unchanged on the next requests of
  the same turn. They have their own cap, `response_bytes`, outside the
  transcript budget (`aggregate_bytes`); the whole request body stays under
  `request_bytes` plus that cap. Past it, the turn stops with a plain message
  instead of continuing without them. Models that need their reasoning back
  (`REPLAY_REASONING_MODELS`) are asked for whole replies, not streams, so the
  details are exactly what the provider sent. They are never shown, logged,
  exported in Debug Mode or stored in history, and they are dropped when the
  turn ends. History is never
  trimmed silently: an oversized turn fails instead.
- **Pictures and recordings (cloud-everything mode only).** With
  `remote_parity` on, a user message may be a list of content parts, each
  checked against the profile's model in `REMOTE_INPUT_MODALITIES`
  (`remote_policy.py`; a model not listed is text only): `text`; `image_url`
  with a `data:image/(png|jpeg|webp);base64,` URL only, never a link, at most
  5 MiB decoded each and 9 per request; `input_audio` in `wav` or `mp3`, base64,
  at most 10 MiB decoded and one per request. Every picture and recording in a
  request together is at most 16 MiB encoded; those bytes are left out of
  `aggregate_bytes`, and the whole body stays under `request_bytes` plus
  replayed reasoning plus that media cap. Without parity, or for a model that
  does not take the input, the request is refused. Pictures in Ask,
  look-at-screen, voice questions and Meet use this; their bytes are never
  logged or exported in Debug Mode.
- **No local models.** oMLX is never started or loaded, there is no local
  fallback, and Settings cannot add a local, provider or cloud target.
  Embeddings, rerank, vision and the cloud Super Model are unavailable.
- **Sends always confirm.** Email, messages, replies, forwards and scheduled
  sends ask every time. A standing grant or full access does not change that.

## What leaves the Mac

Everything the model needs to answer goes to your server: your prompt, the
system prompt and the tool schemas. It also gets the results of any tool the
model runs (calendar, reminders, mail and message text), plus memory facts and
recent conversation history (bounded by `aggregate_bytes`). Choose a server
you trust with that data.

Behavior learning ("Learn my routines", on by default) adds two things. A short
summary of what Wisp learned about your routines is part of the system prompt,
so it goes to your server with every prompt. Nudge wording and event
categorization would send calendar titles and nudge facts to your server, so
they are **off by default in remote mode**; set `WISP_BEHAVIOR_PHRASER=1` at
launch to turn them on. Turn "Learn my routines" off, or use **Forget what Wisp
learned** in Settings, to stop or erase the learning itself.

`wisp_status` ("which model are you using?") names the remote model and host.
