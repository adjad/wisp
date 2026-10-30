#!/bin/bash
# Wisp Ask LoRA: Brev GPU instance setup (one NVIDIA GPU, 48 GB class: L40S, A6000, A100, H100).
# Run once on the instance: `bash vm-setup.sh` (or paste it into Brev's setup-script field).
#  - installs torch + transformers>=5.10.1 + peft>=0.19 into ~/wisp/venv (uv, Python 3.11)
#  - downloads the pinned bf16 base google/gemma-4-E4B-it @ ee0ef602 (~16 GB, not gated)
#  - writes ~/wisp/run.sh, which runs every later step
# Log: ~/wisp/setup.log. Done when ~/wisp/SETUP_DONE exists.
set -uo pipefail

JUSER="$(getent passwd 1000 | cut -d: -f1 || true)"
JHOME="$(getent passwd 1000 | cut -d: -f6 || true)"
[ -n "$JHOME" ] || { JUSER="$(id -un)"; JHOME="$HOME"; }
WORK="$JHOME/wisp"
mkdir -p "$WORK/uploads" "$WORK/hf"
exec > >(tee -a "$WORK/setup.log") 2>&1
echo "== setup start $(date -u +%FT%TZ) user=$JUSER work=$WORK"

BASE_REPO="google/gemma-4-E4B-it"
BASE_REV="ee0ef6023621cff504d758262d4e04895a5af4a2"   # template/tokenizer identical to Wisp's oQ4e build

nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || echo "WARN: nvidia-smi failed"
FREE_GB=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc 0-9)
echo "free disk: ${FREE_GB}GB (need ~60GB: base 16 + merged 16 + checkpoints + data)"
[ "${FREE_GB:-0}" -ge 60 ] || echo "WARN: less than 60GB free on $WORK"

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
pipi "torch>=2.7" "transformers>=5.10.1,<6" "peft>=0.19.0" accelerate safetensors huggingface_hub hf_transfer \
  bitsandbytes sentencepiece || echo "ERROR: install failed"
"$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'cuda',torch.version.cuda,'gpu',torch.cuda.is_available(),'transformers',transformers.__version__,'peft',peft.__version__)"

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
# Wisp Ask LoRA steps:  ~/wisp/run.sh <step>
# In order: check  unpack <sha256>  preflight  smoke  train  status  sample  merge  package
# Unattended: auto <hours>  then  autostatus
set -uo pipefail
WORK="$(cd "$(dirname "$0")" && pwd)"
PY="$WORK/venv/bin/python"
UP="$WORK/uploads"; DATA="$WORK/ask-lora"; OUT="$WORK/ask-lora-out"; MERGED="$WORK/gemma-4-E4B-it-Wisp-Ask-merged"
DATA_ZIP=ask-lora-data.zip
BASE_REPO="google/gemma-4-E4B-it"; BASE_REV="ee0ef6023621cff504d758262d4e04895a5af4a2"
die() { echo "FAIL: $*"; exit 1; }
need() { [ -e "$1" ] || die "$1 missing - run '$2' first"; }
base() { [ -s "$WORK/base_path" ] || die "base not downloaded (run: base)"; cat "$WORK/base_path"; }
T() { "$PY" "$DATA/train.py" "$@"; }
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

