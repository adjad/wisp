#!/bin/bash
# Wisp Ask LoRA: Brev GPU instance setup (one NVIDIA GPU, 48 GB class: L40S, A6000, A100, H100;
# or an 8x Blackwell node such as the B300 288 GiB: torchrun data parallel, see GPUs below).
#
# GPUs (r3). The torch wheel is chosen from the GPU's compute capability (nvidia-smi compute_cap):
#   8.x / 9.x  (A6000 sm_86, A100, L40S, H100)  -> cu128 wheels (driver 570 = CUDA 12.8 is enough)
#   10.x       (B200 sm_100, B300 sm_103)       -> cu130 wheels, which carry sm_103 kernels; they need driver >= 580.
#                                                  A driver below 580 falls back to cu128 (sm_100 only: a B300 may
#                                                  refuse to run it) and then to the nightly cu130 index.
# Every candidate is VERIFIED by running a bf16 matmul and an SDPA call on the GPU, not just by import; the first that
# passes is kept. Override with TORCH_INDEX=<url> (one candidate, still verified) and TORCH_SPEC='torch>=2.9'.
# Several GPUs: `run.sh <tag> train` launches torchrun with NPROC processes (default: every GPU nvidia-smi lists;
# NPROC=1 forces one). smoke, preflight and merge always use one GPU. The effective batch is --global-batch rows
# per optimizer step whatever NPROC is (default 8): GLOBAL_BATCH=<n> LR=<lr> in the environment pass through.
# Round 2 trains TWO separate LoRAs on one shared dataset: Gemma 4 E4B ("gemma") and
# NVIDIA Nemotron 3.5 Bolt E4B ("bolt"; an RL post-train of Gemma 4 E4B, same architecture).
#
# Run once on the instance:
#   bash vm-setup.sh     # Gemma (default); MODELS=bolt|both BOLT_REV=<40-hex commit> only if Bolt returns
#  - MODELS (default gemma): which bases to set up. Bolt is dormant.
#  - BOLT_REV: REQUIRED when bolt is included. Pin the exact commit of
#    nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16 (HF > Files > History). It is unknown to this kit, so setup
#    refuses to run without it. Override the repo with BOLT_REPO if it moved.
#    Check on the repo page whether it is gated or asks for a licence click-through (UraionLabs lists
#    OpenMDW-1.1) and read the terms. For a gated repo export HF_TOKEN first; setup logs in with it.
#  - installs torch + transformers>=5.10.1 + peft>=0.19 into ~/wisp/venv (uv, Python 3.11)
#  - downloads the pinned bf16 base(s), ~16 GB each. Gemma: google/gemma-4-E4B-it @ ee0ef602 (not gated).
#    With less than 150 GB free only the first model is downloaded; the other is fetched by
#    `run.sh <tag> base` (the run-both mode does this itself).
#  - writes ~/wisp/models.env and ~/wisp/run.sh, which runs every later step
# Per-model outputs live under ~/wisp/<tag>/ so a second run never overwrites the first.
# Log: ~/wisp/setup.log. Done when ~/wisp/SETUP_DONE exists.
set -uo pipefail

# The invoking user, except as root (Jupyter's uid 1000). Some Brev hosts log in as a non-1000 user
# (Hyperstack: shadeform, uid 1001), and uid 1000's home is not theirs to write.
JUSER="$(id -un)"; JHOME="$HOME"
if [ "$(id -u)" = 0 ]; then
  U1000="$(getent passwd 1000 | cut -d: -f1 || true)"
  [ -n "$U1000" ] && { JUSER="$U1000"; JHOME="$(getent passwd 1000 | cut -d: -f6)"; }
fi
WORK="$JHOME/wisp"

MODELS="${MODELS:-gemma}"   # round 2 trains Gemma only (Bolt is text-only, dropped 2026-10-31)
case "$MODELS" in
  gemma) TAGS="gemma" ;;
  bolt) TAGS="bolt" ;;
  both) TAGS="gemma bolt" ;;
  *) echo "FAIL: MODELS must be gemma, bolt or both (got '$MODELS')" >&2; exit 2 ;;
esac
BOLT_REPO="${BOLT_REPO:-nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16}"
BOLT_REV="${BOLT_REV:-}"
case " $TAGS " in
  *" bolt "*)
    if ! [[ "$BOLT_REV" =~ ^[0-9a-f]{40}$ ]]; then
      echo "FAIL: BOLT_REV is unset or not a full 40-hex commit sha." >&2
      echo "      Pin the exact revision of $BOLT_REPO (its HF commit history) and re-run:" >&2
      echo "      MODELS=$MODELS BOLT_REV=<sha> bash vm-setup.sh   (or MODELS=gemma to skip Bolt)" >&2
      exit 2
    fi ;;
esac

mkdir -p "$WORK/uploads" "$WORK/hf"
exec > >(tee -a "$WORK/setup.log") 2>&1
echo "== setup start $(date -u +%FT%TZ) user=$JUSER work=$WORK models=$MODELS"

