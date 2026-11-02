#!/bin/bash
# r4 SFT on ONE NVIDIA RTX A6000 (48 GB, sm_86) on Brev: the whole flow from a fresh instance to a picked checkpoint.
# A thin layer over ~/wisp/run.sh (written by ../../ask-lora-r3/brev/vm-setup.sh). README.md next to this file explains every step.
#
#   bash run_sft.sh setup                  pinned stack (torch 2.11.0+cu128, transformers 5.22.1, peft 0.21.2) + the 16 GB base
#   bash run_sft.sh check                  GPU, versions against the pins, zip sha
#   bash run_sft.sh unpack <sha256>        verify + unpack ask-lora-data.zip (sha from pack_r4.py's output / out/upload.json)
#   bash run_sft.sh preflight              template sha, render parity, lengths, stop tokens, vision + audio processor checks
#   bash run_sft.sh smoke                  attention parity (sdpa vs math, STRICT), cache parity, the 12 longest rows' peak memory
#   bash run_sft.sh train [resume-dir]     background run: 2 epochs, batch 8 (1 x 8 accumulation), LoRA r16, eval + snapshot every 200 steps
#   bash run_sft.sh status                 progress, ETA, peak memory, evals
#   bash run_sft.sh unzip-pickers          unzip mmeval-data.zip and pickers.zip into ~/wisp/mmeval
#   bash run_sft.sh pick                   mmeval picker + text picker + the combined decision (hours; run it detached)
#   bash run_sft.sh picked                 print the chosen checkpoint and the tables
#   bash run_sft.sh merge                  fold the PICKED adapter into the bf16 base
#   bash run_sft.sh package                run-info.json, merged checksums (+ the adapter zip run.sh makes)
#   bash run_sft.sh bundle                 one tarball of everything small to download (the merged folder is separate)
#
# Knobs (environment): ATTN=sdpa|math|eager (default sdpa), PIN=0 (unpinned, torch>=2.7), EVAL_EVERY, DEV_TEXT_MAX,
# DEV_STEPS (text picker steps, default 360), MAX_CKPTS (candidates, default 6), CKPTS (comma list overriding the candidates),
# WITH_BASE=1 (also generate with the adapter off, for reference), WISP (default ~/wisp).
set -uo pipefail

WISP="${WISP:-$HOME/wisp}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$WISP/run.sh"
PY="$WISP/venv/bin/python"
TAG=gemma
TD="$WISP/$TAG"
DATA="$TD/ask-lora"
OUT="$TD/ask-lora-out"
MM="$WISP/mmeval"
PK="$TD/pick"
ATTN="${ATTN:-sdpa}"
EVAL_EVERY="${EVAL_EVERY:-200}"          # one eval = ~11 min on the A6000 with --dev-text-max 150; see README
DEV_TEXT_MAX="${DEV_TEXT_MAX:-150}"
DEV_STEPS="${DEV_STEPS:-360}"
SEED=20261029                            # train.py's default; eval_mm.py's serving-shape hash must use the same one

# The pins. r2 trained on an A6000 with exactly this stack (ask-lora-r3/PLAN.md: "r2 stack ... is the starting pin").
PIN_TORCH="2.11.0"; PIN_TV="0.26.0"; PIN_TRANSFORMERS="5.22.1"; PIN_PEFT="0.21.2"
TORCH_IDX="https://download.pytorch.org/whl/cu128"     # driver 570 (CUDA 12.8) is what Brev's A6000 hosts ship

die() { echo "FAIL: $*" >&2; exit 1; }
say() { echo "== $(date '+%F %T %Z') $*"; }
need_setup() { [ -x "$RUN" ] && [ -f "$WISP/SETUP_DONE" ] || die "run '$0 setup' first (needs $RUN and $WISP/SETUP_DONE)"; }

# One GPU, batch 8 = 1 row x 8 accumulation steps (train.py's micro batch is always 1), the r3 recipe otherwise.
# --attn-parity-strict makes a wrong fused attention kernel fatal in smoke on CUDA too (default there: a warning).
export GLOBAL_BATCH=8 LR=2e-4 NPROC=1
export EXTRA_TRAIN_ARGS="--epochs 2 --attn $ATTN --eval-every $EVAL_EVERY --save-every $EVAL_EVERY --dev-text-max $DEV_TEXT_MAX \
--vision-weight 0.12 --audio-weight 0.06 --anchor-kl 0.5 --seed $SEED --attn-parity-strict"

case "${1:-help}" in
setup)
  VM="$HERE/vm-setup.sh"; [ -f "$VM" ] || VM="$HOME/vm-setup.sh"; [ -f "$VM" ] || VM="$WISP/uploads/vm-setup.sh"
  [ -f "$VM" ] || die "vm-setup.sh not found next to run_sft.sh, in ~ or in ~/wisp/uploads"
  if [ "${PIN:-1}" = 1 ]; then
    export TORCH_INDEX="$TORCH_IDX" TORCH_SPEC="torch==$PIN_TORCH" TORCHVISION_SPEC="torchvision==$PIN_TV" \
           TRANSFORMERS_SPEC="transformers==$PIN_TRANSFORMERS" PEFT_SPEC="peft==$PIN_PEFT"
    say "pinned stack: torch $PIN_TORCH (cu128), torchvision $PIN_TV, transformers $PIN_TRANSFORMERS, peft $PIN_PEFT"
  else
    say "PIN=0: unpinned (torch>=2.7 from the cu128 index, transformers>=5.10.1,<6, peft>=0.19)"
  fi
  bash "$VM" ;;
