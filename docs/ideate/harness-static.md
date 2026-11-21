# Ideate harness comparison: Pi 1.6.0 vs OpenCode 2.3.2 (static)

Date 2026-11-21. Read-only study; no model call was made and nothing was sent to oMLX on 127.0.0.1:8000.
All scratch files lived under `/private/tmp/claude-501/hs` (the sandbox `$TMPDIR`). Nothing in the user's own
`~/.pi`, `~/.config/opencode` or `~/.local/share/opencode` was read for credentials or modified.

Two kinds of evidence are marked in this document:
- **[verified]** I ran it on this Mac (idle processes, config only, no model).
- **[docs]** read from the install's own docs or from strings in the shipped binary; not exercised.

## 0. At a glance

| | Pi 1.6.0 | OpenCode 2.3.2 |
|---|---|---|
| Runtime | Node >= 22.19 + JS packages (no single binary) | Bun-compiled single Mach-O, `Contents/Resources/opencode-cli` |
| Architectures here | JS is portable; Node is universal; bundled `rg`/`fd` arm64 only | arm64 only (`lipo -archs` = arm64) |
| Host protocol | stdio JSONL (`--mode rpc`), no port | HTTP + SSE (`opencode serve`), loopback port + password |
| Approval hook | extension `tool_call` event, `ctx.ui.confirm` becomes a client request [verified round trip] | built-in `permission.asked` SSE event + `reply` endpoint [verified round trip] |
| Idle ready time | 0.14 s to first RPC response [verified] | 0.15 s to `/api/info` 200 [verified] |
| Idle RSS | 126 MB peak [verified] | 161 MB [verified] |
| Install to bundle | ~160 MB deps (prunable) + Node (~120 MB arm64 slice) | 215 MB binary |
| Phone-home at startup | none seen in RPC mode; off switches exist | models.opencode.ai fetch + built-in cloud "opencode" provider unless disabled [verified] |
| Session store | JSONL files under `PI_CODING_AGENT_DIR/sessions` or `--session-dir` | SQLite `opencode.db` under `XDG_DATA_HOME/opencode` |
| License | MIT (field in package.json; no LICENSE file in the install) | not shipped in the bundle; upstream MIT per brief |

## 1. Headless and programmatic control

### Pi

Modes (from `docs/cli.md`, `docs/rpc.md`, `docs/json.md`, `pi --help`):
- `pi -p "prompt"` prints the final assistant text and exits.
- `pi --mode json "prompt"` writes one session header then JSONL events, then exits.
- `pi --mode rpc` is a long-lived bidirectional JSONL protocol on stdin/stdout. stderr is diagnostics only.

Exact flags that matter: `--mode rpc`, `--no-session` or `--session-dir <dir>`, `--session-id <id>`, `--continue`,
`--session <path|id>`, `--fork <path|id>`, `--provider/--model`, `--tools a,b,c`, `-e <ext.ts>`, `-ne`, `--no-mcp`,
`--no-skills`, `--no-prompt-templates`, `--no-themes`, `--no-context-files`, `--offline`, `--approve/--no-approve`.

Framing: strict LF-terminated JSON, split only on `\n` (never use a reader that splits on U+2028).

Commands the host sends (all take an optional string `id`; responses repeat it):
```json
{"id":"1","type":"prompt","message":"Build a todo page"}
{"id":"1","type":"response","command":"prompt","success":true,"data":{"disposition":"started"}}
{"type":"prompt","message":"also add dark mode","streamingBehavior":"steer"}   // or "followUp", while a run is active
{"type":"steer","message":"..."}   {"type":"follow_up","message":"..."}
{"type":"abort"}                    // cancel; replies after the session is idle
{"type":"get_state"}  {"type":"get_messages"}  {"type":"get_session_stats"}
{"type":"new_session"}  {"type":"switch_session","sessionPath":"/abs/x.jsonl"}  {"type":"fork",...}
{"type":"bash","command":"ls"}      // user-run shell command (NOT routed through the tool_call hook)
```
Orderly shutdown: close stdin (or SIGTERM); a second signal kills.

