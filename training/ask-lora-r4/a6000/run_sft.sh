#!/bin/bash
# r4 SFT on ONE 48 GB NVIDIA card on Brev, an RTX A6000 (sm_86; the user's choice, 2026-11-03) or an L40 (sm_89): the whole
# flow from a fresh instance to a picked checkpoint.
# A thin layer over ~/wisp/run.sh (written by ../../ask-lora-r3/brev/vm-setup.sh). README.md next to this file explains every step.
#
#   bash run_sft.sh setup                  pinned stack (torch 2.11.0+cu128, transformers 5.22.1, peft 0.21.2) + the 16 GB base
#   bash run_sft.sh check                  GPU, versions against the pins, zip sha
#   bash run_sft.sh unpack <sha256>        verify + unpack ask-lora-data.zip (sha from pack_r4.py's output / out/upload.json)
#   bash run_sft.sh preflight              template sha, render parity, lengths, stop tokens, vision + audio processor checks
#   bash run_sft.sh smoke                  attention parity (sdpa vs math, STRICT), cache parity, the 12 longest rows' peak memory
#   bash run_sft.sh train [resume-dir]     background run: 2 epochs, batch 8 (1 x 8 accumulation), LoRA r16, dev eval every 200 steps,
#                                          ~8 adapter snapshots spread evenly over the run (SAVE_CKPTS), not only late ones
#   bash run_sft.sh status                 progress, ETA, peak memory, evals
#   bash run_sft.sh unzip-pickers          unzip mmeval-data.zip and pickers.zip into ~/wisp/mmeval
#   bash run_sft.sh pick                   STAGE 1 of the behaviour pick: mmeval + text generations for the untuned base and every
#                                          candidate + hard gates (thoughts, reply length, unsafe, absence) + top 2 (hours; run it detached)
#   bash run_sft.sh picked                 print the top 2 and the tables
#   bash run_sft.sh merge [ckpt]           fold each of the TOP 2 adapters (or just [ckpt]) into the bf16 base: merged-<ckpt>/
#   bash run_sft.sh package                run-info.json, merged checksums (+ the adapter zip run.sh makes)
#   bash run_sft.sh bundle                 one tarball of everything small to download (the merged folders are separate)
#   bash run_sft.sh stage2                 print the Mac-side STAGE 2 instructions for the top 2 (also fine to run on the Mac)
#
# Knobs (environment): ATTN=sdpa|math|eager (default sdpa), PIN=0 (unpinned, torch>=2.7), EVAL_EVERY, DEV_TEXT_MAX,
# SAVE_CKPTS (adapter snapshots over the run, default 8) or SAVE_EVERY (steps; overrides SAVE_CKPTS),
# DEV_STEPS (text picker steps, default 360), SAMPLES (per step outside the slices, default 1), SAMPLES_SAFETY / SAMPLES_ABSENCE (gates c / d slices, default 3), SAFETY_STEPS / ABSENCE_STEPS (slice caps, default 60 each, 0 = none), MAX_CKPTS (candidates, default 6), MIN_FRAC (a candidate must be at or past this
# share of the run, default 0.45), CKPTS (comma list overriding the candidates), NO_BASE=1 (dev only: skip the base generation;
# every gate that compares with the base then fails), PRIVATE_EVAL_DIR (stage 2, Mac only; NEVER on the box), WISP (default ~/wisp).
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
EVAL_EVERY="${EVAL_EVERY:-200}"          # one eval = ~11 min on an A6000 (~10 on an L40, estimated) with --dev-text-max 150; see README
DEV_TEXT_MAX="${DEV_TEXT_MAX:-150}"
DEV_STEPS="${DEV_STEPS:-360}"
SEED=20261029                            # train.py's default; eval_mm.py's serving-shape hash must use the same one

# The pins. r2 trained on an A6000 with exactly this stack (ask-lora-r3/PLAN.md: "r2 stack ... is the starting pin").
PIN_TORCH="2.11.0"; PIN_TV="0.26.0"; PIN_TRANSFORMERS="5.22.1"; PIN_PEFT="0.21.2"
TORCH_IDX="https://download.pytorch.org/whl/cu128"     # needs driver 570+ (CUDA 12.8); its sm_86 kernels also run on the L40's sm_89

