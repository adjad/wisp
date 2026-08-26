"""Irreversible bulk deletes must always confirm — regression for real data loss.

WHAT HAPPENED (2026-08-16). Asked to "reorganize the wisp debug logs in my
downloads folder", the agent reached a toolset with no way to move files, fell
back to run_shell, created destination folders INSIDE the source folder, and
then ran

    rm -rf ~/Downloads/DebugLogs ~/Downloads/Logs_2026-08-08 ~/Downloads/Logs_2026-08-09

without ever running a single move. Its own reasoning for that step said "then
I'll move files into organized folders". Three weeks of debug logs were
destroyed — `rm -rf` does not go to Trash, and there was no Time Machine
destination, so nothing was recoverable.

Two independent holes, both covered here:
  * `_SHELL_DENY`'s recursive-rm patterns require the target to be `/`, `~` or
    `$HOME`. Ordinary subdirectories — the commoner and more damaging shape —
    matched nothing, and `full_access: true` then made it ALLOW with no card.
  * a standing "always allow run_shell" would have covered every future delete,
    because grants were checked by TOOL NAME with no view of the arguments.

What must keep holding:
  * every recursive/bulk delete shape CONFIRMS, in full-access mode;
  * a targeted single-file `rm` still auto-runs — gating that would make
    full_access pointless, and it is not the shape that caused this;
  * view-only mode still DENIES rather than confirming;
  * no standing grant can pre-approve a destructive command, even though
    run_shell in general stays grantable;
  * the confirmation card hides its "always allow" button for these.

    .venv/bin/python tests/test_destructive_shell.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.safety import grants, policy  # noqa: E402
from service.safety.policy import Tier, decide, is_destructive_shell  # noqa: E402

PASS, FAIL = 0, 0

# The exact command from the incident.
INCIDENT = ("rm -rf ~/Downloads/DebugLogs ~/Downloads/Logs_2026-08-08 "
            "~/Downloads/Logs_2026-08-09")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_the_incident_command_now_confirms() -> None:
    print("\nthe exact command that destroyed the logs now raises a card")
    policy.set_full_access(True)
    policy.set_read_only(False)
    d = decide("shell", {"cmd": INCIDENT}, tool="run_shell")
    check("tier is CONFIRM, not ALLOW", d.tier is Tier.CONFIRM, f"got {d.tier}")
    check("the reason warns it cannot be undone", "cannot be undone" in d.reason)
    check("the reason says it skips the Trash", "Trash" in d.reason)


def test_every_destructive_shape_confirms_under_full_access() -> None:
    print("\nevery recursive/bulk delete shape confirms, even in full-access mode")
    policy.set_full_access(True)
    policy.set_read_only(False)
    for cmd in [
        "rm -rf ~/Downloads/DebugLogs",
        "rm -r ~/x", "rm -R ~/x", "rm -fr ~/x", "rm -f -r ~/x",
        "rm --recursive ~/x",
        "rm ~/Downloads/*.json",
        "rm ~/Downloads/wisp-debug-2026-08-??.txt",
        "find ~/Downloads -name '*.log' -delete",
        "find ~/Downloads -name '*.log' -exec rm {} \\;",
        "rsync -a --delete ~/src/ ~/dst/",
        "git clean -fdx",
    ]:
        d = decide("shell", {"cmd": cmd}, tool="run_shell")
        check(f"{cmd[:44]!r} -> CONFIRM", d.tier is Tier.CONFIRM, f"got {d.tier}")


def test_targeted_deletes_still_auto_run() -> None:
    print("\na targeted single-file rm still auto-runs — full_access stays useful")
    policy.set_full_access(True)
    policy.set_read_only(False)
    for cmd in ["rm ~/Downloads/one-file.txt", "rm -f ~/Downloads/one.txt",
                "ls -la ~/Downloads", "mkdir -p ~/Downloads/Logs",
                "mv ~/a.txt ~/b.txt", "cp ~/a.txt ~/b.txt"]:
        d = decide("shell", {"cmd": cmd}, tool="run_shell")
        check(f"{cmd[:40]!r} -> ALLOW", d.tier is Tier.ALLOW, f"got {d.tier}")


def test_view_only_denies_rather_than_confirms() -> None:
    print("\nview-only mode denies a destructive delete outright")
    policy.set_full_access(False)
    policy.set_read_only(True)
    d = decide("shell", {"cmd": INCIDENT}, tool="run_shell")
    check("tier is DENY", d.tier is Tier.DENY, f"got {d.tier}")
    policy.set_read_only(False)


def test_no_standing_grant_can_pre_approve_it() -> None:
    print("\na standing grant cannot pre-approve a destructive command")
    policy.set_full_access(True)
    policy.set_read_only(False)
    check("run_shell is grantable in general",
          grants.is_grantable("run_shell", {"cmd": "ls -la"}))
    check("…but NOT for a destructive command",
          not grants.is_grantable("run_shell", {"cmd": INCIDENT}))
    res = grants.grant("run_shell", {"cmd": INCIDENT}, decision="allow")
    check("grant() refuses to record it", res.get("ok") is False, f"{res}")
    # Even with a grant somehow present for the tool, decide() must not use it.
    grants.grant("run_shell", {"cmd": "ls -la"}, decision="allow")
    try:
        d = decide("shell", {"cmd": INCIDENT}, tool="run_shell")
        check("a benign grant does not leak into the destructive case",
              d.tier is Tier.CONFIRM, f"got {d.tier}")
    finally:
        grants.revoke("run_shell")


def test_detector_is_exact() -> None:
    print("\nis_destructive_shell names the rule that fired, and stays quiet otherwise")
    check("reports a pattern for the incident", bool(is_destructive_shell(INCIDENT)))
    check("reports nothing for a plain listing", is_destructive_shell("ls -la ~") is None)
    check("reports nothing for a single-file rm",
          is_destructive_shell("rm ~/a.txt") is None)


if __name__ == "__main__":
    test_the_incident_command_now_confirms()
    test_every_destructive_shape_confirms_under_full_access()
    test_targeted_deletes_still_auto_run()
    test_view_only_denies_rather_than_confirms()
    test_no_standing_grant_can_pre_approve_it()
    test_detector_is_exact()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
