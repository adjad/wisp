# MacBook and mini: architecture and remote operations

## Current status

This describes the prepared design and its remaining deployment requirements.
It is not evidence that a mini is installed, reachable, or ready for unattended
use. The current security follow-up still needs complete validation,
and independent review. No live installation or oMLX update is enabled here.

The shipped model contract contains no qualified models, so inference remains
unavailable. Background jobs and provider connectors are disabled. The arrival
adapter is a simulation; a working live supervisor and recovery integration are
still required. Read [RUNBOOK.md](RUNBOOK.md) for storage and resource details,
and [the provisioning guide](../infra/mac-mini/README.md) in the source checkout
for host and credential setup.

## Terms

| Term | Meaning |
| --- | --- |
| Tailscale / Tailnet | The private network connecting the approved devices. Network membership alone does not grant Wisp access. |
| Gateway | The mini service that authenticates inference requests and applies resource limits before forwarding them to oMLX. |
| oMLX | The process that runs the language model. It has no authority to execute MacBook tools. |
| Node | The mini service that stores and serves background results. Its scheduler is disabled by default. |
| TCC | macOS permissions for personal data and protected capabilities, such as Calendar access. |
| Supervisor | The local component responsible for starting, observing, and switching approved service processes. |
| Release receipt | A record identifying the exact installed version and file hashes. It is not proof that a model works well. |

## Responsibilities

| MacBook | Mini |
| --- | --- |
| Shows the UI, conversations, approvals, and results. | Runs isolated oMLX inference behind the gateway. |
| Reads authorized local personal data through macOS permissions. | Stores model weights, bounded caches, and owned node state. |
| Executes tools and effects through local policy and approval checks. | Returns model output or presentation results; cannot authorize effects. |
| Holds primary credentials and selects approved endpoints. | Holds separate gateway, node, and upstream credentials. |
| May use available local inference under the existing fallback rules. | Provides versioned service installation and recovery preparation. |
| Initiates remote administration and reviews recovery decisions. | Must expose independent administration even when Wisp or oMLX is stopped. |

Mail, Messages, Calendar, and their credentials stay on the MacBook because
their permissions and effects belong to that machine and user session. A model
reply is untrusted input to the local tool policy. Moving inference to the mini
does not move permission to send mail, change a calendar, or run a shell.

## Connections and data

```text
MacBook UI -> local Wisp backend -> local permissions / approvals -> local effects
                     |
                     | private Tailscale network
                     +-- HTTPS 443 ---> Serve -> gateway 127.0.0.1:8765
                     |                              |
                     |                              +-> oMLX 127.0.0.1:8000
                     +-- HTTPS 8443 --> Serve -> node 127.0.0.1:8766
                     |
Remote administration+-- SSH 22 ----> host management -> status / recovery
                                      independent of gateway, node, and oMLX
```

Ports 443 and 8443 require HTTPS certificate verification and distinct service
credentials. Port 22 is for the exact approved host and designated non-root
account, with SSH host identity checks and Tailnet access policy. Internal ports
8000, 8765, and 8766 bind only to loopback; they must not become LAN or public
listeners. Tailscale Serve terminates HTTPS on the mini. Public Funnel access
is not part of this design.

| May cross the private connection | Must not cross through inference or results |
| --- | --- |
| The selected request's text, conversation context, and tool descriptions needed for inference. This can contain personal information if the local workflow includes it. | Whole personal-data stores, unrelated files, Keychain contents, or provider credentials. |
| Model output and explicitly prepared portable snapshots/results. | Automatic Mail/Messages/Calendar access, arbitrary commands, or authority to execute effects. |
| Service authentication to its exact intended service; approved provisioning uses a separate authenticated path. | Credentials in logs, command arguments, environment values, reports, or model prompts. |
| Bounded management requests and redacted status. | Raw secrets or database contents in routine diagnostic output. |

The private network, HTTPS, service credentials, process identity, and local
approval policy protect different boundaries. None substitutes for the others.
Credential startup transfer uses a private inherited pipe in this candidate;
real signing and Keychain access still need deployment validation.

## Normal and background work

For a normal request, the MacBook selects an approved endpoint. The gateway
checks authentication and resource admission, then forwards inference to local
oMLX. The MacBook displays the response and applies its own policy to any tool
request. The mini never executes those MacBook actions.

For future enabled background work, the node records immutable occurrences and
results. The MacBook polls HTTPS 8443 and deduplicates by result identity. A
rotated database restore invalidates old cursors. The candidate resets only the
affected node cursor after the exact authenticated invalid-cursor response and
retries once; other failures do not reset progress. Jobs, provider fetching,
and automatic effects remain disabled. Portable snapshot processing alone does
not enable a live provider.

## oMLX updates and rollback

