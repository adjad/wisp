# Wisp — local Siri-style assistant for macOS

A strictly-local, privacy-first AI assistant with Mixture-of-Experts model routing.
A Swift menu-bar app talks to a Python (FastAPI) agent service, which does all
inference through **oMLX**, a local model server. No prompt, no email, no message,
and no file ever leaves the machine.

Hardware target: MacBook Pro (M5 Pro, 24 GB), with an optional MacBook Air side-node.

## Status

Wisp runs as a real menu-bar app (`dist/Wisp.app`) that launches its own backend
and ships as a sealed, ad-hoc-signed local candidate produced by a reproducible
build. It routes across a single resident model plus a small embedding model and
an optional reranker, drives the Mac through a tool-calling agent loop of **168
built-in tools**, reads Mail/Messages/Notes/Calendar, remembers things, answers
questions about any document on screen, and runs multi-source cited web research.

Common multi-step requests — reminders, replies, scheduled sends — no longer rely
on free-form tool selection at all: a **typed task engine** compiles them into a
plan that application code resolves and executes.

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
each claim and exports to Downloads. A Research Library keeps finished reports
in the main app. Search queries and page requests go to the public web;
planning, evidence, and synthesis remain local.

**Operating the Mac.** The agent loop drives 168 built-in tools across these areas:

| Area | Examples |
| --- | --- |
| Shell & files | `run_shell`, `read_file`, `write_file`, `list_dir`, `delete_path`, `find_files`, `organize_files`, `move_path` |
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

