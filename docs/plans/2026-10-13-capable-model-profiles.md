# Let a stronger model be stronger: capability profiles for Wisp

**Status:** proposed, scoped, not started. **Written:** 2026-10-13 (scoped the same day).
**Base:** `origin/main` @ `efac59d`. **Coordinator:** Claude (Local). **Builders:** one Worktree per PR below.

## 1. Why

At the hackathon Wisp ran every role on `qwen3.6:35b` (GX10, Tailscale). The 35B did every job Ling does
and was clearly better, but not *night and day*. The limiter is Wisp. Everything Wisp does to protect a
4B model is a literal in code, so it applies to every model:

| # | Constraint | Where (main @ efac59d) | Hackathon effect |
| --- | --- | --- | --- |
| 1 | 35-rule, 23,475-char system prompt (~6k tokens); "Keep answers concise" | `service/agent/loop.py` `SYSTEM` | 35B obeyed 4B-era patches; terse |
| 2 | Router steers: 27 forced-first-tool routes, 19 direct-call routes, `resolved_request` *replaces* the user's words | `service/router/router.py` (8,256 lines); `main.py:1664` | model never chose its approach |
| 3 | History = `window − 11,500` (four guessed constants); rolling summary is 6 lines / 400 tokens; hackathon pinned 6,000 | `service/memory/context.py`; `_REMOTE_FULL_HISTORY_TOKENS` | 32k window, few turns visible |
| 4 | Model-led routing only on the managed local engine; embedding retrieval skipped off-device | `main.py:1460-1471`; retrieval gate | strong model forced onto the rules router with keyword retrieval |
| 5 | Output budgets sized for no thinking: agent step 3000, search 400/700, brief 512, extractors 384–650 | `loop.py:1140`, `synth.py:307`, `brief.py:726`, `extract.py` | reasoning ate the budget → short/empty answers |
| 6 | No thinking lever on remote: payload allowlist omits `chat_template_kwargs` / `reasoning_effort` / `think` | hackathon `remote_policy.py` | nothing tunable |
| 7 | Context disclosure keyed on endpoint *name*: `local_provider` and cloud get bare prompts, no memory, no skills | `main.py:1041`, `_agent_memory_context_allowed` | 35B knew less about the user than Ling |

**Principle.** One **capability profile** per model drives every limit above. Ling's profile reproduces
today's requests byte-for-byte (a golden test enforces it), so 1.3.x behavior is untouched. A `capable`
profile lifts the 4B-era limits. **No safety boundary changes for any tier:** approval gating, the
never-claim-an-unseen-action rule, untrusted-web, private-egress guards, source exclusions and argument
bindings stay identical.

### Already in the codebase (reuse, don't rebuild)

- `search/synth.py::pick_model(prefer_capable=…)` — an existing "capable vs fast" notion, scoped to Smart Search.
- `memory/context.py::maybe_summarize` — rolling history summary exists; it is just weak (see Phase 3).
- `inference/qualify.py` — measures real context and tool calling; extend it with a thinking-lever probe.
- `router/model_led.py` (1.3.1, default-off) — discovery tool, catalog index, `routing_guard_contract()`.
- `scripts/eval_core.py` + `eval_ling.py` + `eval_server.py` — human-graded eval runner with a browser board; Phase 0 adds arms to it.
- `test_fixtures/routing/quality_corpus.json` — 439 cases, scorer `scripts/score_routing_quality.py`, CI ratchet `tests/test_routing_quality_corpus.py` (floor 370).

---

## 2. Target model (decision needed, with a default)

The GX10 and remote-only mode are deprecated (2026-10-11). The profile is model-agnostic, but Phase 0 needs
one concrete capable model.

**Default recommendation:** the Mac mini (64 GB) running **Qwen3.6-35B-A3B under oMLX**, reached over the
tailnet. Reasons: same model class as the hackathon; oMLX means `chat_template_kwargs.enable_thinking`
works unchanged and the reasoning parser splits `reasoning_content`; the mini-as-quality-node decision
(2026-09-12) already allows sensitive data across the tailnet. The alternatives (a local provider on the
Pro, the cloud Super Model) are supported by the same profile but measure something different.