GEMMA_REPO="google/gemma-4-E4B-it"
GEMMA_REV="ee0ef6023621cff504d758262d4e04895a5af4a2"   # template/tokenizer identical to Wisp's Gemma oQ4e build

nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || echo "WARN: nvidia-smi failed"
FREE_GB=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc 0-9)
# Estimates, not measurements: one model needs ~60 GB (base 16 + merged 16 + checkpoints + venv + data);
# both without deleting a base in between ~100-150 GB.
echo "free disk: ${FREE_GB}GB (estimate: ~60GB for one model, ~100-150GB for both)"
case "$MODELS" in
  both) [ "${FREE_GB:-0}" -ge 100 ] || echo "WARN: under ~100GB free for both models; run-both deletes each base after its merge when free space is under 80GB" ;;
  *) [ "${FREE_GB:-0}" -ge 60 ] || echo "WARN: less than 60GB free on $WORK" ;;
esac

cat > "$WORK/models.env" <<EOF
MODELS="$MODELS"
BOLT_REPO="$BOLT_REPO"
BOLT_REV="$BOLT_REV"
EOF

cat > "$WORK/run.sh" <<'RUN'
#!/bin/bash
# Wisp Ask LoRA steps (two models, one dataset):
#   ~/wisp/run.sh <gemma|bolt> <step> [args]      steps: check base unpack <sha256> preflight smoke train status sample merge package freebase
#   (train: NPROC=<n> torchrun processes, default every GPU; GLOBAL_BATCH=<rows> LR=<lr> EXTRA_TRAIN_ARGS='--audio-weight 0.08 ...')
#   ~/wisp/run.sh <gemma|bolt> sweep [file]       one training run per GPU (CUDA_VISIBLE_DEVICES=i, NPROC=1, own --out); then <tag> sweepstatus
#   ~/wisp/run.sh <gemma|bolt> auto <hours>       unattended preflight..package for one model; then <tag> autostatus
#   ~/wisp/run.sh both <hours-per-model> <sha256> gemma then bolt back to back, unattended; then bothstatus
# Each model keeps its files under ~/wisp/<tag>/ (data, out, merged, adapter zip), so nothing is overwritten.
set -uo pipefail
WORK="$(cd "$(dirname "$0")" && pwd)"
PY="$WORK/venv/bin/python"
UP="$WORK/uploads"; HF="$WORK/hf"
[ -f "$WORK/models.env" ] && . "$WORK/models.env"
DATA_ZIP=ask-lora-data.zip
TIGHT_GB="${TIGHT_GB:-80}"     # run-both deletes a finished base when free disk is under this
GEMMA_REPO="google/gemma-4-E4B-it"; GEMMA_REV="ee0ef6023621cff504d758262d4e04895a5af4a2"
die() { echo "FAIL: $*"; exit 1; }
need() { [ -e "$1" ] || die "$1 missing - run '$2' first"; }
free_gb() { df -BG --output=avail "$WORK" | tail -1 | tr -dc 0-9; }
bolt_rev_ok() { [[ "${BOLT_REV:-}" =~ ^[0-9a-f]{40}$ ]] || die "BOLT_REV is not a pinned 40-hex commit (see $WORK/models.env). Pin the exact revision of ${BOLT_REPO:-nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16}, edit models.env, and retry"; }
say() { echo "== $(date '+%F %T %Z') $*"; }

cfg() {   # per-model settings
  TD="$WORK/$TAG"
  case "$TAG" in
    gemma) REPO="$GEMMA_REPO"; REV="$GEMMA_REV"; MERGED="$TD/gemma-4-E4B-it-Wisp-Ask-merged" ;;
    bolt) bolt_rev_ok; REPO="${BOLT_REPO:-nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16}"; REV="$BOLT_REV"
          MERGED="$TD/NVIDIA-Nemotron-3.5-Bolt-E4B-Wisp-Ask-merged" ;;
  esac
  DATA="$TD/ask-lora"; OUT="$TD/ask-lora-out"
  mkdir -p "$TD"
}
base() { [ -s "$TD/base_path" ] || die "$TAG base not downloaded (run: $TAG base)"; cat "$TD/base_path"; }
T() { "$PY" "$DATA/train.py" "$@" --tag "$TAG"; }
gpu_count() { nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true; }
# The training launcher: torchrun (data parallel, NPROC processes) on a multi-GPU node, plain python on one GPU.
# Everything else (preflight, smoke, merge, sample) stays single-process on GPU 0.
launch_train() {
  local n="${NPROC:-$(gpu_count)}"
  if [ "${n:-1}" -gt 1 ]; then
    "$WORK/venv/bin/torchrun" --standalone --nproc_per_node="$n" "$DATA/train.py" "$@"
  else
    "$PY" "$DATA/train.py" "$@"
  fi
}
TRAIN_EXTRA=( ${GLOBAL_BATCH:+--global-batch "$GLOBAL_BATCH"} ${LR:+--lr "$LR"} ${EXTRA_TRAIN_ARGS:-} )
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

