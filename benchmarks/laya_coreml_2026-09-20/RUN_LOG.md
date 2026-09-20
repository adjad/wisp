# Reproduction log

Working directory for every command: the repository root, written as `<repo>`
in committed artifacts.

The active benchmark ran for well under the four-hour ceiling. Model downloads
were serialized and the full-repository byte upper bound was 2,207,967,339
bytes. `model_cache/` and `.venv/` are intentionally ignored.

## Environment

```sh
uv venv benchmarks/laya_coreml_2026-09-20/.venv --python /opt/homebrew/bin/python3.11
uv pip install --python benchmarks/laya_coreml_2026-09-20/.venv/bin/python -r requirements-runtime.txt
uv pip install --python benchmarks/laya_coreml_2026-09-20/.venv/bin/python -r benchmarks/laya_coreml_2026-09-20/requirements-benchmark.txt
```

## Downloads

Each block was run separately with `HF_HUB_DOWNLOAD_TIMEOUT=300` and
`HF_HOME="$PWD/benchmarks/laya_coreml_2026-09-20/model_cache"`.

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    'aac6fef/laya-multilingual-coreml-ane',
    revision='39d6a9b3d0f67f06da74fbade6121ea134cbdb21',
    allow_patterns=['coreml_config.json','rl_agent_config.json','encoder/config.json','tokenizer/*','model.mlpackage/**','host_weights.safetensors'],
)
PY
```

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    'aac6fef/laya-multilingual-coreml',
    revision='8139e9089273319512c730218903784074133187',
    allow_patterns=['coreml_config.json','rl_agent_config.json','encoder/config.json','tokenizer/*','model.mlpackage/**'],
)
PY
```

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    'aac6fef/laya-typed-decisions-coreml',
    revision='28d24fa8d67a3264556b23391ec6c3fd98573056',
    allow_patterns=['coreml_config.json','rl_agent_config.json','encoder/config.json','tokenizer/*','model.mlpackage/**'],
)
PY
```

## Baseline and native models

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python benchmarks/laya_coreml_2026-09-20/benchmark.py baseline --warm-repeats 25 --out benchmarks/laya_coreml_2026-09-20/raw/wisp_baseline.json
```

The following environment was set for every native model command:

```sh
HF_HOME="$PWD/benchmarks/laya_coreml_2026-09-20/model_cache"
LAYA_COREML_CACHE="$PWD/benchmarks/laya_coreml_2026-09-20/model_cache/runtime"
HF_HUB_OFFLINE=1
```

The commands were run one at a time:

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python benchmarks/laya_coreml_2026-09-20/benchmark.py model --model-name laya-multilingual-coreml-ane --local-dir benchmarks/laya_coreml_2026-09-20/model_cache/hub/models--aac6fef--laya-multilingual-coreml-ane/snapshots/39d6a9b3d0f67f06da74fbade6121ea134cbdb21 --warm-repeats 25 --out benchmarks/laya_coreml_2026-09-20/raw/laya_multilingual_ane.json
```

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python benchmarks/laya_coreml_2026-09-20/benchmark.py model --model-name laya-multilingual-coreml --local-dir benchmarks/laya_coreml_2026-09-20/model_cache/hub/models--aac6fef--laya-multilingual-coreml/snapshots/8139e9089273319512c730218903784074133187 --warm-repeats 25 --out benchmarks/laya_coreml_2026-09-20/raw/laya_multilingual.json
```

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python benchmarks/laya_coreml_2026-09-20/benchmark.py model --model-name laya-typed-decisions-coreml --local-dir benchmarks/laya_coreml_2026-09-20/model_cache/hub/models--aac6fef--laya-typed-decisions-coreml/snapshots/28d24fa8d67a3264556b23391ec6c3fd98573056 --warm-repeats 25 --out benchmarks/laya_coreml_2026-09-20/raw/laya_typed_decisions.json
```

## Aggregation and validation

```sh
benchmarks/laya_coreml_2026-09-20/.venv/bin/python benchmarks/laya_coreml_2026-09-20/benchmark.py summarize benchmarks/laya_coreml_2026-09-20/raw/wisp_baseline.json benchmarks/laya_coreml_2026-09-20/raw/laya_multilingual_ane.json benchmarks/laya_coreml_2026-09-20/raw/laya_multilingual.json benchmarks/laya_coreml_2026-09-20/raw/laya_typed_decisions.json --out benchmarks/laya_coreml_2026-09-20/summary.json
benchmarks/laya_coreml_2026-09-20/.venv/bin/python -m pytest -q benchmarks/laya_coreml_2026-09-20/test_benchmark.py
benchmarks/laya_coreml_2026-09-20/.venv/bin/python -m pytest -q tests/test_router_scoping.py tests/test_routing_contract_regressions.py
git diff --check
```

## Diagnostic failures

1. The first ANE load failed before inference with `PermissionError: [Errno 1]
   Operation not permitted: '<home>/.cache/laya-coreml'`. Setting
   `LAYA_COREML_CACHE` to the ignored benchmark cache fixed it.
2. The next sandboxed ANE attempt loaded but Core ML reported `Error compiling
   model: Failed to create a working directory appropriate for URL:
   file:///var/folders/.../T/`. Native execution approval fixed it; the pinned
   command then completed.
3. The successful ANE run emitted NumPy divide/overflow/invalid matrix-multiply
   warnings from the runtime's host action head. Predictions and probabilities
   used for scoring were finite; all public action probabilities were exactly
   1.0 and were treated as non-informative.

Structured versions of these diagnostics are in `raw/failures.json`.
