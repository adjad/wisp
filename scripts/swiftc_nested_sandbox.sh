# Sourced by the native Swift contract scripts that compile SwiftUI sources.
#
# Recent SDKs implement SwiftUI's @State as a compiler macro, and swiftc runs
# the macro's plugin server under its own sandbox-exec. Inside Simulation QA's
# outer sandbox that nested sandbox cannot be applied (sandbox_apply: Operation
# not permitted), the plugin server dies, and the compile fails with
# "produced malformed response". The outer sandbox already confines the whole
# compile, so ask swiftc not to add a second one. Toolchains without the flag
# need no change, so it is probed rather than assumed.
swiftc_nested_sandbox_flags=()
if swiftc -disable-sandbox -print-target-info >/dev/null 2>&1; then
    swiftc_nested_sandbox_flags=(-disable-sandbox)
fi
