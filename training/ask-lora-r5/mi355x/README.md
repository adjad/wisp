# r5 on the 8x MI355X node

The node bills $24 an hour from the moment it starts, idle or not, so have the kit ready first and stop it as soon as the last download is done. The user starts and stops the node. Only `r5-kit.tgz` and the three zips go up. No private exports, no tokens (the base model isn't gated).

## On the Mac, before the node starts

```bash
bash training/ask-lora-r5/mi355x/pack_kit.sh BUILD_DIR BUILD_DIR/finals/finals.r4reuse.jsonl training/ask-lora-r5/data/kit/r5-kit.tgz
```

## On the node: base's answers

```bash
mkdir -p ~/wisp/uploads && tar -xzf r5-kit.tgz && cd r5
bash run_r5.sh setup && bash run_r5.sh check
SELFCHECK=1 bash run_r5.sh finals partial.jsonl          # batched and single greedy agree, or stay at BATCH=1
BATCH=8 LIMIT=8 bash run_r5.sh finals partial.jsonl      # speed: multiply tok/s by the 3,225 finals left before going on
BATCH=8 bash run_r5.sh finals partial.jsonl              # resumes; writes ~/wisp/finals/{chosen.jsonl,drop.ids,stats.json}
```

Read `stats.json`. Download `chosen.jsonl` and `drop.ids`.

## On the Mac: the final build

```bash
PY=~/Desktop/MOE_Project/.venv/bin/python
$PY training/ask-lora-r5/mi355x/finish_r5.py --build BUILD_DIR --chosen chosen.jsonl --drop drop.ids --out FINAL
(cd training/ask-lora-r2/builder && ASK_LORA_ROUND=4 ASK_LORA_R5=1 ASK_LORA_OUT=FINAL $PY validate.py)
$PY training/ask-lora-r4/a6000/pack_r4.py --model ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp --data FINAL --out-dir OUT --no-estimate
```

`FINAL` and `OUT` are absolute paths. Validation must exit 0. Upload the three zips to `~/wisp/uploads`.

## On the node: training

```bash
bash run_r5.sh unpack SHA256      # from OUT/upload.json
bash run_r5.sh preflight && bash run_r5.sh smoke
bash run_r5.sh train              # `status` shows the step; check the snapshot at step 400 before paying for the rest
bash run_r5.sh unzip-pickers && bash run_r5.sh pick    # batches 8 at a time and checks that first; PICK_BATCH=1 turns it off
bash run_r5.sh merge && bash run_r5.sh package && bash run_r5.sh bundle
```

Download the bundle and merged model and check their checksums before the user stops the node. Quantizing and the gate run on the Mac.
