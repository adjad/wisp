#!/bin/bash
# r5 on one RTX A6000 (about $0.60/h): base's answers (the user's choice, 2026-11-07) and training (the user's choice,
# 2026-11-08, replacing the AMD node for training). The 8x AMD MI355X path ($24/h) still works. A thin layer over
# ../../ask-lora-r4/a6000/run_sft.sh, the one-GPU A6000 recipe (batch 8 = 1 row x 8 accumulation, so the same optimizer steps
# on one GPU or eight), with what each card needs: on the MI355X --attn math (gfx950's fused SDPA backward fails the LoRA
# grad-cosine check) and one training process per GPU; on the A6000 sdpa, which smoke's strict parity check compares with
# math before anything trains (exit 8: rerun everything with ATTN=math).
#
#   bash run_r5.sh setup            the pinned stack (A6000: torch 2.11.0+cu128; MI355X: ROCm, rocm7.2 index first),
#                                   transformers 5.22.1, peft 0.21.2, the 16 GB base
#   bash run_r5.sh check            GPUs and versions
#   bash run_r5.sh finals PARTIAL [OUT_DIR]   base writes the final answers (../gpu/run_finals.sh), default ~/wisp/finals
#   bash run_r5.sh unpack SHA | preflight | smoke | train [resume-dir] | status     as run_sft.sh, on every GPU
#   bash run_r5.sh unzip-pickers    as run_sft.sh
#   bash run_r5.sh pick             base + up to MAX_CKPTS (3) candidates, PICK_BATCH (8) generations together, then
#                                   run_sft.sh's decision. With a GPU for each model and mmeval, they run side by side;
#                                   with fewer (the A6000), one after another on GPU 0
#   bash run_r5.sh picked | merge [ckpt] | package | bundle | stage2      as run_sft.sh
#
#   bash run_r5.sh pause            ask a running train to save everything at the end of its current step and exit 0
#   bash run_r5.sh resume [dir]     continue from the pause (default: the step in OUT/PAUSED.json, else the newest OUT/step-*)
#
# Early check: `train` stops itself after step 400 (PAUSE_AT, 0 = never) with the full state saved in OUT/step-00400 and
# OUT/PAUSED.json. Adapter snapshots also land at steps 50, 100 and 200 (SNAPSHOT_STEPS) in OUT/ckpt. Check them, then
# `resume`. Never kill the pid: a kill saves nothing. The box bills while idle.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SFT="$HERE/run_sft.sh"; [ -f "$SFT" ] || SFT="$HERE/../../ask-lora-r4/a6000/run_sft.sh"
[ -f "$SFT" ] || { echo "FAIL: run_sft.sh not found next to run_r5.sh" >&2; exit 1; }
# Hardware. NVIDIA (the RTX A6000, sm_86): run_sft.sh's pinned cu128 stack, one GPU, attention sdpa by default, which
# SELFCHECK (answers) and smoke (training) compare with math first. AMD (the 8x MI355X): math only.
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  HW=cuda; ATTN="${ATTN:-sdpa}"; export ATTN WISP_ATTN="$ATTN" NPROC="${NPROC:-$(nvidia-smi -L | grep -c '^GPU')}"
else
  HW=rocm; export ATTN=math WISP_ATTN=math NPROC="${NPROC:-8}" GPU_ARCH=gfx950
fi

WISP="${WISP:-$HOME/wisp}"; PY="$WISP/venv/bin/python"; TD="$WISP/gemma"; OUT="$TD/ask-lora-out"; MM="$WISP/mmeval"; PK="$TD/pick"
SEED=20261029
PAUSE_FILE="${PAUSE_FILE:-$OUT/PAUSE}"; export PAUSE_FILE
say() { echo "== $(date '+%F %T %Z') $*"; }
die() { echo "FAIL: $*" >&2; exit 1; }

case "${1:-help}" in
setup)
  if [ "$HW" = cuda ]; then PIN=1 bash "$SFT" setup; exit $?; fi       # torch 2.11.0+cu128, transformers 5.22.1, peft 0.21.2
  export PIN=0 TRANSFORMERS_SPEC='transformers==5.22.1' PEFT_SPEC='peft==0.21.2'
  bash "$SFT" setup ;;
