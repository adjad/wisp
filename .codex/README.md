# Wisp Codex environments

The checked-in local environment is `.codex/environments/environment.toml`. Codex Desktop uses it when a Wisp worktree selects the **Wisp** environment. Its setup script creates an isolated `.venv` with the CI-pinned Python 3.13.14, using `uv` when available or an installed `python3.13` otherwise. On macOS it installs the same hash-locked Python dependencies used by CI. The **Regression gate** and **Verified macOS build** actions run on macOS only. No credentials, models, user data, or app state are copied into worktrees.

For Codex Cloud, connect `adjad/wisp` in Codex Settings > Environments and use `./scripts/codex-setup` as the setup command with Python 3.13. The Linux setup is for source editing only: Wisp's native build, Keychain, Mail, Messages, Calendar, and live app checks require macOS. GitHub's required CI remains the mechanical gate for cloud-authored changes. Do not add local secrets or permissions to the cloud environment.

Cloud environment registration lives in the user's Codex account, not in this repository; this file does not create or authenticate that account-level environment.
