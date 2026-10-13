#!/bin/bash
# Wisp Ling V3 training VM setup (one NVIDIA A6000 48GB, Ubuntu + CUDA driver, Jupyter).
# Paste into the VM's setup-script field. Runs once at boot, usually as root.
#  - installs Axolotl into ~/wisp/venv (uv, Python 3.11)
#  - pre-downloads the pinned Ling base (inclusionAI/Ling-3.0-tiny @ 9a98e35, ~16GB)
#  - writes ~/wisp/run.sh, which runs every later step from a notebook cell
# Log: ~/wisp/setup.log. Done when ~/wisp/SETUP_DONE exists.
set -uo pipefail

# Whose home: WISP_USER if set, else the sudo caller, else a non-root caller, else uid 1000 (boot-time run as root).
JUSER="${WISP_USER:-${SUDO_USER:-}}"
[ -n "$JUSER" ] || { [ "$(id -u)" != 0 ] && JUSER="$(id -un)"; }
[ -n "$JUSER" ] || JUSER="$(getent passwd 1000 | cut -d: -f1 || true)"
[ -n "$JUSER" ] || JUSER="$(id -un)"
JHOME="$(getent passwd "$JUSER" | cut -d: -f6)"; [ -n "$JHOME" ] || JHOME="$HOME"
WORK="$JHOME/wisp"
mkdir -p "$WORK/uploads" "$WORK/hf"
exec > >(tee -a "$WORK/setup.log") 2>&1
echo "== setup start $(date -u +%FT%TZ) user=$JUSER work=$WORK"

BASE_REPO="inclusionAI/Ling-3.0-tiny"
BASE_REV="9a98e35fe1c9ee255f78dd64771c7ae15a799481"

nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || echo "WARN: nvidia-smi failed"
FREE_GB=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc 0-9)
echo "free disk: ${FREE_GB}GB (need ~60GB: base 16 + merged V2 16 + prepared data + checkpoints)"
[ "${FREE_GB:-0}" -ge 60 ] || echo "WARN: less than 60GB free on $WORK"

if [ -z "${WISP_NO_APT:-}" ] && command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq unzip zip git curl ca-certificates build-essential >/dev/null || echo "WARN: apt install failed"
fi

# Everything below must belong to $JUSER (uv keeps the Python interpreter in the installing user's home),
# so a root/sudo run hands over to $JUSER after the system packages.
if [ "$(id -u)" = 0 ] && [ "$JUSER" != root ]; then
  chown -R "$JUSER" "$WORK"
  exec sudo -u "$JUSER" -H env WISP_USER="$JUSER" WISP_NO_APT=1 bash "$(readlink -f "$0")"
fi

export UV_INSTALL_DIR="$WORK/bin" PATH="$WORK/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
UV="$(command -v uv || echo "$WORK/bin/uv")"
"$UV" venv -q -p 3.11 "$WORK/venv"
PY="$WORK/venv/bin/python"
pipi() { "$UV" pip install -q -p "$PY" "$@"; }

echo "== installing axolotl (pulls torch)"
pipi -U pip setuptools wheel packaging ninja
pipi "axolotl" huggingface_hub hf_transfer safetensors datasets || echo "ERROR: axolotl install failed"
echo "== Ling model code needs fla-core (KDA linear-attention kernels) and einops"
pipi -U fla-core einops || echo "ERROR: fla-core install failed - the model cannot load without it"
# PyPI's default torch wheel may target a newer CUDA than this VM's driver; reinstall the build that matches it.
DRV_CUDA="$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9]*\)\.\([0-9]*\).*/\1\2/p' | head -1)"
if [ -n "$DRV_CUDA" ] && ! "$PY" -c "import torch,sys;sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
  TV="$("$PY" -c "import torch;print(torch.__version__.split('+')[0])")"
  for TAG in cu$DRV_CUDA cu128 cu126 cu124; do
    [ "${TAG#cu}" -le "$DRV_CUDA" ] || continue
    echo "== torch $TV does not see the GPU (driver CUDA $DRV_CUDA); trying $TAG build"
    "$UV" pip install -q -p "$PY" --reinstall-package torch "torch==$TV" --index-url "https://download.pytorch.org/whl/$TAG" \
      && "$PY" -c "import torch,sys;sys.exit(0 if torch.cuda.is_available() else 1)" && break
  done
fi
"$PY" -c "import torch;print('torch',torch.__version__,'cuda',torch.version.cuda,'gpu',torch.cuda.is_available())"

echo "== optional: flash-attn (prebuilt wheel only; skipped if it would compile)"
if timeout 900 "$UV" pip install -q -p "$PY" --no-build-isolation --only-binary flash-attn flash-attn 2>/dev/null; then
  echo "flash-attn OK"
else
  echo "flash-attn unavailable -> run.sh will use sdp_attention"
