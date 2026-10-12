# iPhone foundation checkpoint

Owner chat: 01a12778-56ee-74e0-81bb-18b7b906bd3a. Sole scope ios/**.
Worktree: /Users/adijain/.codex/worktrees/wisp-iphone-port/MOE_Project
Branch: codex/wisp-iphone-port
Base: ed361c0c3b2bce2bd8daf7242b805e3894572f7a

Implemented: standalone SwiftUI app; deterministic synthetic/source-import inspector;
explicit unsupported/partial/unknown-data states; EventKit/Contacts permission
boundary source (not connected to UI); disabled on-device FoundationModels adapter
with exact full-excerpt validation; read-only shared workflow20 v1 schema consumer
that does not decode expect/reference facts into execution inputs.

Current evidence: pre-freeze generic simulator build PASS; 22 Swift test functions PASS.
Shared consumer decoded 597 cases, all explicitly unsupported with zero effects.
Shared grade exited 1 (0 semantic completions, 597 safe-handling results).
Implementation complete for initial foundation; final exact-SHA freeze/gates pending.
Xcode 27.0 (27A266a), Swift 6.4, iOS simulator SDK 27.0 available.
`simctl list devices available` initially failed in sandbox; escalated retry detected
CoreSimulator version update and returned no available devices. No simulator/device
installation, model run, permission prompt or native data read performed.

Planned validation: `swift test --package-path ios --scratch-path /private/tmp/wisp-iphone-build/core`;
unsigned generic simulator `xcodebuild -project ios/WispPhone.xcodeproj -scheme WispPhone -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' -derivedDataPath /private/tmp/wisp-iphone-build/app CODE_SIGNING_ALLOWED=NO build`;
read portable cases from Mac worktree when published; output contract-only unsupported
observations and grade independently. Fixture decoding is not semantic accuracy.

Dependencies: shared CONTRACT.md SHA256 94ed633be54f5610115c86cb0e2f7f51ee3c8a8e01c3ae8cd83d547575136f61;
Mac cases being written. No writes to their paths. No source overlap with Catch.
Outbound coordination auto-review rejects status sends (approval policy never);
Root will pull this checkpoint. Do not retry or bypass rejected sends.

Shared corpus published: 597 cases, 20 families. Approval includes approved_effects; iOS emits no effects or approvals. Formal release audit/specialist QA still require Orchestrator exact-SHA registration.
