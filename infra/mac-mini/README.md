# Primary Mac and future mini provisioning

This candidate prepares a primary Mac and stages a future mini runtime. It never
switches inference roles or enables proactive work. Validation for this change is
synthetic: no live Keychain, Tailnet, firewall, oMLX, system setting, installation,
or deployment operation was performed.

## Commands and effect boundaries

Run `scripts/wisp-node-prep --help` with Python 3.13 or 3.14. The default mode
requires synthetic fixture input for posture checks. `--live` permits system
reads; `--live --apply` is required for the three mutation commands. Never combine
fixtures with live/apply flags. Reports contain fixed checks and statuses only;
raw command output, Serve configuration and exception details are suppressed.

| Command | Default | Explicit live behavior |
| --- | --- | --- |
| `init-primary` | Lists the three planned accounts; no reads or writes | `--live` checks the installed helper; `--live --apply` installs a stable native helper and initializes Keychain entries |
| `policy-render` | Renders an additive policy, warns/returns failure for unresolved access | Does not publish or replace Tailnet policy |
| `preflight` | Evaluates the fixture and complete policy | Reads primary/peer identity, Keychain presence, effective config, firewall, oMLX listeners, remote posture through Tailscale SSH |
| `activate` | Validates the fixture plus pinned versioned bundle | With `--apply`, streams only mini credentials and bundle to stage an inert runtime and disabled launchd templates |
| `doctor` | Same invariant checks as preflight | Reports current posture; does not repair or enable anything |
| `rollback` | Verifies target identity | With `--apply`, unloads only recorded Wisp jobs, restores known primary local bindings and disables proactive polling; preserves keys, state, releases, firewall and oMLX |

The JSON examples are fictional. Replace the target with the mini's exact
MagicDNS FQDN, stable node ID, Tailscale IP, a designated existing non-root macOS
account, and its authorized Tailnet user identity. The primary IP is fixed at
`100.94.211.115`. Never use `root`, an SSH wildcard, or `autogroup:nonroot`.

Safe checks from a checkout:

```sh
scripts/wisp-node-prep init-primary
scripts/wisp-node-prep preflight --plan infra/mac-mini/plan.example.json --policy infra/mac-mini/policy.example.json --fixture infra/mac-mini/preflight.fixture.json
scripts/wisp-node-prep doctor --plan infra/mac-mini/plan.example.json --policy infra/mac-mini/policy.example.json --fixture infra/mac-mini/preflight.fixture.json
scripts/wisp-node-prep rollback --plan infra/mac-mini/plan.example.json --policy infra/mac-mini/policy.example.json --fixture infra/mac-mini/preflight.fixture.json
```

For fixture activation add `--bundle /path/to/reviewed-mini.tar.gz` and
`--bundle-sha256 <reviewed archive SHA-256>` and, for live staging,
`--source-sha <reviewed exact candidate SHA>`. Applied rollback also requires
`--source-sha <reviewed rollback-code SHA>` and a clean checkout at that exact SHA.
Rollback reads its receiver from immutable Git blobs, rechecks the checkout before
SSH, and verifies the remote release ownership record before unloading jobs. The
rollback-code pin may be newer than the recorded release; it authorizes recovery
code, not a different target or ownership record. Obtain that hash from the reviewed
mini release handoff. A digest is an integrity pin, not a signature or proof of
publisher identity. Do not substitute an unreviewed downloaded bundle.

## Credentials and local compatibility

The file-based macOS Keychain service is `com.wisp.inference`:

| Account | Native/backend reference | Scope |
| --- | --- | --- |
| `local-omlx` | `WISP_LOCAL_OMLX_KEY` | That Mac's loopback oMLX only |
| `mini-inference` | `WISP_MINI_INFERENCE_KEY` | Mini HTTPS gateway |
| `mini-node` | `WISP_MINI_NODE_KEY` | Mini HTTPS results API |

