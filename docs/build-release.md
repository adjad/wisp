# Building and releasing Wisp

> **BLOCKED / incomplete checkpoint.** The pipeline below is drafted, not ready
> for release. Reviewed dependency locks are still absent. Public-download
> approval must be provided directly in this task before lock generation,
> clean bootstrap, full artifact validation, and upstream Actions-pin checks
> can finish. The build and CI intentionally fail before bootstrap when locks
> are absent. Four existing routing suites also remain failing; no test waiver
> or fabricated lock is included. See the checkpoint validation record below.

`./scripts/wisp-build` is the source-to-artifact entry point. It bootstraps pinned
build dependencies, runs isolated regressions and Swift contracts, compiles the
app, assembles its backend, and verifies a relocatable release candidate. It
never starts Wisp, stops services, installs an app, or changes user settings.

```sh
./scripts/wisp-build --dry-run
./scripts/wisp-build all
```

A successful run prints its artifact directory under `dist/`. The legacy
`./scripts/package_app.sh` delegates to the same pipeline. **It no longer copies
anything to `/Applications` or `~/Applications`.** The old README's launch
example and `dist/Wisp.app` location describe the former packaging script; use
the directory printed by the new command. Installing or replacing an existing
app remains an explicit, separate user action.

## Architecture and build inputs

The pipeline has three layers:

1. `build-support/toolchain.json`, the runtime/test dependency locks, and
   `locks.json` define reviewed inputs. Source files and release notes come from
   Git; untracked service files, local virtual environments, data, and credentials
   are never copied into the app.
2. `build-support/pipeline.py` performs the same bootstrap, validation, assembly,
   and artifact checks locally and in `.github/workflows/wisp-build.yml`.
3. `build-support/release.py` is an explicit, protected-CI-only extension for
   Developer ID signing, notarization and GitHub publication. Ordinary branches
   and tags produce candidates without external release steps.

The audited old script used a host Python venv, mutable transitive dependencies,
fixed `1.0` versions, opportunistic login-keychain signing and unconditional app
replacement. This pipeline packages a complete relocatable CPython prefix at
`Contents/Resources/backend/.venv` (the directory name preserves the existing
Swift launch contract; **it is not a venv**). BackendManager still invokes its
`bin/python -m uvicorn service.main:app` from the bundled backend directory.
All runtime source, YAML configuration, and bundled skills are included.

Supported output: **Apple Silicon macOS 14 or newer**. The app's inference server
and models are separate: users must install/configure oMLX themselves. Air's
optional deployment, the browser integration, model weights and runtime model
configuration are not part of the Wisp.app artifact.

## Bootstrap and local commands

Requirements: macOS on arm64, Python 3.9+ to run the stdlib driver, and Apple's
command-line developer tools with Swift 6+. Install developer tools through
Apple's normal installer or `xcode-select --install`. A local Command Line Tools
installation can build previews. CI releases require the exact Xcode and Swift
versions in `toolchain.json`; select the documented Xcode before requesting
`--strict-toolchain`.

The driver uses an existing uv only when its binary hash matches the committed
pin. Otherwise it downloads the exact official archive, reads just the known uv
member, verifies its SHA-256 **before execution**, and stores it under
`.wisp-build/tools/`. No remote installer script is executed. The fixed uv
release's Python download catalog verifies the standalone runtime archive; the
pipeline also checks the exact Python version and standalone BUILD identifier.
Python and dependency installs remain under this checkout's `.wisp-build/`.