check)
  "$WISP/run.sh" gemma check
  "$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'cuda',torch.version.cuda,'hip',torch.version.hip,'gpus',torch.cuda.device_count(),torch.cuda.get_device_name(0));print('transformers',transformers.__version__,'peft',peft.__version__)"
  if [ "$HW" = cuda ]; then
    "$PY" -c "import torch,sys;v=torch.__version__;sys.exit(0 if v.startswith('2.11.0') and 'cu128' in v else print('WARNING: the A6000 pin is torch 2.11.0+cu128; this is',v))"
  else
    "$PY" -c "import torch,sys;v=torch.__version__;sys.exit(0 if v.startswith('2.14.') and 'rocm7.2' in v else print('WARNING: r3 measured torch 2.14.1+rocm7.2 good and AMD 2.11.0+rocm7.13.0 bad; this is',v))"
  fi ;;
finals)
  [ -n "${2:-}" ] || die "usage: $0 finals PARTIAL.jsonl [OUT_DIR]"
  G="$HERE/run_finals.sh"; [ -f "$G" ] || G="$HERE/../gpu/run_finals.sh"
  FO="${3:-$WISP/finals}"; mkdir -p "$FO" || die "cannot make $FO"
  [ ! -f "$HERE/finals.r4reuse.jsonl" ] || [ -f "$FO/finals.r4reuse.jsonl" ] || cp "$HERE/finals.r4reuse.jsonl" "$FO/" || die "reuse copy failed"
  ATTN="$ATTN" NPROC="$NPROC" bash "$G" "$2" "$FO" ;;
pick)
  [ -f "$MM/pick_text.py" ] && [ -f "$MM/eval_mm.py" ] && [ -f "$MM/data/text-dev.jsonl" ] || die "run '$0 unzip-pickers' first"
  [ -d "$OUT/ckpt" ] || [ -d "$OUT/final" ] || die "no checkpoints under $OUT"
  BASE="$(cat "$TD/base_path")"; mkdir -p "$PK/text"
  CK="${CKPTS:-$("$PY" "$MM/pick_text.py" candidates --ckpt-root "$OUT" --max "${MAX_CKPTS:-3}" --min-frac "${MIN_FRAC:-0.45}" --include-steps "${EARLY_STEPS:-50,100,200}")}" || die "no candidates"
  [ -n "$CK" ] || die "no candidate checkpoints"
  say "candidates: $CK, plus the base"
  RUN_ID="${RUN_ID:-$(date +%Y%m%dT%H%M%S)}"      # one id for this pick: the base and every checkpoint must share it to be compared
  GATE=(); [ -f "$MM/data/gate_vision/cases.json" ] && GATE+=(--vision-cases "$MM/data/gate_vision/cases.json")
  [ -f "$MM/data/gate_audio/truth.json" ] && GATE+=(--audio-truth "$MM/data/gate_audio/truth.json")
  # Side by side needs a GPU for each model plus one for mmeval; otherwise (one A6000) one after another on GPU 0.
  NMODELS=$(( $(tr ',' '\n' <<< "$CK" | grep -c .) + 1 ))
  if [ "$NPROC" -gt "$NMODELS" ]; then SEQ=0; LAST=$(( NPROC - 1 )); else SEQ=1; LAST=0; fi
  say "$NMODELS models on $NPROC GPU(s): $([ $SEQ = 1 ] && echo 'one after another' || echo 'side by side')"
  PIDS=()
  on_gpu() {      # GPU LOG COMMAND...: in the background on its own GPU, or in the foreground on GPU 0
    local g=$1 log=$2; shift 2
    if [ "$SEQ" = 1 ]; then
      HIP_VISIBLE_DEVICES=0 CUDA_VISIBLE_DEVICES=0 "$@" > "$log" 2>&1 || die "$(basename "$log" .log) failed (tail $log)"
    else
      HIP_VISIBLE_DEVICES=$g CUDA_VISIBLE_DEVICES=$g "$@" > "$log" 2>&1 & PIDS+=($!)
    fi
  }
  on_gpu "$LAST" "$PK/mmeval.log" "$PY" "$MM/eval_mm.py" --base "$BASE" --ckpt-root "$OUT" --ckpts "$CK" --data "$MM/data" \
      ${GATE[@]+"${GATE[@]}"} --seed $SEED --out "$PK/mmeval"
  i=0
  for name in base ${CK//,/ }; do
    D="$PK/text-$name"; mkdir -p "$D"
    if [ "$name" = base ]; then ARGS=(--ckpts base); else ARGS=(--ckpts "$name" --no-base); fi
    on_gpu "$i" "$PK/text-$name.log" "$PY" "$MM/pick_text.py" generate --base "$BASE" --ckpt-root "$OUT" "${ARGS[@]}" --run-id "$RUN_ID" \
        --data "$MM/data/text-dev.jsonl" --out "$D" --limit "${DEV_STEPS:-360}" --samples "${SAMPLES:-1}" \
        --samples-safety "${SAMPLES_SAFETY:-3}" --samples-absence "${SAMPLES_ABSENCE:-3}" --safety-steps "${SAFETY_STEPS:-60}" \
        --absence-steps "${ABSENCE_STEPS:-60}" --batch "${PICK_BATCH:-8}" --device 0
    i=$(( i + 1 ))
  done
  for p in ${PIDS[@]+"${PIDS[@]}"}; do wait "$p" || die "a picker process failed (see $PK/*.log)"; done
  cat "$PK"/text-*/text_answers.jsonl > "$PK/text/text_answers.jsonl"
  "$PY" - "$PK" "$CK" <<'PYI'
import json, sys
from pathlib import Path
pk, ck = Path(sys.argv[1]), sys.argv[2].split(",")
info = json.loads((pk / "text-base" / "text_run_info.json").read_text())
info["checkpoints"] = ["base"] + ck
(pk / "text" / "text_run_info.json").write_text(json.dumps(info, indent=1))
PYI
  "$PY" "$MM/pick_text.py" score --data "$MM/data/text-dev.jsonl" --out "$PK/text" --ckpt-root "$OUT" > "$PK/score.log" 2>&1 || die "score failed"
  TOLS=(); for t in ${AUDIO_TOL-audio_wer_norm=0.03 audio_cer=0.02 audio_punct=0.05 audio_serving_wer_norm=0.03}; do TOLS+=(--tol "$t"); done
  "$PY" "$MM/pick_text.py" pick --text-results "$PK/text/text_results.json" --mmeval-results "$PK/mmeval/results.json" \
      --ckpt-root "$OUT" --strict --require-reading --top 2 "${TOLS[@]}" --json-out "$PK/picked.json" > "$PK/picked.path" 2> "$PK/picked.table.md"; rc=$?
  cat "$PK/picked.table.md"
  [ $rc -eq 0 ] && { say "TOP:"; cat "$PK/picked.path"; } || { [ $rc -eq 3 ] && say "INCOMPLETE: the pick's evidence isn't sound (see the table: pairing or truncation), so nothing is ranked" || say "NO CHECKPOINT QUALIFIES (exit $rc): don't merge; bring $PK back to the Mac"; exit $rc; } ;;
train)
  [ ! -e "$PAUSE_FILE" ] || die "$PAUSE_FILE exists (a left-over pause request): remove it, or use '$0 resume' to continue a paused run"
  PAUSE_AT="${PAUSE_AT:-400}" exec bash "$SFT" train "${2:-}" ;;