Events streamed on stdout (same shapes in json and rpc modes):
```json
{"type":"agent_start"}
{"type":"turn_start"}
{"type":"message_start","message":{"role":"assistant","content":[],"stopReason":"pending"}}
{"type":"message_update","usage":{...},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"Hello "}}
   // other assistantMessageEvent types: thinking_delta, toolcall_start/delta/end, text_end, thinking_end
{"type":"tool_execution_start","toolCallId":"call_abc","toolName":"bash","args":{"command":"ls -la"}}
{"type":"tool_execution_update","toolCallId":"call_abc","toolName":"bash","args":{...},"partialResult":{...}}
{"type":"tool_execution_end","toolCallId":"call_abc","toolName":"bash","result":{"content":[...],"details":{}},"isError":false,"durationMs":12}
{"type":"turn_end","message":{...},"toolResults":[...]}
{"type":"agent_end","messages":[...],"willRetry":false}
{"type":"agent_settled","aborted":false}      // the only reliable "this run is finished" marker
```
A host should subscribe before sending the prompt and treat `agent_settled` as done (retries, compaction and queued
steering can follow `agent_end`). `message_end.message` is the authoritative final message; deltas are best-effort.
`tool_execution_end` fires for every finalized call, including blocked ones (with an error result).

Resume: `--continue`, `--session <id|path>`, or RPC `switch_session`. Verified `get_state` returns `sessionId`,
`isStreaming`, `messageCount`, model, `thinkingLevel` [verified].

### OpenCode

`opencode-cli --help` [verified]: subcommands `run`, `serve`, `acp`, `api`, `session`, `service`, `mini`, `models`, `mcp`, `plugin`, `auth`, ...
- `opencode run [message] --format json [--session <id>] [--continue] [--model provider/model#variant] [--agent x] [--auto]`:
  one-shot. Without `--auto` a non-interactive run cannot ask, so permission requests are rejected (string in binary:
  "This non-interactive run cannot ask the user for permission, so the request was rejected").
  `run` and the TUI attach to a shared "background service" unless `--standalone` or `--server <url>` is given. [docs/--help]
- `opencode acp`: Agent Client Protocol server on stdio (not exercised).
- `opencode serve --hostname 127.0.0.1 --port N [--stdio]`: the v2 HTTP API + SSE. `--stdio` was only tried inside the
  Claude sandbox (which forbids listening) and failed with "Failed to start server. Is port 65535 in use?", so it is
  unproven (it appears to still open a listener on port 65535); the HTTP path below was run unsandboxed on loopback and is [verified].

HTTP API, from `GET /openapi.json` (458 KB, 532 schemas) [verified]. Auth is HTTP Basic, user `opencode`, password
from env `OPENCODE_SERVER_PASSWORD` [verified: 200 with it].
Relevant routes:

| Purpose | Route |
|---|---|
| Server info | `GET /api/info` -> `{"version":"2.3.2","pid":...,"urls":[...]}` |
| Create session | `POST /api/session` body `{"agent":"build","model":{"providerID","id"},"location":{"directory":"/abs/proj"},"permissions":[{"action","resource","effect":"allow\|deny\|ask"}],"metadata":{}}` -> `{"data":{"id":"ses_..."}}` |
| Send prompt | `POST /api/session/{id}/prompt` body `{"text":"...","delivery":"steer\|queue","resume":bool,"files":[...]}` |
| Stream events | `GET /api/event` (SSE, optional `?types=`), heartbeat comments every few seconds |
| Cancel | `POST /api/session/{id}/interrupt` -> `{"interrupted":true}` |
| Resume | sessions persist; `GET /api/session`, `GET /api/session/{id}/message`, then prompt again |
| Approve | `POST /api/session/{id}/permission/{requestID}/reply` body `{"decision":"once\|always\|reject","message":"..."}` -> 204 |
| Pending approvals | `GET /api/permission/request`, `GET /api/session/{id}/permission` |
| Diff / revert | `GET /api/session/{id}/diff`, `POST .../revert/stage|commit`, `POST /api/session/{id}/fork` |

SSE events seen on a live server [verified]: `server.connected`, `*.updated` (provider, model, agent, command, skill...),
`permission.asked`, `permission.replied`, `shell.created`, `session.shell.started`, `shell.exited`. Event types declared
in the OpenAPI schema [docs]: `session.execution.started|succeeded|failed|interrupted`, `session.idle`, `session.status`,
`session.step.started|ended|failed`, `session.text.started|delta|ended`, `session.reasoning.delta`,
`session.tool.input.started|delta|ended`, `session.tool.called` (`data: {sessionID, assistantMessageID, id, input, executed}`),
`session.tool.progress`, `session.tool.success|failed` (`data: {id, content[], metadata, executed}`),
`session.usage.updated`, `session.compaction.*`, `session.permissions`, `form.created|replied`.
Sample (verified):
```
data: {"id":"evt_...","created":1795250467238,"type":"permission.asked","location":{"directory":"/tmp/.../proj2"},
       "data":{"id":"per_...","sessionID":"ses_...","action":"bash","resources":["rm -rf /tmp/x"]}}
data: {"id":"evt_...","type":"permission.replied","data":{"sessionID":"ses_...","requestID":"per_...","reply":"reject"}}
```
`location` (the project directory) is carried per session and per request (`?location=` query on read routes).

