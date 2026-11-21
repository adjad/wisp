"""The sandbox-exec profile an Ideate harness runs under.

Unlike run_shell's guard (service/safety/shell_sandbox.py, "allow default" minus Wisp's
folders), this profile starts from "deny default":

- reads: allowed broadly (the harness needs its runtime, the system's libraries and tools),
  except Wisp's data folder (this project excepted), the Keychains folder, Wisp's other
  stores of keys (the engine's settings hold its key) and other agents' config (~/.pi,
  OpenCode's folders). A harness that lives inside one of those (a source run of the
  user's own Pi under ~/.pi/agent/install) gets read access to its own release folder only;
- writes: only this project's work/ and its private agent folder (where TMPDIR, HOME and
  the harness's own state point), plus the usual /dev nodes;
- network: only outbound TCP to the engine's loopback port. No DNS, no other host, never
  Wisp's own service ports (the profile is refused if the engine port is one of them).

Seatbelt matches resolved paths, so every folder is written in both spellings
(shell_sandbox._spellings: as given and as resolved, e.g. /var and /private/var).
Later rules win over earlier ones, so each allow-after-deny is deliberate.
"""
from __future__ import annotations

from pathlib import Path

from service.safety import shell_sandbox
from service.skills.sandbox import SANDBOX_EXEC, _q


class ProfileError(ValueError):
    pass


def usable() -> bool:
    """Whether a sandbox can be applied to a child here (False inside Simulation QA)."""
    return shell_sandbox.usable()


def denied_read_roots() -> list[Path]:
    """Folders the harness may not read. Wisp's protected roots minus the app bundle
    (the bundled harness and Node run from there), plus other keys and agents' config."""
    home = Path.home()
    bundle = shell_sandbox.app_bundle()
    roots = [root for root in shell_sandbox.protected_roots() if root != bundle]
    roots += [home / ".omlx", home / ".pi", home / ".config" / "opencode",
              home / ".local" / "share" / "opencode", home / ".ssh", home / ".aws", home / ".gnupg",
              home / ".netrc", home / ".docker", home / ".kube", home / ".config" / "gh"]
    # The person's own data stores (the folders Wisp's readers use, plus their neighbours).
    # Wisp holds Full Disk Access and a child inherits it, so these are denied by name.
    lib = home / "Library"
    roots += [lib / name for name in ("Mail", "Messages", "Safari", "Group Containers", "Containers",
                                      "Cookies", "Preferences", "Calendars", "Reminders", "Notes",
                                      "Keychains", "Suggestions", "HomeKit", "Accounts")]
    roots += [lib / "Application Support" / name for name in ("AddressBook", "Google", "Firefox",
                                                              "com.apple.sharedfilelist")]
    return list(dict.fromkeys(roots))


def _subpaths(paths) -> list[str]:
    out = []
    for path in paths:
        for spelled in shell_sandbox._spellings(Path(path)):
            out.append(f'(subpath "{_q(spelled)}")')
    return list(dict.fromkeys(out))


def build_profile(project_root: Path, write_roots: list[Path], engine_port: int, *,
                  harness_roots: list[Path] = ()) -> str:
    """The profile text. `write_roots` must lie inside `project_root`."""
    if not isinstance(engine_port, int) or not 1 <= engine_port <= 65535:
        raise ProfileError("engine port is not a port")
    if engine_port in shell_sandbox.wisp_ports():
        raise ProfileError("the engine port is Wisp's own service port")
    project_root = Path(project_root)
    real_root = Path(shell_sandbox._spellings(project_root)[-1])
    for root in write_roots:
        real = Path(shell_sandbox._spellings(Path(root))[-1])
        if real != real_root and real_root not in real.parents:
            raise ProfileError("a writable folder lies outside the project")
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec*)",
        # Only itself: another process's environment (the harness holds the engine key) is
        # readable through process-info, so a child may not look at its parent.
        "(allow process-info* (target self))",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        # No asking other programs to act outside the box: Launch Services (`open`, which
        # can hand a URL to a browser), Apple Events, the pasteboard.
        '(deny mach-lookup (global-name "com.apple.coreservices.launchservicesd")'
        ' (global-name "com.apple.coreservices.appleevents") (global-name "com.apple.pasteboard.1")'
        ' (global-name-prefix "com.apple.lsd."))',
        # Nothing that brokers secrets, the screen, background transfers or privacy grants.
        '(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.nsurlsessiond")'
        ' (global-name "com.apple.tccd") (global-name-prefix "com.apple.securityd")'
        ' (global-name-prefix "com.apple.windowserver") (global-name-prefix "com.apple.screencapture"))',
        "(allow ipc-posix-shm*)",
        "(allow user-preference-read)",
        "(allow file-ioctl)",
        "(allow file-read*)",
    ]
    denied = _subpaths(denied_read_roots())
    if denied:
        lines.append("(deny file-read* " + " ".join(denied) + ")")
    readable = _subpaths([project_root, *harness_roots])
    lines.append("(allow file-read* " + " ".join(readable) + ")")
    # Node resolves its entry point with realpath, which lstat()s every parent folder.
    # A harness inside a hidden folder (~/.pi/agent/install/…) needs the metadata of
    # those parents, by exact path only: their contents stay denied.
    ancestors = []
    for root in [project_root, *harness_roots]:
        for spelled in shell_sandbox._spellings(Path(root)):
            ancestors += [f'(literal "{_q(str(parent))}")' for parent in Path(spelled).parents]
    if ancestors:
        lines.append("(allow file-read-metadata " + " ".join(dict.fromkeys(ancestors)) + ")")
    writable = _subpaths(write_roots)
    devices = ['(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/stdout")',
               '(literal "/dev/stderr")', '(literal "/dev/dtracehelper")', '(literal "/dev/tty")',
               '(regex #"^/dev/fd/")']
    lines.append("(allow file-write* " + " ".join(writable + devices) + ")")
    lines.append(f'(allow network-outbound (remote ip "localhost:{engine_port}"))')
    for port in shell_sandbox.wisp_ports():
        lines.append(f'(deny network-outbound (remote ip "localhost:{port}"))')
    return "\n".join(lines) + "\n"


def contain(argv: list[str], profile_path: Path) -> list[str]:
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]
