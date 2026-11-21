# Ideate harness live test: Pi 1.6.0 vs OpenCode 2.3.2 on Inkling 2 Mini oQ3e

Model: `Inkling-2-Mini-oQ3e-mtp` on the user's oMLX (127.0.0.1:8000). Raw data: `docs/ideate/bench-results.json`. oQ4e was not tested (coordinator change).

## Methodology
Four invented tasks, each in a fresh folder under `$TMPDIR/ideate-bench/<harness>-oQ3e/<task>/`, one run each, 600 s cap, stdin closed:
- fixbug: pre-made `calc.py` with an off-by-one in `average()` plus failing `unittest` tests; fix without editing the tests.
- jsonflag: add `--json` to an existing `stats.py` (keep plain output as default).
- todo: build a Python todo CLI (add/list/done) with unittest tests, and run them.
- stopwatch: single-page `index.html` with ids display/start/stop/reset.

I verified results myself: unittest runs, running the CLIs, and a node fake-DOM/fake-timer run of the stopwatch. Measured by a wrapper: wall time, time to first stdout line, tool calls from the JSON stream, parseability of every stdout line, and peak RSS from `/usr/bin/time -l`. Each harness ran against a throwaway HOME/XDG/config under `$TMPDIR`; the user's real harness config directories and credential files were not read, written or printed (directory mtimes checked before and after, unchanged). OpenCode ran with `--standalone` (private server), not the user's background service. Offline switches were set for both (`PI_OFFLINE`, `OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_DISABLE_AUTOUPDATE`).

## Results
| Harness | Task | Wall | First event | Tool calls | Outcome (verified) | Ended by guard? | Peak RSS |
|---|---|---|---|---|---|---|---|
| Pi | fixbug | 8.7 s | 0.13 s | 3 | PASS, tests green, tests untouched, gave a final summary | no | 133 MB |
| Pi | jsonflag | 15.8 s | 0.15 s | 2 | PASS | yes (after the write; never ran it) | 135 MB |
| Pi | todo | 36.4 s | 0.13 s | 3 | FAIL: CLI works, its own 6 tests fail (1 failure, 2 errors); model never ran them | yes | 158 MB |
| Pi | stopwatch | 44.0 s | 0.15 s | 2 | PASS (ids, syntax, fake-timer run 00:02.99, stop holds, reset) | yes | 158 MB |
| OpenCode | fixbug | 11.9 s | 4.7 s | 2 | FAIL: read files, ran tests, then rejected before editing | yes, exit 1 | 406 MB |
| OpenCode | jsonflag | 23.5 s | 4.5 s | 3 | PASS (written before the guard hit) | yes, exit 1 | 423 MB |
| OpenCode | todo | 83.1 s | 6.1 s | 2 | PASS (CLI works, its 7 tests pass) | yes, exit 1 | 410 MB |
| OpenCode | stopwatch | 42.8 s | 3.9 s | 2 | PASS | yes, exit 1 | 407 MB |

Pass counts: Pi 3/4, OpenCode 3/4. Only Pi's fixbug run finished cleanly; every other run (both harnesses) was truncated by the engine's prefill memory guard (see `model-eval.md`) one turn after the file write, so "pass" often means the write landed before the context grew past roughly 1.5-2k tokens. None of the runs showed malformed tool calls, loops or refusals; every tool call executed (0 tool errors). Single runs, so treat differences as indicative.

## Findings
1. Both harnesses are unusable at their default prompts on this engine. Pi's default system prompt plus tool schemas was rejected by 20 MB (19.97 vs 19.95 GB limit). OpenCode's default agent produced a request estimated at 9.0 GB KV+SDPA (about 3.3k+ tokens of prompt) and was rejected on turn one: its prompt carries a code-mode tool catalogue, websearch, skill, image-generation and session tools.
2. Both ran only after trimming: Pi with `--system-prompt` (one line) and `--tools bash,read,write,edit`; OpenCode with a custom "lean" agent that disables every tool but `read` and `shell`. Even then the budget allowed 2-3 turns.
3. Failure signalling differs. Pi exits 0 on a provider rejection and puts it only in the stream (`stopReason:"error"`, `errorMessage` on the assistant `message_end`); a host must check it. OpenCode emits `{"type":"error"}` and exits 1.
4. JSON stream: both streams were 100% parseable line by line (0 unparseable lines across all runs). Pi: strict JSONL with a session header, `tool_execution_start/end`, `agent_end`, `agent_settled`; about 12 events for a failed turn, 50-80 for a task. OpenCode: `step_start`, `reasoning`, `tool_use` (completed state included), `text`, `step_finish`, `error`.
5. Start-up: Pi first event 0.13-0.15 s (Node, about 135-160 MB RSS). OpenCode first event 3.9-6.4 s (private server boot plus first model turn; RSS about 400-425 MB).
6. OpenCode gotcha: the shell tool took its working directory from the inherited `PWD` environment variable, not the process cwd, and wrote the first test file in the parent folder. Setting `PWD` to the project folder fixed it (done in the wrapper and the stray file removed). Ideate must set both cwd and `PWD`.
7. OpenCode 2.3.2 exposes no write/edit tool in this configuration; the model writes files with shell heredocs. Pi has real `write`/`edit` tools, which are shorter in the transcript (less context pressure) and safer.
8. Model behaviour was the same in both: it looks around first (`ls`), writes, and tends to skip verification (Pi todo never ran its tests).