## 2. Tools and the approval hook

### Pi

Built-in tools [docs, `pi --help`]: `read`, `bash`, `edit`, `write` (default on); `grep`, `find`, `ls` (off by default,
read-only); `powershell` on Windows; optional `codemode`, `tool_search`, MCP tools.
- Restrict with `--tools read,grep,find,ls,bash,edit,write` (replaces the whole set), `-xt/--exclude-tools`, `--no-builtin-tools`,
  `--no-tools`. Read-only example in the docs: `pi --tools read,grep,find,ls -p ...`.
  MCP tools are kept unless an entry starts with `mcp__`; use `--no-mcp` to drop them.
- Edits: `edit` is exact text replacement, schema `edits: [{oldText, newText}]` (older sessions had top-level oldText/newText);
  `write` creates or overwrites a whole file; `read` takes `{path, offset?, limit?}`; `bash` takes `{command, timeout?}`.

Approval mechanism: an extension file loaded with `-e`. The `tool_call` event fires after `tool_execution_start` and before
execution; it can mutate `event.input` or return `{block:true, reason, terminate?}`. A handler that throws blocks the tool
(fail-safe, docs line "tool_call errors block the tool"). In RPC mode `ctx.hasUI` is true and `ctx.ui.confirm/select/input`
are forwarded as `extension_ui_request` records that block until the host replies with an `extension_ui_response`
(`{"type":"extension_ui_response","id":"<req id>","confirmed":true|false}` or `value`, or `cancelled:true`).
Dialogs accept a `timeout` (ms) that auto-resolves.

Verified round trip [verified]: I loaded `/private/tmp/claude-501/hs/ext/gate.ts` via `-e`, sent `{"type":"prompt","message":"/probe"}`,
received
`{"type":"extension_ui_request","id":"786b...","method":"confirm","title":"Allow bash?","message":"rm -rf build"}`, replied
`confirmed:false`, and received `{"method":"notify","message":"denied"}` in 0.22 s. The `tool_call` handler in the same file is
the documented API (examples/extensions/permission-gate.ts is the reference) but was not triggered because that needs a model turn.

Reference extension (a bridge would ship something like this; 15 lines):
```ts
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
    if (["bash", "write", "edit"].includes(event.toolName)) {
      const ok = await ctx.ui.confirm(`Allow ${event.toolName}?`, JSON.stringify(event.input));
      if (!ok) return { block: true, reason: "Denied by the user" };
    }
  });
}
```
Gaps: `read` of paths outside the project is not intercepted unless the handler checks `event.input.path`; the RPC `bash`
command (user shell) is not hooked, so the bridge should never send it. Parallel sibling tool calls are pre-flighted
sequentially, so approvals queue one at a time.

### OpenCode

Permission actions seen in the shipped agent rulesets and binary [verified via `GET /api/agent`, strings]: `*`, `read`,
`edit`/`write`/`patch`, `shell` (bash), `external_directory`, `question`, `subagent`, `browser`, `skill`, `grep`/`glob`,
`webfetch`, `websearch`. Effects: `allow | deny | ask`; later rules appear to override earlier ones.
The default `build` agent is `allow *` with `ask` for `.env` reads, `deny` for `browser` [verified from `/api/agent`].
A global rule `{"action":"*","resource":"*","effect":"ask"}` in `opencode.json` `permissions` was merged into the build
agent's list after the default allow [verified], so every tool call becomes an ask.

The host mechanism: subscribe to `GET /api/event`, react to `permission.asked` (`data.id`, `sessionID`, `action`,
`resources[]`, optional `save[]` patterns, `source: {type:"tool", messageID, id}`), then
`POST /api/session/{sessionID}/permission/{id}/reply {"decision":"once"|"always"|"reject","message":"..."}`.
[verified]: with `agent:"build"` and a session rule `bash * ask`, `POST /api/session/{id}/permission` (the manual "create"
route) left a pending request visible in `GET /api/session/{id}/permission`, emitted `permission.asked`, and the reply route
emitted `permission.replied`. Caveat: the shell route `POST /api/session/{id}/shell` (human `!` command) ran `echo hi` with no
ask, so the bridge should not use it. Real model-issued tool calls use the same ask path per the docs but I did not
trigger one.

