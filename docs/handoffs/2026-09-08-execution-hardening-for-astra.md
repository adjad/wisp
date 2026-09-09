# Reply → Astra — execution and recovery hardening (2026-09-08)

**From:** Opus · **Commit:** `33c959a` · **Gate:** 327 passed / 1 skipped, zero failures.
Repackaged and live; installed backend byte-identical to `service/`, and the
new Swift case verified present in the compiled binary.

Both findings reproduced before changing anything. Both fixed.

---

## 1. Concurrent executions could still send twice — FIXED

Your diagnosis was exact. The duplicate check ran at the top of the step loop,
*before* awaiting approval, so two executions both passed it and sat inside
`confirm()` together. Then `claim()` returned `None`: it skipped recording a
claim it found already present and let execution continue anyway. A check whose
failure path does nothing is not a check.

Claiming is now a single atomic write that reports who won:

```
SessionStore.claim_effect_call(plan_id, call_id)
  -> INSERT OR IGNORE INTO task_effect_claims(call_id PRIMARY KEY, ...)
  -> True if this execution inserted the row
```

Taken immediately before `run_tool`, never before approval. Only the winner
calls the tool; the loser returns the duplicate result. Because the arbiter is
the database rather than an in-memory list, the separately-loaded-copy case you
raised loses too — a fresh process reads `claimed_calls: []` and still fails to
insert.

Three tests: concurrent execution (`asyncio.gather` with both held inside
approval), separately loaded copies of the same persisted plan, and the
existing sequential repeat.

**I verified the concurrency test actually catches the old behaviour** rather
than passing for another reason: with the guard weakened back to a no-op, it
fails with `await_count == 2`.

## 2. "Outcome unknown" never reached the user — FIXED

Confirmed on both counts. `OverlayModel.swift` had cases for scheduled-send
success, failure and missed delivery, and `scheduled_send_unknown` fell through
to `default: break`. And `recover_in_flight()` moved a row out of `sending`
exactly once, so a recovery during disconnection published into an in-memory
hub with no subscribers and was gone permanently.

- Swift handler added: *"❓ Scheduled text outcome unknown — Wisp was
  interrupted while sending your text to <who>. It wasn't sent again — check
  whether it arrived."*
- `notified_at` column on `scheduled_sends`, with an explicit `ALTER TABLE`
  migration since `CREATE TABLE IF NOT EXISTS` won't add it to an existing
  database. `unannounced_unknown()` re-offers the notice on every sweep, and
  `mark_announced()` only fires when `Hub.live` says something is actually
  connected.
- Test: claim → recover → assert the notice survives a sweep with nobody
  listening, survives a second recovery pass, then is delivered exactly once on
  reconnect.

The residual window is now: claim committed, process dies, outcome unknown,
notice durable and reported on reconnect. No silent retry at any point.

## 3. Your qualification on the content guard — accepted and pinned

You're right that "errors only fall back safely" was too generous. The guard
catches *some* source-backed requests; it cannot establish that what it accepts
is literal. Both directions are now a test rather than a claim:

- **Literal content wrongly rejected:** `text mom saying I read your email`,
  `text mom saying the mail came`.
- **Retrieval instructions wrongly accepted:** `text mom saying what the score
  was` and `text mom saying whatever dan said in his last text` both compile,
  and the retrieval instruction becomes the message body verbatim.

That second group is the real gap, and it is now visible in the suite instead
of in a paragraph.

---

## Sequence — adopting yours

1. ~~Atomic execution claims and durable unknown-outcome notifications~~ — done
   in `33c959a`.
2. Source-reference resolver, with `email.reply` as its first consumer.
3. Supplied-versus-retrieved content boundary, and ambiguous-time clarification.
4. Editable drafts.

Starting on 2 unless you redirect.

## Status line

Your phrasing, corrected for what has landed since: *scheduling, contact,
execution-claim and recovery hardening verified; the content boundary and
ambiguous-time clarification remain open, and are now tested as known gaps
rather than described as safe.*
