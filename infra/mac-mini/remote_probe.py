"""Read-only macOS posture probe; stdout is a fixed, sanitized schema."""
import json
import os
import pathlib
import re
import subprocess

if "tcp_listeners" not in globals():
    from socket_posture import tcp_listeners, backend_ports_silent

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



def firewall_inventory(listing, signed, signed_app):
    """Conservatively reject every inbound exception, including auto-allow rules.

    ALF does not expose a reliable dependency map from service to interpreter.
    Therefore an unrelated-looking Python exception cannot be assumed disjoint.
    Unknown/localized/truncated inventory is inconclusive, never an empty list.
    """
    header = re.fullmatch(r"ALF: total number of apps = ([0-9]+)\n?", listing.splitlines(keepends=True)[0] if listing else "")
    if not header:
        return {"schema_version": 1, "complete": False, "exceptions_absent": False}
    count = int(header[1])
    rest = "\n".join(listing.splitlines()[1:]).strip()
    rows = re.findall(r"(?m)^([0-9]+) : (/[^\n]+)\n\s*\( (Allow|Block) incoming connections \)\s*", rest)
    rebuilt = re.sub(r"(?m)^([0-9]+) : (/[^\n]+)\n\s*\( (Allow|Block) incoming connections \)\s*", "", rest)
    complete = (not rebuilt.strip() and len(rows) == count and
                [int(row[0]) for row in rows] == list(range(1, count + 1)) and
                len({row[1] for row in rows}) == count)
    auto_disabled = (signed.strip() == "Automatically allow built-in signed software DISABLED" and
                     signed_app.strip() == "Automatically allow downloaded signed software DISABLED")
    return {"schema_version": 1, "complete": complete,
            "exceptions_absent": complete and auto_disabled and all(row[2] == "Block" for row in rows)}


def account_posture():
    name = command(["/usr/bin/id", "-un"]).strip()
    uid = command(["/usr/bin/id", "-u"]).strip()
    groups = command(["/usr/bin/id", "-G"]).strip()
    valid = bool(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name)) and name != "root"
    member = command(["/usr/bin/dsmemberutil", "checkmembership", "-U", name, "-G", "admin"]).strip() if valid else ""
    return {"schema_version": 1, "name": name if valid else "",
            "uid": int(uid) if re.fullmatch(r"[1-9][0-9]*", uid) else 0,
            "administrator": bool(valid and re.fullmatch(r"[0-9]+(?: [0-9]+)*", groups)
                                  and "80" in groups.split() and member == "user is a member of the group")}

def probe(tailscale="/opt/homebrew/bin/tailscale", host=None):
    firewall = command(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    listeners = tcp_listeners()
    omlx = [address for address, port in listeners if port == 8000] if listeners is not None else []
    loopback = bool(omlx) and all(address in {"127.0.0.1", "::1"} for address in omlx)
    try:
        serve = json.loads(command([tailscale, "serve", "status", "--json"]))
    except ValueError:
        serve = None
    daemon = command(["/bin/ps", "-A", "-o", "comm="])
    inventory = firewall_inventory(command(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--listapps"]),
        command(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getallowsigned"]),
        command(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getallowsignedapp"]))
    return {"schema_version": 2, "account": account_posture(), "firewall_inventory": inventory,
            "firewall_enabled": firewall.strip() == "Firewall is enabled. (State = 1)",
            "omlx_loopback_only": loopback, "serve_restricted": serve_restricted(serve, host), "funnel_disabled": funnel_disabled(serve),
            "ssh_cli_variant": any(pathlib.Path(line.strip()).name == "tailscaled" for line in daemon.splitlines()),
            "jobs_disabled": jobs_disabled(), "backend_ports_silent": listeners is not None and all(port not in (8765, 8766) for _, port in listeners)}


if __name__ == "__main__":
    import sys
    print(json.dumps(probe(host=sys.argv[1] if len(sys.argv) == 2 else None), sort_keys=True))
