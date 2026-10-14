# System prompt optimizations for Ling V3 — measured

**Written:** 2026-10-13. **Model:** `Ling-3.0-tiny-Wisp-V3-merged-oQ4e`. **Branch:** `claude/ling-v3-prompt`.
**Evidence:** `scripts/eval_prompt_composition.py` runs (`~/.moe/evals/arms/composition-20261013-*.raw.json`),
`loop._system_blocks()` / `build_system()` measured per route, oMLX `server.log` prefix-cache lines,
and the V3 dataset's `assets/system_parts.json`. Tokens are chars/4 unless marked oMLX-exact.

## 1. What the model actually receives

`SYSTEM` is 23,475 chars, but `build_system()` already prunes it per route. Measured rules-only size
per route (chars → ~tokens): inbox 4,804 → 1,201 · calendar 6,391 → 1,597 · messages 4,304 → 1,076 ·
notes 5,387 → 1,346 · web 4,099 → 1,024 · send 7,094 → 1,773 · memory 7,519 → 1,879 · files 4,363 →
1,090 · ambiguous (11 tools) 5,254 → 1,313. Always-on core: 8 blocks, 3,234 chars (~810 tokens).

A live step's system message adds: identity (222 chars; +1,600 of attribution rules only when a
messages tool is offered), installed-skills list (710 chars, every turn), date line (109), light-read
style hint (1,000 chars on light routes), memory block (0 in the sandbox; capped at 2,400 chars
live), and the tool schemas as a separate component (inbox: 1,966 tokens, more than the rules).

Two structural facts set the risk profile:

- **V3 was fine-tuned on this prompt.** 35 of today's 36 `SYSTEM` blocks appear verbatim in the
  training rows' system parts, which also carry the identity block and the attribution rules. They do
  **not** carry the installed-skills list or the light-read style hint. Rewording a trained block
  moves V3 off-distribution and must be measured; removing text V3 never trained on is cheap.
- **oMLX reuses little of the prefix.** Over the last 300 live requests: average prompt 9,648 tokens,
  average re-prefilled 6,679 (69%), average reused 2,970. Anything that varies early in the system
  message (memory block before the skills list, rules that change with the fitted tool set) costs a
  re-prefill of everything after it. Prefill is ~9% of wall time here, but 6.7k tokens per step is
  also the KV-memory story on a 24 GB machine.

## 1a. The finding that outranks the prompt: V3 runs on an 8,000-token guess

oMLX's `model_settings.json` has no `max_context_window` for
`Ling-3.0-tiny-Wisp-V3-merged-oQ4e` (the other rostered models carry 16,000–56,000), so
`config.model_context_window()` falls back to `_DEFAULT_CONTEXT_WINDOW = 8000`. Measured in the
worktree against the live settings file:

| Budget | Value for V3 today | Consequence |
|---|---|---|
| window | 8,000 | `_fit_window` budgets every step against 8k while oMLX itself accepts far more (0 "Prompt too long" rejections in 52k log lines; live prompts average 9,648 tokens) |
| history budget | **1,500** (the floor; `8,000 − 11,500` overhead) | the conversation the model sees is ~3 exchanges, the rest folded into the 400-token summary — this is the "history" limiter from the hackathon diagnosis, and it is a config gap, not a prompt problem |
| tools kept on the ambiguous route | 11 of 19 | the router's picks fall off the end of the request before the model sees them |

The model's native `max_position_embeddings` is 131,072. It is a `bailing_hybrid`: 24 layers with
`layer_group_size` 4, so 6 MLA full-attention layers (`kv_lora_rank` 512 + 64 rope dims) and 18
linear-attention layers with fixed state. Per-token KV is therefore ~7 KB, not the ~128 KB/token
of the earlier 4B dense roster: a 16,000-token window is ~110 MB of KV, 32,000 is ~220 MB.