Two config consequences to decide with it:
- `role_target()` refuses `fast` and `router` off the managed engine. For a `trust: private` node (Phase 1b)
  this should be relaxed, because the hackathon showed one strong model handling the fast role fine.
  Keep `embedding`/`reranker` local regardless (small, latency-bound).
- `_direct_generation_budget` caps `local_provider` at 2,048 output tokens ("Ling's verified 2k cap").
  That cap moves into the compact profile and stops applying to any other model.

---

## 3. Phase 0 — A/B harness and constraint-cost table

**Goal:** a ranked table of how much each constraint costs on the capable model, and a regression harness
every later phase reuses. Nothing ships without it.

**Build on `scripts/eval_core.py`.** Add an `--arm` dimension:

| Arm | What runs |
| --- | --- |
| `raw` | user prompt + the same synthetic tool results, 300-token neutral system prompt, model's defaults |
| `wisp` | the full pipeline as today |
| `wisp-no-prompt` | `wisp` with the capable prompt (Phase 2 draft) |
| `wisp-no-steer` | `wisp` with steering fields cleared (Phase 4 split, see §7) |
| `wisp-history` | `wisp` with the overhead constants measured (Phase 3) |
| `wisp-budgets` | `wisp` with profile budgets + thinking on |

**Corpus (all synthetic; isolated backend on 8775 + sandbox; never the installed app or real data):**
- `quality_corpus.json` (439; action correctness via the existing scorer) — model-led vs rules.
- `open_quality.json` (new, ~40): explain / draft / plan / compare / multi-turn follow-up.
- `long_context.json` (new, ~20): referents 10+ turns back ("reply to the one from Tuesday").
- `summaries.json` (new, ~15): inbox / messages / brief on synthetic data, scored for grounding.

**Metrics per arm:** blind pairwise preference (judge = a strong model, position-swapped, two passes);
tool-call correctness; `finish_reason=length` and empty-answer rate; leaked `<think>` rate; p50/p90
latency; peak RSS for local arms.

**Models:** Ling V2 (`Ling-3.0-tiny-Wisp-V2-merged-oQ4e`, standing test rule) and the capable model from §2.

**Deliverables:** `scripts/eval_arms.py`, three fixture files, `docs/evals/2026-10-xx-constraint-cost.md`
with the ranked table. **Acceptance:** every arm runs end-to-end on both models; judge agreement ≥ 0.8
between the two position-swapped passes.

**Size:** 1 PR, ~3 builder-days. **Review:** low-risk (scripts/fixtures), no Auditor; Control Center records why.

---

## 4. Phase 1 — Capability profile and endpoint trust (foundation)

### 1a. `ModelProfile`

New `service/config/profiles.py`, resolved once per `Target`:

```yaml
# models.yaml
profiles:
  compact:                      # Ling, A1 — today's exact numbers
    prompt: compact
    routing: rules              # model_led once 1.3.2's eval passes
    thinking: {lever: chat_template_kwargs, default: per_site}
    history: {mode: formula_v1, overhead: 11500, min: 1500, keep_messages: 40, fold_batch: 8, summary_tokens: 400}
    budgets: {agent_step: 3000, search_short: 400, search_long: 700, brief: 512, extract: 384, capture: 650, direct_chat: 8000, local_provider_direct: 2048}
    tool_result_chars: {builtin: 12000, web: 3000, action: 3000, mcp: 8000}
    inner_calls: fast
  capable:
    prompt: capable
    routing: model_led
    thinking: {lever: qualified, default: on, reserve: 4096}
    history: {mode: measured, keep_messages: 60, fold_batch: 8, summary_tokens: 1200, pin_referents: true}
    budgets: {agent_step: 8192, search_short: 2048, search_long: 3072, brief: 2048, extract: 1500, capture: 1500, direct_chat: 8192}
    tool_result_chars: {builtin: 24000, web: 16000, action: 8000, mcp: 16000}
    inner_calls: agent
```

