# Changelog

All notable changes to Wisp are documented here.

## [Unreleased]

- Fixed tool menus for requests that match no routing rule offering send, reply, call and bulk-delete tools when the request never asked to contact anyone or delete anything (for example "tell me a joke" or "do I have anything on Sunday"). Those tools stay available whenever the request does ask.
- Fixed a bare "sure", "ok" or "go ahead" to an assistant's offer (open a file, set a reminder) reaching the model with no tools at all once the conversation had earlier turns. A reminder offer still never creates a reminder from a bare assent: Wisp looks the event up and asks for the time.

## [1.2.0] - Pending qualification

### Added

- Added local problem reports: a metadata-only request journal and a reviewable, never-uploaded report with an opt-in detailed section, redacted on a best-effort basis. Today refreshes and background kinds keep their own small retention so they cannot evict chat traces. See docs/DEBUGGING.md.
- Added a local Codex/Claude coworker mailbox with correlated agent replies, acknowledgments and duplicate-delivery protection. All Git Worktrees share it without a background service or model polling.
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

- Fixed rejected study-reminder invitations still being able to produce a false creation promise after “sure” or a dated reply. Compound, quoted, completed and denied invitations now use the receipt guard with no write capability. Unrelated capability statements followed by a date question keep their original routing.
- Fixed tomorrow study planning returning another day or a whole-day window as a short study slot. Exact-day candidates use the local date and requested duration, account for Calendar event endings, and distinguish suggestions from saved events. A preparation reminder requires a dated confirmation; bare assent reports that no reminder was added, and only a successful receipt proves creation. Default/full-access mode allows the confirmed reminder; view-only mode denies it. Legacy availability calls retain their prior behavior. Study requests with time-of-day qualifiers or compounds retain the existing router.
- Fixed questions about your own named item, such as “what's my Amazon order” or “my Costco receipt,” searching only conversation history. When nothing more specific applies, Wisp now searches Apple Notes first and then past conversations; explicit “in my notes” and “what did I tell you” requests, other apps, multi-part requests, and general or third-party questions like “what's the order of the planets” keep their existing handling.
- Tomorrow planning now recognizes common spellings and phrases such as “any plans for tommorow?” and “what is on my do list tmrow.” Agenda reads use the exact next day; to-do requests check Calendar, Reminders, recent Messages, email and Notes before answering. Answers receive an explicit target date and use current source results instead of stale conversation memories, while preserving source exclusions.
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
- Fixed a freshly created Note not being found while an unrelated old record surfaced. A lookup for a named topic now needs every significant word to match (in a Note or a remembered conversation) instead of any one, a miss reports that no matching Note exists rather than showing unrelated recent ones, and Wisp asks the app for a fresh Notes read before answering. The reply states whether Notes was just re-read, or only a snapshot of a stated age was searched, and never claims a Note is absent when that snapshot may be out of date.

### Validation

- Destructive-delete confirmation: 29 checks passing, including the exact command from the incident, every recursive and wildcard variant, and proof that targeted single-file deletes still run unprompted. Verified live twice — a test folder survived both attempts to delete it.
- Move action and summary coverage: 17 checks passing. Measured against the real export that prompted this: 21 of the 30 messages shown came from one group chat, and the new per-conversation sampling surfaces every recent thread instead.
- Router-direct dispatch: 45 routing checks and 24 execution checks passing. Verified live — a battery question answers in 1.42s and a calendar lookup in 2.88s, while "summarize my inbox" completes with no agent-loop model call at all. Requests that must not pre-dispatch (compound device requests, qualified summaries, calendar writes and past-tense questions) all still take the ordinary route.
- Error translation: 14 checks passing, covering a stopped engine, both 400 shapes, and the unmapped fallback.
- Confirmation timeout: 8 checks passing, including that a real answer beating the clock still wins and a late answer is a harmless no-op.
- Daily Summary regression suite: 32 checks passing.
- Router scoping regression suite: 133 checks passing.
- Updated Python modules and dataset scripts compile successfully; the harvester dry-run discovers 84 flows, 117 runs, and 122 model-facing turns.