check)
  need_setup
  "$RUN" $TAG check
  "$PY" - "$PIN_TORCH" "$PIN_TV" "$PIN_TRANSFORMERS" "$PIN_PEFT" <<'PYC'
import sys, torch, torchvision, transformers, peft
want = dict(zip(("torch", "torchvision", "transformers", "peft"), sys.argv[1:5]))
have = {"torch": torch.__version__.split("+")[0], "torchvision": torchvision.__version__.split("+")[0],
        "transformers": transformers.__version__, "peft": peft.__version__}
for k in want:
    print(f"  {k:13s} {have[k]:10s} pinned {want[k]:8s} {'ok' if have[k] == want[k] else 'DIFFERENT (fine only if PIN=0 was used on purpose)'}")
p = torch.cuda.get_device_properties(0)
print(f"  gpu {p.name}, {p.total_memory / 2**30:.1f} GiB, sm_{p.major}{p.minor}; torch cuda {torch.version.cuda}")
if (p.major, p.minor) != (8, 6):
    print("  NOTE: this is not an sm_86 card; the memory numbers in README.md were estimated for an A6000")
if p.total_memory < 44 * 2**30:
    print("  WARNING: under 44 GiB; this recipe was sized for a 48 GB card (QLORA=1 is the 24 GB fallback, untested for r4)")
PYC
  for z in ask-lora-data.zip mmeval-data.zip pickers.zip; do
    [ -f "$WISP/uploads/$z" ] && echo "  $z  $(sha256sum "$WISP/uploads/$z" | cut -c1-64)" || echo "  $z  MISSING in $WISP/uploads"
  done ;;
unpack)
  need_setup; [ -n "${2:-}" ] || die "usage: $0 unpack <sha256 of ask-lora-data.zip>"
  "$RUN" $TAG unpack "$2" ;;
preflight) need_setup; "$RUN" $TAG preflight ;;
smoke)
  need_setup
  say "smoke: attention parity (sdpa vs math, strict), cache parity, anchor, 12 longest rows, 15-step overfit"
  "$RUN" $TAG smoke 10240; rc=$?
  case $rc in
    0) echo "smoke OK: read 'peak allocated' below 40 GiB and 'one epoch ~' in $DATA/smoke.log" ;;
    8) echo "smoke exit 8: --attn $ATTN disagrees with the math reference. Use ATTN=math (slower, ~31 GiB at 10k tokens; r3's MI355X fallback) and rerun." ;;
    3|4) echo "smoke exit $rc (3 = tight, 4 = out of memory): lower the cap with  $0 smoke-len 8192  (see README, memory)" ;;
  esac
  grep -E "SMOKE:|peak|attention parity|MEMORY|overfit" "$DATA/smoke.log" 2>/dev/null | tail -8
  exit $rc ;;
smoke-len)
  need_setup; "$RUN" $TAG smoke "${2:-8192}" ;;
train)
  need_setup
  [ -f "$OUT/smoke.json" ] || die "run '$0 smoke' first (it is the memory and attention gate)"
  "$PY" - "$OUT/smoke.json" <<'PYS' || die "smoke.json does not show a passing smoke"
import json, sys
s = json.load(open(sys.argv[1]))
print(f"smoke: ok={s.get('ok')} peak {s.get('peak_gib')} GiB reserved {s.get('reserved_gib')} of {s.get('total_gib')} GiB, "
      f"{s.get('tok_s')} tok/s, ~{s.get('epoch_min')} min per epoch (+evals {s.get('eval_min_per_epoch')})")
sys.exit(0 if s.get("ok") else 1)
PYS
  # train.py's --max-len is the smoke's cap: pass the same one
  ML="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['max_len'])" "$OUT/smoke.json")"
  "$RUN" $TAG train "${2:-}" "$ML" ;;
status)
  need_setup
  "$RUN" $TAG status
  [ -f "$OUT/train.log.jsonl" ] && "$PY" - "$OUT/train.log.jsonl" <<'PYT'
import json, sys
last = ev = None
for line in open(sys.argv[1]):
    if line.startswith("{"):
        r = json.loads(line)
        if r.get("event") == "eval":
            ev = r
        elif "step" in r and "of" in r and "event" not in r:
            last = r
if last:
    print(f"step {last['step']}/{last['of']} (epoch {last['epoch']}) loss {last['loss']} {last['tok_s']} tok/s "
          f"{last['s_per_step']} s/step peak {last['peak_gib']} GiB, ETA ~{last['eta_min'] / 60:.1f} h (evals not counted)")
    if last["peak_gib"] > 40:
        print("WARNING: peak above 40 GiB; the next longer row could OOM (train.py saves oom-recovery and exits)")