All values are canonical 64 lowercase hexadecimal characters representing 32
bytes. Remote credentials use Security.framework cryptographic randomness.
Initialization imports the **already configured** local oMLX token only if it
has that format, validates existing entries, and never rotates a key. An absent,
short, differently encoded, denied or mismatched local key blocks initialization
before credential writes. A local authentication migration, if needed, is a
separate explicitly authorized operation; this CLI does not change oMLX auth or
persist a new token in its settings. Legacy installations without an injected
local credential retain their existing oMLX settings fallback.

The native launcher reads fixed accounts through Security.framework. It removes
inherited values for the three bridge names and injects only validated entries.
The Python configuration package immediately consumes/removes those names from
`os.environ`, so ordinary shell, skill and MCP children cannot inherit them.
Only fixed credential references resolve from the private store. A remote
endpoint cannot request the local key through either `local_omlx` or the fixed
environment alias. Unrelated legacy `env:` references retain their behavior.

`init-primary --live --apply` uses the installed `/Applications/Wisp.app` as an
explicit trusted reader and places a stable helper at
`~/.moe/provisioning/wisp-keychain-helper`. Keychain ACLs name the helper and app;
there is no trust-all setting. Explicit initialization can tighten an owned,
non-symlink `~/.moe` directory from normal umask-022 mode 0755 to 0700. Other unsafe
permissions, owners, links, or unknown nested provisioning files are refused.

Initialization builds and verifies a fresh helper/receipt pair, preserves unrelated
receipts, then atomically exchanges the complete provisioning directory. The old
pair remains in a private `.helper-previous-*` recovery slot. Publication and the
new helper's initialization, signature/ACL checks, and status acceptance share one
lock with binding receipt writes. A secret-free, directory-synced
`~/.moe/.helper-transaction.json` blocks credential use during interrupted recovery.

If acceptance fails, the exact old directory is atomically restored and its modes,
ownership, inode, file set and hashes are checked without executing the old helper.
For a first installation, the rejected new pair moves out of the active path.
Both states are retained. Because initialization may have created some missing
Keychain entries before failing, the recovery marker remains even after successful
file restoration. Do not delete the marker or recovery slot to retry: a separately
authorized recovery must verify the recorded inventories and Keychain/ACL state.
No automatic credential deletion, ACL widening, rotation, or forced migration occurs.
Helper/app replacement, signed identities, locked Keychain behavior and ACL
access across executables require isolated native qualification before shipping.
The file-based ACL APIs produce macOS deprecation warnings; moving to a signed
shared access group is a separate signing/entitlement decision.

The primary's local key is never transported. Remote import accepts exactly the
two mini accounts through a pipe, checks distinctness, and refuses rotation or
reuse of the mini's local key. After staging, the mini's own trusted helper has
an `init-mini-local` operation that imports its existing canonical local oMLX
token into its own Keychain, with the mini launcher as trusted reader. It creates
no new oMLX token. This is a separate live credential step, not part of staging.
Do not run it during Worktree validation. Qualify authenticated local oMLX and
both mini services before considering service enablement.

Secrets never appear in generated YAML, plists, command arguments, reports or
logs. Native helper export is a machine pipe operation; do not invoke it by hand
or redirect it. The mini launcher passes just the gateway's two keys or the node
API's one key in memory and suppresses arbitrary child stdout/stderr. This is not
protection against code already executing as the same OS user, a debugger, or
an administrator. Do not enable environment/local-variable dumps in diagnostics.

## Tailnet prerequisites and additive policy

Install/configure the **open-source CLI `tailscale` + `tailscaled` variant** on
the mini. The macOS App Store and standalone system-extension variants cannot
host Tailscale SSH. This implementation expects the Apple Silicon Homebrew CLI
at `/opt/homebrew/bin/tailscale` and Python at `/opt/homebrew/bin/python3.13`.
It does not install/enroll Tailscale, create users, enable SSH, or approve devices.