Cheaper but cruder: `opencode run --auto` approves everything not explicitly denied; never use it.
Additional gating: `policies` (`tool.use`, `provider.use`, `mcp.use`, `plugin.use`, `websearch.use`, effect allow/deny) in config.

## 3. Sandboxing

Neither harness confines files or the shell itself.
- Pi `docs/security.md`: "Pi can read, change, and execute files with the permissions of the account that started it";
  "lack of a built-in sandbox" is explicitly out of scope; project trust only controls which `.pi/` resources load.
- OpenCode has an `external_directory` permission action (a prompt when a tool touches paths outside the project) but this is
  a policy check, not an OS boundary, and shell commands can reach anywhere the process can.

What the bridge must add: run the harness process inside macOS `sandbox-exec` with a Seatbelt profile, cwd = project folder.
Verified on this Mac with `/usr/bin/sandbox-exec -f proj.sb` [verified for both]:
```scheme
(version 1)
(deny default)
(allow process-fork) (allow process-exec*) (allow sysctl-read) (allow mach-lookup) (allow ipc-posix-shm*)
(allow signal (target self))
(allow file-read*)
(deny  file-read* (subpath "<HOME>/.ssh") (subpath "<HOME>/Library/Keychains") (subpath "<HOME>/.aws")
                  (subpath "<HOME>/.pi/agent/auth.json"))
(allow file-write* (subpath "<PROJECT>") (subpath "<HARNESS_HOME>") (subpath "/private/var/folders")
                   (literal "/dev/null") (literal "/dev/dtracehelper") (regex #"^/dev/tty"))
(allow network-outbound (remote ip "localhost:8000"))        ; oMLX only
(allow network-outbound (literal "/private/var/run/mDNSResponder"))
(allow network-bind (local ip "localhost:*")) (allow network-inbound (local ip "localhost:*"))   ; OpenCode serve only
(allow network-outbound (remote ip "localhost:*"))                                               ; OpenCode serve only
```
Notes: Seatbelt matches resolved paths, so use `/private/tmp/...` not `/tmp/...` (my first run failed on the symlink).
Results [verified]:
- Pi under the profile: `get_state` OK; the RPC `bash` command could write `<PROJECT>/in.txt` but
  `echo > <outside>/out.txt` gave "Operation not permitted"; `curl http://example.com` exit 7 (no network);
  `cat ~/.pi/agent/auth.json` "Operation not permitted".
- OpenCode `serve` under the profile (plus write access to its own XDG dirs): came up, created a session, and a shell command
  wrote `in2.txt` inside the project while the write outside was blocked (no `out.txt` created).
Remaining design work for the bridge: reads are broad in my profile (`file-read*` allowed with a deny list); a stricter profile
would allow reads only for the project, the runtime and system libraries. Network outbound would be restricted to the oMLX port
only (drop the OpenCode-only localhost lines for Pi). Children (npm, python http.server) inherit the profile, so a dev server the
user wants to preview needs `network-bind` for loopback; decide per project.

