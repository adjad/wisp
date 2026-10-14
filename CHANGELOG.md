# Changelog

All notable changes to Wisp are documented here.

## [Unreleased]

### Fixed

- Behavior learning now sees Messages. Every message was dropped on ingest (the Messages feed sends RFC 3339 timestamps and the runtime parsed them as numbers), so no contact rhythm could ever form. Replies now count toward the same person as their texts, named by Contacts, and automated senders (short codes, toll-free numbers, alphanumeric sender ids, business chats) are skipped.
- Web lookups (news, weather, stock and crypto prices, "search the web for…") now run on the model configured for the `agent` role instead of a model id written into the router. The written id (`Ling-3.0-tiny-oQ6e`) was no longer installed, so oMLX answered its load with 404, reloaded the resident model, and the next turns failed with "The local AI engine returned an error (409)".

### Changed

- Requests to the local oMLX engine no longer re-prove the engine from scratch each time. Wisp keeps one verified record per running oMLX for at most 60 seconds, re-checks the engine process natively before every request and every write, and still checks the listener once per connection and the connection's owner (lsof) before every write. A restart, an exec into another binary, the authorization file appearing or any check failure falls back to the full check. `WISP_ATTESTATION_LEGACY=1` restores the previous per-request check.

## [1.3.1] - 2026-10-12

### Added

- Added model-led routing behind `WISP_MODEL_LED_ROUTING=1` (off by default): the local model reads each request and picks tools from a capability catalog instead of the rules router. Remote-only mode, behavior questions and literal inputs keep the rules router, and sends still always ask first. It stays off by default because Ling V2 does not yet follow its tool-loading step reliably.

### Changed

- Pull-request CI runs a preview build and Simulation QA as parallel jobs, and Simulation QA runs test modules concurrently (`--jobs N`). Tags and releases still run the full strict build with every gate.
- Remote inference is now a generic, user-configured mode with no built-in server. It starts only when Wisp is launched with `WISP_REMOTE_ONLY=1` and `~/.moe/config.yaml` has a complete `inference.remote_only` profile (an HTTPS `base_url`, a `model_id`, a `keychain:<name>` credential and every request limit). With the flag set and no valid profile, the backend refuses to start. See docs/REMOTE_INFERENCE.md.
- Remote inference is now read/write. Every built-in tool that doesn't need Wisp's on-Mac models (so not `create_tool`, `use_skill`, `wisp_skills`, `wisp_mcp` or skill and MCP tools) runs under the normal safety policy: sends always confirm and can't be granted, writes ask once, always or never. This is what `WISP_REMOTE_FULL=1` used to turn on. The separate read-only remote mode is gone, along with its 403s on the behavior and approval endpoints. For a read-only Mac, use View Only, which still denies writes in remote mode. `WISP_REMOTE_FULL` is now ignored (it logs one deprecation line).
- Remote inference refuses a `base_url` with spaces, control characters or non-ASCII characters (a leading space or trailing newline used to pass).
- Behavior learning is now on by default. Turn it off with "Learn my routines" in Settings (Privacy & Access), which takes effect at once and keeps what was already learned, or launch with `WISP_BEHAVIOR=0`. `WISP_BEHAVIOR=1` or `0` overrides the saved setting. It still learns only from your calendar, reminders, message timing (never message text) and how you use Wisp, nudges at most 4 a day, acts automatically only on Wisp's own schedule, and keeps everything on this Mac. New endpoints: `GET/PUT /behavior/settings`.
- Behavior learning privacy controls: Settings has **Forget what Wisp learned** (`DELETE /behavior/data`, JSON body `{"confirm": true}`), which deletes the learned data and removes the untouched reminders and holds Wisp added on its own (anything you edited or completed is kept). The 180-day and 30-day age-out now runs while learning is off too. Automatic actions ask the safety policy like the `add_reminder` tool, so View Only turns them into offers. If `config.yaml` is unreadable, learning is treated as off and saving the setting returns a clear error. A pass already running when you switch learning off publishes and performs nothing. Under remote inference, nudge wording and event labeling are off by default (`WISP_BEHAVIOR_PHRASER=1` opts in), and the Settings text says what goes to your server.

### Removed