TAG="${1:-}"
case "$TAG" in
  gemma|bolt) shift; STEP="${1:-help}"; cfg ;;
  both|bothstatus|_both) STEP="$TAG"; TAG="" ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac

case "$STEP" in
check)
  echo "model: $TAG  repo: $REPO @ $REV"
  [ -f "$WORK/SETUP_DONE" ] && echo "setup: done" || echo "setup: NOT finished (tail ~/wisp/setup.log)"
  nvidia-smi --query-gpu=name,memory.used,memory.total,compute_cap,driver_version --format=csv,noheader
  echo "gpus: $(gpu_count)  (train launches torchrun with NPROC=${NPROC:-$(gpu_count)})"
  "$PY" -c "import torch,torchvision,transformers,peft;print('torch',torch.__version__,'torchvision',torchvision.__version__,'cuda',torch.version.cuda,'cuda ok',torch.cuda.is_available(),'arches',torch.cuda.get_arch_list(),'transformers',transformers.__version__,'peft',peft.__version__)"
  "$PY" -c "import transformers as t;print('multimodal class:', 'AutoModelForMultimodalLM' if hasattr(t,'AutoModelForMultimodalLM') else 'AutoModelForImageTextToText' if hasattr(t,'AutoModelForImageTextToText') else 'NONE')"
  [ -s "$TD/base_path" ] && echo "base: $(cat "$TD/base_path")" || echo "base: not downloaded (run: $TAG base)"
  [ -f "$UP/$DATA_ZIP" ] && echo "data zip: $(sha256sum "$UP/$DATA_ZIP" | cut -c1-64)  (compare with brev/upload-zip.json)" || echo "data zip: MISSING - upload $DATA_ZIP into ~/wisp/uploads"
  df -h "$WORK" | tail -1 ;;
base)
  if [ -s "$TD/base_path" ] && [ -d "$(cat "$TD/base_path")" ]; then echo "base already at $(cat "$TD/base_path")"; exit 0; fi
  [ "$(free_gb)" -ge 25 ] || die "under 25GB free; free space (or run '$TAG freebase' on a finished model) first"
  HF_HUB_ENABLE_HF_TRANSFER=1 "$PY" - "$REPO" "$REV" "$HF/$TAG" "$TD/base_path" <<'PY' || die "$TAG base download failed"
import sys
from pathlib import Path
from huggingface_hub import snapshot_download
repo, rev, cache, dest = sys.argv[1:5]
try:
    p = snapshot_download(repo, revision=rev, cache_dir=cache,
                          allow_patterns=["*.safetensors", "*.json", "*.jinja", "*.txt", "*.model"])
except Exception as exc:  # noqa: BLE001
    print(f"download failed: {type(exc).__name__}: {exc}")
    if any(w in str(exc).lower() for w in ("401", "403", "gated", "restricted", "token", "access")):
        print(f"{repo} may be gated or need a licence click-through: accept the terms on its Hugging Face page,"
              " export HF_TOKEN, and retry. Read the licence before training and sharing a derivative.")
    sys.exit(1)
if Path(p).name != rev:
    sys.exit(f"snapshot {Path(p).name} is not the pinned revision {rev}")
Path(dest).write_text(p)
print("base at", p)
PY
  ;;
unpack)
  # The sha256 comes from brev/upload-zip.json on the Mac, never from the archive itself.
  [ -f "$UP/$DATA_ZIP" ] || die "upload $DATA_ZIP into ~/wisp/uploads first"
  [ -n "${2:-}" ] || die "usage: $TAG unpack <sha256 from brev/upload-zip.json>"
  "$PY" - "$UP/$DATA_ZIP" "$DATA" "$2" <<'PY' || die "archive check failed; nothing unpacked"
import hashlib, json, shutil, sys, tempfile, zipfile
from pathlib import Path
archive, dest, want = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].lower()
names = {"train.jsonl", "dev.jsonl", "train.py", "render.py", "parity.jsonl", "stats.json", "MANIFEST.json"}
optional = {"vision_train.jsonl", "vision_dev.jsonl", "audio_train.jsonl", "audio_dev.jsonl"}
if hashlib.sha256(archive.read_bytes()).hexdigest() != want:
    sys.exit("archive sha256 does not match the one from upload-zip.json")
with zipfile.ZipFile(archive) as z:
    # r3: optional vision rows with their images under vision/, optional audio rows with their 16 kHz WAV clips
    # under audio/ (safe names only, any depth, no "..").
    import re
    got = {i.filename for i in z.infolist()}
    extra = got - names - optional
    bad = [n for n in extra if ".." in n or not (
        re.fullmatch(r"vision/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp)", n)
        or re.fullmatch(r"audio/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.wav", n))]
    if not names <= got or bad or any(i.is_dir() for i in z.infolist()):
        sys.exit("unexpected archive contents")
    files = json.loads(z.read("MANIFEST.json"))["files"]
    if set(files) != got - {"MANIFEST.json"}:
        sys.exit("MANIFEST does not list exactly the archive's files")
    for n, h in files.items():
        if hashlib.sha256(z.read(n)).hexdigest() != h:
            sys.exit(f"{n}: sha256 mismatch")
    stage = Path(tempfile.mkdtemp(prefix=".ask-lora-", dir=dest.parent))
    z.extractall(stage)
