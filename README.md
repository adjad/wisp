# Wisp — local Siri-style assistant for macOS

A strictly-local, privacy-first AI assistant with Mixture-of-Experts model routing.
A Swift menu-bar app talks to a Python (FastAPI) agent service, which does all
inference through **oMLX**, a local model server. No prompt, no email, no message,
and no file ever leaves the machine.

Hardware target: MacBook Pro (M5 Pro, 24 GB), with an optional MacBook Air side-node.

## Status

Wisp runs as a real menu-bar app (`dist/Wisp.app`) that launches its own backend.
It routes across a single resident model plus a small embedding model, drives the
Mac through a tool-calling agent loop of 160+ tools, reads Mail/Messages/Notes/
Calendar, remembers things, answers questions about any document on screen, and
runs multi-source cited web research.

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

**Research mode.** Toggle **Research** in the chat header, enter a question, and
Wisp opens an editable plan in a separate report window. The orchestrator runs an
iterative, multi-round search loop — it keeps searching only the subquestions
that still lack independent-source coverage, checks sources for topical
relevance before fetching, and stops once coverage is met, two rounds add
nothing new, or the depth's budget runs out. Fetching prefers a source's own
official API where one exists (Wikipedia's Action API, the Internet Archive)
over scraping HTML, with a Wayback Machine fallback when a live page is
unreachable. Evidence is atomic, exact-quote, and host-validated — the model
never invents a citation — and conflicting claims across sources surface as a
"Disagreements" section rather than being silently resolved. The live view
shows searches, accepted/failed sources with a quality class, pause/cancel, and
steering. The finished report's citation drawer shows the exact passage behind
each claim and exports to Downloads. Search queries and page requests go to the
public web; planning, evidence, and synthesis remain local.

**Operating the Mac.** The agent loop drives 160+ tools across these areas:

| Area | Examples |
| --- | --- |
| Shell & files | `run_shell`, `read_file`, `write_file`, `list_dir`, `delete_path`, `find_files`, `organize_files` |
| Apps & system | `open_app`, `quit_app`, `set_volume`, `set_wifi`, `lock_screen`, `get_battery_status`, `clipboard_read/write`, `window_control` |
| Calendar & reminders | `get_upcoming`, `get_past_events`, `add_calendar_event`, `find_free_time`, `add_reminder` |
| Mail, Messages, Notes | `summarize_emails`, `send_email`, `triage_inbox`, `summarize_messages`, `send_message`, `search_notes` |
| Contacts | `lookup_contact`, `list_contacts`, `manage_contacts` |
| Web & research | `web_search`, `web_fetch`, `http_request`, `wikipedia_summary` |
| Memory | `remember`, `recall`, `forget` |
| Codex | `get_codex_updates` — running, finished, failed, and possibly stalled local tasks |
| Media | `spotify`, `music`, `play_podcast`, `text_to_speech`, `transcribe_audio` |
| Everyday utilities | `get_weather`, `convert_currency`, `track_package`, `get_directions`, `calculate`, and dozens more |
| Authoring | `create_tool` |

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

**Skills & MCP.** Skills are installable folders (`~/.moe/skills/<name>/SKILL.md`)
that contribute trigger-matched instructions and shell/script-backed tools —
new capabilities without code changes, running through the normal policy engine.
Wisp bundles two multi-turn conversational workflows: `@interview-me` for
clarifying intent one question at a time, and `@idea-refine` for exploring and
converging on a focused concept. They can also activate from their natural
trigger phrases and remain active until completed or stopped.
MCP covers the other half: any server configured in `~/.moe/mcp.json` (Notion,
Linear, GitHub, Postgres…) contributes its tools to the same loop.

**Safety.** Every tool declares a category; `service/config/policy.yaml` decides
allow / confirm / deny. `read_only` makes Wisp inspect-only, `full_access`
auto-runs everything, and a safety floor blocks catastrophic commands (`rm -rf /`,
`sudo`, disk wipes) even then — so a prompt-injected model can't nuke the machine.
Per-tool grants and a `~/.moe/audit.jsonl` log sit alongside it. Research mode is
the same story applied to hostile web content: it gets search/fetch/read only,
never shell/mail/message/calendar/file-write, and text found on a page is stored
as data, never promoted to an instruction.

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
| `fast`, `router`, `coding`, `reasoning`, `agent`, `general` | `Ling-3.0-tiny-oQ4e` | one resident model for every text/tool role — no swap, no cold load, no routing decision that hinges on which model a request lands on |
| `research` | defaults to `coding`'s model unless overridden | Wisp Research's plan/query/extraction/synthesis calls; independently settable in Settings |
| `embedding` | `Qwen3-Embedding-0.6B-4bit-DWQ` | Smart Search's T2 retrieval tier; small enough (320 MB) to co-fit with the chat model, so search never evicts it |

There is no vision role or vision-capable tool — Wisp does not accept images as
a prompt source. Idle models auto-unload after a configurable timeout (default
5 minutes).

## Layout

- `service/` — FastAPI agent service.
  - `router/` MoE routing (keyword tiers → LLM fallback; decides `needs_tools`)
  - `agent/` tool loop + interactive approver · `tools/` the tool registry
  - `memory/` sessions, facts, entities, profile, identity
  - `assistant/` commitments, scheduler, reminders, brief, outbox, connectors
  - `research/` Wisp Research: orchestrator, evidence store, web fetcher, ranking
  - `search/` Smart Search (chunker, lexical, embedder, engine, synth)
  - `safety/` policy, grants, audit · `skills/` · `mcp/` · `inference/` oMLX client
- `app/` — Swift menu-bar frontend (overlay, search panel, settings, OS readers).
- `air/` — the Air periodic node (`wispair/`: config, model, jobs, scheduler, store).
- `scripts/` — run, packaging, and icon helpers.

## State on disk

Everything is local files under `~/.moe/` (and `~/.wispair/` on the Air, 0700):
`config.yaml` (role overlay), `sessions.db`, `facts.db`, `assistant.db`,
`research.db` + `research_cache/`, `profile.md` + `profile.json`, `cache/`
(Mail/Messages/Notes snapshots), `skills/`, `mcp.json`, `grants.json`,
`audit.jsonl`.

Wisp also keeps a read-only eye on local Codex tasks. Ask “catch me up on
Codex,” “which Codex tasks are still running?”, or “does any Codex task need
me?” for an on-demand overview. While Wisp is open, it quietly polls the local
Codex task index and sends a macOS notification only when a task finishes,
fails, or has had no recorded activity for 15 minutes. The first poll is a
baseline, so launching Wisp does not produce a burst of old notifications.

## Docs

- [ASSISTANT_ARCHITECTURE.md](ASSISTANT_ARCHITECTURE.md) — the assistant layer's design.
- [SMART_SEARCH_DESIGN.md](SMART_SEARCH_DESIGN.md) — the four-tier search.
- [RESEARCH_TOOL_PLAN.md](RESEARCH_TOOL_PLAN.md) — Research mode's design and implementation status.
