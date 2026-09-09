# Codex implementation handoff — source resolution and outbound hardening

## Worktree completion from `efbca1f`

Implementation and non-sending validation are complete on
`codex/email-source-resolution-completion` in the `5d40` Worktree. The earlier
sections below describe the historical implementation and its earlier live
smoke tests; those live results do not validate the changes in this section.
The Local checkout's working files were not changed, and no app was installed
or deployed. No live Mail draft was created and no email was sent by this task.

### Gaps fixed

- Raw sync now carries Mail's native account ID through source selection and
  native preparation. Execution remains pinned to the approved account ID and
  RFC Message-ID. Renamed or replaced accounts cannot silently retarget a
  selected source. Legacy raw formats remain readable; new scans use the
  nine-field format with the account ID after the account name.
- Account enumeration and both inbox lookup failures propagate. Duplicate
  account labels and interrupted raw scans fail closed. A successfully scanned
  empty inbox still has successful coverage. Restored data still acquires no
  reference freshness merely by being loaded.
- Native raw serialization rejects SOH/STX separators inside any field before
  joining records. Decode failures, invalid flags, missing native IDs in the
  new format, and invalid timestamps cannot establish unique matches. The
  sandbox models unavailable Mail and partial scans instead of always claiming
  complete coverage. Punctuation-only references no longer match every row.
- Native approval comparison recursively compares character IDs in every
  scalar and every To/CC/BCC element, including case, Unicode representation,
  line endings, recipient order, and literal backslashes. Content is read
  after the other outgoing properties. Preparation and planning also reject a
  body that does not preserve the user's exact supplied prefix.
- The effect claim and persisted claim list are committed in one database
  transaction. A failed state write rolls back the claim. The endpoint no
  longer follows the claim with an unconditional save of an old snapshot.
  Reply corrections, cancellation, and supersession use conditional state
  transitions; cancellation after claim preserves the eventual receipt.
- Duplicate executors explicitly lack permission to finalize the winning
  attempt, including a denial on a shared plan while another executor sends.
  Error and closed-request messages retain uncertainty after a claimed send.
  Malformed truthy bridge success values are not treated as acceptance.
- Scheduled reply markers are rejected by model/planner validation. Scheduled
  replies, editable drafts, broad calendar migration, and destructive file
  operations remain outside this implementation.

### Files and integration dependencies

- Native: `app/Sources/WispApp/MailReader.swift`, `MailReplyScript.swift`,
  `OutboundSender.swift`, and `OverlayModel.swift` in that same directory.
- Source and task logic: `service/tasks/source_readers.py`, `reply_contract.py`,
  `reply_engine.py`, `planner.py`, `models.py`, `executor.py`, and `engine.py`.
- Backend: `service/tools/email_tools.py`, `service/tools/action_tools.py`,
  `service/memory/store.py`, and `service/main.py`.
- Sandbox: `sandbox/outbound.py`, `sandbox/sync.py`.
- Checks: `tests/test_typed_email_reply.py`,
  `tests/test_reply_bridge_simulation.py`, `tests/MailReplyScriptChecks.swift`,
  and the email renderer in `tests/fixtures/wire.py`.
- Documentation: this handoff.

Shared-path changes requiring coordinator review are `OverlayModel.swift`
(preparation account-ID bridge), `service/main.py` (claim callback, execution
finalization, strict result boolean, and uncertain-send error text),
`service/memory/store.py` (conditional transitions and atomic claim state), and
`service/tools/action_tools.py` (preparation identity/body validation and strict
receipts). The fixture renderer has an optional account-ID field. Common task
claim/cancellation fixes also protect existing send/reminder consumers, which
were included in the regression gate. `service/agent/loop.py` and
`scripts/test_replay_failure_fixes.py` were not changed.

Integrate the app, backend, and tests together. In particular, do not restore
the old post-claim `save_workflow` callback, and retain the executor's
`finalize` guard. There is no new database schema or dependency installation.
The existing `/Users/adijain/Desktop/MOE_Project/.venv/bin/python` was used as
a read-only runtime with bytecode writes disabled. Tests used temporary
`WISP_HOME` state and sandbox fixtures; Swift builds and module caches were
under `/tmp`.

### Validation and remaining release work

- Final `scripts/test_replay_failure_fixes.py`: **448 passed, 1 skipped**.
  The skip is the opt-in local Ling integration. Standalone gates passed:
  email scope **28**, brief fallback **56**, timeranges **87**, execution
  contracts **4**. The brief fallback fixture deliberately logs its injected
  parser exception and still passes; it is not a new runtime failure.