shutil.rmtree(dest, ignore_errors=True)
stage.rename(dest)
print(f"unpacked {len(files)} verified files -> {dest}")
PY
  ;;
preflight) need "$DATA/train.jsonl" "$TAG unpack"; T preflight --data "$DATA" --base "$(base)" ;;
smoke)
  need "$DATA/train.jsonl" "$TAG unpack"
  ( while sleep 2; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; done ) > "$DATA/smoke-mem.txt" & M=$!
  CUDA_VISIBLE_DEVICES=0 T smoke --data "$DATA" --base "$(base)" --out "$OUT" ${2:+--max-len "$2"} ${QLORA:+--qlora} "${TRAIN_EXTRA[@]}" 2>&1 | tee "$DATA/smoke.log"
  rc=${PIPESTATUS[0]}; kill $M 2>/dev/null
  echo "nvidia-smi peak: $(sort -n "$DATA/smoke-mem.txt" | tail -1) MiB"; exit "$rc" ;;
train)
  need "$DATA/train.jsonl" "$TAG unpack"
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then die "already running (pid $(cat "$DATA/train.pid"))"; fi
  nohup "$0" "$TAG" _train "${2:-}" "${3:-}" > "$DATA/train.out" 2>&1 & echo $! > "$DATA/train.pid"
  echo "training started (pid $!, ${NPROC:-$(gpu_count)} GPU process(es)). Check with: ~/wisp/run.sh $TAG status; adapters every --save-every steps land in $OUT/ckpt/step-NNNNN" ;;
_train)
  launch_train train --tag "$TAG" --data "$DATA" --base "$(base)" --out "$OUT" ${2:+--resume "$2"} ${3:+--max-len "$3"} \
    ${QLORA:+--qlora} "${TRAIN_EXTRA[@]}" ;;
sweep)
  # One independent single-GPU run per config line, so a B300 node trains up to 8 configs at once. A config file has
  # one run per line: "<name> <train.py flags>", e.g. "lr1e4 --lr 1e-4 --anchor-kl 0.5". Without a file the default
  # grid below runs (8 lines = 8 GPUs). Every run gets its own pid, log, and OUT=$TD/sweep/<name> (so ckpt/ and best/ never
  # collide); the multimodal GPU eval then picks the winner from the sweep/*/ckpt dirs.
  need "$DATA/train.jsonl" "$TAG unpack"
  CFG="${2:-$TD/sweep.txt}"
  if [ ! -f "$CFG" ]; then
    CFG="$TD/sweep.txt"
    cat > "$CFG" <<'GRID'
