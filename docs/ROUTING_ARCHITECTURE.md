# Wisp routing architecture: typed Intent → deterministic compiler

Status: design + evaluation gate (deliverables 1–2), 2026-10-05. Branch
`claude/routing-architecture` from `origin/main` 4994caa (PR #159 merged = the
baseline safety layer). Author: Claude. Inputs: Codex's independent evaluation
(`origin/codex/independent-routing-eval-20261005`, `eval/independent-routing-20261005/`),
the PR #159 routing-quality corpus and diagnosis (`docs/ROUTING_DIAGNOSIS.md`),
and the review/QA records of PR #159.

## 0. Decisions needed from the user (blocking later stages, not this doc)

1. **Latency budget.** The Intent path adds one constrained Ling call
   (~0.4–0.6 s warm p50, ~1.0–1.8 s p95 measured by Codex, routing stage only)
   to every turn that is not an exact fast path. Accept up to **+0.7 s p50 /
   +1.5 s p95** for flexible requests? (Fast paths stay ~1 ms.)
2. **A live measurement window.** The go/no-go decision (§9) needs one live,
   synthetic-only run on the resident Ling while the user is not using Wisp
   (~20 min, sequential, low rate). Please name a time, or confirm the
   recorded-output replay is enough for go/no-go.
3. **A small user-authored held-out set.** Every existing set was written by an
   agent (Codex or Claude). 30–50 requests written by the user in their own
   words (synthetic content, no real names needed) would be the most honest
   go/no-go set. Optional but strongly recommended.
4. **Scope of the first cutover.** This design starts with read-only calendar,
   mail, messages and notes. Writes (reminders, sends) stay on the PR #159
   rule path until the read path has shipped and been measured. Confirm.

## 1. Problem statement and evidence

Routing today is a ~7,500-line rule tower (`service/router/router.py`) plus a
web-request parser (`web_request.py`) and a lexical retrieval fallback. Each
user-reported failure has been patched with another rule; each rule can
pre-empt another. Measured, not assumed:

| evidence | number | source |
|---|---|---|
| Current rules, strict first-call accuracy, 138 held-out | 51.4 % | Codex REPORT.md |
| Current rules, strict, fresh 36 held-out | 22.2 % | Codex REPORT.md |
| Ling typed intent v2 → deterministic read compiler, fresh 36 | **77.8 % strict, 94.4 % tool set, 97.2 % source** | Codex REPORT.md |
| Ling typed intent v1, 138 held-out | 64.5 % strict | Codex REPORT.md |
| Direct Ling tool selection (25-tool catalog) | 44.2 % / 30.6 % strict | Codex REPORT.md |
| Laya hybrids (either checkpoint) | 40–46 % strict; 48/138 sources flip on option reorder | Codex REPORT.md |
| PR #159 rule repairs, Claude dev corpus | 232 → 370/439 (menu-level, not first-call strict) | ROUTING_DIAGNOSIS.md |
| PR #159 rule repairs, Claude held-out 104 | 67 → 87 (menu-level) | ROUTING_DIAGNOSIS.md |
| Answer stage, 165 synthetic continuations | 54/165 full marks; 23 fabricated counts/absence claims | Codex REPORT.md |

Read across: (a) rules generalise poorly to fresh wording; (b) letting the
model pick tools directly gets the *source* right but the *arguments* wrong
(date ranges, filters, operation); (c) the winning shape is **model
interprets, code decides**: the model fills a small typed object, code
validates it and compiles the calls; (d) presentation is a separate defect
(correct routing does not ensure a grounded answer — 19/51 full marks among
correct first calls).

### What the evidence does NOT yet support (stated plainly)

* **Small n, one author.** The 77.8 % comes from 36 requests (×3 correlated
  repetitions) authored by Codex; the 138 set too. Claude's sets are authored
  by Claude. No user-authored set exists.
* **The combined production path is unmeasured.** Fast path → Intent →
  compiler → agent loop → renderer has never run end to end; Codex measured
  first proposed calls only, Claude measured router menus only.
* **Cold start and contention unmeasured.** Ling stayed resident in every
  run. A cold reload, a model swap (e.g. to the coding model) and a
  concurrent live turn on the single-model engine were never measured.
* **Writes unmeasured.** v2 compiled read intents only; creates/sends were
  scored as "no extra effects", never executed.
