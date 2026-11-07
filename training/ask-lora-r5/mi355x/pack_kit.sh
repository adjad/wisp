#!/bin/bash
# The first upload to the MI355X node: one flat folder with what setup and base's answers need. Text only, no private
# exports. Runs on the Mac.
#
#   pack_kit.sh BUILD_DIR REUSE_FILE OUT.tgz
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$HERE/../.."
BUILD="${1:?BUILD_DIR}"; REUSE="${2:?finals.r4reuse.jsonl}"; DEST="${3:?OUT.tgz}"
W="$(mktemp -d "${TMPDIR:-/tmp}/r5kit.XXXXXX")" || exit 1; trap 'rm -rf "$W"' EXIT
K="$W/r5"; mkdir -p "$K"
cp "$HERE/run_r5.sh" "$T/ask-lora-r4/a6000/run_sft.sh" "$T/ask-lora-r3/brev/vm-setup.sh" \
   "$T/ask-lora-r5/gpu/gen_finals.py" "$T/ask-lora-r5/gpu/run_finals.sh" "$T/ask-lora-r5/gpu/choose_r5.py" \
   "$T/ask-lora-r5/gpu/_paths.py" "$T/ask-lora-r5/gpu/source_contract.py" "$T/ask-lora-r5/gpu/choose_traces.py" "$T/ask-lora-r4/distill/gen.py" "$T/ask-lora-r4/distill/choose.py" \
   "$T/ask-lora-r3/brev/render.py" "$K/"
cp "$BUILD/partial.jsonl" "$K/partial.jsonl"
cp "$REUSE" "$K/finals.r4reuse.jsonl"
tar -C "$W" -czf "$DEST" r5
shasum -a 256 "$DEST"; ls -lh "$DEST"