Environment and directories to redirect (so nothing touches the user's own config):

| | Pi | OpenCode |
|---|---|---|
| Config dir | `PI_CODING_AGENT_DIR` (default `~/.pi/agent`): `auth.json`, `models.json`, `models-store.json`, `settings.json`, `trust.json`(+`.lock`), `sessions/` | `XDG_CONFIG_HOME/opencode/opencode.json` (also project `opencode.json` / `.opencode/` up the directory tree; disable with `OPENCODE_DISABLE_PROJECT_CONFIG=1` / `OPENCODE_CONFIG_PROJECT_DISABLE`); `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `OPENCODE_CONFIG_CONTENT` also exist |
| Data / sessions | `PI_CODING_AGENT_SESSION_DIR` or `--session-dir` | `XDG_DATA_HOME/opencode/` (`opencode.db`, `log/opencode.log`, `shell/<hash>/sh_*.out`); `OPENCODE_DB` renames the db or `:memory:` |
| Cache/state | none observed | `XDG_CACHE_HOME/opencode/bin/opencode-pty/<ver>/` (extracted helper), `XDG_STATE_HOME/opencode/pty/<id>/service.json,lock`, `$TMPDIR/opencode` |
| HOME | node reads `$HOME` for the launcher only if `PI_CODING_AGENT_DIR` unset | set `HOME` as well; its default config also reads `~/.config/opencode` unless `XDG_CONFIG_HOME` is set |
| Project-local | `.pi/` dirs only load if project-trusted; `--no-approve`/`--approve` give an explicit decision; project instruction files load regardless unless `--no-context-files` | `opencode.json(c)` and `.opencode/` in every ancestor directory are merged, including above the repo root; an env switch stops importing other agents' instruction files |

Caution: the OpenCode desktop app in `/Applications` keeps a background service whose registration lives in the user's
`~/.config/opencode/service.json` (a server password sits there). A bridge must never use `run`/TUI without `--standalone`
or `serve` on its own port and XDG dirs, or it could attach to the user's shared service and database.

## 4. Custom OpenAI-compatible provider (oMLX)

Both were checked against a throwaway config; the real endpoint was never contacted.

### Pi [verified: `pi --list-models` showed the model; `get_state` showed `baseUrl`]
`$PI_CODING_AGENT_DIR/models.json`:
```json
{
  "providers": {
    "omlx": {
      "baseUrl": "http://127.0.0.1:8000/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "compat": {"supportsDeveloperRole": false, "supportsReasoningEffort": false,
                 "supportsUsageInStreaming": true, "maxTokensField": "max_tokens"},
      "models": [{"id": "Inkling-2-Mini-oQ3e-mtp", "name": "Inkling 2 Mini",
                  "contextWindow": 32768, "maxTokens": 4096}]
    }
  }
}
```
`settings.json`: `{"defaultProvider":"omlx","defaultModel":"Inkling-2-Mini-oQ3e-mtp","enableInstallTelemetry":false}`.
Select on the command line with `--model omlx/Inkling-2-Mini-oQ3e-mtp`. `apiKey` is a literal (any non-empty placeholder),
`"$ENV"`, or `"!command"`; with an unauthenticated local server the placeholder avoids a "no credentials" skip. Reasoning:
set `reasoning: true` plus `thinkingFormat` (e.g. `chat-template`/`qwen-chat-template`) only if the model needs it; `samplingParams` merges extra body fields
(for example `{"temperature":0.2}`); `compat.requiresToolResultName`, `requiresAssistantAfterToolResult` etc. cover strict servers.
Context window and max tokens are mine and must be set to the real values.

### OpenCode [verified: `GET /api/provider`, `/api/model`, `/api/config` resolved the model]
`$XDG_CONFIG_HOME/opencode/opencode.json` (v2 schema: plural `providers`, `permissions` array, `policies`):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "update": "disable",
  "share": "disabled",
  "model": "omlx/Inkling-2-Mini-oQ3e-mtp",
  "providers": {
    "omlx": {
      "name": "oMLX local",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8000/v1" },
      "models": {
        "Inkling-2-Mini-oQ3e-mtp": {
          "name": "Inkling 2 Mini",
          "capabilities": { "tools": true, "reasoning": { "supported": false } },
          "limit": { "context": 32768, "output": 4096 }
        }
      }
    }
  },
  "permissions": [ { "action": "*", "resource": "*", "effect": "ask" } ]
}
```
No API key is needed (none sent). Other provider knobs: `compatibility.maxTokensField`, `requireAssistantAfterTool`,
`streamUsage`, `settings.timeout|headerTimeout|chunkTimeout`, `headers`, `body`. Note v1-style `provider`/`permission`
configs from the public docs do not apply; the v2 schema accepts both but flags v1 fields deprecated.
With the config above and the offline flags in section 7 only `omlx` is listed (the cloud `opencode` provider disappears).
The model id is passed as the OpenAI `model` field unchanged.

## 5. Startup, memory, size, runtime

Measured on this Mac (Apple silicon), idle, with config redirected [verified]:

| | Pi | OpenCode |
|---|---|---|
| `--version` wall time | 0.16 s (`~/.local/bin/pi --version`) | 0.04 s |
| `--help` | 0.12 s | 0.04 s |
| Time to ready, headless | 0.14 s to first `get_state` reply (runs of 0.135, 0.151, 0.143, 0.141) | 0.15 s to `/api/info` 200 |
| Idle memory | 124-128 MB peak RSS (`ru_maxrss`, Python harness; `/usr/bin/time -l` was blocked by the sandbox) | 161 MB RSS (`ps`, 2 s after ready) |
| Extra processes | one node process | `serve` spawns an `opencode-pty` helper on first terminal use (extracted into the cache dir) |