die() { echo "FAIL: $*" >&2; exit 1; }
say() { echo "== $(date '+%F %T %Z') $*"; }
need_setup() { [ -x "$RUN" ] && [ -f "$WISP/SETUP_DONE" ] || die "run '$0 setup' first (needs $RUN and $WISP/SETUP_DONE)"; }

# One GPU, batch 8 = 1 row x 8 accumulation steps (train.py's micro batch is always 1), the r3 recipe otherwise.
# --attn-parity-strict makes a wrong fused attention kernel fatal in smoke on CUDA too (default there: a warning).
# Snapshots: the checkpoint picker needs many, spread evenly, because dev loss does not choose (r3's lowest-loss checkpoint was
# the one that had stopped thinking). A non-eval snapshot step only adds a cheap multimodal-only eval (train.py), ~0.5 min.
SAVE_CKPTS="${SAVE_CKPTS:-8}"
SAVE_EVERY="${SAVE_EVERY:-}"             # empty: derived in 'train' from the number of text rows
export GLOBAL_BATCH=8 LR=2e-4 NPROC=1
train_args() {                           # $1 = --save-every steps
  echo "--epochs 2 --attn $ATTN --eval-every $EVAL_EVERY --save-every $1 --dev-text-max $DEV_TEXT_MAX \
--vision-weight 0.12 --audio-weight 0.06 --anchor-kl 0.5 --seed $SEED --attn-parity-strict"
}
export EXTRA_TRAIN_ARGS="$(train_args "${SAVE_EVERY:-$EVAL_EVERY}")"

# Steps between snapshots so that about $2 of them fall inside a 2-epoch run on $1 text rows (vision 0.12 / audio 0.06 pools,
# estimate.epoch_rows), rounded to 10, at least 50. 4,950 rows -> 1,510 steps -> every 190 -> 7 snapshots + the final.
derive_save_every() {
  local n=$1 k=$2 epoch spe total every
  epoch=$(( (n * 100 + 41) / 82 )); spe=$(( (epoch + 7) / 8 )); total=$(( spe * 2 ))
  every=$(( (total / k + 5) / 10 * 10 )); [ "$every" -ge 50 ] || every=50
  echo "$every"
}

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
known = {(8, 9): "l40", (8, 6): "a6000"}
if (p.major, p.minor) in known:
    print(f"  profile: python estimate.py --gpu {known[(p.major, p.minor)]}  (re-run it with smoke's tok/s before committing hours)")
else:
    print(f"  NOTE: sm_{p.major}{p.minor} is neither the L40 (sm_89) nor the A6000 (sm_86) the README was estimated for")
# A cubin for sm_86 runs on sm_89 (same major, lower minor): the cu128 wheels need not list sm_89 itself.
archs = [a for a in torch.cuda.get_arch_list() if a.startswith("sm_")]
if not any(a[3:-1] == str(p.major) and int(a[-1]) <= p.minor for a in archs if a[3:].isdigit()):
    print(f"  WARNING: this torch build has no kernels for sm_{p.major}{p.minor} (it lists {archs})")
if p.total_memory < 44 * 2**30:
    print("  WARNING: under 44 GiB; this recipe was sized for a 48 GB card (QLORA=1 is the 24 GB fallback, untested for r4)")
PYC
  nvidia-smi --query-gpu=name,driver_version,ecc.mode.current,memory.total,power.limit --format=csv,noheader 2>/dev/null | sed 's/^/  nvidia-smi: /'
  for z in ask-lora-data.zip mmeval-data.zip pickers.zip; do
    [ -f "$WISP/uploads/$z" ] && echo "  $z  $(sha256sum "$WISP/uploads/$z" | cut -c1-64)" || echo "  $z  MISSING in $WISP/uploads"
  done ;;
unpack)
  need_setup; [ -n "${2:-}" ] || die "usage: $0 unpack <sha256 of ask-lora-data.zip>"
  "$RUN" $TAG unpack "$2" ;;
preflight)    # train.py's default cap is 10,240; preflight at the smoke's 11,264 so the two ~10.9k rows count as fitting
  need_setup; B="$(ls -d "$WISP"/hf/gemma/models--google--gemma-4-E4B-it/snapshots/* | head -1)"
  "$PY" "$DATA/train.py" preflight --data "$DATA" --base "$B" --max-len 11264 --tag $TAG ;;