- A model is `capable` only by explicit `models.yaml` config. Never guessed from name or size.
- `Target` gains `.profile`. Every literal in the table in §1 row 5, plus `_MAX_TOOL_RESULT_CHARS`,
  `_MIN_OUTPUT_TOKENS`, `KEEP_MESSAGES`, `_FOLD_BATCH`, `_DEFAULT_CONTEXT_WINDOW`, and the `else 8000` in
  `endpoints.py:162` read the profile. Unknown window on a capable target → qualify or refuse the tier;
  never silently 8,000.
- `inner_calls`: `brief.py:732`, `imessage_tools.py:1694`, `synth.py:157/198`, `memory/capture.py`,
  `attention/extract.py` resolve their model through `profile.inner_calls` instead of hardcoding `fast`.

### 1b. Qualified thinking lever

Extend `qualify.py` with a `thinking` check that sends, in order, `chat_template_kwargs.enable_thinking`,
`reasoning_effort`, Ollama `think`, and records which one produced a separate reasoning channel (or
suppressed it). `Report` gains `thinking_lever`. `no_thinking_kwargs(model)` becomes
`thinking_kwargs(target, on: bool)` and dispatches on the lever; `no_thinking_capable` in `models.yaml`
becomes the compact fallback for models never qualified. The remote payload allowlist (if a tailnet
endpoint keeps one) admits the qualified lever's field.

### 1c. Endpoint trust class

`endpoints.py` `Endpoint` gains `trust: local | private | public`. `_local_provider_direct_messages`,
`_agent_memory_context_allowed`, `allow_skill_context` and `super_model_cloud`'s bare-prompt rule key on
**trust**, not on endpoint name. `private` (tailnet node) gets memory, skills, identity and history like
`local`. `public` stays bare-prompt, Laya-gated, unchanged.

**Deliverables:** `profiles.py`, `models.yaml` schema, `qualify.py` thinking check, trust field, call-site
swaps, `docs/inference-providers.md` update.
**Tests:** `tests/test_model_profiles.py` — golden test asserting the compact profile produces
byte-identical request payloads for 12 recorded turns (agent, chat, brief, search, capture); qualify
probe tests against a fake engine for all four levers; trust-class disclosure tests (private gets
memory, public does not).
**Acceptance:** golden test green; full gate green; `tests/test_inference_endpoints.py`,
`test_inference_providers.py`, `test_cloud_provider_settings.py`, `test_super_model_routing.py` extended.

**Size:** 2 PRs (1a+1b, then 1c), ~5 builder-days. **Review:** Auditor on both (inference config,
privacy boundary). Specialist Live QA not needed (no outbound actions, no migration).

---

## 5. Phase 2 — Tiered system prompt

### Classification of the 35 current rules

| Bucket | Count | Examples | Capable tier |
| --- | --- | --- | --- |
| **Core contract** | 7 | call the tool, read-only auto-runs, report actual result, never claim an unseen action, you CAN send, remember durable facts, run code for computation | keep, tightened |
| **Reasoning-over-evidence** | 3 | "not every tool result is the answer", aggregate questions, live external facts → web | keep, shortened to one rule each |
| **Tool disambiguation** | 20 | summarize vs view emails, calendar vs reminder, cancel vs clear, volume/battery/backlight/music, notes, files, reorganize, get_upcoming includes reminders, account param, archive ≠ delete, recall | **move into the tool descriptions and the model-led catalog index**; drop from the prompt |
| **Compact-model patches** | 5 | state the date without a tool, capability questions, create_tool ×3 | compact only (create_tool stays wherever create_tool is offered) |

Moving the 20 disambiguation rules into tool descriptions helps **both** tiers: the memory
`moe-tool-descriptions-compete` already records that a terse description loses to a verbose rival, and
under model-led discovery the description *is* what the model sees at selection time.

### Work

- `service/agent/prompts.py`: `build_system(profile, *, light_read, clarify, synthesis_guidance,
  workflow_block)`. `compact` reproduces today's string exactly (golden test). `capable` = core contract
  (bulleted **IMPORTANT rules**, your preferred style) + reasoning-over-evidence + a length rule: "Match
  length to the question: brief for actions and lookups; thorough when asked to explain, plan, compare or
  draft." Target ≤ 1,500 tokens.
