# In-app updates (Sparkle)

Wisp updates itself with [Sparkle 2](https://sparkle-project.org) (2.10.1, pinned in
`app/Package.swift`, `app/Package.resolved` and `build-support/toolchain.json`).
Testers install one build with the updater by hand; after that, **Check for Updates…**
in the menu bar menu installs the next one.

## What the app does

- **No network contact unless asked.** Info.plist sets `SUEnableAutomaticChecks`,
  `SUAutomaticallyUpdate` and `SUEnableSystemProfiling` to NO. Settings › General ›
  Updates has **Check for updates automatically** (off by default) and **Include beta
  versions**. Starting Sparkle at launch reads only local state.
- **Nothing about the person is sent.** No system profile, no feed parameters (the
  delegate returns none even if Sparkle would ask). A check is one GET of the feed with
  Sparkle's `Wisp/<build> Sparkle/2.10.1` user agent; an accepted update is one download
  from GitHub Releases.
- **Signed end to end.** Every archive carries an EdDSA signature checked against the
  `SUPublicEDKey` compiled into the running app, before extraction
  (`SUVerifyUpdateBeforeExtraction`). The feed itself is signed (`SURequireSignedFeed`),
  so whoever can write the hosting branch still can't add, reorder or retarget items.
- **Channels.** Pre-releases carry `<sparkle:channel>beta</sparkle:channel>`. A build's
  own channel (`updates.channel` in toolchain.json, recorded as `WispUpdateChannel`)
  sets the default of **Include beta versions**: on for a beta build, off for a stable one.
