# Wisp optimization backlog

Generated 2026-08-09/10 from a six-lens sweep of the codebase in which every finding was
then adversarially verified against the source by an independent pass. 47 candidates →
**23 REAL, 12 REFUTED, 5 UNCERTAIN**. The verdicts and the win estimates below are the
*verifier's*, not the finder's — several original estimates were inflated and have been
corrected downward here.

**Ground rules for everything below.** Do not change the model roster. Stay under 8.5GB.
Do not degrade output quality. An output token costs **~137x** a prompt token, so a pure
prompt-token saving is a memory/reliability win and **not** a speed win. An unused
`max_tokens` is reserved KV (oMLX admits on prompt + max_tokens): it costs memory, never
time.

---

## Shipped

| # | Change | Effect |
|---|---|---|
| 1 | **Files domain had no SYSTEM block**, and `run_shell`'s "defer to the dedicated tool" list omitted files. Added the block, fixed the list, rewrote `list_dir`'s 32-char description (incl. "use `~`, you don't know the account name") | `list_dir` 2/5 → **6/6**; 62.2s → 14.9s; `test_model.py` reached **22/22** for the first time |
| 2 | **`narration_after`** — `multi_round` was a whole-turn veto on the narration gate; now a *condition* satisfied once a route's declared required tools have all answered | aggregate reasoning −78%, ambiguous −73% |
| 3 | **Retry ladder sent byte-identical requests.** `step_msgs` was built above the attempt loop and `_fit_window` returns a copy, so the correcting nudge never reached the model — and forced steps are greedy, so retries were deterministic repeats | 2 of 3 attempts on every failed forced step were provably wasted |
| 4 | **Profile MAP/reduce ran with thinking ON** into a 6,000-token ceiling | **103.0s → 17.0s, 0 facts → 31 facts.** The old arm produced *nothing*, ~50-70 times per build |
| 5 | **Smart Search synthesis ran with thinking ON** into a 400-token ceiling; its `reasoning_effort="low"` is dead config (harmony-only) | **6.4s/0 chars → 0.7s/cited answer.** Every ⌘⇧F was returning `{"error": "empty completion"}` |
| 6 | **Rolling conversation summary** (`maybe_summarize`) — same bug at a 240-token cap, so `summarized_idx` never advanced and the doomed call re-ran every turn on a growing transcript | Long sessions silently lost early context; now folds in batches of 8 |
| 7 | **Sync tools ran on the event loop** — 18 of them, up to `subprocess.run(timeout=120)` | `asyncio.to_thread`; heartbeats and cache syncs no longer freeze |
| 8 | **`short_circuit_tools` could end an aggregate turn on one of four sources** | Now blocked by `multi_round` *and* `tools_answered <= {name}` (also catches the cross-step case) |
| 9 | **Fallbacks surfaced only the last tool result** — a 4-source read answered from 1 | `_merge_results` labels every clean source |
| 10 | **`tools_failed` was a permanent latch** — one bad-args TypeError disabled the narration win for the rest of the turn | Split into recoverable `failed_tools` (clears when that tool later succeeds) and permanent `hard_failed` (policy outcomes) |
| 11 | **`_fit_window` could drop the user's question** (the guard was positional, and stops being true after the first tool call) and **counted `tool_calls` payloads as zero** | Identity-based guard; `_msg_tokens` counts the payload |
| 12 | **Summarizer ceiling was sized for a think block that no longer exists** | 4000 → 2500 after measuring (widest real output 1,003 tokens); ~1,500 tokens of KV freed per summary call |
| 13 | **`search_notes` returned 5 notes for every call** — "what's in my notes?" answered from 5 of ~100 unless the model reasoned its way to a bigger `count` | Browse 15 / search 5 |
| 14 | **Confirmation cards never timed out** — `InteractiveApprover.confirm()` awaited its future forever, and because `idle.begin_foreground()` brackets the whole `/agent` request, a stuck card silently disabled the daily brief and profile rotation for as long as it sat unanswered | 5-minute timeout that auto-DENIES (fail safe) and emits `confirm_timeout` so the client can clear a stale card |
| 15 | **Router-direct dispatch** (was #1 below). `RouteDecision.direct_calls` carries calls the router resolved in full; `run_agent` executes them through the same `decide()`/approver/`run_tool`/`audit` path before its first model call | Measured live: battery **1.42s**, calendar-today **2.88s**. `summarize my inbox` short-circuits to **zero** agent-loop model calls (8.17s is entirely the summarizer's own synthesis) |

**On #15's quality claim, measured rather than asserted:** `is my wifi on` — a
device read with no direct-dispatch rule — took **14.03s and made zero tool
calls**, answering from the model's own memory. That is the failure a
pre-executed call makes structurally impossible, and it is worth more than the
3s.

**Measured result across 12 routes** (`bench.py`; before = pre-session baseline):

| | before | after |
|---|---|---|
| chain-of-thought generated | 25,847 | 6,788 (**−74%**) |
| ANSWER content | 8,088 | 9,274 (**+15%**) |
| model generations | 25 | 22 |
| wall time | 229.9s | 133.4s (**−42%**) |

Correctness: `scripts/test_model.py` **22/22**. Test suite **399 passing** (was 326).

---

## REAL, not yet built — ranked

### 1. `fast` role still generates chain-of-thought on trivial chit-chat
`service/main.py` — `no_thinking_kwargs` is never wired into the `fast` path. **~1–4s** per
greeting/ack (not the ~15s originally claimed — a greeting's monologue is short), plus
~356MB of KV admission held during a ~20-token answer. Low frequency, but it's the
interaction users expect to be instant.

### 2. Smart Search prewarm gate is permanently false
`service/main.py` — the gate asks "is the agent model resident", which is now *always* true.
The prize is `/search/prewarm`, not `/search/warm`: `index_document` writes into
`embedder._cache`, a plain LRU of normalized vectors that **survives model eviction**, so
prewarming permanently removes the embed cost for that document. The model preload is worth
much less — the next chat turn's exclusive `ensure_only` evicts it again.

### 3. Engine death mid-turn destroys all completed tool work
`service/main.py` — one in-place step retry after `ensure_omlx()`. Budget it as a **recovery
costing 10–60s** (the restart path can block 30s + 60s and must make a 4.29GB model
resident), not a free retry — but it beats losing 30–90s of completed tool calls plus
re-approved confirmations.

### 4. `view_emails` truncation (two findings)
`service/tools/email_tools.py` — (a) it silently drops all but the newest `count` matches, so
the model asserts completeness it doesn't have; (b) no per-email body cap, so the loop's
*tail* truncation deletes the **newest** emails, which are the ones the question is about.
Fix by bounding per-email body at the source and announcing the drop.

### 5. Aggregate phrases miss the router
`service/router/router.py` — "what am I forgetting", "catch me up" fall through to the core
route. Narrower than it first looks: the aggregate SYSTEM block *is* still shipped there
(because `get_upcoming`/`search_notes` are in `_CORE_TOOLS`), so the model is already told to
call all four. What's missing is the narrow toolset, the forced first call, and the warm
voice. Sell it as **selection reliability** (14 tools → 4), not speed.

### 6. Small quality repairs
- `_is_looping` returns the canned "I can't do that" **even when a tool already produced the
  answer** (~5 lines; rare but a confidently false refusal)
- summarizer empty-fallback ships raw `sender | subject` lines **unlabelled** as the summary
  — the label costs nothing and turns an invisible quality cliff into a visible one

---

## UNCERTAIN — one measurement each, before building

| Item | The measurement it needs |
|---|---|
| **Calibrated concision directive** (`SYSTEM` says "Keep answers concise" on every route; `_LIGHT_READ_STYLE` literally says to ignore it) | Highest blast radius in the batch — rewrites a directive on every turn. A *longer, more permissive* directive plausibly increases output, which costs at 137x in the wrong direction. Needs an A/B on answer quality **and** length before touching. |
| **Per-turn tool-result budget** (`_MAX_TOOL_RESULT_CHARS` is per-result) | The "4 × 12,000 chars ≈ 1.5GB" premise is fabricated — real results are 800–4,300 chars. But there *is* a genuine truncation path the finder missed: `email_tools` sets `count = max(count, 10)` on a no-exact-match query, and 10 full bodies exceed 12,000. Measure how often that fires. |
| **Summary result cache** | The flagship repeat scenario is **dead code** (`run_daily_email_summary` is never called). The real hit is `_ALL_SOURCES` running `summarize_*` at default `count=20`, byte-identical to a plain "summarize my emails" — so "what do I need to do" then "what's in my inbox" is a genuine hit. Measure the frequency. |
| **Think-brevity rule in SYSTEM** | The finder's mechanism is wrong: the proposed slot is the *lowest*-recency position, not the highest. And telling a 4B model to deliberate less on a greedy `required` step is a weaker version of the measured 0/3 condition. |
| **Scope `_LIGHT_READ_STYLE` to the answer only** | Facts check out (it's in front of the model on the greedy selection step), but "the model reasons longer because it was told to be expansive" is an inference with no number attached — and the warm voice is the product's signature, carried entirely by this text. |

---

## REFUTED — do not re-propose

- **Range-summary output explosion** — the finder inverted the code's own tuning note. Live
  measurement: "this month" samples **15** conversation labels, not "dozens", 10 of them
  ≤2 messages. Capping to 8–10 sections would *replace* one-line sections with a rollup —
  pure quality loss for a near-zero decode win.
- **Parallel tool execution** — already built, measured (27.3s → 37.3s), crashed, reverted.
- **Speculative decoding (DFlash)** — blocked upstream; every Qwen3.5-compatible draft is a
  GDN hybrid the loader rejects.
- **Re-adding an LLM classify step to the router** — removed after mis-classifying ~15% of
  tool-needing requests.
- Also refuted: retry-narrow-schema, light-read-style padding, salvage-think-leak,
  aggregate-style-hint-overrides-grouping, aggregate-source-window,
  system-control-schema-prose, ensure-email-cache-dead-wait,
  carry-compressed-in-turn-reasoning, reasoning-ceiling-forced-conclusion,
  dedupe-system-prompt-vs-tool-descriptions.

---

## The pattern worth remembering

Four separate bugs in this sweep were the **same shape**: a summary-shaped call (source text
already in the prompt, nothing to work out) left with thinking ON under a small `max_tokens`.
The think block eats the ceiling, `_demote_unclosed_think` blanks the content, and the caller's
"no content" fallback silently discards the result. It is invisible because nothing errors —
you get an empty profile, an empty search answer, a summary that never folds.

**When adding any model call that summarizes or extracts from text already in its prompt,
pass `no_thinking_kwargs(model)`.** Then assert on `finish_reason` — `"length"` on these
paths is a quality bug, not a slow one.
