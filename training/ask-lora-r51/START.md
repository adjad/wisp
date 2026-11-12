# r5.1 starting point (decided 2026-11-11 by the user: "start from step 1800 for this")

r5.1 trains from the r5 step-1800 LoRA adapter, not from base. This records the start point only; it is not permission
to train. Training needs the user's separate instruction, and it pauses at step 50 for the frozen gate (eval/CRITERIA.md).

Source on the Mac (copied off the GPU box and checked by SHA-256 before the box was deleted):
`~/Desktop/r5-pilot/box-final/ask-lora-out/step-01800/`

```
b8750d1b21ddd321f0817e626acedf0f4d33fb22a17806ce6e1199dfdb7dc6c0  adapter_model.safetensors
0d43b5410b0c7a206affef65445e080016ecf4c0dfc8d7fefaec6f49e085b414  adapter_config.json
3d96b060589a72db41b2899976c94d4f649f2b7cb4ac5c86fe4c78985d80136a  trainer_state.pt
```

- Load the adapter weights from `adapter_model.safetensors` with the same `adapter_config.json` (rank, alpha, target modules).
- r5.1 is a new run on the r5.1 data, so it starts a fresh optimizer and learning-rate schedule. `trainer_state.pt` is
  r5's optimizer state at step 1800 of 2126, partway through r5's schedule; it is kept for reference only and not resumed.
- Check the three hashes above before the first step.
- The gate compares the step-50 checkpoint against base (the bar) and step 1800 (reference) on identical cases and seeds.
