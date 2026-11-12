# r5.1 step-50 gate, 2026-11-12: BLOCK

Training stopped at the step-50 pause and was not resumed. The user's rule was "Continue if gate passes"; BLOCK stops.

**Run.** Started from r5 step 1800 with weights only (`--init-adapter`, fresh AdamW and cosine). Settings:
- LR 1e-4, 1 epoch (1,226 steps), warmup 37.
- The new safety_contrast_r51 family was repeated 2× (pack v2-up2, 8,042 train rows).
- Box: A6000 `following-indigo-goose`, 62 s/step.
- Paused at step 50 on 2026-11-12 11:07 UTC.

The step-50 adapter was fetched, merged into the bf16 base on the Mac and quantized with the same recipe as base and step 1800. Hashes are in `RUN.txt`.

**Gate** (criteria v2, frozen cases, same clock for all three models; `GATE.md`, `gate.json`):

| | base | step 1800 | step 50 |
|---|---:|---:|---:|
| Safety slice, 200 trials: forbidden calls | 16 | 34 | 32 |
| unsafe proposals | 8 | 23 | 21 |
| forbidden effects | 4 | 7 | 8 |
| passed | 80 | 102 | 94 |
| Authorized writes done (of 25) | 11 | 15 | 17 |
| Grounding passed (of 15) | 7 | 11 | 12 |

Capability holds and improves a little. Safety stays near step 1800's and far from base's on every safety rule, so the verdict is BLOCK. Two cases were unsafe in at least 3 of 5 samples where base never was.

**Dev loss** (train.log.jsonl):
- Overall 0.3295 at step 0 (= step 1800), 0.3432 at step 50.
- Text 0.311 → 0.320.
- Vision 0.321 → 0.352.
- Audio 0.384 → 0.385.

Re-warming the converged adapter to 1e-4 moved it away from its fit faster than 42 new-family rows (in 35 groups) changed its safety behaviour.

**Scoring note.** The gate script's first scoring call failed. zsh doesn't word-split `${CAND:+--cand "$CAND"}`, so argparse got one argument, "--cand <id>", and exited 2, the same code as BLOCK. Nothing had been scored. That line was fixed (`${CAND:+--cand} ${CAND:+"$CAND"}`), and the already-finished runs were scored again with the same scorer and criteria. `results.sha256` pins the run outputs that were scored.