| Command | Purpose |
| --- | --- |
| `./scripts/wisp-build --dry-run` | Print stages and pinned inputs, with no writes or downloads |
| `./scripts/wisp-build doctor` | Check platform, tools, native test sandbox and lock consistency |
| `./scripts/wisp-build bootstrap` | Install the pinned runtime and hash-checked test dependencies |
| `./scripts/wisp-build test` | Run automation contracts, Python regressions, and Swift contracts |
| `./scripts/wisp-build swift --offline` | Run Swift contracts and compile a release binary |
| `./scripts/wisp-build all` | Full verified candidate, requiring committed source |
| `./scripts/wisp-build all --allow-dirty` | Local preview of uncommitted changes; never publishable |
| `./scripts/wisp-build all --offline` | Same full gate, failing if required cached inputs are absent |
| `./scripts/wisp-build verify --output dist/<run>` | Recheck file checksums, bundle inventory and structure |
| `./scripts/wisp-build release --dry-run` | Inspect release requirements without signing or publishing |

`--output dist/<new-directory>` selects a new output directory. Existing output
is never overwritten. Use a new directory for a retry; inspect/remove failed
outputs yourself if needed. Full Git history is required (`fetch-depth: 0`).
`--test-python /absolute/path/to/python` and repeated `--suite tests/test_x.py`
are available with `test` (`--test-python` also supports `swift`), for targeted offline diagnostics; neither can
be used to create a release candidate.

## Dependencies and updates

`requirements-runtime.txt` remains the authoritative direct runtime requirements.
`build-support/requirements-test.in` adds only pytest, pytest-asyncio and Jinja2.
The two `.lock` files pin the full dependency closures with SHA-256 hashes.
Installs require hashes and binary wheels, disable inherited index/configuration
settings and keyring lookup, and use the public PyPI index. Source builds are
not silently substituted. Wheel copies, rather than cache hardlinks, keep
signing from changing cached packages.

After changing a direct requirement or toolchain pin:

```sh
./scripts/wisp-build lock
./scripts/wisp-build bootstrap
./scripts/wisp-build test
```

Review the dependency/version/hash diff and upstream licenses before committing
it. `locks.json` detects stale source requirements or edited locks. Updating uv
also requires independently verifying its official binary digest. Updating
Python requires matching its standalone build identifier and verifying all
wheel architectures/minimum OS targets. Dependency locks stabilize selection;
they are not a vulnerability scanner or a guarantee of upstream trust.

## Safe regression gate

`build-support/test-matrix.json` classifies every top-level `tests/test_*.py`.
New or removed files fail the gate until explicitly classified. Legacy tests
with `PASS`/`FAIL` counters execute as scripts, and pytest/unittest modules use
pytest in separate fresh processes. Every failure is fatal; no expected-failure
baseline is silently accepted.

Each process has scratch `WISP_HOME`, `WISP_SANDBOX_HOME`, and `TMPDIR`; **HOME
is unchanged**. The runner redirects oMLX config to empty synthetic fixtures,
uses a deterministic empty user identity, disables pytest plugin autoload and
live reminder/template opt-ins, and fixes test timezone to America/Los_Angeles
(the historical fixture timezone). macOS Seatbelt denies network activity,
subprocess execution and writes outside the scratch directory, and restricts
reads of home-directory data. A Python audit guard records accidental attempts;
even caught denials fail the suite. SQLite native I/O remains covered by
Seatbelt, including fd-relative operations. These controls protect trusted
regression tests from accidental side effects; they are not a sandbox for
malicious test code.

Two base tests are explicitly excluded pending owner-provided isolation fixes:
`test_paths_override.py` unsets WISP_HOME and imports real stores;
`test_assistant_dedupe.py` changes HOME. Cache-dependent assertions in
`test_notes_defaults.py`, the opt-in live reminder test, and optional model
chat-template tests may report skips. The gate does not claim live model or
real-app coverage. It never runs the live Mail contract or real-app fixture
seed/clear scripts.

SwiftPM currently has no XCTest targets. The gate compiles and runs the actual
Mail reply script, Mail database, source-sync label, Smart Search model, and Research Library
contract programs, then compiles Wisp's release executable. The Research Library
wire fixtures are generated from its real store inside the same guarded scratch
environment before native tests run. No `swift run` or
Wisp process is used. Backend smoke tests import every service module using
the embedded interpreter with isolated state; they never enter FastAPI lifespan
or contact oMLX. Logs retain test output and status, but scratch databases and
fixtures are deleted rather than uploaded.

