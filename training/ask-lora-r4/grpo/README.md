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

New component groups in the logs: `grounding`, `order`, `leak`.

## New label fields (export_prompts.py --round 4)

- `requires_read`: the non-write, non-discovery calls the scripted turn makes before its first write (from the builder meta: `turn_writes` and the other steps of the same turn id).
- `reads_done`: those already in this step's history.
- `narrate`, `chat`, `ground`, `tool_names`, `round`.
- `ground` is the person's words, the tool results, the call arguments and earlier answers, plus the short system lines (the clock). The long rules prompt is left out. Only narrate rows carry it.

## Prompt mix (`--round 4`)

| share | bucket |
|---|---|
| 35% | narrate-after-read steps (rows with labelled facts weighted 1.5x) |
| 20% | decision steps of safe_restraint, gather_then_send_r4, restraint_r4, compose_r3 (weights 3, 3, 3, 2.5), plus safe_effects, ignore_embedded_instructions, no_write_for_reads (2) and reminders_r3 |
| 30% | other text (picture-turn steps count here) |
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
    --prompts ../../ask-lora-r4/grpo/grpo_prompts.jsonl --mm-root ../../ask-lora-r4/grpo/mm --mm-floor <mm-untuned.json> --out <run dir>
```

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
