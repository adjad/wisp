# Workflow20 implementation checkpoint

This is work in progress, not a claim that all 20 outcomes are complete. The
portable corpus contains 597 cases, 500 positive variations including 100 heldout
positives, across 20 families. Corpus validation does not establish execution
accuracy. Model qualification has not run. New publication targets the user's
Wisp_Hackathon repository; original Wisp main remains unchanged.

| Family | Current implementation/evidence | Remaining completion work |
|---|---|---|
| 01 Overview | Personal compound source constraints; single-source shortcut cannot swallow another requested source | Source-backed final answer and partial-source end-to-end grading |
| 02 Daily summary | Bounded composite daily_brief with source exclusions, current receipts, today Calendar and separate overdue/upcoming/undated reminders; shared daily/date-first briefing grammar and Calendar-only brief | Full summary content/coverage and local-model qualification |
| 03 Time ranges | Exact calendar ranges, noun-position cal/weekend questions resolve to Saturday–Sunday ISO ranges; week/month availability and ongoing/overnight overlap regressions | Full end-to-end final answer grading and real-source validation |
| 04 CA weather | 20 city names, aliases/four typo repairs, provider city/state/country validation; 175 focused offline tests | Real provider/network behavior; arbitrary misspellings remain unsupported |
| 05 Priorities | Dedicated bounded collector reads selected Calendar/Reminders rows and receipts together, plus immutable Mail bodies, normalized Messages and coherent Notes publications. Task windows do not filter communication receipt dates. Exact source exclusions, IDs, omissions and uncertainty precede narration | Model-assisted ownership/deadline/ranking and source-backed final answer qualification; bounded cache coverage cannot prove absence |
| 06 Contact said | Strict query/conversation misses; retrospective email/text questions stay reads; relationship questions remain private without saved contact metadata | All contact/time/topic/sender phrase binding and final answer verification |
| 07 Thread/group recap | Cached chronological email bodies, account ambiguity, bounded-history disclosures and explicit named chat/date bindings | Real full-thread linkage/history retrieval; sender-wide filtering and summary reasoning qualification |
| 08 Groceries | Separate bounded Notes/Reminders evidence tools preserve native IDs, exact list versus item filters, note versus folder selection, literal checkbox states, freshness and omissions; quoted selectors remain data | Native list/account identity and rich Notes checkbox state unavailable; quantity conflicts and final synthesis need model qualification |
| 09 Event time | Bounded named event questions preserve Calendar-only title and explicit period; fuzzy subject/assessment and numeric quiz identity controls | Ambiguity/updated event final answers across variations |
| 10 Reminder/event create | Existing native bridge/confirmation/persistence; returned helper rejects naive local DST gaps/folds before effects and preserves explicit-offset instants | Synthetic approved completion matrix; recurrence/calendar selection unsupported |
| 11 SFO arrival | Bounded traffic/current-location provider contract; private arrival boundary preserves unresolved/compound requests for clarification; exact SFO/address/ISO/buffer bindings offer only checked travel read | Actual native provider integration; default provider unavailable, interface alone is not completion |
| 12 Draft/send/schedule | Existing grounded workflows and durable approval-bound effects; recipient-before-channel drafts; source/dispatch dates separated; ambiguous/nonexistent DST clocks reject before queue/Contacts | Cross-source composition qualification, revised recipient approval tests; scheduled threaded replies unsupported |
| 13 Unsubscribe | Truthful sender/account-bound unverified candidate preparation; no false success | Actual verified unsubscribe provider and historical-open evidence unavailable |
| 14 Files | Extension/debug-filename-date grouping, content-bound previews, protected derived paths and atomic no-replace moves pass disposable filesystem tests; late source replacement reports unknown | Exact approved bytes require stable source entries through execution; full workflow grading, semantic content categorization and durable crash recovery remain unsupported |
| 15 Personal knowledge/followups | Explicit knowledge routes precede Notes; bounded saved notes, style examples and inert follow-up candidates; exact cached source IDs and editable revisions; actual synthetic loop checks enforce prior reads, duplicate-effect guards and existing policy | Broad source indexing, automatic preparation and context/style-guided draft qualification remain incomplete; Mail imports now require one immutable eligible cache generation; cross-source draft qualification remains pending |
| 16 Meeting | Checked private proposals use real event ends, exact duration and coherent fresh snapshots; bearer token remains host-only behind a turn-local one-use inert model reference; interactive approval precedes refreshed conflict/reference checks and strict native receipt | Actual native creation qualification and attendee invitation provider; generic meetings cannot silently become private blocks or invitations |
| 17 Fuzzy test time | Bounded subject typo/test-exam synonyms; quiz/midterm/final distinctions; multiple identities retained | Natural request binding and end-to-end ambiguity/answer tests |
| 18 Unread catchup | Actual unread Messages filtering; returned helpers disclose scoped unknown Mail read states even with no known unread rows and keep Messages matched/included/omitted coverage outside capped digests | All qualified scope bindings and complete grounded catchup grading |
| 19 Availability message | Production delivery selects checked fresh/coherent find_free_time through a shared source contract; complete personally owned expression preserves periods/dayparts/integer-hour limits; stale/unknown-end/unsupported scope/denied preview blocks delivery | Empty availability still fails closed because the string result lacks a host-owned completion receipt. Half-hour bounds, multiple ranges and arbitrary conditions clarify. Local-model/native end-to-end qualification pending |
| 20 Pending actions | Bounded recorded-status history, exact-ID full-body inspection, atomic cancel/claim and truthful result classification; history-aware routing | End-to-end status answers and scheduler/native lost-receipt behavior; app acceptance cannot establish recipient delivery |