* **The eight fresh failures are not solved** (unread in query text, recap as
  records, `Imani`→`Youmani`, stale public filter, missing second source,
  dropped `+` in a literal recipient). §3–§4 address each structurally, but
  that is design, not measurement.

## 2. Architecture overview

```
user turn ─► (1) EXACT FAST PATHS (small, contract-tested) ──hit──► compiled calls
                │ miss
                ▼
            (2) INTENT EXTRACTION: Ling, JSON-schema-constrained, temp 0,
                prompt = static system + compact structured context
                │  invalid → ONE bounded repair → still invalid → (5)
                ▼
            (3) LOCAL VALIDATION (code): schema, enums, literal fidelity,
                exclusions/negations, authorization state, injection guards
                │  unsupported/uncertain → clarify or state limitation (no tool search)
                ▼
            (4) DETERMINISTIC COMPILER: Intent → tool calls/menu + required
                groups + forbidden tools + bindings + reminder/receipt state
                ▼
            existing agent loop (approval gates, receipts, policy) ─► tool results
                ▼
            STRUCTURED EVIDENCE (source, direction, scope, coverage, receipts)
                ├─► overview renderer (calendar by day/time; messages by
                │   conversation, pending first; honest partial coverage)
                └─► next turn's follow-up resolution (what "it"/"that" binds to)

(5) FALLBACK: the PR #159 rule router (unchanged) — model unavailable,
    swapping, timeout, invalid after repair, flag off, kill switch.
```

Principles (non-negotiable, in code):

1. **The Intent is the single source of truth for what the user asked**;
   it never carries permission. Model confidence is never authorization.
2. **Everything with consequences is decided by code**: permissions, source
   exclusions/negations, date boundaries, recipients, approval gates,
   receipts. Any model-chosen literal (recipient, number, path) must match a
   literal in the user's own text (or structured prior-turn evidence) byte for
   byte, or the turn clarifies.
3. **Unsupported or uncertain ⇒ clarify or state the limitation**, never an
   unbounded tool search. (The 20-tool lexical menu is the failure mode
   Phase 1 measured; it remains only inside the PR #159 fallback.)
4. **Few, exact fast paths**, each with an explicit contract and tests.
5. **Shadow before cutover, per domain, with a kill switch.**

## 3. The Intent object (schema v1)

Versioned JSON Schema: `service/router/intent/intent.schema.v1.json`
(committed with this doc). Summary:

```json
{
  "version": "1",
  "operation": "read|search|summarize|overview|compose|send|modify|delete|remember|compute|chat|clarify",
  "sources": [{"source": "calendar|reminders|mail|messages|notes|files|web|weather|memory|device|none",
               "account": "string?"}],
  "excluded_sources": ["mail", "..."],
  "presentation": "overview|records|answer",
  "time": {"named": "today|tomorrow|this_week|next_week|...|null",
           "date": "YYYY-MM-DD?", "start": "YYYY-MM-DD?", "end": "YYYY-MM-DD?",
           "month": "YYYY-MM?", "last_n_days": 1..366, "clock": "HH:MM?",
           "rolling_days": 1..60},
  "filters": {"people": ["verbatim names"], "keywords": ["..."], "folder": "...",
              "conversation": "verbatim person/group", "unread": true|false|null,
              "flagged": true|false|null, "calendar_only": true|false|null,
              "count": 1..200, "minutes": 5..600},
  "reference": {"kind": "none|previous_result|item_index|item_title|previous_request",
                "index": 1.., "title": "verbatim"},
  "literals": {"recipients": ["verbatim"], "numbers": ["verbatim"], "paths": ["verbatim"],
               "quoted": ["verbatim"]},
  "action": {"requested": true|false, "draft_only": true|false, "body_quoted": "verbatim?"},
  "unsupported": "short reason?"
}
```

Design notes, each tied to an observed failure:

* **`sources` is a list with `excluded_sources`.** "email and texts" needs two
  sources (fresh failure: one source missing); "check my calendar but don't look
  at email" needs an exclusion that code enforces (`forbidden_tools`).
  The compiler verifies **every requested source produced a call** (or a stated
  limitation).
* **`unread`/`flagged` are typed booleans**, never words inside `keywords`
  (fresh failure: unread in the text query).
* **`time` is structured**, resolved *in code* with the user's timezone by the
  existing `timeranges.resolve_span`. A named week is Monday–Sunday boundaries;
  `rolling_days` exists separately and is only produced for "next 3 days"-style
  phrases. The compiler never turns `this_week` into `days=7` (the weekly bug).
* **`presentation` separates overview from records** (fresh failure: a recap
  treated as unfiltered records).
* **`literals` are copied verbatim** and re-verified against the user's text
  (fresh failures: `Imani`→`Youmani`, a dropped `+`). If the model's literal is
  not a substring of the user's text (after Unicode NFC and whitespace
  normalisation only), validation fails → one repair → clarify.
