# Wisp Full-Codebase Bug Audit — 2026-09-09

- **Audit base:** `c3b5afe26a1744dd7927586262be0533322443af` (`origin/main` at audit start)
- **Initial-publication main:** `f20fb800fddc714d7d3b8d08a489dae4c81ef483` (six commits ahead of the audit base)
- **Correction-time main:** `51fa3ec937df7a19961fbb2103a7654bb058ec74` (15 commits ahead; includes the original audit merge and later Research Library/UI-concept merges)
- **Audit mode:** read-only product review; only this report was added
- **Review status:** the original report was merged before independent review issued a BLOCK on `1050cf932069136d5cd0237e9fd772cc2c204636`; this follow-up addresses that complete P2/P3 batch and requires fresh review

**Confidence labels:** **Confirmed bug** means a safe reproduction or deterministic failing control flow was established. **High-confidence risk** means the defect follows directly from the code but exercising the final effect would have touched real user data, accounts, notifications, or processes. **Test gap** identifies missing or unsafe verification rather than a proven production failure.

## Findings

Findings are ordered by severity, then by expected user impact. The `C-*`, `H-*`, and `M-*` labels are stable finding IDs retained across review revisions, not severity codes. Compiler deprecations, style concerns, UI mockup-only observations, and the separately owned conversation-memory, email-hardening, and autonomous-improvement workstreams are not reported unless they expose a distinct cross-cutting defect.

### Urgent high-priority (P1)

#### C-1 — View-only shell allowlist permits arbitrary mutation

- **Status/confidence:** Confirmed bug; high severity (P1); very high confidence. The demonstrated path requires an agent/tool call to reach `run_shell`; a blanket critical/P0 classification is not established.
- **Evidence:** `service/safety/policy.py:32-38,53-64,400-407`; `service/tools/builtin.py:160-163`.
- **Trigger:** Supply a shell string that starts with an allowlisted read command but delegates to an interpreter or shell metacharacter the mutation blacklist does not recognize. Examples include `env python3 -c "...write_text(...)"`, `python3 --version; python3 -c "...write(...)"`, and `find ... -exec python3 -c "...write(...)"`.
- **Impact:** Once a prompt-injected or mistaken tool call reaches `run_shell`, view-only mode does not prevent it from writing or deleting files or performing arbitrary local actions without confirmation.
- **Root cause:** The policy treats a prefix regex plus a finite mutation-token blacklist as proof that a raw shell string is read-only, then `run_shell` executes that raw string with `shell=True`.
- **Safe reproduction:** Independent review ran the exact AST-extracted `run_shell` implementation against two commands allowed under `read_only=True`/`full_access=False`; both wrote only fixture markers inside a temporary directory. No user path was touched.
- **Recommended fix:** Never auto-approve a shell program string. Parse and execute a single argument vector without a shell, validate the complete vector against command-specific read-only schemas, and reject metacharacters, interpreter delegation, `env` command execution, and `find -exec/-delete`. Add adversarial tests for chaining, substitutions, interpreters, and indirect execution.

#### C-2 — Legacy AssistantStore migration strands or silently corrupts commitments

- **Status/confidence:** Confirmed bug; high severity (P1); very high confidence. The affected installed-schema population cannot be derived from the squashed repository history, so a blanket critical/P0 classification is not established.
- **Evidence:** `service/assistant/store.py:141-151,171-202`.
- **Trigger:** Open a database with the former `UNIQUE(source, source_id)` layout, either before the later columns exist or after `organizer` and `account` were appended in historical physical order.
- **Impact:** On the 14-column layout, construction raises `OperationalError`; the new `commitments` table is empty while user data remains stranded in `commitments_old_migrating`, and a later startup can proceed with an apparently empty schedule. On the appended 16-column layout, startup succeeds but dates, status, context, and other fields are silently reassigned before the original table is dropped.
- **Root cause:** The rebuild performs positional `INSERT ... SELECT *` rather than mapping fields by name. A 14-column legacy table fails against the 16-column replacement. If the existing additive migrations append `organizer` and `account` first, the resulting 16-column physical order still differs from the replacement schema, so the copy succeeds while assigning values to the wrong fields. The multi-step DDL/data move also lacks explicit rollback and interrupted-migration recovery.
- **Safe reproduction:** A temporary 14-column legacy database with one commitment raised `table commitments has 16 columns but 14 values were supplied`; afterward the live table held zero rows and the renamed table held the original row. Independent review also reproduced the appended 16-column layout: migration completed, silently placed values such as location/time/status into the wrong columns, and dropped the original table.
- **Recommended fix:** Use one explicit transaction and an explicit source-to-target column list for every historical layout; never use positional `SELECT *` and do not treat “add columns first” as a safe alternative. Validate preserved values as well as row counts before dropping the old table, retain `notify_log` relationships, and recover or roll back if `commitments_old_migrating` already exists. Add 14-, 15-, and 16-column fixtures, including appended-column order, interrupted migration, and second-start recovery.

### High