fi
echo "== optional: cut-cross-entropy"
pipi "cut-cross-entropy[transformers] @ git+https://github.com/axolotl-ai-cloud/ml-cross-entropy.git" \
  && echo "cut-cross-entropy OK" || echo "cut-cross-entropy unavailable -> run.sh drops the plugin"

echo "== downloading base $BASE_REPO@$BASE_REV"
HF_HUB_ENABLE_HF_TRANSFER=1 "$PY" - "$BASE_REPO" "$BASE_REV" "$WORK" <<'PY' || echo "ERROR: base download failed (re-run: ~/wisp/run.sh base)"
import sys
from huggingface_hub import snapshot_download
repo, rev, work = sys.argv[1:4]
p = snapshot_download(repo, revision=rev, cache_dir=f"{work}/hf", allow_patterns=["*.safetensors", "*.json", "*.py", "*.jinja"])
open(f"{work}/base_path", "w").write(p)
print("base at", p)
PY

cat > "$WORK/run.sh" <<'RUN'
#!/usr/bin/env bash
# Ling V3 steps. Usage: ~/wisp/run.sh <step>   (from a notebook cell: !~/wisp/run.sh <step>)
# Steps in order: check  unpack  rebuild  preflight  preprocess  labels  smoke  train  status  package
set -uo pipefail
WORK="$(cd "$(dirname "$0")" && pwd)"
export PATH="$WORK/venv/bin:$PATH"   # axolotl train execs `accelerate` from PATH
PY="$WORK/venv/bin/python"; AX="$WORK/venv/bin/axolotl"
UP="$WORK/uploads"; DATA="$WORK/ling-v3"; V2="$WORK/Ling-3.0-tiny-Wisp-V2-merged"
DATA_ZIP=ling-train-dev-v3-20261012.zip
DATA_SHA=aab24f88fca2dbabb2b89a2ddb2a38026e655921e3f0b5cfec7b69c4f5c2bb18
RB_ZIP=ling-v2-rebuild.zip
RB_SHA=5c9410913ece6b9513fee5aea735180eeb59f6f5fa93cd71c71c0f107a83def6
BASE_REPO="inclusionAI/Ling-3.0-tiny"; BASE_REV="9a98e35fe1c9ee255f78dd64771c7ae15a799481"
die() { echo "FAIL: $*"; exit 1; }
need() { [ -e "$1" ] || die "$1 missing - run '$2' first"; }

sha_ok() {  # file expected
  [ -f "$UP/$1" ] || { echo "MISSING  $UP/$1  (upload it into wisp/uploads)"; return 1; }
  local got; got=$(sha256sum "$UP/$1" | cut -d' ' -f1)
  [ "$got" = "$2" ] && echo "OK       $1" || { echo "BAD SHA  $1  got $got"; return 1; }
}

case "${1:-help}" in
check)
  [ -f "$WORK/SETUP_DONE" ] && echo "setup: done" || echo "setup: NOT finished (tail ~/wisp/setup.log)"
  nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
  "$PY" -c "import torch,axolotl;print('torch',torch.__version__,'cuda ok',torch.cuda.is_available(),'axolotl',axolotl.__version__)"
  "$PY" -c "import flash_attn" 2>/dev/null && echo "flash-attn: yes" || echo "flash-attn: no (sdp fallback)"
  "$PY" -c "import fla.ops.kda, einops" 2>/dev/null && echo "fla-core: yes" || echo "fla-core: MISSING - model will not load (pip install -U fla-core einops)"
  "$PY" -c "import cut_cross_entropy" 2>/dev/null && echo "cut-CE: yes" || echo "cut-CE: no (plugin dropped)"
  [ -s "$WORK/base_path" ] && echo "base: $(cat "$WORK/base_path") ($(ls "$(cat "$WORK/base_path")"/*.safetensors 2>/dev/null | wc -l) shards)" || echo "base: not downloaded (run: base)"
  sha_ok $DATA_ZIP $DATA_SHA; sha_ok $RB_ZIP $RB_SHA; df -h "$WORK" | tail -1 ;;
base)
  HF_HUB_ENABLE_HF_TRANSFER=1 "$PY" - "$BASE_REPO" "$BASE_REV" "$WORK" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, rev, work = sys.argv[1:4]
p = snapshot_download(repo, revision=rev, cache_dir=f"{work}/hf", allow_patterns=["*.safetensors", "*.json", "*.py", "*.jinja"])
open(f"{work}/base_path", "w").write(p); print("base at", p)
PY
  ;;
unpack)
  sha_ok $DATA_ZIP $DATA_SHA && sha_ok $RB_ZIP $RB_SHA || die "fix uploads first"
  rm -rf "$DATA" "$WORK/ling-v2-rebuild"; mkdir -p "$DATA"
  unzip -q "$UP/$DATA_ZIP" -d "$DATA" && unzip -q "$UP/$RB_ZIP" -d "$WORK" && echo "unpacked -> $DATA and $WORK/ling-v2-rebuild" ;;