* **`reference`** binds "it/that/the second one" to **structured prior-turn
  evidence** (§5), never to free prose.
* **`action.requested`** records that the user asked for an effect; it is *not*
  authorization. Authorization state is computed by code from the turn history
  (pending offer, typed confirmation, PR150/PR153 dated-assent rules).
* **No free-text "reason" field** — nothing the model writes is shown to the
  user or logged.

Validation (code, `service/router/intent/validate.py`): strict schema; enum
checks; field-combination rules (e.g. `presentation=overview` with
`filters.count` only if the user said a number; `operation=chat` ⇒ no sources;
`send` ⇒ recipients required and verbatim); a source in both `sources` and
`excluded_sources` ⇒ invalid; length caps (whole object ≤ 2 KB, each string
≤ 200 chars, ≤ 8 list items); stale-filter guard (a filter inherited from the
previous turn is dropped when the source changes — fresh failure: stale public
location filter); negation cross-check (deterministic negation scanner over the
*user's* text, from PR #159 `utterance_shape`, must agree with
`excluded_sources`/`action`).

## 4. Deterministic compiler (`service/router/intent/compile.py`)

Interface:

```python
def compile_intent(intent: Intent, ctx: TurnContext) -> CompiledRoute | Clarify | Limitation
```

`CompiledRoute` is a `RouteDecision` (unchanged type, so the agent loop,
approval gates and receipts are untouched): `tool_subset`,
`direct_calls`/`tool_argument_bindings` (exact args), `required_tool_groups`
(one per requested source), `forbidden_tools` (exclusions + every write tool
for read operations), `force_first_tool`, `reminder_action`, `light_read`,
`resolved_request` built from the *user's text*, never from model prose.

Per source × operation (stage 1 = read-only):

| source | overview/summarize | records/search/read | free time |
|---|---|---|---|
| calendar | `get_upcoming(period=<named>|<start to end>, calendar_only?)` | same + `query` (verbatim keywords) | `find_free_time(period, minutes?)` |
| mail | `summarize_emails(period?, count?, unread?, account?)` | `view_emails(query?, period?, unread?, count?, account?)` | — |
| messages | `summarize_messages(period?, conversation?, count?)` | `view_messages(query?, period?, count?)` | — |
| notes | `search_notes(query?)` (overview = most recent) | `search_notes(query, period?)` | — |

Rules: (1) one call per requested source, all required; (2) exclusions become
`forbidden_tools`; (3) every write/send/delete tool is forbidden for read
operations; (4) arguments come only from typed fields — never from model
free text; (5) an argument the tool cannot express (e.g. "flagged" on
`view_emails`) ⇒ `Limitation` (state it) not a silent drop; (6) past-only spans
for calendar go to `get_past_events`; (7) an unresolvable period ⇒ clarify.
Stage 3+ adds write operations by compiling into the *existing* typed task
engine (`service/tasks`, `service/workflows`), which already owns reminder
creation, outbound delivery, recipient resolution and receipts — the Intent
replaces only the regex classification in front of it.

## 5. Structured evidence and follow-ups

Every read tool result is wrapped (in the loop, metadata only) as:

```json
{"source": "calendar", "operation": "records", "direction": "incoming|outgoing|both",
 "scope": {"period": "2026-10-05..2026-10-11", "query": "...", "unread": true},
 "coverage": {"returned": 12, "total": 40, "truncated": true, "unavailable": ["Work calendar"]},
 "items": [{"id": "stable-id", "title": "...", "when": "...", "who": "...", "you_sent": false}],
 "receipt": null}
```

* **Follow-ups** ("the second one", "only the ones from Sam", "and next week?")
  resolve `reference`/inherited filters against the previous turn's evidence
  (ids, scope), so the compiler, not the model, decides what "it" binds to.