## Exact working configuration
Pi (`$PI_CODING_AGENT_DIR/models.json`; no real key needed, any string works):
```json
{ "providers": { "omlx": { "baseUrl": "http://127.0.0.1:8000/v1", "api": "openai-completions", "apiKey": "local",
  "models": [ { "id": "Inkling-2-Mini-oQ3e-mtp", "name": "Inkling 2 Mini oQ3e", "reasoning": true, "input": ["text"], "contextWindow": 131072, "maxTokens": 16384 } ] } } }
```
```sh
HOME=<throwaway> PI_CODING_AGENT_DIR=<throwaway>/pi-agent PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 PWD=<project> \
pi --mode json -p --no-session --provider omlx --model Inkling-2-Mini-oQ3e-mtp \
   --no-context-files --no-skills --no-extensions --no-mcp --no-themes --no-prompt-templates \
   --system-prompt "You are a coding agent. Use the tools to do the task, check your work by running it, then stop." \
   --tools bash,read,write,edit "<task>"      # run with cwd=<project>, stdin closed
```
OpenCode (`OPENCODE_CONFIG=<throwaway>/opencode.json`):
```json
{ "autoupdate": false, "share": "disabled", "lsp": false, "mcp": {}, "formatter": false, "snapshot": false,
  "provider": { "omlx": { "npm": "@ai-sdk/openai-compatible", "name": "oMLX local",
    "options": { "baseURL": "http://127.0.0.1:8000/v1", "apiKey": "local" },
    "models": { "Inkling-2-Mini-oQ3e-mtp": { "name": "Inkling 2 Mini oQ3e", "reasoning": true, "tool_call": true, "limit": { "context": 131072, "output": 16384 } } } } },
  "permission": { "skill": "deny", "websearch": "deny", "execute": "deny", "webfetch": "deny", "experimental_image_generate": "deny", "question": "deny", "task": "deny", "todowrite": "deny", "opencode_*": "deny" },
  "agent": { "lean": { "mode": "primary", "description": "lean", "prompt": "You are a coding agent. Use the tools to do the task, check your work by running it, then stop.",
    "tools": { "bash": true, "read": true, "write": true, "edit": true, "skill": false, "execute": false, "websearch": false, "experimental_image_generate": false, "opencode_*": false } } } }
```
```sh
HOME=<throwaway> XDG_CONFIG_HOME=<throwaway>/.config XDG_DATA_HOME=<throwaway>/.local/share XDG_CACHE_HOME=<throwaway>/.cache \
OPENCODE_CONFIG=<throwaway>/opencode.json OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1 PWD=<project> \
/Applications/OpenCode.app/Contents/Resources/opencode-cli run --standalone --auto --format json --thinking \
   --agent lean -m omlx/Inkling-2-Mini-oQ3e-mtp "<task>"      # cwd=<project>, stdin closed
```
Even with these, the request still carried `read`, `shell` and `skill` tool schemas (about 3k characters) plus about 1k characters of system prompt.

## Recommendation: Pi, as a headless child process
- Pi: 3x smaller process (about 135-160 MB vs about 400-425 MB), 30-40x faster to first event, real write/edit tools, one strict JSONL stream, smaller prompt and request footprint (the only run that finished cleanly), no server to manage, and every prompt/tool/skill/extension surface can be switched off by flag. Costs: exits 0 on provider errors (Ideate must read `stopReason`/`errorMessage`), one provider config file.
- OpenCode: heavier, boots a server, default prompt is about 3x over the engine budget and needs an agent definition plus about 9 permission/tool denials to shrink; no write/edit tool; cwd depends on `PWD`. Its exit code and `error` event are cleaner.
- Neither is shippable for Ideate on this engine until the oMLX prefill ceiling is raised or Ideate uses a model with a smaller KV footprint: with the current ceiling the practical conversation is about 1.5-2k tokens, so every multi-step task dies after 2-3 turns. Rerun this suite after that change before final approval.
