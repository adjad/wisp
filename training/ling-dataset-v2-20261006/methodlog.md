# Method log

- User objective: more refined Wisp routing demonstrations after measured development strict matches of 7/140 for base Ling and 44/140 for the previous adapter; keep unrelated assistant behavior out.
- Dataset: fresh synthetic V2 with 4,000 training examples (3,400 routing / 600 grounded), 400 development examples (340 routing / 60 grounded), and a balanced 2,000-row training subset. The existing final test was never read or included.
- Intended method: supervised LoRA using the demonstrated Ling/Axolotl recipe. This task prepares data only; no optimizer, rank, learning-rate experiment, inference or training was launched.
- Proposed controlled comparison: a fresh adapter from the same original Ling base, using the previous rank 16, module targets and learning rate 1e-5. Try the refined 2,000-row subset before scaling. These are experiment proposals, not proven optimal parameters.
- Frozen router contract: `c313a459f6ab259e55981ffcb1bbe3df11fe9307`. Production implementation remains unchanged. Dataset base: `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`.
- Score complete intents, exact arguments, source exclusions and unsupported-capability honesty. Score summaries for grounding, attribution, coverage and readability. Do not choose a checkpoint solely from decreasing loss.
- Authors see aggregate failure classes, never original evaluation prompts or held-out payloads. The sealed final remains the generalization check after choices are frozen.
- Initial independent review required natural source labels in 98 mixed-source references and a correction to an overly literal attribution validator. Original evidence is preserved. The same owners make the bounded repairs; a fresh frozen independent review follows.