Runtime and size:
- Pi launcher `~/.pi/agent/bin/pi` is a POSIX `sh` script that reads `install/current-version`, puts `~/.local/share/pi-node/current/bin`
  on PATH if present, and `exec`s `install/releases/1.6.0/node_modules/.bin/pi` (a `#!/usr/bin/env node` JS file). Here node is the
  user's `/usr/local/bin/node` v24.21.0, a universal binary (236 MB; x86_64+arm64). Harbor cannot assume a system Node, so it would
  have to ship a Node binary (arm64 slice ~120 MB, plus x86_64 for the Intel build) or rely on the Pi installer's `pi-node`.
  Required engine: `node >= 22.19.0`.
- Pi install size: `install/releases/1.6.0` is 160 MB. Biggest parts: `@earendil-works/*` 40.7 MB (coding-agent 27.5 MB of which
  docs/examples/changelog are optional), `openai` 34.9 MB, `@anthropic-ai/sdk` 14 MB, `@google/genai` 12.3 MB, AWS/smithy ~14 MB.
  Cloud SDKs are dead weight for a local-only app; a pruned bundle (dist, schemas, needed deps, no examples/docs/other-OS
  prebuilds) is plausibly 60-90 MB but I did not build one. Native: `pi-tui` ships `darwin-arm64` and `darwin-x64` `.node` prebuilds,
  `quickjs-wasi` and `photon-node` are wasm. Optional helpers `rg` 15.2.0 and `fd` 10.5.2 in `~/.pi/agent/bin` are arm64 only (about 7 MB total).
- OpenCode: `Contents/Resources/opencode-cli` is one 215 MB arm64 Mach-O (signed, hardened runtime, Bun payload under `/$bunfs/`),
  v2.3.2. The rest of the 746 MB `.app` (Electron framework 287 MB, `app.asar` 240 MB) is not needed headless. No x86_64 build
  is present, so Intel needs a separate download (not attempted; "do not download anything").

## 6. Licenses

- Pi: every `@earendil-works/*` `package.json` says `"license": "MIT"` (agent-core, ai, codemode, coding-agent, mcp, schema,
  telemetry, tui; repo `github.com/earendil-works/pi`). The install ships no top-level `LICENSE` file; I found
  `@earendil-works/pi-mcp/THIRD_PARTY_NOTICES` and `pi-schema/THIRD_PARTY_NOTICES`, whose first lines are
  `MIT License / Copyright (c) 2024 Anthropic, PBC / Permission is hereby granted, free of charge...` (an embedded dependency).
  License fields of the 102 packages in `node_modules`: MIT 56, Apache-2.0 35, BSD-3-Clause 13, ISC 7, BlueOak-1.0.0 3,
  0BSD 1, Unlicense 1; 102 `LICENSE*` files exist (e.g. `ms/license.md`, `lru-cache/LICENSE.md`). A pruned bundle must carry the
  upstream Pi MIT text (from the repo, since the install lacks it) plus these per-package files; Apache-2.0 packages need their
  NOTICE retained. `rg` (MIT/Unlicense) and `fd` (MIT/Apache-2.0) texts are needed if bundled; Node (MIT plus bundled deps) too.
- OpenCode: no LICENSE/NOTICE file under `OpenCode.app` (searched `Contents`, depth 6) and no MIT text in the binary's strings
  (only one "Apache License" occurrence). The brief says MIT; the bridge must source the upstream license text and the
  bundled-dependency notices (Bun, many npm deps, `@lydell/node-pty` LICENSE exists in `app.asar.unpacked`) from upstream, since
  the shipped binary does not carry them.

## 7. Telemetry and network at startup

### Pi
Network endpoints in the code [docs/source]: `https://pi.dev/api/report-install` (install/update telemetry, interactive mode,
suppressed by `PI_OFFLINE` or `enableInstallTelemetry:false` or `PI_TELEMETRY=0`), `https://pi.dev/api/latest-version` (suppressed
by `PI_SKIP_VERSION_CHECK=1` or offline), a model-catalog refresh from pi.dev (only when network is allowed; cached copy works
offline; `PI_OFFLINE` stops it), plus provider URLs only if you select those providers. `pi update`, `/login`, `/share`, `/bug` are explicit.
Observed [verified]: starting `--mode rpc` with default env behind a logging HTTP proxy (`HTTPS_PROXY`) produced no CONNECT
requests (caveat: only proof if Node honors the proxy variables; the docs say it does). Source shows the install telemetry
call is in the interactive TUI class, not RPC. Recommended anyway: `PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0` and `--offline`.
The built-in `llama` extension is loaded by default (`get_commands` lists `/llama`) but contacts only a configured llama.cpp router;
`-ne -e gate.ts` removes it.

