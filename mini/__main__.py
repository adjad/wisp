"""Fixed-loopback service entrypoints; provisioning owns Keychain/launchd."""
from __future__ import annotations

import argparse
import os
import sys

from mini.http import credential

ENV_KEYS = ("WISP_LOCAL_OMLX_KEY", "WISP_MINI_INFERENCE_KEY", "WISP_MINI_NODE_KEY")
LOG_CONFIG = {
    "version": 1, "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {name: {"handlers": ["null"], "level": "CRITICAL", "propagate": False}
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")},
}


def application(args):
    from mini.credential_pipe import consume
    keys, _ = consume(args.service)
    present = [key for key in keys.values() if key is not None]
    if len(present) != len(set(present)):
        raise ValueError("Credentials must be distinct")
    if args.service == "gateway":
        from mini.gateway import Gateway
        from mini.resources import ResourceGuard
        resources = None
        config = getattr(args, "resource_contract", None)
        telemetry = getattr(args, "resource_telemetry", None)
        if bool(config) != bool(telemetry):
            raise ValueError("Resource contract and telemetry must be supplied together")
        if config:
            resources = ResourceGuard.from_files(config, telemetry)
            resources.check(preflight=True)
        return Gateway(keys["WISP_MINI_INFERENCE_KEY"], keys["WISP_LOCAL_OMLX_KEY"], resources=resources), 8765
    from mini.node import Node
    from mini.store import Store
    credential(keys["WISP_MINI_NODE_KEY"])
    if not args.state_dir or not args.node_id:
        raise ValueError("Node state directory and identity required")
    return Node(keys["WISP_MINI_NODE_KEY"], Store(args.state_dir, args.node_id)), 8766


def main():
    parser = argparse.ArgumentParser(description="Wisp mini runtime; loopback only, all node jobs disabled")
    parser.add_argument("service", choices=("gateway", "node"), nargs="?", default="gateway")
    parser.add_argument("--resource-contract")
    parser.add_argument("--resource-telemetry")
    parser.add_argument("--state-dir")
    parser.add_argument("--node-id")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        app, port = application(args)
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=port, workers=1, proxy_headers=False,
                    access_log=False, server_header=False, date_header=False,
                    log_config=LOG_CONFIG, timeout_keep_alive=5, timeout_graceful_shutdown=5,
                    h11_max_incomplete_event_size=16_384, http="h11")
    except Exception:
        print("mini-runtime: startup or service failure", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
