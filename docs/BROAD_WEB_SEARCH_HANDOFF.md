# Broad-topic web search

Original base: `f20fb800fddc714d7d3b8d08a489dae4c81ef483`.
Reconciled review base: `51fa3ec937df7a19961fbb2103a7654bb058ec74` (`origin/main`).
Branch: `codex/broad-web-search`.
Worktree: `/private/tmp/wisp-broad-web-search`.

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

Supported web operators and quoted/excluded terms pass unchanged to general
providers; specialist APIs are omitted because they do not implement those
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
query is passed unchanged to the selected provider; no dates are resolved.

| Input context | Route and reason |
| --- | --- |
| `today about the 2026 World Cup`; `from Monday Night Football` | Dated feed: event years and weekday-bearing noun phrases do not establish a period |
| `today site:history.com`; parenthesized site groups | Dated feed: operator payloads are not prose subject/time words |
| `from Monday about markets`; `from the Monday before Labor Day`; `in September 2025` | General: framed, complete temporal clauses |
| `past 24 hours`, `past 24h`, `past 1d`, `past 1440 minutes` | Dated feed: equivalent supported rolling-day durations |
| `past 48hrs`, `past 30 minutes`, `last hour`, `prior 2-day period` | General: both shorter and longer intervals must retain their original constraint |
| `past 1 day and 2 hours`; `when:7d`; `today from Monday` | General: compound periods and explicit constraints cannot collapse to a day |
| `from "last week"`; `for "the past 48 hours"`; `on "Monday"` | General: framed quoted values use the same temporal grammar |
| `from "Previous Week"`; `from "Monday"` | Dated feed: a narrow quoted-source ambiguity rule preserves existing named-source behavior |

For the last row, only a quoted, word-only, title-cased marked interval after
ambiguous `from` receives the source interpretation. Lower-case intervals,
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

- After reconciliation and the six-family extraction repair: **146 Python tests and 1,476 subtests passed**, combining
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

Final builder validation after the last production edit:

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