case "${1:-help}" in
check)
  [ -f "$WORK/SETUP_DONE" ] && echo "setup: done" || echo "setup: NOT finished (tail ~/wisp/setup.log)"
  nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
  "$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'cuda ok',torch.cuda.is_available(),'transformers',transformers.__version__,'peft',peft.__version__)"
  "$PY" -c "import transformers as t;print('multimodal class:', 'AutoModelForMultimodalLM' if hasattr(t,'AutoModelForMultimodalLM') else 'AutoModelForImageTextToText' if hasattr(t,'AutoModelForImageTextToText') else 'NONE')"
  [ -s "$WORK/base_path" ] && echo "base: $(cat "$WORK/base_path")" || echo "base: not downloaded (run: base)"
  [ -f "$UP/$DATA_ZIP" ] && echo "data zip: $(sha256sum "$UP/$DATA_ZIP" | cut -c1-64)  (compare with brev/upload-zip.json)" || echo "data zip: MISSING - upload $DATA_ZIP into ~/wisp/uploads"
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
  # The sha256 comes from brev/upload-zip.json on the Mac, never from the archive itself.
  [ -f "$UP/$DATA_ZIP" ] || die "upload $DATA_ZIP into ~/wisp/uploads first"
  [ -n "${2:-}" ] || die "usage: unpack <sha256 from brev/upload-zip.json>"
  "$PY" - "$UP/$DATA_ZIP" "$DATA" "$2" <<'PY' || die "archive check failed; nothing unpacked"
import hashlib, json, shutil, sys, tempfile, zipfile
from pathlib import Path
archive, dest, want = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].lower()
names = {"train.jsonl", "dev.jsonl", "train.py", "stats.json", "MANIFEST.json"}
if hashlib.sha256(archive.read_bytes()).hexdigest() != want:
    sys.exit("archive sha256 does not match the one from upload-zip.json")
with zipfile.ZipFile(archive) as z:
    if {i.filename for i in z.infolist()} != names or any(i.is_dir() or "/" in i.filename for i in z.infolist()):
        sys.exit("unexpected archive contents")
    files = json.loads(z.read("MANIFEST.json"))["files"]
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
preflight) need "$DATA/train.jsonl" unpack; T preflight --data "$DATA" --base "$(base)" ;;
smoke)
  need "$DATA/train.jsonl" unpack
  ( while sleep 2; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; done ) > "$DATA/smoke-mem.txt" & M=$!
  T smoke --data "$DATA" --base "$(base)" --out "$OUT" ${2:+--max-len "$2"} ${QLORA:+--qlora} 2>&1 | tee "$DATA/smoke.log"
  rc=${PIPESTATUS[0]}; kill $M 2>/dev/null
  echo "nvidia-smi peak: $(sort -n "$DATA/smoke-mem.txt" | tail -1) MiB"; exit "$rc" ;;
train)
  need "$DATA/train.jsonl" unpack
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then die "already running (pid $(cat "$DATA/train.pid"))"; fi
  RES=""; [ -n "${2:-}" ] && RES="--resume $2"
  nohup "$PY" "$DATA/train.py" train --data "$DATA" --base "$(base)" --out "$OUT" $RES ${3:+--max-len "$3"} ${QLORA:+--qlora} \
    > "$DATA/train.out" 2>&1 & echo $! > "$DATA/train.pid"
  echo "training started (pid $!). Check with: ~/wisp/run.sh status" ;;
status)
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then echo "RUNNING pid $(cat "$DATA/train.pid")"; else echo "not running"; fi
  nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader
  [ -f "$OUT/train.log.jsonl" ] && { grep '"event": "eval"' "$OUT/train.log.jsonl" | tail -4; tail -1 "$OUT/train.log.jsonl"; }
  [ -f "$DATA/train.out" ] && grep -E "Error|FAIL|OOM|Traceback" "$DATA/train.out" | tail -5 ;;
sample) need "$OUT/best/adapter_model.safetensors" train; T sample --base "$(base)" --adapter "$OUT/${2:-best}" --data "$DATA" ;;
merge)
  need "$OUT/best/adapter_model.safetensors" train
  rm -rf "$MERGED"
  T merge --base "$(base)" --adapter "$OUT/${2:-best}" --data "$DATA" --out "$MERGED" ;;
package)
  need "$OUT/best/adapter_model.safetensors" train
  "$PY" - "$OUT" "$DATA" "$WORK/ask-lora-adapter.zip" <<'PY' || die "packaging the adapter failed"
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
  sha256sum "$WORK/ask-lora-adapter.zip"
  [ -d "$MERGED" ] && { du -sh "$MERGED"; (cd "$MERGED" && sha256sum *.safetensors > SHA256SUMS && cat SHA256SUMS); }
  echo "download ~/wisp/ask-lora-adapter.zip and the folder $MERGED (for oQ4e quantizing on the Mac)" ;;
