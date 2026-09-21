# Unread message and web synthesis candidate

- Outcome: Restrict broad message summaries to unread messages from the previous three days; improve linked news presentation; route web and stock evidence through Ling synthesis; repair local-only peer-attribution failure without weakening remote endpoint checks.
- Exact base SHA: `ea56992`
- Sole writer: Wisp Hub candidate `codex/unread-web-synthesis`
- Owned paths: `service/tools/imessage_tools.py`, `service/assistant/brief.py`, `service/tools/registry.py`, `service/tools/web_tools.py`, `service/agent/loop.py`, `service/main.py`, `service/inference/attributed_transport.py`, `tests/test_message_digest.py`, `tests/test_web_response_followup.py`, and the reverted protected-gate inventory in `tests/test_runtime_peer.py`.
- Validation plan: focused synthetic regressions for unread filtering, explicit conversation access, linked news synthesis, stock/web synthesis, and local-vs-remote attribution; repository regression gate; verified macOS artifact; independent exact-SHA release audit.
- Dependencies: existing Messages sync metadata must expose an unread signal; web results remain untrusted evidence and may not authorize effects; remote inference keeps fail-closed peer/authentication checks.
- Conflicts: none. Prior `codex/imessage-summary-priority` and `codex/web-response-followup` heads are ancestors of `origin/main`; GitHub has no open PRs at handoff creation.