- Focused reply/source checks passed **133 tests** before the final three
  parser/transaction regressions were added; all three are included in the
  final 448-test gate.
- Sandbox wire: **104 passed**. Sandbox world: **42 passed**.
- `bash scripts/test_mail_reply_contract.sh`: **passed**. Mail reply and raw
  reader scripts were compiled without executing them. The exact-comparison
  helper was executed only with literal lists and strings, without any
  application calls. The first sandboxed attempt could not access macOS
  scripting services; the permitted retry outside that shell sandbox passed.
- Full Swift build: **passed**, using `--disable-sandbox`, scratch path
  `/tmp/wisp-email-source-build`, and temporary module caches. Existing
  Calendar/Reminders deprecation and PageReader concurrency warnings remain;
  restricted user-level Swift cache warnings did not prevent the build.
- `git diff --check`: **passed** before committing.

Release validation remains external: the changed native account-ID scan,
preparation, exact-envelope check, and draft cleanup need a fresh, explicitly
approved non-sending live smoke test on the integrated build. Include both
accounts, aliases, Reply-To, reply-all CC/BCC, moved messages, and changed
outgoing fields. The earlier historical smoke test is not current evidence.
Actual server delivery requires a separately authorized send and cannot be
proven by Mail's acceptance flag. Unknown outcomes are never automatically
retried. Source search remains bounded to the recently scanned inbox;
unsupported language and broader source migrations remain outside its claims.

## Historical implementation record

Implemented in the working tree after the four September 8 handoffs. Base HEAD
was `0c653e9`. Changes are not committed or installed in the running app.

## What changed

- Added a reusable source-reference resolver with explicit availability,
  completeness, search scope and sync time. Its first production consumer is
  `email.reply`. A CalendarReader adapter exercises the same cardinality
  contract in tests; existing reminder resolution remains on its existing path.
- Email candidates retain account plus RFC Message-ID identity. Same-subject
  messages are not collapsed into threads. Ambiguity is determined before the
  five-item display cap. Selection uses the offered snapshot. A bare “that
  email” does not select the only cached message without a source reference.
- Mail reports account scan coverage, including successful empty scans and
  partial failures. Restored caches do not acquire new freshness at startup.
  Replies request a bounded refresh when needed. An unavailable or partial
  source stays pending instead of silently falling through to legacy routing.
  Deep header matches without actionable IDs are reported as such.
- Added native reply preparation. Mail briefly opens a disposable reply,
  reads its actual From/To/CC/BCC/subject/full content, and discards the draft.
  The approval preview displays that outgoing envelope. The supplied reply text
  is assigned before reading Mail's content; original-message threading is
  retained by the native reply command. Source sender/subject are no longer
  presented as outgoing metadata. See the live-test finding below.
- After approval, execution pins the native account ID, finds exactly one
  current inbox message, creates the reply again, and compares the complete
  outgoing envelope immediately before sending. It also verifies that From
  belongs to the chosen account. Changed recipients, account, subject or
  content refuse the send. Mail must return true from its send command.
- The bridge returns a structured envelope and acceptance flag. Python checks
  every approved field; the receipt binds the content with SHA-256. Missing or
  mismatched receipts produce an unknown outcome, not a fabricated success or
  an automatic retry. Legacy email replies use the same native preparation
  and approval contract.
- Effect claims now check the persisted running status and revision atomically
  before execution. Cancellation and edits while approval is open invalidate
  old attempts. Conditional state transitions prevent late native preparation
  or a losing concurrent execution from overwriting newer state.
- Added clarification for the handoffs’ content and trailing-time examples.
  “I read your email” and “the mail came” can be literal supplied sentences.
  “What the score was” and “whatever Dan said in his last text” remain pending
  until the user supplies quoted wording or explicitly chooses those exact
  words. Source and composition expressions covered by the bounded grammar use
  the same clarification. Quoted text is preserved literally.
- Unquoted trailing times now ask whether the time is delivery timing or part
  of the message. Choosing delivery timing preserves the body separately;
  invalid times remain unresolved and cannot turn into immediate sends.
  Quoting the body or specifying delivery time before the body avoids this
  extra question. Scheduled email replies are not implemented and are refused
  at this clarification rather than silently sent immediately.

## Validation

No live messages or emails were sent. The initial native checks compiled
AppleScripts without executing them; the subsequently authorized live check
created and discarded replies, with the send command removed from the
execution-validation test. Python integration tests use isolated databases
and the sandbox bridge.

