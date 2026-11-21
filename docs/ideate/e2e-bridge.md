# Ideate bridge, end to end (2026-11-21)

Isolated Wisp service on 127.0.0.1:8893 (`WISP_HOME` in a temp folder, every text role set to
`Inkling-2-Mini-oQ3e-mtp`, the only model loaded before, during and after). Real Pi 1.6.0 (the user's
install, read-only) under the generated sandbox profile, against the user's oMLX on :8000. The prompts were
invented.

## Two bugs found and fixed

1. **Pi couldn't start from a source run.** The profile hides `~/.pi` (Pi's keys live there). The harness's own
   release folder was re-allowed, but Node's `realpath` also `lstat`s every parent folder: `EPERM lstat ~/.pi`.
   Fix: `file-read-metadata` on the exact ancestor paths of the project and harness roots (contents stay
   denied). The bundled app runs Pi from inside the app, so this only affected source runs, but the fix is
   general. `service/ideate/sandbox.py`.
2. **Every reply was 1 token long (`stopReason: length`).** Captured through a logging proxy: Pi sent
   `max_tokens: 1`. pi-ai sizes a reply as `contextWindow − input − max(4096, 15% of input)`, floored at 1, and
   the bridge told Pi the window was 4096. Fix: `CONTEXT_WINDOW = 8192` (compaction still triggers at the
   1,400-token budget because the reserve is derived from it), plus an import-time assert that the window
   covers budget + 4096 + `MAX_OUTPUT_TOKENS`. After the fix Pi sent `max_tokens` 2048 then 1489.
   `service/ideate/config.py`.

## The run (UI variant): "Make index.html: a stopwatch with start, stop and reset buttons. Then check the file exists with ls."

| seq | event | result |
| --- | --- | --- |
| 2–3 | bash `ls -la` | ran without a card (Wisp's read-only shell list) |
| 5–7 | write `index.html` | written inside `work/`, file event (2,780 bytes, a working stopwatch page) |
| 8 | engine prefill guard tripped | bridge restarted fresh with a short summary + file list |
| 10–12 | bash `ls -la && cat … \| head -50` | compound command → approval card; nobody answered → denied after the timeout |
| 13–16 | bash `ls -la`, `cat index.html` | ran without a card |
| 18–20 | bash `wc -l index.html && cat index.html` | approval card → **Allow** via `POST …/approvals/{id}` → ran ok |
| 21 | engine prefill guard tripped again | status `failed`: "The model ran out of memory room for this step. Try a smaller step, or raise oMLX's memory limit." |

Nothing was written outside the project folder (checked the home folder's top level by mtime).

## What this shows

- The bridge, the sandbox, the read-only auto-allow, approval cards (allow and timeout-deny), file events, the
  guard retry and the honest failure all work with the real harness and the real model.
- The limit is the engine's prefill ceiling (PLAN.md D10): one small file per run works; a second look at the
  file can push the context past ~1.5–2k tokens. Raising oMLX's `custom_ceiling_bytes` (the user's setting) or a
  model with a smaller KV footprint is what makes multi-step builds practical.
- Not tested here: the in-app tab driving this (it is tested after the swap), cancel on a live run, and the
  bundled (in-app) harness path. The bundled path is covered by `tests/test_ideate_harness_bundle.py` and the
  sandboxed `--version`/`get_state` smoke the bridge builder ran.
