# Wisp iPhone foundation

A standalone SwiftUI iPhone app with synthetic data, explicit text import/paste,
source excerpts, permission-aware native boundaries, and an on-device inference
adapter. This is an initial port foundation, **not completion of the twenty
natural-language workflows**. Native integration and local inference are disabled
in the app. No account, model service or Mac resident server is required.

Open `WispPhone.xcodeproj` in Xcode. Deployment target is iOS 17; the optional
Foundation Models adapter is guarded for iOS 26 and builds only when the SDK has
the framework. The checked-in project needs no third-party dependencies, account,
provisioning, network service, model weights or project generator to compile.
`scripts/generate_project.py` regenerates the project using Python's standard
library after adding/removing Swift files.

The Workspace tab starts with fictional mail, messages, calendar, reminders and
notes. Choose Inbox overview and inspect to see three full excerpts and stable
source IDs. Search fields are explicit literal filters, not a validated intent
parser. Calendar's day view uses the fixture's local timezone and overlapping
event intervals. Every view states its available source scope.

Paste and UTF-8 import replace the workspace with a partial, in-memory source.
Imports do not infer contact identity, event times or unread status. Reset discards
the import. There is no disk persistence, account sync or directory scan. A share
extension is not included yet. Imported text is displayed as data, never executed.

## Validate without personal data or model execution

```sh
swift test --package-path ios --scratch-path /private/tmp/wisp-iphone-core
xcodebuild -project ios/WispPhone.xcodeproj -scheme WispPhone \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /private/tmp/wisp-iphone-app CODE_SIGNING_ALLOWED=NO build
```

From a clean committed checkout, `python3 ios/scripts/verify.py --expected-sha
<full-sha> --artifacts <outside-repo-directory> --fixtures
<shared-workflow20-cases-directory>` captures command logs and head/tree bindings.
It does not boot a simulator, install an app, request native permissions or execute
inference. A generic simulator compile does not establish device feasibility.

## Shared workflow20 contract

Use the Mac-owned `docs/workflow20/CONTRACT.md` and
`tests/workflow20/cases/*.json` directly, read-only. The CLI accepts one or more
fixture files and emits JSONL observations:

```sh
swift run --package-path ios --scratch-path /private/tmp/wisp-iphone-core \
  workflow20-ios /path/to/shared/cases/actions.json /path/to/shared/cases/read_query.json
```

The decoder deliberately excludes `expect` and scenario reference `facts` from
execution input. Approval observations include exact `approved_effects`, which
are empty in this foundation. The contract adapter returns `unsupported` for all
semantic cases with zero effects. Source-inspector results are never mapped to
semantic success. The shared grader correctly fails completion for this adapter;
safe abstention and successful schema consumption are separate evidence.

A future semantic adapter must derive facts and actions from source records, keep
approval bound to exact payloads, and measure heldout outcomes without consuming
the reference oracle. No model accuracy or latency has been measured.

## Local inference boundary

`AppleLocalInference` selects `SystemLanguageModel.default` explicitly, checks
availability before each request, exposes no tools/cloud fallback, caps payloads,
checks cancellation, and validates returned whole excerpts and IDs against input.
It cannot create action parameters or new facts. Default scope is nil and fails
before touching the model. The app does not instantiate or call the adapter.
Enabling a runtime requires a separately approved resource profile and supported
hardware; constructor source is not proof of inference quality or execution.

See [capability matrix](docs/CAPABILITIES.md) and [handoff](docs/HANDOFF.md).