The user's update policy prefers the newest official Release Candidate (RC)
whenever one is available; it uses the latest stable release only when no RC is
available. The user accepts the risk of an incompatible or poor-performing RC.
There is no preactivation canary, synthetic prompt, model/API compatibility,
performance, or qualification testing in this update policy.

The required update mechanism verifies release origin, artifact authenticity,
and download integrity; installs an immutable versioned tree; records its exact
version and complete tree hash; checks disk admission; and retains the active
and previous versions. A bounded atomic supervisor switch changes versions.
Automatic rollback is limited to the new process failing to start, stay running,
or bind to the supervisor. A running process is not evidence of compatibility.
Normal use may reveal a problem requiring an explicit remote rollback.

This lifecycle is a requirement, not an enabled updater in the current candidate.
Independent remote status and rollback must work before updates are enabled.
Existing resource limits remain in force; they do not imply that an update has
passed compatibility testing. Do not claim that a new version works with Wisp
until that behavior has actually been observed in normal use.

## Failure and recovery

| Failure | Expected behavior and remote path | Remaining condition |
| --- | --- | --- |
| Mini unavailable | Report unavailable; eligible tool-free work may choose local fallback before a generation request is sent. Do not promise seamless migration of an in-flight request. | A usable local model and resources must exist. |
| Wisp or oMLX stopped | Use independent SSH diagnostics; inspect the recorded process and request a bounded restart or rollback. | SSH, the host, and the relevant user session must remain available. |
| Bad oMLX update | Apply the process-start/bind rollback rule above; use explicit rollback for problems discovered in normal use. | Version-switch implementation and real supervisor integration are outstanding. |
| Credential failure | Refuse requests and inspect redacted credential status. Restore only an independently authorized helper/credential binding. | Stable signing, ACL adoption, and locked-Keychain behavior need real-host validation. Status alone is not credential recovery. |
| Disk pressure | Refuse growth before spending the reserve; inspect usage and explicitly reclaim only eligible inactive releases. Preserve models, state, current/previous versions, and recovery evidence. | Cleanup race/crash repairs require final validation. Ambiguous staging directories are not automatically deleted. |
| Database incident | Stop the producer; inspect main/WAL state; recover to a new directory with original evidence preserved. Coordinate the later state switch and cursor replay. | Recovery/restart serialization and source identity repairs require final validation. There is no automatic destructive repair. |
| Missing release ledger | Stop release mutations; restore an independently approved sequence floor with replay-safe incident authorization. | Recovery authorization replay repair requires final validation. Never delete the initialized sentinel to reset history. |
| Restart or power loss | Reconnect, inspect actual state and incomplete operations, then resume only verified recovery. | Automatic startup, user-session availability, and crash recovery must be demonstrated on the mini. |
| Tailnet unavailable | Wisp remote inference and its SSH management path are both unavailable. Use an independently established alternate administration route, or someone on site. | An alternate route is not implemented or assumed by this package. |

The management receiver has preparation for diagnostics, existing-job restart,
Wisp rollback, credential status, database doctor/recovery, ledger recovery, and
release inventory/reclamation. These capabilities are under review and do not
yet establish unattended readiness. Wisp rollback is not the same operation as
the proposed oMLX version rollback. Database doctor currently depends on a valid
installed runtime and release inventory; repair ledger/receipt issues first.

## When someone may need to be on site

No software recovery path can repair loss of power or all network connectivity.
Disk/hardware failure and a startup unlock screen that blocks remote access may
also require physical access. Before leaving, arrange a trusted on-site contact,
record the recovery procedure, and consider power protection and a separately
secured management route. Verify the actual FileVault, reboot, login, sleep, and
Keychain behavior on this mini; do not assume an SSH session will survive them
or weaken encryption to make the checklist pass.

## Arrival and routine checklist

Before relying on the mini while away:

1. Complete security repairs, all required checks, artifact verification, and
   independent review for the exact release being installed.
2. Verify actual hardware, model/storage placement, startup free space, resource
   enforcement, signing identity, Keychain access, and the live supervisor.
3. Verify full Tailnet policy against an authentic, fresh inventory. Confirm
   exact host/account access and denial for unrelated devices and accounts.
4. From a different network, verify SSH identity and access, then stop Wisp and
   oMLX and demonstrate redacted diagnostics and approved recovery over SSH.
5. Exercise reboot, locked-session behavior, failed startup, rollback, low disk,
   and backup recovery with isolated state. Record what still needs local access.
6. Keep jobs, provider fetching, and updates disabled until their respective
   implementation and deployment requirements are met.

During routine operation, check independent host/service status, free space,
backup freshness, retained versions, credential readiness, and incomplete
operations. Keep the previous release and recovery instructions accessible.
Treat a process being alive as liveness only; investigate actual user-visible
failures without claiming compatibility from a health endpoint.
