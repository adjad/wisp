# Local installs: re-signing with "Wisp Dev" so grants survive

**Problem.** Every pipeline artifact is ad-hoc signed. Its designated requirement is
`cdhash H"…"`, so macOS treats each build as a new application and the Calendar,
Reminders and Full Disk Access grants the user gave the previous build die on every
install (observed on builds 2042, 2192 and 2203; each cost the user two re-grants).

**Fix (local installs only).** `scripts/resign_local_install.py` runs after the verified
preview build and before the swap into `/Applications`:

```bash
scripts/resign_local_install.py dist/preview/Wisp.app --output /Applications/Wisp.app.new --require
```

`--installed` (default `/Applications/Wisp.app`) names the app whose grants should carry
over; its designated requirement is read with `codesign -d -r-`, never changed.

It strictly verifies the artifact, copies it (never signs it in place), verifies the copy,
and re-signs the **outer bundle only** with the self-signed "Wisp Dev" identity from the
login keychain: by its SHA-1 hash (never by name; two identities with one name are
refused), no `--deep`, `--timestamp=none`,
`--preserve-metadata=identifier,entitlements,flags,runtime` (the ad-hoc requirement is
deliberately not preserved). Then it verifies the result:

- `codesign --verify --deep --strict`;
- the designated requirement is exactly
  `identifier "com.wisp.assistant" and certificate leaf = H"<identity sha1>"` (a cdhash,
  an anchor, an `or`, another identifier or another certificate is refused);
- the authority is exactly "Wisp Dev" and the ad-hoc flag is gone;
- every file except `Contents/MacOS/Wisp` and `Contents/_CodeSignature` is byte-identical
  to the artifact (size + SHA-256, symlink targets), so no nested signature changed; and
  the backend interpreter's and the bundled engine interpreters' own cdhashes
  (`Contents/Resources/backend/.venv/bin/python3`, the Mach-O files in
  `Contents/Resources/engine/py/bin`, absent from preview builds) are unchanged; an
  unreadable directory or file anywhere in the bundle is an error, never a skipped subtree.

One JSON line goes to `~/.moe/local-installs.jsonl` (created mode 600) with the artifact's
cdhash, signature and requirement next to the installed ones, the build number, the
commit, the identity hash and the comparison counts.

**What the user sees about grants.** The tool compares the new requirement with the
installed app's (or, with no installed app, the last logged install's). The first install
signed this way replaces an ad-hoc requirement, so macOS asks once more and the tool says
"ask once more" (or "re-prompts once" when nothing is known). From the second install on
the requirement is identical and the tool says the grants carry over. Keychain items created by an ad-hoc build
(`BackendCredentials.swift`'s ACL readers for `/Applications/Wisp.app`) likewise mismatch
once and are stable afterwards.

**Fallback.** No identity, an ambiguous identity, a `security` failure (locked keychain) or a
codesign refusal leaves the **verified** ad-hoc copy at `--output` and says plainly that the
grants **will reset**; `--require` stages nothing, logs nothing and exits 2 instead, which
the install procedure uses. An output that appears mid-run and was not created by this run
is never removed; an interrupt removes only what this run created. A codesign that hangs (an unanswered keychain "allow codesign to use the
key" prompt, which only the user may answer) is a hard failure after the timeout with
nothing left at `--output`.

**Trade-off, stated plainly.** Under ad-hoc signing no other program could inherit Wisp's
grants. With a certificate-bound requirement, anything signed with the "Wisp Dev" key and
`identifier com.wisp.assistant` would match the TCC grants and the keychain ACL readers. The
key lives in the user's login keychain: keep its access control prompting ("Allow", not
"Always Allow") so no same-user program can sign with it silently. The threat model in
`BackendOwnership.swift` is about the backend port and is unchanged, but it is narrower than
what the signature now protects: a same-user process that gets past the key's ACL prompt can
obtain Wisp's Calendar, Reminders and Full Disk Access grants and its keychain ACL reader
identity, which an ad-hoc build never exposed. Its comment records this trade-off.

**What stays as the pipeline made it.** The backend interpreter and the bundled engine keep
their signatures; `service/inference/bundled_peer.py` reads the running backend's own
csops flags and Team ID, and `attributed_transport.py` qualifies the engine process, so an
outer re-sign cannot affect them, and the tool proves nothing nested changed.

**What this is not.** Not part of the CI artifact pipeline, `release.py`,
`verify_bundle_signature`, or the provenance rule that a verified artifact is ad-hoc or
Developer ID signed. The artifact file is never modified. Neither the gate nor CI needs the
identity: the tests use a fake codesign.

**Other readers of the app's signature (checked 2026-10-20).** `BrowserBridge.swift` takes
its peer requirement from configuration per browser; nothing calls `SecCodeCopySelf`;
`build-info.json` and the bundle manifest are compared only against artifacts; the gate's
host-busy detection and the pipeline's `distributed-signature` steps act on the artifact.

**Live pass criterion.** Install main re-signed; grant Calendar/Reminders/FDA once; do a
second no-op reinstall of the same main, re-signed; the grants must stay with no prompt.
