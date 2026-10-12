# iPhone foundation handoff

Owner: 01a12778-56ee-74e0-81bb-18b7b906bd3a, sole writer ios/** under Orchestrator ACK.
Worktree: /Users/adijain/.codex/worktrees/wisp-iphone-port/MOE_Project.
Branch: codex/wisp-iphone-port. Known base and fetched origin/main:
ed361c0c3b2bce2bd8daf7242b805e3894572f7a.

Delivered: standalone compilable iPhone SwiftUI shell, synthetic/demo and selected
text-import source inspector, stable provenance, explicit source coverage and
unsupported actions, permission-aware EventKit/Contacts source boundaries,
disabled on-device FoundationModels integration, shared workflow20 v1 decoder and
contract-only observation CLI. No shared Mac/Catch source edits.

Files changed: all under ios/: Package.swift, .gitignore, App/*.swift and
Info.plist, Sources/WispCore/*.swift, Sources/WorkflowContractRunner/main.swift,
Tests/WispCoreTests/*.swift, WispPhone.xcodeproj/project.pbxproj and shared scheme,
scripts/generate_project.py, scripts/verify.py, README.md, docs/*.md.

Pre-freeze checks: unsigned generic iOS simulator build PASS with Xcode 27.0
(27A266a), SDK 27.0, arm64 and x86_64; Swift 6.4 offline tests PASS (22 test
functions, parameterized state/family cases included). First supported synthetic
flow: overview shows fictional mail, message and calendar excerpts with source
IDs. No native sources, permissions, models or accounts used.

Shared contract: SHA256 94ed633be54f5610115c86cb0e2f7f51ee3c8a8e01c3ae8cd83d547575136f61,
including approval.approved_effects. Read-only fixture hashes:
- actions.json: 0ebadf9ca89a168f155efa512e9a32b1908a22437ea88a2887742284b5139b1e
- read_query.json: 680d4258e62e3f88c77c6aeba1dafc8dcb0cc26a8db843e58550bf341cfdbbec

Shared corpus consumption PASS: 597 observations, all unsupported, zero effects,
zero facts, no inferred approvals and no inference executed. Independent shared
Mac grader exits 1 because semantic completion is unsupported: 0 completion
passes, 597 safe-handling results. This is a truthful product gap, not a benchmark
pass or model measurement. Grader metadata identifies its own Mac dirty worktree;
it is not exact-SHA iPhone mechanical evidence.

Initial simctl inventory failed under sandbox; escalated retry returned no available
simulator devices after detecting a CoreSimulator version update. No simulator was
booted and no physical device was provisioned or installed. AppIntents metadata
extraction is skipped because the app defines no AppIntents. Device/UI/runtime and
local inference feasibility remain unmeasured.

Final freeze procedure: fetch main (done, unchanged at known base), commit and
nonforce push this ios-only branch, create/attach draft PR, then run
`python3 ios/scripts/verify.py --expected-sha <candidate> --artifacts
/private/tmp/wisp-iphone-final-<candidate> --fixtures <shared-cases-directory>`.
Exact SHA/tree/changed-file list, result logs, PR and CI status will be in the
external artifact handoff, leaving the frozen source checkout clean.

Required before qualified readiness: Orchestrator registers exact SHA and triggers
independent Release Auditor plus specialist synthetic QA for platform permissions,
inference boundaries and new build target. No formal audit/QA was dispatched by
this builder. Existing required PR CI remains required; local foundation checks
cannot replace it. No main merge, release, app replacement or physical install.

Incomplete: natural-language twenty-family adapter, real inbox/provider access,
calendar/reminder writes, contacts lookup, live map/weather, drafts/sends/scheduled
delivery, persistent memory/queue, file organization, share extension, signed device
validation, permission/UI QA, actual model accuracy/latency and performance.
See CAPABILITIES.md for all twenty families. No promise of desktop parity.

Coordination: Root reads local handoffs after outbound cross-chat sends were
automatically rejected with approval policy never. No retries or bypasses after
Root's instruction. Mac owns tests/workflow20/**, docs/workflow20/** and shared
runner; consume these read-only. No ownership conflict with Catch or existing
service/SwiftMac paths. Future semantic changes require new exact-head evidence.
