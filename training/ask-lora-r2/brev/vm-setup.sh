#!/bin/bash
# Wisp Ask LoRA: Brev GPU instance setup (one NVIDIA GPU, 48 GB class: L40S, A6000, A100, H100).
# Round 2 trains TWO separate LoRAs on one shared dataset: Gemma 4 E4B ("gemma") and
# NVIDIA Nemotron 3.5 Bolt E4B ("bolt"; an RL post-train of Gemma 4 E4B, same architecture).
#
# Run once on the instance:
#   MODELS=both BOLT_REV=<40-hex commit> bash vm-setup.sh     # or MODELS=gemma / MODELS=bolt
#  - MODELS (default both): which bases to set up.
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

JUSER="$(getent passwd 1000 | cut -d: -f1 || true)"
JHOME="$(getent passwd 1000 | cut -d: -f6 || true)"
[ -n "$JHOME" ] || { JUSER="$(id -un)"; JHOME="$HOME"; }
WORK="$JHOME/wisp"

MODELS="${MODELS:-both}"
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
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

TAG="${1:-}"
case "$TAG" in
  gemma|bolt) shift; STEP="${1:-help}"; cfg ;;
  both|bothstatus|_both) STEP="$TAG"; TAG="" ;;
  *) sed -n '2,6p' "$0"; exit 1 ;;
esac

case "$STEP" in
check)
  echo "model: $TAG  repo: $REPO @ $REV"
  [ -f "$WORK/SETUP_DONE" ] && echo "setup: done" || echo "setup: NOT finished (tail ~/wisp/setup.log)"
  nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
  "$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'cuda ok',torch.cuda.is_available(),'transformers',transformers.__version__,'peft',peft.__version__)"
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
preflight) need "$DATA/train.jsonl" "$TAG unpack"; T preflight --data "$DATA" --base "$(base)" ;;
smoke)
  need "$DATA/train.jsonl" "$TAG unpack"
  ( while sleep 2; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; done ) > "$DATA/smoke-mem.txt" & M=$!
  T smoke --data "$DATA" --base "$(base)" --out "$OUT" ${2:+--max-len "$2"} ${QLORA:+--qlora} 2>&1 | tee "$DATA/smoke.log"
  rc=${PIPESTATUS[0]}; kill $M 2>/dev/null
  echo "nvidia-smi peak: $(sort -n "$DATA/smoke-mem.txt" | tail -1) MiB"; exit "$rc" ;;
train)
  need "$DATA/train.jsonl" "$TAG unpack"
  if [ -f "$DATA/train.pid" ] && kill -0 "$(cat "$DATA/train.pid")" 2>/dev/null; then die "already running (pid $(cat "$DATA/train.pid"))"; fi
  RES=""; [ -n "${2:-}" ] && RES="--resume $2"
  nohup "$PY" "$DATA/train.py" train --tag "$TAG" --data "$DATA" --base "$(base)" --out "$OUT" $RES ${3:+--max-len "$3"} ${QLORA:+--qlora} \
    > "$DATA/train.out" 2>&1 & echo $! > "$DATA/train.pid"
  echo "training started (pid $!). Check with: ~/wisp/run.sh $TAG status" ;;
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
  EP=$("$PY" - "$OUT/smoke.json" "$END" <<'PY'
import json, sys, time
s = json.load(open(sys.argv[1])); end = float(sys.argv[2])
per = (s["epoch_min"] + s["eval_min_per_epoch"]) * 1.15
print(max(1, min(2, int(((end - time.time()) / 60 - 30) // per))))
PY
)
  STOP=$(( END - 30 * 60 ))
  say "[$TAG] training $EP epoch(s) at --max-len $ML; hard stop $(date -d "@$STOP" '+%T %Z')"
  "$PY" "$DATA/train.py" train --tag "$TAG" --data "$DATA" --base "$(base)" --out "$OUT" --epochs "$EP" --max-len "$ML" \
    --stop-at "$STOP" ${QLORA:+--qlora} >> "$DATA/train.out" 2>&1 || { say "STOP: [$TAG] training failed - tail of train.out:"; tail -5 "$DATA/train.out"; exit 1; }
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
*) sed -n '2,6p' "$0" ;;
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
pipi "torch>=2.7" "transformers>=5.10.1,<6" "peft>=0.19.0" accelerate safetensors huggingface_hub hf_transfer \
  bitsandbytes sentencepiece || echo "ERROR: install failed"
"$PY" -c "import torch,transformers,peft;print('torch',torch.__version__,'cuda',torch.version.cuda,'gpu',torch.cuda.is_available(),'transformers',transformers.__version__,'peft',peft.__version__)"

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
