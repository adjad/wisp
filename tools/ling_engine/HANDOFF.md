# Browser boundary repair handoff

Outcome: reject foreign or malformed Host/Origin headers and non-JSON chat POSTs before tokenization or inference, while preserving native loopback and SSE requests.
Base: ebc1a120d9934da91d229069d5576e3b446fadad on codex/ling-engine in this isolated integration worktree.
Owned paths: tools/ling_engine/** and tests/test_ling_engine.py. Sole writer: ling_engine_builder for this bounded repair; root owns the resulting commit and integration.
Validation: focused fake-engine HTTP tests plus the Ling CPU test module; no model load, GPU inference, full-suite rerun, or installed app replacement.
Dependencies: existing Python standard-library HTTP server and installed Ling runtime at deployment; no new packages.