* **Repeat-read stop**: the loop compares (tool, normalized scope) with the
  evidence already gathered this turn; an equivalent read is answered from
  evidence instead of re-run (fresh read only when source/scope/coverage
  changes). Permission-denied is a distinct state, never "empty".
* **Overview renderer** (deterministic, existing digest machinery reused):
  calendar grouped by day then time, reminders separated; messages grouped by
  conversation, *pending requests to the user first*, "you" vs correspondent
  explicit from `you_sent`; one short coverage line only when it changes the
  answer ("Work calendar could not be checked"; "showing 12 of 40"); counts
  come from evidence, never from the model (Codex: 23/165 fabricated counts).
  No second unconstrained model rewrite of an already complete digest.

## 6. Failure and fallback matrix

| condition | detection | what runs instead | user-visible |
|---|---|---|---|
| flag off / kill switch | config | PR #159 rule router | none |
| exact fast path hit | contract matcher | fast path | none |
| Ling not resident / swapping / loading | engine status (cached, checked before call) | PR #159 rules | none |
| intent call timeout (budget 1.2 s warm, 0 s if a live turn holds the engine) | deadline | PR #159 rules | none |
| invalid JSON / schema violation | validator | one repair call; then PR #159 rules | none |
| literal not in user text | validator | one repair; then **clarify** (never send to a guessed recipient) | short question |
| `unsupported` set / operation=clarify | validator | clarify or state limitation | short answer |
| source+exclusion conflict, negation disagreement | validator | clarify | short question |
| compiler cannot express a filter | compiler | Limitation: run the expressible part + say what was not applied | one line |
| read tool fails / permission denied | evidence | renderer states it; no retry unless scope changes | one line |
| intent path raises anything | try/except at the boundary | PR #159 rules; metadata-only journal entry | none |

The PR #159 router remains intact and tested as the safe fallback for the
whole rollout; nothing in it is deleted until a domain has been cut over and
held for one release.

## 7. Latency, memory, contention

* **Extra cost:** one Ling call, ~430 prompt tokens (system ~350 static +
  context) and ~60 output tokens (Codex v2: p50 0.53 s, p95 1.0 s on the fresh
  set; v1 0.74/1.82 s). Decode dominates (91 %, `moe-decode-dominates-latency`),
  so the schema is kept short and output tokens minimal (no reasoning field,
  omitted optional keys).
* **Prefix cache:** the system prompt and schema are byte-stable and first;
  per-turn context goes last, so oMLX's single-entry prefix cache is hit for
  consecutive intent calls. But the main generation call that follows has a
  *different* prefix, so the two calls evict each other's cache entry. Measure
  both orders in the live run; if eviction costs more than ~150 ms, put the
  intent call on the same leading system block as the main turn.
* **Memory:** no new model. KV for ~500 tokens is negligible (~64 MB at
  ~128 KB/token, `moe-kv-memory-blowup`). Laya is **not** used (unreliable,
  +1–2 GB RSS).
* **Cold start / swap:** if Ling is not resident (coding model swapped in,
  engine restarting), the intent path is skipped — never trigger a load from
  the router. Rules answer that turn.
* **Contention:** the engine has no parallelism
  (`omlx-single-model-no-parallelism`; background calls stalled live turns
  12.8 s → 319.7 s). The intent call runs inline in the user's own turn,
  sequentially before generation, never from a background task. Shadow mode
  (§10) is **not** allowed to issue extra live calls on the user's engine:
  it replays recorded inputs offline, or runs only when the engine is idle
  and the flag `routing.intent.shadow_live` is explicitly enabled.

## 8. Prompt-injection and privacy

* The intent prompt contains the user's text and **structured** prior-turn
  metadata (tool names, sources, scopes, item ids/titles). Tool result
  *bodies* (mail, notes, messages, web pages) are never put in the intent
  prompt. Quoted text is labelled as data.
* Authorization never comes from the Intent: pending-offer/confirmation state
  is computed by code from history (PR #159 standalone-offer logic, PR150/PR153
  dated-assent and receipt guards). A note saying "send this to x@y" cannot
  produce a recipient because recipients must be literals in the *user's*
  text.
