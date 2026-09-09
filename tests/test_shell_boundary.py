"""C-1 policy-to-execution regressions using only disposable fixture state."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_scratch = tempfile.TemporaryDirectory(prefix="wisp-shell-boundary-")
os.environ["WISP_HOME"] = _scratch.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.safety import grants, policy  # noqa: E402
from service.tools import builtin  # noqa: E402


class ShellBoundary(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=_scratch.name)))
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.enterContext(patch.object(policy, "_READ_ONLY", True))
        self.enterContext(patch.object(policy, "_FULL_ACCESS", False))
        self.enterContext(patch.object(policy, "_KEEP_FLOOR", True))
        self.enterContext(patch.object(policy, "_CFG", {}))
        self.enterContext(patch.object(grants, "_CACHE", {}))
        self.fixture = self.root / "fixture with spaces.txt"
        self.fixture.write_text("alpha\nbeta\ngamma\n")

    def decision(self, cmd):
        return policy.decide("shell", {"cmd": cmd}, tool="run_shell")

    def marker_command(self, marker):
        code = f"from pathlib import Path; Path({str(marker)!r}).write_text('fixture-only')"
        return shlex.join([sys.executable, "-I", "-c", code])

    def test_auditor_marker_bypasses_cannot_execute(self):
        for case in ("env", "version-chain", "echo-chain", "substitution", "find-exec"):
            with self.subTest(case=case):
                marker = self.root / f"{case}.marker"
                payload = self.marker_command(marker)
                command = {
                    "env": f"env {payload}",
                    "version-chain": f"python3 --version; {payload}",
                    "echo-chain": f"echo fixture; {payload}",
                    "substitution": f"echo $({payload})",
                    "find-exec": f"find {shlex.quote(str(self.fixture))} -exec {payload} \\;",
                }[case]
                decision = self.decision(command)
                if decision.tier is policy.Tier.ALLOW:
                    # On the original source this safely reproduces actual
                    # unauthorized execution, using only this case's marker.
                    builtin.run_shell(command)
                self.assertFalse(marker.exists(), f"view-only policy executed {case}")
                self.assertIs(decision.tier, policy.Tier.DENY)

    def test_shell_grammar_and_unknown_arguments_never_gain_implicit_permission(self):
        commands = [
            "ls; echo hi", "ls && echo hi", "ls || echo hi", "ls | cat", "ls &",
            "pwd\necho hi", "pwd\r", "echo `pwd`", "cat <(echo hi)",
            "echo hi > 'output file'", "echo hi >>output", "cat < input",
            "echo ${USER}", "echo $((1+1))", "ls *.txt", "ls [ab]", "ls ~/Documents",
            "echo {a,b}", "echo hi # comment", "ls \x00", "cat 'unclosed",
            "env python3 -c pass", "python3 --version", "node --version", "pip list",
            "git status", "git diff --output=fixture", "git branch new-branch",
            "git remote add fixture https://example.invalid", "git config --get core.pager",
            "find . -delete", "find . -exec echo hi \\;", "rg --pre=echo fixture",
            "less fixture", "man ls", "file -C", "date 010100002030", "hostname new-name",
            "ls --output=fixture", "ls fixture --output=fixture", "cat --output=fixture",
            "head -n", "head -n nope fixture", "head -n -1 fixture", "tail -f fixture",
            "tail fixture -F", "wc --files0-from=fixture", "uname --output=fixture",
            "whoami fixture", "pwd fixture", "echo\tfixture", "PATH=/tmp ls",
            "command ls", "./ls", "/tmp/ls", "LS", "", "   ", None, 7, ["ls"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIs(self.decision(command).tier, policy.Tier.DENY)

    def test_accepted_reads_use_validated_argv_and_preserve_fixture(self):
        path = shlex.quote(str(self.fixture))
        cases = [
            ("pwd", str(self.root)),
            ("echo fixture", "fixture"),
            (f"ls -lah {shlex.quote(str(self.root))}", self.fixture.name),
            (f"cat {path}", "alpha\nbeta\ngamma"),
            (f"head -n 1 {path}", "alpha"),
            (f"tail -n 1 {path}", "gamma"),
            (f"wc -l {path}", "3"),
            ("uname -s", "Darwin" if sys.platform == "darwin" else "Linux"),
        ]
        for cmd, expected in cases:
            with self.subTest(cmd=cmd):
                decision = self.decision(cmd)
                self.assertIs(decision.tier, policy.Tier.ALLOW)
                with patch.object(builtin.subprocess, "run", wraps=subprocess.run) as run:
                    result = builtin.run_shell(cmd)
                self.assertIn(expected, result)
                self.assertFalse(run.call_args.kwargs["shell"])
                self.assertEqual(tuple(run.call_args.args[0]), decision.shell_argv)
                self.assertTrue(Path(run.call_args.args[0][0]).is_absolute())
                self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(self.fixture.read_text(), "alpha\nbeta\ngamma\n")

    def test_option_terminator_cannot_turn_filenames_into_options(self):
        operand = self.root / "--output=marker"
        operand.write_text("literal filename\n")
        result = builtin.run_shell("cat -- --output=marker")
        self.assertEqual(result, "literal filename")
        self.assertIs(self.decision("cat fixture --output=marker").tier, policy.Tier.DENY)

    def test_empty_operands_are_not_reinterpreted_as_home(self):
        for cmd in ("ls ''", "cat ''", "head -n 1 ''", "wc -- ''"):
            with self.subTest(cmd=cmd), patch.object(builtin.subprocess, "run") as run:
                self.assertIs(self.decision(cmd).tier, policy.Tier.DENY)
                self.assertIn("blocked", builtin.run_shell(cmd).lower())
                run.assert_not_called()

    def test_stdin_is_empty_and_ambiguous_dash_operands_are_refused(self):
        (self.root / "-").write_text("this is a file, not stdin")
        for name in ("cat", "wc"):
            with self.subTest(name=name):
                decision = self.decision(f"{name} -")
                self.assertIs(decision.tier, policy.Tier.ALLOW)
                self.assertEqual(decision.shell_argv[-1], "-")
                result = builtin.run_shell(f"{name} -")
                self.assertNotIn("this is a file", result)
                self.assertNotIn("error", result.lower())
                if name != "wc":
                    self.assertEqual(result, "(exit 0, no output)")
        for name in ("head", "tail"):
            self.assertIs(self.decision(f"{name} -").tier, policy.Tier.DENY)
            self.assertEqual(builtin.run_shell(name), "(exit 0, no output)")
            self.assertEqual(builtin.run_shell(f"{name} ./-"), "this is a file, not stdin")

    def test_direct_execution_rechecks_current_denial(self):
        marker = self.root / "direct.marker"
        for cmd in (f"env {self.marker_command(marker)}", "cat 'unclosed", "ls | cat"):
            with self.subTest(cmd=cmd), patch.object(builtin.subprocess, "run") as run:
                result = builtin.run_shell(cmd)
                self.assertIn("blocked", result.lower())
                run.assert_not_called()
        self.assertFalse(marker.exists())
        policy.set_read_only(False)
        self.assertIs(self.decision(self.marker_command(marker)).tier, policy.Tier.CONFIRM)
        policy.set_read_only(True)
        self.assertIn("blocked", builtin.run_shell(self.marker_command(marker)).lower())
        self.assertFalse(marker.exists())

    def test_path_and_inherited_environment_cannot_replace_safe_execution(self):
        marker = self.root / "environment.marker"
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_ls = fake_bin / "ls"
        fake_ls.write_text("#!/bin/sh\n" + self.marker_command(marker) + "\n")
        fake_ls.chmod(0o700)
        startup = self.root / "startup.sh"
        startup.write_text(self.marker_command(marker) + "\n")
        hostile = {
            "PATH": str(fake_bin), "ENV": str(startup), "BASH_ENV": str(startup),
            "LD_PRELOAD": str(self.root / "fake.so"),
            "DYLD_INSERT_LIBRARIES": str(self.root / "fake.dylib"),
            "DYLD_LIBRARY_PATH": str(fake_bin), "LD_LIBRARY_PATH": str(fake_bin),
            "SHELLOPTS": "xtrace", "GIT_PAGER": str(startup), "PAGER": str(startup),
        }
        with patch.dict(os.environ, hostile), patch.object(
                builtin.subprocess, "run", wraps=subprocess.run) as run:
            result = builtin.run_shell("ls")
        self.assertIn(self.fixture.name, result)
        self.assertFalse(marker.exists())
        self.assertEqual(run.call_args.args[0][0], "/bin/ls")
        environment = run.call_args.kwargs["env"]
        self.assertNotEqual(environment.get("PATH"), str(fake_bin))
        self.assertTrue(all(key not in environment for key in hostile if key != "PATH"))
        self.assertIs(self.decision(shlex.quote(str(fake_ls))).tier, policy.Tier.DENY)

    def test_configuration_can_narrow_but_never_widen_implicit_permission(self):
        with patch.object(policy, "_CFG", {"shell_allow": [".*"], "shell_mutate": []}):
            self.assertIs(self.decision("env python3 -c pass").tier, policy.Tier.DENY)
            self.assertIs(self.decision("git branch new").tier, policy.Tier.DENY)
        with patch.object(policy, "_CFG", {"shell_allow": [r"^pwd$"]}):
            self.assertIs(self.decision("pwd").tier, policy.Tier.ALLOW)
            self.assertIs(self.decision("ls").tier, policy.Tier.DENY)
        with patch.object(policy, "_CFG", {"shell_mutate": [r"^ls$"]}):
            self.assertIs(self.decision("ls").tier, policy.Tier.DENY)

    def test_confirmed_full_access_and_explicit_grants_keep_shell_behavior(self):
        for mode in ("confirmed", "full-access", "grant"):
            with self.subTest(mode=mode):
                marker = self.root / f"{mode}.marker"
                cmd = f"echo fixture; {self.marker_command(marker)}"
                policy.set_read_only(mode == "grant")
                policy.set_full_access(mode == "full-access")
                with patch.object(grants, "check", return_value="allow" if mode == "grant" else None):
                    decision = self.decision(cmd)
                    self.assertIs(decision.tier, policy.Tier.CONFIRM if mode == "confirmed" else policy.Tier.ALLOW)
                    # A confirmed caller invokes the tool only after approval;
                    # the actual command here writes only its disposable marker.
                    with patch.object(builtin.subprocess, "run", wraps=subprocess.run) as run:
                        builtin.run_shell(cmd)
                self.assertTrue(run.call_args.kwargs["shell"])
                self.assertEqual(run.call_args.args[0], cmd)
                self.assertEqual(marker.read_text(), "fixture-only")

    def test_explicit_denial_still_precedes_execution(self):
        for full_access in (False, True):
            with self.subTest(full_access=full_access):
                policy.set_full_access(full_access)
                with patch.object(grants, "check", return_value="deny"), patch.object(
                        builtin.subprocess, "run") as run:
                    self.assertIs(self.decision("pwd").tier, policy.Tier.DENY)
                    self.assertIn("blocked", builtin.run_shell("pwd").lower())
                    run.assert_not_called()

    def test_agent_dispatch_requires_approval_before_unrestricted_execution(self):
        from service.agent import loop

        class FixtureClient:
            async def ensure_only(self, *args, **kwargs):
                pass

            async def stream_events(self, *args, **kwargs):
                yield {"kind": "final", "message": {
                    "role": "assistant", "content": "Fixture result.", "tool_calls": None}}

        for mode in ("view-only", "denied", "approved", "full-access"):
            with self.subTest(mode=mode):
                marker = self.root / f"agent-{mode}.marker"
                cmd = f"env {self.marker_command(marker)}"
                policy.set_read_only(mode == "view-only")
                policy.set_full_access(mode == "full-access")
                approver = type("Approver", (), {"confirm": AsyncMock(return_value=mode == "approved")})()
                events = []

                async def emit(event):
                    events.append(event)

                with patch.object(loop, "audit"):
                    asyncio.run(loop.run_agent(
                        FixtureClient(), "fixture-model", [{"role": "user", "content": "Fixture command"}],
                        emit, approver, tools=["run_shell"], max_steps=1,
                        direct_calls=[("run_shell", {"cmd": cmd})]))
                self.assertEqual(marker.exists(), mode in ("approved", "full-access"))
                self.assertEqual(approver.confirm.await_count, int(mode in ("denied", "approved")))
                call = next(event for event in events if event["type"] == "tool_call")
                self.assertEqual(call["decision"], {
                    "view-only": "deny", "denied": "confirm", "approved": "confirm", "full-access": "allow",
                }[mode])


if __name__ == "__main__":
    unittest.main()
