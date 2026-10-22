"""Phone gateway: the only door the iPhone app has into Wisp on this Mac.

The Wisp backend stays on 127.0.0.1:8765. This package runs a small separate
process, bound only to the Mac's Tailscale address, that authenticates paired
phones and forwards a fixed set of routes. See ios/docs/PHONE_GATEWAY.md.
"""

GATEWAY_VERSION = 1
DEFAULT_PORT = 8799
DEFAULT_BACKEND = "http://127.0.0.1:8765"
