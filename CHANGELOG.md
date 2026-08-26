# Changelog

All notable changes to Wisp are documented here.

## [Unreleased]

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