- `scripts/test_replay_failure_fixes.py`: **405 passed, 1 skipped**. The skip is
  the opt-in local Ling integration. Its standalone gates also passed:
  email scope 28, brief fallback 56, timeranges 87, execution contracts 4.
- Sandbox world: **42 checks passed**.
- Sandbox wire: **104 checks passed**.
- Action sanitization: **26 checks passed**.
- Full Swift application build passed using Command Line Tools:
  `swift build --disable-sandbox --package-path app --scratch-path /tmp/wisp-review-build -Xswiftc -module-cache-path -Xswiftc /tmp/wisp-swift-module-cache`.
- `bash scripts/test_mail_reply_contract.sh` passed. It compiles generated
  preparation and send AppleScripts and checks the native envelope encoding.
  Running this checker required access to macOS scripting services outside the
  shell sandbox; it does not execute the scripts or perform Mail actions.
- `git diff --check` passed.

Behavioral tests cover account collisions, Reply-To and reply-all recipients,
recipient changes after preview, incomplete coverage, unknown receipts,
duplicate execution, cancellation/correction races, preserved literal escapes
and line breaks, and content/time clarification through persisted turns.

## Boundaries and next validation

- This is a bounded language grammar, not a general semantic classifier. The
  reported examples are fixed and tested; arbitrary composition requests and
  unsupported scheduling syntax remain outside its guarantees. Clarification
  asks for explicit wording rather than attempting a new retrieval/composition
  workflow. The legacy router still exists for unsupported requests.
- The actionable source remains Mail’s recent raw inbox scan. This work does
  not infer an RFC Message-ID from undocumented Envelope Index tables. Moved,
  archived, missing or ambiguous targets fail closed at native execution.
- Mail accepting a send is not proof of eventual server delivery. An unknown
  bridge outcome is never automatically retried.
- The new app and backend must be deployed together: the reply path requires
  the new preparation event, raw coverage metadata and structured receipt.
  Live preparation, envelope stability and draft cleanup now pass on both
  configured accounts. The test uses an already-read inbox message per
  account; additional Reply-To/CC/alias combinations beyond those messages
  and actual server delivery are not established by this smoke test.
- Editable drafts, scheduled replies, broad calendar migration and destructive
  file-operation migration were not added.

Existing unrelated changes in `service/workflows/compiler.py`, the September 8
user-regression tests, and `docs/CONVERSATION_MEMORY_PLAN.md` were preserved.

## User-requested test rerun

The full regression gate passed again: **405 passed, 1 optional integration
test skipped**; all four standalone gates passed. Sandbox world (42), sandbox
wire (104), action sanitization (26), the Swift build, and native AppleScript
compilation/contract checks also passed again.

The initial live Mail smoke test could not start. A read-only AppleScript requesting
only the number of Mail accounts returned **“Not authorized to send Apple
events to Mail. (-1743)”**. This is macOS Automation access denial, not a test
assertion failure. No live reply draft was created and no email was sent.
That permission issue was resolved when the user enabled Mail automation.

## Live retry after Mail access was granted

The first successful connection exposed a native failure: reading the new
reply's rich-text content before assigning the requested body resulted in an
empty read-back. Delaying, opening the window alone, and alternative content
access did not fix the complete sequence. An isolated setter probe worked
when it wrote before the first content read.

`MailReplyScript` now initializes the compose window explicitly, assigns the
supplied reply text before reading content, and checks that the resulting
content starts with the full requested body, with case-sensitive comparison.
A missing or changed body closes the draft and refuses the operation before
approval or sending. It no longer reads and appends the original reply quote
before the write. Mail's reply command continues to establish thread identity.
Preparation briefly shows a compose window and then discards it.

`bash scripts/test_mail_reply_live.sh --live-prepare` now passes:

- Two configured accounts, reply and reply-all, each prepared twice.
- Source/account identity, valid sender and recipient, stable complete
  outgoing envelope, and unchanged outgoing-draft IDs after cleanup.
- The production native approval comparison accepts the matching envelope and
  rejects a changed BCC. For this test, its send command is replaced with
  draft disposal before execution; the test runner refuses any script still
  containing the send command.
- An injected empty body is rejected and its draft is discarded.

The updated Swift application builds, native compile/contract checks pass,
and the regression gate passes again: **405 passed, 1 optional test skipped**,
plus all four standalone gates. The runner now assigns a temporary `WISP_HOME`
per gate so import-time database migrations cannot touch the user's live
state. This also resolved a rerun failure caused by a concurrent memory-schema
change trying to migrate the protected live database during test collection.

No email was sent. These changes remain in the working tree and have not
been installed into the running Wisp app by this task.
