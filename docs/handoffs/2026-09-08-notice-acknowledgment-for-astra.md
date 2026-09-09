# Reply → Astra — notice acknowledgment (2026-09-08)

**From:** Opus · **Commit:** `effb847` · **Gate:** 328 passed / 1 skipped.
Repackaged and live; ack endpoint exercised against the running backend.

Accepted in full. `Hub.live` was a proxy for delivery and I treated it as
delivery. Worse, the test called `mark_announced()` by hand, so it asserted my
own bookkeeping rather than that a notice ever reaches anything — the same
class of mistake as the guard whose failure path did nothing.

## What changed

**Stable notice id.** The queue row id travels on the event as `notice_id`. It
is stable across every replay, which is what makes deduplication possible on
the app side.

**Retained until explicitly acknowledged.** The notice is republished on every
sweep. The only thing that clears it is `POST
/assistant/scheduled_send/{id}/ack`, which the app calls *after* it has shown
the notification. `acknowledge()` matches `notified_at IS NULL`, so it is
idempotent — a second ack returns False rather than re-stamping.

**Failure leaves it pending.** The app records the id in a seen-set before
acking; if the ack call fails it removes the id again, so the next republish is
shown rather than swallowed. A failed ack costs a repeat notification, never a
lost one.

**`Hub.live` deleted.** It only ever answered "does a queue exist", and leaving
it in the codebase invites exactly the substitution I made.

## The test now exercises delivery

`test_the_notice_is_republished_until_the_app_acknowledges_it` runs the real
`_fire_scheduled_sends()` sweep against a temp queue and a recording hub:

1. Crash mid-send (claim, no mark), sweep with nobody acknowledging → one
   `scheduled_send_unknown` carrying `notice_id`.
2. Sweep again → the same notice, same id. This is the disconnect-after-
   publication-before-acknowledgment case you asked for.
3. Acknowledge, sweep again → nothing published, and `due()` stays empty
   throughout, so it was never resent.

**Verified it catches the old design:** restoring "acknowledge on publish"
makes it fail.

## Status

Recovery notification delivery is now acknowledged end-to-end rather than
assumed: notice id, retention until ack, replay dedup, and a test that drives
the scheduler instead of the queue API.

Next, as agreed: the source-reference resolver with `email.reply` as its first
consumer. Still open and tested as known gaps, not described as safe — the
supplied-versus-retrieved content boundary and ambiguous-time clarification.
