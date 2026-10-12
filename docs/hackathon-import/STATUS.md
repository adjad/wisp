# Hackathon integration — work in progress

The repository's `main` starts with one parentless snapshot commit dated October 10, 2026 in America/Los_Angeles. It contains the three frozen checkpoints below. Older source history is not imported into this starting commit. This is work in progress and is not release-qualified.

| Input | Frozen source checkpoint | Current disposition |
| --- | --- | --- |
| Catch | `960df0336bcdda0a38763a717f1c4308e676e23e` | Imported without product-file conflicts. Builder reports 398 focused checks and 28 native-contract checks passed; these source-checkpoint results do not qualify the combined snapshot. |
| iPhone foundation | `45e042563fdd79821ceaa404be115e9620947b70` | Imported without conflicts; changes are confined to `ios/`. Builder reports core tests and simulator compilation passed. No device installation, simulator app launch or model inference was performed. |
| Mac workflow20 | `c220e4a4c6173fe37e3fa62c9945ca7b76b563f1` | Imported without conflicts: 27 changed paths from the common Wisp base. Builder reports 454 focused checks passed; full-suite, CI, model and independent combined-candidate qualification remain pending. See [the checkpoint status](../workflow20/STATUS.md) for the remaining gaps across all twenty families. |

The iPhone checkpoint is foundation plumbing, not completion of the twenty workflows: its contract runner recorded all 597 observations as unsupported, with zero effects and no inference. Those observations are not successful workflow outcomes.

Import verification covered Git ancestry, exact source blobs and modes, preservation of the initial hackathon README and license, clean staged merges and whitespace checks. No product tests, model inference or native execution were run by the importer. Source-checkpoint results do not qualify the combined snapshot. Destination CI and any required independent review and specialist QA must be evaluated against the current snapshot commit before a ready-to-ship claim.

The original Wisp repository's `main` remains unchanged. Paused PR174/full-model-led routing work and private PR160 proposals were not imported into this feature branch.
