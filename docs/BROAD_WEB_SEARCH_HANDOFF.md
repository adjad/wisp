# Broad-topic web search

Original base: `f20fb800fddc714d7d3b8d08a489dae4c81ef483`.
Successor repair base: `32fc3346b0a8c8591b3cb736b8ff75387d8e52be` (`origin/main`).
Branch: `codex/broad-web-search-audit-repairs`.
Worktree: `/private/tmp/wisp-broad-web-search`.

PR #16 was externally merged at its blocked head
`d31ac6c6c60dd44c1853414025177a77611caade`. This successor addresses the three
remaining P2 audit families in a separate draft PR. Only the news router, its
tests, and this handoff change relative to the successor base. Earlier validation
and delivery sections below are historical; fresh independent gates are required
for the successor's exact pushed SHA.

## Outcome and evidence

The user asked to improve web search across broad topics. Two read-only helper
audits found that the shared search layer gave ordinary queries an equal mix of
DuckDuckGo, OpenAlex, and Wikipedia results. At a six-result budget, only two
general-web pages survived even when six useful pages were available. If DDG
failed, scholarly and encyclopedia pages silently became the entire result set.
The chat shortcut also sent every query containing “news” or “headline” to a
strict 24-hour feed, including documentation and historical questions.

The implementation prioritizes general-web results for ordinary questions,
balances relevant papers with web sources for explicit scholarly requests, and
uses matching specialist results only to fill sparse general coverage. Compact
science queries still reach OpenAlex after general providers settle short, so
existing research discovery does not require adding the word “papers.” Chat
labels background-only coverage; the research caller retains the existing list
and source-provenance contract.

Result collection uses a 12-second deadline, including courtesy-lock waits.
Completed results survive another provider's failure or timeout. A full general
result set returns early; unfinished requests are cancelled and awaited. That
cleanup may extend elapsed time beyond 12 seconds, so this is not a hard bound
on the complete call's duration. Malformed rows and duplicate URLs do not consume
result slots or discard valid siblings. Titles
and snippets are normalized and bounded, and caller-owned hits are not mutated.

Supported web operators and quoted/excluded terms retain their meaning in general
search; specialist APIs are omitted because they do not implement those
operators. Ordinary colons do not disable background discovery. Short technical
and non-ASCII anchors are retained by the conservative specialist matching guard.

Only clear current-news intent uses the dated feed. Documentation, history, and
explicit broader/past ranges use general search. Current-news failures never
fall back to undated snippets. News queries retain the user's topic, region,
quotes, and exclusions instead of being replaced by generic stock-market terms.
Explicitly framed quoted dates and historical points retain their requested
period. Quoted titles and excluded terms do not become positive time/subject
intent. Explicit `when:` filters use general search without an appended day filter.

News routing now separates search syntax from prose, extracts complete temporal
clauses, and then applies current-news intent. Any explicit unsupported period
or positive date operator takes precedence over `today`/`latest`. The original
query is passed unchanged by `web_search()` to its selected helper; no dates are
resolved. The existing general provider still normalizes whitespace and applies
its 500-character bound. RSS receives the original query plus `when:1d`.