rebuild)
  need "$WORK/ling-v2-rebuild/rebuild_v2.py" unpack; need "$WORK/base_path" base
  "$PY" "$WORK/ling-v2-rebuild/rebuild_v2.py" --out "$V2" --base-dir "$(cat "$WORK/base_path")"
  rc=$?; [ $rc -eq 0 ] || [ $rc -eq 2 ] || die "rebuild failed (exit $rc)"
  echo "V2 merged weights at $V2" ;;
preflight)
  need "$DATA/preflight.py" unpack; need "$V2/config.json" rebuild
  cd "$DATA" && "$PY" preflight.py --local-files-only --tokenizer-dir "$V2" | tail -40 ;;
config)
  need "$DATA/axolotl-a6000.yaml" unpack
  cd "$DATA"; Y=axolotl-a6000.yaml
  [ -f $Y.orig ] || cp $Y $Y.orig
  cp $Y.orig $Y
  sed -i "s#^base_model: .*#base_model: $V2#" $Y
  if ! "$PY" -c "import flash_attn" 2>/dev/null; then
    sed -i 's#^flash_attention: true.*#flash_attention: false\nsdp_attention: true#' $Y; echo "config: flash-attn -> sdp_attention"; fi
  # cut-CE cannot patch Ling's custom bailing_hybrid model; never enable it for this model.
  sed -i '/^plugins:/d; /CutCrossEntropyPlugin/d; /^cut_cross_entropy:/d' $Y
  sed -e 's#^output_dir: .*#output_dir: ./smoke-out#' -e 's#^evals_per_epoch: .*#evals_per_epoch: 0#' \
      -e 's#^saves_per_epoch: .*#saves_per_epoch: 0#' $Y > smoke.yaml
  echo "max_steps: 20" >> smoke.yaml
  grep -E "^(base_model|flash_attention|sdp_attention|cut_cross_entropy|sequence_len|lora_alpha):" $Y ;;
preprocess)
  "$0" config || exit 1
  cd "$DATA" && "$AX" preprocess axolotl-a6000.yaml 2>&1 | tail -30 ;;
labels)
  need "$DATA/last_run_prepared" preprocess
  cd "$DATA"
  "$PY" check_labels.py ./last_run_prepared --tokenizer-dir "$V2" --local-files-only --sample 200 | tail -15 || die "label check (sample) FAILED - do not train"
  "$PY" check_labels.py ./last_run_prepared --tokenizer-dir "$V2" --local-files-only | tail -15 || die "label check FAILED - do not train"
  touch "$DATA/LABELS_OK"; echo "LABELS OK - safe to run smoke" ;;
smoke)
  need "$DATA/LABELS_OK" labels
  cd "$DATA"; rm -rf smoke-out
  ( while sleep 5; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; done ) > smoke-mem.txt & M=$!
  s=$(date +%s); "$AX" train smoke.yaml > smoke.log 2>&1; rc=$?; e=$(date +%s); kill $M 2>/dev/null
  tail -15 smoke.log
  echo "exit $rc; 20 steps in $((e-s))s; peak GPU memory $(sort -n smoke-mem.txt | tail -1) MiB"
  [ $rc -eq 0 ] && echo "full run estimate: ~$(( (e-s)*156/10/60 )) min (312 steps)" ;;
train)
  need "$DATA/LABELS_OK" labels
  cd "$DATA"
  if [ -f train.pid ] && kill -0 "$(cat train.pid)" 2>/dev/null; then die "already running (pid $(cat train.pid))"; fi
  nohup "$AX" train axolotl-a6000.yaml > train.log 2>&1 & echo $! > train.pid
  echo "training started (pid $!). Check with: !~/wisp/run.sh status" ;;
status)
  cd "$DATA" 2>/dev/null || die "not unpacked"
  if [ -f train.pid ] && kill -0 "$(cat train.pid)" 2>/dev/null; then echo "RUNNING pid $(cat train.pid)"; else echo "not running"; fi
  nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader
  [ -f train.log ] && tail -c 3000 train.log | tr '\r' '\n' | tail -8 ;;
package)
  O="$DATA/ling-v3-lora-out"; need "$O/adapter_model.safetensors" train
  cd "$DATA"; rm -f "$WORK/ling-v3-adapter.zip"
  zip -qj "$WORK/ling-v3-adapter.zip" "$O/adapter_model.safetensors" "$O/adapter_config.json" train.log axolotl-a6000.yaml smoke.log 2>/dev/null
  sha256sum "$WORK/ling-v3-adapter.zip"; echo "download wisp/ling-v3-adapter.zip from the file browser" ;;
*)
  sed -n '2,3p' "$0" ;;
esac
RUN
chmod +x "$WORK/run.sh"
chown -R "$JUSER" "$WORK" 2>/dev/null || true
touch "$WORK/SETUP_DONE"
echo "== setup done $(date -u +%FT%TZ). Next: upload the two zips into wisp/uploads, then !~/wisp/run.sh check"
