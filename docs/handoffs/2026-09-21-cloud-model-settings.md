# Cloud model settings and compact menu

- Outcome: Add a native Models settings interface for origin-bound cloud LLM connections and remove the first five workflow shortcuts from the Wisp menu-bar menu.
- Base: `ea56992f8590379689c1ef2825f558678bbb5492`
- Branch: `codex/cloud-model-settings`
- Sole writer: this Wisp Hub implementation worktree.
- Owned paths: `app/Sources/WispApp/AppDelegate.swift`, `app/Sources/WispApp/SettingsView.swift`, `service/config/__init__.py`, `service/config/endpoints.py`, `service/main.py`, `scripts/run_simulation_qa.py`, `tests/test_cloud_provider_settings.py`, and this handoff.
- Dependencies: Existing OpenRouter/OpenAI-compatible provider transport, macOS Keychain, and the local Wisp backend.
- Validation plan: GitHub `python-regressions`, `Verified macOS artifact`, independent Release Auditor, and security/native Simulation QA because this changes credential and external-network configuration surfaces.
- Safety boundaries: API keys remain in Keychain and never enter configuration, backend request bodies, logs, or debug exports. Remote endpoints require HTTPS and an exact inventory match before persistence. Fast/router, summaries, retrieval, and tools remain local.
- Audit repairs: local rollback resolves from the local roster; versioned Keychain credentials are staged until the full requested backend state is confirmed and old credentials are retired only afterward; ambiguous responses and cleanup failures persist the complete nonsecret requested state so a later refresh adopts or removes the staged item; cloud origins reject userinfo, paths, queries, and fragments; disconnect switches roles local before credential cleanup; cloud IDs are not offered by local pickers; rolling conversation summaries use the local fast client only outside test mode and prepare it only when folding is required; Settings retains a Research Library entry point.