| Input context | Route and reason |
| --- | --- |
| `today about the 2026 World Cup`; `from Monday Night Football` | Dated feed: event years and weekday-bearing noun phrases do not establish a period |
| `today site:history.com`; parenthesized site groups; `on API pricing today` | Dated feed: operator payloads and non-temporal topics are not the requested format |
| `from Monday about markets`; `from the Monday before Labor Day`; `in September 2025` | General: framed, complete temporal clauses |
| `past 24 hours`, `past 24h`, `past 1d`, `past 1440 minutes` | Dated feed: equivalent supported rolling-day durations |
| `past 48hrs`, `past 30 minutes`, `last hour`, `prior 2-day period` | General: both shorter and longer intervals must retain their original constraint |
| `past 1 day and 2 hours`; `past day and a half`; `when:7d`; `today from Monday` | General: compound/fractional periods and explicit constraints cannot collapse to a day |
| `from Monday -sports`; `from Monday (site:bbc.com OR site:reuters.com)`; trailing whitespace | General: removing search syntax preserves the extracted period |
| `from Monday lang:en`; `from Monday NOT (site:bbc.com)` | General: language and negated filter groups preserve the historical clause |
| `guide on the latest Hacker News API`; `advice on how to write news headlines today` | General: a topic introducer cannot erase the requested format |
| `what is on the news today about API pricing?` | Dated feed: pre-news `on` does not hide the later topic boundary |
| `past 1½ days`; `past 1 1/2 days`; `past day and 1½ hours` | General: complete mixed-number intervals cannot become a day |
| `past 1 and a half days`; `past 1 and 1/2 days`; `past day and 1 and a half hours` | General: joined numeric-whole fractions share the complete interval grammar |
| `from Monday NOT sports`; `from Monday NOT "sports"`; `from Monday NOT -sports` | General: unsupported Boolean operands cannot hide the preceding period |
| `today NOT (before:2020)`; `today NOT (NOT before:2020)` | Dated feed for the excluded date filter; general for the restored positive constraint |
| `from "last week"`; `for "the past 48 hours"`; `on "Monday"` | General: framed quoted values use the same temporal grammar |
| `from "Previous Week"`; `from "Monday"` | Dated feed: a narrow quoted-source ambiguity rule preserves existing named-source behavior |

For the last row, only a quoted, word-only, title-cased marked interval after
ambiguous `from` receives the source interpretation, except explicit compound
continuations. Lower-case intervals,
numeric values, relative points, and stronger frames such as `for`/`as of` remain
temporal. A separate historical clause elsewhere still wins. Capitalization is
a name signal here, not reliable knowledge of the user's intended meaning.

## Optional independent general-web provider

The backend recognizes `WISP_BRAVE_SEARCH_API_KEY` in its process environment.
When configured, it can use Brave's structured web results alongside DDG. No
Brave request occurs without this setting. The key goes only in the authentication
header, redirects are refused, and diagnostics omit remote error bodies and
authenticated requests. No key was read, provisioned, stored, or tested here.