- Removed the GX10 server support: `brain_server/`, `deploy/gx10/`, the GX10 runbooks, the remote behavior brain (`WISP_BRAIN_MODE`, `WISP_BRAIN_URL`, `WISP_BRAIN_TOKEN`) and the pinned GX10 remote-inference profile.
- Removed the Intel Mac builds: the `WISP_REMOTE_ONLY_DEMO`, `WISP_FOUNDER_DEMO` and `WISP_REMOTE_FULL` app compile flags and their code paths.
- Removed every demo mode:
  - the founder demo and its fictional fixtures (`WISP_FOUNDER_DEMO`);
  - the Aria demo world (`WISP_ARIA_DEMO`, the `/demo/aria/*` endpoints, the menu toggle and the separate demo state directory);
  - the behavior engine's synthetic persona mode (`WISP_BEHAVIOR_SYNTHETIC_ONLY`, `POST /behavior/reset`);
  - the Catch demo (`WISP_ATTENTION_DEMO`). The proactive attention runner is unchanged;
  - the `demo/` tooling.

An older install may still have an unused `~/.moe/aria-demo/` folder from the Aria demo. Wisp no longer reads it, so you can delete it.

### Fixed

- `http_request` no longer follows redirects. A 307 or 308 used to re-send the request body and custom headers to a host you never approved; a redirect is now reported as not followed.
- "What do you know about me?" answers from what you asked Wisp to remember again, now that behavior learning is on by default. "What have you learned about me?" and questions about your habits still list learned routines.

## [1.3.0] - 2026-10-11

### Added

- Added proactive attention: Wisp watches incoming messages for plans or commitments that aren't on your calendar ("Can we do Thursday at 3?") and, in shadow mode (the default), records what it would do without acting. Opting in to live mode creates a verified Apple Reminder for each one, at most 3 a day, never 22:00–08:00, only when Wisp's safety policy allows the write (view-only blocks it), with Undo that only removes what Wisp created. Message quotes are cleared from its ledger after 7 days.
- Added `./scripts/wisp-build preview`: an installable, ad-hoc-signed preview build in minutes (no Simulation QA), never eligible for release.

### Changed

- Model-backed tests use Ling 3.0 tiny V2 (oQ4e).
- `correct_pattern` now asks before changing a learned pattern, and the behavior write endpoints are refused in read-only remote builds.
- Wisp is now a regular Mac app instead of a menu-bar-only one. It has a Dock icon, an app menu (Settings, Hide, Close Window, Quit) and a Window menu, opening it shows the Chat window, and clicking the Dock icon brings the window back. Closing the window leaves Wisp running in the notch and menu bar; ⌘Q quits it through the same shutdown as the menu-bar Quit. The notch and the menu-bar icon are unchanged.

### Fixed

- The Chat thread no longer pulls you back to the bottom while you scroll up to read.
- Fixed `wisp-build all --allow-dirty` failing its own final check on a clean checkout.

## [1.2.5] - 2026-10-11

Merges the Wisp hackathon work (adjad/Wisp_Hackathon) into Wisp.

### Added

