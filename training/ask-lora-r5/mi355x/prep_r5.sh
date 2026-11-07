#!/bin/bash
# Build, seal, check and report everything that needs no GPU, into one folder. No model is loaded and nothing here trains.
# The result is status incomplete_no_base_answers until base's answers exist: pack_r4.py refuses it, and it isn't the dataset.
#
#   prep_r5.sh OUT_DIR PRIVATE_DIR AUDIT_DIR [--keep-own-wording]
#
# The served-prompt check needs oMLX's Python and a Metal device: see training/ask-lora-r3/brev/test_serving_parity.py.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)" || exit 1
T="$(cd "$HERE/../.." && pwd)" || exit 1
D="${1:?OUT_DIR}"; PRIVATE="${2:?PRIVATE_DIR}"; AUDIT="${3:?AUDIT_DIR}"; KEEP="${4:-}"
PY="${PY:-$HOME/Desktop/MOE_Project/.venv/bin/python}"
MODEL="${MODEL:-$HOME/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp}"
PIN="${PIN:-$T/ask-lora-r5/data/pin-7e442f2d}"
ZIP="${ZIP:-$T/ask-lora-r4/a6000/out/ask-lora-data.zip}"
[ -e "$D" ] && { echo "FAIL: $D exists"; exit 1; }
mkdir -p "$D/build" "$D/finals" "$D/report" || exit 1
die() { echo "FAIL: $1"; exit 1; }

cd "$T/ask-lora-r2/builder" || exit 1
ASK_LORA_ROUND=4 ASK_LORA_R5=1 ASK_LORA_OUT="$D/build" WISP_REPO="$PIN" "$PY" build.py > "$D/build.log" 2>&1 || die "build: see $D/build.log"
sed -n 4,5p "$D/build.log" | cut -c1-200
cp -R "$D/build" "$D/sealed" || exit 1

cd "$HERE" || exit 1
# shellcheck disable=SC2086
env -u WISP_REPO "$PY" seal_r5.py --build "$D/sealed" --private-dir "$PRIVATE" --audit-dir "$AUDIT" $KEEP > "$D/seal.log" 2>&1 || die "seal: see $D/seal.log"
"$PY" verify_contract.py --build "$D/sealed" --out "$D/sealed/contract_verification.json" || die "contract verification"

cd "$T/ask-lora-r5/gpu" || exit 1
"$PY" reuse_r4.py --zip "$ZIP" --partial "$D/sealed/partial.jsonl" --out "$D/finals/finals.r4reuse.jsonl" > "$D/reuse.log" 2>&1 || die "reuse: see $D/reuse.log"
"$PY" choose_r5.py --partial "$D/sealed/partial.jsonl" --raw "$D/finals/finals.r4reuse.jsonl" --out-chosen "$D/finals/chosen.jsonl" --out-stats "$D/finals/stats.json" \
    --out-nopass-ids "$D/finals/nopass.ids" --out-drop-ids "$D/finals/drop.ids" --out-rejections "$D/finals/rejections.jsonl" > "$D/choose.log" 2>&1 || die "choose: see $D/choose.log"

cd "$HERE" || exit 1
env -u WISP_REPO "$PY" finish_r5.py --build "$D/sealed" --chosen "$D/finals/chosen.jsonl" --drop "$D/finals/drop.ids" --choose-stats "$D/finals/stats.json" \
    --rejections "$D/finals/rejections.jsonl" --out "$D/prep-INCOMPLETE" --allow-incomplete 2>&1 | tail -1 | cut -c1-700
cp "$D/sealed/overlap_manifest.json" "$D/sealed/contract_verification.json" "$D/prep-INCOMPLETE/" || exit 1

cd "$T/ask-lora-r2/builder" || exit 1
ASK_LORA_ROUND=4 ASK_LORA_R5=1 WISP_REPO="$PIN" "$PY" validate.py "$D/prep-INCOMPLETE/all.jsonl" "$D/prep-INCOMPLETE/train.jsonl" "$D/prep-INCOMPLETE/valid.jsonl" > "$D/prep-INCOMPLETE/validate.log" 2>&1
echo "validate exit $? (a prep build isn't expected to reach 0 before base's answers exist; read the log)"
grep -v "Deprecat\|noqa\|WARN" "$D/prep-INCOMPLETE/validate.log" | cut -c1-240

cd "$HERE" || exit 1
"$PY" report_r5.py --build "$D/prep-INCOMPLETE" --model "$MODEL" --out "$D/report" --r4-zip "$ZIP" > "$D/report/report.log" 2>&1 || die "report: see $D/report/report.log"
bash "$HERE/pack_kit.sh" "$D/sealed" "$D/finals/finals.r4reuse.jsonl" "$D/r5-kit.tgz" | tail -1 | cut -c1-120
echo done
