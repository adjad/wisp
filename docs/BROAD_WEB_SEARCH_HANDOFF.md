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

Provider work has one 12-second budget, including courtesy-lock waits. Completed
results survive another provider's failure or timeout. A full general result set
returns early; unfinished requests are cancelled and awaited. Malformed rows and
duplicate URLs do not consume result slots or discard valid siblings. Titles
and snippets are normalized and bounded, and caller-owned hits are not mutated.

Supported web operators and quoted/excluded terms pass unchanged to general
providers; specialist APIs are omitted because they do not implement those
operators. Ordinary colons do not disable background discovery. Short technical
and non-ASCII anchors are retained by the conservative specialist matching guard.

Only clear current-news intent uses the dated feed. Documentation, history, and
explicit broader/past ranges use general search. Current-news failures never
fall back to undated snippets. News queries retain the user's topic, region,
quotes, and exclusions instead of being replaced by generic stock-market terms.

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

- After reconciliation: **130 Python tests and 95 subtests passed**, combining
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
WISP_HOME="$web_state" PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  -q -p no:cacheprovider tests/test_broad_web_search.py tests/test_research_mode.py \
  tests/test_research_library.py tests/test_search_reliability.py
PYTHONDONTWRITEBYTECODE=1 python -B scripts/test_replay_failure_fixes.py
```

No implementation work remains. Specialist matching is lexical and may omit
synonym-only background results; it never filters general-provider results on
that basis. Lazy scholarly fallback shares the overall deadline and may have no
remaining time if all general providers stall. The news route is a conservative
intent check, not a general date parser. No live provider validation, merge,
deployment, installation, or archive was performed by this builder. Exact final
SHA and PR are supplied in the task's delivery message.