Because a long tool list measurably degrades selection, each request sees a
narrowed subset rather than all 168 — built from the request's read *and* write
intent, so a "summarize this and send it" request keeps the sending tool. An
ambiguous route retrieves its menu by lexical shortlist (a cross-encoder
reranker is available for comparison but is not the default: the lexical top-20
scored 40/40 at ~4.4 ms against the reranker's 39/40 at ~1.08 s).

**Typed task engine** (`service/tasks/`). For the task shapes that have to be
right every time, the model interprets language and application code decides
everything else. A request compiles into a typed plan; the **engine** resolves
its slots — dates, recipients, source references, reply targets — against real
records between compile and execute, and the executor only runs a plan whose
slots are all grounded. Missing information becomes a specific question instead
of a guess, and an outbound plan carries its exact recipient, account, and
scheduled time through corrections. `service/workflows/` is the same idea for
longer multi-turn flows, held in a persistent state machine that runs *before*
ordinary routing.

**Assistant layer.** The Swift app syncs Calendar, Mail, Messages, and Notes into
the backend (TCC permissions are per-process, and only the signed app bundle can
get a coherent "Wisp" prompt). A background scheduler runs lead-time rules over a
commitments store and pushes reminders over SSE; there's a daily brief on a
configurable schedule that separates human mail from automated senders, and an
outbox that holds outbound mail/messages for approval before anything is sent.
Delivery is durable: a queued send records a receipt, reconciles against the
native app's own result, and recovers orphaned work after a backend restart
rather than claiming a send that never landed. Scheduled sends freeze their
approved time at approval.

**Memory.** Server-side conversation sessions persisted to SQLite, with a
token-budgeted context builder that keeps a fixed working set (and summarizes)
so a long chat can't blow the memory budget. Alongside it: a fact store
(`remember` / `recall`, pinnable) fed by a conservative background capture pass
that only records attributable, complete user statements; entity tracking; and a
bounded **connections** investigator that proposes links between local records,
each backed by an exact excerpt and marked for review — an email's sender field
is treated as an observation, not proof of identity. A shared identity block
states who the user *is* in every second-person prompt, so "your post got 439
likes" in a group chat isn't misattributed to the user.

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
allow / confirm / deny. `read_only` makes Wisp inspect-only — including through
shell, where view-only strings cannot write — `full_access` auto-runs everything,
and a safety floor blocks catastrophic commands (`rm -rf /`, `sudo`, disk wipes)
even then, so a prompt-injected model can't nuke the machine. Irreversible
deletes (recursive, wildcard) always show the exact command for approval in every
mode and cannot be pre-approved. Per-tool grants and a `~/.moe/audit.jsonl` log
sit alongside it. Research mode is the same story applied to hostile web content:
it gets search/fetch/read only, never shell/mail/message/calendar/file-write, and
text found on a page is stored as data, never promoted to an instruction. Public
web requests are classified and their consent and effect boundaries frozen at the
request root, so a follow-up can't quietly widen what was authorized.

**When things go wrong.** A failed request explains itself in plain language — a
stopped engine, a memory limit, an over-long conversation each get their own
sentence and, where one exists, a next step; the raw error goes to the debug
export instead of the headline. Debug Mode captures tool-internal model calls
(source plus inner request and response), not just the final output.

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

## Build and release

`build-support/` produces an **ad-hoc sealed local candidate**: the Swift app,
tracked backend source and resources, and a relocatable Python runtime installed
from a hash-pinned lockfile. The build never installs or launches Wisp, verifies
the sealed artifact, and keeps signing QA isolated from simulation. oMLX and its
models stay separately installed runtime prerequisites. Ad-hoc sealing carries no
team identity and is **not** notarization. See [docs/build-release.md](docs/build-release.md)
and the non-mutating release gate in [docs/SIMULATION_QA.md](docs/SIMULATION_QA.md).

## Model roster

The router emits a **role**; `service/config/models.yaml` resolves it to an oMLX
model id. `~/.moe/config.yaml` overlays that at runtime (Settings writes here).

| Role | Model | Why |
| --- | --- | --- |
| `fast`, `router`, `coding`, `reasoning`, `agent`, `general` | `Ling-3.0-tiny-oQ4e` | one resident model for every text/tool role — no swap, no cold load, no routing decision that hinges on which model a request lands on |
| `research` | defaults to `coding`'s model unless overridden | Wisp Research's plan/query/extraction/synthesis calls; independently settable in Settings |
| `embedding` | `Qwen3-Embedding-0.6B-4bit-DWQ` | Smart Search's T2 retrieval tier; small enough (320 MB) to co-fit with the chat model, so search never evicts it |
| `reranker` | `Qwen3-Reranker-0.6B-mlx-6bit` | optional cross-encoder for tool retrieval; not the default, and it evicts the embedder before use so all three never sit resident together |

Quantization matters more than it looks: there is a hard reliability cliff below
4 bits. Measured over 10 reps on a 16-tool ambiguous menu — oQ3e 5/10, oQ3.5e
6/10, **oQ4e 10/10**, oQ5e 10/10, oQ6e 10/10. Below 4 bits the model picks the
wrong tool about half the time and answers confidently, which is silent.

There is no vision role or vision-capable tool — Wisp does not accept images as
a prompt source. Idle models auto-unload after a configurable timeout (default
5 minutes).

## Layout

- `service/` — FastAPI agent service.
  - `router/` MoE routing (keyword tiers → LLM fallback; decides `needs_tools`)
  - `agent/` tool loop + interactive approver · `tools/` the tool registry
  - `tasks/` typed task engine (compiler, engine, planner, executor, references)
  - `workflows/` persistent multi-turn workflow state machine
  - `memory/` sessions, context budgeting, facts, capture, connections, identity
  - `assistant/` commitments, scheduler, reminders, brief, outbox, delivery, recovery
  - `research/` Wisp Research: orchestrator, evidence store, web fetcher, ranking
  - `search/` Smart Search (chunker, lexical, embedder, engine, synth)
  - `safety/` policy, grants, audit · `skills/` · `mcp/` · `inference/` oMLX client
  - `errors.py` plain-language error translation · `debug_capture.py` Debug Mode
  - `codex_monitor.py` read-only watch on local Codex tasks
- `app/` — Swift menu-bar frontend (overlay, search panel, research windows,
  settings, OS readers, outbound sender).
- `air/` — the Air periodic node (`wispair/`: config, model, jobs, scheduler, store).
- `build-support/` — the sealed-candidate build pipeline and lockfiles.
- `scripts/` — run, packaging, evaluation, and icon helpers.
- `docs/` — see [docs/README.md](docs/README.md).

## State on disk

Everything is local files under `~/.moe/` (and `~/.wispair/` on the Air, 0700):
`config.yaml` (role overlay), `sessions.db`, `facts.db`, `assistant.db`,
`research.db` + `research_cache/`, `cache/` (Mail/Messages/Notes snapshots),
`skills/`, `mcp.json`, `grants.json`, `audit.jsonl`, plus small state files for
the brief, delivery receipts, and the Codex monitor.

Wisp also keeps a read-only eye on local Codex tasks. Ask "catch me up on
Codex," "which Codex tasks are still running?", or "does any Codex task need
me?" for an on-demand overview. While Wisp is open, it quietly polls the local
Codex task index and sends a macOS notification only when a task finishes,
fails, or has had no recorded activity for 15 minutes. The first poll is a
baseline, so launching Wisp does not produce a burst of old notifications.

## Docs

[docs/README.md](docs/README.md) indexes everything. The usual entry points:

- [docs/WISP_OVERVIEW.md](docs/WISP_OVERVIEW.md) — long-form system reference.
- [docs/ASSISTANT_ARCHITECTURE.md](docs/ASSISTANT_ARCHITECTURE.md) — the assistant layer's design.
- [docs/TYPED_TASK_ENGINE_PLAN.md](docs/TYPED_TASK_ENGINE_PLAN.md) — the typed task engine.
- [docs/SMART_SEARCH_DESIGN.md](docs/SMART_SEARCH_DESIGN.md) — the four-tier search.
- [docs/RESEARCH_TOOL_PLAN.md](docs/RESEARCH_TOOL_PLAN.md) — Research mode's design and status.
- [CHANGELOG.md](CHANGELOG.md) — what shipped.
