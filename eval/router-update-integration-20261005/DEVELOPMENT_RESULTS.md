# Development replay — integration evidence only

The harness executes actual `service.main.agent` orchestration with synthetic source fixtures and scripted oracle model replies. It measures the implementation contract, not Ling prediction quality, latency or real source behavior. The same frozen 80 development cases cover ten families; the eight variants in each family are correlated.

| Check | Original baseline | Before compiler repair | After compiler repair |
| --- | ---: | ---: | ---: |
| Exact calls/arguments | 75/80 | 77/80 | 80/80 |
| Sources | 75/80 | 77/80 | 80/80 |
| First call | 43/48 | 45/48 | 48/48 |
| Original end-to-end | 67/80 | 69/80 | 72/80 |

The repaired message prompts include “Summarize recent text messages,” “What did people text me?” and “Show my recent text summary.” Their earlier outbound workflow preemption is preserved in the before-repair raw artifact. They now reach the requested message read without creating a delivery workflow.

After repair, all 80 cases pass effects, excluded-source, runtime-constraint, completion and duplicate checks; all 16 applicable failure-honesty checks pass. Eight cases still fail the original end-to-end/literal measure: their expected synthetic mail source is unavailable and the answer correctly reports the error, while the original annotation also expects a success-only literal. These eight are annotation limitations, disclosed separately without changing gold, raw outputs or the original scores. The supplemental guarded end-to-end is likewise 72/80. Answer-fact and query-guard measures have zero applicable cases here, so this replay establishes neither property. Dedicated synthetic helper tests cover strict message query matches/misses and scoped filters.

Post-repair code revision: `97d7fdf54dabb150e167a9f7250ed8fdf4a3b249`. Focused combined checks: 240 passed, one original-control-only skip. Both raw replays are compressed unchanged, with original raw hashes and manifest hashes beside them. The 320-case sealed held-out corpus remains unopened for comparison/tuning. Final full-gate receipts belong to the subsequent frozen integration head. No 95% actual-model result or fine-tuning uplift is claimed.

## After independent audit repairs

The combined repair at `d02c03d` preserves the same development result: 80/80 exact calls and sources, 48/48 first calls, 72/80 original and guarded end-to-end, and 16/16 applicable failure-honesty checks. It passes 401 focused integration tests with one original-control-only skip. `development-after-audit-repairs/` preserves that raw replay and manifest. These are scripted synthetic checks, not model accuracy.

The first frozen candidate `3f86187` is retained as a failed candidate in `first-candidate-gates/`: full local regression175/179, independent audit BLOCK, both required CI checks failed. Repairs require requested dates/reminder scope/duration and complete unquoted query phrases, preserve the calendar-only shortcut, explicitly classify reviewed synthetic tests, and make evaluation timezone behavior reproducible on UTC and Pacific hosts. The original full native-peer failure was a nested-sandbox launch error; its same-SHA compatible launch passed9 synthetic cases without code changes. None of these followups rewrites the original failed gate. The next frozen head still needs all final gates.