- Migrate the 20 disambiguation rules into their tools' `description` fields (`service/tools/*`) and the
  catalog index text in `router/model_led.py`. Each migrated rule keeps its origin-failure comment.
- `ROLE_SYSTEM["general"]` for capable: who Wisp is, what it can reach, how to use the memory block.
  `reasoning`/`coding` prompts reviewed for the same.
- `_LIGHT_READ_STYLE` stays for both tiers (it is a voice, not a crutch) but loses "ignore the concise
  instruction above" on capable, since that instruction is gone.

### Method

Rule by rule, replay each rule's origin-failure prompt (the `# Verified failure 2026-xx-xx` comments)
on the capable model **without** the rule, three samples at production sampling. Promote to core only if
the capable model also fails. Record the matrix in `docs/evals/2026-10-xx-prompt-rules.md`.

**Tests:** golden compact-prompt test; `scripts/test_replay_failure_fixes.py` on compact (must stay
green); capable-arm run of Phase 0 before/after; `test_latency_prompt_contract.py` updated for the
tier split.
**Acceptance:** compact byte-identical; capable prompt ≤ 1,500 tokens; Phase 0 `open_quality` preference
for `wisp-no-prompt` ≥ `wisp` with no regression on `quality_corpus`; replay suite green.

**Size:** 2 PRs (prompt builder + tier; description migration), ~4 builder-days. **Review:** Auditor on
the prompt PR (safety surface). Description migration is mechanical → no Auditor, recorded.

---

## 6. Phase 3 — History budget (constraint #3)

### What exists and why it under-delivers

- `history_budget = max(1500, window − 11500)`. The 11,500 is `4000 + 2500 + 3000 + 2000` — four guesses
  from `context.py:36`. Under model-led discovery the schema term is far smaller; under the unscoped rules
  route it is ~9,500. Either way the number is wrong in one direction.
- `maybe_summarize` folds turns older than `KEEP_MESSAGES=40` in batches of 8 into a **6-line, 400-token,
  thinking-off** summary — and it is awaited before the SSE stream closes, so it is live tail latency.
- `_fit_window` evicts the oldest turns on overflow. Correct ordering, but silent: "reply to that" after a
  long turn just fails.

### Changes (capable tier; compact unchanged unless measured better)

1. **Measured overhead.** `history_budget(target)` = `window − tokens(system block) − tokens(offered
   schemas) − profile.budgets.agent_step − profile.thinking.reserve − profile.tool_result_reserve`,
   computed from the actual blocks for this turn (they are already built before `build_messages` runs;
   reorder `main.py:1664` so the prompt is assembled first). On a 32k window with discovery this frees
   ~15–20k for history vs. ~20k reserved today.
2. **Structured rolling summary.** `summary_tokens` from the profile (1,200 capable). Prompt asks for
   sections: *facts stated by the user*, *decisions*, *open threads*, *items referred to by id/name
   (emails, events, reminders, files) with their identifiers verbatim*. Thinking per profile.
3. **Fold off the live path.** Schedule `maybe_summarize` after `done` is emitted, as a low-priority
   task that yields to a live turn (same rule as the background-contention memory: never contend with a
   live turn on the single resident model; on a private node contention is cheaper but the ordering
   stays).
4. **Pin referents.** When an assistant turn listed enumerable items (the `routed`/tool events already
   carry ids), mark that turn `pinned_until_idx = idx + N`. `_fit_window.droppable()` and
   `build_messages` skip pinned turns until N later turns pass. Resolves "the second one" after
   compaction.
5. **Recall path.** Capable prompt states that older context is reachable via `search_conversations`.

