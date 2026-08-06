# Wisp — local Siri-style assistant for macOS

A strictly-local, privacy-first AI assistant with Mixture-of-Experts model routing.
A Swift menu-bar app talks to a Python (FastAPI) agent service, which does all
inference through **oMLX**, a local model server. No prompt, no email, no message,
and no file ever leaves the machine.

Hardware target: MacBook Pro (M5 Pro, 24 GB), with an optional MacBook Air side-node.

## Status

Past the phased build — Wisp runs as a real menu-bar app (`dist/Wisp.app`) that
launches its own backend. It routes across a five-model roster, drives the Mac
through a tool-calling agent loop, reads Mail/Messages/Notes/Calendar, remembers
things, and answers questions about any document on screen. Pre-release: the
end-to-end pass in [TESTING.md](TESTING.md) is the gate.

## What it does

**Chat overlay — ⌥Space.** A notch-anchored panel (collapsed it's a black bar
fused with the camera housing; hover expands it). Streaming replies, markdown,
session history, per-action confirmation prompts.

**Smart Search — ⌘⇧F.** Ask a question about whatever document is on screen and
get a grounded answer. Four tiers stream out as they land, each independently
failable: **T0** literal matches (~1 ms), **T1** BM25 lexical, **T2** semantic
(embeddings, fused with T1 via RRF), **T3** a synthesized answer with citations.
Deliberately not ⌘F — Carbon would claim that system-wide and break native find
everywhere. Never swaps the resident chat model.

**Operating the Mac.** The agent loop drives ~42 tools:

| Area | Tools |
| --- | --- |
| Shell & files | `run_shell`, `read_file`, `write_file`, `list_dir`, `delete_path` |
| Apps & system | `open_app`, `quit_app`, `get_volume`, `set_volume`, `set_wifi`, `lock_screen`, `set_keyboard_backlight`, `get_battery_status`, `run_speed_test`, `clipboard_read`, `clipboard_write` |
| Calendar & reminders | `get_upcoming`, `get_past_events`, `add_calendar_event`, `cancel_event`, `add_reminder` |
| Mail, Messages, Notes | `summarize_emails`, `view_emails`, `send_email`, `summarize_messages`, `view_messages`, `send_message`, `search_notes` |
| Contacts | `lookup_contact`, `list_contacts` |
| Vision & web | `see_screen`, `describe_image`, `web_fetch`, `http_request` |
| Memory | `remember`, `recall`, `forget`, `show_profile`, `build_profile` |
| Media | `spotify`, `music` |
| Authoring | `write_code`, `create_tool` |

**Assistant layer.** The Swift app syncs Calendar, Mail, Messages, and Notes into
the backend (TCC permissions are per-process, and only the signed app bundle can
get a coherent "Wisp" prompt). A background scheduler runs lead-time rules over a
commitments store and pushes reminders over SSE; there's a daily brief on a
configurable schedule, and an outbox that holds outbound mail/messages for
approval before anything is sent.

**Memory.** Conversation sessions with summarization, a fact store (`remember` /
`recall`, pinnable), entity tracking, and a **user profile** built map-reduce
style over everything already synced — a MAP pass on the small model extracts
durable facts batch by batch, a REDUCE pass on the big model merges them into
`~/.moe/profile.md`. A shared identity block states who the user *is* in every
second-person prompt, so "your post got 439 likes" in a group chat isn't
misattributed to the user.

**Super Model.** Deliberately engage a heavier model for the hardest work: it
quits memory-hungry apps, pauses background AppleScript syncs, doubles the live
history budget to 32K, bumps the model's oMLX context window, and tells the model
to actually run and verify the code it writes.

**Skills & MCP.** Skills are installable folders (`~/.moe/skills/<name>/SKILL.md`)
that contribute trigger-matched instructions and shell/script-backed tools —
new capabilities without code changes, running through the normal policy engine.
MCP covers the other half: any server configured in `~/.moe/mcp.json` (Notion,
Linear, GitHub, Postgres…) contributes its tools to the same loop.

**Safety.** Every tool declares a category; `service/config/policy.yaml` decides
allow / confirm / deny. `read_only` makes Wisp inspect-only, `full_access`
auto-runs everything, and a safety floor blocks catastrophic commands (`rm -rf /`,
`sudo`, disk wipes) even then — so a prompt-injected model can't nuke the machine.
Per-tool grants and a `~/.moe/audit.jsonl` log sit alongside it.

