"""Read-only macOS posture probe; stdout is a fixed, sanitized schema."""
import json
import os
import pathlib
import subprocess

LABELS = ("com.wisp.mini.gateway", "com.wisp.mini.node")


def command(argv):
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        return result.stdout.decode() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def jobs_disabled():
    # Inspect explicit domains; SSH's default bootstrap domain is insufficient.
    try:
        for domain in ("gui/", "user/"):
            result = subprocess.run(["/bin/launchctl", "print", domain + str(os.getuid())],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            if result.returncode != 0:
                # An absent GUI login domain has no loaded GUI jobs. Other errors
                # are inconclusive. launchctl uses error 125 for absent domains.
                if domain == "gui/" and result.returncode == 125:
                    continue
                return False
            for label in LABELS:
                service = subprocess.run(["/bin/launchctl", "print", domain + str(os.getuid()) + "/" + label],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if service.returncode == 0:
                    return False
                # launchctl error 113 means the exact service does not exist.
                if service.returncode != 113:
                    return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def funnel_disabled(config):
    if not isinstance(config, dict) or set(config) - {"TCP", "Web", "AllowFunnel", "Foreground", "ETag"}:
        return False
    if any(k in config and not isinstance(config[k], dict) for k in ("TCP", "Web", "AllowFunnel", "Foreground")) or config.get("AllowFunnel"):
        return False
    foreground = config.get("Foreground", {})
    return isinstance(foreground, dict) and all(funnel_disabled(v) for v in foreground.values())


def serve_restricted(config, host=None):
    """Disabled or exactly the two reviewed private HTTPS reverse proxies."""
    if not funnel_disabled(config) or config.get("Foreground"):
        return False
    if not config.get("TCP") and not config.get("Web"):
        return True
    if not isinstance(host, str) or not host.endswith(".ts.net"):
        return False
    tcp = config.get("TCP", {})
    if any(not isinstance(value, dict) or value.get("HTTPS") is not True for value in tcp.values()):
        return False
    return (config.get("TCP") == {"443": {"HTTPS": True}, "8443": {"HTTPS": True}}
            and config.get("Web") == {
                host + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}},
                host + ":8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}})


def probe(tailscale="/opt/homebrew/bin/tailscale", host=None):
    firewall = command(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    listeners = command(["/usr/sbin/lsof", "-nP", "-iTCP:8000", "-sTCP:LISTEN"])
    rows = [line.split() for line in listeners.splitlines()[1:] if line.strip()]
    loopback = bool(rows) and all(len(row) > 8 and row[8] in {"127.0.0.1:8000", "[::1]:8000"} for row in rows)
    try:
        serve = json.loads(command([tailscale, "serve", "status", "--json"]))
    except ValueError:
        serve = None
    daemon = command(["/bin/ps", "-A", "-o", "comm="])
    return {"firewall_enabled": "State = 1" in firewall,
            "omlx_loopback_only": loopback, "serve_restricted": serve_restricted(serve, host), "funnel_disabled": funnel_disabled(serve),
            "ssh_cli_variant": any(pathlib.Path(line.strip()).name == "tailscaled" for line in daemon.splitlines()),
            "jobs_disabled": jobs_disabled()}


if __name__ == "__main__":
    import sys
    print(json.dumps(probe(host=sys.argv[1] if len(sys.argv) == 2 else None), sort_keys=True))
