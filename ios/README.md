# Wisp for iPhone

A SwiftUI iPhone app for Wisp with three places to run, switchable from the
chat's title bar:

| Mode | Where turns run | What it can do |
| --- | --- | --- |
| **Mac** | Wisp on your Mac, through the phone gateway over Tailscale | Everything Wisp does on the Mac: mail, messages, calendar, reminders, tools, with approval cards for actions |
| **iPhone** | Ling-3.0-tiny on the phone with MLX | Private chat with no network. No tools. |
| **Cloud** | Any OpenAI-compatible provider, with your own API key | Chat with a larger model. No tools. |

A conversation belongs to one mode (a Mac chat's history lives on the Mac), so
switching modes mid-chat starts a new chat.

## Layout

- `App/`: SwiftUI app (chat, settings, pairing scanner, engines, on-device model copy).
- `Sources/WispPhoneKit/`: platform-neutral logic, unit-tested on the Mac:
  SSE and OpenAI stream decoding, `<think>` filtering, pairing links, gateway and cloud
  clients, conversation storage, phone system prompt.
- `Packages/WispLing/`: Ling's `bailing_hybrid` architecture ported to MLX Swift, plus
  a parity tool against the Python reference. See its README.
- `Sources/WispCore/`, `Sources/WorkflowContractRunner/`: the earlier workflow20 contract
  foundation, unchanged.
- `../service/phone_gateway/`: the Mac side. Protocol: [docs/PHONE_GATEWAY.md](docs/PHONE_GATEWAY.md).

## Set up the Mac side

1. Install Tailscale on the Mac and the iPhone and sign both in to the same tailnet.
2. In the Wisp project folder on the Mac:

   ```sh
   .venv/bin/python -m service.phone_gateway pair --name "My iPhone"
   ```

   Scan the QR code with the iPhone's Camera (it opens Wisp) or from Settings → Mac.
3. Keep the gateway running while you use Mac mode (`--model` also offers Ling to the phone):

   ```sh
   .venv/bin/python -m service.phone_gateway serve --model ~/Desktop/OMLX_Model_Files/Ling-3.0-tiny-Wisp-V3-merged-oQ4e
   ```

The gateway binds only the Mac's Tailscale address, needs a paired token for
everything except `ping`, forwards a fixed set of routes to the backend on
`127.0.0.1:8765`, and never exposes the backend itself. `devices` lists paired
phones; `revoke <id>` removes one.

## Build and run

```sh
python3 ios/scripts/generate_project.py          # after adding/removing Swift files
open ios/WispPhone.xcodeproj
```

To run on your iPhone, create `ios/Config/Local.xcconfig` (git-ignored):

```
DEVELOPMENT_TEAM = ABCDE12345
WISP_BUNDLE_ID = com.yourname.wisp
```

A free Apple ID "Personal Team" works for installing on your own phone. Ling needs
the *Increased Memory Limit* entitlement (`App/WispPhone.entitlements`). If your
team can't sign it, remove that key; Mac and Cloud modes don't need it.

The Simulator runs Mac and Cloud modes. MLX needs a real GPU, so iPhone mode only
works on a device. Simulator builds also accept a `127.0.0.1` pairing link, which
reaches a gateway started with `serve --host 127.0.0.1`. Sign Simulator builds you
run, even ad hoc (`CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM=`):
an unsigned app has no Keychain access, so saving a pairing or key fails with -34018.

## Tests

```sh
swift test --package-path ios                                         # WispPhoneKit + WispCore
.venv/bin/python -m pytest tests/test_phone_gateway.py -q             # Mac gateway
xcodebuild -project ios/WispPhone.xcodeproj -scheme WispPhone -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

## Privacy

- **iPhone mode**: nothing leaves the phone.
- **Mac mode**: traffic stays inside your tailnet (WireGuard-encrypted). The pairing
  token is in the iPhone Keychain (this device only); the Mac stores only its SHA-256.
- **Cloud mode**: prompts and recent chat history go to the provider you chose. The
  API key is in the iPhone Keychain (this device only). Cloud mode has no tools and
  no access to mail, calendar or files.
- Model files copied from the Mac are checked against the Mac's SHA-256 before use,
  are excluded from iCloud backup, and contain no executable code (`.py` is never served).

## Shared workflow20 contract

The earlier foundation's contract runner is unchanged; see
[CAPABILITIES.md](docs/CAPABILITIES.md) and [HANDOFF.md](docs/HANDOFF.md):

```sh
swift run --package-path ios workflow20-ios /path/to/cases/actions.json
```