- Added the behavior engine: Wisp learns routines from Calendar, Reminders, message metadata and what you ask it, predicts the day, and sends a few nudges (at most 4 a day, quiet 22:00–07:00). Patterns are deterministic statistics with evidence counts; the model only phrases them. Reversible actions (a reminder, a "Tentative:" hold on Wisp's own schedule) are done automatically with Undo; messages and meeting changes are only drafted. Off unless `WISP_BEHAVIOR=1`. Ask "What do you know about me?", "What will tomorrow look like?" or "Which meetings do I usually cancel?". See docs/BEHAVIOR_ENGINE.md.
- Added nudge cards and actionable notifications (Undo, Do it, Useful, Not now, Wrong) in the app.
- Added Catch: Wisp notices a plan stated in a text ("Dinner at 7, I booked a table") that isn't on your schedule and adds it to Wisp's schedule with an alert. Off unless `WISP_ATTENTION_DEMO=1`.
- Added Workflow20 grounded multi-source workflows with guarded action execution, a personal knowledge index, calendar planning, file organization and grocery-list tools.
- Added a remote-inference mode for Intel Macs: every model role runs on a paired GX10 (or other OpenAI-compatible server) over Tailscale with a Keychain token, while data and tools stay on the Mac. A read-only founder profile and a full profile (all tools with normal approvals, the scheduler and 32k context) are available.
- Added the Aria demo mode for remote builds: a fully synthetic, interactive demo world (mail, messages, notes, reminders, contacts, calendar) where sends land in synthetic threads with scripted replies and nothing leaves the Mac, with proactive items in the Daily Summary and the top bar.
- Added an optional brain server and GX10 deploy scripts (gateway supervision, 32k context) under brain_server/ and deploy/gx10/.
- Added the iPhone app foundation (ios/): WispCore, the Workflow20 contract and a local-inference interface.
- Added demo tooling: `demo/seed_mac_persona.py` and `demo/wisp_demo.py` (persona seeding, behavior and Catch demos, rehearsal reset).

- Added the Wisp Chat window: a real, resizable window for long conversations, opened from the menu-bar menu ("Open Wisp Chat") or from the notch panel's new "Open in Wisp Chat" button, which continues the current conversation there. A sidebar lists every saved chat by day with search and delete; the thread shows which tools Wisp used above each answer; confirmation cards and message drafts work as they do in the notch, in the thread; and a details panel (⌘I) shows the route, model, speed, and tools for any answer. It follows the Mac's light or dark appearance. Today, Research, Memory, Search, and Settings open from its sidebar. See docs/CHAT_WINDOW.md.
- Added `GET /chats`, a conversation list with a title (the first prompt) and a one-line preview for each saved chat.

### Fixed

- Fixed the sandboxed Today and Diagnostic Report contract gates under Xcode 27, where SwiftUI's `@State` is a compiler macro whose plugin server cannot start inside Simulation QA's sandbox. Those scripts now ask `swiftc` not to add a second, nested sandbox when it supports the flag; the outer sandbox is unchanged.

## [1.2.0] - 2026-10-06

### Added

- Added local inference apps (Ollama, LM Studio, llama.cpp, MTPLX) as tool-capable engines. Wisp measures the context window the app really honors, runs a synthetic tool-calling probe on the server, and only then lets the app drive Agent and Coding work. Settings has a "Test Tool Calling" check with per-check results and the exact fix when one fails.
- Added local problem reports: a metadata-only request journal with stage timing, and a reviewable report that is never uploaded. Detailed content is optional, shown in a preview, and redacted on a best-effort basis. Today refreshes and background kinds keep their own small retention so they cannot evict chat traces.
- Added a persistent debugging timeline and an opt-in capture of Today planner inputs for deterministic replay (see docs/DEBUGGING.md).
- Added undated Apple reminders to reminder search: incomplete reminders with no due date are now synced and listed.
- Added an inbox digest sorted by what needs attention (people first, then automated senders), replacing the dense statistics paragraph and per-sender lists.
- Added a local coworker mailbox for correlated agent messages with acknowledgments and duplicate-delivery protection.
- Added a release performance gate and benchmark harness: publication requires attributable, live-measured performance evidence for the exact release commit. The harness's first live runs happened during this release and could not yet produce a passing receipt on the build host, so 1.2.0 is published under an explicit, committed waiver limited to this version (see docs/RELEASE_PERFORMANCE_BENCHMARK.md#waiver); a passing benchmark is a 1.3 requirement.

### Changed

- The notch is easier to open: the top edge of the screen now counts as part of the hover zone, and most of the notch (all but the bottom strip) opens the menu.
- Mail history is read from Mail's local index when Full Disk Access is granted, with no Apple Events, and otherwise synced incrementally instead of re-reading two years on every pass. Gmail accounts, whose Inbox is a label, are included.
- A native source (such as Reminders) that has been syncing for 90 seconds without progress is reported as unavailable with the exact next step instead of reading "syncing" forever.
- Engine errors, memory limits and over-long conversations explain themselves in plain language, with the technical reason kept in the debug detail.
- Tool routing is measured: a frozen 361-prompt corpus and a CI ratchet now guard routing quality, which rose from 64% to 82% on that corpus (see docs/ROUTING_DIAGNOSIS.md). Fallback menus no longer offer send, reply, call or bulk-delete tools unless the request asks for them.
- Wisp starts inference with one bounded engine status read and reuses a recent proof of which model is resident across the steps of a turn, instead of asking the engine before every step. A simple greeting now makes 2 engine calls instead of 3. The proof is dropped on any model change Wisp makes or observes, and is re-checked at least every 10 seconds, so a change made outside Wisp is noticed within that bound.

### Performance

- Verifying the local oMLX engine before each request does the same checks with the same verdicts in less time: the engine's Python tree is read with `scandir` (2.2x faster on the real 47,320-entry tree), the independent process inspections run concurrently (about 1.4x on a request-shaped total), and executable paths use the native `proc_pidpath` call instead of spawning `lsof`.
- Routing reuses each exact query's lexical ranking within a request (about 1.9x faster routing for ambiguous requests).

### Security

- Fixed an unreliable "Local inference peer attribution unavailable" refusal on about half of live turns when other clients were connected to the engine.
- Pasted API keys and similar secrets in Agent chats are redacted before they reach the turn store, the audit log or a model. Keys already stored in older history are not rewritten and should be rotated.
- Approvals are strict: only an exact `true`/`false` is accepted, and an answer can only resolve the request that raised the confirmation.
- Launch no longer terminates unrelated programs on Wisp's ports, and the app verifies the backend's identity before trusting it.
- The protected-path floor now checks every path argument (including lists, symlinks and `/private` aliases), and an MCP server can no longer mark its own tool read-only.
- "Always allow" can no longer be granted for tools that must confirm every time (calendar writes, outbound actions, network and tool-authoring tools).
- A sync can no longer erase reminders you created in Wisp.

### Fixed

- Fixed authored emails and texts ("email Sam asking to move our meeting") being compiled into data deliveries that mailed the user's calendar.
- Fixed everyday prompts that dead-ended or routed to the wrong tools, including "summarize my emails and text it to Mom".
- Fixed a closed connection leaving a turn "running" forever.
- Fixed tool menus for requests that match no routing rule offering send, reply, call and bulk-delete tools when the request plainly asks for neither.
- Fixed a bare "sure", "ok" or "go ahead" to an assistant's offer reaching the model with no tools. An assent to an offer cannot create a reminder.
- Fixed "any new emails?" and "any new texts?" being answered with a web search, and email write requests offering the file "move to Trash" tool.
- Fixed "draft a reply … but don't send it" losing the draft tool along with the send tools.
- Fixed common typos and shorthand ("tmrw", "calender", "txt", "remeber") sending a request to a broad, generic tool list, without ever rewriting recipients, message text or file names.
- Fixed tomorrow planning ("any plans for tommorow?", "what is on my do list tmrow") using another day or stale memory, and study-slot requests returning a whole free window as a short slot.
- Fixed rejected study-reminder invitations producing a false creation promise after "sure" or a dated reply.
- Fixed a freshly created Note not being found while an unrelated old record surfaced, and "what's my Amazon order" searching only conversation history.
- Fixed long-overdue Wisp-only records reading as current reminders (they are left out of search and counted, never deleted).

## [1.1.5] - 2026-09-30

### Added

- Added a confirmation step for shell commands that delete irreversibly — recursive deletes, wildcard deletes, and their equivalents. These now always show the exact command for approval, in every access mode including full access, and cannot be pre-approved with "always allow". Deleting a single named file still runs without prompting.
- Added a move action for files and folders, so reorganizing, sorting, and filing things away no longer depends on shell commands. It creates the destination folder as needed and refuses to overwrite anything that already exists.
- Added router-direct dispatch: when a routing rule resolves a tool call in full — name and arguments both — Wisp now runs it before the first model call instead of spending a model step re-deriving it. Covers zero-argument device reads, calendar-only lookups with the window resolved in Python, and unqualified inbox or message summaries.
- Added an error-translation layer so a failed request explains itself in plain language — a stopped engine, a memory limit, or an over-long conversation each get their own sentence and, where one exists, a next step. The raw error is kept in the debug export rather than shown as the headline.
- Added a regression suite for Daily Summary fallbacks, live mail-cache row shapes, prompt leakage, prompt-only glyph cleanup, and completion-token headroom.
- Added a shape-stable `header_rows()` API for cached email metadata so cross-module consumers can read named fields without depending on internal tuple layouts.
- Added a training-data harvester that runs prompts through the real router, agent loop, and tool pipeline, then records successful trajectories as SFT examples and routing/tool-selection failures as DPO pairs.
- Added guarded self-target execution, dry-run and no-execution modes, resumable flow selection, and compound multi-tool workflow coverage to the training harvester.
- Added a dataset merge utility that combines harvested and real-usage examples while removing samples contaminated by harness-generated denial messages.

### Changed

- Daily Summary now separates mail from people from newsletters, notifications, and other automated senders. Automated mail is reduced to an optional sender/count roll-up instead of competing with actionable messages.
- Daily Summary email context now preserves unread state, includes account labels when multiple accounts are present, uses the last 24 hours when available, and falls back to the newest messages on quiet days.
- Daily Summary and message prompts now use explicit output skeletons and placeholder-only examples, with tighter rules for dates, attribution, section structure, reply status, and grounding.
- The deterministic Daily Summary fallback now prioritizes human mail, groups automated arrivals separately, and renders calendar, email, and message data in a user-facing format.
- The macOS client now allows up to 240 seconds for Daily Summary generation, covering cold model loads without presenting them as immediate failures.
- Real-usage LoRA dataset generation now re-runs the production router for each triggering user turn and stores the same routed tool subset the model would receive at inference time.
- Email and message routing now recognizes self-directed requests such as “email me,” “text me,” “email it to me,” and “text it to me,” enabling chained read-then-send workflows.

### Fixed

- Fixed reorganizing files being able to delete them. Wisp had no way to move a file, so a request to tidy a folder fell back to raw shell and could remove the originals instead of relocating them. Moving is now a first-class action, and it cannot delete anything.
- Fixed message summaries hiding entire conversations. The recent view took the newest messages across all chats at once, so a single busy group chat could fill the whole window and quieter threads never reached the summary. Recent messages are now sampled per conversation, and any conversation still left out is named rather than silently dropped.
- Fixed inbox summaries implying they covered everything. A summary of the most recent mail now says how much of the inbox it actually looked at, and how to ask for more.
- Fixed confirmation prompts hanging indefinitely when left unanswered. An unanswered prompt now expires after five minutes and is declined, since a stalled prompt also held back the daily brief and profile updates for as long as it sat there. An expired prompt is cleared from the window rather than left waiting on an answer that no longer has anywhere to go.
- Fixed Daily Summary failures caused by positional unpacking after cached email rows gained a read/unread field.
- Fixed inflated inbox counts and repeated brief items by collapsing byte-identical cached mail-header lines before downstream processing.
- Fixed the Daily Summary endpoint so an unavailable local model returns a displayable deterministic brief instead of an unhandled server error.
- Fixed on-demand and scheduled summaries so source, cache, or model failures degrade to a non-empty fallback rather than dropping the entire brief.
- Fixed empty or truncated model responses leaking raw prompt scaffolding and source rows into the user-facing summary.
- Fixed realistic examples in summary prompts being copied as fabricated events or message details by replacing them with non-content placeholders.
- Fixed unread bullets and message-routing markers leaking from prompt context into rendered summaries.
- Fixed message-summary failures from preventing calendar and email sections from being generated.

### Validation

- Destructive-delete confirmation: 29 checks passing, including the exact command from the incident, every recursive and wildcard variant, and proof that targeted single-file deletes still run unprompted. Verified live twice — a test folder survived both attempts to delete it.
- Move action and summary coverage: 17 checks passing. Measured against the real export that prompted this: 21 of the 30 messages shown came from one group chat, and the new per-conversation sampling surfaces every recent thread instead.
- Router-direct dispatch: 45 routing checks and 24 execution checks passing. Verified live — a battery question answers in 1.42s and a calendar lookup in 2.88s, while "summarize my inbox" completes with no agent-loop model call at all. Requests that must not pre-dispatch (compound device requests, qualified summaries, calendar writes and past-tense questions) all still take the ordinary route.
- Error translation: 14 checks passing, covering a stopped engine, both 400 shapes, and the unmapped fallback.
- Confirmation timeout: 8 checks passing, including that a real answer beating the clock still wins and a late answer is a harmless no-op.
- Daily Summary regression suite: 32 checks passing.
- Router scoping regression suite: 133 checks passing.
- Updated Python modules and dataset scripts compile successfully; the harvester dry-run discovers 84 flows, 117 runs, and 122 model-facing turns.
