# External local inference provider: implementation handoff

- Outcome: Connect one OpenAI-compatible app on a distinct numeric loopback
  HTTP port, discover its models, and bind it to Reasoning in Wisp Settings.
  Ling defaults to `127.0.0.1:8767`; Wisp backend remains on `8765` and
  managed oMLX on `8000`.
- Exact base: `6c7bae346e26b6a593ecd7f038bb3e40dddf0afa` (`origin/main`).
- Sole writer: Wisp Hub, in the `local-provider-settings` isolated worktree.
- Owned paths: `service/config/endpoints.py`, `service/config/__init__.py`,
  `service/main.py`, `app/Sources/WispApp/SettingsView.swift`,
  `tests/test_local_provider_settings.py`, and this handoff.
- Dependency: Ling app must return an exact model ID from `GET /v1/models`
  and valid non-streaming and SSE `POST /v1/chat/completions` responses.
  Tool use remains on managed local models until independently qualified.
- Security: anonymous HTTP is confined to a numeric `127.0.0.1` origin on
  a distinct nonprivileged port. Settings warns that peer identity is not
  verified. No credentials are stored or transmitted by this integration.
- Validation plan: synthetic endpoint/configuration and route tests, app
  build, exact-head GitHub CI, and independent release review. Avoid live
  inference during the Ling engine's GPU/MLX measurement lease.
- Release boundary: no merge or installed-app replacement without the
  applicable exact-head gates and task-specific authorization.
