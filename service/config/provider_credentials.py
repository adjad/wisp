"""Read origin-bound provider secrets from macOS Keychain without secret argv."""
import hashlib
import subprocess

from . import quarantine

SERVICE = "com.wisp.inference"


def keychain_account(name: str, base_url: str) -> str:
    # A copied credential reference cannot authorize a different destination.
    digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
    return f"{name}:{digest}"


def resolve_keychain(name: str, base_url: str) -> str:
    from .endpoints import EndpointConfigurationError
    quarantine.check()
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", SERVICE,
             "-a", keychain_account(name, base_url), "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
        )
        quarantine.check()
        value = result.stdout.decode("ascii").rstrip("\n")
        if (result.returncode or result.stderr or not value or len(value) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError
        return value
    except (OSError, subprocess.SubprocessError, ValueError):
        raise EndpointConfigurationError("Provider credential unavailable") from None
