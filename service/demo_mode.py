"""Run a fresh-install demo of Wisp on this Mac, then put the real Wisp back exactly.

    python3 scripts/wisp_demo.py start [--quit-app] [--launch]
    python3 scripts/wisp_demo.py stop  [--quit-app] [--launch] [--discard]
    python3 scripts/wisp_demo.py status

The notch's /demo command runs this module from the app's bundled Python with
--detach --wait --relaunch: Wisp quits, the detached helper waits for it to be
gone, swaps the homes, and reopens the app.

`start` moves the user's whole ~/.moe aside (one atomic rename) and gives Wisp an
empty ~/.moe, so every cache, Brief/Day card, "Before you ask" card, summary,
memory fact, know-you profile and onboarding step is regenerated from the real
Calendar, Reminders, Mail, Messages, Notes and Contacts, as on a new install. It
also backs up and clears the app's UserDefaults domain, so the setup guide, the
notch tip and the permission prompts run again (macOS keeps the TCC grants).

Two things carry over so the demo works without a re-download or re-setup:
the bundled engine (an APFS clone of ~/.moe/engine: no extra disk, instant, the
original untouched) and the connection settings (config.yaml for the model roster
and engine mode, plus Gmail, Google Workspace and MCP connections).

Why a swap rather than WISP_HOME: the app hard-codes ~/.moe in many places, and a
backend whose home is not ~/.moe is a sandbox the app refuses to act on and that
refuses port 8765. The demo is therefore a real, production-mode Wisp: it reads the
real sources, and sends still always need on-screen confirmation, as normal.

`stop` moves the demo home to ~/.moe-demo-last (or deletes it with --discard),
renames the saved home back to ~/.moe and restores the UserDefaults domain from its
backup. Both commands refuse while Wisp is running, because a live backend or
engine would keep writing to whichever directory it opened.

Standard library only, and never imports the rest of service.*: importing it would
run schema migrations against whatever ~/.moe is at that moment.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DOMAIN = "com.wisp.assistant"
APP_NAME = "Wisp"
PORTS = (8765, 8000)
# Settings and connections, not derived state: a fresh install would get these from
# setup, and without them the demo would start on the wrong model or engine.
CARRY = ("config.yaml", "gmail.json", "google_workspace", "mcp.json")
SAVED = ".moe-demo-saved"
LAST = ".moe-demo-last"
STATE = ".wisp-demo.json"            # in the saved home: what start did
DEMO_MARK = ".wisp-demo-home"        # in the demo home: proves it is ours to move/delete
DEFAULTS_BACKUP = ".wisp-demo-defaults.plist"


class DemoError(Exception):
    pass


class Demo:
    def __init__(self, home: Path, domain: str, app_name: str, ports: tuple[int, ...]):
        self.home = home
        self.live = home / ".moe"
        self.saved = home / SAVED
        self.last = home / LAST
        self.domain = domain
        self.app_name = app_name
        self.ports = ports

    # -- checks -----------------------------------------------------------------

    def active(self) -> bool:
        return (self.saved / STATE).is_file()

    def running(self) -> list[str]:
        """Why Wisp still counts as running, empty when it is safe to move ~/.moe."""
        reasons = []
        if self.app_name and subprocess.run(["pgrep", "-x", self.app_name],
                                            capture_output=True).returncode == 0:
            reasons.append(f"the {self.app_name} app is running")
        for port in self.ports:
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    reasons.append(f"something is listening on port {port}")
        if self.live.is_dir():
            # Top level only: the databases live there, and +D over the engine is slow.
            out = subprocess.run(["lsof", "-Fc", "+d", str(self.live)],
                                 capture_output=True, text=True).stdout
            names = sorted({line[1:] for line in out.splitlines() if line.startswith("c")})
            if names:
                reasons.append("files in ~/.moe are open by: " + ", ".join(names))
        return reasons

    def quit_app(self, timeout: float = 45) -> None:
        subprocess.run(["osascript", "-e", f'tell application "{self.app_name}" to quit'],
                       capture_output=True)
        deadline = time.monotonic() + timeout
        while self.running() and time.monotonic() < deadline:
            time.sleep(1)

    def require_stopped(self, quit_app: bool = False, wait: float = 0) -> None:
        if quit_app and self.running():
            print(f"Quitting {self.app_name}…")
            self.quit_app()
        deadline = time.monotonic() + wait
        while (reasons := self.running()) and time.monotonic() < deadline:
            time.sleep(1)
        if reasons:
            raise DemoError("Quit Wisp first (or pass --quit-app): " + "; ".join(reasons))

    def launch(self, app_path: str | None = None) -> None:
        target = ["open", app_path] if app_path else ["open", "-a", self.app_name]
        subprocess.run(target, check=False)

    def preflight(self, command: str) -> None:
        """What can be refused before Wisp quits, so /demo never quits for nothing."""
        if command == "start":
            if self.active():
                raise DemoError(f"A demo is already running (saved home at {self.saved}). "
                                "Run `stop` first.")
            if self.saved.exists():
                raise DemoError(f"{self.saved} exists but is not a demo backup; "
                                "move it away by hand before starting a demo.")
            if not self.live.is_dir():
                raise DemoError(f"{self.live} does not exist: nothing to demo against.")
        elif not self.active():
            raise DemoError("No demo is running.")

    # -- defaults ---------------------------------------------------------------

    def _defaults(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["defaults", *args], capture_output=True, check=check)

    def export_defaults(self, path: Path) -> None:
        self._defaults("export", self.domain, str(path))
        # defaults(1) can exit 0 without writing (seen in a sandboxed shell); clearing
        # the domain after that would lose the user's settings, so prove the backup.
        try:
            plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException) as error:
            path.unlink(missing_ok=True)
            raise DemoError(f"could not back up the {self.domain} settings: {error}")

    def read_defaults(self) -> dict:
        out = self._defaults("export", self.domain, "-", check=False).stdout
        try:
            return plistlib.loads(out)
        except plistlib.InvalidFileException:
            return {}

    def clear_defaults(self) -> None:
        # Fails harmlessly when the domain is already empty.
        self._defaults("delete", self.domain, check=False)

    def import_defaults(self, path: Path) -> None:
        self.clear_defaults()   # import merges into what is there; start from nothing
        self._defaults("import", self.domain, str(path))
        if self.read_defaults() != plistlib.loads(path.read_bytes()):
            raise DemoError(f"the {self.domain} settings did not restore; the backup is "
                            f"kept at {path} (`defaults import {self.domain} <file>`).")

    # -- commands ---------------------------------------------------------------

    def start(self, *, quit_app: bool = False, wait: float = 0) -> None:
        self.preflight("start")
        self.require_stopped(quit_app, wait)

        stamp = dt.datetime.now().isoformat(timespec="seconds")
        # Both records go inside the real home first, so the rename carries them and
        # there is never a saved home that `stop` cannot recognise.
        state = {"started": stamp, "domain": self.domain, "carried": []}
        try:
            self.export_defaults(self.live / DEFAULTS_BACKUP)
            (self.live / STATE).write_text(json.dumps(state, indent=2))
            self.live.rename(self.saved)        # the one step that moves user data
        except (OSError, subprocess.CalledProcessError):
            for record in (STATE, DEFAULTS_BACKUP):
                (self.live / record).unlink(missing_ok=True)
            raise

        self.live.mkdir(mode=0o755)
        (self.live / DEMO_MARK).write_text(stamp + "\n")
        engine = self.saved / "engine"
        if engine.is_dir():
            subprocess.run(["cp", "-cR", str(engine), str(self.live / "engine")], check=True)
            for stale in ("engine.pid", "engine.log"):
                (self.live / "engine" / stale).unlink(missing_ok=True)
            state["carried"].append("engine (APFS clone)")
        for name in CARRY:
            source = self.saved / name
            if source.is_dir():
                shutil.copytree(source, self.live / name)
            elif source.is_file():
                shutil.copy2(source, self.live / name)
            else:
                continue
            state["carried"].append(name)
        (self.saved / STATE).write_text(json.dumps(state, indent=2))
        self.clear_defaults()

        print(f"Demo started. Your Wisp is saved at {self.saved}.")
        print("Carried over: " + (", ".join(state["carried"]) or "nothing"))

    def stop(self, *, quit_app: bool = False, discard: bool = False, wait: float = 0) -> None:
        self.preflight("stop")
        self.require_stopped(quit_app, wait)
        state = json.loads((self.saved / STATE).read_text())

        if self.live.exists():
            if not (self.live / DEMO_MARK).is_file():
                raise DemoError(f"{self.live} is not the demo home (no {DEMO_MARK}); "
                                "refusing to move it. Restore by hand.")
            if self.last.exists():
                if not (self.last / DEMO_MARK).is_file():
                    raise DemoError(f"{self.last} is not an old demo home; move it away.")
                shutil.rmtree(self.last)
            if discard:
                shutil.rmtree(self.live)
            else:
                self.live.rename(self.last)

        self.saved.rename(self.live)
        (self.live / STATE).unlink()
        backup = self.live / DEFAULTS_BACKUP
        if backup.is_file():
            self.import_defaults(backup)
            backup.unlink()
        else:
            print(f"warning: no UserDefaults backup found; {state['domain']} left as is.")

        print("Demo ended. Your Wisp is restored.")
        if not discard:
            print(f"The demo's state is kept at {self.last} (deleted by the next demo).")

    def status(self) -> None:
        if not self.active():
            print("No demo is running.")
            return
        state = json.loads((self.saved / STATE).read_text())
        print(f"Demo running since {state['started']}; your Wisp is saved at {self.saved}.")
        print("Carried over: " + (", ".join(state.get("carried") or []) or "nothing"))


def _detach(log: Path) -> None:
    """Carry on in a new session so Wisp quitting cannot take this process with it.

    The caller sees this process exit 0 once the preflight has passed."""
    sys.stdout.flush()
    sys.stderr.flush()
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    log.parent.mkdir(parents=True, exist_ok=True)
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    print(f"--- {dt.datetime.now().isoformat(timespec='seconds')} pid {os.getpid()}", flush=True)


def _notify(message: str) -> None:
    script = f"display notification {json.dumps(message)} with title \"Wisp demo\""
    subprocess.run(["osascript", "-e", script], capture_output=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "stop", "status"):
        command = sub.add_parser(name)
        if name == "status":
            continue
        command.add_argument("--quit-app", action="store_true",
                             help="quit Wisp first instead of refusing")
        command.add_argument("--launch", action="store_true", help="open Wisp afterwards")
        command.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                             help="wait this long for Wisp to quit instead of refusing")
        command.add_argument("--detach", action="store_true",
                             help="after the checks, carry on in the background and log "
                                  "to ~/Library/Logs/Wisp/demo.log")
        command.add_argument("--relaunch", metavar="APP", help=argparse.SUPPRESS)
        if name == "stop":
            command.add_argument("--discard", action="store_true",
                                 help="delete the demo's state instead of keeping it")
    # Test hooks: run against a scratch home and a scratch defaults domain.
    parser.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
    parser.add_argument("--domain", default=DOMAIN, help=argparse.SUPPRESS)
    parser.add_argument("--app-name", default=APP_NAME, help=argparse.SUPPRESS)
    parser.add_argument("--ports", default=",".join(map(str, PORTS)), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    ports = tuple(int(p) for p in args.ports.split(",") if p)
    demo = Demo(Path(args.home).expanduser(), args.domain, args.app_name, ports)
    if args.command == "status":
        demo.status()
        return 0
    try:
        demo.preflight(args.command)
    except DemoError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.detach:
        _detach(demo.home / "Library" / "Logs" / "Wisp" / "demo.log")
    status = 0
    try:
        if args.command == "start":
            demo.start(quit_app=args.quit_app, wait=args.wait)
        else:
            demo.stop(quit_app=args.quit_app, discard=args.discard, wait=args.wait)
    except Exception as error:  # noqa: BLE001 - reported, and Wisp still reopens
        print(f"error: {error}", file=sys.stderr)
        if args.detach:
            _notify(f"The demo could not {args.command}: {error}")
        status = 1
    if args.launch or args.relaunch:
        demo.launch(args.relaunch)
    elif status == 0 and args.command == "start":
        print(f"Open {demo.app_name} to begin; run `stop` to restore your Wisp.")
    return status


if __name__ == "__main__":
    sys.exit(main())