#### H-1 — Standing grants bypass the “always confirm” calendar invariant

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/safety/grants.py:46-56,125-145`; `service/safety/policy.py:334-340,367-371`; `service/tools/assistant_tools.py:492,630,737`.
- **Trigger:** Choose “Always allow” for `update_event`, `clear_past_reminders`, or `clear_reminders`, then invoke it again.
- **Impact:** Later calendar changes or bulk reminder deletion execute without review even though `calendar_write` is documented as always-confirm.
- **Root cause:** The tool-name denylist omits those calendar tools, and grant evaluation returns `ALLOW` before the calendar confirmation block. `_ALWAYS_CONFIRM` at the grant check is a different, incomplete category set.
- **Safe reproduction:** With an in-memory grant cache, each omitted tool was grantable and changed from `CONFIRM` to `ALLOW`; `add_calendar_event` and `cancel_event` remained non-grantable.
- **Recommended fix:** Make grantability category-aware and evaluate all always-confirm categories before grants. Add a registry-wide invariant test proving no tool in an always-confirm category can store or consume an allow grant, including future tools.

#### H-2 — The protected-path floor ignores most path-bearing arguments

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/safety/policy.py:66-70,248-264`; `service/tools/builtin.py:495-559`; `service/tools/files_tools.py:348-405,470-533`.
- **Trigger:** In full-access mode, move a protected source or write to a protected destination through a filesystem tool whose arguments are named `source`, `destination`, or `archive_path`; terminal protected directories such as `~/.ssh` also miss the `.ssh/` regex.
- **Impact:** Protected credentials or system-sensitive files can be moved out of place, or new/copy/archive output can be written into protected locations, despite the documented unconditional write/delete floor. Merely reading a protected source for copy/archive does not by itself violate that floor.
- **Root cause:** `_hard_deny` checks only `args["path"]`, does not normalize/canonicalize paths, and does not inspect list elements, both endpoints, or symlink-resolved parents.
- **Safe reproduction:** A policy-only `move_path` from `~/.ssh/id_rsa` returned `ALLOW`; protected `destination` and `archive_path` arguments likewise bypass the check because it reads only `args["path"]`. The equivalent direct `write_file(path=...)` was denied. Copying/archiving from a protected source was removed from the violation evidence because that is a read, not a protected-path write/delete.
- **Recommended fix:** Centralize tool-specific extraction of every path-bearing argument, expand and resolve paths safely, inspect sources, destinations, list elements, archive targets, and parents, and enforce the same boundary inside primitives. Cover exact directory endpoints and symlink traversal.

#### H-3 — An MCP server can self-certify a mutating tool as read-only

- **Status/confidence:** Confirmed policy bug; high confidence.
- **Evidence:** `service/mcp/__init__.py:299-311`; `service/safety/policy.py:462-471`.
- **Trigger:** A configured MCP server annotates a mutating tool with `readOnlyHint: true`.
- **Impact:** The tool becomes `mcp_read` and auto-runs even in view-only mode. A malicious, compromised, or simply incorrect server can bypass Wisp’s action confirmation boundary.
- **Root cause:** The server-controlled annotation is treated as authoritative even though the adjacent comment calls it a claim rather than a guarantee.
- **Recommended fix:** Treat third-party MCP annotations as presentation hints only. Default every MCP tool to confirmation unless the user explicitly trusts an exact server/tool capability or Wisp independently verifies an allowlisted manifest. Add a fixture server that lies about a delete tool.

#### H-5 — Overlapping turns can cross-authorize calendar batches and corrupt UI state

- **Status/confidence:** Confirmed bug from deterministic backend and UI control flow; very high confidence.
- **Evidence:** `service/agent/loop.py:2010-2033`; `service/main.py:1363-1372`; `app/Sources/WispApp/OverlayModel.swift:243-248,281-311,314-318,396-404`; `app/Sources/WispApp/OverlayView.swift:212-214`.
- **Trigger:** Submit after the first answer token appears, submit while a confirmation is displayed, or click New Chat during an active stream; have two same-session requests produce multi-change calendar confirmations.
- **Impact:** Both streams mutate shared answer/activity/session/pending state. A stale stream can repopulate a cleared chat or erase a newer card, while approving the newer-looking card can authorize the older batch.
- **Root cause:** `isProcessing` becomes false during nonempty streaming and confirmation, the stream `Task` is not retained/cancelled/request-tagged, and every batch confirmation uses the constant action ID `batch_calendar` although the backend router assumes global uniqueness and resolves the first match.
- **Safe reproduction:** Two stub approvers produced the same action ID; registry-order resolution approved the older request while the newer request remained pending.
- **Recommended fix:** Maintain a request-in-flight state through confirmation and completion, retain/cancel the stream task, generation-tag every callback, and make approval IDs cryptographically/request-scoped unique. New Chat must cancel or explicitly resolve pending approval state.

#### H-6 — App launch sends `SIGTERM` to unrelated listeners

- **Status/confidence:** Confirmed bug; very high confidence. No signal was sent during the audit.
- **Evidence:** `app/Sources/WispApp/PortGuard.swift:3-16`; `app/Sources/WispApp/AppDelegate.swift:42-53`; `app/Sources/WispApp/BackendManager.swift:7-38`.
- **Trigger:** Launch Wisp while any process listens on 8765, or a process outside the broad oMLX prefix exemptions listens on 8000.
- **Impact:** Wisp silently terminates unrelated development servers or applications, risking unsaved work. A second Wisp launch can kill the first backend and then race against its shutdown.
- **Root cause:** Ownership is inferred only from a port and executable prefix; the 8765 call has no exemptions and unconditionally calls `kill(pid, SIGTERM)`.
- **Recommended fix:** Use a single-instance lock and an authenticated identity/version handshake. Adopt a compatible Wisp backend or show a port conflict. Only terminate a persisted child PID whose executable, start identity, and per-launch nonce prove Wisp ownership.

