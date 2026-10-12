# Ling dataset V2

This is a synthetic dataset for Wisp 1.3.0 routing and concise summaries of supplied source fixtures. It changes no production code. The upload ZIP is inert: it does not train, contact tools, or load model weights.

## Use on Brev

1. Upload `ling-train-dev-v2-20261006.zip`. Verify its SHA256 against `upload-zip.json`, then extract into a NEW directory. Preserve the previous dataset and trained adapter.
2. In the existing Ling training environment, run `python preflight.py --local-files-only`. This uses the pinned tokenizer already cached on Brev. If the pinned tokenizer is absent, omit `--local-files-only` to download its files; no model weights or training are loaded by this script.
3. Run `python validate.py` for the portable CPU dataset checks. Read any failures; never bypass a truncation/template or semantic check.
4. For the first controlled experiment, select `pilot-train.jsonl` (2,000 rows). For the larger experiment, select `train.jsonl` (4,000 rows). These are alternatives, not files to concatenate: the pilot is an exact subset of full training data.
5. `dev.jsonl` is development-only. Neither dev rows nor provenance/oracle sidecars are training inputs. A separate original final test remains sealed and is not in this upload.
6. Use the previously verified final-response-only Axolotl preparation pipeline. Run preprocessing and inspect actual prepared labels BEFORE training; every earlier assistant turn must be masked. The JSON training flags alone are not proof that a trainer respects this rule.

Preserve each row's system message. Routing rows target JSON; grounded rows target prose. Do not replace both with one global role prompt. Use the same pinned Ling revision/chat template and thinking-disabled generation settings as the original pilot. No V2 model-quality improvement has been measured.

## Reproduce locally

From this directory in the full repository checkout, use standard-library Python:

```
python3 generate.py
python3 validate.py
python3 package.py
```

The generator executes the three shard generators only in temporary output directories. `--output-dir /private/tmp/ling-v2-reproduced` rebuilds payloads without changing author shards. No model/GPU/cloud/native operations run. Parent integration, authors and review have distinct ownership.

Read `DATA_CARD.md` for distribution, limitations and validation scope. `validation.json` is static data QA; it does not establish tokenizer lengths, trainer masking, routing success, Mac export compatibility, or production readiness.