a_lr1e4_k05 --lr 1e-4 --epochs 2 --anchor-kl 0.5
b_lr2e4_k05 --lr 2e-4 --epochs 2 --anchor-kl 0.5
c_lr1e4_k10 --lr 1e-4 --epochs 2 --anchor-kl 1.0
d_lr2e4_k10 --lr 2e-4 --epochs 2 --anchor-kl 1.0
e_lr1e4_k05_e3 --lr 1e-4 --epochs 3 --anchor-kl 0.5
f_lr2e4_k20 --lr 2e-4 --epochs 2 --anchor-kl 2.0
g_lr1e4_k05_text --lr 1e-4 --epochs 2 --anchor-kl 0.5 --anchor-text-frac 0.1
h_lr5e5_k05_e3 --lr 5e-5 --epochs 3 --anchor-kl 0.5
GRID
    echo "no config file given: wrote the default 8-run grid to $CFG"
  fi
  NG=$(gpu_count); mkdir -p "$TD/sweep"; i=0
  while IFS= read -r line; do
    [ -z "${line// }" ] && continue
    case "$line" in \#*) continue ;; esac
    name="${line%% *}"; args="${line#"$name"}"
    [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || die "bad sweep run name '$name'"
    [ "$i" -lt "${NG:-1}" ] || die "config $((i + 1)) ('$name') has no GPU: this node lists ${NG:-0}; trim $CFG"
    R="$TD/sweep/$name"; mkdir -p "$R"
    if [ -f "$R/pid" ] && kill -0 "$(cat "$R/pid")" 2>/dev/null; then die "sweep run $name already running (pid $(cat "$R/pid"))"; fi
    # shellcheck disable=SC2086
    CUDA_VISIBLE_DEVICES=$i nohup "$PY" "$DATA/train.py" train --tag "$TAG" --data "$DATA" --base "$(base)" --out "$R/out" \
      ${QLORA:+--qlora} "${TRAIN_EXTRA[@]}" $args > "$R/train.out" 2>&1 & echo $! > "$R/pid"
    echo "gpu $i: $name (pid $!)  flags:$args"
    i=$((i + 1))
  done < "$CFG"
  echo "$i sweep run(s) started. Check with: ~/wisp/run.sh $TAG sweepstatus" ;;
sweepstatus)
  for R in "$TD"/sweep/*/; do
    [ -d "$R" ] || continue
    n=$(basename "$R"); st="not running"
    [ -f "$R/pid" ] && kill -0 "$(cat "$R/pid")" 2>/dev/null && st="RUNNING pid $(cat "$R/pid")"
    echo "== $n: $st"
    [ -f "$R/out/train.log.jsonl" ] && { grep '"event": "eval"' "$R/out/train.log.jsonl" | tail -1; }
    [ -f "$R/train.out" ] && grep -E "Error|FAIL|OOM|Traceback" "$R/train.out" | tail -2
  done ;;
status)
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then echo "RUNNING pid $(cat "$DATA/train.pid")"; else echo "not running"; fi
  nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader
  [ -f "$OUT/train.log.jsonl" ] && { grep '"event": "eval"' "$OUT/train.log.jsonl" | tail -4; tail -1 "$OUT/train.log.jsonl"; }
  [ -f "$DATA/train.out" ] && grep -E "Error|FAIL|OOM|Traceback" "$DATA/train.out" | tail -5 ;;
sample) need "$OUT/best/adapter_model.safetensors" "$TAG train"; T sample --base "$(base)" --adapter "$OUT/${2:-best}" --data "$DATA" ;;
merge)
  need "$OUT/best/adapter_model.safetensors" "$TAG train"
  rm -rf "$MERGED"
  T merge --base "$(base)" --adapter "$OUT/${2:-best}" --data "$DATA" --out "$MERGED" ;;
package)
  need "$OUT/best/adapter_model.safetensors" "$TAG train"
  "$PY" - "$OUT" "$DATA" "$TD/ask-lora-adapter.zip" <<'PY' || die "packaging the adapter failed"
import sys, zipfile
from pathlib import Path
from safetensors import safe_open
out, data, dest = map(Path, sys.argv[1:4])
best = out / "best" / "adapter_model.safetensors"
with safe_open(str(best), "pt") as fh:
    empty = [k for k in fh.keys() if 0 in fh.get_slice(k).get_shape()]
    n = len(list(fh.keys()))
if empty or not n:
    sys.exit(f"adapter has {len(empty)} empty tensors of {n} - not packaging")
files = [f for d in ("best", "final") for f in sorted((out / d).glob("*")) if f.name != "trainer_state.pt"]
files += [p for p in (out / "train.log.jsonl", out / "smoke.json", data / "smoke.log", data / "train.out") if p.exists()]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(f, f.relative_to(out) if f.is_relative_to(out) else f.name)
print(f"{dest}: {len(files)} files, {n} adapter tensors, {dest.stat().st_size / 1e6:.1f} MB")
PY
  # provenance for the Mac-side quantisation and the before/after comparison
  "$PY" - "$TAG" "$REPO" "$REV" "$(cat "$TD/base_path" 2>/dev/null)" "$UP/$DATA_ZIP" "$TD/run-info.json" <<'PY' || echo "WARN: run-info.json not written"
import hashlib, json, sys, time
from pathlib import Path
import peft, torch, transformers
tag, repo, rev, base, zp, dest = sys.argv[1:7]
tpl = Path(base) / "chat_template.jinja"
info = {"tag": tag, "base_repo": repo, "base_rev": rev, "written": time.strftime("%FT%TZ", time.gmtime()),
        "base_template_sha256": hashlib.sha256(tpl.read_bytes()).hexdigest() if tpl.is_file() else None,
        "data_zip_sha256": hashlib.sha256(Path(zp).read_bytes()).hexdigest() if Path(zp).is_file() else None,
        "torch": torch.__version__, "transformers": transformers.__version__, "peft": peft.__version__}
Path(dest).write_text(json.dumps(info, indent=1) + "\n")
print(json.dumps(info))
PY
  sha256sum "$TD/ask-lora-adapter.zip"
  [ -d "$MERGED" ] && { du -sh "$MERGED"; (cd "$MERGED" && sha256sum *.safetensors > SHA256SUMS && cat SHA256SUMS); }
  echo "download $TD/ask-lora-adapter.zip, $TD/run-info.json and the folder $MERGED (for quantizing on the Mac)" ;;
freebase)
  # Delete this model's bf16 base download once its merge is packaged (only when disk is tight).
  need "$TD/ask-lora-adapter.zip" "$TAG package"; need "$MERGED/SHA256SUMS" "$TAG package"
  rm -rf "$HF/$TAG" "$TD/base_path"
  echo "$TAG base deleted; free disk now $(free_gb)GB (re-download with: $TAG base)" ;;
auto)
  need "$DATA/train.jsonl" "$TAG unpack <sha256>"
  if [ -f "$TD/auto.pid" ] && kill -0 "$(cat "$TD/auto.pid")" 2>/dev/null; then die "auto already running (pid $(cat "$TD/auto.pid"))"; fi
  nohup setsid "$0" "$TAG" _auto "${2:-3}" > "$TD/auto.log" 2>&1 < /dev/null & echo $! > "$TD/auto.pid"
  sleep 2; echo "unattended run started (pid $(cat "$TD/auto.pid")); check with: ~/wisp/run.sh $TAG autostatus" ;;
_auto)
  END=$("$PY" -c "import time,sys; print(int(time.time() + float(sys.argv[1]) * 3600))" "${2:-3}")
  say "[$TAG] deadline $(date -d "@$END" '+%F %T %Z')"
  "$0" "$TAG" preflight || { say "STOP: [$TAG] preflight failed"; exit 1; }
  ML=""
  for try in 10240 8192; do
    say "[$TAG] smoke --max-len $try"
    "$0" "$TAG" smoke "$try"; rc=$?
    [ "$rc" -eq 0 ] && { ML=$try; break; }
    [ "$rc" -ge 5 ] && { say "STOP: [$TAG] smoke found a pipeline problem (exit $rc) - see $DATA/smoke.log"; exit 1; }
    say "[$TAG] smoke exit $rc (3 = memory tight, 4 = OOM); trying a shorter cap"
  done
  [ -n "$ML" ] || { say "STOP: [$TAG] no length cap passed smoke"; exit 1; }
  # smoke timed ONE GPU; N data-parallel GPUs measured about 0.8 N x (row lengths vary, the slowest rank sets the pace)
  EP=$(SPEEDUP="${SPEEDUP:-$(n=${NPROC:-$(gpu_count)}; [ "${n:-1}" -gt 1 ] && echo "$((n * 8 / 10))" || echo 1)}" "$PY" - "$OUT/smoke.json" "$END" <<'PY'
import json, os, sys, time
s = json.load(open(sys.argv[1])); end = float(sys.argv[2])
per = (s["epoch_min"] + s["eval_min_per_epoch"]) * 1.15 / max(1.0, float(os.environ.get("SPEEDUP") or 1))
print(max(1, min(2, int(((end - time.time()) / 60 - 30) // per))))
PY
)
  STOP=$(( END - 30 * 60 ))
  say "[$TAG] training $EP epoch(s) at --max-len $ML; hard stop $(date -d "@$STOP" '+%T %Z')"
  launch_train train --tag "$TAG" --data "$DATA" --base "$(base)" --out "$OUT" --epochs "$EP" --max-len "$ML" \
    --stop-at "$STOP" ${QLORA:+--qlora} "${TRAIN_EXTRA[@]}" >> "$DATA/train.out" 2>&1 || { say "STOP: [$TAG] training failed - tail of train.out:"; tail -5 "$DATA/train.out"; exit 1; }
  say "[$TAG] training done: $(grep -E '"event": "(done|stopped_at_deadline)"' "$OUT/train.log.jsonl" | tail -1)"
  "$0" "$TAG" sample > "$TD/sample.txt" 2>&1 || say "[$TAG] sample failed (non-fatal)"
  "$0" "$TAG" merge || { say "STOP: [$TAG] merge failed (adapter is in $OUT/best)"; exit 1; }
  "$0" "$TAG" package
  say "[$TAG] ALL DONE - download $TD/ask-lora-adapter.zip and $MERGED" ;;
autostatus)
  if [ -f "$TD/auto.pid" ] && kill -0 "$(cat "$TD/auto.pid")" 2>/dev/null; then echo "AUTO RUNNING (pid $(cat "$TD/auto.pid"))"; else echo "auto not running"; fi
  [ -f "$TD/auto.log" ] && grep '^== ' "$TD/auto.log" | tail -12
  "$0" "$TAG" status 2>/dev/null | tail -6 ;;
both)
  # gemma, then bolt, each with its own deadline of <hours-per-model>. Stops at the first failure
  # (GPU hours are billed): fix it, then resume that model's steps by hand or re-run both.
  H="${2:-3}"; SHA="${3:-}"
  [ -n "$SHA" ] || die "usage: both <hours-per-model> <sha256 from brev/upload-zip.json>"
  bolt_rev_ok
  [ -f "$UP/$DATA_ZIP" ] || die "upload $DATA_ZIP into ~/wisp/uploads first"
  if [ -f "$WORK/both.pid" ] && kill -0 "$(cat "$WORK/both.pid")" 2>/dev/null; then die "both already running (pid $(cat "$WORK/both.pid"))"; fi
  nohup setsid "$0" _both "$H" "$SHA" > "$WORK/both.log" 2>&1 < /dev/null & echo $! > "$WORK/both.pid"
  sleep 2; echo "run-both started (pid $(cat "$WORK/both.pid")); check with: ~/wisp/run.sh bothstatus" ;;
_both)
  H="${2:-3}"; SHA="$3"
  for t in gemma bolt; do
    say "=== $t: base, unpack, auto ($H h)"
    "$0" "$t" base || { say "STOP: $t base download failed; nothing later was started"; exit 1; }
    "$0" "$t" unpack "$SHA" || { say "STOP: $t unpack failed"; exit 1; }
    "$0" "$t" _auto "$H" || { say "STOP: $t failed; the other model was not started"; exit 1; }
    if [ "$t" = gemma ] && [ "$(free_gb)" -lt "$TIGHT_GB" ]; then
      say "free disk $(free_gb)GB < ${TIGHT_GB}GB: deleting the $t base before the next download"
      "$0" "$t" freebase || { say "STOP: could not free $t base"; exit 1; }
    fi
  done
  say "BOTH DONE - download ~/wisp/gemma and ~/wisp/bolt (adapter zip, run-info.json, merged folder), then stop the instance" ;;
bothstatus)
  if [ -f "$WORK/both.pid" ] && kill -0 "$(cat "$WORK/both.pid")" 2>/dev/null; then echo "BOTH RUNNING (pid $(cat "$WORK/both.pid"))"; else echo "both not running"; fi
  [ -f "$WORK/both.log" ] && grep '^== ' "$WORK/both.log" | tail -12
  for t in gemma bolt; do [ -d "$WORK/$t/ask-lora-out" ] && { echo "-- $t"; "$0" "$t" status 2>/dev/null | tail -4; }; done ;;
*) sed -n '2,8p' "$0" ;;
esac
RUN
chmod +x "$WORK/run.sh"

export UV_INSTALL_DIR="$WORK/bin" PATH="$WORK/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
UV="$(command -v uv || echo "$WORK/bin/uv")"
"$UV" venv -q -p 3.11 "$WORK/venv"
PY="$WORK/venv/bin/python"
pipi() { "$UV" pip install -q -p "$PY" "$@"; }

echo "== installing torch, transformers, peft"
# transformers>=5.5.2 fixes E4B's KV-shared layers under use_cache=False; Google's guide pins >=5.10.1.
# peft>=0.19 needs torch>=2.7.
pipi -U pip setuptools wheel packaging

# Which torch wheel? Decided by the GPU, then PROVEN on it (see the header). PyPI's default wheel (cu132 as of torch 2.15)
# needs a newer driver than Brev's hosts ship (Hyperstack A6000: driver 570 = CUDA 12.8), and torch then silently runs
# without a GPU, so the default is never used.
GPU_CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | sort -u | head -1 | tr -d ' ')"
DRV_MAJOR="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1)"
GPU_COUNT="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true)"
echo "gpus: ${GPU_COUNT:-0}  compute capability: ${GPU_CC:-unknown}  driver major: ${DRV_MAJOR:-unknown}"
CC_MAJOR="${GPU_CC%%.*}"; CC_MAJOR="${CC_MAJOR:-0}"; DRV_MAJOR="${DRV_MAJOR:-0}"
WHL="https://download.pytorch.org/whl"
CANDIDATES=()     # "index|extra uv flags|spec"
if [ -n "${TORCH_INDEX:-}" ]; then
  CANDIDATES=("${TORCH_INDEX}||${TORCH_SPEC:-torch>=2.7}")
elif [ "$CC_MAJOR" -ge 10 ] 2>/dev/null; then
  # Blackwell. sm_100 (B200) is in the cu128 wheels; sm_103 (B300, compute_cap 10.3) arrives with CUDA 12.9/13.0 wheels.
  echo "NOTE: Blackwell GPU ($GPU_CC): needs a torch build with sm_${GPU_CC/./} kernels; trying cu130 first"
  if [ "$DRV_MAJOR" -ge 580 ]; then
    CANDIDATES=("$WHL/cu130||torch>=2.9" "$WHL/cu128||torch>=2.8" "$WHL/nightly/cu130|--prerelease=allow|torch")
  else
    echo "WARN: driver $DRV_MAJOR is older than 580 (CUDA 13): cu130 wheels will not run. Ask for a newer driver image for a B300;"
    echo "      falling back to cu128 (sm_100 SASS + PTX; may fail on sm_103, in which case the verification below says so)"
    CANDIDATES=("$WHL/cu128||torch>=2.8" "$WHL/nightly/cu128|--prerelease=allow|torch")
  fi
else
  # Ampere/Ada/Hopper (A6000 sm_86, A100, L40S, H100): cu128 runs on driver 570; cu126 for older hosts.
  CANDIDATES=("$WHL/cu128||torch>=2.7" "$WHL/cu126||torch>=2.7")
fi
TORCH_OK=0
for cand in "${CANDIDATES[@]}"; do
  IFS='|' read -r IDX FLAGS SPEC <<< "$cand"
  echo "== torch candidate: $SPEC from $IDX $FLAGS"
  "$UV" pip uninstall -q -p "$PY" torch 2>/dev/null || true
  # shellcheck disable=SC2086
  if ! "$UV" pip install -q -p "$PY" --index-url "$IDX" $FLAGS "$SPEC"; then echo "install failed for $IDX"; continue; fi
  if "$PY" - <<'PYV'
import sys, torch
if not torch.cuda.is_available():
    sys.exit("torch.cuda.is_available() is False (driver too old for this wheel?)")
n = torch.cuda.device_count()
for i in range(n):
    cc = torch.cuda.get_device_capability(i)
    arch = f"sm_{cc[0]}{cc[1]}"
    have = torch.cuda.get_arch_list()
    # a real kernel launch: a missing sm_xxx image surfaces here as 'no kernel image is available'
    x = torch.randn(256, 256, device=f"cuda:{i}", dtype=torch.bfloat16)
    y = torch.nn.functional.scaled_dot_product_attention(x[None, None], x[None, None], x[None, None])
    assert torch.isfinite((x @ x).float().sum() + y.float().sum()).item()
    print(f"gpu {i}: {torch.cuda.get_device_name(i)} {arch} OK with torch {torch.__version__} cuda {torch.version.cuda}; wheel arches {have}")
    if i == 0 and arch not in have:
        print(f"note: {arch} is not in the wheel's arch list; it runs through PTX JIT (slower first launch, check speed in smoke)")
PYV
  then TORCH_OK=1; echo "== torch OK: $SPEC from $IDX"; break; fi
  echo "WARN: this torch build does not run on this GPU; trying the next candidate"
done
[ "$TORCH_OK" = 1 ] || echo "ERROR: no torch build ran on this GPU. Set TORCH_INDEX=<url> TORCH_SPEC='torch>=x' and re-run; a B300 needs a cu130 build and driver >= 580"
pipi "torch>=2.7" "transformers>=5.10.1,<6" "peft>=0.19.0" accelerate safetensors huggingface_hub hf_transfer \
  sentencepiece numpy pillow || echo "ERROR: install failed"
# Gemma 4's AutoProcessor loads its video processor, which needs torchvision. It
# must come from the same wheel index as the torch build that passed above.
# shellcheck disable=SC2086
"$UV" pip install -q -p "$PY" --index-url "$IDX" $FLAGS torchvision \
  || echo "ERROR: torchvision install failed (the vision preflight and image rows need it)"
# QLoRA only (not used on a 48 GB / 288 GB card); Blackwell wheels can lag, so a failure here is not fatal
pipi bitsandbytes || echo "WARN: bitsandbytes did not install (only --qlora needs it)"
"$PY" -c "import torch,torchvision,transformers,peft;print('torch',torch.__version__,'torchvision',torchvision.__version__,'cuda',torch.version.cuda,'gpu',torch.cuda.is_available(),'gpus',torch.cuda.device_count(),'transformers',transformers.__version__,'peft',peft.__version__)"
# multi-GPU needs NCCL to work between the GPUs: a tiny all-reduce on every GPU, so a broken fabric shows now, not mid-run
if [ "${GPU_COUNT:-0}" -gt 1 ]; then
  cat > "$WORK/nccl_check.py" <<'PYN'
import os, torch, torch.distributed as dist
dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
t = torch.ones(4, device="cuda") * (dist.get_rank() + 1)
dist.all_reduce(t)
if dist.get_rank() == 0:
    print(f"NCCL all-reduce over {dist.get_world_size()} GPUs OK: {t.tolist()}")
dist.destroy_process_group()
PYN
  "$WORK/venv/bin/torchrun" --standalone --nproc_per_node="$GPU_COUNT" "$WORK/nccl_check.py" 2>&1 | tail -3 \
    || echo "WARN: multi-GPU NCCL check failed; train with NPROC=1 or fix NCCL"
fi

# A gated repo needs a token on the box; log in once so later `run.sh <tag> base` calls work too.
if [ -n "${HF_TOKEN:-}" ]; then
  "$PY" -c "import os; from huggingface_hub import login; login(token=os.environ['HF_TOKEN'], add_to_git_credential=False)" \
    && echo "logged in to Hugging Face" || echo "WARN: Hugging Face login failed"
fi

FREE_GB=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc 0-9)
FIRST=1
for tag in $TAGS; do
  if [ "$FIRST" -eq 0 ] && [ "${FREE_GB:-0}" -lt 150 ]; then
    echo "== skipping $tag base for now (${FREE_GB}GB free, under 150GB): run '$WORK/run.sh $tag base' later; run-both does it itself"
    continue
  fi
  echo "== downloading $tag base"
  "$WORK/run.sh" "$tag" base || echo "ERROR: $tag base download failed (re-run: ~/wisp/run.sh $tag base)"
  FIRST=0
done

chown -R "$JUSER" "$WORK" 2>/dev/null || true
touch "$WORK/SETUP_DONE"
echo "== setup done $(date -u +%FT%TZ). Next: upload ask-lora-data.zip into ~/wisp/uploads, then ~/wisp/run.sh <gemma|bolt> check"