#### H-7 — Calendar and Reminder mutations report success without native acknowledgement

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/assistant/hub.py:9-23`; `service/tools/assistant_tools.py:404-427,452-507,553-590`; `app/Sources/WispApp/CalendarReader.swift:142-190`.
- **Trigger:** The Swift app is disconnected, native authorization is denied, EventKit throws, or the connection fails between the delete and recreate halves of an update.
- **Impact:** Users receive false Added/Cancelled/Updated receipts; local and native stores diverge; `update_event` can delete the original and fail to create the replacement. The returned two-line result can also be classified as successful/read because `service/tools/registry.py:60-108` omits `update_event` and does not recognize the second-line failure.
- **Root cause:** These tools publish to an in-memory subscriber bus with no receipt, mutate local state immediately, and implement update as cancel-then-create without prevalidating the new event or providing atomic native semantics.
- **Safe reproduction:** `Hub.publish()` with zero subscribers completed normally while `add_calendar_event` returned “Added.” A monkeypatched update that “cancelled” first and then received an invalid date returned a cancellation plus error and classified as `ToolOutcome(status='succeeded', effect='read')`.
- **Recommended fix:** Route every native mutation through a durable acknowledged outbox, prevalidate before dispatch, implement native update atomically, and advance local state only after a matching native receipt. Add explicit `update_event` outcome mapping and receipt validation.

#### H-8 — Cancelling one recurring event can remove the wrong occurrence

- **Status/confidence:** Confirmed payload bug; very high confidence.
- **Evidence:** `service/tools/assistant_tools.py:559-590`; `service/main.py:1258-1277`; `app/Sources/WispApp/CalendarReader.swift:159-190`; `tests/test_sandbox_world.py:226-252`.
- **Trigger:** Cancel a single visible occurrence in a recurring series whose occurrences share EventKit’s source identifier.
- **Impact:** The Swift bridge may delete an arbitrary representative occurrence while Wisp dismisses the selected local row.
- **Root cause:** `_retire` publishes only `source_id`; the native bridge needs `when_ts` to disambiguate. The REST delete path includes the timestamp, and the sandbox regression test manually supplies it, masking the production caller gap.
- **Recommended fix:** Include the exact occurrence timestamp and stable occurrence identity in every delete request, require native acknowledgement of identifier plus start time, and add an end-to-end production-caller contract test.

#### H-9 — The sync endpoint can erase all manual Wisp reminders

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/main.py:1024-1056`; `service/assistant/store.py:261-318`.
- **Trigger:** POST `/assistant/sync/calendar` with `source: "manual"` and an empty event set.
- **Impact:** All user-created manual commitments are deleted as absent from an “authoritative” replace-set.
- **Root cause:** The endpoint accepts arbitrary source names and passes them to a store method that deletes every active row for that source not present in the input.
- **Safe reproduction:** A manual reminder in a temporary database was present before `sync_source("manual", [])` and absent afterward.
- **Recommended fix:** Allowlist native replace-set sources (`calendar` and `reminders`) in both the HTTP request model and store boundary; reject reserved/local sources independently of client input.

#### H-10 — A malformed sync batch leaves a partial transaction and database lock

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/main.py:1040-1055`; `service/assistant/store.py:279-317`.
- **Trigger:** A batch contains one valid item followed by a value SQLite cannot bind, such as a dictionary in `context`, `organizer`, `account`, or `location`.
- **Impact:** Earlier rows stay visible in the open transaction, other connections receive `database is locked`, and a later unrelated commit can make the partial synchronization durable.
- **Root cause:** The batch is neither fully validated before writing nor protected by rollback-on-exception transaction handling.
- **Safe reproduction:** The second item raised `ProgrammingError`; `connection.in_transaction` remained true, the first row was visible on that connection, another connection’s write was locked, and a later unrelated commit persisted the partial row.
- **Recommended fix:** Parse and validate the complete payload first, then apply it in an explicit transaction that rolls back on every exception. Add cross-connection failure tests.

#### H-11 — Dedupe hides distinct events and cancellation mutates the hidden rows

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/assistant/store.py:60-80,205-258,423-429`; `service/tools/assistant_tools.py:541-590`.
- **Trigger:** Two independent events or reminders share a normalized title and minute, for example “Busy” in Work and Personal calendars.
- **Impact:** One disappears from reads. Cancelling or updating the visible result can mutate/delete every `duplicate_id`, affecting unrelated calendars or commitments from one approval.
- **Root cause:** Identity is only `(casefolded title, minute)`; source, account, source ID, and verified mirror linkage are ignored. `_retire` deliberately expands mutations across the collapsed group.
- **Safe reproduction:** Two temporary Calendar rows titled “Busy” at the same timestamp remained two SQLite records but `upcoming()` returned one record with the other in `duplicate_ids`.
- **Recommended fix:** Collapse only proven manual↔Apple Reminder mirrors using durable linkage. Never collapse same-source/same-kind collisions solely from title/time, and mutate exact stable IDs.

#### H-12 — Reminders, briefs, and missed-send notices are consumed before delivery

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/assistant/reminders.py:40-71`; `service/assistant/scheduler.py:79-84,131-155,219-228`; `service/assistant/brief.py:1417-1428`; `service/assistant/outbound_queue.py:161-174`; `service/assistant/hub.py:21-23`.
- **Trigger:** A due reminder, daily brief, or missed scheduled-send notice fires while no `/assistant/events` subscriber is connected.
- **Impact:** The user permanently misses reminders, a brief can be suppressed for the day, and a send that never occurred can fail without warning.
- **Root cause:** Durable “notified/done/missed” state is advanced before or regardless of publishing through a zero-buffer, zero-ack Hub. Publishing to zero subscribers succeeds.
- **Safe reproduction:** With zero subscribers, the first `due_reminders()` call returned one and marked it notified; `publish()` discarded it; the next due check returned zero.
- **Recommended fix:** Use a durable notification outbox with stable notice IDs, replay, and explicit client acknowledgement. Do not mark a reminder/brief/missed notice delivered until that acknowledgement; the unknown-send replay code at `scheduler.py:141-155` is a partial model.

#### H-13 — Scheduled outbound approval omits recipient, content, and delivery time

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/tools/action_tools.py:46-48,102-140,468-500`; `service/agent/loop.py:2310-2340`; `app/Sources/WispApp/OverlayModel.swift:7-25,598-606`; `app/Sources/WispApp/OverlayView.swift:431-451`; `tests/test_latency_prompt_contract.py:254-258`.
- **Trigger:** The agent proposes `schedule_send` (and similarly `forward_email`).
- **Impact:** The only authorization for a later unattended send can be approved without the card showing whom it will reach, what it will say, or when it will send.
- **Root cause:** `confirm_preview` omits these always-confirm outbound tools, while the card shows only a generic policy reason and optional preview. Existing latency contract coverage explicitly expects `None`, codifying the omission.
- **Recommended fix:** Generate a structured preview for every always-confirm outbound tool and show channel, all recipients, subject/body, local time, and timezone. Add a registry parity invariant so any new outbound tool fails tests until it supplies a reviewable preview.

