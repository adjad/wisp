# iMessage summary priority handoff

## Outcome

Broad iMessage and Daily Summary digests include substantive unread messages
plus read messages with a conservative, explainable importance reason. An
explicit person or group-chat summary bypasses that broad importance filter and
summarizes the selected conversation's substantive messages.

## Ownership

- Sole writer: Wisp Hub
- Base: `014aee2c9b73658e439c820f9e649ff926fabdb9` (`origin/main`)
- Branch: `codex/imessage-summary-priority`
- Owned paths:
  - `app/Sources/WispApp/MessagesReader.swift`
  - `service/tools/imessage_tools.py`
  - `service/assistant/brief.py`
  - `tests/test_message_digest.py`
  - this handoff

## Behavior and boundaries

- The native reader exports `U` or `R` plus an opaque SQLite chat identity for
  each cached message; duplicate display names remain separate and an explicit
  ambiguous request is refused.
- Legacy three-field cache rows remain eligible until a successful native sync.
- Read-message importance is deterministic and limited to direct questions or
  requests, commitments, deadlines or appointments, logistics changes,
  health/safety, money/security, work/school decisions, and major life events.
- Read requests are omitted only after an outgoing completion statement shares
  action-specific terms; acknowledgments and future promises remain visible.
- `summarize_messages(conversation=...)` resolves named groups and people,
  refuses ambiguous matches, and does not apply the broad importance filter.
- Raw `view_messages` behavior remains complete and unchanged.
- User-facing digests keep model/fallback status in diagnostics and never show
  implementation disclaimers such as "Basic digest" or generic verification
  caveats. The fallback uses the same warm, direct presentation as the normal
  path.
- No Messages data, Full Disk Access database, personal records, or live model
  was accessed during implementation or validation.

## Validation

- `python -B -m pytest -q tests/test_message_digest.py`: 642 passed.
- `swift build --package-path app`: passed; existing deprecation and Sendable
  warnings remain outside this change.
- `git diff --check`: passed.

## Remaining gates

GitHub `python-regressions`, GitHub `Verified macOS artifact`, independent
Release Auditor review, and native/external-integration specialist review are
required for the final remote candidate SHA.
