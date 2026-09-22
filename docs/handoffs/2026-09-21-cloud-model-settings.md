# Cloud model settings and compact menu

- Outcome: Add a native Models settings interface for origin-bound cloud LLM connections and remove the first five workflow shortcuts from the Wisp menu-bar menu.
- Base: `ea56992f8590379689c1ef2825f558678bbb5492`
- Branch: `codex/cloud-model-settings`
- Sole writer: this Wisp Hub implementation worktree.
- Owned paths: `app/Sources/WispApp/AppDelegate.swift`, `app/Sources/WispApp/SettingsView.swift`, `service/config/__init__.py`, `service/config/endpoints.py`, `service/main.py`, `tests/test_cloud_provider_settings.py`, and this handoff.
- Dependencies: Existing OpenRouter/OpenAI-compatible provider transport, macOS Keychain, and the local Wisp backend.
- Validation plan: GitHub `python-regressions`, `Verified macOS artifact`, independent Release Auditor, and security/native Simulation QA because this changes credential and external-network configuration surfaces.
- Safety boundaries: API keys remain in Keychain and never enter configuration, backend request bodies, logs, or debug exports. Remote endpoints require HTTPS and an exact inventory match before persistence. Fast/router, summaries, retrieval, and tools remain local.
