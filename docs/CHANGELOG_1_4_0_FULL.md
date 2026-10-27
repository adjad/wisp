# Wisp 1.4.0 — full changelog

**Status:** unreleased draft beta. Candidate is PR #238 at `793cd0a4`. CI is green and the independent review returned PASS_WITH_NOTES on that exact commit. Real-app acceptance has not been done.
**Base:** `main` at `acac9395` (1.3.3) plus #239.
**Size:** 116 files, about 25,600 lines added, 63 commits (plus merges).

## In plain words

Wisp now **learns your routines** and uses them to **suggest how to spend your day**. It reads your Calendar, completed reminders, Mail, Messages and Notes, and optionally your Gmail and your app/idle activity. From these it infers tentative patterns ("you usually reply to email around 1pm") and tentative demands ("this looks like it needs prep time"). It proposes time for them in **Today**.

Rules that never change in this release:
- **Scheduled is not done.** A calendar entry never proves you attended or finished something.
- **Everything inferred is tentative, cited, and has no authority.** Wisp cannot send messages or create, move or cancel real events because of an inference.
- **Your plans win.** Pins, your edits and calendar commitments always come first.
- **Off means off.** Disabling, revoking, expiry, changed content and Forget make dependent suggestions disappear.

---

## Added

### Weekly behavior reports
- Weekly reports compare the last seven completed local dates against earlier covered periods.
- Weeks are compared only where source coverage supports it. Incomplete coverage or missing event end times stay "unknown" instead of being guessed.
- Calendar entries are kept as scheduled evidence and are separate from dated task completions.
- Long events are kept by their valid end time. Ambiguous carry-in events no longer produce precise weekly values.

### Device-use patterns and day planning
- Opt-in **app, idle, away and screen-state** observations support recurring patterns, found without app, site or scenario rules.
- Today can show **tentative busy windows**, **"possible time away"** (recurring computer inactivity), and **preparation suggestions** backed by a source.
- Time away never claims you are unavailable. Your personal availability stays unknown.
- Absence of activity alone, or a single point visit, can never become a confident "busy" label.
- Website visits give context only. They do not measure time spent on a site.
- Focus/DND and device activity Wisp never collected remain unknown.
- Planning fits your authored work first and puts optional away guidance after it. Pins, edits and calendar constraints are preserved.
- A new public planning context lets the daily planner read this behavior without refreshing sources or calling a model.

### History seeding
- **Calendar and completed-reminder history** scans use resumable, permission-bound checkpoints, with a limit of eight native queries per run.
- **Bounded cross-source memory** indexes permitted Mail, Messages, Notes and retained Calendar/reminder evidence. It records metadata and source proofs, not copied bodies, and it trains nothing.
- Messages records that are individually complete are kept even when a whole publication is capped or partial.
- **Gmail history** is opt-in, per account, bounded, and started explicitly:
  - It needs a fresh Start after every restart.
  - Message bodies live only in an in-memory cache. There is no disk store and no permanent mailbox memory.
  - Removing a Gmail account is rejected if the request carries a browser Origin header.
- Progress separates **fetched**, **usable** and **analyzed** records, and partial coverage is always visible.
- Expired behavior memory recovers on its own, and capacity retries are bounded.

### Consent and control
- Separate switches for **source evidence**, **semantic interpretation** and **remote analysis**.
- Suggestions require current source and permission evidence. Changed, expired or revoked proof withdraws them.
- Original analysis dates stay separate from the date a source was freshly verified.
- **Undo and Forget** use durable ownership receipts:
  - They keep work you edited, completed, or that another source owns.
  - They surface incomplete recovery instead of hiding it.
  - Forget removes both behavior databases, their WAL/SHM files and nudge events.
