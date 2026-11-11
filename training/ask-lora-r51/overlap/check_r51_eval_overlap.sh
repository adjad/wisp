#!/bin/sh
# usage: check_r51_eval_overlap.sh BUILD_DIR EVAL_DIR OUT_JSON   (needs the sandbox off to read ~/.moe)
cd "$(dirname "$0")/../../.." || exit 1
exec /Users/adijain/Desktop/MOE_Project/.venv/bin/python training/ask-lora-r51/overlap/check_r51_eval_overlap.py --build "$1" --eval-dir "$2" --out "$3"
