# Ling comparison harness — Wisp 1.3.0 preparation

This is a standalone **model diagnostic**, not the production router or a release gate. It runs 80 routing prompts and 20 synthetic grounded-overview prompts against two explicitly selected models. It never invokes calendar, messages, email, native tools, outbound actions, server model management, or Wisp settings. Source facts are invented. See `CASES.md` for coverage and label conventions.

The default command only validates the corpus, without model imports, network, credentials, or downloads. Real inference requires the explicit `run` command. No real model results are bundled or claimed. An oracle `mock` arm is only a runner self-test and cannot be paired with real inference.

## What is compared

1. **Brev:** the pinned unquantized Ling base versus that same base with your completed saved LoRA adapter. This isolates the adapter effect using the same GPU, tokenizer, system messages, synthetic clock, greedy generation and limits. The user-provided, inspected `cloud_probe.py` loader handles Ling's Axolotl architecture; this harness only calls `tokenizer()` and `load_model(adapter)`, never its training/dataset/evaluation functions.
2. **Mac, later:** explicitly selected current oQ6e versus the exported fine-tuned model at the same quantization level, hardware, and server policy. The HTTP backend accepts only a numeric loopback endpoint. You select/load the model yourself. It never lists, unloads, reloads, configures or warms up a server. Do not run it against a busy production server without a separately coordinated measurement window. Model names and revisions declared in HTTP config are not proof of the loaded weights/template.

Brev versus Mac timing is not a fair speed comparison. Full precision versus quantized accuracy combines training and quantization effects; report those separately. Neither comparison demonstrates actual production source selection, date compilation, tool admission/execution or chat UI quality.

## First: offline checks

Requires Python 3.10+ on Linux/macOS; no extra dependencies for validation or mock runs.

```bash
cd "$HOME/ling-comparison-harness-20261006"
python runner.py validate
python -m unittest discover -s . -p test_harness.py -v
```

Unzip the provided archive in your home folder to obtain that directory. `BUNDLE.json` records SHA256 for each bundled source/fixture; verify it before use. You can recreate the source-only ZIP with `python bundle.py --output /tmp/ling-comparison-new.zip` (it refuses existing outputs). Do not overwrite an older harness used for recorded results: keep each evaluation version with its outputs.

## Brev: base then your completed adapter

Run these yourself in your Brev terminal after training and any other GPU job have ended. The VM continues charging while running. These commands load weights and perform inference; no additional training happens. Use your existing virtual environment and a **new** output directory. Inspect the supplied probe and its pinned identity before allowing it to execute. If `cloud_probe.py` and `pins.json` live in `wisp-ling-smoke` instead of `wisp-ling-pilot`, change `probe` below; the adapter path still points to your pilot adapter.

```bash
source "$HOME/wisp-ling-env/bin/activate"
cd "$HOME/ling-comparison-harness-20261006"
python - <<'PY'
import json
from pathlib import Path
probe = Path.home() / "wisp-ling-pilot"
assert (probe / "cloud_probe.py").is_file() and (probe / "pins.json").is_file()
adapter = Path.home() / "wisp-ling-pilot/artifacts/pilot-adapter"
assert (adapter / "adapter_model.safetensors").is_file()
base = {"backend": "hf_probe", "probe_dir": str(probe), "adapter": None}
tuned = {**base, "adapter": str(adapter)}
Path("base.json").write_text(json.dumps(base, indent=2))
Path("tuned.json").write_text(json.dumps(tuned, indent=2))
PY

set -o pipefail
python runner.py run --config base.json --output results/brev-v1 --arm base 2>&1 | tee base-run.log
```

After the base command finishes successfully, run the next arm. Each command owns one process and closes it on completion; arms never overlap within the same output root. An error stops the arm. Inspect the error/runtime receipt before continuing—do not substitute a failed base arm with a successful adapter arm and describe the pair as complete.

```bash
python runner.py run --config tuned.json --output results/brev-v1 --arm tuned 2>&1 | tee tuned-run.log
python runner.py report --output results/brev-v1 --left base --right tuned
```

For an initial small check, use `--lane routing --limit 6` on **both** arms with a separate output root such as `results/brev-small`; do not mix those with the full run. This is an inference check, not a quality-uplift claim. The per-request deadline defaults to 180 seconds and model-load deadline to 600 seconds. Set larger values identically for both arms if needed. These are bounded waits, not a training timeout.

## Mac: exported model comparison

This becomes usable only after full-precision merging/export and MLX compatibility have been independently verified, the exported model has been quantized, and both models are available to a coordinated isolated local endpoint. **A PEFT adapter alone is not an MLX model.** The harness does not merge/export or qualify that conversion.

Create two JSON configurations yourself, selecting actual available model IDs. Example shape (replace the model and identity values):

```json
{
  "backend": "http",
  "endpoint": "http://127.0.0.1:8000/v1",
  "model": "YOUR_EXACT_MODEL_ID",
  "identity": {
    "revision": "unknown",
    "checkpoint_sha256": "unknown",
    "quantization": "oQ6e",
    "tokenizer_revision": "unknown",
    "chat_template_sha256": "unknown"
  }
}
```

