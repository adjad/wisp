# A07 Safari extension staging target

This Xcode target builds `WispSafariExtension.appex` independently of the
SwiftPM host app. It includes the exact merged A01 contracts and A05 public
extractor as copied resources. The manifest has native messaging only: no host
permissions, content script, tab capture, automatic native call or action path.
The background worker imports shared modules to catch packaging drift.

The native handler reads `SFExtensionProfileKey` from Safari's extension
context, never from JavaScript. It returns only a closed denial response. A
private claim is denied explicitly; absent native profile, malformed payloads,
and every otherwise valid request are also denied. Because Safari does not
provide a trusted private-mode attestation through this message, `false` from
JavaScript is never treated as proof of a public page. No profile state is
stored or shared. This stage cannot observe a page or send a bridge frame.

Run `python3 build-support/safari_extension.py` from the repository. It uses
full Xcode, builds the `.appex` in a temporary directory, checks Info.plist,
manifest, copied JS bytes, arm64/minimum-macOS compatibility and system-only
library links, runs synthetic native profile/private cases, then embeds and
ad-hoc signs the extension in a disposable host bundle. It installs nothing.

Operational A07 still requires a native-owned per-profile permission/private
authority, reviewed public catalog acquisition, A04 credential/transport
bootstrap, response delivery and uncertainty tracking, and integration of the
appex into the actual SwiftPM app assembly/signing pipeline. Those changes
need the host-app and release owners. A browser installation or live-page
capture is not qualified by this target.
