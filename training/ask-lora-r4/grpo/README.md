# Ask LoRA r4, stage 2: GRPO on the merged r4 model

Prep only. Nothing here has run on a GPU, and the r4 data and vision folders did not exist when this was written. The code lives in `training/ask-lora-r3/grpo/` behind a round switch; this folder is where the r4 export and run outputs go (`grpo_prompts.jsonl`, `grpo_mm_dev.jsonl`, `mm/`, run logs).

## Round switch

- `reward.score(completion, label, rnd=None)`; `rnd=None` uses `reward.ROUND` (`REWARD_ROUND` in the environment, default 3).
- `grpo.py ... --round 4` sets it. `export_prompts.py --round 4` stamps `label.round = 4` on every row, and `grpo.py` refuses a flag that disagrees with the labels or a file that mixes rounds.
- Round 3 is byte-identical: the r3 export of `ask-lora-r3/data` is unchanged, and all 100 r3 tests pass untouched.

## Reward changes (text steps only; vision, audio and picture reads are unchanged)

| term | round 4 |
|---|---|
| answer facts | up to +1.0 when `label.narrate` (a final after a tool result); other finals and rows with no labelled facts stay at 0.5 |
| ungrounded | -0.5 per time, number, quoted title or name in the answer found in no tool result and not in the person's words (`label.ground`), capped at -1. Narrate rows only. Bare integers under 32 are skipped (counts, days of the month) |
| thinking | -0.005 per thought word above 120 (cap -0.5); thinking itself earns nothing |
| writes before reads | -2 for send_message, send_email, schedule_send, reply_to_email, move_path, delete_path, trash_path, cancel_event, clear_reminders, clear_past_reminders while a read in `label.requires_read` is not in `label.reads_done`. Not stacked on a forbidden -2 |
| leaks | -1 once, in a final with no call: a tool name (`label.tool_names`, or any snake_case word the prompt does not contain), JSON, `UNTRUSTED`, receipt phrasing when `label.chat`, a source-stall line |

Audit repairs (pre-GPU audit, all round 4 only; vision-0, unsafe-2, reward-0/2/3/4):