* Even a fully adversarial Intent can at most: choose a read of a source the
  user did not ask for (bounded, read-only, logged as metadata), or produce a
  clarify. Writes require stage-3 compilation into the typed task engine plus
  approval cards.
* **Journal:** metadata only — intent field *names* present, enums
  (operation, sources), validation result codes, latency, fallback reason. No
  user text, no literal values, no model output text.

## 9. Evaluation gate (deliverable 2) — summary

`test_fixtures/routing/unified/` holds one frozen, versioned, checksummed
suite merged from: Claude's 439-case corpus and 104 held-out; Codex's 168 +
48 cases (138 + 36 held-out, credited, copied unchanged from its branch);
the historical regression prompts; and contract invariants from PR131,
PR150, PR153 and PR159. `scripts/run_unified_routing_suite.py` scores any arm
deterministically in CI (rules arm live; intent arm from **recorded,
checksummed Ling outputs** in replay mode). Metrics reported separately:
intent accuracy; first-tool + exact-argument accuracy (strict, and with
Codex's documented scoring-sensitivity rules); **hard-gate violations
(permission/exclusion/authorization — must be zero)**; literal-entity fidelity;
false-action rate; overview answer quality (synthetic fixtures + rubric);
latency p50/p95 cold/warm; recovery. Details: `docs/ROUTING_EVAL_SUITE.md`.

**Go/no-go (after the first measured prototype run, stage 2):** go if, on the
unified held-out partitions (Codex fresh 36 + Codex 138 + Claude 104 +
user-authored set if available): hard-gate violations = 0; intent arm ≥ rules
arm + 15 points strict on read cases; literal fidelity 100 %; p95 added
latency ≤ the budget in §0; fallback rate ≤ 10 % with Ling resident. Otherwise
no-go: keep PR #159 rules, fix the specific defects, re-run.

## 10. Rollout

1. `routing.intent.enabled` (default **false**), `routing.intent.domains`
   (allow-list), `routing.intent.kill` (immediate off, checked per turn),
   `routing.intent.shadow` (offline replay comparison only).
2. **Shadow:** the decision of both paths is computed for recorded synthetic
   inputs in CI; metadata-only diffs (which tools, which args *shape*) are
   reported. No live shadow on the user's engine by default.
3. **Cutover per domain**, read-only first: calendar → mail → messages →
   notes. Each cutover is one PR with its own gate run and an independent
   review; rollback is the flag.
4. Writes (reminders, then sends) only after reads have shipped one release,
   and only through the typed task engine.
5. Retire regex rules behind each cut-over domain one release later, keeping
   the fast paths and the fallback router.

## 11. Risk register

| # | risk | likelihood | impact | mitigation |
|---|---|---|---|---|
| R1 | Ling invents a literal (recipient/name) | medium (seen: `Imani`→`Youmani`, dropped `+`) | high for writes | verbatim-substring validation; writes out of scope until stage 4; clarify on mismatch |
| R2 | Added latency annoys the user | high | medium | fast paths for frequent exact requests; 1.2 s deadline → rules; user budget decision (§0) |
| R3 | Engine contention / cold model | medium | high (stalls) | never load from router; skip when not resident; inline only; no live shadow |
| R4 | Overfitting to agent-authored corpora | high | medium | user-authored held-out (§0); frozen checksummed suite; no tuning on held-out |
| R5 | Prompt injection via notes/mail | low (bodies excluded) | high | no tool bodies in intent prompt; authorization in code; literals from user text only |
| R6 | Two routers drift / double maintenance | high during rollout | medium | per-domain cutover; retire rules one release later; unified suite gates both |
| R7 | Schema too narrow → many `unsupported` | medium | medium | `unsupported` → precise limitation, measured as its own rate; schema is versioned |
| R8 | Answer quality still poor after correct routing | high (19/51) | high | deterministic overview renderer + evidence counts; separate gate metric |
| R9 | Recorded-output replay goes stale when the prompt changes | certain | medium | recordings keyed by prompt+schema hash; CI fails closed on hash mismatch; re-record only in a live window |
| R10 | Kill switch not honoured mid-turn | low | medium | checked at turn start; fallback path always constructed |
| R11 | Model update changes behaviour silently | medium | high | model id + revision in the recording key; go/no-go re-run on any model change |

## 12. Staged implementation plan

See `docs/ROUTING_IMPLEMENTATION_PLAN.md` (deliverable 4).
