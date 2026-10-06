#!/usr/bin/env bash
# Run only inside the qualified private Brev VM, from this bundle's directory.
# Admission must already include independent VM termination, not just timeout.
set -euo pipefail
cd "$(dirname "$0")"
python admission.py launch-admission.json
mkdir -p artifacts
export HF_HUB_DISABLE_TELEMETRY=1
export AXOLOTL_DO_NOT_TRACK=1
export WANDB_MODE=disabled
export TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
phase="${1:-prepare}"
if [[ "$phase" == "prepare" ]]; then
  python cloud_probe.py runtime > artifacts/runtime.log 2>&1
  python cloud_probe.py tokens > artifacts/tokens.log 2>&1
  axolotl preprocess smoke.yml --debug > artifacts/preprocess.log 2>&1
  python bundle_identity.py > artifacts/prepared-input-identity.json
  printf '%s\n' 'Preparation complete. Parent must audit label spans before the train phase.'
  exit 0
fi
if [[ "$phase" != "train" ]]; then
  printf '%s\n' 'Expected prepare or train' >&2
  exit 2
fi
# The parent audits actual label spans and records the report's SHA before release.
python bundle_identity.py --check > artifacts/train-input-identity.json
python cloud_probe.py base > artifacts/base.log 2>&1
python admission.py launch-admission.json
python bundle_identity.py --check > artifacts/train-input-identity.json
# Process timeout is secondary protection; it does not terminate billable VM time.
timeout --signal=TERM --kill-after=30s 1800s axolotl train smoke.yml > artifacts/train.log 2>&1
python cloud_probe.py inspect-adapter > artifacts/adapter-check.log 2>&1
# Distinct process: training exited before this invocation.
python cloud_probe.py reload > artifacts/reload.log 2>&1
python -m pip freeze > artifacts/packages.txt
printf '%s\n' 'Cloud smoke complete. Export, private backup verification, VM teardown, and isolated Mac-serving reload remain required. Pilot is held.'
