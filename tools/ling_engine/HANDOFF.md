# Browser boundary repair handoff

Outcome: reject foreign or malformed Host/Origin headers and non-JSON chat POSTs before tokenization or inference, while preserving native loopback and SSE requests.
Base: ebc1a120d9934da91d229069d5576e3b446fadad on codex/ling-engine in this isolated integration worktree.
Owned paths: tools/ling_engine/** and tests/test_ling_engine.py. Sole writer: ling_engine_builder for this bounded repair; root owns the resulting commit and integration.
Validation: focused fake-engine HTTP tests plus the Ling CPU test module; no model load, GPU inference, full-suite rerun, or installed app replacement.
Dependencies: existing Python standard-library HTTP server and installed Ling runtime at deployment; no new packages.

## CI sandbox test harness repair

Outcome: exercise the real Ling HTTP handler through a socket pair without binding a listener, preserving request parsing, browser boundary, JSON, SSE, and authentication assertions under the no-network CI sandbox.
Base: 165ece11edd5b4583eac3e1a01e5e53237c30d0a on codex/ling-engine.
Owned paths: tests/test_ling_engine.py and this handoff. Sole writer: ling_engine_builder; root owns commit and final integration.
Validation: target Ling module under the default restricted sandbox (no escalation), followed by exact-head full regression gate after root commits. No model load or GPU inference.
Dependencies: Python standard-library socketpair, http.client, and the existing handler factory; no production behavior change or new packages.

Integration ownership returned to root after code freeze. Root preserves the prior three-second socket timeout on both socket-pair ends so a regression fails instead of waiting indefinitely.
