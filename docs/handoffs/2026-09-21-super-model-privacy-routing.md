# Cloud-first Super Model privacy routing

- Outcome: reintroduce Super Model as an optional cloud-first policy without
  quitting applications, closing windows, changing VRAM limits, or loading a
  larger local model.
- Exact base SHA: `b36f4299ad2cf2955e99eea4ab1431dbee2f0633` (`origin/main`).
- Branch: `codex/super-model-privacy-routing`.
- Sole writer: Wisp Hub in the isolated `super-model-privacy-routing` worktree.
- Owned paths: `service/config/__init__.py`, `service/config/endpoints.py`,
  `service/inference/super_model.py`, `service/main.py`,
  `app/Sources/WispApp/SettingsView.swift`, focused cloud/Super Model tests,
  and this handoff.
- Behavior: the local 1,024-token `aac6fef/laya-multilingual-coreml` CPU+GPU
  model scores private-content, computer-access, and conversation-context risk.
  The configured cloud model is used only when all three are at or below 5%.
  Tool use, private-data reads, explicit local-only requests, obvious secrets,
  over-capacity prompts, and every unavailable/timeout/malformed Laya outcome
  fail local before any provider client is created. Cloud-routed Super Model
  requests receive only the current prompt, not Wisp memory facts or stored
  conversation history. Enabling the feature starts the public model download
  and warmup; requests stay local until it is ready.
- Validation plan: focused Python policy/config tests; GitHub
  `python-regressions`; GitHub `Verified macOS artifact`; independent Release
  Auditor review; and synthetic security/privacy Simulation QA on the exact
  candidate SHA.
- Dependency/conflict: based on merged PR #70. PR #71 independently edits
  `service/main.py`; reconcile after #71 lands, then rerun every exact-SHA gate.
- Runtime dependency: pinned `laya-coreml==0.1.0`; regenerate and review the
  hash-locked packaged runtime before this candidate is committed.