**Owned paths:** `service/memory/context.py`, `service/memory/store.py` (pin column, migration),
`main.py` `_tool_turn_messages` / `_history_budget` / fold scheduling, `loop._fit_window`.
**Tests:** extend `tests/test_conversation_memory.py` (52 tests today): 10/20/40-turn synthetic chats with
referents at depth 5/15/30 → referent resolves; window never exceeded (assert on the fitted request);
fold runs after `done`; summary keeps ids verbatim. `test_read_context_continuations.py` updated.
**Acceptance:** Phase 0 `long_context` pass rate ≥ 90% on capable (baseline measured in Phase 0);
no change on compact unless the compact variant is also adopted after measurement.

**Size:** 2 PRs (budget + fold scheduling; pinning + structured summary with store migration),
~5 builder-days. **Review:** Auditor on the store-migration PR (persisted data).

---

## 7. Phase 4 — Tool selection and router (constraint #4)

### What 1.3.2 gives and the one gap

`model_led.py`: `get_tool_schemas` discovery, ≤2 discoveries, ≤24 schemas / 32 KB, 24 KB index,
`routing_guard_contract()`, deterministic claims stay deterministic. Default-off pending the implicit-load
fix and Ling V3. **Gap:** `main.py:1460-1471` enables it only when `role_target("agent")` is the
*managed local oMLX* connection; anything else raises. The model that can do model-led well is the one
excluded.

### The steering/guard split (the heart of this phase)

`Decision` fields, classified:

| Steering (drop for capable; keep for compact) | Guard (keep for every tier) |
| --- | --- |
| `force_first_tool`, `expect_tool_first` | `forbidden_tools` |
| `direct_calls` | `conditional_tools` |
| `tool_subset` (→ discovery index) | `tool_argument_bindings` |
| `required_tool_groups`, `multi_round`, `narration_after` | `verified_results_only`, `strict_read_limits` |
| `resolved_request` *as replacement* | `reminder_action` (clarification is legitimate) |
| `light_read` style selection | source exclusions, `routing_guard_contract()`, `PRIVATE_EGRESS_TOOLS` after a personal read, approval policy |

`run_agent` already takes each of these as a parameter; the change is in `main.py` dispatch: for
`profile.routing == "model_led"`, pass the guard set and leave the steering set empty.

### Changes

1. **Gate on profile.** `model_led_turn = profile.routing == "model_led" and "tools" in target.capabilities`.
   Keep the managed-local check only as the compact tier's additional condition. The `ValueError` at
   `main.py:1470` becomes "fall back to rules with a `routed.reason`" for compact, and a hard error only
   when a capable profile has no qualified tools.
2. **User's words, router's facts.** For capable, `user_msg.content = prompt`. `resolved_request`
   becomes a one-line runtime system note: `Resolved for this turn: "mom" → Trisha Jain (Contacts);
   "next week" → Oct 19–25.` Built from the same router output; nothing new is computed. Source
   exclusions ("Calendar was excluded…") stay as notes too — they are guards, so they are always sent.
3. **Discovery replaces retrieval.** For capable, the catalog index is the menu; the embedding-retrieval
   call in `main.py` (gated on `retrieval_provider != "lexical"`) is skipped, removing the engine
   dependency the hackathon tripped on. Compact keeps embedding retrieval on the local engine.
4. **Discovery limits from the profile.** `MAX_SCHEMAS`/`MAX_SCHEMA_BYTES`/`MAX_DISCOVERIES` read
   `profile` (capable: 40 / 64 KB / 3). The hackathon 32-tool slice is gone on main already; keep a
   byte guard only.
5. **Short-circuits follow the profile.** `_PRESYNTHESIZED_TOOLS` short-circuit applies only when
   `profile.inner_calls == "fast"`. For capable, `summarize_emails`/`summarize_messages` results return to
   the agent like any tool result, so one answer can combine a summary with other reads.
6. **`pick_model(prefer_capable)`** in `synth.py` reads the profile instead of its own heuristic.

