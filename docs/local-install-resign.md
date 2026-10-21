# Local installs: re-signing with "Wisp Dev" so grants survive

**Problem.** Every pipeline artifact is ad-hoc signed. Its designated requirement is
`cdhash H"…"`, so macOS treats each build as a new application and the Calendar,
Reminders and Full Disk Access grants the user gave the previous build die on every
install (observed on builds 2042, 2192 and 2203; each cost the user two re-grants).

**Fix (local installs only).** `scripts/resign_local_install.py` runs after the verified
preview build and before the swap into `/Applications`:

```bash
scripts/resign_local_install.py dist/preview/Wisp.app --output /Applications/Wisp.app.new
```

It copies the artifact (never signs it in place), re-signs the **outer bundle only** with
the self-signed "Wisp Dev" identity from the login keychain (by its SHA-1 hash, no `--deep`,
`--timestamp=none`, `--preserve-metadata=identifier,entitlements,flags,runtime`), verifies
the result (`codesign --verify --deep --strict`, a certificate-bound designated requirement
`identifier "com.wisp.assistant" and certificate leaf = H"…"`, authority "Wisp Dev", and
byte-identical nested signatures), and appends one JSON line to
`~/.moe/local-installs.jsonl` with the artifact's cdhash next to the installed one.

If the identity is missing or codesign refuses (a keychain "allow codesign to use the key"
prompt the user must answer themselves), the output is the untouched ad-hoc copy and the
tool says plainly that the grants will reset. `--require` turns that into a failure.

**What stays as the pipeline made it.** The backend interpreter
(`Contents/Resources/backend/.venv/bin/python3`) and the bundled engine
(`Contents/Resources/engine/*`, absent from preview builds) keep their signatures: the
attribution checks (`service/inference/bundled_peer.py` reads the backend's own
csops flags and Team ID; `attributed_transport.py` qualifies the engine process) read
the running processes, which are unaffected by the outer bundle's signature. The tool
fails if any nested cdhash changed.

**What this is not.** Not part of the CI artifact pipeline, `release.py`, or the
provenance rule that a verified artifact is ad-hoc or Developer ID signed. The artifact
file is never modified; the re-sign is a local-install transformation recorded in the log.

**Things that reference the app's signature (checked 2026-10-20).**
- `BackendOwnership.swift` threat model: unchanged in substance (a same-user attacker who
  can sign with the user's own key already controls Wisp); its comment now says so.
- `BackendCredentials.swift`: keychain ACL readers are `SecTrustedApplication` identities
  for `/Applications/Wisp.app`. With ad-hoc signing that identity changed every build; with
  "Wisp Dev" it is stable, so the first re-signed install is the last time the readers change.
- `BrowserBridge.swift`: the peer requirement comes from configuration per browser, not from
  Wisp's own signature.
- The gate's host-busy detection and the pipeline's `distributed-signature` steps act on the
  artifact before the re-sign, not on the installed app.

**Live pass criterion.** Install main re-signed; grant Calendar/Reminders/FDA once; do a
second no-op reinstall of the same main, re-signed; the grants must stay with no prompt.
