# r5 on the 8x MI355X node

The node bills $24 an hour from the moment it starts, idle or not, so have the kit ready first and stop it as soon as the last download is done. The user starts and stops the node. Only `r5-kit.tgz` and the three zips go up. No private exports, no tokens (the base model isn't gated).

## On the Mac, before the node starts

```bash
PY=~/Desktop/MOE_Project/.venv/bin/python
# no WISP_REPO here: this reads this checkout's toolbench, not the pin's
env -u WISP_REPO $PY training/ask-lora-r5/mi355x/seal_r5.py --build BUILD_DIR --private-dir ~/.moe/evals/private-real-use
$PY training/ask-lora-r5/mi355x/verify_contract.py --build BUILD_DIR --out BUILD_DIR/contract_verification.json
bash training/ask-lora-r5/mi355x/pack_kit.sh BUILD_DIR REUSE_FILE training/ask-lora-r5/data/kit/r5-kit.tgz
```

`seal_r5.py` removes every turn close to a test question, before any node time goes to it, and lists them in `overlap_manifest.json`.

## On the node: base's answers

```bash
mkdir -p ~/wisp/uploads && tar -xzf r5-kit.tgz && cd r5
bash run_r5.sh setup && bash run_r5.sh check
SELFCHECK=1 bash run_r5.sh finals partial.jsonl          # batched and single greedy agree, or stay at BATCH=1
BATCH=8 LIMIT=8 bash run_r5.sh finals partial.jsonl      # speed: multiply tok/s by the finals left (`DATASET-HANDOFF.md`) before going on
BATCH=8 K=1 TOPUP=0 FAMILIES=all_sources_r5,memory_contrast_r5,write_contrast_r5 LIMIT=40 bash run_r5.sh finals partial.jsonl
# ^ the contract on base's real wording: read smoke/ with choose_r5.py before going on. If over half of these are rejected, look at why first.
BATCH=8 bash run_r5.sh finals partial.jsonl              # resumes; writes ~/wisp/finals/{chosen,drop.ids,stats.json,rejections.jsonl}
TRACES=1 BATCH=8 bash run_r5.sh finals partial.jsonl     # recommended, needs the user's OK for the cost: base's own thought at tool steps, about 1-2 h more (unmeasured)
```

Read `stats.json`. Download `chosen.jsonl`, `drop.ids`, `stats.json`, `rejections.jsonl`, `generation.*.json` and, with `TRACES=1`, `traces.chosen.jsonl`.

## On the Mac: the final build

```bash
PY=~/Desktop/MOE_Project/.venv/bin/python
$PY training/ask-lora-r5/mi355x/finish_r5.py --build BUILD_DIR --chosen chosen.jsonl --drop drop.ids --rejections rejections.jsonl \
    --choose-stats stats.json [--traces traces.chosen.jsonl] --out FINAL
(cd training/ask-lora-r2/builder && ASK_LORA_ROUND=4 ASK_LORA_R5=1 ASK_LORA_OUT=FINAL WISP_REPO=PIN_DIR $PY validate.py)
$PY training/ask-lora-r5/mi355x/report_r5.py --build FINAL --model MODEL_DIR --out FINAL/report --r4-zip training/ask-lora-r4/a6000/out/ask-lora-data.zip
$PY training/ask-lora-r4/a6000/pack_r4.py --model ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp --data FINAL --out-dir OUT --no-estimate
```

`FINAL` and `OUT` are absolute paths. Validation must exit 0. `finish_r5.py` stops if any final has no base sample, and `pack_r4.py` refuses anything whose `stats.json` isn't `complete` or doesn't match its rows. Upload the three zips to `~/wisp/uploads`.

**Training needs its own explicit OK.** The 2026-11-07 audit says NO-GO until this dataset, its review and the gate fixes in `DATASET-HANDOFF.md` are done.

## On the node: training

```bash
bash run_r5.sh unpack SHA256      # from OUT/upload.json
bash run_r5.sh preflight && bash run_r5.sh smoke
bash run_r5.sh train              # `status` shows the step; check the snapshot at step 400 before paying for the rest
bash run_r5.sh unzip-pickers && bash run_r5.sh pick    # batches 8 at a time and checks that first; PICK_BATCH=1 turns it off
bash run_r5.sh merge && bash run_r5.sh package && bash run_r5.sh bundle
```

Download the bundle and merged model and check their checksums before the user stops the node. Quantizing and the gate run on the Mac.