#### H-14 — Multi-recipient scheduled email sends only to the first address

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/tools/action_tools.py:535-564`.
- **Trigger:** Schedule an email with two or more comma/semicolon-separated recipients.
- **Impact:** Only the first recipient is queued, while the display and success receipt name the whole original list; users believe delivery was scheduled for everyone.
- **Root cause:** The function parses all recipients but assigns and persists only `recipients[0]`.
- **Safe reproduction:** A monkeypatched queue captured `a@example.test` while the returned display/receipt still included both `a@example.test` and `b@example.test`.
- **Recommended fix:** Either reject multiple recipients with a clear error or persist one durable child action per validated recipient under an atomic parent operation. Receipts and later status must enumerate the actual queued destinations.

#### H-15 — Lost native send receipts create false failures and duplicate-send risk

- **Status/confidence:** High-confidence risk; high confidence. A live send/fault injection was intentionally not performed.
- **Evidence:** `app/Sources/WispApp/OutboundSender.swift:80-92,95-127,415-426`; `service/assistant/outbox.py:37-56`; `service/assistant/scheduler.py:157-175`.
- **Trigger:** Mail or Messages completes the send, but the fire-and-forget result POST is lost, the backend restarts, or the request times out after native dispatch.
- **Impact:** Wisp reports failure, which can lead the user to request a resend of a message that was already delivered. The scheduler marks a timed-out scheduled row failed; it does not automatically retry failed/unknown rows because `due()` excludes them.
- **Root cause:** Native sending and receipt persistence are not atomic. The Swift app neither stores pending receipts nor observes/retries HTTP responses; the backend converts timeout into ordinary failure, removes its waiter, and cannot distinguish “not sent” from “sent but receipt lost.”
- **Recommended fix:** Give every effect an idempotency key, durably persist native completion before acknowledging it, retry result delivery with response handling, and classify any post-dispatch timeout as `unknown`, not failed/safe-to-retry.

#### H-16 — Sandbox port configuration can bridge sandbox actions into real accounts

- **Status/confidence:** High-confidence risk; high confidence. It was not exercised because it could send or mutate real data.
- **Evidence:** `sandbox/run.sh:6-34`; `app/Sources/WispApp/WispClient.swift:24-26,274-288`; `app/Sources/WispApp/AppDelegate.swift:47-53`.
- **Trigger:** An already-running/reconnecting real Wisp app exists, its normal backend is absent, and the sandbox is launched with `WISP_BACKEND_PORT=8765` while that port is free.
- **Impact:** The real Swift subscriber can receive sandbox-approved Mail, Messages, Calendar, or Reminder actions and execute them against real user accounts.
- **Root cause:** The launcher documents but does not enforce the production-port prohibition. The real client trusts any server on hardcoded localhost:8765; PortGuard only runs during app launch and cannot protect an already-running app.
- **Recommended fix:** Hard-reject 8765 and any non-approved backend port in the sandbox launcher/server, then require a per-instance authenticated handshake and an explicit sandbox/production mode identity before subscribing or executing effects.

#### H-17 — Browser History privacy toggle can take 30 minutes to take effect

- **Status/confidence:** Confirmed bug from the Swift/Python state contract; high confidence. No real browser history was read.
- **Evidence:** `app/Sources/WispApp/SettingsView.swift:116,209-227`; `app/Sources/WispApp/BrowserHistoryReader.swift:33-64`; `service/assistant/sync_status.py:99-112`.
- **Trigger:** Disable or enable Browser History after startup retry scans have completed.
- **Impact:** After disabling, the UI says Wisp does not read history while the backend remains enabled and cached browsing remains queryable until the next 30-minute tick. After enabling, on-demand source checks can reuse the terminal disabled state and not request a scan.
- **Root cause:** The toggle only updates `UserDefaults`; the reader rereads it during scheduled `sync()`, and terminal source status short-circuits `ensure_sources()`.
- **Recommended fix:** Send toggle changes directly to the reader/backend, invalidate access to cached results immediately on disable, publish disabled state synchronously, and start a scan immediately on enable.

#### H-18 — Revoked or emptied Contacts never clear persisted contact data

- **Status/confidence:** Confirmed bug; high confidence.
- **Evidence:** `app/Sources/WispApp/ContactsReader.swift:33-37,40-80`; `service/tools/imessage_tools.py:122-169`.
- **Trigger:** Revoke Contacts permission, delete all contacts, retain only contacts without mapped handles, or encounter a read error.
- **Impact:** Old names, handles, and birthdays remain persisted and usable indefinitely, including stale recipient resolution for approved sends.
- **Root cause:** Denied/error and successful-empty reads return without posting; the Python cache is replaced only when a nonempty contacts map arrives.
- **Recommended fix:** Distinguish unavailable/error from a successful empty snapshot, publish authoritative empties, and immediately gate or purge persisted contacts on revocation. Include a data-lifecycle test for permission loss and zero contacts.

#### H-19 — Legacy workflows can duplicate effects and overwrite cancellation

- **Status/confidence:** High-confidence risk; high confidence. Real outbound execution was intentionally not reproduced.
- **Evidence:** `service/workflows/engine.py:93-109,193-233`; `service/workflows/executor.py:44-137`; `service/main.py:459-468`.
- **Trigger:** Cancel or retry while the original executor is running, or crash after native dispatch but before final workflow persistence.
- **Impact:** Email/messages/scheduled sends can execute twice; a stale executor can overwrite `cancelled` with `completed`.
- **Root cause:** Legacy workflow transitions are unconditional and irreversible effects have no compare-and-swap/effect-claim boundary or persisted unknown state. The typed task path already has the safer atomic-claim pattern at `service/memory/store.py:296-332`.
- **Recommended fix:** Port legacy workflows to revision-checked state transitions and an atomic effect claim immediately before irreversible dispatch. Recheck persisted status before every effect and treat post-dispatch crashes as unknown rather than automatically retryable.

### Medium

#### H-4 — JSON string `"false"` authorizes an approval

- **Status/confidence:** Confirmed input-validation bug; medium severity (P2); very high confidence.
- **Evidence:** `service/main.py:1352-1356`; the current Swift approval caller sends a real Boolean.
- **Trigger:** A malformed or non-current local client submits `{"approved":"false"}` instead of Boolean `false`. The shipped Swift caller does not produce this shape, and a deliberately adversarial local client could already submit Boolean `true`, so current reachability and incremental exploitability are limited.
- **Impact:** For an affected buggy/legacy client, an intended denial of a destructive, calendar, or outbound action is interpreted as approval.
- **Root cause:** `bool(body["approved"])` treats every non-empty string as true.
- **Safe reproduction:** Python’s `bool("false")` evaluates to `True`, which is the endpoint’s exact conversion.
- **Recommended fix:** Use a strict request model and reject non-Boolean values with 422; at minimum require `type(value) is bool`. Add endpoint tests for strings, integers, null, missing fields, and valid Booleans.

#### M-1 — Rescheduling a synced item discards stable state

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `service/assistant/store.py:261-317,431-449`.
- **Trigger:** The same upstream item moves to a different `when_ts`.
- **Impact:** It receives a new Wisp ID and `created_at`, loses notification history/dismissal continuity, can appear newly added, and can notify twice.
- **Root cause:** Existing rows are keyed by `(source_id, when_ts)`, so a time change creates a new row and deletes the old row plus `notify_log`, contrary to the method comment’s in-place-state claim.
- **Safe reproduction:** Changing only the timestamp produced a different ID and creation time and removed a prior T-30m notification marker.
- **Recommended fix:** Ingest a stable occurrence identity from Swift and transfer local state across an unambiguous move; recurring series need an occurrence-specific upstream key.

#### M-2 — Unknown-send warnings are acknowledged even if notification display fails

- **Status/confidence:** Confirmed control-flow bug; high confidence.
- **Evidence:** `app/Sources/WispApp/Notifications.swift:4-20`; `app/Sources/WispApp/OverlayModel.swift:986-1009`.
- **Trigger:** A `scheduled_send_unknown` event arrives while notification permission is denied/suppressed or `UNUserNotificationCenter.add` fails.
- **Impact:** The only warning that a message may already have been sent is acknowledged and disappears; retrying can duplicate it.
- **Root cause:** `Notifications.post` exposes no completion/error result, yet the notice is immediately marked seen and acknowledged to the backend.
- **Recommended fix:** First persist the warning in a durable in-app notice inbox/banner, then acknowledge it. Treat OS notification delivery as best-effort secondary presentation and surface authorization/enqueue failures.

#### M-3 — Agent SSE disconnect leaks registry state and leaves work running

- **Status/confidence:** High-confidence risk; high confidence.
- **Evidence:** `service/main.py:459-468,518-529,955-968`; `service/agent/approver.py:20-28,67-96`.
- **Trigger:** The client disconnects while an agent turn is running or waiting up to five minutes for confirmation.
- **Impact:** `SESSIONS` entries can persist, abandoned work can keep running, and `foreground_busy` can block scheduled brief work.
- **Root cause:** The runner task is detached/untracked; cleanup occurs only after the stream naturally consumes its sentinel and is not protected by a stream `finally` or task completion callback.
- **Recommended fix:** Retain the runner task, unregister in both stream-finally and task-done paths, cancel work that has not crossed an irreversible-effect boundary, and persist an unknown outcome for work that has.

#### M-4 — Mail snapshots mishandle both empty success and partial failure

- **Status/confidence:** Confirmed bug; high confidence.
- **Evidence:** `app/Sources/WispApp/MailReader.swift:757-816`; `service/tools/email_tools.py:183-240`.
- **Trigger:** A valid history scan returns zero records, or Mail closes/fails after some accounts/batches were scanned.
- **Impact:** Empty success leaves stale mail cached, while an interrupted scan can overwrite the last complete cache with a truncated snapshot.
- **Root cause:** Empty payloads are dropped, but partial accumulated chunks are posted as an authoritative replacement without a completeness/coverage flag.
- **Recommended fix:** Stage and commit snapshots atomically only after complete traversal; publish explicit authoritative empty snapshots; retain the last known complete cache on interruption.

#### M-5 — Notes and Browser SQLite errors can masquerade as successful snapshots

- **Status/confidence:** High-confidence risk; high confidence. Fault injection against real user databases was not performed.
- **Evidence:** `app/Sources/WispApp/NotesDBReader.swift:51-69`; `app/Sources/WispApp/NotesReader.swift:82-111`; `app/Sources/WispApp/BrowserHistoryReader.swift:103-154`.
- **Trigger:** Schema drift, failed statement preparation, `SQLITE_BUSY`, `SQLITE_IOERR`, or a terminal row-step error.
- **Impact:** Last-known-good caches can be erased or truncated while source status is reported available/complete.
- **Root cause:** Prepare/entity failures return empty success and row loops do not verify a terminal `SQLITE_DONE` result.
- **Recommended fix:** Return typed failures for prepare/entity/step errors, use busy timeouts, and only return successful empty after `SQLITE_DONE` with zero rows. Test fixture databases with malformed schema, lock contention, and injected step failures.

#### M-6 — Failed Research deletion is presented as success

- **Status/confidence:** Confirmed bug from deterministic control flow; high confidence.
- **Evidence:** `app/Sources/WispApp/ResearchModel.swift:146-164`; `app/Sources/WispApp/WispClient.swift:727-745`; `app/Sources/WispApp/ResearchView.swift:56-68`.
- **Trigger:** The delete request times out, the backend is unavailable, or it returns non-200.
- **Impact:** The view dismisses as if persisted research data was removed, while the job remains.
- **Root cause:** `WispClient.deleteResearch` returns a meaningful Boolean, but `ResearchModel.deleteJob` discards it and always calls the dismissal callback.
- **Recommended fix:** Dismiss only after success; retain the view and display a retryable error otherwise. Add offline/non-200 UI-model tests.

#### M-7 — Sandbox action coverage silently diverges from production

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `sandbox/outbound.py:26-31,83-109`; `app/Sources/WispApp/OverlayModel.swift:939-943,1043-1053`; `app/Sources/WispApp/OutboundSender.swift:378-412`.
- **Trigger:** Invoke `forward_email`, `flag_email`, or `update_apple_reminder` through the sandbox.
- **Impact:** Forward/flag operations wait about 45 seconds and time out; Reminder updates are ignored. Sandbox demos and tests can falsely represent production behavior.
- **Root cause:** The sandbox maintains incomplete handwritten result-bearing/fire-and-forget registries; unknown events fall through, and tests iterate only the same incomplete registry.
- **Recommended fix:** Define one shared bridge protocol manifest and require exhaustive parity across service emitters, native handlers, and sandbox handlers. Add explicit implementations and an unknown-action failure response.

#### M-8 — Sandbox launcher cleanup can orphan its backend

- **Status/confidence:** Confirmed bug; very high confidence.
- **Evidence:** `sandbox/run.sh:13,49-55,66-74`.
- **Trigger:** Interrupt or exit after the cleanup trap is registered but before `SANDBOX_PID` is assigned, including backend health-check failure.
- **Impact:** `set -u` makes the trap fail on an unbound variable and the spawned backend can remain running.
- **Safe reproduction:** A minimal equivalent shell trap produced `SANDBOX_PID: unbound variable`; `bash -n sandbox/run.sh` still passes because this is runtime state.
- **Recommended fix:** Initialize PID variables to empty values before registering the trap, conditionally kill/wait only valid PIDs, and add forced-startup-failure and interrupt integration tests.

#### M-9 — Sandbox loses recent state on graceful shutdown

- **Status/confidence:** Confirmed bug; high confidence.
- **Evidence:** `sandbox/world.py:71-89`; `sandbox/server.py:36-53`; `sandbox/outbound.py:45-52,71`.
- **Trigger:** Stop the sandbox within the 0.5-second persistence debounce window or while an untracked dispatch task is active.
- **Impact:** Recent simulated mutations/action logs disappear, making reproductions nondeterministic; in-flight dispatch cleanup is also not deterministic.
- **Root cause:** The debounce task is not flushed/awaited by lifespan shutdown, and per-event tasks are spawned without tracking.
- **Safe reproduction:** Mutating a temporary `World` and ending the loop before the debounce completed left no persisted update.
- **Recommended fix:** Add an async `World.close()` that cancels the debounce and persists under lock, call it in lifespan cleanup, and track/gather or cancel all dispatch tasks.

#### M-10 — The documented all-tools smoke script directly mutates the real machine

- **Status/confidence:** Confirmed unsafe code path; very high confidence. It was not executed.
- **Evidence:** `scripts/test_all_tools.py:1-16,89-180,279-285,306-374`; `scripts/demo.py:1-4,21-28`.
- **Trigger:** Run the script using its documented default command.
- **Impact:** It bypasses Wisp’s policy/confirmation layer and creates Notes/Keychain entries, timers/alarms/tasks/goals, changes clipboard/media/system state, and lacks complete cleanup. The demo also leaves a Desktop artifact.
- **Root cause:** The script calls `tool.func` directly with live-looking fixtures, advertises them as safe, and has no mandatory live-effects opt-in, domain gates, or isolated `WISP_HOME` default.
- **Recommended fix:** Make dry-run the default, require explicit `--live-effects` plus per-domain opt-ins, force isolated Wisp storage, restore mutable OS state, and clean up by exact returned IDs.

## Test and verification gaps

### TG-1 — The repository-wide Python test command is unsafe and not self-contained

- **Status/confidence:** Test gap; confirmed harness behavior.
- **Evidence:** Import-time singletons at `service/memory/store.py:103-110,459-460` and `service/memory/facts.py:62-72,383`; direct-execution collection exit at `tests/test_forced_step_withholding.py:26,134-171`; worktree-local interpreter assumption at `tests/test_paths_override.py:27-28,77-82`; no repository pytest configuration or declared pytest/async plugin dependency.
- **Observed behavior:** `python3 -m pytest -q` failed because system Python has no pytest. Running via the saved-project virtual environment without `WISP_HOME` produced 53 collection errors while import-time stores attempted to open the real `~/.moe` databases (blocked here as read-only). With an isolated `WISP_HOME` and the direct-execution module ignored, collection completed at **843 passed, 2 skipped, 23 failed**: 17 async tests lacked an async pytest plugin, four path tests hardcoded a nonexistent worktree `.venv`, and two order/global-state-sensitive assertions failed in the combined run.
- **Impact:** A routine test invocation can touch or migrate real user state and cannot serve as a portable clean-worktree gate. Collection behavior also hides product regressions behind harness failures.
- **Recommended fix:** Set an isolated temporary `WISP_HOME` before any service import in a root `conftest.py`, replace import-time stores with dependency injection/lazy initialization, declare a reproducible dev-test environment including the async plugin, use `sys.executable`, convert direct-execution scripts into pytest tests or exclude them explicitly, and add a guard that fails if any test path resolves to real `~/.moe`.

### TG-2 — Swift model/lifecycle behavior has no unit-test target

- **Status/confidence:** Test gap; confirmed from `app/Package.swift:4-16`.
- **Missing coverage:** `OverlayModel`, `PortGuard`, `Notifications`, `ResearchModel`, `BrowserHistoryReader`, `NotesDBReader`, `ContactsReader`, and native bridge acknowledgement/failure paths.
- **Recommended fix:** Add a Swift test target with injectable networking, notification, process, EventKit, Contacts, and SQLite boundaries. Prioritize overlapping turns/approvals, port ownership, permission revocation, empty/partial snapshots, and result-delivery failure.

### TG-3 — High-impact invariants lack end-to-end fault injection

No current test establishes all of the following across the service and Swift app:

- every always-confirm tool is non-grantable and has a complete approval preview;
- every path-bearing tool enforces the same canonical protected-path floor;
- every native effect has a durable request, matching receipt, idempotency key, and unknown-outcome state;
- reconnect replays notifications until acknowledgement;
- recurring Calendar deletion preserves all non-selected occurrences;
- synchronization rejects reserved sources, rolls back malformed batches, and preserves stable occurrence state;
- sandbox and production bridge manifests are exhaustive and cannot cross-connect.

## Validation results

No production data, accounts, native application mutations, outbound messages, notifications, or process signals were used in validation.

| Check | Outcome |
| --- | --- |
| Exact base | `HEAD` and `origin/main` were `c3b5afe26a1744dd7927586262be0533322443af` at audit start. |
| Initial-publication divergence | At initial publication, `origin/main` was `f20fb800fddc714d7d3b8d08a489dae4c81ef483`, six commits ahead. The intervening release-gate and Smart Search merges touched ten files, none of the production paths that establish the findings. Independent review confirmed that statement for the original candidate. |
| Correction-time divergence | A fresh `git fetch origin main` resolved `origin/main` to `51fa3ec937df7a19961fbb2103a7654bb058ec74`, 15 commits beyond the audit base. It includes the merged original report, Research Library PR #10, and Apple UI concept PR #11. Research Library changed `AppDelegate.swift`, `ResearchModel.swift`, and `ResearchView.swift`, so H-6 and M-6 remain exact-base findings and require current-main revalidation before repair. |
| `swift build --package-path app` | Passed after rerunning outside the tool sandbox so SwiftPM could invoke its compiler sandbox. Only existing deprecation/non-Sendable warnings were emitted. |
| `swift build --package-path air --scratch-path /private/tmp/wisp-air-audit-build` | Passed, 12 build steps. |
| `tests/test_sandbox_world.py` as an isolated script | 42 passed, 0 failed. |
| `tests/test_sandbox_wire.py` as an isolated script | 104 passed, 0 failed. |
| `tests/test_schedule_send.py` as an isolated script | 5 passed, 0 failed. |
| Focused pytest: `test_latency_prompt_contract.py`, `test_reply_bridge_simulation.py`, `test_scheduled_send_claims.py` | 58 passed. |
| `bash scripts/test_mail_reply_contract.sh` | Passed; AppleScript was compiled but no Mail action was executed. |
| Standalone Swift Mail DB regression | 11 checks passed. |
| Standalone Swift source-sync label regression | 8 checks passed. |
| `bash -n sandbox/run.sh` | Passed; the cleanup finding is a runtime unset-variable fault, not syntax. |
| Isolated full Python pytest attempt | 843 passed, 2 skipped, 23 failed; not a valid green gate for the harness reasons in TG-1. |
| Safe focused reproductions | Confirmed shell policy bypass through temporary marker writes, protected-path argument gaps, both 14-column migration stranding and appended-16-column field corruption, sync rollback/locking, manual-source erasure, dedupe collision, moved-item state loss, notification consumption, approval coercion/collision, scheduled preview omission, first-recipient-only queuing, zero-subscriber native false success, recurring deletion payload omission, sandbox cleanup, and sandbox persistence loss. |

## Coverage map

| Area | Depth | Representative paths | Notes |
| --- | --- | --- | --- |
| Safety and permissions | Deep | `service/safety/*`, approval handling in `service/main.py` and `service/agent/*`, MCP categorization | Adversarial shell/path/grant/MCP inputs reviewed and safely policy-tested. |
| Agent orchestration | Deep | `service/agent/*`, `service/router/*`, `service/workflows/*`, typed task effect claiming | Focused on lifecycle, confirmation, overlapping requests, receipts, cancellation, and retry semantics. |
| Persistence and scheduling | Deep | `service/assistant/*`, `service/memory/store.py`, sync endpoints | Schema migration, transactions, identity/dedupe, notification/outbound durability, and crash recovery reviewed. |
| Tool execution | Deep for effectful tools | `service/tools/action_tools.py`, `assistant_tools.py`, `builtin.py`, `files_tools.py`, registry outcomes | Mail hardening internals were not re-audited except where orchestration exposed a distinct defect. |
| macOS app/native bridges | Deep | App lifecycle/backend/ports, Overlay/WispClient, Mail/Messages/Calendar/Reminder/Contacts/Browser/Notes readers, notifications, research | Native destructive/outbound effects were traced statically, not executed. Rendering/theme/system-stat helpers received sanity review only. |
| Sandbox | Deep | `sandbox/run.sh`, server/world/outbound/sync/proxy, wire/world tests | Isolation boundary, protocol parity, action lifecycle, and shutdown persistence reviewed. |
| Air helper | Build plus targeted review | `air/Package.swift`, reader/writer sources | Built successfully; native reader overlap with app paths received sanity review rather than duplicate line-by-line findings. |
| Scripts and packaging | Targeted | `scripts/test_all_tools.py`, `demo.py`, `package_app.sh`, regression scripts | Reviewed for user-state mutation, permission disclosure, and validation reliability. |
| Conversation memory | Boundary-only | `service/memory/*` where required for test/effect-claim evidence | Deeper behavior intentionally left to the active conversation-memory workstream. |
| Current email hardening/autonomous improvement | Boundary-only | Native result delivery, shared orchestration, and test contracts only | Findings here are distinct cross-cutting durability or harness issues, not duplicate workstream review. |

## Areas not fully assessed

- Real EventKit, Contacts, Mail, Messages, Notifications, browser databases, and process termination behavior were not exercised because doing so could mutate user data or external state.
- Historical migration prevalence could not be established from the current squashed history; the migration failure itself is deterministic for the reconstructed prior schema.
- Model quality, prompt routing accuracy, and broad autonomous-improvement behavior were outside this correctness/safety audit except where deterministic code forced an unsafe result.
- Performance under sustained concurrency, long-running oMLX inference, network partitions, disk-full conditions, and macOS permission transitions was reviewed by control flow but not load- or fault-tested end to end.
- Static UI appearance/accessibility and mockup polish were not evaluated.

## Integration and active-work overlap

- The original report is already merged on `main`. This follow-up changes only `docs/WISP_BUG_AUDIT_2026-09-09.md`; it must be based on correction-time `main` so the new PR contains only review corrections rather than replaying the merged audit history.
- Research Library PR #10 from **Build a substantial Wisp experience…** is now integrated and changed `AppDelegate.swift`, `ResearchModel.swift`, and `ResearchView.swift`. H-6 and M-6 describe the frozen audit base; revalidate them on current main before assigning ordered workstream 9 rather than assuming the finding or line anchors remain unchanged.
- The completed but not yet integrated task **Finish Wisp email reply hardening** owns `OutboundSender.swift`, `OverlayModel.swift`, `service/main.py`, `service/memory/store.py`, `service/tools/action_tools.py`, `sandbox/outbound.py`, and related tests. H-4, H-5, H-7, H-13 through H-15, M-3, and M-7 must be revalidated against that commit before implementation; they are distinct orchestration/durability findings, not a duplicate review of its email source-resolution work. Follow the shared-file serialization table below rather than dispatching those repairs independently.
- The completed but not yet integrated task **Finish Wisp conversation memory** owns `service/main.py`, `service/memory/*`, `MemoryView.swift`, and related tests. The audit intentionally excluded its feature behavior, but sync/approval endpoint work and TG-1 test-bootstrap changes should be integrated after that branch to avoid `service/main.py` and store/bootstrap conflicts.
- The active **Create Codex chat tracker** task runs in the Local checkout and is unrelated to Wisp production paths; there is no expected implementation overlap with this report.

## Remediation ownership and serialization

These are dependency-aware workstreams, not independently dispatchable packages. Several findings converge on `service/main.py`, `service/assistant/scheduler.py`, and `OverlayModel.swift`; each shared file must have one primary writer at a time, with later work rebased/revalidated after the earlier owner integrates.

### Current assignment state

- The designated Maintainer is the exclusive production repair owner for **C-2**, limited to `service/assistant/store.py` and migration-only fixtures. That repair must land and pass independent re-review before any broader AssistantStore/sync work begins.
- **C-1 is reserved but unassigned.** No policy repair should start until the coordinator records one owner. This audit task owns only this report and must not dispatch or implement either repair.
- No other finding below is authorized for implementation merely because it is documented here. The coordinator must assign one owner and exact path scope before work begins.

### Shared-file serialization locks

| Shared path | Findings/workstreams | Required serialization |
| --- | --- | --- |
| `service/main.py` | Store/sync H-9/H-10; approval H-4/H-5/M-3; native effects H-7/H-8/H-15; completed memory/email branches | Integrate the completed memory and email branches first. Then use one `main.py` writer in order: store/sync contract, approval lifecycle, native-effect protocol. Revalidate each later step against the integrated predecessor. |
| `service/assistant/scheduler.py` | Notification durability H-12/M-2; native send outcome H-15; scheduled-send behavior H-13/H-14 | One scheduler/outbound integration owner must sequence native outcome semantics before durable notices, then scheduled-send presentation/recipient work. Do not split concurrent scheduler writers. |
| `app/Sources/WispApp/OverlayModel.swift` | Approval lifecycle H-5/M-3; native effects H-7/H-15; durable notices M-2; completed email branch | Integrate email hardening first, then serialize approval state, native receipt handling, and notice persistence through one Swift integration owner. |
| `service/assistant/store.py` | C-2; H-9/H-10/H-11; M-1 | The exclusive C-2 owner goes first. Broader identity/dedupe/sync work starts only after the migration fix is integrated and its historical fixtures are the shared baseline. |
| `AppDelegate.swift` and Research UI/model paths | H-6/M-6; merged Research Library work | Revalidate both findings against current main after the Research Library merge. Then use separate owners for PortGuard and Research only where paths do not overlap and shared app entry-point ownership is clear. |
| Test bootstrap and `service/memory/*` | TG-1; completed conversation-memory branch | Integrate conversation memory first, then assign one test-bootstrap owner; do not rewrite memory singletons concurrently with that branch. |
| `sandbox/outbound.py` and the bridge manifest | H-16/M-7/M-9; completed email branch; native-effect protocol | Integrate email hardening and freeze the revised native protocol before sandbox parity work. One sandbox owner then updates handlers, isolation, and shutdown tests together. |

### Ordered workstreams

1. **C-2 migration repair (already exclusively assigned)** — explicit name-mapped atomic copying, value preservation for 14/15/16-column historical layouts including appended order, notification linkage, interrupted recovery, and second-start tests.
2. **C-1 shell boundary (reserved; assign one owner after C-2 gate)** — own `service/safety/policy.py`, the safe execution adapter, and adversarial shell tests. Other grant/path/MCP policy findings may join only if the same policy owner accepts the expanded scope.
3. **AssistantStore sync integrity** — after C-2 lands, address H-9, H-10, H-11, and M-1; coordinate the sole `service/main.py` writer for the endpoint change.
4. **Approval and turn lifecycle** — after memory/email integration and store/sync `main.py` work, address H-4, H-5, and M-3 with the sole `main.py`/`OverlayModel.swift` integration owner.
5. **Native effect transaction protocol** — after approval lifecycle, address H-7, H-8, and H-15 using durable IDs, acknowledged receipts, native atomic updates, and unknown outcomes.
6. **Durable user notices** — after native outcome semantics are fixed, address H-12 and M-2 through the same scheduler/Overlay integration owner.
7. **Scheduled-send contract** — after the email branch and scheduler protocol stabilize, address H-13/H-14 in `action_tools.py` and outbound queue modeling; confirmation UI changes serialize behind workstreams 4–6.
8. **Legacy workflow effect claims** — H-19 may proceed separately within `service/workflows/*`, but any `service/main.py` integration waits for workstreams 3–5.
9. **App lifecycle/privacy sources** — first revalidate H-6 and M-6 against current main after the Research Library merge, then split H-6 from H-17/H-18/M-4 through M-6 only where paths do not overlap; AppDelegate/Research entry-point edits remain serialized.
10. **Sandbox isolation/parity** — after email/native protocol integration, address H-16 and M-7 through M-9 with one sandbox owner.
11. **Test and script safety** — after production interfaces settle, address M-10 and TG-1 through TG-3; coordinate any production dependency-injection change with the current owner of that module.

## Executive summary

The audit’s two highest-priority findings are urgent high-severity/P1 defects, not blanket critical/P0 incidents: view-only shell policy can be bypassed after an agent/tool call reaches `run_shell`, and an installed-database migration can strand or silently corrupt commitments for affected historical layouts whose prevalence is unknown. The highest-risk cluster is not isolated algorithmic correctness; it is **authority and acknowledgement**. Wisp repeatedly decides that an action was permitted, executed, or delivered based on an annotation, a lossy in-memory publish, an unvalidated body value, or a fire-and-forget receipt rather than a durable, exact identity-and-acknowledgement boundary.

The recommended sequencing is: (1) finish and independently review the already-assigned C-2 migration repair, (2) assign one owner for reserved C-1 and then the remaining policy/approval boundaries, (3) make native effects and user notices durable and idempotent under the shared-file locks above, then (4) repair sync identity/transactions, privacy-source lifecycle, sandbox isolation, and the test harness. Production deployment was not attempted.