| term | round 4 |
|---|---|
| missing vision proposal | -1.0 when `label.proposal` is set and no proposal block comes (round 3: 0). The mirror of the -1.0 for a spurious proposal, so a policy unsure whether one is due has no reason to omit |
| early write | also `forward_email`, and `organize_files` once the call carries `confirm=true` (without it the tool is a preview) |
| forbidden tools | `export_prompts.py --round 4` forbids every effectful tool of the live registry the ideal turn does not call: `WRITE_TOOLS_R4` (adds forward_email, organize_files, toggle_setting, write_document, save/edit/delete_knowledge, create_tool, window_control, mac_set_volume, mac_mute, mac_lock_screen, mac_media and more) unioned at export time with `live_effect_tools()` (a child process reads `service.router.model_led.needs_effect_owner` over the registry with a throwaway WISP_HOME); a test fails when the static list falls behind |
| thinking floor | up to -0.5 on a post-read step (`label.think` and `label.post_read`) whose thought has fewer than min(6, ideal thought) words; an empty thought loses the full 0.5. Nothing pays for a shorter thought or answer |
| instructions | `label.constraints` (sentence and word caps, no "?", no bullets, bullets only, numbered, no emoji, no digits, yes-or-no first, Spanish): +0.5 when all are met, -0.5 for each broken (cap -1.0), on a final with no call. Derived from the person's wishes in the history (standing wishes carry forward, the latest of a kind wins, "back to normal" / "make it casual" lift them) or taken from `meta.constraints` when the builder writes it; a constraint the ideal answer itself breaks is dropped |
| session recall | `session_recall_r4` finals carry `label.recall`, `label.facts` (the ideal's facts, else up to three words of it that the earlier turns contain) and `label.ground`: facts up to +1.0, ungrounded details -0.5 each, -0.5 for denying the memory without stating the fact |
| chat style | `label.style` = the distilled base reply's word count and emoji presence (chat_voice, chat_direct_r4, session_chat_r4): up to -0.4 for a reply outside 0.5x-2x of its length, -0.2 when emoji presence differs. Penalties only |

The chat families (`chat_voice_r3`, `chat_voice_r4`, `chat_direct_r4`, `session_chat_r4`) are in `R4_CHAT_FAMILIES`, so a casual reply that ends in "?" is a final, not an `ask` row that pays +0.5 for the "?". In `instructions_r4` and `session_recall_r4` a final is an ask only when it is one short question (20 words or fewer).

Reference model for chat (reward-3): chosen against a KL-to-base option. The SFT chat finals are the base's own replies, so the existing adapter-disabled KL already anchors chat near the base; the style term supplies the missing gradient (chat rewards tie otherwise). `--mm-ref base:DIR` still covers vision and audio only and is worth passing in the run commands below.

New component groups in the logs: `grounding`, `order`, `leak`, `session` (`instruction`, `recall`); `think_floor` and `chat_style` are in `style`.

## New label fields (export_prompts.py --round 4)

- `requires_read`: the non-write, non-discovery calls the scripted turn makes before its first write (from the builder meta: `turn_writes` and the other steps of the same turn id).
- `reads_done`: those already in this step's history.
- `narrate`, `chat`, `post_read`, `ground`, `tool_names`, `round`, and from the audit repairs `constraints`, `recall`, `style`, `thought_words` (the ideal thought's length).
- `ground` is the person's words, the tool results, the call arguments and earlier answers, plus the short system lines (the clock). The long rules prompt is left out. Only narrate rows carry it.

## Prompt mix (`--round 4`)

| share | bucket |
|---|---|
| 30% | narrate-after-read steps (rows with labelled facts weighted 1.5x), not counting the session families |
| 15% | `instructions_r4` (2.0), `session_recall_r4` (2.0), `session_chat_r4` (1.5): every step of the three families |
| 20% | decision steps of safe_restraint, gather_then_send_r4, restraint_r4, compose_r3 (weights 3, 3, 3, 2.5), plus safe_effects, ignore_embedded_instructions, no_write_for_reads (2) and reminders_r3 |
| 20% | other text (picture-turn steps count here) |
| 15% | vision and audio, picture reads included; injection 2.5, proposal 2.5, abstain 1.5 |

No row appears more than 3 times. A thin bucket is topped up from "other" and reported in `grpo_prompts.stats.json` under `r4_mix`.

## Export (Mac)

```
cd training/ask-lora-r3/grpo
../../../.venv/bin/python export_prompts.py --round 4 \
    --model ~/.moe/engine/models/Jundot/gemma-4-E4B-it-oQ4e-mtp \
    --build-dir ../../ask-lora-r4/data \
    --vision-rows ../../ask-lora-r4/vision/vision_rows.jsonl \
    --audio-rows <the audio agent's audio_rows.jsonl> \
    --n 1500
```

Defaults for round 4: build dir `ask-lora-r4/data` (or `ASK_LORA_OUT`), vision rows `ask-lora-r4/vision/vision_rows.jsonl`, output `ask-lora-r4/grpo/grpo_prompts.jsonl`, media in `ask-lora-r4/grpo/mm/`. Rows are cut to a multiple of 16: `--n 1500` gives 1,488 rows, 372 A6000 steps. The valid split stays out.

## A6000 run (G=6, beta 0.04, lr 5e-6, 1 epoch)

```
accelerate launch --config_file accelerate_a6000.yaml grpo.py check --base <merged r4> --prompts ../../ask-lora-r4/grpo/grpo_prompts.jsonl --profile a6000 --round 4
accelerate launch --config_file accelerate_a6000.yaml grpo.py train --profile a6000 --round 4 --base <merged r4> \
    --prompts ../../ask-lora-r4/grpo/grpo_prompts.jsonl --mm-root ../../ask-lora-r4/grpo/mm --mm-floor <mm-untuned.json> --mm-ref base:<untuned Jundot gemma dir> --out <run dir>
```

`--mm-ref base:<untuned>` anchors the vision and audio KL to the untuned model instead of the r4 SFT (+16 GB of weights per GPU; drop it only if the A6000 smoke run runs out of memory, and say so in the run notes).

Profile defaults: 4 prompts x G=6 = 24 completions per step, micro_bs 3, beta 0.04 (0.2 on vision and audio), lr 5e-6. The completion cap is 512 tokens in round 4 (a thought of up to 120 words plus the answer); 384 truncated thoughts would score as malformed.

`python plan.py --round 4` prints the estimate: **one A6000, 1,488 prompts, 1 epoch: 13.6 / 17.7 / 24.6 h of training (optimistic / middle / pessimistic) plus ~1.8 h of guard evals and 0.75 h setup, so about 16 / 20 / 27 h.** On 8x B300 the same epoch is 0.8 / 1.2 / 1.8 h of training (1.8-2.8 h with setup). All of it is an estimate: the completion length (260 tokens) and the A6000 decode speed are assumptions to replace from the smoke run.

## Checked on r3 data (Mac)

- `export_prompts.py --round 4` on `ask-lora-r3/data` with the real tokenizer: 1,488 rows, narrate 535, compose-bucket 300, 223 vision/audio/picture-read rows. `requires_read` is set on 337 text rows, 107 of them with a read still pending.
- Ideal completions under round 4: 4 of 1,265 text rows trip `ungrounded` (three computed values and a "Professor" for "Prof."), 0 trip `leak` or `early_write`. `grpo.py check` fails if more than a handful do.
- Not verified: anything that needs torch or a GPU, and r4 data (families gather_then_send_r4 and restraint_r4 do not exist in r3 data, so their weights were only unit-tested).

## Known limits of the new terms

- Ungrounded is a surface check: a computed value (a conversion, a sum) or a title ("Professor" for "Prof.") that no tool result contains costs up to -1. It is rare in the ideals (<0.5% of rows) and the group-relative advantage cancels it when every sample makes the same slip.
- Leaks match tool names by `label.tool_names` plus any snake_case word the prompt lacks; a single-word tool name is only caught as `name(`.
- `requires_read` counts the history only: a read and a write in the same completion still scores as an early write.
