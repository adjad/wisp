# Cloud output limit repair

- Outcome: remove Wisp's fixed 8,000-token ceiling from direct cloud-model
  answers, prevent duplicate provider reasoning representations from consuming
  the answer budget twice, and preserve useful partial text when a provider
  closes a long response early.
- Exact base SHA: `b36f4299ad2cf2955e99eea4ab1431dbee2f0633` (`origin/main`).
- Branch: `codex/cloud-output-limits`.
- Sole writer: Wisp Hub in the isolated `cloud-output-limits` worktree.
- Owned paths: `service/inference/omlx_client.py`, `service/main.py`,
  `service/errors.py`, `tests/test_inference_providers.py`,
  `tests/test_error_translation.py`, and this handoff.
- Validation plan: focused provider and error regressions; GitHub
  `python-regressions`; GitHub `Verified macOS artifact`; independent Release
  Auditor review; and synthetic security/external-provider Simulation QA for
  the exact candidate SHA.
- Boundaries: only direct remote text generation expands to the remaining
  configured context. Local inference and internal structured/tool calls keep
  their existing explicit token budgets. Remote output remains bounded by
  per-channel and total-wire byte ceilings; no credentials, live personal data,
  or provider calls are used in validation.
- Dependency: preserves the cloud settings and origin-bound credential model
  merged in PR #70.
