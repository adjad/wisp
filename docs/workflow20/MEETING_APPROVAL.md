# Private meeting approval integration

Status: implemented with offline synthetic integration tests. Actual native
Calendar and local-model qualification have not run. Invitations are unsupported.

## Host boundary

The existing meeting owner retains `calendar_planning.py`, `schedule_extras.py`
and `test_workflow20_meeting.py`. Narrow host-loop, registry and credential
redaction amendments were acknowledged separately; main.py is unchanged.
Main already supplies a trusted
`current_source` context with session ID, request ID and original user quote.
Caller-supplied context or `approved=true` cannot confer authority.

1. `plan_meeting` validates complete fresh coherent Calendar snapshots. A ready
   private proposal may issue a random bounded, expiring capability bound to
   canonical title, start, duration, end, reference occurrence, source receipt,
   and originating host context. Missing inputs ask a question without a forced
   tool obligation. A preview is not approval or a reservation.
2. `commit_private_meeting` accepts the capability plus the complete explicit
   title/start/duration. The tool is a Calendar write, always requiring exact
   confirmation through the existing policy/approver. It never invites anyone.
3. Before confirmation, the host calls `private_meeting_review(args)`. Invalid
   or mismatched capabilities do not produce an approvable action. The human
   preview shows title, full local date/time/zone, end, duration, reference
   event if any, and private-block-only semantics. A token is not a useful
   human preview. The fingerprint covers the canonical full action.
4. Only after that exact action is approved, the host enters
   `approved_private_meeting(args)` around execution. This context binds the
   exact approved payload and trusted originating request. The tool refuses a
   direct call, a different session/request or changed args.
5. Execution atomically consumes the capability before any possible effect,
   refreshes and revalidates coherent source coverage, exact reference identity
   and end, and target conflicts after approval. Changed/stale/incomplete
   evidence requires a new preview and approval. An uncertain native outcome
   stays unknown; consuming the capability prevents automatic replay.

## Transport and persistence

The raw bearer capability stays in a bounded host-only map for this turn. The
model receives an inert random proposal reference in the same argument field.
Before confirmation the host consumes that reference, checks the exact other
three JSON values and originating context, then resolves separate private args.
Denial, mismatch, expiry and replay require a fresh proposal. Approval cards,
fingerprints, audit fields and model transcripts retain only the inert args.
Only the approved execution receives the raw capability; existing helper checks
still enforce expiry, one use, context and refreshed source evidence.

The host also sanitizes emitted tool-call/result/debug events, audit fields,
final returns and exceptions as defense in depth. Incoming recognizable pasted
capabilities are scrubbed, but stored prose or transformed inert references
cannot authorize a commit. Guarded planning requires a managed loopback client,
checked before every model call and guarded tool execution. Direct commit
execution remains refused. Confirmation uses independent frozen copies so
visible card mutation cannot change the reviewed effect.

## Receipt and validation contract

The registry must classify the guarded tool as a Calendar effect, not a generic
successful read. A native accepted result is private event creation only;
attendee notification and recipient delivery are separate unsupported outcomes.
Timeouts, malformed acknowledgements or interrupted execution are unknown and
cannot count as success. No database migration is proposed.

Offline tests drive the real loop/InteractiveApprover using fake native sentinels:
missing duration/start, denied/cancelled approval, full exact preview, forged,
expired, reused and cross-context capabilities, mismatched args, source changes
during approval, reference deletion/end changes, inserted conflicts, uncertain
dispatch and every durable token carrier. Source-only inspection and fake
provider tests do not qualify actual Calendar integration.
