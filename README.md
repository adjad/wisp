# Wisp: a private, local-first assistant for macOS

Wisp lives at the top of your screen, in the notch. Ask it things, get a daily Brief of what
needs you, and see your day hour by hour. The AI model runs on your Mac, and your mail, calendar
and messages stay there.

A Swift menu-bar app talks to a bundled Python (FastAPI) agent service and a private copy of the
oMLX model engine. This is the **0.5.0 release candidate** for testers; see
[docs/releases/0.5.0-rc1.md](docs/releases/0.5.0-rc1.md) for what is new and what is still rough.

## Install Wisp (read this first)

**macOS will block Wisp the first time you open it.** The app is not notarized by Apple yet, so
on macOS 15 (Sequoia) and later you have to allow it once by hand:

1. Download the ZIP from [GitHub Releases](https://github.com/adjad/wisp/releases), double-click
   it to unzip, and drag **Wisp.app** into your **Applications** folder.
2. Open Wisp. macOS says it can't verify the app. Click **Done** (do not move it to the Bin).
3. Open **System Settings**, choose **Privacy & Security** in the sidebar, and scroll all the way
   down to the **Security** section.
4. Next to the message about "Wisp" being blocked, click **Open Anyway**.
5. Enter your Mac password or use Touch ID, then click **Open Anyway** once more in the dialog.

You only do this once. On macOS 14 and earlier, Control-click **Wisp.app**, choose **Open**, and
confirm. If macOS still refuses, open Terminal and run
`xattr -dr com.apple.quarantine /Applications/Wisp.app`, then open Wisp again.

If you are updating from an earlier build, quit the old Wisp first (menu-bar icon, **Quit Wisp**).
macOS can ask you to allow permissions again after an update. If Full Disk Access stops working,
remove Wisp from the list with the **−** button and add it again.

### What you need

| | |
| --- | --- |
| Mac | Apple silicon (M1 or later). Intel Macs are not supported. |
| macOS | **26.2 or newer** for the AI engine built into Wisp. The app itself opens on macOS 14 and newer, but on an older Mac it needs another engine (see [Using another local inference app](#using-another-local-inference-app)). |
| Memory | **16 GB is the minimum and 24 GB is recommended.** Wisp's model keeps several GB in memory while it answers, so close other heavy apps on a 16 GB Mac. |
| Disk | About 8 GB free for the first download. |
| Internet | For the first-run model download, and for web questions and Google. |

### First run

The first time Wisp opens it shows a short setup window. It never blocks you: every step can be
skipped and finished later from the menu-bar icon (**Finish Setup...**).

1. **Welcome and download.** Wisp picks the AI model for your Mac, **Gemma 4 E4B**, and downloads
   it once. It is about **5.5 GB**: roughly 10 minutes on a fast home connection and half an hour or
   more on a slow one. The engine that runs it is already inside Wisp, so there is nothing else to
   install. The first start of the engine can take a few minutes. You can keep going through the
   other steps while it downloads, and Wisp sends a notification when it is ready. When the download
   finishes, Wisp checks that the model really answers.
2. **Permissions.** Each one is optional, and you can change your mind later in System Settings:

   | Permission | Why Wisp asks |
   | --- | --- |
   | Calendar | To plan your day from your meetings and events, and to add or move events when you ask. |
   | Reminders | To show what is due, and to add or complete reminders when you ask. |
   | Mail and Notes (Automation) | To read your mail and notes while those apps are open. Wisp opens them in the background to ask. |
   | Full Disk Access | To read Messages, and Mail and Notes when those apps are closed. macOS can't prompt for this one, so Wisp opens the right page of System Settings and you switch **Wisp** on. macOS may offer to reopen Wisp, and setup picks up where you left off. |
   | Contacts | To show names instead of phone numbers. |
   | Notifications | For reminder alerts and "Wisp is ready". |

   Screen Recording (for "what is on my screen?") and Accessibility (to search text inside other
   apps) are not asked for here. macOS asks the first time a feature needs them.
3. **How to use it.** A short tour of the notch and the shortcuts, with a few questions to try.

If you leave before the model is ready, Wisp keeps a small cue on the notch and tells you when it
can answer.

### Using Wisp

- **Where it is.** On a MacBook with a notch, hover over the notch to open it. On a Mac without a
  notch (M1 Air, 13-inch Pro, iMac) nothing is drawn: click the Wisp icon in the menu bar, or push
  the pointer against the top-centre edge of the screen. On an external monitor a small bar sits
  under the menu bar.
- **Three tabs: Ask, Brief and Day.** **Ask** is the conversation. **Brief** is a short summary of
  what needs you today, from your mail, texts, calendar and reminders. **Day** lays the rest of today
  out hour by hour.
- **Shortcuts.** **⌥Space** opens Ask from anywhere. **⌘⇧F** searches the document on
  screen (Smart Search). **⌘,** opens Settings. If another app already uses one of these, the
  setup tour says so, and the top of the screen always works.
- **Getting to know you.** Once Wisp can answer, it offers three optional questions (what to call
  you, your goals, how you like replies). Reopen it any time with **Tell Wisp About You...** in the
  menu-bar icon's menu.

### Connect Google (optional)

Wisp can read your Gmail and Google Calendar so Brief and Day cover them too, and your Drive, Docs,
Sheets, Slides, Chat, Tasks and Contacts so Ask can answer questions about them. It is one
sign-in, and everything it asks for is read-only.

- Open **Settings ▸ Google** and click **Connect Google account**. Before you have connected
  an account, the notch's Google row (**Connect Google**) does the same thing.
- **You have to be on the tester list.** For now Wisp's Google sign-in is in Google's "Testing"
  mode, so only Google accounts the developer has added can use it. If you see "Access blocked" or
  "This Google account isn't on Wisp's tester list yet", send the developer the address you tried.
- **Google shows an "unverified app" warning.** That is expected while Wisp is in testing. Choose
  **Continue** (on some screens **Advanced**, then **Go to Wisp**). Google then lists what Wisp wants
  to read. Gmail is required; you can untick the rest, and **Reconnect** in Settings adds them
  later. Wisp can't send, edit or delete anything in Google with this sign-in.
- **The Google tools set themselves up on your first connection.** Ask's Drive, Docs and other
  Google tools need a small download the first time, so connect while online. Nothing else needs
  installing: the tool that runs them (uv) is inside Wisp.
- **Sign-in expires every 7 days** while Wisp is in testing. Wisp sends a notification when it does.
  Open **Settings ▸ Google** and reconnect that account.
- **Disconnect & erase** in the same pane revokes Wisp's access and deletes that account's mail,
  events and to-dos from Wisp.
- A build without a built-in Google sign-in needs your own Google Cloud client instead. See
  [docs/google-cloud-setup.md](docs/google-cloud-setup.md).

### Your privacy

- **Local by default.** The AI model runs on your Mac. Your mail, calendar, messages and notes are
  read on the Mac and stored in `~/.moe`. Wisp doesn't send them to a cloud model unless you set up
  one yourself in Settings.
- **What does go out.** The search terms of a web question, plus the requests that connecting Google
  makes to Google. The model download comes from Hugging Face.
- **Getting to know you stays here.** Your answers live in `~/.moe/profile.json` and go only to
  Wisp's own model on this Mac, or to your own Mac on your tailnet if you set one up. They are never
  sent to an optional cloud model. (If you hand-edit the config to run all of Wisp on a cloud
  model, they go there with everything else.) **Erase everything** in the Wisp window's Memory ▸ About you
  deletes them.
- **It asks before sending.** Wisp prepares mail and message drafts and waits for you. Nothing is
  sent, and no event is changed in Calendar, until you approve it.

### Quit, uninstall and reset

- **Quit.** Click the Wisp icon in the menu bar and choose **Quit Wisp**. Quitting unloads the model
  and stops the engine, so the memory is free again. Closing a window does not quit Wisp.
- **Uninstall.** Disconnect any Google account first (**Settings ▸ Google ▸ Disconnect & erase**),
  quit Wisp, and drag **Wisp.app** from Applications to the Bin. To delete everything Wisp stored,
  including the 5.5 GB model, also delete the `~/.moe` folder. To remove the preferences and
  permissions, run `defaults delete com.wisp.assistant` and `tccutil reset All com.wisp.assistant`
  in Terminal.
- **Reset first run.** Quit Wisp, run `defaults delete com.wisp.assistant`, and open Wisp again to see
  the setup window again. Your chats, memory and downloaded model are kept.

### Logs and bug reports

- **Report a Problem...** in the menu-bar icon's menu (or `/report` in the notch) builds a report
  under the reply it is about. It is saved only where you choose and is never uploaded. Attach it
  to your bug report.
- **Export Chat Debug Log...** in the same menu saves the detailed log of the current chat.
- **Service log.** `$TMPDIR/moe-backend.log` (in Terminal: `open "$TMPDIR"`).
- **Engine log.** `~/.moe/engine/engine.log`.
- **Audit log of actions Wisp took.** `~/.moe/audit.jsonl`.

## What it does

**Chat overlay — ⌥Space.** A top-centre panel with three tabs (Ask, Brief and Day) that
adapts to your display. On a MacBook with a notch it hangs from the notch: collapsed it is a
small black bar wrapped around the camera housing, and hovering it opens the panel. On a Mac
without a notch (M1 Air, 13" Pro, iMac) nothing is drawn: the menu-bar icon is the anchor and
carries the status (working, due today, setup unfinished), and pushing the pointer against the
top-centre edge opens the same panel as a card under the menu bar. On an external monitor a
small capsule sits under the menu bar. Hover opens it only after a brief dwell, never while a
mouse button is held (so dragging a file past the notch doesn't trigger it), and never again
under a pointer that simply stayed put after you closed it. Layout follows the displays live,
so plugging in a monitor or changing resolution re-fits it. Streaming replies, markdown,
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

**Operating the Mac.** The agent loop drives about 90 built-in tools across these areas:

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
narrowed subset rather than the whole list — built from the request's read *and* write
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

Sync is background work, not a composer lock. If a tool or summary is still waiting
for fresh source data, new prompts are queued in order and sent automatically after
the active turn closes. Attachments stay bound to the prompt they were submitted
with, and the composer shows the queue count.

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
shell, where view-only strings cannot write — `full_access` auto-runs routine work,
and a safety floor blocks catastrophic commands (`rm -rf /`, `sudo`, disk wipes)
even then, so a prompt-injected model can't nuke the machine. A few things ask even
under `full_access`: every AppleScript, and any shell command that could send
something off the Mac, drive another app or read a key or saved sign-in. The card
shows the exact command or script. After Wisp has read a file, page, mail or message
in a turn, a Shortcut, a call, a web request or an action from an MCP server or
skill asks as well. Irreversible
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

### Which model for which Mac

Fresh installs use **Gemma 4 E4B** (4-bit with multi-token prediction, `Jundot/gemma-4-E4B-it-oQ4e-mtp`,
about 5.5 GB). It is the model the first-run window downloads, and the engine's per-model
settings for it (sampling, thinking, a 16k context window) are written by Wisp itself. The
Ling 3.0 tiny builds below remain available in the detailed setup guide (menu-bar icon,
**Finish Setup...** or **Model & Engine...**, or **Settings → Models → Setup guide...**).
Existing role overrides and working models are preserved. Mellum remains an optional choice,
marked validation pending. The guide ranks memory fit separately from test status:

| Memory | Recommended | Download | Notes |
| --- | --- | --- | --- |
| 24 GB or more | `gemma-4-E4B-it-oQ4e-mtp` | 5.5 GB from Hugging Face | The default. `Ling-3.0-tiny-oQ6e` (6.6 GB) is the best-quality alternative. |
| 16 GB | `gemma-4-E4B-it-oQ4e-mtp` | 5.5 GB | Leaves room for your other apps; close heavy ones. `Ling-3.0-tiny-oQ4e` (4.6 GB) is the smaller alternative. Tool selection measured 10/10 at 4, 5 and 6 bits (on a 24 GB Mac; not yet re-measured on 16 GB hardware). |
| Under 16 GB | not supported for this release | | Wisp may still offer the default. Expect it to be tight. |

Sizes are the published files' sizes on Hugging Face
(`mlx-works/Ling-3.0-tiny-oQ6e`, `mlx-works/Ling-3.0-tiny-oQ4e`). For Smart Search, also
install `mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ` (0.35 GB). Any other model oMLX
serves can be selected, but it is marked untested. Memory-fit rankings are estimates,
not load guarantees. Wisp preserves the engine's existing memory limits and profile;
a refused load requires an explicit retry after freeing memory or selecting a smaller model.

### Using another local inference app

| App | Default port | What Wisp uses it for |
| --- | --- | --- |
| oMLX | 8000 | Everything, including tool use and Smart Search |
| Ollama | 11434 | Chat and reasoning |
| LM Studio | 1234 | Chat and reasoning |
| llama.cpp (`llama-server`) | 8080 | Chat and reasoning |
| [MTPLX](https://github.com/youssofal/MTPLX) | 8000 | Chat and reasoning — but 8000 is oMLX's port and Wisp refuses it, so start MTPLX on another port |
| Any OpenAI-compatible app | your choice | Chat and reasoning |

Another app connects on `127.0.0.1` only, with no credentials, on a port other than
8000 or 8765. Wisp keeps **tool use on oMLX** until another engine has been shown to call
tools reliably, so a connected app answers reasoning requests but cannot send mail or
touch files. The connection is tested with a real streamed reply before it is saved. Reasoning
prompts are sent to that app, and Wisp cannot verify which program owns a port or what it does
with them, so connect only one you trust. Cloud providers are configured separately; see
[docs/inference-providers.md](docs/inference-providers.md).

## Run from source

```bash
# oMLX must be running (its menu-bar app auto-starts the server on :8000)
./scripts/run.sh                      # Wisp backend on :8765
```

Build a verified local candidate:

```bash
./scripts/wisp-build all --strict-toolchain --output dist/candidate
```

The candidate directory contains a ZIP with `Wisp.app` plus checksums, provenance,
release notes, and Simulation QA evidence. Development source changes do not alter an
installed app until a new candidate is packaged and installed.

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
from a hash-pinned lockfile, plus a private copy of the oMLX engine. The build never
installs or launches Wisp, verifies the sealed artifact, and keeps signing QA isolated
from simulation. Model weights are not part of the app; the first-run window downloads
them. Ad-hoc sealing carries no
team identity and is **not** notarization. See [docs/build-release.md](docs/build-release.md)
and the non-mutating release gate in [docs/SIMULATION_QA.md](docs/SIMULATION_QA.md).

## Model roster and inference providers

The router emits a **role**; `service/config/models.yaml` resolves it to an oMLX
model id. `~/.moe/config.yaml` overlays that at runtime (Settings writes here).

| Role | Model | Why |
| --- | --- | --- |
| `fast`, `router`, `coding`, `reasoning`, `agent`, `general` | `gemma-4-E4B-it-oQ4e-mtp` | one resident model for every text/tool role — no swap, no cold load, no routing decision that hinges on which model a request lands on |
| `research` | defaults to `coding`'s model unless overridden | Wisp Research's plan/query/extraction/synthesis calls; independently settable in Settings |
| `embedding` | `Qwen3-Embedding-0.6B-4bit-DWQ` | Smart Search's T2 retrieval tier; small enough (320 MB) to co-fit with the chat model, so search never evicts it |
| `reranker` | `Qwen3-Reranker-0.6B-mlx-6bit` | optional cross-encoder for tool retrieval; not the default, and it evicts the embedder before use so all three never sit resident together |

Generation roles can instead point to an explicitly configured OpenAI-compatible
endpoint. Wisp keeps embeddings and reranking local, bounds remote responses, and
does not expose arbitrary provider administration routes. Provider credentials belong
in the protected credential store, never YAML or debug exports. See
[docs/inference-providers.md](docs/inference-providers.md).

Quantization matters more than it looks: there is a hard reliability cliff below
4 bits. Measured over 10 reps on a 16-tool ambiguous menu — oQ3e 5/10, oQ3.5e
6/10, **oQ4e 10/10**, oQ5e 10/10, oQ6e 10/10. Below 4 bits the model picks the
wrong tool about half the time and answers confidently, which is silent.

Image attachments require a selected model and endpoint that support image input.
Idle local models auto-unload after a configurable timeout (default 5 minutes).

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
  - `setup/` guided-setup logic: hardware tiers, model catalog, engine detection, checklist
  - `errors.py` plain-language error translation · `debug_capture.py` Debug Mode
  - `codex_monitor.py` read-only watch on local Codex tasks
- `app/` — Swift menu-bar frontend (overlay, search panel, research windows,
  settings, OS readers, outbound sender).
- `mini/` and `infra/mac-mini/` — disabled-by-default remote inference and proactive-node infrastructure for a future private Mac mini deployment.
- `build-support/` — the sealed-candidate build pipeline and lockfiles.
- `scripts/` — run, packaging, evaluation, and icon helpers.
- `docs/` — see [docs/README.md](docs/README.md).

## State on disk

Wisp-owned state is stored under `~/.moe/`:
`config.yaml` (role overlay), `sessions.db`, `facts.db`, `assistant.db`,
`research.db` + `research_cache/`, `cache/` (Mail/Messages/Notes snapshots),
`gmail.json` (connected Google accounts), `engine/` (the built-in engine, its settings, its
downloaded models and `engine.log`),
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
