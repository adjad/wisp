#!/usr/bin/env bash
# Wisp Mellum R1 training VM setup (one NVIDIA A6000 48GB, Ubuntu + CUDA driver, Jupyter).
# Paste into the VM's setup-script field. Runs once at boot, usually as root.
#  - installs torch + transformers (>=5.8.1, which has Mellum built in) + peft into ~/wisp/venv (uv, Python 3.11)
#  - pre-downloads the pinned bf16 base JetBrains/Mellum2.1-12B-A2.5B-Thinking @ 92ddae9f (~24GB)
#  - writes ~/wisp/run.sh, which runs every later step from a notebook cell
# Log: ~/wisp/setup.log. Done when ~/wisp/SETUP_DONE exists.
set -uo pipefail

JUSER="$(getent passwd 1000 | cut -d: -f1 || true)"
JHOME="$(getent passwd 1000 | cut -d: -f6 || true)"
[ -n "$JHOME" ] || { JUSER="$(id -un)"; JHOME="$HOME"; }
WORK="$JHOME/wisp"
mkdir -p "$WORK/uploads" "$WORK/hf"
exec > >(tee -a "$WORK/setup.log") 2>&1
echo "== setup start $(date -u +%FT%TZ) user=$JUSER work=$WORK"

BASE_REPO="JetBrains/Mellum2.1-12B-A2.5B-Thinking"
BASE_REV="92ddae9fc7665e9f801d141d2e5a6b2caf2460c4"   # the revision the local oQ5e build was quantized from

nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || echo "WARN: nvidia-smi failed"
FREE_GB=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc 0-9)
echo "free disk: ${FREE_GB}GB (need ~80GB: base 24 + merged 24 + checkpoints + data)"
[ "${FREE_GB:-0}" -ge 80 ] || echo "WARN: less than 80GB free on $WORK"

if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq unzip zip git curl ca-certificates build-essential >/dev/null || echo "WARN: apt install failed"
fi

export UV_INSTALL_DIR="$WORK/bin" PATH="$WORK/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
UV="$(command -v uv || echo "$WORK/bin/uv")"
"$UV" venv -q -p 3.11 "$WORK/venv"
PY="$WORK/venv/bin/python"
pipi() { "$UV" pip install -q -p "$PY" "$@"; }

echo "== installing torch, transformers, peft"
pipi -U pip setuptools wheel packaging ninja
pipi torch "transformers>=5.8.1,<6" peft accelerate safetensors huggingface_hub hf_transfer || echo "ERROR: install failed"
"$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'cuda',torch.version.cuda,'gpu',torch.cuda.is_available(),'transformers',transformers.__version__,'peft',peft.__version__)"
"$PY" -c "from transformers import MellumForCausalLM" 2>/dev/null && echo "Mellum class: OK" || echo "ERROR: this transformers has no MellumForCausalLM (need >=5.8.1)"

echo "== optional: flash-attn (prebuilt wheel only; skipped if it would compile)"
if timeout 900 "$UV" pip install -q -p "$PY" --no-build-isolation --only-binary flash-attn flash-attn 2>/dev/null; then
  echo "flash-attn OK"
else
  echo "flash-attn unavailable -> train.py uses sdpa"
fi

echo "== downloading base $BASE_REPO@$BASE_REV"
HF_HUB_ENABLE_HF_TRANSFER=1 "$PY" - "$BASE_REPO" "$BASE_REV" "$WORK" <<'PY' || echo "ERROR: base download failed (re-run: ~/wisp/run.sh base)"
import sys
from huggingface_hub import snapshot_download
repo, rev, work = sys.argv[1:4]
p = snapshot_download(repo, revision=rev, cache_dir=f"{work}/hf",
                      allow_patterns=["*.safetensors", "*.json", "*.jinja", "*.txt", "*.model"])
open(f"{work}/base_path", "w").write(p)
print("base at", p)
PY

cat > "$WORK/run.sh" <<'RUN'
#!/usr/bin/env bash
# Mellum R1 steps. Usage from a notebook cell: !~/wisp/run.sh <step>
# In order: check  unpack  preflight  smoke  train  status  merge  package
set -uo pipefail
WORK="$(cd "$(dirname "$0")" && pwd)"
PY="$WORK/venv/bin/python"
UP="$WORK/uploads"; DATA="$WORK/mellum-r1"; OUT="$WORK/mellum-r1-lora"; MERGED="$WORK/Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged"
DATA_ZIP=mellum-r1-data.zip
BASE_REPO="JetBrains/Mellum2.1-12B-A2.5B-Thinking"; BASE_REV="92ddae9fc7665e9f801d141d2e5a6b2caf2460c4"
die() { echo "FAIL: $*"; exit 1; }
need() { [ -e "$1" ] || die "$1 missing - run '$2' first"; }
base() { [ -s "$WORK/base_path" ] || die "base not downloaded (run: base)"; cat "$WORK/base_path"; }
T() { "$PY" "$DATA/train/train.py" "$@"; }
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

case "${1:-help}" in
check)
  [ -f "$WORK/SETUP_DONE" ] && echo "setup: done" || echo "setup: NOT finished (tail ~/wisp/setup.log)"
  nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
  "$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'cuda ok',torch.cuda.is_available(),'transformers',transformers.__version__,'peft',peft.__version__)"
  "$PY" -c "from transformers import MellumForCausalLM" 2>/dev/null && echo "Mellum class: yes" || echo "Mellum class: NO (need transformers>=5.8.1)"
  "$PY" -c "import flash_attn" 2>/dev/null && echo "flash-attn: yes" || echo "flash-attn: no (sdpa)"
  [ -s "$WORK/base_path" ] && echo "base: $(cat "$WORK/base_path") ($(ls "$(cat "$WORK/base_path")"/*.safetensors 2>/dev/null | wc -l) shards)" || echo "base: not downloaded (run: base)"
  # Expert layout decides whether 4-bit QLoRA could help (it cannot quantize fused 3-D expert tensors).
  [ -s "$WORK/base_path" ] && "$PY" -c "