- **Fails closed.** A build without a public key (today's toolchain.json) has no feed;
  the menu item is disabled and Settings says updates aren't set up. The app refuses a
  configuration that is not https or that loosens any of the switches above. The one
  exception is `http://127.0.0.1`, for the local end-to-end test below; the pipeline only
  ever writes the https feed.

The framework is bundled at `Contents/Frameworks/Sparkle.framework` and signed with the
app. Wisp is not sandboxed, so Sparkle installs with its own `Autoupdate` tool and
`Updater.app`; the Installer and Downloader XPC services are only for sandboxed apps
and are removed, with the headers and module maps. The pipeline verifies the version,
the removed parts, the binary artifact checksum and that `Wisp` loads
`@rpath/Sparkle.framework` through `@executable_path/../Frameworks` (absolute toolchain
search paths are stripped, so dyld never looks outside the bundle first).

## Where the appcast lives

`appcast.xml` is the only file on the orphan **`appcast` branch** of `adjad/wisp`, served
from `https://raw.githubusercontent.com/adjad/wisp/appcast/appcast.xml`.

Why this and not the alternatives:

| Option | Problem |
| --- | --- |
| A fixed "appcast" release with `appcast.xml` as an asset | It shows in the release list, so the list gets cluttered. Replacing an asset means delete then upload, so the feed is briefly 404 on every publish. |
| `gh-pages` + GitHub Pages | It needs Pages turned on in the repository settings, and it adds a deploy step that can lag or fail separately from the release. It would work otherwise. |
| **`appcast` branch (chosen)** | Nothing in the release list, and no setup. Each publish is one commit through the contents API, which refuses a stale write. The branch history is the audit trail, and a revert rolls back. It never touches `main` or its protection. |

The download list stays exactly one asset per release: `01-Wisp-<label>-<build>-arm64.zip`.
No deltas, notes or appcast files are attached.

Recommended: a branch ruleset on `appcast` that blocks deletion and force pushes. The
feed signature already protects testers if the branch is tampered with; the ruleset
protects availability.

**Deltas are not used.** Hosting them as release assets would break the
one-asset rule. Most are too large for a git branch (GitHub's 100 MB file limit). Most of
the 378 MB is the Python runtime and the engine, which change between most betas anyway.
Worth revisiting if the size becomes a problem, with a separate download host.

## Keys: what you (the owner) do once

Nobody but you creates, sees or stores these keys. CI fails closed without them: the
`publish_update` job stops before building if `SPARKLE_ED_PRIVATE_KEY` is missing, and
`release-update` refuses to publish unless the archive's signature verifies against the
public key inside the app it is publishing.

1. **Generate the EdDSA key pair** (stays in your login keychain):

   ```bash
   ./scripts/wisp-build swift   # or any build; it fetches Sparkle's tools
   .wisp-build/swift/artifacts/sparkle/Sparkle/bin/generate_keys
   ```

   It prints the public key (`SUPublicEDKey`). Running it again only prints the existing key.

2. **Put the public key in the app**: set `updates.public_ed_key` in
   `build-support/toolchain.json` to that base64 string, run `./scripts/wisp-build lock`
   (toolchain.json is a locked input), and merge that as a normal PR. rc1-beta2 must be
   built from a commit that has it.

3. **Give CI the private key** (scoped to the protected `release` environment):

   ```bash
   .wisp-build/swift/artifacts/sparkle/Sparkle/bin/generate_keys -x "$TMPDIR/sparkle-private.key"
   gh secret set SPARKLE_ED_PRIVATE_KEY --env release --repo adjad/wisp < "$TMPDIR/sparkle-private.key"
   rm -P "$TMPDIR/sparkle-private.key"
   ```

4. **Back it up** (for example in your password manager, from the keychain item
   "Private key for signing Sparkle updates"). If it is lost, installed copies can no
   longer be updated in place; testers would reinstall by hand once.

## Publishing an update

From main, after the PR with the key is merged:

```bash
gh workflow run wisp-build.yml --repo adjad/wisp --ref main -f publish_update=true -f update_label=0.5.0-rc1-beta2
```

The `publish-update-release` job (in the `release` environment) does the following:

1. Rebuilds and verifies `main` exactly like `full`.
2. Signs the ZIP with `sign_update`.
3. Verifies the signature against the app's own key with an independent Ed25519 check.
4. Creates release `v0.5.0-rc1-beta2` at that commit, as a pre-release for the beta channel, with the ZIP as its only asset.
5. Publishes the release.
6. Only then commits the new signed `appcast.xml` to the `appcast` branch.

A rerun after a failure between steps 5 and 6 reuses the release only if its single
asset has exactly the signed archive's name, size and SHA-256.

A label with a suffix (`-rc1-beta2`, `-dev9`) goes to the beta channel; a plain
`0.5.0` goes to stable. Release notes come from `docs/releases/<label>.md` when that file
exists. They appear in the update window and on the release page, and you can still edit
the page afterwards.

**By hand instead** (for example from your Mac, signing with the keychain key):

```bash
./scripts/wisp-build appcast --output dist/candidate --label 0.5.0-rc1-beta2 \
    --feed appcast.xml --update-output dist/update-beta2
```

`--output` is a verified (non-preview) candidate. `--feed` is the current appcast.xml from the
`appcast` branch; leave it out for the first one. Upload the printed ZIP as the release's only
asset first, then commit `dist/update-beta2/appcast.xml` to the `appcast` branch.

## Code signing and privacy grants (TCC)

**Findings**

- **Grants are lost on every update while releases are ad-hoc signed.** macOS keys
  Full Disk Access, Screen Recording, Accessibility, Automation, Calendars, Contacts
  and Reminders grants to the app's *designated requirement*. For an ad-hoc signature
  that is `cdhash H"…"`, a hash of that exact build, so every new build (installed by
  hand or by Sparkle) is a different app to TCC. Calendars, Contacts and Reminders ask
  again. For Full Disk Access, Screen Recording and Accessibility the switch in System
  Settings can still look on while it no longer applies; the tester has to turn it off
  and on again. Sparkle doesn't make this worse than a manual reinstall, but it makes
  updates frequent. (This repository saw it on every ad-hoc install: builds 2042, 2192, 2203, 2211.)
- **Sparkle accepts ad-hoc builds.** Sparkle 2.10.1's `SUUpdateValidator` counts an ad-hoc
  app as code signed. It requires the update to be validly signed as well (ad-hoc
  qualifies), and accepts it when the EdDSA signature verifies with the *old* app's key.
  The code-signing match is not needed then, which is also what allows a later move from
  ad-hoc to self-signed or Developer ID. Sparkle removes the quarantine attribute from the
  installed update, so an update doesn't hit the Gatekeeper "Open Anyway" step; only the
  first manual install does. The local end-to-end test below exercises exactly this.

**Options**

| | Grants survive updates | First launch | Cost and risk |
| --- | --- | --- | --- |
| Ad-hoc (today, `updates.signing: "ad-hoc"`) | No: a re-grant round on every update | Open Anyway | Free |
| **Stable self-signed identity in CI** (`"self-signed"`, implemented) | Yes, from the first self-signed install on | Open Anyway (unchanged) | Free. Anyone holding the .p12 can sign code that macOS treats as Wisp for those grants, so keep it in the `release` environment and an offline backup only. Moving from ad-hoc to it costs one more re-grant round, so turn it on before rc1-beta2, which testers install by hand anyway. |
| Apple Developer ID + notarization | Yes (the requirement is anchored to Apple and your Team ID) | Opens normally | $99 a year and your Apple account. The pipeline's `release` path already signs and notarizes with Developer ID; Sparkle accepts the change of identity through EdDSA. |

`scripts/resign_local_install.py` already does the self-signed approach for your own
installs ("Wisp Dev"). It was verified on 2026-10-21: a re-signed reinstall kept
Calendar, Reminders and Full Disk Access with no prompt.

**Turning on the self-signed identity** (yours to do; nothing here creates it):

```bash
cd "$(mktemp -d)"
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=Wisp Updates" \
  -keyout wisp-updates.key -out wisp-updates.crt \
  -addext "basicConstraints=critical,CA:false" -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning"
openssl pkcs12 -export -legacy -inkey wisp-updates.key -in wisp-updates.crt -name "Wisp Updates" -out wisp-updates.p12
openssl x509 -in wisp-updates.crt -noout -fingerprint -sha1 | sed 's/.*=//; s/://g' | tr 'A-F' 'a-f'
base64 -i wisp-updates.p12 | gh secret set WISP_UPDATE_SIGNING_P12_BASE64 --env release --repo adjad/wisp
gh secret set WISP_UPDATE_SIGNING_P12_PASSWORD --env release --repo adjad/wisp   # the export password
```

Keep `wisp-updates.p12` (and its password) somewhere safe offline, then `rm -P` the
files. In `build-support/toolchain.json` set `updates.signing` to `"self-signed"` and
`updates.self_signed_leaf_sha1` to the printed fingerprint, then run
`./scripts/wisp-build lock`. `-legacy` is needed because macOS's `security import`
doesn't read OpenSSL 3's default PKCS#12 encryption. Ten years of validity matters: an
expired identity can't sign, and a new certificate means a new requirement, which means
one more re-grant round.

With the switch on, `release-update` imports the .p12 into a throwaway keychain and
re-signs only the outer app. It checks that the designated requirement is
`identifier "com.wisp.assistant" and certificate leaf = H"<pinned sha1>"` and that
nothing nested changed, then signs that archive for Sparkle. A missing secret or a
different certificate stops the job before anything is published.

## Local end-to-end test

The test proves the flow with two builds, a loopback feed and a throwaway test key. It
never touches the installed Wisp, the real key or the network:

1. Take a preview build and make two copies, **A** (build 9001) and **B** (build 9002).
   Set `SUFeedURL` to `http://127.0.0.1:<port>/appcast.xml` and `SUPublicEDKey` to a
   *test* key's public half. Re-sign each outer app ad-hoc.
2. Zip **B**, sign it with the test key's private seed (`sign_update --ed-key-file`),
   build the feed with `sparkle.item`/`sparkle.merge`, and sign the feed.
3. Serve the feed and the ZIP from `python3 -m http.server --bind 127.0.0.1`, put **A**
   in a scratch folder (not /Applications), launch it with an isolated `WISP_HOME`,
   and choose Check for Updates…. Sparkle offers B and installs it over A in place. The
   relaunched app reports build 9002.
