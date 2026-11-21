# Ideate's service bridge

Ideate is the notch tab where a small coding agent builds a project in its own folder. The tab
(`app/Sources/WispApp/IdeateModel.swift`) talks to the routes here. The service runs one harness
process, Pi in `--mode rpc` (PLAN.md D9), on Inkling 2 Mini oQ3e (D7), on the on-Mac engine.

## Files

- `api.py`: the routes (`/ideate/status`, `/ideate/projects`, `.../message`, `.../cancel`,
  `.../approvals/{id}`). Every write goes through `native_only`, like Meetings and Study.
- `runner.py`: one run at a time, the run thread, events, approvals, cancel.
- `harness.py`: the adapter interface (`HarnessAdapter`, `HarnessSession`) and the Pi adapter:
  finding Pi, its private config, the gate extension, translating its RPC JSONL.
- `gate.py`: the decision for each tool call, using Wisp's policy (`service/safety/policy.py`).
- `sandbox.py`: the `sandbox-exec` profile.
- `engine.py`: where the engine is, whether Inkling is there and loaded, and residency.
- `store.py`: project folders, meta, the event log, the file list, path confinement.
- `config.py`: every number below.

## Projects on disk

`<Wisp data folder>/ideate/projects/<id>/`, where the data folder is `service.paths.MOE_DIR`
(`WISP_HOME` moves it). The id is 8 random lowercase letters and digits. The name comes from
the prompt: letters, digits, spaces, `-` and `'` only, at most 28 characters.

- `work/`: the harness's cwd (and `PWD`). This is the folder the tab opens.
- `.ideate/`: Wisp's own record. `meta.json`, `events.jsonl`, `harness.log` (stderr, capped),
  the gate extension, the system prompt and the sandbox profile. The harness can read these
  files but not write them.
- `.ideate/agent/`: the harness's private, writable state. `pi/` is `PI_CODING_AGENT_DIR`,
  plus `sessions/`, `home/` (`HOME`), `tmp/` (`TMPDIR`) and `xdg/`. Wisp rewrites
  `models.json` and `settings.json` before every start and removes `auth.json`.

A project left active when the service stopped is marked failed the next time it is read.

## Safety (D6)

- **Sandbox.** The profile starts from `deny default`. Reads are allowed broadly, except
  Wisp's data folder (this project excepted), the Keychains folder, `~/.omlx` (holds the
  engine's key), `~/.pi`, OpenCode's folders, `~/.ssh`, `~/.aws` and a few other key stores.
  A source-run Pi under `~/.pi/agent/install` gets read access to its own release folder only.
  Writes are allowed only in `work/`, `.ideate/agent/` and the usual `/dev` nodes. The only
  network allowed is outbound TCP to the engine's loopback port. There is no DNS, and Wisp's
  own service ports are refused. Launch Services, Apple Events and the pasteboard are denied,
  so `open` can't hand a URL to a browser. Paths are written in both spellings
  (`/var` and `/private/var`). If `sandbox-exec` can't be applied
  (`shell_sandbox.usable()` is False, as inside Simulation QA), `/ideate/status` says why and
  nothing starts: Ideate never runs unconfined.
- **Gate.** Wisp's extension (`.ideate/ideate-gate.ts`, loaded with `-ne -e`) asks the host
  before every tool call, over Pi's RPC `extension_ui_request` (an `input` dialog titled
  `wisp-ideate-gate`). It blocks unless the answer is an explicit yes. A timeout blocks, and
  so does any error. The host decides with `gate.judge()`:
  - bash: Wisp's floor and never-run list refuse. A command runs without a card only when
    Wisp's policy allows it AND it is one of Wisp's validated read-only commands (`ls`,
    `cat`, `head`, and so on). Full access doesn't lift this. Package installs and
    anything that reaches the network always get a card, and the card says the sandbox has
    no internet. Every other command gets a card.
  - write/edit inside `work/` runs; the sandbox is the boundary. Outside `work/`, or through
    `..`, `~` or a symlink, it gets a card saying the sandbox will refuse the write anyway,
    or it's refused when Wisp's path floor refuses it.
  - read runs. Any other tool is refused.
  - A card waits `APPROVAL_TIMEOUT_S`; no answer counts as no. Pi's RPC `bash` command (the
    human `!` command, which skips the hook) is never sent.
- **Engine key.** When the engine has a key, it reaches Pi only as the environment variable
  `WISP_IDEATE_ENGINE_KEY` (`models.json` says `"$WISP_IDEATE_ENGINE_KEY"`), and the gate
  prefixes every bash command with `unset WISP_IDEATE_ENGINE_KEY;`. The harness environment
  is built from scratch; nothing from the service's own environment is passed on.
- **Offline.** `PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 --offline`. Every
  discovered extension, skill, prompt template, theme, MCP server and context file is off.

## The lean profile (D10)

The engine's prefill guard rejects prompts past roughly 1.5 to 2k tokens while oQ3e is
resident. Ideate never raises that ceiling; it's the user's engine setting. So the harness
runs small. All of these are constants in `config.py`:

| Constant | Value | Why |
| --- | --- | --- |
| `TOOLS` | read, write, edit, bash | grep/find/ls cost schema tokens; bash covers them |
| system prompt | about 170 tokens + a kind line of 30 or fewer | replaces Pi's default with `--system-prompt <file>` (the bench's working setup) |
| `PROMPT_BUDGET_TOKENS` | 1400 | the conversation is kept below the guard |
| `CONTEXT_WINDOW` | 4096 | what Pi is told; with `COMPACT_RESERVE_TOKENS` = 2696, Pi compacts once the context passes 1400 |
| `COMPACT_KEEP_RECENT_TOKENS` | 500 | recent turns kept verbatim through a compaction |
| `MAX_OUTPUT_TOKENS` | 2048 | one reply, thinking included |
| `THINKING_LEVEL` | low | sent as `reasoning_effort` (`thinkingFormat: openai`); 70 to 95% of output is thinking. Not yet measured whether the engine honours it |
| `TOOL_RESULT_CHARS` | 600 | the gate extension's `tool_result` hook keeps the head and tail |
| `FRESH_RESTARTS` | 1 | on a guard rejection, one fresh conversation with a summary and the file list |
| `CARRY_SUMMARY_CHARS` | 500 | the carried summary |
| `APPROVAL_TIMEOUT_S` | 600 | a card unanswered for 10 minutes is a no |
| `CANCEL_GRACE_S` | 5 | abort, then kill the process group |
| `LOAD_TIMEOUT_S` | 240 | loading Inkling |

Pi exits 0 when the provider rejects a request. The rejection shows up only in the stream: an
assistant `message_end` with `stopReason: "error"` and `errorMessage`. The runner reads that.
A memory or prefill rejection triggers the one fresh restart, and if that fails too the run
ends `failed` with "The model ran out of memory room for this step. Try a smaller step, or
raise oMLX's memory limit." Any other model error ends it `failed` with one line.

## Model residency (D5)

Wisp keeps Ask's chat model warm (`main.py`'s keep-warm set). `memory_saver.py` parks it when
Wisp is idle, and `idle_unloader.py` drops other idle models. Ideate works with that:

1. Before a run, `engine.Residency.state()` reads `/v1/models/status`. If Inkling isn't
   listed, the run fails with "isn't downloaded yet". If it isn't loaded, the run's first
   event says "Loading Inkling 2 Mini. Ask's model steps aside while Ideate works, and comes
   back after." Then the run calls the engine client's own swap helper,
   `OMLXClient.ensure_only(MODEL_ID, exclusive=True)`, the same call Research makes. On the
   bundled engine that evicts the other models; on the user's oMLX.app it unloads nothing.
2. During the run, `idle.begin(MODEL_ID)` marks Inkling in flight, so the idle unloader
   never drops it mid-run. `idle.begin_foreground()` makes background model work (mail
   gists, the Brief, Save memory's batch windows) stand down rather than reload Ask's model
   beside it.
3. After the run, `idle.end` and `idle.end_foreground`. Nothing is pinned or unloaded. The
   idle unloader releases Inkling after Wisp's idle minutes, and Ask's model returns on the
   next Ask turn or prewarm.

These calls run on the service's own event loop (`bind_loop`, from the routes), because the
client's shared state (prewarm task, epochs) lives there.

Known gap: an Ask turn started *during* an Ideate run calls `ensure_only` for Ask's model.
On the bundled engine that evicts Inkling mid-run, and Pi's next request then waits for a
reload or fails. That's the user's own action and is left to the existing swap logic.

## Harness discovery

1. Packaged app: `Contents/Resources/ideate/{node/bin/node, pi/...}`
   (`build-support/ideate_harness.py`). If it's missing (built without `--ideate-harness`),
   the status says "Ideate's coding agent isn't included in this build of Wisp."
2. `WISP_IDEATE_PI`: a Pi release folder, the Pi install root, or `cli.js`. Node comes from
   `WISP_IDEATE_NODE`, else as below.
3. Source runs: the user's Pi (`~/.pi/agent/install/current-version`, then that release's
   `cli.js`), with Node from `~/.local/share/pi-node/current/bin/node`, `/opt/homebrew/bin/node`
   or `/usr/local/bin/node`. Only the release folder and `current-version` are read. Never
   `auth.json`, `settings.json`, `sessions/` or `models-store.json`.

`WISP_IDEATE_HARNESS` can name another registered adapter. Only `pi` exists. OpenCode would
map its `serve` API onto the same session calls.

## Event contract

`{seq, kind, text, tool, path, status, ts}`, with kind one of text, thinking, tool, approval,
file, done or error. `seq` increases monotonically per project and is persisted. Texts are
capped at 2000 characters and commands at 400. Tool status values are `running`, `ok`, `error`
and `denied`. An approval carries `{id, tool, text, path, note, reason}`. Project status is
`idle|starting|running|waiting|done|failed|cancelled`.