import json,sys; m=json.load(open(sys.argv[1]+'/model.safetensors.index.json'))['weight_map']
print('layer-0 weights:', ', '.join(k.split('layers.0.')[1] for k in sorted(m) if '.layers.0.' in k and 'norm' not in k)[:400])" "$(cat "$WORK/base_path")"
  [ -f "$UP/$DATA_ZIP" ] && echo "data zip: $(sha256sum "$UP/$DATA_ZIP" | cut -c1-64)  (compare with upload-zip.json)" || echo "data zip: MISSING - upload $DATA_ZIP into wisp/uploads"
  df -h "$WORK" | tail -1 ;;
base)
  HF_HUB_ENABLE_HF_TRANSFER=1 "$PY" - "$BASE_REPO" "$BASE_REV" "$WORK" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, rev, work = sys.argv[1:4]
p = snapshot_download(repo, revision=rev, cache_dir=f"{work}/hf",
                      allow_patterns=["*.safetensors", "*.json", "*.jinja", "*.txt", "*.model"])
open(f"{work}/base_path", "w").write(p); print("base at", p)
PY
  ;;
unpack)
  [ -f "$UP/$DATA_ZIP" ] || die "upload $DATA_ZIP into wisp/uploads first"
  rm -rf "$DATA"; mkdir -p "$DATA"; unzip -q "$UP/$DATA_ZIP" -d "$DATA" || die "unzip failed"
  # Every file must match the manifest packed inside the zip.
  (cd "$DATA" && "$PY" -c "
import hashlib,json,sys
m=json.load(open('MANIFEST.json')); bad=[f for f,h in m['files'].items() if hashlib.sha256(open(f,'rb').read()).hexdigest()!=h]
print('manifest:',len(m['files']),'files,',('ALL OK' if not bad else 'BAD: '+', '.join(bad))); sys.exit(1 if bad else 0)") || die "manifest check failed"
  echo "unpacked -> $DATA" ;;
preflight)
  need "$DATA/train.jsonl" unpack
  T preflight --data "$DATA" --base "$(base)" && touch "$DATA/PREFLIGHT_OK" ;;
smoke)
  need "$DATA/PREFLIGHT_OK" preflight
  ( while sleep 2; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; done ) > "$DATA/smoke-mem.txt" & M=$!
  T smoke --data "$DATA" --base "$(base)" --out "$OUT" ${2:+--max-len "$2"} 2>&1 | tee "$DATA/smoke.log"; kill $M 2>/dev/null
  echo "nvidia-smi peak: $(sort -n "$DATA/smoke-mem.txt" | tail -1) MiB" ;;
train)
  need "$DATA/PREFLIGHT_OK" preflight
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then die "already running (pid $(cat "$DATA/train.pid"))"; fi
  RES=""; [ -n "${2:-}" ] && RES="--resume $2"
  nohup "$PY" "$DATA/train/train.py" train --data "$DATA" --base "$(base)" --out "$OUT" $RES ${3:+--max-len "$3"} \
    > "$DATA/train.out" 2>&1 & echo $! > "$DATA/train.pid"
  echo "training started (pid $!). Check with: !~/wisp/run.sh status" ;;
status)
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then echo "RUNNING pid $(cat "$DATA/train.pid")"; else echo "not running"; fi
  nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader
  [ -f "$OUT/train.log.jsonl" ] && { grep '"event": "eval"' "$OUT/train.log.jsonl" | tail -3; tail -2 "$OUT/train.log.jsonl"; }
  [ -f "$DATA/train.out" ] && grep -E "Error|FAIL|OOM|Traceback" "$DATA/train.out" | tail -5 ;;
merge)
  need "$OUT/best/adapter_model.safetensors" train
  rm -rf "$MERGED"
  T merge --base "$(base)" --adapter "$OUT/${2:-best}" --data "$DATA" --out "$MERGED" ;;
package)
  need "$OUT/best/adapter_model.safetensors" train
  rm -f "$WORK/mellum-r1-adapter.zip"
  (cd "$OUT" && zip -qr "$WORK/mellum-r1-adapter.zip" best final train.log.jsonl -x '*/trainer_state.pt')
  (cd "$DATA" && zip -qj "$WORK/mellum-r1-adapter.zip" smoke.log train.out 2>/dev/null)
  sha256sum "$WORK/mellum-r1-adapter.zip"
  [ -d "$MERGED" ] && { du -sh "$MERGED"; (cd "$MERGED" && sha256sum *.safetensors > SHA256SUMS && cat SHA256SUMS); }
  echo "download wisp/mellum-r1-adapter.zip (small) and, for quantizing on the Mac, the files in $MERGED" ;;
*)
  sed -n '2,3p' "$0" ;;
esac
RUN
chmod +x "$WORK/run.sh"
chown -R "$JUSER" "$WORK" 2>/dev/null || true
touch "$WORK/SETUP_DONE"
echo "== setup done $(date -u +%FT%TZ). Next: upload mellum-r1-data.zip into wisp/uploads, then !~/wisp/run.sh check"
