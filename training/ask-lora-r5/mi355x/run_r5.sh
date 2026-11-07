#!/bin/bash
# r5 on an 8x AMD MI355X node ($3 per GPU-hour, $24/h; the user's choice, 2026-11-06). A thin layer over
# ../../ask-lora-r4/a6000/run_sft.sh with what this hardware needs: --attn math (gfx950's fused SDPA backward fails the LoRA
# grad-cosine check) and one training process per GPU. r3 ran this hardware at 2.8 s per step (2026-11-02).
#
#   bash run_r5.sh setup            ROCm torch (rocm7.2 index first), transformers 5.22.1, peft 0.21.2, the 16 GB base
#   bash run_r5.sh check            GPUs and versions
#   bash run_r5.sh finals PARTIAL   base writes the final answers (../gpu/run_finals.sh)
#   bash run_r5.sh unpack SHA | preflight | smoke | train [resume-dir] | status     as run_sft.sh, on all GPUs
#   bash run_r5.sh unzip-pickers    as run_sft.sh
#   bash run_r5.sh pick             base + up to MAX_CKPTS (3) candidates, one GPU each, then run_sft.sh's decision
#   bash run_r5.sh picked | merge [ckpt] | package | bundle | stage2      as run_sft.sh
#
# Early stop: `status` shows the step. Stop at step 400 by killing the pid in ~/wisp/gemma/ask-lora/train.pid, then check
# that snapshot before paying for the rest. Never leave the node idle: it bills while idle.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SFT="$HERE/run_sft.sh"; [ -f "$SFT" ] || SFT="$HERE/../../ask-lora-r4/a6000/run_sft.sh"
[ -f "$SFT" ] || { echo "FAIL: run_sft.sh not found next to run_r5.sh" >&2; exit 1; }
export ATTN=math WISP_ATTN=math NPROC="${NPROC:-8}" GPU_ARCH=gfx950

WISP="${WISP:-$HOME/wisp}"; PY="$WISP/venv/bin/python"; TD="$WISP/gemma"; OUT="$TD/ask-lora-out"; MM="$WISP/mmeval"; PK="$TD/pick"
SEED=20261029
say() { echo "== $(date '+%F %T %Z') $*"; }
die() { echo "FAIL: $*" >&2; exit 1; }

case "${1:-help}" in
setup)
  export PIN=0 TRANSFORMERS_SPEC='transformers==5.22.1' PEFT_SPEC='peft==0.21.2'
  bash "$SFT" setup ;;
check)
  "$WISP/run.sh" gemma check
  "$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'hip',torch.version.hip,'gpus',torch.cuda.device_count());print('transformers',transformers.__version__,'peft',peft.__version__)"
  "$PY" -c "import torch,sys;v=torch.__version__;sys.exit(0 if v.startswith('2.14.') and 'rocm7.2' in v else print('WARNING: r3 measured torch 2.14.1+rocm7.2 good and AMD 2.11.0+rocm7.13.0 bad; this is',v))" ;;
finals)
  [ -n "${2:-}" ] || die "usage: $0 finals PARTIAL.jsonl"
  G="$HERE/run_finals.sh"; [ -f "$G" ] || G="$HERE/../gpu/run_finals.sh"
  ATTN=math NPROC="$NPROC" bash "$G" "$2" ;;
pick)
  [ -f "$MM/pick_text.py" ] && [ -f "$MM/eval_mm.py" ] && [ -f "$MM/data/text-dev.jsonl" ] || die "run '$0 unzip-pickers' first"
  [ -d "$OUT/ckpt" ] || [ -d "$OUT/final" ] || die "no checkpoints under $OUT"
  BASE="$(cat "$TD/base_path")"; mkdir -p "$PK/text"
  CK="${CKPTS:-$("$PY" "$MM/pick_text.py" candidates --ckpt-root "$OUT" --max "${MAX_CKPTS:-3}" --min-frac "${MIN_FRAC:-0.45}")}" || die "no candidates"
  [ -n "$CK" ] || die "no candidate checkpoints"
  say "candidates: $CK, plus the base; each on its own GPU, mmeval on the last GPU"
  GATE=(); [ -f "$MM/data/gate_vision/cases.json" ] && GATE+=(--vision-cases "$MM/data/gate_vision/cases.json")
  [ -f "$MM/data/gate_audio/truth.json" ] && GATE+=(--audio-truth "$MM/data/gate_audio/truth.json")
  LAST=$(( NPROC - 1 ))
  HIP_VISIBLE_DEVICES=$LAST CUDA_VISIBLE_DEVICES=$LAST "$PY" "$MM/eval_mm.py" --base "$BASE" --ckpt-root "$OUT" --ckpts "$CK" --data "$MM/data" \
      "${GATE[@]}" --seed $SEED --out "$PK/mmeval" > "$PK/mmeval.log" 2>&1 &
  PIDS=($!); i=0
  for name in base ${CK//,/ }; do
    D="$PK/text-$name"; mkdir -p "$D"
    if [ "$name" = base ]; then ARGS=(--ckpts base); else ARGS=(--ckpts "$name" --no-base); fi
    HIP_VISIBLE_DEVICES=$i CUDA_VISIBLE_DEVICES=$i "$PY" "$MM/pick_text.py" generate --base "$BASE" --ckpt-root "$OUT" "${ARGS[@]}" \
        --data "$MM/data/text-dev.jsonl" --out "$D" --limit "${DEV_STEPS:-360}" --samples "${SAMPLES:-1}" \
        --samples-safety "${SAMPLES_SAFETY:-3}" --samples-absence "${SAMPLES_ABSENCE:-3}" --safety-steps "${SAFETY_STEPS:-60}" \
        --absence-steps "${ABSENCE_STEPS:-60}" --device 0 > "$PK/text-$name.log" 2>&1 &
    PIDS+=($!); i=$(( i + 1 ))
  done
  for p in "${PIDS[@]}"; do wait "$p" || die "a picker process failed (see $PK/*.log)"; done
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
  [ $rc -eq 0 ] && { say "TOP:"; cat "$PK/picked.path"; } || { say "NO CHECKPOINT QUALIFIES (exit $rc): don't merge; bring $PK back to the Mac"; exit $rc; } ;;
*)
  exec bash "$SFT" "$@" ;;
esac
