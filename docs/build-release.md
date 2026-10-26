# Wisp build and release

The build creates an **ad-hoc sealed local candidate** containing the Swift app,
tracked backend source and resources, and a relocatable Python runtime. It never
installs or launches Wisp. By default oMLX and its models remain separately installed
runtime prerequisites; a release may opt into the bundled engine described below.
Ad-hoc sealing carries no team identity and is not notarization, so a candidate is
still not a Developer ID signed or notarized release.

## Local commands

Run from a full Git checkout on Apple Silicon macOS with Python 3.9+ and Apple
Command Line Tools. Source builds target macOS 14 or newer. CI additionally
requires the exact Xcode and Swift versions in `build-support/toolchain.json`.
The driver records the actual compiler, SDK, OS, and whether strict checking ran.

```sh
./scripts/wisp-build all --dry-run
./scripts/wisp-build doctor
./scripts/wisp-build bootstrap
./scripts/wisp-build test
./scripts/wisp-build swift
./scripts/wisp-build preview
./scripts/wisp-build all
./scripts/wisp-build verify --output dist/<candidate-directory>
```

`all` requires committed, clean source and refuses to reuse an output directory.
`--allow-dirty` permits a development preview, explicitly marked in its metadata
and QA report; it is never release eligible. `--test-python` supports test auditing
with an existing interpreter and cannot be used to package an app. `swift` runs
all native fixture contracts and a release-mode Swift compilation.

`preview` is the fast path for local installs: it runs the same Swift release
build, bundle assembly, dependency bundling, ad-hoc signing, structure/native
validation and relocated-backend smoke as `all`, but skips Simulation QA, the
native/browser peer gates, benchmark contracts and distribution-evidence
validation. It accepts a dirty tree implicitly and writes
`dist/<version>-<build>-<commit>-preview/` with a `-preview` ZIP, `WispPreviewBuild`
set in Info.plist, and `preview: true` in provenance. `verify`, `release` and
`release-ad-hoc` refuse preview artifacts. Use `all` for anything that may ship.

`--jobs N` sets how many Simulation QA gates run at once (`test`, `swift`, `all`).
The default is one per CPU; `--jobs 1` is the original sequential run. Each gate
keeps its own process, temporary `WISP_HOME`/HOME and the same outer sandbox, so
concurrency changes neither isolation nor the report: results are listed in
selection order with the same schema, totals and failure rules at any `--jobs`.
Native compile-then-contract gates run as one ordered chain on a single worker.
Modules with real-socket peer attestation under a fixed readiness time budget
(`RUN_ALONE_TESTS`, currently `tests/test_runtime_peer.py`) run alone after the
concurrent gates finish, so CPU contention cannot turn that budget into a false
failure; the slowest known modules are queued first (scheduling only).
`scripts/run_simulation_qa.py --jobs N` and
`scripts/test_replay_failure_fixes.py --jobs N` accept the same option.

`./scripts/package_app.sh` delegates to `wisp-build all`. It does not install the
result. All generated files stay under `.wisp-build/` and `dist/`, except temporary
QA fixtures and reports that are removed after textual evidence is copied into
`.wisp-build/run-*/`. Neither generated directory belongs in Git.

## Bundled engine

The bundled oMLX engine is opt-in. Pass `--engine-app` to a build command to stage
the pruned server from a genuine, signed oMLX release:

```sh
./scripts/wisp-build preview --engine-app /Applications/oMLX.app
./scripts/wisp-build all --engine-app /Applications/oMLX.app
```

`--fetch-engine` does the same for the pinned release without a local oMLX.app: it downloads
the asset named in `build-support/engine-source.json` (official GitHub release URL only, TLS
validated, https-only redirects, bounded size and time), refuses it unless its size and SHA-256
match the pin, mounts it read-only, verifies the app's Developer ID signature and team, stages the
engine, and always unmounts (`.wisp-build/engine-cache/` keeps the verified image; an offline build
needs it cached). It is mutually exclusive with `--engine-app`. CI passes `--fetch-engine` in every
build command (PR preview, full, release rebuild, ad-hoc rebuild), so a release made by CI carries
the engine. Moving to a new oMLX release means updating `engine-source.json`, re-running
`engine.py lock` and refreshing the licence file together.