## Evidence boundaries

- Focused tests run over fake readers/providers or isolated SQLite. No personal
  reads, real communications, native permissions, model calls or live weather.
- Header digests cannot prove body-only deadlines. Corrected fixtures require
  body reads. Historical superseded thread proposals are allowed only with
  explicit supersession provenance.
- Route diagnostic tool availability, deterministic argument binding, actual
  tool semantics and full model-assisted execution are different measurements.
- Unsupported handling can pass safety while failing desired capability. Unrun
  cases never count as passes. Performance cannot pass without correctness.
- Native application acceptance is different from recipient delivery. Unknown
  outcomes must remain unknown and must not trigger an automatic resend.
- iPhone has separate ios/** ownership and its own build evidence; Mac regression
  results do not qualify iPhone native data access or local inference performance.
- Verified no-opening availability cannot yet be delivered by this executor:
  `no_match` remains non-success. Never treat arbitrary "No gaps" text as proof
  of fresh complete Calendar evidence. A separate host-owned receipt proposal
  is required before changing that result contract.
- A default availability window is 09:00–18:00; its result labels that window.
  Explicit integer-hour `after`/`before` bounds use the stated hour through
  midnight/from midnight. Unsupported combinations do not broaden the read.
- This candidate does not implement the separately planned remote-only
  inference runtime; startup, authentication, packaging and endpoint changes
  for that milestone remain outside this diff.

See CONTRACT.md for fixture and observation format. Use the benchmark runner's
validate/routes/grade modes explicitly; grade accepts externally supplied traces
and does not itself prove their execution provenance.

## Current development base

Follow-up work starts from Wisp_Hackathon root commit
`d20f834d6d3f7e15a36daf6228c722a088a4c40f`. Prior source checkpoint
`c220e4a4c6173fe37e3fa62c9945ca7b76b563f1` remains preserved separately.
Dirty-worktree test runs are development evidence, not exact committed-head
qualification. The original Catch PR's required CI failed on five fixed-clock
fixtures; the failures were reproduced under UTC and repaired here by freezing
the test store clock. Production expiry and all assertions remain unchanged.
UTC and Pacific focused regressions pass; fresh committed-head CI is pending.

## Regression repair evidence

The first frozen PR3 candidate `0bb920222a364066e047db091a40eec41145a390`
failed its isolated full Python run: 189 of 198 gates passed, nine failed,
27,172 assertions passed and 20 failed. One gate supplied an incomplete count;
`counts_complete=false` is retained. That commit and its report are preserved.

The successor repairs arrival aliases, ordinary Calendar scope/context isolation,
exact weekend ranges, and public-weather voice classification. Explicit knowledge
instructions now remain separate from Apple Notes and from commands inside their
authored text. Edit/delete require the selected record read; current revision,
trusted request and existing mutation policy remain host checks. Authored text
preserves case, CRLF and trailing whitespace.

Actual synthetic loop tests reproduced failed-read deletion and changed-argument
repeat dispatch before the three knowledge mutation names joined the existing
effect guards. After the change, 83 focused knowledge cases pass, including
ordering, changed-argument duplicates, confirmation refusal, view-only denial,
missing records, stale revisions and stored-command isolation. These are fake
model traces with isolated storage; they do not qualify real model behavior.
Source-import instructions still require canonical single-space kind labels.
An empty saved-record read does not prove a record exists; the host refuses a
missing-ID mutation even if the model tries it after that read.
Fresh committed-head full checks, required CI and independent review remain
necessary. Priority body evidence/ranking and all listed native/provider gaps
remain open.

## Knowledge entrypoint repair

Frozen `8b6ec0e1341264ab56b61f1f9544a3de16cfbc03` passed all 198 offline
gates, with 27,298 reported passing assertions, zero failures and 155 skips.
One legacy unittest count remained unparsed (`counts_complete=false`). Distinct
scoped synthetic QA passed 498 unique cases. Independent source audit still
returned **BLOCK**, finding `W20KNOWLEDGEENTRY01`: a knowledge-note body such as
`Delete all reminders.` reached the earlier typed Reminder compiler before
knowledge routing. Confirmation remained enforced; no real deletion was observed.

Two isolated actual-handler regressions reproduced that interception with empty
and populated Reminder stores. The wrong destructive preview was refused by the
fixture. Those failures and the frozen commit's reports remain preserved.

The repair uses one pure knowledge-envelope grammar at the production entry
boundary and in routing. Complete and malformed addressed knowledge requests
skip earlier news, task, workflow and structured-read interpretation. All text
after a valid save/edit header's newline remains literal body data, including
later lines that look like commands. Stored task/workflow revisions and active
skill state remain intact. The host suppresses automatic skill context for this
turn, including tool-free clarification; ordinary turns keep the default skill
behavior. Credential redaction remains before parsing, and mutation policy,
confirmation, current-user provenance and exact argument checks still apply.

The expanded development suite passes 132 knowledge cases, including four
in-process HTTP `/agent` and `/agent/approve` requests, exact LF/CRLF/trailing
whitespace, authored Reminder/outbound/skill commands, malformed requests,
credential redaction, denial/view-only controls and ordinary Reminder/skill
positive controls. Models, native sinks and data are synthetic. Fresh committed
head full checks, required CI, independent audit and distinct specialist QA are
still required; the prior passes do not qualify this repair or all 20 workflows.

The next frozen revision, `b7012e5143f9ee2db0733d1d849ff4522c85b3e4`, also
passed all 198 offline gates (27,347 reported passes, zero failures, 155 skips,
one unparsed legacy count). Independent audit blocked it on
`W20KNOWLEDGEENTRY02`: an invalid Email/chat or Messages/account source-import
pairing returned no knowledge envelope, allowing an authored title to reach
Reminder parsing. Four actual HTTP regressions reproduced that failure with
empty and populated Reminder stores; the fixtures refused unintended previews.

The successor separates addressed import recognition from accepted import
authority. An invalid full import, or an incomplete Email/Messages import with
an explicit unquoted knowledge destination before its title, receives an empty
clarification envelope. Quoted subject text and incidental knowledge words do
not establish that destination. Exact accepted import grammar, source/container
validation, source identities, policy and approval remain unchanged. No main,
loop or task-compiler change is needed for this repair.

Expanded synthetic HTTP tests cover incomplete headers and trailing actions,
unchanged task/workflow/skill state, valid immutable Mail and normalized Messages
evidence, denial/view-only policy, stale/unavailable Mail publications and
current-request bindings that replace changed model arguments before approval.
Intermediate fixture mistakes (SSE event naming and expecting binding rejection
instead of exact host replacement) are preserved with their corrected runs.
The expanded suite passes 179 cases, including the prior 132 unchanged cases;
the focused cross-suite passes 14 files including that suite. These are
development results.
Fresh committed-head full checks, CI, audit and distinct QA remain necessary.

Frozen `dc07e1c2406ef33d5cbdcdacbe0d6b5235a23648` passed all 198 offline
gates (27,394 reported passes, zero failures, 155 skips, one unparsed legacy
count). Distinct isolated QA passed 311 checks. Independent audit still returned
**BLOCK** for residual `W20KNOWLEDGEENTRY02`: unsupported source wording, polite
direct requests and `titled` inside opaque IDs escaped the incomplete-request
recognizer. Eight actual HTTP cases reproduced the four residual variants over
empty and populated Reminder stores with unrelated pending state.

The next repair has one bounded clarification-only recognizer before the full
category check. It accepts optional Please and Can/Could/Would-you wrappers,
direct knowledge/style/follow-up category attempts, or Save/Store/Remember/Import
headers with an explicit unquoted `as` category destination. Unsupported source
descriptors, partial kind labels and immediately quoted kind labels clarify;
they acquire no tools or import authority. Accepted executable grammar and
bindings remain unchanged. Header roles distinguish source/container data from
title fields, including opaque IDs equal to `titled` or `in`. A quoted whole
subject cannot establish the destination, nor can text after a pre-destination
title or body delimiter. Filename punctuation does not become a category label.

The ambiguity policy preserves ordinary source-title requests such as
`Save email source-1 titled as a knowledge note` and
`Save email titled as a knowledge note` on their existing route (`None`). The
second form could also mean an incomplete import with ID `titled`; without a
structural `in account/chat` field it remains outside this bounded knowledge
attempt grammar. This is not a guarantee for arbitrary natural language or an
isolation fix for all ordinary Mail title/body requests.

Development tests pass 565 cases, retaining all prior 179 cases unchanged and
adding 386 cases, including 47 actual in-process HTTP cases. These cover pending
state preservation, zero other-source reads, hostile model calls, ordinary
source/title/filename controls and six valid immutable synthetic imports with
opaque source/account/chat IDs. A helper found three additional malformed field
variants before freeze; six failing HTTP cases were preserved and repaired.
Intermediate event-name assertion mistakes and a malformed pytest selector are
also retained. These results do not qualify the next committed head; fresh full
checks, required CI, independent audit and distinct QA remain required.

## Family 15 reference scope

The [Sentient OS product page](https://sentient-os.ai/) describes selected local
source analysis, editable personal knowledge, writing-style examples, contextual
drafts and proactive preparation. These are the relevant non-vision capabilities
for Wisp. This checkpoint does not implement their complete combination.

## Model evaluation

No Workflow20 model run or latency qualification has occurred. The routing
diagnostic freezes synthetic domain context and includes the earlier
`compile_read` entrypoint; it does not replay typed workflows, earlier tool
results or approvals. Cases without prescribed tool groups are unmeasured,
not positive routing evidence. Authentication and watchdog clocks must remain
real in a future model harness; the route diagnostic's frozen clock is not
an inference guard. Missing unsubscribe/invitation/traffic capabilities must
remain unsupported in synthetic model runs rather than receive fake success.

## NEXT parallel development checkpoint

The prior candidate `8f29033709694a8f1f6216f306c0bf3db3ef71bf` passed all
198 isolated gates (27,780 reported passes, zero failures, 155 skips; one legacy
count unparsed), both required CI jobs, independent audit `PASS_WITH_NOTES` and
distinct isolated synthetic QA. The sole integrator merged PR3 as
`5b04a7f4c60a6d6ee4fb6562b0618205a65fdf0a`, with the exact audited tree.
These receipts qualify that frozen candidate's stated scope only.

Separate NEXT work adds the priority and grocery collectors above. Before
consolidation, development checks passed 300 focused and 1,068 broader cases.
Agents reviewed the new source boundaries and found two repaired issues: quoted
shopping labels could activate unrelated finalization obligations, and filtered
query matches were counted as inspected native rows. Regression cases preserve
quoted data isolation and distinguish scan caps, query matches and omissions.
Metadata overflow now returns a bounded error after whole records are exhausted.
Initial fixture failures and the reproduced routing failures remain in external
development logs. After the final count and selector validation repairs, 566
focused/source-sync/Notes regressions passed; these remain development evidence.

Returned helper checkpoints are Mail `c879e114d01dd48a7e886aac4159448c077ea746`
(106 focused passes), Messages `f8f4a3352528202ca066fb207bc397458df926b4`
(1,472 combined passes, including its predecessor), and Reminder
`a11d43d9ad805da3e4d4c89948a242af98ab7781` (87 passes, one opt-in Ling skip).
Their initial failures and repairs are retained. These are isolated development
results; consolidation requires a new frozen head, full checks, required CI,
independent audit and distinct QA.

Priority native completeness remains unknown because the existing receipt
carrier lacks a complete inventory attestation. Global scan limits precede
selectors and explicitly disclose uninspected rows. Mail direction and Messages
publication freshness may be unknown; a Notes modification time cannot date
individual sentences. No real model, native data, device, performance or all-20
qualification has run. iPhone has separate source ownership and unsigned
simulator evidence; this Mac checkpoint does not qualify an iPhone deployment.

## PR4 blocked checkpoint and subsequent repairs

Frozen `80d953f1690e4e4c669407f32edd0aeca30da8b3` is blocked. Its isolated
full run completed 201 gates: 199 passed, two failed, 27,931 reported passes,
19 failures and 155 skips; one legacy count remained unparsed. The two failing
suites retained retired priority-reader expectations and omitted final-route
demonstrations for the three new tools. Narrow test amendments now assert the
exact collector/source flags, read-first direct call and narration requirements;
other route and aggregate-planning assertions remain intact.

Independent audit blocked two P1 findings. A real disposable queue/registry/loop
reproduction showed a request to cancel A could cancel model-selected B. The
repair binds the complete unconditional literal ID to both tools; quoted IDs,
malformed targets, conditions and conflicting clauses cannot authorize an
unbound effect. Additional named-selector conditions were tightened during
helper review. All earlier failing runs and intermediate commits are retained.

The second finding concerned quoted grocery selectors reaching the earlier
Reminder deletion compiler. Six real isolated HTTP/SSE cases reproduced the
unintended preview or wrong no-match answer; the harness refused destructive
approval. The new pure outer-read guard suspends earlier interpreters and skill
context for grocery reads while preserving unrelated plans and saved skill
state. Unsupported addressed reads clarify with no tools. After repair, 895
combined grocery/routing/unchanged-knowledge cases passed, including actual HTTP
empty/populated stores, malformed selectors, hostile calls and genuine Reminder
controls. These are development checks, not independent acceptance.

Parallel repairs preserve native Calendar occurrence identity and validate
weather request times/provider dates/numbers before rendering. Calendar's first
checkpoint passed 424 tests plus 61 subtests; helper review then identified a
second-read generation mix requiring repair before qualification. Weather's
exact checkpoint passed 290 focused cases; hourly/daypart forecasts remain
unsupported. No completeness, local-model, device, native-provider, performance
or all-20 qualification is implied. A fresh combined head must pass full checks,
both required CI jobs, independent audit and distinct QA.


## Combined validated reads successor

Both independent reviewers sealed BLOCK results for the preserved 80d candidate.
The QA packet independently reproduced the wrong-target queue mutation and 24
grocery HTTP failures; all 12 unintended destructive cards were declined. Its
2,804 unique synthetic checks include 25 failures. Those results and the failed
full run remain preserved and cannot qualify this successor.

The Calendar repair now uses one raw Calendar contribution throughout schedule
presentation. Secondary legacy rows and their Calendar provenance cannot mix an
older occurrence generation with the selected raw rows. Exact native identity
and conflict variants remain visible. The returned Calendar checkpoint passed
430 checks plus 61 subtests, including mixed-generation location, moved-event
and deletion cases. Calendar completeness and production reader qualification
remain open.

Additional actual isolated HTTP regressions reproduced punctuation, adjacent
quoted selectors, common Show-me/courtesy wording, and excluded-source history
or memory entering grocery/priority model inputs. The shared anchored grocery
prefix and word boundary keep those malformed addressed reads inert; executable
read grammar still consumes the whole request. Valid grocery and priority reads,
and grocery clarification, use the existing current-evidence-only context flag.
Prior conversation and memory cannot reintroduce excluded-source claims in these
self-contained routes. This is a bounded grammar, not a guarantee for arbitrary
natural-language paraphrases. Genuine Reminder commands and prior knowledge
controls remain covered.

Named cancellation also reproduced an actual wrong-target mutation under the
ordinary write policy. Its discovery route now offers only recorded queue reads,
shows actual IDs, and asks for a new unconditional exact-ID request. A scripted
model cannot select an unrelated cancellation target after named discovery.
Separate explicit-ID controls still bind the selected ID to both read and cancel;
read failures, pending-state races and duplicate execution retain their controls.
Full host-resolved named cancellation remains unsupported.

Weather rejects unsupported clock/offset/datetime period components before HTTP,
including midnight and compound forms. The returned exact checkpoint passed 329
focused checks; 24 prerepair failures are retained. Whole-date, relative-date and
bounded range controls pass. Hourly/daypart forecasts remain unsupported.

The combined development selection passed 1,403 checks over eight suites. Its
initial failures and fixture corrections are retained outside the checkout.
Fresh exact-head full checks, required CI, independent audit and distinct QA are
still necessary. Real local-model behavior, grounded final answers across all 20
families, native actions and device performance remain unqualified. Authenticated
unsubscribe/open-history, live traffic/location, attendee invitations, unknown
send receipts and frozen draft recipients still need separate implementation or
qualification. Synthetic input does not authorize real communications or native
side effects.

## Public queue guard and app entry successor

Frozen `63ed233e9b048ace5d6a29b5ca06a695ed1b0d27` remains blocked. Its
202-gate full run finished with 199 passing gates, three failing gates, 28,234
reported passes, five failures and 155 skips. Counts are incomplete for two
gates. Independent audit blocked the actual app entry path. Distinct QA was
interrupted by platform filtering; its sealed partial evidence is
BLOCKED_INCOMPLETE and cannot count as acceptance. Existing seals and failing
runs remain immutable. No filtered QA request was transferred or repeated.

The successor suspends early interpreters and skill context for recognized
recorded queue requests. Actual isolated HTTP tests now reach the checked queue
read and cancellation path, while preserving unrelated stored plans and skills.
A completely consumed queue-status read offers only the pending-list tool, with
host-bound status, count and empty ID. Named discovery still requires a new
exact-ID request. Literal queued-send cancellation now reaches the existing
exact fullmatch before the generic send-word exclusion; its grammar is unchanged.
Four prerepair failures for the supported literal queued-send forms are retained.

Every normal return from public `route` now passes a cancellation-only guard.
Its implementation body and public signature are preserved by AST checks. Only
the current prompt can identify a cancellation target; prior conversation,
resolved text and model output cannot. Both existing argument bindings must
match the same literal ID, with the forced queue read and its successful-read
obligation intact and no direct cancellation. Otherwise cancellation is forbidden
and removed from all execution metadata. Other tools and constraints remain;
empty obligations remain unsatisfied. This is a public-route boundary; workflow
created decisions and arbitrary internal registry calls are outside its scope.
App entry, actual loop, current-versus-history, concurrency, missing-prerequisite,
view-only, race and repeat controls use disposable stores and scripted clients.

The grocery prefix also recognizes these bounded courtesy-comma forms:
`Please, show my`, `Please,show my`, `Please , show my`,
`Can you, please, show me my`, and `Please, can you please, show my`.
Ten own-builder prerepair failures are retained. Complete exact-selector reads
stay reads, and malformed addressed reads clarify. This does not expand effect
authority or claim arbitrary paraphrase coverage.

Narrow fixture amendments supply the existing raw Calendar carrier in two
Reminder-source tests and the single holiday-exclusion test. Existing event rows,
legacy mocks and all their assertions remain unchanged. Compound queue tests
now assert exact-ID clarification and read-only canonical queue inspection;
unrelated pending-send, channel, status and planning assertions remain intact.

The returned weather repair rejects compact datetime, ISO week-date and numeric
`at` clock suffixes in location before fetch. Its own builder run retained 17
prerepair failures and passed 379 exact-commit focused checks. Existing city,
postal-code, date-only and range controls remain. This is bounded syntactic
validation, with no real provider or hourly forecast qualification.

Fresh full checks, both required CI jobs, independent audit and distinct QA are
required after the new combined freeze. Local inference, device performance,
real native effects and all-20 outcome qualification remain NOT_RUN. Unknown
outbound receipts, recipient-bound draft cards, complete Calendar carriers and
empty-availability completion remain separately scoped work.
