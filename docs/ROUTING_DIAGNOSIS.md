# Wisp tool-routing diagnosis (Phase 1, 2026-10-05)

Base: `origin/main` fa66cb3 (1.2.0 RC). Router-only measurement: no model, no
embedding server, no network (sockets disabled), no user data. Every number
below is reproducible with:

```
PYTHONHASHSEED=0 WISP_HOME=<empty dir> HOME=<empty dir> TZ=UTC \
  python scripts/score_routing_quality.py --json-out /tmp/q.json --verbose
```

## 1. What was built

* `test_fixtures/routing/quality_corpus.json` — **361 held-out synthetic
  prompts**, 24 domains (calendar read/write, reminders read/write, mail
  read/send/draft, messages read/send/draft, notes read/write, named items,
  files, web, device, code, memory read/write, compose, chat, compound,
  follow-ups-with-context, ambiguous). Styles: typos, casing, slang, voice
  fillers, `tmrw`/`tmr`/`tommorow`, "my X", polite/indirect, negations, quoted
  text, multi-clause, bare confirmations/declines, pronoun and list
  references. Expectations are written from the user's side: any-of tool
  groups that must be reachable, acceptable forced-first tools (or "nothing may
  be forced"), hard-forbidden tools (sends, permanent/bulk deletes) and soft
  wrong-direction tools.
* `scripts/score_routing_quality.py` — runs the real `route()` exactly as
  `main.py` calls it (incl. `last_user`/`recent_users`/`last_assistant`/
  `last_tools`, then the session pin) and computes reachable vs forced tools
  the way the agent loop sees them (subset − forbidden_tools, + direct calls and
  required groups). Reports per-domain pass rate, recall (needed groups
  reachable), precision (fraction of the menu that is needed), mean menu size,
  per-failure-class counts and latency.

Failure classes. **Hard** (a case fails): `zero_tool`, `missing_tool`,
`wrong_domain` (no needed group reachable), `leak_forbidden`, `forced_wrong`,
`full_registry`, `acted_on_chat`. **Soft** (reported only): `not_forced`,
`leak_avoid`, `generic_fallback` (the retrieved ~20-tool "ambiguous" menu),
`wide_menu` (>12 tools).

## 2. Baseline (fa66cb3), UTC and Pacific/Kiritimati identical

**232/361 pass (64%)** · recall 0.88 · precision 0.40 · mean menu 8.8 tools ·
router latency p50 0.78 ms, p95 7.6 ms.

| domain | n | pass | recall | precision | mean menu | top classes |
|---|---|---|---|---|---|---|
| ambiguous | 12 | 4/12 (33%) | 1.00 | - | 12.5 | leak_forbidden:8, generic_fallback:8, leak_avoid:6, wide_menu:5 |
| calendar | 31 | 22/31 (71%) | 1.00 | 0.45 | 10.5 | not_forced:28, generic_fallback:13, wide_menu:13, leak_forbidden:9 |
| calendar_write | 18 | 14/18 (78%) | 1.00 | 0.23 | 12.3 | not_forced:15, leak_avoid:12, leak_forbidden:4, generic_fallback:4 |
| chat | 15 | 10/15 (67%) | 1.00 | - | 6.3 | generic_fallback:6, leak_forbidden:4, wide_menu:4, leak_avoid:2 |
| code | 12 | 7/12 (58%) | 1.00 | - | 10.8 | generic_fallback:6, wide_menu:6, leak_forbidden:5, leak_avoid:5 |
| compose | 11 | 4/11 (36%) | 1.00 | - | 13.7 | leak_forbidden:7, generic_fallback:7, wide_menu:7, leak_avoid:4 |
| compound | 16 | 4/16 (25%) | 0.50 | 0.33 | 6.5 | missing_tool:11, wrong_domain:5, zero_tool:4, wide_menu:3 |
| device | 24 | 20/24 (83%) | 0.92 | 0.23 | 14.1 | wide_menu:15, generic_fallback:6, missing_tool:2, wrong_domain:2 |
| files | 16 | 11/16 (69%) | 0.88 | 0.15 | 15.0 | wide_menu:12, generic_fallback:4, leak_forbidden:3, missing_tool:2 |
| followup | 28 | 15/28 (54%) | 0.68 | 0.19 | 6.0 | missing_tool:9, wrong_domain:9, generic_fallback:7, zero_tool:6 |
| mail | 26 | 17/26 (65%) | 0.89 | 0.51 | 7.4 | leak_avoid:21, leak_forbidden:6, wide_menu:4, missing_tool:3 |
| mail_draft | 8 | 3/8 (38%) | 0.75 | 0.05 | 10.6 | missing_tool:2, wrong_domain:2, forced_wrong:2, wide_menu:2 |
| mail_send | 10 | 2/10 (20%) | 0.70 | 0.11 | 10.5 | leak_forbidden:5, wide_menu:5, missing_tool:3, wrong_domain:3 |
| memory | 8 | 5/8 (62%) | 0.88 | 0.24 | 9.5 | generic_fallback:4, wide_menu:3, leak_avoid:2, missing_tool:1 |
| memory_write | 6 | 5/6 (83%) | 0.83 | 0.28 | 3.0 | leak_avoid:5, missing_tool:1, wrong_domain:1, not_forced:1 |
| messages | 16 | 14/16 (88%) | 0.94 | 0.70 | 3.9 | leak_avoid:4, missing_tool:1, wrong_domain:1, leak_forbidden:1 |
| messages_draft | 4 | 2/4 (50%) | 0.50 | 0.10 | 7.0 | missing_tool:2, wrong_domain:2, wide_menu:1 |
| messages_send | 10 | 7/10 (70%) | 0.90 | 0.16 | 6.9 | forced_wrong:2, missing_tool:1, wrong_domain:1, generic_fallback:1 |
| named_item | 10 | 2/10 (20%) | 0.60 | 0.16 | 17.0 | wide_menu:8, missing_tool:7, leak_forbidden:7, generic_fallback:6 |
| notes | 16 | 15/16 (94%) | 1.00 | 0.89 | 2.5 | not_forced:16, leak_avoid:2, leak_forbidden:1, generic_fallback:1 |
| notes_write | 10 | 8/10 (80%) | 0.90 | 0.35 | 6.2 | not_forced:10, missing_tool:1, wrong_domain:1, leak_forbidden:1 |
| reminders | 23 | 18/23 (78%) | 0.91 | 0.73 | 4.5 | wide_menu:4, leak_forbidden:3, missing_tool:2, wrong_domain:2 |
| reminders_read | 8 | 7/8 (88%) | 0.88 | 0.78 | 1.5 | not_forced:2, missing_tool:1, wrong_domain:1, forced_wrong:1 |
| web | 23 | 16/23 (70%) | 0.96 | 0.46 | 9.3 | generic_fallback:12, wide_menu:9, leak_forbidden:6, leak_avoid:3 |
| **overall** | 361 | 232/361 (64%) | 0.88 | 0.40 | 8.8 | wide_menu:114, generic_fallback:93, leak_avoid:81, leak_forbidden:79 |

(Generated by the scorer; identical under TZ=UTC and Pacific/Kiritimati. Calibration only ever relaxed an expectation when the router's
behavior is a documented design choice — e.g. a reminder with no clock time or
an am/pm-ambiguous "at 3" is routed to the clarify-time path — never to make a
router bug pass.)

Existing router-only suites on the same base: `audit_router_prompts.py`
(PYTHONHASHSEED=0) 85 prompts, 4 concrete routes flagged, 25 stub-retrieval;
`grade_historical_routing.py` 22/22; router pytest suites 450 passed. The
model-in-the-loop stress suite (`run_routing_stress_suite.py`) was **not run**:
it needs the live model, which is out of bounds on this machine.

## 3. Root causes (129 failing cases)

| # | root cause | failing cases | share | user impact |
|---|---|---|---|---|
| RC1 | **No rule fires → lexical BM25 top-20 grab-bag.** 125/361 prompts (35%) land on retrieval; mean menu 17.5 tools, precision ≈0.2. Recall is fine (only 16/125 lack a needed tool) — the problem is *breadth*: send tools appear in 52 of these menus for reads/chat/code. | 71 | 55% | High (Ling picks wrong among 20) |
| RC3 | **Rule misfire / pre-emption** — a keyword rule wins over the real intent | 30 | 23% | High, specific |
| RC6 | Scoped domain menus too broad (email write set carries `trash_file`, read+write menus on reads) | 13 | 10% | Medium |
| RC5 | Compound under-coverage (second clause's tool missing) | 7 | 5% | Medium |
| RC2 | **Bare confirmation of a pending offer reaches the model with ZERO tools** | 5 | 4% | **Very high** (live failure #3: model claims success) |
| RC4 | Draft-only / negated intent ignored | 3 (+6 counted under RC3) | — | High (live failure #4) |

Key quantified findings:

1. **35% of prompts fall to retrieval, and that is where 55% of failures are.**
   The packaged provider is `lexical` (models.yaml), which always returns 20
   tools + `recall` + `run_shell` with no score floor (the embedding provider
   has a relative floor; lexical does not). Hub tools appear regardless of the
   request: `search_coverage` 33/125, `list_shortcuts` 29, `list_bluetooth_devices`
   27, `find_my_device` 24, `create_tool` 21, `send_message` 23,
   `reply_to_email` 26. Half of the RC1 failures are conversational prompts
   ("write a haiku", "tell me a joke", "explain recursion") that only fail
   because committing send tools sit in the menu; the other half are domain
   paraphrases no rule recognises ("do i have anything on sunday", "what's my
   first meeting tomorrow morning", "find me a free hour tomorrow", "whats my
   costco membership number").
2. **RC2 is reproducible and live.** With any `last_user` in the session (i.e.
   every real conversation), `web_request` marks "sure"/"ok"/"yes please"/"go
   ahead"/"yeah do it" as `acknowledgement_without_offer` unless the offer is a
   *public-web delivery*; `route()` then forbids all 168 tools and sets
   `needs_tools=False`. The same prompts route correctly when `last_user` is
   absent (the `standalone_offer` path), so the history condition — added to
   stop replaying an older web+delivery request — over-blocks every local
   offer (reminder, reply, text, open file).
3. **Local inbox words are classified as "current public information".**
   "any new emails?", "any new texts?", "any important mail this morning" are
   router-direct `web_search` calls (forced, nothing else reachable).
4. **Draft-only intent loses the draft tool.** "draft a reply to Priya but
   don't send it", "compose an email … but don't send", "draft a text to mom …
   but don't send", and the follow-ups "just draft it" / "dont send it yet,
   just save a draft" get read(+mutate) menus with **no** `draft_email`/
   `draft_message`; the negated-send stripping removes the whole outbound side.
   (No route in the corpus forces or direct-calls a send the user did not ask
   for — see §5.)
5. **Rule misfires are a long tail, each a different regex** (RC3, 30 cases,
   17 distinct rules): "set an alarm for 7am" → `add_reminder` forced;
   "do you remember my favorite coffee order" → `remember` (save) forced;
   "remind me what i said about…" / "what was i supposed to remember to do
   today" → reminder *creation*; "find the latest screenshot" →
   `screen_capture`; "whats in report.docx" → `write_document`; "whats the code
   for my storage unit" → "code-related request" with no tools; "text mom that
   i'm on my way" → forced `search_notes` first; "draft an email to Sam" →
   forced `search_notes`. Five compound/assent prompts with a reminder clause
   ("add milk to my grocery note and remind me to shop at 6pm", "sure, remind
   me") hit "unresolved reminder task clauses -> clarify before effects" with
   zero tools.
6. **Typos matter less than paraphrase.** A typo-normalisation prototype
   (tmrw/cal/emial/txt/remeber/wether…) re-routed only 6 of the 71 RC1
   failures to a correct rule. Paraphrases ("anything on sunday", "free hour",
   "group chat", "my locker combo") are the bigger gap.
7. **Semantic (embedding) layer**: not the packaged default; with no live
   embedder it cannot be measured honestly here (the stub embedder is word
   overlap). All numbers above are for the shipping `lexical` provider.

Competing tool descriptions: the hub tools above win BM25 on generic tokens
("find", "list", "my", "check", "what"). This is the lexical analogue of
[[moe-tool-descriptions-compete]]; fixing descriptions one by one would be
another long tail — narrowing the candidate pool by domain is the structural
lever.

## 4. Proposed structural fixes (ranked by impact × frequency)

| # | fix | where | expected gain | risk | tests |
|---|---|---|---|---|---|
| F1 | **Outbound/bulk-destructive retrieval gate.** Retrieval may offer committing outbound tools (`send_*`, `reply_to_email`, `forward_email`, `schedule_send`, `place_call`) and bulk deletes (`clear_reminders`, `clear_past_reminders`, `clear_memory`, `delete_path`) only when the request (or the confirmed offer) shows outbound/delete intent — same mechanism as the existing `fs_write` gate. | `reranker.lexical_candidates`, `semantic.candidates` (shared predicate) | ~25–30 cases (chat/code/compose/ambiguous + reads); precision ↑ on every retrieval menu | Low: only removes tools from menus that lack the verb; sends are confirm-gated anyway, so this is an accuracy fix, not a safety one | corpus red→green; semantic-routing + scoping suites |
| F2 | **Acknowledge a pending *local* offer with history.** Treat a pending, non-public offer in the latest assistant turn as `standalone_offer` even when `last_user` exists, unless the previous user turn was a public-web/delivery request (keeps the tested "no replay of an older web delivery" contract). Add `ya`/`yep`/`sounds good`-style assents to the acknowledgement grammar only if needed. | `web_request._parse_request` | 5–6 cases; closes live failure #3 | Medium: touches a tested authorization contract; mitigated by keeping the web/delivery exclusion and the offer-only replay | test_current_web_routing + new cases |
| F3 | **Personal-data nouns are never "current public information".** email/mail/inbox/texts/messages/calendar/reminders/notes in a "any new / latest / this morning" question → private provenance. | `web_request` provenance/current | 3 cases (high-impact wrong-source answers) | Low | current-web suite + new cases |
| F4 | **Draft-only intent keeps the draft tool.** "draft/compose/prepare … (but) don't send", "just draft it", "save a draft": keep `draft_email`/`draft_message` (and the channel's lookup) while still stripping sends. | `_domain_subset` negation path + follow-up | 6–8 cases; closes live failure #4 | Low–medium | contextual-outbound + new |
| F5 | **Domain prior for the fallback.** When no rule fires, a small normalized lexicon (typo map + synonym table: meeting/appointment/agenda/"anything on <day>"/free hour → calendar; group chat → messages; code/combo/number/password of "my X" → notes+memory) narrows retrieval to that domain's tools instead of the BM25 top-20. Fall-through only (never pre-empts an existing rule), never forces. | new helper used only in `_semantic_core` callers' fallback | ~10–15 cases; menu 17.5 → ~6 on those | Low (fall-through only) | corpus + everyday + scoping |
| F6 | **Router-quality CI gate.** `tests/test_routing_quality_corpus.py`: every case that passes today must keep passing (ratchet list), plus minimum per-domain floors; registered in `run_simulation_qa.py`. | tests | prevents the regex-whack-a-mole regressions | None | itself |

Not proposed as structural work (left as ranked gaps): the 17 individual
regex misfires of RC3 beyond those F2–F4 cover; compound clause coverage
(RC5); `trash_file` in the email write set (RC6 — a scoping bug, reported, fs
writes are confirm-gated); the strict reminder-compound clarify that zeroes
the whole turn.

Latency: F1/F3/F4 are regex/set operations on paths that already run; F5 adds
one dictionary normalisation pass (~µs) on the fallback path only. The router
stays sub-10 ms p95.

## 5. Safety check (P1 screen)

No corpus prompt produces a forced or direct-called send, reply, forward or
delete that the user did not ask for (forced sends appear only for explicit
"send an email to … saying …" / "text Sam if …" requests). Sends offered in
menus are CONFIRM-gated by `safety.policy`; `fs_write`/`fs_delete` (incl.
`trash_file`) are CONFIRM-gated too. **No P1 routing safety issue found.**