## Artifact and version contract

The version is numeric `major.minor.patch` in `toolchain.json`. The default build
number is the full-history Git commit count; `--build-number` accepts Apple's
numeric CFBundleVersion form for a reviewed override. For concurrent release
lines, assign increasing release build numbers explicitly; commit count is
stable but not a global sequence across arbitrary branches. Tag `v<version>`
must match the configured version exactly. Dirty builds are labelled previews
and fail release preflight.

Each candidate directory contains:

- `Wisp.app` and a ZIP preserving its executable modes and relative symlinks;
- `SHA256SUMS` covering all adjacent artifacts;
- `bundle-manifest.json` with every bundled file's hash, size and mode;
- `dependencies.json` with installed package versions/license metadata;
- `provenance.json` with source SHA, build inputs, toolchain, step outcomes,
  signature/notarization status and reproducibility limits;
- `release-notes.md`, generated from Git changes since the previous version tag.

Validation checks required resources and privacy keys, app version/identity,
relative nonescaping symlinks, absence of local state/credentials/host venv
markers, executable modes, arm64 Mach-O slices, deployment targets, dynamic
library references and rpaths. Every native binary is explicitly signed ad hoc
before the outer candidate app is sealed. The app is never signed using an
identity found in a personal keychain. Signature errors fail the build.

The backend is copied to a path with spaces and imported there. The generated
ZIP is also extracted with Apple's `ditto`; file inventory and signatures must
survive the roundtrip. Candidate archives use sorted members and source-commit
timestamps. These are **reproducible inputs and repeatable assembly**, not a
promise of bit-identical Swift binaries across SDKs/OS images or Apple signing
runs. CI pins Xcode/Swift but hosted images still change; provenance records the
actual tools. Signed/notarized ZIPs use `ditto` to preserve tickets and extended
attributes and are verified after extraction.

## GitHub Actions and protected release setup

`Wisp build` runs on pull requests, pushes to main/codex branches, version tags
and manual dispatch. It uses an explicit macOS runner and Xcode path, full Git
history, read-only default permissions, SHA-pinned actions, nonpersistent
checkout credentials and bounded job timeouts. Cache keys include OS, arch and
lock/toolchain hashes; only uv dependency download caches are shared. Runtime
installs, signed artifacts, test state, private keys and Swift executables are
not cached. Diagnostic artifact retention is seven days; verified candidate
retention is fourteen days.

Require **Verified macOS artifact** in branch protection/rulesets before merge.
Separately retain Wisp's exact-SHA Release Auditor and Live QA gates; this CI
does not approve or merge pull requests and does not substitute for those gates.

Before enabling publication, a repository administrator must create the
**release** environment with required reviewers, prevent self-review where
supported, restrict deployment refs to version tags, and protect version tags
against unauthorized creation/updates. Configure the following **environment
secrets**, never repository files, shell history, logs or command-line examples:

| Secret | Value |
| --- | --- |
| `WISP_SIGNING_P12_BASE64` | Base64 Developer ID Application certificate and private key export |
| `WISP_SIGNING_P12_PASSWORD` | Password protecting that export |
| `WISP_SIGNING_IDENTITY` | Exact `Developer ID Application: …` identity |
| `WISP_APPLE_API_KEY_BASE64` | Base64 App Store Connect team API private key |
| `WISP_APPLE_KEY_ID` | API key identifier |
| `WISP_APPLE_ISSUER_ID` | API issuer identifier |

The job-scoped GitHub token supplies `GH_TOKEN` with `contents: write` only in
the release job. No secrets are supplied to the candidate build or fork PRs.
An environment named `release` alone does **not** create approval protections;
configuration is an administrator prerequisite and is not changed by this task.

