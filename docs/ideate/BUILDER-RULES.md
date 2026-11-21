# Rules every Ideate builder follows

- Worktree: /Users/adijain/Desktop/MOE_Project/.claude/worktrees/ideate (branch claude/harbor-ideate). Work ONLY there.
  Never touch the main checkout, other worktrees, or any claude/harbor-0.1.7* / 0.1.8* branch.
- Edit ONLY the files your brief says you own. Other builders are editing other files in this same worktree at
  the same time; if a compile fails in a file you don't own, report it, don't fix it. If you need a change in a
  file you don't own, say exactly what in your final reply.
- Do NOT commit, push, stash, reset, checkout or switch branches. The orchestrator commits.
- Heavy lock: EVERY Swift compile (swiftc, swift build, scripts/test_*.sh) and any heavy job runs under
  `bash /Users/adijain/Desktop/MOE_Project/.claude/harbor-import/tools/locked.sh <logfile> <cmd...>`
  (it waits for the lock; read the log afterwards). Never run two at once, never remove a lock you didn't
  create, never stop the job that holds it. Use `--jobs 2` / `-j 2` where a tool takes it.
  The locked.sh call needs `dangerouslyDisableSandbox: true` (the lock lives in /private/tmp/claude-501).
- Quick compile of pure files: `swiftc -parse-as-library -swift-version 5 -target arm64-apple-macosx14.0
  -module-cache-path $TMPDIR/mc <files> tests/<YourChecks>.swift -o $TMPDIR/x && $TMPDIR/x` (under the lock).
  Full app compile: `bash scripts/test_first_run_setup.sh` (under the lock, ~several minutes).
- Python tests: `/Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest -q tests/<file>`; a bare `pytest tests/` runs nothing.
- Never launch the app, never use ports 8765 or 8775, never load models into oMLX (127.0.0.1:8000), never
  read/print API keys, tokens, auth.json or the Keychain, no `security`, no pkill by path, no downloads.
- Any Python that imports service.* must run with WISP_HOME set to a scratch dir under $TMPDIR
  (importing service.assistant.store opens the LIVE ~/.moe store otherwise).
- No personal data anywhere (tests, fixtures, mockups, docs, comments): invent every name, project, course, number.
- UI rules (Harbor notch): black monochrome; colour only for status (Theme.bad soft red = error, Theme.warn amber =
  needs you; working is neutral). Use Theme / WispInk / `Font.wisp(_ role:)` tokens, never raw sizes or hex.
  Panel is 640 pt wide (wide mode exists). No sheets, alerts or popovers in notch views (non-activating panel);
  confirmations are inline cards. No tables, no headings or nested bullets in Markdown. Short, plain, calm copy.
  Write "Wisp" in user-facing strings and identifiers, like the existing code: the import pipeline renames
  Wisp→Harbor for the shipped app.
- Keep the approval and safety layer intact (service/safety, service/agent/approver.py). Nothing new may run a shell
  command or write outside a project folder without that layer's confirmation.
- Finish with a reply listing: files changed, checks run with results (failures included), what you couldn't do,
  and anything the orchestrator must wire.
- CREDENTIALS (added 2026-11-21): never open, read, print or copy anything under `~/.config/opencode`, `~/.pi/agent/auth.json`,
  `~/.local/share/opencode`, or any other file holding a password, token or key. Never use the user's running OpenCode
  service or its service.json. A harness server for a test is a fresh instance with its own invented config in a scratch
  folder, on a port checked free.