**Owned paths:** `main.py` dispatch (lines ~1450–1530 and ~1660–1800), `router/model_led.py` limits,
`agent/loop.py` short-circuit condition, `search/synth.py::pick_model`. **Not** `router/router.py` rules
themselves — they keep serving compact.
**Tests:** `quality_corpus` on capable, model-led vs rules, must be ≥ rules on the action cases;
`test_router_scoping.py` + `test_router_no_vision.py` + a new `test_router_steering_split.py` (every
guard field survives the capable dispatch; every steering field is cleared; the resolved-note text is
exact); safety suite (`test_execution_contract_loop.py`, `test_router_execution_contract.py`,
`test_contextual_outbound_routing.py`) identical on both tiers; `routing_quality_corpus` ratchet
unchanged for compact.
**Acceptance:** capable model-led ≥ 370/439 on the corpus and ≥ rules on `wisp-no-steer` preference;
zero forbidden-tool or egress regressions.

**Dependency:** lands **after** 1.3.2's implicit-load fix merges (it touches the same dispatch block).
**Size:** 3 PRs (gate + steering split; resolved-note + user's words; short-circuit + discovery limits),
~6 builder-days. **Review:** Auditor on the first two (routing + privacy guards). Specialist Simulation
QA on the first (tool-selection risk), synthetic state only.

---

## 8. Phase 5 — Thinking per site

### Current state and recommendation

| Site | Today | Compact (Ling) recommendation | Capable | Budget change |
| --- | --- | --- | --- | --- |
| Agent tool-selection steps | on | keep on (off = 0/3 calls) | on | step 8192 |
| Agent narration (`narration_mode=broad`) | off | **on for compound turns and after any tool error**; off for single-read narration (`strict` carve-out already exists) | on | +reserve |
| Plain chat general/reasoning/coding | on | keep | on | 8192 |
| Plain chat `fast` | off | keep off | off | — |
| Daily brief, iMessage digest | on | keep | on | 2048 |
| Smart Search synthesis | off | **try on**, budget 400/700 → 1,800/2,400; ⌘⇧F latency is the cost, measure | on | profile |
| Memory capture (`capture.py`) | off | **on, idle-only** | on | 650 → 1,500 |
| Memory query context (`context.py`) | off (live path) | keep off | on | — |
| Memory connections | off | **on, idle-only** | on | — |
| Attention extract | off, 384 | **on**, budget 1,500; precision is the feature's main risk | on | 384 → 1,500 |
| Rolling summary (`maybe_summarize`) | off, 400 | off on compact (it was the fix for an infinite-retry bug); on for capable at 1,200 | on | per profile |
| Research orchestrator JSON steps | off | keep off for planning JSON | on | — |

### Rules

- **Budget = answer + reasoning.** Each site turned on raises `max_tokens` by `profile.thinking.reserve`.
  Never a thinking-budget cap on Ling (forced `</think>` leaks the monologue — `moe-thinking-budget-leak`).
- **Measure at production sampling** (oMLX profile; Wisp omits the params). Forced sampling inverts the
  answer (`moe-sampling-thinking-interaction`).
- **Latency gate:** decode is 91% of wall time (~137× per token). Every compact site ships with p50/p90 on
  Ling V2 beside its quality score. Background sites run idle-only (never contend with a live turn).
- **Leak guards stay:** `_strip_think`, `_demote_unclosed_think` and the empty-content fallback on every
  newly enabled site.

**Deliverable:** per-site matrix in `docs/evals/2026-10-xx-thinking-sites.md`; one small PR per adopted
site (each is localized → Luna-class work, no Auditor unless it touches the live agent path).
**Tests:** extend `test_narration_thinking.py` (12) and `test_user_facing_summary_thinking.py` (6) per
site; leak-guard tests on each.
**Acceptance:** adopt a compact site only if quality improves and p90 stays within its budget (narration
≤ +2s, search ≤ +1.5s, background unconstrained). Capable: on everywhere except `fast`.

**Size:** ~6 small PRs, ~4 builder-days total; the Ling sites need only Phase 0's harness.

---

## 9. Sequencing, parallelism, ownership

```
Phase 0 (harness) ──┬── Phase 1a+1b (profile, lever) ── Phase 1c (trust)
                    │          │
                    │          ├── Phase 2 (prompt)      ┐ parallel worktrees,
                    │          ├── Phase 3 (history)     │ disjoint paths
                    │          └── Phase 4 (router) ←── 1.3.2 merge
                    └── Phase 5 Ling sites (can start with Phase 0)
```

| Order | PRs | Builder-days | Owned paths | Review |
| --- | --- | --- | --- | --- |
| 0 | 1 | 3 | `scripts/eval_arms.py`, `test_fixtures/evals/*` | none (recorded) |
| 1 | 2 | 5 | `config/profiles.py`, `models.yaml`, `qualify.py`, `endpoints.py`, call-site literals | Auditor ×2 |
| 2 | 2 | 4 | `agent/prompts.py`, `tools/*` descriptions, `model_led.py` index text, `ROLE_SYSTEM` | Auditor ×1 |
| 3 | 2 | 5 | `memory/context.py`, `memory/store.py`, `main.py` history/fold, `loop._fit_window` | Auditor ×1 |
| 4 | 3 | 6 | `main.py` dispatch, `model_led.py` limits, `loop.py` short-circuit, `synth.py::pick_model` | Auditor ×2, Sim QA ×1 |
| 5 | ~6 | 4 | one site each | none unless live path |

**Total:** ~16 PRs, ~27 builder-days; with Phases 2/3/5 in parallel after Phase 1, roughly three to four
calendar weeks on top of 1.3.2. Caps: at most two proactive worktrees live at once per `AGENTS.md`;
Phase 4 is user-requested and does not count against that cap.

**Model routing for builders** (`AGENTS.md` table): Phases 0 and 5 and the description migration are
bounded → Luna/xhigh. Phases 1, 2 (prompt), 3 and 4 are cross-component → Sol/high. No Astra.

---

## 10. Risks

- **Golden test is the regression firewall.** If Phase 1's compact byte-identity test is skipped or
  loosened, 1.3.x behavior drifts silently. It is a required check, never waived.
- **Judge bias in Phase 0.** Mitigated by position-swapping and two passes; disagreement > 0.2 blocks the
  table from being used for ordering.
- **Private-node disclosure.** Phase 1c widens what a `private` endpoint receives. The trust field is set
  only by explicit config and the public tier is untouched; Auditor-reviewed.
- **Phase 4 collides with 1.3.2.** Sequenced strictly after; the implicit-load fix owns the dispatch block
  until it merges.
- **Capable prompt loses a needed rule.** The rule-by-rule replay in Phase 2 catches it per rule; the
  Phase 0 corpus catches interactions.
- **Mini availability.** If the mini isn't ready, Phase 0 can run the capable model as a local provider on
  the Pro (memory-constrained, no concurrency) with the same harness; it measures prompt/router/history
  costs correctly and budget/latency costs pessimistically.

---

## 11. Build log

**2026-10-13 — scope narrowed to Ling first.** The user asked to build this out optimized for
`Ling-3.0-tiny-Wisp-V3-merged-oQ4e` (the live resident on every text role). The capable tier waits;
everything below is the compact-tier work from Phases 0, 3 and 5, in the order the measurements allow.

**Slice 1 (branch `worktree-claude+ling-v3-profile-opt`, base `efac59d`): experiment switches + arm
harness.**
- `service/config/experiments.py`: `WISP_EXPERIMENTS` launch-time switches, all off by default, unknown
  names fail at startup. Switches: `narration_think_compound`, `search_think`, `capture_think`.
- `agent/loop.py` narration gate: one added conjunct; `search/synth.py`: thinking + 1800/2400 budgets
  under the switch; `memory/capture.py`: thinking + 1500 under the switch (idle-only worker).
- `scripts/eval_arms.py`: one isolated backend + sandbox server per arm, the `eval_ling_tasks` prompt set
  through the real `/agent`, six built-in Smart Search cases through `/search`, mechanical metrics table,
  blind pairs file for the eval board. `scripts/eval_core.py` gained a per-call `base` URL.
- Tests: `tests/test_experiments.py` (parsing, default-off, gate lockstep, wiring);
  `tests/test_narration_thinking.py` still 47/47.

**Finding that reshaped Phase 5 (2026-10-13, smoke run + direct oMLX probes).** V3 emits **zero
reasoning** under its default template on every prompt shape and still selects tools correctly (14
tokens, 0.4s). It does think when `enable_thinking=True` is sent explicitly (3–4k chars), at ~10× the
latency ("explain" 1.4s → 14.7s) and with the classic budget starvation (a planning prompt spent all
1,200 tokens reasoning, no answer). The V3 dataset README confirms training rows were rendered thinking
off. Separately, the oMLX admin profile for V3 was switched to `enable_thinking: False` at 13:07 that
day; because Wisp only ever sent `False` or *nothing*, every "thinking" path had silently inherited that
toggle. Decision (user, same day): **default off, explicitly; thinking on only where it earns its
latency; measure the two contested sites.**

**Slice 2: explicit think-block policy.**
- `models.yaml` `explicit_thinking`: the three Wisp-merged Ling ids. `config.thinking_kwargs(model, on)`
  sends `enable_thinking` both ways for them; every other model's kwargs are byte-identical to before
  (pinned by `tests/test_thinking_kwargs.py`).
- Policy for explicit models: OFF on agent selection and narration steps, plain chat (all roles),
  background extraction, search, rolling summary; ON for the daily brief and Messages digest
  (`user_facing_summary_kwargs`) with `user_facing_summary_budget` raising 512/600 → 1,800, and for an
  agent step after a tool failure that is still outstanding (`step_think_on` in `run_agent`).
- The two contested sites stay behind the experiment switches (`narration_think_compound`,
  `search_think`) and are measured by `scripts/eval_arms.py` against baseline: compound prompts × 3
  reps plus the six search cases.
- Open item from the smoke run: Smart Search discarded 6/12 correct answers because V3 omitted `[n]`
  citations and `verify()` drops uncited sentences. Not a thinking problem; belongs with the prompt work.

**Measurement 1 (run `20261013-140051`): baseline / `narration_think_compound` / `search_think`,
12 "compound" prompts × 3 reps + 6 search cases × 3 reps, V3 on isolated backends.**
- `search_think`: **no effect — delete.** Search correct 33% / 33% / 33%, the same two cases pass in
  every arm and every rep; the thinking arm was slower (search p50 6.2s vs 3.6s). The failures are
  deterministic per question (uncited answer → `verify()` drops it), so reasoning cannot fix them.
- `narration_think_compound`: **inconclusive.** The "compound" eval tasks are mostly plan-mode or
  deterministic turns; only `agentic_compose` reached two answered tools (2 samples, ~75 chars of
  reasoning, same answer). Re-measured on live multi-source prompts in Measurement 2.
- Harness lesson: arm-level p50 drifted 2.3 → 4.3 → 7.1s on code paths identical across arms, so
  arm-major ordering confounds with time. Bracket runs with a second baseline; trust per-task pairs.
- Incidental: every web route 404s because `router.py:6428` pins `decision.model` to the uninstalled
  `Ling-3.0-tiny-oQ6e` (`_LING_WEB_MODEL`), and the 404 triggers an oMLX variant reload that 409s the
  next ~3s of requests. Affects the installed app. Spun out as its own task (`task_a8ee6fa1`).

**Measurement 2 (run `20261013-142918`): baseline / `narration_think_compound` / baseline, the brief +
personal prompt categories × 2 reps (60 live turns).** Zero turns answered two or more tools. With
Measurement 1 that is 2 of 177 live V3 turns for the condition the switch gates, so it was retired.
Error-retry thinking stays as policy (`step_think_on`).

**Citation probe (same day).** V3 with synth's real prompt on the six search cases: 6/6 answers
correct, 3/6 cited. `verify()` dropped the uncited three. Fix (slice 3): an uncited span-scope
sentence is grounded by content-token overlap against the retrieved passages — full match for
answers under three tokens, 60% otherwise — and cited to that passage; anything that cannot be
traced is still dropped. `search_think` retired with its numbers; `capture_think` kept, unmeasured.
Confirmed live (run `20261013-143849`, V3, 6 cases × 3 reps): **18/18 correct**, p50 5.2s, versus
33% in every run before the fix.