The external path is invoked only by manually dispatching the workflow **on the
exact version tag**, setting `publish=true`, then approving its protected
release job. A normal tag push never signs, notarizes or publishes. Missing
secrets, preview inputs, mismatched tags, uncommitted source, a candidate built
with the wrong toolchain, failed checks or a tag not on `origin/main` all fail
before external signing/upload. The release job rebuilds its clean source
without restoring caches or consuming a PR-produced artifact.

Release steps: copy the candidate; import credentials into an ephemeral private
keychain without changing the user's keychain list/default; sign native code
inside-out with hardened runtime; seal the outer app using only the Apple
Events entitlement; validate signature/native imports; upload a notarization
ZIP with the team API key; require `Accepted`; staple/validate the ticket;
assess Gatekeeper; create the final ZIP and validate its extracted form; generate
final hashes; upload a draft GitHub release; publish only after upload succeeds.
Credential-operation output and exceptions are sanitized. Cleanup removes the
ephemeral keychain and private files on success/failure.

Do not add broad JIT or disabled-library-validation entitlements speculatively.
All embedded native libraries are signed with the same identity. Real Developer
ID signing, notarization and Gatekeeper acceptance must be tested in the
approved CI environment before claiming a distributable release. Permission
prompts/TCC behavior and first launch on a clean Mac are independent Live QA.

## Failure diagnostics, recovery and rollback

Every subprocess writes a named log and status/timing row under
`.wisp-build/run-*/`; errors print the relevant tail and its exact path. Candidate
publication is atomic at the GitHub draft boundary, not at the local staging
directory boundary: a failed local build can leave an incomplete output for
inspection, but no success message or complete verified release is issued.
Rerun into a new `--output` directory after fixing the cause. `--offline` fails
with missing-input diagnostics rather than silently using other dependencies.

For cache problems, remove only this checkout's `.wisp-build/uv-cache` after
confirming no build is running, then bootstrap again. Never delete global Python
installs, user state, installed apps or another Worktree's output. A cold release
job is a useful independent check against cache corruption.

For notarization failure/timeout, use the retained submission ID and Apple's
notarytool log in the protected environment to investigate; there is no automatic
retry that might duplicate an upload. On an ambiguous timeout, inspect Apple's
submission history before retrying. Fix the source or signing configuration and
run a fresh approved build. No failed candidate is published.

If GitHub upload fails, any created release remains a draft. The pipeline refuses
to overwrite an existing draft or published version. Inspect its assets and
checksums, repair/delete that draft only with maintainer authorization, then
retry. If publication's result is uncertain, inspect the remote tag/release
before doing anything else. Do not force-push or reuse a published version tag.

To recover a bad published version, distribute the last verified, notarized
artifact with its checksum or prepare a new corrective version. Replacing an
installed app remains separately authorized. This repository has no automated
user-data rollback or migration reversal; never downgrade or delete Wisp's
persistent state as part of build recovery. Keep prior release artifacts and
receipts according to the project's retention policy.