smoke)
  need_setup
  say "smoke: attention parity (sdpa vs math, strict), cache parity, anchor, 12 longest rows, 15-step overfit"
  # 11,264: the final r4 build has two rows of ~10.9k tokens (recover_from_tool_error-017-s2, session_recall_r4-230-s0)
  "$RUN" $TAG smoke 11264; rc=$?
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
  if [ -z "$SAVE_EVERY" ]; then
    NROWS="$(grep -c . "$DATA/train.jsonl" 2>/dev/null)"; NROWS="${NROWS:-0}"
    if [ "$NROWS" -gt 0 ]; then SAVE_EVERY="$(derive_save_every "$NROWS" "$SAVE_CKPTS")"
    else SAVE_EVERY="$EVAL_EVERY"; echo "WARN: $DATA/train.jsonl not found; snapshots every $SAVE_EVERY steps"; fi
  fi
  export EXTRA_TRAIN_ARGS="$(train_args "$SAVE_EVERY")"
  say "snapshots every $SAVE_EVERY steps (SAVE_CKPTS=$SAVE_CKPTS over $NROWS text rows), dev eval every $EVAL_EVERY"
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
  CK="${CKPTS:-$("$PY" "$MM/pick_text.py" candidates --ckpt-root "$OUT" --max "${MAX_CKPTS:-6}" --min-frac "${MIN_FRAC:-0.45}")}" || die "could not list candidates"
  [ -n "$CK" ] || die "no candidate checkpoints"
  say "candidates: $CK  (+ the untuned base as the reference of the behaviour gates)"
  GATE=(); [ -f "$MM/data/gate_vision/cases.json" ] && GATE+=(--vision-cases "$MM/data/gate_vision/cases.json")
  [ -f "$MM/data/gate_audio/truth.json" ] && GATE+=(--audio-truth "$MM/data/gate_audio/truth.json")
  say "1/3 mmeval (base + each candidate on vision, reading, documents, audio dev rows)"
  "$PY" "$MM/eval_mm.py" --base "$BASE" --ckpt-root "$OUT" --ckpts "$CK" --data "$MM/data" "${GATE[@]}" --seed $SEED \
      --out "$PK/mmeval" > "$PK/mmeval.log" 2>&1 || die "eval_mm failed (tail $PK/mmeval.log)"
  say "2/3 text generations: $DEV_STEPS valid steps x (base + each candidate), production sampling, same seeds, scored with reward.py"
  "$PY" "$MM/pick_text.py" generate --base "$BASE" --ckpt-root "$OUT" --ckpts "$CK" --data "$MM/data/text-dev.jsonl" \
      --out "$PK/text" --limit "$DEV_STEPS" --samples "${SAMPLES:-1}" --samples-safety "${SAMPLES_SAFETY:-3}" --samples-absence "${SAMPLES_ABSENCE:-3}" \
      --safety-steps "${SAFETY_STEPS:-60}" --absence-steps "${ABSENCE_STEPS:-60}" ${NO_BASE:+--no-base} > "$PK/text.log" 2>&1 || die "pick_text generate failed (tail $PK/text.log)"
  # The user accepts a small audio regression (2026-11-03): transcription quality may slip a little against the base
  # (mmeval's defaults are WER/CER +0.01, punctuation -0.02). No-speech-empty and injection obedience stay strict.
  # AUDIO_TOL="" restores the defaults.
  TOLS=(); for t in ${AUDIO_TOL-audio_wer_norm=0.03 audio_cer=0.02 audio_punct=0.05 audio_serving_wer_norm=0.03}; do TOLS+=(--tol "$t"); done
  say "3/3 decision: forbidden, mmeval (audio tolerances: ${TOLS[*]:-defaults}), gates a-d against the base, then the top 2 for stage 2"
  "$PY" "$MM/pick_text.py" pick --text-results "$PK/text/text_results.json" --mmeval-results "$PK/mmeval/results.json" \
      --ckpt-root "$OUT" --strict --require-reading --top 2 "${TOLS[@]}" --json-out "$PK/picked.json" > "$PK/picked.path" 2> "$PK/picked.table.md"; rc=$?
  cat "$PK/picked.table.md"
  [ $rc -eq 0 ] && { say "TOP (stage 2 candidates, best first):"; cat "$PK/picked.path"; echo "next: $0 merge, download, then $0 stage2 for the Mac steps"; } \
    || { say "NO CHECKPOINT QUALIFIES (exit $rc): do not merge; bring $PK back to the Mac"; exit $rc; } ;;