- **Settings are safe under rapid changes.** The Settings UI shows learning on/off only after the backend confirms:
  - A failed or unclear confirmation shows as **unknown**, never as Off.
  - Pending writes are serialized and stale replies are ignored.
  - The backend orders accepted changes through a bounded queue (16 pending) and a one-time control epoch. An older enable delayed in flight cannot switch learning back on after you turned it off.
  - Turning learning off never needs a successful status read first.
  - New `POST /behavior/settings/check` returns a settled receipt. Legacy GET remains a snapshot without proof.

### Setup and models
- **Bundled-engine setup:** public model download with progress, cancel and retry, and no exposure of the engine's private key.
  - Setup guards check Host/Origin, require JSON, cap bodies at 4 KB and accept only catalogue models.
  - Upstream errors are scrubbed before reaching the client.
- Readiness requires a live model-list response. The setup test accepts only a complete visible assistant reply.
- Closing or changing the setup guide invalidates older test results.
- Fresh installs default to **`Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e`** for text roles. Existing overrides and embedding settings are kept. The model is provisioned from a local folder, so there is no public download for it yet.
- Model-load memory refusals are reported as a clear app error (**HTTP 503**) before chat streaming or setup certification starts. A refused optional warmup no longer invalidates a successful model.

### Routing (from #239)
- Fixes for about 20 prompt families where no model could answer because the right tool was never offered, or a tool was forced on a plain chat request. These include weather, stock lookups, "reply to my landlord", weekday and tomorrow reminders, calendar offsets, notes, contacts and file searches.
- Offline coverage rose from partial to full on every audited family. For example, weekday reminders went from 112/168 to 168/168 and landlord replies from 0/9 to 9/9.
- Adds `scripts/toolbench/family_route_audit.py` to check routing offline.

## Changed
- Planner context is rebuilt from live source evidence at read time, not from stored text.
- Native behavior snapshots, Gmail records and runtime publication now carry publication-authority stamps and ordered per-record witnesses. A changed or withdrawn source invalidates what depends on it.
- Planning times are stored as absolute times and survive restarts.

## Fixed
- **"What should I do tomorrow"** no longer shows raw tool JSON or an empty reply. Malformed or cut-off structured output falls back to plain text. Message verification codes are never quoted.
- A bare "tomorrow" passed as a search query is ignored instead of filtering every row away.
- **Concurrent database startup:** two parts of Wisp opening the private SQLite store at once no longer fail on a half-created file or a sidecar being deleted.
- Stale history rescans and unsupported outputs are corrected so every output is grounded in its source.
- A fresh-start setup test could pass on a stale result. The ordering is now enforced and tested.

## Security and privacy
- Inferred demands carry `action_authority: none`, and the daily workspace rejects any that do not.
- Disable, revoke and expiry withdraw suggestions from the plan. Queued behavior delivery is guarded against stale state.
- Store initialization needs write admission and the volume lease, and opens files with `O_NOFOLLOW`.
- Untrusted-browser request guards protect daily-workspace changes and setup downloads.
- Credential recovery withdraws publications in order, and a clock rollback is latched instead of trusted.

## Known limits
- History scans make eight native queries per run, with no per-minute cap.
- Live Calendar results, native transport cost, real-provider behavior and real-model behavior were not qualified on the exact final build.
- The selected Mellum model passed on the aggressive memory profile only. The balanced profile refused to load it twice earlier.
- A 503 for a model-load failure cannot be returned once a chat stream has already started.
- A stale settings epoch can reject a disable request (retrying without the epoch works).
- A 503 can include raw error text from the bundled engine, which should be checked for file paths.
- Setting WAL mode can change four header bytes in a valid empty database before an unsupported schema is refused (existing behavior).
- Some test totals are incomplete, and a few sandbox skips are disclosed in CI.

## Still required before release
1. Real-app acceptance: Mail → model → Today and withdrawal, native consent check, private cleanup.
2. A decision on the balanced-profile Mellum limit.
3. A version bump and your approval of this text in `CHANGELOG.md`.
4. A release performance check if the release gate requires one.