pause)
  mkdir -p "$(dirname "$PAUSE_FILE")" || die "cannot make $(dirname "$PAUSE_FILE")"
  date '+%F %T %Z' > "$PAUSE_FILE" || die "cannot write $PAUSE_FILE"
  say "pause requested: training saves OUT/step-N and exits 0 after its current optimizer step (watch: $0 status; $OUT/PAUSED.json appears)" ;;
resume)
  R="${2:-}"
  if [ -z "$R" ] && [ -f "$OUT/PAUSED.json" ]; then
    R="$("$PY" - "$OUT/PAUSED.json" <<'PYR'
import json, sys
print(json.load(open(sys.argv[1])).get("resume_dir", ""))
PYR
)"
  fi
  if [ -z "$R" ]; then R="$(ls -d "$OUT"/step-* 2>/dev/null | sort | tail -1)"; fi
  [ -n "$R" ] && [ -d "$R" ] || die "nothing to resume from (no OUT/PAUSED.json and no $OUT/step-*)"
  [ -f "$R/trainer_state.pt" ] && [ -f "$R/adapter_model.safetensors" ] || die "$R has no trainer state; resume needs a step-NNNNN dir, not ckpt/step-*"
  rm -f "$PAUSE_FILE" "$OUT/PAUSED.json"
  say "resuming from $R (PAUSE_AT=${PAUSE_AT:-0})"
  PAUSE_AT="${PAUSE_AT:-0}" exec bash "$SFT" train "$R" ;;
*)
  exec bash "$SFT" "$@" ;;
esac
