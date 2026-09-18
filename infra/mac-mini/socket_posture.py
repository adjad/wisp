"""Read-only kernel socket inventory, independent of process-owner visibility."""
import subprocess


def tcp_listeners():
    try:
        result = subprocess.run(["/usr/sbin/netstat", "-an", "-p", "tcp"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                                env={"PATH": "/usr/bin:/bin:/usr/sbin", "LC_ALL": "C"})
        if result.returncode or result.stderr.strip():
            return None
        lines = result.stdout.decode("ascii").splitlines()
        if (len(lines) < 2 or lines[0] != "Active Internet connections (including servers)"
                or lines[1].split() != ["Proto", "Recv-Q", "Send-Q", "Local", "Address", "Foreign", "Address", "(state)"]):
            return None
        listeners = []
        for line in lines[2:]:
            if not line.strip():
                continue
            row = line.split()
            if (len(row) != 6 or row[0] not in ("tcp4", "tcp6", "tcp46")
                    or not row[1].isdigit() or not row[2].isdigit()):
                return None
            address, separator, port = row[3].rpartition(".")
            if not separator or not address or not (port == "*" or port.isdigit() and 0 <= int(port) <= 65535):
                return None
            if row[5] == "LISTEN":
                if port == "*":
                    return None
                listeners.append((address, int(port)))
        return listeners
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return None


def backend_ports_silent():
    listeners = tcp_listeners()
    return listeners is not None and all(port not in (8765, 8766) for _, port in listeners)
