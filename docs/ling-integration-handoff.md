# Ling app and engine integration handoff

Outcome: standalone native Ling app and actual local OpenAI-compatible streaming endpoint, interoperable with Wisp provider settings. Full benchmark and M5/Metal research report accompanies delivery.
Base:6c7bae346e26b6a593ecd7f038bb3e40dddf0afa, isolated codex/ling-engine workspace.
Source: audited baseline 3b57cb8a9f45afa13246ff29c1775b3fdb2b4c35. Both optimization candidates were rejected; the larger-chunk candidate e83b58fac22e0334b283a7f1d1fa5022ee64d27e was invalid-pruned for an exact-output regression. Stock 2048-token prefill is the sole serving path.
Ownership before integration: primary sole writer tools/ling_engine/** and tests/test_ling_engine.py for extraction; transfer those paths plus tests/test_ling_engine*.py to Sol ling_engine_builder for API implementation after extraction. Luna ling_draft_feasibility owns apps/LingLocal/**. Primary owns docs/ling-* and docs/m5-* reports. Wisp Hub owns Wisp settings/backend in separate worktree; no overlap.
Validation: CPU request/API/SSE tests, synthetic raw-token parity, independent baseline/candidate recheck and direct-vs-oMLX pilot; native build and app lifecycle/UI; Wisp configured-provider synthetic integration. Heavy work serialized via explicit Hub leases. Independent release audit and exact-head CI required before release claims.
Dependencies: installed oMLX Python/MLX/Ling adapter and existing model; no downloads/installs/training required. Native SwiftUI uses system frameworks.
Boundaries: preserve Local and experiment artifacts; no weights/quant/tokenizer changes, no benchmark edits; no installed Wisp/oMLX replacement, no merge. Default Ling port8767; Wisp backend owns8765. Wisp settings takes origin with separate /v1 prefix. Engine must never claim tool capability until parsed structured calls and tool history are tested.

Evidence ownership: primary owns docs/ling-evidence/** for immutable copies of synthetic gate/API/comparison records. No evaluator logic changes.

CI manifest repair ownership: primary sole writer of the tests/test_ling_engine.py entry in scripts/run_simulation_qa.py ADDITIONAL_FULL_TESTS on this isolated branch. Purpose: include synthetic CPU/API tests in the mandatory offline full profile. Existing Release Auditor independently reviews classification; validate test_simulation_qa_runner.py and rerun full gate plus exact-head CI. Wisp Hub owns its provider-test entry in a separate branch.
