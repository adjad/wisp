#!/bin/bash
# Base writes r5's final answers on the GPU box: K samples per final, then one top-up pass for ids with no passing
# sample, then the choose step.
#
#   run_finals.sh PARTIAL OUT_DIR
#
# Env: ATTN=math (sdpa|math|eager)   NPROC=<GPUs> (default: every GPU torch sees)   BATCH=1 (see gen_finals.py)
#      K=1   TOPUP=4 (extra samples per no-pass id; 0 = none)   BASE=<model dir> (default: ~/wisp/gemma/base_path)
#      FAMILIES=a,b   LIMIT=N (smoke: first N rows of one shard, into OUT_DIR/smoke, no choose)
#      WISP_WORK=~/wisp (the setup's venv)   SELFCHECK=1 (batched vs single greedy check on GPU 0, then exit)
# The box needs this directory plus gen.py, choose.py and render.py from the repo (copied beside these scripts).
# Output: OUT_DIR/finals.s<I>of<N>.jsonl (raw), OUT_DIR/chosen.jsonl, stats.json, nopass.ids, drop.ids, logs/.
# Rerunning resumes: finished (id, sample) pairs are skipped.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)" || exit 1
PARTIAL="${1:?usage: run_finals.sh PARTIAL OUT_DIR}"
OUT="${2:?usage: run_finals.sh PARTIAL OUT_DIR}"
WORK="${WISP_WORK:-$HOME/wisp}"
PY="${PY:-$WORK/venv/bin/python}"
[ -f "$PARTIAL" ] || { echo "FAIL: $PARTIAL missing"; exit 1; }
BASE="${BASE:-$(cat "$WORK/gemma/base_path" 2>/dev/null)}"
[ -d "$BASE" ] || { echo "FAIL: base model dir not found (set BASE or run ~/wisp/run.sh gemma base)"; exit 1; }
ATTN="${ATTN:-math}"; BATCH="${BATCH:-1}"; K="${K:-1}"; TOPUP="${TOPUP:-4}"
NPROC="${NPROC:-$("$PY" -c 'import torch; print(torch.cuda.device_count())' 2>/dev/null || echo 0)}"
[ "${NPROC:-0}" -ge 1 ] || { echo "FAIL: no GPU found (set NPROC)"; exit 1; }
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
mkdir -p "$OUT/logs" || exit 1
FAM=(); [ -z "${FAMILIES:-}" ] || FAM=(--families "$FAMILIES")

if [ -n "${SELFCHECK:-}" ]; then
  exec "$PY" "$HERE/gen_finals.py" --partial "$PARTIAL" --out-dir "$OUT/selfcheck" --base "$BASE" --gpu 0 \
    --attn "$ATTN" --selfcheck ${FAM[@]+"${FAM[@]}"}
fi
if [ -n "${LIMIT:-}" ]; then
  echo "== smoke: $LIMIT rows, K=$K, batch $BATCH, attn $ATTN on GPU 0"
  exec "$PY" "$HERE/gen_finals.py" --partial "$PARTIAL" --out-dir "$OUT/smoke" --base "$BASE" --gpu 0 \
    --attn "$ATTN" --batch "$BATCH" --k "$K" --limit "$LIMIT" ${FAM[@]+"${FAM[@]}"}
fi

# $1 = total samples per id, $2 = optional ids file. One process per GPU, each its own shard and output file.
gen_pass() {
  local k="$1" ids=(); [ -z "${2:-}" ] || ids=(--ids "$2")
  local pids=() i rc=0
  echo "== $(date '+%F %T') generating: k=$k, $NPROC GPU(s), batch $BATCH, attn $ATTN ${2:+(top-up ids: $(wc -l < "$2"))}"
  for ((i = 0; i < NPROC; i++)); do
    "$PY" "$HERE/gen_finals.py" --partial "$PARTIAL" --out-dir "$OUT" --base "$BASE" --gpu "$i" \
      --shard "$i/$NPROC" --attn "$ATTN" --batch "$BATCH" --k "$k" ${FAM[@]+"${FAM[@]}"} ${ids[@]+"${ids[@]}"} \
      >> "$OUT/logs/gpu$i.log" 2>&1 &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || rc=1; done
  [ "$rc" -eq 0 ] || echo "WARN: a generation process failed; see $OUT/logs (a rerun resumes)"
  return 0
}
choose_step() {
  "$PY" "$HERE/choose_r5.py" --partial "$PARTIAL" --raw "$OUT/finals.*.jsonl" --out-chosen "$OUT/chosen.jsonl" \
    --out-stats "$OUT/stats.json" --out-nopass-ids "$OUT/nopass.ids" --out-drop-ids "$OUT/drop.ids" \
    --out-rejections "$OUT/rejections.jsonl" ${CHOOSE_ARGS:-}
}

gen_pass "$K"
echo "== choose after $K samples"; choose_step || exit 1
if [ "$TOPUP" -gt 0 ] && [ -s "$OUT/nopass.ids" ]; then
  cp "$OUT/nopass.ids" "$OUT/topup.ids"
  gen_pass "$((K + TOPUP))" "$OUT/topup.ids"
  echo "== final choose"; choose_step || exit 1
fi
# TRACES=1: base's own thought at each tool-call step, one sample each. Costs more node time: see README.md.
if [ -n "${TRACES:-}" ]; then
  pids=(); rc=0
  echo "== $(date '+%F %T') traces: $NPROC GPU(s), batch $BATCH"
  for ((i = 0; i < NPROC; i++)); do
    "$PY" "$HERE/gen_finals.py" --partial "$PARTIAL" --out-dir "$OUT" --base "$BASE" --gpu "$i" --targets calls \
      --shard "$i/$NPROC" --attn "$ATTN" --batch "$BATCH" --k 1 ${FAM[@]+"${FAM[@]}"} >> "$OUT/logs/traces$i.log" 2>&1 &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || rc=1; done
  [ "$rc" -eq 0 ] || echo "WARN: a trace process failed; see $OUT/logs (a rerun resumes)"
  "$PY" "$HERE/choose_traces.py" --partial "$PARTIAL" --raw "$OUT/traces.*.jsonl" --out-chosen "$OUT/traces.chosen.jsonl" \
    --out-stats "$OUT/traces.stats.json" || exit 1
fi
echo "== done $(date '+%F %T'): $OUT/chosen.jsonl, $OUT/stats.json, $OUT/drop.ids, $OUT/rejections.jsonl"