Upstream references: [uv Python distributions](https://docs.astral.sh/uv/concepts/python-versions/),
[GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[Apple notarization troubleshooting](https://developer.apple.com/documentation/security/resolving-common-notarization-issues),
[Apple Events entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.automation.apple-events),
[Apple distribution packaging](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution).

## Simulation QA integration contract

The independent Simulation QA runner is owned by its infrastructure task. Its
reviewed interface at commit `5d21cbed4651d7d588e75eceb48fd2d0262e934f` (PR #12) is:

```sh
<test-python> scripts/run_simulation_qa.py \
  --expected-sha <full-candidate-sha> --profile full \
  --safety-mode offline --report <caller-owned-report.json>
```

Integrate it only after that source lands on the selected main/base. Preserve
its file ownership. A report is acceptable only when `schema_version == 1`,
`status == PASS`, `candidate_sha == ending_sha == expected SHA`, `sha_stable`
is true, `dirty_allowed` is false, and `safety_mode == offline`. Require full
native coverage, or retain and check both halves of a split native/Python run.
Each result includes command, return code, duration, counts and captured output;
the report also identifies interpreter/dependency versions and explicit live
command exclusions. Always require the runner's zero exit code in addition to
checking its JSON. Do not reuse a report after changing source or reconciling
main.

The independent runner documents that its WISP_HOME fixtures are not a kernel
sandbox. A future caller must retain native sandbox/audit/oMLX protection when
running it. Until that protected caller is integrated, the current pipeline's
own guarded matrix is its required offline regression gate; it does not claim
an independent Simulation QA verdict.

## Checkpoint validation record — 2026-09-09

This incomplete checkpoint was reconciled without conflicts to
`origin/main` at `51fa3ec937df7a19961fbb2103a7654bb058ec74`, from the task's
original base `c3b5afe26a1744dd7927586262be0533322443af`. Only pipeline-owned
paths were edited. Branch: `codex/wisp-production-pipeline`.

The offline audit used an existing **Python 3.14.3** environment read-only and
local **Swift 6.2 Command Line Tools**. These are not the intended pinned
Python 3.13.14 / Xcode 16.4 release toolchain; this record does not claim clean
bootstrap or strict-release reproducibility.

Commands and outcomes on the reconciled source plus the checkpoint diff:

- `python3 -B -m unittest discover -s tests/build_pipeline -q`: **27 passed**.
- `./scripts/wisp-build test --offline --test-python <existing-python>`:
  **66 Python suite processes passed; four failed; two excluded**. The manifest
  covers 72 files (39 script runners, 31 pytest runners, two exclusions).
  The command correctly returned nonzero. Diagnostics: `.wisp-build/run-d7aq95vx`.
- A focused run with `--suite tests/test_research_library.py --suite
  tests/test_research_mode.py`: **12 library tests plus four subtests and 66
  research-mode tests passed**, followed by all five Swift contracts.
  Diagnostics: `.wisp-build/run-34docmly`.
- `./scripts/wisp-build swift --offline --test-python <existing-python>`:
  **release build and five native contract programs passed**. Mail reply script
  compilation performed no Mail actions; MailDB had 11 checks, source-sync had
  eight, Smart Search had 50, and Research Library had 95.
  Diagnostics: `.wisp-build/run-r_g2wtux`.
- `all --dry-run` and `release --dry-run`: passed without downloads or writes.
- Shell syntax, Python syntax, YAML parsing, workflow permission/trigger/SHA-pin
  structure, and `git diff --check`: passed. **Upstream action pin existence and
  exact runner/toolchain availability have not been checked.**
- `bootstrap --offline`: deliberately failed before downloads because locks are
  absent. Local release CLI/contract tests rejected execution before any
  credential operation. Synthetic artifact, relocation-link, checksum, native
  deployment-target and secret-timeout contracts passed; **no complete Wisp.app
  artifact or signed distribution has been built or validated**.

The four failing existing routing suites are retained as fatal gates:

| Suite | Observed failures |
| --- | --- |
| `test_alias_reachability.py` | Eight own-alias routes intercepted by unrelated rules |
| `test_forced_step_withholding.py` | Two assertions expecting the former reorganize route/run_shell availability |
| `test_router_scoping.py` | Missing route/alias coverage for clear_memory and search_conversations |
| `test_semantic_routing.py` | Expects static-core fallback while current implementation returns lexical fallback |

The two exclusions are `test_paths_override.py` and
`test_assistant_dedupe.py`, with reasons in the matrix. The simulation runner's
unmerged PR #12 isolation changes are a future integration dependency. Routing
repairs belong to the appropriate service/test owner, not this build workstream.

Remaining required work: obtain direct public-download approval in this task;
generate and review actual runtime/test locks; independently verify upstream uv
and action pins; exercise clean bootstrap and the pinned toolchain; assemble,
relocate and extract a real complete bundle; validate its checksums/provenance;
resolve the four fatal regression suites through their owner; then complete the
normal independent exact-SHA audit and QA gates. No real Apple signing,
notarization upload, release publication, installed-app replacement, deployment,
real outbound tool action or user-data mutation was performed.