picked)
  [ -f "$PK/picked.json" ] || die "no decision yet: run '$0 pick'"
  cat "$PK/picked.table.md"
  "$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));[print('top',t['rank'],t['name'],'step',t['step'],t['path']) for t in d['top']]" "$PK/picked.json" ;;
merge)
  need_setup
  [ -f "$PK/picked.json" ] || die "no decision yet: run '$0 pick' (merging an unpicked checkpoint is what the picker exists to prevent)"
  # the TOP 2 of stage 1 (or just [ckpt], which must be one of them); each lands in $TD/merged-<ckpt>/ for the Mac to quantise
  LIST="$("$PY" - "$PK/picked.json" "${2:-}" <<'PYM'
import json, sys
d = json.load(open(sys.argv[1])); want = sys.argv[2]
top = [t for t in d.get("top") or [] if t.get("path")]
if want:
    top = [t for t in top if t["name"] == want]
    if not top:
        sys.exit(f"{want} is not one of the picked top {[t['name'] for t in d.get('top') or []]}")
if not top:
    sys.exit("picked.json names no adapter dir (chosen: none?)")
for t in top:
    print(t["name"], t["path"])
PYM
)" || die "no mergeable checkpoint (see above)"
  while read -r NAME P; do
    [ -d "$P" ] || die "$P is not a directory"
    REL="$("$PY" -c "import os,sys;print(os.path.relpath(sys.argv[1], sys.argv[2]))" "$P" "$OUT")"
    say "merging $NAME ($REL)"
    "$RUN" $TAG merge "$REL" || die "merge of $NAME failed"
    rm -rf "$TD/merged-$NAME"; mv "$TD/gemma-4-E4B-it-Wisp-Ask-merged" "$TD/merged-$NAME" || die "could not move the merged folder"
    ( cd "$TD/merged-$NAME" && sha256sum *.safetensors > SHA256SUMS ) && echo "  $TD/merged-$NAME  $(du -sh "$TD/merged-$NAME" | cut -f1)"
  done <<< "$LIST" ;;
stage2)
  # Printable anywhere. PRIVATE_EVAL_DIR is for the Mac: the private eval is never uploaded to the box.
  case "${PRIVATE_EVAL_DIR:-}" in "$WISP"/*) die "PRIVATE_EVAL_DIR=$PRIVATE_EVAL_DIR is under $WISP: the private eval must stay on the Mac, never on the GPU box";; esac
  PT="$MM/pick_text.py"; [ -f "$PT" ] || PT="$HERE/pick_text.py"; [ -f "$PT" ] || die "pick_text.py not found (run '$0 unzip-pickers', or run this on the Mac next to it)"
  PYX="$PY"; [ -x "$PYX" ] || PYX=python3
  [ -f "${PICKED:-$PK/picked.json}" ] || die "no picked.json: run '$0 pick' (or set PICKED=/path/to/picked.json on the Mac)"
  "$PYX" "$PT" stage2 --picked "${PICKED:-$PK/picked.json}" ;;
package) need_setup; "$RUN" $TAG package ;;
bundle)
  [ -f "$PK/picked.json" ] || die "run '$0 pick' first"
  B="$TD/r4-sft-bundle.tgz"
  ( cd "$TD" && tar czf "$B" --ignore-failed-read ask-lora-out/ckpt ask-lora-out/final ask-lora-out/train.log.jsonl ask-lora-out/smoke.json \
      pick run-info.json ask-lora-adapter.zip ask-lora/stats.json ask-lora/smoke.log ask-lora/train.out "$WISP/freeze.txt" 2>/dev/null )
  ls -la "$B"; sha256sum "$B"
  echo "download: $B  and the folders $TD/merged-<ckpt> for the top 2 (SHA256SUMS inside each); then '$0 stage2'" ;;
*) sed -n '2,26p' "$0" ;;
esac
