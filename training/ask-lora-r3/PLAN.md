# Ask LoRA round 3: SFT, then GRPO (plan, 2026-11-01)

**Target:** Jundot gemma-4-E4B-it-oQ4e-mtp, Wisp's default, served with thinking on and temperature 1.0 / top_p 0.95 / top_k 64. Nothing runs on a GPU, on Brev or locally without a separate go-ahead from the user.

## Why round 3

r2's gate failed on 2026-11-01. The toolbench run is `20261101-123417`, 1 trial, comparing the untuned model to r2. The full table is in the shared note `ask-lora-r2-gate-results.md`.

| Kept from r2 (the base for r3) | Broken by r2 (what r3 must fix) |
|---|---|
| Writes 49.6 → 64.7% (p = 0.01) | Unsafe calls 5 → 16; safety 20 → 18 of 23 |
| Think p50 1479 → 69 chars; turn p50 7.8 → 3.3 s | Vision facts 25 → 20 of 25; proposals 3 → 0 of 6; 2 empty replies |
| Blind chat won 28–12; emoji 54% → 0% | 6 chat non-replies (receipt phrasing leaked into chat, incoherent fragments) |
| mail +5, web +4, notes +3, chat +5 | contacts −1, device −1, calendar −1, files −1 |

Two of r2's 16 unsafe calls ran with no card: `move_path` on a "show" request, and `open_app` because a file's text said to. The rest were wrong-channel sends and over-broad reminder clears that a card would have stopped.

## Stage 1: r3 SFT (data)

The r2 builder now runs against current main:
- **Runnable stub calls:** since #424, `harness.RUNNABLE_STUBS` lets a stub call with valid arguments run directly.
- **Separate output:** `ASK_LORA_OUT` points the build at its own directory.
- **Round-3 families:** `ASK_LORA_ROUND=3` loads the `scen_r3_*.py` modules.
- **Probe:** a probe build gave 1,506 examples with 5 failures to fix.

r3 = all r2 families, rebuilt on main, plus these:

| Family | Module | Fixes |
|---|---|---|
| `safe_restraint` | scen_r3_safe.py | the r2 and Bolt unsafe patterns, plus about 25% positive controls |
| `chat_voice_r3` | scen_r3_chat.py | about 320 coherent casual replies, with no tool or receipt language |
| `compose_r3` | scen_r3_compose.py | the weakest category: gather, then send in one turn, on the right channel |
| `followup_r3` | scen_r3_followup.py | multi-turn follow-ups, r2's "single turn only" gap |
| `mail_r3`, `web_r3` | scen_r3_mailweb.py | grounded mail and web answers; web_search over web_fetch |
| `device_r3`, `contacts_r3`, `reminders_r3` | scen_r3_device.py | r2's regressions plus weak categories |
| `vision_r3` | vision/make_vision_data.py | about 220 images and about 30 video items through vision.py's own request; visible text always; proposals only for future events; planted text ignored |

**Training changes** (brev/render.py and train.py):
- image rows go through the processor, with towers frozen and about 12% of steps;
- chat rows are capped so r2's terseness doesn't recur.

**Open question:** whether to keep 2 epochs at lr 2e-4, or 1.5 epochs. Decide from the dev loss of each family in `smoke`.

## Stage 2: GRPO (grpo/)

- **Single-step bandit GRPO** on the r3 merged model, over decision steps exported from the build, with a fresh LoRA.
- **The reward is deterministic** (reward.py):
  - right tools and arguments;
  - −2 for any forbidden tool;
  - a penalty for calling when an answer or question was ideal;
  - the answer's facts must be present;
  - penalties for third-person naming, closers and a thought over 60 words.
- **Settings:** 6 generations at production sampling, beta 0.04, lr 5e-6, 1 epoch over about 1,500 prompts weighted towards safety.
- **No rollout environment.** Gemma 4 has no vLLM, and multi-turn rollouts would be too slow.

## Gate

Same as r2, plus:
- 3 trials;
- unsafe calls at or below the untuned model's;
- vision facts and proposals at or above untuned on the 60-image set;
- 0 chat non-replies;
- MTP acceptance measured.

Measure after SFT, and again after GRPO.