Never put credentials in JSON or URLs. If your isolated endpoint needs authentication, the optional `token_env` field names an environment variable; the harness reads its value only inside an explicit inference request and never records it. It refuses redirects/proxies and non-loopback URLs. It requires the response model ID to match the requested ID and plain text instead of tool calls. HTTP sends `chat_template_kwargs.enable_thinking=false`; template/no-thinking support remains a server qualification requirement, not a verified claim by this client. Unknown finish reasons or token-limit terminations get no exact-correctness credit.

Use `--config current-mac.json --arm current` followed by `--config tuned-mac.json --arm tuned`, sharing a new `--output results/mac-v1`, then `report --left current --right tuned`. Keep all other options identical. Do not combine these receipts with the Brev arms.

## Outputs and scoring

Each arm saves an immutable `manifest.json`, runtime/load receipts and append-only `raw.jsonl`. Runtime receipts are checksum-bound to each raw case and validated on resume/report. Startup failures without case journals are reported as partial evidence, with no quality credit. These retain raw answers, errors, request/corpus/system/schema/backend/adapter hashes, generation policy, finish reason, timings and available memory. HF reports actual package/GPU/tensor dtype identities, pinned base revision and template verification; it does not independently hash every cached base weight. HTTP revision/quantization/tokenizer binding is user-declared, and server GPU memory is unavailable. No credential values or arbitrary exception bodies are journaled.

`comparison.json` reports strict JSON/schema/semantic validity, kind/source/operation/argument/exclusion/unsupported-string matches, exact and order-normalized exact outcomes, paired wins/regressions, family results and failure denominators. Unsupported-presence match only compares whether a constraint list is empty, not whether its content is correct. Normalized exact only ignores order; it is **not** a semantic judge for paraphrased unsupported constraints. `arguments_match` includes source operations; `kind_match` is reported separately. Unexpected/excluded source **intents** are diagnostic errors, not claims that tools were executed. Unrecorded selected routes remain in the requested denominator; failures and truncated/incomplete answers earn no exact credit. A complete comparison requires all selected cases recorded in both arms.

Overview accuracy is **pending manual review**, never inferred from string similarity or routing JSON. The pair-bound `overview-review-template-<id>.json` includes both answers and explicit required facts, caveats, forbidden claims, attribution and readability criteria. Fill out reviewer identity/notes and judgments yourself; preserve the file with raw receipts. The report does not automatically aggregate human ratings or pretend an unreviewed overview passed. Source/data completion statements must be justified by the synthetic fixture. Share the comparison and raw receipts in this chat for interpretation.

Latency means HF generation (CUDA-synchronized, excluding tokenization/device-transfer) or HTTP whole request (including transport/server work), as explicitly different definitions. Startup/load time lives in runtime receipts. First request after each load is listed separately; p50/p95 use usable subsequent requests only, with sample count. These are warm-request diagnostics, **not** controlled cold-cache, TTFT, prefill or release performance benchmarks. Failed request wall times remain in raw receipts; success-only latency must never hide the failure counts. Memory is Torch's peak allocated/reserved GPU bytes where available, not whole-system RAM. There is one attempt per prompt; repeat stability and production recovery are unmeasured.

## Resume and failure handling

Use `--resume` only with the identical arm, cases, code, loader/adapter files, generation policy and deadlines. Runtime identity must remain the same. Completed error receipts are retained rather than silently retried. A truncated journal, corrupt/duplicate receipt or changed configuration is rejected. Before launching any arm, the locked output root is checked for uncertain work across **all** arm folders, including earlier base runs and remote-unknown failures. The measurement lock only coordinates arms using the same output directory; it does not protect other tools/jobs or separate output roots.

A timed-out/interrupted worker is terminated only within this harness's process scope. An HTTP timeout does not prove that the server stopped computing. A worker lifecycle marker is written before launch and removed only after confirmed own-worker cleanup. Remote-unknown, unclosed `pending.json`, or leftover `worker-active.json` receipts refuse resume: preserve them, verify device/server quiescence yourself, and start a new output root. Never launch a duplicate arm while unsure. Do not delete incomplete receipts to make results look complete. Keyboard cancellation preserves the pending request and interruption marker.

## Provenance and release boundaries

The public contracts are byte-identical copies from frozen router contract commit `c313a459f6ab259e55981ffcb1bbe3df11fe9307`:

- `router-system.txt`: SHA256 `800b235e8ab3ed7d76e74461cd20dbd0d58cb111e3d8f31b5adba27117b1b830`.
- `intent.schema.v1.json`: SHA256 `85186431c4d3e8f1c38ca7d2aa899714da1dd5b275de97998432a6de76705678`.

The harness adds explicit schema/clock context to both arms; it is not the frozen production-path runner. Its overview system is recorded through the runner source hash. Cases were freshly authored from stated user needs and these public contracts without reading/copying training/dev/old test/private logs. The visible set is not a blind final test, other-user telemetry, or a promise of statistical generalization. Any case used for tuning becomes development data; use separately authored future challenge cases for subsequent confirmation. Strict syntax/ordering and freeform gold wording can underestimate semantically equivalent output; preserve both exact and diagnostic metrics, with manual review of disputed cases instead of editing gold after viewing model results.

This preparation neither changes Wisp 1.2.0 nor activates/merges Wisp 1.3.0. Final 1.2.0 reconciliation, same-head CI/review, serving export qualification, production routing integration, visual overview QA and heldout model-quality evidence are separate requirements.
