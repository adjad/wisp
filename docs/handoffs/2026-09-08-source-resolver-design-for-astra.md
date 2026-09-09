# Handoff → Astra — source-reference resolver design (2026-09-08)

**From:** Opus · **Branch:** `feat/typed-task-engine` · **Head:** `a11e820`
**Gate:** 330 passed / 1 skipped, standalone checks unchanged. Repackaged and live.

Design by Fable (architect), consulted **before** any code was written this
time rather than after. I verified its load-bearing claims against the code
before acting; the verification notes are inline below.

Reviewing this before I build the resolver. One scope question at the end is
genuinely yours to rule on.

---

## Landed since your last review

Three defects, all found by Fable while designing, all fixed and live:

1. **`_verified_send` vs. the reply receipt.** `reply_to_email` returned a bare
   `"Reply sent."`, but `_verified_send` requires the approved address in the
   receipt text — so adding `email.reply` to `OUTBOUND_INTENTS` unchanged would
   have made every *successful* reply report "not confirmed as sent". Verified
   at `action_tools.py:494`. The app now echoes Message-ID, account, recipient
   and subject, and the tool builds its receipt from those. The result channel
   passes arbitrary fields through `/assistant/action_result`, so this
   generalises past replies. Confirmed the new string still satisfies the
   registry's `reply sent` prefix and the failure path is unchanged.

   Worth noting: this is the first defect today caught *before* shipping rather
   than in review.

2. **Cross-account misdelivery.** `findMessage` walked `repeat with acct in
   accounts` and took the first hit (`OutboundSender.swift:155`). A message the
   user was cc'd on exists in two accounts under one Message-ID, so an unpinned
   reply went out from whichever account Mail enumerated first — not the copy
   the user selected. The search can now be pinned to an account, and the
   script reports the account it used either way. Same family as the unified-
   inbox bug already in this project's history.

3. **The guard was eating its own answers.** `_UNRELATED_SUBJECT_REPLY` keys off
   leading verbs so a pending clarification cannot swallow a new request — but
   the natural answer to "which address should I use?" is *"email the work
   one"*. The answer was dropped as unrelated. Scoped, not loosened: text that
   resolves against the candidates **we actually offered** counts as an answer.
   `"check my email for purchases from PlayStation"` during an open
   clarification is still not consumed, and that direction has its own test.
   Both verified to catch the old behaviour by inverting the fix.

## Mail schema investigation — partial, and one claim I got wrong

Fable flagged that it had **not** verified whether Mail's Envelope Index carries
an RFC Message-ID, and told me not to assume it does. I could not check either:
`~/Library/Mail` is unreachable from my shell (Full Disk Access — Wisp.app holds
the grant, the shell does not). The user ran the probes.

Findings:

- `messages.message_id` **exists** and is **INTEGER** — an FK, not the RFC
  string. This corrects Fable's premise: a column exists, which it had assumed
  might not.
- `MailDBReader`'s header query selects no id column at all
  (`MailDBReader.swift:73`), so the year-deep path today cannot produce an
  actionable reply target regardless of what the schema could support.
- Tables present that might intern the string: **`message_global_data`**,
  **`message_references`** (the latter likely References/In-Reply-To, i.e.
  threading).

**Correction to something I asserted:** I told the user "no table is named
`*message_id*`", inferring it from a paste. That was wrong — `.tables` shows
`conversation_id_message_id`. It is a conversation↔message join table, so it
does not change the conclusion, but the inference was unfounded and I am
flagging it rather than leaving it in the record.

**Still unverified:** which column or table, if any, yields the RFC Message-ID
string. Until that is settled I am building against Fable's original premise —
resolvable universe is the ~50-message AppleScript raw cache — with the source
behind a Protocol so it can be swapped without touching the architecture.

## The design (Fable's, condensed)

**The source read goes in the ENGINE, not a graph step.** Precedent already
exists: `find_contacts` is lazily imported and injected as `contacts_resolver`
(`engine.py:493`). Fable's argument against the graph step is stronger than the
approval-binding one I had: `execute_task` never forwards a result to a later
step's args, so a `source.read` step means building Phase 4 composition to ship
one intent.

Cost it names honestly: `prepare_task_turn` is synchronous, and the raw cache is
warmed by an async call, so the reader must be sync over the parsed cache and
return a distinct `unavailable` when cold, with `main.py` pre-warming when the
compiled intent declares a mail source.

**Interface:**

```python
class SourceRef:  kind: str; query: str; hints: dict[str, str]
class Candidate:  kind: str; id: str; label: str; fields: dict; actionable: bool; tier: str
class Resolution: status: Literal["resolved", "ambiguous", "no_match", "unavailable"]
                  candidates: list[Candidate]; snapshot: dict | None; question: str; event: str
class SourceReader(Protocol):
    kind: str
    def candidates(self, ref: SourceRef, *, now) -> list[Candidate] | None   # None = unavailable
def resolve_reference(ref, reader, *, now, cardinality, max_offered=5) -> Resolution
def select_candidate(offered, reply) -> Candidate | None
```

Snapshots live on `plan.resolved_references: dict[slot, snapshot]`. Mail
snapshot: `{kind, id, account, sender, to, subject, ts, body_digest,
cache_synced_at, candidates_considered, match}`.

**Retrieval:** parse hints rather than score — sender, topic, time. Filter in
order, collapse a thread to its newest message, offer ≤5 newest-first, answerable
by ordinal, subject token, date word, or "yes" against one. **No-match must be
scoped honestly** ("in the ~50 most recent emails"), and a candidate visible in
the deep reader but lacking an actionable id must say *"I can see it but can't
reply to it yet"* rather than reporting no match.

**Do not retrofit `_resolve_recipient_slot`** — contacts carry channel, handle
and self-guard semantics plus 25 fresh tests; a contact is not a source
reference. `_resolve_operation_targets` only if its goldens stay untouched.

**Receipt for `email.reply`** must prove: bridge ok, echoed Message-ID equals
the snapshot id, echoed account equals the snapshot account, recipients ⊆
snapshot sender (or thread when `reply_all`), subject reads back as
`Re: <snapshot subject>`. Items 1 and 2 above are the first half of this.

## The question for you

**Does the resolver ship with `email.reply` alone, or with the `CalendarReader`
wrapper in the same slice?**

Fable proposes wrapping the existing `resolve_event_reference`
(`temporal.py:146`) as a second `SourceReader` immediately, arguing an interface
with one implementation is not yet proven general — and you asked for a
*reusable* resolver, not an email special case.

Against: it widens a slice that already includes a new reader, new plan state, a
new compiler entry point, and a bridge contract. Reminder reference resolution
works today and has goldens I would be moving underneath.

I lean toward including it, because the cost of discovering the interface is
email-shaped only after `email.reply` ships is a second migration. But this is a
scope call with a real trade-off and you have overruled me usefully on scope
before.

Secondary, lower stakes:

- Is fail-closed on stale references acceptable — a moved or archived message is
  caught only when the bridge errors, never silently repaired?
- Should `unavailable` (cold cache) be a clarification the user sees, or a
  silent fall-through to the existing router?

## Still open, tested as known gaps

Unchanged and not described as safe: the supplied-versus-retrieved content
boundary, and ambiguous-time clarification. Both have tests pinning the
failure, including retrieval instructions accepted verbatim as literal bodies.
