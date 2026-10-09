# Changelog

All notable changes to Wisp are documented here.

## [Unreleased]

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