An administrator must enable device approval, approve the exact mini, assign
`tag:wisp-inference`, enable MagicDNS and HTTPS, and configure Tailscale SSH.
Keep the primary user-owned so SSH check-mode reauthentication can work. HTTPS
certificate issuance exposes the device FQDN in public certificate-transparency
logs; choose a non-sensitive machine name. These control-plane prerequisites
must be reviewed in the admin console; a local status response does not prove
the global device-approval setting or that an exported policy is currently
published. Preflight does not claim to audit those admin settings.

Export the **complete** existing Tailnet policy as strict JSON (not HuJSON with
comments), then use `policy-render --plan PLAN --policy COMPLETE_POLICY`.
The renderer preserves existing entries and adds exactly:

- Network grant: `100.94.211.115` → `tag:wisp-inference`, TCP 22/443/8443.
- SSH rule: designated Tailnet user → mini tag, `action: check`,
  `checkPeriod: always`, one designated non-root macOS account.
- TCP/UDP network assertions and SSH check/root-denial assertions.

SSH rules cannot use literal IP sources. The network grant and identity-based
SSH rule must both match. Add actual denied peer IPs and alternate account tests
from your Tailnet inventory before publishing. Never describe sample test peers
as proof against every real device.

**Grants are additive.** A narrow rule cannot override a wildcard ACL, broader
CIDR, another matching tag/group/IP set, broader SSH account, or Funnel attribute.
The conservative checker blocks *all* extra grants/SSH rules, nonempty legacy
ACLs/node attributes, changed tag ownership and unknown policy syntax. This may
block genuinely unrelated rules: human review must prove disjointness and extend
the checker with evidence, rather than adding a bypass flag. Do not replace your
entire Tailnet policy with this example to make a check pass.

Publish only after the full policy passes Tailnet's own tests and review. Keep
the exported policy used by live preflight synchronized with that approved
publication. The CLI does not call the admin API or alter policy.

## Serve, ports, firewall and disabled services

The primary and mini oMLX must remain bound to loopback on port 8000. Preflight
uses the kernel TCP socket inventory and fails on wildcard binds or unknown state.
Disabled staging additionally requires no listeners on ports 8765 and 8766,
including other users' listeners; the receiver checks again during staging.
Both firewalls must already be enabled; the only firewall command is
`socketfilterfw --getglobalstate`. No command enables, disables or resets it.

