# Replay failure remediation — September 8, 2026

Implemented in the working tree. **The installed Wisp app has not been rebuilt
or replaced.** These results are isolated regression checks, not a second live
execution of the 81-prompt, 12-conversation suite.

> **Superseded 2026-09-08 17:15** — the app *was* packaged later the same day.
> `/Applications/Wisp.app/Contents/Resources/backend/service` is now
> byte-identical to `service/` (packaged 16:12:56, backend restarted 16:13:11,
> after the last source edit at 16:12:31), so these changes are live in the
> installed app. The paragraph above is kept for the record. The live-suite
> caveat still stands: the 81-prompt replay has not been re-run.

## Changes by failure category

| Category | Local implementation |
| --- | --- |
| Fabricated delivery content | `workflows/executor.py` executes required sources and builds a source-excerpt payload without a model composition pass. Source failures stop delivery. The exact arguments shown for approval are executed once. Overlong reports require a narrower scope. |
| Attribution and context | Email, Messages, and Daily Summary use labeled source digests instead of generative rewrites. Invitations are not promoted to acceptance; dates and names remain quoted. Delivery explains that second-person references in excerpts refer to the sender. |
| Recipient/channel binding | Pending plans retain sources through channel replies, lowercase names, `texxt message`, explicit retargeting, and contact corrections. Clarification questions are not copied as reports. Phone numbers cannot be used for email. Multiple saved email addresses require a choice. Approval includes the requested name and resolved destination. |
| Execution and uncertain outcomes | Required read failures stop downstream sends. Denials do not send or retry. Unverified effect results stop the agent loop. A delivery with an uncertain receipt cannot be repeated by a bare `yes`. Reminder failure text no longer asserts that nothing was created when persistence could not be verified. |
| Temporal scope | Calendar periods use half-open calendar boundaries, not rolling 7/31-day substitutes. Move-in date reads exclude preparation reminders. `10am today` retains `today`. A passed reminder time asks for a future time. A bare `12pm` correction preserves the recent reminder's date. |
| Reminder intent | `set me a reminder` is recognized. `move in` inside a reference is not an update verb. Markdown punctuation is removed from titles. `nope` cancels a pending clarification. Compound reminder/notification requests keep notification intent separate; only a verified reminder receipt becomes the notification payload. |
| Stock/news/weather coverage | Recognized company names normalize to tickers; all requested symbols up to the declared limit are retained. `1w` requests history; unknown periods fail rather than becoming current quotes. Current news uses dated RSS headlines from the last 24 hours, with publisher/date/link and no academic/Wikipedia fallback. Weather coverage must include the requested dates; three days are not relabeled a week. |
| Retrieval/privacy scope | Narrow read intents return their actual tool result without additional tool selection or rewriting. Purchase lookup keeps its keyword scope. An empty email match no longer falls back to unrelated inbox contents. An empty date range is distinguished from an empty inbox. |
| Filesystem safety | Bulk organization requires a short-lived, single-use preview token tied to source, destination, pattern, names and file metadata. A changed snapshot requires a new preview. Relative destinations resolve within the source folder. Real moves require confirmation even in full-access mode; previews list the exact set. |

## Verification

Run from the repository:

```sh
.venv/bin/python scripts/test_replay_failure_fixes.py
```

The runner executes the selected pytest suite and also invokes legacy
`check()/FAIL`-counter scripts directly; pytest alone does not reliably detect
those scripts' failed checks.

- New assertion-based behavioral regressions: **40 passed**.
- Selected pytest regression suite: **248 passed, 1 skipped**.
- The skipped test is the opt-in local-model reminder integration test.
- Standalone checks: email scoping **28 passed**; brief fallback **56 passed**;
  time ranges **87 passed**; execution contracts **4 passed**.
- Python compilation and `git diff --check` passed.

Source results, recipients, approval decisions and write effects are mocked in
the new behavioral tests. The file-move test uses temporary fixture files only.
No live messages/emails were sent, and the historical replay was not repeated
against the user's live accounts.

## Scope and remaining validation

- Grounded excerpts prevent these composition errors; they do not prove that
  an email's claim or a publisher's headline is true. News is explicitly
  presented as attributed headlines, not independently verified events.
- The new deterministic paths cover the implemented intent patterns. Other
  free-form requests still use the general router/model. This is not a claim
  that every possible conversation or every original failure now passes live.
- Source freshness and completeness still depend on native Calendar, Mail,
  Contacts and Messages readers. Missing permissions or unavailable providers
  are surfaced, not repaired by these changes.
- Apple reminder mirroring is not newly verified by this patch. Existing
  unverified-mirror labels are preserved in reports and notifications.
- Summaries are intentionally more extractive and less conversational. Existing
  sampling/filtering limits are retained and disclosed; large outbound reports
  are rejected instead of silently truncated.
- `me` without an explicit address/number requires clarification. Ambiguous
  contacts are not guessed. A full-week weather request is declined when the
  provider only supplies three days.
- No installed-app deployment, live-provider accuracy check, actual send,
  scheduled-send dispatch, or end-to-end Apple reminder mirroring test was
  performed. The next release check is a rebuilt app with synthetic contacts
  and deny-only outgoing approvals.
- Pre-existing working-tree changes were retained. Previously moved debug files
  and reminders created during the historical replay were not moved or deleted.
