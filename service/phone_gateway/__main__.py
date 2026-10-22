"""CLI: python -m service.phone_gateway serve|pair|devices|revoke"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from . import DEFAULT_BACKEND, DEFAULT_PORT
from . import qr as qrmod
from . import tailscale
from .store import DeviceStore, StoreError, new_token, state_dir


def _computer_name() -> str:
    try:
        out = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True,
                             text=True, timeout=5, check=False).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    import socket
    return socket.gethostname()


def build_pair_url(*, host: str, ip: str, port: int, token: str, mac: str) -> str:
    query = urlencode([("host", host), ("ip", ip), ("port", str(port)), ("token", token), ("mac", mac)],
                      quote_via=quote)
    return f"wisp-pair://v1?{query}"


def _err(message: str) -> int:
    print(f"phone_gateway: {message}", file=sys.stderr)
    return 2


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    try:
        host = tailscale.resolve_bind_host(args.host)
        app = create_app(state=state_dir(), backend=args.backend,
                         model_dirs=[Path(p) for p in args.model], mac_name=_computer_name())
    except (tailscale.BindError, ValueError, StoreError, OSError) as exc:
        return _err(str(exc))
    config = uvicorn.Config(app, host=host, port=args.port, log_level="warning", access_log=False,
                            proxy_headers=False, server_header=False, ws="none")
    print(f"Wisp phone gateway listening on http://{host}:{args.port} -> {args.backend}", flush=True)
    uvicorn.Server(config).run()
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    ip = tailscale.tailscale_ipv4()
    if ip is None:
        return _err("no Tailscale IPv4 address; start Tailscale before pairing.")
    host = tailscale.magicdns_name() or ip
    try:
        store = DeviceStore(state_dir())
        token = new_token()
        device = store.add(args.name, token)
    except (StoreError, OSError) as exc:
        return _err(str(exc))
    url = build_pair_url(host=host, ip=ip, port=args.port, token=token, mac=_computer_name())
    print(f"Paired '{device['name']}' (id {device['id']}). Scan with the Wisp iPhone app:\n")
    art = None if args.no_qr else qrmod.render_qr(url, color=sys.stdout.isatty())
    if art:
        print(art)
    else:
        print("(QR code unavailable: Swift toolchain not found. Use the link below.)")
    print(f"\n{url}\n")
    print("This link contains the device's secret and is shown only once. "
          f"Revoke it with: python -m service.phone_gateway revoke {device['id']}")
    return 0


def cmd_devices(_args: argparse.Namespace) -> int:
    try:
        devices = DeviceStore(state_dir()).devices()
    except (StoreError, OSError) as exc:
        return _err(str(exc))
    if not devices:
        print("No paired devices.")
        return 0
    print(f"{'ID':<10}{'NAME':<30}{'CREATED':<22}LAST SEEN")
    for d in devices:
        print(f"{d['id']:<10}{str(d.get('name', ''))[:28]:<30}{str(d.get('created_at') or '-'):<22}"
              f"{d.get('last_seen') or 'never'}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    try:
        removed = DeviceStore(state_dir()).revoke(args.id)
    except (StoreError, OSError) as exc:
        return _err(str(exc))
    if not removed:
        print(f"No device with id {args.id!r}.", file=sys.stderr)
        return 1
    print(f"Revoked {args.id}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m service.phone_gateway",
                                     description="Wisp phone gateway (Tailscale-only).")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the gateway on the Tailscale address")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--host", default=None, help="127.0.0.1 or ::1 only (tests/debugging)")
    serve.add_argument("--backend", default=DEFAULT_BACKEND, help="loopback backend URL")
    serve.add_argument("--model", action="append", default=[], metavar="DIR",
                       help="model bundle directory to offer the phone (repeatable)")
    serve.set_defaults(func=cmd_serve)

    pair = sub.add_parser("pair", help="pair a phone and print the link and QR code")
    pair.add_argument("--name", default="iPhone")
    pair.add_argument("--port", type=int, default=DEFAULT_PORT)
    pair.add_argument("--no-qr", action="store_true")
    pair.set_defaults(func=cmd_pair)

    sub.add_parser("devices", help="list paired devices").set_defaults(func=cmd_devices)
    revoke = sub.add_parser("revoke", help="delete a paired device")
    revoke.add_argument("id")
    revoke.set_defaults(func=cmd_revoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
