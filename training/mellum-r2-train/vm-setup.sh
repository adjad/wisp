#!/bin/bash
# Wisp Mellum R2 training VM setup (one NVIDIA A6000 48GB, Ubuntu + CUDA driver, Jupyter).
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
#!/bin/bash
# Mellum R2 steps. Usage from a notebook cell: !~/wisp/run.sh <step>
# In order: check  unpack  preflight  smoke  train  status  merge  package
# Unattended overnight: auto <hours>  then  autostatus
set -uo pipefail
WORK="$(cd "$(dirname "$0")" && pwd)"
PY="$WORK/venv/bin/python"
UP="$WORK/uploads"; DATA="$WORK/mellum-r2"; OUT="$WORK/mellum-r2-lora"; MERGED="$WORK/Mellum2.1-12B-A2.5B-Thinking-Wisp-R2-merged"
DATA_ZIP=mellum-r2-data.zip
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
  rm -rf "$DATA"; mkdir -p "$DATA"; "$PY" -m zipfile -e "$UP/$DATA_ZIP" "$DATA" || die "unzip failed"   # python: zip/unzip may be absent
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
  rm -f "$WORK/mellum-r2-adapter.zip"
  # Python's zipfile, not zip(1): apt may not have run, and a missing zip once printed ALL DONE with no adapter.
  "$PY" - "$OUT" "$DATA" "$WORK/mellum-r2-adapter.zip" <<'PY' || die "packaging the adapter failed"
import sys, zipfile
from pathlib import Path
out, data, dest = map(Path, sys.argv[1:4])
files = [f for d in ("best", "final") for f in sorted((out / d).glob("*")) if f.name != "trainer_state.pt"]
files += [p for p in (out / "train.log.jsonl", data / "smoke.log", data / "train.out", out / "smoke.json") if p.exists()]
if not (out / "best" / "adapter_model.safetensors").exists():
    sys.exit("no best/adapter_model.safetensors")
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(f, f.relative_to(out) if f.is_relative_to(out) else f.name)
print(f"{dest}: {len(files)} files, {dest.stat().st_size / 1e6:.1f} MB")
PY
  sha256sum "$WORK/mellum-r2-adapter.zip"
  [ -d "$MERGED" ] && { du -sh "$MERGED"; (cd "$MERGED" && sha256sum *.safetensors > SHA256SUMS && cat SHA256SUMS); }
  echo "download wisp/mellum-r2-adapter.zip (small) and, for quantizing on the Mac, the files in $MERGED" ;;
auto)
  # Unattended: preflight -> smoke (lowering the length cap / dropping flash-attn until memory fits)
  # -> train with as many epochs (max 2) as fit before the deadline -> merge -> package.
  # Usage: !~/wisp/run.sh auto <hours until you need it>   e.g. auto 8
  [ -f "$DATA/train.jsonl" ] || { "$0" unpack || exit 1; }
  if [ -f "$WORK/auto.pid" ] && kill -0 "$(cat "$WORK/auto.pid")" 2>/dev/null; then die "auto already running (pid $(cat "$WORK/auto.pid"))"; fi
  nohup setsid "$0" _auto "${2:-8}" > "$WORK/auto.log" 2>&1 < /dev/null & echo $! > "$WORK/auto.pid"
  sleep 2; echo "overnight run started (pid $(cat "$WORK/auto.pid")); it survives closing the notebook."
  echo "check it with: !~/wisp/run.sh autostatus" ;;
_auto)
  say() { echo "== $(date '+%F %T %Z') $*"; }
  END=$("$PY" -c "import time,sys; print(int(time.time() + float(sys.argv[1]) * 3600))" "${2:-8}")
  say "deadline $(date -d "@$END" '+%F %T %Z') (${2:-8} h from now)"
  T preflight --data "$DATA" --base "$(base)" || { say "STOP: preflight failed"; exit 1; }
  touch "$DATA/PREFLIGHT_OK"
  ML=""; ATTN=""
  for try in "12288 auto" "12288 sdpa" "10240 sdpa" "8192 sdpa"; do
    set -- $try
    say "smoke --max-len $1 --attn $2"
    T smoke --data "$DATA" --base "$(base)" --out "$OUT" --max-len "$1" --attn "$2" 2>&1 | tee "$DATA/smoke.log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then ML=$1; ATTN=$2; break; fi
    say "smoke failed (exit $rc: 3 = memory tight, 4 = out of memory, other = crash); trying the next setting"
  done
  [ -n "$ML" ] || { say "STOP: no setting passed smoke - see $DATA/smoke.log"; exit 1; }
  EP=$("$PY" - "$OUT/smoke.json" "$END" <<'PY'
import json, sys, time
s = json.load(open(sys.argv[1])); end = float(sys.argv[2])
per = (s["epoch_min"] + s["eval_min_per_epoch"]) * 1.15        # 15% safety on the smoke speed
avail = (end - time.time()) / 60 - 30                         # merge + package + load margin
print(max(1, min(2, int(avail // per))))
PY
)
  STOP=$(( END - 30 * 60 ))
  say "smoke passed at --max-len $ML --attn $ATTN; training $EP epoch(s); hard stop $(date -d "@$STOP" '+%T %Z')"
  for attempt in 1 2; do
    RES=""
    if [ "$attempt" -eq 2 ]; then
      LAST=$(ls -d "$OUT"/step-* 2>/dev/null | tail -1)
      [ -d "$OUT/oom-recovery" ] && LAST="$OUT/oom-recovery"   # newer than any step checkpoint
      [ -n "$LAST" ] && RES="--resume $LAST"
      [ -d "$OUT/oom-recovery" ] && ML=$(( ML - 2048 ))
      ATTN=sdpa
      say "retrying: $RES --max-len $ML --attn $ATTN"
    fi
    "$PY" "$DATA/train/train.py" train --data "$DATA" --base "$(base)" --out "$OUT" --epochs "$EP" \
      --max-len "$ML" --attn "$ATTN" --stop-at "$STOP" $RES >> "$DATA/train.out" 2>&1 && break
    say "train exited with an error (attempt $attempt) - tail of train.out:"; tail -5 "$DATA/train.out"
    [ "$attempt" -eq 2 ] && { say "STOP: training failed twice"; exit 1; }
  done
  say "training done: $(grep -E '"event": "(done|stopped_at_deadline)"' "$OUT/train.log.jsonl" | tail -1)"
  "$0" merge || { say "STOP: merge failed (adapter is in $OUT/best)"; exit 1; }
  "$0" package
  say "ALL DONE - download wisp/mellum-r2-adapter.zip and the merged folder" ;;
autostatus)
  if [ -f "$WORK/auto.pid" ] && kill -0 "$(cat "$WORK/auto.pid")" 2>/dev/null; then echo "AUTO RUNNING (pid $(cat "$WORK/auto.pid"))"; else echo "auto not running"; fi
  [ -f "$WORK/auto.log" ] && grep '^== ' "$WORK/auto.log" | tail -12
  "$0" status 2>/dev/null | tail -6 ;;
*)
  sed -n '2,3p' "$0" ;;
esac
RUN
chmod +x "$WORK/run.sh"
chown -R "$JUSER" "$WORK" 2>/dev/null || true
touch "$WORK/SETUP_DONE"
echo "== setup done $(date -u +%FT%TZ). Next: upload mellum-r2-data.zip into wisp/uploads, then !~/wisp/run.sh check"