After future service qualification, an administrator may configure **Serve**:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve --bg --https=8443 http://127.0.0.1:8766
```

These are guidance only and are not executed by provisioning. Never run
`tailscale funnel`; do not grant the `funnel` node attribute. The probe checks
`AllowFunnel` recursively, including foreground sessions, and treats unknown
Serve schemas as inconclusive. Check any separate Tailscale Services inventory
in the admin console too. Backend ports 8000/8765/8766 remain inaccessible through
the proposed network grant; only Serve terminates HTTPS on 443/8443.

Activation verifies the exact peer hostname/IP/node ID/tag, online status and
advertised SSH host keys, then rechecks identity immediately before secret
access. `tailscale ssh` verifies advertised host keys. Interactive check-mode
authentication may need operator completion; timeout/failure blocks staging and
never prints remote diagnostics.

The receiver verifies archive and per-file hashes, version, exact runtime file
set, service routes/ports/argv/keys, and rejects duplicate, noncanonical,
traversal and non-regular archive entries. A private release directory under
`~/.wisp-mini` is identified by runtime digest, provisioning assets and node ID.
It accepts only an offline runtime artifact built for the reviewed candidate SHA
by `build-support/mini_artifact.py`; source-only bundles cannot be staged. CI uses
the pinned Python archive and compiler plus a complete hash-locked wheel closure.
Dependencies install with `--no-index --require-hashes --only-binary=:all:` during
artifact creation. The mini performs no compilation, dependency resolution, or
network installation. The `venv` directory contains a relocatable standalone
Python; only `python -m` entrypoints are supported.

The receiver materializes the complete artifact into a private temporary directory,
checks helper signatures/protocol versions, imports, and synthetic runtime health,
then atomically publishes it. Repeat staging reconstructs the expected inventory
from the externally pinned bundle and checks every file's digest, type and mode.
Unknown directories, marker-only state, and changed runtime files are refused.
Disabled launchd templates remain inside the release; no job is installed or loaded.
Rollback checks both
`gui/<uid>` and `user/<uid>` launchd domains and unloads only recorded labels,
restores known primary local bindings, and disables primary proactive polling.
Restart the Wisp backend after a manual/runtime configuration rollback to clear
any already-running poll/request. Unknown roles require manual resolution;
rollback does not guess a local model. It retains user state and credentials. Applied rollback returns exit code 2 and
`status: restart_required`; it does not claim the running backend has reloaded
its cached configuration. This remains incomplete until that restart is verified.

## Release evidence still required

Synthetic fixture runs do not qualify real Tailscale SSH, Keychain ACLs, device
approval, HTTPS issuance, dependency installation, native service lifetime,
installed-app packaging, or model health/performance. These are explicit
specialist gates for a separately approved isolated/live environment, followed
by the Release Auditor at the exact candidate SHA. Do not deploy or enable
services from a successful fixture report.

References: [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh),
[grants](https://tailscale.com/docs/reference/syntax/grants),
[policy tests](https://tailscale.com/kb/1337/policy-syntax),
[Serve](https://tailscale.com/docs/reference/tailscale-cli/serve),
[device approval](https://tailscale.com/docs/features/access-control/device-management/device-approval),
[HTTPS](https://tailscale.com/docs/how-to/set-up-https-certificates),
[Apple Keychain ACLs](https://developer.apple.com/documentation/security/access-control-lists).


Repair qualification: native reads inspect decrypt/ANY ACLs and require the exact
trusted reader identity set before readiness or reuse. Unknown/broad existing ACLs
are refused, never silently rewritten. Primary helpers require a private owner,
regular executable, source/hash receipt, valid signature, and protocol v2. Explicit
staging records a separate reviewed host/node receipt: model configuration cannot
swap native node/inference credentials or redirect them to another HTTPS origin.
Local Swift 6.2 development artifacts cannot satisfy the CI Swift 6.1.2 release
contract; those artifacts are marked unqualified and cannot be activated.


## Isolated signed ACL qualification fixture

`python3 build-support/isolated_acl_fixture.py --output /private/tmp/acl-report`
compiles and ad-hoc signs four temporary executables, verifies their signatures,
and writes a report. This default mode **does not run any Keychain operation**;
its qualification status is `UNAVAILABLE_NOT_EXECUTED`, even when compilation
passes. CI performs this compile-only check and retains its report in diagnostics.

Execution with `--ephemeral-macos` is reserved for a separately reviewed disposable
macOS VM/runner, with no user data or imported signing identities. The flag is an
operator isolation assertion, not a sandbox guarantee. The driver requires a clean
exact candidate and supported macOS/architecture. Ordinary local simulation does
not contain the system `securityd` service and must not run this mode. No execution
qualification is claimed by the compile-only report.

The fixture creates a unique private temporary Keychain and scopes every read/write
to its explicit reference; it never queries or changes default/search lists or calls
production initialization. Only synthetic values pass through stdin. It checks the
original reader, unrelated-reader denial, signed replacement refusal, restoration,
explicit fixture-only ACL rebinding, and locked-store denial. Fixed typed outcomes
distinguish policy/OS denial from unavailable or isolation failures; inconclusive
results block qualification. The report records exact SHA, OS build, architecture,
compiler and ad-hoc signature identities. A pass covers only these synthetic ad-hoc
identities, not Developer ID upgrades, login Keychain behavior or production rollout.

Generic remote readiness reads also have deadlines and pre-parse byte limits:
64 KiB for health, 1 MiB for model/status inventory. Compressed, oversized, malformed,
or interrupted responses cannot advertise readiness; optional tool-free generation
may choose the existing local fallback before any generation request is sent.