Release builds require the reviewed source pin at
`build-support/engine.lock.json`. Create it from the exact oMLX app intended for
the release with:

```sh
python build-support/engine.py lock --app /Applications/oMLX.app
```

The command verifies the oMLX bundle identity and signing team, then hashes the
files and relative links selected by the prune rules and records the highest
Mach-O macOS deployment target. It refuses to replace an existing lock; use
`--force` only when intentionally reviewing a new engine input. `--output PATH`
selects another lock path. The build checks the source version, selected-tree
digest, and macOS floor against the lock. A build with `--engine-app` and no lock
is a preview and cannot ship. Inspect included dependency licence metadata with
`python build-support/engine.py inventory --app /Applications/oMLX.app`.

After verifying that unchanged upstream input, staging applies the exact Wisp
guard overlay in `build-support/engine_guard/`. The manifest pins each upstream
Python file, exact transformations, the added proof module and resulting bytes;
changed inputs or outputs refuse the build. `ENGINE.json` retains the upstream
tree identity and separately records the overlay identity and effective derived
tree before signing. Structure validation rechecks the modified Python files.
The normal bundle manifest binds the final signed bytes. The signed source app
and its upstream lock are never modified by staging.
Changing the engine input also requires reviewing and updating this overlay;
an unmatched engine is refused even for a preview.

The existing authenticated engine status endpoint reports a bounded guard
snapshot from actual propagation and successful enforcement iterations. Default
guarded loads require fresh dynamic telemetry and a live enforcement task before
allocation, including pinned startup, lazy text starts and engine fallbacks.
Missing evidence produces a sanitized refusal rather than a retry or relaxed
limit. Explicit custom/disabled user profiles retain their existing memory
policy and are reported as unqualified for balanced-default testing.

The engine currently needs macOS 26.2. Wisp's app deployment target remains
macOS 14.0; the bundled engine is unavailable on systems below its recorded
floor. The staged engine is approximately 716 MB uncompressed (the full oMLX.app
was about 1.6 GB in the feasibility measurement); ZIP size varies by release.
Nested engine code is signed with identifier
`com.wisp.engine.python3` for its interpreter and the entitlements in
`build-support/engine.entitlements` (JIT, unsigned executable memory, and
disabled library validation).

### Licence review and notices

Every build that stages the engine also reviews the licences of the packages it kept
(`build-support/engine_licenses.py`). Each package is classified from its declared
licence and the title of its licence files. Permissive licences pass. A weak-copyleft
(LGPL, MPL), strong-copyleft (GPL, AGPL) or unrecognised licence fails the build unless
`build-support/engine-licenses.json` has an entry for that package with the same class
and a written reason, so a package update that changes a licence cannot slip through. A
package with no licence file in its wheel also fails until it is listed with
`no_licence_file`. A passing build writes `Documentation/THIRD-PARTY-NOTICES.md` (oMLX,
the copyleft packages with their source addresses, and a table of every package) and
copies each package's licence files to `Documentation/licenses/<name>-<version>/`; the
structure check refuses a bundle whose notice does not list every shipped package.
The classifier reads a declared licence only when it is short (a pasted text is judged through
the licence files), recognises `GPLv3`/`LGPLv2+` spellings and a GPL title or the short
"free software" notice in any licence file, and never auto-passes Commons Clause, Business
Source or non-commercial terms. The CPython interpreter and the oMLX DeepSeek patch are listed
under `components` in `engine-licenses.json`; their licence files must exist at the named path.
oMLX's own Apache-2.0 text is `build-support/engine-licenses/oMLX-LICENSE.txt`: the oMLX
app carries no top-level LICENSE, so it is the LICENSE file from the `v0.7.0` tag of
github.com/jundot/omlx (the repository has no NOTICE file). When `engine.lock.json` moves to
a new oMLX release, refresh it from that release's tag. The pinned release is 0.7.0
(`oMLX-0.7.0-macos26-27.dmg`, sha256 2e3bb06a…e3bce0, Developer ID team PSK5Q5T46L); its
admin UI lives in `omlx/admin`, so the staged engine has no `omlx_web` folder.

