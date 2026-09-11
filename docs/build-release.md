# Wisp build and release

The build creates an **ad-hoc sealed local candidate** containing the Swift app,
tracked backend source and resources, and a relocatable Python runtime. It never
installs or launches Wisp. oMLX and its models remain separately installed runtime
prerequisites. Ad-hoc sealing carries no team identity and is not notarization, so a
candidate is still not a Developer ID signed or notarized release.

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
./scripts/wisp-build all
./scripts/wisp-build verify --output dist/<candidate-directory>
```

`all` requires committed, clean source and refuses to reuse an output directory.
`--allow-dirty` permits a development preview, explicitly marked in its metadata
and QA report; it is never release eligible. `--test-python` supports test auditing
with an existing interpreter and cannot be used to package an app. `swift` runs
all native fixture contracts and a release-mode Swift compilation.

`./scripts/package_app.sh` delegates to `wisp-build all`. It does not install the
result. All generated files stay under `.wisp-build/` and `dist/`, except temporary
QA fixtures and reports that are removed after textual evidence is copied into
`.wisp-build/run-*/`. Neither generated directory belongs in Git.

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
trusted repository tests, not a sandbox for hostile code. Live app commands are
excluded by the authoritative runner. No test baseline waives a failure.

After QA passes, the driver compiles the Swift executable and generates the icon.
It copies tracked service files and the pristine, hash-verified standalone Python
archive into
`Wisp.app/Contents/Resources/backend/.venv`, matching BackendManager's interpreter
path. Using the original archive avoids uv's local-install dylib ID rewriting
and preserves upstream Mach-O bytes without signing or binary patching. The
extractor rejects traversal, special files, and escaping symlinks before writing.
It synchronizes runtime dependencies into that owned copy, removes
non-runtime console scripts with fixed build prefixes, and includes licenses,
required YAML and skill resources, documentation, and build metadata.

Validation checks required resources and permissions, version metadata, symlink
containment, absence of databases/credentials/host virtualenv markers, arm64
Mach-O slices, deployment targets, and external runtime library dependencies.
Dylib identity labels are distinguished from loads. An upstream build search path
is retained and recorded only on libraries whose declared dependencies are all
system libraries; paths that could affect a non-system dependency fail validation. Backend imports
run inside a separate sandbox after moving the bundle beneath a path containing
spaces. The smoke check also imports the document libraries and roundtrips tiny in-memory
PNG/JPEG fixtures. It does not start the application lifespan, server, or app UI.

The ZIP uses stable ordering, timestamps, permissions, and Unix symlink records.
Apple's `ditto` extracts it, after which the inventory must match and the relocated
backend smoke check repeats. After assembly the bundle is sealed with the ad-hoc
identity: every bundled Mach-O file is signed, then the bundle itself, and strict
whole-bundle verification must pass. Without that seal the linker's per-executable
ad-hoc signature leaves the bundle unsealed and macOS cannot validate an installed
copy. Sealing uses no identity, keychain, credential, entitlement, or network
timestamp, and no packaging step invokes Developer ID signing or notarization.

Each candidate directory contains:

- `Wisp.app` and a version/build/SHA-associated ZIP;
- `bundle-manifest.json`, `dependencies.json`, and `simulation-qa.json`;
- `provenance.json`, `release-notes.md`, and `SHA256SUMS`.

Provenance identifies the exact source, dependency inputs, actual toolchain,
`adhoc` signature status with `notarized: false`, QA report hash, and step logs. The driver rejects source changes
during assembly. `verify` rechecks checksums, the app inventory, and QA evidence.
Pinned inputs and normalized ZIP metadata improve repeatability; they do not
promise bit-identical Swift binaries across SDKs or signed artifacts.

## CI and explicit release hooks

The workflow builds and uploads candidates on PRs, main/codex branch pushes, tags,
and manual dispatch. [GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
identifies `macos-15` as arm64; `doctor` independently rejects any other host
architecture. CI selects Xcode 16.4 and requires Swift 6.1.2. A local Command Line
Tools build records `strict_toolchain: false` and cannot pass release preflight.
The hosted workflow and strict toolchain require their own CI validation.

Signing/notarization/publication are a separate, explicit `release` command,
reserved for a protected GitHub Actions `release` environment. Configure required
reviewers on that environment before enabling it. Only a manual dispatch with
`publish: true` on the exact `v<version>` tag can reach the release job. Ordinary
builds and tags do not sign or publish.

Release preflight requires a clean candidate matching HEAD, passing full QA and
strict compiler evidence, the exact tag on main, no existing GitHub release, and
all credentials before external steps. The hook uses an ephemeral keychain,
signs nested Mach-O files and the app with hardened runtime entitlements,
notarizes/staples, verifies the signed archive, and creates a GitHub release.
The release job rebuilds from source and exposes secrets only to that step.

Required environment secrets are `WISP_SIGNING_P12_BASE64`,
`WISP_SIGNING_P12_PASSWORD`, `WISP_SIGNING_IDENTITY`, `WISP_APPLE_API_KEY_BASE64`,
`WISP_APPLE_KEY_ID`, and `WISP_APPLE_ISSUER_ID`; GitHub supplies `GH_TOKEN`.
Credential operations suppress command/output and delete temporary key material.
The hook does not alter the user's default keychain or global keychain search list.

These external release hooks have not been exercised locally. This implementation
phase authorizes local compilation, fixture QA, and ad-hoc sealed artifacts only.