**Recommendation:** set `max_context_window` for V3 to 16,000 in oMLX (the value the other
16k-class roster entries use) and restart oMLX — `set_model_context_window` documents that the
server caches the file at startup, so a model reload is not enough. Wisp then budgets history at
4,500 tokens and keeps the full ambiguous menu. Measure before/after with `eval_arms.py` on a
seeded multi-turn session (the sandbox's fresh sessions never exercise the history budget).
This is a user-side oMLX admin change; Wisp has the helper but nothing calls it.

## 2. Optimizations, ranked by (savings × certainty) ÷ risk

| # | Optimization | Saves (per step) | Risk | Evidence |
|---|---|---|---|---|
| A | **Build rules from the tools actually sent.** On the ambiguous route the router retrieves 19 tools, `_fit_window` sends 11, but `build_system()` ran on all 19: mail, notes and aggregate blocks rode along for tools the model never saw. | **~1,260 tokens** on the ambiguous route (10,313 → 5,254 chars); 0 elsewhere | none — these rules describe tools that are absent, and the trained prompt was per-route pruned the same way | live step for "What time is it?": 11 tools in request, 10.3k-char system message |
| B | **Gate the installed-skills list** on `use_skill`/`wisp_skills` being offered (or a skill having matched the turn), instead of every turn. | ~180 tokens on every route while any skill is installed | low — not in the training prompt | `base_lines` of `system_parts.json` has no skills list |
| C | **Route "what time is it?"-class prompts to the no-tool path.** The prompt's own rule says to answer from the date line, yet the router sent it to the agent with 19 tools and a full model step. | a whole model step (~3.8k tokens prompt + a generation) for those prompts | low — router rule; measure on the 439-case corpus | `R.route("What time is it?")` → `source=default`, subset 19 |
| D | **Compress the light-read style hint** (1,000 → ~350 chars) and make the base "Keep answers concise" conditional so the hint stops having to say "ignore the instruction above". | ~160 tokens on light routes | low–medium — not in training, but it shapes the voice; grade blind pairs | hint text at `main.py::_LIGHT_READ_STYLE` |
| E | **Move the memory block to the runtime (second) system message**, after the stable rules/identity/skills prefix. It is retrieved per query, so it changes every turn; today it sits before the skills list. | 0 tokens; raises prefix reuse (the stable prefix becomes byte-identical across turns of a route) | low — order only; the loop's own comment says precedence rests on wording, not position | oMLX reuse 2,970 of 9,648 |
| F | **Gate the capability-questions core rule** (797 chars) to routes that offer the mail/messages/calendar/notes tools it talks about — it is "core" only because its first line names no tool. A deterministic `wisp_capabilities` tool now answers those questions. | ~200 tokens on every non-personal-data route | medium — trained block; measure tool-choice on the corpus and the capability-question replay | `_system_blocks()` tags it CORE |
| G | **Trim the three largest tool descriptions on the inbox/messages routes** (`view_emails` 686, `summarize_messages` 641, `summarize_emails` 635 tokens; median tool is 135). Descriptions carry failure prose that belongs in code comments. | ~600–900 tokens on inbox/messages routes (schemas, not rules) | medium — descriptions drive routing retrieval and tool choice; run the routing-quality ratchet and the inbox eval tasks | schema sizes measured over the 182-tool registry (31.5k tokens total) |
| H | **Rewrite the two heavy trained blocks** (send rules 3,852 chars; memory rules 3,377) to ≤40%. | ~1,000 tokens on send/memory routes | **high** for V3 — trained verbatim; do it with the next fine-tune (regenerate `system_parts.json`) rather than against V3 | 35/36 blocks verbatim in training |
| I | **Trim the "evidence" (1,064) and "inverse" (482) core rules** to half. | ~190 tokens every route | high for V3, same reason as H | — |

Cumulative, low-risk set (A+B+C+D+E): ambiguous route −1,440 tokens; every other route −180 to −340
tokens plus better prefix reuse; "what time is it" prompts lose a model step entirely. The
medium/high set (F–I) is worth ~400–1,200 more per route but belongs with a V4 training run.

**Implemented on this branch (A and B):** `run_agent` now fits the request once up front with the
full rules and, when tools fall out, rebuilds the rules from the survivors (once per turn, so the
prompt-cache prefix stays stable across steps); `skills_context_block(..., catalog=False)` drops
the installed-skills catalog when `use_skill` is not offered, keeping any matched or active skill
body. Tests: `tests/test_prompt_scope_fit.py` (3), registered in the Simulation QA manifest; the
seven existing modules that touch the prompt builder or the skills block pass (911 tests).
Note that A's measured saving shrinks once the window is fixed (§1a) — with 16k, fewer tools are
dropped and the rules were right all along; it stays correct in both states.

## 3. Validation plan

- A, B, E: byte-level tests on `build_system`/assembly plus one `eval_arms.py` run (baseline vs
  switched, bracketed) on `cat:brief,personal,agentic` to confirm tool choice is unchanged.
- C: the 439-case routing corpus ratchet (`tests/test_routing_quality_corpus.py`) plus the standing
  router tests.
- D: blind pairs on the light-read tasks graded on the eval board.
- F, G: routing corpus ratchet + inbox/messages eval tasks; keep if tool choice holds.
- H, I: only with a regenerated dataset; measure old vs new fine-tune on the same harness.

## 4. Not a prompt problem, but found on the way

- The largest per-step cost in the composition probe was the refused-repeat loop (tool results 40% of
  all tokens), fixed in #204, not any prompt text.
- Real sessions prefill ~10k tokens where the sandbox showed ~3.5k: the difference is history, memory
  and larger tool results. The history budget (`window − 11,500`) and the 400-token rolling summary
  are the next measurement after the prompt work.