if ev:
    print("last eval:", {k: ev[k] for k in ev if k.startswith(("dev_loss", "dev_kl", "step")) or k == "mm_ok"})
PYT
  ;;
unzip-pickers)
  for z in mmeval-data.zip pickers.zip; do [ -f "$WISP/uploads/$z" ] || die "$WISP/uploads/$z missing"; done
  mkdir -p "$MM" && unzip -q -o -d "$MM" "$WISP/uploads/mmeval-data.zip" && unzip -q -o -d "$MM" "$WISP/uploads/pickers.zip" \
    && ls "$MM" "$MM/data" | head -30 ;;
pick)
  need_setup
  [ -f "$MM/pick_text.py" ] && [ -f "$MM/eval_mm.py" ] && [ -f "$MM/data/text-dev.jsonl" ] || die "run '$0 unzip-pickers' first"
  [ -d "$OUT/ckpt" ] || [ -d "$OUT/final" ] || die "no checkpoints under $OUT"
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then die "training is still running; the pickers need the GPU"; fi
  BASE="$(cat "$TD/base_path")"; mkdir -p "$PK"
  CK="${CKPTS:-$("$PY" "$MM/pick_text.py" candidates --ckpt-root "$OUT" --max "${MAX_CKPTS:-6}")}" || die "could not list candidates"
  [ -n "$CK" ] || die "no candidate checkpoints"
  say "candidates: $CK"
  GATE=(); [ -f "$MM/data/gate_vision/cases.json" ] && GATE+=(--vision-cases "$MM/data/gate_vision/cases.json")
  [ -f "$MM/data/gate_audio/truth.json" ] && GATE+=(--audio-truth "$MM/data/gate_audio/truth.json")
  say "1/3 mmeval (base + each candidate on vision, reading, documents, audio dev rows)"
  "$PY" "$MM/eval_mm.py" --base "$BASE" --ckpt-root "$OUT" --ckpts "$CK" --data "$MM/data" "${GATE[@]}" --seed $SEED \
      --out "$PK/mmeval" > "$PK/mmeval.log" 2>&1 || die "eval_mm failed (tail $PK/mmeval.log)"
  say "2/3 text generations: $DEV_STEPS valid steps x each candidate, production sampling, scored with reward.py"
  "$PY" "$MM/pick_text.py" generate --base "$BASE" --ckpt-root "$OUT" --ckpts "$CK" --data "$MM/data/text-dev.jsonl" \
      --out "$PK/text" --limit "$DEV_STEPS" ${WITH_BASE:+--with-base} > "$PK/text.log" 2>&1 || die "pick_text generate failed (tail $PK/text.log)"
  say "3/3 decision"
  "$PY" "$MM/pick_text.py" pick --text-results "$PK/text/text_results.json" --mmeval-results "$PK/mmeval/results.json" \
      --ckpt-root "$OUT" --strict --require-reading --json-out "$PK/picked.json" > "$PK/picked.path" 2> "$PK/picked.table.md"; rc=$?
  cat "$PK/picked.table.md"
  [ $rc -eq 0 ] && { say "PICKED: $(cat "$PK/picked.path")"; } || { say "NO CHECKPOINT QUALIFIES (exit $rc): do not merge; bring $PK back to the Mac"; exit $rc; } ;;
picked)
  [ -f "$PK/picked.json" ] || die "no decision yet: run '$0 pick'"
  cat "$PK/picked.table.md"; "$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print('chosen:',d['chosen'],d['path'])" "$PK/picked.json" ;;
merge)
  need_setup
  [ -f "$PK/picked.json" ] || die "no decision yet: run '$0 pick' (merging an unpicked checkpoint is what the picker exists to prevent)"
  P="$("$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print(d['path'] or '')" "$PK/picked.json")"
  [ -n "$P" ] && [ -d "$P" ] || die "picked.json names no adapter dir (chosen: none?)"
  REL="$("$PY" -c "import os,sys;print(os.path.relpath(sys.argv[1], sys.argv[2]))" "$P" "$OUT")"
  say "merging $REL"
  "$RUN" $TAG merge "$REL" ;;
package) need_setup; "$RUN" $TAG package ;;
bundle)
  [ -f "$PK/picked.json" ] || die "run '$0 pick' first"
  B="$TD/r4-sft-a6000-bundle.tgz"
  ( cd "$TD" && tar czf "$B" --ignore-failed-read ask-lora-out/ckpt ask-lora-out/final ask-lora-out/train.log.jsonl ask-lora-out/smoke.json \
      pick run-info.json ask-lora-adapter.zip ask-lora/stats.json ask-lora/smoke.log ask-lora/train.out "$WISP/freeze.txt" 2>/dev/null )
  ls -la "$B"; sha256sum "$B"
  echo "download: $B  and the folder $TD/gemma-4-E4B-it-Wisp-Ask-merged (SHA256SUMS inside)" ;;
*) sed -n '2,26p' "$0" ;;
esac
