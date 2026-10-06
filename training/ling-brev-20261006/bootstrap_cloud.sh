#!/usr/bin/env bash
# Cloud only. Invoke only after recorded billing/admission and GPU environment review.
set -euo pipefail
cd "$(dirname "$0")"
python admission.py launch-admission.json
test "$(uname -s)" = Linux
python - <<'PY'
import sys,torch
assert sys.version_info >= (3,12)
assert torch.cuda.is_available() and torch.cuda.device_count()==1
assert 'A6000' in torch.cuda.get_device_name(0)
assert torch.cuda.is_bf16_supported()
print('Existing CUDA/Torch:',torch.__version__,torch.version.cuda,torch.cuda.get_arch_list())
PY
# Official source only. No blind Triton downgrade from the obsolete FLA0.4.1 note.
python -m pip install --no-build-isolation 'axolotl @ git+https://github.com/axolotl-ai-cloud/axolotl.git@4608acfd55d5cb202148cba38566480f1540e91e'
python cloud_probe.py runtime