The adapter follows the official [Brave Web Search API reference](https://api-dashboard.search.brave.com/api-reference/web/search/get)
and [authentication documentation](https://api-dashboard.search.brave.com/documentation/guides/authentication),
checked September 9, 2026. It uses the existing HTTP client dependency.

Default installations still depend on DDG for general-web coverage. A provider
challenge can therefore leave only matching background results or an honest
failure until another general provider is configured. The change does not claim
to eliminate provider restrictions or demonstrate live search accuracy. Bing
HTML/RSS remains excluded following the repository's earlier off-topic result
reproduction.

## Owned files and integration

The table records the original feature scope. The successor repair changes only
`service/tools/web_tools.py`, `tests/test_broad_web_search.py`, and this handoff.
`service/research/web.py` remains byte-identical to the merged candidate, blob
`e82ffddfe12b410ebfc2f66379542345d38b8ef5`.

| File | Change |
| --- | --- |
| `service/research/web.py` | Provider policy, optional Brave adapter, matching, merge, deadline, defensive parsing, coverage note |
| `service/tools/web_tools.py` | News intent and preservation of the complete news query |
| `tests/test_broad_web_search.py` | Synthetic topic, adapter, lifecycle, chat and real research-discovery contracts |
| `docs/BROAD_WEB_SEARCH_HANDOFF.md` | Plan, evidence, validation and handoff |

No app, router, research orchestrator/store, schema, dependency, or installed-app
changes. Research Library PR #10 was left frozen and arrived only through latest
main. Reconciliation was a clean fast-forward with no conflicts. Future edits to
the two web modules may overlap; no implementation dependency on another branch.

## Validation

- On the successor base after all repairs: **154 Python tests and 2,451 subtests passed**, combining
  broad web search, research mode, Research Library, and Smart Search reliability.
- The new suite includes 16 broad-topic fixtures: practical tasks, short coding
  queries, Python disambiguation, travel, health, history, shopping, recipes,
  repair, music, civic information, and non-ASCII queries. Each retains all six
  supplied general results; the baseline retained two. These are deterministic
  provider-policy fixtures, not a live relevance benchmark.
- Real research discovery and SQLite persistence were exercised with compact
  science queries and synthetic provider results. No model or worker was started.
- Existing regression gate: **448 passed, 1 optional Ling integration skipped**;
  all **175 legacy checks** passed. Final run after reconciliation also passed.
- Whitespace checks passed. No Swift changes; no app build or launch was needed.
- Selected new regressions run against the original base produced **26 expected
  failures**, establishing the old general-result crowd-out, malformed-row loss,
  news-subject hijacking, and discarded geographic/topic qualifiers.
- Initial tests exposed invalid JSON handling and incidental specialist matches;
  fixes are covered. Review caught compact-science fallback and explicit-range
  edge cases; their numeric and word-quantity fixtures now pass. No final failed
  checks remain.

All HTTP in the new suite is intercepted by `httpx.MockTransport`; unexpected
requests fail the test. Tests use synthetic credentials and temporary Wisp state.
No live providers, models, source apps, real communications, or user data were
used. The independent release audit, Simulation QA, and Live QA remain separate
gates against the exact pushed commit.

Reproduce with the repository's Python dependencies:

```bash
web_state=$(mktemp -d /tmp/wisp-broad-web-tests.XXXXXX)
env -i PATH="$PATH" WISP_HOME="$web_state" WISP_BRAVE_SEARCH_API_KEY= \
  WISP_OPENALEX_API_KEY= PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  -q -p no:cacheprovider tests/test_broad_web_search.py tests/test_research_mode.py \
  tests/test_research_library.py tests/test_search_reliability.py
env -i PATH="$PATH" WISP_HOME="$web_state" WISP_BRAVE_SEARCH_API_KEY= \
  WISP_OPENALEX_API_KEY= PYTHONDONTWRITEBYTECODE=1 python -B scripts/test_replay_failure_fixes.py
```

No implementation work remains. Specialist matching is lexical and may omit
synonym-only background results; it never filters general-provider results on
that basis. Lazy scholarly fallback shares the overall deadline and may have no
remaining time if all general providers stall. The news route is a conservative
intent check, not a general date parser. No live provider validation, merge,
deployment, installation, or archive was performed by this builder. Exact final
SHA and PR are supplied in the task's delivery message.

## Independent QA repair: WEB16-PASTTIME-1

Independent Simulation QA reported `SIM_FAIL` on initial candidate
`9bb103f223d4442d60babe679e15109610ffd5c9`: five explicit historical queries
(including “two days ago,” “48 hours ago,” “September 1,” and “from Monday”)
incorrectly selected the rolling-day feed. The builder reproduced all five
failures before editing, and 24 permanent historical cases failed on that code.

The sole-owner repair adds bounded historical-point detection before current-news
selection. Temporal units followed by ago/earlier/back, framed calendar dates,
and complete weekday phrases stay on general search without date resolution or
query rewriting. Current rolling-day requests are retained. Quoted product names,
bare month names, weekday domains/possessives and publisher/product names such as
The Sun and Sun Microsystems are covered by current-news controls.

The permanent suite now covers 26 historical forms and 14 additional current
controls. The independently supplied five-case reproduction passes when rerun
by the builder (1 test plus 5 subtests); this is repair evidence, not an
independent QA verdict. Combined tests and the full regression gate above passed
again. The read-only repair review's Sun-name finding was fixed and covered.
No final failed checks remain.

The repair changes only `service/tools/web_tools.py`, the dedicated test file,
and this handoff. Latest main remains the recorded review base, with no conflict
or new integration dependency. PR #16 is updated by a normal non-force push.
All gate evidence for the old SHA is stale; independent Audit, Simulation QA and
Live QA must review the new exact SHA. The bounded predicate still does not claim
to parse every natural-language date expression. No merge, deployment, archive,
live provider request, or actual credential access occurred.

## Independent QA repairs: WEB16-PASTTIME-2 and WEB16-PASTRANGE-1

Independent Simulation QA reported two blockers on
`9fba97c29b2c57687eeaf3e7b5217232738e83c1`: appending “about markets” to “latest
news from Monday” switched it to the rolling-day feed, and “latest news over the
prior 2 days” incorrectly selected that feed. The builder reproduced both before
production edits. The initial permanent neighbor matrix exposed 108 failing
variations, including the two exact reported queries.

The sole-owner repair makes a framed full weekday's temporal meaning independent
of a following topic clause, while preserving domain/possessive guards. Short
weekday abbreviations retain source-name boundaries and accept normal topic
continuations. One marker family (`last`, `past`, `previous`, `prior`, `preceding`)
now shares named-period and quantified-period handling. Named and quantified
quarters, fortnights and weekends use the same unit set. Only recognized exact
rolling-day intervals retain the dated route.

Interval quantities stop at their first time unit, so a topic such as “about
Days Gone” cannot change “past 24 hours” into a different interval. Time analysis
excludes quoted titles; the complete original query still reaches the selected
provider unchanged. No date resolution or general temporal NLP was added.

This repair initially added 147 weekday/frame/topic combinations,
300 interval-marker/period/topic combinations, and 24 named-source/topic controls.
The latter retain The Sun, Sun Microsystems, Monday.com, possessives and quoted
titles. Review found the greedy quantity/topic issue and missing quantified
calendar units; both are fixed and included in this matrix.

The builder reran both independently supplied scripts, including the original
five-case reproduction and the newer 22-case neighbor suite: **3 tests and 27
subtests passed**. At intermediate commit `cc98642`, the combined candidate suites
passed **134 tests and 606 subtests**, and the regression gate passed **448 tests
with 1 optional Ling skip plus all 175 legacy checks**. These runs used `env -i`, temporary
`WISP_HOME`, blank provider keys, and intercepted providers; no live credential,
provider or model activity occurred. Whitespace checks passed. No final failed
checks remain.

Only `service/tools/web_tools.py`, `tests/test_broad_web_search.py`, and this
handoff changed for these repairs. `service/research/web.py` remains unchanged
from the original candidate. Fresh main remains the recorded review base; no
reconciliation conflict or new dependency. PR #16 is updated by a non-force push.
Prior gate evidence is stale, and independent Audit, Simulation QA and Live QA
must repeat against the new pushed SHA. The builder's reruns do not constitute
independent gate approval. No merge, deployment, installation or archive.

## Consolidated Auditor repairs: quoted time values and explicit operators

The Release Auditor added variants on `9fba97c`: `latest news from Monday on the
election`, `latest news on "September 1"`, `latest news from "two days ago"`,
and `latest news when:7d`. The quoted-title guard was hiding explicit time values,
and `when:` could receive a conflicting appended `when:1d` filter. The Auditor
also requested current-news controls for “Last Week Tonight” and excluded
`-history -archives` terms. These findings arrived before the intermediate
commit `cc98642` was frozen for review; that commit is not the final candidate.

Time analysis now retains a complete quoted calendar date or relative point
when an explicit temporal frame identifies it, while masking quoted titles and
negative terms. Framed named dates accept optional years in both month/day and
day/month order. The exact original query still reaches the selected provider.
Explicit `when:`, `before:`, and `after:` operators use general search, preserving
the requested constraint. Weekday topic continuations include “on the election.”

The pre-fix Auditor matrix reproduced **90 expected failures**. A subsequent
read-only review found quoted dates with years were still erased; **48 expected
failures** reproduced that issue before its fix. Final permanent coverage now
includes **168 weekday/frame/topic combinations**, **132 quoted temporal-value
combinations**, **10 operator/filter combinations**, and **23 quoted-title or
excluded-term controls**, alongside the 300 interval and 24 named-source cases.
The bounded helper review passed 116 additional stdlib assertions after the fix;
this is builder review evidence, not a release gate verdict.

Validation at the subsequently rejected `3ae3b0c`: **137 tests and 792 subtests passed**, both
independently supplied reproduction scripts passed in builder reruns (**3 tests,
27 subtests**), and the regression gate passed **448 tests with 1 optional Ling
skip plus 175 legacy checks**. All runs used isolated temporary state and blank
provider keys; provider interactions were intercepted. No final failed checks
remain. The deadline wording above now explicitly distinguishes the collection
deadline from awaited cancellation cleanup, which may extend elapsed time.

Only the same three repair-owned files changed. A fresh fetch confirmed main at
`51fa3ec937df7a19961fbb2103a7654bb058ec74`, already an ancestor of this branch.
There are no new integration conflicts or dependencies. The final exact SHA is
provided in the delivery message after a normal push and PR update. All earlier
Audit, Simulation QA and Live QA evidence is stale for that SHA and must be
renewed. No merge, deployment, installation, or archive is authorized here.

## Six-family extraction repair after independent BLOCK / SIM_FAIL

Both independent gates rejected `3ae3b0cc65607960d78873d0375c427774ed6080` despite
its authored suites passing. The Orchestrator assigned this builder sole repair
ownership of the same three files. The six required families were:

- `WEB16-QUOTED-RANGE`: framed quoted ranges/history words were erased as titles.
- `WEB16-INTERVAL-UNITS`: missing aliases, compact/hyphenated units and singular
  periods silently acquired a broader or narrower one-day filter.
- `WEB16-CURRENTYEAR-1`: an event/product year overrode explicit current intent.
- `WEB16-CURRENTTITLE-1`: weekday-bearing source/title nouns became past dates.
- `WEB16-CURRENTOPERATOR-1`: site payload words such as `history` became prose.
- `WEB16-PASTFORM-1`: prior/previously points and ordinary historical variants
  incorrectly selected the one-day feed.

All **23 supplied examples failed before production edits** in a permanent
regression test exercising `web_search()`, intercepted RSS HTTP, a fixed clock,
and actual output filtering. A 5-minute and 12-hour item must survive the current
feed; a 36-hour item must not. General requests must preserve the complete query,
make no RSS request, and retain their synthetic older general result. This
checks the effect of selecting the wrong route, not just the intent predicate.

The repair replaces the accumulated global vetoes with the extraction/precedence
rules in the table above. Interval aliases share a quantity/unit interpretation;
only recognized exactly-one-day fixed durations use the feed. Compound periods
stay general without trying to sum or resolve dates. Weekday dayparts are part
of a complete temporal phrase, so a following noun remains part of a name.
Quoted values share interval, point, weekday and calendar grammar. Search syntax,
URLs and negative terms are omitted only from analysis, with positive temporal
operators separately retained as constraints.

Systematic tests vary marker, unit alias, spacing/hyphenation, quotation style,
temporal frame, topic/source continuation, event year, operator payload and
conflicting constraints. A bounded read-only review additionally reproduced
**13 failing neighbors** involving quote frames, parentheses, polite/geographic
tails and month/year dates. Those cases are repaired and permanent. All previous
historical and current controls remain unchanged and pass. No title blacklist or
full-query special cases were added. A final bounded helper recheck passed
72 route probes across these findings and controls.

Builder validation at the subsequently rejected `8f764eb`:

- Combined suites: **146 tests and 1,476 subtests passed**.
- Three independently supplied reproduction scripts, rerun by the builder:
  **5 tests and 47 subtests passed**, including the new Simulation harness.
- Existing gate: **448 passed, 1 optional Ling integration skipped**, plus
  **175 legacy checks passed**. Whitespace checks passed.

These counts demonstrate the stated fixtures and regressions only; the previous
gate failures show that large authored matrices are not proof of semantic
correctness. Fresh exact-SHA Audit, Simulation QA and Live QA are required.
Tests used `env -i`, isolated temporary Wisp state, blank provider keys and
synthetic/intercepted HTTP. No live provider, model, native app, credential or
user-data activity occurred.

The grammar remains bounded. Capitalization and unquoted names made entirely of
temporal words can be ambiguous; for example, unquoted `Last Week Tonight` and
`The Day After Tomorrow` can still be read as time vocabulary, while their quoted
topic forms are preserved as titles. This repair does not claim general title
recognition, arbitrary temporal NLP, or measured live relevance. No failures remain
in the assigned six families or the recorded high-confidence review neighbors.

Repair files: `service/tools/web_tools.py`, `tests/test_broad_web_search.py`, and
this handoff. `service/research/web.py` is unchanged from the frozen candidate.
Base remains `51fa3ec937df7a19961fbb2103a7654bb058ec74`; the worktree is isolated,
and no cross-file ownership or integration conflict arose. The final commit is
delivered through a normal non-force push and updated PR #16, then frozen for
fresh independent gates. No merge, deployment, installation or archive.

## Fractional-duration, syntax, and topic repair after `8f764eb`

Independent Simulation QA and Release Audit rejected
`8f764eb52f5b23ae3bc59fbfc00c931fee87deb2`. The Orchestrator explicitly assigned
three P2 findings to the same sole owner and three repair files:
`WEB16-FRACTIONAL-DAY-1`, `WEB16-SYNTAX-PERIOD-1`, and
`WEB16-CURRENT-TOPIC-ON-1`.

The actual-HTTP regression reproduced **22 failing supplied variants** before
production edits, while its 23 previous examples remained passing. The five
focused new/extended tests initially exposed **267 failing subtests** across
these exact cases and nearby transformations. The failures were semantic even
though the earlier authored suite passed.

The interval grammar now consumes its entire recognized continuation. An added
explicit duration or unitless fraction makes a one-day base unsupported; the
same whole-interval match handles quoted values. It covers word fractions,
numeric/Unicode equivalents, and joined or hyphenated forms. No fractional
arithmetic or date resolution is needed. Fractional-looking topic words such as
`half-price`, `Half-Life`, and `half price sale` must complete a duration phrase
before they can extend it; one-day controls retain their dated route.

The bounded helper review found that adding a unit to a supported fraction
(`1/2 day`, `.5 hours`, or `½ hour`) could still discard the continuation.
**38 additional failing subtests** reproduced that defect before the fraction
branch was extended to accept an optional explicit unit. The quoted/unquoted
`past day and 1/2 day` cases also exercise the 30-hour output fixture. The final
bounded recheck passed **267 isolated probes**, including fractional units and
nearby topic guards; no actionable finding remained within that review scope.

After extracting query tokens, analysis-only normalization collapses whitespace
and recursively removes parenthesized groups containing only whitespace/Boolean
connectors. Groups with real topic/date prose remain. Clause boundaries tolerate
whitespace before punctuation or a new parenthesized clause. Temporal operators
retain their separately recorded constraint. None of this changes the original
provider query, including its spacing, quotes, exclusions or filters. Tab-separated
`from` retains the existing quoted-source ambiguity rules.

Non-temporal `on` joins `about` as a topic introducer only after temporal clauses
are extracted. Thus `on API pricing today` retains the dated feed, while
`on Monday about API pricing today` remains general. Existing documentation and
history requests still use general search.

The output test now includes a 30-hour article inside a day-and-a-half interval,
as well as 5-minute, 12-hour and 36-hour items. Wrong current routing would drop
that 30-hour article; the correct general path preserves the original request
and supplied older result. No assertion claims that 30 hours lies within
“24 hours and a half.” Systematic controls cover quoted/unquoted fractions,
genuine one-day intervals, fractional-looking topic nouns, prefix/suffix filters,
trailing/internal whitespace, nested Boolean groups and paired `on`/`about` topics.

Historical builder validation for `d31ac6c` (subsequently blocked by the audit):

- **150 tests and 2,044 subtests passed** across the four candidate suites.
- All four independently supplied reproduction scripts passed in builder reruns:
  **8 tests and 78 subtests**, including the fixed-clock RSS fractional harness.
- Existing gate: **448 passed, 1 optional Ling integration skipped**, plus all
  **175 legacy checks passed**. Whitespace checks passed.

All tests used isolated temporary state, blank provider keys, and synthetic or
intercepted providers. No live provider/model/native-app, credential, or user-data
activity occurred. These results establish fixture behavior only; renewed
exact-SHA independent gates remain required. The bounded temporal/title ambiguity
limitations above remain unchanged. No assigned failed case remains.

Only `service/tools/web_tools.py`, `tests/test_broad_web_search.py`, and this
handoff changed. Provider discovery remains byte-identical to `8f764eb`. Fresh
main remains `51fa3ec937df7a19961fbb2103a7654bb058ec74`, already an ancestor, with
no integration conflict or new dependency. One replacement commit is pushed
normally to PR #16 and its exact SHA is supplied in the delivery message. No
merge, deployment, installation, Live QA activation, or archive by this builder.

## Successor repair after the `d31ac6c` audit

Trigger: `/private/tmp/wisp-release-audit-pr16-d31ac6c.md`. The Orchestrator
retained the original builder as sole owner for `WEB16-CURRENT-TOPIC-ON-1`,
`WEB16-FRACTIONAL-DAY-1`, and `WEB16-SYNTAX-PERIOD-1`. An interrupted patch was
blocked by the approval service's account usage limit; the existing edits were
preserved, and work resumed only after the coordinator reported restored
capacity. No reset credit or workaround was used.

The first actual-output regression reproduced all **11 supplied failures**.
Systematic format, mixed-number and filter transformations initially produced
**112 failing subtests**. The repair preserves guide/documentation/writing
intent when `on` precedes the news term; recognizes mixed numeric fractions
before shorter quantity forms; and aligns language filters with the provider's
existing operator vocabulary. Mixed fractions remain unsupported by the day-only
feed without evaluating their arithmetic.

A bounded review found a further polarity defect in grouped `NOT` and unary
minus. **34 failing subtests** reproduced it before the final parser change.
Recognized filter operands now share one negation-parity path, including nested
groups and `NOT -before:2020`. A group is consumed only after all its operands
parse. Quoted payloads remain opaque, quoted temporal frames retain their
original offsets, and mixed prose groups remain available to the prose analysis.
Nesting is bounded at 32 levels; unsupported/deeper syntax uses the existing
fallback. This is not a general Boolean-language or natural-language parser.

The permanent regression uses real `web_search()` entry/output behavior with
intercepted RSS, a fixed clock, and 5-minute, 12-hour, 25-hour, 30-hour and
36-hour fixtures. A 25-hour result fits the 25.5-hour compound request; a 30-hour
result fits a 36-hour mixed-day request. General fixtures prove selected-helper
query preservation and output retention, not live-provider enforcement of
arbitrary time windows. Current controls retain only the two in-day RSS items.

Historical builder validation for `ffad69a` (subsequently blocked by the PR #20 audit):

- Four candidate suites: **153 tests and 2,306 subtests passed**.
- Four supplied independent harnesses, rerun by the builder: **8 tests and
  78 subtests passed**.
- Auditor's replayable actual-output script: **18/18 passed**, including exact
  selected-helper/RSS queries and retained ages. The original evidence JSON was
  checksum-verified unchanged; new observations are at
  `/tmp/wisp-successor-audit-replay-upladxqh/observations.json`.
- Existing regression gate: **448 passed, 1 optional Ling integration skipped**,
  plus **175 legacy checks passed**.
- Bounded read-only helper recheck: **551 focused probes** and **20
  source-extracted entry-point/output checks** passed; no remaining actionable
  finding in the three assigned families. Deep nesting terminated safely.
- `git diff --check` passed; provider discovery retains the blob above.

All runs used isolated temporary state and synthetic/intercepted providers.
No live provider, model, native app, credential, or user-data effects occurred.
The earlier title/source ambiguity, lexical specialist matching, default DDG
availability, and cancellation-cleanup timing limits remain. Authored counts and
builder replays do not replace independent gate verdicts.

The successor branch safely fast-forwarded across the intervening main merges
without touching owned paths or losing edits. There is no unresolved integration
conflict or new dependency. The final three-file commit is pushed normally to
the successor draft PR and frozen for fresh Release Audit and Simulation QA;
Live QA follows their passing verdicts. The exact SHA is recorded in the delivery
message. No merge, deployment, installed-app replacement, or archive by this
builder.

## PR #20 repair after the `ffad69a` audit

Trigger: `/private/tmp/wisp-release-audit-pr20-ffad69a.md`. The Orchestrator
acknowledged the same sole owner and three-file scope for the existing P2
families. Fresh `origin/main` remains
`32fc3346b0a8c8591b3cb736b8ff75387d8e52be`, already an ancestor; no reconciliation
change, dependency, or conflict was needed.

The permanent actual-entry/output regression reproduced all **nine new audit
counterexamples** before production changes. The expanded paired matrices
produced **129 failing subtests** in total before repair, including those nine.
Earlier assertions remain intact.

- `WEB16-CURRENT-TOPIC-ON-1`: topic search starts after the matched news-format
  phrase and retains absolute offsets. The full prefix still participates in
  format checks. Thus `what is on the news today about API pricing?` receives
  dated news, while guide and writing requests remain general even when they
  contain a later topic clause.
- `WEB16-FRACTIONAL-DAY-1`: numeric wholes joined with `and` now accept the
  shared bounded word/numeric fraction grammar. Base quantities and continuation
  hours use the same forms, including quotes and hyphens. No arithmetic is
  performed; the complete non-one-day interval remains general. Noun boundaries
  preserve current requests about a fractional-length festival or documentary.
- `WEB16-SYNTAX-PERIOD-1`: `NOT` is a temporal-clause boundary alongside `AND`
  and `OR`. An established historical period survives ordinary, quoted, minus,
  and unsupported trailing operands. The filter parser and its polarity contract
  are unchanged; ordinary prose is not deleted or interpreted as a Boolean AST.

Final builder validation for this replacement:

- **154 tests and 2,451 subtests passed** across the four candidate suites.
- Regression gate: **448 passed, 1 optional Ling integration skipped**, plus
  **175 legacy checks passed**.
- Four supplied independent scripts rerun by the builder: **8 tests and
  78 subtests passed**.
- All **28 Auditor actual-entry/output probes passed**, asserting exact helper
  queries and retained article ages. The original audit JSON is checksum-verified
  unchanged; new observations are at
  `/tmp/wisp-pr20-repair-replay-w_oepoey/observations.json`.
- Bounded read-only helper: **771 unique classifier probes** and **24
  source-extracted entry/output checks passed**, including all nine findings,
  format prefixes, joined fractions, source boundaries, and filter polarity.
- Whitespace checks passed. Provider discovery remains the unchanged blob
  `e82ffddfe12b410ebfc2f66379542345d38b8ef5`.

All tests used isolated state and synthetic/intercepted providers; no live
provider/model/native-app, credential, or user-data effects occurred. The bounded
parser, quoted-source/title ambiguity, provider normalization, default provider
availability, lexical matching, and cancellation-cleanup timing limits above
remain. Test counts do not establish live search relevance.

Only the three owned files change. The replacement is committed and pushed
normally to existing draft PR #20, with its exact SHA in the delivery handoff,
then frozen for fresh Release Audit and Simulation QA. Earlier verdicts are
stale. No merge, Live QA activation, deployment, or archive by this builder.