### OpenCode
With defaults and a logging proxy [verified]: three `CONNECT models.opencode.ai:443` attempts at startup
(`GET https://models.opencode.ai/experimental/v2.0/api.json?type=chat,decision,image,video`, log line "Failed to fetch models.dev"),
and the built-in `opencode` cloud provider ("OpenCode Zen", `https://opencode.ai/inference/openai/v1`, free hosted models, `apiKey: "public"`)
is listed as enabled. Other possible calls from strings: auto-update (`https://opencode.ai/update/api/latest/desktop/opencode/`, `update` config),
share, websearch, integrations, MCP/OAuth, plugin update checks, cloud model catalog.
With `OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_FREE_MODELS=1 OPENCODE_DISABLE_CLAUDE_CODE=1
OPENCODE_DISABLE_FILEWATCHER=1` and config `update:"disable"`, `share:"disabled"`: zero CONNECT attempts during startup, provider
listing, model listing, session create, permission and shell round trips [verified, proxy log empty]. The `opencode` provider disappeared.
Stricter belt-and-braces: add `policies` with `{"action":"provider.use","resource":"*","effect":"deny"}` plus an allow for `omlx`,
and `websearch.use`, `mcp.use`, `plugin.use` deny; and a Seatbelt profile that only permits loopback:8000.
`OPENCODE_MODELS_URL`/`OPENCODE_MODELS_PATH` exist to point the catalog at a local file.

## 8. Session stores

- Pi: JSONL tree per session, `<session-dir>/--<cwd with / replaced by ->--/<timestamp>_<uuid>.jsonl`; `--session-dir <project>/.harbor/pi-sessions`
  (or `PI_CODING_AGENT_SESSION_DIR`) moves it into the project; `--no-session` keeps nothing. The file can contain prompts, tool
  arguments and command output. Verified the dir option is accepted [verified: `--session-dir` run]. Resume with `--continue` (per cwd),
  `--session <id|path>`, or RPC `switch_session`; `PI_SESSION_ID`/`PI_SESSION_FILE` are exported to the bash tool.
- OpenCode: one global SQLite database (`opencode.db` + `-wal`, `-shm`) in `XDG_DATA_HOME/opencode`, 62 migrations run at first start;
  sessions are keyed by project/directory inside it. Per-project isolation = a separate `XDG_DATA_HOME` per project (or
  `OPENCODE_DB=<path>`), which also duplicates the PTY helper cache unless `XDG_CACHE_HOME` is shared. Logs go to
  `$XDG_DATA_HOME/opencode/log/opencode.log`. `opencode run --archived` hides scripted sessions.

## 9. What was not verified (needs the live-test agent)

1. A real model turn: that the Inkling model emits tool calls this harness can parse (both are OpenAI `chat/completions` with
   tools), and that Pi's `tool_call` handler or OpenCode's `permission.asked` fires for model-issued `bash`/`write`/`edit`.
