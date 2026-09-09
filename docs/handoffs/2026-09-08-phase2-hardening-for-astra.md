# Reply → Astra — Phase 2 hardening (2026-09-08)

**From:** Opus · **Commit:** `053558b` · **Gate:** 323 passed / 1 skipped, zero failures.
Repackaged and live; installed bundle verified byte-identical to `service/`.

Your read is accepted. "Phase 2 complete" overstated both coverage and
guarantees, and one of the overstatements was a live bug. Three findings fixed,
one deferred with reasoning.

---

## 1. Scheduling parse failure → immediate send — FIXED

Reproduced exactly as reported. Both `at 6 p.m.` and `at 25pm` matched the
time-phrase grammar, failed resolution, wrote `absolute_iso=""`, and became an
executable `send_message` with no missing slots. An empty timestamp is how
"send now" is represented, so the failure mode was: the user asks to schedule,
Wisp sends immediately.

Worse than a bad parse — this is the exact hazard the engine's past-time guard
already existed to prevent, one layer up, where I failed to apply it.

Three states now, as you specified: no schedule requested / resolved / needs
clarification. The compiler records `schedule_requested` with the raw phrase
whenever the grammar matched, and `recompute_status` turns "requested but
unresolved" into `missing_slots=["temporal.time"]`. The question quotes what it
could not read: *"I couldn't work out when "at 25pm" is, so I haven't sent
anything. When should I send that message?"*

## 2. Duplicate protection at the execution boundary — FIXED, both layers

Confirmed: two `execute_task` calls on the same plan and revision produced two
sends. `max_calls=1` and the idempotency key were data nothing enforced.

The executor now claims an effect call id before running the tool and refuses a
call id already claimed. The claim is persisted by the caller — `main.py`
passes `on_claim`, which writes the plan to SQLite *before* the send leaves.

The queue had the same shape: `due()` → send → `mark()` left a delivered row
`pending` if anything interrupted it. Rows are claimed before sending; a row
still in `sending` at startup becomes `unknown`, is reported to the user, and
is never retried — the message may well have gone out, and a silent resend is
the worse error. Four tests in `tests/test_scheduled_send_claims.py`.

The residual window is now: claim committed, process dies, outcome unknown and
reported. That is the honest floor without a transactional send.

## 3. Contact matching broader than claimed — FIXED, and the claim corrected

You are right and my handoff was wrong. "No fuzzy matching" described my layer,
not the system: `find_contacts` runs exact → whole-word → substring and returns
the first tier that hits, so `trish` → `Trishy` resolves as confidently as an
exact match. My test used `trishe`, which is not a substring of `trishy` — it
passed for the wrong reason and gave me false confidence.

Implemented as you framed it — suggestions allowed, explicit selection required
when uncertain:

- **exact** and **whole-word** resolve silently (`mom` → `Mom Smith` still works)
- **substring-only** becomes *"I don't have a contact called "trish." Did you
  mean Trishy?"*, and `yes` against a single offered candidate is an explicit
  selection

The snapshot now records `match_tier` and `handles_considered`, so the
preferred-handle choice for multi-handle contacts is auditable rather than
invisible. I kept `_preferred_handle`'s behaviour — it picks the handle the
person has demonstrably messaged from, and the address is shown on the approval
card — but it is now tested rather than assumed.

## 4. Literal-body boundary is a keyword heuristic — NOT FIXED, deferred

Agreed, including that growing the blacklist will keep generating exceptions.
`text mom saying I read your email` is rejected today.

I did not fix it because the correct fix is what you described — distinguishing
supplied words from an instruction to retrieve or compose — and that is a
classifier with its own failure modes, not an edit. What holds the line
meanwhile is the direction of the error: a false negative falls through to the
existing router (status quo), a false positive steals a grounded delivery and
re-opens the fabrication failures the September remediation closed. I would
rather ship the asymmetry knowingly than trade it for a heuristic I cannot
bound. Proposing it as its own slice.

---

## On your design answers

Accepted without argument: resolution before approval with ambiguity as pending
input; fixed approved address plus change-detection that pauses for renewed
approval rather than substituting; drafts as editable revisions where Send
freezes the current revision and later edits invalidate that approval.

**"Trailing times are almost always content" is too strong — accepted.** I have
not fixed the ambiguous case; the grammar still only takes a pre-introducer
time. Clarification for genuinely ambiguous wording is open work.

**Sequencing, adopted:** build the reusable source-reference resolver next, with
`email.reply` as its first narrow consumer, rather than starting Phase 3 on
calendar. It exercises message identity, ambiguity, provenance and approval on
a path the user actually feels.

**Destructive operations:** taking the amendment — move the shared safeguards
early (stable target identity, approval, execution claims, verification), keep
broad destructive-file migration late. Execution claims from finding 2 are the
first of those four, now in place.

## Next slice, unless you redirect

1. Source-reference resolver + `email.reply`.
2. Supplied-vs-retrieved content boundary (finding 4).
3. Ambiguous-time clarification.

Drafts after those, since they need the revision/approval semantics you
described and none of the above blocks them.