### Engine import smoke on older build hosts

The build imports the engine's server and initialises MLX on a GPU from the relocated, signed
bundle. MLX cannot be imported below the engine's own minimum macOS, so a build host older than
that (the hosted `macos-15` runners are) records `engine_smoke: {mode: skip, reason: ...}` in the
build metadata and provenance, writes the reason to `relocated-engine-smoke.log`, and the release
notes say the smoke was not run on the build host. The engine tree is still verified byte for byte
against `engine.lock.json` and the pinned release asset. A host that meets the minimum always runs
the smoke and fails the build if it fails (including a host with no usable GPU). The smoke was run
for this exact pinned tree on a macOS 26+ Apple Silicon Mac when 0.7.0 was pinned.

To exercise an already staged engine with a downloaded model, run the smoke on a
machine with compatible Apple Silicon and macOS:

```sh
python scripts/engine_smoke.py \
  --engine-root /Applications/Wisp.app/Contents/Resources/engine \
  --model-dir /path/to/model-directory
```

The smoke allocates a random loopback port, starts the engine with a temporary
private data directory and HOME, discovers the smallest listed model in the
provided model directory, then requests one short chat completion and one
embedding. It prints a JSON result and stops the engine on success or failure.
Use `--model NAME` to require a particular model id. It does not use the user's
`~/.omlx` state.

## Reviewed inputs and offline builds

`toolchain.json` pins uv, Python, the standalone Python build date, architecture,
minimum OS, app version, and CI compiler policy. The uv archive, extracted uv
binary, and Python archive have SHA-256 pins. Downloads use macOS curl with TLS
verification, no curl configuration, HTTPS-only redirects, and bounded size and
time. The driver checks hashes before executing or unpacking downloaded content.
Pinned uv installs Python from a verified local archive mirror and checks the
installed interpreter version and upstream BUILD marker.

The two committed `requirements-*.lock` files contain exact package versions and
PyPI hashes. Runtime and test environments use binary wheels only and `uv pip
sync --require-hashes --strict`; ambient package indexes and Python injection
variables are not inherited. `locks.json` covers both lock files and their source
requirements/toolchain inputs. Missing or stale locks stop packaging before
bootstrap. To intentionally refresh dependencies:

```sh
./scripts/wisp-build lock
# Review both lock files and locks.json, then commit the changes.
./scripts/wisp-build all --offline
```

Offline builds require a previously verified uv binary, the verified Python
archive, and the wheel cache. An empty cache fails explicitly. CI caches only
`.wisp-build/uv-cache`, never installed runtimes, executable build products, or
test fixtures. A release rebuild restores no dependency cache.

Pin provenance, reviewed September 10, 2026:

- [uv 0.12.1 release](https://github.com/astral-sh/uv/releases/tag/0.12.1): archive
  `uv-aarch64-apple-darwin.tar.gz`; official release asset SHA-256 recorded in the
  toolchain file, plus the extracted executable hash.
- [Standalone Python build 20260728](https://github.com/astral-sh/python-build-standalone/releases/tag/20260728):
  `cpython-3.13.14+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz`;
  official release asset SHA-256 recorded in the toolchain file.
- Official tag references for [checkout v4.2.2](https://github.com/actions/checkout/releases/tag/v4.2.2),
  [cache v4.2.3](https://github.com/actions/cache/releases/tag/v4.2.3), and
  [upload-artifact v4.6.2](https://github.com/actions/upload-artifact/releases/tag/v4.6.2)
  resolve to the full commit SHAs pinned in the workflow.

## Test and packaging gates

`build-support/simulation.py` adapts `scripts/run_simulation_qa.py`. It preserves
that runner's per-file script/pytest classification, recursive reviewed manifest,
exit/count interpretation, and schema-3 exact-SHA report. It adds the explicit
`tests/build_pipeline/pipeline_checks.py` unittest entry point, plus native
AssistantDelivery and BackendRecovery contracts, to the full build gate. The
product manifest stays unchanged and its standalone CLI remains usable. The build
entry point requires a nonzero reported test count; it cannot pass with an empty
suite. To run only these automation contracts, use
`.wisp-build/test-env/bin/python -B tests/build_pipeline/pipeline_checks.py`. New Python test files still fail manifest validation
until their owner reviews them. The previous build-specific test matrix,
exclusions, and routing failure baseline have been removed.

The full gate runs all reviewed Python files and the native Mail reply, Mail DB,
privacy sync, source label, search, Research Library, assistant delivery, and
backend recovery fixture programs. Counts that a native command does not report
remain null; gate success does not pretend those commands reported test counts.
A failed compile blocks its dependent contract and fails the candidate.
`FixtureTemporaryDirectory.swift` is linked only into direct native fixture
programs: it makes FileManager honor the gate's TMPDIR on hosts where Foundation
uses the shared user temp directory instead. It is never linked into WispApp.

Simulation QA creates synthetic homes and per-process Wisp state. An outer macOS
Seatbelt profile also denies network traffic, Apple Events, private-home file
reads, writes outside owned scratch/build state and the uniquely named native fixture
replacement folders used by Foundation atomic writes, and execution outside the
reviewed interpreter/compiler/fixture tool allowlist. These are safeguards for
trusted repository tests, not a sandbox for hostile code. A required separate native
signing probe adds only `/usr/bin/codesign` to its profile and verifies that the
general profile denies it and both profiles deny unrelated credential tools.
General Simulation Python tests do not receive this signing permission. Live app commands are
excluded by the authoritative runner. No test baseline waives a failure.

After QA passes, the driver compiles the Swift executable and generates the icon.
It copies tracked service files and the pristine, hash-verified standalone Python
archive into
`Wisp.app/Contents/Resources/backend/.venv`, matching BackendManager's interpreter
path. Using the original archive avoids uv's local-install dylib ID rewriting
and preserves upstream code before the documented local metadata cleanup and signing. The
extractor rejects traversal, special files, and escaping symlinks before writing.
It synchronizes runtime dependencies into that owned copy, removes
non-runtime console scripts with fixed build prefixes, and includes licenses,
required YAML and skill resources, documentation, and build metadata.

Validation checks required resources and permissions, version metadata, symlink
containment, absence of databases/credentials/host virtualenv markers, arm64
Mach-O slices, deployment targets, and external runtime library dependencies.
Dylib identity labels are distinguished from loads. Unused absolute build search paths are removed only from libraries whose declared
dependencies are all system libraries; paths that could affect a non-system
dependency fail validation. Backend imports
run inside a separate sandbox after moving the bundle beneath a path containing
spaces. The smoke check also imports the document libraries and roundtrips tiny in-memory
PNG/JPEG fixtures. It does not start the application lifespan, server, or app UI.

The Swift release build uses a tracked source copy with the development checkout
fallback removed, and maps compiler source paths to `/wisp/source`. The source
checkout is unchanged. Debug symbols are stripped from copied Mach-O files before
signing. Every Mach-O is scanned for this host’s repository and temporary prefix,
and the development fallback and its home prefix in UTF-8 and UTF-16. The app executable additionally
rejects any user-home or macOS temporary path. Pinned third-party binaries can retain
their upstream builders’ diagnostic paths; those are not this host’s checkout paths.

The ZIP uses stable ordering, timestamps, permissions, and Unix symlink records.
Apple's `ditto` extracts it, after which the inventory must match and the relocated
backend smoke check repeats. After native metadata cleanup, every bundled Mach-O
and nested code bundle is signed deepest first, with the app bundle last. Strict
whole-bundle verification must pass before inventorying or archiving signed bytes,
and again after ZIP extraction. Without that seal the linker's per-executable
ad-hoc signature leaves the bundle unsealed and macOS cannot validate an installed
copy. Sealing uses only the local `-` identity, with no keychain, credential, entitlement, or network
timestamp, and no packaging step invokes Developer ID signing or notarization.

Each candidate directory contains:

- `Wisp.app` and a version/build/SHA-associated ZIP;
- `bundle-manifest.json`, `dependencies.json`, and `simulation-qa.json`;
- `provenance.json`, `release-notes.md`, and `SHA256SUMS`.

Public releases contain one uploaded app ZIP, GitHub's automatic source ZIP and
TAR.GZ archives, and release notes in the page body. The app download link is the
first line of the page, and its `01-Wisp-<version>-<build>-arm64.zip` name puts it
ahead of the source archives. Do not attach duplicate release notes or technical
verification files unless the user explicitly requests an exception.

The complete checksum manifest, dependency inventory, provenance, bundle
manifest, and QA results remain in the verified local candidate and CI artifacts.
Every internal file is still validated before selecting the app for public
upload; reducing the public download list does not reduce the build gate.

Provenance identifies the exact source, dependency inputs, actual toolchain,
`ad-hoc` signature status with `notarized: false`, QA report hash, and step logs. The driver rejects source changes
during assembly. `verify` rechecks checksums, the app inventory, and QA evidence.
Pinned inputs and normalized ZIP metadata improve repeatability; they do not
promise bit-identical Swift binaries across SDKs or signed artifacts.

## CI and explicit release hooks

CI splits by event. Branch protection requires two checks, **Verified macOS
artifact** and **python-regressions**; both names exist on every event.

Pull requests (fast path, `wisp-build.yml`):

- **Preview build (PR)** (`macos-15`): ACL qualification, `wisp-build preview
  --strict-toolchain` (Swift release build, assembly, native validation, ad-hoc
  signing, relocated-backend smoke), the synthetic publisher/provisioning tests,
  and the offline mini artifact. Uploads a `wisp-preview-<sha>` ZIP that every
  release command refuses.
- **Simulation QA (PR)** (`macos-15`, in parallel): `wisp-build test
  --strict-toolchain`, the same `simulation_tests` stage `all` runs (sandbox
  contracts, native peer, browser bridge and benchmark contract gates, then the
  sandboxed full profile at one gate per runner CPU). The native/benchmark
  contract gates stay in this job because their hash-pinned reports are inputs to
  the same Simulation QA run; splitting them out would repeat them.
- **Verified macOS artifact** (`ubuntu-latest`, no checkout, no permissions):
  passes only if both jobs above succeeded and each reported the exact
  pull-request head SHA it verified.
- **python-regressions** (`regression-gate.yml`): runs only the modules that
  Simulation QA's full profile does not run (`--only-outside-simulation-qa`).
  The full profile refuses manifest drift, so this is normally an empty set and
  a pull request no longer runs every module twice. The sandboxed Simulation QA
  is kept because it is the stricter gate (pinned runtime, OS sandbox,
  reviewed manifest) and is the one release evidence relies on.

Pushes to main, `v*` tags and manual dispatch (full path):

- **Full verified artifact** runs `wisp-build all --strict-toolchain` unchanged:
  every gate plus distribution evidence, then the provisioning tests and mini
  artifact, and uploads `wisp-candidate-<sha>`. **Verified macOS artifact**
  passes only if it succeeded. Release jobs depend on it directly and still
  rebuild from source.
- **python-regressions** runs the complete regression gate outside the sandbox
  as a cross-check, one module per runner CPU at a time.

The full path builds and uploads candidates on main pushes, tags, and manual
dispatch. [GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
identifies `macos-15` as arm64; `doctor` independently rejects any other host
architecture. CI selects Xcode 16.4 and requires Swift 6.1.2. A local Command Line
Tools build records `strict_toolchain: false` and cannot pass release preflight.
The hosted workflow and strict toolchain require their own CI validation.

Signing/notarization/publication are a separate, explicit `release` command,
reserved for a protected GitHub Actions `release` environment. Configure required
reviewers on that environment before enabling it. Only a manual dispatch with
`publish: true` on the exact `v<version>` tag can reach the release job. Ordinary
builds and tags perform local ad-hoc signing only; they do not use Developer ID
credentials, notarize, or publish a release.

Because the public ZIP is ad-hoc signed and not notarized, `create_public_draft` in
`build-support/release.py` puts a short "First launch" note directly under the
first-line download link: open Wisp once (macOS 15 and later blocks it), then System
Settings > Privacy & Security > Open Anyway > confirm. Control-click > Open is only for
macOS 14 and earlier, and `xattr -dr com.apple.quarantine <path to Wisp.app>` is the
fallback. The same steps lead the README's install section. A release promoted by hand
(for example a release candidate) uses its curated `docs/releases/<version>.md` as the
notes file, so the same steps must be at the top of that file.

Release preflight requires a clean candidate matching HEAD, passing full QA and
strict compiler evidence, the exact tag on main, no existing GitHub release, and
all credentials before external steps. The hook uses an ephemeral keychain,
replaces the candidate’s ad-hoc signatures in the same nested-first order with
Developer ID signatures and hardened runtime entitlements bound from the exact
candidate commit. It verifies each target's signed entitlements and directory-complete
payload identity before and after signing, notarizes/staples, and verifies the signed
archive. Uploaded assets remain a draft until the complete verified output has been
atomically installed locally; a placement or publication failure never exposes a
partial public release.
The release job rebuilds from source and exposes secrets only to that step.

Required environment secrets are `WISP_SIGNING_P12_BASE64`,
`WISP_SIGNING_P12_PASSWORD`, `WISP_SIGNING_IDENTITY`, `WISP_APPLE_API_KEY_BASE64`,
`WISP_APPLE_KEY_ID`, and `WISP_APPLE_ISSUER_ID`; GitHub supplies `GH_TOKEN`.
Credential operations suppress command/output and delete temporary key material.
The hook does not alter the user's default keychain or global keychain search list.

### GitHub ad-hoc downloads

Every eligible CI build retains the verified versioned `Wisp.app` ZIP and its
complete technical evidence as workflow artifacts. To make that free, ad-hoc build
available on GitHub, manually run **Wisp build** from the matching `v<version>`
tag with **Publish the verified ad-hoc ZIP** enabled and **Sign, notarize and
publish** disabled. The tag-only job rebuilds with the strict toolchain and
verifies the clean source, exact configured version tag, and main ancestry.
It binds the complete artifact set to retained file descriptors, rechecks the
ZIP roundtrip and ad-hoc signature, then uses the repository `GITHUB_TOKEN`
to create a draft and upload the verified bytes. Only a complete upload is
published. Existing releases (including failed drafts) are never overwritten;
review a failed draft before any manual recovery.

The release benchmark (`docs/RELEASE_PERFORMANCE_BENCHMARK.md`) is an optional measurement tool and is no
longer a publication gate (removed for 1.2.5 by owner decision). Neither publisher takes performance inputs.

Download the app ZIP from the first link on the release page and extract it
with Finder or `ditto`. Maintainers can download the complete CI candidate and
verify its internal files with `shasum -a 256 -c SHA256SUMS`. The public app ZIP
uses a download-friendly alias while retaining the verified archive bytes.
This build is ad-hoc signed only: it is not notarized and
does not establish Gatekeeper trust. On macOS 15 and later, the first open is
blocked and must be allowed under System Settings > Privacy & Security >
**Open Anyway** (Control-click > **Open** only works on macOS 14 and earlier).
Removing the downloaded quarantine attribute with `xattr` is the fallback. A free Apple personal-team signature is not suitable for
general GitHub distribution; use the protected Developer ID path for a signed,
notarized public release.

These external release hooks have not been exercised locally. This implementation
phase authorizes local compilation, fixture QA, and ad-hoc sealed artifacts only.