2. `pi --mode json` and `opencode run --format json` event bodies on real output (Pi's schema is from `docs/json.md`).
3. OpenCode behaviour when the approval client is slow (does the session time out) and Pi's `ctx.ui.confirm` timeout default.
4. x86_64: Pi should work with an x86_64 Node (pi-tui ships a `darwin-x64` prebuild, `rg`/`fd` need Intel builds); OpenCode needs an Intel binary.
5. Idle memory after a real prompt (KV is in oMLX, not the harness) and RSS growth during a long session.
6. A pruned Pi bundle and its true size.

## Recommendation for a headless notch bridge

**Pick Pi for the first integration**, with OpenCode as the fallback if the live test shows Inkling cannot drive Pi's tools.

Why Pi:
- One child process over stdio JSONL: no loopback port, no password, no SSE reconnect logic, lifetime tied to the pipe (close stdin
  or SIGTERM = clean exit). A Python service reads LF-split JSON from `proc.stdout`.
- The approval layer is a ~15-line extension we own that turns every `bash`/`write`/`edit` (and out-of-project `read`) into a
  `confirm` request the notch can render, and can also rewrite `event.input` (for example force `cwd`) or block with a reason that
  the model sees. It fails closed (a throwing handler blocks). The round trip was verified end to end.
- Everything stateful lives in two directories we control (`PI_CODING_AGENT_DIR`, `--session-dir`), nothing global, no database,
  no shared background service, no startup network in RPC mode, and the whole process ran correctly inside `sandbox-exec`.
- Lighter at idle (126 MB vs 161 MB) and no cloud provider to disable.

Why not OpenCode first: its approval flow is declarative and equally capable (`permission.asked` + `reply`), and the binary is a single
file, but it is v2.3.2 with an "experimental HttpApi" surface, a loopback listener + HTTP basic password to manage, a shared
background-service default that can attach to the user's own app, a cloud catalog and a free cloud provider that must be switched
off, a single global SQLite database, arm64-only, and no license text shipped.

Cost of Pi: it needs a Node >= 22.19 runtime (ship a private arm64 Node ~120 MB, Intel build needs x86_64 Node) and a pruned
node_modules; Pi's own license text is not in the install and must be copied from upstream.

### Command line the bridge should use

Per project `P=<project dir>`, per-project Harbor dir `H=$P/.harbor` (or an app-support dir keyed by project id):

```sh
env -i HOME="$H/home" PATH="$HARBOR_RUNTIME/bin:/usr/bin:/bin" TMPDIR="$H/tmp" \
  PI_CODING_AGENT_DIR="$H/pi-agent" PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 \
  /usr/bin/sandbox-exec -f "$H/project.sb" \
  "$HARBOR_RUNTIME/pi/node_modules/.bin/pi" --mode rpc --offline \
     --model omlx/Inkling-2-Mini-oQ3e-mtp --thinking off \
     --session-dir "$H/sessions" --name "<project name>" \
     --tools read,edit,write,bash,grep,find,ls \
     -ne -e "$HARBOR_RUNTIME/ideate-gate.ts" --no-mcp --no-skills --no-prompt-templates --no-themes \
     --no-context-files --no-approve
# cwd = $P
```
(`--no-context-files` stops a stray project instruction file steering the model; drop it if Harbor wants to honor user context files.)
For a first session omit `--session-dir` resume flags; to resume send `--continue` (same cwd) or RPC `switch_session`.

`$H/pi-agent/models.json` and `settings.json`: exactly the files in section 4 (Pi), with `defaultProvider`/`defaultModel` set and
the real context window and max tokens. `auth.json` stays absent or `{}`.

`$H/project.sb`: the section 3 profile with `<PROJECT>=$P`, `<HARNESS_HOME>=$H`, `/private/var/folders` replaced by `$H/tmp`, network
limited to `(remote ip "localhost:8000")` (oMLX), no bind rule, and a deny on reading `~/.pi`, `~/.ssh`, Keychains and other
home dotfiles; add a `network-bind` rule only for projects that start a preview server. Resolve every path with `realpath` first.

`ideate-gate.ts`: the section 2 extension, extended to (a) block `read/edit/write` whose resolved path is outside `$P`, (b) send a
`confirm` per `bash`/`write`/`edit` with a `timeout`, treating a timeout as deny, and (c) allow `read`/`grep`/`find`/`ls` inside `$P`
without a prompt.

Protocol the Python host drives:
1. Spawn the process; write `{"id":"s","type":"get_state"}` and wait for the reply (ready in ~0.15 s).
2. Send `{"id":"p1","type":"prompt","message":"..."}`; subscribe to events before sending.
3. Relay `message_update` text deltas, `tool_execution_start/end` to the notch; on `extension_ui_request` with `method:"confirm"`
   show the approval card and reply `{"type":"extension_ui_response","id":"<id>","confirmed":true|false}`.
4. Finish on `agent_settled`; cancel with `{"type":"abort"}`; mid-run guidance with `streamingBehavior:"steer"`.
5. Shutdown by closing stdin, wait a few seconds, then SIGTERM, then kill by PID.

### OpenCode fallback command line (if needed)
```sh
env -i HOME="$H/home" PATH=/usr/bin:/bin TMPDIR="$H/tmp" \
  XDG_CONFIG_HOME="$H/xdg/config" XDG_DATA_HOME="$H/xdg/data" XDG_CACHE_HOME="$H/xdg/cache" XDG_STATE_HOME="$H/xdg/state" \
  OPENCODE_SERVER_PASSWORD="$(random)" OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1 \
  OPENCODE_DISABLE_FREE_MODELS=1 OPENCODE_DISABLE_CLAUDE_CODE=1 OPENCODE_DISABLE_FILEWATCHER=1 OPENCODE_DISABLE_PROJECT_CONFIG=1 \
  /usr/bin/sandbox-exec -f "$H/project-oc.sb" "$HARBOR_RUNTIME/opencode-cli" serve --hostname 127.0.0.1 --port <free port>
```
with `$H/xdg/config/opencode/opencode.json` from section 4, then: `POST /api/session` with `location.directory=$P` and agent `build`;
open `GET /api/event`; `POST /api/session/{id}/prompt {"text":...}`; answer `permission.asked` via the reply route; `POST .../interrupt` to cancel.