**Air node** (`air/`). A MacBook Air running every 3 hours while the Pro's lid is
closed: reads new mail and iMessages, summarizes each window in one call, writes
action items into Apple Reminders, and holds the summaries until the Pro asks.
Strictly sequential — the runtime's prompt cache holds exactly one entry.

## Quick start

```bash
# oMLX must be running (its menu-bar app auto-starts the server on :8000)
./scripts/run.sh                      # Wisp backend on :8765
```

Then build and launch the app:

```bash
./scripts/package_app.sh && open dist/Wisp.app
```

`Wisp.app` bundles its own copy of `service/` and `.venv/` and starts the backend
itself — **backend changes only reach the app after re-running `package_app.sh`.**

Hitting the API directly:

```bash
curl -sN localhost:8765/agent -H 'Content-Type: application/json' \
  -d '{"prompt":"what is on my screen?"}'
```

`/agent` routes the request and streams SSE events: `session`, `routed`, `delta`,
`text`, `tool_call`, `confirm`, `tool_result`, `done`, `error`. `/chat` is the
plain-completion path (pass a `role` or a `model`, optional `stream`).

## Model roster

The router emits a **role**; `service/config/models.yaml` resolves it to an oMLX
model id. `~/.moe/config.yaml` overlays that at runtime (Settings writes here).

| Role | Model | Why |
| --- | --- | --- |
| `fast`, `router` | `gemma-4-E4B-it-qat-4bit` | tiny classifier / trivial answers (~6.3 GB) |
| `agent`, `general`, `coding`, `reasoning` | `gpt-oss-20b-MXFP4-Q8` | the only model trusted to emit parseable tool calls (~11.8 GB) |
| `vision` | `Qwen3-VL-8B-Instruct-4bit` | screenshots and images (5.8 GB) |
| `embedding` | `Qwen3-Embedding-0.6B-4bit-DWQ` | Smart Search T2; co-fits with everything (320 MB) |
| `profile` / `profile_map` | gpt-oss / gemma | reduce needs judgment, the 70+ map calls don't |

Two retirements are load-bearing and documented inline in `models.yaml`:
Qwen2.5-Coder-14B (gpt-oss was ~3× the throughput at a smaller footprint) and
Phi-4-reasoning-plus (no more rigorous than gpt-oss at `reasoning_effort=medium`,
5–8× slower). Idle models auto-unload after 5 minutes.

## Layout

- `service/` — FastAPI agent service.
  - `router/` MoE routing (keyword tiers → LLM fallback; decides `needs_tools`)
  - `agent/` tool loop + interactive approver · `tools/` the tool registry
  - `memory/` sessions, facts, entities, profile, identity
  - `assistant/` commitments, scheduler, reminders, brief, outbox, connectors
  - `search/` Smart Search (chunker, lexical, embedder, engine, synth)
  - `safety/` policy, grants, audit · `skills/` · `mcp/` · `inference/` oMLX client
- `app/` — Swift menu-bar frontend (overlay, search panel, settings, OS readers).
- `air/` — the Air periodic node (`wispair/`: config, model, jobs, scheduler, store).
- `scripts/` — run, packaging, icon, and test-data helpers.
- `tests/`, `test_fixtures/` — automated tests and synthetic Mail/Messages/Notes data.

## State on disk

Everything is local files under `~/.moe/` (and `~/.wispair/` on the Air, 0700):
`config.yaml` (role overlay), `sessions.db`, `facts.db`, `assistant.db`,
`profile.md` + `profile.json`, `cache/` (Mail/Messages/Notes snapshots),
`skills/`, `mcp.json`, `grants.json`, `audit.jsonl`.

## Testing

[TESTING.md](TESTING.md) is the end-to-end checklist. Test data is opt-in and
reversible — `scripts/wisp_testdata.py seed-db` tags everything so it can't
collide with real data, and cache fixtures install/restore around your real
`~/.moe/cache`:

```bash
.venv/bin/python scripts/wisp_testdata.py seed-db      # Wisp's own DBs
.venv/bin/python scripts/wisp_testdata.py build-cache  # repo-local fixtures
```

## Docs

- [ASSISTANT_ARCHITECTURE.md](ASSISTANT_ARCHITECTURE.md) — the assistant layer's design.
- [SMART_SEARCH_DESIGN.md](SMART_SEARCH_DESIGN.md) — the four-tier search.
- [CLAUDE_AIR_PERIODIC_HANDOFF.md](CLAUDE_AIR_PERIODIC_HANDOFF.md) — the Air node brief.
- [BUILD_PLAN.md](BUILD_PLAN.md) — the original phased plan (historical).
