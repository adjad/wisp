#!/bin/zsh
# r5.1 after the step-50 pause: fetch the step-50 adapter from the box, merge it into the bf16 base on the Mac, quantize
# it with the same recipe as base and step 1800 (oQ4e, the shipped MTP head), then run the frozen gate with it as CAND.
# Training stays paused throughout; this script never resumes it.
#
#   BOX=following-indigo-goose zsh step50_chain.sh
setopt pipefail
BOX=${BOX:-following-indigo-goose}
W=$HOME/Desktop/r51-run
STEP=step-00050
ID=gemma-4-E4B-it-r51-step-00050-oQ4e-mtp
BF16=$HOME/Desktop/OMLX_Model_Files/_wisp_ask_lora/gemma-4-E4B-it-bf16-ee0ef602
QOUT=$HOME/Desktop/OMLX_Model_Files/_wisp_ask_lora/$ID
MTP=$HOME/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp
T=/Users/adijain/Desktop/MOE_Project/.claude/worktrees/ask-lora-brev-2caec5/training
PY=/Users/adijain/Desktop/MOE_Project/.venv/bin/python
LOCK=/private/tmp/claude-$(id -u)/wisp-heavy.lock
say() { echo "== $(date -u +%T) $*"; }
mkdir -p $W/ckpt || exit 2

say "fetch $STEP"
ssh -n $BOX "test -f ~/wisp/gemma/ask-lora-out/PAUSED.json && cat ~/wisp/gemma/ask-lora-out/PAUSED.json" || { say "box is not paused"; exit 2; }
scp -q -r $BOX:wisp/gemma/ask-lora-out/ckpt/$STEP $W/ckpt/ || exit 2
scp -q $BOX:wisp/gemma/ask-lora-out/train.log.jsonl $W/train.log.jsonl || exit 2
want=$(ssh -n $BOX "sha256sum ~/wisp/gemma/ask-lora-out/ckpt/$STEP/adapter_model.safetensors" | cut -d' ' -f1)
got=$(shasum -a 256 $W/ckpt/$STEP/adapter_model.safetensors | cut -d' ' -f1)
[ "$want" = "$got" ] || { say "adapter sha mismatch $want vs $got"; exit 2; }
say "adapter sha256 $got"

if [ ! -d $W/merged-$STEP ]; then
  say "merge"
  $PY $T/ask-lora-r3/brev/merge_mac.py --base $BF16 --adapter $W/ckpt/$STEP --out $W/merged-$STEP || exit 3
fi

if [ ! -d $QOUT ]; then
  say "quantize: waiting for the heavy lock"
  until mkdir "$LOCK" 2>/dev/null; do sleep 30; done
  echo "r5.1 step-50 quantize pid $$ $(date)" > "$LOCK/owner" || { rmdir "$LOCK"; exit 2; }
  R=/Applications/oMLX.app/Contents/Resources; P=$R/Python
  (cd $T/ask-lora-r3/brev && PYTHONHOME=$P/cpython-3.11 PYTHONPATH=$R:$P/framework-mlx-base/lib/python3.11/site-packages \
    $P/cpython-3.11/bin/python3 -s quantize.py --src $W/merged-$STEP --out $QOUT --mtp-from $MTP); rc=$?
  rm -f "$LOCK/owner"; rmdir "$LOCK" 2>/dev/null
  [ $rc -eq 0 ] || { say "quantize exit $rc"; exit 4; }
fi

say "gate (takes the heavy lock itself)"
RUN=$HOME/Desktop/r5-pilot/step50-gate-r51-$(date +%Y%m%d-%H%M%S)
RUN=$RUN CAND=$ID zsh $T/ask-lora-r51/eval/run_step50_eval.sh; rc=$?
say "gate exit $rc; run $RUN"
echo "$RUN" > $W/LAST_GATE_RUN
exit $rc