auto)
  need "$DATA/train.jsonl" "unpack <sha256>"
  if [ -f "$WORK/auto.pid" ] && kill -0 "$(cat "$WORK/auto.pid")" 2>/dev/null; then die "auto already running (pid $(cat "$WORK/auto.pid"))"; fi
  nohup setsid "$0" _auto "${2:-3}" > "$WORK/auto.log" 2>&1 < /dev/null & echo $! > "$WORK/auto.pid"
  sleep 2; echo "unattended run started (pid $(cat "$WORK/auto.pid")); check with: ~/wisp/run.sh autostatus" ;;
_auto)
  say() { echo "== $(date '+%F %T %Z') $*"; }
  END=$("$PY" -c "import time,sys; print(int(time.time() + float(sys.argv[1]) * 3600))" "${2:-3}")
  say "deadline $(date -d "@$END" '+%F %T %Z')"
  "$0" preflight || { say "STOP: preflight failed"; exit 1; }
  ML=""
  for try in 10240 8192; do
    say "smoke --max-len $try"
    "$0" smoke "$try"; rc=$?
    [ "$rc" -eq 0 ] && { ML=$try; break; }
    [ "$rc" -ge 5 ] && { say "STOP: smoke found a pipeline problem (exit $rc) - see $DATA/smoke.log"; exit 1; }
    say "smoke exit $rc (3 = memory tight, 4 = OOM); trying a shorter cap"
  done
  [ -n "$ML" ] || { say "STOP: no length cap passed smoke"; exit 1; }
  EP=$("$PY" - "$OUT/smoke.json" "$END" <<'PY'
import json, sys, time
s = json.load(open(sys.argv[1])); end = float(sys.argv[2])
per = (s["epoch_min"] + s["eval_min_per_epoch"]) * 1.15
print(max(1, min(2, int(((end - time.time()) / 60 - 30) // per))))
PY
)
  STOP=$(( END - 30 * 60 ))
  say "training $EP epoch(s) at --max-len $ML; hard stop $(date -d "@$STOP" '+%T %Z')"
  "$PY" "$DATA/train.py" train --data "$DATA" --base "$(base)" --out "$OUT" --epochs "$EP" --max-len "$ML" \
    --stop-at "$STOP" ${QLORA:+--qlora} >> "$DATA/train.out" 2>&1 || { say "STOP: training failed - tail of train.out:"; tail -5 "$DATA/train.out"; exit 1; }
  say "training done: $(grep -E '"event": "(done|stopped_at_deadline)"' "$OUT/train.log.jsonl" | tail -1)"
  "$0" sample > "$WORK/sample.txt" 2>&1 || say "sample failed (non-fatal)"
  "$0" merge || { say "STOP: merge failed (adapter is in $OUT/best)"; exit 1; }
  "$0" package
  say "ALL DONE - download ~/wisp/ask-lora-adapter.zip and $MERGED" ;;
autostatus)
  if [ -f "$WORK/auto.pid" ] && kill -0 "$(cat "$WORK/auto.pid")" 2>/dev/null; then echo "AUTO RUNNING (pid $(cat "$WORK/auto.pid"))"; else echo "auto not running"; fi
  [ -f "$WORK/auto.log" ] && grep '^== ' "$WORK/auto.log" | tail -12
  "$0" status 2>/dev/null | tail -6 ;;
*) sed -n '2,4p' "$0" ;;
esac
RUN
chmod +x "$WORK/run.sh"
chown -R "$JUSER" "$WORK" 2>/dev/null || true
touch "$WORK/SETUP_DONE"
echo "== setup done $(date -u +%FT%TZ). Next: upload ask-lora-data.zip into ~/wisp/uploads, then ~/wisp/run.sh check"
